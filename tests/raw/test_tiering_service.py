"""등급은 저장된 분석 문서(AI 판정 + 생성 자막)로만, 그릴 때 바로, 분석 지문 밖에서 매긴다(D51·D52).

유료 호출 0회 — 제공자 조립 자체를 막아 두고 잰다. 로컬 측정(PCM RMS·프레임 화질)과 그 곁파일·
백그라운드 계산 작업은 없다(D52): 보고서는 문서를 읽을 때마다 다시 계산하고 어디에도 남기지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sqlite3
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_tier_review_gate import analyzed_tier_project
from vlogsrt import preprocess
from vlogsrt.project.raw import analysis_config, tiering
from vlogsrt.project.raw.analysis_config import TIER_SETTINGS, RawUnderstandingSettings
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.service import RawWorkspaceService

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _request_rows(root: Path) -> int:
    with sqlite3.connect(root / "project.sqlite3") as db:
        present = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='analysis_requests'"
        ).fetchone()
        return db.execute("SELECT COUNT(*) FROM analysis_requests").fetchone()[0] if present else 0


def _only_row(root: Path):
    service = RawWorkspaceService()
    summary = service.analysis_overview(root)
    assert summary is not None
    (row,) = [r for r in summary.results if r.document_path]
    return service, summary.revision.revision_id, row


def _forbid_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("등급 계산이 제공자를 조립했다")

    for name in ("_analysis_providers", "_live_asr_providers", "_live_semantic_providers"):
        monkeypatch.setattr(RawWorkspaceService, name, refuse)


def _listing(directory: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(directory)): path.read_bytes()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _tiers(service: RawWorkspaceService, root: Path, revision_id: str, asset_id: str) -> list:
    return service.selection_suitability(root, revision_id, (asset_id,))[asset_id]


@needs_ffmpeg
def test_tier_report_uses_stored_evidence_with_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, root = analyzed_tier_project(tmp_path, suitability="usable")
    service, revision_id, row = _only_row(root)
    document = root / row.document_path
    before = (_listing(document.parent), (root / row.record_path).read_bytes(), _request_rows(root))
    _forbid_providers(monkeypatch)
    report = service.tier_report(root, revision_id, row.asset_id)
    assert [(s.tier, s.rule) for s in report.spans] == [
        ("yellow", "video-usable"),
        ("red", "silent-not-scenery"),
        ("yellow", "video-usable"),
    ]
    red = report.spans[1]
    assert (red.start, red.end) == (Fraction(33, 10), Fraction(107, 10))
    assert (red.audio.status, red.video.status) == ("subtitle_silence", "usable")
    rows = _tiers(service, root, revision_id, row.asset_id)
    assert [r["tier"] for r in rows] == ["yellow", "red", "yellow"]
    assert {"recommendation", "tier", "rule", "audio", "video", "red_reason", "hold"} <= set(
        rows[0]
    )
    assert rows[1]["red_reason"] == "tier_silence" and rows[1]["hold"] is False
    after = (_listing(document.parent), (root / row.record_path).read_bytes(), _request_rows(root))
    assert after == before  # 분석 폴더에 아무것도 쓰지 않고, 새 요청 0건
    # 저장 PCM과 프레임 그림이 없어도 등급은 같다 — 둘 다 재료가 아니다.
    (document.parent / "analysis.pcm").unlink()
    shutil.rmtree(document.parent / "frames", ignore_errors=True)
    assert _tiers(service, root, revision_id, row.asset_id) == rows
    assert service.tier_report(root, revision_id, row.asset_id) == report


@needs_ffmpeg
def test_tiers_follow_document_without_cache(tmp_path: Path):
    """보관본이 없다 — 문서의 Luna 판정이 바뀌면 다음 읽기가 곧바로 따른다."""
    _, root = analyzed_tier_project(tmp_path, suitability="usable")
    service, revision_id, row = _only_row(root)
    assert [r["tier"] for r in _tiers(service, root, revision_id, row.asset_id)] == [
        "yellow",
        "red",
        "yellow",
    ]
    document = root / row.document_path
    body = json.loads(document.read_text(encoding="utf-8"))
    body["annotations"][0]["suitability"] = "recommended"
    document.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    rows = _tiers(service, root, revision_id, row.asset_id)
    assert [(r["tier"], r["rule"]) for r in rows] == [
        ("green", "both-good"),
        ("yellow", "scenery-no-speech"),
        ("green", "both-good"),
    ]
    assert not list(root.rglob("tier_report.json"))
    assert not list(root.rglob("quality.json"))


@needs_ffmpeg
def test_tier_settings_change_does_not_change_analysis_fingerprint_or_cached_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, root = analyzed_tier_project(tmp_path)
    service, revision_id, row = _only_row(root)
    order = service.latest_global_source_order(root)
    assets = {a.asset_id: a for a in service.list_source_assets(root)}
    workflow = service.settings(root)

    def fingerprints() -> tuple[str, str, str | None]:
        return (
            service._analysis_fingerprint(
                order.revision_id, assets, RawUnderstandingSettings(), workflow, "replay", root=root
            ),
            service._analysis_fingerprint(
                order.revision_id, assets, RawUnderstandingSettings(), workflow, "live", root=root
            ),
            service.cached_selection_analysis(root, tuple(assets)),
        )

    before = fingerprints()
    stored = AnalysisStore(service._workspace(root)).revision(revision_id)
    assert stored is not None and stored.fingerprint == before[0]
    changed = replace(TIER_SETTINGS, silence_min_seconds=9.0, speech_density_green=0.9)
    monkeypatch.setattr(analysis_config, "TIER_SETTINGS", changed)
    monkeypatch.setattr(tiering, "TIER_SETTINGS", changed)
    assert fingerprints() == before
    # 등급 칸은 분석 설정·워크플로 설정 어디에도 없다 — 분석 지문에 닿을 길이 없다.
    tier_fields = set(vars(TIER_SETTINGS))
    assert not tier_fields & set(vars(RawUnderstandingSettings()))
    assert not tier_fields & set(json.loads(stored.settings_json))
    assert "tier" not in json.dumps(workflow.__dict__, default=str)


@needs_ffmpeg
def test_progress_document_yields_no_red_audio(tmp_path: Path):
    _, root = analyzed_tier_project(tmp_path, suitability="usable")
    service, revision_id, row = _only_row(root)
    document = root / row.document_path
    hidden = document.with_name("hidden.json")
    document.rename(hidden)  # Luna가 아직 도는 모양 — 완성 문서가 없고 진행 문서만 있다
    report = service.tier_report(root, revision_id, row.asset_id)
    assert report is not None and report.spans
    assert all(s.tier != "red" for s in report.spans)
    assert {s.audio.status for s in report.spans} <= {
        "speech_dense",
        "speech_sparse",
        "subtitle_pending",
    }
    assert "subtitle_pending" in {s.audio.status for s in report.spans}
    progress = json.loads((document.parent / "suitability_progress.json").read_text())
    progress["analysis_revision_id"] = "anal_other"
    (document.parent / "suitability_progress.json").write_text(json.dumps(progress))
    assert service.tier_report(root, revision_id, row.asset_id) is None


@needs_ffmpeg
def test_render_returns_tiers_immediately_without_background_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_tier_review_gate import tier_project
    from tests.raw.test_visual_workbench_ui import _model_of
    from ui.workspace_service import RawWorkspaceUi

    _, root = tier_project(tmp_path, suitability="usable")
    service, revision_id, row = _only_row(root)
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.open_workspace_view(str(root))
    monkeypatch.setattr(
        server,
        "_task_views",
        lambda _root: {row.asset_id: {"revision": revision_id, "stage": "추천 구간 준비 완료"}},
    )
    jobs_before = {job.job_id for job in service.list_jobs(root)}
    model = _model_of(server.visual_workbench_render(root)[1])
    source = model["sources"][row.asset_id]
    assert [s["tier"] for s in source["suggestions"]] == ["yellow", "red", "yellow"]
    assert source["analysisState"] == "추천 구간 준비 완료"
    assert "등급 계산" not in source["analysisState"] and "무료" not in source["analysisState"]
    new_jobs = [job for job in service.list_jobs(root) if job.job_id not in jobs_before]
    assert new_jobs == []
    assert not any(job.kind == "tier_reports" for job in service.list_jobs(root))


def test_free_tier_measurement_is_gone():
    """D52 — 로컬 측정 계층(공백 RMS·프레임 화질·곁파일·계산 작업)은 지워졌다."""
    assert importlib.util.find_spec("vlogsrt.project.raw.tier_inputs") is None
    for name in ("frame_quality_stats", "parse_frame_quality", "FrameQuality"):
        assert not hasattr(preprocess, name), name
    for name in ("start_tier_reports", "_compute_tier_reports", "refresh_tiers"):
        assert not hasattr(RawWorkspaceService, name), name
    from vlogsrt.project.raw import service

    for name in ("TIER_REPORT_JOB_KIND", "TIER_REPORT_FINGERPRINT", "report_from_document"):
        assert not hasattr(service, name), name
