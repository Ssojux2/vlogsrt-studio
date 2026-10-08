"""취소 계약의 회귀 시험 — 앱이 기록한 CancellationRequest가 남은 호출을 멈춘다.

스탠드얀 `project analyze --online` 경로를 끝까지 돌린다. 러너 세트과
`commands`·`cli`는 진짜고 가짜는 바깥 경계뿐이다(test_project_e2e.py의 기법
복제): ffmpeg/PCM/프레임/ONNX/전사기/비전 클라이언트. `openai.OpenAI` 생성에
가드를 심어 유료 경로가 열리면 즉시 실패한다.
"""

from __future__ import annotations

import array
import hashlib
import json
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from urllib.parse import quote

import pytest

from vlogsrt import cli, preprocess, transcribe
from vlogsrt.config import Config
from vlogsrt.project import commands as project_commands
from vlogsrt.project import pcm as pcm_module
from vlogsrt.project import runner
from vlogsrt.project.vision import sampling
from vlogsrt.project.workflow_protocol import (
    ActualUsage,
    AnalysisAttempt,
    AnalysisAuthorization,
    AnalysisEstimate,
    CancellationRequest,
    WorkflowJournal,
    encode_message,
)
from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, LiveTranscriber, TranscriptionJob

RATE = 16000
CLIP_SECONDS = 12
FD30 = Fraction(1, 30)
SEGMENTS = ((0.5, 1.0, "인사"), (10.5, 11.0, "마무리"))


@pytest.fixture(autouse=True)
def supported_audio_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 합성 미디어는 오디오 스트림 하나다(S2A-2 위상 관문의 전제).

    inspect·유료 analyze가 실제 위상 검사를 지나므로 확인 함수만 1스트림으로
    대신한다 — 위상 검사 자체의 실패·거절 경로는 test_media_topology.py가 든다.
    """
    from vlogsrt.project import topology

    monkeypatch.setattr(topology, "probe_audio_stream_count", lambda source: 1)


@pytest.fixture(autouse=True)
def synthetic_stt_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """엔드포인트 신원이 inspect·analyze에서 같은 값을 보게 한다(S2B-2b).

    추정 지문이 실제 엔드포인트를 묶는 지금, inspect(패치 전)과 analyze(패치
    후)이 다른 값을 보면 정당한 드리프트 거부가 된다 — 이 파일의 시험들이 보려는
    것(취소)과 무관하므로 파일 전체에서 한 값으로 못 박는다.
    """
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")


def _file_uri(path: Path) -> str:
    return "file://" + quote(str(path))


def _project(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-cancellation-media")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip.mov" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(media)}"/>\n    </asset>\n'
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="취소 프로젝트" uid="PROJECT-CANCEL">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s"'
        ' duration="12s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "cancel.fcpxml"
    path.write_text(body, encoding="utf-8")
    return path


def _two_clip_project(tmp_path: Path) -> Path:
    """서로 다른 미디어 두 개를 잇는 프로젝트 — 창이 둘 이상 나와야 하는 시험용."""
    media_a = tmp_path / "clip-a.mov"
    media_b = tmp_path / "clip-b.mov"
    media_a.write_bytes(b"synthetic-cancellation-media-a")
    media_b.write_bytes(b"synthetic-cancellation-media-b")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip-a.mov" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(media_a)}"/>\n    </asset>\n    '
        '<asset id="r3" name="clip-b.mov" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(media_b)}"/>\n    </asset>\n'
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="취소 프로젝트" uid="PROJECT-CANCEL">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s"'
        ' duration="12s"/>\n'
        '            <asset-clip ref="r3" name="클립1" offset="12s" start="0s"'
        ' duration="12s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "cancel-two.fcpxml"
    path.write_text(body, encoding="utf-8")
    return path


def _no_audio_project(tmp_path: Path) -> Path:
    """오디오가 없는 프로젝트 — 전사도 Luna 창도 없이 review_required까지 간다."""
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-cancellation-media")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip.mov" start="0s" duration="600s" hasAudio="0"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(media)}"/>\n    </asset>\n'
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="취소 프로젝트" uid="PROJECT-CANCEL">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s"'
        ' duration="12s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "cancel-noaudio.fcpxml"
    path.write_text(body, encoding="utf-8")
    return path


def _write_config(directory: Path) -> Path:
    path = directory / "test.project.toml"
    path.write_text("schema_version = 1\n", encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
#  가짜 경계 — 전사기 · ffmpeg/PCM · 프레임 · ONNX (test_project_e2e.py 기법)
# ─────────────────────────────────────────────────────────────


@dataclass
class _Scripted:
    whisper_calls: list[TranscriptionJob] = field(default_factory=list)

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


def _patch_paid_boundaries(
    monkeypatch: pytest.MonkeyPatch,
    luna_responses: list[dict],
    vision_seen: dict[str, object] | None = None,
) -> None:
    """온라인 경로의 유료 문을 전부 가짜로 막는다 — 키 값은 절대 만들지 않는다.

    `vision_seen`을 주면 실제 비전 공장이 받은 kwargs를 기록한다(S2A-2 호환성
    증명) — 공장은 가짜로 갈아끼우되 소비 지점과 값은 진짜 경로로 잰다.
    """
    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
    # 유료 역할 게이트가 공급자 앞에서 비전 역할도 잰다(S2A-2) — 합성값으로 채운다.
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://example.invalid")

    def vision_client(**kwargs: object) -> object:
        if vision_seen is not None:
            vision_seen.update(kwargs)
        return object()

    monkeypatch.setattr(transcribe, "create_vision_client", vision_client)

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


class _SdkFace:
    """가짜 SDK의 실제 호출 자리 — `audio.transcriptions.create` 한 군데만 산다."""

    def __init__(self, sdk: _SdkFake) -> None:
        self._sdk = sdk
        self.transcriptions = self

    def create(self, **kwargs: object) -> dict[str, object]:
        self._sdk.transcription_calls.append(kwargs)
        model = str(kwargs.get("model", ""))
        payload: dict[str, object] = {"duration": float(CLIP_SECONDS)}
        if model == "gpt-transcribe":  # Config.verify_model 기본값 — 글자 교정 호출
            payload["text"] = "인사 마무리"
        else:
            payload["segments"] = [
                {"start": start, "end": end, "text": text} for start, end, text in SEGMENTS
            ]
        return payload


class _ResponsesFace:
    def __init__(self, sdk: _SdkFake) -> None:
        self._sdk = sdk

    def create(self, **kwargs: object) -> dict[str, object]:
        self._sdk.responses_calls.append(kwargs)
        return {
            "output_text": json.dumps(
                {"choice": "keep", "confidence": 0.5, "evidence_frame_ids": []}
            ),
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }


class _SdkFake:
    """`openai.OpenAI` 생성자 자리의 가짜 SDK — 관찰자·원장은 이 안에서 움직인다."""

    def __init__(self) -> None:
        self.transcription_calls: list[dict[str, object]] = []
        self.responses_calls: list[dict[str, object]] = []

    def __call__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        max_retries: int = -1,
        timeout: float | None = None,
    ) -> _SdkFake:
        assert max_retries == 0, "SDK 내부 재시도는 관찰 경계를 우회한다"
        assert timeout is not None and timeout > 0, "멈춘 요청이 작업을 붙잡지 않는다"
        return self

    @property
    def audio(self) -> _SdkFace:
        return _SdkFace(self)

    @property
    def responses(self) -> _ResponsesFace:
        return _ResponsesFace(self)


def _patch_sdk_paid_boundary(monkeypatch: pytest.MonkeyPatch) -> _SdkFake:
    """유료 경계 전부를 실제 SDK 호출 자리로 가짜로 채운다 — 과금 단언용(S2B-2b).

    `create_vision_client`는 진짜로 지나간다 — 가짜 SDK(응답 경계 포함)가 그 안에서
    만들어져야 Luna 호출이 실제 자리까지 닿는다.
    """
    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://example.invalid")
    sdk = _SdkFake()
    import openai

    monkeypatch.setattr(openai, "OpenAI", sdk)
    return sdk


def _authorization_file(tmp_path: Path, workspace: Path) -> Path:
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    path = tmp_path / "authorization.jsonl"
    path.write_text(
        encode_message(
            AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-21T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    return path


def _analyze_argv(workspace: Path, authorization: Path) -> list[str]:
    return [
        "project",
        "analyze",
        str(workspace),
        "--online",
        "--authorization",
        str(authorization),
    ]


def _running_attempt_id(workspace: Path) -> str:
    running = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt) and row.status == "running"
    ]
    assert len(running) == 1
    return running[0].attempt_id


def test_mid_run_cancellation_stops_remaining_calls_and_closes_the_journal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg 단계 뒤에 기록된 취소가 전사·Luna 호출을 막고 상태를 cancelled로 닫는다."""
    input_path = _project(tmp_path)
    workspace = tmp_path / "workspace"
    project_config = _write_config(tmp_path)
    argv_tail = ["--project-config", str(project_config)]
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0

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
                attempt_id=_running_attempt_id(workspace),
                requested_at="2026-09-21T04:00:30Z",
                reason="user",
            ),
        )
        return prepared

    monkeypatch.setattr(runner, "prepare_audio", prepare_then_cancel)
    argv = _analyze_argv(workspace, _authorization_file(tmp_path, workspace)) + argv_tail
    assert cli.main(argv) == 2
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    # 취소가 확인된 뒤에는 Luna 호출이 한 번도 없다 — 취소 뒤 새 호출 차단.
    assert luna_calls == []
    # 취소 뒤 같은 저널로 재시작할 수 없다 — 종료 상태는 재사용 불가다.
    argv = _analyze_argv(workspace, _authorization_file(tmp_path, workspace)) + argv_tail
    assert cli.main(argv) == 2


def test_cancellation_for_another_attempt_does_not_stop_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """다른 시도·다른 입력 리비전의 취소 기록이 현재 실행을 가로채는 회귀를 잡는다."""
    input_path = _project(tmp_path)
    workspace = tmp_path / "workspace"
    project_config = _write_config(tmp_path)
    argv_tail = ["--project-config", str(project_config)]
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0
    journal = WorkflowJournal.open(workspace / "workflow.jsonl")
    (workspace / "cancellation.jsonl").write_text(
        encode_message(
            CancellationRequest.create(
                input_revision=journal.revision.revision,
                attempt_id="attempt-something-else",
                requested_at="2026-09-21T04:00:30Z",
                reason="active_project_changed",
            )
        )
        + encode_message(
            CancellationRequest.create(
                input_revision="b" * 64,
                attempt_id="attempt-irrelevant",
                requested_at="2026-09-21T04:00:31Z",
                reason="user",
            )
        ),
        encoding="utf-8",
    )

    _patch_boundaries(monkeypatch)
    # 과금 단언이 있는 시험이다 — 전사기·Luna을 통째로 바꾸면 회계를 증명하지
    # 못하므로 가짜 SDK를 **실제 호출 자리**(openai.OpenAI → create)에 앉힌다.
    sdk = _patch_sdk_paid_boundary(monkeypatch)

    argv = _analyze_argv(workspace, _authorization_file(tmp_path, workspace)) + argv_tail
    assert cli.main(argv) == 0
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "succeeded"]
    # 이 실행과 무관한 취소 기록은 Luna 호출을 막지 않는다.
    assert sdk.responses_calls, "취소와 무관한 실행인데 Luna 호출이 일어나지 않았다"
    usage = [
        row
        for row in project_commands._read_messages(workspace / "analysis-usage.jsonl")
        if isinstance(row, ActualUsage)
    ]
    # 사용량 줄은 원장 확정의 투영이다 — 전사 실측(창 없음)과 Luna 실측(창 있음)이
    # 실제 SDK 호출 수와 정확히 짝을 이룬다.
    whisper_rows = [row for row in usage if row.window_id is None]
    assert whisper_rows and all(row.model for row in whisper_rows)
    luna_rows = [row for row in usage if row.window_id is not None]
    assert len(luna_rows) == len(sdk.responses_calls)


def test_receive_rejects_multi_project_fcpxml(tmp_path: Path) -> None:
    """프로젝트가 둘 이상인 문서가 한 작업공간으로 들어오는 회귀를 잡는다."""
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-media")
    two_projects = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE fcpxml>
<fcpxml version="1.14">
  <resources>
    <format id="r1" name="FFVideoFormat1080p25" frameDuration="1/25s"/>
    <asset id="r2" name="clip.mov" uid="ASSET-1" start="0s" duration="12s" hasAudio="1" format="r1">
      <media-rep kind="original-media" src="{media.as_uri()}"/>
    </asset>
  </resources>
  <library><event name="Event" uid="EVENT-1">
    <project name="A" uid="PROJECT-A">
      <sequence format="r1" duration="12s" tcStart="0s"><spine>
        <asset-clip ref="r2" name="clip" offset="0s" start="0s" duration="12s"/>
      </spine></sequence>
    </project>
    <project name="B" uid="PROJECT-B">
      <sequence format="r1" duration="12s" tcStart="0s"><spine>
        <asset-clip ref="r2" name="clip" offset="0s" start="0s" duration="12s"/>
      </spine></sequence>
    </project>
  </event></library>
</fcpxml>
"""
    path = tmp_path / "two.fcpxml"
    path.write_text(two_projects, encoding="utf-8")
    workspace = tmp_path / "workspace-two"
    assert cli.main(["project", "receive", str(path), "--workspace", str(workspace)]) == 2
    assert not (workspace / "workflow.jsonl").exists()


def test_wildcard_cancellation_stops_any_attempt_for_its_revision(
    tmp_path: Path, monkeypatch
) -> None:
    """시도 ID를 모르는 앱 취소(attempt_id=*)가 현재 시도를 멈추는 회귀를 잡는다."""
    input_path = _project(tmp_path)
    workspace = tmp_path / "workspace"
    project_config = _write_config(tmp_path)
    argv_tail = ["--project-config", str(project_config)]
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

    real_prepare = runner.prepare_audio

    def prepare_then_wildcard_cancel(*args: object, **kwargs: object) -> object:
        prepared = real_prepare(*args, **kwargs)
        revision = WorkflowJournal.open(workspace / "workflow.jsonl").revision.revision
        project_commands._append_message(
            workspace / "cancellation.jsonl",
            CancellationRequest.create(
                input_revision=revision,
                attempt_id="*",
                requested_at="2026-09-21T04:05:00Z",
                reason="user",
            ),
        )
        return prepared

    monkeypatch.setattr(runner, "prepare_audio", prepare_then_wildcard_cancel)
    assert (
        cli.main(_analyze_argv(workspace, _authorization_file(tmp_path, workspace)) + argv_tail)
        == 2
    )
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    assert luna_calls == []


# ─────────────────────────────────────────────────────────────
#  엔진 잡 루트 관측 — 앱 CancellationStore가 남기는 곳을 엔진이 본다(S1A)
# ─────────────────────────────────────────────────────────────

_JOB_DIR_ENV = "VLOGSRT_ENGINE_JOB_DIR"


def _record_root_cancellation(job_root: Path, workspace: Path) -> None:
    """앱 CancellationStore와 같은 모양으로 job 루트에만 남긴다 — workspace는 비워둔다.

    앱은 사용자 취소를 `attempt_id="*"`와 진행 중인 입력 리비전으로 기록한다.
    엔진 진입점이 job 루트를 `_JOB_DIR_ENV`로 알리므로 실행 중 기록도 보인다.
    """
    revision = WorkflowJournal.open(workspace / "workflow.jsonl").revision.revision
    project_commands._append_message(
        job_root / "cancellation.jsonl",
        CancellationRequest.create(
            input_revision=revision,
            attempt_id="*",
            requested_at="2026-09-27T05:00:00Z",
            reason="user",
        ),
    )


class _RootCancellingTranscriber:
    """첫 위임 호출이 끝난 뒤 job 루트에 취소를 남긴다 — 앱의 실행 중 취소 흉내."""

    def __init__(self, inner: _Scripted, record: Callable[[], None]) -> None:
        self._inner = inner
        self._record = record
        self.kinds: list[str] = []

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.kinds.append(job.kind)
        responses = self._inner.transcribe(job, cfg)
        if len(self.kinds) == 1:
            self._record()
        return responses


def _engine_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, input_path: Path) -> Path:
    """엔진이 잡는 job 폴더 모양(`<job>/workspace`)을 만들고 루트를 알린다."""
    job_root = tmp_path / "engine-job"
    workspace = job_root / "workspace"
    project_config = _write_config(tmp_path)
    argv_tail = ["--project-config", str(project_config)]
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0
    monkeypatch.setenv(_JOB_DIR_ENV, str(job_root))
    return job_root


def _engine_analyze(tmp_path: Path, job_root: Path) -> tuple[list[str], Path]:
    workspace = job_root / "workspace"
    argv_tail = ["--project-config", str(_write_config(tmp_path))]
    return (
        _analyze_argv(workspace, _authorization_file(tmp_path, workspace)) + argv_tail,
        workspace,
    )


def test_root_cancellation_after_the_first_transcription_call_blocks_every_later_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """첫 전사 위임 뒤 job 루트에 남긴 취소가 두 번째 전사·Luna 호출을 막는다."""
    job_root = _engine_job(tmp_path, monkeypatch, _project(tmp_path))
    workspace = job_root / "workspace"

    _patch_boundaries(monkeypatch)
    cancelling = _RootCancellingTranscriber(
        _Scripted(), lambda: _record_root_cancellation(job_root, workspace)
    )
    monkeypatch.setattr(transcribe, "get_transcriber", lambda *args, **kwargs: cancelling)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

    argv, workspace = _engine_analyze(tmp_path, job_root)
    assert cli.main(argv) == 2
    # 취소는 첫 1차 전사가 끝난 뒤에 왔다 — 그 뒤의 어떤 위임(재전사·두 번째
    # 모델)도 일어나지 않는다.
    assert cancelling.kinds == [FIXTURE_KIND]
    assert luna_calls == []
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    # 관측 근거: 취소는 job 루트에만 있다 — workspace 복사가 아니라 루트 경로로 읽었다.
    assert (job_root / "cancellation.jsonl").is_file()
    assert not (workspace / "cancellation.jsonl").exists()


def test_root_cancellation_during_frame_sampling_blocks_the_next_luna_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """창 게이트가 지난 뒤 프레임 뽑기 동안 남은 취소가 Luna API 호출을 막는다."""
    job_root = _engine_job(tmp_path, monkeypatch, _two_clip_project(tmp_path))
    workspace = job_root / "workspace"

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

    real_sample = project_commands.sample_frames
    sample_calls: list[object] = []

    def sample_then_cancel(*args: object, **kwargs: object) -> object:
        samples = real_sample(*args, **kwargs)
        sample_calls.append(samples)
        # 첫 창의 Luna 호출은 이미 끝났고, 둘째 창의 프레임을 뽑는 지금이다 —
        # 창 loop의 취소 확인은 지나갔으니 Luna 직전 게이트가 잡아야 한다.
        if len(sample_calls) == 2:
            _record_root_cancellation(job_root, workspace)
        return samples

    monkeypatch.setattr(project_commands, "sample_frames", sample_then_cancel)

    argv, workspace = _engine_analyze(tmp_path, job_root)
    assert cli.main(argv) == 2
    assert len(sample_calls) >= 2, "둘째 창이 있어야 Luna 직전 게이트를 잰다"
    assert len(luna_calls) == 1
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"


def test_root_cancellation_for_foreign_revisions_does_not_stop_this_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """job 루트의 다른 리비전·다른 시도 기록은 현재 실행을 가로채지 않는다."""
    job_root = _engine_job(tmp_path, monkeypatch, _project(tmp_path))
    workspace = job_root / "workspace"
    journal = WorkflowJournal.open(workspace / "workflow.jsonl")
    (job_root / "cancellation.jsonl").write_text(
        encode_message(
            CancellationRequest.create(
                input_revision=journal.revision.revision,
                attempt_id="attempt-something-else",
                requested_at="2026-09-27T05:00:30Z",
                reason="user",
            )
        )
        + encode_message(
            CancellationRequest.create(
                input_revision="b" * 64,
                attempt_id="*",
                requested_at="2026-09-27T05:00:31Z",
                reason="active_project_changed",
            )
        )
        + encode_message(
            CancellationRequest.create(
                input_revision="0" * 64,
                attempt_id="*",
                requested_at="2026-09-27T05:00:32Z",
                reason="user",
            )
        ),
        encoding="utf-8",
    )

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

    argv, workspace = _engine_analyze(tmp_path, job_root)
    assert cli.main(argv) == 0
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_root_cancellation_reaches_the_live_transcriber_chunk_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LiveTranscriber 안의 조각 게이트가 잡 루트 취소를 보고 실행을 cancelled로 닫는다.

    첫 조각 요청이 진행 중인 동안 앱이 취소를 남기는 모양이다 — 위임 호출 게이트가
    아니라 조각 단위 게이트가 잡아야 한다(한 위임에 조각이 여럿일 때).
    """
    media = tmp_path / "clip-long.mov"
    media.write_bytes(b"synthetic-cancellation-media-long")
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        '<asset id="r2" name="clip-long.mov" start="0s" duration="600s" hasAudio="1"'
        ' format="r1">\n'
        f'      <media-rep kind="original-media" src="{_file_uri(media)}"/>\n    </asset>\n'
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="취소 프로젝트" uid="PROJECT-CANCEL-LONG">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        "          <spine>\n"
        '            <asset-clip ref="r2" name="클립0" offset="0s" start="0s"'
        ' duration="300s"/>\n'
        "          </spine>\n        </sequence>\n      </project>\n    </event>\n"
        "  </library>\n</fcpxml>\n"
    )
    input_path = tmp_path / "cancel-long.fcpxml"
    input_path.write_text(body, encoding="utf-8")
    job_root = _engine_job(tmp_path, monkeypatch, input_path)
    workspace = job_root / "workspace"

    # 300초 PCM — 조각이 둘이 되어 둘째 조각 직전 게이트가 온다.
    _patch_boundaries(monkeypatch)

    def long_pcm(
        source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        pcm = _pattern_pcm(300 * RATE)
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

    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", long_pcm)

    class RecordingClient:
        """실제 LiveTranscriber 밑에 깔리는 클라이언트 — 첫 조각 요청 중에 취소를 남긴다."""

        def __init__(self) -> None:
            self.audio = self
            self.transcriptions = self
            self.calls: list[dict[str, object]] = []

        def create(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                _record_root_cancellation(job_root, workspace)
            return {
                "segments": [
                    {"start": start, "end": end, "text": text} for start, end, text in SEGMENTS
                ]
            }

    client = RecordingClient()
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: client)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

    argv, workspace = _engine_analyze(tmp_path, job_root)
    assert cli.main(argv) == 2
    # 첫 조각만 나갔다 — 둘째 조각 요청은 조각 게이트가 막았다.
    assert len(client.calls) == 1
    assert luna_calls == []
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"


def test_root_cancellation_during_the_final_luna_call_closes_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """마지막 Luna 호출이 진행 중인 사이 남겨진 취소가 성공 발행으로 못 간다(S1A-R1).

    마지막 창의 호출 직전 게이트는 이미 지났고 그 뒤에는 아무 게이트도 없다 —
    발행(candidates.jsonl·review_required) 앞의 마지막 확인이 이를 닫는다.
    이미 남은 증거(판정·사용량)는 지우지 않는다.
    """
    job_root = _engine_job(tmp_path, monkeypatch, _project(tmp_path))
    workspace = job_root / "workspace"

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)
    argv, workspace = _engine_analyze(tmp_path, job_root)
    inner_luna = transcribe.request_luna_vision

    def luna_then_cancel(*args: object, **kwargs: object) -> object:
        response = inner_luna(*args, **kwargs)
        if len(luna_calls) == 1:  # 이 프로젝트의 창은 하나 — 이 호출이 마지막이다
            _record_root_cancellation(job_root, workspace)
        return response

    monkeypatch.setattr(transcribe, "request_luna_vision", luna_then_cancel)

    assert cli.main(argv) == 2
    assert len(luna_calls) == 1
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    # 발행은 막고 증거는 남긴다 — 판정 기록은 그대로, 후보 발행은 없다.
    assert not (workspace / "candidates.jsonl").exists()
    assert (workspace / "vision-decisions.jsonl").is_file()
    # 취소된 시도는 성공 집계를 남기지 않는다 — 앱이 취소를 성공으로 읽으면 안 된다(S1B).
    assert not (workspace / "analysis-result.jsonl").exists()


def test_root_cancellation_after_sources_with_no_windows_closes_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """창이 하나도 없는 실행에서 마지막 소스 뒤의 취소도 성공 발행을 막는다(S1A-R1).

    오디오가 없는 프로젝트는 전사도 Luna 호출도 없다 — 창 loop은 한 번도 돌지
    않으므로 발행 앞의 마지막 확인이 유일한 사후 게이트다.
    """
    job_root = _engine_job(tmp_path, monkeypatch, _no_audio_project(tmp_path))
    workspace = job_root / "workspace"

    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)
    real_analyze = runner.analyze_audio

    def analyze_then_cancel(*args: object, **kwargs: object) -> object:
        analyzed = real_analyze(*args, **kwargs)
        _record_root_cancellation(job_root, job_root / "workspace")
        return analyzed

    monkeypatch.setattr(runner, "analyze_audio", analyze_then_cancel)

    argv, workspace = _engine_analyze(tmp_path, job_root)
    assert cli.main(argv) == 2
    assert luna_calls == []
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    assert not (workspace / "candidates.jsonl").exists()


def test_input_bound_pre_receive_cancellation_stops_analyze_before_model_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """receive 전의 취소는 스테이징된 input.fcpxml 해시로 job에 묶여 분석을 막는다(S1A-R1).

    앱은 리비전을 아직 모른다 — 엔진의 input_sha256과 같은 값으로 묶으면 all-zero
    와일드카드 없이도 이 job의 첫 모델 호출 앞에서 정확히 걸린다.
    """
    input_path = _project(tmp_path)
    job_root = tmp_path / "engine-job"
    job_root.mkdir()
    staged_input = job_root / "input.fcpxml"
    shutil.copyfile(input_path, staged_input)
    input_sha256 = hashlib.sha256(staged_input.read_bytes()).hexdigest()
    project_commands._append_message(
        job_root / "cancellation.jsonl",
        CancellationRequest.create(
            input_revision=input_sha256,
            attempt_id="*",
            requested_at="2026-09-27T06:00:00Z",
            reason="user",
        ),
    )

    workspace = job_root / "workspace"
    project_config = _write_config(tmp_path)
    argv_tail = ["--project-config", str(project_config)]
    assert cli.main(["project", "receive", str(staged_input), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0
    monkeypatch.setenv(_JOB_DIR_ENV, str(job_root))

    _patch_boundaries(monkeypatch)
    scripted = _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

    argv, workspace = _engine_analyze(tmp_path, job_root)
    assert cli.main(argv) == 2
    # 첫 게이트에서 막는다 — 모델 호출은 하나도 나가지 않는다.
    assert scripted.whisper_calls == []
    assert luna_calls == []
    attempts = [
        row
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]
    assert [row.status for row in attempts] == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    assert not (workspace / "candidates.jsonl").exists()


def _keep_luna_responses(count: int = 8) -> list[dict]:
    return [
        {
            "output_text": json.dumps(
                {"choice": "keep", "confidence": 0.5, "evidence_frame_ids": []}
            ),
            "usage": {"input_tokens": 100, "output_tokens": 10},
        }
        for _ in range(count)
    ]


def test_staged_vision_consumer_separates_v2_roles_from_the_legacy_shared_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실제 소비자가 받는 비전 자격 증명(S2A-2 호환성 보존).

    마커 없는 직접 실행은 기본선(c96e0d5)의 계약 그대로 — 공유 키와 base URL가
    비전 공장으로 간다. V2 앱 운반이 적용된 실행은 별도 역할 값(비전 키·엔드
    포인트)만 간다. SDK 생성은 가짜로 막히고 네트워크는 0회다.
    """
    from vlogsrt import env

    # 마커 없는 경로 — 실제 파이프라인이 비전 공장까지 간다.
    input_path = _project(tmp_path)
    workspace = tmp_path / "workspace"
    project_config = _write_config(tmp_path)
    argv_tail = ["--project-config", str(project_config)]
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert cli.main(["project", "inspect", str(workspace), *argv_tail]) == 0
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    legacy_seen: dict[str, object] = {}
    _patch_paid_boundaries(monkeypatch, _keep_luna_responses(), legacy_seen)
    assert (
        cli.main(_analyze_argv(workspace, _authorization_file(tmp_path, workspace)) + argv_tail)
        == 0
    )
    assert legacy_seen == {"api_key": "test-key", "base_url": "https://example.invalid"}

    # V2 앱 운반 — 실제 적용 + 유효한 권한 문서 아래에서 같은 파이프라인.
    second = tmp_path / "second"
    second.mkdir()
    input_path = _project(second)
    v2_workspace = second / "workspace"
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(v2_workspace)]) == 0
    (v2_workspace / "media-grant.json").write_text(
        json.dumps(
            {
                "grant_revision": project_commands._swift_grant_revision([str(second)]),
                "roots": [str(second)],
            }
        ),
        encoding="utf-8",
    )
    v2_seen: dict[str, object] = {}
    # V2 적용은 패치 **뒤에, inspect보다 앞에** 온다 — 패치가 환경 이름을 덮어쓰면
    # 역할 값이 섞이고(실제 순서: 경계 가짜 → 실제 운반 적용 → 실행), 추정 지문이
    # 엔드포인트를 묶는 지금 inspect와 analyze가 같은 역할 라우팅을 봐야 한다.
    _patch_paid_boundaries(monkeypatch, _keep_luna_responses(), v2_seen)
    env.apply_role_credentials(
        "synthetic-stt-key", "synthetic-vision-key", "https://example.invalid/v1"
    )
    try:
        assert cli.main(["project", "inspect", str(v2_workspace), *argv_tail]) == 0
        assert (
            cli.main(
                _analyze_argv(v2_workspace, _authorization_file(second, v2_workspace)) + argv_tail
            )
            == 0
        )
        assert v2_seen == {
            "api_key": "synthetic-vision-key",
            "base_url": "https://example.invalid/v1",
        }
    finally:
        env._applied_vision_key_name = None  # 과정 표식을 시험이 치운다
