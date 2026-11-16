"""STAB-S2A-3 — 분석 진행의 실행 중 원자 스냅샷 회귀(S2A-3).

기존 API만 쓴다: `cli.main` 파이프라인 전체와 가짜 경계(전사기·Luna·
ffmpeg/PCM/프레임), 그리고 엔진 진입점이 이미 알리는 `VLOGSRT_ENGINE_JOB_DIR`
계약. 새 심볼은 하나도 import하지 않는다 — 단언은 파일 바이트와 JSON 키로만
한다. 관측 지점은 **실행 중**이다: Luna 호출을 감싼 훅이 그 사이 job 루트의
`analysis-progress.jsonl`을 읽는다. 지금 코드는 진행 파일을 전혀 쓰지
않으므로 실행 중 관측은 실패해야 한다(RED).

스냅샷 한 장의 계약(이 시험이 못 박는 것):
- 봉투는 프로젝트 JSONL 모양 그대로다(`payload`·`type`·`protocol_version`).
- `payload.status`는 running/succeeded/failed/cancelled, `payload.stage`는
  preparation/audio/vision/publication.
- 창 계수는 아는 때만 실린다(vision 단계): `completed`·`total` 정수.
- job 루트 발행은 엔진이 알린 두 환경값(루트·job 신원)이 함께 있을 때만
  일어난다 — 직접 CLI는 workspace에만 남는다.
"""

from __future__ import annotations

import array
import hashlib
import importlib.util
import json
import os
import shutil
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from urllib.parse import quote

import pytest
from analysis_scope import scope_of

from tests._dotenv_guard import stage_synthetic_dotenv
from vlogsrt import cli, preprocess, transcribe
from vlogsrt.config import Config
from vlogsrt.project import commands as project_commands
from vlogsrt.project import pcm as pcm_module
from vlogsrt.project import runner
from vlogsrt.project.vision import sampling
from vlogsrt.project.workflow_protocol import (
    AnalysisAuthorization,
    AnalysisProgress,
    CancellationRequest,
    WorkflowJournal,
    decode_message,
    encode_message,
)
from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, TranscriptionJob

RATE = 16000
CLIP_SECONDS = 12
FD30 = Fraction(1, 30)
SEGMENTS = ((0.5, 1.0, "인사"), (10.5, 11.0, "마무리"))
_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"
_JOB_ID_ENV = "VLOGSRT_ENGINE_JOB_ID"
_ENGINE_INVOCATION_ENV = "VLOGSRT_ENGINE_INVOCATION_ID"
_PROGRESS = "analysis-progress.jsonl"


@pytest.fixture(autouse=True)
def offline_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """cli가 찾는 dotenv는 이 모듈이 지은 합성물뿐이다(S2A-3 시험 격리).

    ensure_loaded가 cwd 조상을 거슬러 올라가므로 합성 dotenv를 tmp_path 꼭대기에
    두고 실행 위치도 그 안으로 옮긴다 — 탐색이 실제 저장소(PACKAGE_ROOT fallback
    포함)에 닿기 전에 멈춘다. 취소 시험의 conftest 보호만으로는 이 경계가 못
    박히지 않는다(test_engine_entrypoint.py와 같은 구조).
    """
    dotenv = stage_synthetic_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    return dotenv


@pytest.fixture(autouse=True)
def supported_audio_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 합성 미디어는 오디오 스트림 하나다(S2A-2 위상 관문의 전제)."""
    from vlogsrt.project import topology

    monkeypatch.setattr(topology, "probe_audio_stream_count", lambda source: 1)


@pytest.fixture(autouse=True)
def synthetic_stt_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """엔드포인트 신원이 inspect·analyze에서 같은 값을 보게 한다(S2B-2b).

    추정 지문이 실제 엔드포인트를 묶는 지금, 이 파일은 첫 실행(패치 전)과 둘째
    실행(패치 후)이 같은 job에서 섞이므로 파일 전체에서 한 값으로 못 박는다.
    """
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")


def _file_uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _project_xml(media_uris_with_clips: list[tuple[str, str, str]]) -> str:
    assets = "".join(
        f'<asset id="{asset_id}" name="{name}" start="0s" duration="600s" hasAudio="1"'
        f' format="r1">\n'
        f'      <media-rep kind="original-media" src="{uri}"/>\n    </asset>\n    '
        for asset_id, name, uri in media_uris_with_clips
    )
    clips = "".join(
        f'<asset-clip ref="{asset_id}" name="클립{index}" offset="{index * 12}s"'
        f' start="0s" duration="12s"/>\n            '
        for index, (asset_id, _name, _uri) in enumerate(media_uris_with_clips)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    ' + assets + "</resources>\n  <library>\n    "
        '    <event name="이벤트">\n'
        '      <project name="진행 프로젝트" uid="PROJECT-PROGRESS">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        f"          <spine>\n            {clips}"
        "</spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )


def _single_clip_project(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-progress-media")
    path = tmp_path / "progress.fcpxml"
    path.write_text(_project_xml([("r2", "clip.mov", _file_uri(media))]), encoding="utf-8")
    return path


def _two_clip_project(tmp_path: Path) -> Path:
    """서로 다른 미디어 두 개를 잇는 프로젝트 — 창이 둘 이상 나와야 한다."""
    media_a = tmp_path / "clip-a.mov"
    media_b = tmp_path / "clip-b.mov"
    media_a.write_bytes(b"synthetic-progress-media-a")
    media_b.write_bytes(b"synthetic-progress-media-b")
    path = tmp_path / "progress-two.fcpxml"
    path.write_text(
        _project_xml(
            [
                ("r2", "clip-a.mov", _file_uri(media_a)),
                ("r3", "clip-b.mov", _file_uri(media_b)),
            ]
        ),
        encoding="utf-8",
    )
    return path


def _write_config(directory: Path) -> Path:
    path = directory / "test.project.toml"
    path.write_text("schema_version = 1\n", encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
#  가짜 경계 — test_workflow_cancellation.py와 같은 기법
# ─────────────────────────────────────────────────────────────


class _Scripted:
    def __init__(self) -> None:
        self.whisper_calls: list[TranscriptionJob] = []

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.whisper_calls.append(job)
        if job.kind != FIXTURE_KIND:
            raise RuntimeError(f"예약하지 않은 {job.kind} 호출이다")
        payload = {
            "segments": [
                {"start": start, "end": end, "text": text} for start, end, text in SEGMENTS
            ]
        }
        return [
            ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
            for chunk in job.chunks
        ]


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
    def __init__(self, probability: float) -> None:
        self.probability = probability

    def run(self, output_names: object, feeds: dict[str, object]) -> list[_FakeArray]:
        return [
            _FakeArray([[self.probability]], "float32"),
            _FakeArray([[[0.0] * 128], [[0.0] * 128]], "float32"),
        ]


def _pattern_pcm(frames: int) -> bytes:
    values = array.array("h", ((frame * 3) % 28001 - 14000 for frame in range(frames)))
    return values.tobytes()


def _probe_stream(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
    return preprocess.AudioStreamInfo(
        stream_index=1, start_time=Fraction(0), sample_rate=48000, channels=1
    )


def _extract_pattern_pcm(
    source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
) -> preprocess.PcmExtraction:
    pcm = _pattern_pcm(CLIP_SECONDS * RATE)
    destination.write_bytes(pcm)
    return preprocess.PcmExtraction(
        path=destination,
        stream=preprocess.AudioStreamInfo(
            stream_index=stream_index + 1,
            start_time=Fraction(0),
            sample_rate=48000,
            channels=1,
        ),
        sample_count=len(pcm) // 2,
        sample_width=2,
        pcm_sample0_file_time=Fraction(0),
    )


def _patch_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    monkeypatch.setattr(runner, "probe_audio_stream", _probe_stream)
    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", _extract_pattern_pcm)
    monkeypatch.setattr(runner, "bundled_artifact", lambda cfg: Path("/fake/silero.onnx"))
    monkeypatch.setattr(runner, "create_cpu_session", lambda artifact: _ConstantSession(0.1))

    def probe(source: Path, stream_index: int = 0) -> preprocess.VideoStreamInfo:
        return preprocess.VideoStreamInfo(
            stream_index=0,
            presentation_origin=Fraction(0),
            frame_duration=FD30,
            frame_count=360,
        )

    def extract(
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

    monkeypatch.setattr(sampling, "probe_video_stream", probe)
    monkeypatch.setattr(sampling, "extract_frame_index", extract)
    monkeypatch.setattr(
        sampling, "sha256_file", lambda path: hashlib.sha256(path.read_bytes()).hexdigest()
    )


def _patch_paid_boundaries(monkeypatch: pytest.MonkeyPatch, luna_responses: list[dict]) -> None:
    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(transcribe, "create_vision_client", lambda **kwargs: object())

    def fake_luna(
        client: object,
        *,
        model: str,
        payload: dict,
        online: bool,
        observer: object = None,
        cancellation: object = None,
    ) -> dict:
        response = {
            "output_text": json.dumps(
                {"choice": "keep", "confidence": 0, "evidence_frame_ids": []}
            ),
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        luna_responses.append(response)
        return response

    monkeypatch.setattr(transcribe, "request_luna_vision", fake_luna)

    import openai

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("openai.OpenAI가 불렸다 — 유료 경로가 열렸다")

    monkeypatch.setattr(openai, "OpenAI", boom)


def _stub_transcriber(monkeypatch: pytest.MonkeyPatch) -> _Scripted:
    scripted = _Scripted()
    monkeypatch.setattr(transcribe, "get_transcriber", lambda *args, **kwargs: scripted)
    return scripted


def _authorization_file(tmp_path: Path, workspace: Path) -> Path:
    estimate = scope_of(workspace)
    path = tmp_path / "authorization.jsonl"
    path.write_text(
        encode_message(
            AnalysisAuthorization(
                input_revision=estimate.input_revision,
                settings_sha256=estimate.settings_sha256,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-28T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    return path


def _analyze_argv(workspace: Path, authorization: Path, project_config: Path) -> list[str]:
    """analyze의 재계산 추정이 inspect와 같은 합성 프로젝트 설정을 보게 한다."""
    return [
        "project",
        "analyze",
        str(workspace),
        "--online",
        "--authorization",
        str(authorization),
        "--project-config",
        str(project_config),
    ]


def _engine_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_path: Path
) -> tuple[Path, Path]:
    """엔진이 잡는 job 폴더 모양(`<job>/workspace`)을 만들고 루트·job 신원을 알린다."""
    job_root = tmp_path / "engine-job"
    workspace = job_root / "workspace"
    argv_tail = ["--project-config", str(_write_config(tmp_path))]
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0
    monkeypatch.setenv(_JOB_DIR_ENV, str(job_root))
    monkeypatch.setenv(_JOB_ID_ENV, "roundtrip-progress")
    return job_root, workspace


def _read_progress(root: Path) -> dict | None:
    """진행 스냅샷 한 장 — 없거나 깨졌으면 None(보수적 읽기)."""
    path = root / _PROGRESS
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def _payload(snapshot: dict | None) -> dict:
    assert snapshot is not None, "진행 스냅샷이 없다"
    assert snapshot.get("type") == "AnalysisProgress"
    payload = snapshot.get("payload")
    assert isinstance(payload, dict)
    return payload


# ─────────────────────────────────────────────────────────────
#  RED — 실행 중·종료 뒤 job 루트에서 진행이 보여야 한다
# ─────────────────────────────────────────────────────────────


def test_running_window_counts_are_durable_at_the_job_root_mid_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """둘째 창을 부르는 사이 job 루트의 스냅샷은 running·실제 창 계수여야 한다."""
    job_root, workspace = _engine_job(tmp_path, monkeypatch, _two_clip_project(tmp_path))

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)
    observed: list[dict | None] = []
    inner_luna = transcribe.request_luna_vision

    def luna_then_observe(*args: object, **kwargs: object) -> dict:
        response = inner_luna(*args, **kwargs)  # type: ignore[operator]
        observed.append(_read_progress(job_root))
        return response  # type: ignore[no-any-return]

    monkeypatch.setattr(transcribe, "request_luna_vision", luna_then_observe)

    assert (
        cli.main(
            _analyze_argv(
                workspace, _authorization_file(tmp_path, workspace), _write_config(tmp_path)
            )
        )
        == 0
    )
    assert len(luna_calls) >= 2, "둘 이상의 창이 있어야 실행 중 관측이 의미 있다"
    mid = _payload(observed[1])
    assert mid["status"] == "running"
    assert mid["stage"] == "vision"
    assert isinstance(mid["completed"], int) and not isinstance(mid["completed"], bool)
    assert isinstance(mid["total"], int) and not isinstance(mid["total"], bool)
    assert mid["completed"] == 1
    assert mid["total"] == len(luna_calls)


def test_successful_run_closes_a_publication_snapshot_in_both_locations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """성공 종료는 candidates·시도 기록이 정착한 뒤 publication/succeeded로 닫는다."""
    job_root, workspace = _engine_job(tmp_path, monkeypatch, _single_clip_project(tmp_path))

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)
    observed: list[dict | None] = []
    inner_luna = transcribe.request_luna_vision

    def luna_then_observe(*args: object, **kwargs: object) -> dict:
        response = inner_luna(*args, **kwargs)  # type: ignore[operator]
        observed.append(_read_progress(job_root))
        return response  # type: ignore[no-any-return]

    monkeypatch.setattr(transcribe, "request_luna_vision", luna_then_observe)

    assert (
        cli.main(
            _analyze_argv(
                workspace, _authorization_file(tmp_path, workspace), _write_config(tmp_path)
            )
        )
        == 0
    )
    # 실행 중 첫 관측 — 창 계획이 0/N으로 보인다(아직 창은 하나도 안 끝났다).
    first = _payload(observed[0])
    assert first["status"] == "running"
    assert first["stage"] == "vision"
    assert first["completed"] == 0
    assert first["total"] == len(luna_calls)
    # 종료 뒤 — job 루트와 workspace 둘 다 같은 종료 스냅샷을 가진다.
    final = _payload(_read_progress(job_root))
    assert final["status"] == "succeeded"
    assert final["stage"] == "publication"
    archived = _read_progress(workspace)
    assert archived is not None
    assert archived["payload"]["status"] == "succeeded"


def test_cancelled_run_marks_the_snapshot_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """준비 뒤의 취소도 진행 스냅샷을 cancelled로 닫는다 — 성공으로 못 간다."""
    job_root, workspace = _engine_job(tmp_path, monkeypatch, _single_clip_project(tmp_path))

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)
    real_prepare = runner.prepare_audio

    def prepare_then_cancel(*args: object, **kwargs: object) -> object:
        prepared = real_prepare(*args, **kwargs)
        project_commands._append_message(
            workspace / "cancellation.jsonl",
            CancellationRequest.create(
                input_revision=WorkflowJournal.open(workspace / "workflow.jsonl").revision.revision,
                attempt_id="*",
                requested_at="2026-09-28T04:00:30Z",
                reason="user",
            ),
        )
        return prepared

    monkeypatch.setattr(runner, "prepare_audio", prepare_then_cancel)

    assert (
        cli.main(
            _analyze_argv(
                workspace, _authorization_file(tmp_path, workspace), _write_config(tmp_path)
            )
        )
        == 2
    )
    final = _payload(_read_progress(job_root))
    assert final["status"] == "cancelled"
    # 취소된 시도는 Luna 호출 없이 끝났다 — 진행도 유료 성공을 말하면 안 된다.
    assert luna_calls == []


# ─────────────────────────────────────────────────────────────
#  계약 — AnalysisProgress 웜(wire) 검증
# ─────────────────────────────────────────────────────────────


def _progress_message(**overrides: object) -> AnalysisProgress:
    fields: dict[str, object] = {
        "input_revision": "a" * 64,
        "estimate_revision": "b" * 64,
        "attempt_id": "attempt-contract",
        "job_id": "roundtrip-progress",
        "invocation_id": "inv-contract-1",
        "stage": "vision",
        "status": "running",
        "completed": 1,
        "total": 3,
        "recorded_at": "2026-09-28T05:00:00Z",
    }
    fields.update(overrides)
    created = AnalysisProgress.create(**fields)  # type: ignore[arg-type]
    return created


def test_progress_roundtrip_preserves_identity_and_counts() -> None:
    message = _progress_message()
    decoded = decode_message(encode_message(message))
    assert decoded == message
    payload = json.loads(encode_message(message))
    assert payload["type"] == "AnalysisProgress"
    assert payload["protocol_version"] == "1.3"
    assert payload["payload"]["invocation_id"] == "inv-contract-1"
    # 선택 신원은 생략 가능하다 — 직접 CLI 실행의 모양.
    bare = decode_message(
        encode_message(
            _progress_message(job_id=None, invocation_id=None, completed=None, total=None)
        )
    )
    assert isinstance(bare, AnalysisProgress)
    assert bare.job_id is None and bare.completed is None


def test_progress_rejects_malformed_identity() -> None:
    with pytest.raises(ValueError):
        _progress_message(input_revision="short")
    with pytest.raises(ValueError):
        _progress_message(estimate_revision="")
    with pytest.raises(ValueError):
        _progress_message(attempt_id="")
    with pytest.raises(ValueError):
        _progress_message(job_id="")
    with pytest.raises(ValueError):
        _progress_message(invocation_id="")
    with pytest.raises(ValueError):
        _progress_message(recorded_at="")


def test_progress_rejects_bad_counts() -> None:
    with pytest.raises(ValueError):
        _progress_message(completed=-1)
    with pytest.raises(ValueError):
        _progress_message(total=True)  # type: ignore[call-overload]
    with pytest.raises(ValueError):
        _progress_message(completed=3, total=2)
    with pytest.raises(ValueError):
        _progress_message(completed=1, total=None)
    with pytest.raises(ValueError):
        _progress_message(stage="audio", completed=0, total=1)


def test_progress_rejects_unknown_enums_and_impossible_terminals() -> None:
    with pytest.raises(ValueError):
        _progress_message(stage="transcription")
    with pytest.raises(ValueError):
        _progress_message(status="paused")
    with pytest.raises(ValueError):
        _progress_message(stage="vision", status="succeeded", completed=None, total=None)


def test_progress_decode_rejects_tampered_revision() -> None:
    envelope = json.loads(encode_message(_progress_message()))
    envelope["payload"]["completed"] = 2  # 리비전을 다시 계산하지 않았다
    with pytest.raises(ValueError):
        decode_message(json.dumps(envelope) + "\n")


# ─────────────────────────────────────────────────────────────
#  발행기 — job 루트 게이트와 무해한 실패
# ─────────────────────────────────────────────────────────────


def _publisher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, env: dict[str, str]
) -> tuple[project_commands._ProgressPublisher, Path, Path]:
    """엔진 job 모양(`<job>/workspace`)의 발행기 — 환경값은 시험이 정한다."""
    for name in (_JOB_DIR_ENV, _JOB_ID_ENV, _ENGINE_INVOCATION_ENV):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    job_root = tmp_path / "engine-job"
    workspace = job_root / "workspace"
    workspace.mkdir(parents=True)
    publisher = project_commands._ProgressPublisher(
        workspace, "c" * 64, "d" * 64, "attempt-publisher"
    )
    return publisher, workspace, job_root


def test_job_root_publication_requires_both_engine_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, workspace, job_root = _publisher(
        tmp_path, monkeypatch, env={_JOB_DIR_ENV: str(tmp_path / "engine-job")}
    )
    publisher.boundary("preparation")
    assert (workspace / _PROGRESS).is_file()
    assert not (job_root / _PROGRESS).exists(), "job 신원 없이는 job 루트에 쓰지 않는다"


def test_job_root_sink_is_bound_to_this_workspace_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """남은 환경값이 남의 job 루트를 가리키면 job 루트 발행은 일어나지 않는다."""
    stranger = tmp_path / "someone-elses-job"
    stranger.mkdir()
    publisher, workspace, job_root = _publisher(
        tmp_path,
        monkeypatch,
        env={_JOB_DIR_ENV: str(stranger), _JOB_ID_ENV: "roundtrip-progress"},
    )
    publisher.boundary("audio")
    assert (workspace / _PROGRESS).is_file()
    assert not (stranger / _PROGRESS).exists()
    assert not (job_root / _PROGRESS).exists()

    not_a_directory = tmp_path / "plain-file"
    not_a_directory.write_text("x", encoding="utf-8")
    for name in (_JOB_DIR_ENV,):
        monkeypatch.setenv(name, str(not_a_directory))
    publisher.boundary("audio")
    assert not (not_a_directory.parent / _PROGRESS).exists(), "파일 자리는 job 루트가 아니다"


def test_terminated_attempts_close_every_branch_without_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, workspace, job_root = _publisher(
        tmp_path,
        monkeypatch,
        env={
            _JOB_DIR_ENV: str(tmp_path / "engine-job"),
            _JOB_ID_ENV: "roundtrip-progress",
            _ENGINE_INVOCATION_ENV: "inv-publisher",
        },
    )
    publisher.boundary("vision", completed=1, total=2)
    publisher.terminate("failed")
    snapshot = json.loads((job_root / _PROGRESS).read_text(encoding="utf-8"))
    assert snapshot["payload"]["status"] == "failed"
    assert snapshot["payload"]["stage"] == "vision"
    assert snapshot["payload"]["completed"] is None  # 종료 경계는 계수 없이 닫는다
    publisher.terminate("cancelled")
    assert (
        json.loads((workspace / _PROGRESS).read_text(encoding="utf-8"))["payload"]["status"]
        == "cancelled"
    )


def test_publish_failure_is_reported_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    publisher, workspace, _ = _publisher(
        tmp_path,
        monkeypatch,
        env={_JOB_DIR_ENV: str(tmp_path / "engine-job"), _JOB_ID_ENV: "roundtrip-progress"},
    )

    def exploding(path: Path, payload: bytes) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr(project_commands, "_write_bytes", exploding)
    publisher.boundary("vision", completed=0, total=2)  # 예외가 새지 않는다
    assert not (workspace / _PROGRESS).exists()


# ─────────────────────────────────────────────────────────────
#  진입점 — 호출 신원이 실제 실행길을 지나 진행에 실리는가
# ─────────────────────────────────────────────────────────────


@pytest.fixture()
def engine() -> ModuleType:
    if os.name != "posix":
        # 개발 엔진(`macos/scripts/`)은 POSIX 전용이다 — `fcntl` 잠금(W-84a).
        pytest.skip("POSIX 전용 개발 엔진(macos/scripts)")
    saved = {
        name: os.environ.get(name) for name in (_JOB_DIR_ENV, _JOB_ID_ENV, _ENGINE_INVOCATION_ENV)
    }
    module = importlib.util.spec_from_file_location(
        "vlogsrt_engine_progress_entrypoint",
        Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py",
    )
    assert module is not None and module.loader is not None
    created = importlib.util.module_from_spec(module)
    module.loader.exec_module(created)
    yield created
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


def _stage_entrypoint_job(tmp_path: Path) -> Path:
    job_dir = tmp_path / "engine-job"
    job_dir.mkdir()
    shutil.copyfile(_single_clip_project(tmp_path), job_dir / "input.fcpxml")
    return job_dir


def test_entrypoint_inspect_only_run_leaves_no_progress(tmp_path: Path, engine: ModuleType) -> None:
    """승인 없는 첫 실행(receive·inspect)에는 시도가 없다 — 진행 파일도 없다."""
    job_dir = _stage_entrypoint_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir), "--invocation-id", "inv-a"]) == 0
    assert os.environ[_JOB_ID_ENV] == "job-1"
    assert os.environ[_ENGINE_INVOCATION_ENV] == "inv-a"
    assert not (job_dir / _PROGRESS).exists()
    assert not (job_dir / "workspace" / _PROGRESS).exists()


def test_entrypoint_carries_invocation_identity_into_job_root_progress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    job_dir = _stage_entrypoint_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir), "--invocation-id", "inv-a"]) == 0
    estimate = scope_of(job_dir / "workspace")
    (job_dir / "analysis-authorization.jsonl").write_text(
        encode_message(
            AnalysisAuthorization(
                input_revision=estimate.input_revision,
                settings_sha256=estimate.settings_sha256,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-28T06:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)
    assert engine.main(["analyze", "job-1", str(job_dir), "--invocation-id", "inv-b"]) == 0
    payload = _payload(_read_progress(job_dir))
    assert payload["job_id"] == "job-1"
    assert payload["invocation_id"] == "inv-b"
    assert payload["status"] == "succeeded"
    assert payload["stage"] == "publication"


def test_entrypoint_rejects_malformed_invocation_token(tmp_path: Path, engine: ModuleType) -> None:
    job_dir = _stage_entrypoint_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir), "--invocation-id", "bad token"]) == 2
    assert engine.main(["analyze", "job-1", str(job_dir), "--invocation-id"]) == 2
    assert (
        engine.main(
            ["analyze", "job-1", str(job_dir), "--invocation-id", "a", "--invocation-id", "b"]
        )
        == 2
    )


def test_entrypoint_accepts_status_and_invocation_together(
    tmp_path: Path, engine: ModuleType
) -> None:
    """상태 기록과 호출 신원 짝이 함께 와도 계약 위반이 아니다(S2A-3)."""
    job_dir = _stage_entrypoint_job(tmp_path)
    status_dir = tmp_path / "vlogsrt-status-token9"
    status_dir.mkdir()
    status = status_dir / "token9.status"
    assert (
        engine.main(["analyze", "job-1", str(job_dir), str(status), "--invocation-id", "inv-9"])
        == 0
    )
    assert status.read_text(encoding="utf-8") == "VLOGSRT-ENGINE-STATUS token9 0\n"
    assert os.environ[_ENGINE_INVOCATION_ENV] == "inv-9"
