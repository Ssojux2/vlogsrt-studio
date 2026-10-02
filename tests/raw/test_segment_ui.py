"""화면 2·3의 A3 묶음 — 결정적 계획·추출·갱신 보기가 서명 아래에서만 움직인다.

가벼운 경로(서명·빈 상태·구간 해석)는 ffmpeg 없이, 실제 흐름 한 번은 진짜
합성 미디어로: 조사 → 확정 → 전체 순서 → 계획 → 추출 → 새로고침 → 조각 재생
목록·package 경로까지 화면 처리기로 지나간다(§12 화면 2·3, §14.1).
"""

from __future__ import annotations

import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import workspace_service  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


@pytest.fixture
def server(tmp_path: Path) -> workspace_service.RawWorkspaceUi:
    return workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")


def _opened(server: workspace_service.RawWorkspaceUi, tmp_path: Path) -> tuple[str, Path]:
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    return view[1], Path(view[0].split("`")[1])


# ── 경로 검사 — 서명 없이·옛 서명으로는 아무 것도 움직이지 않는다 ─────────────


def test_segment_views_require_current_selection(server, tmp_path: Path) -> None:
    signature, _ = _opened(server, tmp_path)
    for call in (
        lambda: server.segment_plan_view(None, ""),
        lambda: server.segment_plan_view("stale-signature", ""),
        lambda: server.segment_extract_view(None, None),
        lambda: server.segment_refresh_view("stale-signature"),
        lambda: server.segment_retry_view(None, "job_000001"),
        lambda: server.package_refresh_view(None),
    ):
        with pytest.raises(gr.Error, match="작업공간이 열려 있지 않다|바뀌었다"):
            call()


def test_plan_view_reports_missing_order_honestly(server, tmp_path: Path) -> None:
    """확정 순서 없이는 계획이 되지 않는다 — 조용한 빈 성공이 아니다(§6.2)."""
    signature, _ = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="확정된 전체 원본 순서가 없다"):
        server.segment_plan_view(signature, "")


def test_empty_states_do_not_pretend(server) -> None:
    """작업공간 없는 빈 패널은 '아직 없다'고 말한다 — 완성으로 위장하지 않는다."""
    assert "작업공간을 열면" in server._segment_state_markdown(None)
    assert "추출이 끝나면" in server._package_state_markdown(None)
    assert server.clip_choices(None) == []
    assert server.resumable_extract_choices(None) == []


def test_interval_parsing_rejects_malformed_lines(server, tmp_path: Path) -> None:
    signature, _ = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="모양이 아니다"):
        server.segment_plan_view(signature, "source_abc 1")
    with pytest.raises(gr.Error, match="유리수가 아니다"):
        server.segment_plan_view(signature, "source_abc 0 later")
    parsed = workspace_service._parse_intervals("source_abc 0 2\nsource_abc 2 5.5\n# 메모")
    assert parsed is not None
    assert [span.end for span in parsed["source_abc"]] == [Fraction(2), Fraction("5.5")]
    assert workspace_service._parse_intervals("  ") is None


# ── 실제 흐름 — 합성 미디어로 화면 처리기 전체를 한 번 ────────────────────────


@needs_ffmpeg
def test_browser_flow_plan_extract_refresh_and_playback(
    server: workspace_service.RawWorkspaceUi, tmp_path: Path
) -> None:
    """원본 하나가 화면 단추만으로 조각 폴더가 된다 — A3의 브라우저 경로."""
    card = tmp_path / "card"
    card.mkdir()
    clip_source = card / "하루.MOV"
    completed = subprocess.run(
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
            "testsrc2=size=320x180:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-force_key_frames",
            "expr:gte(t,n_forced*3)",
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
            "-shortest",
            str(clip_source),
        ],  # fmt: skip
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-300:]

    signature, root = _opened(server, tmp_path)
    server.ingest_scan_view(signature, str(card))
    server.ingest_confirm_view(signature)
    server.ingest_copy_view(signature)  # 화면 단추 그대로 — 확정된 순서로 복사·검증
    handle = server.raw.list_jobs(root)[0]
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        time.sleep(0.02)
    assert server.raw.get_job(root, handle.job_id).status == "succeeded"
    server.ingest_global_confirm_view(signature, "")

    markdown = server.segment_plan_view(signature, "")
    assert "분할 계획" in markdown
    assert "리비전" in markdown

    extract_md, _, package_md = server.segment_extract_view(signature, "source_matched")
    assert "제출" in extract_md
    assert "package" in package_md
    job_id = server.raw.list_jobs(root)[0].job_id
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        record = server.raw.get_job(root, job_id)
        if record.status in TERMINAL:
            break
        time.sleep(0.05)
    assert record.status == "succeeded", record.error

    segment_md, _, refreshed_package, clip_dd = server.segment_refresh_view(signature)
    assert "complete" in refreshed_package
    assert "게시 1/1" in refreshed_package  # 4초 원본 — 기본 타일이면 조각 하나
    assert clip_dd is not None
    choices = server.clip_choices(root)
    assert len(choices) == 1
    file_id = choices[0][1]
    resolved = server.raw.resolve_media(root, file_id)
    assert resolved.kind == "artifact"
    assert resolved.path.is_file()
    assert server._latest_package_path(root).endswith("clips/package_000001")
