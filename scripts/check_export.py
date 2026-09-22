#!/usr/bin/env python3
"""내보낸 .fcpxml을 파서에 먹여 거부 이유를 그대로 받아 적는다. 코드를 안 고친다."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from vlogsrt.project.fcpxml import UnsupportedProject, parse_project  # noqa: E402

if len(sys.argv) != 2:
    raise SystemExit("사용법: python check_export.py <내보낸.fcpxml>")

path = Path(sys.argv[1]).expanduser()
try:
    document = parse_project(path)
except UnsupportedProject as exc:
    print("거부됐다")
    print(f"  자리 : {exc.location}")
    print(f"  이유 : {exc.reason}")
    raise SystemExit(1) from None
except Exception as exc:  # noqa: BLE001 - 어떤 실패든 그대로 보여준다
    print(f"다른 예외: {type(exc).__name__}: {exc}")
    raise SystemExit(2) from None

clips = [item for item in document.storyline if type(item).__name__ != "GapInstance"]
print("통과했다")
print(
    f"  리소스 {len(document.resources)}개 · 스토리라인 {len(document.storyline)}항목 "
    f"(클립 {len(clips)}개)"
)
