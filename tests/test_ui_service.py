"""UI 로직. gradio 없이 돈다 — `ui/service.py`가 gradio를 import하지 않기 때문이다."""

from __future__ import annotations

from pathlib import Path

import pytest

from ui import service
from ui.service import UiError
from vlogsrt.config import Config

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
VIDEO = "golden/demo.mp4"


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


# ─────────────────────────────────────────────────────────────
#  경로 해석 — UI를 만든 이유의 절반이 여기 있다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "/tmp/a b/vlog.mp4",
        "  /tmp/a b/vlog.mp4  ",
        "'/tmp/a b/vlog.mp4'",
        '"/tmp/a b/vlog.mp4"',
        "/tmp/a\\ b/vlog.mp4",
        "file:///tmp/a%20b/vlog.mp4",
    ],
)
def test_resolve_path_accepts_what_people_actually_paste(raw: str) -> None:
    assert service.resolve_path(raw) == Path("/tmp/a b/vlog.mp4")


def test_resolve_path_expands_home() -> None:
    assert service.resolve_path("~/Movies/a.mp4") == Path.home() / "Movies/a.mp4"


def test_resolve_path_rejects_empty() -> None:
    with pytest.raises(UiError, match="경로를 입력"):
        service.resolve_path("   ")


def test_recorded_stems_lists_fixtures() -> None:
    assert "demo" in service.recorded_stems()


# ─────────────────────────────────────────────────────────────
#  확인
# ─────────────────────────────────────────────────────────────


def test_inspect_uses_meta_without_the_video_file() -> None:
    inspection = service.inspect_source(VIDEO, Config())
    assert inspection.recorded is True
    assert inspection.exists is False
    assert inspection.duration == 24.0
    assert "무료" in service.describe(inspection)


def test_inspect_unknown_file_is_explained() -> None:
    with pytest.raises(UiError, match="파일이 없습니다"):
        service.inspect_source("/tmp/definitely-not-here.mp4", Config())


def test_inspect_rejects_non_media(tmp_path: Path) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("x", encoding="utf-8")
    with pytest.raises(UiError, match="영상/오디오 파일이 아닌"):
        service.inspect_source(str(document), Config())


# ─────────────────────────────────────────────────────────────
#  설정 상자
# ─────────────────────────────────────────────────────────────


def test_keywords_box_takes_commas_or_newlines() -> None:
    mixed = service.parse_keywords_box("블루펭귄, 망원동\n연남동")
    assert mixed == ["블루펭귄", "망원동", "연남동"]
    assert service.parse_keywords_box("가\n가\n  \n# 주석") == ["가"]
    assert service.parse_keywords_box("") == []


def test_build_config_applies_sliders_over_preset() -> None:
    cfg = service.build_config(
        "shorts", max_chars=18, max_lines=2, max_cps=11.0, keywords_text="가"
    )
    assert cfg.max_chars_per_line == 18
    assert cfg.max_cps == 11.0
    assert cfg.keywords == ("가",)


# ─────────────────────────────────────────────────────────────
#  실행
# ─────────────────────────────────────────────────────────────


def test_generate_produces_every_output(tmp_path: Path) -> None:
    outcome = service.generate(VIDEO, Config(), str(tmp_path))
    assert outcome.srt_path.is_file()
    assert outcome.review_path.is_file()
    assert outcome.report_path.is_file()
    assert outcome.trace_path.is_file()
    assert "망원동" in outcome.srt_text
    assert "[?" in outcome.review_text
    assert "큐 7개" in outcome.summary
    assert outcome.review_rows  # 고아 큐가 우선 확인에 올라온다
    assert outcome.srt_path.parent.name == "demo"


def test_generate_refuses_unrecorded_video(tmp_path: Path) -> None:
    video = tmp_path / "brandnew.mp4"
    video.write_bytes(b"\x00")
    with pytest.raises(UiError, match="아직 녹화되지 않았"):
        service.generate(str(video), Config(), str(tmp_path))


def test_paid_path_needs_consent(tmp_path: Path) -> None:
    """동의 없이는 API 근처에도 가지 않는다. 이게 이 UI의 유일한 안전장치다."""
    with pytest.raises(UiError, match="동의"):
        service.record_and_generate(VIDEO, Config(), str(tmp_path), agreed=False)


def test_paid_path_needs_the_actual_file(tmp_path: Path) -> None:
    with pytest.raises(UiError, match="영상 파일이 있어야"):
        service.record_and_generate(VIDEO, Config(), str(tmp_path), agreed=True)


def test_paid_path_respects_the_cost_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_COST_GUARD_SECONDS", "10")
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00")
    with pytest.raises(UiError, match="안전장치"):
        service.record_and_generate(str(video), Config(), str(tmp_path), agreed=True)


def test_retune_reuses_the_trace_without_fixtures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = service.generate(VIDEO, Config(), str(tmp_path))
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "gone"))
    narrow = service.build_config("vlog", max_chars=12, max_lines=2, max_cps=12.0, keywords_text="")
    again = service.retune(str(first.trace_path), narrow, str(tmp_path))
    assert again.srt_path.is_file()
    assert max(len(line) for line in again.srt_text.splitlines() if not line[:1].isdigit()) <= 14


def test_retune_without_a_run_is_explained(tmp_path: Path) -> None:
    with pytest.raises(UiError, match="먼저 자막을"):
        service.retune(str(tmp_path / "nope.json"), Config(), str(tmp_path))


def test_mode_banner_reflects_offline_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    assert "오프라인" in service.mode_banner()
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    assert "온라인" in service.mode_banner()


def test_service_never_imports_gradio() -> None:
    """UI 로직은 gradio 없이도 돌아야 한다. 화면단만 gradio를 안다."""
    source = (Path(__file__).parent.parent / "ui" / "service.py").read_text(encoding="utf-8")
    assert "import gradio" not in source
