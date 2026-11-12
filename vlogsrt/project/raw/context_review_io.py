"""라이브러리 맥락 평가(W-39b, D99)의 저장·요청 본문 — 서비스만 부른다.

순수 계층(`context_review`)이 짓고 해석한 값을 파일로 남기고, Gemini generateContent 본문을
짓는다(견적은 없다, D110). 부르기(유료)는 하지 않는다 — 서비스가 승인 뒤
`transcribe.request_gemini_generate`로만 한다(규칙 7).

평가 결과는 `analysis/context_review/<입력 열쇠>.json` 하나다 — 열쇠가 곧 이름이라 지금 열쇠의
결과가 있으면 지금 평가이고, 없으면(입력이 바뀌었다) 등급에 쓸 평가가 없다(낡음). 실패는
`<열쇠>.failed.json`에 까닭과 함께 남고 등급은 그대로다.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import oscompat
from vlogsrt.project.raw import context_review, tiering
from vlogsrt.project.raw.analysis_config import ContextReviewSettings
from vlogsrt.project.raw.config import RawGeminiAnalysisProfile

CONTEXT_DIRNAME = "context_review"
RESULT_SCHEMA = "context-review-result/v1"
FAILURE_SCHEMA = "context-review-failure/v1"
JSON_MIME_TYPE = "application/json"


def folder(workspace_root: Path, analysis_root: str) -> Path:
    return workspace_root / analysis_root / CONTEXT_DIRNAME


def result_path(workspace_root: Path, analysis_root: str, key: str) -> Path:
    return folder(workspace_root, analysis_root) / f"{key}.json"


def failure_path(workspace_root: Path, analysis_root: str, key: str) -> Path:
    return folder(workspace_root, analysis_root) / f"{key}.failed.json"


def load(path: Path, schema: str, key: str) -> dict[str, Any] | None:
    """그 열쇠의 문서 — 없거나, 모양이 다르거나, 다른 열쇠의 것이면 None(쓰지 않는다)."""
    if not path.is_file():
        return None
    body = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(body, dict) or body.get("schema") != schema or body.get("key") != key:
        return None
    return body


def write(path: Path, body: Mapping[str, Any]) -> None:
    """임시 파일에 쓰고 바꿔 단다 — 반쯤 쓴 평가가 지금 평가로 읽히지 않게."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(body, ensure_ascii=False, indent=2, sort_keys=True)
    handle, temp = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".partial")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
    oscompat.replace(temp, path)


def payload(
    given: context_review.ContextInput,
    profile: RawGeminiAnalysisProfile,
    settings: ContextReviewSettings,
) -> dict[str, object]:
    """generateContent 본문 — 안내문과 입력 글, JSON schema 강제, 생각 수준은 프로파일 값."""
    return {
        "contents": [
            {
                "role": "user",
                "parts": [{"text": context_review.INSTRUCTION}, {"text": given.text}],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": settings.max_output_tokens,
            "responseMimeType": JSON_MIME_TYPE,
            "responseJsonSchema": context_review.RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingLevel": profile.thinking_level},
        },
    }


def result_document(
    key: str,
    model: str,
    review: context_review.ContextReview,
    given: context_review.ContextInput,
    usage: Mapping[str, Any],
) -> dict[str, Any]:
    """저장할 평가 — 판정은 원본 · 구간 (시작, 끝)으로 펴서 싣는다(입력 없이 등급 값을 만든다)."""
    rows = []
    for asset_id in dict.fromkeys(ref.asset_id for ref in given.spans.values()):
        signals = context_review.signals_for(review, given, asset_id)
        for (start, end), verdict in signals.verdicts.items():
            rows.append(
                {
                    "asset_id": asset_id,
                    "start": _rational(start),
                    "end": _rational(end),
                    "verdict": verdict.verdict,
                    "confidence": verdict.confidence,
                    "reason": verdict.reason,
                    "duplicate_of": verdict.duplicate_of,
                }
            )
    return {
        "schema": RESULT_SCHEMA,
        "key": key,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "storyline": review.storyline,
        "verdicts": rows,
        "spans": len(given.spans),
        "unjudged": review.unjudged,
        "input": {
            "dropped_lines": given.dropped_lines,
            "dropped_windows": given.dropped_windows,
            "clipped_texts": given.clipped_texts,
        },
        "usage": dict(usage),
    }


def failure_document(key: str, model: str, reason: str) -> dict[str, Any]:
    return {
        "schema": FAILURE_SCHEMA,
        "key": key,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "reason": reason,
    }


def signals_from(document: Mapping[str, Any], asset_id: str) -> tiering.ContextSignals:
    """저장된 평가 → 한 원본 몫의 등급 값. 다른 원본의 줄은 싣지 않는다."""
    verdicts: dict[tuple[Fraction, Fraction], tiering.ContextVerdict] = {}
    for row in _rows(document):
        if row.get("asset_id") != asset_id:
            continue
        start, end = _fraction(row["start"]), _fraction(row["end"])
        verdicts[(start, end)] = tiering.ContextVerdict(
            str(row["verdict"]),
            str(row["confidence"]),
            str(row.get("reason") or ""),
            row.get("duplicate_of"),
        )
    return tiering.ContextSignals(verdicts)


def _rows(document: Mapping[str, Any]) -> Sequence[Mapping[str, Any]]:
    rows = document.get("verdicts")
    return [row for row in rows if isinstance(row, Mapping)] if isinstance(rows, list) else []


def _rational(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _fraction(value: Mapping[str, Any]) -> Fraction:
    return Fraction(int(value["num"]), int(value["den"]))


__all__ = [
    "CONTEXT_DIRNAME",
    "FAILURE_SCHEMA",
    "RESULT_SCHEMA",
    "failure_document",
    "failure_path",
    "folder",
    "load",
    "payload",
    "result_document",
    "result_path",
    "signals_from",
    "write",
]
