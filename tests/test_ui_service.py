"""UI 로직. gradio 없이 돈다 — `ui/service.py`가 gradio를 import하지 않기 때문이다."""

from __future__ import annotations

import socket
import subprocess
import sys
from pathlib import Path

import pytest

from ui import service
from ui.service import UiError, build_config
from vlogsrt import log, pipeline, transcribe
from vlogsrt.config import Config
from vlogsrt.models import Trace

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"


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


SRT = "1\n00:00:01,000 --> 00:00:02,000\n망원동 빵집\n"


def fake_result(outdir: Path) -> pipeline.RunResult:
    outdir.mkdir(parents=True, exist_ok=True)
    srt = outdir / "demo.srt"
    srt.write_text(SRT, encoding="utf-8")
    return pipeline.RunResult(
        trace=Trace(source="x", duration=10.0, spine=(), config_snapshot={}),
        cues=(),
        outputs={"srt": srt},
    )


@pytest.fixture
def stub_pipeline(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """`pipeline.run`을 가로챈다. UI가 무엇을 어떻게 부르는지만 본다."""
    seen: list[dict[str, object]] = []

    def fake_run(video: Path, cfg: object, outdir: Path, **kwargs: object) -> pipeline.RunResult:
        seen.append({"video": video, "cfg": cfg, "outdir": outdir, **kwargs})
        return fake_result(outdir)

    monkeypatch.setattr(service.pipeline, "run", fake_run)
    monkeypatch.setattr(service.preprocess, "probe_duration", lambda path: 300.0)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    return seen


def make_video(folder: Path, name: str = "demo.mp4") -> Path:
    video = folder / name
    video.write_bytes(b"\x00")
    return video


# ─────────────────────────────────────────────────────────────
#  매번 새로 전사한다 — 지난 실행을 재사용하지 않는다
# ─────────────────────────────────────────────────────────────


def test_every_run_transcribes_the_video_fresh(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    video = make_video(tmp_path)
    service.make_subtitles(str(video))
    service.make_subtitles(str(video))

    assert len(stub_pipeline) == 2  # 두 번 다 전사했다
    for call in stub_pipeline:
        assert call["online"] is True
        assert call["artifacts"] is False
        assert call["video"] == video


def test_recorded_fixtures_are_never_consulted(
    tmp_path: Path, stub_pipeline: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """녹화된 응답이 있어도 무시한다. 낡은 기록이 섞이는 경로를 없앴다."""
    fixtures = tmp_path / "api"
    fixtures.mkdir()
    for kind in ("whisper", "gpt", "meta"):
        (fixtures / f"demo.{kind}.json").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(fixtures))

    report = service.make_subtitles(str(make_video(tmp_path)))
    assert report.ok
    assert len(stub_pipeline) == 1
    assert "재사용" not in report.log


def test_nothing_is_left_behind_but_the_srt(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    """작업 폴더는 임시 폴더이고, 끝나면 사라진다."""
    video = make_video(tmp_path)
    report = service.make_subtitles(str(video))

    assert sorted(path.name for path in tmp_path.iterdir()) == ["demo.mp4", "demo.srt"]
    assert not (tmp_path / "out").exists()
    scratch = Path(str(stub_pipeline[0]["outdir"]))
    assert not scratch.exists()  # 임시 폴더가 지워졌다
    assert report.files == (str(tmp_path / "demo.srt"),)


def test_work_and_out_live_in_the_same_scratch(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    service.make_subtitles(str(make_video(tmp_path)))
    call = stub_pipeline[0]
    scratch = Path(str(call["outdir"])).parent
    assert Path(str(call["workdir"])).parent == scratch
    assert scratch.name.startswith("vlogsrt-")


# ─────────────────────────────────────────────────────────────
#  whisper 힌트 — 사람이 실행할 때마다 정한다
# ─────────────────────────────────────────────────────────────


def test_prompt_from_the_screen_reaches_the_config(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    service.make_subtitles(str(make_video(tmp_path)), "망원동, 아페로, 코나")
    cfg = stub_pipeline[0]["cfg"]
    assert isinstance(cfg, Config)
    assert cfg.prompt == "망원동, 아페로, 코나"


def test_no_prompt_means_no_prompt(tmp_path: Path, stub_pipeline: list[dict[str, object]]) -> None:
    """비워두면 빈 문자열이 간다. toml에 남은 값이 몰래 따라붙으면 안 된다."""
    service.make_subtitles(str(make_video(tmp_path)))
    cfg = stub_pipeline[0]["cfg"]
    assert isinstance(cfg, Config)
    assert cfg.prompt == ""


def test_prompt_is_trimmed(tmp_path: Path, stub_pipeline: list[dict[str, object]]) -> None:
    service.make_subtitles(str(make_video(tmp_path)), "  망원동  ")
    cfg = stub_pipeline[0]["cfg"]
    assert isinstance(cfg, Config)
    assert cfg.prompt == "망원동"


def test_prompt_is_echoed_in_the_log(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    """무엇을 넣고 돌렸는지 로그에 남아야 결과를 해석할 수 있다."""
    report = service.make_subtitles(str(make_video(tmp_path)), "망원동, 아페로")
    assert "힌트   망원동, 아페로" in report.log
    blank = service.make_subtitles(str(make_video(tmp_path)))
    assert "힌트" not in blank.log


def test_project_toml_ships_no_prompt() -> None:
    """메타 지시문을 넣었다가 570초를 날렸다(D13). 기본값은 비어 있어야 한다."""
    assert build_config().prompt == ""


# ─────────────────────────────────────────────────────────────
#  결과 배치
# ─────────────────────────────────────────────────────────────


def test_final_srt_lands_next_to_the_video(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    video = make_video(tmp_path)
    report = service.make_subtitles(str(video))
    beside = tmp_path / "demo.srt"
    assert beside.read_text(encoding="utf-8") == SRT
    assert report.srt_text == SRT
    assert f"저장   {beside}" in report.log


def test_srt_name_follows_the_video_name(
    tmp_path: Path, stub_pipeline: list[dict[str, object]]
) -> None:
    video = make_video(tmp_path, "fr_day_1_2_fhd.mov")
    service.make_subtitles(str(video))
    assert (tmp_path / "fr_day_1_2_fhd.srt").is_file()


def test_uploaded_copy_says_where_the_file_went(
    tmp_path: Path, stub_pipeline: list[dict[str, object]], monkeypatch: pytest.MonkeyPatch
) -> None:
    uploaded = tmp_path / "gradio" / "abc123" / "demo.mp4"
    uploaded.parent.mkdir(parents=True)
    uploaded.write_bytes(b"\x00")
    monkeypatch.setattr(service.tempfile, "gettempdir", lambda: str(tmp_path))
    assert service.is_uploaded_copy(uploaded) is True

    report = service.make_subtitles(str(uploaded))
    assert report.ok
    assert "원본 폴더를 알 수 없습니다" in report.log


def test_a_real_path_is_not_mistaken_for_an_upload(tmp_path: Path) -> None:
    assert service.is_uploaded_copy(tmp_path / "Movies" / "vlog.mp4") is False


def test_upload_detection_survives_the_macos_temp_symlink() -> None:
    """gettempdir()는 /var/folders/..., 실제 경로는 /private/var/folders/... 다.

    양쪽을 resolve하지 않으면 업로드본을 못 알아본다.
    """
    import tempfile

    real_temp = Path(tempfile.gettempdir()).resolve()
    uploaded = real_temp / "gradio" / "deadbeef" / "vlog.mp4"
    assert service.is_uploaded_copy(uploaded) is True


# ─────────────────────────────────────────────────────────────
#  진행 상황과 실패
# ─────────────────────────────────────────────────────────────


def test_progress_streams_before_the_slow_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """긴 단계에 들어가기 **전에** 무엇을 하는지 먼저 내보내야 한다.

    끝나고 한 번에 주면 사람은 몇 분 동안 빈 화면을 보고 멈췄다고 판단한다.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(service.preprocess, "probe_duration", lambda path: 300.0)
    seen: list[str] = []

    def slow_run(*args: object, **kwargs: object) -> None:
        seen.append("전사 시작")
        raise RuntimeError("여기서 멈춘다")

    monkeypatch.setattr(service.pipeline, "run", slow_run)
    steps = list(service.stream_subtitles(str(make_video(tmp_path))))

    assert len(steps) >= 3  # 영상 → 길이 확인 → 전사 안내 → (실패)
    before_slow = steps[-2].log
    assert "OpenAI 호출" in before_slow
    assert "몇 분 걸립니다" in before_slow
    # 금액을 박지 않는다. 모델이 하나가 되며 값이 반으로 줄었다(D21).
    # 지켜야 할 계약은 "느린 단계 전에 예상 비용을 보여준다"이지 특정 숫자가 아니다.
    assert f"${transcribe.estimate_cost(300.0, Config()):.3f}" in before_slow
    assert seen == ["전사 시작"]


def test_stream_reports_each_stage(tmp_path: Path, stub_pipeline: list[dict[str, object]]) -> None:
    steps = list(service.stream_subtitles(str(make_video(tmp_path))))
    assert len(steps) >= 3
    assert all(step.ok is False for step in steps[:-1])  # 중간 상태
    assert steps[-1].ok is True
    assert "영상" in steps[0].log


def test_log_carries_pipeline_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_pipeline: list[dict[str, object]]
) -> None:
    """파이프라인이 stderr로 찍는 진행 상황이 화면 로그에 들어와야 한다."""
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "INFO")

    def noisy_run(video: Path, cfg: object, outdir: Path, **kwargs: object) -> object:
        log.info("오디오 추출 완료")
        return fake_result(outdir)

    monkeypatch.setattr(service.pipeline, "run", noisy_run)
    report = service.make_subtitles(str(make_video(tmp_path)))
    assert "진행   오디오 추출 완료" in report.log


def test_api_errors_are_shown_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stub_pipeline: list[dict[str, object]]
) -> None:
    """openai 예외는 RuntimeError가 아니다. 화면에 트레이스백을 띄우지 않는다."""

    class BadRequestError(Exception):
        pass

    def refuse(*args: object, **kwargs: object) -> None:
        raise BadRequestError("Error code: 400 - 파라미터가 잘못됐습니다")

    monkeypatch.setattr(service.pipeline, "run", refuse)
    report = service.make_subtitles(str(make_video(tmp_path)))
    assert report.ok is False
    assert "BadRequestError" in report.log
    assert "400" in report.log


def test_missing_key_stops_before_any_work(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """키가 없으면 오디오를 뽑기 전에 멈춘다. 몇 분 기다린 뒤 실패하면 안 된다."""
    monkeypatch.setenv("OPENAI_API_KEY", "")
    called: list[str] = []
    monkeypatch.setattr(
        service.preprocess, "probe_duration", lambda path: called.append("probe") or 1.0
    )
    report = service.make_subtitles(str(make_video(tmp_path)))
    assert report.ok is False
    assert "OPENAI_API_KEY" in report.log
    assert called == []


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


def test_cost_guard_stops_long_videos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """안전장치를 넘는 영상은 API로 넘어가기 전에 멈춘다."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VLOGSRT_COST_GUARD_SECONDS", "10")
    monkeypatch.setattr(service.preprocess, "probe_duration", lambda path: 600.0)
    called: list[str] = []
    monkeypatch.setattr(service.pipeline, "run", lambda *a, **k: called.append("run"))

    report = service.make_subtitles(str(make_video(tmp_path)))
    assert report.ok is False
    assert "안전장치" in report.log
    assert "VLOGSRT_COST_GUARD_SECONDS=3600" in report.log
    assert called == []


def test_guard_suggestion_covers_the_video() -> None:
    """77분짜리를 돌리려면 2시간(7200)을 제안해야 한다."""
    assert service._suggest_guard(77 * 60) == 7200
    assert service._suggest_guard(30 * 60) == 3600
    assert service._suggest_guard(125 * 60) == 10800


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
