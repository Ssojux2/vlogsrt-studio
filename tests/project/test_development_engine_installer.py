from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path


def test_installed_engine_wrapper_runs_inspect_and_returns_success(tmp_path: Path) -> None:
    """설치된 래퍼가 없는 module entrypoint와 선택 파일 부재 때문에 실패하는 회귀를 잡는다."""
    repo = tmp_path / "repo"
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='fake'\n", encoding="utf-8")
    log = repo / "calls.log"
    python.write_text(
        "#!/bin/zsh\n"
        'print -r -- "$@" >> "${0:A:h:h:h}/calls.log"\n'
        "workspace=${@: -1}\n"
        "if [[ \"$*\" == *' project receive '* ]]; then\n"
        '  mkdir -p "$workspace"; print x > "$workspace/workflow.jsonl"\n'
        "fi\n"
        "if [[ \"$*\" == *' project inspect '* ]]; then\n"
        '  mkdir -p "$workspace"; print estimate > "$workspace/analysis-estimate.jsonl"\n'
        "fi\n"
        "if [[ \"$*\" == *' project analyze '* ]]; then\n"
        '  print candidate > "$5/candidates.jsonl"\n'
        "fi\n"
        "if [[ \"$*\" == *' project review '* ]]; then\n"
        '  print plan > "$5/edit-plan.jsonl"\n'
        "fi\n"
        "if [[ \"$*\" == *' project roughcut '* ]]; then\n"
        '  print roughcut > "$7"\n'
        "fi\n",
        encoding="utf-8",
    )
    python.chmod(python.stat().st_mode | stat.S_IXUSR)
    destination = tmp_path / "scripts"
    installer = Path("macos/scripts/install-development-engine.sh").resolve()
    subprocess.run([str(installer), str(repo), str(destination)], check=True)
    wrapper = destination / "vlogsrt-engine"
    job = tmp_path / "job"
    job.mkdir()
    (job / "input.fcpxml").write_text("<fcpxml/>", encoding="utf-8")
    completed = subprocess.run(
        [str(wrapper), "analyze", "job-1", str(job)],
        check=False,
        env={**os.environ, "VLOGSRT_OFFLINE": "1"},
    )
    assert completed.returncode == 0
    calls = log.read_text(encoding="utf-8")
    assert "-m vlogsrt.cli project receive" in calls
    assert "-m vlogsrt.cli project inspect" in calls
    assert (job / "analysis-estimate.jsonl").is_file()

    (job / "analysis-authorization.jsonl").write_text("authorization", encoding="utf-8")
    subprocess.run([str(wrapper), "analyze", "job-1", str(job)], check=True)
    assert (job / "candidates.jsonl").is_file()

    approved = "c" * 64
    (job / "approved-candidates.txt").write_text(approved + "\n", encoding="utf-8")
    subprocess.run([str(wrapper), "roughcut", "job-1", str(job)], check=True)
    calls = log.read_text(encoding="utf-8")
    assert f"project review {job / 'workspace'} --approve {approved}" in calls
    assert "-m vlogsrt.cli project roughcut" in calls
    assert (job / "roughcut.fcpxml").is_file()
