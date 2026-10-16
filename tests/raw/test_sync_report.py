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
import types
from dataclasses import fields, replace
from fractions import Fraction
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
REAL_SESSION_FACTORY = sync_report.default_session_factory


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


REPLAY_ANALYSIS_MODEL = "gpt-6-luna"
"""재생 의미 판정 픽스처(`luna-fixtures.json`, `output_text` 모양)를 읽는 분석 모델.

18a(D58)부터 새 작업공간의 기본은 Claude Sonnet 5.5인데 이 픽스처는 Claude 응답 모양이 아니라
창마다 거부돼 원본 행이 조용히 `failed`로 끝났다 — 「분석했다」는 재료가 분석되지 않았다(D59).
"""


@pytest.fixture
def analyzed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """톤 덩어리 원본 하나를 재생 분석한 작업공간 — 분석 뒤 확인 작업까지 끝난 상태."""
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    service, root, asset_id, handle = start_tone_analysis(tmp_path, model=REPLAY_ANALYSIS_MODEL)
    assert wait(service, root, handle.job_id).status == "succeeded"
    assert source_statuses(service, root) == ["analyzed"]  # 이름 그대로 분석이 끝났다
    wait_sync(service, root)
    return service, root, asset_id


def source_statuses(service: raw.RawWorkspaceService, root: Path) -> list[str]:
    """가장 최근 분석 리비전의 원본 행 상태들."""
    from vlogsrt.project.raw.analysis_store import AnalysisStore

    store = AnalysisStore(service._workspace(root))
    latest = store.list_revisions()[-1]
    return [row.status for row in store.list_source_results(latest.revision_id)]


def start_tone_analysis(
    tmp_path: Path, *, declared: bool = True, model: str | None = None
) -> tuple[Any, Path, str, Any]:
    """톤 덩어리 원본 하나를 가져와 재생 분석을 건다(유료 호출 0) — 작업 손잡이를 돌려준다.

    `declared=False`면 재생 ASR 선언을 비워 둔다 — 모든 청크가 전사 실패로 기록된다.
    `model`이 있으면 작업공간을 만든 직후 의미 분석 모델을 그것으로 저장한다(재생 의미 판정은
    `REPLAY_ANALYSIS_MODEL`의 응답 모양이다).
    """
    card = tmp_path / "card"
    make_media(card / "tone.mov")
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "시각")
    if model is not None:
        workflow = service.settings(root)
        models = replace(workflow.models, luna_model=model)
        service.update_settings(root, replace(workflow, models=models))
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
    sources = {asset.content_sha256: spoken} if declared else {}
    (replay / "asr-fixtures.json").write_text(json.dumps({"sources": sources}), encoding="utf-8")
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
    service, root, source_id, handle = start_tone_analysis(tmp_path, model=REPLAY_ANALYSIS_MODEL)
    assert wait(service, root, handle.job_id).status == "succeeded"
    assert source_statuses(service, root) == ["analyzed"]
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


def test_a_failed_check_job_greys_only_the_sources_it_left_unmeasured(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """작업 본문이 원본별 측정 밖에서 터지면(보고서 쓰기 등) 잴 차례였던 원본은 회색 표지가 된다 —
    콘솔과 작업 기록에만 남으면 화면은 아무 일 없었던 것처럼 보인다."""
    service, root, source_id = analyzed

    def broken(*_: Any, **__: Any) -> Any:
        raise OSError("디스크가 가득 찼다")

    shutil.rmtree(root / sync_report.SYNC_CACHE / "reports")
    real_write = sync_report.write_report
    monkeypatch.setattr(sync_report, "write_report", broken)
    warnings: list[str] = []
    monkeypatch.setattr(raw.log, "warn", warnings.append)
    service.start_sync_checks(root, (source_id,), force=True)
    latest = service.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
    assert latest is not None and wait(service, root, latest.job_id).status == "failed"
    view = service.sync_check_view(root, source_id)
    assert view is not None and (view["label"], view["tone"]) == (FAILED_LABEL, "grey")
    assert any("디스크가 가득 찼다" in reason for reason in view["reasons"])
    assert [line for line in warnings if "디스크가 가득 찼다" in line]
    monkeypatch.setattr(sync_report, "write_report", real_write)
    assert service.start_sync_checks(root, (source_id,), force=True) is not None  # 「다시 재기」
    wait_sync(service, root)
    view = service.sync_check_view(root, source_id)
    assert view is not None and view["label"] != FAILED_LABEL


# ── 앱이 끝날 때 — 새 VAD 창·세션을 거절하고, 거절은 보고서로 남기지 않는다 ──────────
# (종료 abort의 원인은 onnxruntime 원격 측정 업로더였다 — tests/project/test_vad_runtime.py.
#  이 문은 그 위의 방어선일 뿐이다, D55.)


def test_vad_gate_refuses_new_windows_at_exit_and_waits_for_running_ones() -> None:
    gate = sync_report._VadGate()
    started, release = threading.Event(), threading.Event()

    def hold() -> None:
        with gate.held():
            started.set()
            release.wait(5)

    worker = threading.Thread(target=hold)
    worker.start()
    assert started.wait(5)
    closer = threading.Thread(target=gate.close, args=(5.0,))
    closer.start()
    time.sleep(0.05)
    assert closer.is_alive()  # 돌던 창이 끝나기를 기다린다
    release.set()
    closer.join(5)
    worker.join(5)
    assert not closer.is_alive()
    with pytest.raises(sync_report.SyncShutdown, match="끝나는 중"), gate.held():
        pass


def _close_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    closed = sync_report._VadGate()
    closed.close(0.0)
    monkeypatch.setattr(sync_report, "_VAD_GATE", closed)


def _reports_under(root: Path) -> list[Path]:
    return sorted((root / sync_report.SYNC_CACHE).rglob("reports/**/*.json"))


def test_a_window_refused_at_exit_leaves_no_report_and_the_next_run_remeasures(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """종료 때 거절된 측정은 측정 불가가 아니다 — 회색 보고서를 남기면 다음 실행이 '지금
    것'으로 믿고 다시 재지 않는다. 아무것도 쓰지 않고, 다음 앱 열기가 다시 잰다."""
    service, root, source_id = analyzed
    _close_gate(monkeypatch)
    shutil.rmtree(root / sync_report.SYNC_CACHE)
    service.start_sync_checks(root, (source_id,), force=True)
    wait_sync(service, root)
    assert _reports_under(root) == []
    assert service.sync_report(root, source_id) is None
    assert source_id in sync_report.pending_sources(service._workspace(root))
    monkeypatch.setattr(sync_report, "_VAD_GATE", sync_report._VadGate())
    reopened = raw.RawWorkspaceService()  # 다음 앱 실행
    assert reopened.start_sync_upkeep(root) is not None
    wait_sync(reopened, root)
    report = reopened.sync_report(root, source_id)
    assert report is not None and report["verdict"] != sync_check.LEVEL_SKIP


def test_a_session_refused_at_exit_leaves_no_report(
    analyzed: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, source_id = analyzed
    made: list[object] = []
    monkeypatch.setattr(sync_report, "default_session_factory", REAL_SESSION_FACTORY)
    monkeypatch.setattr(sync_report, "_SESSIONS", {})
    monkeypatch.setattr(sync_report, "create_cpu_session", made.append)
    monkeypatch.setattr(sync_report, "bundled_artifact", lambda project: "artifact")
    _close_gate(monkeypatch)
    shutil.rmtree(root / sync_report.SYNC_CACHE)
    service.start_sync_checks(root, (source_id,), force=True)
    wait_sync(service, root)
    assert made == []  # 끝나는 중에는 세션을 만들지 않는다
    assert _reports_under(root) == []
    with pytest.raises(sync_report.SyncShutdown):
        sync_report.default_session_factory(ProjectConfig())()


def test_vad_session_is_made_once_per_process_and_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """작업 스레드가 세션을 만들고 버리지 않는다 — 소멸이 앱 종료와 겹치지 않게 모듈이 쥔다."""
    made: list[object] = []

    def create(_artifact: Any) -> object:
        made.append(object())
        return made[-1]

    monkeypatch.setattr(sync_report, "_SESSIONS", {})
    monkeypatch.setattr(sync_report, "create_cpu_session", create)
    monkeypatch.setattr(sync_report, "bundled_artifact", lambda project: "artifact")
    project = ProjectConfig()
    first = sync_report.default_session_factory(project)()
    second = sync_report.default_session_factory(project)()
    assert first is second and len(made) == 1


# ── D59 — 전사 뒤에 분석이 죽어도 실패 구간이 보인다(0018 꼴) ──────────────────────


def _stage1_body(created_at: str, *, failed: bool, document: str = "asr_outcome.json") -> dict:
    """1단계 기록 한 장 — 0018처럼 16kHz 1359초, 마지막 청크(1260–1359초)가 실패한 모양."""
    rate = 16000
    chunks = [
        {
            "chunk_index": index,
            "sample_start": start * rate,
            "sample_end": end * rate,
            "failure": "connection: 연결이 끊겼다" if failed and index == 7 else "",
        }
        for index, (start, end) in enumerate(
            [(i * 180, (i + 1) * 180) for i in range(7)] + [(1260, 1359)]
        )
    ]
    return {
        "schema": "raw-analysis-asr-outcome/v1" if document == "asr_outcome.json" else "x",
        "source_sha256": "f" * 64,
        "created_at": created_at,
        "video": {"presentation_origin": {"num": 0, "den": 1}},
        "audio": {
            "pcm_sha256": "0" * 64,
            "audio_stream_index": 1,
            "sample_rate": rate,
            "channels": 2,
            "sample_count": rate * 1359,
            "pcm_sample0_file_time": {"num": 0, "den": 1},
        },
        "asr": {"chunks": chunks},
    }


def _stage1_folder(root: Path, revision: str, body: dict, name: str = "asr_outcome.json") -> Path:
    folder = root / "analysis" / revision / "000011_src"
    folder.mkdir(parents=True)
    (folder / name).write_text(json.dumps(body), encoding="utf-8")
    (folder / "analysis.pcm").write_bytes(b"\x00" * 64)  # 0018 폴더처럼 PCM·청크 WAV는 있다
    (folder / "asr-chunk-0007.wav").write_bytes(b"RIFF")
    return folder


def _source_with(stage1: sync_report.Stage1Audio | None) -> sync_report.SyncSource:
    return sync_report.SyncSource(
        source_id="src",
        source_name="DJI_0018_D.mov",
        source_sha256="f" * 64,
        original=None,
        original_note="원본 없음",
        transcript_revision="rev",
        cues=(),
        stage1=stage1,
    )


def test_a_failed_chunk_is_found_in_the_record_written_right_after_transcription(
    tmp_path: Path,
) -> None:
    """0018 꼴 — 실패·중단된 리비전 폴더에 `asr_phase.json`이 없어도 전사 직후 기록이 있다.

    더 오래된 리비전의 온전한 1단계 기록(실패 없음)보다 이 기록이 최근이다 — 지금 자막을 심은
    전사가 이것이다. 보고서는 실패 구간을 「1260–1359초」로 말한다.
    """
    _stage1_folder(
        tmp_path, "anal_old", _stage1_body("2026-10-08T11:02:00+00:00", failed=False),
        name="asr_phase.json",
    )  # fmt: skip
    _stage1_folder(tmp_path, "anal_died", _stage1_body("2026-10-09T08:44:00+00:00", failed=True))
    stage1 = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert stage1 is not None and stage1.folder.parent.name == "anal_died"
    document = sync_report.measure_source(_source_with(stage1), cache_root=tmp_path / "cache")
    assert document["failed_chunks"] == [
        {"start": "1260/1", "end": "1359/1", "reason": "connection: 연결이 끊겼다"}
    ]
    texts = [reason["text"] for reason in document["reasons"]]
    assert any("전사 청크 1개가 실패해 그 구간(1260–1359초)에 자막이 없음" in t for t in texts)
    asset = types.SimpleNamespace(asset_id="src", content_sha256="f" * 64)
    workspace = types.SimpleNamespace(root=tmp_path)
    assert sync_report.caption_gaps(workspace, asset) == document["failed_chunks"]


def test_a_richer_record_in_the_same_folder_wins_over_the_early_one(tmp_path: Path) -> None:
    """끝까지 간 실행은 둘 다 남긴다 — 같은 전사의 더 큰 기록(`asr_phase.json`)을 읽는다."""
    folder = _stage1_folder(
        tmp_path, "anal_done", _stage1_body("2026-10-09T08:44:00+00:00", failed=True)
    )
    whole = _stage1_body("2026-10-09T08:50:00+00:00", failed=True, document="asr_phase.json")
    whole["asr"]["chunks"][7]["clamped_segments"] = [2]
    (folder / "asr_phase.json").write_text(json.dumps(whole), encoding="utf-8")
    stage1 = sync_report.stage1_audio(tmp_path, "src", "f" * 64)
    assert stage1 is not None and stage1.clamped == 1 and len(stage1.failed) == 1


def test_without_any_record_the_report_says_it_does_not_know(tmp_path: Path) -> None:
    """19a 이전에 죽은 실행(실측 0018의 두 폴더)은 PCM·청크 WAV만 있다 — 지어내지 않고 「모름」."""
    folder = tmp_path / "analysis" / "anal_815ac9f11ed4d35e" / "000010_src"
    folder.mkdir(parents=True)
    (folder / "analysis.pcm").write_bytes(b"\x00" * 64)
    (folder / "asr-chunk-0007.wav").write_bytes(b"RIFF")
    assert sync_report.stage1_audio(tmp_path, "src", "f" * 64) is None
    document = sync_report.measure_source(_source_with(None), cache_root=tmp_path / "cache")
    assert document["failed_chunks"] == []
    checks = {check["name"]: check for check in document["checks"]}
    assert checks["전사 실패 구간"]["detail"] == "1단계 기록이 없어 실패 구간을 모름"
    asset = types.SimpleNamespace(asset_id="src", content_sha256="f" * 64)
    assert sync_report.caption_gaps(types.SimpleNamespace(root=tmp_path), asset) is None


def test_an_analysis_that_dies_after_transcription_leaves_the_failed_chunks_on_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """끝까지 — 재생 전사가 실패하고(선언 없음) 장면 단계에서 원본 분석이 죽는다(0018 꼴).

    원본 자막은 이미 심겼고 1단계 기록(`asr_phase.json`·`analysis_transcript.json`)은 없다.
    전사 직후 기록이 남아 확인 보고서가 실패 구간을 말한다.
    """
    from vlogsrt.project.raw import analysis_workflow
    from vlogsrt.project.raw.analysis_store import AnalysisStore

    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)

    def died(*_: Any, **__: Any) -> Any:
        raise RuntimeError("장면 단계가 죽었다(합성)")

    monkeypatch.setattr(analysis_workflow, "_scene_plan", died)
    service, root, asset_id, handle = start_tone_analysis(tmp_path, declared=False)
    wait(service, root, handle.job_id)
    latest = service.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
    if latest is not None:
        wait(service, root, latest.job_id)
    workspace = service._workspace(root)
    store = AnalysisStore(workspace)
    (revision,) = store.list_revisions()
    (row,) = store.list_source_results(revision.revision_id)
    assert row.status == "failed" and "장면 단계가 죽었다" in str(row.error)
    (folder,) = root.glob(f"analysis/*/*_{asset_id}")
    assert (folder / "asr_outcome.json").is_file()
    assert not (folder / "asr_phase.json").exists()
    assert not (folder / "analysis_transcript.json").exists()
    source = sync_report.gather_source(workspace, workspace.source_asset(asset_id))
    assert source is not None and source.cues == ()  # 자막은 심겼다(큐 0개)
    assert source.stage1 is not None and len(source.stage1.failed) == 1
    document = sync_report.measure_source(source, cache_root=tmp_path / "measure")
    assert [Fraction(row["start"]) for row in document["failed_chunks"]] == [0]
    assert "failed_chunks" in {reason["code"] for reason in document["reasons"]}
