"""초안 영어 자막(W-53, D105)의 저장·요청 본문·견적 — 서비스만 부른다.

순수 계층(`translation`)이 지은 묶음으로 Gemini generateContent 본문을 짓고, 공식 단가로 상한을
재고, 원본마다 번역을 `analysis/translation/<열쇠>.json` 하나로 남긴다 — 열쇠가 곧 이름이라 그
열쇠의 문서가 있으면 지금 자막의 번역이다(자막을 고치면 열쇠가 바뀌어 그 원본만 다시 옮긴다). 실패는
`<열쇠>.failed.json`에 까닭과 함께 남는다. 부르기(유료)는 서비스가 승인 뒤
`transcribe.request_gemini_generate`로만 한다(규칙 7). 쓰기·읽기·상한은 맥락 평가와 같은 함수다.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from vlogsrt.project.raw import translation
from vlogsrt.project.raw.config import TranslationSettings
from vlogsrt.project.raw.context_review_io import JSON_MIME_TYPE, load, write

TRANSLATION_DIRNAME = "translation"
RESULT_SCHEMA = "raw-translation-result/v1"
FAILURE_SCHEMA = "raw-translation-failure/v1"

__all__ = [
    "FAILURE_SCHEMA",
    "RESULT_SCHEMA",
    "failure_document",
    "failure_path",
    "load",
    "payload",
    "result_document",
    "result_path",
    "write",
]


def folder(workspace_root: Path, analysis_root: str) -> Path:
    return workspace_root / analysis_root / TRANSLATION_DIRNAME


def result_path(workspace_root: Path, analysis_root: str, key: str) -> Path:
    return folder(workspace_root, analysis_root) / f"{key}.json"


def failure_path(workspace_root: Path, analysis_root: str, key: str) -> Path:
    return folder(workspace_root, analysis_root) / f"{key}.failed.json"


def payload(
    batch: translation.TranslationBatch,
    glossary_terms: Sequence[str],
    settings: TranslationSettings,
) -> dict[str, object]:
    """generateContent 본문 — 안내문과 묶음 글, JSON schema 강제, 생각 수준은 번역 설정 값."""
    return {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": translation.INSTRUCTION},
                    {"text": translation.batch_text(batch, glossary_terms)},
                ],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": settings.max_output_tokens,
            "responseMimeType": JSON_MIME_TYPE,
            "responseJsonSchema": translation.RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingLevel": settings.thinking_level},
        },
    }


def result_document(
    key: str,
    asset_id: str,
    model: str,
    english: Mapping[str, str],
    usage: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """저장할 번역 — 줄 아이디 → 영어(나누지 않은 한 줄), 묶음마다 잰 사용량."""
    return {
        "schema": RESULT_SCHEMA,
        "key": key,
        "asset_id": asset_id,
        "model": model,
        "created_at": datetime.now(UTC).isoformat(),
        "english": dict(english),
        "usage": [dict(row) for row in usage],
    }


def failure_document(key: str, asset_id: str, model: str, reason: str) -> dict[str, Any]:
    return {
        "schema": FAILURE_SCHEMA,
        "key": key,
        "asset_id": asset_id,
        "model": model,
        "failed_at": datetime.now(UTC).isoformat(),
        "reason": reason,
    }
