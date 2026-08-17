"""완성된 자막을 gpt-transcribe 텍스트와 대조해 의심 지점을 표시한다.

이 모듈은 읽기 전용이다(불변 규칙 4). 큐를 만들지도 고치지도 못하고,
`Verification` 플래그만 반환한다. SRT는 이 모듈이 돌기 전에 이미 저장돼 있다.

두 층위로 본다:
- 큐 일치율(coverage): 큐 텍스트가 같은 청크의 gpt 텍스트 안에서 매칭되는 비율.
  낮으면 whisper가 잘못 들었을 가능성 — 의심 큐.
- 청크 유사도(similarity): 구간 내 자막 전체 vs gpt 텍스트. 큐들이 멀쩡해도
  유사도가 낮으면 gpt 쪽에만 있는 발화가 있다는 뜻 — 자막이 놓친 구간.
  큐 잘못이 아니므로 청크 수준으로만 표시한다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher

from .config import Config
from .hangul import strip_for_compare
from .models import Cue
from .transcribe import ChunkResponse

Span = tuple[float, float]


@dataclass(frozen=True, slots=True)
class ChunkCheck:
    """청크 하나의 대조 결과. flagged면 자막이 발화를 놓쳤을 수 있는 구간이다."""

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
    cue_total: int


def chunk_spans(responses: Sequence[ChunkResponse], duration: float) -> list[Span]:
    """gpt 응답 오프셋으로 시간축을 빈틈없이 나눈다. 마지막 구간은 영상 끝까지."""
    offsets = sorted(response.offset for response in responses)
    ends = [*offsets[1:], max(duration, offsets[-1])]
    return list(zip(offsets, ends, strict=True))


def verify(
    cues: Sequence[Cue],
    responses: Sequence[ChunkResponse],
    duration: float,
    cfg: Config,
) -> Verification:
    """순수 함수. 입력을 바꾸지 않고 플래그만 만든다."""
    ordered = sorted(responses, key=lambda response: response.offset)
    spans = chunk_spans(ordered, duration)

    chunk_checks: list[ChunkCheck] = []
    suspects: list[CueCheck] = []
    for response, span in zip(ordered, spans, strict=True):
        members = [cue for cue in cues if span[0] <= cue.start < span[1]]
        gpt_norm = strip_for_compare(str(response.payload.get("text", "")))
        chunk_checks.append(_check_chunk(response.index, span, members, gpt_norm, cfg))
        suspects.extend(_check_cues(members, gpt_norm, cfg))

    return Verification(
        chunks=tuple(chunk_checks),
        suspects=tuple(suspects),
        cue_total=len(cues),
    )


def summarize(verification: Verification) -> str:
    flagged = sum(1 for chunk in verification.chunks if chunk.flagged)
    suspect_count = len(verification.suspects)
    percent = suspect_count / verification.cue_total * 100 if verification.cue_total else 0.0
    return (
        f"검증: 큐 {verification.cue_total}개 중 {suspect_count}개 의심 ({percent:.1f}%)"
        f" · 청크 {len(verification.chunks)}개 중 {flagged}개 저유사도"
    )


def _check_chunk(
    index: int, span: Span, members: Sequence[Cue], gpt_norm: str, cfg: Config
) -> ChunkCheck:
    subtitle_norm = strip_for_compare(" ".join(cue.text for cue in members))
    if subtitle_norm or gpt_norm:
        matcher = SequenceMatcher(None, subtitle_norm, gpt_norm, autojunk=False)
        similarity = matcher.ratio()
    else:
        similarity = 1.0
    return ChunkCheck(
        index=index,
        span=span,
        similarity=similarity,
        cue_count=len(members),
        flagged=similarity < cfg.verify_chunk_sim_floor,
    )


def _check_cues(members: Sequence[Cue], gpt_norm: str, cfg: Config) -> list[CueCheck]:
    # seq2(b2j 색인)는 청크당 한 번만 만든다. set_seq1은 그 색인을 재사용한다.
    matcher = SequenceMatcher(None, "", gpt_norm, autojunk=False)
    suspects: list[CueCheck] = []
    for cue in members:
        cue_norm = strip_for_compare(cue.text)
        if len(cue_norm) < cfg.verify_short_cue_chars:
            continue
        matcher.set_seq1(cue_norm)
        matched = sum(block.size for block in matcher.get_matching_blocks())
        coverage = matched / len(cue_norm)
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
