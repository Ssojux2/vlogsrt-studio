"""원본 PCM 참조 — 저장하고 정확히 자르고 자를 자리를 고른다 (P2-02).

P2-01이 잰 시계(`pcm_sample0_file_time`, 변환 후 첫 프레임의 실측 pts)를 그대로
믿고, 16 kHz s16le PCM을 **내용으로 주소를 매기는** 불변 참조로 저장한다. 참조의
키는 원본 해시·오디오 정책·원점·PCM 해시·채널·주파수다 — 경로·이름·source_id는
키 재료가 아니다. 원본 파일은 통째로 해시해 주어진 정체성과 같은지 직접 확인한다.

**완성물은 세대로 산다.** `destination`은 논리 캐시 주소이고 그 자리에는 포인터가
하나 살 뿐이다. 참조를 만들면 **새 세대** PCM과 세대 기록을 남이 쓰지 않는 새
이름에 쓰고, 짝을 완전히 검증한 뒤 포인터를 원자적으로 갈아끼운다 — 이 갈아끼움이
유일한 출판이다. 어떤 세대도 지우거나 덮거나 이름을 바꾸지 않으므로, 출판이
실패하거나 프로세스가 포인터 앞에서 죽어도 손에 쥔 옛 참조는 그대로 읽히고
슬라이스도 재추출 없이 된다. 성공한 갱신도 옛 세대를 남긴다. `PcmReference.path`는
포인터가 가리키는 **불변 세대 PCM 그 자체**다 — 논리 주소가 아니다.

슬라이스는 저장된 그 PCM에서 `[start, end)` 프레임을 **바이트 그대로** 옮긴다.
시작은 ceil, 끝은 floor — 제거된 오디오의 덧붙임도 문맥도 없고 남은 오디오가
줄어들 일도 없다. 저장 PCM을 읽는 소비자(슬라이스·자를 자리)는 크기만이 아니라
해시까지 참조와 대조한다. 자를 자리(`quiet_sample`)는 창의 진폭만 잴 뿐
말/침묵을 판정하지 않는다. 시각은 전부 정확한 `Fraction`이며 float는 들어오지
못한다.

남긴 구간 한 덩어리(`split_retained_ranges`)도 같은 시계로 자른다(P2-03). 전체·
중간·마지막·재전사 구간을 가리지 않고 조각의 원점은 언제나
`Fraction(시작 프레임, 주파수)`다 — 인코더가 세그먼트 목록으로 알려주던 좌표는
이 길에 없다. 합성 재현에서 그 레거시 좌표의 어긋남은 확인했지만, 역사적
77분 회차의 원인·규모와 중간/마지막 차이는 재현되지 않았다(PENDING_HUMAN) —
이 모듈이 말하는 건 남긴 PCM 구간 좌표의 정확함뿐이다.

참조는 오디오가 **선언된** 자원에만 존재한다. 프로브·추출이 실패하면 경로·해시·
프레임 수·원점을 지어내지 않고 문마 있는 오류로 죽는다 — unavailable 타입은
P3가 소유하고 P7이 만든다. ffmpeg 경계는 P2-01(`vlogsrt.preprocess`)이고 이
모듈은 subprocess를 모른다.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import tempfile
import wave
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vlogsrt import log
from vlogsrt.preprocess import extract_pcm_s16le

_CONTRACT_SAMPLE_RATE = 16000
"""이 단계의 계약 주파수(P2-01이 강제하는 값). 원본은 무엇이든 여기로 리샘플된다."""

_CONTRACT_SAMPLE_WIDTH = 2
"""s16le 한 샘플의 바이트 폭. 포맷의 사실이지 조정 대상이 아니다."""

_KEY_DOMAIN = "vlogsrt-pcm-reference-v1"
"""분석 키 재료의 언어 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_POINTER_FORMAT = "vlogsrt-pcm-pointer-v1"
_GENERATION_FORMAT = "vlogsrt-pcm-generation-v1"
_POINTER_KEYS = frozenset({"format", "generation_pcm", "generation_metadata"})
_GENERATION_KEYS = frozenset(
    {
        "format",
        "source_id",
        "original_stem",
        "original_sha256",
        "stream_index",
        "sample_rate",
        "sample_width",
        "pcm_sha256",
        "pcm_sample0_file_time",
        "channels",
        "sample_count",
    }
)

_READ_BLOCK_BYTES = 1 << 20  # 해시·복사 때 읽는 덩어리 — 입출력 단위다


@dataclass(frozen=True, slots=True)
class AudioPolicy:
    """어떤 오디오를 어떤 형태로 저장할지.

    `stream_index`는 **오디오 순번**(0이면 첫 오디오)이다 — 전역 스트림 번호가
    아니다. 전역 번호는 P2-01이 ffprobe로 얻어 매핑에 쓰고, 참조에는 남기지
    않는다: 추출 증명(분석 키)이 정책을 묶어 두기 때문이다. 정책은 **만드는
    순간**에 온전함을 검사한다 — 잘못된 정책이 든 참조가 아예 생기지 않게.
    """

    stream_index: int = 0
    sample_rate: int = 16000
    sample_width: int = 2

    def __post_init__(self) -> None:
        if _bad_count(self.stream_index) or self.stream_index < 0:
            raise ValueError(f"오디오 순번은 0 이상 정수여야 한다: {self.stream_index!r}")
        if _bad_count(self.sample_rate) or self.sample_rate != _CONTRACT_SAMPLE_RATE:
            raise ValueError(
                f"저장 PCM의 주파수는 {_CONTRACT_SAMPLE_RATE}Hz가 아니면 안 된다: "
                f"{self.sample_rate!r}"
            )
        if _bad_count(self.sample_width) or self.sample_width != _CONTRACT_SAMPLE_WIDTH:
            raise ValueError(
                f"저장 PCM은 s16le다 — 폭은 {_CONTRACT_SAMPLE_WIDTH}바이트여야 한다: "
                f"{self.sample_width!r}"
            )


@dataclass(frozen=True, slots=True)
class PcmReference:
    """저장된 원본 PCM과 그 시계의 원점.

    `path`는 포인터가 가리키는 **불변 세대 PCM**이다 — 논리 주소가 아니다. 그래서
    같은 주소에서 포인터가 바뀌어도 손에 쥔 참조는 그대로 읽힌다.
    `pcm_sample0_file_time`은 P2-01이 잰 디코드 원점이다 — ffprobe의
    `stream.start_time`과 다르고, 그 차이(AAC/MKV에서 64ms)가 자막 전체에
    실린다. `sample_count`는 채널별 프레임 수다.
    """

    source_id: str
    original_stem: str
    original_sha256: str
    path: Path
    pcm_sha256: str
    analysis_key: str
    pcm_sample0_file_time: Fraction
    sample_rate: int
    channels: int
    sample_count: int


@dataclass(frozen=True, slots=True)
class SampleRange:
    """`[start, end)` — 채널별 프레임 번호."""

    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Stored:
    """세대 기록에서 읽은 완성물의 사실 — 재사용 판정에만 쓴다."""

    source_id: str
    original_stem: str
    original_sha256: str
    policy: AudioPolicy
    pcm_sha256: str
    origin: Fraction
    channels: int
    sample_count: int


def build_reference(
    source_id: str,
    original_stem: str,
    original: Path,
    original_sha256: str,
    policy: AudioPolicy,
    destination: Path,
) -> PcmReference:
    """원본에서 PCM 참조를 만들어 논리 주소 `destination`에 출판한다.

    원본은 통째로 해시해 `original_sha256`과 같은지 먼저 확인한다 — 재사용이든
    추출이든 그 앞에서. 완성 세대는 포인터가 가리키는 불변 짝이 **전부** 맞을
    때만 다시 쓴다: 세대 기록의 정체성·정책·측정값이 이번 호출과 같고, PCM
    크기가 프레임 수와 맞고, 파일 해시까지 기록과 일치한다. 하나라도 어긋나면
    새 세대를 만들어 포인터를 옮긴다. 원본 미디어를 덮어쓰는 경로는 시작 전에
    거부한다.
    """
    _require_text(source_id, "source_id")
    _require_text(original_stem, "original_stem")
    _require_text(original_sha256, "original_sha256")
    _require_distinct_paths(original, destination)
    _require_original_identity(original, original_sha256)
    reusable = _reusable_reference(destination, source_id, original_stem, original_sha256, policy)
    if reusable is not None:
        return reusable
    return _extract_reference(
        source_id, original_stem, original, original_sha256, policy, destination
    )


def retained_sample_range(reference: PcmReference, start: Fraction, end: Fraction) -> SampleRange:
    """초 단위 구간을 저장 프레임의 반개 구간으로 — 시작은 ceil, 끝은 floor.

    환산은 **안쪽으로만** 간다: 남은 오디오 이상을 남기지 않는다. 양 끝은 실제
    프레임 수에 묶는다. 환산 뒤 `end <= start`면 남는 프레임이 없다는 뜻이다 —
    tolerance 샘플을 더해 살리지 않는다.
    """
    _require_exact_seconds(start, "start")
    _require_exact_seconds(end, "end")
    rate = reference.sample_rate
    first = min(reference.sample_count, max(0, math.ceil(start * rate)))
    last = min(reference.sample_count, max(0, math.floor(end * rate)))
    if last <= first:
        raise ValueError(
            f"환산 뒤 구간이 비었다: start {start}초 → {first}프레임, "
            f"end {end}초 → {last}프레임 (저장 {reference.sample_count}프레임)"
        )
    return SampleRange(start=first, end=last)


def write_wav_slice(reference: PcmReference, samples: SampleRange, destination: Path) -> Path:
    """저장 PCM의 `[start, end)` 프레임을 보통의 WAV 헤더만 붙여 옮긴다.

    데이터는 저장된 바이트 그대로다 — 다시 인코딩하지 않고, 채널 인터리브도
    그대로다. 제거된 오디오의 덧붙임이나 문맥은 없다. 세대 PCM 자신의 경로로는
    못 쓴다 — 불변 세대를 슬라이스로 갈아끼우는 일이 없게.
    """
    _require_slice(reference, samples)
    if destination.resolve() == reference.path.resolve():
        raise ValueError(f"슬라이스가 저장 PCM 자리로는 못 간다: {destination} == {reference.path}")
    data = _read_stored_bytes(reference, samples.start, samples.end - samples.start)
    temp = _begin_atomic(destination)
    try:
        with wave.open(str(temp), "wb") as handle:
            handle.setnchannels(reference.channels)
            handle.setsampwidth(_CONTRACT_SAMPLE_WIDTH)
            handle.setframerate(reference.sample_rate)
            handle.writeframes(data)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)
    return destination


def split_retained_ranges(
    reference: PcmReference,
    ranges: Sequence[tuple[Fraction, Fraction]],
    directory: Path,
) -> tuple[tuple[SampleRange, Path], ...]:
    """남긴 구간들을 같은 저장 PCM에서 잘라 WAV로 낸다 — 좌표는 샘플 번호뿐이다.

    1차 전체 구간이든 중간·마지막 구간이든 재전사 부분구간이든, 조각의 시각 원점은
    언제나 `Fraction(시작 프레임, 주파수)`(+ 참조의 원점)이다 — 인코더가 세그먼트
    목록으로 알려주던 좌표는 이 길에 없다. 그 좌표를 믿으면 경계마다 프레임 단위로
    밀리는 조각이 나온다는 것은 합성 재현으로 확인했다(역사적 77분 회차의 원인·
    규모와 중간/마지막 차이는 재현되지 않았다 — PENDING_HUMAN). 구간은 안쪽 환산
    (시작 ceil·끝 floor) 뒤 순서와 비겹침을 무엇을 쓰기도 전에 확인한다 —
    **잘못된 범위 입력으로는** 아무 조각도 못 나간다. 조각 하나하나는 따로 원자적으로
    나간다: 뒤쪽 조각의 쓰기가 죽으면 앞쪽 조각은 이미 디스크에 있다(묶음 거래가
    아니다). 마지막 구간은 저장 PCM의 실제 마지막 프레임에서 끝날 수 있다.
    """
    spans: list[SampleRange] = []
    for position, pair in enumerate(ranges):
        span = _retained_range(reference, pair, position)
        if spans and span.start < spans[-1].end:
            raise ValueError(
                f"남긴 구간이 겹치거나 순서가 어긋난다: {position}번째 구간이 "
                f"[{span.start}, {span.end})인데 앞 구간이 {spans[-1].end}까지 간다"
            )
        spans.append(span)
    return tuple(
        (span, write_wav_slice(reference, span, _retained_path(directory, index)))
        for index, span in enumerate(spans)
    )


def quiet_sample(
    reference: PcmReference, target: int, search_samples: int, window_samples: int
) -> int:
    """목표 주위의 묶인 창 안에서 가장 조용한 창의 시작 프레임을 고른다.

    채널은 **따로** 잰다 — 반대 위상이 합쳐져 조용해 보이는 것을 피한다. 순위는
    (창의 최대 절대 진폭, 목표까지 거리, 앞쪽 위치)다. 자를 자리를 고를 뿐:
    말인지 침묵인지 판정하지 않고, 조용한 곳이 없어도 목표에서 잘린다.
    """
    _require_frame("target", target, reference.sample_count)
    if _bad_count(search_samples) or search_samples < 0:
        raise ValueError(f"탐색 폭(search_samples)은 0 이상 정수여야 한다: {search_samples!r}")
    if _bad_count(window_samples) or window_samples < 1:
        raise ValueError(f"잰 창(window_samples)은 1 이상 정수여야 한다: {window_samples!r}")
    if window_samples > reference.sample_count:
        raise ValueError(
            f"잰 창이 저장된 프레임보다 길다: 창 {window_samples}, 저장 {reference.sample_count}"
        )
    lo = max(0, target - search_samples)
    hi = min(target + search_samples, reference.sample_count - window_samples)
    if lo > hi:
        raise ValueError(
            f"탐색 범위가 비었다: 목표 {target}, 탐색 {search_samples}, "
            f"창 {window_samples}, 저장 {reference.sample_count}프레임"
        )
    samples = _read_stored_bytes(reference, lo, hi - lo + window_samples)
    interleaved = array.array("h")
    interleaved.frombytes(samples)
    per_channel = [
        interleaved[channel :: reference.channels] for channel in range(reference.channels)
    ]
    best = min(
        (
            max(_window_abs_peak(values, offset, window_samples) for values in per_channel),
            abs(lo + offset - target),
            lo + offset,
        )
        for offset in range(hi - lo + 1)
    )
    return best[2]


def _sample_file_time(reference: PcmReference, sample_index: int) -> Fraction:
    """`file_time = sample_index/sample_rate + 원점` — 정확한 유리수 한 줄."""
    _require_frame("sample_index", sample_index, reference.sample_count)
    return Fraction(sample_index, reference.sample_rate) + reference.pcm_sample0_file_time


# ─────────────────────────────────────────────────────────────
#  참조 만들기 — 세대·검증·포인터 출판
# ─────────────────────────────────────────────────────────────


def _extract_reference(
    source_id: str,
    original_stem: str,
    original: Path,
    original_sha256: str,
    policy: AudioPolicy,
    destination: Path,
) -> PcmReference:
    """새 불변 세대를 만들고, 짝을 검증한 뒤 포인터를 한 번 갈아끼운다.

    세대 PCM과 세대 기록은 남이 쓰지 않는 새 이름에 만든다 — 기존 세대와
    포인터는 손대지 않는다. 검증이 끝나야 포인터가 새 세대를 가리키므로, 출판이
    실패하거나 프로세스가 그 앞에서 죽어도 포인터는 완전한 짝을 가리킨 채다.
    실패한 시도의 사유 세대는 **그대로 남는다** — 출판이 애매한 순간에 무언가를
    파괴적으로 지우는 일은 없다. 남는 잔여물은 참조되지 않는 사유 자료일 뿐이다.
    """
    generation_pcm = _begin_atomic(destination, suffix=".pcm")
    generation_meta = _begin_atomic(destination, suffix=".json")
    extraction = extract_pcm_s16le(
        original,
        generation_pcm,
        stream_index=policy.stream_index,
        sample_rate=policy.sample_rate,
    )
    if extraction.sample_width != policy.sample_width:
        raise RuntimeError(
            f"P2-01 추출의 폭이 정책과 다르다: 요청 {policy.sample_width}바이트, "
            f"추출 {extraction.sample_width}바이트 — 참조로 삼지 않는다"
        )
    pcm_sha256 = _sha256_file(generation_pcm)
    reference = _assemble_reference(
        source_id,
        original_stem,
        original_sha256,
        generation_pcm,
        policy,
        extraction.pcm_sample0_file_time,
        pcm_sha256,
        extraction.stream.channels,
        extraction.sample_count,
    )
    _write_generation(generation_meta, reference, policy)
    _verify_generation(generation_pcm, generation_meta, reference, policy)
    _publish_pointer(destination, generation_pcm, generation_meta)
    return reference


def _assemble_reference(
    source_id: str,
    original_stem: str,
    original_sha256: str,
    generation_pcm: Path,
    policy: AudioPolicy,
    origin: Fraction,
    pcm_sha256: str,
    channels: int,
    sample_count: int,
) -> PcmReference:
    return PcmReference(
        source_id=source_id,
        original_stem=original_stem,
        original_sha256=original_sha256,
        path=generation_pcm,
        pcm_sha256=pcm_sha256,
        analysis_key=_analysis_key(
            original_sha256, policy, origin, pcm_sha256, channels, policy.sample_rate
        ),
        pcm_sample0_file_time=origin,
        sample_rate=policy.sample_rate,
        channels=channels,
        sample_count=sample_count,
    )


def _reusable_reference(
    destination: Path,
    source_id: str,
    original_stem: str,
    original_sha256: str,
    policy: AudioPolicy,
) -> PcmReference | None:
    """포인터가 가리키는 세대가 이번 호출과 전부 같고 해시까지 증명될 때만 되살린다.

    짝이 어긋난 어떤 상태도 여기를 통과하지 못 한다 — 통과하지 못 한 짝은
    완성물이 아니므로 새 세대가 곧 수복이다.
    """
    generation = _load_generation(destination)
    if generation is None:
        return None
    generation_pcm, stored = generation
    if (
        stored.source_id != source_id
        or stored.original_stem != original_stem
        or stored.original_sha256 != original_sha256
        or stored.policy != policy
    ):
        return None
    expected = stored.sample_count * stored.policy.sample_width * stored.channels
    if stored.channels < 1 or not _stored_file_matches(generation_pcm, expected, stored.pcm_sha256):
        log.warn(f"가리킨 세대 PCM이 기록과 어긋난다 — 다시 뽑는다: {generation_pcm}")
        return None
    log.debug(f"검증된 PCM 세대를 다시 쓴다: {generation_pcm}")
    return _assemble_reference(
        source_id,
        original_stem,
        original_sha256,
        generation_pcm,
        policy,
        stored.origin,
        stored.pcm_sha256,
        stored.channels,
        stored.sample_count,
    )


def _load_generation(destination: Path) -> tuple[Path, _Stored] | None:
    """포인터를 읽어 (세대 PCM 경로, 세대 기록). 못 믿으면 `None`."""
    pointer = _load_json_object(destination)
    if pointer is None or set(pointer) != _POINTER_KEYS:
        return None
    if pointer["format"] != _POINTER_FORMAT:
        return None
    generation_pcm = _private_generation_path(destination, pointer["generation_pcm"])
    generation_meta = _private_generation_path(destination, pointer["generation_metadata"])
    if generation_pcm is None or generation_meta is None:
        return None
    record = _load_json_object(generation_meta)
    if record is None:
        return None
    try:
        stored = _stored_from_record(record)
    except ValueError:
        log.debug(f"세대 기록을 못 믿는다 — 다시 뽑는다: {generation_meta}")
        return None
    return generation_pcm, stored


def _private_generation_path(destination: Path, name: object) -> Path | None:
    """포인터는 이 논리 주소를 위해 만든 사유 세대만 가리킬 수 있다.

    같은 디렉터리의, 주소 이름으로 시작하는 평범한 파일 이름이어야 한다 —
    경로 이탈이나 남의 파일을 가리키는 포인터는 못 믿는다.
    """
    if not isinstance(name, str) or not name or Path(name).name != name:
        return None
    if not name.startswith(f"{destination.name}."):
        return None
    return destination.parent / name


def _publish_pointer(destination: Path, generation_pcm: Path, generation_meta: Path) -> None:
    """유일한 출판 — 논리 주소의 포인터를 같은 디렉터리에서 원자적으로 갈아끼운다.

    포인터가 갈아끼워진 뒤의 임시파일 치우기는 **최선책**이다 — 치우다 죽어서
    이미 커밋된 출판을 실패로 되돌리는 일이 없어야 한다. 갈아끼우기 전의 실패는
    그대로 들어난다(그때 포인터는 아직 옛 세대를 가리킨다).
    """
    record: dict[str, object] = {
        "format": _POINTER_FORMAT,
        "generation_pcm": generation_pcm.name,
        "generation_metadata": generation_meta.name,
    }
    temp = _begin_atomic(destination)
    replaced = False
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, destination)
        replaced = True
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            if not replaced:
                raise
            log.warn(f"포인터는 출판됐지만 임시파일을 못 치웠다: {temp}")


def _write_generation(path: Path, reference: PcmReference, policy: AudioPolicy) -> None:
    """세대 기록 — 새 이름에 만드는 불변 파일이다. 기존 세대는 건드리지 않는다."""
    record: dict[str, object] = {
        "format": _GENERATION_FORMAT,
        "source_id": reference.source_id,
        "original_stem": reference.original_stem,
        "original_sha256": reference.original_sha256,
        "stream_index": policy.stream_index,
        "sample_rate": policy.sample_rate,
        "sample_width": policy.sample_width,
        "pcm_sha256": reference.pcm_sha256,
        "pcm_sample0_file_time": f"{reference.pcm_sample0_file_time.numerator}"
        f"/{reference.pcm_sample0_file_time.denominator}",
        "channels": reference.channels,
        "sample_count": reference.sample_count,
    }
    temp = _begin_atomic(path)
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def _verify_generation(
    generation_pcm: Path,
    generation_meta: Path,
    reference: PcmReference,
    policy: AudioPolicy,
) -> None:
    """포인터를 옮기기 전에 디스크의 짝을 완전히 검증한다 — 부분 짝은 못 나간다."""
    record = _load_json_object(generation_meta)
    if record is None:
        raise RuntimeError(f"세대 기록을 다시 읽지 못했다: {generation_meta}")
    try:
        stored = _stored_from_record(record)
    except ValueError as exc:
        raise RuntimeError(f"세대 기록이 스스로와 어긋난다: {generation_meta} ({exc})") from exc
    if (
        stored.source_id != reference.source_id
        or stored.original_stem != reference.original_stem
        or stored.original_sha256 != reference.original_sha256
        or stored.policy != policy
        or stored.pcm_sha256 != reference.pcm_sha256
        or stored.origin != reference.pcm_sample0_file_time
        or stored.channels != reference.channels
        or stored.sample_count != reference.sample_count
    ):
        raise RuntimeError(f"세대 기록이 참조와 어긋난다: {generation_meta}")
    # 포인터가 이름 붙이려면 기록의 사실만으로 키가 다시 만들어져야 한다.
    recomputed_key = _analysis_key(
        stored.original_sha256,
        stored.policy,
        stored.origin,
        stored.pcm_sha256,
        stored.channels,
        stored.policy.sample_rate,
    )
    if recomputed_key != reference.analysis_key:
        raise RuntimeError(
            f"세대 기록이 분석 키를 다시 만들지 못한다: {generation_meta} "
            f"(기록 → {recomputed_key}, 참조 {reference.analysis_key})"
        )
    expected = reference.sample_count * policy.sample_width * reference.channels
    if not _stored_file_matches(generation_pcm, expected, reference.pcm_sha256):
        raise RuntimeError(f"세대 PCM이 참조와 어긋난다: {generation_pcm}")


def _analysis_key(
    original_sha256: str,
    policy: AudioPolicy,
    origin: Fraction,
    pcm_sha256: str,
    channels: int,
    sample_rate: int,
) -> str:
    """내용이 키다 — 원본 해시·정책·원점·PCM 해시·채널·주파수의 해시."""
    return hashlib.sha256(
        _key_material(original_sha256, policy, origin, pcm_sha256, channels, sample_rate).encode()
    ).hexdigest()


def _key_material(
    original_sha256: str,
    policy: AudioPolicy,
    origin: Fraction,
    pcm_sha256: str,
    channels: int,
    sample_rate: int,
) -> str:
    """키 재료 — 이름 붙은 줄마다 하나의 입력이다. 경로·이름·source_id는 없다."""
    return "\n".join(
        [
            _KEY_DOMAIN,
            f"original_sha256={original_sha256}",
            f"audio_ordinal={policy.stream_index}",
            f"pcm_sample_rate={policy.sample_rate}",
            f"pcm_sample_width={policy.sample_width}",
            f"pcm_sample0_file_time={origin.numerator}/{origin.denominator}",
            f"pcm_sha256={pcm_sha256}",
            f"channels={channels}",
            f"sample_rate={sample_rate}",
        ]
    )


def _stored_file_matches(path: Path, expected_bytes: int, expected_sha256: str) -> bool:
    if not path.is_file() or path.stat().st_size != expected_bytes:
        return False
    return _sha256_file(path) == expected_sha256


def _load_json_object(path: Path) -> dict[str, object] | None:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 없거나 깨졌거나 — 다시 뽑는 것이 곧 치료다
    return record if isinstance(record, dict) else None


def _stored_from_record(record: dict[str, object]) -> _Stored:
    if set(record) != _GENERATION_KEYS:
        raise ValueError(f"세대 기록 키 집합이 다르다: {sorted(record)}")
    if record["format"] != _GENERATION_FORMAT:
        raise ValueError(f"세대 기록 형식이 다르다: {record['format']!r}")
    return _Stored(
        source_id=_record_text(record, "source_id"),
        original_stem=_record_text(record, "original_stem"),
        original_sha256=_record_text(record, "original_sha256"),
        policy=AudioPolicy(
            stream_index=_record_count(record, "stream_index"),
            sample_rate=_record_count(record, "sample_rate"),
            sample_width=_record_count(record, "sample_width"),
        ),
        pcm_sha256=_record_text(record, "pcm_sha256"),
        origin=_parse_fraction(record["pcm_sample0_file_time"]),
        channels=_record_count(record, "channels"),
        sample_count=_record_count(record, "sample_count"),
    )


def _record_count(record: dict[str, object], key: str) -> int:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"세대 기록 {key}가 정수가 아니다: {value!r}")
    return value


def _record_text(record: dict[str, object], key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"세대 기록 {key}가 비지 않은 문자열이 아니다: {value!r}")
    return value


def _parse_fraction(value: object) -> Fraction:
    if not isinstance(value, str):
        raise ValueError(f"원점 기록이 문자열이 아니다: {value!r}")
    numerator, _, denominator = value.partition("/")
    try:
        return Fraction(int(numerator), int(denominator))
    except ZeroDivisionError as exc:
        raise ValueError(f"원점 기록의 분모가 0이다: {value!r}") from exc


# ─────────────────────────────────────────────────────────────
#  저장 PCM 읽기 — 크기·해시 검증과 묶인 읽기
# ─────────────────────────────────────────────────────────────


def _read_stored_bytes(reference: PcmReference, start_frame: int, frame_count: int) -> bytes:
    """저장 PCM에서 프레임 범위를 읽는다 — 읽기 전에 파일 전체를 해시로 대조한다.

    크기만 같고 내용이 바뀐 파일은 참조가 아니다. 같은 길이의 조용한 변조도
    걸리게 하려면 통째로 해시하는 수밖에 없다.
    """
    frame_bytes = _CONTRACT_SAMPLE_WIDTH * reference.channels
    expected = reference.sample_count * frame_bytes
    if not reference.path.is_file():
        raise RuntimeError(f"저장 PCM이 없다: {reference.path}")
    actual = reference.path.stat().st_size
    if actual != expected:
        raise RuntimeError(
            f"저장 PCM이 참조와 어긋난다: {reference.path} "
            f"(기록 {reference.sample_count}프레임 × {frame_bytes}바이트 "
            f"= {expected}, 실제 {actual})"
        )
    if _sha256_file(reference.path) != reference.pcm_sha256:
        raise RuntimeError(f"저장 PCM 해시가 참조와 어긋난다: {reference.path}")
    with reference.path.open("rb") as handle:
        handle.seek(start_frame * frame_bytes)
        data = handle.read(frame_count * frame_bytes)
    if len(data) != frame_count * frame_bytes:
        raise RuntimeError(
            f"저장 PCM이 요청만큼 주지 못했다: {reference.path} "
            f"({start_frame}프레임부터 {frame_count}프레임)"
        )
    return data


def _window_abs_peak(values: array.array[int], start: int, window: int) -> int:
    """한 채널의 `[start, start+window)` 창 안 최대 절대 진폭."""
    scoped = values[start : start + window]
    return max(max(scoped), -min(scoped))


# ─────────────────────────────────────────────────────────────
#  검증·공용구 — 세는 수·문자열·경로·새 파일 이름
# ─────────────────────────────────────────────────────────────


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}은 비지 않은 문자열이어야 한다: {value!r}")


def _require_distinct_paths(original: Path, destination: Path) -> None:
    """원본 미디어는 캐시 주소와 같은 곳에 살면 안 된다 — 덮어쓸 수 있기 때문이다."""
    if original.resolve() == destination.resolve():
        raise ValueError(
            f"경로가 충돌한다 — 원본을 덮어쓸 수 없다: 원본 {original}, 논리 주소 {destination}"
        )


def _require_original_identity(original: Path, original_sha256: str) -> None:
    """원본을 통째로 해시해 주어진 정체성과 같은지 — 재사용·추출 전에."""
    if not original.is_file():
        raise ValueError(f"원본 파일이 없다: {original}")
    digest = _sha256_file(original)
    if digest != original_sha256:
        raise ValueError(
            f"원본 해시가 주어진 정체성과 다르다: {original} "
            f"(주어진 {original_sha256}, 실제 {digest})"
        )


def _require_exact_seconds(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
        raise ValueError(
            f"{name}은 정확한 Fraction 또는 정수여야 한다 (float는 쓰지 않는다): {value!r}"
        )


def _require_frame(name: str, value: object, stored_frames: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= stored_frames:
        raise ValueError(
            f"{name}은 0 이상 {stored_frames} 이하의 정수 프레임 번호여야 한다: {value!r}"
        )


def _require_slice(reference: PcmReference, samples: SampleRange) -> None:
    if _bad_count(samples.start) or _bad_count(samples.end):
        raise ValueError(f"슬라이스 범위는 정수 프레임 번호다: {samples.start!r}..{samples.end!r}")
    if samples.start < 0 or samples.end > reference.sample_count or samples.end <= samples.start:
        raise ValueError(
            f"참조가 줄 수 없는 범위다: [{samples.start}, {samples.end}) "
            f"(저장 {reference.sample_count}프레임)"
        )


def _retained_range(reference: PcmReference, pair: object, position: int) -> SampleRange:
    """남긴 구간 목록의 한 항목을 환산한다 — (start, end) 초 쌍이 아니면 그 자리에서 죽인다."""
    if not isinstance(pair, tuple) or len(pair) != 2:
        raise ValueError(f"{position}번째 구간은 (start, end) 초 쌍이어야 한다: {pair!r}")
    start, end = pair
    return retained_sample_range(reference, start, end)


def _retained_path(directory: Path, index: int) -> Path:
    """남긴 구간 조각의 이름 — 순번만으로 짓는다.

    참조의 stem은 계약상 비지 않은 문자열이면 무엇이든 될 수 있다. 그걸 경로
    문법으로 끼우면 "../escape" 같은 정상 참조가 요청한 디렉터리 밖에 쓰고 절대경로
    stem은 디렉터리를 통째로 점령한다 — stem은 이름 재료가 아니라 정체성 문자열이다.
    순번뿐인 이름은 결정적이기도 하다(재실행이 같은 이름을 낸다).
    """
    return directory / f"range-{index:04d}.wav"


def _bad_count(value: object) -> bool:
    """bool은 int인 척한다 — 세는 수에서는 걸러낸다."""
    return isinstance(value, bool) or not isinstance(value, int)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _begin_atomic(destination: Path, suffix: str = ".tmp") -> Path:
    """같은 디렉터리의 남이 쓰지 않는 새 이름 — 세대도 임시파일도 이것으로 만든다."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f"{destination.name}.", suffix=suffix
    )
    os.close(handle)
    return Path(name)
