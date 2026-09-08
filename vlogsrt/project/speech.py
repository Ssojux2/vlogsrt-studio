"""소스 전사 어댑터 — 유일 소스마다 명시적 네임스페이스로 엔진을 돈다 (P3-02).

계획("Source analysis, clocks, identity")의 정본:

- **소스 하나는 내용이다.** 재사용 신원은 `analysis_key`(원본 해시·정책·원점·
  PCM 해시·채널·주파수)이고, 경로·이름·XML 국소 resource id는 신원이 아니다.
  같은 내용의 반복 인스턴스는 한 번만 전사하고 같은 기록을 소비한다.
- **엔진은 레거시 그대로다.** 시각과 글자는 whisper 응답에서만 나온다(규칙 1).
  이 모듈이 새로 만드는 것은 조각의 좌표(저장 PCM의 정확한 샘플)와 실패의
  상태뿐이다. `parse_segments`·`build_spine`·`repair.settle`·`refine.refine`의
  float 호환은 그대로 쓴다.
- **시계는 두 개가 나란히 남는다.** `SourceSegment.audio_*`는 레거시 세그먼트의
  디코드 오디오 국소 시각을 `Fraction(str(value))`로 옮긴 것이고, `file_*`는
  거기에 잰 원점 `pcm_sample0_file_time`을 **정확히 한 번** 더한 것이다.
  `repairs`·`cues`는 레거시 국소 float 그대로의 불변 프로버넌스다 — P3-03의
  조판 투영은 이미 정제된 `cues`에서 나온다(settled_spine이 아니다).
- **실패는 조용한 성공이 아니다.** 주입된 커터와 전사기를 싼 사유 포획 래퍼가
  호출·응답 계약 위반을 기록하고 다시 던진다. 레거시 `repair`는 그 예외를 오늘과
  같이 잡아 유지·접기 정책으로 흘러가게 두지만, 어댑터는 settle 뒤 포획 기록을
  보고 **실제 참조 신원을 가진 error 기록**만 남긴다 — 완성물은 나오지 않는다.

호출·ffmpeg 경계는 건드리지 않는다. 네트워크는 없다.
"""

from __future__ import annotations

import hashlib
import json
import math
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, TypeAlias

from vlogsrt import cutpoints, log, output, pipeline, refine, repair, transcribe
from vlogsrt.config import Config
from vlogsrt.models import Chunk, Cue, Repair, Segment, Span
from vlogsrt.project.media import ResolvedMedia, sha256_file
from vlogsrt.project.pcm import (
    PcmReference,
    SampleRange,
    quiet_sample,
    retained_sample_range,
    write_wav_slice,
)
from vlogsrt.transcribe import (
    ARBITER_KIND,
    FIXTURE_KIND,
    RESCUE_KIND,
    VERIFY_KIND,
    ChunkCoverage,
    ChunkResponse,
    MalformedTranscript,
    Transcriber,
    TranscriptionJob,
)

_SCRATCH_DIR = "work"
"""조각 WAV가 사는 휘발성 자리 — 완성물 네임스페이스와 갈라진다(형식 사실)."""

_SAMPLE_WIDTH = 2
"""s16le 한 샘플의 바이트 폭 — 조각 바이트 한도를 잴 때 쓰는 포맷의 사실이다."""

_SEGMENT_ID_DOMAIN = "vlogsrt-source-segment-v1"
"""세그먼트 신원의 도메인 구분자 — 다른 해시 재료와 우연히 겹치지 않게 한다."""

_STATUS_SUCCESS = "success"
_STATUS_EMPTY = "empty"
_STATUS_ERROR = "error"

_TRACE_STATUSES = frozenset({_STATUS_SUCCESS, _STATUS_EMPTY, _STATUS_ERROR})
_UNAVAILABLE_STATUSES = frozenset({"no_audio", "error"})
_UNAVAILABLE_REASONS = frozenset({"declared_no_audio", "stream_unavailable", "pcm_unavailable"})
_TRACE_TEXT_FIELDS = (
    "source_id",
    "original_stem",
    "analysis_key",
    "pcm_analysis_key",
    "model",
    "config_sha256",
)
_UNAVAILABLE_TEXT_FIELDS = ("resource_scoped_id", "source_id", "original_sha256", "reason")

# 글자만 주는 호출. 시각을 만들지 않으므로 응답 계약도 글자만 요구한다.
_SEGMENT_PAYLOAD_KINDS = frozenset({FIXTURE_KIND, RESCUE_KIND})
_KIND_LABELS = {
    FIXTURE_KIND: "전사",
    VERIFY_KIND: "검증",
    RESCUE_KIND: "재전사",
    ARBITER_KIND: "심판",
}


# ─────────────────────────────────────────────────────────────
#  기록 — 소스 분석의 두 변별자
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class SourceSegment:
    """레거시 세그먼트를 소스 시계로 옮긴 기록. `segment_id`는 결정적 해시다."""

    segment_id: str
    audio_start: Fraction
    audio_end: Fraction
    file_start: Fraction
    file_end: Fraction
    text: str
    compression_ratio: float
    avg_logprob: float
    no_speech_prob: float


@dataclass(frozen=True, slots=True)
class SourceTrace:
    """소스 하나의 전사 결과. 완성되면 불변이고 캐시 신원은 `analysis_key`다.

    `repairs`·`cues`는 레거시 **디코드 오디오 국소 float 시각** 그대로의 불변
    프로버넌스다. P3-03의 조판 투영은 이미 정제된 `cues`에서 나온다.
    """

    variant: Literal["source_trace"]
    source_id: str
    original_stem: str
    analysis_key: str
    pcm_analysis_key: str
    pcm_sample0_file_time: Fraction
    status: Literal["success", "empty", "error"]
    draft: tuple[SourceSegment, ...]
    settled_spine: tuple[SourceSegment, ...]
    repairs: tuple[Repair, ...]
    cues: tuple[Cue, ...]
    coverage: tuple[ChunkCoverage, ...]
    model: str
    config_sha256: str
    source_srt_relative_path: str | None
    source_srt_sha256: str | None
    error: str | None

    def __post_init__(self) -> None:
        if self.variant != "source_trace":
            raise ValueError(f"변별자가 source_trace가 아니다: {self.variant!r}")
        if self.status not in _TRACE_STATUSES:
            raise ValueError(f"알 수 없는 상태다: {self.status!r}")
        for name in _TRACE_TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name}은 비지 않은 문자열이어야 한다: {value!r}")
        if (self.status == _STATUS_ERROR) != (self.error is not None):
            raise ValueError(
                f"status={self.status!r}와 error={self.error!r}가 어긋난다 — 실패는 상태로 남는다"
            )
        published = self.status in {_STATUS_SUCCESS, _STATUS_EMPTY}
        if published != (self.source_srt_relative_path is not None):
            raise ValueError(
                f"status={self.status!r}인데 source_srt_relative_path="
                f"{self.source_srt_relative_path!r} — 성공·빈 성공은 SRT를 내놓고 실패는 못 낸다"
            )
        if published != (self.source_srt_sha256 is not None):
            raise ValueError(
                f"status={self.status!r}인데 source_srt_sha256={self.source_srt_sha256!r}"
            )
        if self.status == _STATUS_EMPTY and (self.draft or self.settled_spine or self.cues):
            raise ValueError("빈 성공인데 초안·골격·큐가 남아 있다")
        if self.status == _STATUS_SUCCESS and not self.cues:
            raise ValueError("성공인데 큐가 하나도 없다")


@dataclass(frozen=True, slots=True)
class UnavailableSourceAudio:
    """PCM 참조가 아예 없는 리소스 — 전사할 오디오가 없다는 기록일 뿐이다.

    PCM 경로·해시·원점·키, 전사문, 덮개가 없다. P7이 문맥 있는 pre-PCM 실패를
    잡아 이 기록을 만든다 — 여기서는 신원만 검사하고 그대로 흘려보낸다.
    """

    variant: Literal["unavailable_source_audio"]
    resource_scoped_id: str
    source_id: str
    original_sha256: str
    status: Literal["no_audio", "error"]
    reason_code: Literal["declared_no_audio", "stream_unavailable", "pcm_unavailable"]
    reason: str

    def __post_init__(self) -> None:
        if self.variant != "unavailable_source_audio":
            raise ValueError(f"변별자가 unavailable_source_audio가 아니다: {self.variant!r}")
        if self.status not in _UNAVAILABLE_STATUSES:
            raise ValueError(f"알 수 없는 상태다: {self.status!r}")
        if self.reason_code not in _UNAVAILABLE_REASONS:
            raise ValueError(f"알 수 없는 사유 코드다: {self.reason_code!r}")
        for name in _UNAVAILABLE_TEXT_FIELDS:
            value = getattr(self, name)
            if not isinstance(value, str) or not value:
                raise ValueError(f"{name}은 비지 않은 문자열이어야 한다: {value!r}")
        if (self.status == "no_audio") != (self.reason_code == "declared_no_audio"):
            raise ValueError(
                f"status={self.status!r}와 reason_code={self.reason_code!r}가 어긋난다 — "
                "no_audio는 선언된 무음에만, stream/pcm 실패는 error에만 온다"
            )


SourceAnalysis: TypeAlias = SourceTrace | UnavailableSourceAudio
AudioSourceInput: TypeAlias = tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]


# ─────────────────────────────────────────────────────────────
#  조각 계획 — 전체를 한 번씩만, 경계는 조용한 창에서
# ─────────────────────────────────────────────────────────────


def plan_source_ranges(reference: PcmReference, cfg: Config) -> tuple[SampleRange, ...]:
    """저장 PCM 전체를 빈틈없이 덮는 조각 범위 — 자리만 고르고 구간은 못 고른다.

    조각 수는 레거시와 같은 두 한도(`chunk_max_bytes`·`chunk_max_seconds`)가 정하고,
    경계는 목표 지점 주위의 가장 조용한 창(`quiet_sample`)으로 옮긴 뒤 정확한
    샘플로 자른다. 합치면 언제나 `[0, sample_count)`다(규칙 4).
    """
    frames = reference.sample_count
    if frames < 1:
        raise ValueError(f"저장 프레임이 없다: {frames!r} — 덮을 오디오가 없다")
    rate = reference.sample_rate
    duration = float(Fraction(frames, rate))
    count = cutpoints.chunk_count(_payload_bytes(reference), duration, cfg)
    search = max(0, round(cfg.cut_search_seconds * rate))
    window = min(frames, max(1, round(cfg.cut_quiet_window_ms * rate / 1000)))
    cuts: list[int] = []
    previous = 0
    for index in range(1, count):
        target = frames * index // count
        cut = _quiet_cut(reference, target, search, window)
        if not previous < cut < frames:
            cut = target if previous < target < frames else (previous + frames) // 2
        cuts.append(cut)
        previous = cut
    edges = [0, *cuts, frames]
    return tuple(SampleRange(start, end) for start, end in pairwise(edges))


def _payload_bytes(reference: PcmReference) -> int:
    """조각이 실릴 WAV의 데이터 바이트 — 모델 입력 한도를 잰다."""
    return reference.sample_count * _SAMPLE_WIDTH * reference.channels


def _quiet_cut(reference: PcmReference, target: int, search: int, window: int) -> int:
    """목표 주위의 묶인 창 안에서 가장 조용한 창의 시작 프레임.

    창을 놓을 자리가 없으면 목표 그대로다 — 자를 자리를 못 고르는 것이 구간을
    잃을 이유가 되지 못한다(규칙 4). 말인지 침묵인지는 판단하지 않는다.
    """
    lo = max(0, target - search)
    hi = min(target + search, reference.sample_count - window)
    if lo > hi:
        return target
    return quiet_sample(reference, target=target, search_samples=search, window_samples=window)


# ─────────────────────────────────────────────────────────────
#  조각 — 저장 PCM의 실제 바이트 슬라이스와 실제 프레임 좌표
# ─────────────────────────────────────────────────────────────


def _snapped_chunk(reference: PcmReference, span: SampleRange, index: int, path: Path) -> Chunk:
    """잘린 프레임에서 좌표를 만들고, 그 좌표를 다시 실제 프레임과 대조한다.

    좌표는 **환산된(snapped) 샘플**에서 나온다 — 요청한 초 원본이 아니다. 경계는
    ceil·floor로 안쪽에 묶여 있으므로, 원시 float 구간 끝과 비교하면 옳은 조각까지
    거짓으로 버린다.
    """
    rate = reference.sample_rate
    chunk = Chunk(
        path=path,
        offset=float(Fraction(span.start, rate)),
        duration=float(Fraction(span.end - span.start, rate)),
        index=index,
    )
    expected_offset = float(Fraction(span.start, rate))
    expected_duration = float(Fraction(span.end - span.start, rate))
    if chunk.offset != expected_offset or chunk.duration != expected_duration:
        raise RuntimeError(
            f"잘린 조각 {index}의 좌표가 실제 프레임 [{span.start}, {span.end})과 다르다: "
            f"offset {chunk.offset!r}(기대 {expected_offset!r}), "
            f"duration {chunk.duration!r}(기대 {expected_duration!r})"
        )
    return chunk


def _initial_chunks(
    reference: PcmReference, ranges: Sequence[SampleRange], scratch: Path
) -> tuple[Chunk, ...]:
    chunks: list[Chunk] = []
    for index, span in enumerate(ranges):
        path = scratch / f"initial-{index:04d}.wav"
        write_wav_slice(reference, span, path)
        chunks.append(_snapped_chunk(reference, span, index, path))
    return tuple(chunks)


def _repair_cutter(
    reference: PcmReference, scratch: Path, capture: _Capture
) -> Callable[[Sequence[Span]], list[Chunk]]:
    """재전사 구간을 같은 저장 PCM에서 자르는 주입 커터 — 실패는 기록되고 올라간다."""

    def cut(spans: Sequence[Span]) -> list[Chunk]:
        try:
            chunks: list[Chunk] = []
            for index, (low, high) in enumerate(spans):
                span = retained_sample_range(reference, Fraction(str(low)), Fraction(str(high)))
                path = scratch / f"repair-{index:04d}.wav"
                write_wav_slice(reference, span, path)
                chunks.append(_snapped_chunk(reference, span, index, path))
            return chunks
        except Exception as exc:  # noqa: BLE001 - 기록하고 그대로 다시 던진다
            capture.record("재전사 조각", exc)
            raise

    return cut


# ─────────────────────────────────────────────────────────────
#  실패 포획 — 사유 래퍼가 계약을 지키고 기록만 남긴다
# ─────────────────────────────────────────────────────────────


@dataclass
class _Capture:
    """포획된 운영 실패의 목록 — 어댑터가 settle 뒤에 보는 유일한 증거."""

    failures: list[str] = field(default_factory=list)

    def record(self, stage: str, exc: BaseException | str) -> None:
        text = " ".join(str(exc).split())
        self.failures.append(f"{stage}: {text}" if text else f"{stage}: {exc.__class__.__name__}")

    def describe(self) -> str:
        return "; ".join(self.failures)


class _CapturingTranscriber:
    """주입된 전사기를 싸는 사유 래퍼 — 계약 위반을 기록하고 그대로 다시 던진다.

    레거시 `repair`는 이 예외를 오늘과 같이 잡아 되풀이 정책(유지·접기)으로
    흘러가게 둔다. 어댑터는 settle 뒤 포획 기록을 보고 완성물을 내주지 않는다.
    세그먼트를 주는 호출(전사·재전사)은 P3-01의 엄격한 꼴을, 글자만 주는 호출
    (심판·검증)은 명시적 문자열 `text` 또는 `parse_text`가 읽는 목록 형태를 요구한다.
    """

    def __init__(self, inner: Transcriber, capture: _Capture) -> None:
        self._inner = inner
        self._capture = capture

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        label = _KIND_LABELS.get(job.kind, job.kind)
        try:
            responses = self._inner.transcribe(job, cfg)
            for chunk, response in _match_requested(job, responses):
                defect = _response_defect(job.kind, response.payload, f"{label} 조각 {chunk.index}")
                if defect is not None:
                    raise MalformedTranscript(defect)
        except Exception as exc:  # noqa: BLE001 - 기록하고 그대로 다시 던진다
            self._capture.record(f"{label} 호출", exc)
            raise
        return responses


def _match_requested(
    job: TranscriptionJob, responses: Sequence[ChunkResponse]
) -> list[tuple[Chunk, ChunkResponse]]:
    """요청한 조각마다 응답이 정확히 하나 — 누락·중복·요청 밖·오프셋 어긋남은 위반.

    위치로 짝짓지 않는다. 픽스처는 요청과 무관하게 녹음된 전부를 돌려줄 수 있으므로
    조각 번호와 오프셋으로 짝짓는다(네임스페이스 안의 부분집합 선택은 P3-01이 이미
    결정적으로 걸렀다).
    """
    requested = {chunk.index: chunk for chunk in job.chunks}
    matched: dict[int, ChunkResponse] = {}
    for response in responses:
        index = response.index
        if isinstance(index, bool) or not isinstance(index, int):
            raise MalformedTranscript(f"응답 조각 번호가 정수가 아닙니다: {index!r}")
        if index not in requested:
            raise MalformedTranscript(f"요청하지 않은 조각 응답이 돌아왔다: {index}")
        if index in matched:
            raise MalformedTranscript(f"조각 {index} 응답이 둘 이상 돌아왔다")
        chunk = requested[index]
        if not _finite(response.offset) or response.offset != chunk.offset:
            raise MalformedTranscript(
                f"조각 {index} 응답 오프셋이 요청({chunk.offset})과 다릅니다: {response.offset!r}"
            )
        matched[index] = response
    absent = sorted(set(requested) - set(matched))
    if absent:
        raise MalformedTranscript(f"응답이 돌아오지 않은 조각: {absent}")
    ordered = sorted(job.chunks, key=lambda chunk: chunk.index)
    return [(chunk, matched[chunk.index]) for chunk in ordered]


def _response_defect(kind: str, payload: Any, where: str) -> str | None:
    """호출 종류별 응답 계약. 위반을 하나만 골라 돌려주고 맞으면 None."""
    if kind in _SEGMENT_PAYLOAD_KINDS:
        return _segment_defect(payload, where)
    return _text_defect(payload, where)


def _segment_defect(payload: Any, where: str) -> str | None:
    """세그먼트를 주는 호출의 엄격한 꼴 — P3-01의 전사 결과형과 같은 선이다.

    여기가 사유 복제본인 것은 의도다: 포획 검증은 프로젝트가 소유하고(P4-03도 같은
    규칙을 소유한다), 공개 결과형 `transcribe_outcome`을 글자 전용 응답까지 끌어
    들이는 확장은 하지 않는다.
    """
    if not isinstance(payload, dict):
        return f"{where}: 응답이 JSON 객체가 아닙니다: {type(payload).__name__}"
    if "segments" not in payload:
        return f"{where}: 응답에 segments가 없습니다"
    segments = payload["segments"]
    if not isinstance(segments, list):
        return f"{where}: segments가 리스트가 아닙니다: {type(segments).__name__}"
    for position, segment in enumerate(segments):
        if not isinstance(segment, dict):
            return f"{where}: 세그먼트 {position}이 JSON 객체가 아닙니다"
        if "text" not in segment:
            return f"{where}: 세그먼트 {position}에 text가 없습니다"
        if not isinstance(segment["text"], str):
            return f"{where}: 세그먼트 {position}의 text가 문자열이 아닙니다: {segment['text']!r}"
        for key in ("start", "end"):
            if key not in segment:
                return f"{where}: 세그먼트 {position}에 {key}가 없습니다"
            if not _finite(segment[key]):
                return (
                    f"{where}: 세그먼트 {position}의 {key}가 유한 숫자가 아닙니다: {segment[key]!r}"
                )
        for key in ("compression_ratio", "avg_logprob", "no_speech_prob"):
            if key in segment and not _finite(segment[key]):
                return (
                    f"{where}: 세그먼트 {position}의 {key}가 유한 숫자가 아닙니다: {segment[key]!r}"
                )
    return None


def _text_defect(payload: Any, where: str) -> str | None:
    """글자만 주는 호출(심판·검증)의 꼴 — 시각은 요구하지도 지어내지도 않는다.

    명시적인 문자열 `text`(빈 문자열은 말이 없다는 증거) 또는 `parse_text`가 읽는
    형태 — `text`가 문자열인 객체들의 목록. 최상위 목록은 이 형태가 아니고, 둘 다
    없는 꼴(빈 객체 포함)은 위반이다.
    """
    if not isinstance(payload, dict):
        return f"{where}: 응답이 JSON 객체가 아닙니다: {type(payload).__name__}"
    if "text" in payload:
        if not isinstance(payload["text"], str):
            return f"{where}: text가 문자열이 아닙니다: {payload['text']!r}"
        return None
    if "segments" in payload:
        segments = payload["segments"]
        if not isinstance(segments, list):
            return f"{where}: segments가 리스트가 아닙니다: {type(segments).__name__}"
        for position, segment in enumerate(segments):
            if not isinstance(segment, dict):
                return f"{where}: 세그먼트 {position}이 JSON 객체가 아닙니다"
            if "text" not in segment:
                return f"{where}: 세그먼트 {position}에 text가 없습니다"
            if not isinstance(segment["text"], str):
                return (
                    f"{where}: 세그먼트 {position}의 text가 문자열이 아닙니다: {segment['text']!r}"
                )
        return None
    return f"{where}: 응답에 text도 segments도 없습니다"


def _finite(value: Any) -> bool:
    """숫자이면서 유한한 값만 받는다 — bool과 NaN·무한대는 시각이 될 수 없다."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# ─────────────────────────────────────────────────────────────
#  소스 하나의 전사 — 엔진을 순서대로 돌고 결과를 상태로 남긴다
# ─────────────────────────────────────────────────────────────


@dataclass
class _Progress:
    """이미 완성된 증거 — 실패 기록에도 초안·덮개가 남는다."""

    ranges: tuple[SampleRange, ...] = ()
    draft: tuple[Segment, ...] = ()
    coverage: tuple[ChunkCoverage, ...] = ()

    def evidence(self, reference: PcmReference) -> tuple[SourceSegment, ...]:
        return _source_segments(reference, self.ranges, self.draft) if self.draft else ()


def transcribe_source(
    media: ResolvedMedia,
    reference: PcmReference,
    cfg: Config,
    transcriber: Transcriber,
    artifact_dir: Path,
) -> SourceTrace:
    """검증된 짝 하나를 레거시 엔진 전체로 전사한다. 예외를 밖으로 새지 않는다.

    순서: 조각 계획 → 1차 전사(P3-01 결과형) → `repair.settle`(주입 커터) →
    불변 골격 → `pipeline.spine_to_cues` → `refine.refine`(polish 포함, 한 번) →
    소스 로컬 SRT. 실패는 언제나 **실제 참조의 신원을 가진 error 기록**이다 —
    unavailable로 강등되지 않고 완성물도 나오지 않는다.
    """
    _require_pair(media, reference)
    namespace = artifact_dir / media.asset_content_id
    scratch = namespace / _SCRATCH_DIR / reference.analysis_key
    capture = _Capture()
    progress = _Progress()
    try:
        return _analyze(
            reference,
            cfg,
            _CapturingTranscriber(transcriber, capture),
            capture,
            artifact_dir,
            namespace,
            scratch,
            progress,
        )
    except Exception as exc:  # noqa: BLE001 - 상태로 바꿔 남긴다. 조용히 두지는 않는다
        capture.record("소스 엔진", exc)
        return _error_trace(
            reference,
            cfg,
            artifact_dir,
            error=capture.describe(),
            draft=progress.evidence(reference),
            coverage=progress.coverage,
        )


def _analyze(
    reference: PcmReference,
    cfg: Config,
    wrapped: _CapturingTranscriber,
    capture: _Capture,
    artifact_dir: Path,
    namespace: Path,
    scratch: Path,
    progress: _Progress,
) -> SourceTrace:
    """1차 전사의 상태를 갈라 보내고, 초안이 있으면 재전사·조판·SRT로 간다."""
    outcome = _first_pass(reference, cfg, wrapped, namespace, scratch, progress)
    if outcome.status == _STATUS_ERROR:
        return _error_trace(
            reference,
            cfg,
            artifact_dir,
            error=outcome.error or "전사가 실패로 돌아왔다",
            draft=(),
            coverage=(),
        )
    progress.draft = outcome.segments
    if not progress.draft:
        # 명시적 빈이든 걸러진 빈이든 SRT는 같은 결정적 빈 바이트다.
        return _empty_trace(reference, cfg, artifact_dir, namespace, progress.coverage)
    return _settled_trace(
        reference, cfg, wrapped, capture, artifact_dir, namespace, scratch, progress
    )


def _first_pass(
    reference: PcmReference,
    cfg: Config,
    wrapped: _CapturingTranscriber,
    namespace: Path,
    scratch: Path,
    progress: _Progress,
) -> transcribe.TranscriptionOutcome:
    """조각을 만들어 1차 전사를 부른다. 완성된 증거는 progress에 남긴다."""
    progress.ranges = plan_source_ranges(reference, cfg)
    job = TranscriptionJob(
        stem=reference.original_stem,
        chunks=_initial_chunks(reference, progress.ranges, scratch),
        namespace=namespace,
    )
    outcome = transcribe.transcribe_outcome(wrapped, job, cfg)
    progress.coverage = outcome.coverage
    return outcome


def _settled_trace(
    reference: PcmReference,
    cfg: Config,
    wrapped: _CapturingTranscriber,
    capture: _Capture,
    artifact_dir: Path,
    namespace: Path,
    scratch: Path,
    progress: _Progress,
) -> SourceTrace:
    """재전사(주입 커터) → 불변 골격. 포획 실패가 있으면 여기서 끝난다."""
    duration = float(Fraction(reference.sample_count, reference.sample_rate))
    stem = reference.original_stem
    settled = repair.settle(
        list(progress.draft),
        repair.Source(
            stem=stem,
            duration=duration,
            namespace=namespace,
            cutter=_repair_cutter(reference, scratch, capture),
        ),
        cfg,
        wrapped,
    )
    evidence = progress.evidence(reference)
    if capture.failures:
        return _error_trace(
            reference,
            cfg,
            artifact_dir,
            error=capture.describe(),
            draft=evidence,
            coverage=progress.coverage,
        )
    return _published_trace(
        reference, cfg, artifact_dir, namespace, settled, evidence, progress, duration
    )


def _published_trace(
    reference: PcmReference,
    cfg: Config,
    artifact_dir: Path,
    namespace: Path,
    settled: repair.Settled,
    evidence: tuple[SourceSegment, ...],
    progress: _Progress,
    duration: float,
) -> SourceTrace:
    """불변 골격 → 큐 → 정제 → 소스 로컬 SRT. polish은 refine 안에서 한 번뿐이다."""
    stem = reference.original_stem
    cues = refine.refine(pipeline.spine_to_cues(list(settled.spine)), cfg, duration)
    if not cues:
        return _error_trace(
            reference,
            cfg,
            artifact_dir,
            error="정제를 끝내고 큐가 하나도 남지 않았다",
            draft=evidence,
            coverage=progress.coverage,
        )
    srt = output.save_srt(cues, namespace / f"{stem}.SOURCE.srt")
    log.info(f"소스 전사 완료: {stem} · 큐 {len(cues)}장")
    return _trace(
        reference,
        cfg,
        artifact_dir,
        status=_STATUS_SUCCESS,
        draft=evidence,
        settled=_source_segments(reference, progress.ranges, settled.spine),
        repairs=settled.repairs,
        cues=tuple(cues),
        coverage=progress.coverage,
        srt=srt,
        error=None,
    )


def transcribe_unique_sources(
    items: Sequence[AudioSourceInput],
    cfg: Config,
    transcriber: Transcriber,
    artifact_dir: Path,
) -> tuple[SourceAnalysis, ...]:
    """입력 쌍마다 분석을 하나씩 돌려준다 — 재사용 신원은 analysis_key다.

    모든 쌍의 신원·스키마를 **호출 앞에서 전부** 검사한다: 하나라도 어긋나면
    유료 호출은 깨진 입력에 값을 쓰지 않는다. unavailable 기록은 그 자원에 대해
    한 번만 그대로 흐르고 전사 비용은 0이다.
    """
    pairs = _validated_pairs(items)
    analyses: list[SourceAnalysis] = []
    by_analysis: dict[str, SourceTrace] = {}
    by_resource: dict[str, UnavailableSourceAudio] = {}
    for media, second in pairs:
        if isinstance(second, UnavailableSourceAudio):
            analyses.append(by_resource.setdefault(second.resource_scoped_id, second))
            continue
        trace = by_analysis.get(second.analysis_key)
        if trace is None:
            trace = transcribe_source(media, second, cfg, transcriber, artifact_dir)
            by_analysis[second.analysis_key] = trace
        analyses.append(trace)
    return tuple(analyses)


# ─────────────────────────────────────────────────────────────
#  짝 검증 — 경로·순서·이름으로 묶지 않는다
# ─────────────────────────────────────────────────────────────


def _validated_pairs(
    items: Sequence[AudioSourceInput],
) -> list[tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]]:
    pairs: list[tuple[ResolvedMedia, PcmReference | UnavailableSourceAudio]] = []
    for position, item in enumerate(items):
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError(
                f"{position}번째 입력이 (ResolvedMedia, PcmReference|UnavailableSourceAudio)"
                f" 쌍이 아니다: {item!r}"
            )
        media, second = item
        if not isinstance(media, ResolvedMedia):
            raise ValueError(f"{position}번째 입력의 첫 원소가 ResolvedMedia가 아니다: {media!r}")
        if isinstance(second, UnavailableSourceAudio):
            _require_unavailable_pair(media, second, position)
        elif isinstance(second, PcmReference):
            _require_pair(media, second)
        else:
            raise ValueError(
                f"{position}번째 입력의 둘째 원소가 PCM 참조도 unavailable 기록도 아니다: "
                f"{second!r}"
            )
        pairs.append((media, second))
    return pairs


def _require_pair(media: ResolvedMedia, reference: PcmReference) -> None:
    """짝은 정확한 신원으로만 증명한다 — 경로·목록 위치·세그먼트 데이터가 아니다."""
    if media.asset_content_id != reference.source_id:
        raise ValueError(
            "미디어와 참조의 소스 신원이 다르다: "
            f"media.asset_content_id={media.asset_content_id!r}, "
            f"reference.source_id={reference.source_id!r}"
        )
    if media.original_sha256 != reference.original_sha256:
        raise ValueError(
            "미디어와 참조의 원본 해시가 다르다: "
            f"media={media.original_sha256!r}, reference={reference.original_sha256!r}"
        )
    _require_reference_shape(reference)


def _require_unavailable_pair(
    media: ResolvedMedia, unavailable: UnavailableSourceAudio, position: int
) -> None:
    if unavailable.resource_scoped_id != media.resource_scoped_id:
        raise ValueError(
            f"{position}번째 unavailable 기록의 자원 신원이 미디어와 다르다: "
            f"{unavailable.resource_scoped_id!r} != {media.resource_scoped_id!r}"
        )
    if unavailable.source_id != media.asset_content_id:
        raise ValueError(
            f"{position}번째 unavailable 기록의 소스 신원이 미디어와 다르다: "
            f"{unavailable.source_id!r} != {media.asset_content_id!r}"
        )
    if unavailable.original_sha256 != media.original_sha256:
        raise ValueError(
            f"{position}번째 unavailable 기록의 원본 해시가 미디어와 다르다: "
            f"{unavailable.original_sha256!r} != {media.original_sha256!r}"
        )


def _require_reference_shape(reference: PcmReference) -> None:
    """참조의 최소 스키마 — 호출 앞에서 거절한다. 파일·해시는 읽는 길에서 증명된다."""
    for name in ("source_id", "original_stem", "analysis_key", "pcm_sha256"):
        value = getattr(reference, name)
        if not isinstance(value, str) or not value:
            raise ValueError(f"참조의 {name}이 비지 않은 문자열이 아니다: {value!r}")
    for name in ("sample_rate", "channels", "sample_count"):
        value = getattr(reference, name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"참조의 {name}이 1 이상 정수가 아니다: {value!r}")


# ─────────────────────────────────────────────────────────────
#  기록 만들기 — 세그먼트 신원·덮개·SRT·설정 지문
# ─────────────────────────────────────────────────────────────


def _source_segments(
    reference: PcmReference, ranges: Sequence[SampleRange], segments: Sequence[Segment]
) -> tuple[SourceSegment, ...]:
    """레거시 세그먼트를 소스 시계의 기록으로 옮긴다 — 값은 고치지 않는다.

    세그먼트가 어느 조각에서 왔는지는 그 시작 시각을 덮는 조각으로 정한다.
    길이 0·뒤집힌 유한 기록도 그대로 남는다(규칙 9) — 접는 것은 레거시 엔진의 몫이다.
    """
    if not ranges:
        return ()
    starts = [float(Fraction(span.start, reference.sample_rate)) for span in ranges]
    origin = reference.pcm_sample0_file_time
    records: list[SourceSegment] = []
    for segment in segments:
        chunk_index = max(0, bisect_right(starts, segment.start) - 1)
        span = ranges[chunk_index]
        audio_start = Fraction(str(segment.start))
        audio_end = Fraction(str(segment.end))
        records.append(
            SourceSegment(
                segment_id=_segment_id(reference.analysis_key, chunk_index, segment.id, span),
                audio_start=audio_start,
                audio_end=audio_end,
                file_start=audio_start + origin,
                file_end=audio_end + origin,
                text=segment.text,
                compression_ratio=segment.compression_ratio,
                avg_logprob=segment.avg_logprob,
                no_speech_prob=segment.no_speech_prob,
            )
        )
    return tuple(records)


def _segment_id(analysis_key: str, chunk_index: int, legacy_id: int, span: SampleRange) -> str:
    """세그먼트 신원 — 분석 키·조각 번호·레거시 id·정확한 조각 범위의 해시."""
    material = "\n".join(
        [
            _SEGMENT_ID_DOMAIN,
            f"analysis_key={analysis_key}",
            f"chunk_index={chunk_index}",
            f"legacy_segment_id={legacy_id}",
            f"source_range={span.start}/{span.end}",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _empty_trace(
    reference: PcmReference,
    cfg: Config,
    artifact_dir: Path,
    namespace: Path,
    coverage: tuple[ChunkCoverage, ...],
) -> SourceTrace:
    """빈 성공 — 결정적인 빈 SRT와 그 해시를 낸다. 조용한 실패가 아니다."""
    srt = output.save_srt([], namespace / f"{reference.original_stem}.SOURCE.srt")
    log.info(f"소스 전사 완료: {reference.original_stem} · 빈 (무음의 증거는 아니다)")
    return _trace(
        reference,
        cfg,
        artifact_dir,
        status=_STATUS_EMPTY,
        draft=(),
        settled=(),
        repairs=(),
        cues=(),
        coverage=coverage,
        srt=srt,
        error=None,
    )


def _error_trace(
    reference: PcmReference,
    cfg: Config,
    artifact_dir: Path,
    *,
    error: str,
    draft: tuple[SourceSegment, ...],
    coverage: tuple[ChunkCoverage, ...],
) -> SourceTrace:
    """실패 기록 — 실제 참조의 신원을 그대로 담고 완성물은 내놓지 않는다."""
    return _trace(
        reference,
        cfg,
        artifact_dir,
        status=_STATUS_ERROR,
        draft=draft,
        settled=(),
        repairs=(),
        cues=(),
        coverage=coverage,
        srt=None,
        error=error,
    )


def _trace(
    reference: PcmReference,
    cfg: Config,
    artifact_dir: Path,
    *,
    status: str,
    draft: tuple[SourceSegment, ...],
    settled: tuple[SourceSegment, ...],
    repairs: tuple[Repair, ...],
    cues: tuple[Cue, ...],
    coverage: tuple[ChunkCoverage, ...],
    srt: Path | None,
    error: str | None,
) -> SourceTrace:
    return SourceTrace(
        variant="source_trace",
        source_id=reference.source_id,
        original_stem=reference.original_stem,
        analysis_key=reference.analysis_key,
        pcm_analysis_key=reference.analysis_key,
        pcm_sample0_file_time=reference.pcm_sample0_file_time,
        status=status,  # type: ignore[arg-type]
        draft=draft,
        settled_spine=settled,
        repairs=repairs,
        cues=cues,
        coverage=coverage,
        model=cfg.transcribe_model,
        config_sha256=_config_sha256(cfg),
        source_srt_relative_path=(
            srt.relative_to(artifact_dir).as_posix() if srt is not None else None
        ),
        source_srt_sha256=sha256_file(srt) if srt is not None else None,
        error=error,
    )


def _config_sha256(cfg: Config) -> str:
    """설정 스냅샷의 지문 — 같은 설정은 같은 지문을 낸다."""
    body = json.dumps(cfg.to_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()
