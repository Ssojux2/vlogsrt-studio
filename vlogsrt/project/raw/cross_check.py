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
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import log
from vlogsrt.project.raw import asr_epoch, glossary, read_scope, source_text
from vlogsrt.project.raw.config import SOURCE_TEXT_SETTINGS
from vlogsrt.project.raw.models import AnalysisEstimate, RawWorkspaceError
from vlogsrt.project.raw.understanding import CHUNK_PLAN_FIXED, SampleSpan
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE

META_KEY = "source_text_cross_check"
SETTING_ON = "on"
SETTING_OFF = "off"
TAG_PREFIX = "cross-check/"
"""결속·입력 지문 끝에 붙는 꼬리표의 머리 — `cross-check/<글자 요청 신원>`."""

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


def set_enabled(workspace: Any, on: bool) -> bool:
    """사람이 고른 값을 저장한다(무료) — 앱을 다시 열어도 남는다."""
    if not isinstance(on, bool):
        raise RawWorkspaceError(f"교차 확인 설정은 켬·끔 둘 중 하나입니다: {on!r}")
    with workspace.connect() as db:
        db.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (META_KEY, SETTING_ON if on else SETTING_OFF),
        )
        db.commit()
    return on


def request_identity(workspace: Any) -> str:
    """지금 용어집으로 묻는 글자 요청의 신원 — 승인·꼬리표가 묶는 값."""
    return source_text.text_identity(SOURCE_TEXT_SETTINGS, glossary.current(workspace).sha)


def binding_tag(workspace: Any) -> str | None:
    """결속·입력 지문에 덧붙일 꼬리표 — 꺼져 있으면 None(덧붙이지 않는다, 19d 이전 바이트)."""
    if not enabled(workspace):
        return None
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
    """견적 줄마다 교차 확인 글자 분량을 다시 잰다 — 꺼져 있으면 그 칸을 비운다(바이트 그대로)."""
    if not enabled(workspace):
        return tuple(_without_text(item) for item in estimates)
    digest = glossary.current(workspace).sha
    owners = {
        asset.asset_id: asset.content_sha256 or "" for asset in workspace.list_source_assets()
    }
    return tuple(
        _with_text(
            workspace.root,
            item,
            owners.get(item.asset_id, ""),
            digest,
            replanned=asr_epoch.state(workspace, item.asset_id).pending,
        )
        for item in estimates
    )


def _without_text(item: AnalysisEstimate) -> AnalysisEstimate:
    return replace(
        item, text_chunk_count=None, text_seconds=None, text_usd=None, text_chunk_keys=None
    )


def _with_text(
    root: Path,
    item: AnalysisEstimate,
    source_sha256: str,
    glossary_digest: str,
    *,
    replanned: bool = False,
) -> AnalysisEstimate:
    if not item.has_audio:
        return _without_text(item)  # 소리가 없으면 물을 것이 없다
    count, seconds, keys = _pending_text(
        root, item, source_sha256, glossary_digest, replanned=replanned
    )
    return replace(
        item,
        text_chunk_count=count,
        text_seconds=str(seconds),
        text_usd=str(text_cost(seconds)),
        text_chunk_keys=keys,
    )


def text_cost(seconds: Fraction) -> Decimal:
    """글자 요청 분량의 공식 단가 금액 — 분당 단가(문구 역할 스냅샷) × 분, 올림."""
    from vlogsrt.project.raw.capabilities import ROLE_TEXT, price_per_minute

    exact = Decimal(seconds.numerator) / Decimal(seconds.denominator) / Decimal(60)
    return (exact * price_per_minute(ROLE_TEXT)).quantize(USD_STEP, rounding=ROUND_CEILING)


def _pending_text(
    root: Path,
    item: AnalysisEstimate,
    source_sha256: str,
    glossary_digest: str,
    *,
    replanned: bool = False,
) -> tuple[int, Fraction, tuple[str, ...] | None]:
    """(물을 청크 수, 그 길이, 그 청크의 글자 캐시 열쇠) — 기록이 없으면 전부·열쇠 없음.

    기록의 청크 계획이 견적과 같을 때만 캐시를 빼고 센다. 열쇠는 승인이 「이 청크만」으로 묶는다.
    `replanned`(다시 듣기 대기)면 다음 실행이 조용한 계획으로 다시 자르므로(D74) 고정 계획 기록은
    쓰지 않는다."""
    chunks = _recorded_chunks(root, item.asset_id, source_sha256, replanned=replanned)
    if chunks is None or len(chunks) != item.asr_chunk_count:
        return item.asr_chunk_count, Fraction(item.asr_seconds), None
    cache_dir = root / "cache" / source_text.TEXT_CACHE_DIRNAME / item.asset_id
    keyed = [(chunk, _text_key(chunk, glossary_digest)) for chunk in chunks]
    pending = [(c, key) for c, key in keyed if not source_text.cached_answer_exists(cache_dir, key)]
    seconds = sum(
        (Fraction(c.sample_end - c.sample_start, c.sample_rate) for c, _ in pending), Fraction(0)
    )
    return len(pending), seconds, tuple(key for _, key in pending)


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
            log.warn(f"교차 확인 견적 — 1단계 기록을 읽지 못해 건너뜁니다: {folder.name} ({error})")
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
    "binding_tag",
    "enabled",
    "request_identity",
    "set_enabled",
    "text_cost",
    "with_text_passes",
]
