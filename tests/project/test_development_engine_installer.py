"""설치된 개발 래퍼의 회귀 시험 — 공유 파이썬 진입점에 위임한다(S1A).

래퍼는 흐름을 두 벌로 갖지 않는다: 번들 엔진과 같은 `vlogsrt-engine.py`를 부른다.
여기서는 (1) 위임 argv 계약을 가짜 인터프리터로 못 박고 (2) 실제 파이썬으로
설치된 래퍼 전체를 새 임시 job에서 돌려 스테이징·재개가 번들 엔진과 같은지를
본다. (2)의 저장소는 **합성 sandbox**다 — 래퍼가 `cd <repo>` 후 dotenv 로더를
거치므로, 공개 파이썬 소스와 바이트 동일 엔진 스크립트만 복사해 넣고 dotenv는
시험이 지은 오프라인 합성물 하나뿐이다(STAB-TEST-ISOLATION). 진입점 자체의
세부는 test_engine_entrypoint.py가 든다.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

from test_engine_entrypoint import _GRANT, _stage_job, _staged_cancellation

from tests._dotenv_guard import (
    CANARY_VALUE,
    build_engine_sandbox,
    preflight_child_guard,
    read_child_proofs,
    stage_canary_standin,
)

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "macos" / "scripts" / "install-development-engine.sh"


def _install(tmp_path: Path, *, repo: Path) -> Path:
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
    """설치된 래퍼 → 실제 파이썬 → 실제 엔진까지 오프라인으로 잇는다 — 합성 저장소 안에서.

    미디어 권한·취소 기록을 스테이징한 새 job에서 receive·inspect가 끝나고,
    재개 실행 때 이미 있는 workspace 기록을 덮어쓰지 않는다(번들 엔진과 같은 순서).
    래퍼는 `cd <repo>` 후 진입점을 부르므로 저장소는 sandbox여야 한다: cwd·
    PYTHONPATH·PACKAGE_ROOT가 전부 임시 안에 갇혀 실제 저장소의 보호된 `.env`로
    흐를 길이 없다(S2A-1 브리지 시험과 같은 원리).
    """
    sandbox = build_engine_sandbox(tmp_path, source=REPO)
    assert sandbox.dotenv_files() == [sandbox.dotenv]  # 보호 철자는 합성 dotenv 하나뿐이다
    assert sandbox.symlinks() == []  # 저장소 원본을 향한 심볼링크는 없다
    standin = stage_canary_standin(tmp_path / "sealed")
    wrapper = _install(tmp_path, repo=sandbox.repo)
    job_dir = _stage_job(tmp_path)
    (job_dir / "media-grant.json").write_text(_GRANT, encoding="utf-8")
    staged = _staged_cancellation("b" * 64)
    (job_dir / "cancellation.jsonl").write_text(staged, encoding="utf-8")
    workspace = job_dir / "workspace"
    assert not workspace.exists()

    # 자식 보호(sitecustomize)가 이 환경에 함께 흘러간다 — 증명은 임시 안에 남는다.
    # PYTHONPATH는 래퍼가 export하는 모양 그대로 앞에 붙인다 — canary가 실제 자식과
    # 같은 수입 경로로 sitecustomize를 찾아야 사전확인이 같은 세계를 본다.
    guard_env = sandbox.guard_env(standin=standin)
    inherited = os.environ.get("PYTHONPATH", "")
    guarded_env = {
        **os.environ,
        "VLOGSRT_OFFLINE": "1",
        "PYTHONPATH": str(sandbox.repo) + (os.pathsep + inherited if inherited else ""),
        **guard_env,
    }
    # 보호 사전확인 게이트 — 설치된 래퍼를 띄우기 전에 canary로 증명한다.
    preflight_child_guard(guarded_env, cwd=sandbox.repo, expected_package_root=sandbox.package)
    completed = subprocess.run(
        [str(wrapper), "analyze", "job-1", str(job_dir)],
        check=False,
        env=guarded_env,
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
            env=guarded_env,
        ).returncode
        == 0
    )
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == staged

    # 자식 증명 — 보호 작동, cwd·수입 출처 전부 합성 저장소 안이다(카나리는 없다).
    proofs = read_child_proofs(sandbox.proof)
    assert proofs, "자식 보호 증명이 없다 — sitecustomize가 자식에 실리지 않았다"
    for record in proofs:
        assert record["blocked_read"] is True
        assert record["blocked_read_bytes"] is True
        assert record["blocked_path_open"] is True
        assert record["blocked_io_open"] is True
        assert record["blocked_os_open"] is True
        assert record["probe_miss"] is True
        assert record["socket_refused"] is True
        assert record["connect_refused"] is True
        assert record["cwd"] == str(sandbox.repo.resolve())
        assert record["vlogsrt_file"].startswith(str(sandbox.package.resolve()))
    assert CANARY_VALUE not in sandbox.proof.read_text(encoding="utf-8")
