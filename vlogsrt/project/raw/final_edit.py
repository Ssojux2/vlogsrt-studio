"""최종 편집(W-66, D117) — 초안 영상 위의 사람 컷 편집과 자막 편집을 값으로만 다룬다.

문서는 둘이다. **클립**은 초안 영상(`draft.mp4`)의 프레임 구간 `[start, end)`이고 언제나 초안 차례
그대로다(갭도 순서 바꾸기도 없다 — 프라이머리 스토리라인). **자막**은 초안 시각의 정수 밀리초다.
편집 타임라인의 시각은 저장하지 않고 투영한다 — 클립을 지우거나 줄이면 그 자리의 자막이 함께 줄고
뒤의 자막이 당겨지며(리플), 지운 구간에 다 들어간 자막은 **가려진다**(편집본 SRT에서 빠지지만
문서에는 남는다 — 규칙 9: 조용히 사라지지 않는다). 클립을 다시 늘리면 가려진 자막이 돌아온다.

계산은 모두 정수 틱(1초 = `1000 × 프레임 길이 분모`)이다 — 프레임과 밀리초가 함께 정수가 되어
브라우저(JS)가 같은 규칙을 옮겨 같은 값을 낸다(시험이 공용 사례로 정확히 맞춘다). 편집 시각 → 초안
밀리초는 반내림, 편집본 SRT를 쓸 때는 `format_srt_time`의 반올림 한 번 — 사람이 친 편집 밀리초가
그대로 되돌아온다.

이 모듈은 파일·프로세스·서비스를 모른다(불변식 「최종 편집 모델은 순수하다」). 시각을 만들지
않는다 — 자막 시각은 엔진이 준 초안 SRT 또는 사람이 고른 값이고, 프로그램은 컷에 따른 투영만
계산한다(규칙 1·3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS, FinalEditSettings
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.timebase import format_srt_time

SCHEMA = "vlogsrt-final-edit/v1"
SIDE_START = "start"
SIDE_END = "end"
SEED_PREFIX = "c"
"""초안 SRT에서 심은 자막의 아이디 머리 — `c1…cN`."""
NEW_PREFIX = "n"
"""사람이 넣거나 나눠 생긴 자막의 아이디 머리 — `n{next_cue}`."""
WARNING_KINDS = ("empty", "zero", "short", "long", "fast", "overlap", "gap")
_MS_PER_SECOND = 1000


class FinalEditError(RawWorkspaceError):
    """편집 문서나 연산이 틀렸다 — 까닭을 말하고 문서를 바꾸지 않는다."""


@dataclass(frozen=True, slots=True)
class DraftFacts:
    """초안 영상의 프레임 격자 — 보고서의 `sequence.frame_duration`·`total_frames`."""

    frame_duration: Fraction
    total_frames: int


@dataclass(frozen=True, slots=True)
class Clip:
    """초안 프레임 구간 `[start_frame, end_frame)`."""

    start_frame: int
    end_frame: int


@dataclass(frozen=True, slots=True)
class EditCue:
    """자막 하나 — 초안 시각 밀리초 `[start_ms, end_ms]`."""

    cue_id: str
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True, slots=True)
class EditDocument:
    draft: DraftFacts
    clips: tuple[Clip, ...]
    cues: tuple[EditCue, ...]
    next_cue: int
    """다음에 넣을 자막 아이디 번호(`n{next_cue}`)."""


@dataclass(frozen=True, slots=True)
class ProjectedCue:
    """편집 타임라인의 자막 — 시각은 정확 유리수 초(0 = 편집본 처음)."""

    cue_id: str
    start: Fraction
    end: Fraction
    text: str
    trimmed: bool
    """컷이 자막의 일부를 덮었다(앞·뒤가 잘렸거나 가운데가 빠졌다)."""


@dataclass(frozen=True, slots=True)
class Projection:
    cues: tuple[ProjectedCue, ...]
    hidden: tuple[EditCue, ...]
    """지운 구간에 다 들어가 편집본에서 빠진 자막."""
    total_frames: int


@dataclass(frozen=True, slots=True)
class CueWarning:
    cue_id: str
    kind: str


# ─── 틱 ──────────────────────────────────────────────────────────────────


def tick_rate(draft: DraftFacts) -> int:
    """1초의 틱 수 — 프레임 길이 분모 × 1000."""
    return _MS_PER_SECOND * draft.frame_duration.denominator


def frame_ticks(draft: DraftFacts) -> int:
    """한 프레임의 틱 수 — 프레임 길이 분자 × 1000."""
    return _MS_PER_SECOND * draft.frame_duration.numerator


def ms_ticks(draft: DraftFacts) -> int:
    """1밀리초의 틱 수 — 프레임 길이 분모."""
    return draft.frame_duration.denominator


def total_ms(draft: DraftFacts) -> int:
    """초안 길이의 밀리초(올림) — 자막 시각의 상한."""
    whole = draft.total_frames * frame_ticks(draft)
    return -(-whole // ms_ticks(draft))


def _ms_half_down(ticks: int, unit: int) -> int:
    """틱 → 밀리초, 정확히 반이면 아래로 — `ceil(ticks/unit - 1/2)`."""
    return -((unit - 2 * ticks) // (2 * unit))


# ─── 만들기 · 전선 모양 · 검사 ───────────────────────────────────────────


def seed(draft: DraftFacts, srt_cues: Sequence[tuple[Fraction, Fraction, str]]) -> EditDocument:
    """초안 SRT 그대로 — 클립 하나 `[0, total)`, 자막 `c1…cN`."""
    return EditDocument(
        draft=draft,
        clips=(Clip(0, draft.total_frames),) if draft.total_frames > 0 else (),
        cues=_seed_cues(srt_cues),
        next_cue=1,
    )


def _seed_cues(srt_cues: Sequence[tuple[Fraction, Fraction, str]]) -> tuple[EditCue, ...]:
    cues = [
        EditCue(f"{SEED_PREFIX}{number}", _whole_ms(start), _whole_ms(end), text)
        for number, (start, end, text) in enumerate(srt_cues, start=1)
    ]
    return _sorted(cues)


def _whole_ms(seconds: Fraction) -> int:
    """SRT 시각은 밀리초 정확값이다 — 아니면 고쳐 읽지 않고 거부한다."""
    value = seconds * _MS_PER_SECOND
    if value.denominator != 1 or value < 0:
        raise FinalEditError(f"SRT 시각 {seconds}초가 밀리초 정수가 아니다")
    return int(value)


def replace_cues(
    document: EditDocument, srt_cues: Sequence[tuple[Fraction, Fraction, str]]
) -> EditDocument:
    """클립은 그대로 두고 자막만 새 SRT로 다시 심는다(「새 자막으로 다시 시작」)."""
    return replace(document, cues=_seed_cues(srt_cues))


def to_payload(document: EditDocument) -> dict[str, object]:
    """브라우저와 저장 파일이 주고받는 모양."""
    return {
        "clips": [[clip.start_frame, clip.end_frame] for clip in document.clips],
        "cues": [[cue.cue_id, cue.start_ms, cue.end_ms, cue.text] for cue in document.cues],
        "nextCue": document.next_cue,
    }


def from_payload(
    payload: Mapping[str, object],
    draft: DraftFacts,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> EditDocument:
    """받은 모양 → 문서. 모양이 틀리면 거부하고, 만든 문서는 `validate`를 지난다."""
    clips = _payload_list(payload, "clips")
    cues = _payload_list(payload, "cues")
    document = EditDocument(
        draft=draft,
        clips=tuple(Clip(*_ints(row, 2, "클립")) for row in clips),
        cues=tuple(_payload_cue(row) for row in cues),
        next_cue=_int(payload.get("nextCue"), "nextCue"),
    )
    validate(document, settings)
    return document


def _payload_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise FinalEditError(f"편집 문서의 `{key}`가 목록이 아니다")
    return value


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FinalEditError(f"{what}이(가) 정수가 아니다: {value!r}")
    return value


def _ints(row: object, count: int, what: str) -> list[int]:
    if not isinstance(row, list) or len(row) != count:
        raise FinalEditError(f"{what} 한 줄의 모양이 틀렸다: {row!r}")
    return [_int(value, what) for value in row]


def _payload_cue(row: object) -> EditCue:
    if not isinstance(row, list) or len(row) != 4:
        raise FinalEditError(f"자막 한 줄의 모양이 틀렸다: {row!r}")
    cue_id, start, end, text = row
    if not isinstance(cue_id, str) or not isinstance(text, str):
        raise FinalEditError(f"자막 아이디·글이 문자열이 아니다: {row!r}")
    return EditCue(cue_id, _int(start, "자막 시작"), _int(end, "자막 끝"), text)


def validate(document: EditDocument, settings: FinalEditSettings = FINAL_EDIT_SETTINGS) -> None:
    """문서 전체 검사 — 클립 범위·차례, 자막 아이디·범위·글, 상한. 틀리면 거부한다."""
    _validate_clips(document)
    _validate_cues(document, settings)
    if document.next_cue < 1:
        raise FinalEditError(f"다음 자막 번호가 1보다 작다: {document.next_cue}")


def _validate_clips(document: EditDocument) -> None:
    previous_end = 0
    for clip in document.clips:
        if not previous_end <= clip.start_frame < clip.end_frame <= document.draft.total_frames:
            raise FinalEditError(
                f"클립 [{clip.start_frame}, {clip.end_frame})이 초안 "
                f"{document.draft.total_frames}프레임 안에서 차례대로 놓이지 않았다"
            )
        previous_end = clip.end_frame


def _validate_cues(document: EditDocument, settings: FinalEditSettings) -> None:
    if len(document.cues) > settings.max_cues:
        raise FinalEditError(f"자막이 {settings.max_cues}개를 넘는다: {len(document.cues)}")
    limit = total_ms(document.draft)
    seen: set[str] = set()
    for cue in document.cues:
        if not valid_cue_id(cue.cue_id) or cue.cue_id in seen:
            raise FinalEditError(f"자막 아이디가 틀렸거나 겹친다: {cue.cue_id!r}")
        seen.add(cue.cue_id)
        if not 0 <= cue.start_ms <= cue.end_ms <= limit:
            raise FinalEditError(
                f"자막 {cue.cue_id}의 시각 [{cue.start_ms}, {cue.end_ms}]ms가 초안 {limit}ms 밖이다"
            )
        _validate_text(cue, settings)
    if list(document.cues) != list(_sorted(document.cues)):
        raise FinalEditError("자막이 시각 차례로 놓이지 않았다")


def valid_cue_id(cue_id: str) -> bool:
    """`c` 또는 `n` + 0으로 시작하지 않는 십진수."""
    number = cue_id[1:]
    return (
        cue_id[:1] in (SEED_PREFIX, NEW_PREFIX)
        and number.isascii()
        and number.isdigit()
        and not number.startswith("0")
    )


def _validate_text(cue: EditCue, settings: FinalEditSettings) -> None:
    text = cue.text
    if len(text) > settings.max_text_chars:
        raise FinalEditError(f"자막 {cue.cue_id}의 글이 {settings.max_text_chars}자를 넘는다")
    if "\r" in text or "\n\n" in text or text.startswith("\n") or text.endswith("\n"):
        raise FinalEditError(f"자막 {cue.cue_id}의 글에 빈 줄이나 \\r이 있다 — SRT가 깨진다")


def _sorted(cues: Sequence[EditCue]) -> tuple[EditCue, ...]:
    return tuple(sorted(cues, key=lambda cue: (cue.start_ms, cue.end_ms, cue.cue_id)))


# ─── 투영 ────────────────────────────────────────────────────────────────


def _clip_spans(document: EditDocument) -> list[tuple[int, int, int]]:
    """클립마다 (초안 시작 틱, 초안 끝 틱, 편집 시작 틱)."""
    unit = frame_ticks(document.draft)
    spans = []
    offset = 0
    for clip in document.clips:
        start, end = clip.start_frame * unit, clip.end_frame * unit
        spans.append((start, end, offset))
        offset += end - start
    return spans


def edited_frames(document: EditDocument) -> int:
    return sum(clip.end_frame - clip.start_frame for clip in document.clips)


def draft_frame_at(document: EditDocument, edited_frame: int) -> int:
    """편집 프레임 → 초안 프레임. 끝(편집 길이)은 마지막 클립의 끝이다."""
    if not 0 <= edited_frame <= edited_frames(document) or not document.clips:
        raise FinalEditError(f"편집 프레임 {edited_frame}이 편집본 밖이다")
    offset = 0
    for clip in document.clips:
        length = clip.end_frame - clip.start_frame
        if edited_frame < offset + length:
            return clip.start_frame + edited_frame - offset
        offset += length
    return document.clips[-1].end_frame


def edited_frame_of(document: EditDocument, draft_frame: int) -> int | None:
    """초안 프레임 → 편집 프레임. 지운 구간이면 None."""
    offset = 0
    for clip in document.clips:
        if clip.start_frame <= draft_frame < clip.end_frame:
            return offset + draft_frame - clip.start_frame
        offset += clip.end_frame - clip.start_frame
    return None


def edited_ticks(document: EditDocument, draft_ms: int) -> int:
    """초안 밀리초 → 편집 틱. 지운 구간 안이면 그 컷 점(다음 클립의 편집 시작)."""
    point = draft_ms * ms_ticks(document.draft)
    spans = _clip_spans(document)
    for start, end, offset in spans:
        if point < start:
            return offset
        if point <= end:
            return offset + point - start
    return edited_frames(document) * frame_ticks(document.draft)


def draft_ms_at(document: EditDocument, ticks: int, side: str) -> int:
    """편집 틱 → 초안 밀리초(반내림). 컷 위의 시각은 시작이면 다음 클립, 끝이면 앞 클립 쪽이다."""
    spans = _clip_spans(document)
    if not spans:
        raise FinalEditError("클립이 없다 — 편집 시각이 초안의 어디에도 닿지 않는다")
    chosen = _span_at(spans, ticks, side)
    start, _, offset = chosen
    return _ms_half_down(start + ticks - offset, ms_ticks(document.draft))


def _span_at(spans: list[tuple[int, int, int]], ticks: int, side: str) -> tuple[int, int, int]:
    total = spans[-1][2] + spans[-1][1] - spans[-1][0]
    if not 0 <= ticks <= total:
        raise FinalEditError(f"편집 시각 {ticks}틱이 편집본 밖이다")
    for span in spans:
        start, end, offset = span
        length = end - start
        if side == SIDE_START and offset <= ticks < offset + length:
            return span
        if side == SIDE_END and offset < ticks <= offset + length:
            return span
    return spans[-1] if side == SIDE_START else spans[0]


def _kept_measure(spans: list[tuple[int, int, int]], start: int, end: int) -> int:
    return sum(
        max(0, min(end, span_end) - max(start, span_start)) for span_start, span_end, _ in spans
    )


def _point_kept(spans: list[tuple[int, int, int]], point: int) -> bool:
    return any(span_start <= point <= span_end for span_start, span_end, _ in spans)


def project(document: EditDocument) -> Projection:
    """편집 타임라인의 자막 — 남은 구간과 양의 길이로 겹치는 자막(길이 0이면 클립 안의 것)."""
    unit = ms_ticks(document.draft)
    rate = tick_rate(document.draft)
    spans = _clip_spans(document)
    visible: list[ProjectedCue] = []
    hidden: list[EditCue] = []
    for cue in document.cues:
        start, end = cue.start_ms * unit, cue.end_ms * unit
        kept = _kept_measure(spans, start, end)
        if kept == 0 and not (start == end and _point_kept(spans, start)):
            hidden.append(cue)
            continue
        visible.append(
            ProjectedCue(
                cue_id=cue.cue_id,
                start=Fraction(edited_ticks(document, cue.start_ms), rate),
                end=Fraction(edited_ticks(document, cue.end_ms), rate),
                text=cue.text,
                trimmed=kept < end - start,
            )
        )
    return Projection(tuple(visible), tuple(hidden), edited_frames(document))


def srt_text(projection: Projection) -> str:
    """편집본 SRT(번호는 1부터) — 초안 SRT(`render_plan.cues_srt_text`)와 같은 바이트 꼴."""
    blocks = []
    for number, cue in enumerate(projection.cues, start=1):
        timeline = f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}"
        blocks.append(f"{number}\n{timeline}\n{cue.text}\n")
    return "\n".join(blocks)


def keep_ranges(document: EditDocument) -> tuple[tuple[int, int, Fraction, Fraction], ...]:
    """번인(3단계)이 남길 초안 구간 — (시작 프레임, 끝 프레임, 시작 초, 끝 초), 차례대로."""
    duration = document.draft.frame_duration
    return tuple(
        (clip.start_frame, clip.end_frame, clip.start_frame * duration, clip.end_frame * duration)
        for clip in document.clips
    )


def removed_ranges(document: EditDocument) -> tuple[tuple[int, int], ...]:
    """초안에서 빠진 프레임 구간 — 요약에 남긴다."""
    gaps = []
    cursor = 0
    for clip in document.clips:
        if clip.start_frame > cursor:
            gaps.append((cursor, clip.start_frame))
        cursor = clip.end_frame
    if cursor < document.draft.total_frames:
        gaps.append((cursor, document.draft.total_frames))
    return tuple(gaps)


def warnings(
    document: EditDocument, settings: FinalEditSettings = FINAL_EDIT_SETTINGS
) -> tuple[CueWarning, ...]:
    """보이는 자막의 경고(빈 글·길이 0·짧음·김·빠름·겹침·좁은 틈) — 저장을 막지 않는다."""
    rate = tick_rate(document.draft)
    cues = project(document).cues
    found: list[CueWarning] = []
    for index, cue in enumerate(cues):
        following = cues[index + 1] if index + 1 < len(cues) else None
        found += [
            CueWarning(cue.cue_id, kind) for kind in _cue_warnings(cue, following, rate, settings)
        ]
    return tuple(found)


def _cue_warnings(
    cue: ProjectedCue, following: ProjectedCue | None, rate: int, settings: FinalEditSettings
) -> list[str]:
    span = (cue.end - cue.start) * rate
    unit = Fraction(rate, _MS_PER_SECOND)
    chars = text_chars(cue.text)
    kinds = []
    if chars == 0:
        kinds.append("empty")
    if span == 0:
        kinds.append("zero")
    elif span < settings.min_duration_ms * unit:
        kinds.append("short")
    if span > settings.max_duration_ms * unit:
        kinds.append("long")
    if span > 0 and chars * rate > settings.max_cps * span:
        kinds.append("fast")
    if following is not None:
        gap = (following.start - cue.end) * rate
        if gap < 0:
            kinds.append("overlap")
        elif 0 < gap < settings.min_gap_ms * unit:
            kinds.append("gap")
    return kinds


def text_chars(text: str) -> int:
    """글자/초를 재는 글자 수 — 공백과 줄바꿈을 뺀다."""
    return sum(1 for char in text if char not in " \n")


# ─── 클립 연산(자막은 그대로 — 투영이 따라온다) ───────────────────────────


def blade(document: EditDocument, edited_frame: int) -> EditDocument:
    """편집 프레임에서 클립을 둘로 — 편집 점 위면 거부한다."""
    offset = 0
    for index, clip in enumerate(document.clips):
        length = clip.end_frame - clip.start_frame
        if offset < edited_frame < offset + length:
            cut = clip.start_frame + edited_frame - offset
            pieces = (Clip(clip.start_frame, cut), Clip(cut, clip.end_frame))
            return _with_clips(
                document, document.clips[:index] + pieces + document.clips[index + 1 :]
            )
        offset += length
    raise FinalEditError(f"편집 프레임 {edited_frame}은 클립 안이 아니다(편집 점 위거나 밖이다)")


def ripple_delete(document: EditDocument, edited_start: int, edited_end: int) -> EditDocument:
    """편집 프레임 `[edited_start, edited_end)`를 지우고 뒤를 당긴다(클립이 잘리거나 둘이 된다)."""
    if not 0 <= edited_start < edited_end <= edited_frames(document):
        raise FinalEditError(f"지울 범위 [{edited_start}, {edited_end})이 편집본 밖이거나 비었다")
    kept: list[Clip] = []
    offset = 0
    for clip in document.clips:
        length = clip.end_frame - clip.start_frame
        head = min(max(edited_start - offset, 0), length)
        tail = min(max(edited_end - offset, 0), length)
        if head > 0:
            kept.append(Clip(clip.start_frame, clip.start_frame + head))
        if tail < length:
            kept.append(Clip(clip.start_frame + tail, clip.end_frame))
        offset += length
    return _with_clips(document, tuple(kept))


def delete_clips(document: EditDocument, indexes: Sequence[int]) -> EditDocument:
    """고른 클립을 지운다(리플)."""
    chosen = set(indexes)
    if not chosen or not chosen <= set(range(len(document.clips))):
        raise FinalEditError(f"지울 클립 번호가 틀렸다: {sorted(chosen)}")
    return _with_clips(
        document, tuple(clip for index, clip in enumerate(document.clips) if index not in chosen)
    )


def set_clip_start(
    document: EditDocument,
    index: int,
    draft_frame: int,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> EditDocument:
    """클립 시작을 초안 프레임으로 — 앞 클립 끝과 제 끝 − 최소 길이 사이.

    늘리면 지웠던 것이 돌아온다.
    """
    clip = _clip(document, index)
    floor = document.clips[index - 1].end_frame if index > 0 else 0
    if not floor <= draft_frame <= clip.end_frame - settings.min_clip_frames:
        raise FinalEditError(f"클립 시작 {draft_frame}이 [{floor}, {clip.end_frame}) 밖이다")
    return _replace_clip(document, index, Clip(draft_frame, clip.end_frame))


def set_clip_end(
    document: EditDocument,
    index: int,
    draft_frame: int,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> EditDocument:
    """클립 끝을 초안 프레임으로 — 제 시작 + 최소 길이와 다음 클립 시작 사이."""
    clip = _clip(document, index)
    ceiling = (
        document.clips[index + 1].start_frame
        if index + 1 < len(document.clips)
        else document.draft.total_frames
    )
    if not clip.start_frame + settings.min_clip_frames <= draft_frame <= ceiling:
        raise FinalEditError(f"클립 끝 {draft_frame}이 ({clip.start_frame}, {ceiling}] 밖이다")
    return _replace_clip(document, index, Clip(clip.start_frame, draft_frame))


def join_clips(document: EditDocument, index: int) -> EditDocument:
    """이어진 두 클립(끝 = 다음 시작)을 하나로 — 블레이드의 거꾸로."""
    clip = _clip(document, index)
    following = _clip(document, index + 1)
    if clip.end_frame != following.start_frame:
        raise FinalEditError(f"클립 {index}와 다음 클립 사이에 지운 구간이 있어 합칠 수 없다")
    merged = Clip(clip.start_frame, following.end_frame)
    return _with_clips(document, document.clips[:index] + (merged,) + document.clips[index + 2 :])


def _clip(document: EditDocument, index: int) -> Clip:
    if not 0 <= index < len(document.clips):
        raise FinalEditError(f"클립 번호 {index}가 없다")
    return document.clips[index]


def _replace_clip(document: EditDocument, index: int, clip: Clip) -> EditDocument:
    clips = list(document.clips)
    clips[index] = clip
    return _with_clips(document, tuple(clips))


def _with_clips(document: EditDocument, clips: tuple[Clip, ...]) -> EditDocument:
    return replace(document, clips=clips)


# ─── 자막 연산(초안 밀리초) ──────────────────────────────────────────────


def _cue_index(document: EditDocument, cue_id: str) -> int:
    for index, cue in enumerate(document.cues):
        if cue.cue_id == cue_id:
            return index
    raise FinalEditError(f"자막 {cue_id}이(가) 없다")


def _with_cues(
    document: EditDocument, cues: Sequence[EditCue], next_cue: int | None = None
) -> EditDocument:
    return replace(
        document,
        cues=_sorted(cues),
        next_cue=document.next_cue if next_cue is None else next_cue,
    )


def _check_times(document: EditDocument, start_ms: int, end_ms: int) -> None:
    limit = total_ms(document.draft)
    if not 0 <= start_ms <= end_ms <= limit:
        raise FinalEditError(f"자막 시각 [{start_ms}, {end_ms}]ms가 [0, {limit}] 밖이거나 뒤집혔다")


def cue_set_times(document: EditDocument, cue_id: str, start_ms: int, end_ms: int) -> EditDocument:
    _check_times(document, start_ms, end_ms)
    index = _cue_index(document, cue_id)
    cues = list(document.cues)
    cues[index] = replace(cues[index], start_ms=start_ms, end_ms=end_ms)
    return _with_cues(document, cues)


def cue_shift(document: EditDocument, cue_id: str, delta_ms: int) -> EditDocument:
    """자막을 통째로 민다 — 초안 처음·끝에서 멈춘다(길이는 그대로)."""
    cue = document.cues[_cue_index(document, cue_id)]
    delta = max(delta_ms, -cue.start_ms)
    delta = min(delta, total_ms(document.draft) - cue.end_ms)
    return cue_set_times(document, cue_id, cue.start_ms + delta, cue.end_ms + delta)


def cue_set_text(
    document: EditDocument,
    cue_id: str,
    text: str,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> EditDocument:
    index = _cue_index(document, cue_id)
    cues = list(document.cues)
    cues[index] = replace(cues[index], text=text)
    _validate_text(cues[index], settings)
    return _with_cues(document, cues)


def cue_delete(document: EditDocument, cue_id: str) -> EditDocument:
    index = _cue_index(document, cue_id)
    return _with_cues(document, document.cues[:index] + document.cues[index + 1 :])


def cues_delete_inside(document: EditDocument, start_ms: int, end_ms: int) -> EditDocument:
    """`[start_ms, end_ms]` 안에 다 들어간 자막을 지운다(영상은 그대로 — Shift+Delete)."""
    _check_times(document, start_ms, end_ms)
    kept = [cue for cue in document.cues if not (start_ms <= cue.start_ms and cue.end_ms <= end_ms)]
    if len(kept) == len(document.cues):
        raise FinalEditError("범위 안에 다 들어간 자막이 없다")
    return _with_cues(document, kept)


def split_text(text: str) -> tuple[str, str]:
    """나눌 글 — 두 줄이면 줄에서, 아니면 가운데에 가장 가까운 공백(같으면 앞).

    공백이 없으면 모두 왼쪽에 남는다.
    """
    if "\n" in text:
        left, right = text.split("\n", 1)
        return left, right
    spaces = [index for index, char in enumerate(text) if char == " "]
    if not spaces:
        return text, ""
    middle = len(text) / 2
    cut = min(spaces, key=lambda index: (abs(index - middle), index))
    return text[:cut], text[cut + 1 :]


def cue_split(
    document: EditDocument, cue_id: str, at_ms: int, left_text: str, right_text: str
) -> EditDocument:
    """자막을 `at_ms`에서 둘로 — 왼쪽은 아이디를 지키고 오른쪽은 새 아이디다."""
    index = _cue_index(document, cue_id)
    cue = document.cues[index]
    if not cue.start_ms < at_ms < cue.end_ms:
        raise FinalEditError(f"나눌 시각 {at_ms}ms가 자막 {cue_id} 안이 아니다")
    left = replace(cue, end_ms=at_ms, text=left_text)
    right = EditCue(f"{NEW_PREFIX}{document.next_cue}", at_ms, cue.end_ms, right_text)
    cues = [*document.cues[:index], left, right, *document.cues[index + 1 :]]
    return _with_cues(document, cues, document.next_cue + 1)


def cue_merge_next(document: EditDocument, cue_id: str) -> EditDocument:
    """보이는 차례의 다음 자막과 하나로 — 글은 공백으로 잇고 시각은 둘을 덮는다."""
    visible = [cue.cue_id for cue in project(document).cues]
    if cue_id not in visible or visible.index(cue_id) + 1 >= len(visible):
        raise FinalEditError(f"자막 {cue_id} 다음에 합칠 보이는 자막이 없다")
    first = document.cues[_cue_index(document, cue_id)]
    second = document.cues[_cue_index(document, visible[visible.index(cue_id) + 1])]
    text = " ".join(part for part in (first.text, second.text) if part)
    merged = replace(
        first,
        start_ms=min(first.start_ms, second.start_ms),
        end_ms=max(first.end_ms, second.end_ms),
        text=text,
    )
    cues = [merged if cue is first else cue for cue in document.cues if cue is not second]
    return _with_cues(document, cues)


def cue_insert(document: EditDocument, start_ms: int, end_ms: int, text: str) -> EditDocument:
    """새 자막(`n{next_cue}`)을 넣는다."""
    _check_times(document, start_ms, end_ms)
    cue = EditCue(f"{NEW_PREFIX}{document.next_cue}", start_ms, end_ms, text)
    return _with_cues(document, [*document.cues, cue], document.next_cue + 1)
