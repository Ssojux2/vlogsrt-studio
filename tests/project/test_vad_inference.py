"""채널별 연속 상태 Silero 추론의 계약 (P5-02).

추론은 **확률 창만 낸다** — 임계값(`vad_positive`)을 모르고 판단하지 않는다.
이 파일이 증명하는 선:

- 입력열: 한 채널의 호출 입력은 "이전 문맥 64 + 다음 창 512"이고, 돌아온 상태가
  곧 다음 호출의 상태다. 상태·문맥은 채널·자산이 바뀌면 초기화되고 창 사이에서는
  절대 초기화되지 않는다.
- 채널 독립: 반대 위상 스테레오를 합치면 조용해 보이는 사칭이 생긴다 — 채널은
  따로 훑고 두 계열이 각자 산다.
- 꼬리: 마지막 조각 창의 0 덧대기는 추론을 위해서만 있고 `end_sample`은 실제
  프레임 수를 넘지 않는다. 꼬리는 정확히 한 번 나온다.
- 실패 닫힘: NaN·무한·모양·타입·런타임 오류는 자산 **전체**를 `error`로 닫는다 —
  부분 성공이 없다. 저장 PCM의 크기·해시도 세션 앞에서 대조한다.

onnxruntime는 가짜 세션으로 갈아끼운다(P5-01과 같은 선). numpy는 **있으면 진짜를,
없으면 가짜**를 쓴다 — extra 없는 환경에서도 전체 꾸러미 검사가 통과해야 하지만,
진짜 numpy가 있는 환경(isolated)에서는 같은 단언이 실제 ndarray의 dtype·모양·
값을 그대로 증명한다. 단언은 두쪽 다 성립하는 `tolist()`·`module.float32`로만
쓴다 — int16/2^15 정규화는 2의 거듭제곱이라 float32에서 손실이 없다.
"""

from __future__ import annotations

import array
import dataclasses
import hashlib
import os
import subprocess
import sys
import types
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.pcm import PcmReference

CFG = ProjectConfig()
WINDOW = CFG.vad_window_samples
CONTEXT = CFG.vad_context_samples
# 그래프 상태 폭 — 문맥 64의 두 배(양방향)지만 아티팩트의 사실이다.
STATE_WIDTH = 128
FEED_WIDTH = CONTEXT + WINDOW
_FRAME_BYTES = 2  # s16le — 참조 계약의 폭
_SCALE = 32768.0  # int16 → [-1, 1) 정규화 — 2^15

# ─── numpy — 있으면 진짜, 없으면 최소 가짜 ──────────────────────────────


def _shape_of(data: object) -> tuple[int, ...]:
    """중첩 목록에서 모양을 읽는다 — 스칼라은 ()다."""
    dims: list[int] = []
    level = data
    while isinstance(level, list):
        dims.append(len(level))
        if not level:
            break
        level = level[0]
    return tuple(dims)


class _FakeArray:
    """최소 ndarray — 모양·타입·tolist만 흉내낸다. 논리 검사에 충분하다."""

    __slots__ = ("data", "shape", "dtype")

    def __init__(self, data: object, dtype: object) -> None:
        self.data = data
        self.shape = _shape_of(data)
        self.dtype = str(dtype)

    def __getitem__(self, index: int) -> object:
        return self.data[index]

    def tolist(self) -> object:
        return self.data


def _numpy_or_fake(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """진짜 numpy가 있으면 그것을 돌려준다 — 없는 환경에서만 가짜를 깐다.

    진짜가 있는 환경에서는 가짜를 깔지 않는다: 구현의 `import numpy`가 진짜를
    끌어와 모든 단언이 실제 ndarray 위에서 돈다. 가짜는 extra 없는 환경의
    꾸러미 검사를 죽이지 않기 위해서만 있다.
    """
    try:
        import numpy
    except ImportError:
        return _install_fake_numpy(monkeypatch)
    return numpy


def _install_fake_numpy(monkeypatch: pytest.MonkeyPatch) -> types.ModuleType:
    """구현이 쓰는 최소 numpy — zeros·array·asarray와 dtype 이름만 산다."""
    module = types.ModuleType("numpy")
    module.float32 = "float32"  # type: ignore[attr-defined]
    module.int64 = "int64"  # type: ignore[attr-defined]
    module.ndarray = _FakeArray  # type: ignore[attr-defined]

    def zeros(shape: object, dtype: object = None) -> _FakeArray:
        dims = tuple(int(dim) for dim in shape)  # type: ignore[union-attr]
        value = 0.0 if dtype == module.float32 else 0
        return _FakeArray(_nested(dims, value), dtype)

    module.zeros = zeros  # type: ignore[attr-defined]
    module.array = asarray  # type: ignore[attr-defined]
    module.asarray = asarray  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "numpy", module)
    return module


def asarray(data: object, dtype: object = None) -> Any:
    return _FakeArray(data, dtype)


def _nested(dims: tuple[int, ...], value: float) -> object:
    if not dims:
        return value
    return [_nested(dims[1:], value) for _ in range(dims[0])]


# ─── 재료 — 패턴 PCM·참조 ───────────────────────────────────────────────


def _frame_value(frame: int, channel: int) -> int:
    """프레임·채널마다 다른 값 — 남의 프레임이 실리면 바로 드러난다."""
    return ((frame * 7 + channel * 1543) % 28001) - 14000


def _pattern_pcm(frames: int, channels: int, frame_value: Callable[[int, int], int]) -> bytes:
    samples = array.array("h")
    for frame in range(frames):
        for channel in range(channels):
            samples.append(frame_value(frame, channel))
    return samples.tobytes()


def _reference(
    tmp_path: Path,
    frames: int,
    channels: int,
    *,
    frame_value: Callable[[int, int], int] = _frame_value,
    name: str = "stored",
) -> PcmReference:
    """패턴 PCM을 직접 쓰고 참조를 손으로 만든다 — 해시·프레임 수는 진짜."""
    pcm_bytes = _pattern_pcm(frames, channels, frame_value)
    path = tmp_path / f"{name}-{frames}-{channels}.pcm"
    path.write_bytes(pcm_bytes)
    return PcmReference(
        source_id="asset-1",
        original_stem="demo",
        original_sha256="a" * 64,
        path=path,
        pcm_sha256=hashlib.sha256(pcm_bytes).hexdigest(),
        analysis_key="hand-built",
        pcm_sample0_file_time=Fraction(0),
        sample_rate=16000,
        channels=channels,
        sample_count=frames,
    )


def _norm(value: int) -> float:
    return value / _SCALE


def _zeros_state_data() -> list[list[list[float]]]:
    """초기 상태의 내용 — 0으로 채운 (2, 1, 128)."""
    return [[[0.0] * STATE_WIDTH], [[0.0] * STATE_WIDTH]]


# ─── 가짜 세션 — 호출을 기록하고 대본대로 답한다 ────────────────────────


def _probability_output(module: types.ModuleType, value: float, *, dtype: object = None) -> Any:
    return module.asarray([[value]], dtype if dtype is not None else module.float32)


def _indexed_state(module: types.ModuleType, index: int) -> Any:
    """호출순번을 상태에 싣는다 — 다음 호출이 누구의 상태를 받았는지 드러난다."""
    return module.asarray(
        [[[float(index)] * STATE_WIDTH], [[float(index) + 0.5] * STATE_WIDTH]],
        module.float32,
    )


def _echo_responder(module: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
    value = feeds["input"].tolist()[0][CONTEXT]
    return [_probability_output(module, value), _indexed_state(module, index)]


class _RecordingSession:
    """run 호출의 입력·상태·sr을 전부 기록하는 가짜 세션."""

    def __init__(
        self,
        module: types.ModuleType,
        respond: Callable[[types.ModuleType, int, dict[str, Any]], Any] | None = None,
    ) -> None:
        self._module = module
        self._respond = respond or _echo_responder
        self.calls: list[dict[str, Any]] = []

    def run(self, output_names: object, feeds: dict[str, Any]) -> Any:
        index = len(self.calls)
        self.calls.append(dict(feeds))
        return self._respond(self._module, index, feeds)


def _healthy_until(
    window: int, broken: Callable[[types.ModuleType, int, dict[str, Any]], Any]
) -> Callable[[types.ModuleType, int, dict[str, Any]], Any]:
    """앞 창은 정상으로 답하고 `window`부터(만) 망가뜨린다."""

    def respond(module: types.ModuleType, index: int, feeds: dict[str, Any]) -> Any:
        if index < window:
            return _echo_responder(module, index, feeds)
        return broken(module, index, feeds)

    return respond


# ─── 입력열 — 문맥·창·상태의 정확한 순서 ────────────────────────────────


def test_mono_stream_feeds_context_then_windows_in_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """첫 호출은 0 문맥·0 상태, 둘째부터는 앞 창 꼬리와 앞 응답 상태를 받는다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    frames = 1000  # 창 512 + 조각 488
    reference = _reference(tmp_path, frames, 1)
    norm = [_norm(_frame_value(frame, 0)) for frame in range(frames)]

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "success"
    assert result.error is None
    assert result.source_id == "asset-1"
    assert isinstance(result.windows, tuple)
    assert [(w.channel, w.start_sample, w.end_sample) for w in result.windows] == [
        (0, 0, 512),
        (0, 512, 1000),
    ]
    assert len(session.calls) == 2

    first = session.calls[0]
    assert first["input"].shape == (1, FEED_WIDTH)
    assert first["input"].dtype == module.float32
    assert isinstance(first["input"], module.ndarray)
    assert first["input"].tolist()[0][:CONTEXT] == [0.0] * CONTEXT
    assert first["input"].tolist()[0][CONTEXT:] == norm[:512]
    assert first["state"].tolist() == _zeros_state_data()
    assert first["sr"].tolist() == CFG.vad_sample_rate
    assert first["sr"].dtype == module.int64
    assert first["sr"].shape == ()

    second = session.calls[1]
    assert second["input"].tolist()[0][:CONTEXT] == norm[448:512]
    assert second["input"].tolist()[0][CONTEXT : CONTEXT + 488] == norm[512:1000]
    assert second["input"].tolist()[0][CONTEXT + 488 :] == [0.0] * 24  # 덧댄 0 — 추론만
    assert second["state"].tolist() == _indexed_state(module, 0).tolist()

    # 확률은 응답이 준 값 그대로 — 창 첫 실샘플로 돌려준 대본과 같다.
    assert result.windows[0].probability == norm[0]
    assert result.windows[1].probability == norm[512]


def test_int16_extremes_normalize_into_unit_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """32767은 1보다 작게, -32768은 정확히 -1이 된다 — 채널별 float32 정규화."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    extremes = [32767, -32768, 0]
    reference = _reference(
        tmp_path, len(extremes), 1, frame_value=lambda frame, channel: extremes[frame]
    )

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "success"
    assert session.calls[0]["input"].tolist()[0][CONTEXT : CONTEXT + 3] == [
        32767 / _SCALE,
        -1.0,
        0.0,
    ]


# ─── 채널 독립 — 반대 위상 스테레오 ────────────────────────────────────


def _opposed(frame: int, channel: int) -> int:
    value = 1000 + (frame * 13) % 900
    return value if channel == 0 else -value


def test_phase_opposed_stereo_runs_two_independent_series(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """채널 0을 다 돌고 채널 1을 초기 상태·문맥에서 시작한다 — 합치면 사라질 신호도."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    frames = 1024  # 채널마다 정창 2개
    reference = _reference(tmp_path, frames, 2, frame_value=_opposed)
    left = [_norm(_opposed(frame, 0)) for frame in range(frames)]
    right = [_norm(_opposed(frame, 1)) for frame in range(frames)]

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "success"
    assert [(w.channel, w.start_sample, w.end_sample) for w in result.windows] == [
        (0, 0, 512),
        (0, 512, 1024),
        (1, 0, 512),
        (1, 512, 1024),
    ]
    assert len(session.calls) == 4
    # 채널 0: 창 사이 상태·문맥 연속
    assert session.calls[0]["input"].tolist()[0][CONTEXT:] == left[:512]
    assert session.calls[1]["input"].tolist()[0][:CONTEXT] == left[448:512]
    assert session.calls[1]["state"].tolist() == _indexed_state(module, 0).tolist()
    # 채널 1: 초기화된 상태·문맥에서 시작 — 채널이 바뀌는 유일한 리셋점
    assert session.calls[2]["input"].tolist()[0][:CONTEXT] == [0.0] * CONTEXT
    assert session.calls[2]["state"].tolist() == _zeros_state_data()
    assert session.calls[2]["input"].tolist()[0][CONTEXT:] == right[:512]
    assert session.calls[3]["input"].tolist()[0][:CONTEXT] == right[448:512]
    assert session.calls[3]["state"].tolist() == _indexed_state(module, 2).tolist()
    # 두 계열은 독립적이다 — 반대 위상 값이 각자 산다
    assert result.windows[2].probability == -result.windows[0].probability != 0.0


def test_state_resets_between_assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """자산이 바뀌면 상태·문맥이 초기화된다 — 세션을 공유해도 흘러들지 않는다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    first = _reference(tmp_path, 512, 1, name="first")
    second = _reference(tmp_path, 512, 1, name="second")

    first_result = infer_vad(first, "asset-1", CFG, session)
    second_result = infer_vad(second, "asset-2", CFG, session)

    assert first_result.status == "success"
    assert second_result.status == "success"
    assert second_result.source_id == "asset-2"
    assert len(session.calls) == 2
    assert session.calls[1]["input"].tolist()[0][:CONTEXT] == [0.0] * CONTEXT
    assert session.calls[1]["state"].tolist() == _zeros_state_data()


# ─── 꼬리 — 마지막 조각 창의 덧대기 ────────────────────────────────────


def test_short_asset_pads_once_and_times_real_samples_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """창 하나보다 짧은 자산 — 덧댄 0으로 한 번 부르고 end는 실제 프레임 수다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    frames = 100
    reference = _reference(tmp_path, frames, 1)
    norm = [_norm(_frame_value(frame, 0)) for frame in range(frames)]

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "success"
    assert [(w.channel, w.start_sample, w.end_sample) for w in result.windows] == [(0, 0, 100)]
    assert len(session.calls) == 1
    feed = session.calls[0]["input"].tolist()[0]
    assert feed[:CONTEXT] == [0.0] * CONTEXT
    assert feed[CONTEXT : CONTEXT + frames] == norm
    assert feed[CONTEXT + frames :] == [0.0] * (WINDOW - frames)


def test_exact_multiple_emits_full_windows_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프레임 수가 창 폭의 배수면 덧대기가 없다 — 정확히 frames/창 개수의 호출."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    frames = 512
    reference = _reference(tmp_path, frames, 1)
    norm = [_norm(_frame_value(frame, 0)) for frame in range(frames)]

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "success"
    assert [(w.start_sample, w.end_sample) for w in result.windows] == [(0, 512)]
    assert len(session.calls) == 1
    assert session.calls[0]["input"].tolist()[0][CONTEXT:] == norm  # 덧댄 0이 없다


def test_zero_length_asset_is_success_with_no_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """샘플이 없으면 부를 창도 없다 — 성공이고 빈 창열이다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    reference = _reference(tmp_path, 0, 1)

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "success"
    assert result.error is None
    assert result.windows == ()
    assert session.calls == []


# ─── 확률 — 유한하면 하나도 버리지 않는다 ─────────────────────────────


def test_every_finite_probability_is_preserved_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """0.0·1.0 극값도 그대로 실리고, 임계값은 이 층이 모른다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _reference(tmp_path, 1024, 1)
    scripted = [0.0, 1.0]

    def respond(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
        return [_probability_output(npm, scripted[index]), _indexed_state(npm, index)]

    default_session = _RecordingSession(module, respond)
    with_threshold = _RecordingSession(module, respond)

    plain = infer_vad(reference, "asset-1", CFG, default_session)
    drifted = dataclasses.replace(CFG, vad_positive=0.99)

    same = infer_vad(reference, "asset-1", drifted, with_threshold)

    assert plain.status == "success"
    assert [w.probability for w in plain.windows] == scripted
    assert plain.windows == same.windows  # vad_positive가 결과를 바꾸지 않는다


# ─── 실패 닫힘 — 잘못된 출력·런타임 오류는 자산 전체를 닫는다 ───────────


def _error_reference(tmp_path: Path) -> PcmReference:
    return _reference(tmp_path, 1024, 1)  # 정상 창 2개 — 부분 성공의 유혹


def test_nonfinite_probability_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """NaN·무한이 섞이면 그 창까지만이 아니라 자산 전체가 error다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    for bad in (float("nan"), float("inf"), float("-inf")):

        def broken(
            npm: types.ModuleType, index: int, feeds: dict[str, Any], value: float = bad
        ) -> list[Any]:
            return [_probability_output(npm, value), _indexed_state(npm, index)]

        session = _RecordingSession(module, _healthy_until(1, broken))
        result = infer_vad(reference, "asset-1", CFG, session)
        assert result.status == "error", bad
        assert result.windows == ()
        assert "채널 0" in result.error and "창 1" in result.error, bad


def test_wrong_output_shape_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
        return [npm.asarray([[0.5, 0.5]], npm.float32), _indexed_state(npm, index)]

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()
    assert "모양" in result.error


def test_wrong_output_dtype_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
        return [_probability_output(npm, 0.5, dtype="float64"), _indexed_state(npm, index)]

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()
    assert "타입" in result.error


def test_wrong_state_shape_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
        narrow = [[[0.0] * (STATE_WIDTH - 1)], [[0.0] * (STATE_WIDTH - 1)]]
        return [_probability_output(npm, 0.5), npm.asarray(narrow, npm.float32)]

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()
    assert "상태" in result.error


def test_missing_state_output_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
        return [_probability_output(npm, 0.5)]  # stateN이 없다

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()
    assert "출력" in result.error


def test_nonsequence_run_result_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> Any:
        return 0.5  # 배열이 아니라 낱값

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()


def test_output_without_shape_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """모양을 읽을 수 없는 출력도 닫는다 — AttributeError가 새어 나가면 안 된다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> list[Any]:
        return [0.5, _indexed_state(npm, index)]  # .shape가 없는 낱값

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()


def test_session_run_failure_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    reference = _error_reference(tmp_path)

    def broken(npm: types.ModuleType, index: int, feeds: dict[str, Any]) -> Any:
        raise RuntimeError("onnx boom")

    session = _RecordingSession(module, _healthy_until(1, broken))
    result = infer_vad(reference, "asset-1", CFG, session)
    assert result.status == "error"
    assert result.windows == ()
    assert "채널 0" in result.error and "창 1" in result.error
    assert "RuntimeError" in result.error and "onnx boom" in result.error


# ─── 실패 닫힘 — 저장 PCM·참조 계약은 세션 앞에서 닫는다 ────────────────


def test_missing_stored_pcm_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    reference = _error_reference(tmp_path)
    reference.path.unlink()

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "error"
    assert result.windows == ()
    assert result.error is not None and "없다" in result.error
    assert session.calls == []


def test_truncated_stored_pcm_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    reference = _error_reference(tmp_path)
    reference.path.write_bytes(_pattern_pcm(512, 1, _frame_value))  # 기록은 1024프레임 — 실제는 512

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "error"
    assert result.windows == ()
    assert "어긋난다" in result.error
    assert session.calls == []


def test_tampered_stored_pcm_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """길이는 같아도 한 샘플이 바뀌면 해시가 잡는다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    reference = _error_reference(tmp_path)
    tampered = bytearray(reference.path.read_bytes())
    tampered[0] ^= 0xFF
    reference.path.write_bytes(bytes(tampered))

    result = infer_vad(reference, "asset-1", CFG, session)

    assert result.status == "error"
    assert result.windows == ()
    assert "해시" in result.error
    assert session.calls == []


def test_sample_rate_mismatch_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """참조가 16 kHz가 아니면 모델에 sr과 데이터가 어긋난다 — 세션 앞에서 닫는다."""
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    drifted = dataclasses.replace(_error_reference(tmp_path), sample_rate=44100)

    result = infer_vad(drifted, "asset-1", CFG, session)

    assert result.status == "error"
    assert result.windows == ()
    assert "주파수" in result.error
    assert session.calls == []


def test_channelless_reference_fails_whole_asset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.vad.inference import infer_vad

    module = _numpy_or_fake(monkeypatch)
    session = _RecordingSession(module)
    drifted = dataclasses.replace(_error_reference(tmp_path), channels=0)

    result = infer_vad(drifted, "asset-1", CFG, session)

    assert result.status == "error"
    assert result.windows == ()
    assert "채널" in result.error
    assert session.calls == []


# ─── 기록 — 계약 그대로의 얼어 있는 모양 ───────────────────────────────


def test_result_records_are_frozen_with_exact_fields() -> None:
    from vlogsrt.project.vad.inference import VadResult, VadWindow

    window = VadWindow(channel=0, start_sample=0, end_sample=512, probability=0.5)
    assert type(window) is VadWindow
    assert [field.name for field in dataclasses.fields(window)] == [
        "channel",
        "start_sample",
        "end_sample",
        "probability",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        window.channel = 1  # type: ignore[misc]

    result = VadResult(source_id="a", status="success", windows=(window,), error=None)
    assert type(result) is VadResult
    assert [field.name for field in dataclasses.fields(result)] == [
        "source_id",
        "status",
        "windows",
        "error",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.status = "error"  # type: ignore[misc]


# ─── import 위생 ────────────────────────────────────────────────────────


def test_import_does_not_pull_optional_runtimes() -> None:
    """import만으로 numpy·onnxruntime를 끌어오면 안 된다 — extra 밖 환경이 죽는다."""
    root = Path(__file__).resolve().parents[2]
    code = (
        "import sys; import vlogsrt.project.vad.inference; "
        "assert 'onnxruntime' not in sys.modules, 'eager onnxruntime'; "
        "assert 'numpy' not in sys.modules, 'eager numpy'"
    )
    env = {**os.environ, "PYTHONPATH": str(root)}
    subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
