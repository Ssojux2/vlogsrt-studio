"""D61 — 지금 도는 외부 프로세스의 등록표와 opt-in `-progress`.

긴 ffmpeg(분석 프록시·미리보기·썸네일)가 20–60분 돌 때 화면에는 「완료 12/18」뿐이라 멈춤과
느림을 가를 수 없었다. 등록표는 `run_process` 한 자리에서만 채워지고(규칙 7), 진행 파일은
서버가 켰을 때만 ffmpeg argv에 붙는다 — 꺼져 있으면 argv가 바이트 그대로다.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import preprocess
from vlogsrt.task_runtime import cancellation_scope

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg가 없는 환경")

FFMPEG_ARGV = [
    "/opt/homebrew/bin/ffmpeg", "-hide_banner", "-nostdin", "-y",
    "-i", "/media/in.mov", "-c:v", "libx264", "/out/proxy.mp4",
]  # fmt: skip
FFPROBE_ARGV = ["/opt/homebrew/bin/ffprobe", "-v", "error", "-of", "json", "/media/in.mov"]


@pytest.fixture(autouse=True)
def progress_off() -> Any:
    """시험마다 진행 파일을 끈 채로 시작하고 끝낸다 — 전역 상태가 다른 시험에 새지 않는다."""
    preprocess.disable_process_progress()
    yield
    preprocess.disable_process_progress()


def _completed(command: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 0, "", "")


def _spy_run(monkeypatch: pytest.MonkeyPatch, during: Any = None) -> list[dict[str, Any]]:
    """빠른 길(`subprocess.run`)을 가로채 argv와 그 순간의 등록표를 남긴다."""
    seen: list[dict[str, Any]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if during is not None:
            during(argv)
        seen.append({"argv": list(argv), "active": preprocess.active_processes()})
        return _completed(argv)

    monkeypatch.setattr(preprocess.subprocess, "run", run)
    return seen


# ── 등록과 해제 ─────────────────────────────────────────────────────────────


def test_a_running_process_is_listed_with_its_step_and_then_removed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_run(monkeypatch)
    before = time.time()
    preprocess.run_process(FFMPEG_ARGV, capture_output=True, step="분석 프록시 만들기")
    assert preprocess.active_processes() == ()
    (snapshot,) = seen[0]["active"]
    assert snapshot.step == "분석 프록시 만들기"
    assert snapshot.tool == "ffmpeg"
    assert snapshot.input_path == "/media/in.mov"
    assert snapshot.output_path == "/out/proxy.mp4"
    assert snapshot.timeout_seconds is None  # 빠른 길에 한도가 없으면 한도 없음
    assert snapshot.progress_seconds is None  # 진행 파일이 꺼져 있다
    assert snapshot.whole_input is True
    assert before <= snapshot.started_at <= time.time()


def test_ffprobe_reads_its_input_from_the_last_argument(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_run(monkeypatch)
    preprocess.run_process(FFPROBE_ARGV, capture_output=True, timeout=30)
    (snapshot,) = seen[0]["active"]
    assert (snapshot.tool, snapshot.input_path, snapshot.output_path) == (
        "ffprobe",
        "/media/in.mov",
        None,
    )
    assert snapshot.step == "ffprobe"  # 단계 이름이 없으면 도구 이름(ProcessTimeout과 같다)
    assert snapshot.timeout_seconds == 30


def test_a_pipe_output_and_a_ranged_decode_are_described_honestly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_run(monkeypatch)
    ranged = ["ffmpeg", "-ss", "12.5", "-i", "/media/in.mov", "-t", "3", "-f", "null", "-"]
    preprocess.run_process(ranged, capture_output=True)
    (snapshot,) = seen[0]["active"]
    assert snapshot.output_path is None  # `-`는 파일이 아니다
    assert snapshot.whole_input is False  # 범위 디코드는 원본 길이로 %를 재지 않는다


def test_each_process_gets_its_own_increasing_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = _spy_run(monkeypatch)
    preprocess.run_process(FFPROBE_ARGV)
    preprocess.run_process(FFPROBE_ARGV)
    first, second = (entry["active"][0].process_id for entry in seen)
    assert second > first


def test_the_cancellable_path_records_its_effective_limit() -> None:
    """작업 안(취소를 듣는 실행)은 Popen 길이다 — 한도를 주지 않으면 이름 붙은 상한이 한도다."""
    observed: list[tuple[preprocess.ProcessActivity, ...]] = []
    sleeper = [sys.executable, "-c", "import time; time.sleep(0.6)"]
    with cancellation_scope(lambda: None):
        preprocess.run_process(
            sleeper,
            capture_output=True,
            step="미리보기 만들기",
            on_poll=lambda: observed.append(preprocess.active_processes()),
        )
    seen = [snapshot for batch in observed for snapshot in batch]
    assert seen and {s.step for s in seen} == {"미리보기 만들기"}
    assert {s.timeout_seconds for s in seen} == {preprocess._CANCELLABLE_TIMEOUT_SECONDS}
    assert preprocess.active_processes() == ()


def test_a_real_process_is_visible_from_another_thread_while_it_runs() -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(1.5)"]
    worker = threading.Thread(
        target=preprocess.run_process, args=(sleeper,), kwargs={"step": "썸네일 추출"}
    )
    worker.start()
    deadline = time.monotonic() + 10
    found: tuple[preprocess.ProcessActivity, ...] = ()
    while time.monotonic() < deadline and not found:
        found = preprocess.active_processes()
        time.sleep(0.02)
    worker.join(timeout=20)
    assert [s.step for s in found] == ["썸네일 추출"]
    assert preprocess.active_processes() == ()


@pytest.mark.parametrize("cancellable", [False, True])
def test_the_entry_is_removed_on_timeout(cancellable: bool) -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    scope = cancellation_scope(lambda: None) if cancellable else _nothing()
    with scope, pytest.raises(preprocess.ProcessTimeout):
        preprocess.run_process(sleeper, capture_output=True, timeout=0.3, step="장면 컷 찾기")
    assert preprocess.active_processes() == ()


def test_the_entry_is_removed_when_the_program_cannot_start() -> None:
    with pytest.raises(OSError):
        preprocess.run_process(["/nonexistent/ffmpeg-binary", "-i", "x", "y"])
    assert preprocess.active_processes() == ()


def test_the_entry_is_removed_when_polling_raises() -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]

    def boom() -> None:
        raise RuntimeError("취소 흉내")

    with pytest.raises(RuntimeError, match="취소 흉내"):
        preprocess.run_process(sleeper, capture_output=True, on_poll=boom)
    assert preprocess.active_processes() == ()


class _nothing:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *exc: object) -> None:
        return None


# ── 단계 이름 ───────────────────────────────────────────────────────────────


def test_a_step_scope_names_untagged_processes_and_the_outer_scope_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen = _spy_run(monkeypatch)

    @preprocess.process_step("프레임 추출")
    def inner() -> None:
        preprocess.run_process(FFMPEG_ARGV)

    inner()
    with preprocess.process_step("썸네일 추출"):
        inner()  # 바깥이 더 구체적이다 — 썸네일을 만드는 자리가 이름을 정한다
        preprocess.run_process(FFMPEG_ARGV, step="장면 컷 찾기")  # 명시한 이름이 이긴다
    assert [entry["active"][0].step for entry in seen] == [
        "프레임 추출",
        "썸네일 추출",
        "장면 컷 찾기",
    ]


def test_a_timeout_inside_a_step_scope_names_that_step() -> None:
    sleeper = [sys.executable, "-c", "import time; time.sleep(30)"]
    step = preprocess.process_step("미리보기 만들기")
    with step, pytest.raises(preprocess.ProcessTimeout) as caught:
        preprocess.run_process(sleeper, capture_output=True, timeout=0.3)
    assert caught.value.step == "미리보기 만들기"


def test_the_untagged_ffmpeg_entry_points_now_carry_a_step_name() -> None:
    """화면이 「ffmpeg」 대신 무엇을 하는지 말한다 — 썸네일·미리보기는 사용자가 기다리는 단계다."""
    names = {
        preprocess.extract_visual_review_preview: "미리보기 만들기",
        preprocess.extract_frame_index: "프레임 추출",
        preprocess.extract_audio: "오디오 추출",
        preprocess.extract_pcm_s16le: "PCM 추출",
        preprocess.extract_raw_clip: "클립 추출",
        preprocess.extract_caption_preview: "자막 미리보기 만들기",
        preprocess.split_at: "조각 나누기",
        preprocess.probe_duration: "길이 확인",
    }
    for function, label in names.items():
        assert getattr(function, "__vl_process_step__", None) == label, function.__name__


# ── 진행 파일 (opt-in) ─────────────────────────────────────────────────────


@pytest.mark.parametrize("cancellable", [False, True])
def test_argv_is_identical_when_progress_is_disabled(
    monkeypatch: pytest.MonkeyPatch, cancellable: bool
) -> None:
    sent: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        sent.append(argv)
        return _completed(argv)

    class Popen:
        returncode = 0

        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            sent.append(argv)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "", ""

    monkeypatch.setattr(preprocess.subprocess, "run", run)
    monkeypatch.setattr(preprocess.subprocess, "Popen", Popen)
    command = list(FFMPEG_ARGV)
    scope = cancellation_scope(lambda: None) if cancellable else _nothing()
    with scope:
        completed = preprocess.run_process(command, capture_output=True, step="분석 프록시 만들기")
    assert sent == [FFMPEG_ARGV]
    assert sent[0] is command  # 꺼져 있으면 새 목록조차 만들지 않는다
    assert completed.args == FFMPEG_ARGV


def test_progress_is_inserted_right_after_the_ffmpeg_binary_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen = _spy_run(monkeypatch)
    preprocess.enable_process_progress(tmp_path / "progress")
    completed = preprocess.run_process(list(FFMPEG_ARGV))
    preprocess.run_process(list(FFPROBE_ARGV))
    preprocess.run_process(["ffmpeg", "-hide_banner", "-encoders"])  # 입력이 없는 출력은 그대로
    ffmpeg_argv, probe_argv, info_argv = (entry["argv"] for entry in seen)
    process_id = seen[0]["active"][0].process_id
    progress = tmp_path / "progress" / f"{process_id}.txt"
    # `file:` — 경로의 `:`가 프로토콜로 읽히지 않는다.
    assert ffmpeg_argv == [FFMPEG_ARGV[0], "-progress", f"file:{progress}", *FFMPEG_ARGV[1:]]
    assert probe_argv == FFPROBE_ARGV
    assert info_argv == ["ffmpeg", "-hide_banner", "-encoders"]
    assert completed.args == FFMPEG_ARGV  # 호출자는 자기가 준 argv를 돌려받는다


def test_progress_seconds_come_from_the_last_out_time(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def write_progress(argv: list[str]) -> None:
        Path(argv[2].removeprefix("file:")).write_text(
            "frame=10\nout_time_us=1000000\nout_time_ms=1000000\nprogress=continue\n"
            "frame=20\nout_time_us=N/A\nprogress=continue\n"
            "frame=30\nout_time_ms=2500000\nout_time_us=2500000\nprogress=continue\n",
            encoding="utf-8",
        )

    seen = _spy_run(monkeypatch, during=write_progress)
    preprocess.enable_process_progress(tmp_path)
    preprocess.run_process(list(FFMPEG_ARGV))
    (snapshot,) = seen[0]["active"]
    assert snapshot.progress_seconds == 2.5
    assert list(tmp_path.iterdir()) == []  # 끝나면 진행 파일을 지운다


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", None),
        ("out_time_us=N/A\nprogress=continue\n", None),
        ("out_time_ms=1500000\nprogress=continue\n", 1.5),  # ffmpeg의 ms 칸도 마이크로초다
        ("out_time_us=3000000\nprogress=continue\nout_time_us=3500", 3.0),  # 쓰다 만 줄은 안 본다
        ("out_time_us=-9223372036854775807\nprogress=continue\n", None),
    ],
)
def test_progress_parsing(tmp_path: Path, text: str, expected: float | None) -> None:
    path = tmp_path / "1.txt"
    path.write_text(text, encoding="utf-8")
    assert preprocess._read_progress_seconds(path) == expected


def test_a_missing_progress_file_is_unknown_progress(tmp_path: Path) -> None:
    assert preprocess._read_progress_seconds(tmp_path / "absent.txt") is None


def test_only_a_long_tail_of_the_progress_file_is_read(tmp_path: Path) -> None:
    """한 시간짜리 인코드의 진행 파일은 MB 단위로 자란다 — 끝부분만 읽어도 마지막 값이 있다."""
    path = tmp_path / "1.txt"
    block = "frame=1\nout_time_us=1000000\nprogress=continue\n"
    path.write_text(block * 50000 + "out_time_us=7000000\nprogress=continue\n", encoding="utf-8")
    assert preprocess._read_progress_seconds(path) == 7.0


def test_the_progress_file_is_removed_even_when_the_process_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        Path(argv[2].removeprefix("file:")).write_text("out_time_us=1\n", encoding="utf-8")
        raise subprocess.TimeoutExpired(argv, 1.0)

    monkeypatch.setattr(preprocess.subprocess, "run", run)
    preprocess.enable_process_progress(tmp_path)
    with pytest.raises(preprocess.ProcessTimeout):
        preprocess.run_process(list(FFMPEG_ARGV), timeout=1.0)
    assert list(tmp_path.iterdir()) == []
    assert preprocess.active_processes() == ()


def test_progress_is_off_unless_the_server_turns_it_on() -> None:
    """CLI와 시험은 진행 파일을 켜지 않는다 — 켜는 곳은 브라우저 서버 진입점 하나다."""
    root = Path(__file__).resolve().parent.parent
    callers = sorted(
        str(path.relative_to(root))
        for folder in ("vlogsrt", "ui", "scripts")
        for path in (root / folder).rglob("*.py")
        if "enable_process_progress(" in path.read_text(encoding="utf-8")
    )
    assert callers == ["ui/app.py", "vlogsrt/preprocess.py"]


@needs_ffmpeg
def test_real_ffmpeg_output_bytes_do_not_change_with_progress(tmp_path: Path) -> None:
    def encode(name: str) -> bytes:
        target = tmp_path / name
        preprocess.run_process(
            [
                shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin",
                "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=16000:duration=2",
                "-c:a", "pcm_s16le", "-fflags", "+bitexact", "-flags", "+bitexact",
                "-f", "wav", str(target),
            ],
            capture_output=True,
            check=True,
        )  # fmt: skip
        return target.read_bytes()

    plain = encode("plain.wav")
    preprocess.enable_process_progress(tmp_path / "progress")
    tracked = encode("tracked.wav")
    assert tracked == plain
    assert list((tmp_path / "progress").iterdir()) == []


def _sine_to_null(progress_seen: list[str]) -> None:
    """실제 ffmpeg 한 번 — 도는 동안 붙은 argv를 남긴다(진행 파일이 붙었는지 본다)."""
    real_run = subprocess.run

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        progress_seen.extend(arg for arg in argv if arg.startswith("file:"))
        return real_run(argv, **kwargs)

    command = [
        shutil.which("ffmpeg") or "ffmpeg", "-hide_banner", "-nostdin", "-loglevel", "error",
        "-y", "-f", "lavfi", "-i", "sine=duration=1", "-f", "null", "-",
    ]  # fmt: skip
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(preprocess.subprocess, "run", run)
        completed = preprocess.run_process(command, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


@needs_ffmpeg
def test_a_vanished_progress_folder_is_made_again_and_ffmpeg_still_runs(tmp_path: Path) -> None:
    """며칠 켜 둔 서버에서 `$TMPDIR` 청소가 폴더를 지우면 ffmpeg가 254로 죽었다 — 다시 만든다."""
    folder = tmp_path / "with:colon" / "progress"
    preprocess.enable_process_progress(folder)
    shutil.rmtree(tmp_path / "with:colon")
    seen: list[str] = []
    _sine_to_null(seen)
    assert len(seen) == 1 and seen[0].startswith(f"file:{folder}")
    assert list(folder.iterdir()) == []  # 진행 파일은 끝나면 지운다


@needs_ffmpeg
def test_an_uncreatable_progress_folder_means_no_progress_not_a_failure(tmp_path: Path) -> None:
    blocker = tmp_path / "blocker"
    preprocess.enable_process_progress(tmp_path / "ok")
    blocker.write_text("폴더 자리를 막는 파일", encoding="utf-8")
    preprocess._PROGRESS_DIR = blocker / "progress"  # 부모가 파일이라 만들 수 없다
    seen: list[str] = []
    _sine_to_null(seen)
    assert seen == []  # 진행 없이 그대로 돌았다
