"""UI가 부르는 순수 로직. **gradio를 import하지 않는다.**

화면 조립은 `ui/app.py`가 하고, 여기서는 경로 해석과 파이프라인 호출만 한다.
그래야 gradio 없이도 테스트가 돈다.
"""

from __future__ import annotations

import contextlib
import io
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from vlogsrt import env, keywords, pipeline, preprocess, transcribe
from vlogsrt.config import Config, load_config

VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".m4a", ".mp3", ".wav")
_QUOTES = "\"'“”‘’"


class UiError(RuntimeError):
    """사람에게 그대로 보여줄 오류. 화면에서는 로그 한 줄로 나간다."""


# ─────────────────────────────────────────────────────────────
#  경로
# ─────────────────────────────────────────────────────────────


def resolve_path(raw: str) -> Path:
    """사람이 붙여넣는 온갖 형태를 받아준다.

    터미널에서 드래그하면 따옴표와 이스케이프가 붙고, Finder에서 복사하면
    앞뒤 공백이 붙는다. 그걸 매번 손으로 지우게 하면 UI를 만든 의미가 없다.
    """
    text = (raw or "").strip().strip(_QUOTES).strip()
    if not text:
        raise UiError("영상 경로를 입력하세요.")
    if text.startswith("file://"):
        from urllib.parse import unquote, urlparse

        text = unquote(urlparse(text).path)
    text = text.replace("\\ ", " ").replace("\\~", "~")
    return Path(text).expanduser()


# 서버가 곧 이 컴퓨터라서 쓸 수 있는 방법이다. 브라우저는 보안상 실제 경로를
# 절대 넘겨주지 않으므로(C:\fakepath\...), 대화상자를 서버 쪽에서 연다.
_CHOOSE_FILE = """
tell application "System Events"
	activate
	set chosen to choose file with prompt "영상 파일을 고르세요" of type {__TYPES__}
end tell
POSIX path of chosen
"""
_CANCEL_CODE = -128


def pick_file_dialog(timeout: float = 300.0) -> str | None:
    """OS 파일 대화상자를 열고 **실제 경로**를 돌려준다. 취소하면 None.

    업로드가 아니라 경로만 받으므로 큰 영상도 복사가 일어나지 않는다.
    `--share` 로 남에게 열어준 상태라면 이 대화상자는 서버 컴퓨터에 뜬다.
    """
    if sys.platform != "darwin":
        raise UiError("파일 대화상자는 macOS에서만 열립니다. 경로를 직접 붙여넣으세요.")
    types = ", ".join(f'"{suffix.lstrip(".")}"' for suffix in VIDEO_SUFFIXES)
    try:
        chosen = subprocess.run(
            ["osascript", "-e", _CHOOSE_FILE.replace("__TYPES__", types)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise UiError("파일 대화상자가 응답하지 않아 닫았습니다.") from None
    except OSError as exc:
        raise UiError(f"파일 대화상자를 열지 못했습니다: {exc}") from exc

    if chosen.returncode != 0:
        if f"({_CANCEL_CODE})" in chosen.stderr:
            return None
        raise UiError(f"파일 대화상자 오류: {chosen.stderr.strip().splitlines()[-1:] or ''}")
    return chosen.stdout.strip() or None


def status_line() -> str:
    """키 상태만 보여준다. **키 값은 절대 화면에 올리지 않는다.**

    모드 표시는 뺐다. 이 화면은 언제나 영상을 새로 전사하며, 지난 실행을
    재사용하는 경로가 없다.
    """
    return f"키 {env.api_key_source()}" if env.has_api_key() else "키 없음"


def build_config(prompt: str = "") -> Config:
    """`vlogsrt.toml`과 기본값에 화면에서 받은 힌트만 얹는다.

    힌트는 회차마다 다르다(등장하는 고유명사가 다르다). 그래서 설정 파일이 아니라
    화면에서 사람이 매번 정한다.
    """
    cfg = load_config()
    entries = keywords.load_keywords(base_file=env.project_root() / keywords.BASE_KEYWORDS)
    if entries:
        cfg = replace(cfg, keywords=tuple(entries))
    return replace(cfg, prompt=prompt.strip())


# ─────────────────────────────────────────────────────────────
#  실행
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RunReport:
    """화면에 그대로 뿌릴 결과. 예외를 던지지 않고 log에 담아 돌려준다."""

    ok: bool
    log: str
    srt_text: str = ""
    files: tuple[str, ...] = ()


@contextlib.contextmanager
def _captured_log() -> Iterator[io.StringIO]:
    """파이프라인이 rich로 stderr에 찍는 진행 로그를 화면용으로 가로챈다."""
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer):
        yield buffer


def _run_streaming(
    work: Callable[[], object], lines: list[str], poll: float = 0.4
) -> Iterator[RunReport | object]:
    """오래 걸리는 단계를 별도 스레드로 돌리며 로그를 실시간으로 흘린다.

    77분짜리 영상은 오디오 추출만 몇 분이다. 끝나고 한꺼번에 보여주면
    그 몇 분 동안 화면이 멈춘 것처럼 보인다. 마지막에 결과값을 내보낸다.
    """
    outcome: dict[str, object] = {}
    failure: dict[str, BaseException] = {}

    def target() -> None:
        try:
            outcome["value"] = work()
        except BaseException as exc:  # noqa: BLE001 - 스레드 밖으로 그대로 옮긴다
            failure["error"] = exc

    with contextlib.redirect_stderr(io.StringIO()) as buffer:
        worker = threading.Thread(target=target, daemon=True)
        worker.start()
        shown = 0
        while worker.is_alive():
            worker.join(poll)
            progress = _progress_lines(buffer.getvalue())
            if len(progress) > shown:
                shown = len(progress)
                yield _running([*lines, *progress])
        trailing = _progress_lines(buffer.getvalue())

    if failure:
        raise failure["error"]
    lines.extend(trailing)
    yield outcome.get("value")


def stream_subtitles(raw_path: str, prompt: str = "") -> Iterator[RunReport]:
    """영상 하나를 처음부터 끝까지 새로 전사한다. 진행 상황은 단계마다 흘려보낸다.

    지난 실행을 재사용하지 않는다. 녹화된 응답도, 오디오 캐시도 보지 않는다.
    같은 영상을 두 번 돌리면 두 번 다 전사한다 — 낡은 기록이 조용히 섞여
    엉뚱한 자막이 나오는 편보다 낫다.

    남기는 파일은 영상 옆의 SRT 하나뿐이다. 오디오·조각·trace는 임시 폴더에서
    만들어지고 함께 사라진다.
    """
    lines: list[str] = []
    try:
        path = resolve_path(raw_path)
        _check_source(path)
        lines.append(f"영상   {path}")
        yield _running(lines)

        if not env.has_api_key():
            raise UiError("OPENAI_API_KEY가 없습니다. .env 에 넣고 다시 실행하세요.")

        cfg = build_config(prompt)
        if cfg.prompt:
            lines.append(f"힌트   {cfg.prompt}")
            yield _running(lines)

        lines.append("확인   길이를 재는 중…")
        yield _running(lines)
        duration = preprocess.probe_duration(path)
        _check_cost_guard(duration, cfg)
        cost = transcribe.estimate_cost(duration, cfg)
        lines[-1] = f"전사   OpenAI 호출 · {duration / 60:.1f}분 · 예상 ${cost:.3f}"
        lines.append("       오디오 추출 → 전사 → 정제. 길이에 비례해 몇 분 걸립니다…")
        yield _running(lines)
        lines.pop()

        srt_text = ""
        summary = ""
        with tempfile.TemporaryDirectory(prefix="vlogsrt-") as scratch:
            result = None
            for step in _run_streaming(lambda: _transcribe(path, cfg, Path(scratch)), lines):
                if isinstance(step, RunReport):
                    yield step
                else:
                    result = step
            assert isinstance(result, pipeline.RunResult)
            summary = _summary(result)
            srt_text = result.outputs["srt"].read_text(encoding="utf-8")
    except UiError as exc:
        yield RunReport(ok=False, log=_join(lines, str(exc)))
        return
    except Exception as exc:  # noqa: BLE001 - 사람에게 보여주고 끝낸다. 삼키지 않는다.
        yield RunReport(ok=False, log=_join(lines, f"실패   {_describe(exc)}"))
        return

    lines.append(summary)
    final, note = _write_beside(path, srt_text)
    lines.append(f"저장   {final}")
    if note:
        lines.append(note)
    yield RunReport(ok=True, log=_join(lines), srt_text=srt_text, files=(str(final),))


def _transcribe(video: Path, cfg: Config, scratch: Path) -> pipeline.RunResult:
    """임시 폴더 안에서만 돈다. `artifacts=False`라 trace를 쓰지 않는다."""
    return pipeline.run(
        video,
        cfg,
        scratch / "out",
        online=True,
        workdir=scratch / "work",
        artifacts=False,
    )


def _check_cost_guard(duration: float, cfg: Config) -> None:
    guard = env.cost_guard_seconds()
    if duration <= guard:
        return
    raise UiError(
        f"{duration / 60:.0f}분은 안전장치({guard / 60:.0f}분)를 넘습니다.\n"
        f"이 영상을 돌리려면 .env 에서 안전장치를 올리세요:\n"
        f"  VLOGSRT_COST_GUARD_SECONDS={_suggest_guard(duration)}\n"
        f"예상 비용 약 ${transcribe.estimate_cost(duration, cfg):.2f}"
    )


def _describe(exc: BaseException) -> str:
    """openai의 예외는 RuntimeError가 아니다. 화면에 한 줄로 보여준다."""
    message = str(exc).strip() or exc.__class__.__name__
    return f"{exc.__class__.__name__}: {message}" if not isinstance(exc, RuntimeError) else message


def make_subtitles(raw_path: str, prompt: str = "") -> RunReport:
    """마지막 상태만 필요할 때. 테스트와 비대화형 호출이 쓴다."""
    last = RunReport(ok=False, log="아무 일도 일어나지 않았습니다.")
    for report in stream_subtitles(raw_path, prompt):
        last = report
    return last


def _running(lines: list[str]) -> RunReport:
    return RunReport(ok=False, log=_join(lines))


def is_uploaded_copy(path: Path) -> bool:
    """파일 선택창으로 올라온 임시 복사본인지.

    브라우저는 보안상 원본 경로를 넘겨주지 않는다. 그래서 gradio 임시 폴더에
    복사된 것만 손에 들어오고, 원본이 어느 폴더에 있었는지는 알 방법이 없다.
    """
    resolved = path.resolve()
    if "gradio" not in resolved.parts:
        return False
    # macOS의 gettempdir()는 /var/folders/..., 실제 경로는 /private/var/folders/... 다.
    # 양쪽 다 resolve해야 비교가 성립한다.
    temp_root = Path(tempfile.gettempdir()).resolve()
    return resolved.is_relative_to(temp_root)


def _write_beside(source_video: Path, srt_text: str) -> tuple[Path, str | None]:
    """SRT를 원본 영상 옆에 쓴다. 이것이 이 프로그램이 남기는 **유일한** 파일이다."""
    destination = source_video.with_suffix(".srt")
    note = None
    if is_uploaded_copy(source_video):
        note = (
            "참고   파일 선택창으로 올린 파일이라 원본 폴더를 알 수 없습니다.\n"
            "       원본 옆에 두려면 경로를 직접 붙여넣으세요. 지금은 아래에서 받으세요."
        )
    try:
        destination.write_text(srt_text, encoding="utf-8")
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / destination.name
        fallback.write_text(srt_text, encoding="utf-8")
        return fallback, f"참고   영상 폴더에 쓰지 못해 임시 폴더에 두었습니다: {exc}"
    return destination, note


def _suggest_guard(duration: float) -> int:
    """넘어선 길이를 덮는 한 시간 단위 값. 사람이 그대로 붙여넣을 수 있게."""
    hours = int(duration // 3600) + 1
    return hours * 3600


def _check_source(path: Path) -> None:
    if not path.is_file():
        raise UiError(f"파일이 없습니다: {path}")
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise UiError(f"영상 파일이 아닙니다: {path.suffix or '확장자 없음'}")


_LOG_PREFIX = {"WARN": "경고", "ERR": "오류"}


def _progress_lines(captured: str) -> list[str]:
    """파이프라인 로그에서 사람이 볼 줄만 남긴다. 경고는 눈에 띄어야 한다."""
    kept = []
    for raw in captured.splitlines():
        text = raw.strip().removeprefix("›").strip()
        if not text:
            continue
        head, _, rest = text.partition(" ")
        label = _LOG_PREFIX.get(head)
        kept.append(f"{label}   {rest}" if label and rest else f"진행   {text}")
    return kept


def _summary(result: pipeline.RunResult) -> str:
    last = result.cues[-1].end if result.cues else 0.0
    return f"결과   큐 {len(result.cues)}개 · 마지막 자막 {last:.0f}초"


def _join(lines: list[str], *extra: str) -> str:
    return "\n".join([*lines, *extra])


# ─────────────────────────────────────────────────────────────
#  실행 환경
# ─────────────────────────────────────────────────────────────


def stale_ui_pids(port: int) -> list[int]:
    """그 포트를 잡고 있는 **우리 UI** 프로세스만 골라낸다.

    남이 쓰는 포트를 건드리면 안 되므로 명령줄에 `ui.app` 이 있는 것만 센다.
    lsof가 없는 환경이면 조용히 빈 목록을 준다.
    """
    try:
        listening = subprocess.run(
            ["lsof", "-t", f"-i:{port}", "-sTCP:LISTEN"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return []
    mine = os.getpid()
    found = []
    for raw in listening.stdout.split():
        try:
            pid = int(raw)
        except ValueError:
            continue
        if pid == mine:
            continue
        described = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            check=False,
        )
        if "ui.app" in described.stdout:
            found.append(pid)
    return found


def stop_stale_ui(port: int, wait_seconds: float = 3.0) -> list[int]:
    """예전에 띄운 UI를 정리한다.

    남겨두면 사람은 습관대로 원래 주소를 열고, 구버전 화면에 붙어
    알 수 없는 컴포넌트 오류를 본다. 그 함정을 아예 없앤다.
    """
    stopped = []
    for pid in stale_ui_pids(port):
        try:
            os.kill(pid, signal.SIGTERM)
            stopped.append(pid)
        except OSError:
            continue
    deadline = time.monotonic() + wait_seconds
    while stopped and stale_ui_pids(port) and time.monotonic() < deadline:
        time.sleep(0.2)
    return stopped


def busy_port_notice(preferred: int, chosen: int) -> list[str]:
    """포트를 비켜갈 때 하는 말.

    예전에 띄운 서버가 남아 있으면 사람은 습관대로 원래 주소를 연다. 그러면
    구버전 화면에 붙어 알 수 없는 컴포넌트 오류가 난다. 그 함정을 미리 말해준다.
    """
    return [
        f"{preferred} 포트를 이미 쓰고 있습니다 — 이전에 띄운 UI가 남아 있을 수 있습니다.",
        '예전 화면에 붙으면 엉뚱한 오류가 납니다. 정리하려면:  pkill -f "ui.app"',
        f"이번에는 {chosen} 으로 띄웁니다. 아래 주소를 여세요.",
    ]


def pick_port(preferred: int, attempts: int = 20) -> int:
    """이미 쓰는 포트면 옆으로 비켜간다.

    앞서 띄운 UI가 남아 있는 일이 흔한데, 그때마다 프로세스를 죽이라고 하는 것보다
    다음 포트로 뜨고 알려주는 편이 낫다.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise UiError(
        f"{preferred}부터 {preferred + attempts - 1}까지 빈 포트가 없습니다. "
        "--port 로 다른 번호를 지정하세요."
    )
