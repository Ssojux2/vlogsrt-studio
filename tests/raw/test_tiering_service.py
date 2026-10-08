"""등급 보고서는 저장된 증거로만, 곁파일에 보관되고, 분석 지문 밖에 산다(W-11, D51).

유료 호출 0회 — 제공자 조립 자체를 막아 두고 잰다. 화질 측정(2단계)은 이미 저장된 프레임
JPEG만 ffmpeg로 읽는다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_tier_review_gate import analyzed_tier_project
from tests.raw.test_tiering import FIXTURE, QUIET_GAP, write_pcm
from vlogsrt import preprocess
from vlogsrt.project.raw import analysis_config, tier_inputs, tiering
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


@needs_ffmpeg
def test_tier_report_uses_stored_evidence_with_zero_provider_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, root = analyzed_tier_project(tmp_path)
    service, revision_id, row = _only_row(root)
    document = root / row.document_path
    before = (document.read_bytes(), (root / row.record_path).read_bytes(), _request_rows(root))
    _forbid_providers(monkeypatch)
    report = service.tier_report(root, revision_id, row.asset_id)
    assert [(s.tier, s.rule) for s in report.spans] == [
        ("green", "both-good"),
        ("red", "A-quiet"),
        ("green", "both-good"),
    ]
    red = report.spans[1]
    assert (red.start, red.end) == (Fraction(33, 10), Fraction(107, 10))
    assert red.audio.rms_dbfs is not None and red.audio.rms_dbfs <= -35
    after = (document.read_bytes(), (root / row.record_path).read_bytes(), _request_rows(root))
    assert after == before  # 분석 기록은 그대로, 새 요청 0건
    sidecar = json.loads((document.parent / "tier_report.json").read_text())
    assert sidecar["tier_version"] == "raw-tier/v1"
    assert set(sidecar) == {"tier_version", "settings", "document_sha256", "audio_start", "spans"}
    quality = json.loads((document.parent / "frames" / "quality.json").read_text())
    assert quality["version"] == "frame-quality/v1" and len(quality["frames"]) == 8
    assert quality["windows"]["window_000000"]["frames"] == 8
    rows = service.selection_suitability(root, revision_id, (row.asset_id,))[row.asset_id]
    assert [r["tier"] for r in rows] == ["green", "red", "green"]
    assert {"recommendation", "tier", "rule", "audio", "video", "red_reason", "hold"} <= set(
        rows[0]
    )
    assert rows[1]["red_reason"] == "tier_silence" and rows[1]["hold"] is False


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
    changed = replace(TIER_SETTINGS, silence_rms_dbfs=-70.0, speech_density_green=0.9)
    monkeypatch.setattr(analysis_config, "TIER_SETTINGS", changed)
    monkeypatch.setattr(tiering, "TIER_SETTINGS", changed)
    assert fingerprints() == before
    # 등급 칸은 분석 설정·워크플로 설정 어디에도 없다 — 분석 지문에 닿을 길이 없다.
    tier_fields = set(vars(TIER_SETTINGS))
    assert not tier_fields & set(vars(RawUnderstandingSettings()))
    assert not tier_fields & set(json.loads(stored.settings_json))
    assert "tier" not in json.dumps(workflow.__dict__, default=str)


def _source_dir(tmp_path: Path) -> Path:
    source_dir = tmp_path / "000001_source_tier"
    source_dir.mkdir()
    shutil.copy(FIXTURE, source_dir / "analysis_transcript.json")
    write_pcm(source_dir / "analysis.pcm")
    return source_dir


def test_sidecar_recomputed_on_version_or_document_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = _source_dir(tmp_path)
    document = source_dir / "analysis_transcript.json"
    first = tier_inputs.tier_report(document, audio_start=Fraction(0))
    assert [(s.start, s.end) for s in first.spans if s.tier == "red"] == [QUIET_GAP]
    calls: list[str] = []
    real = tiering.classify

    def counted(*args, **kwargs):
        calls.append(kwargs["settings"].tier_version)
        return real(*args, **kwargs)

    monkeypatch.setattr(tiering, "classify", counted)
    assert tier_inputs.tier_report(document, audio_start=Fraction(0)) == first
    assert calls == []  # 같은 판·설정·문서면 곁파일 그대로
    bumped = replace(TIER_SETTINGS, tier_version="raw-tier/v2")
    assert tier_inputs.tier_report(
        document, audio_start=Fraction(0), settings=bumped
    ).tier_version == ("raw-tier/v2")
    tighter = replace(TIER_SETTINGS, silence_rms_dbfs=-70.0)
    assert not [
        s
        for s in tier_inputs.tier_report(document, audio_start=Fraction(0), settings=tighter).spans
        if s.tier == "red"
    ]
    shifted = tier_inputs.tier_report(document, audio_start=Fraction(1, 2))
    assert shifted is not None and shifted != first  # PCM 원점이 다르면 공백 RMS가 다르다
    assert tier_inputs.tier_report(document, audio_start=Fraction(0)) == first
    body = json.loads(document.read_text())
    body["utterances"].pop()
    document.write_text(json.dumps(body))
    again = tier_inputs.tier_report(document, audio_start=Fraction(0))
    assert again.document_sha256 != first.document_sha256
    assert calls == ["raw-tier/v2", "raw-tier/v1", "raw-tier/v1", "raw-tier/v1", "raw-tier/v1"]
    cached = json.loads((source_dir / "tier_report.json").read_text())
    assert cached["document_sha256"] == again.document_sha256
    assert cached["audio_start"] == {"num": 0, "den": 1}
    assert tier_inputs.tier_report(document, audio_start=Fraction(0), compute=False) == again
    assert tier_inputs.tier_report(document, audio_start=Fraction(1), compute=False) is None


@needs_ffmpeg
def test_progress_document_yields_no_red_audio(tmp_path: Path):
    _, root = analyzed_tier_project(tmp_path)
    service, revision_id, row = _only_row(root)
    document = root / row.document_path
    hidden = document.with_name("hidden.json")
    document.rename(hidden)  # Luna가 아직 도는 모양 — 완성 문서가 없고 진행 문서만 있다
    report = service.tier_report(root, revision_id, row.asset_id)
    assert report is not None and report.spans
    assert all(s.tier != "red" for s in report.spans)
    assert {s.audio.status for s in report.spans} <= {"speech_dense", "speech_sparse", "unmeasured"}
    assert (
        not (document.parent / "tier_report.json").exists()
        or json.loads((document.parent / "tier_report.json").read_text())["document_sha256"]
        != report.document_sha256
    )
    progress = json.loads((document.parent / "suitability_progress.json").read_text())
    progress["analysis_revision_id"] = "anal_other"
    (document.parent / "suitability_progress.json").write_text(json.dumps(progress))
    assert service.tier_report(root, revision_id, row.asset_id) is None


# ── 2단계: 저장 프레임의 로컬 화질 ──────────────────────────────────────

SIGNALSTATS_SAMPLE = """\
[Parsed_metadata_2 @ 0x1] frame:0    pts:0       pts_time:0
[Parsed_metadata_2 @ 0x1] lavfi.signalstats.YMIN=16
[Parsed_metadata_2 @ 0x1] lavfi.signalstats.YAVG=97.4108
[Parsed_metadata_2 @ 0x1] lavfi.signalstats.YDIF=0
[Parsed_metadata_2 @ 0x1] lavfi.blur=4.181742
[Parsed_blurdetect_1 @ 0x2] blur mean: 4.1817422
"""


def test_frame_quality_parse_reads_luma_and_blur():
    quality = preprocess.parse_frame_quality(Path("f.jpg"), SIGNALSTATS_SAMPLE)
    assert (quality.yavg, quality.blur) == (97.4108, 4.181742)
    without_blur = preprocess.parse_frame_quality(
        Path("f.jpg"), SIGNALSTATS_SAMPLE.replace("lavfi.blur", "x")
    )
    assert without_blur.blur is None
    with pytest.raises(RuntimeError, match="YAVG"):
        preprocess.parse_frame_quality(Path("f.jpg"), "nothing")


def _jpeg(path: Path, source: str) -> None:
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", source, "-frames:v", "1", str(path)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr


@needs_ffmpeg
def test_frame_quality_stats_measure_black_and_picture(tmp_path: Path):
    black, picture = tmp_path / "black.jpg", tmp_path / "picture.jpg"
    _jpeg(black, "color=c=black:s=320x180")
    _jpeg(picture, "testsrc2=s=320x180")
    measured = preprocess.frame_quality_stats((black, picture))
    assert [m.path for m in measured] == [black, picture]
    assert measured[0].yavg <= TIER_SETTINGS.black_luma_max < measured[1].yavg
    assert measured[1].blur is not None  # 평평한 검은 화면은 blurdetect가 값을 안 낼 수 있다


@needs_ffmpeg
def test_frame_quality_sidecar_aggregates_and_feeds_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = _source_dir(tmp_path)
    frames = source_dir / "frames"
    frames.mkdir()
    document = json.loads((source_dir / "analysis_transcript.json").read_text())
    for index in range(6):  # 창2(30–45초)의 두 장만 검다
        _jpeg(
            frames / f"frame_{index:08d}.jpg",
            "color=c=black:s=160x90" if index in (4, 5) else "testsrc2=s=160x90",
        )
    measured = tier_inputs.frame_quality(source_dir, document)
    assert set(measured) == {f"frame_{i:08d}" for i in range(6)}  # 실패한 6·7번은 잴 장이 없다
    sidecar = json.loads((frames / "quality.json").read_text())
    assert sidecar["windows"]["window_000002"]["yavg_min"] <= TIER_SETTINGS.black_luma_max
    assert sidecar["windows"]["window_000000"]["frames"] == 2
    assert "window_000003" not in sidecar["windows"]

    def refuse(paths):
        raise AssertionError("이미 잰 장을 다시 쟀다")

    monkeypatch.setattr(preprocess, "frame_quality_stats", refuse)
    assert tier_inputs.frame_quality(source_dir, document) == measured
    report = tier_inputs.tier_report(
        source_dir / "analysis_transcript.json", audio_start=Fraction(0)
    )
    defect = [s for s in report.spans if s.video.status == "defect"]
    assert defect and all(s.tier == "red" and s.red_reason == "tier_video_defect" for s in defect)
    assert (defect[0].start, defect[-1].end) == (Fraction(30), Fraction(45))
    assert defect[0].video.reason == "검은 화면 2/2장"


def _measurement_spy(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, int]]:
    """PCM·프레임 측정이 어느 스레드에서 불렸는지 남긴다 — 화면 스레드는 0이어야 한다."""
    calls: list[tuple[str, int]] = []
    real_levels, real_quality = tier_inputs.measure_gap_levels, preprocess.frame_quality_stats

    def levels(*args, **kwargs):
        calls.append(("pcm", threading.get_ident()))
        return real_levels(*args, **kwargs)

    def quality(*args, **kwargs):
        calls.append(("frames", threading.get_ident()))
        return real_quality(*args, **kwargs)

    monkeypatch.setattr(tier_inputs, "measure_gap_levels", levels)
    monkeypatch.setattr(preprocess, "frame_quality_stats", quality)
    return calls


def _wait_tier_jobs(service: RawWorkspaceService, root: Path, kind: str) -> None:
    from tests.raw.test_review_workspace import _wait_terminal

    jobs = [j for j in service.list_jobs(root) if j.kind == kind]
    assert jobs, kind
    assert all(_wait_terminal(service, root, j.job_id).status == "succeeded" for j in jobs)


@needs_ffmpeg
def test_render_path_never_measures_and_the_background_job_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, root = analyzed_tier_project(tmp_path)
    service, revision_id, row = _only_row(root)
    calls = _measurement_spy(monkeypatch)
    pending: set[str] = set()
    assert service.selection_suitability(root, revision_id, (row.asset_id,), pending=pending) == {}
    assert pending == {row.asset_id} and calls == []
    assert not (root / row.document_path).with_name("tier_report.json").exists()
    assert service.start_tier_reports(root) is None
    _wait_tier_jobs(service, root, "tier_reports")
    assert {kind for kind, _ in calls} == {"pcm", "frames"}
    assert threading.get_ident() not in {thread for _, thread in calls}
    pending.clear()
    rows = service.selection_suitability(root, revision_id, (row.asset_id,), pending=pending)
    assert [r["tier"] for r in rows[row.asset_id]] == ["green", "red", "green"] and not pending


@needs_ffmpeg
def test_workbench_render_without_sidecar_shows_pending_and_does_not_measure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_tier_review_gate import tier_project
    from tests.raw.test_visual_workbench_ui import _model_of
    from ui.workspace_service import TIER_PENDING_NOTE, RawWorkspaceUi

    _, root = tier_project(tmp_path)
    service, revision_id, row = _only_row(root)
    (root / row.document_path).with_name("tier_report.json").unlink()
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.open_workspace_view(str(root))
    monkeypatch.setattr(
        server,
        "_task_views",
        lambda _root: {row.asset_id: {"revision": revision_id, "stage": "추천 구간 준비 완료"}},
    )
    calls = _measurement_spy(monkeypatch)
    model = _model_of(server.visual_workbench_render(root)[1])
    source = model["sources"][row.asset_id]
    assert source["analysisState"] == TIER_PENDING_NOTE and source["suggestions"] == []
    assert threading.get_ident() not in {thread for _, thread in calls}
    _wait_tier_jobs(service, root, "tier_reports")
    model = _model_of(server.visual_workbench_render(root)[1])
    assert [s["tier"] for s in model["sources"][row.asset_id]["suggestions"]] == [
        "green",
        "red",
        "green",
    ]


def test_frame_measurement_failure_is_logged_not_silent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    source_dir = _source_dir(tmp_path)
    (source_dir / "frames").mkdir()
    (source_dir / "frames" / "frame_00000000.jpg").write_bytes(b"not a jpeg")
    document = json.loads((source_dir / "analysis_transcript.json").read_text())
    warnings: list[str] = []
    monkeypatch.setattr(tier_inputs.log, "warn", warnings.append)

    def broken(paths):
        raise RuntimeError("ffmpeg 실패 (코드 1)")

    monkeypatch.setattr(preprocess, "frame_quality_stats", broken)
    assert tier_inputs.frame_quality(source_dir, document) == {}
    assert warnings and "frame_00000000.jpg" in warnings[0] and "ffmpeg 실패" in warnings[0]
    stored = json.loads((source_dir / "frames" / "quality.json").read_text())
    assert stored["frames"]["frame_00000000"]["error"] == "ffmpeg 실패 (코드 1)"
