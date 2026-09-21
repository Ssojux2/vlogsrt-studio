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
from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, TranscriptionJob

RATE = 16000
CLIP_SECONDS = 12
FD30 = Fraction(1, 30)
SEGMENTS = ((0.5, 1.0, "인사"), (10.5, 11.0, "마무리"))


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


def _patch_paid_boundaries(monkeypatch: pytest.MonkeyPatch, luna_responses: list[dict]) -> None:
    """온라인 경로의 유료 문을 전부 가짜로 막는다 — 키 값은 절대 만들지 않는다."""
    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
    monkeypatch.setattr(transcribe, "create_vision_client", lambda **kwargs: object())

    def fake_luna(client: object, *, model: str, payload: dict, online: bool) -> dict:
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
    _stub_transcriber(monkeypatch)
    luna_calls: list[dict] = []
    _patch_paid_boundaries(monkeypatch, luna_calls)

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
    usage = [
        row
        for row in project_commands._read_messages(workspace / "analysis-usage.jsonl")
        if isinstance(row, ActualUsage)
    ]
    whisper_rows = [row for row in usage if row.window_id is None]
    assert whisper_rows and all(row.model for row in whisper_rows)
    if luna_calls:
        luna_rows = [row for row in usage if row.window_id is not None]
        assert len(luna_rows) == len(luna_calls)


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
