"""A7 작업공간 저장 선로 — 덧붙이기 표·첫 값 생존·왕복의 계약 검사(§9.2·§11.1).

순수 SQLite 선로의 시험이다: 스키마가 덧붙이기만 하는지, 등록·승인·응답 캐시가
재시작 뒤에도 같은 값으로 돌아오는지, 같은 신원의 두 번째 값이 첫 값을 못
바꾸는지를 잰다. 유리수 원점은 분자·분모 정수 쌍으로 왕복한다(§13.3).
"""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw.caption_workspace_store import (
    CAPTION_SCHEMA_STATEMENTS,
    CaptionWorkspaceStore,
    caption_schema_sql,
    init_schema,
)
from vlogsrt.project.raw.caption_workspace_types import (
    REGISTRATION_NO_AUDIO,
    REGISTRATION_READY,
    RESPONSE_ORIGIN_INJECTED,
    RESPONSE_ORIGIN_LIVE,
    ApprovalRecord,
    CachedResponse,
    CaptionWorkspaceError,
    FinalVideoRegistration,
    RunIdentity,
)
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace

REV = "a" * 64
FP = "f" * 64
OTHER_FP = "e" * 64


@pytest.fixture
def workspace(tmp_path: Path) -> RawWorkspace:
    return RawWorkspace.create(tmp_path / "ws", "프로젝트")


def a_registration(*, revision: str = REV, ordinal: int | None = 0) -> FinalVideoRegistration:
    return FinalVideoRegistration(
        registration_id="creg_0011223344556677",
        source_id="final_1",
        final_video_revision=revision,
        status=REGISTRATION_READY,
        audio_ordinal=ordinal,
        audio_stream_count=1,
        external_source="/어딘가/final.mp4",
        audio_stream_index=1,
        pcm_sha256="b" * 64,
        sample_rate=16000,
        channels=2,
        sample_count=64000,
        origin=Fraction(2),
        video_duration=8.0,
        verified_copy_rel="final/captions/final-video/aa.mp4",
        pcm_rel="final/captions/caption-audio/aaaaaaaaaaaaaaaa-o0/analysis.pcm",
        manifest_rel="final/captions/caption-audio/aaaaaaaaaaaaaaaa-o0/final-video.json",
        created_at="2026-10-02T00:00:00+00:00",
    )


def an_approval(identity: RunIdentity, *, note: str = "검토했다") -> ApprovalRecord:
    return ApprovalRecord(
        identity=identity,
        audio_stream_index=1,
        sample_start=0,
        sample_end=64000,
        text_model="gpt-transcribe",
        timing_model="whisper-1",
        settings_fingerprint=identity.fingerprint,
        budget_usd="0.1000",
        note=note,
        created_at="2026-10-02T00:00:00+00:00",
    )


def a_response(run_id: str, request_id: str, *, fingerprint: str = FP) -> CachedResponse:
    return CachedResponse(
        run_id=run_id,
        fingerprint=fingerprint,
        request_id=request_id,
        role="timing",
        chunk_index=0,
        audio_sha256="b" * 64,
        sample_start=0,
        sample_end=48000,
        sample_rate=16000,
        payload={"segments": [{"text": " 말", "start": 0.5, "end": 2.5}]},
        created_at="2026-10-02T00:00:00+00:00",
    )


# ─────────────────────────────────────────────────────────────
#  스키마 — 덧붙이기만 한다, 두 번 돌려도 무사하다
# ─────────────────────────────────────────────────────────────


def test_schema_hook_is_additive_and_idempotent(workspace: RawWorkspace) -> None:
    """`init_schema`·SQL 뭉치가 W1 마이그레이션에 쓸 수 있는 모양이다."""
    connection = sqlite3.connect(":memory:")
    init_schema(connection)
    init_schema(connection)  # 두 번 돌려도 같다
    tables = {
        row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
    }
    connection.close()
    assert {"caption_registrations", "caption_approvals", "caption_runs", "caption_responses"} <= (
        tables
    )
    second = sqlite3.connect(":memory:")
    second.executescript(caption_schema_sql())
    second.executescript(caption_schema_sql())
    second.close()


def test_store_leaves_workspace_version_untouched(workspace: RawWorkspace) -> None:
    """표를 덧붙여도 작업공간 버전 표기는 그대로다 — 중앙 마이그레이션은 W1의 것."""
    before = workspace.db_schema_version()
    CaptionWorkspaceStore(workspace.root)
    CaptionWorkspaceStore(workspace.root)  # 재시작 재열기
    assert workspace.db_schema_version() == before
    with workspace.connect() as connection:
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"caption_runs", "caption_responses"} <= names


# ─────────────────────────────────────────────────────────────
#  등록 — 재시작 왕복·유리수 원점
# ─────────────────────────────────────────────────────────────


def test_registration_roundtrip_after_restart(workspace: RawWorkspace) -> None:
    store = CaptionWorkspaceStore(workspace.root)
    store.save_registration(a_registration())
    reopened = CaptionWorkspaceStore(workspace.root)  # 프로세스가 다시 떴다
    loaded = reopened.registration(REV, 0)
    assert loaded == a_registration()
    assert loaded is not None and loaded.origin == Fraction(2)
    assert reopened.registration(REV, 1) is None
    assert [row.registration_id for row in reopened.registrations()] == ["creg_0011223344556677"]


def test_state_registration_keeps_null_ordinal(workspace: RawWorkspace) -> None:
    """no_audio 상태의 등록 — 오디오 칸들은 비어 있고 ordinal도 없다."""
    hollow = replace(
        a_registration(),
        registration_id="creg_9988776655443322",
        status=REGISTRATION_NO_AUDIO,
        audio_ordinal=None,
        audio_stream_count=0,
        audio_stream_index=None,
        pcm_sha256=None,
        sample_rate=None,
        channels=None,
        sample_count=None,
        origin=None,
        video_duration=None,
        verified_copy_rel=None,
        pcm_rel=None,
        manifest_rel=None,
    )
    store = CaptionWorkspaceStore(workspace.root)
    store.save_registration(hollow)
    loaded = store.registration(REV, None)
    assert loaded == hollow


# ─────────────────────────────────────────────────────────────
#  승인·응답 캐시 — 같은 신원의 첫 값이 산다
# ─────────────────────────────────────────────────────────────


def test_approval_first_value_survives(workspace: RawWorkspace) -> None:
    identity = RunIdentity(REV, "run-1", FP)
    store = CaptionWorkspaceStore(workspace.root)
    store.save_approval(an_approval(identity))
    store.save_approval(an_approval(identity))  # 같은 값 — 멱등
    with pytest.raises(CaptionWorkspaceError):
        store.save_approval(an_approval(identity, note="몰래 바꾼 승인"))
    assert store.approval(identity) == an_approval(identity)
    assert store.approval(RunIdentity(REV, "run-1", OTHER_FP)) is None


def test_responses_first_value_survives_and_fingerprint_filters(workspace: RawWorkspace) -> None:
    store = CaptionWorkspaceStore(workspace.root)
    store.record_responses([a_response("run-1", "run-1:timing:0000")])
    other = a_response("run-1", "run-1:timing:0000")
    other = replace(other, payload={"segments": []}, origin=RESPONSE_ORIGIN_LIVE)
    store.record_responses([other])  # 같은 자리 — 첫 값이 산다
    rows = store.responses("run-1", FP)
    assert len(rows) == 1 and rows[0].payload["segments"]
    assert rows[0].origin == RESPONSE_ORIGIN_INJECTED  # 출처도 첫 값이다
    assert store.responses("run-1", OTHER_FP) == ()  # 다른 지문은 못 본 척하지 않는다


# ─────────────────────────────────────────────────────────────
#  실행 행 — 만들기·갱신 경계
# ─────────────────────────────────────────────────────────────


def test_run_ensure_and_update_guards(workspace: RawWorkspace) -> None:
    identity = RunIdentity(REV, "run-1", FP)
    store = CaptionWorkspaceStore(workspace.root)
    record = store.ensure_run(identity)
    assert record.status == "running"
    assert store.ensure_run(identity).created_at == record.created_at  # 같은 행에 붙는다
    updated = store.update_run(identity, status="succeeded", draft_rel="subtitles/x")
    assert updated.status == "succeeded" and updated.draft_rel == "subtitles/x"
    with pytest.raises(CaptionWorkspaceError):
        store.update_run(identity, fingerprint="hacked")  # 신원 칸은 못 고친다
    with pytest.raises(CaptionWorkspaceError):
        store.update_run(RunIdentity(REV, "run-404", FP), status="failed")
    assert [row.identity.run_id for row in store.runs(REV)] == ["run-1"]


def test_db_name_matches_workspace(workspace: RawWorkspace) -> None:
    """자막 표는 작업공간 DB 안에 산다 — 따로 파는 파일이 아니다(§9.2)."""
    assert (workspace.root / DB_NAME).is_file()
    assert len(CAPTION_SCHEMA_STATEMENTS) >= 6  # 표 4 + 색인
