"""`make check`를 파이썬 하나로 — Windows에는 make가 없다(W-84, D144).

Makefile의 `check: lint types test invariant`와 같은 단계·같은 명령을 같은 차례로
돈다. 처음 실패에서 멈추고 그 종료 코드를 돌려준다(make와 같다). 시험
(`tests/test_check_all.py`)이 Makefile을 읽어 두 목록이 같은지 대조한다 — 한쪽만
고치면 시험이 깨진다.

    python scripts/check_all.py            # 전부
    python scripts/check_all.py lint test  # 고른 단계만(차례는 Makefile 차례)

Windows에서 mypy는 그 운영체제를 기본 플랫폼으로 본다 — 이 맥에서 `make types-win`
(`--platform win32`)이 보던 분기를 Windows 실행이 그대로 본다.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ["vlogsrt", "ui", "tests", "scripts"]

# (단계, 명령들) — 명령은 `python` 뒤의 인자다. Makefile의 `$(PY)` 자리에 이 파이썬이 들어간다.
STEPS: tuple[tuple[str, tuple[tuple[str, ...], ...]], ...] = (
    (
        "lint",
        (
            ("-m", "ruff", "check", *TARGETS),
            ("-m", "ruff", "format", "--check", *TARGETS),
        ),
    ),
    ("types", (("-m", "mypy"),)),
    ("test", (("-m", "pytest"),)),
    ("invariant", (("scripts/check_invariants.py",),)),
)


def selected(names: list[str]) -> list[tuple[str, tuple[tuple[str, ...], ...]]]:
    """고른 단계만, Makefile 차례로. 모르는 이름은 거부한다."""
    known = {name for name, _ in STEPS}
    unknown = [name for name in names if name not in known]
    if unknown:
        raise SystemExit(f"모르는 단계: {', '.join(unknown)} (있는 것: {', '.join(known)})")
    return [step for step in STEPS if not names or step[0] in names]


def main(argv: list[str] | None = None) -> int:
    names = list(sys.argv[1:] if argv is None else argv)
    for name, commands in selected(names):
        for args in commands:
            command = [sys.executable, *args]
            sys.stdout.write(f"[{name}] {' '.join(args)}\n")
            sys.stdout.flush()
            code = subprocess.run(command, cwd=ROOT, check=False).returncode
            if code:
                sys.stderr.write(f"[{name}] 실패 — 종료 코드 {code}\n")
                return code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
