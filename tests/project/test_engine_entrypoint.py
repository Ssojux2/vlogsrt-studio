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
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest

from tests._dotenv_guard import stage_synthetic_dotenv
from vlogsrt import transcribe
from vlogsrt.env import find_env_file
from vlogsrt.project import runner
from vlogsrt.project.workflow_protocol import (
    AnalysisAuthorization,
    AnalysisEstimate,
    AnalysisResult,
    CancellationRequest,
    encode_message,
)

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"
_GRANT = json.dumps({"grant_revision": "a" * 64})


def _load_engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vlogsrt_engine_entrypoint", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def offline_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """진입점이 참조하는 dotenv는 이 모듈이 지은 합성물뿐이다.

    cli.main의 ensure_loaded가 cwd 조상을 거슬러 올라가므로, 합성 dotenv를
    tmp_path 꼭대기에 두고 실행 위치도 그 안으로 옮겨 탐색이 실제 저장소
    (PACKAGE_ROOT fallback 포함)에 닿기 전에 멈춘다(S2A-1 격리 원리).
    conftest의 dotenv 보호가 그 경계를 못 박는다.
    """
    dotenv = stage_synthetic_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    yield dotenv


@pytest.fixture()
def engine() -> ModuleType:
    """진입점 모듈 — main이 남기는 job 루트 환경변수를 테스트 뒤 되돌린다."""
    saved = os.environ.get(_JOB_DIR_ENV)
    yield _load_engine()
    if saved is None:
        os.environ.pop(_JOB_DIR_ENV, None)
    else:
        os.environ[_JOB_DIR_ENV] = saved


def _stage_job(tmp_path: Path, *, two_clips: bool = False) -> Path:
    """input.fcpxml만 있는 새 job 폴더 — 미디어는 합성 바이트다."""
    job_dir = tmp_path / "engine-job"
    job_dir.mkdir()
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-entrypoint-media")
    media_rep = '      <media-rep kind="original-media" src="file://{src}"/>\n    </asset>\n    '

    def asset(index: int, name: str, path: Path) -> str:
        return (
            f'<asset id="r{index + 2}" name="{name}" start="0s" duration="600s"'
            f' hasAudio="1" format="r1">\n' + media_rep.format(src=quote(str(path)))
        )

    assets = asset(0, "clip.mov", media)
    clips = (
        '<asset-clip ref="r2" name="클립0" offset="0s" start="0s" duration="12s"/>\n            '
    )
    if two_clips:
        media_b = tmp_path / "clip-b.mov"
        media_b.write_bytes(b"synthetic-entrypoint-media-b")
        assets += asset(1, "clip-b.mov", media_b)
        clips += (
            '<asset-clip ref="r3" name="클립1" offset="12s" start="0s"'
            ' duration="12s"/>\n            '
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        f' width="1920" height="1080"/>\n    {assets}</resources>\n  <library>\n    '
        '<event name="이벤트">\n'
        '      <project name="진입점 프로젝트" uid="PROJECT-ENTRY">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        f"          <spine>\n            {clips}"
        "</spine>\n        </sequence>\n      </project>\n    </event>\n"
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


def test_engine_cli_loads_only_the_staged_synthetic_dotenv(
    tmp_path: Path, engine: ModuleType
) -> None:
    """cli 진입의 ensure_loaded가 찾는 dotenv는 합성물 하나뿐이다.

    실행 뒤에도 탐색 결과는 tmp_path의 합성 `.env`다 — PACKAGE_ROOT(진짜
    저장소) fallback으로 흐르지 않는다는 구조적 증명이다.
    """
    job_dir = _stage_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    assert find_env_file() == (tmp_path / ".env").resolve()


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


# ─────────────────────────────────────────────────────────────
#  STAB-S1B — 실패한 실행도 증거를 job 루트로 옮긴다(진단 파일만)
# ─────────────────────────────────────────────────────────────


def _write_authorization(job_dir: Path) -> None:
    """job 루트의 추정에서 승인 파일을 만든다 — 실제 앱이 남기는 모양이다."""
    from vlogsrt.project import commands as project_commands

    estimate = project_commands._read_one(job_dir / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    (job_dir / "analysis-authorization.jsonl").write_text(
        encode_message(
            AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )


def _patch_paid_gates(
    monkeypatch: pytest.MonkeyPatch,
    luna: Callable[..., dict] | None = None,
) -> None:
    from vlogsrt.project import commands as project_commands

    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(transcribe, "create_vision_client", lambda **kwargs: object())

    def keep(client: object, *, model: str, payload: dict, online: bool) -> dict:
        return {
            "output_text": json.dumps(
                {"choice": "keep", "confidence": 0.5, "evidence_frame_ids": []}
            ),
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }

    monkeypatch.setattr(transcribe, "request_luna_vision", luna or keep)

    import openai

    monkeypatch.setattr(
        openai,
        "OpenAI",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("paid path")),
    )


class _ProposingLuna:
    """문면의 첫 후보를 propose_cut로 답한다 — 지정한 창은 예외로 실패한다."""

    def __init__(self, fail_windows: set[int] | None = None) -> None:
        self.fail_windows = fail_windows or set()
        self.calls = 0

    def __call__(self, client: object, *, model: str, payload: dict, online: bool) -> dict:
        import re

        index = self.calls
        self.calls += 1
        if index in self.fail_windows:
            raise RuntimeError("luna flaked")
        content = payload["input"][0]["content"]
        texts = [item["text"] for item in content if item.get("type") == "input_text"]
        candidate = re.search(r"- ([0-9a-f]{64}): ", texts[0])
        assert candidate is not None
        frames = re.findall(r"frame_id=(\S+)", " ".join(texts))
        assert frames
        return {
            "output_text": json.dumps(
                {
                    "choice": candidate.group(1),
                    "confidence": 0.9,
                    "evidence_frame_ids": [frames[0]],
                }
            ),
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }


def test_engine_nonzero_exit_still_copies_diagnostics_but_no_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """분석이 실패로 끝나도 시도·집계 증거는 job 루트로 나온다 — 후보는 나오지 않는다."""
    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    _write_authorization(job_dir)

    def no_stream(source: Path, stream_index: int) -> object:
        raise RuntimeError("no stream")

    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    monkeypatch.setattr(runner, "probe_audio_stream", no_stream)
    _patch_paid_gates(monkeypatch)

    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1

    # 진단 증거 — 집계·시도는 실패 사실을 job 루트로 전한다.
    result = _read_result(job_dir / "analysis-result.jsonl")
    assert result.status == "error"
    assert result.upstream_audio_failed == 1
    attempts = (job_dir / "analysis-attempts.jsonl").read_text(encoding="utf-8")
    assert '"failed"' in attempts
    # 성공 산물은 나오지 않는다 — 실패 동기화는 진단 전용이다.
    assert not (job_dir / "candidates.jsonl").exists()
    assert not (job_dir / "roughcut.fcpxml").exists()


def test_engine_success_copies_aggregate_with_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """성공 실행은 집계·판정·후보를 모두 job 루트로 옮긴다 — 후보는 실물이다."""
    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    _write_authorization(job_dir)
    _patch_analysis_boundaries(monkeypatch)
    _patch_paid_gates(monkeypatch, _ProposingLuna())

    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0

    result = _read_result(job_dir / "analysis-result.jsonl")
    assert result.status == "success"
    assert result.candidate_count == 1
    assert (job_dir / "vision-decisions.jsonl").is_file()
    candidates = (job_dir / "candidates.jsonl").read_text(encoding="utf-8")
    assert '"propose_cut"' in candidates or '"CutCandidate"' in candidates


def test_engine_partial_failure_exits_zero_and_propagates_safe_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """부분 실패는 종료 코드 0으로 검토 경로를 간다 — 안전 후보가 앱에 보인다."""
    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path, two_clips=True)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    _write_authorization(job_dir)
    _patch_analysis_boundaries(monkeypatch)
    _patch_paid_gates(monkeypatch, _ProposingLuna(fail_windows={1}))

    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0

    result = _read_result(job_dir / "analysis-result.jsonl")
    assert result.status == "partial_error"
    assert result.windows_successful == 1
    assert result.windows_failed == 1
    # 앱에 보이는 안전 후보 — 성공한 창의 것만, 실패 창의 것은 없다.
    candidates = (job_dir / "candidates.jsonl").read_text(encoding="utf-8")
    assert candidates.count('"type":"CutCandidate"') == 1
    decisions = (job_dir / "vision-decisions.jsonl").read_text(encoding="utf-8")
    assert '"status":"error"' in decisions


def _read_result(path: Path) -> AnalysisResult:
    from vlogsrt.project import commands as project_commands

    message = project_commands._read_one(path)
    assert isinstance(message, AnalysisResult)
    return message


# ─────────────────────────────────────────────────────────────
#  STAB-S2A-1 — 취소로 끝난 실행도 소유권을 돌려준다
# ─────────────────────────────────────────────────────────────


def test_engine_releases_ownership_after_a_cancelled_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """취소된 실행(실패 종료) 뒤에도 같은 job은 곧바로 다시 실행된다 — exit 3이 아니다.

    진짜 오프라인 경로로 취소를 일으킨다: job 루트에 남은 취소 기록을 집계의
    리비전에 묶어 스테이징하면 승인된 analyze가 시작 게이트에서 멈춘다.
    잠금이 취소된 실행 뒤에도 남아 있다면 다음 호출은 중복(3)으로 물러나야
    한다 — 그 반대를 못 박는다.
    """
    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    _write_authorization(job_dir)
    _patch_paid_gates(monkeypatch)

    from vlogsrt.project import commands as project_commands

    estimate = project_commands._read_one(job_dir / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    (job_dir / "cancellation.jsonl").write_text(
        _staged_cancellation(estimate.input_revision), encoding="utf-8"
    )

    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1
    attempts = (job_dir / "analysis-attempts.jsonl").read_text(encoding="utf-8")
    assert '"cancelled"' in attempts  # 실패가 아니라 취소로 끝났다

    # 취소된 실행도 소유권을 돌려줬다 — 다시 실행은 중복(3)이 아니라 취소(1)다.
    # 중복 진단의 실제 stderr 계약은 subprocess 시험(test_engine_execution_lock.py)이 못 박는다.
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1


def _patch_analysis_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """ffmpeg/PCM/프레임/VAD 경계를 가짜로 막는다 — 미디어 바이트는 합성물이다."""
    import array
    from fractions import Fraction

    from vlogsrt import preprocess
    from vlogsrt.config import Config
    from vlogsrt.project import pcm as pcm_module
    from vlogsrt.project.vision import sampling
    from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, TranscriptionJob

    rate = 16000

    class _FakeArray:
        __slots__ = ("data", "shape", "dtype")

        def __init__(self, data: object, dtype: str) -> None:
            self.data = data
            self.dtype = dtype
            dims: list[int] = []
            level = data
            while isinstance(level, list):
                dims.append(len(level))
                level = level[0] if level else None
            self.shape = tuple(dims)

        def __getitem__(self, index: int) -> object:
            return self.data[index]  # type: ignore[index]

        def tolist(self) -> object:
            return self.data

    class _ConstantSession:
        def run(self, output_names: object, feeds: dict[str, object]) -> list[_FakeArray]:
            return [
                _FakeArray([[0.1]], "float32"),
                _FakeArray([[[0.0] * 128], [[0.0] * 128]], "float32"),
            ]

    class _Scripted:
        def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
            if job.kind != FIXTURE_KIND:
                raise RuntimeError(f"예약하지 않은 {job.kind} 호출이다")
            payload = {
                "segments": [
                    {"start": 0.5, "end": 1.0, "text": "인사"},
                    {"start": 10.5, "end": 11.0, "text": "마무리"},
                ]
            }
            return [
                ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
                for chunk in job.chunks
            ]

    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    monkeypatch.setattr(
        runner,
        "probe_audio_stream",
        lambda source, stream_index: preprocess.AudioStreamInfo(
            stream_index=1, start_time=Fraction(0), sample_rate=48000, channels=1
        ),
    )

    def extract_pcm(
        source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        values = array.array("h", ((frame * 3) % 28001 - 14000 for frame in range(12 * rate)))
        destination.write_bytes(values.tobytes())
        return preprocess.PcmExtraction(
            path=destination,
            stream=preprocess.AudioStreamInfo(
                stream_index=stream_index + 1,
                start_time=Fraction(0),
                sample_rate=48000,
                channels=1,
            ),
            sample_count=len(values),
            sample_width=2,
            pcm_sample0_file_time=Fraction(0),
        )

    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", extract_pcm)
    monkeypatch.setattr(runner, "bundled_artifact", lambda cfg: Path("/fake/silero.onnx"))
    monkeypatch.setattr(runner, "create_cpu_session", lambda artifact: _ConstantSession())
    monkeypatch.setattr(
        sampling,
        "probe_video_stream",
        lambda source, stream_index=0: preprocess.VideoStreamInfo(
            stream_index=0,
            presentation_origin=Fraction(0),
            frame_duration=Fraction(1, 30),
            frame_count=360,
        ),
    )

    def extract_frame(
        source: Path,
        stream_index: int,
        frame_index: int,
        destination: Path,
        *,
        long_edge: int,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"frame:{frame_index}".encode())
        return destination

    monkeypatch.setattr(sampling, "extract_frame_index", extract_frame)

    def sha256_of(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()

    monkeypatch.setattr(sampling, "sha256_file", sha256_of)
    monkeypatch.setattr(transcribe, "get_transcriber", lambda *args, **kwargs: _Scripted())
