"""설치된 개발 래퍼의 회귀 시험 — 공유 파이썬 진입점에 위임한다(S1A).

래퍼는 흐름을 두 벌로 갖지 않는다: 번들 엔진과 같은 `vlogsrt-engine.py`를 부른다.
여기서는 (1) 위임 argv 계약을 가짜 인터프리터로 못 박고 (2) 실제 저장소
파이썬으로 설치된 래퍼 전체를 새 임시 job에서 돌려 스테이징·재개가 번들 엔진과
같은지를 본다. 진입점 자체의 세부는 test_engine_entrypoint.py가 든다.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from test_engine_entrypoint import _GRANT, _staged_cancellation

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "macos" / "scripts" / "install-development-engine.sh"


def _install(tmp_path: Path, *, repo: Path = REPO) -> Path:
    destination = tmp_path / "scripts"
    subprocess.run([str(INSTALLER), str(repo), str(destination)], check=True)
    return destination / "vlogsrt-engine"


def _fake_repo_with_logging_python(tmp_path: Path) -> Path:
    """가짜 인터프리터 — 받은 argv를 기록한다. 엔진 내용은 해석하지 않는다."""
    repo = tmp_path / "repo"
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='fake'\n", encoding="utf-8")
    python.write_text(
        '#!/bin/zsh\nprint -r -- "$@" >> "${0:A:h:h:h}/calls.log"\n',
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    return repo


def test_installed_wrapper_delegates_both_operations_to_the_shared_entrypoint(
    tmp_path: Path,
) -> None:
    """래퍼는 `<operation> <job_id> <job_dir>`을 공유 진입점에 그대로 넘긴다."""
    repo = _fake_repo_with_logging_python(tmp_path)
    wrapper = _install(tmp_path, repo=repo)
    job = tmp_path / "job"
    job.mkdir()
    log = repo / "calls.log"
    engine_script = str(repo / "macos/scripts/vlogsrt-engine.py")

    for operation in ("analyze", "roughcut"):
        completed = subprocess.run(
            [str(wrapper), operation, "job-1", str(job)],
            check=False,
            env={**os.environ, "VLOGSRT_OFFLINE": "1"},
        )
        assert completed.returncode == 0
    calls = log.read_text(encoding="utf-8").splitlines()
    assert calls == [
        f"{engine_script} analyze job-1 {job}",
        f"{engine_script} roughcut job-1 {job}",
    ]

    # 계약 밖 호출은 파이썬을 띄우지 않고 2로 닫는다.
    assert (
        subprocess.run(
            [str(wrapper), "analyze", "a/b", str(job)],
            check=False,
            env={**os.environ, "VLOGSRT_OFFLINE": "1"},
        ).returncode
        == 2
    )
    assert len(log.read_text(encoding="utf-8").splitlines()) == 2


def test_installed_wrapper_runs_the_real_engine_on_a_fresh_job(tmp_path: Path) -> None:
    """설치된 래퍼 → 실제 파이썬 → 실제 엔진까지 오프라인으로 잇는다.

    미디어 권한·취소 기록을 스테이징한 새 job에서 receive·inspect가 끝나고,
    재개 실행 때 이미 있는 workspace 기록을 덮어쓰지 않는다(번들 엔진과 같은 순서).
    """
    from test_engine_entrypoint import _stage_job

    wrapper = _install(tmp_path)
    job_dir = _stage_job(tmp_path)
    (job_dir / "media-grant.json").write_text(_GRANT, encoding="utf-8")
    staged = _staged_cancellation("b" * 64)
    (job_dir / "cancellation.jsonl").write_text(staged, encoding="utf-8")
    workspace = job_dir / "workspace"
    assert not workspace.exists()

    completed = subprocess.run(
        [str(wrapper), "analyze", "job-1", str(job_dir)],
        check=False,
        env={**os.environ, "VLOGSRT_OFFLINE": "1"},
    )
    assert completed.returncode == 0
    assert (workspace / "workflow.jsonl").is_file()
    assert (job_dir / "analysis-estimate.jsonl").is_file()
    assert (workspace / "media-grant.json").read_text(encoding="utf-8") == _GRANT
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == staged

    # 재개: 앱이 실행 중 새로 남긴 루트 기록이 workspace 기록을 덮어쓰지 않는다.
    (job_dir / "cancellation.jsonl").write_text(_staged_cancellation("c" * 64), encoding="utf-8")
    assert (
        subprocess.run(
            [str(wrapper), "analyze", "job-1", str(job_dir)],
            check=False,
            env={**os.environ, "VLOGSRT_OFFLINE": "1"},
        ).returncode
        == 0
    )
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == staged
