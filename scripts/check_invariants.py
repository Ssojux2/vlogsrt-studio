#!/usr/bin/env python3
"""CLAUDE.md의 불변 규칙을 기계적으로 검사한다.

사람 눈보다 싸고 잊지 않는다. `make check`가 이걸 부른다.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "vlogsrt"

BANNED_IMPORTS = (
    "jamo", "konlpy", "pysrt", "ffmpeg", "pydantic", "dotenv", "regex", "numpy",
    # UI는 ui/ 에만 산다. 핵심 패키지가 gradio를 끌어오면 규칙 7이 무너진다.
    "gradio",
)  # fmt: skip
ALLOWED_DEPENDENCIES = {"openai", "tomli-w", "rich"}


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def check_openai_boundary() -> list[str]:
    """규칙 5 — API 호출은 transcribe.py에서만."""
    failures = []
    for path in _sources():
        if path.name == "transcribe.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.match(r"\s*(import openai|from openai)", line):
                failures.append(
                    f"{path.relative_to(ROOT)}:{number} openai import는 transcribe.py에만"
                )
    return failures


def check_autojunk() -> list[str]:
    """규칙 4 — SequenceMatcher는 항상 autojunk=False."""
    failures = []
    for path in _sources():
        body = path.read_text(encoding="utf-8")
        for match in re.finditer(r"SequenceMatcher\((?P<args>[^)]*)\)", body, re.DOTALL):
            if "autojunk=False" not in match.group("args"):
                line = body[: match.start()].count("\n") + 1
                failures.append(
                    f"{path.relative_to(ROOT)}:{line} SequenceMatcher에 autojunk=False 없음"
                )
    return failures


def check_banned_imports() -> list[str]:
    """규칙 7 — 의존성은 openai, tomli-w, rich 뿐."""
    failures = []
    pattern = re.compile(rf"^\s*(?:import|from)\s+({'|'.join(BANNED_IMPORTS)})\b")
    for path in _sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = pattern.match(line)
            if match:
                failures.append(f"{path.relative_to(ROOT)}:{number} 금지된 의존성 {match.group(1)}")
    return failures


def check_dependencies() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        declared = tomllib.load(handle)["project"]["dependencies"]
    names = {re.split(r"[<>=\[ ]", entry)[0] for entry in declared}
    if names != ALLOWED_DEPENDENCIES:
        return [f"pyproject 의존성이 {sorted(names)}. 허용: {sorted(ALLOWED_DEPENDENCIES)}"]
    return []


def check_frozen_models() -> list[str]:
    """규칙 2 — spine은 수정 불가. 데이터클래스가 frozen이어야 강제된다."""
    body = (PACKAGE / "models.py").read_text(encoding="utf-8")
    failures = []
    for name in ("Segment", "Cue", "Trace"):
        pattern = rf"@dataclass\(frozen=True[^)]*\)\s*\nclass {name}\b"
        if not re.search(pattern, body):
            failures.append(f"models.py: {name}이 frozen dataclass가 아니다")
    return failures


def check_env_untouched() -> list[str]:
    """`.env`는 읽지도 고치지도 않는다 — 코드가 경로를 하드코딩해 덮어쓰지 않는지 본다."""
    failures = []
    for path in _sources():
        body = path.read_text(encoding="utf-8")
        if re.search(r"write_text\([^)]*\)\s*#\s*\.env", body):
            failures.append(f"{path.relative_to(ROOT)}: .env에 쓰기 시도")
    return failures


DELETED_MODULES = {
    "mapping.py": "D19 — 사상(mapping)",
    "gapfill.py": "D19 — 놓친 구간 되돌리기",
    "verify.py": "D21 — 교차 검증",
    "loops.py": "D21 — 되풀이 강등",
    "validate.py": "D21 — 정제 결과 검사",
    "report.py": "D21 — 검토 리포트",
}


def check_inspection_stays_dead() -> list[str]:
    """규칙 4 — 검사·재검증 계층은 의도적으로 삭제했다. 부활은 설계 위반이다."""
    failures = []
    for name, why in DELETED_MODULES.items():
        if (PACKAGE / name).exists():
            failures.append(f"vlogsrt/{name}: 삭제된 계층이 부활했다 ({why})")
    return failures


PURE_MODULES = ("spans.py", "align.py")


def check_pure_layer() -> list[str]:
    """규칙 5 — 시각을 만드는 계층은 순수해야 한다.

    `spans.py`가 ffmpeg를 직접 부르기 시작하면 무음 임계값을 바꿔가며 시험할 수
    없게 되고, `align.py`가 API를 알면 배분을 테스트할 수 없게 된다.
    """
    failures = []
    forbidden = re.compile(r"^\s*(?:import|from)\s+(openai|subprocess|shutil)\b")
    for name in PURE_MODULES:
        path = PACKAGE / name
        if not path.exists():
            failures.append(f"vlogsrt/{name}: 순수 계층이 사라졌다 (D24)")
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = forbidden.match(line)
            if match:
                where = f"vlogsrt/{name}:{number}"
                failures.append(f"{where} 순수 계층이 {match.group(1)}을 import한다")
    return failures


def check_ffmpeg_boundary() -> list[str]:
    """규칙 5 — ffmpeg 호출은 preprocess.py에서만."""
    failures = []
    for path in _sources():
        if path.name == "preprocess.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.match(r"\s*(import subprocess|from subprocess)", line):
                failures.append(f"{path.relative_to(ROOT)}:{number} subprocess는 preprocess.py에만")
    return failures


def check_times_do_not_come_from_the_model() -> list[str]:
    """규칙 1 — 시각은 오디오에서 온다. 응답에서 시각을 읽던 코드가 남으면 안 된다(D24)."""
    body = (PACKAGE / "transcribe.py").read_text(encoding="utf-8")
    failures = []
    for ghost in ("parse_segments", "seg_id_base", "verbose_json", "diarized_json"):
        if ghost in body:
            failures.append(f"transcribe.py: 모델이 시각을 주던 시절의 잔재 {ghost!r}")
    return failures


def check_one_transcription_call() -> list[str]:
    """규칙 1 — 모델은 하나다. 호출 지점이 둘이면 출처가 둘이라는 뜻이다."""
    body = (PACKAGE / "transcribe.py").read_text(encoding="utf-8")
    calls = re.findall(r"transcriptions\.create\(", body)
    if len(calls) != 1:
        return [f"transcribe.py: transcriptions.create 호출이 {len(calls)}개다 (1개여야 한다)"]
    return []


def check_no_hardcoded_model() -> list[str]:
    """규칙 6 — 모델 이름도 config에서 온다. 코드에 박으면 되돌리기가 커밋이 된다."""
    failures = []
    for path in _sources():
        if path.name == "config.py":
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"[\"'](whisper-1|gpt-[\w.-]*transcribe[\w.-]*)[\"']", line):
                failures.append(f"{path.relative_to(ROOT)}:{number} 모델 이름이 코드에 박혀 있다")
    return failures


CHECKS = (
    ("openai 경계", check_openai_boundary),
    ("ffmpeg 경계", check_ffmpeg_boundary),
    ("순수 계층", check_pure_layer),
    ("시각은 오디오에서", check_times_do_not_come_from_the_model),
    ("autojunk=False", check_autojunk),
    ("금지 의존성", check_banned_imports),
    ("의존성 3개", check_dependencies),
    ("frozen 모델", check_frozen_models),
    (".env 보호", check_env_untouched),
    ("검사 계층 부활 금지", check_inspection_stays_dead),
    ("전사 호출 1개", check_one_transcription_call),
    ("모델 이름 비하드코딩", check_no_hardcoded_model),
)


def main() -> int:
    failures: list[str] = []
    for name, check in CHECKS:
        found = check()
        status = "OK" if not found else "FAIL"
        print(f"[{status}] {name}")
        failures.extend(found)
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
