"""A5 검토용 자막 투영 — 잰 발화 증거에서 `.review.vtt`까지의 순수 계산.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §10.2·§10.3·§13.3·16-A5. 입력은
**이미 측정된** 내부 전사 증거(`understanding.UtteranceEvidence`와 구조가 같으면
무엇이든)와 검토 리비전의 조각뿐이다 — 이 모듈은 전사를 다시 부르지 않고,
문장을 다시 쓰지 않고, 단어 시각을 균등 배분하지 않는다.

지키는 선(§10.2):

- **좌표 변환은 딱 두 번이다.** 선택 PCM 원점은 `timed_utterances`에서 파일
  좌표로 **한 번**, 클립 시작은 `project_review_vtt`에서 클립 국소 좌표로
  **한 번** — 각각 정확 유리수이고 겹쳐 적용되지 않는다(§13.3).
- **측정된 증거만 큐가 된다.** 발화 하나가 큐 하나다. 시각 증거가 없으면 큐가
  없고, 없는 이유(`unavailable`·`failure`·`noaudio`·`unconfirmed_empty`)가
  서로 다른 기록으로 남는다(§7.3). 경계에 걸린 큐는 클립 안으로 잘리고
  '경계 전사 확인' 표시가 남는다 — 문장을 고쳐 쓰지 않는다.
- **검토용일 뿐이다.** 최종 자막 파이프라인·API·ffmpeg·파일 쓰기는 없다.
  표시 모드 토글과 아무 상관이 없다(§10.3).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Protocol, runtime_checkable

from vlogsrt.project.raw.segment_review import (
    SPEECH_DETECTED,
    ReviewRevision,
    ReviewSegment,
    SourceClock,
)
from vlogsrt.project.timebase import milliseconds_half_up

REVIEW_VTT_HEADER = "WEBVTT"
"""WebVTT 문서 머리 — 검토용 문서는 이 한 줄로 시작한다."""

TRANSCRIPT_STATUS_PREFIX = "transcript_status:"
"""발화 상태 기록의 접두어 — 네 상태·무증거가 각각 다른 문자열이 된다(§7.3)."""

BOUNDARY_REVIEW_NOTE = "boundary_transcript_review"
"""경계에 걸려 잘린 큐가 있다는 표시 — 문장 재작성 없이 검토만 남긴다(§10.2)."""

NO_TIMED_EVIDENCE = "no_timed_evidence"
"""상태는 발화 감지인데 시각 붙은 증거가 하나도 없다 — 큐를 지어내지 않는다."""

DEGENERATE_MS_FLOOR_PREFIX = "degenerate_ms_floor:"
"""밀리초 양자화에서 길이가 0이 된 큐 — 표시용으로 1ms 바닥을 깔고 기록에 남긴다."""

ZERO_LENGTH_PREFIX = "zero_length_utterance:"
"""길이 0으로 잰 발화 — 큐가 못 되고 글자는 기록에 남는다(규칙 9의 정신)."""


class ProjectionError(RuntimeError):
    """투영 계약 위반 — 메시지가 조각·클립·좌표를 말한다."""


@runtime_checkable
class UtteranceLike(Protocol):
    """측정된 발화 증거의 구조 계약 — `UtteranceEvidence`가 import 없이 통과한다.

    `start`·`end`는 **정규화 분석 축**의 초(정확 유리수)다. 파일 좌표로 가는
    원점 변환은 이 모듈의 `timed_utterances`가 **한 번**만 한다.
    """

    utterance_id: str
    text: str
    start: Fraction
    end: Fraction
    evidence_id: str


@dataclass(frozen=True, slots=True)
class TimedUtterance:
    """파일 좌표의 발화 — 시각은 선택 PCM의 잰 원점이 이미 더해진 초다."""

    utterance_id: str
    text: str
    start_file: Fraction
    end_file: Fraction
    evidence_id: str


def timed_utterances(
    evidence: Iterable[UtteranceLike], pcm_sample0_file_time: Fraction
) -> tuple[TimedUtterance, ...]:
    """정규화 축 증거를 파일 좌표로 — `pcm_sample0_file_time`를 딱 한 번 더한다.

    원점 차이를 두 번 이상 적용하면 A/V 싱크가 겹쳐 흐른다(§13.3). 이 함수가
    그 변환의 유일한 자리다.
    """
    return tuple(
        TimedUtterance(
            utterance_id=item.utterance_id,
            text=item.text,
            start_file=pcm_sample0_file_time + item.start,
            end_file=pcm_sample0_file_time + item.end,
            evidence_id=item.evidence_id,
        )
        for item in evidence
    )


@dataclass(frozen=True, slots=True)
class ProjectionClock:
    """발화 파일축과 원본 PTS 축을 잇는 다리 — 틱→초 환산의 정본.

    `file_zero_pts`는 발화 파일축 0초가 앉은 원본 PTS(틱, 정확 유리수)다.
    A/V 기준 오프셋이 필요하면 이 값 하나로 표현된다(§10.2).
    """

    time_base_num: int
    time_base_den: int
    file_zero_pts: Fraction

    @property
    def time_base(self) -> Fraction:
        return Fraction(self.time_base_num, self.time_base_den)


@dataclass(frozen=True, slots=True)
class ClipWindow:
    """투영의 창 — 실제로 뽑힌 클립의 원본 좌표 범위(핸들이면 core보다 넓다, §6.5)."""

    clip_id: str
    source_id: str
    media_start_pts: int
    media_end_pts: int


@dataclass(frozen=True, slots=True)
class ReviewVttCue:
    """검토용 큐 하나 — 시각은 클립 국소 초(정확 유리수), 글자는 증거 그대로다."""

    start: Fraction
    end: Fraction
    text: str
    boundary_clipped: bool
    utterance_id: str


@dataclass(frozen=True, slots=True)
class ReviewVttResult:
    """조각 하나의 검토용 자막 — 큐와 상태 기록. 표시 모드와 무관하다(§10.3)."""

    segment_id: str
    clip_id: str
    source_id: str
    language: str
    cues: tuple[ReviewVttCue, ...]
    status_notes: tuple[str, ...]


def project_review_vtt(
    revision: ReviewRevision,
    segment_id: str,
    clip: ClipWindow,
    utterances: Sequence[TimedUtterance],
    clock: ProjectionClock,
    *,
    language: str = "",
) -> ReviewVttResult:
    """측정된 발화를 조각의 검토용 큐로 투영한다 — 큐는 발화 수와 같거나 적다.

    상태가 `speech_detected`가 아니면 큐 없이 이유만 남는다. 발화 하나가 큐
    하나고 시각은 그 발화의 잰 값 그대로다 — 요약·균등 분할·단어 시각
    지어내기가 없다.
    """
    segment = _review_segment(revision, segment_id)
    source_clock = _require_source_clock(revision, segment, clip)
    if (clock.time_base_num, clock.time_base_den) != (
        source_clock.time_base_num,
        source_clock.time_base_den,
    ):
        raise ProjectionError(
            f"투영 시계 timebase {clock.time_base_num}/{clock.time_base_den}가 원본 "
            f"{source_clock.time_base_num}/{source_clock.time_base_den}와 다르다"
        )
    _require_clip_window(source_clock, clip)
    if segment.speech_status != SPEECH_DETECTED:
        return ReviewVttResult(
            segment_id=segment.segment_id,
            clip_id=clip.clip_id,
            source_id=segment.source_id,
            language=language,
            cues=(),
            status_notes=(f"{TRANSCRIPT_STATUS_PREFIX}{segment.speech_status}",),
        )
    return _project_cues(segment, clip, utterances, clock, language)


def _review_segment(revision: ReviewRevision, segment_id: str) -> ReviewSegment:
    for segment in revision.segments:
        if segment.segment_id == segment_id:
            return segment
    raise ProjectionError(f"리비전 {revision.revision_id!r}에 조각 {segment_id!r}가 없다")


def _require_source_clock(
    revision: ReviewRevision, segment: ReviewSegment, clip: ClipWindow
) -> SourceClock:
    if segment.source_id != clip.source_id:
        raise ProjectionError(
            f"클립 {clip.clip_id!r}은 원본 {clip.source_id!r}인데 조각은 "
            f"{segment.source_id!r}다 — 다른 원본끼리 투영하지 않는다"
        )
    source_clock = revision.clock_for(segment.source_id)
    if source_clock is None:
        raise ProjectionError(f"원본 {segment.source_id!r}의 시계가 리비전에 없다")
    return source_clock


def _require_clip_window(source_clock: SourceClock, clip: ClipWindow) -> None:
    if clip.media_start_pts >= clip.media_end_pts:
        raise ProjectionError(
            f"클립 {clip.clip_id!r}: media 범위가 뒤집혔다 "
            f"[{clip.media_start_pts}, {clip.media_end_pts})"
        )
    for pts in (clip.media_start_pts, clip.media_end_pts):
        if not source_clock.covers(pts) or not source_clock.is_frame_boundary(pts):
            raise ProjectionError(
                f"클립 {clip.clip_id!r}: {pts}가 원본 {source_clock.source_id!r}의 격자·범위 밖이다"
            )


def _project_cues(
    segment: ReviewSegment,
    clip: ClipWindow,
    utterances: Sequence[TimedUtterance],
    clock: ProjectionClock,
    language: str,
) -> ReviewVttResult:
    """감지된 발화의 클립 국소 큐 — 자르기는 창 경계에서만, 글자는 못 고친다."""
    offset = (clock.file_zero_pts - Fraction(clip.media_start_pts)) * clock.time_base
    duration = Fraction(clip.media_end_pts - clip.media_start_pts) * clock.time_base
    cues: list[ReviewVttCue] = []
    notes: list[str] = []
    for item in sorted(utterances, key=lambda unit: (unit.start_file, unit.utterance_id)):
        note, cue = _clip_utterance(item, offset, duration)
        if note is not None:
            notes.append(note)
        if cue is not None:
            cues.append(cue)
    if any(cue.boundary_clipped for cue in cues):
        notes.append(BOUNDARY_REVIEW_NOTE)
    if not utterances:
        notes.insert(0, f"{TRANSCRIPT_STATUS_PREFIX}{NO_TIMED_EVIDENCE}")
    return ReviewVttResult(
        segment_id=segment.segment_id,
        clip_id=clip.clip_id,
        source_id=segment.source_id,
        language=language,
        cues=tuple(cues),
        status_notes=tuple(notes),
    )


def _clip_utterance(
    item: TimedUtterance, offset: Fraction, duration: Fraction
) -> tuple[str | None, ReviewVttCue | None]:
    """발화 하나를 클립 국소로 자른다 — 창 밖은 큐 없음, 길이 0은 기록만 남는다."""
    local_start = item.start_file + offset
    local_end = item.end_file + offset
    visible_start = max(local_start, Fraction(0))
    visible_end = min(local_end, duration)
    if local_start >= duration or local_end <= 0:
        return None, None
    if visible_start >= visible_end:
        return f"{ZERO_LENGTH_PREFIX}{item.utterance_id}", None
    clipped = local_start < 0 or local_end > duration
    cue = ReviewVttCue(
        start=visible_start,
        end=visible_end,
        text=item.text,
        boundary_clipped=clipped,
        utterance_id=item.utterance_id,
    )
    return None, cue


# ─────────────────────────────────────────────────────────────
#  WebVTT 직렬화 — 이곳에서만 밀리초 양자화가 일어난다(§13.3)
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RenderedVttCue:
    """양자화가 끝난 큐 — 시각은 밀리초 정수고 줄은 이스케이프된 그대로다."""

    index: int
    start_ms: int
    end_ms: int
    lines: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RenderedVtt:
    """검토용 문서의 렌더 결과 — 본문·양자화 큐·직렬화가 남긴 기록."""

    text: str
    cues: tuple[RenderedVttCue, ...]
    notes: tuple[str, ...]


def render_review_vtt(result: ReviewVttResult) -> RenderedVtt:
    """큐를 WebVTT 본문으로 — half-up 양자화와 이스케이프가 여기서 딱 한 번."""
    rendered: list[RenderedVttCue] = []
    notes = list(result.status_notes)
    blocks = [REVIEW_VTT_HEADER, ""]
    for index, cue in enumerate(result.cues, start=1):
        start_ms = milliseconds_half_up(cue.start)
        end_ms = milliseconds_half_up(cue.end)
        if end_ms <= start_ms:
            end_ms = start_ms + 1
            notes.append(f"{DEGENERATE_MS_FLOOR_PREFIX}{cue.utterance_id}")
        lines = _escape_cue_text(cue.text)
        rendered.append(RenderedVttCue(index=index, start_ms=start_ms, end_ms=end_ms, lines=lines))
        blocks.append(str(index))
        blocks.append(f"{_format_vtt_ms(start_ms)} --> {_format_vtt_ms(end_ms)}")
        blocks.extend(lines)
        blocks.append("")
    return RenderedVtt(text="\n".join(blocks) + "\n", cues=tuple(rendered), notes=tuple(notes))


def serialize_review_vtt(result: ReviewVttResult) -> str:
    """검토용 `.review.vtt` 본문 — UTF-8 문자열, 개행은 언제나 `\\n`이다."""
    return render_review_vtt(result).text


def _format_vtt_ms(total_ms: int) -> str:
    """`HH:MM:SS.mmm` — 시각 자리는 두 개 이상(긴 영상에서 잘리지 않게)."""
    if total_ms < 0:
        raise ProjectionError(f"음수 시각은 WebVTT로 못 쓴다: {total_ms}ms")
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, millis = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{millis:03d}"


def _escape_cue_text(text: str) -> tuple[str, ...]:
    """큐 문장을 WebVTT 문법에 안전하게 — 마크업 문자는 실체로, 빈 줄은 버린다.

    `-->`는 `--&gt;`로 감춘다(레이아웃에서 `>`로 되돌아온다). 빈 줄은 큐 문법이
    못 실으니 버린다 — 내용 없는 줄만이므로 글자를 지우는 것이 아니다.
    """
    lines = []
    for raw_line in text.replace("\r", "").split("\n"):
        if not raw_line.strip():
            continue
        lines.append(raw_line.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    return tuple(lines) if lines else ("",)


# ─────────────────────────────────────────────────────────────
#  WebVTT 재독기 — 이 모듈이 쓰는 문법만 받는다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ParsedVttCue:
    """재독기가 돌려준 큐 — 시각은 밀리초, 문장은 이스케이프를 푼 값이다."""

    identifier: str
    start_ms: int
    end_ms: int
    text: str


_VTT_TIMELINE = re.compile(
    r"^(\d{2,}):([0-5]\d):([0-5]\d)\.(\d{3}) --> (\d{2,}):([0-5]\d):([0-5]\d)\.(\d{3})$"
)
_VTT_FORBIDDEN_BLOCK = re.compile(r"^(NOTE|STYLE|REGION)\b")


def parse_review_vtt(text: str) -> tuple[ParsedVttCue, ...]:
    """검토용 문서를 되돌린다 — 이 직렬화가 만드는 문법 밖은 전부 거부한다.

    왕복 검증용의 좁은 재독기다: 머리 `WEBVTT`, 숫자 식별자, 타임라인, 문장
    줄만 받고 NOTE·STYLE·REGION 블록·뒤집힌 시각·빈 큐는 예외로 막는다.
    """
    lines = _normalize_vtt_lines(text)
    if not lines or lines[0] != REVIEW_VTT_HEADER:
        raise ProjectionError(f"WebVTT 머리가 아니다: {lines[0]!r}" if lines else "빈 문서다")
    cues: list[ParsedVttCue] = []
    for block in _cue_blocks(lines[1:]):
        cues.append(_parse_cue_block(block))
    return tuple(cues)


def _normalize_vtt_lines(text: str) -> list[str]:
    """BOM·CRLF를 정규화한다 — 쓰는 쪽은 언제나 `\\n`이지만 읽는 쪽은 넉넉히."""
    return text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _cue_blocks(lines: list[str]) -> Iterable[list[str]]:
    """빈 줄로 나뉜 블록을 차례로 내놓는다 — 머리 뒤의 첫 빈칸은 건너뛴다."""
    block: list[str] = []
    for line in lines:
        if line.strip():
            block.append(line)
            continue
        if block:
            yield block
            block = []
    if block:
        yield block


def _parse_cue_block(block: list[str]) -> ParsedVttCue:
    if _VTT_FORBIDDEN_BLOCK.match(block[0]):
        raise ProjectionError(f"검토용 문서에는 {block[0].split()[0]!r} 블록이 없다")
    timeline_at = 1 if _VTT_TIMELINE.match(block[0]) is None else 0
    if timeline_at >= len(block) or _VTT_TIMELINE.match(block[timeline_at]) is None:
        raise ProjectionError(f"타임라인을 찾지 못했다: {block[0]!r}")
    identifier = "" if timeline_at == 0 else block[0]
    start_ms = _vtt_timestamp_ms(block[timeline_at].split("-->")[0].strip())
    end_ms = _vtt_timestamp_ms(block[timeline_at].split("-->")[1].strip())
    if start_ms >= end_ms:
        raise ProjectionError(f"큐 시각이 뒤집혔다: {start_ms}ms >= {end_ms}ms ({identifier!r})")
    text_lines = block[timeline_at + 1 :]
    if not text_lines:
        raise ProjectionError(f"문장 없는 큐다 ({identifier!r})")
    return ParsedVttCue(
        identifier=identifier,
        start_ms=start_ms,
        end_ms=end_ms,
        text=_unescape_vtt_text(text_lines),
    )


def _vtt_timestamp_ms(value: str) -> int:
    match = re.fullmatch(r"(\d{2,}):([0-5]\d):([0-5]\d)\.(\d{3})", value)
    if match is None:
        raise ProjectionError(f"WebVTT 시각 형식이 아니다: {value!r}")
    hours, minutes, seconds, millis = (int(part) for part in match.groups())
    return hours * 3_600_000 + minutes * 60_000 + seconds * 1000 + millis


def _unescape_vtt_text(lines: Sequence[str]) -> str:
    """실체 참조를 렌더 값으로 되돌린다 — 왕복 검증이 이스케이프를 증명한다."""
    restored = [
        line.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&") for line in lines
    ]
    return "\n".join(restored)
