"""「다른 귀로 교차 확인」(D59 19d) — 작업공간 설정 하나와, 켜져 있을 때만 실리는 결속·견적 칸.

켜져 있으면(기본) 1단계가 whisper와 같은 청크 WAV를 두 번째 귀(gpt-transcribe)에 한 번 더
묻는다 — 견적에 그 분량·금액이 보이고 사람이 승인했을 때만(`AsrPhaseApproval.text_model`). 꺼져
있으면 결속·견적 digest·승인이 19d 이전 바이트 그대로다(글자 요청 0회, 캐시에 이미 있는 답만
19c처럼 얹힌다).

- **설정**은 작업공간 meta 한 칸이다 — `RawWorkflowConfig` 밖(19b 승인 범위와 같은 자리). 내부
  whisper 청크 열쇠가 그 설정을 통째로 해시하므로 설정에 넣으면 모든 전사가 낡는다(D47·D55의
  교훈). 고른 적이 없으면 켜짐이다(사용자 결정: GPT-Transcribe로 교차 검증한다).
- **결속 꼬리표** `cross-check/<신원>` — 신원은 글자 요청을 정하는 값(`source_text.text_identity`:
  schema·모델·언어·문맥·용어집 sha). 켜져 있을 때만 리비전 결속·견적 digest·원본 입력 지문 끝에
  실린다. 용어집이나 문맥을 바꾸면 꼬리표가 바뀌어 그 글자로 다시 듣는다(새 견적·새 승인).
- **견적의 글자 분량**은 「아직 답이 없는 청크」만 센다 — 가장 최근 1단계 기록의 청크 WAV·범위로
  글자 캐시 열쇠(출처 `live`)를 다시 재어 본다. 기록이 없거나 청크 계획이 견적과 다르면 전부 센다
  (상한 쪽). 승인의 글자 요청 상한이 이 수다 — 사람이 본 것보다 많이 묻지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from vlogsrt import log
from vlogsrt.project.raw import (
    gemini_text,
    glossary,
    read_scope,
    source_text,
    text_ear,
)
from vlogsrt.project.raw.config import GEMINI_TEXT_SETTINGS, SOURCE_TEXT_SETTINGS
from vlogsrt.project.raw.models import AnalysisEstimate
from vlogsrt.project.raw.understanding import CHUNK_PLAN_FIXED, SampleSpan
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE

META_KEY = "source_text_cross_check"
SETTING_ON = "on"
SETTING_OFF = "off"
TAG_PREFIX = "cross-check/"
"""결속·입력 지문 끝에 붙는 꼬리표의 머리 — `cross-check/<글자 요청 신원>`."""
GEMINI_TAG_PREFIX = "gemini-text/"
"""글자 귀가 Gemini인 원본(W-35b)의 꼬리표 머리 — `gemini-text/<Gemini 글자 요청 신원>`."""

STAGE1_RECORDS = ("asr_outcome.json", "asr_phase.json", "analysis_transcript.json")
"""청크 WAV·범위를 실은 1단계 기록 — 한 폴더에 여럿이면 앞의 것(전사 직후 기록)을 읽는다."""

USD_STEP = Decimal("0.000001")
"""견적 금액의 자리 — 분당 단가 × 초를 올림해 담는다(화면은 다시 센트로 올린다)."""


def enabled(workspace: Any) -> bool:
    """교차 확인이 켜져 있는가 — 고른 적이 없거나 모르는 값이면 켜짐(기본)이다.

    읽기 범위(그리기 한 번) 안에서는 한 번만 읽는다(D63)."""
    return read_scope.memo(
        ("cross_check.enabled", str(workspace.root)), lambda: _enabled(workspace)
    )


def _enabled(workspace: Any) -> bool:
    with workspace.connect() as db:
        row = db.execute("SELECT value FROM meta WHERE key=?", (META_KEY,)).fetchone()
    return row is None or str(row[0]) != SETTING_OFF


def request_identity(workspace: Any) -> str:
    """지금 용어집으로 묻는 글자 요청의 신원 — 승인·꼬리표가 묶는 값."""
    return source_text.text_identity(SOURCE_TEXT_SETTINGS, glossary.current(workspace).sha)


def binding_tag(workspace: Any) -> str | None:
    """gpt 다른 귀의 작업공간 꼬리표 — 꺼져 있으면 None(덧붙이지 않는다, 19d 이전 바이트).

    견적이 digest와 함께 들고 다니며 승인 직전에 용어집이 바뀌었는지 대조한다. 원본 하나의 결속·
    입력 지문에 붙는 꼬리표는 `source_tag`다(글자 귀마다 다르다, W-35b)."""
    if not enabled(workspace):
        return None
    return TAG_PREFIX + request_identity(workspace)


def source_tag(workspace: Any, asset_id: str) -> str | None:
    """원본 하나의 결속·입력 지문 꼬리표 — 꺼져 있으면 None.

    글자 귀(`text_ear`)가 Gemini면 `gemini-text/<신원>`, gpt면 작업공간 꼬리표 그대로다 — W-35b
    이전에 들은 영상은 gpt 귀로 남으므로 결속·지문이 바이트 그대로다(끝난 분석을 다시 사지
    않는다)."""
    if not enabled(workspace):
        return None
    ear = text_ear.resolve(workspace, asset_id)
    if ear == text_ear.EAR_NONE:
        return None  # Gemini 시간 엔진 원본 — 다른 귀가 없다(D104)
    if ear == text_ear.EAR_GEMINI:
        return GEMINI_TAG_PREFIX + gemini_text.text_identity(GEMINI_TEXT_SETTINGS)
    return TAG_PREFIX + request_identity(workspace)


# ─────────────────────────────────────────────────────────────
#  견적 — 아직 답이 없는 청크만 센다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _RecordedChunk:
    """가장 최근 1단계 기록의 청크 하나 — 글자 캐시 열쇠를 다시 잴 재료."""

    sample_start: int
    sample_end: int
    wav_sha256: str
    sample_rate: int


def with_text_passes(
    workspace: Any, estimates: Sequence[AnalysisEstimate]
) -> tuple[AnalysisEstimate, ...]:
    """견적 줄마다 교차 확인 글자 분량 칸을 비운다 — gpt·whisper 귀는 은퇴했다(D163, 바이트 그대로).

    켬·끔 표지는 이미 들은 원본의 결속 꼬리표(`source_tag`)로만 읽는다."""
    del workspace
    return tuple(_without_text(item) for item in estimates)


def _without_text(item: AnalysisEstimate) -> AnalysisEstimate:
    return replace(
        item,
        text_chunk_count=None,
        text_seconds=None,
        text_usd=None,
        text_chunk_keys=None,
        text_model=None,
        rescue_chunk_count=None,
        rescue_usd=None,
    )


def _text_key(chunk: _RecordedChunk, glossary_digest: str) -> str:
    span = SampleSpan(chunk.sample_start, chunk.sample_end)
    return source_text.text_chunk_key(
        chunk.wav_sha256, SOURCE_TEXT_SETTINGS, glossary_digest, span, ORIGIN_LIVE
    )


def _recorded_chunks(
    root: Path, asset_id: str, source_sha256: str, *, replanned: bool = False
) -> tuple[_RecordedChunk, ...] | None:
    """이 원본의 가장 최근 1단계 기록의 청크 — 원본 해시가 같은 것만. 없으면 None. 읽기만 한다.

    `replanned`면 여러 청크의 고정 계획 기록은 건너뛴다 — 다음 실행의 청크가 아니다(D74)."""
    found: list[tuple[str, tuple[_RecordedChunk, ...]]] = []
    for folder in sorted(root.glob(f"analysis/*/*_{asset_id}")):
        try:
            parsed = _chunks_in(folder, source_sha256, replanned)
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.warn(f"교차 확인 범위 — 1단계 기록을 읽지 못해 건너뜁니다: {folder.name} ({error})")
            continue
        if parsed is not None:
            found.append(parsed)
    return max(found, key=lambda pair: pair[0])[1] if found else None


def _chunks_in(
    folder: Path, source_sha256: str, replanned: bool = False
) -> tuple[str, tuple[_RecordedChunk, ...]] | None:
    for name in STAGE1_RECORDS:
        path = folder / name
        if not path.is_file():
            continue
        body = json.loads(path.read_text(encoding="utf-8"))
        asr = body.get("asr")
        if body.get("source_sha256") != source_sha256 or not isinstance(asr, dict):
            return None
        if not asr.get("chunks"):
            return None
        rate = int(asr["sample_rate"])
        chunks = tuple(
            _RecordedChunk(
                int(chunk["sample_start"]), int(chunk["sample_end"]), str(chunk["wav_sha256"]), rate
            )
            for chunk in asr["chunks"]
        )
        fixed = asr.get("chunk_plan", CHUNK_PLAN_FIXED) == CHUNK_PLAN_FIXED
        if replanned and fixed and len(chunks) > 1:
            return None
        return str(body.get("created_at") or ""), chunks
    return None


__all__ = [
    "META_KEY",
    "SETTING_OFF",
    "SETTING_ON",
    "TAG_PREFIX",
    "GEMINI_TAG_PREFIX",
    "binding_tag",
    "enabled",
    "request_identity",
    "source_tag",
    "with_text_passes",
]
