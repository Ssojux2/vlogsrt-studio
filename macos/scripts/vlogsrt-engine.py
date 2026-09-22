#!/usr/bin/env python3
"""번들 엔진 진입점 — 개발 래퍼(install-development-engine.sh)과 같은 CLI 계약.

    vlogsrt-engine <operation> <job_id> <job_dir>

PyInstaller onedir로 얼릴 때 이 파일이 진입점이다(scripts/build_engine.py).
개발 래퍼와 달리 파이썬을 다시 띄우지 않고 `vlogsrt.cli`를 직접 부른다.
API 키는 부모가 준 환경변수(VLOGSRT_ENGINE_API_KEY)로만 받는다 — argv에
키가 오는 경로는 없다. 받은 키는 엔진이 읽는 이름(OPENAI_API_KEY)으로
프로세스 안에서만 옮겨 담는다.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

_CANDIDATE_ID = re.compile(r"^[0-9a-f]{64}$")
_RESULT_FILES = (
    "analysis-estimate.jsonl",
    "analysis-attempts.jsonl",
    "analysis-usage.jsonl",
    "candidates.jsonl",
    "vision-decisions.jsonl",
)
_CARRIER = "VLOGSRT_ENGINE_API_KEY"
_ENGINE_KEY_NAME = "OPENAI_API_KEY"


def _carry_api_key() -> None:
    carrier = os.environ.get(_CARRIER, "")
    if carrier:
        os.environ.setdefault(_ENGINE_KEY_NAME, carrier)
    # 운반자 이름은 지운다 — 키가 두 이름으로 흐르지 않게.
    os.environ.pop(_CARRIER, None)


_STAGED_FILES = ("cancellation.jsonl", "media-grant.json")


def _propagate_staged(job_dir: Path, workspace: Path) -> None:
    """앱이 작업 폴더에 남긴 취소·미디어 권한 기록을 엔진 작업공간으로 옮긴다.

    엔진(Python)은 workspace 안만 본다 — 앱이 job 루트에 스테이징한 파일을
    여기서 옮겨 담는다. 없는 파일은 만들지 않는다.
    """
    for name in _STAGED_FILES:
        source = job_dir / name
        if not source.is_file():
            continue
        destination = workspace / name
        if not destination.exists():
            shutil.copyfile(source, destination)


def _run(argv: list[str]) -> int:
    from vlogsrt.cli import main as cli_main

    return int(cli_main(argv))


def _step(argv: list[str], *, when: bool = True) -> int:
    if when and _run(argv) != 0:
        return 1
    return 0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: vlogsrt-engine <operation> <job_id> <job_dir>", file=sys.stderr)
        return 2
    operation, job_id, job_dir_text = argv
    if "/" in job_id or ".." in job_id:
        return 2
    _carry_api_key()
    job_dir = Path(job_dir_text)
    workspace = job_dir / "workspace"
    _propagate_staged(job_dir, workspace)
    if _step(
        [
            "project",
            "receive",
            str(job_dir / "input.fcpxml"),
            "--workspace",
            str(workspace),
        ],
        when=not (workspace / "workflow.jsonl").is_file(),
    ):
        return 1
    if operation == "analyze":
        if _step(
            ["project", "inspect", str(workspace)],
            when=not (workspace / "analysis-estimate.jsonl").is_file(),
        ):
            return 1
        authorization = job_dir / "analysis-authorization.jsonl"
        if _step(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
            ],
            when=authorization.is_file(),
        ):
            return 1
    elif operation == "roughcut":
        review_args = ["project", "review", str(workspace)]
        approved = job_dir / "approved-candidates.txt"
        if approved.is_file():
            for candidate in approved.read_text(encoding="utf-8").splitlines():
                if not _CANDIDATE_ID.fullmatch(candidate):
                    return 2
                review_args += ["--approve", candidate]
        if _step(review_args):
            return 1
        if _step(["project", "roughcut", str(workspace), "-o", str(job_dir / "roughcut.fcpxml")]):
            return 1
    else:
        return 2
    for name in _RESULT_FILES:
        source = workspace / name
        if source.is_file():
            shutil.copyfile(source, job_dir / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
