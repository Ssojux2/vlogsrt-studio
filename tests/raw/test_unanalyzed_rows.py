"""선택 밖 원본의 「분석 안 함」 행을 한 번에 쓰기(P-2 · 성능 계획 Phase 4-6, D156).

리비전은 실행마다 새로 서고 순서는 언제나 전부 산다 — 그래서 선택 밖 원본마다 「unanalyzed」
행이 있어야 한다. 예전에는 그 행을 원본마다 따로(연결 · `BEGIN IMMEDIATE` · 커밋 · 다시 읽기,
그리고 원본 이름 조회 한 번) 썼다 — 영상마다 승인하는 일괄 승인이면 라이브러리 크기의 제곱이다.
시험이 못 박는 것:

- 행은 예전과 같다(시각 칸만 다르다) — 순번 · 이름 · 지문 「-」 · 상태.
- 한 실행에서 그 행들은 트랜잭션 하나, 원본 목록 읽기 하나로 쓴다. 분석한 원본의 행은 예전 길
  그대로다.
- 같은 리비전에서 신원(순번 · 이름)이 다른 행이 있으면 그 묶음 전체가 들어가지 않는다
  (예전처럼 실패).
- 모르는 원본은 예전처럼 「없는 자산 ID」로 실패한다.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt.project.raw import analysis_workflow as aw
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.models import AnalysisError, AnalysisRevision, RawWorkspaceError

N = 7
SELECTED = ("a2", "a5")


def _workspace(tmp_path: Path) -> Any:
    service = raw_service.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "행 시험")
    workspace = service._workspace(root)
    assets = [SimpleNamespace(asset_id=f"a{k}", source_name=f"clip{k}.mov") for k in range(N)]
    by_id = {asset.asset_id: asset for asset in assets}
    lookups: list[str] = []

    def source_asset(asset_id: str) -> Any:
        lookups.append(asset_id)
        if asset_id not in by_id:
            raise RawWorkspaceError(f"없는 자산 ID: {asset_id}")
        return by_id[asset_id]

    workspace.source_asset = source_asset
    workspace.list_source_assets = lambda: list(assets)
    workspace.lookups = lookups
    return workspace


def _revision(store: AnalysisStore, name: str) -> AnalysisRevision:
    now = aw._now()
    revision = AnalysisRevision(
        name, "o", "user_selected", "full", "{}", "{}", "fp", "reserved", N, 0, None, now, now
    )
    store.insert_revision(revision)
    return revision


def _context() -> Any:
    return SimpleNamespace(
        check_cancel=lambda: None,
        set_total=lambda total: None,
        set_progress=lambda value: None,
        advance=lambda: None,
    )


def _run(
    monkeypatch: pytest.MonkeyPatch, workspace: Any, store: AnalysisStore, revision: Any
) -> dict[str, int]:
    def analyze_one(context, workspace, store, revision, ordinal, asset, *rest):  # noqa: ANN001
        aw._upsert(
            store,
            revision,
            asset_id=asset.asset_id,
            source_order=ordinal,
            source_name=asset.source_name,
            fingerprint="fp-" + asset.asset_id,
            status="analyzed",
        )

    calls = {"one": 0, "many": 0}
    one, many = store.upsert_source_result, store.upsert_source_results

    def count_one(result: Any) -> Any:
        calls["one"] += 1
        return one(result)

    def count_many(results: Any) -> None:
        calls["many"] += 1
        many(results)

    monkeypatch.setattr(aw, "_analyze_one", analyze_one)
    monkeypatch.setattr(store, "upsert_source_result", count_one)
    monkeypatch.setattr(store, "upsert_source_results", count_many)
    monkeypatch.setattr(aw, "AnalysisStore", lambda _workspace: store)
    order = SimpleNamespace(
        entries=[SimpleNamespace(asset_id=f"a{k}", ordinal=k) for k in range(N)]
    )
    assets = {asset_id: workspace.source_asset(asset_id) for asset_id in SELECTED}
    workspace.lookups.clear()
    providers = SimpleNamespace(phase="full")
    aw.run_analysis(_context(), workspace, revision, order, assets, None, None, providers)
    return calls


def _plain(rows: Any) -> list[tuple[Any, ...]]:
    keep = ("asset_id", "source_order", "source_name", "fingerprint", "status", "error")
    return [tuple(getattr(row, name) for name in keep) for row in rows]


def test_unselected_rows_are_the_same_and_written_in_one_transaction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    store = AnalysisStore(workspace)
    revision = _revision(store, "r1")
    calls = _run(monkeypatch, workspace, store, revision)
    rows = sorted(store.list_source_results("r1"), key=lambda row: row.source_order)
    expected = [
        (
            f"a{k}",
            k,
            f"clip{k}.mov",
            ("fp-" if f"a{k}" in SELECTED else "") + (f"a{k}" if f"a{k}" in SELECTED else "-"),
            "analyzed" if f"a{k}" in SELECTED else "unanalyzed",
            None,
        )
        for k in range(N)
    ]
    assert _plain(rows) == expected
    assert calls == {"one": len(SELECTED), "many": 1}  # 분석한 원본만 예전 길, 나머지는 한 번에
    assert workspace.lookups == []  # 이름은 원본 목록 한 번에서 — 원본마다 묻지 않는다


def test_the_batch_matches_the_old_row_by_row_rows(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = AnalysisStore(workspace)
    old, new = _revision(store, "old"), _revision(store, "new")
    names = [(f"a{k}", k, f"clip{k}.mov") for k in range(N)]
    for asset_id, order, name in names:
        aw._upsert(
            store,
            old,
            asset_id=asset_id,
            source_order=order,
            source_name=name,
            fingerprint="-",
            status="unanalyzed",
        )
    aw._record_unanalyzed_rows(store, new, names)
    strip = lambda rows: [  # noqa: E731
        dataclasses.replace(row, revision_id="", created_at="", updated_at="") for row in rows
    ]
    assert strip(store.list_source_results("new")) == strip(store.list_source_results("old"))


def test_an_identity_clash_writes_none_of_the_batch(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    store = AnalysisStore(workspace)
    revision = _revision(store, "r1")
    aw._upsert(
        store,
        revision,
        asset_id="a3",
        source_order=3,
        source_name="clip3.mov",
        fingerprint="-",
        status="unanalyzed",
    )
    clash = [("a1", 1, "clip1.mov"), ("a3", 9, "moved.mov"), ("a4", 4, "clip4.mov")]
    with pytest.raises(AnalysisError, match="신원이 기록과 다르다"):
        aw._record_unanalyzed_rows(store, revision, clash)
    assert [row.asset_id for row in store.list_source_results("r1")] == ["a3"]


def test_an_unknown_source_still_fails_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    store = AnalysisStore(workspace)
    revision = _revision(store, "r1")
    workspace.list_source_assets = lambda: []
    with pytest.raises(RawWorkspaceError, match="없는 자산 ID"):
        aw._unanalyzed_rows(workspace, [SimpleNamespace(asset_id="ghost", ordinal=0)])
    assert store.list_source_results(revision.revision_id) == ()
