"""라이브러리 전체 맥락 평가(W-39, D99) — 입력 짓기 · 응답 해석 · 등급 값 · 입력 열쇠(순수).

클립(창) 하나씩 보는 분석(D51·D85)과 달리, 라이브러리의 모든 원본을 촬영 순서대로 한 번에 보여 주고
등급 구간마다 「이 브이로그 이야기에서 어떤 자리인가」를 묻는다 — 핵심 · 보조 · 중복(어느 구간과) ·
맥락 밖. 판정은 등급의 마지막 단계(`tiering._contextualized`)에서 녹색을 내리거나 좁은 칸의 노랑을
올리는 **값**으로만 쓰인다(경계·빨강·보류·사람 결정은 그대로).

- 입력: 원본마다 등급 구간(짧은 아이디 `S001`…) · 창 요약(말·화면·태그) · 자막 줄. 예산
  (`ContextReviewSettings`)을 넘는 줄·창은 싣지 않고, 긴 글자는 자른다 — 그 수를 센다.
- 응답: `responseJsonSchema`로 묶은 JSON. 모르는 구간 아이디 · 두 번 나온 아이디 · 목록 밖
  판정·확신은 결함으로 거부한다(조용히 고치지 않는다). 판정이 없는 구간은 모름이다.
- 열쇠: 촬영 순서의 (원본 · 분석 리비전 · 증거 문서 sha · 원본 자막 리비전) + 등급판 + 모델 +
  안내문 판 + 예산. 지금 열쇠와 다른 평가는 낡았다 — 등급에 쓰지 않는다(서비스가 본다).

이 모듈은 전송·전사·서비스·DB·파일을 모른다(`check_context_review_stays_pure`). 부르는 일(유료)은
서비스가 승인 뒤 `transcribe.request_gemini_generate`로만 한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any

from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.analysis_config import (
    CONTEXT_CONFIDENCES,
    CONTEXT_REVIEW,
    CONTEXT_VERDICTS,
    ContextReviewSettings,
)

SPAN_ID_WIDTH = 3
"""구간 아이디 `S001`의 숫자 자리 — 모양일 뿐 판정이 아니다(더 많으면 자리가 늘어난다)."""


class ContextReviewError(ValueError):
    """맥락 평가 응답이 계약을 어긴다 — 그 평가는 쓰지 않는다."""


@dataclass(frozen=True, slots=True)
class ContextSpan:
    start: Fraction
    end: Fraction
    tier: str
    rule: str
    audio: str
    video: str


@dataclass(frozen=True, slots=True)
class ContextWindow:
    start: Fraction
    end: Fraction
    speech: str
    visual: str
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ContextLine:
    start: Fraction
    end: Fraction
    text: str


@dataclass(frozen=True, slots=True)
class ContextSource:
    """원본 하나의 재료 — 서비스가 등급 보고서·분석 문서·원본 자막에서 값으로 만든다."""

    asset_id: str
    name: str
    spans: tuple[ContextSpan, ...]
    windows: tuple[ContextWindow, ...]
    lines: tuple[ContextLine, ...]


@dataclass(frozen=True, slots=True)
class SpanRef:
    asset_id: str
    name: str
    start: Fraction
    end: Fraction


@dataclass(frozen=True, slots=True)
class ContextInput:
    """보낼 글(JSON)과 구간 아이디 표 · 줄인 수."""

    text: str
    spans: Mapping[str, SpanRef]
    dropped_lines: int
    dropped_windows: int
    clipped_texts: int


@dataclass(frozen=True, slots=True)
class ContextReview:
    storyline: str
    verdicts: Mapping[str, tiering.ContextVerdict]
    unjudged: int


INSTRUCTION = (
    "너는 한국어 여행 브이로그 편집자다. 아래는 한 라이브러리의 모든 원본 영상을 촬영 순서대로"
    " 정리한 것이다 — 원본마다 등급 구간(span_id · 시각 · 지금 등급), 분석 창 요약(말·화면·태그),"
    " 자막 줄이 있다. 전체 이야기(하루의 흐름)를 먼저 파악한 뒤, 구간마다 그 이야기에서의 자리를"
    " 판정해라:\n"
    "- core: 이야기에 꼭 필요한 장면(도착·핵심 경험·감정이 드러나는 말).\n"
    "- supporting: 있으면 좋은 보조 장면.\n"
    "- redundant: 다른 구간과 같은 장면·같은 말의 되풀이 — duplicate_of에 더 나은 쪽 span_id를"
    " 적는다.\n"
    "- off_topic: 이야기와 무관하거나 실수로 찍힌 장면.\n"
    "confidence는 low · medium · high. reason은 한국어 한 문장(60자 안). 모든 span_id를 판정하고,"
    " 없는 span_id를 지어내지 마라. storyline_summary에는 전체 줄거리를 한국어 세 문장 안으로"
    " 적어라."
)

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "storyline_summary": {"type": "string"},
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "span_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": list(CONTEXT_VERDICTS)},
                    "confidence": {"type": "string", "enum": list(CONTEXT_CONFIDENCES)},
                    "reason": {"type": "string"},
                    "duplicate_of": {"type": ["string", "null"]},
                },
                "required": ["span_id", "verdict", "confidence", "reason", "duplicate_of"],
            },
        },
    },
    "required": ["storyline_summary", "segments"],
}


def build_input(
    sources: Sequence[ContextSource], settings: ContextReviewSettings = CONTEXT_REVIEW
) -> ContextInput:
    """원본(촬영 순서 그대로) → 보낼 글. 빨강 구간은 싣지 않는다(맥락이 바꿀 수 없다)."""
    spans: dict[str, SpanRef] = {}
    counts = {"lines": 0, "windows": 0, "clipped": 0}
    rows = [_source_row(source, spans, counts, settings) for source in sources]
    text = json.dumps({"sources": rows}, ensure_ascii=False, separators=(",", ":"))
    return ContextInput(text, spans, counts["lines"], counts["windows"], counts["clipped"])


def _source_row(
    source: ContextSource,
    spans: dict[str, SpanRef],
    counts: dict[str, int],
    settings: ContextReviewSettings,
) -> dict[str, Any]:
    shown_spans = []
    for span in source.spans:
        if span.tier == tiering.TIER_RED:
            continue
        span_id = f"S{len(spans) + 1:0{SPAN_ID_WIDTH}d}"
        spans[span_id] = SpanRef(source.asset_id, source.name, span.start, span.end)
        shown_spans.append(
            {
                "span_id": span_id,
                "at": _clock(span.start, span.end),
                "tier": tiering.TIER_LABELS[span.tier],
                "audio": span.audio,
                "video": span.video,
            }
        )
    windows = source.windows[: settings.max_windows_per_source]
    lines = source.lines[: settings.max_lines_per_source]
    counts["windows"] += len(source.windows) - len(windows)
    counts["lines"] += len(source.lines) - len(lines)
    return {
        "name": source.name,
        "spans": shown_spans,
        "windows": [_window_row(window, counts, settings) for window in windows],
        "subtitles": [
            {
                "at": _clock(line.start, line.end),
                "text": _clip(line.text, settings.max_line_chars, counts),
            }
            for line in lines
        ],
    }


def _window_row(
    window: ContextWindow, counts: dict[str, int], settings: ContextReviewSettings
) -> dict[str, Any]:
    limit = settings.max_summary_chars
    return {
        "at": _clock(window.start, window.end),
        "speech": _clip(window.speech, limit, counts),
        "visual": _clip(window.visual, limit, counts),
        "tags": list(window.tags),
    }


def _clip(text: str, limit: int, counts: dict[str, int]) -> str:
    if len(text) <= limit:
        return text
    counts["clipped"] += 1
    return text[:limit] + "…"


def _clock(start: Fraction, end: Fraction) -> str:
    return f"{_mmss(start)}–{_mmss(end)}"


def _mmss(value: Fraction) -> str:
    whole = int(value)
    return f"{whole // 60:02d}:{whole % 60:02d}.{int((value - whole) * 10)}"


def parse(body: Mapping[str, Any], given: ContextInput) -> ContextReview:
    """응답 JSON → 판정. 계약을 어기면(모르는 아이디·중복·목록 밖 값·모양) 거부한다."""
    storyline = body.get("storyline_summary")
    segments = body.get("segments")
    if not isinstance(storyline, str) or not isinstance(segments, list):
        raise ContextReviewError("응답에 storyline_summary(글)와 segments(목록)가 없다")
    verdicts: dict[str, tiering.ContextVerdict] = {}
    for item in segments:
        span_id, verdict = _verdict(item, given)
        if span_id in verdicts:
            raise ContextReviewError(f"같은 구간을 두 번 판정했다: {span_id}")
        verdicts[span_id] = verdict
    return ContextReview(storyline.strip(), verdicts, len(given.spans) - len(verdicts))


def _verdict(item: Any, given: ContextInput) -> tuple[str, tiering.ContextVerdict]:
    if not isinstance(item, Mapping):
        raise ContextReviewError(f"판정 하나가 객체가 아니다: {item!r}")
    span_id = item.get("span_id")
    if span_id not in given.spans:
        raise ContextReviewError(f"보내지 않은 구간 아이디다: {span_id!r}")
    verdict, confidence = item.get("verdict"), item.get("confidence")
    if verdict not in CONTEXT_VERDICTS or confidence not in CONTEXT_CONFIDENCES:
        raise ContextReviewError(f"{span_id}: 판정·확신이 목록 밖이다: {verdict!r}/{confidence!r}")
    reason, duplicate = item.get("reason"), item.get("duplicate_of")
    if not isinstance(reason, str):
        raise ContextReviewError(f"{span_id}: 까닭이 글이 아니다")
    if duplicate is not None and duplicate not in given.spans:
        raise ContextReviewError(f"{span_id}: 겹친다는 구간이 없다: {duplicate!r}")
    return str(span_id), tiering.ContextVerdict(verdict, confidence, reason.strip(), duplicate)


def signals_for(
    review: ContextReview, given: ContextInput, asset_id: str
) -> tiering.ContextSignals:
    """한 원본 몫의 등급 값 — 구간 (시작, 끝) → 판정. 겹친 구간은 까닭에 그 자리를 붙인다."""
    verdicts: dict[tuple[Fraction, Fraction], tiering.ContextVerdict] = {}
    for span_id, verdict in review.verdicts.items():
        ref = given.spans[span_id]
        if ref.asset_id != asset_id:
            continue
        shown = verdict
        if verdict.duplicate_of is not None:
            other = given.spans[verdict.duplicate_of]
            place = f"{other.name} {_mmss(other.start)[:5]}"
            reason = f"{verdict.reason} ({place}와 겹침)" if verdict.reason else f"{place}와 겹침"
            shown = tiering.ContextVerdict(
                verdict.verdict, verdict.confidence, reason, verdict.duplicate_of
            )
        verdicts[(ref.start, ref.end)] = shown
    return tiering.ContextSignals(verdicts)


def context_key(
    sources: Sequence[Sequence[str]],
    *,
    tier_version: str,
    model: str,
    settings: ContextReviewSettings = CONTEXT_REVIEW,
) -> str:
    """평가 입력 열쇠 — 원본마다 (아이디 · 분석 리비전 · 증거 sha · 자막 리비전), 촬영 순서대로."""
    body = {
        "sources": [list(row) for row in sources],
        "tier_version": tier_version,
        "model": model,
        "settings": asdict(settings),
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


__all__ = [
    "INSTRUCTION",
    "RESPONSE_SCHEMA",
    "ContextInput",
    "ContextLine",
    "ContextReview",
    "ContextReviewError",
    "ContextSource",
    "ContextSpan",
    "ContextWindow",
    "SpanRef",
    "build_input",
    "context_key",
    "parse",
    "signals_for",
]
