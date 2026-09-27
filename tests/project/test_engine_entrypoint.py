"""번들 엔진 진입점의 회귀 시험 — 실제 `macos/scripts/vlogsrt-engine.py`를 부른다.

도우미만 직접 부르는 시험으로는 스테이징 순서 결함(workspace가 생기기 전에
`media-grant.json`을 복사하다 `FileNotFoundError`)을 못 잡는다. 새 임시 job
폴더에서 진입점 전체를 돌린다 — receive·inspect까지 오프라인으로 끝나고 승인
파일이 없으면 analyze는 스킵이므로 유료 경로가 열리지 않는다. 진입점이 job
루트를 `VLOGSRT_ENGINE_JOB_DIR`로 알리는 계약도 여기서 못 박는다 — 앱이 실행
중 남기는 취소가 엔진 안 관측 경로와 만나는 지점이다(취소 통합 시험은
test_workflow_cancellation.py가 든다).
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

from vlogsrt.project.workflow_protocol import CancellationRequest, encode_message

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"
_GRANT = json.dumps({"grant_revision": "a" * 64})


def _load_engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vlogsrt_engine_entrypoint", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def engine() -> ModuleType:
    """진입점 모듈 — main이 남기는 job 루트 환경변수를 테스트 뒤 되돌린다."""
    saved = os.environ.get(_JOB_DIR_ENV)
    yield _load_engine()
    if saved is None:
        os.environ.pop(_JOB_DIR_ENV, None)
    else:
        os.environ[_JOB_DIR_ENV] = saved


def _stage_job(tmp_path: Path) -> Path:
    """input.fcpxml만 있는 새 job 폴더 — 미디어는 합성 바이트다."""
    job_dir = tmp_path / "engine-job"
    job_dir.mkdir()
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-entrypoint-media")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip.mov" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="file://{quote(str(media))}"/>\n    </asset>\n'
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="진입점 프로젝트" uid="PROJECT-ENTRY">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s"'
        ' duration="12s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    (job_dir / "input.fcpxml").write_text(body, encoding="utf-8")
    return job_dir


def _staged_cancellation(revision: str) -> str:
    return encode_message(
        CancellationRequest.create(
            input_revision=revision,
            attempt_id="*",
            requested_at="2026-09-27T04:00:00Z",
            reason="user",
        )
    )


def test_engine_main_stages_selected_files_into_a_fresh_workspace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """workspace가 없는 새 job에서 스테이징이 먼저 무너지는 결함을 잡는다."""
    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path)
    (job_dir / "media-grant.json").write_text(_GRANT, encoding="utf-8")
    staged = _staged_cancellation("b" * 64)
    (job_dir / "cancellation.jsonl").write_text(staged, encoding="utf-8")
    workspace = job_dir / "workspace"
    assert not workspace.exists()

    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0

    # receive·inspect까지 끝났고, 결과 파일은 job 루트로 동기화됐다.
    assert (workspace / "workflow.jsonl").is_file()
    assert (workspace / "analysis-estimate.jsonl").is_file()
    assert (job_dir / "analysis-estimate.jsonl").is_file()
    # 스테이징 파일은 바이트 그대로 workspace로 들어가고 원본은 그대로다.
    assert (workspace / "media-grant.json").read_text(encoding="utf-8") == _GRANT
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == staged
    assert (job_dir / "media-grant.json").read_text(encoding="utf-8") == _GRANT
    assert (job_dir / "cancellation.jsonl").read_text(encoding="utf-8") == staged
    # 진입점은 job 루트를 알린다 — 실행 중 취소 관측 계약(S1A).
    assert os.environ[_JOB_DIR_ENV] == str(job_dir.resolve())


def test_engine_main_resume_skips_receive_and_keeps_workspace_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """없는 선택 파일로 시작해도 안전하고, 재개 때 이미 있는 workspace 기록은 못 박는다."""
    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path)
    workspace = job_dir / "workspace"

    # 첫 실행: 선택 파일이 전혀 없다 — 만들지도 복사하지도 않는다.
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    assert not (workspace / "media-grant.json").exists()
    assert not (workspace / "cancellation.jsonl").exists()
    journal_bytes = (workspace / "workflow.jsonl").read_bytes()

    # 재개: 루트에 남긴 취소가 workspace로 들어온다(아직 없으니까).
    first = _staged_cancellation("b" * 64)
    (job_dir / "cancellation.jsonl").write_text(first, encoding="utf-8")
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == first
    assert (workspace / "workflow.jsonl").read_bytes() == journal_bytes

    # 다시 재개: 앱이 실행 중 새로 남긴 루트 기록이 workspace 기록을 덮어쓰지 않는다.
    (job_dir / "cancellation.jsonl").write_text(_staged_cancellation("c" * 64), encoding="utf-8")
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    assert (workspace / "cancellation.jsonl").read_text(encoding="utf-8") == first
    assert (workspace / "workflow.jsonl").read_bytes() == journal_bytes


def test_engine_main_rejects_broken_invocations(tmp_path: Path, engine: ModuleType) -> None:
    """`<operation> <job_id> <job_dir>` 계약 밖의 호출은 2로 닫는다."""
    job_dir = tmp_path / "job"
    workspace = job_dir / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "workflow.jsonl").write_text("{}\n", encoding="utf-8")
    assert engine.main(["analyze", "job-1", str(job_dir), "extra"]) == 2
    assert engine.main(["analyze", "a/b", str(job_dir)]) == 2
    assert engine.main(["analyze", "..", str(job_dir)]) == 2
    assert engine.main(["transcribe", "job-1", str(job_dir)]) == 2
