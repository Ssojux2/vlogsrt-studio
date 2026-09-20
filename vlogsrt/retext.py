"""글자 교정 — 시각은 whisper, 글자는 gpt-transcribe(D37).

gpt-transcribe는 글자가 낫지만 시각을 주지 않는다. 그래서 **whisper 세그먼트의 시각과
경계는 그대로 두고, 그 세그먼트에 대응하는 gpt 글자로 글자만 갈아낀다.** 이 모듈은 어느
세그먼트를 무엇으로 갈지 **계획만** 세운다 — 큐를 만들지도, 골격을 고치지도 않는다.

D24와 다른 점이 셋이고, 셋 다 안전선이다:

- **세그먼트 사이에 낀 gpt 글자는 출력하지 않는다.** 그 글자에는 시각이 없다. 어느
  세그먼트에 붙이는 순간 그 세그먼트의 시각을 준 것이 된다 — D24가 무너진 모양이다.
  whisper가 통째로 놓친 말은 그래서 되살리지 못하고, `unplaced`로 기록만 남는다.
- **세그먼트마다 게이트를 통과해야 교체한다.** 조각 유사도만 보면 멀쩡한 조각 안에서
  큐 하나에 수십 배가 몰린다. 통과하지 못하면 whisper 글자가 그대로 나간다.
- **한 gpt 글자는 한 세그먼트에만 간다.** 경계 위치로 gpt 글자를 칸막이하므로, 불통과
  세그먼트의 몫은 이웃에게도 가지 않는다 — 같은 말이 두 번 나가지 않는다.

순수 함수 모듈이다. 입력은 전부 값이고 I/O도 API도 없다.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import Config
from .hangul import is_ignorable, to_nfc
from .models import (
    REPAIR_COLLAPSED,
    REPAIR_KEPT,
    RETEXT_APPLIED,
    RETEXT_OFF,
    RETEXT_UNAVAILABLE,
    Repair,
    RetextChunk,
    RetextExcluded,
    RetextRecord,
    RetextRejection,
    RetextUnplaced,
    Segment,
    Span,
)

__all__ = [
    "REASON_CHUNK",
    "REASON_EMPTY",
    "REASON_MATCH",
    "REASON_NO_LETTERS",
    "REASON_TOO_LONG",
    "REASON_TOO_SHORT",
    "ChunkText",
    "RetextRecord",
    "plan",
    "switched_off",
    "unavailable",
]

REASON_CHUNK = "chunk_similarity"  # 조각 전체가 두 모델 사이에서 너무 다르다
REASON_NO_LETTERS = "no_whisper_letters"  # whisper 글자가 부호뿐이라 대응할 자리가 없다
REASON_EMPTY = "empty"  # 대응된 gpt 글자가 없다
REASON_TOO_LONG = "too_long"  # 시각 없는 말이 이 세그먼트로 몰렸다
REASON_TOO_SHORT = "too_short"  # gpt가 whisper의 말을 지웠다
REASON_MATCH = "match"  # 같은 자리에서 같은 글자를 들은 비율이 낮다

# 되풀이를 회복하지 못한 구간. 그 글자는 이미 whisper가 무너진 자리라 대응 자체를 믿을 수 없다.
_UNTOUCHABLE = frozenset({REPAIR_COLLAPSED, REPAIR_KEPT})


@dataclass(frozen=True, slots=True)
class ChunkText:
    """두 번째 모델이 조각 하나에 준 글자. 응답 형태를 모르게 하려고 값만 받는다."""

    index: int
    offset: float
    text: str


def unavailable(error: str) -> RetextRecord:
    return RetextRecord(status=RETEXT_UNAVAILABLE, error=error)


def switched_off() -> RetextRecord:
    return RetextRecord(status=RETEXT_OFF)


def plan(
    spine: Sequence[Segment],
    chunks: Sequence[ChunkText],
    duration: float,
    repairs: Sequence[Repair],
    cfg: Config,
) -> RetextRecord:
    """세그먼트마다 교체 글자를 정한다. **시각은 읽기만 한다** — 조각 배정과 되풀이 제외에만."""
    ordered = sorted(chunks, key=lambda chunk: chunk.offset)
    if not ordered:
        return unavailable("두 번째 모델의 조각 응답이 하나도 없다")
    holds = _holds(spine, repairs)
    held = {seg_id for hold in holds for seg_id in hold.seg_ids}
    outcomes = [
        _plan_chunk(chunk, span, members, held, cfg)
        for chunk, span, members in zip(
            ordered, _chunk_spans(ordered, duration), _assign(spine, ordered), strict=True
        )
    ]
    letters = {seg_id: count for outcome in outcomes for seg_id, count in outcome.letters.items()}
    unplaced = tuple(item for outcome in outcomes for item in outcome.unplaced)
    return RetextRecord(
        status=RETEXT_APPLIED,
        texts=tuple(sorted(pair for outcome in outcomes for pair in outcome.texts)),
        chunks=tuple(outcome.record for outcome in outcomes),
        rejections=tuple(item for outcome in outcomes for item in outcome.rejections),
        unplaced=unplaced,
        excluded=tuple(_count_held(hold, letters, unplaced) for hold in holds),
    )


# ─────────────────────────────────────────────────────────────
#  조각 배정 · 되풀이 제외
# ─────────────────────────────────────────────────────────────


def _chunk_spans(ordered: Sequence[ChunkText], duration: float) -> list[Span]:
    """조각 오프셋으로 시간축을 나눈다. 마지막 구간은 영상 끝까지. `verify`와 같은 규칙이다."""
    offsets = [chunk.offset for chunk in ordered]
    ends = [*offsets[1:], max(duration, offsets[-1])]
    return list(zip(offsets, ends, strict=True))


def _assign(spine: Sequence[Segment], ordered: Sequence[ChunkText]) -> list[list[Segment]]:
    """시작 시각으로 조각을 정한다. 첫 조각 앞이면 첫 조각에 넣는다 — 빠지는 세그먼트가 없다."""
    offsets = [chunk.offset for chunk in ordered]
    groups: list[list[Segment]] = [[] for _ in ordered]
    for segment in spine:
        groups[max(0, bisect_right(offsets, segment.start) - 1)].append(segment)
    return groups


@dataclass(frozen=True, slots=True)
class _Hold:
    span: Span
    outcome: str
    seg_ids: tuple[int, ...]
    span_missing: bool


def _holds(spine: Sequence[Segment], repairs: Sequence[Repair]) -> list[_Hold]:
    """회복 못 한 되풀이와 **시간이 겹치는** 세그먼트.

    재전사가 번호를 다시 매기므로 id로는 못 찾는다.
    """
    holds: list[_Hold] = []
    for repair in repairs:
        if repair.outcome not in _UNTOUCHABLE:
            continue
        missing = repair.span[1] <= repair.span[0]
        low, high = (repair.repeat.start, repair.repeat.end) if missing else repair.span
        seg_ids = tuple(segment.id for segment in spine if _overlaps(segment, low, high))
        holds.append(_Hold((low, high), repair.outcome, seg_ids, missing))
    return holds


def _overlaps(segment: Segment, low: float, high: float) -> bool:
    if segment.start >= high:
        return False
    return segment.end > low or (segment.end == segment.start and segment.start >= low)


def _count_held(
    hold: _Hold, letters: dict[int, int], unplaced: Sequence[RetextUnplaced]
) -> RetextExcluded:
    """그 구간에 gpt가 들은 글자 수. 크면 접힌 자리에 실제 발화가 있었다는 신호다(규칙 9)."""
    inside = set(hold.seg_ids)
    beside = sum(item.chars for item in unplaced if item.before in inside or item.after in inside)
    return RetextExcluded(
        span=hold.span,
        outcome=hold.outcome,
        seg_ids=hold.seg_ids,
        gpt_chars=sum(letters.get(seg_id, 0) for seg_id in hold.seg_ids) + beside,
        span_missing=hold.span_missing,
    )


# ─────────────────────────────────────────────────────────────
#  글자 접기 — 비교는 정규형으로, 자르기는 원문으로
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Folded:
    """비교용 글자와 원문 위치의 대응표. 출력은 **원문에서** 잘라야 gpt의 부호·띄어쓰기가 산다."""

    raw: str
    letters: str
    where: tuple[int, ...]  # letters[k]가 raw의 몇 번째 글자에서 왔나
    stops: tuple[int, ...]  # 자를 수 있는 raw 위치 — 어절이 시작하는 곳

    def raw_at(self, letter: int) -> int:
        """letters 위치 → raw 위치. 앞머리·꼬리의 부호는 첫·마지막 몫으로 간다."""
        if letter <= 0:
            return 0
        if letter >= len(self.letters):
            return len(self.raw)
        return self.where[letter]

    def snap(self, position: int) -> int:
        """가장 가까운 어절 경계로. 같은 거리면 앞쪽 — 단조라서 칸막이 순서가 뒤집히지 않는다."""
        index = bisect_left(self.stops, position)
        if index < len(self.stops) and self.stops[index] == position:
            return position
        left, right = self.stops[index - 1], self.stops[index]
        return left if position - left <= right - position else right

    def letters_before(self, position: int) -> int:
        return bisect_left(self.where, position)


def _fold(text: str) -> _Folded:
    """`hangul.strip_for_compare`와 같은 규칙으로 접되, 글자마다 원문 위치를 남긴다."""
    raw = to_nfc(text)
    letters: list[str] = []
    where: list[int] = []
    for position, char in enumerate(raw):
        if is_ignorable(char):
            continue
        for folded in char.casefold():
            letters.append(folded)
            where.append(position)
    stops = [
        position
        for position in range(len(raw) + 1)
        if position in (0, len(raw))
        or (is_ignorable(raw[position - 1]) and not is_ignorable(raw[position]))
    ]
    return _Folded(raw=raw, letters="".join(letters), where=tuple(where), stops=tuple(stops))


# ─────────────────────────────────────────────────────────────
#  정렬 — whisper 글자 위치마다 gpt 글자 위치
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Alignment:
    """whisper 경계 p에서 gpt를 자를 자리.

    `left[p]`~`right[p]` 사이가 그 자리에 **끼어든** gpt 글자다.
    """

    left: tuple[int, ...]
    right: tuple[int, ...]
    equal: tuple[tuple[int, int, int], ...]  # (whisper 시작, whisper 끝, gpt 시작)
    similarity: float


def _align(whisper: str, gpt: str, edges: frozenset[int]) -> _Alignment:
    matcher = SequenceMatcher(None, whisper, gpt, autojunk=False)
    left = [-1] * (len(whisper) + 1)
    right = [-1] * (len(whisper) + 1)

    def touch(position: int, cut: int) -> None:
        if left[position] < 0:
            left[position] = cut
        right[position] = cut

    touch(0, 0)
    opcodes = _slide_inserts(matcher.get_opcodes(), gpt, edges)
    equal: list[tuple[int, int, int]] = []
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "insert":
            touch(i1, j1)
            touch(i1, j2)
            continue
        if tag == "equal":
            equal.append((i1, i2, j1))
        # replace 블록 안에서는 글자 위치 비례로 나눈다. 시간이 아니라 글자 위치다 —
        # 블록 전체가 블록이 걸친 세그먼트에 빠짐없이 나뉘고, 잉여를 버리지 않는다.
        for position in range(i1, i2 + 1):
            touch(position, j1 + round((position - i1) * (j2 - j1) / (i2 - i1)))
    touch(len(whisper), len(gpt))
    return _Alignment(tuple(left), tuple(right), tuple(equal), matcher.ratio())


_Opcode = tuple[str, int, int, int, int]


def _slide_inserts(opcodes: Sequence[_Opcode], gpt: str, edges: frozenset[int]) -> list[_Opcode]:
    """세그먼트 안쪽에 떨어진 삽입을 같은 글자 위로 미끄러뜨려 가장 가까운 경계에 붙인다.

    `첫번째말|두번째말` 대 `첫번째말 그사이에…놓친말 두번째말`에서 matcher는 `말두번째말`을
    한 덩어리로 잡아 삽입을 `첫번째|말` 사이에 둔다. 글자가 같아 어느 쪽이든 정렬은 맞지만,
    안쪽에 두면 세그먼트 사이의 말이 세그먼트 몫이 되어 멀쩡한 교정까지 막힌다.
    """
    ops = list(opcodes)
    index = 0
    while index < len(ops):
        op = ops[index]
        shift = (
            0
            if op[0] != "insert" or op[1] in edges
            else _nearest_edge_shift(ops, index, gpt, edges)
        )
        if shift:
            # 옮긴 삽입은 이제 경계 위라, 앞에 equal이 하나 생겨 다시 만나도 건너뛴다.
            ops = _shifted(ops, index, shift)
        index += 1
    return [op for op in ops if op[1] != op[2] or op[3] != op[4]]


def _nearest_edge_shift(ops: Sequence[_Opcode], index: int, gpt: str, edges: frozenset[int]) -> int:
    """경계에 닿는 가장 짧은 이동. 앞뒤 equal 안에서, 삽입 글자가 되풀이되는 만큼만 갈 수 있다."""
    _, position, _, j1, j2 = ops[index]
    after = ops[index + 1] if index + 1 < len(ops) and ops[index + 1][0] == "equal" else None
    before = ops[index - 1] if index > 0 and ops[index - 1][0] == "equal" else None
    rightward = leftward = None
    step = 0
    while after and step < after[2] - position and gpt[j1 + step] == gpt[j2 + step]:
        step += 1
        if position + step in edges:
            rightward = step
            break
    step = 0
    while before and step < position - before[1] and gpt[j1 - 1 - step] == gpt[j2 - 1 - step]:
        step += 1
        if position - step in edges:
            leftward = -step
            break
    choices = [shift for shift in (rightward, leftward) if shift is not None]
    return min(choices, key=abs) if choices else 0


def _shifted(ops: list[_Opcode], index: int, shift: int) -> list[_Opcode]:
    """삽입을 `shift`만큼 옮기고, 양옆 equal을 그만큼 늘이고 줄인다. 글자 짝은 그대로 유지된다."""
    _, position, _, j1, j2 = ops[index]
    moved: _Opcode = ("insert", position + shift, position + shift, j1 + shift, j2 + shift)
    head, tail = (ops[: index - 1] if index > 0 else []), ops[index + 2 :]
    before, after = (
        ops[index - 1] if index > 0 else None,
        ops[index + 1] if index + 1 < len(ops) else None,
    )
    if shift > 0 and after is not None:
        grown = _grow_before(before, position, j1, shift)
        shrunk: _Opcode = ("equal", position + shift, after[2], j2 + shift, after[4])
        return [*head, *grown, moved, shrunk, *tail]
    if before is not None and after is not None:
        cut: _Opcode = ("equal", before[1], position + shift, before[3], j1 + shift)
        extended = _grow_after(after, position, j2, shift)
        return [*head, cut, moved, *extended, *tail]
    return ops


def _grow_before(before: _Opcode | None, position: int, j1: int, shift: int) -> list[_Opcode]:
    if before is not None and before[0] == "equal":
        return [("equal", before[1], position + shift, before[3], j1 + shift)]
    created: _Opcode = ("equal", position, position + shift, j1, j1 + shift)
    return [before, created] if before is not None else [created]


def _grow_after(after: _Opcode, position: int, j2: int, shift: int) -> list[_Opcode]:
    if after[0] == "equal":
        return [("equal", position + shift, after[2], j2 + shift, after[4])]
    return [("equal", position + shift, position, j2 + shift, j2), after]


# ─────────────────────────────────────────────────────────────
#  조각 하나
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _ChunkOutcome:
    record: RetextChunk
    texts: tuple[tuple[int, str], ...]
    rejections: tuple[RetextRejection, ...]
    unplaced: tuple[RetextUnplaced, ...]
    letters: dict[int, int]  # seg_id → 그 세그먼트 몫의 gpt 글자 수


@dataclass(frozen=True, slots=True)
class _Slot:
    """세그먼트 하나에 대응된 gpt 몫. 칸막이의 한 칸이다."""

    segment: Segment
    bounds: tuple[int, int]  # whisper letters 안의 범위
    cut: tuple[int, int]  # gpt raw 안의 범위(어절 경계로 스냅됨)


def _plan_chunk(
    chunk: ChunkText, span: Span, members: Sequence[Segment], held: set[int], cfg: Config
) -> _ChunkOutcome:
    gpt = _fold(chunk.text)
    bounds = _whisper_bounds(members)
    whisper = "".join(_fold(segment.text).letters for segment in members)
    edges = frozenset(edge for bound in bounds for edge in bound)
    alignment = _align(whisper, gpt.letters, edges)
    slots = [
        _slot(segment, bound, alignment, gpt)
        for segment, bound in zip(members, bounds, strict=True)
    ]
    trusted = not members or alignment.similarity >= cfg.retext_chunk_similarity_floor
    texts: list[tuple[int, str]] = []
    rejections: list[RetextRejection] = []
    for slot in slots:
        if slot.segment.id in held:
            continue
        verdict = (
            _judge(slot, alignment, gpt, cfg) if trusted else _rejected(slot, REASON_CHUNK, gpt)
        )
        if isinstance(verdict, RetextRejection):
            rejections.append(verdict)
        else:
            texts.append((slot.segment.id, verdict))
    record = RetextChunk(
        chunk.index, span, alignment.similarity, trusted, len(texts), len(rejections)
    )
    unplaced = _unplaced(chunk.index, members, bounds, alignment, gpt) if trusted else ()
    letters = {slot.segment.id: _letters_in(slot, gpt) for slot in slots}
    return _ChunkOutcome(record, tuple(texts), tuple(rejections), tuple(unplaced), letters)


def _whisper_bounds(members: Sequence[Segment]) -> list[tuple[int, int]]:
    bounds: list[tuple[int, int]] = []
    cursor = 0
    for segment in members:
        size = len(_fold(segment.text).letters)
        bounds.append((cursor, cursor + size))
        cursor += size
    return bounds


def _slot(segment: Segment, bound: tuple[int, int], alignment: _Alignment, gpt: _Folded) -> _Slot:
    start = gpt.snap(gpt.raw_at(alignment.right[bound[0]]))
    end = gpt.snap(gpt.raw_at(alignment.left[bound[1]]))
    return _Slot(segment=segment, bounds=bound, cut=(start, max(start, end)))


def _letters_in(slot: _Slot, gpt: _Folded) -> int:
    return gpt.letters_before(slot.cut[1]) - gpt.letters_before(slot.cut[0])


# ─────────────────────────────────────────────────────────────
#  세그먼트 게이트
# ─────────────────────────────────────────────────────────────


def _judge(slot: _Slot, alignment: _Alignment, gpt: _Folded, cfg: Config) -> str | RetextRejection:
    """셋 다 통과해야 교체한다: 길이 비 상한, 길이 비 하한, 같은 자리 일치율."""
    whisper_size = slot.bounds[1] - slot.bounds[0]
    gpt_size = _letters_in(slot, gpt)
    candidate = gpt.raw[slot.cut[0] : slot.cut[1]].strip()
    if not whisper_size:
        return _rejected(slot, REASON_NO_LETTERS, gpt)
    if not gpt_size or not candidate:
        return _rejected(slot, REASON_EMPTY, gpt)
    ratio = gpt_size / whisper_size
    match = _matched(slot, alignment, gpt) / whisper_size
    reason = None
    if abs(gpt_size - whisper_size) > cfg.retext_length_slack_chars:
        if ratio > cfg.retext_length_ratio_max:
            reason = REASON_TOO_LONG
        elif ratio < cfg.retext_length_ratio_min:
            reason = REASON_TOO_SHORT
    if reason is None and match < cfg.retext_segment_match_floor:
        reason = REASON_MATCH
    if reason is not None:
        return _rejected(slot, reason, gpt, match=match, ratio=ratio)
    return candidate


def _matched(slot: _Slot, alignment: _Alignment, gpt: _Folded) -> int:
    """이 세그먼트의 whisper 글자 중, **이 세그먼트 몫의** gpt 글자와 equal로 짝지어진 수."""
    low, high = slot.bounds
    gpt_low = gpt.letters_before(slot.cut[0])
    gpt_high = gpt.letters_before(slot.cut[1])
    total = 0
    for i1, i2, j1 in alignment.equal:
        start = max(low, i1, gpt_low - j1 + i1)
        end = min(high, i2, gpt_high - j1 + i1)
        total += max(0, end - start)
    return total


def _rejected(
    slot: _Slot, reason: str, gpt: _Folded, *, match: float = 0.0, ratio: float = 0.0
) -> RetextRejection:
    return RetextRejection(
        seg_id=slot.segment.id,
        reason=reason,
        match=match,
        length_ratio=ratio,
        candidate=gpt.raw[slot.cut[0] : slot.cut[1]].strip(),
    )


# ─────────────────────────────────────────────────────────────
#  세그먼트 사이에 낀 글자 — 출력하지 않고 기록만
# ─────────────────────────────────────────────────────────────


def _unplaced(
    chunk_index: int,
    members: Sequence[Segment],
    bounds: Sequence[tuple[int, int]],
    alignment: _Alignment,
    gpt: _Folded,
) -> list[RetextUnplaced]:
    """세그먼트 경계마다 끼어든 gpt 글자. 경계 **안쪽**의 삽입은 그 세그먼트 몫이라 여기 없다."""
    edges = sorted({0, len(alignment.left) - 1, *(edge for bound in bounds for edge in bound)})
    found: list[RetextUnplaced] = []
    for edge in edges:
        start = gpt.snap(gpt.raw_at(alignment.left[edge]))
        end = gpt.snap(gpt.raw_at(alignment.right[edge]))
        chars = gpt.letters_before(end) - gpt.letters_before(start)
        if chars <= 0:
            continue
        before, after = _neighbours(members, bounds, edge)
        found.append(RetextUnplaced(chunk_index, gpt.raw[start:end].strip(), chars, before, after))
    return found


def _neighbours(
    members: Sequence[Segment], bounds: Sequence[tuple[int, int]], edge: int
) -> tuple[int | None, int | None]:
    ahead = [index for index, bound in enumerate(bounds) if bound[1] <= edge]
    last = ahead[-1] if ahead else -1
    following = [index for index, bound in enumerate(bounds) if index > last and bound[0] >= edge]
    before = members[last].id if ahead else None
    after = members[following[0]].id if following else None
    return before, after
