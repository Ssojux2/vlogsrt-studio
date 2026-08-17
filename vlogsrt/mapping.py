"""경계 매핑 — whisper 골격의 절단점을 gpt 정본 좌표로 옮긴다.

좌표계가 넷이다. 혼동하면 원인 불명의 오류가 된다.

    원본 W ──normalize──> 정규화 W ──SequenceMatcher──> 정규화 A ──imap_a──> 원본 A

이 모듈의 어떤 함수도 큐 시각을 만들거나 바꾸지 않는다(불변 규칙 3).
텍스트 인덱스만 다룬다.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Sequence
from dataclasses import dataclass, replace
from difflib import SequenceMatcher

from . import log
from .config import Config
from .hangul import normalize, to_nfc
from .models import ORIGIN_ORPHAN, VERDICT_OK, Cue, Segment, Word

Opcode = tuple[str, int, int, int, int]
Span = tuple[float, float]
SNAP_FAILED = "snap_failed"
TIME_ESTIMATED = "time_estimated"


@dataclass(frozen=True, slots=True)
class Alignment:
    opcodes: tuple[Opcode, ...]
    imap_w: tuple[int, ...]
    imap_a: tuple[int, ...]
    similarity: float


# ─────────────────────────────────────────────────────────────
#  W 구성
# ─────────────────────────────────────────────────────────────


def build_w(
    segments: Sequence[Segment], include_all: bool = False
) -> tuple[str, list[int], list[int]]:
    """골격 텍스트를 이어 붙이고 세그먼트 경계를 절단점으로 기록한다.

    `cuts[i]`는 i번째 세그먼트가 끝나는 W 좌표다. `owner[c]`는 W의 c번째 글자를
    소유한 세그먼트 id다. 판정이 `ok`가 아닌 세그먼트는 기본적으로 빠진다.
    """
    parts: list[str] = []
    cuts: list[int] = []
    owner: list[int] = []
    length = 0
    included = [s for s in segments if include_all or s.verdict == VERDICT_OK]
    for position, segment in enumerate(included):
        text = to_nfc(segment.text.strip())
        chunk = text if position == len(included) - 1 else f"{text} "
        parts.append(chunk)
        owner.extend([segment.id] * len(chunk))
        length += len(chunk)
        cuts.append(length)
    return "".join(parts), cuts, owner


# ─────────────────────────────────────────────────────────────
#  정렬
# ─────────────────────────────────────────────────────────────


def align(w: str, a: str) -> list[Opcode]:
    """자모 정규화 좌표에서의 opcode. `autojunk=False`가 핵심이다(불변 규칙 4).

    기본값이면 `ㅇ`, `ㅏ` 같은 고빈도 자모가 잡음으로 버려져 정렬이 무너진다.
    """
    norm_w, _ = normalize(w)
    norm_a, _ = normalize(a)
    matcher = SequenceMatcher(None, norm_w, norm_a, autojunk=False)
    return list(matcher.get_opcodes())


def build_alignment(w: str, a: str) -> Alignment:
    norm_w, imap_w = normalize(w)
    norm_a, imap_a = normalize(a)
    matcher = SequenceMatcher(None, norm_w, norm_a, autojunk=False)
    return Alignment(
        opcodes=tuple(matcher.get_opcodes()),
        imap_w=tuple(imap_w),
        imap_a=tuple(imap_a),
        similarity=matcher.ratio(),
    )


def similarity(left: str, right: str) -> float:
    """자모 단위 유사도. 한 글자 차이가 한 자모 차이로 줄어 감점이 작아진다."""
    norm_left, _ = normalize(left)
    norm_right, _ = normalize(right)
    if not norm_left and not norm_right:
        return 1.0
    if not norm_left or not norm_right:
        return 0.0
    return SequenceMatcher(None, norm_left, norm_right, autojunk=False).ratio()


# ─────────────────────────────────────────────────────────────
#  절단점 사상
# ─────────────────────────────────────────────────────────────


def map_position(
    pos_w: int,
    opcodes: Sequence[Opcode],
    imap_w: Sequence[int],
    imap_a: Sequence[int],
) -> int:
    """원본 W 좌표를 원본 A 좌표로 옮긴다. 시간 값은 들어오지도 나가지도 않는다."""
    if not imap_a:
        return 0
    nw = bisect_left(list(imap_w), pos_w)
    if nw <= 0:
        return 0
    if nw >= len(imap_w):
        return imap_a[-1] + 1
    return _to_origin(_map_normalized(nw, opcodes), imap_a)


def _map_normalized(nw: int, opcodes: Sequence[Opcode]) -> int:
    for tag, i1, i2, j1, j2 in opcodes:
        if not (i1 <= nw < i2 or (i1 == i2 == nw)):
            continue
        if tag == "equal":
            return j1 + (nw - i1)
        if tag in ("insert", "delete"):
            return j1
        span_w = i2 - i1
        if span_w == 0:  # replace인데 W쪽이 비어 있을 수는 없지만, 나눗셈은 막는다
            return j1
        ratio = (nw - i1) / span_w
        return j1 + int(round(ratio * (j2 - j1)))
    return opcodes[-1][4] if opcodes else 0


def _to_origin(na: int, imap_a: Sequence[int]) -> int:
    if na <= 0:
        return 0
    if na >= len(imap_a):
        return imap_a[-1] + 1
    return imap_a[na]


def enforce_monotonic(cuts: Sequence[int], length: int) -> list[int]:
    """`replace` 블록의 비율 배분에서 드물게 절단점이 역전된다. 음수 길이 슬라이스를 막는다."""
    result: list[int] = []
    previous = 0
    for cut in cuts:
        value = min(max(cut, previous), length)
        if value != cut:
            log.warn(f"절단점 역전을 바로잡았다: {cut} → {value}. 잦으면 사상 로직에 버그다.")
        result.append(value)
        previous = value
    return result


def map_cuts(cuts_w: Sequence[int], alignment: Alignment, text_a: str) -> list[int]:
    mapped = [
        map_position(cut, alignment.opcodes, alignment.imap_w, alignment.imap_a) for cut in cuts_w
    ]
    monotonic = enforce_monotonic(mapped, len(text_a))
    if monotonic:
        monotonic[-1] = len(text_a)  # 마지막 조각은 남은 글자를 전부 가져간다
    return monotonic


# ─────────────────────────────────────────────────────────────
#  어절 스냅
# ─────────────────────────────────────────────────────────────


def _is_boundary(text: str, index: int) -> bool:
    if index <= 0 or index >= len(text):
        return True
    return text[index - 1].isspace() or text[index].isspace()


def _nearest_left(text: str, cut: int) -> int | None:
    for index in range(cut, -1, -1):
        if _is_boundary(text, index):
            return index
    return None


def _nearest_right(text: str, cut: int) -> int | None:
    for index in range(cut, len(text) + 1):
        if _is_boundary(text, index):
            return index
    return None


def snap_to_word_boundary(
    cut_a: int,
    text_a: str,
    words: Sequence[Word],
    boundary_time: float,
    cfg: Config,
) -> tuple[int, list[str]]:
    """어절 중간에 떨어진 절단점을 가장 가까운 공백으로 옮긴다.

    `cfg`는 문서상의 시그니처에 없지만 `snap_max_distance`가 필요하다 —
    임계값을 코드에 박지 않기 위해서다(불변 규칙 6).
    """
    cut = min(max(cut_a, 0), len(text_a))
    if _is_boundary(text_a, cut):
        return cut, []

    left = _nearest_left(text_a, cut)
    right = _nearest_right(text_a, cut)
    left_distance = cut - left if left is not None else None
    right_distance = right - cut if right is not None else None
    limit = cfg.snap_max_distance

    if not words:
        # --fast. 한국어는 어절 끝에 조사가 붙어 앞 세그먼트에 속하는 일이 많다.
        if left is not None and left_distance is not None and left_distance <= limit:
            return left, []
        if right is not None and right_distance is not None and right_distance <= limit:
            return right, []
        return cut, [SNAP_FAILED]

    left_ok = left is not None and left_distance is not None and left_distance <= limit
    right_ok = right is not None and right_distance is not None and right_distance <= limit
    if not left_ok and not right_ok:
        return cut, [SNAP_FAILED]
    if left_ok and not right_ok:
        assert left is not None
        return left, []
    if right_ok and not left_ok:
        assert right is not None
        return right, []

    assert left is not None and right is not None
    assert left_distance is not None and right_distance is not None
    if left_distance < right_distance:
        return left, []
    if right_distance < left_distance:
        return right, []
    return (right if _word_belongs_left(words, boundary_time) else left), []


def _word_belongs_left(words: Sequence[Word], boundary_time: float) -> bool:
    """경계에 걸친 어절이 앞 세그먼트 소유인지 본다. 절반 넘게 앞이면 앞 소유다."""
    for word in words:
        if word.start <= boundary_time <= word.end:
            midpoint = (word.start + word.end) / 2
            return midpoint <= boundary_time
    return True


# ─────────────────────────────────────────────────────────────
#  큐에 정본 텍스트 얹기
# ─────────────────────────────────────────────────────────────


def spine_to_cues(spine: Sequence[Segment]) -> list[Cue]:
    """골격 하나에 큐 하나. 시각은 여기서 결정되고 이후 매핑이 건드리지 않는다."""
    return [
        Cue(
            start=segment.start,
            end=segment.end,
            lines=(segment.text.strip(),),
            seg_ids=(segment.id,),
            whisper_text=segment.text.strip(),
            index=position + 1,
        )
        for position, segment in enumerate(s for s in spine if s.verdict == VERDICT_OK)
    ]


def apply_canonical(
    cues: Sequence[Cue],
    spine: Sequence[Segment],
    text_a: str,
    words: Sequence[Word],
    cfg: Config,
) -> tuple[list[Cue], list[int], float]:
    """큐의 텍스트만 정본으로 갈아 끼운다. start/end는 손대지 않는다."""
    canonical = to_nfc(text_a)
    w_text, cuts_w, _owner = build_w(spine)
    alignment = build_alignment(w_text, canonical)
    cuts_a = map_cuts(cuts_w, alignment, canonical)

    snapped: list[int] = []
    flags_per_cut: list[list[str]] = []
    for position, cut in enumerate(cuts_a):
        if position == len(cuts_a) - 1:
            snapped.append(len(canonical))
            flags_per_cut.append([])
            continue
        boundary_time = cues[position].end if position < len(cues) else 0.0
        moved, flags = snap_to_word_boundary(cut, canonical, words, boundary_time, cfg)
        snapped.append(moved)
        flags_per_cut.append(flags)
    snapped = enforce_monotonic(snapped, len(canonical))
    if snapped:
        snapped[-1] = len(canonical)

    updated: list[Cue] = []
    start = 0
    for position, cue in enumerate(cues):
        end = snapped[position] if position < len(snapped) else len(canonical)
        text = canonical[start:end].strip()
        new_cue = replace(cue, lines=(text,))
        for flag in flags_per_cut[position] if position < len(flags_per_cut) else []:
            new_cue = new_cue.with_flag(flag)
        updated.append(new_cue)
        start = end
    return updated, snapped, alignment.similarity


# ─────────────────────────────────────────────────────────────
#  고아 텍스트
# ─────────────────────────────────────────────────────────────


def rescue_orphans(
    cuts_a: Sequence[int],
    spine: Sequence[Segment],
    cues: Sequence[Cue],
    silences: Sequence[Span],
    text_a: str,
    cfg: Config,
) -> list[Cue]:
    """환청 필터로 빠진 구간의 정본 텍스트를 되살린다.

    whisper가 헛소리를 했다는 게 그 시간에 발화가 없었다는 뜻은 아니다.
    텍스트를 버리지 않는 것이 원칙이라, 시각을 추정해서라도 큐로 남긴다.
    """
    excluded = [segment for segment in spine if segment.verdict != VERDICT_OK]
    if not excluded:
        return list(cues)

    canonical = to_nfc(text_a)
    full_w, full_cuts, _ = build_w(spine, include_all=True)
    alignment = build_alignment(full_w, canonical)
    full_a = map_cuts(full_cuts, alignment, canonical)

    result = list(cues)
    for position, segment in enumerate(spine):
        if segment.verdict == VERDICT_OK:
            continue
        span_start = full_a[position - 1] if position > 0 else 0
        text = canonical[span_start : full_a[position]].strip()
        if len(text) < cfg.orphan_min_chars:
            continue
        result = _drop_duplicate(result, text, cuts_a)
        result.append(_orphan_cue(segment, text, silences, cfg))
    return sorted(result, key=lambda cue: (cue.start, cue.end))


def _drop_duplicate(cues: list[Cue], orphan_text: str, cuts_a: Sequence[int]) -> list[Cue]:
    """같은 텍스트가 이웃 큐에 흡수되어 있으면 거기서 뺀다. 두 번 나오면 안 된다."""
    del cuts_a  # ok 전용 분할은 참고만 한다. 실제 판단은 텍스트 일치로 한다.
    trimmed: list[Cue] = []
    removed = False
    for cue in cues:
        if not removed and orphan_text and orphan_text in cue.text:
            leftover = cue.text.replace(orphan_text, "", 1).strip()
            removed = True
            if not leftover:
                log.debug(f"고아 분리 후 빈 큐가 되어 제거: {cue.start:.2f}초")
                continue
            trimmed.append(replace(cue, lines=(leftover,)))
            continue
        trimmed.append(cue)
    return trimmed


def _orphan_cue(segment: Segment, text: str, silences: Sequence[Span], cfg: Config) -> Cue:
    start, end = _trim_to_speech(segment.start, segment.end, silences, cfg)
    return Cue(
        start=start,
        end=end,
        lines=(text,),
        seg_ids=(segment.id,),
        origin=ORIGIN_ORPHAN,
        conf=cfg.orphan_conf_max,
        flags=(TIME_ESTIMATED,),
        whisper_text="",
    )


def _trim_to_speech(
    start: float, end: float, silences: Sequence[Span], cfg: Config
) -> tuple[float, float]:
    """앞뒤로 붙은 무음을 걷어낸다. 최소 길이는 지킨다."""
    trimmed_start, trimmed_end = start, end
    for silence_start, silence_end in silences:
        if silence_start <= trimmed_start < silence_end:
            trimmed_start = min(silence_end, trimmed_end)
        if silence_start < trimmed_end <= silence_end:
            trimmed_end = max(silence_start, trimmed_start)
    if trimmed_end - trimmed_start < cfg.min_duration:
        return start, max(end, start + cfg.min_duration)
    return trimmed_start, trimmed_end


# ─────────────────────────────────────────────────────────────
#  신뢰도
# ─────────────────────────────────────────────────────────────

_VERDICT_PENALTY = {
    "low_logprob": "low_logprob_penalty",
    "compression_fail": "compression_penalty",
}


def cue_confidence(cue: Cue, segment: Segment | None, cfg: Config) -> float:
    """whisper와 gpt가 얼마나 같은 말을 했는지.

    한계: 두 모델이 **똑같이 틀리면** 유사도가 높아 놓친다. 이건 구조적 한계이고
    `keywords` 사전이 유일한 방어선이다.
    """
    if cue.end <= cue.start or not cue.text.strip():
        return 0.0
    if cue.origin == ORIGIN_ORPHAN:
        return min(cfg.orphan_conf_max, cue.conf)

    score = similarity(cue.whisper_text, cue.text) if cue.whisper_text else cfg.orphan_conf_max
    if segment is not None:
        penalty_field = _VERDICT_PENALTY.get(segment.verdict)
        if penalty_field:
            score *= float(getattr(cfg, penalty_field))
    if cue.origin in ("split", "merge"):
        score *= cfg.refine_penalty
    if SNAP_FAILED in cue.flags or "forced_break" in cue.flags:
        score *= cfg.refine_penalty
    return max(0.0, min(1.0, score))


def score_cues(cues: Sequence[Cue], spine: Sequence[Segment], cfg: Config) -> list[Cue]:
    by_id = {segment.id: segment for segment in spine}
    scored: list[Cue] = []
    for cue in cues:
        segment = by_id.get(cue.seg_ids[0]) if cue.seg_ids else None
        scored.append(replace(cue, conf=cue_confidence(cue, segment, cfg)))
    return scored
