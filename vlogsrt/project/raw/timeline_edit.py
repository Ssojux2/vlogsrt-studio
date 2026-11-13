"""원본 범위 타임라인(W-71a, D125) — 렌더 없이 원본 구간을 잇고, 클립 앞뒤를 원본 안에서 고친다.

D117의 최종 편집은 렌더한 초안(`draft.mp4`) 위의 컷이었다. 여기서 **클립은 원본의 프레임 구간**
`[start, end)`이고 차례가 곧 타임라인이다 — 추천 구간(`render_plan`의 조각)을 그대로 심고, 사람이
클립 가장자리를 원본 처음·끝까지 늘이거나 줄인다(사람 결정이 등급을 이긴다). 같은 원본의 클립은
타임라인에서 붙어 있고 원본 차례로 겹치지 않는다 — 원본의 한 프레임은 많아야 한 번 나온다.

**출력 위치는 렌더 계획과 같은 함수다.** 클립의 시퀀스 경계는 `render_plan.sequence_edges`(누적
길이를 가장 가까운 시퀀스 프레임에, 원본 끝에 닿은 클립은 내림)로 놓는다 — 손대지 않은
타임라인의 배치가 `build_plan`의 조각과 같으므로 화면이 곧 출력이다. 시퀀스 포맷은 타임라인을
만들 때 정해 문서와 함께 둔다(클립을 늘려도 레이트 투표가 바뀌지 않게 — Final Cut 프로젝트
설정과 같다).

**자막은 원본 시계(밀리초)에 산다.** 클립을 늘리면 그 자리의 원본 자막이 돌아오고, 어느 클립과도
겹치지 않는 자막은 **가려질** 뿐 문서에 남는다(규칙 9). 심을 때 한 번만 조판 정리(`caption_text`)를
거치고, 각 자막은 원본 줄 아이디(`origin`)를, 문서는 원본마다 심은 리비전(`seeded`)을 싣는다 — 다시
듣기 뒤 같은 줄 아이디가 다른 말이 되므로 리비전과 함께만 뜻이 있다. 원본마다 남긴 클립과 뺀
구간(`removed_ranges`)이 `[0, 프레임 수)`를 빈틈없이 덮는다(D35).

계산은 정수 틱(1초 = `1000 × lcm(시퀀스·원본 프레임 길이 분모)`)이다 — 시퀀스 프레임·원본 프레임·
밀리초가 모두 정수라 브라우저(JS)가 같은 규칙으로 같은 값을 낸다. 틱이 2⁵³을 넘는 문서는 받지 않는다
(JS 정수가 정확하지 않다). 파일·프로세스·서비스를 모르고(불변식 「원본 범위 타임라인은 순수하다」)
시각을 만들지 않는다 — 자막 시각은 엔진이 준 원본 자막 또는 사람이 고른 값이다(규칙 1·3).
"""

from __future__ import annotations

import bisect
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction

from vlogsrt.project.raw import render_plan
from vlogsrt.project.raw.final_edit import text_chars, valid_cue_id
from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS, FinalEditSettings
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.timebase import milliseconds_half_up

SCHEMA = "vlogsrt-timeline-edit/v1"
SIDE_START = "start"
SIDE_END = "end"
SEED_PREFIX = "c"
NEW_PREFIX = "n"
EXACT_TICK_LIMIT = 2**53
"""JS 수(IEEE 배정밀도)가 정수를 정확히 담는 한계 — 조정값이 아니라 브라우저와 같은 값의 조건."""
_MS_PER_SECOND = 1000


class TimelineEditError(RawWorkspaceError):
    """타임라인 문서나 연산이 틀렸다 — 까닭을 말하고 문서를 바꾸지 않는다."""


# ─── 값 ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class TimelineSource:
    """원본 하나의 프레임 격자 — 렌더 계획의 `FrameClock`과 같은 값(원점 0)."""

    source_id: str
    frame_duration: Fraction
    frame_count: int

    @property
    def extent(self) -> Fraction:
        return self.frame_count * self.frame_duration


@dataclass(frozen=True, slots=True)
class TimelineFacts:
    """서버가 정하는 사실 — 시퀀스 포맷과 원본들(촬영 시각 순). 브라우저에서 받지 않는다."""

    sequence: render_plan.SequenceFormat
    sources: tuple[TimelineSource, ...]


@dataclass(frozen=True, slots=True)
class Clip:
    source_id: str
    start_frame: int
    end_frame: int


@dataclass(frozen=True, slots=True)
class EditCue:
    """자막 하나 — 그 원본 시계의 밀리초 `[start_ms, end_ms]`. `origin`은 심은 원본 줄 아이디."""

    cue_id: str
    source_id: str
    start_ms: int
    end_ms: int
    text: str
    origin: str | None


@dataclass(frozen=True, slots=True)
class TimelineDocument:
    facts: TimelineFacts
    clips: tuple[Clip, ...]
    cues: tuple[EditCue, ...]
    next_cue: int
    seeded: tuple[tuple[str, str], ...]
    """원본마다 심은 원본 자막 리비전 — 원본 차례."""


@dataclass(frozen=True, slots=True)
class Span:
    """클립 하나의 출력 자리 — 시퀀스 프레임 `[out_start, out_start + out_frames)`."""

    index: int
    source_id: str
    start_frame: int
    end_frame: int
    out_start: int
    out_frames: int


@dataclass(frozen=True, slots=True)
class ProjectedCue:
    """타임라인의 자막 — 정확 유리수 초(0 = 타임라인 처음)."""

    cue_id: str
    source_id: str
    start: Fraction
    end: Fraction
    text: str
    trimmed: bool


@dataclass(frozen=True, slots=True)
class Projection:
    cues: tuple[ProjectedCue, ...]
    hidden: tuple[EditCue, ...]
    total_frames: int


@dataclass(frozen=True, slots=True)
class Seeded:
    """심은 문서와 센 것 — 정리 뒤 빈 줄(`empty`)은 넣지 않고, 원본 밖 시각은 안으로(`clamped`)."""

    document: TimelineDocument
    empty: int
    clamped: int


@dataclass(frozen=True, slots=True)
class CueWarning:
    cue_id: str
    kind: str


# ─── 틱 ──────────────────────────────────────────────────────────────────


def tick_rate(facts: TimelineFacts) -> int:
    """1초의 틱 수 — 1000 × 시퀀스·원본 프레임 길이 분모의 최소공배수."""
    denominators = [facts.sequence.frame_duration.denominator]
    denominators += [source.frame_duration.denominator for source in facts.sources]
    return _MS_PER_SECOND * math.lcm(*denominators)


def sequence_frame_ticks(facts: TimelineFacts) -> int:
    return int(facts.sequence.frame_duration * tick_rate(facts))


def source_frame_ticks(facts: TimelineFacts, source_id: str) -> int:
    return int(_source(facts, source_id).frame_duration * tick_rate(facts))


def ms_ticks(facts: TimelineFacts) -> int:
    return tick_rate(facts) // _MS_PER_SECOND


def source_total_ms(source: TimelineSource) -> int:
    """원본 길이의 밀리초(올림) — 그 원본 자막 시각의 상한."""
    return math.ceil(source.extent * _MS_PER_SECOND)


def _source(facts: TimelineFacts, source_id: str) -> TimelineSource:
    for source in facts.sources:
        if source.source_id == source_id:
            return source
    raise TimelineEditError(f"원본 {source_id!r}이(가) 타임라인 사실에 없다")


def _source_order(facts: TimelineFacts) -> dict[str, int]:
    return {source.source_id: index for index, source in enumerate(facts.sources)}


def _ms_half_down(ticks: int, unit: int) -> int:
    """틱 → 밀리초, 정확히 반이면 아래로 — `ceil(ticks/unit - 1/2)`."""
    return -((unit - 2 * ticks) // (2 * unit))


# ─── 만들기 ──────────────────────────────────────────────────────────────


def facts_from_plan(
    plan: render_plan.RenderPlan, sources: Sequence[render_plan.RenderSource]
) -> TimelineFacts:
    """계획이 고른 시퀀스와 계획이 받는 원본(격자를 증명한 것) — 남긴 조각이 없는 원본도 싣는다."""
    if plan.sequence is None:
        raise TimelineEditError("남길 조각이 하나도 없어 시퀀스 포맷을 정할 수 없다")
    kept = [s for s in sources if render_plan.excluded_reason(s) is None]
    return TimelineFacts(
        sequence=plan.sequence,
        sources=tuple(
            TimelineSource(s.source_id, s.clock.frame_duration, s.clock.frame_count) for s in kept
        ),
    )


def seed(
    facts: TimelineFacts,
    clips: Sequence[Clip],
    transcripts: Mapping[str, tuple[str, Sequence[render_plan.SourceCue]]],
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> Seeded:
    """클립과 원본마다 (리비전, 원본 자막) — 원본의 자막을 **모두** 심는다(늘이면 돌아온다)."""
    order = _source_order(facts)
    unknown = sorted(set(transcripts) - set(order))
    if unknown:
        raise TimelineEditError(f"타임라인 사실에 없는 원본의 자막이다: {', '.join(unknown)}")
    rows: list[tuple[int, int, int, int, EditCue]] = []
    empty = clamped = 0
    for source_id, (_, cues) in transcripts.items():
        source = _source(facts, source_id)
        for number, cue in enumerate(cues):
            made = _seed_cue(source, cue)
            if made is None:
                empty += 1
                continue
            clamped += made[1]
            rows.append((order[source_id], made[0].start_ms, made[0].end_ms, number, made[0]))
    rows.sort(key=lambda row: row[:4])
    # 번호는 시각 차례로 주고, 같은 시각끼리는 검사와 같은 열쇠(아이디 문자열)로 다시 줄 세운다.
    cues_out = _sorted(
        facts, [replace(row[4], cue_id=f"{SEED_PREFIX}{n}") for n, row in enumerate(rows, 1)]
    )
    seeded = tuple((sid, transcripts[sid][0]) for sid in order if sid in transcripts)
    document = TimelineDocument(facts, tuple(clips), cues_out, 1, seeded)
    validate(document, settings)
    return Seeded(document, empty, clamped)


def _seed_cue(source: TimelineSource, cue: render_plan.SourceCue) -> tuple[EditCue, bool] | None:
    """조판 정리 한 번 · 밀리초 반올림 · 뒤집힌 끝은 시작으로 · 원본 밖은 원본 안으로."""
    text = render_plan.caption_text(cue.text)
    if not text.strip():
        return None
    start = milliseconds_half_up(cue.start)
    end = milliseconds_half_up(max(cue.end, cue.start))
    limit = source_total_ms(source)
    inside_start = min(max(start, 0), limit)
    inside_end = min(max(end, inside_start), limit)
    made = EditCue("", source.source_id, inside_start, inside_end, text, cue.utterance_id)
    return made, (inside_start, inside_end) != (start, end)


# ─── 전선 모양 ───────────────────────────────────────────────────────────


def to_payload(document: TimelineDocument) -> dict[str, object]:
    """브라우저와 저장 파일이 주고받는 모양 — 사실(시퀀스·원본)은 싣지 않는다."""
    return {
        "clips": [[c.source_id, c.start_frame, c.end_frame] for c in document.clips],
        "cues": [
            [c.cue_id, c.source_id, c.start_ms, c.end_ms, c.text, c.origin] for c in document.cues
        ],
        "nextCue": document.next_cue,
        "seeded": dict(document.seeded),
    }


def from_payload(
    payload: Mapping[str, object],
    facts: TimelineFacts,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> TimelineDocument:
    """받은 모양 → 문서. 모양이 틀리면 거부하고, 만든 문서는 `validate`를 지난다."""
    seeded = payload.get("seeded")
    if not isinstance(seeded, dict) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in seeded.items()
    ):
        raise TimelineEditError("타임라인 문서의 `seeded`가 원본 → 리비전 사전이 아니다")
    order = _source_order(facts)
    document = TimelineDocument(
        facts=facts,
        clips=tuple(_payload_clip(row) for row in _payload_list(payload, "clips")),
        cues=tuple(_payload_cue(row) for row in _payload_list(payload, "cues")),
        next_cue=_int(payload.get("nextCue"), "nextCue"),
        seeded=tuple(sorted(seeded.items(), key=lambda item: order.get(item[0], -1))),
    )
    validate(document, settings)
    return document


def _payload_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise TimelineEditError(f"타임라인 문서의 `{key}`가 목록이 아니다")
    return value


def _int(value: object, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TimelineEditError(f"{what}이(가) 정수가 아니다: {value!r}")
    return value


def _payload_clip(row: object) -> Clip:
    if not isinstance(row, list) or len(row) != 3 or not isinstance(row[0], str):
        raise TimelineEditError(f"클립 한 줄의 모양이 틀렸다: {row!r}")
    return Clip(row[0], _int(row[1], "클립 시작"), _int(row[2], "클립 끝"))


def _payload_cue(row: object) -> EditCue:
    if not isinstance(row, list) or len(row) != 6:
        raise TimelineEditError(f"자막 한 줄의 모양이 틀렸다: {row!r}")
    cue_id, source_id, start, end, text, origin = row
    if not all(isinstance(value, str) for value in (cue_id, source_id, text)):
        raise TimelineEditError(f"자막 아이디·원본·글이 문자열이 아니다: {row!r}")
    if origin is not None and not isinstance(origin, str):
        raise TimelineEditError(f"자막 원본 줄 아이디가 문자열이 아니다: {row!r}")
    return EditCue(
        str(cue_id),
        str(source_id),
        _int(start, "자막 시작"),
        _int(end, "자막 끝"),
        str(text),
        origin,
    )


# ─── 검사 ────────────────────────────────────────────────────────────────


def validate(document: TimelineDocument, settings: FinalEditSettings = FINAL_EDIT_SETTINGS) -> None:
    """문서 전체 검사 — 사실·틱 한계, 클립 차례, 자막, 다음 번호, 심은 리비전. 틀리면 거부한다."""
    _validate_facts(document.facts)
    _validate_clips(document)
    total = edited_frames(document) * sequence_frame_ticks(document.facts)
    if total >= EXACT_TICK_LIMIT:
        raise TimelineEditError("타임라인이 너무 길어 브라우저가 틱을 정확히 셀 수 없다")
    _validate_cues(document, settings)
    if document.next_cue < 1:
        raise TimelineEditError(f"다음 자막 번호가 1보다 작다: {document.next_cue}")
    unknown = [sid for sid, _ in document.seeded if sid not in _source_order(document.facts)]
    if unknown:
        raise TimelineEditError(f"심은 리비전의 원본이 사실에 없다: {', '.join(unknown)}")


def _validate_facts(facts: TimelineFacts) -> None:
    if facts.sequence.frame_duration <= 0 or not facts.sources:
        raise TimelineEditError("시퀀스 프레임 길이가 양수가 아니거나 원본이 없다")
    if len(_source_order(facts)) != len(facts.sources):
        raise TimelineEditError("원본 아이디가 겹친다")
    rate = tick_rate(facts)
    for source in facts.sources:
        if source.frame_duration <= 0 or source.frame_count <= 0:
            raise TimelineEditError(f"원본 {source.source_id}의 격자가 비었거나 틀렸다")
        if source.extent * rate >= EXACT_TICK_LIMIT:
            raise TimelineEditError(f"원본 {source.source_id}이 너무 길어 틱을 정확히 셀 수 없다")


def _validate_clips(document: TimelineDocument) -> None:
    """원본 안 · 비지 않음 · 같은 원본은 붙어 있고 원본 차례로 겹치지 않는다."""
    finished: set[str] = set()
    previous: Clip | None = None
    for clip in document.clips:
        source = _source(document.facts, clip.source_id)
        if not 0 <= clip.start_frame < clip.end_frame <= source.frame_count:
            raise TimelineEditError(
                f"클립 {clip.source_id} [{clip.start_frame}, {clip.end_frame})이 원본 "
                f"{source.frame_count}프레임 안의 빈 아닌 구간이 아니다"
            )
        if previous is not None and previous.source_id == clip.source_id:
            if clip.start_frame < previous.end_frame:
                raise TimelineEditError(f"원본 {clip.source_id}의 클립이 겹치거나 거꾸로 간다")
        elif clip.source_id in finished:
            raise TimelineEditError(f"원본 {clip.source_id}의 클립이 타임라인에서 떨어져 있다")
        if previous is not None and previous.source_id != clip.source_id:
            finished.add(previous.source_id)
        previous = clip


def _validate_cues(document: TimelineDocument, settings: FinalEditSettings) -> None:
    if len(document.cues) > settings.max_cues:
        raise TimelineEditError(f"자막이 {settings.max_cues}개를 넘는다: {len(document.cues)}")
    seen: set[str] = set()
    for cue in document.cues:
        if not valid_cue_id(cue.cue_id) or cue.cue_id in seen:
            raise TimelineEditError(f"자막 아이디가 틀렸거나 겹친다: {cue.cue_id!r}")
        seen.add(cue.cue_id)
        limit = source_total_ms(_source(document.facts, cue.source_id))
        if not 0 <= cue.start_ms <= cue.end_ms <= limit:
            raise TimelineEditError(
                f"자막 {cue.cue_id}의 시각 [{cue.start_ms}, {cue.end_ms}]ms가 원본 {limit}ms 밖이다"
            )
        _validate_text(cue, settings)
    if list(document.cues) != list(_sorted(document.facts, document.cues)):
        raise TimelineEditError("자막이 원본·시각 차례로 놓이지 않았다")


def _validate_text(cue: EditCue, settings: FinalEditSettings) -> None:
    text = cue.text
    if len(text) > settings.max_text_chars:
        raise TimelineEditError(f"자막 {cue.cue_id}의 글이 {settings.max_text_chars}자를 넘는다")
    if "\r" in text or "\n\n" in text or text.startswith("\n") or text.endswith("\n"):
        raise TimelineEditError(f"자막 {cue.cue_id}의 글에 빈 줄이나 \\r이 있다 — SRT가 깨진다")


def _sorted(facts: TimelineFacts, cues: Sequence[EditCue]) -> tuple[EditCue, ...]:
    order = _source_order(facts)
    return tuple(
        sorted(cues, key=lambda c: (order.get(c.source_id, -1), c.start_ms, c.end_ms, c.cue_id))
    )


# ─── 배치 ────────────────────────────────────────────────────────────────


def layout(document: TimelineDocument) -> tuple[Span, ...]:
    """클립마다 출력 자리 — 렌더 계획의 `sequence_edges` 그대로(화면 = 출력)."""
    pieces = []
    for clip in document.clips:
        source = _source(document.facts, clip.source_id)
        length = (clip.end_frame - clip.start_frame) * source.frame_duration
        pieces.append((length, clip.end_frame == source.frame_count))
    edges, _ = render_plan.sequence_edges(pieces, document.facts.sequence.frame_duration)
    return tuple(
        Span(i, c.source_id, c.start_frame, c.end_frame, edges[i], edges[i + 1] - edges[i])
        for i, c in enumerate(document.clips)
    )


def edited_frames(document: TimelineDocument) -> int:
    spans = layout(document)
    return spans[-1].out_start + spans[-1].out_frames if spans else 0


def _offset_frames(document: TimelineDocument, span: Span, edited_frame: int) -> int:
    """클립 안 시퀀스 프레임 → 그 원본의 프레임 수(내림) — 레이트가 달라도 같은 규칙이다."""
    offset_ticks = (edited_frame - span.out_start) * sequence_frame_ticks(document.facts)
    return offset_ticks // source_frame_ticks(document.facts, span.source_id)


def source_point(document: TimelineDocument, edited_frame: int) -> tuple[int, int]:
    """타임라인 프레임 → (클립 번호, 원본 프레임). 출력이 원본보다 조금 길면 끝 프레임이다."""
    for span in layout(document):
        if span.out_start <= edited_frame < span.out_start + span.out_frames:
            frame = span.start_frame + _offset_frames(document, span, edited_frame)
            return span.index, min(frame, span.end_frame - 1)
    raise TimelineEditError(f"타임라인 프레임 {edited_frame}이 타임라인 밖이다")


def _tick_spans(document: TimelineDocument) -> list[tuple[str, int, int, int, int]]:
    """클립마다 (원본, 원본 시작 틱, 원본 끝 틱, 출력 시작 틱, 출력 끝 틱)."""
    rate = tick_rate(document.facts)
    seq = int(document.facts.sequence.frame_duration * rate)
    units = {
        source.source_id: int(source.frame_duration * rate) for source in document.facts.sources
    }
    rows = []
    for span in layout(document):
        unit = units[span.source_id]
        rows.append(
            (
                span.source_id,
                span.start_frame * unit,
                span.end_frame * unit,
                span.out_start * seq,
                (span.out_start + span.out_frames) * seq,
            )
        )
    return rows


def edited_ticks(
    document: TimelineDocument, source_id: str, ms: int, side: str = SIDE_START
) -> int:
    """원본 밀리초 → 타임라인 틱 — 렌더 계획의 `_piece`와 같은 규칙.

    클립 안이면 원본 시각 + 그 클립의 밀기(출력 끝을 넘지 않는다). 그 원본 클립 사이(뺀 구간)는
    시작이면 다음 클립 시작, 끝이면 앞 클립의 원본 끝을 민 자리다 — 레이트가 달라 출력이 원본보다
    조금 길면 둘이 다르다. 첫 클립 앞은 첫 클립 시작, 마지막 클립 뒤는 그 클립의 원본 끝을 민
    자리다.
    """
    return _edited_in(_tick_spans(document), ms_ticks(document.facts), source_id, ms, side)


def _edited_in(
    rows: Sequence[tuple[str, int, int, int, int]], unit: int, source_id: str, ms: int, side: str
) -> int:
    """`edited_ticks`의 몸 — 자막 여럿을 옮기는 쪽(`project`)이 클립 틱을 한 번만 잰다."""
    point = ms * unit
    mine = [row for row in rows if row[0] == source_id]
    if not mine:
        raise TimelineEditError(f"원본 {source_id}의 클립이 타임라인에 없다")
    previous: tuple[str, int, int, int, int] | None = None
    for row in mine:
        _, start, end, out_start, out_end = row
        if point < start:
            if side == SIDE_END and previous is not None:
                return _mapped_end(previous)
            return out_start
        if point <= end:
            return min(out_start + point - start, out_end)
        previous = row
    return _mapped_end(mine[-1])


def _mapped_end(row: tuple[str, int, int, int, int]) -> int:
    _, start, end, out_start, out_end = row
    return min(out_start + end - start, out_end)


def source_ms_at(document: TimelineDocument, ticks: int, side: str) -> tuple[str, int]:
    """타임라인 틱 → (원본, 밀리초 반내림). 컷 위는 시작이면 다음 클립, 끝이면 앞 클립 쪽이다."""
    rows = _tick_spans(document)
    if not rows or not 0 <= ticks <= rows[-1][4]:
        raise TimelineEditError(f"타임라인 시각 {ticks}틱이 타임라인 밖이다")
    chosen = rows[-1] if side == SIDE_START else rows[0]
    for row in rows:
        if side == SIDE_START and row[3] <= ticks < row[4]:
            chosen = row
            break
        if side == SIDE_END and row[3] < ticks <= row[4]:
            chosen = row
            break
    source_id, start, end, out_start, _ = chosen
    point = min(start + ticks - out_start, end)
    return source_id, _ms_half_down(point, ms_ticks(document.facts))


def removed_ranges(document: TimelineDocument) -> tuple[tuple[str, int, int], ...]:
    """원본마다 타임라인에 없는 프레임 구간 — 남긴 클립과 함께 원본 전체를 덮는다(D35)."""
    gaps = []
    for source in document.facts.sources:
        cursor = 0
        for clip in document.clips:
            if clip.source_id != source.source_id:
                continue
            if clip.start_frame > cursor:
                gaps.append((source.source_id, cursor, clip.start_frame))
            cursor = clip.end_frame
        if cursor < source.frame_count:
            gaps.append((source.source_id, cursor, source.frame_count))
    return tuple(gaps)


# ─── 투영 · SRT · 경고 ───────────────────────────────────────────────────


def project(document: TimelineDocument) -> Projection:
    """타임라인의 자막 — 그 원본 클립과 양의 길이로 겹치는 자막(길이 0이면 클립 안의 것)."""
    unit = ms_ticks(document.facts)
    rate = tick_rate(document.facts)
    rows = _tick_spans(document)
    visible: list[tuple[int, int, int, ProjectedCue]] = []
    hidden: list[EditCue] = []
    for number, cue in enumerate(document.cues):
        mine = [row for row in rows if row[0] == cue.source_id]
        start, end = cue.start_ms * unit, cue.end_ms * unit
        kept = sum(max(0, min(end, row[2]) - max(start, row[1])) for row in mine)
        point = start == end and any(row[1] <= start < row[2] for row in mine)
        if kept == 0 and not point:
            hidden.append(cue)
            continue
        out_start = _edited_in(rows, unit, cue.source_id, cue.start_ms, SIDE_START)
        out_end = _edited_in(rows, unit, cue.source_id, cue.end_ms, SIDE_END)
        made = ProjectedCue(
            cue.cue_id,
            cue.source_id,
            Fraction(out_start, rate),
            Fraction(out_end, rate),
            cue.text,
            kept < end - start,
        )
        visible.append((out_start, out_end, number, made))
    visible.sort(key=lambda row: row[:3])
    total = rows[-1][4] // sequence_frame_ticks(document.facts) if rows else 0
    return Projection(tuple(row[3] for row in visible), tuple(hidden), total)


def render_cues(
    document: TimelineDocument,
) -> tuple[tuple[render_plan.ProjectedCue, ...], render_plan.CueCounts]:
    """타임라인 자막을 렌더 계획의 장으로(D130) — 길이 0인 장은 D115 그대로 늘린다.

    조각 번호는 장의 시작이 놓인 클립이다(경계면 뒤 클립). 센 것: 원본 = 문서의 자막 수 = 보인 것 +
    가린 것(`dropped`), 잘린 것(`clipped`), 늘린 것·길이 0으로 남은 것.
    """
    projection = project(document)
    rate = tick_rate(document.facts)
    edges = [span.out_start * sequence_frame_ticks(document.facts) for span in layout(document)]
    ordered = [
        render_plan.ProjectedCue(
            c.cue_id,
            c.source_id,
            max(0, bisect.bisect_right(edges, c.start * rate) - 1),
            c.start,
            c.end,
            c.text,
            c.trimmed,
        )
        for c in projection.cues
    ]
    total = projection.total_frames * document.facts.sequence.frame_duration
    lengthened, stretched = render_plan.lengthen_cues(ordered, total)
    zero = sum(1 for cue in lengthened if _ms(cue.start) == _ms(cue.end))
    counts = render_plan.CueCounts(
        source=len(document.cues),
        kept=len(lengthened),
        clipped=sum(1 for cue in lengthened if cue.clipped),
        dropped=len(projection.hidden),
        zero_length=zero,
        lengthened=stretched,
    )
    return lengthened, counts


def _ms(seconds: Fraction) -> int:
    return milliseconds_half_up(seconds)


def srt_text(document: TimelineDocument) -> str:
    """타임라인 SRT — 초안 SRT와 같은 바이트 꼴, 길이 0인 장은 D115 그대로 늘린다."""
    return render_plan.cues_srt_text(render_cues(document)[0])


def warnings(
    document: TimelineDocument, settings: FinalEditSettings = FINAL_EDIT_SETTINGS
) -> tuple[CueWarning, ...]:
    """보이는 자막의 경고(빈 글·길이 0·짧음·김·빠름·겹침·좁은 틈) — 저장을 막지 않는다(D117)."""
    rate = tick_rate(document.facts)
    cues = project(document).cues
    found: list[CueWarning] = []
    for index, cue in enumerate(cues):
        following = cues[index + 1] if index + 1 < len(cues) else None
        kinds = _cue_warnings(cue, following, rate, settings)
        found += [CueWarning(cue.cue_id, kind) for kind in kinds]
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


# ─── 클립 연산(자막은 그대로 — 투영이 따라온다) ───────────────────────────


def _with_clips(document: TimelineDocument, clips: Sequence[Clip]) -> TimelineDocument:
    changed = replace(document, clips=tuple(clips))
    _validate_clips(changed)
    return changed


def _clip(document: TimelineDocument, index: int) -> Clip:
    if not 0 <= index < len(document.clips):
        raise TimelineEditError(f"클립 번호 {index}가 없다")
    return document.clips[index]


def blade(document: TimelineDocument, edited_frame: int) -> TimelineDocument:
    """타임라인 프레임에서 클립을 둘로 — 편집 점 위거나 원본 격자에서 한 점이면 거부한다."""
    index, cut = source_point(document, edited_frame)
    clip = document.clips[index]
    span = layout(document)[index]
    if edited_frame == span.out_start or not clip.start_frame < cut < clip.end_frame:
        raise TimelineEditError(f"타임라인 프레임 {edited_frame}은 클립 안이 아니다(편집 점 위다)")
    pieces = (replace(clip, end_frame=cut), replace(clip, start_frame=cut))
    return _with_clips(document, document.clips[:index] + pieces + document.clips[index + 1 :])


def ripple_delete(
    document: TimelineDocument, edited_start: int, edited_end: int
) -> TimelineDocument:
    """타임라인 프레임 `[edited_start, edited_end)`를 지우고 뒤를 당긴다 — 원본 격자로 내림."""
    if not 0 <= edited_start < edited_end <= edited_frames(document):
        raise TimelineEditError(
            f"지울 범위 [{edited_start}, {edited_end})이 타임라인 밖이거나 비었다"
        )
    kept: list[Clip] = []
    for span, clip in zip(layout(document), document.clips, strict=True):
        low = max(edited_start, span.out_start)
        high = min(edited_end, span.out_start + span.out_frames)
        if low >= high:
            kept.append(clip)
            continue
        head = clip.start_frame + _offset_frames(document, span, low)
        tail = clip.start_frame + _offset_frames(document, span, high)
        if head > clip.start_frame:
            kept.append(replace(clip, end_frame=min(head, clip.end_frame)))
        if high < span.out_start + span.out_frames and tail < clip.end_frame:
            kept.append(replace(clip, start_frame=tail))
    return _with_clips(document, kept)


def delete_clips(document: TimelineDocument, indexes: Sequence[int]) -> TimelineDocument:
    chosen = set(indexes)
    if not chosen or not chosen <= set(range(len(document.clips))):
        raise TimelineEditError(f"지울 클립 번호가 틀렸다: {sorted(chosen)}")
    return _with_clips(document, [c for i, c in enumerate(document.clips) if i not in chosen])


def clip_bounds(document: TimelineDocument, index: int) -> tuple[int, int]:
    """클립 가장자리가 갈 수 있는 원본 프레임 — 같은 원본의 이웃 클립 사이, 없으면 원본 처음·끝."""
    clip = _clip(document, index)
    before = document.clips[index - 1] if index > 0 else None
    after = document.clips[index + 1] if index + 1 < len(document.clips) else None
    floor = before.end_frame if before and before.source_id == clip.source_id else 0
    ceiling = (
        after.start_frame
        if after and after.source_id == clip.source_id
        else _source(document.facts, clip.source_id).frame_count
    )
    return floor, ceiling


def set_clip_start(
    document: TimelineDocument,
    index: int,
    source_frame: int,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> TimelineDocument:
    """클립 시작을 원본 프레임으로 — 늘리면 원본의 그 앞이 들어온다(추천 밖이어도)."""
    clip = _clip(document, index)
    floor, _ = clip_bounds(document, index)
    if not floor <= source_frame <= clip.end_frame - settings.min_clip_frames:
        raise TimelineEditError(f"클립 시작 {source_frame}이 [{floor}, {clip.end_frame}) 밖이다")
    clips = list(document.clips)
    clips[index] = replace(clip, start_frame=source_frame)
    return _with_clips(document, clips)


def set_clip_end(
    document: TimelineDocument,
    index: int,
    source_frame: int,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> TimelineDocument:
    """클립 끝을 원본 프레임으로 — 늘리면 원본의 그 뒤가 들어온다(추천 밖이어도)."""
    clip = _clip(document, index)
    _, ceiling = clip_bounds(document, index)
    if not clip.start_frame + settings.min_clip_frames <= source_frame <= ceiling:
        raise TimelineEditError(f"클립 끝 {source_frame}이 ({clip.start_frame}, {ceiling}] 밖이다")
    clips = list(document.clips)
    clips[index] = replace(clip, end_frame=source_frame)
    return _with_clips(document, clips)


def join_clips(document: TimelineDocument, index: int) -> TimelineDocument:
    """같은 원본에서 맞닿은 두 클립(끝 = 다음 시작)을 하나로 — 블레이드의 거꾸로."""
    clip = _clip(document, index)
    following = _clip(document, index + 1)
    if following.source_id != clip.source_id or clip.end_frame != following.start_frame:
        raise TimelineEditError(
            f"클립 {index}와 다음 클립은 같은 원본에서 맞닿지 않아 합칠 수 없다"
        )
    merged = replace(clip, end_frame=following.end_frame)
    return _with_clips(document, document.clips[:index] + (merged,) + document.clips[index + 2 :])


# ─── 자막 연산(원본 밀리초) ──────────────────────────────────────────────


def _cue_index(document: TimelineDocument, cue_id: str) -> int:
    for index, cue in enumerate(document.cues):
        if cue.cue_id == cue_id:
            return index
    raise TimelineEditError(f"자막 {cue_id}이(가) 없다")


def _with_cues(
    document: TimelineDocument, cues: Sequence[EditCue], next_cue: int | None = None
) -> TimelineDocument:
    return replace(
        document,
        cues=_sorted(document.facts, cues),
        next_cue=document.next_cue if next_cue is None else next_cue,
    )


def _check_times(document: TimelineDocument, source_id: str, start_ms: int, end_ms: int) -> None:
    limit = source_total_ms(_source(document.facts, source_id))
    if not 0 <= start_ms <= end_ms <= limit:
        raise TimelineEditError(
            f"자막 시각 [{start_ms}, {end_ms}]ms가 [0, {limit}] 밖이거나 뒤집혔다"
        )


def cue_set_times(
    document: TimelineDocument, cue_id: str, start_ms: int, end_ms: int
) -> TimelineDocument:
    index = _cue_index(document, cue_id)
    cue = document.cues[index]
    _check_times(document, cue.source_id, start_ms, end_ms)
    cues = list(document.cues)
    cues[index] = replace(cue, start_ms=start_ms, end_ms=end_ms)
    return _with_cues(document, cues)


def cue_shift(document: TimelineDocument, cue_id: str, delta_ms: int) -> TimelineDocument:
    """자막을 통째로 민다 — 그 원본의 처음·끝에서 멈춘다(길이는 그대로)."""
    cue = document.cues[_cue_index(document, cue_id)]
    limit = source_total_ms(_source(document.facts, cue.source_id))
    delta = min(max(delta_ms, -cue.start_ms), limit - cue.end_ms)
    return cue_set_times(document, cue_id, cue.start_ms + delta, cue.end_ms + delta)


def cue_set_text(
    document: TimelineDocument,
    cue_id: str,
    text: str,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> TimelineDocument:
    index = _cue_index(document, cue_id)
    cues = list(document.cues)
    cues[index] = replace(cues[index], text=text)
    _validate_text(cues[index], settings)
    return _with_cues(document, cues)


def cue_delete(document: TimelineDocument, cue_id: str) -> TimelineDocument:
    index = _cue_index(document, cue_id)
    return _with_cues(document, document.cues[:index] + document.cues[index + 1 :])


def cues_delete_inside(
    document: TimelineDocument, start_ticks: int, end_ticks: int
) -> TimelineDocument:
    """타임라인 `[start, end]`(틱) 안에 다 들어간 보이는 자막을 지운다(영상은 그대로)."""
    rate = tick_rate(document.facts)
    inside = {
        cue.cue_id
        for cue in project(document).cues
        if start_ticks <= cue.start * rate and cue.end * rate <= end_ticks
    }
    if not inside:
        raise TimelineEditError("범위 안에 다 들어간 보이는 자막이 없다")
    return _with_cues(document, [cue for cue in document.cues if cue.cue_id not in inside])


def cue_split(
    document: TimelineDocument, cue_id: str, at_ms: int, left_text: str, right_text: str
) -> TimelineDocument:
    """자막을 원본 `at_ms`에서 둘로 — 왼쪽은 아이디·원본 줄을 지키고 오른쪽은 새 자막이다."""
    index = _cue_index(document, cue_id)
    cue = document.cues[index]
    if not cue.start_ms < at_ms < cue.end_ms:
        raise TimelineEditError(f"나눌 시각 {at_ms}ms가 자막 {cue_id} 안이 아니다")
    left = replace(cue, end_ms=at_ms, text=left_text)
    right = EditCue(
        f"{NEW_PREFIX}{document.next_cue}", cue.source_id, at_ms, cue.end_ms, right_text, None
    )
    cues = [*document.cues[:index], left, right, *document.cues[index + 1 :]]
    return _with_cues(document, cues, document.next_cue + 1)


def cue_merge_next(document: TimelineDocument, cue_id: str) -> TimelineDocument:
    """보이는 차례의 다음 자막과 하나로 — 같은 원본일 때만(원본이 다르면 시각이 이어지지 않는다)."""
    visible = [cue.cue_id for cue in project(document).cues]
    if cue_id not in visible or visible.index(cue_id) + 1 >= len(visible):
        raise TimelineEditError(f"자막 {cue_id} 다음에 합칠 보이는 자막이 없다")
    first = document.cues[_cue_index(document, cue_id)]
    second = document.cues[_cue_index(document, visible[visible.index(cue_id) + 1])]
    if second.source_id != first.source_id:
        raise TimelineEditError(f"자막 {cue_id} 다음 자막은 다른 원본이라 합칠 수 없다")
    merged = replace(
        first,
        start_ms=min(first.start_ms, second.start_ms),
        end_ms=max(first.end_ms, second.end_ms),
        text=" ".join(part for part in (first.text, second.text) if part),
    )
    cues = [merged if cue is first else cue for cue in document.cues if cue is not second]
    return _with_cues(document, cues)


def cue_insert(
    document: TimelineDocument, source_id: str, start_ms: int, end_ms: int, text: str
) -> TimelineDocument:
    """새 자막(`n{next_cue}`)을 그 원본 시계에 넣는다."""
    _check_times(document, source_id, start_ms, end_ms)
    cue = EditCue(f"{NEW_PREFIX}{document.next_cue}", source_id, start_ms, end_ms, text, None)
    return _with_cues(document, [*document.cues, cue], document.next_cue + 1)
