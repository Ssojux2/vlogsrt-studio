"""작업대의 자막 시각 확인(D55) — 분석이 끝나면 무료 작업이 걸리고, 자막·지문은 그대로다.

재생 분석(유료 호출 0)으로 원본 자막을 심고, 가짜 에너지 VAD로 잰다. 확인은 보고서만
쓴다 — 원본 자막·분석 결과·DB의 다른 표는 한 바이트도 바뀌지 않는다. 자막을 고치면 다시
재지만 디코드·VAD는 캐시에서 온다. VAD가 없으면 측정 불가(회색)이고, 실패해도 자막은 그대로다.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from tests._sync_fakes import bursts, energy_factory, make_media, numpy_or_fake
from vlogsrt import cli
from vlogsrt.project import sync_check
from vlogsrt.project.config import ProjectConfig, SyncCheckSettings
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw import sync_report
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.vad.runtime import VadUnavailable

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


def wait(service: raw.RawWorkspaceService, root: Path, job_id: str) -> Any:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    raise AssertionError(f"작업이 끝나지 않았다: {job_id}")


def wait_sync(service: raw.RawWorkspaceService, root: Path) -> Any:
    latest = service.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
    assert latest is not None, "자막 시각 확인 작업이 걸리지 않았다"
    record = wait(service, root, latest.job_id)
    assert record.status == "succeeded", record.error
    return record


def snapshot(root: Path) -> tuple[dict[str, list[tuple[Any, ...]]], dict[str, bytes]]:
    """DB의 자막·분석 표와 `cache/sync-check` 밖의 파일 전부 — 확인이 건드리면 안 되는 것."""
    connection = sqlite3.connect(f"file:{root / 'project.sqlite3'}?mode=ro", uri=True)
    try:
        tables = {
            name: sorted(connection.execute(f"SELECT * FROM {name}").fetchall())
            for name in ("source_transcripts", "analysis_source_results", "analysis_revisions")
        }
    finally:
        connection.close()
    sync_root = root / sync_report.SYNC_CACHE
    files = {
        str(path.relative_to(root)): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
        and sync_root not in path.parents
        and not path.name.startswith("project.sqlite3")
    }
    return tables, files


@pytest.fixture(autouse=True)
def numpy_present(monkeypatch: pytest.MonkeyPatch) -> None:
    numpy_or_fake(monkeypatch)  # VAD extra 없는 개발 환경에서도 같은 단언이 돈다


@pytest.fixture
def analyzed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """톤 덩어리 원본 하나를 재생 분석한 작업공간 — 분석 뒤 확인 작업까지 끝난 상태."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    service, root, asset_id, handle = start_tone_analysis(tmp_path)
    assert wait(service, root, handle.job_id).status == "succeeded"
    wait_sync(service, root)
    return service, root, asset_id


def start_tone_analysis(tmp_path: Path) -> tuple[Any, Path, str, Any]:
    """톤 덩어리 원본 하나를 가져와 재생 분석을 건다(유료 호출 0) — 작업 손잡이를 돌려준다."""
    card = tmp_path / "card"
    make_media(card / "tone.mov")
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "시각")
    proposal = service.scan_sources(root, card)
    confirmation = service.confirm_ingest(root, proposal)
    assert wait(service, root, service.start_ingest_copy(root, confirmation).job_id).status == (
        "succeeded"
    )
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    replay = root / "analysis" / "replay"
    replay.mkdir(parents=True, exist_ok=True)
    spoken = [
        {"start": float(start), "end": float(end), "text": f"말 {index}"}
        for index, (start, end) in enumerate(bursts())
    ]
    (replay / "asr-fixtures.json").write_text(
        json.dumps({"sources": {asset.content_sha256: spoken}}), encoding="utf-8"
    )
    luna = {f"window_{i:06d}": _luna(f"window_{i:06d}") for i in range(12)}
    (replay / "luna-fixtures.json").write_text(json.dumps({"windows": luna}), encoding="utf-8")
    return service, root, asset.asset_id, service.start_analysis(root)


def _luna(window_id: str) -> dict[str, Any]:
    return {
        "window_id": window_id,
        "speech_summary": "말한다",
        "visual_summary": "회색 화면",
        "scene_tags": [],
        "evidence_ids": [window_id],
        "suggested_boundary_ids": [],
        "editing_notes": [],
        "needs_review": False,
        "uncertainties": [],
    }


def test_analysis_completion_queues_a_check_that_writes_only_its_report(
    analyzed: tuple[Any, Path, str],
) -> None:
    service, root, source_id = analyzed
    report = service.sync_report(root, source_id)
    assert report is not None and report["stale"] is False
    assert report["verdict"] == sync_check.LEVEL_GREEN, report["reasons"]
    assert report["pcm"]["basis"] == "stored", [
        c for c in report["checks"] if c["status"] != "pass"
    ]  # 1단계 PCM이 원본과 맞았다
    transcript = service.source_transcript(root, source_id)
    assert transcript is not None and transcript["revision"] in str(
        sync_report.report_path(
            root / sync_report.SYNC_CACHE,
            source_id,
            transcript["revision"],
            sync_report.SYNC_CHECK_SETTINGS,
        )
    )
    before = snapshot(root)
    handle = service.start_sync_checks(root, (source_id,), force=True)
    assert handle is not None
    wait_sync(service, root)
    assert snapshot(root) == before  # 자막·분석 표·다른 파일은 한 바이트도 바뀌지 않았다
    view = service.sync_check_view(root, source_id)
    assert view is not None and (view["label"], view["tone"]) == ("자막 시각 OK", "green")


def test_edit_makes_it_stale_and_the_rerun_needs_no_decode_or_vad(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, source_id = analyzed
    from vlogsrt import preprocess

    def forbidden(*_: Any, **__: Any) -> Any:
        raise AssertionError("다시 재기는 캐시만 써야 한다 — ffmpeg·VAD 호출 금지")

    monkeypatch.setattr(preprocess, "extract_pcm_s16le", forbidden)
    monkeypatch.setattr(preprocess, "pcm_window_s16le", forbidden)
    monkeypatch.setattr(sync_report, "infer_vad", forbidden)
    transcript = service.source_transcript(root, source_id)
    first = transcript["utterances"][0]
    service.edit_source_transcript(
        root,
        source_id,
        transcript["revision"],
        action="edit",
        utterance_id=first["utterance_id"],
        text="고친 말",
    )
    wait_sync(service, root)
    report = service.sync_report(root, source_id)
    edited = service.source_transcript(root, source_id)
    assert report is not None and report["stale"] is False
    assert report["source"]["transcript_revision"] == edited["revision"] != transcript["revision"]
    assert report["verdict"] == sync_check.LEVEL_GREEN


def test_missing_vad_runtime_is_a_grey_skip(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, source_id = analyzed

    def unavailable(project: Any = None) -> Any:
        def factory() -> object:
            raise VadUnavailable("onnxruntime가 설치되지 않았다")

        return factory

    monkeypatch.setattr(sync_report, "default_session_factory", unavailable)
    shutil.rmtree(root / sync_report.SYNC_CACHE / "vad")
    service.start_sync_checks(root, (source_id,), force=True)
    wait_sync(service, root)
    report = service.sync_report(root, source_id)
    assert report is not None and report["verdict"] == sync_check.LEVEL_SKIP
    assert any("VAD" in reason["text"] for reason in report["reasons"])
    view = service.sync_check_view(root, source_id)
    assert view is not None and (view["label"], view["tone"]) == ("측정 불가", "grey")


def test_a_failing_measurement_leaves_a_skip_report_and_subtitles_untouched(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, source_id = analyzed

    def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError("측정기가 부서졌다")

    monkeypatch.setattr(sync_report, "measure_source", broken)
    before = snapshot(root)
    service.start_sync_checks(root, (source_id,), force=True)
    wait_sync(service, root)
    report = service.sync_report(root, source_id)
    assert report is not None and report["verdict"] == sync_check.LEVEL_SKIP
    assert "측정 중 오류" in report["reasons"][0]["text"]
    assert snapshot(root) == before


def test_upkeep_measures_missing_reports_once_per_app_run(
    analyzed: tuple[Any, Path, str],
) -> None:
    _service, root, source_id = analyzed
    assert raw.RawWorkspaceService().start_sync_upkeep(root) is None  # 보고서가 지금 것이다
    shutil.rmtree(root / sync_report.SYNC_CACHE / "reports")
    reopened = raw.RawWorkspaceService()
    message = reopened.start_sync_upkeep(root)
    assert message is not None and "무료" in message
    wait_sync(reopened, root)
    shutil.rmtree(root / sync_report.SYNC_CACHE / "reports")
    assert reopened.start_sync_upkeep(root) is None  # 같은 실행에서 같은 할 일은 한 번만
    assert raw.RawWorkspaceService().start_sync_upkeep(root) is not None  # 앱을 다시 열면 한 번 더
    wait_sync(raw.RawWorkspaceService(), root)
    assert raw.RawWorkspaceService().sync_report(root, source_id) is not None


def test_media_cli_no_write_prints_and_writes_nothing(
    analyzed: tuple[Any, Path, str], capsys: pytest.CaptureFixture[str]
) -> None:
    _service, root, source_id = analyzed
    before = snapshot(root), sorted(p for p in root.rglob("*") if p.is_file())
    assert cli.main(["media", "sync-check", "--workspace", str(root), "--json", "--no-write"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [item["source"]["source_id"] for item in printed["reports"]] == [source_id]
    assert (snapshot(root), sorted(p for p in root.rglob("*") if p.is_file())) == before


def test_sync_settings_live_outside_every_fingerprinted_config() -> None:
    """기준은 어느 지문 사전에도 칸이 없다 — 바꿔도 분석·전사·승인이 낡지 않는다(D47)."""
    sync_names = {field.name for field in fields(SyncCheckSettings)}
    for config in (ProjectConfig, RawWorkflowConfig, RawUnderstandingSettings):
        names = {field.name for field in fields(config)}
        assert not names & sync_names, config
        assert not any("sync" in name for name in names), config


def test_workbench_panel_says_not_found_never_not_speech() -> None:
    """작업대 패널 — 표지 큐는 원본 자막 이동(source-pick)으로 가고 「다시 재기」는 무료다."""
    from ui import ui_facade

    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert "function syncPanel" in script and "function syncBadge" in script
    assert "말소리를 못 찾음 (측정 한계일 수 있음)" in script
    assert "말이 아니" not in script and "말이 아님" not in script
    assert 'data-vl-act="sync-recheck"' in script and "vl_sync_recheck" in script
    badge_tones = script[script.index("function syncBadge") : script.index("function syncFlagWord")]
    assert "grey" not in badge_tones  # 회색(측정 불가)은 어떤 색 표지에도 이어지지 않는다
    assert 'data-vl-act="source-pick"' in script[script.index("function syncPanel") :]


@pytest.mark.parametrize(
    ("level", "expected"),
    [
        ("red", ("자막 시각 어긋남", "red")),
        ("amber", ("자막 시각 확인", "amber")),
        ("green", ("자막 시각 OK", "green")),
        ("skip", ("측정 불가", "grey")),
        ("모르는 값", ("측정 불가", "grey")),
    ],
)
def test_badge_mapping_never_shows_skip_as_green(level: str, expected: tuple[str, str]) -> None:
    assert sync_report.badge(level) == expected
    view = sync_report.view_of({"verdict": level, "reasons": [], "checks": []}, running=False)
    assert view is not None and (view["label"], view["tone"]) == expected


# ── 표시 전용 고리는 부른 쪽의 결과를 바꾸지 못한다 · 한 번에 작업 하나 ──────────


def test_a_broken_hook_never_changes_the_analysis_or_the_edit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """확인을 걸다 DB가 잠겨도 분석은 성공으로, 편집은 저장된 그대로 돌아온다 — 경고만 남는다."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)

    def locked(*_: Any, **__: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sync_report, "pending_sources", locked)
    warnings: list[str] = []
    monkeypatch.setattr(raw.log, "warn", warnings.append)
    service, root, source_id, handle = start_tone_analysis(tmp_path)
    assert wait(service, root, handle.job_id).status == "succeeded"
    assert service.latest_job(root, raw.SYNC_CHECK_JOB_KIND) is None
    transcript = service.source_transcript(root, source_id)
    edited = service.edit_source_transcript(
        root,
        source_id,
        transcript["revision"],
        action="edit",
        utterance_id=transcript["utterances"][0]["utterance_id"],
        text="고친 말",
    )
    assert edited["utterances"][0]["text"] == "고친 말"
    assert service.source_transcript(root, source_id)["revision"] == edited["revision"]
    hooked = [line for line in warnings if "자막 시각 확인을 걸지 못했다" in line]
    assert len(hooked) == 2 and all("OperationalError" in line for line in hooked)


def test_only_one_sync_job_runs_and_later_requests_join_it(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """확인이 도는 동안 온 요청은 새 작업을 만들지 않고 도는 작업에 합쳐진다(앱 열기·분석·편집)."""
    service, root, source_id = analyzed
    real = sync_report.measure_source
    release = threading.Event()
    calls: list[str] = []

    def held(source: Any, **kwargs: Any) -> Any:
        calls.append(source.source_id)
        release.wait(30)
        return real(source, **kwargs)

    monkeypatch.setattr(sync_report, "measure_source", held)
    before = [job for job in service.list_jobs(root) if job.kind == raw.SYNC_CHECK_JOB_KIND]
    first = service.start_sync_checks(root, (source_id,), force=True)
    assert first is not None
    deadline = time.monotonic() + 30
    while not calls and time.monotonic() < deadline:
        time.sleep(0.01)
    second = service.start_sync_checks(root, (source_id,), force=True)
    third = service.start_sync_checks(root, None, force=True)
    assert second is not None and third is not None
    assert second.job_id == third.job_id == first.job_id and second.reattached
    release.set()
    assert wait(service, root, first.job_id).status == "succeeded"
    after = [job for job in service.list_jobs(root) if job.kind == raw.SYNC_CHECK_JOB_KIND]
    assert len(after) == len(before) + 1  # 두 번째 작업은 없다
    assert calls == [source_id, source_id]  # 합쳐진 요청은 같은 작업이 이어서 쟀다
    finished = service.get_job(root, first.job_id)
    assert (finished.progress_current, finished.progress_total) == (2, 2)  # 진행은 늘어난다
    assert service.start_sync_checks(root, (source_id,), force=True) is not None  # 다시 걸 수 있다
    wait_sync(service, root)


def test_clamped_lines_from_stage_one_are_shown(tmp_path: Path) -> None:
    """1단계가 청크 끝에 맞춘 줄 수는 보고서에 남는다 — 「N줄을 조각 끝에 맞춤」(D55)."""
    folder = tmp_path / "analysis" / "anal_1" / "000001_src"
    folder.mkdir(parents=True)
    body = {
        "source_sha256": "f" * 64,
        "created_at": "2026-10-09T00:00:00+00:00",
        "audio": {
            "pcm_sha256": "0" * 64,
            "audio_stream_index": 1,
            "sample_rate": 16000,
            "channels": 2,
            "sample_count": 16000 * 360,
            "pcm_sample0_file_time": {"num": 0, "den": 1},
        },
        "asr": {
            "chunks": [
                {
                    "chunk_index": 0,
                    "sample_start": 0,
                    "sample_end": 2880000,
                    "failure": "",
                    "clamped_segments": [20],
                },
                {
                    "chunk_index": 1,
                    "sample_start": 2880000,
                    "sample_end": 5760000,
                    "failure": "",
                    "clamped_segments": [3, 7],
                },
            ]
        },
    }
    (folder / "asr_phase.json").write_text(json.dumps(body), encoding="utf-8")
    stage1 = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert stage1 is not None and stage1.clamped == 3
    source = sync_report.SyncSource(
        source_id="src",
        source_name="src.mov",
        source_sha256="f" * 64,
        original=None,
        original_note="원본 없음",
        transcript_revision="rev",
        cues=(),
        stage1=stage1,
    )
    document = sync_report.measure_source(source, cache_root=tmp_path / "cache")
    texts = [reason["text"] for reason in document["reasons"]]
    assert any("3줄을 조각 끝에 맞춤" in text for text in texts), texts


# ── 검증자 재검 2(D55) — 끝나는 틈·죽은 줄·실패 표지 ─────────────────────────


def counting(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    real = sync_report.measure_source
    calls: list[str] = []

    def counted(source: Any, **kwargs: Any) -> Any:
        calls.append(source.source_id)
        return real(source, **kwargs)

    monkeypatch.setattr(sync_report, "measure_source", counted)
    return calls


def test_a_recheck_in_the_settle_gap_gets_its_own_job(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """본문은 끝났고 DB는 아직 running인 틈 — 그때 온 「다시 재기」는 새 작업이 된다."""
    from vlogsrt.project.raw import jobs

    service, root, source_id = analyzed
    real_settle = jobs.JobManager._settle
    entered, release = threading.Event(), threading.Event()

    def slow_settle(self: Any, job_id: str, **changes: object) -> None:
        record = self._store.get(job_id)
        if record.kind == raw.SYNC_CHECK_JOB_KIND and not entered.is_set():
            entered.set()
            release.wait(30)
        real_settle(self, job_id, **changes)

    monkeypatch.setattr(jobs.JobManager, "_settle", slow_settle)
    calls = counting(monkeypatch)
    first = service.start_sync_checks(root, (source_id,), force=True)
    assert first is not None and entered.wait(30)
    assert service.get_job(root, first.job_id).status == "running"  # 아직 정산 전
    second = service.start_sync_checks(root, (source_id,), force=True)
    assert second is not None and second.job_id != first.job_id and not second.reattached
    release.set()
    assert wait(service, root, first.job_id).status == "succeeded"
    assert wait(service, root, second.job_id).status == "succeeded"
    assert calls == [source_id, source_id]


def test_a_stale_running_row_never_wedges_the_check(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """정산을 못 한 채 running으로 남은 줄(죽은 스레드)도, 비우지 못한 칸도 확인을 막지 못한다."""
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace

    service, root, source_id = analyzed
    dead = service.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
    assert dead is not None
    JobStore(RawWorkspace.open(root)).update(dead.job_id, status="running")
    service._sync_running[root.expanduser().resolve()] = dead.job_id
    calls = counting(monkeypatch)
    for _ in range(2):
        handle = service.start_sync_checks(root, (source_id,), force=True)
        assert handle is not None and handle.job_id != dead.job_id and not handle.reattached
        assert wait(service, root, handle.job_id).status == "succeeded"
    assert calls == [source_id, source_id]


FAILED_LABEL = "자막 시각 확인을 걸지 못함 (로그 참고)"


def test_a_failed_hook_shows_a_grey_badge_and_recheck_retries(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """고리가 실패하면 콘솔만이 아니라 화면에도 회색 표지가 선다 — 「다시 재기」가 다시 건다."""
    service, root, source_id = analyzed
    real = sync_report.pending_sources

    def locked(*_: Any, **__: Any) -> Any:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(sync_report, "pending_sources", locked)
    service._queue_sync_checks(root, (source_id,))
    view = service.sync_check_view(root, source_id)
    assert view is not None and (view["label"], view["tone"]) == (FAILED_LABEL, "grey")
    assert "OperationalError" in view["reasons"][0]
    monkeypatch.setattr(sync_report, "pending_sources", real)
    handle = service.start_sync_checks(root, (source_id,), force=True)  # 「다시 재기」의 길
    assert handle is not None
    wait_sync(service, root)
    view = service.sync_check_view(root, source_id)
    assert view is not None and view["label"] != FAILED_LABEL


def test_an_upkeep_failure_is_shown_once_and_retried_by_recheck(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """앱 열기 고리가 걸지 못하면 회색 표지 — 1초 틱마다 경고를 되풀이하지 않는다."""
    _service, root, source_id = analyzed
    shutil.rmtree(root / sync_report.SYNC_CACHE / "reports")
    service = raw.RawWorkspaceService()
    real = service.start_sync_checks

    def broken(*_: Any, **__: Any) -> Any:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(service, "start_sync_checks", broken)
    warnings: list[str] = []
    monkeypatch.setattr(raw.log, "warn", warnings.append)
    for _ in range(3):
        assert service.start_sync_upkeep(root) is None
    view = service.sync_check_view(root, source_id)
    assert view is not None and (view["label"], view["tone"]) == (FAILED_LABEL, "grey")
    assert len([line for line in warnings if "걸지 못했다" in line]) == 1
    monkeypatch.setattr(service, "start_sync_checks", real)
    assert service.start_sync_checks(root, (source_id,), force=True) is not None
    wait_sync(service, root)
    view = service.sync_check_view(root, source_id)
    assert view is not None and view["label"] != FAILED_LABEL
