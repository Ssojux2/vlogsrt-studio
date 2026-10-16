"""저장된 선택 분석 상태는 가리키는 작업을 아이디로 한 번에 찾는다(W-24 세션 G).

예전에는 상태마다 최근 작업 1000줄(`list_jobs(limit=1000)`)을 통째로 읽었다 — 실데이터 상태 108개
≈ 260 ms가 영상 카드를 누를 때마다 읽기 범위 밖에서 돌았다. 그리고 작업이 1000개를 넘으면 창 밖의
끝난 작업을 못 찾아, 취소된 상태를 「중단 · 재시도 승인 필요」로 잘못 불렀다. 유료 호출 0.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.project.raw.jobs import JobStore
from vlogsrt.project.raw.selection_analysis import load_state, load_states
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.raw.workspace import DB_NAME

RECENT_WINDOW = 1000  # 예전 창 — 이보다 많은 작업이 앞에 쌓이면 가장 오래된 작업이 창 밖이다


def _workspace(tmp_path: Path) -> tuple[RawWorkspaceService, Path]:
    service = RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "선택 상태")
    return service, root


def _insert_jobs(root: Path, rows: list[tuple[str, str, str]]) -> None:
    """(아이디, 상태, 만든 시각) — 끝난 작업 줄을 바로 쓴다(본문 없음)."""
    connection = sqlite3.connect(root / DB_NAME)
    try:
        connection.executemany(
            "INSERT INTO jobs (job_id, kind, input_fingerprint, status, progress_current,"
            " progress_total, error, cancellation_requested, created_at, updated_at)"
            " VALUES (?, 'selection_analysis', ?, ?, 0, 0, NULL, 0, ?, ?)",
            [(job_id, job_id, status, at, at) for job_id, status, at in rows],
        )
        connection.commit()
    finally:
        connection.close()


def _save(root: Path, key: str, state: dict[str, Any]) -> None:
    connection = sqlite3.connect(root / DB_NAME)
    try:
        connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (key, json.dumps(state, ensure_ascii=False)),
        )
        connection.commit()
    finally:
        connection.close()


def _counted(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    counts: Counter[str] = Counter()
    for owner, name in ((JobStore, "_read_jobs"), (JobStore, "_read_by_ids")):
        original = getattr(owner, name)

        def call(*args: Any, _original: Any = original, _name: str = name, **kwargs: Any) -> Any:
            counts[_name] += 1
            return _original(*args, **kwargs)

        monkeypatch.setattr(owner, name, call)
    return counts


def test_states_find_their_jobs_past_the_old_recent_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path)
    oldest = [("job_old_cancelled", "cancelled", "2026-01-01T00:00:00+00:00")]
    newer = [
        (
            f"job_new_{index:05d}",
            "succeeded",
            f"2026-02-01T00:{index // 60 % 60:02d}:{index % 60:02d}+00:00",
        )
        for index in range(RECENT_WINDOW)
    ]
    _insert_jobs(root, oldest + newer)
    _save(
        root,
        "selection_task:cancelled",
        {"token": "cancelled", "stage": "영상 준비", "job_id": "job_old_cancelled"},
    )
    _save(
        root, "selection_task:lost", {"token": "lost", "stage": "영상 준비", "job_id": "job_gone"}
    )
    _save(
        root,
        "selection_task:done",
        {"token": "done", "stage": "추천 구간 준비 완료", "job_id": "job_new_00007"},
    )
    counts = _counted(monkeypatch)
    states = {state["token"]: state for state in load_states(service, root)}
    # 창 밖의 취소된 작업 — 예전엔 못 찾아 「중단 · 재시도 승인 필요」였다.
    assert states["cancelled"]["stage"] == "취소됨"
    assert states["lost"]["stage"] == "중단 · 재시도 승인 필요"  # 작업 기록이 없다 — 그대로다
    assert states["done"]["stage"] == "추천 구간 준비 완료"
    assert states["done"]["root"] == root and states["done"]["asset_ids"] == ()
    # 상태 셋 — 작업 목록은 한 번도 통째로 읽지 않고, 아이디 묶음 한 번이다.
    assert counts == Counter({"_read_by_ids": 1})


def test_the_selected_state_reads_only_its_own_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path)
    _insert_jobs(root, [("job_only", "cancelled", "2026-01-01T00:00:00+00:00")])
    _save(
        root, "selection_analysis_state", {"token": "t", "stage": "영상 준비", "job_id": "job_only"}
    )
    counts = _counted(monkeypatch)
    assert load_state(service, root)["stage"] == "취소됨"
    assert counts == Counter({"_read_by_ids": 1})


def test_states_without_jobs_read_no_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, root = _workspace(tmp_path)
    _save(root, "selection_task:a", {"token": "a", "stage": "추천 구간 준비 완료"})
    counts = _counted(monkeypatch)
    assert [state["stage"] for state in load_states(service, root)] == ["추천 구간 준비 완료"]
    assert load_state(service, root) == {}
    assert counts == Counter()
