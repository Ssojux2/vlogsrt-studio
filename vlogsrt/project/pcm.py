"""원본 PCM 참조 — 저장하고 정확히 자르고 자를 자리를 고른다 (P2-02).

P2-01이 잰 시계(`pcm_sample0_file_time`, 변환 후 첫 프레임의 실측 pts)를 그대로
믿고, 16 kHz s16le PCM을 **내용으로 주소를 매기는** 불변 참조로 저장한다. 참조의
키는 원본 해시·오디오 정책·원점·PCM 해시·채널·주파수다 — 경로·이름·source_id는
키 재료가 아니다. 완성물은 곁 기록(해시·정책·측정값)이 전부 맞고 파일 해시까지
일치할 때만 다시 쓴다.

슬라이스는 저장된 그 PCM에서 `[start, end)` 프레임을 **바이트 그대로** 옮긴다.
시작은 ceil, 끝은 floor — 제거된 오디오의 덧붙임도 문맥도 없고 남은 오디오가
줄어들 일도 없다. 자를 자리(`quiet_sample`)는 창의 진폭만 잴 뿐 말/침묵을
판정하지 않는다. 시각은 전부 정확한 `Fraction`이며 float는 들어오지 못한다.

참조는 오디오가 **선언된** 자원에만 존재한다. 프로브·추출이 실패하면 여기서
경로·해시·프레임 수·원점을 지어내지 않고 그대로 죽는다 — 명시적인 unavailable
처분은 P3-02 소유다(spre disposition 문서). ffmpeg 경계는 P2-01
(`vlogsrt.preprocess`)이고 이 모듈은 subprocess를 모른다.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import os
import tempfile
import wave
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

_SIDECAR_FORMAT = "vlogsrt-pcm-sidecar-v1"
_SIDECAR_SUFFIX = ".json"
_SIDECAR_KEYS = frozenset(
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
    않는다: 추출 증명(분석 키)이 정책을 묶어 두기 때문이다.
    """

    stream_index: int = 0
    sample_rate: int = 16000
    sample_width: int = 2


@dataclass(frozen=True, slots=True)
class PcmReference:
    """저장된 원본 PCM과 그 시계의 원점.

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
    """곁 기록에서 읽은 완성물의 사실 — 재사용 판정에만 쓴다."""

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
    """원본에서 PCM 참조를 만들어 `destination`에 원자적으로 저장한다.

    `destination`이 이미 있을 때 **재사용하는 조건은 전부**다: 곁 기록의 정체성·
    정책·측정값이 이번 호출과 같고, 파일 크기가 프레임 수와 맞고, 파일 해시까지
    곁 기록과 일치한다. 하나라도 어긋나면 다시 뽑아 덮어쓴다.
    """
    _require_text(source_id, "source_id")
    _require_text(original_stem, "original_stem")
    _require_text(original_sha256, "original_sha256")
    _require_policy(policy)
    sidecar = _sidecar_path(destination)
    reusable = _reusable_reference(
        destination, sidecar, source_id, original_stem, original_sha256, policy
    )
    if reusable is not None:
        return reusable
    return _extract_reference(
        source_id, original_stem, original, original_sha256, policy, destination, sidecar
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
    그대로다. 제거된 오디오의 덧붙임이나 문맥은 없다.
    """
    _require_slice(reference, samples)
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


def sample_file_time(reference: PcmReference, sample_index: int) -> Fraction:
    """`file_time = sample_index/sample_rate + 원점` — 정확한 유리수 한 줄."""
    _require_frame("sample_index", sample_index, reference.sample_count)
    return Fraction(sample_index, reference.sample_rate) + reference.pcm_sample0_file_time


# ─────────────────────────────────────────────────────────────
#  참조 만들기 — 추출·곁 기록·재사용
# ─────────────────────────────────────────────────────────────


def _extract_reference(
    source_id: str,
    original_stem: str,
    original: Path,
    original_sha256: str,
    policy: AudioPolicy,
    destination: Path,
    sidecar: Path,
) -> PcmReference:
    """P2-01로 임시 파일에 뽑고, 해시로 검증한 뒤 원자적으로 갈아끼운다."""
    temp = _begin_atomic(destination)
    try:
        extraction = extract_pcm_s16le(
            original,
            temp,
            stream_index=policy.stream_index,
            sample_rate=policy.sample_rate,
        )
        if extraction.sample_width != policy.sample_width:
            raise RuntimeError(
                f"P2-01 추출의 폭이 정책과 다르다: 요청 {policy.sample_width}바이트, "
                f"추출 {extraction.sample_width}바이트 — 참조로 삼지 않는다"
            )
        pcm_sha256 = _sha256_file(temp)
        reference = _assemble_reference(
            source_id,
            original_stem,
            original_sha256,
            destination,
            policy,
            extraction.pcm_sample0_file_time,
            pcm_sha256,
            extraction.stream.channels,
            extraction.sample_count,
        )
        os.replace(temp, destination)
        _write_sidecar(sidecar, reference, policy)
        return reference
    finally:
        temp.unlink(missing_ok=True)


def _assemble_reference(
    source_id: str,
    original_stem: str,
    original_sha256: str,
    destination: Path,
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
        path=destination,
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
    sidecar: Path,
    source_id: str,
    original_stem: str,
    original_sha256: str,
    policy: AudioPolicy,
) -> PcmReference | None:
    """곁 기록·파일 해시가 전부 맞는 완성물만 되살린다."""
    record = _load_sidecar(sidecar)
    if record is None:
        return None
    try:
        stored = _stored_from_record(record)
    except ValueError:
        log.debug(f"곁 기록을 못 믿는다 — 다시 뽑는다: {sidecar}")
        return None
    if (
        stored.source_id != source_id
        or stored.original_stem != original_stem
        or stored.original_sha256 != original_sha256
        or stored.policy != policy
    ):
        return None
    if not _verify_stored(destination, stored):
        return None
    log.debug(f"검증된 PCM 참조를 다시 쓴다: {destination}")
    return _assemble_reference(
        source_id,
        original_stem,
        original_sha256,
        destination,
        policy,
        stored.origin,
        stored.pcm_sha256,
        stored.channels,
        stored.sample_count,
    )


def _analysis_key(
    original_sha256: str,
    policy: AudioPolicy,
    origin: Fraction,
    pcm_sha256: str,
    channels: int,
    sample_rate: int,
) -> str:
    """내용이 키다 — 원본 해시·정책·원점·PCM 해시·채널·주파수의 해시."""
    material = "\n".join(
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
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _sidecar_path(destination: Path) -> Path:
    return destination.with_name(destination.name + _SIDECAR_SUFFIX)


def _verify_stored(destination: Path, stored: _Stored) -> bool:
    """파일이 곁 기록이 말하는 것과 같은지 — 크기부터, 해시까지."""
    expected = stored.sample_count * stored.policy.sample_width * stored.channels
    if stored.channels < 1 or not destination.is_file():
        return False
    actual = destination.stat().st_size
    if actual != expected:
        log.warn(
            f"저장 PCM 크기가 곁 기록과 어긋난다 — 다시 뽑는다: {destination} "
            f"(기록 {expected} 바이트, 실제 {actual} 바이트)"
        )
        return False
    if _sha256_file(destination) != stored.pcm_sha256:
        log.warn(f"저장 PCM 해시가 곁 기록과 어긋난다 — 다시 뽑는다: {destination}")
        return False
    return True


def _write_sidecar(sidecar: Path, reference: PcmReference, policy: AudioPolicy) -> None:
    """완성물의 사실을 곁에 남긴다 — PCM 다음에 쓰므로 남은 쪽이 늘 진실을 말한다."""
    record: dict[str, object] = {
        "format": _SIDECAR_FORMAT,
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
    temp = _begin_atomic(sidecar)
    try:
        temp.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temp, sidecar)
    finally:
        temp.unlink(missing_ok=True)


def _load_sidecar(sidecar: Path) -> dict[str, object] | None:
    try:
        record = json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 없거나 깨졌거나 — 다시 뽑는 것이 곧 치료다
    return record if isinstance(record, dict) else None


def _stored_from_record(record: dict[str, object]) -> _Stored:
    if set(record) != _SIDECAR_KEYS:
        raise ValueError(f"곁 기록 키 집합이 다르다: {sorted(record)}")
    if record["format"] != _SIDECAR_FORMAT:
        raise ValueError(f"곁 기록 형식이 다르다: {record['format']!r}")
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
        raise ValueError(f"곁 기록 {key}가 정수가 아니다: {value!r}")
    return value


def _record_text(record: dict[str, object], key: str) -> str:
    value = record[key]
    if not isinstance(value, str) or not value:
        raise ValueError(f"곁 기록 {key}가 비지 않은 문자열이 아니다: {value!r}")
    return value


def _parse_fraction(value: object) -> Fraction:
    if not isinstance(value, str):
        raise ValueError(f"원점 기록이 문자열이 아니다: {value!r}")
    numerator, _, denominator = value.partition("/")
    return Fraction(int(numerator), int(denominator))


# ─────────────────────────────────────────────────────────────
#  저장 PCM 읽기 — 크기 검증과 묶인 읽기
# ─────────────────────────────────────────────────────────────


def _read_stored_bytes(reference: PcmReference, start_frame: int, frame_count: int) -> bytes:
    frame_bytes = _CONTRACT_SAMPLE_WIDTH * reference.channels
    expected = reference.sample_count * frame_bytes
    if not reference.path.is_file() or reference.path.stat().st_size != expected:
        actual = reference.path.stat().st_size if reference.path.is_file() else None
        raise RuntimeError(
            f"저장 PCM이 참조와 어긋난다: {reference.path} "
            f"(기록 {reference.sample_count}프레임 × {frame_bytes}바이트 "
            f"= {expected}, 실제 {actual})"
        )
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
#  검증·공용구 — 세는 수와 문자열과 원자적 쓰기
# ─────────────────────────────────────────────────────────────


def _require_text(value: object, name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name}은 비지 않은 문자열이어야 한다: {value!r}")


def _require_policy(policy: AudioPolicy) -> None:
    if _bad_count(policy.stream_index) or policy.stream_index < 0:
        raise ValueError(f"오디오 순번은 0 이상 정수여야 한다: {policy.stream_index!r}")
    if _bad_count(policy.sample_rate) or policy.sample_rate != _CONTRACT_SAMPLE_RATE:
        raise ValueError(
            f"저장 PCM의 주파수는 {_CONTRACT_SAMPLE_RATE}Hz가 아니면 안 된다: "
            f"{policy.sample_rate!r}"
        )
    if _bad_count(policy.sample_width) or policy.sample_width != _CONTRACT_SAMPLE_WIDTH:
        raise ValueError(
            f"저장 PCM은 s16le다 — 폭은 {_CONTRACT_SAMPLE_WIDTH}바이트여야 한다: "
            f"{policy.sample_width!r}"
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


def _bad_count(value: object) -> bool:
    """bool은 int인 척한다 — 세는 수에서는 걸러낸다."""
    return isinstance(value, bool) or not isinstance(value, int)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_READ_BLOCK_BYTES), b""):
            digest.update(block)
    return digest.hexdigest()


def _begin_atomic(destination: Path) -> Path:
    """같은 디렉터리의 임시 경로 — `os.replace`가 원자적 갈아끼움을 보장한다."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f"{destination.name}.", suffix=".tmp"
    )
    os.close(handle)
    return Path(name)
