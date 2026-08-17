"""UI 로직. gradio 없이 돈다 — `ui/service.py`가 gradio를 import하지 않기 때문이다."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ui import service
from ui.service import UiError

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
VIDEO = "golden/demo.mp4"


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))
    # 결과물이 저장소 out/ 을 건드리지 않게 한다.
    monkeypatch.setattr(service.env, "project_root", lambda: tmp_path)


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


# ─────────────────────────────────────────────────────────────
#  실행
# ─────────────────────────────────────────────────────────────


def test_recorded_video_runs_without_touching_the_api(tmp_path: Path) -> None:
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00")
    report = service.make_subtitles(str(video))
    assert report.ok
    assert "망원동" in report.srt_text
    assert "기록된 응답 재사용" in report.log
    assert "결과" in report.log
    assert len(report.files) == 3


def test_final_srt_lands_next_to_the_video(tmp_path: Path) -> None:
    """최종 SRT는 원본 영상과 같은 폴더에 둔다."""
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00")
    report = service.make_subtitles(str(video))
    beside = video.parent / "demo.srt"
    assert beside.is_file()
    assert report.files[0] == str(beside)
    assert f"저장   {beside}" in report.log
    # 나머지 산출물은 작업 폴더에 남는다
    assert (tmp_path / "out" / "demo" / "trace.json").is_file()


def test_uploaded_copy_cannot_know_the_original_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """파일 선택창으로 올린 파일은 원본 폴더를 알 수 없다. 그 사실을 로그로 알린다."""
    uploaded = tmp_path / "gradio" / "abc123" / "demo.mp4"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_bytes(b"\x00")
    monkeypatch.setattr(service.tempfile, "gettempdir", lambda: str(tmp_path))
    assert service.is_uploaded_copy(uploaded) is True

    report = service.make_subtitles(str(uploaded))
    assert report.ok
    assert "원본 폴더를 알 수 없습니다" in report.log
    assert not (uploaded.parent / "demo.srt").is_file()


def test_a_real_path_is_not_mistaken_for_an_upload(tmp_path: Path) -> None:
    assert service.is_uploaded_copy(tmp_path / "Movies" / "vlog.mp4") is False


def test_upload_detection_survives_the_macos_temp_symlink() -> None:
    """gettempdir()는 /var/folders/..., 실제 경로는 /private/var/folders/... 다.

    양쪽을 resolve하지 않으면 업로드본을 못 알아보고 임시 폴더에 SRT를 흘린다.
    """
    import tempfile

    real_temp = Path(tempfile.gettempdir()).resolve()
    uploaded = real_temp / "gradio" / "deadbeef" / "vlog.mp4"
    assert service.is_uploaded_copy(uploaded) is True


def test_log_carries_pipeline_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    """파이프라인이 stderr로 찍는 진행 상황이 화면 로그에 들어와야 한다."""
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "INFO")  # conftest는 테스트 소음을 줄이려 ERROR로 둔다
    report = service.make_subtitles(VIDEO)
    assert "진행" in report.log
    assert "골격" in report.log


def test_missing_file_is_reported_in_the_log_not_raised(tmp_path: Path) -> None:
    report = service.make_subtitles(str(tmp_path / "nope.mp4"))
    assert report.ok is False
    assert "파일이 없습니다" in report.log
    assert report.srt_text == ""


def test_blank_path_is_reported_in_the_log() -> None:
    report = service.make_subtitles("   ")
    assert report.ok is False
    assert "경로를 입력" in report.log


def test_non_media_file_is_rejected(tmp_path: Path) -> None:
    document = tmp_path / "notes.txt"
    document.write_text("x", encoding="utf-8")
    report = service.make_subtitles(str(document))
    assert report.ok is False
    assert "영상 파일이 아닙니다" in report.log


def test_cost_guard_stops_long_unrecorded_videos(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """안전장치를 넘는 새 영상은 API로 넘어가기 전에 멈춘다."""
    monkeypatch.setenv("VLOGSRT_COST_GUARD_SECONDS", "10")
    monkeypatch.setattr(service.preprocess, "probe_duration", lambda path: 600.0)
    video = tmp_path / "long.mp4"
    video.write_bytes(b"\x00")
    report = service.make_subtitles(str(video))
    assert report.ok is False
    assert "안전장치" in report.log
    # 그대로 붙여넣을 수 있는 값을 알려준다
    assert "VLOGSRT_COST_GUARD_SECONDS=3600" in report.log


def test_guard_suggestion_covers_the_video(monkeypatch: pytest.MonkeyPatch) -> None:
    """77분짜리를 돌리려면 2시간(7200)을 제안해야 한다."""
    assert service._suggest_guard(77 * 60) == 7200
    assert service._suggest_guard(30 * 60) == 3600
    assert service._suggest_guard(125 * 60) == 10800


def test_unrecorded_video_reports_the_estimate_before_calling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(service.preprocess, "probe_duration", lambda path: 300.0)

    def refuse(*args: object, **kwargs: object) -> None:
        raise RuntimeError("여기까지 왔으면 예상 비용이 로그에 있어야 한다")

    monkeypatch.setattr(service.pipeline, "record_fixtures", refuse)
    video = tmp_path / "fresh.mp4"
    video.write_bytes(b"\x00")
    report = service.make_subtitles(str(video))
    assert report.ok is False
    assert "OpenAI 호출" in report.log
    assert "$0.06" in report.log


# ─────────────────────────────────────────────────────────────
#  파일 대화상자 — 업로드가 아니라 진짜 경로를 받는다
# ─────────────────────────────────────────────────────────────


class _FakeRun:
    """`osascript` 호출을 흉내낸다. 테스트가 실제 대화상자를 띄우면 안 된다."""

    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_dialog_returns_the_real_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "darwin")
    captured: dict[str, list[str]] = {}

    def fake(command: list[str], **kwargs: object) -> _FakeRun:
        captured["command"] = command
        return _FakeRun(0, stdout="/Users/me/Movies/vlog01.mp4\n")

    monkeypatch.setattr(service.subprocess, "run", fake)
    assert service.pick_file_dialog() == "/Users/me/Movies/vlog01.mp4"
    assert captured["command"][0] == "osascript"
    assert "choose file" in captured["command"][2]
    # 확장자 필터가 설정에서 온다
    assert '"mp4"' in captured["command"][2]


def test_dialog_cancel_returns_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda command, **kwargs: _FakeRun(1, stderr="execution error: User canceled. (-128)"),
    )
    assert service.pick_file_dialog() is None


def test_dialog_reports_other_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(
        service.subprocess,
        "run",
        lambda command, **kwargs: _FakeRun(1, stderr="execution error: 뭔가 잘못됨 (-1700)"),
    )
    with pytest.raises(UiError, match="파일 대화상자 오류"):
        service.pick_file_dialog()


def test_generated_applescript_actually_compiles() -> None:
    """만들어낸 스크립트가 osascript에 먹히는지 실제로 컴파일해 본다.

    플레이스홀더가 중괄호를 먹어 `of type "mp4", ...` 가 된 적이 있다.
    문자열만 훑어서는 안 잡히고, 컴파일해야 잡힌다.
    """
    if sys.platform != "darwin":
        pytest.skip("osacompile은 macOS 전용")
    types = ", ".join(f'"{suffix.lstrip(".")}"' for suffix in service.VIDEO_SUFFIXES)
    script = service._CHOOSE_FILE.replace("__TYPES__", types)
    assert "of type {" in script
    compiled = subprocess.run(
        ["osacompile", "-o", "/dev/null", "-"],
        input=script,
        capture_output=True,
        text=True,
        check=False,
    )
    assert compiled.returncode == 0, compiled.stderr


def test_dialog_is_macos_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service.sys, "platform", "linux")
    with pytest.raises(UiError, match="macOS에서만"):
        service.pick_file_dialog()


def test_dialog_does_not_hang_forever(monkeypatch: pytest.MonkeyPatch) -> None:
    """대화상자를 열어둔 채 두면 gradio 워커가 묶인다."""
    monkeypatch.setattr(service.sys, "platform", "darwin")

    def stall(command: list[str], **kwargs: object) -> _FakeRun:
        raise service.subprocess.TimeoutExpired(command, 1.0)

    monkeypatch.setattr(service.subprocess, "run", stall)
    with pytest.raises(UiError, match="응답하지 않아"):
        service.pick_file_dialog()


# ─────────────────────────────────────────────────────────────
#  실행 환경 — `make ui`가 조용히 죽던 자리
# ─────────────────────────────────────────────────────────────


def test_pick_port_returns_the_preferred_one_when_free(local_socket: None) -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        free = probe.getsockname()[1]
    assert service.pick_port(free) == free


def test_pick_port_steps_aside_when_busy(local_socket: None) -> None:
    """앞서 띄운 UI가 남아 있어도 죽으라고 하지 않는다."""
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        assert service.pick_port(busy) > busy


def test_busy_port_notice_warns_about_the_stale_server() -> None:
    """포트가 밀리면 예전 화면에 붙는 함정을 반드시 알려야 한다."""
    notice = "\n".join(service.busy_port_notice(7860, 7861))
    assert "7860" in notice and "7861" in notice
    assert "pkill" in notice


def test_pick_port_gives_up_loudly(local_socket: None) -> None:
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen(1)
        busy = taken.getsockname()[1]
        with pytest.raises(UiError, match="빈 포트가 없습니다"):
            service.pick_port(busy, attempts=1)


# ─────────────────────────────────────────────────────────────
#  상태 표시
# ─────────────────────────────────────────────────────────────


def test_status_line_never_shows_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-proj-THIS-MUST-NEVER-APPEAR"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assert secret not in service.status_line()
    monkeypatch.setenv("OPENAI_API_KEY", "")
    assert "키 없음" in service.status_line()


def test_service_never_imports_gradio() -> None:
    """UI 로직은 gradio 없이도 돌아야 한다. 화면단만 gradio를 안다."""
    source = (Path(__file__).parent.parent / "ui" / "service.py").read_text(encoding="utf-8")
    assert "import gradio" not in source
