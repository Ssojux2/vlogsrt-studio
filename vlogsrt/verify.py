"""완성된 자막을 두 번째 모델의 텍스트와 대조해 **의심 지점을 표시한다.**

이 모듈은 읽기 전용이다(불변 규칙 5). 큐를 만들지도 고치지도 지우지도 못하고,
`refine`·`output`을 import하지 않는다. SRT는 이 모듈이 돌기 전에 이미 저장돼 있다 —
검증을 켜든 끄든 실패하든 SRT 바이트는 같다.

세 가지를 본다:
- **큐 일치율(coverage)**: 큐 텍스트가 같은 조각의 검증 텍스트 안에서 매칭되는 비율.
  낮으면 whisper가 잘못 들었을 가능성 — 의심 큐.
- **조각 유사도(similarity)**: 구간 내 자막 전체 vs 검증 텍스트. 큐들이 멀쩡해도
  유사도가 낮으면 자막이 놓친 발화가 있다는 뜻이다. 조각 수준으로만 표시한다.
- **반복 환청(loops)**: 연속 세그먼트가 같은 말을 되풀이하는 구간. whisper가 긴
  조각에서 빠지는 병리다(D13: 38분 조각에서 539초, 11.7%).

**찾아도 빼지 않는다**(규칙 9). 자막에 쓰레기가 보이는 편이 공백보다 낫다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import Config
from .hangul import strip_for_compare, to_nfc
from .models import Cue, Segment, Span
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
class Loop:
    """같은 텍스트가 연속된 세그먼트에서 되풀이된 구간."""

    start: float
    end: float
    count: int
    text: str

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass(frozen=True, slots=True)
class Verification:
    chunks: tuple[ChunkCheck, ...]
    suspects: tuple[CueCheck, ...]
    loops: tuple[Loop, ...]
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
    if not ordered:
        return Verification(chunks=(), suspects=(), loops=(), cue_total=len(cues))

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
        loops=tuple(find_loops(spine, cfg)),
        cue_total=len(cues),
    )


def find_loops(spine: Sequence[Segment], cfg: Config) -> list[Loop]:
    """연속 세그먼트가 같은 말을 되풀이하는 구간.

    세그먼트 하나만 보면 정상이라 whisper의 환청 필터를 그대로 통과한다. 실측: 한
    문장이 103개 세그먼트에 걸쳐 570초 동안 반복됐다(D13).
    """
    loops: list[Loop] = []
    position = 0
    while position < len(spine):
        text = _key(spine[position])
        last = position
        while last + 1 < len(spine) and text and _key(spine[last + 1]) == text:
            last += 1
        run = spine[position : last + 1]
        if _qualifies(run, text, cfg):
            loops.append(Loop(start=run[0].start, end=run[-1].end, count=len(run), text=text))
        position = last + 1
    return loops


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


def _key(segment: Segment) -> str:
    """비교용 표준형. 공백과 유니코드 정규화 차이로 반복을 놓치지 않게 한다."""
    return " ".join(to_nfc(segment.text).split())


def _qualifies(run: Sequence[Segment], text: str, cfg: Config) -> bool:
    if not text or len(run) < cfg.loop_min_repeats or len(text) < cfg.loop_min_chars:
        return False
    return run[-1].end - run[0].start >= cfg.loop_min_seconds


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
