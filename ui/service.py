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
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

from vlogsrt import env, keywords, pipeline, preprocess, transcribe
from vlogsrt.config import Config, load_config
from vlogsrt.validate import ERR, WARN

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


def has_fixture(stem: str) -> bool:
    return (env.fixture_dir() / f"{stem}.whisper.json").is_file()


def status_line() -> str:
    """모드와 키 상태. **키 값은 절대 화면에 올리지 않는다.**"""
    mode = "오프라인" if env.is_offline() else "온라인"
    key = f"키 {env.api_key_source()}" if env.has_api_key() else "키 없음"
    return f"{mode} · {key}"


def build_config() -> Config:
    """`vlogsrt.toml`과 기본값만 쓴다. 세부 조정은 `vlogsrt redo`가 맡는다."""
    cfg = load_config()
    entries = keywords.load_keywords(base_file=env.project_root() / keywords.BASE_KEYWORDS)
    return replace(cfg, keywords=tuple(entries)) if entries else cfg


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


def make_subtitles(raw_path: str) -> RunReport:
    """UI의 유일한 실행 경로.

    녹화 기록이 없으면 API를 호출해 받아오고, 있으면 그것을 재사용한다.
    같은 영상을 다시 눌러도 API를 두 번 부르지 않는다.
    """
    lines: list[str] = []
    try:
        path = resolve_path(raw_path)
        recorded = has_fixture(path.stem)
        lines.append(f"영상   {path}")
        if not recorded:
            _check_source(path)
            duration = preprocess.probe_duration(path)
            guard = env.cost_guard_seconds()
            if duration > guard:
                raise UiError(
                    f"{duration / 60:.0f}분은 안전장치({guard / 60:.0f}분)를 넘습니다. "
                    "짧게 자르거나 .env 의 VLOGSRT_COST_GUARD_SECONDS 를 올리세요."
                )
            cost = transcribe.estimate_cost(duration, build_config())
            lines.append(f"전사   OpenAI 호출 · {duration / 60:.1f}분 · 예상 ${cost:.3f}")
        else:
            lines.append("전사   기록된 응답 재사용")

        with _captured_log() as buffer:
            cfg = build_config()
            if not recorded:
                pipeline.record_fixtures(path, cfg)
            workdir = env.project_root() / "out" / path.stem
            result = pipeline.run(path, cfg, workdir, force_offline=True)
    except UiError as exc:
        return RunReport(ok=False, log=_join(lines, str(exc)))
    except RuntimeError as exc:
        return RunReport(ok=False, log=_join(lines, f"실패   {exc}"))

    lines.extend(_progress_lines(buffer.getvalue()))
    lines.append(_summary(result))
    final, note = _place_final_srt(path, result.outputs["srt"])
    lines.append(f"저장   {final}")
    if note:
        lines.append(note)
    lines.append(f"작업물 {result.outputs['srt'].parent}")
    return RunReport(
        ok=True,
        log=_join(lines),
        srt_text=final.read_text(encoding="utf-8"),
        files=(str(final), *_downloads(result)[1:]),
    )


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


def _place_final_srt(source_video: Path, produced: Path) -> tuple[Path, str | None]:
    """최종 SRT를 원본 영상 옆에 둔다. 나머지 산출물은 작업 폴더에 남긴다."""
    if is_uploaded_copy(source_video):
        return produced, (
            "참고   파일 선택창으로 올린 파일이라 원본 폴더를 알 수 없습니다.\n"
            "       원본 옆에 저장하려면 경로를 직접 붙여넣으세요."
        )
    destination = source_video.parent / produced.name
    if destination.resolve() == produced.resolve():
        return produced, None
    try:
        destination.write_text(produced.read_text(encoding="utf-8"), encoding="utf-8")
    except OSError as exc:
        return produced, f"참고   원본 폴더에 쓰지 못했습니다: {exc}"
    return destination, None


def _check_source(path: Path) -> None:
    if not path.is_file():
        raise UiError(f"파일이 없습니다: {path}")
    if path.suffix.lower() not in VIDEO_SUFFIXES:
        raise UiError(f"영상 파일이 아닙니다: {path.suffix or '확장자 없음'}")


def _progress_lines(captured: str) -> list[str]:
    """파이프라인 로그에서 사람이 볼 줄만 남긴다."""
    kept = []
    for raw in captured.splitlines():
        text = raw.strip().removeprefix("›").strip()
        if text:
            kept.append(f"진행   {text}")
    return kept


def _summary(result: pipeline.RunResult) -> str:
    errors = sum(1 for issue in result.issues if issue.level == ERR)
    warnings = sum(1 for issue in result.issues if issue.level == WARN)
    threshold = float(result.trace.config_snapshot.get("conf_threshold", 0.75))
    check = sum(1 for cue in result.cues if cue.conf < threshold or cue.flags)
    return f"결과   큐 {len(result.cues)}개 · 오류 {errors} · 경고 {warnings} · 확인할 큐 {check}개"


def _downloads(result: pipeline.RunResult) -> tuple[str, ...]:
    return tuple(
        str(result.outputs[name]) for name in ("srt", "review", "report") if name in result.outputs
    )


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
