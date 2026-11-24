"""원본마다의 1단계 글자 귀(W-35b, D95) — 정하기·고정·「새로 듣는 글자 귀」.

whisper 원본의 글자는 이제 Gemini 3.5 Transcribe 단어가 메인이다(`word_text`). 그런데 이미 들은
영상은 gpt-transcribe 다른 귀(D37 게이트)로 교차 확인한 글자와 그 꼬리표(`cross-check/<신원>`)를
결속·입력 지문에 싣고 있다 — 작업공간 하나로 귀를 바꾸면 그 꼬리표가 모두 바뀌어 끝난 분석(전사·
의미 판정)을 다시 산다. 그래서 귀는 시간 엔진(W-31d)처럼 **원본마다** 고정된다: 이미 들은 영상은
예전 귀(`gpt`) 그대로 — 결속·지문이 바이트 그대로이고, 그 귀에 새로 묻지도 않는다(캐시만) —
처음 듣거나 다시 듣는 영상만 「새로 듣는 글자 귀」(기본 `gemini`)다. 이미 들은 영상에 Gemini 글자를
얹는 길은 「글자만 다시 듣기」(W-35d)다.

정하는 순서(`resolve`):

1. 시간 엔진이 whisper가 아니면(Gemini 원본, D187) → 다시 듣기가 기다리면 이번 차례에 이미 고정된
   귀, 없으면 `hydrogen` · 이번 차례에 고정된 귀가 있으면 그 귀 · 원본 자막 문서가 이미 있으면
   `none`(W-114c 이전에 들은 Gemini 영상 — 결속·지문이 바이트 그대로다) · 처음 듣는 영상은
   `hydrogen`. 「새로 듣는 글자 귀」 표지는 whisper 원본의 것이라 보지 않는다(옛 값 `gpt`·`gemini`가
   남아 있을 수 있다).
2. 다시 듣기가 기다리면 → 이번 차례에 이미 고정된 귀, 없으면 「새로 듣는 글자 귀」
3. 이번 차례에 고정된 귀가 있으면 → 그 귀
4. 원본 자막 문서가 이미 있으면 → `gpt`(W-35b 이전에 들은 영상)
5. 처음 듣는 영상 → 「새로 듣는 글자 귀」

고정(`pin`)은 1단계 듣기가 시작될 때, 첫 유료 호출 전에 쓰이고 바뀌지 않는다. 「새로 듣는 글자
귀」는 작업공간 `meta` 표지 하나다(설정·지문 밖, 화면 칸은 없다).
"""

from __future__ import annotations

from typing import Any

from vlogsrt.project.raw import asr_epoch, read_scope, source_transcript, timing_engine
from vlogsrt.project.raw.config import TIMING_ENGINE_WHISPER
from vlogsrt.project.raw.models import RawWorkspaceError

EAR_GPT = "gpt"
"""gpt-transcribe 다른 귀 — whisper 원본은 D37 게이트, Gemini 원본은 후보로만(D59 19c·D92)."""
EAR_GEMINI = "gemini"
"""Gemini 3.5 Transcribe 단어를 whisper 줄에 배정한다(D95)."""
EAR_NONE = "none"
"""다른 귀가 없다 — W-114c 이전에 들은 Gemini 시간 엔진 원본(D104). 글자와 시각이 한 응답이다."""
EAR_HYDROGEN = "hydrogen"
"""Gemini 원본의 줄 글자를 Gemini 4 Hydrogen이 같은 청크 WAV로 다시 듣는다(D187) — 시각·줄은
Transcribe 그대로, 줄 아이디로만 맞춘다(`hydrogen_text`)."""
EARS = (EAR_GPT, EAR_GEMINI, EAR_NONE, EAR_HYDROGEN)
DEFAULT_GEMINI_ENGINE_EAR = EAR_HYDROGEN
"""처음 듣거나 다시 듣는 Gemini 원본의 글자 귀 — 사용자 지시(2026-11-21, D187)."""

TABLE = "source_text_ears"
META_KEY = "stage1_text_ear"
DEFAULT_NEW_HEARING_EAR = EAR_GEMINI
"""처음 듣는 whisper 원본의 글자 귀 — 사용자 지시(2026-10-27, D95): 자막 글자는 Gemini가 메인."""


def new_hearing_ear(workspace: Any) -> str:
    """처음 듣거나 다시 듣는 영상이 쓸 귀 — 표지가 없거나 모르는 값이면 기본이다."""
    value = _state(workspace)[1]
    return value if value in EARS else DEFAULT_NEW_HEARING_EAR


def set_new_hearing_ear(workspace: Any, ear: str) -> None:
    """「새로 듣는 글자 귀」를 바꾼다 — 이미 들은 영상은 그대로다(바꾸려면 다시 듣기)."""
    _require_ear(ear)
    workspace.set_meta_value(META_KEY, ear)


def pinned(workspace: Any, asset_id: str, epoch: int) -> str | None:
    return _state(workspace)[0].get((asset_id, epoch))


def pin(workspace: Any, asset_id: str, epoch: int, ear: str) -> None:
    """그 원본의 그 차례를 그 귀로 고정한다 — 같은 값은 다시 써도 되고, 다른 값은 거부한다."""
    _require_ear(ear)
    with workspace.connect() as db:
        db.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} (asset_id TEXT NOT NULL, epoch INTEGER NOT NULL,"
            " ear TEXT NOT NULL, PRIMARY KEY(asset_id, epoch))"
        )
        db.execute("BEGIN IMMEDIATE")
        row = db.execute(
            f"SELECT ear FROM {TABLE} WHERE asset_id=? AND epoch=?", (asset_id, epoch)
        ).fetchone()
        if row is not None and str(row[0]) != ear:
            db.rollback()
            raise RawWorkspaceError(
                f"원본 {asset_id}의 차례 {epoch}는 이미 {row[0]} 글자 귀로 고정됐다 — 귀를"
                " 바꾸려면 「다시 듣기」를 고르세요"
            )
        if row is None:
            db.execute(
                f"INSERT INTO {TABLE}(asset_id, epoch, ear) VALUES(?,?,?)", (asset_id, epoch, ear)
            )
        db.commit()


def resolve(workspace: Any, asset_id: str) -> str:
    """그 원본이 지금 쓰는 글자 귀 — 정하는 순서는 모듈 설명 그대로다."""
    gemini = timing_engine.resolve(workspace, asset_id) != TIMING_ENGINE_WHISPER
    fresh = DEFAULT_GEMINI_ENGINE_EAR if gemini else new_hearing_ear(workspace)
    heard = EAR_NONE if gemini else EAR_GPT
    state = asr_epoch.state(workspace, asset_id)
    found = pinned(workspace, asset_id, state.epoch)
    if state.pending:
        return found or fresh
    if found is not None:
        return found
    if source_transcript.load(workspace, asset_id) is not None:
        return heard
    return fresh


def _require_ear(ear: str) -> None:
    if ear not in EARS:
        raise RawWorkspaceError(f"모르는 글자 귀입니다: {ear!r} — {', '.join(EARS)}")


def _state(workspace: Any) -> tuple[dict[tuple[str, int], str], str | None]:
    """(고정 표, 「새로 듣는 글자 귀」 표지) — 그리기 범위 안에서는 한 번만 읽는다."""
    return read_scope.memo(("text_ear.state", str(workspace.root)), lambda: _read(workspace))


def _read(workspace: Any) -> tuple[dict[tuple[str, int], str], str | None]:
    with read_scope.shared(("workspace.db", str(workspace.root)), workspace.connect) as db:
        meta = db.execute("SELECT value FROM meta WHERE key = ?", (META_KEY,)).fetchone()
        exists = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
        ).fetchone()
        rows = []
        if exists:
            rows = db.execute(f"SELECT asset_id, epoch, ear FROM {TABLE}").fetchall()
    pins = {(str(row[0]), int(row[1])): str(row[2]) for row in rows}
    return pins, None if meta is None else str(meta[0])


__all__ = [
    "DEFAULT_GEMINI_ENGINE_EAR",
    "DEFAULT_NEW_HEARING_EAR",
    "EARS",
    "EAR_GEMINI",
    "EAR_GPT",
    "EAR_HYDROGEN",
    "EAR_NONE",
    "META_KEY",
    "TABLE",
    "new_hearing_ear",
    "pin",
    "pinned",
    "resolve",
    "set_new_hearing_ear",
]
