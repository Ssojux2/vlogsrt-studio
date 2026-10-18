"""VFR 원본이 섞인 라이브러리 — 원본 하나가 전체 검토를 막지 않는다(W-27a, D68).

실데이터: 아이폰 영상 34개 가운데 19개가 VFR(프레임 PTS 지터)이었다. 분할 계획이 모든 원본을 재고
하나라도 격자가 고르지 않으면 통째로 거부해서, 검토 머리도 영상 목록도 Final Cut 보내기도 없었다.
이제 계획은 덮을 수 있는 원본만 덮고 VFR 원본은 까닭과 함께 `unsupported`에 남긴다 — 목록·보내기
보고서가 그 기록을 이름으로 보인다. 덮은 원본은 아무것도 바뀌지 않는다(CFR만인 라이브러리는
D68 이전과 바이트째 같다). 유료 호출 0회 — 합성 ffmpeg 미디어만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_review_workspace import _ingest_card, _wait_terminal
from vlogsrt import preprocess
from vlogsrt.project.raw import finalcut_export
from vlogsrt.project.raw.models import UNSUPPORTED_GRID_REASON, SegmentationError
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.raw.workspace import DB_NAME, UNSUPPORTED_TABLE

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _ffmpeg(*args: str) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-400:]


def make_cfr(path: Path, seconds: str) -> Path:
    """고른 격자(25fps)의 소리 있는 합성 원본."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=25:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", str(path),
    )  # fmt: skip
    return path


def make_vfr(path: Path) -> Path:
    """가운데서 25 → 30fps로 바뀌는 합성 원본 — 아이폰처럼 격자를 증명하지 못한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=2",
        "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30:duration=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]", "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-fps_mode", "passthrough", "-c:a", "aac", "-b:a", "64k", str(path),
    )  # fmt: skip
    assert not preprocess.probe_extraction_clock(path).uniform_grid  # 재료가 정말 VFR이다
    return path


def mixed_card(card: Path) -> None:
    """촬영 시각이 없으니 자연 경로 순(D53) — CFR · VFR · CFR."""
    make_cfr(card / "01_first.mov", "3")
    make_vfr(card / "02_iphone.mov")
    make_cfr(card / "03_last.mov", "2")


def _mixed(tmp_path: Path) -> tuple[RawWorkspaceService, Path, dict[str, str]]:
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "VFR 섞임")
    card = tmp_path / "card"
    mixed_card(card)
    _ingest_card(service, root, card)
    service.ensure_source_order(root)
    ids = {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}
    return service, root, ids


def _preview(service: RawWorkspaceService, root: Path, *asset_ids: str) -> Any:
    record = _wait_terminal(
        service, root, service.start_source_preview(root, asset_ids=asset_ids).job_id
    )
    assert record.status == "succeeded", record.error
    return record


# ── 계획 — 덮을 수 있는 원본만 덮고, VFR은 까닭과 함께 남는다 ───────────────────


def test_plan_covers_supported_sources_and_records_vfr_with_its_reason(tmp_path: Path) -> None:
    service, root, ids = _mixed(tmp_path)

    plan = service.plan_segmentation(root)

    assert [c.asset_id for c in plan.coverage] == [ids["01_first.mov"], ids["03_last.mov"]]
    assert {e.asset_id for e in plan.entries} == {ids["01_first.mov"], ids["03_last.mov"]}
    (skipped,) = plan.unsupported
    assert (skipped.asset_id, skipped.source_order) == (ids["02_iphone.mov"], 2)
    assert skipped.reason == UNSUPPORTED_GRID_REASON and "격자" in skipped.detail
    # 계획과 같은 트랜잭션에 묻혔다 — 다시 읽어도 같은 값이다.
    assert service.segment_plan_revision(root, plan.revision_id) == plan


def test_review_head_initializes_and_keeps_shooting_order(tmp_path: Path) -> None:
    service, root, ids = _mixed(tmp_path)
    _preview(service, root, ids["01_first.mov"])

    facade = service.review_workspace_facade(root)
    head = facade.load_head().revision
    assert [c.source_id for c in head.clocks] == [ids["01_first.mov"], ids["03_last.mov"]]
    ordered = [s.source_id for s in facade.chronological(head)]
    assert ordered[0] == ids["01_first.mov"] and ordered[-1] == ids["03_last.mov"]
    # 머리의 계획이 VFR을 기록했다 — 같은 영상 묶음이므로 다음 미리보기가 다시 계획하지 않는다.
    assert [u.asset_id for u in service.review_unsupported_sources(root)] == [ids["02_iphone.mov"]]
    _preview(service, root, ids["03_last.mov"])
    assert len(service.list_segment_plan_revisions(root)) == 1


def test_preview_of_a_vfr_source_prepares_nothing_and_does_not_replan(tmp_path: Path) -> None:
    service, root, ids = _mixed(tmp_path)
    _preview(service, root, ids["01_first.mov"])
    before = {job.job_id for job in service.list_jobs(root)}

    _preview(service, root, ids["02_iphone.mov"])

    started = [job for job in service.list_jobs(root) if job.job_id not in before]
    assert [job.kind for job in started] == ["source_preview"]  # 쪽 준비를 하나도 걸지 않았다
    assert len(service.list_segment_plan_revisions(root)) == 1


def test_analysis_plan_skips_the_vfr_source_without_failing(tmp_path: Path) -> None:
    """분석 없는 원본도 증거 계획(정리 고리의 다시 세우기)은 VFR에서 멈추지 않는다."""
    service, root, ids = _mixed(tmp_path)

    plan = service.plan_segmentation_from_evidence(root, {})

    assert [u.asset_id for u in plan.unsupported] == [ids["02_iphone.mov"]]
    assert len(plan.coverage) == 2


# ── Final Cut 보내기 — 덮은 원본만 싣고, VFR은 이름과 까닭을 남긴다 ────────────────


def test_export_sends_supported_sources_and_reports_the_vfr_one(tmp_path: Path) -> None:
    service, root, ids = _mixed(tmp_path)
    _preview(service, root, ids["01_first.mov"])

    path = service.export_finalcut_timeline(root)

    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    assert [row["name"] for row in report["sources"]] == ["01_first", "03_last"]
    (left_out,) = report["not_exported"]
    assert left_out["asset_id"] == ids["02_iphone.mov"] and left_out["name"] == "02_iphone.mov"
    assert left_out["reason"].startswith(UNSUPPORTED_GRID_REASON)
    assert "02_iphone" not in path.read_text(encoding="utf-8")  # 격자 계산이 닿지 않았다


def test_a_library_of_only_vfr_refuses_by_name(tmp_path: Path) -> None:
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "VFR만")
    card = tmp_path / "card"
    make_vfr(card / "solo.mov")
    _ingest_card(service, root, card)
    service.ensure_source_order(root)

    with pytest.raises(SegmentationError) as planned:
        service.plan_segmentation(root)
    assert "solo.mov" in str(planned.value) and UNSUPPORTED_GRID_REASON in str(planned.value)
    with pytest.raises(finalcut_export.FinalCutExportError) as sent:
        service.export_finalcut_timeline(root)
    assert "solo.mov" in str(sent.value) and "VFR" in str(sent.value)


def test_an_all_cfr_library_leaves_no_trace_of_the_vfr_record(tmp_path: Path) -> None:
    """CFR만이면 덮지 못한 원본의 표가 생기지 않는다 — 계획·DB가 D68 이전과 같다."""
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "CFR만")
    card = tmp_path / "card"
    make_cfr(card / "a.mov", "2")
    _ingest_card(service, root, card)
    service.ensure_source_order(root)

    plan = service.plan_segmentation(root)

    assert plan.unsupported == ()
    with sqlite3.connect(root / DB_NAME) as connection:
        rows = connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = {row[0] for row in rows}
    assert UNSUPPORTED_TABLE not in tables
