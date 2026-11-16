"""더블클릭 실행 파일(`실행.command`·`실행.bat`·`실행.sh`)이 함께 쓰는 확인 (D100 §8, W-84).

두 실행 파일이 같은 판단을 따로 짜면 한쪽만 고쳐지고 갈린다. 그래서 판단은 여기 한 곳에 두고,
실행 파일은 이 명령의 종료 코드만 본다. 표준 라이브러리만 쓰고 앱(`vlogsrt`·`ui`)을 import하지
않는다 — 꾸러미가 아직 깔리지 않은 첫 실행에서도 돌아야 한다. 프로세스를 띄우지 않는다
(pip·venv 호출은 실행 파일이 한다).

    python scripts/launch_check.py stamp-ok      # 설치 표지가 지금 pyproject.toml과 맞으면 0
    python scripts/launch_check.py write-stamp   # 설치가 끝났다고 표지를 남긴다
    python scripts/launch_check.py importable    # 설치가 실패했어도 필요한 꾸러미가 다 있으면 0
    python scripts/launch_check.py tools         # ffmpeg·ffprobe가 PATH에 있으면 0
    python scripts/launch_check.py advice        # 없어도 뜨지만 알려 줄 것(tkinter) — 언제나 0
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

MIN_PYTHON = (3, 11)
# 실행 파일의 `pip install -e ".[ui,project]"`와 같아야 한다 — 시험이 두 파일의 글자를 대조한다.
INSTALL_EXTRAS = "ui,project"
STAMP_NAME = ".vlogsrt-install-stamp"
STAMP_FORMAT = "launch-stamp/v1"
REQUIRED_TOOLS = ("ffmpeg", "ffprobe")
# 설치 확인용 — import하지 않고 찾기만 한다(find_spec). 핵심 셋 + ui·project extra + 이 꾸러미.
REQUIRED_MODULES = (
    "openai", "rich", "tomli_w", "gradio", "uvicorn", "numpy", "onnxruntime", "vlogsrt",
)  # fmt: skip

PROJECT_DIR = Path(__file__).resolve().parent.parent


def python_supported(version: Sequence[int]) -> bool:
    """이 Python으로 앱을 돌릴 수 있는가 — `pyproject.toml`의 `requires-python = ">=3.11"`."""
    return tuple(version[:2]) >= MIN_PYTHON


def install_stamp(pyproject: Path, extras: str = INSTALL_EXTRAS) -> str:
    """설치 표지 — pyproject.toml 바이트와 extra 목록이 바뀌면 다른 값이다."""
    digest = hashlib.sha256(pyproject.read_bytes()).hexdigest()
    return f"{STAMP_FORMAT} extras={extras} pyproject-sha256={digest}\n"


def stamp_path(project_dir: Path) -> Path:
    return project_dir / ".venv" / STAMP_NAME


def stamp_current(project_dir: Path) -> bool:
    """표지가 있고 지금 pyproject.toml로 계산한 값과 같은가. 없거나 다르면 다시 설치한다."""
    path = stamp_path(project_dir)
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8") == install_stamp(project_dir / "pyproject.toml")


def write_stamp(project_dir: Path) -> Path:
    path = stamp_path(project_dir)
    path.write_text(install_stamp(project_dir / "pyproject.toml"), encoding="utf-8")
    return path


def missing_modules() -> list[str]:
    return [name for name in REQUIRED_MODULES if importlib.util.find_spec(name) is None]


def missing_tools() -> list[str]:
    return [tool for tool in REQUIRED_TOOLS if shutil.which(tool) is None]


def tools_message(missing: Sequence[str], platform: str) -> str:
    """빠진 도구를 알리는 한국어 안내 — 운영체제마다 설치 명령이 다르다."""
    lines = [f"찾지 못한 것: {', '.join(missing)}"]
    if platform == "darwin":
        lines.append("ffmpeg가 필요합니다: brew install ffmpeg")
    elif platform == "win32":
        lines.append("ffmpeg가 필요합니다: winget install Gyan.FFmpeg")
        # 이미 열린 창은 설치가 바꾼 PATH를 모른다 — 같은 창에서 다시 실행하면 또 못 찾는다.
        lines.append("설치한 뒤에는 이 창을 닫고 새 창에서 다시 실행하세요 (PATH가 새로 읽힙니다).")
    else:
        lines.append(
            "ffmpeg가 필요합니다: 꾸러미 관리자로 설치하세요 (예: sudo apt install ffmpeg)"
        )
    return "\n".join(lines)


def _check_tools() -> int:
    missing = missing_tools()
    if not missing:
        return 0
    print(tools_message(missing, sys.platform), file=sys.stderr)
    return 1


def _check_importable() -> int:
    missing = missing_modules()
    if not missing:
        return 0
    print(f"설치되지 않은 꾸러미: {', '.join(missing)}", file=sys.stderr)
    return 1


def _write_stamp() -> int:
    write_stamp(PROJECT_DIR)
    return 0


def advice_lines(platform: str, tkinter_present: bool) -> list[str]:
    """막지 않는 안내 — macOS는 선택 창을 AppleScript로 연다(tkinter가 필요 없다, D106).

    다른 운영체제의 「찾아보기」는 tkinter 자식 프로세스다. 없으면 앱이 단추를 감추고 경로 입력만
    남긴다(W-46a) — 그래서 막지 않고 까는 명령만 알린다."""
    if platform == "darwin" or tkinter_present:
        return []
    lines = ["「찾아보기」 선택 창에 tkinter가 필요합니다 — 없으면 경로를 직접 입력하세요."]
    if platform == "win32":
        lines.append("python.org 설치 프로그램의 「tcl/tk and IDLE」을 켜고 다시 설치하세요.")
    else:
        lines.append("예: sudo apt install python3-tk (Fedora: sudo dnf install python3-tkinter)")
    return lines


def _advice() -> int:
    present = importlib.util.find_spec("tkinter") is not None
    for line in advice_lines(sys.platform, present):
        print(line, file=sys.stderr)
    return 0


COMMANDS: dict[str, Callable[[], int]] = {
    "stamp-ok": lambda: 0 if stamp_current(PROJECT_DIR) else 1,
    "write-stamp": _write_stamp,
    "importable": _check_importable,
    "tools": _check_tools,
    "advice": _advice,
}


def main(argv: Sequence[str]) -> int:
    if not python_supported(sys.version_info):
        print("Python 3.11 이상이 필요합니다.", file=sys.stderr)
        return 1
    if len(argv) != 1 or argv[0] not in COMMANDS:
        print(f"사용법: launch_check.py {{{'|'.join(COMMANDS)}}}", file=sys.stderr)
        return 2
    return COMMANDS[argv[0]]()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
