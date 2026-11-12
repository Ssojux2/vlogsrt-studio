"""Native macOS panels hosted by a foreground LaunchServices app, not a CLI process."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

from vlogsrt import log

WORKSPACE_KIND = "workspace"
FOLDER_KIND = "folder"
VIDEO_KIND = "video"
SRT_KIND = "srt"
CHOOSER_CONCURRENCY_ID = "native-chooser"
DIALOG_TIMEOUT_SECONDS = 120.0
VIDEO_DIALOG_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a")
PICKER_CACHE_NAME = "vlogsrt"
PICKER_APP_NAME = "vlogsrt Picker.app"
PICKER_SHA_CHARS = 12
PICKER_COMPILE_TIMEOUT_SECONDS = 15


class ChooserError(RuntimeError):
    """A failed panel, distinct from the user's explicit cancellation."""


PathChooser = Callable[[str], "str | None"]

# Applet run(args) does NOT receive `open --args` arguments. Read process arguments
# explicitly; no selected/user-entered path is interpolated into executable code.
PICKER_JXA = """
ObjC.import('AppKit');
function run() {
    const argv = ObjC.deepUnwrap($.NSProcessInfo.processInfo.arguments);
    const output = argv[argv.length - 1];
    const kind = argv[argv.length - 2];
    function write(path, text) {
        if (!$(text).writeToFileAtomicallyEncodingError(path, true, $.NSUTF8StringEncoding, null)) {
            throw new Error('Cannot write picker result');
        }
    }
    write(output + '.pid', String($.NSProcessInfo.processInfo.processIdentifier));
    try {
        const host = Application.currentApplication();
        host.includeStandardAdditions = true;
        host.activate();
        const panel = $.NSOpenPanel.openPanel;
        panel.canChooseFiles = kind !== 'folder';
        panel.canChooseDirectories = kind !== 'video' && kind !== 'srt';
        panel.canCreateDirectories = false;
        panel.allowsMultipleSelection = false;
        panel.treatsFilePackagesAsDirectories = false;
        panel.title = 'vlogsrt';
        panel.prompt = '고르기';
        panel.message = kind === 'workspace'
            ? '프로젝트 폴더 또는 Final Cut 라이브러리(.fcpbundle)를 고르세요'
            : kind === 'folder' ? '폴더를 고르세요'
            : kind === 'srt' ? '타이틀로 달 SRT 자막 파일을 고르세요'
            : '영상 파일을 고르세요';
        if (kind === 'video') {
            panel.allowedFileTypes = ['mp4','mov','m4v','mkv','webm','avi','mp3','wav','m4a'];
        }
        if (kind === 'srt') {
            panel.allowedFileTypes = ['srt'];
        }
        panel.makeKeyAndOrderFront(null);
        $.NSRunningApplication.currentApplication.activateWithOptions(3);
        const accepted = Number(panel.runModal) === 1;
        write(output, JSON.stringify({path: accepted ? ObjC.unwrap(panel.URL.path) : null}));
    } catch (error) {
        write(output, JSON.stringify({error: String(error)}));
    }
}
"""


def _failure(kind: str, completed: subprocess.CompletedProcess[str]) -> ChooserError:
    lines = completed.stderr.strip().splitlines()
    detail = lines[-1] if lines else "표준 오류 출력 없음"
    code = completed.returncode
    if code < 0:
        detail = f"{signal.Signals(-code).name} 신호로 종료됨 · {detail}"
    return ChooserError(f"경로 선택 오류({kind}, 종료코드 {code}): {detail}")


def _stop_owned_helper(result: Path, app: Path) -> None:
    """Timeout/interrupt must not leave our independent GUI app's panel behind."""
    try:
        pid = int(result.with_suffix(result.suffix + ".pid").read_text())
        process = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True, check=False
        )
        if str(app / "Contents" / "MacOS") in process.stdout:
            os.kill(pid, signal.SIGTERM)
    except (OSError, ValueError):
        pass


def _picker_cache_dir() -> Path:
    """The per-user cache root — the one place tests redirect away from the real home."""
    return Path.home() / "Library" / "Caches" / PICKER_CACHE_NAME


def _picker_app_path() -> Path:
    """Keyed by the JXA text at call time: a changed script gets a fresh bundle (D106)."""
    sha = hashlib.sha256(PICKER_JXA.encode("utf-8")).hexdigest()[:PICKER_SHA_CHARS]
    return _picker_cache_dir() / f"picker-{sha}" / PICKER_APP_NAME


def _compile_picker(kind: str, app: Path) -> None:
    compiled = subprocess.run(
        ["/usr/bin/osacompile", "-l", "JavaScript", "-o", str(app), "-e", PICKER_JXA],
        capture_output=True,
        text=True,
        check=False,
        timeout=PICKER_COMPILE_TIMEOUT_SECONDS,
    )
    if compiled.returncode:
        raise _failure(kind, compiled)


def _adopt_compiled(staged: Path, final: Path) -> None:
    """Atomically hang a staged bundle folder; if another click won the race, keep its bundle.

    The loser's staged folder is left for the caller's cleanup.
    """
    try:
        os.rename(staged, final)
    except OSError:
        if not (final / PICKER_APP_NAME / "Contents" / "MacOS").is_dir():
            raise


def _stage_dir(final: Path) -> Path:
    """A staging folder beside the final one — same filesystem, so the rename is atomic."""
    final.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{final.name}.tmp-{os.getpid()}-", dir=final.parent))


def _ensure_picker_app(kind: str, scratch: Path | None = None) -> Path:
    """Compile once into the user cache and reuse it — LaunchServices then knows the app.

    If the cache can't be prepared, compile into `scratch` as before (one call only) rather
    than adding a new way to fail; without `scratch` the error propagates.
    """
    app = _picker_app_path()
    if (app / "Contents" / "MacOS").is_dir():
        return app
    try:
        staged = _stage_dir(app.parent)
    except OSError as error:
        if scratch is None:
            raise
        log.warn(f"선택 창 캐시를 쓸 수 없어 이번만 임시로 만듭니다: {error}")
        _compile_picker(kind, scratch / PICKER_APP_NAME)
        return scratch / PICKER_APP_NAME
    try:
        _compile_picker(kind, staged / PICKER_APP_NAME)
        _adopt_compiled(staged, app.parent)
    finally:
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
    return app


def warm_picker_app() -> None:
    """서버 시작 때 미리 컴파일한다 — 첫 「찾아보기」도 osacompile을 기다리지 않는다(D106)."""
    if sys.platform != "darwin":
        return
    try:
        _ensure_picker_app(FOLDER_KIND)
    except (ChooserError, OSError, subprocess.TimeoutExpired) as error:
        log.warn(f"선택 창 미리 만들기 실패 — 누를 때 다시 만듭니다: {error}")


def applescript_path_chooser(kind: str, timeout: float = DIALOG_TIMEOUT_SECONDS) -> str | None:
    """Launch one native panel; return its path, None on cancel, or a useful error."""
    if sys.platform != "darwin":
        raise ChooserError("경로 선택 대화상자는 macOS에서만 열립니다. 경로 입력을 이용하세요.")
    if kind not in (WORKSPACE_KIND, FOLDER_KIND, VIDEO_KIND, SRT_KIND):
        raise ChooserError(f"모르는 선택기 종류: {kind}")
    with tempfile.TemporaryDirectory(prefix="vlogsrt-picker-") as directory:
        app = Path(directory) / PICKER_APP_NAME
        result = Path(directory) / "result.json"
        try:
            app = _ensure_picker_app(kind, Path(directory))
            opened = subprocess.run(
                ["/usr/bin/open", "-W", "-n", str(app), "--args", kind, str(result)],
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
            if opened.returncode:
                raise _failure(kind, opened)
            if not result.is_file():
                raise ChooserError("선택 창이 결과 없이 종료됐습니다. 경로 입력으로 열어 주세요.")
            payload = json.loads(result.read_text(encoding="utf-8"))
            if payload.get("error"):
                raise ChooserError(f"경로 선택 오류({kind}): {payload['error']}")
            selected = payload.get("path")
            if selected is not None and not isinstance(selected, str):
                raise ChooserError("선택 창이 잘못된 경로를 반환했습니다.")
            return selected or None
        except subprocess.TimeoutExpired:
            raise ChooserError(
                "선택 창의 대기 시간이 끝났습니다. 경로 입력으로 열어 주세요."
            ) from None
        except OSError as error:
            raise ChooserError(
                f"선택 창을 열 수 없습니다: {error}. 경로 입력을 이용하세요."
            ) from error
        finally:
            _stop_owned_helper(result, app)


TK_CHOOSER_SCRIPT = Path(__file__).with_name("tk_chooser.py")
"""macOS 밖의 선택 창을 여는 자식 스크립트 — 표준 라이브러리만 쓴다(W-46a)."""


def native_chooser_available() -> bool:
    """이 컴퓨터에서 「찾아보기」 창을 열 수 있는가 — macOS는 언제나, 그 밖은 tkinter가 있을 때.

    tkinter는 import하지 않고 찾기만 한다(서버 프로세스에 Tk를 들이지 않는다). 없으면 화면이
    「찾아보기」를 숨기고 경로 입력만 남긴다.
    """
    if sys.platform == "darwin":
        return True
    return importlib.util.find_spec("tkinter") is not None


def tkinter_path_chooser(kind: str, timeout: float = DIALOG_TIMEOUT_SECONDS) -> str | None:
    """tkinter 창 하나를 **따로 띄운 프로세스**에서 연다 — 고른 경로, 취소면 None, 실패는 사유.

    `-I`(격리)로 띄워 작업 폴더의 같은 이름 파일이 표준 라이브러리를 가리지 못하게 한다.
    """
    if kind not in (WORKSPACE_KIND, FOLDER_KIND, VIDEO_KIND, SRT_KIND):
        raise ChooserError(f"모르는 선택기 종류: {kind}")
    try:
        completed = subprocess.run(
            [sys.executable, "-I", str(TK_CHOOSER_SCRIPT), kind],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise ChooserError("선택 창의 대기 시간이 끝났습니다. 경로 입력으로 열어 주세요.") from None
    except OSError as error:
        raise ChooserError(f"선택 창을 열 수 없습니다: {error}. 경로 입력을 이용하세요.") from error
    return _tk_result(kind, completed)


def _tk_result(kind: str, completed: subprocess.CompletedProcess[str]) -> str | None:
    lines = completed.stdout.strip().splitlines()
    if completed.returncode or not lines:
        raise _failure(kind, completed)
    try:
        payload = json.loads(lines[-1])
    except ValueError as error:
        raise ChooserError(f"선택 창이 알 수 없는 답을 냈습니다: {lines[-1][:200]}") from error
    if not isinstance(payload, dict):
        raise ChooserError("선택 창이 잘못된 답을 냈습니다.")
    if payload.get("error"):
        raise ChooserError(f"경로 선택 오류({kind}): {payload['error']}. 경로 입력을 이용하세요.")
    selected = payload.get("path")
    if selected is not None and not isinstance(selected, str):
        raise ChooserError("선택 창이 잘못된 경로를 반환했습니다.")
    return selected or None


def native_path_chooser(kind: str) -> str | None:
    """운영체제에 맞는 선택 창 — macOS는 NSOpenPanel(osascript), 그 밖은 tkinter(W-46a)."""
    if sys.platform == "darwin":
        return applescript_path_chooser(kind)
    return tkinter_path_chooser(kind)
