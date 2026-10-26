"""Native macOS panels hosted by a foreground LaunchServices app, not a CLI process."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Callable
from pathlib import Path

WORKSPACE_KIND = "workspace"
FOLDER_KIND = "folder"
VIDEO_KIND = "video"
SRT_KIND = "srt"
CHOOSER_CONCURRENCY_ID = "native-chooser"
DIALOG_TIMEOUT_SECONDS = 120.0
VIDEO_DIALOG_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mp3", ".wav", ".m4a")


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


def applescript_path_chooser(kind: str, timeout: float = DIALOG_TIMEOUT_SECONDS) -> str | None:
    """Launch one native panel; return its path, None on cancel, or a useful error."""
    if sys.platform != "darwin":
        raise ChooserError("경로 선택 대화상자는 macOS에서만 열립니다. 경로 입력을 이용하세요.")
    if kind not in (WORKSPACE_KIND, FOLDER_KIND, VIDEO_KIND, SRT_KIND):
        raise ChooserError(f"모르는 선택기 종류: {kind}")
    with tempfile.TemporaryDirectory(prefix="vlogsrt-picker-") as directory:
        app = Path(directory) / "vlogsrt Picker.app"
        result = Path(directory) / "result.json"
        try:
            compiled = subprocess.run(
                ["/usr/bin/osacompile", "-l", "JavaScript", "-o", str(app), "-e", PICKER_JXA],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            if compiled.returncode:
                raise _failure(kind, compiled)
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
