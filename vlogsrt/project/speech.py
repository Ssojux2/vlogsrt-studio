"""소스 전사 어댑터 — 유일 소스마다 명시적 네임스페이스로 엔진을 돈다 (P3-02).

계획("Source analysis, clocks, identity")의 정본:

- **소스 하나는 내용이다.** 재사용 신원은 `analysis_key`(원본 해시·정책·원점·
  PCM 해시·채널·주파수)이고, 경로·이름·XML 국소 resource id는 신원이 아니다.
  같은 내용의 반복 인스턴스는 한 번만 전사하고 같은 기록을 소비한다.
- **엔진은 레거시 그대로다.** 시각과 세그먼트 경계는 whisper 응답에서만 나온다
  (규칙 1). 게이트를 통과한 세그먼트의 글자만 두 번째 모델의 것으로 갈아낸다
  (D37·D39) — 파생 계층도 이제 하이브리드고, 두 번째 모델은 1차 전사와 **같은
  조각 경계**를 보며 죽으면 whisper 글자로 나간다. 이 모듈이 새로 만드는 것은
  조각의 좌표(저장 PCM의 정확한 샘플)와 실패의 상태뿐이다.
  `parse_segments`·`build_spine`·`repair.settle`·`refine.refine`의 float 호환은
  그대로 쓴다.
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
import os
import tempfile
from bisect import bisect_right
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any, Literal, TypeAlias

from vlogsrt import log, output, pipeline, refine, repair, retext, transcribe
from vlogsrt.config import Config
from vlogsrt.models import Chunk, Cue, Repair, RetextRecord, Segment, Span
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

_SOURCE_KEY_DOMAIN = "vlogsrt-source-analysis-v2"
"""소스 분석 키의 도메인 구분자 — PCM 키와 어댑터 스키마를 갈라 붙인다.

v2(D39): 글자 교정이 파생 계층에 켜졌다. 설정 지문은 `cfg.to_dict()` 전체 해시라
`retext_*`가 이미 들어 있지만 값이 그대로인 채 동작만 켜지면 옛 whisper 글자
완성물이 같은 키로 재사용된다 — 도메인을 올려 키 자체를 갈라놓는다."""

_WAV_HEADER_BYTES = 44
"""내는 WAV 헤더의 바이트 수 — 바이트 한도는 파일 전체로 잰다(포맷의 사실)."""

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
    # 글자 교정 기록(D39). 선택 필드다 — None은 이 단계에 도달하지 못했다는 뜻이다.
    retext: RetextRecord | None = None

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

    조각 수는 두 한도가 정하고, 경계는 목표 지점 주위의 가장 조용한 창
    (`quiet_sample`)에서 고른 뒤 정확한 샘플로 자른다. **모든 조각이 두 한도를
    지킨다** — 조용한 창이 경계를 끌고 가서 남는 조각이 한도를 넘는 일이 없게,
    경계마다 남은 조각들 몫의 프레임을 남겨 둔다. 합치면 언제나 `[0, sample_count)`다
    (규칙 4). 바이트 한도는 실제 내는 WAV 바이트(헤더 포함)로 잰다.
    """
    frames = reference.sample_count
    if frames < 1:
        raise ValueError(f"저장 프레임이 없다: {frames!r} — 덮을 오디오가 없다")
    rate = reference.sample_rate
    max_len = _max_range_frames(reference, cfg)
    count = max(1, -(-frames // max_len))
    search = max(0, round(cfg.cut_search_seconds * rate))
    window = min(frames, max(1, round(cfg.cut_quiet_window_ms * rate / 1000)))
    cuts: list[int] = []
    previous = 0
    for index in range(1, count):
        target = frames * index // count
        remaining = count - index
        lo = max(previous + 1, frames - max_len * remaining)
        hi = min(previous + max_len, frames - remaining)
        if lo > hi:
            raise ValueError(
                f"조각 한도를 채울 수 없다: {frames}프레임을 한도 {max_len}프레임인 "
                f"{count}조각으로 못 나눈다 ({index}번째 경계 [{lo}, {hi}])"
            )
        cuts.append(_bounded_cut(reference, target, search, window, lo, hi))
        previous = cuts[-1]
    edges = [0, *cuts, frames]
    return tuple(SampleRange(start, end) for start, end in pairwise(edges))


def _max_range_frames(reference: PcmReference, cfg: Config) -> int:
    """한 조각이 담을 수 있는 최대 프레임 — 초 한도와 실제 WAV 바이트 한도 중 작은 쪽.

    바이트 한도는 내는 파일 전체(44바이트 헤더 포함)로 잰다 — payload만 세면 매
    조각이 헤더만큼 한도를 넘는다. 한도가 헤더·한 프레임조차 못 담으면 명시적으로
    죽는다: 조용히 한 조각을 통째로 내는 길이 될 수 없기 때문이다.
    """
    if cfg.chunk_max_bytes <= _WAV_HEADER_BYTES:
        raise ValueError(
            f"chunk_max_bytes가 WAV 헤더({_WAV_HEADER_BYTES}바이트)보다 작다: "
            f"{cfg.chunk_max_bytes!r} — 조각 하나를 못 만든다"
        )
    frame_bytes = _SAMPLE_WIDTH * reference.channels
    by_bytes = (cfg.chunk_max_bytes - _WAV_HEADER_BYTES) // frame_bytes
    if by_bytes < 1:
        raise ValueError(
            f"chunk_max_bytes가 한 프레임({frame_bytes}바이트)도 못 담는다: {cfg.chunk_max_bytes!r}"
        )
    if cfg.chunk_max_seconds > 0:
        by_seconds = int(cfg.chunk_max_seconds * reference.sample_rate)
        if by_seconds < 1:
            raise ValueError(
                f"chunk_max_seconds가 한 프레임(1/{reference.sample_rate}초)도 못 담는다: "
                f"{cfg.chunk_max_seconds!r} — 조각 하나를 못 만든다"
            )
        return min(by_bytes, by_seconds)
    return by_bytes


def _bounded_cut(
    reference: PcmReference,
    target: int,
    search: int,
    window: int,
    lo: int,
    hi: int,
) -> int:
    """조용한 창의 경계 후보를 한도가 허락하는 범위로 묶는다.

    후보(조용한 창)·목표(균등 분할) 순서로 범위 안의 것을 고르고, 둘 다 밖에
    있으면 가장 가까운 끝에 붙인다. 자를 자리를 못 고르는 것이 구간을 잃을 이유가
    되지 못한다(규칙 4). 말인지 침묵인지는 판단하지 않는다.
    """
    candidate = _quiet_cut(reference, target, search, window)
    if lo <= candidate <= hi:
        return candidate
    if lo <= target <= hi:
        return target
    return min(max(target, lo), hi)


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
    """이미 완성된 단계의 증거 — 실패 기록에도 완성된 것은 버리지 않는다.

    초안·덮개는 1차 전사 뒤에, 골격·재전사 기록은 settle 뒤에, 교정 기록은 settle 뒤
    큐 앞에서, 큐는 refine 뒤에 채워진다. 실패 시점에 존재하는 단계만큼만 error 기록에
    남는다 — 지어내지도 조금히 지우지도 않는다.
    """

    ranges: tuple[SampleRange, ...] = ()
    chunks: tuple[Chunk, ...] = ()  # 두 번째 모델이 같은 경계를 봐야 한다(D39)
    draft: tuple[Segment, ...] = ()
    coverage: tuple[ChunkCoverage, ...] = ()
    settled: tuple[Segment, ...] = ()
    repairs: tuple[Repair, ...] = ()
    cues: tuple[Cue, ...] = ()
    retext: RetextRecord | None = None

    def evidence(self, plan: _SourcePlan) -> tuple[SourceSegment, ...]:
        if not self.draft:
            return ()
        return _source_segments(plan.reference, plan.source_key, self.ranges, self.draft)

    def settled_evidence(self, plan: _SourcePlan) -> tuple[SourceSegment, ...]:
        if not self.settled:
            return ()
        return _source_segments(plan.reference, plan.source_key, self.ranges, self.settled)


@dataclass(frozen=True, slots=True)
class _SourcePlan:
    """소스 하나의 이 번 분석 자리 — 참조·설정·분석 키·경로가 다닌다."""

    reference: PcmReference
    cfg: Config
    source_key: str
    artifact_dir: Path
    namespace: Path

    @property
    def scratch(self) -> Path:
        return self.namespace / _SCRATCH_DIR / self.source_key


def transcribe_source(
    media: ResolvedMedia,
    reference: PcmReference,
    cfg: Config,
    transcriber: Transcriber,
    artifact_dir: Path,
) -> SourceTrace:
    """검증된 짝 하나를 레거시 엔진 전체로 전사한다. 예외를 밖으로 새지 않는다.

    순서: 조각 계획 → 1차 전사(P3-01 결과형) → `repair.settle`(주입 커터) →
    불변 골격 → 두 번째 모델 조각 응답(같은 경계, D39) → `retext.plan` →
    `pipeline.spine_to_cues` → `pipeline.apply_retext` → `refine.refine`(polish 포함,
    한 번) → 소스 로컬 SRT. 실패는 언제나 **실제 참조의 신원을 가진 error 기록**이다
    — unavailable로 강등되지 않고 완성물도 나오지 않는다. 두 번째 모델 호출만은
    예외다: 죽어도 whisper 글자로 완성물이 나간다(D37과 같은 선).
    """
    _require_pair(media, reference)
    plan = _SourcePlan(
        reference=reference,
        cfg=cfg,
        source_key=_source_analysis_key(reference, cfg),
        artifact_dir=artifact_dir,
        namespace=artifact_dir / media.asset_content_id,
    )
    capture = _Capture()
    progress = _Progress()
    try:
        return _analyze(plan, _CapturingTranscriber(transcriber, capture), capture, progress)
    except Exception as exc:  # noqa: BLE001 - 상태로 바꿔 남긴다. 조용히 두지는 않는다
        capture.record("소스 엔진", exc)
        return _error_trace(plan, progress, error=capture.describe())


def _analyze(
    plan: _SourcePlan,
    wrapped: _CapturingTranscriber,
    capture: _Capture,
    progress: _Progress,
) -> SourceTrace:
    """1차 전사의 상태를 갈라 보내고, 초안이 있으면 재전사·조판·SRT로 간다."""
    outcome = _first_pass(plan, wrapped, progress)
    if outcome.status == _STATUS_ERROR:
        return _error_trace(plan, progress, error=outcome.error or "전사가 실패로 돌아왔다")
    progress.draft = outcome.segments
    if outcome.status == _STATUS_EMPTY:
        return _empty_trace(plan, progress.coverage)
    if not progress.draft:
        # 걸러져 빈 성공은 쓸 초안이 없는 것이다(EmptyTranscript) — 명시적 빈과
        # 갈라진다. 무음의 증거가 아니므로 빈 성공으로 내주지 않는다.
        return _error_trace(
            plan,
            progress,
            error="조립 가능한 초안이 하나도 없습니다 — 공백 글자만 걸러져 빈 성공이다",
        )
    return _settled_trace(plan, wrapped, capture, progress)


def _first_pass(
    plan: _SourcePlan,
    wrapped: _CapturingTranscriber,
    progress: _Progress,
) -> transcribe.TranscriptionOutcome:
    """조각을 만들어 1차 전사를 부른다. 완성된 증거는 progress에 남긴다."""
    progress.ranges = plan_source_ranges(plan.reference, plan.cfg)
    progress.chunks = _initial_chunks(plan.reference, progress.ranges, plan.scratch)
    job = TranscriptionJob(
        stem=plan.reference.original_stem,
        chunks=progress.chunks,
        namespace=plan.namespace,
    )
    outcome = transcribe.transcribe_outcome(wrapped, job, plan.cfg)
    progress.coverage = outcome.coverage
    return outcome


def _settled_trace(
    plan: _SourcePlan,
    wrapped: _CapturingTranscriber,
    capture: _Capture,
    progress: _Progress,
) -> SourceTrace:
    """재전사(주입 커터) → 불변 골격 → 글자 교정 계획. 포획 실패가 있으면 여기서 끝난다."""
    reference = plan.reference
    duration = float(Fraction(reference.sample_count, reference.sample_rate))
    settled = repair.settle(
        list(progress.draft),
        repair.Source(
            stem=reference.original_stem,
            duration=duration,
            namespace=plan.namespace,
            cutter=_repair_cutter(reference, plan.scratch, capture),
        ),
        plan.cfg,
        wrapped,
    )
    progress.settled = tuple(settled.spine)
    progress.repairs = settled.repairs
    if capture.failures:
        return _error_trace(plan, progress, error=capture.describe())
    record = _plan_retext(plan, wrapped, progress, settled, duration)
    progress.retext = record
    return _published_trace(plan, progress, settled, duration, record)


def _plan_retext(
    plan: _SourcePlan,
    wrapped: _CapturingTranscriber,
    progress: _Progress,
    settled: repair.Settled,
    duration: float,
) -> RetextRecord:
    """settle 뒤 큐 앞에서 세그먼트마다 갈 글자를 정한다(D37·D39).

    두 번째 모델은 1차 전사와 **같은 조각**을 다시 본다 — 두 모델이 같은 경계를 봐야
    대조가 성립한다. 호출이 죽어도 완성물은 whisper 글자로 나간다(레거시
    `pipeline.run`과 같은 선): 실패는 `unavailable` 기록과 WARN으로 남고 오류가
    아니다. 포획 목록에 남은 이 실패를 오류로 세는 길은 없다 — 이미 기록했다.
    """
    if not plan.cfg.retext_enabled:
        return retext.switched_off()
    job = TranscriptionJob(
        stem=plan.reference.original_stem,
        chunks=progress.chunks,
        namespace=plan.namespace,
        kind=VERIFY_KIND,
    )
    try:
        responses = wrapped.transcribe(job, plan.cfg)
    except Exception as exc:  # noqa: BLE001 - 두 번째 모델이 죽어도 whisper 자막은 나온다
        record = retext.unavailable(f"{exc.__class__.__name__}: {exc}")
        pipeline.report_retext(record)
        return record
    chunks = [
        retext.ChunkText(response.index, response.offset, transcribe.parse_text(response.payload))
        for response in responses
    ]
    record = retext.plan(settled.spine, chunks, duration, settled.repairs, plan.cfg)
    pipeline.report_retext(record)
    return record


def _published_trace(
    plan: _SourcePlan,
    progress: _Progress,
    settled: repair.Settled,
    duration: float,
    record: RetextRecord,
) -> SourceTrace:
    """불변 골격 → 큐 → 교정 → 정제 → 소스 로컬 SRT. polish은 refine 안에서 한 번뿐이다."""
    cues = refine.refine(
        pipeline.apply_retext(pipeline.spine_to_cues(list(settled.spine)), record),
        plan.cfg,
        duration,
    )
    progress.cues = tuple(cues)
    stem = plan.reference.original_stem
    if not cues:
        return _error_trace(plan, progress, error="정제를 끝내고 큐가 하나도 남지 않았다")
    srt = _publish_srt(plan, output.write_srt(cues).encode("utf-8"))
    if srt is None:
        return _error_trace(
            plan,
            progress,
            error=(
                f"완성물이 이미 다른 바이트로 있다: {plan.source_key}/{stem}.SOURCE.srt — "
                "있는 것을 보존하고 이 번 결과를 버린다"
            ),
        )
    log.info(f"소스 전사 완료: {stem} · 큐 {len(cues)}장")
    return _trace(
        plan,
        status=_STATUS_SUCCESS,
        draft=progress.evidence(plan),
        settled=progress.settled_evidence(plan),
        repairs=progress.repairs,
        cues=progress.cues,
        coverage=progress.coverage,
        srt=srt,
        error=None,
        retext_record=record,
    )


def transcribe_unique_sources(
    items: Sequence[AudioSourceInput],
    cfg: Config,
    transcriber: Transcriber,
    artifact_dir: Path,
) -> tuple[SourceAnalysis, ...]:
    """입력 쌍을 유일 분석으로 정규화해 돌려준다 — 신원은 소스 분석 키다.

    모든 쌍의 신원·삼태·스키마를 **호출 앞에서 전부** 검사한다: 하나라도 어긋나면
    유료 호출은 깨진 입력에 값을 쓰지 않는다. 실제 참조는 소스 키마다, unavailable
    기록은 자원마다 **정확히 한 번** 처음 보인 순서로 돌아간다 — 반복 입력이 같은
    출력을 늘리지 않으므로 소비자는 중복을 정규화하지 않고 거부하면 된다.
    """
    pairs = _validated_pairs(items)
    analyses: list[SourceAnalysis] = []
    by_analysis: dict[str, SourceTrace] = {}
    by_resource: dict[str, UnavailableSourceAudio] = {}
    for media, second in pairs:
        if isinstance(second, UnavailableSourceAudio):
            if second.resource_scoped_id not in by_resource:
                by_resource[second.resource_scoped_id] = second
                analyses.append(second)
            continue
        source_key = _source_analysis_key(second, cfg)
        if source_key not in by_analysis:
            trace = transcribe_source(media, second, cfg, transcriber, artifact_dir)
            by_analysis[source_key] = trace
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
    if not media.has_audio:
        raise ValueError(
            "has_audio=False로 선언된 미디어에 실제 PCM 참조가 왔다 — 선언된 삼태와 어긋난다"
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
    declared = unavailable.status == "no_audio"
    if media.has_audio and declared:
        raise ValueError(
            f"{position}번째 기록: has_audio=True인 미디어에 선언된 무음(no_audio)이 왔다 — "
            "삼태는 그 미디어의 선언과 같아야 한다"
        )
    if not media.has_audio and not declared:
        raise ValueError(
            f"{position}번째 기록: has_audio=False인 미디어에 pre-PCM 오류 기록이 왔다 — "
            "선언된 무음(no_audio)만 받는다"
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
    reference: PcmReference,
    source_key: str,
    ranges: Sequence[SampleRange],
    segments: Sequence[Segment],
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
                segment_id=_segment_id(source_key, chunk_index, segment.id, span),
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


def _publish_srt(plan: _SourcePlan, body: bytes) -> Path | None:
    """소스 로컬 SRT를 독점 출판한다. 충돌이면 None — 있는 바이트를 보존한다.

    같은 분석 키의 완성물은 같은 자리에 산다. 다른 바이트로 덮어쓰는 길은 없다:
    이미 있던 것과 바이트가 같으면 재사용이고, 다르면 있는 것을 보존한 채 오류다.
    """
    stem = plan.reference.original_stem
    return _publish_exclusive(plan.namespace / plan.source_key / f"{stem}.SOURCE.srt", body)


def _publish_exclusive(destination: Path, body: bytes) -> Path | None:
    """완전한 파일을 원자적으로 독점 생성한다 — 조사 후 덮어쓰는 경주가 아니다.

    임시 이름에 전부 쓴 뒤 `os.link`로 옮겨 닮는다. 링크는 이미 자리가 차 있으면
    실패하므로, 반쯤 쓴 파일이 완성물 자리에 나타나는 일이 없다. 임시 파일은
    이 번 호출의 것을 치우는 것뿐 — 완성물을 치우는 길은 없다.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(
        dir=destination.parent, prefix=f".{destination.name}.", suffix=".tmp"
    )
    temporary = Path(name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(body)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return destination if destination.read_bytes() == body else None
        return destination
    finally:
        temporary.unlink(missing_ok=True)


def _empty_trace(plan: _SourcePlan, coverage: tuple[ChunkCoverage, ...]) -> SourceTrace:
    """빈 성공 — 결정적인 빈 SRT와 그 해시를 낸다. 조용한 실패가 아니다."""
    stem = plan.reference.original_stem
    srt = _publish_srt(plan, b"")
    if srt is None:
        return _error_trace(
            plan,
            _Progress(coverage=coverage),
            error=f"완성물이 이미 다른 바이트로 있다: {plan.source_key}/{stem}.SOURCE.srt",
        )
    log.info(f"소스 전사 완료: {stem} · 빈 (무음의 증거가 아니다)")
    return _trace(
        plan,
        status=_STATUS_EMPTY,
        draft=(),
        settled=(),
        repairs=(),
        cues=(),
        coverage=coverage,
        srt=srt,
        error=None,
        retext_record=None,
    )


def _error_trace(plan: _SourcePlan, progress: _Progress, *, error: str) -> SourceTrace:
    """실패 기록 — 실제 참조 신원과 **이미 완성된 단계**를 그대로 담는다.

    초안은 물론 settle이 끝났으면 골격·재전사 기록까지, 교정 계획이 섰으면 그
    기록까지, refine이 끝났으면 큐까지 남는다. 완성물(SRT 경로·해시)만은 내놓지
    않는다 — 실패를 성공으로 꾸미지 않는다는 선과, 실패 때문에 증거를 버린다는
    함정을 함께 끊는다.
    """
    return _trace(
        plan,
        status=_STATUS_ERROR,
        draft=progress.evidence(plan),
        settled=progress.settled_evidence(plan),
        repairs=progress.repairs,
        cues=progress.cues,
        coverage=progress.coverage,
        srt=None,
        error=error,
        retext_record=progress.retext,
    )


def _trace(
    plan: _SourcePlan,
    *,
    status: str,
    draft: tuple[SourceSegment, ...],
    settled: tuple[SourceSegment, ...],
    repairs: tuple[Repair, ...],
    cues: tuple[Cue, ...],
    coverage: tuple[ChunkCoverage, ...],
    srt: Path | None,
    error: str | None,
    retext_record: RetextRecord | None,
) -> SourceTrace:
    reference = plan.reference
    return SourceTrace(
        variant="source_trace",
        source_id=reference.source_id,
        original_stem=reference.original_stem,
        analysis_key=plan.source_key,
        pcm_analysis_key=reference.analysis_key,
        pcm_sample0_file_time=reference.pcm_sample0_file_time,
        status=status,  # type: ignore[arg-type]
        draft=draft,
        settled_spine=settled,
        repairs=repairs,
        cues=cues,
        coverage=coverage,
        model=plan.cfg.transcribe_model,
        config_sha256=_config_sha256(plan.cfg),
        source_srt_relative_path=(
            srt.relative_to(plan.artifact_dir).as_posix() if srt is not None else None
        ),
        source_srt_sha256=sha256_file(srt) if srt is not None else None,
        error=error,
        retext=retext_record,
    )


def _source_analysis_key(reference: PcmReference, cfg: Config) -> str:
    """소스 분석 키 — PCM 키에 이 분석을 정하는 소스 설정을 더해 파생한다.

    키 재료는 도메인 구분자·PCM 분석 키·설정 지문이다. 설정 지문은 모델과 모든
    임계값을 담으므로 같은 PCM에 다른 설정은 다른 분석이고, 다른 출판 자리를 얻는다.
    완성물·스크래치·세그먼트 신원·중복 제거가 전부 이 키를 따르고, 픽스처 뿌리는
    애셋(`asset_content_id`)에만 남는다.
    """
    material = "\n".join(
        [
            _SOURCE_KEY_DOMAIN,
            f"pcm_analysis_key={reference.analysis_key}",
            f"config_sha256={_config_sha256(cfg)}",
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def _config_sha256(cfg: Config) -> str:
    """설정 스냅샷의 지문 — 같은 설정은 같은 지문을 낸다."""
    body = json.dumps(cfg.to_dict(), ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode()).hexdigest()
