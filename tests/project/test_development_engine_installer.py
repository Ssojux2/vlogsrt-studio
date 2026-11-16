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

import pytest
from test_engine_entrypoint import _grant_document, _stage_job, _staged_cancellation
from test_engine_execution_lock import _snapshot

from tests._dotenv_guard import (
    CANARY_VALUE,
    EngineSandbox,
    build_engine_sandbox,
    preflight_child_guard,
    read_child_proofs,
    stage_canary_standin,
)

# 개발 엔진(`macos/scripts/`)은 POSIX 전용이다 — `fcntl` 잠금 · zsh 설치 스크립트. Windows에는 그
# 엔진이 없다(작업대는 `vlogsrt/filelock.py`의 msvcrt 길을 쓴다, W-84a).
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 전용 개발 엔진(macos/scripts)")

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


def _synthetic_tools_bin(sandbox: EngineSandbox, *, variant: str = "one-audio") -> Path:
    """시험 소유 ffprobe — 실제 진입점을 지나는 시험의 새 전제(S2A-2).

    inspect가 실제 원본의 오디오 위상을 ffprobe로 확인하므로 합성 미디어를
    읽을 실행파일이 필요하다. 기본은 한 비디오·한 오디오 스트림; `variant`로
    위상 검사의 실제 경계를 흉내 낸다(multi/zero/fail). 보호 의미는 그대로다
    (PATH 에 이 디렉터리만 더한다).
    """
    bodies = {
        "one-audio": 'printf \'%s\\n\' \'{"streams": [{"codec_type": "video"},'
        ' {"codec_type": "audio"}]}\'',
        "multi-audio": 'printf \'%s\\n\' \'{"streams": [{"codec_type": "video"},'
        ' {"codec_type": "audio"}, {"codec_type": "audio"}]}\'',
        "zero-audio": 'printf \'%s\\n\' \'{"streams": [{"codec_type": "video"}]}\'',
        "fail": "echo 'synthetic probe failure' >&2; exit 1",
    }
    bin_dir = sandbox.base / f"synthetic tools {variant}"
    bin_dir.mkdir(exist_ok=True)
    script = "#!/bin/sh\n" + bodies[variant] + "\n"
    for tool in ("ffprobe", "ffmpeg"):
        probe = bin_dir / tool
        probe.write_text(script, encoding="utf-8")
        probe.chmod(0o755)
    return bin_dir


def _guarded_wrapper_env(
    sandbox: EngineSandbox, standin: Path, *, tools_variant: str = "one-audio"
) -> dict[str, str]:
    """래퍼가 export하는 모양 그대로 PYTHONPATH를 앞에 두고 보호 환경을 섞는다.

    canary가 실제 자식과 같은 수입 경로로 sitecustomize를 찾아야 사전확인이
    같은 세계를 본다. PATH는 시험 소유 도구 bin을 앞에 둔다(S2A-2 위상 확인).
    """
    inherited = os.environ.get("PYTHONPATH", "")
    prefix = str(sandbox.repo) + (os.pathsep + inherited if inherited else "")
    tools = str(_synthetic_tools_bin(sandbox, variant=tools_variant))
    return {
        **os.environ,
        "VLOGSRT_OFFLINE": "1",
        "PYTHONPATH": prefix,
        "PATH": tools + os.pathsep + os.environ.get("PATH", ""),
        **sandbox.guard_env(standin=standin),
    }


def _run_installed_wrapper(
    wrapper: Path,
    argv: list[str],
    *,
    sandbox: EngineSandbox,
    env: dict[str, str],
) -> subprocess.CompletedProcess[bytes]:
    """설치된 래퍼를 보호 사전확인 **뒤에만** 띄운다 — 이 모듈의 유일한 실행 길이다.

    첫 실행이든 재개든 이 helper를 지나며, canary가 보호 증명을 못 내면
    래퍼는 아예 뜨지 않는다(실패 닫힘).
    """
    preflight_child_guard(env, cwd=sandbox.repo, expected_package_root=sandbox.package)
    return subprocess.run([str(wrapper), *argv], check=False, env=env)


def test_installed_wrapper_runs_the_real_engine_on_a_fresh_job(tmp_path: Path) -> None:
    """설치된 래퍼 → 실제 파이썬 → 실제 엔진까지 오프라인으로 잇는다 — 합성 저장소 안에서.

    미디어 권한·취소 기록을 스테이징한 새 job에서 receive·inspect가 끝나고,
    재개 실행 때 이미 있는 workspace 기록을 덮어쓰지 않는다(번들 엔진과 같은 순서).
    래퍼는 `cd <repo>` 후 진입점을 부르므로 저장소는 sandbox여야 한다: cwd·
    PYTHONPATH·PACKAGE_ROOT가 전부 임시 안에 갇혀 실제 저장소의 보호된 `.env`로
    흐를 길이 없다(S2A-1 브리지 시험과 같은 원리). 첫 실행과 재개 모두 보호
    사전확인 게이트를 지난다.
    """
    sandbox = build_engine_sandbox(tmp_path, source=REPO)
    assert sandbox.dotenv_files() == [sandbox.dotenv]  # 보호 철자는 합성 dotenv 하나뿐이다
    assert sandbox.symlinks() == []  # 저장소 원본을 향한 심볼링크는 없다
    standin = stage_canary_standin(tmp_path / "sealed")
    guarded_env = _guarded_wrapper_env(sandbox, standin)
    wrapper = _install(tmp_path, repo=sandbox.repo)
    job_dir = _stage_job(tmp_path)
    grant = _grant_document(tmp_path)
    (job_dir / "media-grant.json").write_text(grant, encoding="utf-8")
    staged = _staged_cancellation("b" * 64)
    (job_dir / "cancellation.jsonl").write_text(staged, encoding="utf-8")
    workspace = job_dir / "workspace"
    assert not workspace.exists()

    completed = _run_installed_wrapper(
        wrapper, ["analyze", "job-1", str(job_dir)], sandbox=sandbox, env=guarded_env
    )
    assert completed.returncode == 0
    assert (workspace / "workflow.jsonl").is_file()
    assert not (job_dir / "analysis-estimate.jsonl").is_file()  # D110: 범위는 인쇄만 한다
    assert (workspace / "media-grant.json").read_text(encoding="utf-8") == grant
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == staged

    # 재개: 앱이 실행 중 새로 남긴 루트 기록이 workspace 기록을 덮어쓰지 않는다.
    (job_dir / "cancellation.jsonl").write_text(_staged_cancellation("c" * 64), encoding="utf-8")
    resumed = _run_installed_wrapper(
        wrapper, ["analyze", "job-1", str(job_dir)], sandbox=sandbox, env=guarded_env
    )
    assert resumed.returncode == 0
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


# ─────────────────────────────────────────────────────────────
#  STAB-TEST-ISOLATION — 설치 래퍼 길의 보호 게이트
# ─────────────────────────────────────────────────────────────


def _prepare_wrapped_sandbox_job(
    tmp_path: Path,
) -> tuple[EngineSandbox, Path, Path, dict[str, str]]:
    """게이트 시험이 쓰는 재료 — sandbox·래퍼·job·보호 환경을 한 자리에 묶는다."""
    sandbox = build_engine_sandbox(tmp_path, source=REPO)
    standin = stage_canary_standin(tmp_path / "sealed")
    env = _guarded_wrapper_env(sandbox, standin)
    wrapper = _install(tmp_path, repo=sandbox.repo)
    job = tmp_path / "engine-job"
    job.mkdir()
    (job / "input.fcpxml").write_text("<fcpxml/>\n", encoding="utf-8")
    return sandbox, wrapper, job, env


def test_wrapper_launch_gate_refuses_when_the_child_guard_bootstrap_is_missing(
    tmp_path: Path,
) -> None:
    """sitecustomize가 없으면 canary가 증명을 못 낸다 — 래퍼는 뜨지도 않는다.

    job 트리는 그대로다(workspace도 잠금 파일도 시도 기록도 없다) — 게이트가
    실제 실행 길(_run_installed_wrapper)에서 막는 것을 잠금 시험과 별개로 못 박는다.
    """
    sandbox, wrapper, job, env = _prepare_wrapped_sandbox_job(tmp_path)
    (sandbox.repo / "sitecustomize.py").unlink()
    before = _snapshot(job)

    with pytest.raises(RuntimeError, match="사전확인 실패"):
        _run_installed_wrapper(wrapper, ["analyze", "job-1", str(job)], sandbox=sandbox, env=env)
    assert _snapshot(job) == before
    assert not (job / "workspace").exists()
    assert not (job / ".vlogsrt-engine.lock").exists()


def test_wrapper_launch_gate_refuses_when_the_child_guard_bootstrap_is_broken_syntax(
    tmp_path: Path,
) -> None:
    """문법이 깨진 sitecustomize도 조용히 무시된다 — 게이트가 래퍼를 막는다."""
    sandbox, wrapper, job, env = _prepare_wrapped_sandbox_job(tmp_path)
    (sandbox.repo / "sitecustomize.py").write_text("def broken(:\n", encoding="utf-8")
    before = _snapshot(job)

    with pytest.raises(RuntimeError, match="사전확인 실패"):
        _run_installed_wrapper(wrapper, ["analyze", "job-1", str(job)], sandbox=sandbox, env=env)
    assert _snapshot(job) == before
    assert not (job / "workspace").exists()


def test_wrapper_launch_gate_refuses_editable_fallback_when_the_sandbox_package_is_missing(
    tmp_path: Path,
) -> None:
    """합성 패키지가 없으면 canary의 `import vlogsrt`가 편집 설치로 샌다 — 거부된다.

    수입 출처 증명이 곧 게이트다: vlogsrt가 sandbox 밖(실제 저장소)에서 오면
    payload는 띄우지 않는다. 보호는 이미 켜져 있고 보호 대상 파일은 조사하지
    않는다 — 이 시험은 실제 저장소 **원본**을 향한 실행 거부만 증명한다.
    """
    sandbox, wrapper, job, env = _prepare_wrapped_sandbox_job(tmp_path)
    sandbox.package.rename(sandbox.base / "package-hidden")  # 합성 패키지를 치운다
    before = _snapshot(job)

    with pytest.raises(RuntimeError, match="사전확인 실패"):
        _run_installed_wrapper(wrapper, ["analyze", "job-1", str(job)], sandbox=sandbox, env=env)
    assert _snapshot(job) == before
    assert not (job / "workspace").exists()


def _run_wrapper_captured(
    wrapper: Path, argv: list[str], *, sandbox: EngineSandbox, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """위상 경계 시험용 실행 — 사전확인 게이트는 그대로, 출력을 붙잡는다."""
    preflight_child_guard(env, cwd=sandbox.repo, expected_package_root=sandbox.package)
    return subprocess.run(
        [str(wrapper), *argv],
        cwd=sandbox.repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _stage_job_no_audio_declaration(tmp_path: Path) -> Path:
    """hasAudio=0으로 선언된 잡 — 선언이 실제 위상을 숨일 수 없음을 증명하는 재료."""
    job = tmp_path / "job-no-audio"
    job.mkdir()
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-topology-media")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip.mov" start="0s" duration="600s" hasAudio="0"'
        f' format="r1">\n      <media-rep kind="original-media" src="file://{media}"/>\n'
        '    </asset>\n  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="위상" uid="PROJECT-TOPOLOGY">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s" duration="12s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    (job / "input.fcpxml").write_text(body, encoding="utf-8")
    return job


def test_real_engine_rejects_multi_audio_despite_a_zero_audio_declaration(
    tmp_path: Path,
) -> None:
    """실제 래퍼 → 실제 진입점 → 실제 위상 관문: hasAudio=0 선언 뒤의 실제
    스트림 둘은 거절된다 — 추정·후보는 남지 않는다(유료 호출 0회)."""
    sandbox = build_engine_sandbox(tmp_path, source=REPO)
    standin = stage_canary_standin(tmp_path / "sealed")
    guarded_env = _guarded_wrapper_env(sandbox, standin, tools_variant="multi-audio")
    wrapper = _install(tmp_path, repo=sandbox.repo)
    job_dir = _stage_job_no_audio_declaration(tmp_path)

    completed = _run_wrapper_captured(
        wrapper, ["analyze", "job-1", str(job_dir)], sandbox=sandbox, env=guarded_env
    )

    assert completed.returncode != 0
    assert "오디오 스트림" in completed.stderr
    assert "믹스다운" in completed.stderr
    assert not (job_dir / "analysis-estimate.jsonl").is_file()
    assert not (job_dir / "candidates.jsonl").is_file()
    _assert_engine_sandbox_hygiene(sandbox)


def test_real_engine_accepts_zero_and_single_audio_topologies(tmp_path: Path) -> None:
    """0스트림·1스트림은 실제 진입점에서 계속 지원된다 — 추정이 나온다."""
    sandbox = build_engine_sandbox(tmp_path, source=REPO)
    standin = stage_canary_standin(tmp_path / "sealed")

    zero_env = _guarded_wrapper_env(sandbox, standin, tools_variant="zero-audio")
    wrapper = _install(tmp_path, repo=sandbox.repo)
    zero_job = _stage_job(tmp_path)
    zero = _run_wrapper_captured(
        wrapper, ["analyze", "job-1", str(zero_job)], sandbox=sandbox, env=zero_env
    )
    assert zero.returncode == 0
    assert (zero_job / "workspace" / "workflow.jsonl").is_file()  # inspect가 통과했다

    # 1스트림(기본 도구)은 격리 뿌리에서 새 job로 다시 — workspace 재사용 금지.
    second_root = tmp_path / "second"
    second_sandbox = build_engine_sandbox(second_root, source=REPO)
    second_standin = stage_canary_standin(second_root / "sealed")
    one_env = _guarded_wrapper_env(second_sandbox, second_standin, tools_variant="one-audio")
    second_wrapper = _install(second_root, repo=second_sandbox.repo)
    one_job = _stage_job(second_root)
    one = _run_wrapper_captured(
        second_wrapper, ["analyze", "job-1", str(one_job)], sandbox=second_sandbox, env=one_env
    )
    assert one.returncode == 0
    assert (one_job / "workspace" / "workflow.jsonl").is_file()  # inspect가 통과했다
    _assert_engine_sandbox_hygiene(second_sandbox)


def test_real_engine_probe_failure_is_an_error_never_silence(tmp_path: Path) -> None:
    """ffprobe가 실패로 끝나면 위상은 '0스트림·무음'이 아니라 실패다 — 실행
    가능한 문면으로 죽고 추정은 남지 않는다."""
    sandbox = build_engine_sandbox(tmp_path, source=REPO)
    standin = stage_canary_standin(tmp_path / "sealed")
    guarded_env = _guarded_wrapper_env(sandbox, standin, tools_variant="fail")
    wrapper = _install(tmp_path, repo=sandbox.repo)
    job_dir = _stage_job(tmp_path)

    completed = _run_wrapper_captured(
        wrapper, ["analyze", "job-1", str(job_dir)], sandbox=sandbox, env=guarded_env
    )

    assert completed.returncode != 0
    assert "ffprobe" in completed.stderr
    assert not (job_dir / "analysis-estimate.jsonl").is_file()
    assert not (job_dir / "candidates.jsonl").is_file()
    _assert_engine_sandbox_hygiene(sandbox)


def _assert_engine_sandbox_hygiene(sandbox: EngineSandbox) -> None:
    """보호 철자·심볼링크 위생 — 위상 시험도 같은 경계 안에 있다."""
    assert sandbox.dotenv_files() == [sandbox.dotenv]
    assert sandbox.symlinks() == []


def test_installed_wrapper_forwards_extra_launch_arguments(tmp_path: Path) -> None:
    """부가 실행 인자(상태 기록·호출 신원)가 래퍼를 통과해 그대로 간다(S2A-3).

    래퍼는 옛 계약처럼 넷째·다섯째 인자만 골라 넘기지 않는다 — 호출 신원 깃발은
    값 짝이므로 찢어지면 진행 발행의 신원 계약이 무너진다.
    """
    repo = _fake_repo_with_logging_python(tmp_path)
    wrapper = _install(tmp_path, repo=repo)
    job = tmp_path / "job"
    job.mkdir()
    status = tmp_path / "vlogsrt-status-token7" / "token7.status"
    status.parent.mkdir()
    log = repo / "calls.log"
    engine_script = str(repo / "macos/scripts/vlogsrt-engine.py")

    completed = subprocess.run(
        [
            str(wrapper),
            "analyze",
            "job-7",
            str(job),
            str(status),
            "--invocation-id",
            "inv-wrap-7",
        ],
        check=False,
        env={**os.environ, "VLOGSRT_OFFLINE": "1"},
    )
    assert completed.returncode == 0
    assert log.read_text(encoding="utf-8").splitlines() == [
        f"{engine_script} analyze job-7 {job} {status} --invocation-id inv-wrap-7",
    ]
