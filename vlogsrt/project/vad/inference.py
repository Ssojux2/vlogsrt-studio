"""채널별 연속 상태 Silero 추론 — 확률 창만 낸다 (P5-02).

P5-01이 고정한 CPU 세션을 받아 저장 PCM(16 kHz s16le)을 **채널마다 따로** 훑고
채널·샘플 좌표가 붙은 확률 창 `VadWindow[]`을 낸다. 이 모듈은 판단하지 않는다 —
임계값(`vad_positive`)을 모르고, 말이냐 침묵이냐를 정하지 않는다. 그 계열의
union과 보호 판단은 P5-03의 몫이다. 지키는 선:

- **int16 → float32 정규화는 채널 안에서만.** 반대 위상 스테레오를 합치면
  조용해 보이는 사칭이 생긴다 — 채널을 섞지 않는다(union은 나중, 다른 층에서).
- **상태는 채널 안에서만 연속이다.** 초기 상태는 0으로 (2,1,128), 문맥은 0으로
  64샘플. 호출 입력은 "이전 문맥 64 + 다음 창 512"이고 돌아온 상태가 곧 다음
  호출의 상태다. 채널과 자산이 바뀌면 둘 다 초기화된다 — 창 사이에서는 절대.
  상태를 쥐는 것은 이 호출의 지역뿐이므로 자산 사이 리셋은 구조가 보증한다.
- **마지막 조각 창의 0 덧대기는 추론을 위해서만.** `end_sample`은 실제 프레임
  수에서만 움직이고 덧대인 샘플에 시각이 붙지 않는다.
- **유한한 확률은 하나도 버리지 않고, 무한·무효 출력·런타임 오류는 자산 전체를
  `error`로 닫는다.** 부분 성공이란 없다 — 메시지는 채널·창 좌표를 말한다.
  돌아온 **상태**의 원소도 하나하나 유한해야 한다(무한 상태는 다음 창에 흐른다).
- **신원은 참조와 정확히 같아야 한다.** 요청 source_id가 참조의 source_id와
  다르면 위조다 — 파일·세션 작업 앞에서 닫는다.

실패는 예외가 아니라 `VadResult(status="error")`로 돌아온다(자산별 런타임 오류는
호출자가 keep+review로 다룐다). numpy import는 함수 안에서만 — 모듈 import이
extra 유무 검사가 되는 일은 없다(runtime과 같은 선).
"""

from __future__ import annotations

import array
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, cast

from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.pcm import PcmReference

# 그래프 이름·모양·출력 수는 silero v6.2.1 계약이다 — P5-01이 세션을 만들 때
# 메타데이터와 대조해 검증했다. 여기는 그 계약을 베끼는 게 아니라 쓰는 곳이다.
_INPUT_NAME = "input"
_STATE_NAME = "state"
_SR_NAME = "sr"
_OUTPUT_COUNT = 2
_OUTPUT_SHAPE = (1, 1)
# 상태 (2, 1, 128) — 마지막 128은 문맥 64의 두 배(양방향 상태)이지만 유도가
# 아니라 아티팩트의 사실이다(runtime의 그래프 계약과 같은 근거).
_STATE_SHAPE = (2, 1, 128)
# s16le 한 샘플의 폭과 int16 → [-1, 1) 정규화 비율 — 포맷의 사실이지 조정 대상이
# 아니다. 2^15로 나누는 것은 2의 거듭제곱이라 float32에서 정확하다.
_SAMPLE_WIDTH = 2
_SAMPLE_SCALE = 32768.0
_READ_BLOCK_BYTES = 1 << 20


class _VadFailure(Exception):
    """안쪽에서 진 상황을 좌표와 함께 올린다 — infer_vad가 error 결과로 바꾼다."""


@dataclass(frozen=True, slots=True)
class VadWindow:
    """확률 창 한 장 — 채널과 `[start, end)` 샘플 좌표가 붙는다."""

    channel: int
    start_sample: int
    end_sample: int
    probability: float


@dataclass(frozen=True, slots=True)
class VadResult:
    """자산 하나의 추론 결과 — 성공이면 창열, 실패면 좌표 있는 error 문구."""

    source_id: str
    status: Literal["success", "error"]
    windows: tuple[VadWindow, ...]
    error: str | None


def infer_vad(
    reference: PcmReference, source_id: str, cfg: ProjectConfig, session: object
) -> VadResult:
    """저장 PCM 전체를 채널별 연속 상태로 훑어 확률 창을 낸다.

    어떤 실패도 자산 전체를 `error`로 닫는다 — 창 일부만 실은 부분 성공은
    없다. 새어 나가는 예상 밖의 예외까지 결과 안으로 접는 것은 실패 닫힘의
    보호막이다(BaseException은 잡지 않는다).
    """
    try:
        windows = _infer_reference(reference, source_id, cfg, session)
    except _VadFailure as failure:
        return VadResult(source_id=source_id, status="error", windows=(), error=str(failure))
    except Exception as exc:
        return VadResult(
            source_id=source_id,
            status="error",
            windows=(),
            error=f"VAD 추론이 예상 밖의 오류로 실패했다 — {type(exc).__name__}: {exc}",
        )
    return VadResult(source_id=source_id, status="success", windows=windows, error=None)


def _infer_reference(
    reference: PcmReference, source_id: str, cfg: ProjectConfig, session: object
) -> tuple[VadWindow, ...]:
    """자산 전체 — 신원·참조 검증 → 저장 PCM 검증 → 채널마다 처음부터 끝까지."""
    _require_identity(source_id, reference)
    _require_compatible_reference(reference, cfg)
    _verify_stored_pcm(reference)
    windows: list[VadWindow] = []
    for channel in range(reference.channels):
        windows.extend(_infer_channel(reference, cfg, session, channel))
    return tuple(windows)


def _infer_channel(
    reference: PcmReference, cfg: ProjectConfig, session: object, channel: int
) -> list[VadWindow]:
    """한 채널을 처음부터 끝까지 — 상태·문맥은 이 루프의 지역에서만 산다.

    채널이 바뀌면 이 함수가 새로 불려 0 상태·0 문맥에서 시작한다. 창 사이에서는
    돌아온 상태와 창의 마지막 64샘플이 그대로 다음 호출로 흘러간다.
    """
    import numpy

    frame_bytes = _SAMPLE_WIDTH * reference.channels
    state = numpy.zeros(_STATE_SHAPE, dtype=numpy.float32)
    context: list[float] = [0.0] * cfg.vad_context_samples
    sample_rate = numpy.array(cfg.vad_sample_rate, dtype=numpy.int64)
    windows: list[VadWindow] = []
    with reference.path.open("rb") as handle:
        starts = range(0, reference.sample_count, cfg.vad_window_samples)
        for index, start in enumerate(starts):
            count = min(cfg.vad_window_samples, reference.sample_count - start)
            block = _read_window_bytes(handle, start, count, frame_bytes)
            padded = _normalized_window(block, reference.channels, channel, count, cfg)
            probability, state = _run_window(
                session, context, padded, state, sample_rate, channel, index
            )
            windows.append(
                VadWindow(
                    channel=channel,
                    start_sample=start,
                    end_sample=start + count,
                    probability=probability,
                )
            )
            context = padded[-cfg.vad_context_samples :]
    return windows


def _run_window(
    session: object,
    context: list[float],
    padded: list[float],
    state: Any,
    sample_rate: Any,
    channel: int,
    index: int,
) -> tuple[float, Any]:
    """이전 문맥 + (덧대기 끝난) 창을 한 번 부르고 출력을 검증해 돌려준다."""
    import numpy

    feed = numpy.asarray([context + padded], dtype=numpy.float32)
    runner = cast(Any, session)
    try:
        outputs = runner.run(None, {_INPUT_NAME: feed, _STATE_NAME: state, _SR_NAME: sample_rate})
    except Exception as exc:
        raise _VadFailure(
            f"VAD 세션 run이 실패했다 — 채널 {channel} 창 {index}: {type(exc).__name__}: {exc}"
        ) from exc
    return _validate_outputs(outputs, channel, index)


def _validate_outputs(outputs: object, channel: int, index: int) -> tuple[float, Any]:
    """출력 둘 — 확률은 (1,1) float32 유한값, 상태는 다음 호출에 실을 (2,1,128)."""
    if not isinstance(outputs, (list, tuple)) or len(outputs) != _OUTPUT_COUNT:
        raise _VadFailure(
            f"VAD 출력 목록이 계약과 다르다 — 채널 {channel} 창 {index}: "
            f"{type(outputs).__name__} 항목 {len(outputs) if hasattr(outputs, '__len__') else '?'}"
        )
    try:
        probability = _window_probability(outputs[0], channel, index)
        state = _feedable_state(outputs[1], channel, index)
    except _VadFailure:
        raise
    except Exception as exc:
        raise _VadFailure(
            f"VAD 출력에서 모양·타입을 못 읽었다 — 채널 {channel} 창 {index}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not math.isfinite(probability):
        raise _VadFailure(f"VAD 확률이 유한하지 않다 — 채널 {channel} 창 {index}: {probability!r}")
    return probability, state


def _window_probability(output: Any, channel: int, index: int) -> float:
    """첫 출력을 검증하고 스칼라 확률로 편다 — 유한성은 호출자가 본다."""
    if tuple(output.shape) != _OUTPUT_SHAPE:
        raise _VadFailure(
            f"VAD 출력 모양이 계약과 다르다 — 채널 {channel} 창 {index}: "
            f"기대 {_OUTPUT_SHAPE}, 실제 {output.shape}"
        )
    if str(output.dtype) != "float32":
        raise _VadFailure(
            f"VAD 출력 타입이 계약과 다르다 — 채널 {channel} 창 {index}: "
            f"기대 float32, 실제 {output.dtype}"
        )
    return float(output[0][0])


def _feedable_state(state: Any, channel: int, index: int) -> Any:
    """둘째 출력은 그대로 다음 호출에 실린다 — 모양·타입·유한성을 여기서 증명한다.

    상태의 원소 하나라도 NaN·무한이면 다음 창부터 확률이 쓰레기로 흐른다 —
    그 창에서 자산째 닫는다(마지막 창의 상태는 안 쓰이지만 같은 잣대로 본다).
    """
    if tuple(state.shape) != _STATE_SHAPE:
        raise _VadFailure(
            f"VAD 상태 모양이 계약과 다르다 — 채널 {channel} 창 {index}: "
            f"기대 {_STATE_SHAPE}, 실제 {state.shape}"
        )
    if str(state.dtype) != "float32":
        raise _VadFailure(
            f"VAD 상태 타입이 계약과 다르다 — 채널 {channel} 창 {index}: "
            f"기대 float32, 실제 {state.dtype}"
        )
    bad = _first_nonfinite(state.tolist())
    if bad is not None:
        raise _VadFailure(
            f"VAD 상태에 유한하지 않은 원소가 있다 — 채널 {channel} 창 {index}: {bad!r}"
        )
    return state


def _first_nonfinite(values: Any) -> float | None:
    """중첩 목록을 펴서 처음 나오는 NaN·무한값 — 전부 유한하면 None."""
    if isinstance(values, list):
        for value in values:
            found = _first_nonfinite(value)
            if found is not None:
                return found
        return None
    number = float(values)
    return None if math.isfinite(number) else number


def _normalized_window(
    block: bytes, channels: int, channel: int, count: int, cfg: ProjectConfig
) -> list[float]:
    """교차 int16에서 한 채널의 창을 [-1, 1)로 — 남는 폭은 0으로 덧댄다.

    덧대기는 세션이 고정 폭 입력을 받도록 하기 위해서만 있고 `count`는 실제
    프레임 수만 안다 — 창 좌표를 만들 때 이 함수의 0은 세지 않는다.
    """
    interleaved = array.array("h")
    interleaved.frombytes(block)
    values = [sample / _SAMPLE_SCALE for sample in interleaved[channel::channels]]
    return values + [0.0] * (cfg.vad_window_samples - count)


def _read_window_bytes(handle: BinaryIO, start: int, count: int, frame_bytes: int) -> bytes:
    """저장 PCM에서 이 창의 교차 바이트를 정확히 읽는다 — 모자라면 닫는다."""
    try:
        handle.seek(start * frame_bytes)
        block = handle.read(count * frame_bytes)
    except OSError as exc:
        raise _VadFailure(
            f"저장 PCM 읽기가 실패했다 — {start}프레임부터 {count}프레임 ({exc})"
        ) from exc
    if len(block) != count * frame_bytes:
        raise _VadFailure(
            f"저장 PCM이 요청만큼 주지 못했다 — {start}프레임부터 {count}프레임 "
            f"({len(block)}바이트밖에 없다)"
        )
    return block


def _verify_stored_pcm(reference: PcmReference) -> None:
    """세션을 부르기 전에 저장 PCM 전체를 참조와 대조한다 — 크기만이 아니라 해시까지."""
    frame_bytes = _SAMPLE_WIDTH * reference.channels
    expected = reference.sample_count * frame_bytes
    if not reference.path.is_file():
        raise _VadFailure(f"저장 PCM이 없다: {reference.path}")
    try:
        actual = reference.path.stat().st_size
    except OSError as exc:
        raise _VadFailure(f"저장 PCM의 상태를 읽지 못했다: {reference.path} ({exc})") from exc
    if actual != expected:
        raise _VadFailure(
            f"저장 PCM이 참조와 어긋난다: {reference.path} "
            f"(기록 {reference.sample_count}프레임 × {frame_bytes}바이트 "
            f"= {expected}, 실제 {actual})"
        )
    if _sha256_stored(reference.path) != reference.pcm_sha256:
        raise _VadFailure(f"저장 PCM 해시가 참조와 어긋난다: {reference.path}")


def _sha256_stored(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(_READ_BLOCK_BYTES), b""):
                digest.update(block)
    except OSError as exc:
        raise _VadFailure(f"저장 PCM을 해시하지 못했다: {path} ({exc})") from exc
    return digest.hexdigest()


def _require_identity(source_id: str, reference: PcmReference) -> None:
    """요청 신원은 비지 않은 문자열이고 참조의 신원과 정확히 같아야 한다.

    참조의 PCM으로 다른 source_id의 결과를 내는 것은 위조다 — 파일·세션 작업
    앞에서 닫고 메시지가 두 신원을 다 말한다.
    """
    if not isinstance(source_id, str) or not source_id.strip():
        raise _VadFailure(f"source_id가 비지 않은 문자열이 아니다: {source_id!r}")
    if source_id != reference.source_id:
        raise _VadFailure(
            f"요청 source_id {source_id!r}가 참조의 신원 {reference.source_id!r}과 다르다 — "
            "위조 신원으로는 추론하지 않는다"
        )


def _require_compatible_reference(reference: PcmReference, cfg: ProjectConfig) -> None:
    """세션 앞에 참조 계약을 본다 — 주파수가 어긋나면 sr과 데이터가 갈라진다."""
    if reference.channels < 1:
        raise _VadFailure(f"참조 채널이 {reference.channels}이다 — VAD는 채널마다 따로 돈다")
    if reference.sample_count < 0:
        raise _VadFailure(f"참조 프레임 수가 음수다: {reference.sample_count}")
    if reference.sample_rate != cfg.vad_sample_rate:
        raise _VadFailure(
            f"참조 주파수가 VAD 계약과 다르다 — 참조 {reference.sample_rate}Hz, "
            f"설정 {cfg.vad_sample_rate}Hz"
        )
