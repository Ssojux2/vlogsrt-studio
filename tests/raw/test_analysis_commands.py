"""A4 진단 CLI(`vlogsrt media analyze`) — 견적·오프라인 재생 분석·계획 연결.

실제 ffmpeg 합성 미디어로 init → import → analyze의 흐름을 돈다. 어떤 플래그도
유료 호출을 켜지 않는다 — 실행의 근원은 언제나 `replay`다(§14.3).

W-18(D58)부터 `media init`이 만드는 새 작업공간의 의미 분석 기본은 Claude Sonnet 5.5다. Luna
재생·견적을 보는 시험은 W-18 이전 작업공간처럼 `media settings --analysis-model gpt-6-luna`로
Luna를 저장한 뒤에 돈다(`_as_luna`).
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from vlogsrt.project.raw import commands as media

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _parser() -> argparse.ArgumentParser:
    import vlogsrt.project.commands as project_commands

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    project_commands.add_media_parser(sub)
    return parser


def _as_luna(workspace: Path) -> None:
    """W-18 이전에 만든 작업공간처럼 — 의미 분석 모델로 gpt-6-luna를 저장해 둔다."""
    assert media.media_command(_settings_args(workspace, "--analysis-model", LUNA)) == 0


def _make_mov(path: Path, seconds: str = "5") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size=320x180:rate=25:duration={seconds}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ],
        check=True,
        capture_output=True,
    )


def _ingest(tmp_path: Path, workspace: Path) -> str:
    card = tmp_path / "card" / "DCIM"
    _make_mov(card / "제주.mov")
    args = _parser().parse_args(
        ["media", "import", str(tmp_path / "card"), "--workspace", str(workspace), "--confirm"]
    )
    assert media.media_command(args) == 0
    confirm_args = _parser().parse_args(
        [
            "media",
            "inspect",
            "--workspace",
            str(workspace),
        ]
    )
    assert media.media_command(confirm_args) == 0
    service = media._service()
    service.open_workspace(workspace)
    service.confirm_global_source_order(workspace)  # 전체 순서 확정은 서비스 경로다
    return service.list_source_assets(workspace)[0].content_sha256 or ""


@needs_ffmpeg
def test_analyze_estimate_only_prints_without_running(tmp_path: Path, capsys) -> None:
    """견적만 인쇄 — 작업·리비전이 하나도 생기지 않는다."""
    workspace = tmp_path / "ws"
    init = _parser().parse_args(["media", "init", str(workspace), "--name", "제주"])
    assert media.media_command(init) == 0
    _as_luna(workspace)
    _ingest(tmp_path, workspace)
    capsys.readouterr()
    args = _parser().parse_args(
        ["media", "analyze", "--workspace", str(workspace), "--estimate-only"]
    )
    assert media.media_command(args) == 0
    out = capsys.readouterr().out
    assert "분석 견적" in out and "Luna 단가는 미확인" in out and "Luna 요청 1" in out
    service = media._service()
    service.open_workspace(workspace)
    assert service.analysis_overview(workspace) is None  # 아무 것도 실행하지 않았다


@needs_ffmpeg
def test_analyze_replay_runs_and_plans(tmp_path: Path, capsys) -> None:
    """재생 분석 실행 — 없는 픽스처는 실패로 기록되고 계획 연결까지 끝난다."""
    workspace = tmp_path / "ws"
    init = _parser().parse_args(["media", "init", str(workspace), "--name", "제주"])
    assert media.media_command(init) == 0
    _as_luna(workspace)
    stereo_sha = _ingest(tmp_path, workspace)
    fixtures = tmp_path / "fixtures"
    fixtures.mkdir()
    (fixtures / "asr-fixtures.json").write_text(
        json.dumps({"sources": {stereo_sha: []}}), encoding="utf-8"
    )
    (fixtures / "luna-fixtures.json").write_text(
        json.dumps(
            {
                "windows": {
                    "window_000000": {
                        "window_id": "window_000000",
                        "speech_summary": "",
                        "visual_summary": "풍경",
                        "scene_tags": [],
                        "evidence_ids": ["window_000000"],
                        "suggested_boundary_ids": [],
                        "editing_notes": [],
                        "needs_review": False,
                        "uncertainties": [],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    args = _parser().parse_args(
        [
            "media",
            "analyze",
            "--workspace",
            str(workspace),
            "--fixtures",
            str(fixtures),
            "--plan",
        ]
    )
    assert media.media_command(args) == 0
    out = capsys.readouterr().out
    assert "오프라인 재생 분석 작업" in out
    assert "analyzed" in out  # 원본별 결과 표
    assert "offline_replay" in out  # 실행 증거 표기
    assert "semantic_analysis" in out  # 분할 계획 연결까지 끝났다


@needs_ffmpeg
def test_analyze_reports_failures_honestly(tmp_path: Path, capsys) -> None:
    """픽스처 없는 재생 — 창이 실패로 기록되고 완료로 위장하지 않는다."""
    workspace = tmp_path / "ws"
    init = _parser().parse_args(["media", "init", str(workspace), "--name", "제주"])
    assert media.media_command(init) == 0
    _ingest(tmp_path, workspace)
    args = _parser().parse_args(["media", "analyze", "--workspace", str(workspace)])
    assert media.media_command(args) == 0
    out = capsys.readouterr().out
    assert "failed" in out  # 모든 분석 창의 실패를 완료로 숨기지 않는다
    service = media._service()
    service.open_workspace(workspace)
    summary = service.analysis_overview(workspace)
    assert summary is not None
    document = service.analysis_document(
        workspace, summary.revision.revision_id, summary.results[0].asset_id
    )
    assert document is not None
    assert document["annotations"][0]["status"] == "failed"  # 재생 없음 — 실패 판정
    assert document["execution_mode"] == "offline_replay"


# ── W-14 / D54 — `media settings`: 의미 분석 모델을 고른다(로컬 설정만) ──────────


CLAUDE = "claude-opus-5-5"
SONNET = "claude-sonnet-5-5"
LUNA = "gpt-6-luna"


def _settings_args(workspace: Path, *extra: str) -> argparse.Namespace:
    return _parser().parse_args(["media", "settings", "--workspace", str(workspace), *extra])


def _init(workspace: Path) -> None:
    init = _parser().parse_args(["media", "init", str(workspace), "--name", "제주"])
    assert media.media_command(init) == 0


def test_media_settings_prints_and_switches_the_analysis_model(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "ws"
    _init(workspace)
    service = media._service()
    service.open_workspace(workspace)
    before = service.settings(workspace)
    capsys.readouterr()

    assert media.media_command(_settings_args(workspace)) == 0
    out = " ".join(capsys.readouterr().out.split())  # rich가 좁은 폭에서 줄을 접는다
    # W-18(D58): 새 작업공간은 Claude Sonnet 5.5로 시작한다 — 공식 단가 한 줄이 함께 나온다
    assert before.models.luna_model == SONNET
    assert "Claude Sonnet 5.5" in out and "$2" in out and "$10" in out
    assert "단가 미확인" not in out
    assert service.settings(workspace) == before  # 인쇄만 — 아무 것도 바꾸지 않는다

    assert media.media_command(_settings_args(workspace, "--analysis-model", CLAUDE)) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "Claude Opus 5.5" in out and "$4" in out and "$20" in out
    assert "ANTHROPIC_API_KEY" in out  # 키는 이름만 — 값은 어디에도 없다
    switched = service.settings(workspace)
    assert switched.models.luna_model == CLAUDE
    assert switched.models.timing_model == before.models.timing_model
    assert switched.network_mode == before.network_mode

    assert media.media_command(_settings_args(workspace, "--analysis-model", LUNA)) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert "gpt-6-luna" in out and "단가 미확인" in out  # Luna도 그대로 고를 수 있다
    assert service.settings(workspace).models.luna_model == LUNA

    assert media.media_command(_settings_args(workspace, "--analysis-model", SONNET)) == 0
    assert service.settings(workspace) == before


def test_media_settings_rejects_an_unregistered_model(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        _settings_args(tmp_path / "ws", "--analysis-model", "claude-unknown-9")


def test_media_settings_refuses_a_switch_during_an_active_analysis(
    tmp_path: Path, capsys, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vlogsrt.project.raw import service as raw_service
    from vlogsrt.project.raw.models import ANALYSIS_JOB_KIND, JobRecord

    workspace = tmp_path / "ws"
    _init(workspace)
    running = JobRecord(
        job_id="job_000001",
        kind=ANALYSIS_JOB_KIND,
        input_fingerprint="f" * 64,
        status="running",
        progress_current=0,
        progress_total=1,
        error=None,
        cancellation_requested=False,
        created_at="2026-10-09T00:00:00Z",
        updated_at="2026-10-09T00:00:00Z",
    )
    monkeypatch.setattr(
        raw_service.RawWorkspaceService, "list_jobs", lambda self, root, limit=50: (running,)
    )
    assert media.media_command(_settings_args(workspace, "--analysis-model", CLAUDE)) == 1
    assert "분석" in capsys.readouterr().err
    service = media._service()
    service.open_workspace(workspace)
    # W-18(D58): 새 작업공간의 모델(Sonnet 5.5)이 거절 뒤에도 그대로다
    assert service.settings(workspace).models.luna_model == SONNET


@needs_ffmpeg
def test_the_analyze_estimate_names_the_selected_model(tmp_path: Path, capsys) -> None:
    workspace = tmp_path / "ws"
    _init(workspace)
    _ingest(tmp_path, workspace)
    assert media.media_command(_settings_args(workspace, "--analysis-model", CLAUDE)) == 0
    capsys.readouterr()
    args = _parser().parse_args(
        ["media", "analyze", "--workspace", str(workspace), "--estimate-only"]
    )
    assert media.media_command(args) == 0
    out = " ".join(capsys.readouterr().out.split())  # rich가 좁은 폭에서 줄을 접는다
    assert "Claude Opus 5.5 요청 1" in out
    assert "Luna 단가는 미확인" not in out


@needs_ffmpeg
def test_a_new_workspace_estimate_names_sonnet_without_choosing(tmp_path: Path, capsys) -> None:
    """W-18(D58) — 고르지 않은 새 작업공간의 견적은 Sonnet 5.5의 요청과 공식 단가 상한을 말한다."""
    workspace = tmp_path / "ws"
    _init(workspace)
    _ingest(tmp_path, workspace)
    capsys.readouterr()
    args = _parser().parse_args(
        ["media", "analyze", "--workspace", str(workspace), "--estimate-only"]
    )
    assert media.media_command(args) == 0
    out = " ".join(capsys.readouterr().out.split())  # rich가 좁은 폭에서 줄을 접는다
    assert "Claude Sonnet 5.5 요청 1" in out
    assert "Luna 단가는 미확인" not in out
