"""원본마다의 1단계 시간 엔진(W-31d, D92) — 정하기·고정·「새로 듣는 엔진」.

한 원본의 지금 전사는 한 엔진의 것이다. 저장된 작업공간 설정(`RawWorkflowConfig`)은 whisper 그대로
두고, 원본마다 그 엔진의 설정 보기(`source_workflow`)를 만든다 — whisper 원본은 **같은 객체**라 청크
열쇠·입력 지문·견적 digest·결속이 바이트 그대로이고(이미 산 34개를 다시 사지 않는다), Gemini 원본만
새 열쇠를 갖는다.

정하는 순서(`resolve`):

1. 다시 듣기가 기다리면 → 이번 차례에 이미 고정된 엔진, 없으면 「새로 듣는 엔진」
2. 이번 차례에 고정된 엔진이 있으면 → 그 엔진
3. 원본 자막 문서가 이미 있으면 → whisper(엔진을 바꾸는 길은 「다시 듣기」뿐이다)
4. 처음 듣는 영상 → 「새로 듣는 엔진」

고정(`pin`)은 1단계 듣기가 시작될 때, 첫 유료 호출 전에 쓰이고 바뀌지 않는다 — 들은 뒤 설정을 바꿔도
그 전사의 엔진이 조용히 바뀌지 않는다. 「새로 듣는 엔진」은 작업공간 `meta` 표지 하나다(설정·지문
밖). 읽기는 표를 만들지 않고, 그리기 범위 안에서는 표지와 고정 표를 한 번에 읽는다(D63).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from vlogsrt.project.raw import asr_epoch, read_scope, source_transcript
from vlogsrt.project.raw.config import (
    CONFIRMED_TIMING_SHUTDOWN_DATE,
    GEMINI_TRANSCRIBE_PROFILES,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
    TIMING_ENGINES,
    RawWorkflowConfig,
    timing_engine_for_model,
    timing_engine_view,
)
from vlogsrt.project.raw.models import RawWorkspaceError

TABLE = "source_timing_engines"
META_KEY = "stage1_timing_engine"
TAG_PREFIX = "timing-engine/"
"""결속·입력 지문에 실리는 꼬리표의 머리 — whisper 원본에는 붙지 않는다(바이트 그대로)."""
DEFAULT_NEW_HEARING_ENGINE = TIMING_ENGINE_GEMINI
"""「새로 듣는 엔진」의 기본 — Gemini 3.5 Transcribe(D104, 2026-10-29 사용자 지시: whisper 종료를
기다리지
않는다). whisper는 종료일까지 고를 수 있는 이전 엔진이다 — 이미 들은 영상은 고정 그대로다."""


NEW_HEARING_ENGINES: tuple[str, ...] = (TIMING_ENGINE_GEMINI,)
"""새로 들을 수 있는 엔진 — whisper는 은퇴했다(D163). 저장된 「whisper」 표지는 무시한다."""


def new_hearing_engine(workspace: Any) -> str:
    """처음 듣거나 다시 듣는 영상이 쓸 엔진 — 표지가 없거나 고를 수 없는 값이면 기본이다."""
    value = _state(workspace)[1]
    return value if value in NEW_HEARING_ENGINES else DEFAULT_NEW_HEARING_ENGINE


def set_new_hearing_engine(workspace: Any, key: str) -> None:
    """「새로 듣는 엔진」을 바꾼다 — 이미 들은 영상은 그대로다(바꾸려면 다시 듣기)."""
    _require_engine(key)
    if key not in NEW_HEARING_ENGINES:
        raise RawWorkspaceError(f"{key}로는 새로 듣지 않는다 — whisper는 은퇴했다(D163)")
    workspace.set_meta_value(META_KEY, key)


def pinned(workspace: Any, asset_id: str, epoch: int) -> str | None:
    return _state(workspace)[0].get((asset_id, epoch))


def pin(workspace: Any, asset_id: str, epoch: int, key: str) -> None:
    """그 원본의 그 차례를 그 엔진으로 고정한다 — 같은 값은 다시 써도 되고, 다른 값은 거부한다."""
    _require_engine(key)
    with workspace.connect() as db:
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} (asset_id TEXT NOT NULL, epoch INTEGER NOT NULL,"
            " engine TEXT NOT NULL, PRIMARY KEY(asset_id, epoch))"
        )
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            f"SELECT engine FROM {TABLE} WHERE asset_id=? AND epoch=?", (asset_id, epoch)
        ).fetchone()
        stale = row is not None and _retired_pending(workspace, asset_id, epoch, str(row[0]))
        if row is not None and str(row[0]) != key and not stale:
            db.rollback()
            raise RawWorkspaceError(
                f"원본 {asset_id}의 차례 {epoch}는 이미 {row[0]} 엔진으로 고정됐다 — 엔진을"
                " 바꾸려면 「다시 듣기」를 고르세요"
            )
        if row is None or str(row[0]) != key:
            db.execute(
                f"INSERT OR REPLACE INTO {TABLE}(asset_id, epoch, engine) VALUES(?,?,?)",
                (asset_id, epoch, key),
            )
        db.commit()


def _retired_pending(workspace: Any, asset_id: str, epoch: int, engine: str) -> bool:
    """기다리는 다시 듣기가 은퇴한 whisper로 고정됐다 — 새 엔진으로 바꾼다(D163)."""
    if engine in NEW_HEARING_ENGINES:
        return False
    state = asr_epoch.state(workspace, asset_id)
    return state.pending and state.epoch == epoch


def resolve(workspace: Any, asset_id: str) -> str:
    """그 원본이 지금 쓰는 엔진의 열쇠 — 정하는 순서는 모듈 설명 그대로다."""
    state = asr_epoch.state(workspace, asset_id)
    found = pinned(workspace, asset_id, state.epoch)
    if state.pending:  # 은퇴한 whisper로 고정된 다시 듣기는 새 엔진으로 듣는다(D163)
        return found if found in NEW_HEARING_ENGINES else new_hearing_engine(workspace)
    if found is not None:
        return found
    if source_transcript.load(workspace, asset_id) is not None:
        return TIMING_ENGINE_WHISPER
    return new_hearing_engine(workspace)


def source_workflow(workspace: Any, asset_id: str, base: RawWorkflowConfig) -> RawWorkflowConfig:
    """그 원본을 듣는 설정 보기 — whisper면 `base` 그대로(같은 객체)다."""
    return timing_engine_view(base, TIMING_ENGINES[resolve(workspace, asset_id)])


def binding_tag(workspace: Any, asset_id: str) -> str | None:
    """결속·견적에 실을 꼬리표 — whisper면 None(덧붙이지 않는다, 바이트 그대로)."""
    key = resolve(workspace, asset_id)
    return None if key == TIMING_ENGINE_WHISPER else f"{TAG_PREFIX}{key}"


@dataclass(frozen=True, slots=True)
class EngineFacts:
    """화면이 말하는 엔진 하나(W-31e) — 이름·그날의 분당 단가(없으면 가격 미정)·확인일·기한·종료일.

    whisper의 분당 단가는 싣지만 화면은 말하지 않는다(D57 결정 7 — whisper 전사비는 금액 없이)."""

    key: str
    model: str
    label: str
    per_minute: Decimal | None
    checked_at: date | None
    valid_through: date | None
    retires_on: date | None
    contract_status: str | None = None
    """등록표의 실응답 계약 시험 상태(Gemini만 — `not_run`이면 실제 응답 모양을 아직 녹화로 확인하지
    않았다,
    H1). whisper는 None이다."""


def facts(key: str, on: date) -> EngineFacts:
    """엔진 열쇠 하나의 화면 사실 — Gemini는 등록표, whisper는 능력표의 종료일이다."""
    from vlogsrt.project.raw.capabilities import timing_price_per_minute

    _require_engine(key)
    engine = TIMING_ENGINES[key]
    profile = GEMINI_TRANSCRIBE_PROFILES.get(engine.model)
    whisper = key == TIMING_ENGINE_WHISPER
    return EngineFacts(
        key=key,
        model=engine.model,
        label=engine.label,
        per_minute=timing_price_per_minute(engine.model, on),
        checked_at=None if profile is None else profile.pricing_checked_at,
        valid_through=None if profile is None else profile.price_valid_through,
        retires_on=CONFIRMED_TIMING_SHUTDOWN_DATE if whisper else None,
        contract_status=None if profile is None else profile.integration_test_status,
    )


def key_for_model(model: str) -> str:
    """견적 줄의 시간 모델 → 엔진 열쇠(모르는 모델은 ValueError — 조용히 whisper로 읽지 않는다)."""
    return timing_engine_for_model(model).key


def _require_engine(key: str) -> None:
    if key not in TIMING_ENGINES:
        raise RawWorkspaceError(f"모르는 시간 엔진입니다: {key!r} — {', '.join(TIMING_ENGINES)}")


def _state(workspace: Any) -> tuple[dict[tuple[str, int], str], str | None]:
    """(고정 표, 「새로 듣는 엔진」 표지) — 그리기 범위 안에서는 한 번만 읽는다."""
    return read_scope.memo(("timing_engine.state", str(workspace.root)), lambda: _read(workspace))


def _read(workspace: Any) -> tuple[dict[tuple[str, int], str], str | None]:
    with read_scope.shared(("workspace.db", str(workspace.root)), workspace.connect) as db:
        meta = db.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        rows = []
        if exists:
            rows = db.execute(f"SELECT asset_id, epoch, engine FROM {TABLE}").fetchall()
    pins = {(str(row[0]), int(row[1])): str(row[2]) for row in rows}
    return pins, None if meta is None else str(meta[0])


__all__ = [
    "DEFAULT_NEW_HEARING_ENGINE",
    "EngineFacts",
    "META_KEY",
    "TABLE",
    "binding_tag",
    "facts",
    "key_for_model",
    "new_hearing_engine",
    "pin",
    "pinned",
    "resolve",
    "set_new_hearing_engine",
    "source_workflow",
]
