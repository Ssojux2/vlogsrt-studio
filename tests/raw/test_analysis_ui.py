"""화면 2의 A4 묶음 — 분석 견적·오프라인 재생 분석·결과 읽기가 서명 아래에서만.

가벼운 경로(서명·빈 상태·견적 거절)는 ffmpeg 없이, 실제 흐름 한 번은 진짜
합성 미디어로: 조사 → 확정 → 전체 순서 → 견적 → **오프라인 재생 분석** →
새로고침 → 요약·태그 읽기 → 분석 결과로 분할 계획까지 화면 처리기로 지나간다
(§12 화면 2, §14.1·§14.3 — 브라우저는 실제 호출을 만들 수 없다).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
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


def test_analysis_views_require_current_selection(server, tmp_path: Path) -> None:
    signature, _ = _opened(server, tmp_path)
    for call in (
        lambda: server.analysis_estimate_view(None, None),
        lambda: server.analysis_estimate_view("stale-signature", None),
        lambda: server.analysis_start_view("stale-signature", None),
        lambda: server.analysis_refresh_view(None),
        lambda: server.analysis_retry_view(None, "job_000001"),
        lambda: server.analysis_plan_view("stale-signature"),
    ):
        with pytest.raises(gr.Error):
            call()


def test_analysis_views_before_order_confirmed_explain_honestly(server, tmp_path: Path) -> None:
    """확정 순서가 없으면 안내로 알린다 — 빈 성공으로 위장하지 않는다."""
    signature, root = _opened(server, tmp_path)
    with pytest.raises(gr.Error, match="전체 원본 순서가 없다"):
        server.analysis_estimate_view(signature, None)
    with pytest.raises(gr.Error, match="전체 원본 순서가 없다"):
        server.analysis_start_view(signature, None)
    markdown = server._analysis_state_markdown(root)
    assert "오프라인 재생" in markdown  # 모드 표기가 처음부터 정직하다
    empty = server._analysis_state_markdown(None)
    assert "오프라인 재생" in empty and "승인" in empty


def test_analysis_settings_summary_shows_frozen_defaults(server, tmp_path: Path) -> None:
    """동결 기본값(15초·2초·8장·768)과 모델이 설정 요약에 보인다(§7.4)."""
    signature, _ = _opened(server, tmp_path)
    markdown = server._analysis_settings_markdown(None)
    assert "15초" in markdown and "2초" in markdown and "8장" in markdown and "768" in markdown


# ── 실제 흐름 — 견적 → 오프라인 재생 분석 → 읽기 → 분할 계획 ─────────────────


def _make_media(path: Path, seconds: str = "6") -> None:
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
            "sine=frequency=440:sample_rate=48000:duration=4",
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


@needs_ffmpeg
def test_browser_replay_analysis_flow_to_segment_plan(server, tmp_path: Path) -> None:
    """견적 → 재생 분석 → 상태·요약 읽기 → 분석 결과로 분할 계획까지(§12 화면 2)."""
    signature, root = _opened(server, tmp_path)
    card = tmp_path / "card" / "DCIM"
    _make_media(card / "제주.mov")
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        time.sleep(0.02)
    assert server.raw.get_job(root, handle.job_id).status == "succeeded"
    server.raw.confirm_global_source_order(root)
    asset = server.raw.list_source_assets(root)[0]

    estimate = server.analysis_estimate_view(signature, None)
    assert "분석 견적" in estimate and "Luna 요청" in estimate
    assert "금액 추정을 만들지 않는다" in estimate  # 미확인 단가 — 0원 계산 금지

    # 명시적 합성 픽스처 — 없는 창·청크는 실패로 기록된다(지어내지 않는다).
    fixtures = root / "analysis" / "replay"
    fixtures.mkdir(parents=True)
    (fixtures / "asr-fixtures.json").write_text(
        json.dumps({"sources": {asset.content_sha256 or "": []}}), encoding="utf-8"
    )
    (fixtures / "luna-fixtures.json").write_text(
        json.dumps(
            {
                "windows": {
                    "window_000000": {
                        "window_id": "window_000000",
                        "speech_summary": "",
                        "visual_summary": "실내 풍경이 이어진다",
                        "scene_tags": ["indoor"],
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
    message, _ = server.analysis_start_view(signature, None)
    assert "오프라인 재생" in message and "지어내지 않는다" in message
    job_id = message.split("`")[1]
    deadline = time.monotonic() + 300
    while time.monotonic() < deadline:
        if server.raw.get_job(root, job_id).status in TERMINAL:
            break
        time.sleep(0.05)
    assert server.raw.get_job(root, job_id).status == "succeeded"

    markdown, _jobs, sources = server.analysis_refresh_view(signature)
    assert "의미 분석" in markdown
    assert "오프라인 재생" in markdown  # 실행 모드 표기
    assert "발화" in markdown  # 원본별 상태 표
    assert sources.choices  # 원본 선택 목록이 찬다
    assert "실내 풍경이 이어진다" in markdown  # 요약 읽기(accepted 판정)

    segment_md = server.analysis_plan_view(signature)
    assert "semantic_analysis" in segment_md  # 분석 근거의 분할 계획이 패널에 떴다

    # 재시도 목록 — 성공한 작업은 재시도 대상이 아니다.
    assert server.resumable_analysis_choices(root) == []
