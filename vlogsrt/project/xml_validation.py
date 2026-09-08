"""xmllint-only XML 검증 경계 — XML을 향한 유일한 외부 프로세스다.

얼어 붙은 계약(docs/orchestration/tasks/p1-04-xml-validation.md)을
`scripts/check_invariants.py`가 **형태로** 검사하고, 이 모듈이 **행동으로** 지킨다:

- 실행파일은 `shutil.which("xmllint")`로만 얻는다. 다른 검증기·별칭이 없다.
- 호출은 `subprocess.run` 하나, `shell=False`, `executable=` 없음, argv 조립 없음.
- argv는 정확히 두 형태뿐이다(정해진 것 외에는 어떤 깃발도 붙지 않는다)::

      [xmllint, "--nonet", "--noout", path]
      [xmllint, "--nonet", "--noout", "--dtdvalid", dtd_uri, path]

DTD를 `Path.resolve().as_uri()`로 넘기는 것은 취향이 아니라 실측이다: 설치된
Final Cut Pro DTD 경로에는 빈칸이 있고("Final Cut Pro.app"), `--dtdvalid`에
평범한 경로를 주면 xmllint가 DTD를 아예 읽지 못한다(종료 코드 2). URI의 %20
인코딩만이 빈칸을 하나의 argv 칸에 온전히 보존한다. `--nonet`은 그 DTD가 외부
스키마를 참조하더라도 가져오지 못하게 막는다.

"없다"와 "틀리다"를 섞지 않는다: 도구나 요청한 DTD가 없으면
`XmlValidationUnavailable` — 판정을 내린 적이 없다. xmllint가 0이 아닌 코드로
끝나면 예외 대신 `valid=False`와 진단을 돌려준다 — 진단을 버리는 예외가 더 나쁘다.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# 진단의 상한 — stderr을 그대로 흘려보내면 악의적인 문서 하나가 로그를 채운다.
_MAX_DIAGNOSTIC_LINES = 8
_MAX_DIAGNOSTIC_LINE_CHARS = 400

# C0/C1 제어 문자와 DEL — xmllint 진단에 실려 올 이유가 없고, 왔으면 걷어낸다.
_CONTROL_CHARACTERS = dict.fromkeys([*range(0x20), 0x7F, *range(0x80, 0xA0)], None)

_TRUNCATION_MARK = "…"


@dataclass(frozen=True, slots=True)
class XmlValidation:
    """xmllint 판정의 불변 기록 — 검증은 표시할 뿐, 무언가를 고치지 않는다."""

    valid: bool
    diagnostics: tuple[str, ...]
    dtd_checked: bool


class XmlValidationUnavailable(RuntimeError):
    """xmllint 또는 요청한 DTD가 없다 — 판정 자체를 내릴 수 없는 환경이다."""


def validate_xml(path: Path, *, dtd_path: Path | None = None) -> XmlValidation:
    """`path`의 XML을 xmllint로 검증한다. `dtd_path`를 주면 DTD 대조까지 한다.

    `path` 자체의 존재는 xmllint에게 맡긴다 — 없는 파일은 0이 아닌 종료 코드와
    진단으로 돌아온다. 이 경계가 예외를 내는 것은 환경뿐이다(도구·DTD).
    """
    xmllint = shutil.which("xmllint")
    if xmllint is None:
        raise XmlValidationUnavailable("xmllint이 없다 — PATH에서 찾을 수 없다")
    if dtd_path is None:
        completed = subprocess.run(
            [xmllint, "--nonet", "--noout", str(path)],
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        dtd_checked = False
    else:
        completed = subprocess.run(
            [xmllint, "--nonet", "--noout", "--dtdvalid", _dtd_uri(dtd_path), str(path)],
            shell=False,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
        )
        dtd_checked = True
    return XmlValidation(
        valid=completed.returncode == 0,
        diagnostics=_diagnostic_lines(completed.stderr),
        dtd_checked=dtd_checked,
    )


def _dtd_uri(dtd_path: Path) -> str:
    """DTD를 파일 URI로 — 요청한 파일이 없으면 판정을 내릴 수 없다."""
    resolved = dtd_path.resolve()
    if not resolved.is_file():
        raise XmlValidationUnavailable(f"DTD 파일이 없다: {dtd_path}")
    return resolved.as_uri()


def _diagnostic_lines(stderr: str) -> tuple[str, ...]:
    """xmllint의 stderr을 정돈된 몇 줄로 — 제어 문자를 걷어내고 상한을 지킨다."""
    cleaned = [
        line for raw in stderr.splitlines() if (line := raw.translate(_CONTROL_CHARACTERS).strip())
    ]
    if len(cleaned) <= _MAX_DIAGNOSTIC_LINES:
        return tuple(_bounded(line) for line in cleaned)
    kept = [_bounded(line) for line in cleaned[: _MAX_DIAGNOSTIC_LINES - 1]]
    kept.append(f"{_TRUNCATION_MARK} 진단 {len(cleaned) - len(kept)}줄이 잘렸다")
    return tuple(kept)


def _bounded(line: str) -> str:
    """한 줄의 상한 — 잘리면 표지를 붙인다(길이는 상한 + 표지 1글자)."""
    if len(line) <= _MAX_DIAGNOSTIC_LINE_CHARS:
        return line
    return line[:_MAX_DIAGNOSTIC_LINE_CHARS] + _TRUNCATION_MARK
