"""STAB-S1B — 분석 결과의 명시적 상태 회귀 시험.

비전 판정이 success/error/skipped(·legacy) 상태와 정돈된 사유 코드를 가지고,
시도마다 집계 결과(analysis-result.jsonl)가 남는다. 모델·상류 실패가 "후보 0건
성공"으로 위장되는 결함을 잡는다. 스탠드얼론 `project analyze --online` 경로를
끝까지 돌린다(test_workflow_cancellation.py의 기법 복제) — 진짜는
`commands`·`cli`, 가짜는 바깥 경계뿐이다.
"""

from __future__ import annotations

import array
import hashlib
import json
import re
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
from vlogsrt.project.workflow_analysis import (
    CandidateSeed,
    LunaWindow,
    aggregate_analysis_outcome,
    error_window_decision,
    normalize_luna_response,
    skipped_window_decision,
)
from vlogsrt.project.workflow_protocol import (
    AnalysisAttempt,
    AnalysisEstimate,
    AnalysisResult,
    UpstreamStages,
    VisionWindowDecision,
    WorkflowJournal,
    decode_message,
    encode_message,
)
from vlogsrt.transcribe import FIXTURE_KIND, ChunkResponse, TranscriptionJob

RATE = 16000
CLIP_SECONDS = 12
FD30 = Fraction(1, 30)
SEGMENTS = ((0.5, 1.0, "인사"), (10.5, 11.0, "마무리"))
REASON_CODE = re.compile(r"^[a-z][a-z0-9_]*$")


@pytest.fixture(autouse=True)
def supported_audio_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 합성 미디어는 오디오 스트림 하나다(S2A-2 위상 관문의 전제).

    `_prepare`의 inspect와 유료 analyze가 실제 위상 검사를 지나므로 확인
    함수만 1스트림으로 대신한다 — 검사 자체의 경로는 test_media_topology.py가 든다.
    """
    from vlogsrt.project import topology

    monkeypatch.setattr(topology, "probe_audio_stream_count", lambda source: 1)


# ─────────────────────────────────────────────────────────────
#  입력 문서 — 한 클립 · 두 클립 · 무오디오
# ─────────────────────────────────────────────────────────────


def _fcpxml(medias: list[tuple[str, str]], *, has_audio: bool = True) -> str:
    media_rep = '      <media-rep kind="original-media" src="file://{src}"/>\n    </asset>\n    '

    def asset(index: int, name: str, path: str) -> str:
        src = quote(str(path))
        return (
            f'<asset id="r{index + 2}" name="{name}" start="0s" duration="600s"'
            f' hasAudio="{int(has_audio)}" format="r1">\n' + media_rep.format(src=src)
        )

    assets = "".join(asset(index, name, path) for index, (name, path) in enumerate(medias))
    clips = "".join(
        f'<asset-clip ref="r{index + 2}" name="클립{index}" offset="{index * 12}s"'
        f' start="0s" duration="12s"/>\n            '
        for index in range(len(medias))
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        f'{assets}</resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="결과 프로젝트" uid="PROJECT-OUTCOME">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        f"          <spine>\n            {clips}</spine>\n        </sequence>\n"
        "      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )


def _project(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-outcome-media")
    path = tmp_path / "outcome.fcpxml"
    path.write_text(_fcpxml([("clip.mov", str(media))]), encoding="utf-8")
    return path


def _two_clip_project(tmp_path: Path) -> Path:
    media_a = tmp_path / "clip-a.mov"
    media_b = tmp_path / "clip-b.mov"
    media_a.write_bytes(b"synthetic-outcome-media-a")
    media_b.write_bytes(b"synthetic-outcome-media-b")
    path = tmp_path / "outcome-two.fcpxml"
    path.write_text(
        _fcpxml([("clip-a.mov", str(media_a)), ("clip-b.mov", str(media_b))]),
        encoding="utf-8",
    )
    return path


def _no_audio_project(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-outcome-media")
    path = tmp_path / "outcome-noaudio.fcpxml"
    path.write_text(_fcpxml([("clip.mov", str(media))], has_audio=False), encoding="utf-8")
    return path


def _write_config(directory: Path) -> Path:
    path = directory / "test.project.toml"
    path.write_text("schema_version = 1\n", encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────
#  가짜 경계 — 전사기 · ffmpeg/PCM · 프레임 · ONNX · Luna
# ─────────────────────────────────────────────────────────────


@dataclass
class _Scripted:
    fail: bool = False
    kinds: list[str] = field(default_factory=list)

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.kinds.append(job.kind)
        if self.fail:
            raise RuntimeError("synthetic transcription failure")
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


class _FailingVadSession:
    def run(self, output_names: object, feeds: dict[str, object]) -> list[_FakeArray]:
        raise RuntimeError("synthetic vad failure")


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


class _LunaScript:
    """창 순서대로 응답을 내는 가짜 Luna — 마지막에 예외를 던질 수도 있다."""

    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.calls = 0

    def __call__(self, client: object, *, model: str, payload: dict, online: bool) -> dict:
        response = self.responses[min(self.calls, len(self.responses) - 1)]
        self.calls += 1
        if isinstance(response, BaseException):
            raise response
        assert isinstance(response, dict)
        return response


class _ProposingLuna:
    """요청 문면에 적힌 첫 후보를 propose_cut로 답한다 — 실패 창 지정도 가능하다.

    창 판정 요청의 input_text 문면에는 후보 ID(`- <id>: ...`)와 각 프레임의
    `frame_id=`가 그대로 적혀 있다(build_luna_payload). 그대로 골라 유효한 판정을
    만든다 — 프레임 ID는 창의 요청 프레임 목록 안이므로 근거 검증을 통과한다.
    """

    def __init__(self, fail_windows: set[int] | None = None) -> None:
        self.fail_windows = fail_windows or set()
        self.calls = 0

    def __call__(self, client: object, *, model: str, payload: dict, online: bool) -> dict:
        index = self.calls
        self.calls += 1
        if index in self.fail_windows:
            raise RuntimeError("luna flaked")
        content = payload["input"][0]["content"]
        texts = [item["text"] for item in content if item.get("type") == "input_text"]
        candidate = re.search(r"- ([0-9a-f]{64}): ", texts[0])
        assert candidate is not None
        # 근거 프레임 ID는 프레임을 앞서는 input_text 문면에 적혀 있다.
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


def _keep_response() -> dict:
    return {
        "output_text": json.dumps({"choice": "keep", "confidence": 0.5, "evidence_frame_ids": []}),
        "usage": {"input_tokens": 100, "output_tokens": 10},
    }


def _patch_paid_boundaries(monkeypatch: pytest.MonkeyPatch, luna: _LunaScript) -> None:
    monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
    monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
    # 유료 역할 게이트가 공급자 앞에서 비전 역할도 잰다(S2A-2) — 합성값으로 채운다.
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://example.invalid")
    monkeypatch.setattr(transcribe, "create_vision_client", lambda **kwargs: object())
    monkeypatch.setattr(transcribe, "request_luna_vision", luna)

    import openai

    def boom(*args: object, **kwargs: object) -> None:
        raise AssertionError("openai.OpenAI가 불렸다 — 유료 경로가 열렸다")

    monkeypatch.setattr(openai, "OpenAI", boom)


def _stub_transcriber(monkeypatch: pytest.MonkeyPatch, *, fail: bool = False) -> _Scripted:
    scripted = _Scripted(fail=fail)
    monkeypatch.setattr(transcribe, "get_transcriber", lambda *args, **kwargs: scripted)
    return scripted


def _analyze(
    tmp_path: Path,
    workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    luna: _LunaScript | _ProposingLuna,
    *,
    after_boundaries: Callable[[], None] | None = None,
) -> int:
    _patch_boundaries(monkeypatch)
    if after_boundaries is not None:
        # 경계 가짜를 심은 뒤 시험 고유의 덮어쓰기(프레임 실패 등)가 와야 한다.
        after_boundaries()
    _stub_transcriber(monkeypatch)
    _patch_paid_boundaries(monkeypatch, luna)
    authorization = tmp_path / "authorization.jsonl"
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    return cli.main(
        [
            "project",
            "analyze",
            str(workspace),
            "--online",
            "--authorization",
            str(authorization),
            "--project-config",
            str(_write_config(tmp_path)),
        ]
    )


def _attempts(workspace: Path) -> list[str]:
    return [
        row.status
        for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
        if isinstance(row, AnalysisAttempt)
    ]


def _result(workspace: Path) -> AnalysisResult:
    message = project_commands._read_one(workspace / "analysis-result.jsonl")
    assert isinstance(message, AnalysisResult)
    return message


def _decisions(workspace: Path) -> list[VisionWindowDecision]:
    path = workspace / "vision-decisions.jsonl"
    return [
        row
        for row in project_commands._read_messages(path)
        if isinstance(row, VisionWindowDecision)
    ]


# ─────────────────────────────────────────────────────────────
#  정규화 — 상태 · 사유 코드 · 안전 닫힘
# ─────────────────────────────────────────────────────────────


def _window() -> LunaWindow:
    return LunaWindow("a" * 64, "window-1", ("candidate",), ("f1",), "전사", ("c" * 64,))


def test_normalize_separates_success_error_and_skipped_states() -> None:
    """유효 keep은 success, API/형식 결함은 error — 상태 없이 전부 keep으로 보이는 회귀를 잡는다."""
    window = _window()
    ok = normalize_luna_response(
        window, {"output_text": '{"choice":"keep","confidence":0.9,"evidence_frame_ids":[]}'}
    )
    assert ok.status == "success"
    assert ok.reason_code == "ok"
    assert ok.choice == "keep"

    api = normalize_luna_response(window, RuntimeError("503 /v1/responses timeout"))
    assert api.status == "error"
    assert api.reason_code == "api_exception"
    assert api.choice == "keep"
    assert api.candidate_id is None
    assert api.evidence_frame_ids == ()
    assert api.confidence == "0"

    empty = normalize_luna_response(window, {})
    assert empty.status == "error"
    assert empty.reason_code == "empty_output"

    malformed = normalize_luna_response(window, {"output_text": "not json"})
    assert malformed.status == "error"
    assert malformed.reason_code == "malformed_response"

    unknown = normalize_luna_response(
        window,
        {"output_text": '{"choice":"no-such-candidate","confidence":0.9,"evidence_frame_ids":[]}'},
    )
    assert unknown.status == "error"
    assert unknown.reason_code == "unknown_candidate"

    evidence = normalize_luna_response(
        window,
        {"output_text": '{"choice":"candidate","confidence":0.9,"evidence_frame_ids":["f9"]}'},
    )
    assert evidence.status == "error"
    assert evidence.reason_code == "invalid_evidence_identity"

    # 후보를 댔는데 근거 프레임이 빈 판정도 error다 — 검증 예외가 실행을 무너뜨리지 않는다.
    bare = normalize_luna_response(
        window, {"output_text": '{"choice":"candidate","confidence":0.9,"evidence_frame_ids":[]}'}
    )
    assert bare.status == "error"
    assert bare.reason_code == "invalid_evidence"
    assert bare.choice == "keep"

    skipped = skipped_window_decision("a" * 64, "window-1", "cost_ceiling_exceeded")
    assert skipped.status == "skipped"
    assert skipped.reason_code == "cost_ceiling_exceeded"
    assert skipped.choice == "keep"
    assert skipped.candidate_id is None

    error = error_window_decision("a" * 64, "window-1")
    assert error.status == "error"
    assert error.reason_code == "window_error"


def test_decision_reasons_are_sanitized_and_hashes_preserved() -> None:
    """사유 코드는 정돈된 어휘뿐이고 원문 응답 해시는 그대로 살아 있다."""
    window = _window()
    secret_text = "Authorization: Bearer sk-super-secret-value"
    boom = RuntimeError(secret_text)
    decision = normalize_luna_response(window, boom)
    assert decision.reason_code == "api_exception"
    assert REASON_CODE.fullmatch(decision.reason_code)
    rendered = encode_message(decision)
    assert "sk-super-secret-value" not in rendered
    assert "RuntimeError" not in rendered
    # 해시 보존 — 원문 해시는 예외 타입에서만 만들어지던 기존 계산 그대로다.
    from vlogsrt.project.workflow_analysis import _response_hash

    assert decision.response_sha256 == _response_hash(boom)
    for row in (skipped_window_decision("a" * 64, "w", "cost_ceiling_exceeded"),):
        assert REASON_CODE.fullmatch(row.reason_code)


def test_error_and_skipped_decisions_never_yield_cut_candidates() -> None:
    """상태가 success가 아닌 판정은 어떤 경우도 컷 후보를 만들지 않는다."""
    from vlogsrt.project.workflow_analysis import finalize_candidate

    seed = CandidateSeed(
        "candidate",
        "window-1",
        "clip-a",
        None,  # type: ignore[arg-type]
        None,  # type: ignore[arg-type]
        "internal_silence",
        ("vad:no_speech",),
    )
    window = LunaWindow("a" * 64, "window-1", ("candidate",), ("f1",), "전사", ("c" * 64,))
    propose = normalize_luna_response(
        window,
        {"output_text": '{"choice":"candidate","confidence":0.9,"evidence_frame_ids":["f1"]}'},
    )
    assert propose.status == "success"
    assert finalize_candidate(seed, propose) is not None
    for decision in (
        normalize_luna_response(window, RuntimeError("boom")),
        skipped_window_decision("a" * 64, "window-1", "cost_ceiling_exceeded"),
        error_window_decision("a" * 64, "window-1"),
    ):
        assert finalize_candidate(seed, decision) is None


# ─────────────────────────────────────────────────────────────
#  집계 — 상태 규칙 (순수 계층)
# ─────────────────────────────────────────────────────────────


def _decision(
    window_id: str,
    status: str,
    *,
    reason_code: str = "ok",
    input_revision: str = "a" * 64,
) -> VisionWindowDecision:
    if status == "skipped":
        return skipped_window_decision(input_revision, window_id, reason_code)
    if status == "error":
        return error_window_decision(input_revision, window_id, reason_code)
    return normalize_luna_response(
        LunaWindow(input_revision, window_id, (), (), "", ()),
        {"output_text": '{"choice":"keep","confidence":0.9,"evidence_frame_ids":[]}'},
    )


def _aggregate(
    decisions: list[VisionWindowDecision],
    upstream: UpstreamStages,
    *,
    input_revision: str = "a" * 64,
) -> AnalysisResult:
    return aggregate_analysis_outcome(
        input_revision=input_revision,
        attempt_id="attempt-x",
        estimate_revision="e" * 64,
        decisions=tuple(decisions),
        windows_total=len(decisions),
        candidate_count=0,
        upstream=upstream,
        upstream_reasons=(),
    )


_CLEAN = UpstreamStages()
_ALL_AUDIO_FAILED = UpstreamStages(audio_total=1, audio_failed=1)
_ALL_VAD_FAILED = UpstreamStages(vad_total=1, vad_failed=1)
_ALL_TRANSCRIPTION_FAILED = UpstreamStages(transcription_total=1, transcription_failed=1)
_PARTIAL_AUDIO = UpstreamStages(audio_total=2, audio_failed=1)


def test_zero_windows_is_success_only_when_upstream_actually_succeeded() -> None:
    """창 0개 성공은 상류가 전부 성공했을 때뿐 — 무오디오·무발화와 실패를 갈라놓는다."""
    assert _aggregate([], _CLEAN).status == "success"
    # 전사는 성공했는데 VAD가 전부 실패 — 성공이 아니다.
    assert _aggregate([], _ALL_VAD_FAILED).status == "error"
    assert _aggregate([], _ALL_AUDIO_FAILED).status == "error"
    assert _aggregate([], _ALL_TRANSCRIPTION_FAILED).status == "error"
    # 상류 일부 실패 + 성공 커버리지 없음 — 부분이 아니라 실패다.
    assert _aggregate([], _PARTIAL_AUDIO).status == "error"
    # 창 0개인데 쓸 수 있는 소스가 하나라도 있으면 부분 실패다 — 전체 실패가 아니다.
    usable_neighbor = UpstreamStages(
        sources_total=2, sources_usable=1, audio_total=2, audio_failed=1
    )
    partial = _aggregate([], usable_neighbor)
    assert partial.status == "partial_error"
    assert partial.upstream_sources_usable == 1


def test_aggregate_partial_error_and_skip_accounting() -> None:
    """성공 커버리지가 있고 실패가 섞이면 partial_error — 건너뛴 창은 성공으로 세지 않는다."""
    assert (
        _aggregate(
            [_decision("w1", "success"), _decision("w2", "error")],
            _CLEAN,
        ).status
        == "partial_error"
    )
    assert (
        _aggregate(
            [
                _decision("w1", "success"),
                _decision("w2", "skipped", reason_code="cost_ceiling_exceeded"),
            ],
            _CLEAN,
        ).status
        == "partial_error"
    )
    # 전부 실패한 창 — 상류가 성공했어도 error다.
    all_failed = _aggregate([_decision("w1", "error"), _decision("w2", "error")], _CLEAN)
    assert all_failed.status == "error"
    assert all_failed.windows_successful == 0
    assert all_failed.windows_failed == 2
    # 전부 건너뛴 창도 성공이 아니다.
    all_skipped = _aggregate(
        [_decision("w1", "skipped", reason_code="cost_ceiling_exceeded")], _CLEAN
    )
    assert all_skipped.status == "error"
    assert all_skipped.windows_skipped == 1
    # 상류 일부 실패 + 창 성공 — partial_error.
    assert _aggregate([_decision("w1", "success")], _PARTIAL_AUDIO).status == "partial_error"
    # 사유 코드는 정렬·중복 제거된 채 남는다.
    mixed = _aggregate(
        [
            _decision("w1", "error", reason_code="api_exception"),
            _decision("w2", "error", reason_code="api_exception"),
        ],
        _CLEAN,
    )
    assert mixed.reason_codes == ("api_exception",)


def test_aggregate_success_requires_no_failures_anywhere() -> None:
    """success는 창·상류 어디에도 실패가 없을 때뿐이라고 스스로 못 박는다."""
    result = _aggregate([_decision("w1", "success")], _CLEAN)
    assert result.status == "success"
    assert result.windows_total == 1
    with pytest.raises(ValueError, match="success"):
        AnalysisResult.create(
            input_revision="a" * 64,
            attempt_id="attempt-x",
            estimate_revision="e" * 64,
            status="success",
            windows_total=1,
            windows_successful=1,
            windows_failed=0,
            windows_skipped=0,
            candidate_count=0,
            upstream=_ALL_VAD_FAILED,
            reason_codes=(),
        )


# ─────────────────────────────────────────────────────────────
#  프로토콜 — 집계 라운드트립 · 낡은 판정 읽기
# ─────────────────────────────────────────────────────────────


def test_analysis_result_round_trips_through_jsonl() -> None:
    result = _aggregate([_decision("w1", "success")], _CLEAN)
    line = encode_message(result)
    assert decode_message(line) == result
    assert '"type":"AnalysisResult"' in line


def test_legacy_decisions_decode_as_unknown_not_success() -> None:
    """1.1/1.2 판정 기록은 legacy/unknown — 새 성공 증거로 읽히지 않고 재실행도 하지 않는다."""
    from vlogsrt.project.workflow_protocol import ANALYSIS_PROTOCOL_VERSION

    decision = _decision("w1", "success")
    current = json.loads(encode_message(decision))
    # 1.2 기록의 리비전은 상태 없는 필드만 묶은 옛 공식이다 — legacy 상태로 만들면
    # 같은 공식이 나오므로 낡은 기록의 decision_revision을 그대로 검증할 수 있다.
    old_formula = VisionWindowDecision.create(
        input_revision=decision.input_revision,
        window_id=decision.window_id,
        choice=decision.choice,
        candidate_id=decision.candidate_id,
        evidence_frame_ids=decision.evidence_frame_ids,
        confidence=decision.confidence,
        response_sha256=decision.response_sha256,
        status="legacy",
        reason_code="legacy_no_outcome",
    )
    for legacy_version in ("1.1", "1.2"):
        payload = dict(current["payload"])
        payload.pop("status")
        payload.pop("reason_code")
        payload["decision_revision"] = old_formula.decision_revision
        stale = (
            json.dumps(
                {
                    "payload": payload,
                    "protocol_version": legacy_version,
                    "type": "VisionWindowDecision",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        decoded = decode_message(stale)
        assert isinstance(decoded, VisionWindowDecision)
        assert decoded.status == "legacy"
        assert decoded.reason_code == "legacy_no_outcome"
        assert decoded.decision_revision == old_formula.decision_revision
        from vlogsrt.project.workflow_analysis import finalize_candidate

        seed = CandidateSeed(
            "candidate",
            "w1",
            "clip-a",
            None,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            "internal_silence",
            (),
        )
        assert finalize_candidate(seed, decoded) is None
        assert decoded.window_id == decision.window_id
    # 지원 밖 버전은 여전히 거부다.
    for rejected in ("1.0", "9.0"):
        payload = dict(current["payload"])
        stale = (
            json.dumps(
                {"payload": payload, "protocol_version": rejected, "type": "VisionWindowDecision"},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        )
        with pytest.raises(ValueError, match="프로토콜"):
            decode_message(stale)
    # 1.3 봉투에서 결과 상태가 빠졌으면 깨진 기록이다 — legacy로 읽히지 않는다.
    payload = dict(current["payload"])
    payload.pop("status")
    payload.pop("reason_code")
    broken = (
        json.dumps(
            {"payload": payload, "protocol_version": "1.3", "type": "VisionWindowDecision"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    )
    with pytest.raises(ValueError, match="결과 상태"):
        decode_message(broken)
    assert ANALYSIS_PROTOCOL_VERSION == "1.3"


# ─────────────────────────────────────────────────────────────
#  통합 — 실제 진입점으로 상태·종료코드·기록을 잰다
# ─────────────────────────────────────────────────────────────


def _prepare(tmp_path: Path, input_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace)]) == 0
    assert (
        cli.main(
            [
                "project",
                "inspect",
                str(workspace),
                "--project-config",
                str(_write_config(tmp_path)),
            ]
        )
        == 0
    )
    return workspace


def test_all_window_failure_persists_error_result_and_stays_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """모든 창이 실패하면 후보 0건 성공이 아니라 실패 — 증거를 남기고 재시도 가능하다."""
    workspace = _prepare(tmp_path, _project(tmp_path))
    exit_code = _analyze(tmp_path, workspace, monkeypatch, _LunaScript([RuntimeError("luna down")]))
    assert exit_code == 2
    result = _result(workspace)
    assert result.status == "error"
    assert result.windows_total == 1
    assert result.windows_failed == 1
    assert result.windows_successful == 0
    assert result.candidate_count == 0
    assert result.reason_codes == ("api_exception",)
    assert _attempts(workspace) == ["pending", "running", "failed"]
    decisions = _decisions(workspace)
    assert [row.status for row in decisions] == ["error"]
    # 발행은 없다 — 후보가 없고 저널은 재시도 가능한 상태에 남는다.
    assert not (workspace / "candidates.jsonl").exists()
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "authorized"
    # 재시도 — 같은 입력으로 다시 돌면 성공으로 닫힌다.
    exit_code = _analyze(tmp_path, workspace, monkeypatch, _LunaScript([_keep_response()]))
    assert exit_code == 0
    assert _result(workspace).status == "success"
    assert _attempts(workspace) == [
        "pending",
        "running",
        "failed",
        "pending",
        "running",
        "succeeded",
    ]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_mixed_outcome_is_partial_error_with_retained_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """부분 실패는 검토 경로로 간다 — 성공 창의 안전 후보만 발행되고 경고가 남는다."""
    workspace = _prepare(tmp_path, _two_clip_project(tmp_path))
    luna = _ProposingLuna(fail_windows={1})
    exit_code = _analyze(tmp_path, workspace, monkeypatch, luna)
    assert exit_code == 0
    result = _result(workspace)
    assert result.status == "partial_error"
    assert result.windows_successful == 1
    assert result.windows_failed == 1
    assert result.candidate_count == 1
    assert result.reason_codes == ("api_exception",)
    assert _attempts(workspace) == ["pending", "running", "succeeded"]
    decisions = _decisions(workspace)
    assert [row.status for row in decisions] == ["success", "error"]
    # 발행된 후보는 성공한 창의 판정에 정확히 묶여 있다 — 실패 창의 후보는 없다.
    candidates = [
        row
        for row in project_commands._read_messages(workspace / "candidates.jsonl")
        if isinstance(row, project_commands.CutCandidate)
    ]
    assert len(candidates) == 1
    assert candidates[0].decision_revision == decisions[0].decision_revision
    assert candidates[0].window_id == decisions[0].window_id
    assert decisions[0].choice == "propose_cut"
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_window_exception_retains_earlier_window_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """뒤 창의 준비 예외가 앞 창의 완성 증거를 지우지 않는다."""
    workspace = _prepare(tmp_path, _two_clip_project(tmp_path))
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    _patch_paid_boundaries(monkeypatch, _LunaScript([_keep_response()]))
    real_sample = project_commands.sample_frames
    calls: list[object] = []

    def sample_then_fail(*args: object, **kwargs: object) -> object:
        if len(calls) >= 1:
            raise RuntimeError("synthetic frame sampling failure")
        samples = real_sample(*args, **kwargs)
        calls.append(samples)
        return samples

    monkeypatch.setattr(project_commands, "sample_frames", sample_then_fail)
    authorization = tmp_path / "authorization.jsonl"
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
                "--project-config",
                str(_write_config(tmp_path)),
            ]
        )
        == 0
    )
    result = _result(workspace)
    assert result.status == "partial_error"
    statuses = [row.status for row in _decisions(workspace)]
    assert statuses == ["success", "error"]
    assert _decisions(workspace)[1].reason_code == "window_error"
    assert result.reason_codes == ("window_error",)
    # 뒤 창의 실패가 앞 창의 증거·검토 가능성을 지우지 않는다.
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_valid_zero_window_no_audio_is_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """선언된 무오디오는 실패가 아니다 — 창 0개 성공으로, 미적용 단계는 보이는 그대로."""
    workspace = _prepare(tmp_path, _no_audio_project(tmp_path))
    exit_code = _analyze(tmp_path, workspace, monkeypatch, _LunaScript([]))
    assert exit_code == 0
    result = _result(workspace)
    assert result.status == "success"
    assert result.windows_total == 0
    assert result.candidate_count == 0
    # 무오디오는 시도도 실패도 아닌 미적용이다 — 성공 시도로 세지 않는다.
    # 세 단계 모두 total은 계획한 소스 전체(환산이 같다).
    assert result.upstream_audio_total == 1
    assert result.upstream_audio_failed == 0
    assert result.upstream_audio_skipped == 1
    assert result.upstream_transcription_total == 1
    assert result.upstream_transcription_skipped == 1
    assert result.upstream_vad_total == 1
    assert result.upstream_vad_skipped == 1
    assert result.upstream_sources_total == 1
    assert result.upstream_sources_usable == 0
    assert _attempts(workspace) == ["pending", "running", "succeeded"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_usable_zero_window_source_with_failed_source_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """좋은 무발화 소스 옆의 실패 소스는 전체 실패가 아니다 — 부분 실패다."""
    workspace = _prepare(tmp_path, _two_clip_project(tmp_path))

    def speech_everywhere_and_probe_fails_b() -> None:
        monkeypatch.setattr(runner, "create_cpu_session", lambda artifact: _ConstantSession(0.9))

        def probe(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
            if "clip-b" in source.name:
                raise RuntimeError(f"no stream: {source}")
            return _probe_stream(source, stream_index)

        monkeypatch.setattr(runner, "probe_audio_stream", probe)

    exit_code = _analyze(
        tmp_path,
        workspace,
        monkeypatch,
        _LunaScript([]),
        after_boundaries=speech_everywhere_and_probe_fails_b,
    )
    assert exit_code == 0
    result = _result(workspace)
    # 한 소스는 전사·VAD까지 성공(발화가 없어 창 0개), 다른 소스는 오디오 실패.
    assert result.status == "partial_error"
    assert result.windows_total == 0
    assert result.upstream_sources_total == 2
    assert result.upstream_sources_usable == 1
    assert result.upstream_audio_failed == 1
    assert "stream_unavailable" in result.reason_codes
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_stage_failure_before_windows_persists_sanitized_error_aggregate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """준비·분석 단계가 무너져도 정돈된 집계와 시도 기록이 남는다 — 원문 예외는 못 들어간다."""
    for stage_name, patch_stage in (
        ("preparation_error", "prepare_audio"),
        ("analysis_stage_error", "analyze_audio"),
    ):
        stage_dir = tmp_path / stage_name
        stage_dir.mkdir()
        workspace = _prepare(stage_dir, _project(stage_dir))
        _patch_boundaries(monkeypatch)

        def boom(*args: object, _stage: str = stage_name, **kwargs: object) -> object:
            raise RuntimeError(f"secret {_stage} detail /tmp/key.pem")

        # 단계별 패치는 반복마다 독립적으로 걸고 끝낸다 — 첫 반복의 준비 실패가
        # 둘째 반복(분석 실패)까지 남으면 안 된다.
        stage_patch = pytest.MonkeyPatch()
        stage_patch.setattr(runner, patch_stage, boom)
        monkeypatch.setattr(project_commands.env, "require_api_key", lambda **kwargs: "test-key")
        monkeypatch.setattr(project_commands.env, "base_url", lambda: "https://example.invalid")
        monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
        monkeypatch.setenv("GEMINI_BASE_URL", "https://example.invalid")
        monkeypatch.setattr(transcribe, "create_vision_client", lambda **kwargs: object())
        authorization = tmp_path / f"authorization-{stage_name}.jsonl"
        estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
        assert isinstance(estimate, AnalysisEstimate)
        authorization.write_text(
            encode_message(
                project_commands.AnalysisAuthorization(
                    input_revision=estimate.input_revision,
                    estimate_revision=estimate.estimate_revision,
                    pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                    media_grant_revision=estimate.media_grant_revision,
                    authorized_at="2026-09-27T04:00:00Z",
                )
            ),
            encoding="utf-8",
        )
        exit_code = cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
                "--project-config",
                str(_write_config(tmp_path / stage_name)),
            ]
        )
        assert exit_code == 2
        result = _result(workspace)
        assert result.status == "error"
        assert stage_name in result.reason_codes
        attempts_text = (workspace / "analysis-attempts.jsonl").read_text(encoding="utf-8")
        assert "secret" not in attempts_text
        assert "key.pem" not in attempts_text
        assert stage_name in attempts_text
        assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "authorized"
        stage_patch.undo()


def test_all_audio_failure_with_zero_windows_is_error_not_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미디어 오디오를 열 수 없으면 창 0개 성공이 아니라 상류 실패 error다."""
    workspace = _prepare(tmp_path, _project(tmp_path))

    def fail_probe() -> None:
        def boom(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
            raise RuntimeError(f"no stream: {source}")

        monkeypatch.setattr(runner, "probe_audio_stream", boom)

    exit_code = _analyze(
        tmp_path, workspace, monkeypatch, _LunaScript([]), after_boundaries=fail_probe
    )
    assert exit_code == 2
    result = _result(workspace)
    assert result.status == "error"
    assert result.upstream_audio_total == 1
    assert result.upstream_audio_failed == 1
    assert result.windows_total == 0
    assert "stream_unavailable" in result.reason_codes
    assert _attempts(workspace) == ["pending", "running", "failed"]


def test_all_transcription_failure_with_zero_windows_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전사가 전부 실패하면 창 0개 성공이 아니라 error다."""
    workspace = _prepare(tmp_path, _project(tmp_path))
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch, fail=True)
    _patch_paid_boundaries(monkeypatch, _LunaScript([]))
    authorization = tmp_path / "authorization.jsonl"
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
                "--project-config",
                str(_write_config(tmp_path)),
            ]
        )
        == 2
    )
    result = _result(workspace)
    assert result.status == "error"
    assert result.upstream_transcription_total == 1
    assert result.upstream_transcription_failed == 1
    assert "transcription_error" in result.reason_codes
    assert _attempts(workspace) == ["pending", "running", "failed"]


def test_all_vad_failure_with_zero_windows_is_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """전사는 성공해도 VAD가 전부 실패하면 창 0개 성공이 아니다."""
    workspace = _prepare(tmp_path, _project(tmp_path))
    _patch_boundaries(monkeypatch)
    monkeypatch.setattr(runner, "create_cpu_session", lambda artifact: _FailingVadSession())
    _stub_transcriber(monkeypatch)
    _patch_paid_boundaries(monkeypatch, _LunaScript([]))
    authorization = tmp_path / "authorization.jsonl"
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
                "--project-config",
                str(_write_config(tmp_path)),
            ]
        )
        == 2
    )
    result = _result(workspace)
    assert result.status == "error"
    assert result.upstream_vad_total == 1
    assert result.upstream_vad_failed == 1
    assert result.upstream_transcription_failed == 0
    assert "vad_error" in result.reason_codes


def test_cost_ceiling_marks_windows_skipped_not_successful(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """비용 상한에 걸린 창은 skipped — 성공으로 세지 않고 나머지도 부르지 않는다."""
    workspace = _prepare(tmp_path, _two_clip_project(tmp_path))
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    luna = _LunaScript([_keep_response(), _keep_response()])
    _patch_paid_boundaries(monkeypatch, luna)
    # 첫 usage 기록이 상한을 넘게 만든다 — 첫 창 뒤 남은 창은 skipped다.
    monkeypatch.setattr(
        project_commands,
        "UsageLedger",
        lambda limit: _ZeroCeilingLedger(limit),
    )
    authorization = tmp_path / "authorization.jsonl"
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
                "--project-config",
                str(_write_config(tmp_path)),
            ]
        )
        == 2
    )
    result = _result(workspace)
    assert result.status == "error"
    assert result.windows_skipped == 2
    # 상한은 첫 사용량 기록(전사)에서 이미 걸렸다 — Luna 호출은 한 번도 없다.
    assert luna.calls == 0
    assert "cost_ceiling_exceeded" in result.reason_codes


class _ZeroCeilingLedger:
    """첫 기록에서 즉시 상한을 넘는 장부 — UsageLedger와 같은 계약."""

    def __init__(self, limit: object) -> None:
        self.limit = limit

    def record(self, usage: object) -> None:
        from vlogsrt.project.workflow_analysis import CostCeilingExceeded

        raise CostCeilingExceeded("실제 사용량이 승인 상한을 넘었다")


def test_cancellation_stays_cancelled_and_writes_no_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """취소는 비전 실패로 정규화되지 않는다 — 집계 결과도 남기지 않는다."""
    workspace = _prepare(tmp_path, _project(tmp_path))
    _patch_boundaries(monkeypatch)
    _stub_transcriber(monkeypatch)
    _patch_paid_boundaries(monkeypatch, _LunaScript([_keep_response()]))
    real_prepare = runner.prepare_audio

    def prepare_then_cancel(*args: object, **kwargs: object) -> object:
        prepared = real_prepare(*args, **kwargs)
        journal = WorkflowJournal.open(workspace / "workflow.jsonl")
        running = [
            row
            for row in project_commands._read_messages(workspace / "analysis-attempts.jsonl")
            if isinstance(row, AnalysisAttempt) and row.status == "running"
        ]
        project_commands._append_message(
            workspace / "cancellation.jsonl",
            project_commands.CancellationRequest.create(
                input_revision=journal.revision.revision,
                attempt_id=running[0].attempt_id,
                requested_at="2026-09-27T04:00:30Z",
                reason="user",
            ),
        )
        return prepared

    monkeypatch.setattr(runner, "prepare_audio", prepare_then_cancel)
    authorization = tmp_path / "authorization.jsonl"
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-27T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "project",
                "analyze",
                str(workspace),
                "--online",
                "--authorization",
                str(authorization),
                "--project-config",
                str(_write_config(tmp_path)),
            ]
        )
        == 2
    )
    assert _attempts(workspace) == ["pending", "running", "cancelled"]
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "cancelled"
    assert not (workspace / "analysis-result.jsonl").exists()
