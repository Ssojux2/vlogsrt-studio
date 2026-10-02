"""A4 진단 CLI(`vlogsrt media analyze`) — 견적·오프라인 재생 분석·계획 연결.

실제 ffmpeg 합성 미디어로 init → import → analyze의 흐름을 돈다. 어떤 플래그도
유료 호출을 켜지 않는다 — 실행의 근원은 언제나 `replay`다(§14.3).
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
    _ingest(tmp_path, workspace)
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
    assert "analyzed" in out  # 원본은 기록으로 완성된다(실패 창과 함께)
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
