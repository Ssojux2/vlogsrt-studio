"""완성된 자막을 두 번째 모델의 텍스트와 대조해 **의심 지점을 표시한다.**

이 모듈은 읽기 전용이다(불변 규칙 5). 큐를 만들지도 고치지도 지우지도 못하고,
`refine`·`output`을 import하지 않는다. SRT는 이 모듈이 돌기 전에 이미 저장돼 있다 —
검증을 켜든 끄든 실패하든 SRT 바이트는 같다.

세 가지를 본다:
- **큐 일치율(coverage)**: 큐 텍스트가 같은 조각의 검증 텍스트 안에서 매칭되는 비율.
  낮으면 whisper가 잘못 들었을 가능성 — 의심 큐.
- **조각 유사도(similarity)**: 구간 내 자막 전체 vs 검증 텍스트. 큐들이 멀쩡해도
  유사도가 낮으면 자막이 놓친 발화가 있다는 뜻이다. 조각 수준으로만 표시한다.
- **되풀이(loops)**: 같은 말이 되풀이되는 구간. 검출은 `repetition.py`가 하고
  여기서는 **남은 것만 표시한다** — 재전사(`repair.py`)가 먼저 걷어내므로, 여기에
  뜨는 것은 다시 물어봐도 회복하지 못했거나 다시 물어보지 않은 구간이다(D27).

**여기서는 찾아도 빼지 않는다**(규칙 5). 자막을 바꾸는 것은 재전사뿐이고, 그것도
골격이 조립되기 전에 끝난다. 이 모듈이 도는 시점에 SRT는 이미 디스크에 있다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from . import repetition
from .config import Config
from .hangul import strip_for_compare
from .models import Cue, Repeat, Segment, Span
from .transcribe import ChunkResponse, parse_text


@dataclass(frozen=True, slots=True)
class ChunkCheck:
    """조각 하나의 대조 결과. flagged면 자막이 발화를 놓쳤을 수 있는 구간이다."""

    index: int
    span: Span
    similarity: float
    cue_count: int
    flagged: bool


@dataclass(frozen=True, slots=True)
class CueCheck:
    """의심 큐 하나. coverage가 낮아 사람 확인이 필요하다."""

    cue_index: int
    span: Span
    text: str
    coverage: float


@dataclass(frozen=True, slots=True)
class Verification:
    chunks: tuple[ChunkCheck, ...]
    suspects: tuple[CueCheck, ...]
    loops: tuple[Repeat, ...]
    cue_total: int


def chunk_spans(responses: Sequence[ChunkResponse], duration: float) -> list[Span]:
    """조각 오프셋으로 시간축을 빈틈없이 나눈다. 마지막 구간은 영상 끝까지."""
    offsets = sorted(response.offset for response in responses)
    ends = [*offsets[1:], max(duration, offsets[-1])]
    return list(zip(offsets, ends, strict=True))


def verify(
    cues: Sequence[Cue],
    spine: Sequence[Segment],
    responses: Sequence[ChunkResponse],
    duration: float,
    cfg: Config,
) -> Verification:
    """순수 함수. 입력을 바꾸지 않고 플래그만 만든다(규칙 5)."""
    ordered = sorted(responses, key=lambda response: response.offset)
    loops = tuple(repetition.find_repeats(spine, cfg))
    if not ordered:
        # 대조할 응답이 없어도 되풀이는 골격만 보면 알 수 있다. 그것까지 잃을 이유는 없다.
        return Verification(chunks=(), suspects=(), loops=loops, cue_total=len(cues))

    chunk_checks: list[ChunkCheck] = []
    suspects: list[CueCheck] = []
    for response, span in zip(ordered, chunk_spans(ordered, duration), strict=True):
        members = [cue for cue in cues if span[0] <= cue.start < span[1]]
        other = strip_for_compare(parse_text(response.payload))
        chunk_checks.append(_check_chunk(response.index, span, members, other, cfg))
        suspects.extend(_check_cues(members, other, cfg))

    return Verification(
        chunks=tuple(chunk_checks),
        suspects=tuple(suspects),
        loops=loops,
        cue_total=len(cues),
    )


def summarize(verification: Verification) -> str:
    flagged = sum(1 for chunk in verification.chunks if chunk.flagged)
    suspects = len(verification.suspects)
    total = verification.cue_total
    percent = suspects / total * 100 if total else 0.0
    looped = sum(loop.duration for loop in verification.loops)
    return (
        f"검증: 큐 {total}개 중 {suspects}개 의심 ({percent:.1f}%)"
        f" · 조각 {len(verification.chunks)}개 중 {flagged}개 저유사도"
        f" · 되풀이 {len(verification.loops)}구간 {looped:.0f}초"
    )


def details(verification: Verification, limit: int = 10) -> list[str]:
    """사람이 눈으로 확인할 지점. 자막을 고치지는 않는다."""
    lines = [
        f"  조각 {chunk.index} ({chunk.span[0]:.0f}~{chunk.span[1]:.0f}초) "
        f"유사도 {chunk.similarity:.2f} — 자막이 놓친 발화가 있을 수 있다"
        for chunk in verification.chunks
        if chunk.flagged
    ]
    lines += [
        f"  {loop.start:.0f}~{loop.end:.0f}초 되풀이 {loop.count}회: {loop.text[:30]!r}"
        for loop in verification.loops
    ]
    lines += [
        f"  큐 {suspect.cue_index} ({suspect.span[0]:.0f}초) "
        f"일치율 {suspect.coverage:.2f}: {suspect.text[:30]!r}"
        for suspect in verification.suspects[:limit]
    ]
    if len(verification.suspects) > limit:
        lines.append(f"  … 의심 큐 {len(verification.suspects) - limit}개 더")
    return lines


def _check_chunk(
    index: int, span: Span, members: Sequence[Cue], other: str, cfg: Config
) -> ChunkCheck:
    subtitle = strip_for_compare(" ".join(cue.text for cue in members))
    if subtitle or other:
        similarity = SequenceMatcher(None, subtitle, other, autojunk=False).ratio()
    else:
        similarity = 1.0
    return ChunkCheck(
        index=index,
        span=span,
        similarity=similarity,
        cue_count=len(members),
        flagged=similarity < cfg.verify_chunk_sim_floor,
    )


def _check_cues(members: Sequence[Cue], other: str, cfg: Config) -> list[CueCheck]:
    # seq2(b2j 색인)는 조각당 한 번만 만든다. set_seq1은 그 색인을 재사용한다.
    matcher = SequenceMatcher(None, "", other, autojunk=False)
    suspects: list[CueCheck] = []
    for cue in members:
        text = strip_for_compare(cue.text)
        if len(text) < cfg.verify_short_cue_chars:
            continue
        matcher.set_seq1(text)
        coverage = sum(block.size for block in matcher.get_matching_blocks()) / len(text)
        if coverage < cfg.verify_cue_match_floor:
            suspects.append(
                CueCheck(
                    cue_index=cue.index,
                    span=(cue.start, cue.end),
                    text=cue.text,
                    coverage=coverage,
                )
            )
    return suspects
