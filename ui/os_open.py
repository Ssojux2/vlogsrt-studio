"""운영체제의 파일 관리자로 폴더를 열거나 항목을 보인다 — macOS·Windows·그 밖(D100 §1b).

macOS는 `open`(Finder), Windows는 탐색기, 그 밖은 `xdg-open`이다. 인자는 리스트로만 넘기고 셸을
쓰지 않는다. 실패는 사유를 실은 `OSError`다 — 화면이 그 문장을 그대로 보인다.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_MAC_OPEN = "/usr/bin/open"
_XDG_OPEN = "xdg-open"
_STDERR_EXCERPT = 200
"""화면에 옮기는 오류 출력의 최대 글자 수 — 예전 Finder 열기와 같다."""


def reveal(path: Path) -> None:
    """그 항목을 파일 관리자에서 골라 보인다 — 폴더면 그 부모 창에서 그 폴더가 골라진다."""
    if sys.platform == "darwin":
        _run([_MAC_OPEN, "-R", str(path)])
    elif sys.platform == "win32":
        # 탐색기는 성공해도 종료 코드 1을 낸다 — 코드로 실패를 가르지 않는다.
        subprocess.run([_explorer(), "/select,", str(path)], check=False)
    else:
        _run([_XDG_OPEN, str(path.parent)])  # xdg-open에는 「골라 보이기」가 없다


def open_folder(path: Path) -> None:
    """그 폴더를 파일 관리자 창으로 연다."""
    if sys.platform == "darwin":
        _run([_MAC_OPEN, str(path)])
    elif sys.platform == "win32":
        os.startfile(path)  # 셸 연결 그대로 연다 — 폴더는 탐색기 창이다
    else:
        _run([_XDG_OPEN, str(path)])


def _explorer() -> str:
    """`PATH`가 아니라 Windows 폴더의 탐색기 — 작업 폴더의 가짜 `explorer.exe`를 고르지 않는다."""
    return os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "explorer.exe")


def _run(argv: list[str]) -> None:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, check=False)
    except OSError as error:
        raise OSError(f"{argv[0]}을(를) 실행하지 못했다: {error}") from error
    if completed.returncode != 0:
        raise OSError((completed.stderr or "").strip()[:_STDERR_EXCERPT])
