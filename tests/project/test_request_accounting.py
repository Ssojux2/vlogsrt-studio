"""STAB-S2B-2b — 실제 SDK 호출 하나하나의 내구 예약·확정 회계 경계 시험.

가짜 SDK는 **실제 호출 자리**(`openai.OpenAI` → `audio.transcriptions.create`·
`responses.create`)에 앉는다. `request_luna_vision`이나 `Transcriber` 전체를 통째로
바꾸면 회계를 증명하지 못한다 — 관찰자·원장은 그 안쪽에서만 움직인다. 이 파일의
기법은 `test_analysis_outcomes.py`의 합성 미디어·경계 가짜를 그대로 잇는다.

이 파일이 증명하는 것:
- 유료 SDK 호출마다 호출 **전에** 내구 예약이 남고(예약이 곧 허가), 응답의 실측
  미터링으로 딱 한 번 확정된다.
- 실측이 승인 상한을 넘으면 그 사실이 기록으로 남고, 이후 예약은 전부 거부된다
  — 다음 유료 호출은 물리적으로 가지 않는다.
- `analysis-usage.jsonl`은 원장 기록의 멱등 투영이다(집계 whisper 청구·응답 뒤
  Luna 청구가 함께 남는 옛 모양은 더 이상 없다).
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any
from urllib.parse import quote

import pytest

from vlogsrt import cli, preprocess
from vlogsrt.project import commands as project_commands
from vlogsrt.project import pcm as pcm_module
from vlogsrt.project import request_accounting, runner
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.pricing import official_pricing_snapshot
from vlogsrt.project.vision import sampling
from vlogsrt.project.workflow_analysis import AnalysisSettings
from vlogsrt.project.workflow_protocol import (
    ActualUsage,
    AnalysisAuthorization,
    AnalysisEstimate,
    AnalysisResult,
    WorkflowJournal,
    encode_message,
)
from vlogsrt.transcribe import (
    RESPONSES_OPERATION,
    TRANSCRIPTION_OPERATION,
    ObserverFailure,
    RequestAttemptMeta,
)

RATE = 16000
CLIP_SECONDS = 12
FD30 = Fraction(1, 30)
SEGMENTS = ((0.5, 1.0, "인사"), (10.5, 11.0, "마무리"))

# 원장·투영의 자리 — commands가 쓰는 파일 이름 계약과 같다.
LEDGER_PATH = ("accounting", "budget-ledger.json")
USAGE_NAME = "analysis-usage.jsonl"
RESULT_NAME = "analysis-result.jsonl"


@pytest.fixture(autouse=True)
def supported_audio_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일의 합성 미디어는 오디오 스트림 하나다(위상 관문의 전제)."""
    from vlogsrt.project import topology

    monkeypatch.setattr(topology, "probe_audio_stream_count", lambda source: 1)


# ─────────────────────────────────────────────────────────────
#  합성 입력 — 두 클립(창 2개)
# ─────────────────────────────────────────────────────────────


def _two_clip_project(tmp_path: Path) -> Path:
    def asset(index: int, name: str, path: Path) -> str:
        src = quote(str(path))
        # 자원 길이는 클립과 같은 12초다 — 600초짜리 자원이면 고유 오디오 추정이
        # 1,200초로 뛰고, 옛 집계 게이트가 전사만으로 상한을 먹어버린다(RED가
        # Luna 경계에 도달하기 전에 죽는다).
        return (
            f'<asset id="r{index + 2}" name="{name}" start="0s" duration="12s" hasAudio="1"'
            f' format="r1">\n      <media-rep kind="original-media" src="file://{src}"/>\n'
            "    </asset>\n    "
        )

    media_a = tmp_path / "clip-a.mov"
    media_b = tmp_path / "clip-b.mov"
    media_a.write_bytes(b"synthetic-accounting-media-a")
    media_b.write_bytes(b"synthetic-accounting-media-b")
    assets = "".join(
        asset(index, name, path)
        for index, (name, path) in enumerate((("a.mov", media_a), ("b.mov", media_b)))
    )
    clips = "".join(
        f'<asset-clip ref="r{index + 2}" name="클립{index}" offset="{index * 12}s"'
        f' start="0s" duration="12s"/>\n            '
        for index in range(2)
    )
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        f'{assets}</resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="결과 프로젝트" uid="PROJECT-ACCOUNTING">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        f"          <spine>\n            {clips}</spine>\n        </sequence>\n"
        "      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "accounting-two.fcpxml"
    path.write_text(text, encoding="utf-8")
    return path


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
                str(_project_config(tmp_path)),
            ]
        )
        == 0
    )
    return workspace


def _project_config(directory: Path) -> Path:
    config = directory / "test.project.toml"
    if not config.is_file():
        config.write_text("schema_version = 1\n", encoding="utf-8")
    return config


# ─────────────────────────────────────────────────────────────
#  경계 가짜 — ffmpeg/PCM/ONNX/프레임(준비)과 SDK(실제 호출 자리)
# ─────────────────────────────────────────────────────────────


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


def _patch_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """준비 단계(ffmpeg·PCM·VAD·프레임 뽑기)의 합성 가짜 — 유료 호출 아니다."""
    import array

    def pattern_pcm(frames: int) -> bytes:
        values = array.array("h", ((frame * 3) % 28001 - 14000 for frame in range(frames)))
        return values.tobytes()

    def probe_stream(source: Path, stream_index: int) -> preprocess.AudioStreamInfo:
        return preprocess.AudioStreamInfo(
            stream_index=1, start_time=Fraction(0), sample_rate=48000, channels=1
        )

    def extract_pcm(
        source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
    ) -> preprocess.PcmExtraction:
        pcm = pattern_pcm(CLIP_SECONDS * RATE)
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

    monkeypatch.setattr(runner, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    monkeypatch.setattr(runner, "probe_audio_stream", probe_stream)
    monkeypatch.setattr(pcm_module, "extract_pcm_s16le", extract_pcm)
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


class _Api500(Exception):
    """재시도 판정이 알아보는 500 — `_is_retryable`이 status_code를 본다."""

    status_code = 500


@dataclass
class _FakeSdk:
    """`openai.OpenAI` 생성자 자리의 가짜 SDK — 실제 호출 경계에 앉는다.

    전사는 `client.audio.transcriptions.create`, Luna는 `client.responses.create`를
    그대로 탄다. 호출 인자·횟수를 기록하고, 응답의 미터링 칸(duration·usage)은
    시험이 정한 값으로 채운다 — 관찰자가 있으면 그 값으로 확정된다. 실패 주입은
    오디오(재시도 판정을 지나는 500)와 Luna(단발 오류) 각각에 있다.
    """

    audio_duration: float = float(CLIP_SECONDS)
    luna_usage: tuple[int, int] = (100, 10)
    audio_failures: list[Exception] = field(default_factory=list)
    luna_error: str | None = None
    transcription_calls: list[dict[str, object]] = field(default_factory=list)
    responses_calls: list[dict[str, object]] = field(default_factory=list)

    def __call__(
        self,
        *,
        api_key: str,
        base_url: str | None = None,
        max_retries: int = -1,
        timeout: float | None = None,
    ) -> _FakeSdk:
        # SDK 생성 인자도 게이트의 계약이다(max_retries=0) — 시험이 잠근다.
        assert max_retries == 0, "SDK 내부 재시도는 관찰 경계를 우회한다 — max_retries=0이어야 한다"
        assert timeout is not None and timeout > 0, "멈춘 요청이 작업을 붙잡지 않는다"
        return self

    @property
    def audio(self) -> _AudioApi:
        return _AudioApi(self)

    @property
    def responses(self) -> _ResponsesApi:
        return _ResponsesApi(self)


class _AudioApi:
    def __init__(self, sdk: _FakeSdk) -> None:
        self._sdk = sdk
        self.transcriptions = self

    def create(self, **kwargs: object) -> dict[str, object]:
        self._sdk.transcription_calls.append(kwargs)
        if self._sdk.audio_failures:
            raise self._sdk.audio_failures.pop(0)
        model = str(kwargs.get("model", ""))
        payload: dict[str, object] = {"duration": self._sdk.audio_duration}
        if model == "gpt-transcribe":  # Config.verify_model 기본값 — 글자 교정 호출
            payload["text"] = "인사 마무리"
        else:
            payload["segments"] = [
                {"start": start, "end": end, "text": text} for start, end, text in SEGMENTS
            ]
        return payload


class _ResponsesApi:
    def __init__(self, sdk: _FakeSdk) -> None:
        self._sdk = sdk

    def create(self, **kwargs: object) -> dict[str, object]:
        self._sdk.responses_calls.append(kwargs)
        if self._sdk.luna_error is not None:
            raise RuntimeError(self._sdk.luna_error)
        input_tokens, output_tokens = self._sdk.luna_usage
        return {
            "output_text": json.dumps(
                {"choice": "keep", "confidence": 0.5, "evidence_frame_ids": []}
            ),
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }


def _install_fake_sdk(monkeypatch: pytest.MonkeyPatch, sdk: _FakeSdk) -> _FakeSdk:
    import openai

    monkeypatch.setattr(openai, "OpenAI", sdk)
    # 유료 역할 게이트(require_paid_roles)와 전사기 생성이 읽는 합성 값 — 키가
    # 아니면 통과하는 문들만 채운다. base URL 값은 내구 신원이 묶는 대상이다.
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-stt-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://example.invalid")
    return sdk


def _analyze_online(tmp_path: Path, workspace: Path) -> int:
    estimate = project_commands._read_one(workspace / "analysis-estimate.jsonl")
    assert isinstance(estimate, AnalysisEstimate)
    authorization = tmp_path / "authorization.jsonl"
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate.input_revision,
                estimate_revision=estimate.estimate_revision,
                pricing_snapshot_revision=estimate.pricing_snapshot_revision,
                media_grant_revision=estimate.media_grant_revision,
                authorized_at="2026-09-28T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )
    config = _project_config(tmp_path)
    return cli.main(
        [
            "project",
            "analyze",
            str(workspace),
            "--online",
            "--authorization",
            str(authorization),
            "--project-config",
            str(config),
        ]
    )


def _result(workspace: Path) -> AnalysisResult:
    message = project_commands._read_one(workspace / RESULT_NAME)
    assert isinstance(message, AnalysisResult)
    return message


# ─────────────────────────────────────────────────────────────
#  회귀 — 예약 없는 유료 경로·무한 입장을 잡는다
# ─────────────────────────────────────────────────────────────


def test_paid_sdk_calls_reserve_durably_and_overrun_closes_further_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실측이 상한을 넘었는데 다음 유료 호출이 계속 가는 회귀를 잡는다.

    두 클립(창 2개)에서 첫 Luna 응답의 실측 사용량을 승인 상한보다 크게 만든다
    (입력 5,000만 토큰 = $10 ≫ 승인 상한 — 고유 오디오 24초 × 4회 전사 × 분당
    $0.0105 + 토큰 상한 ≈ $0.02). 올바른 동작: 첫 창 실측은 그대로 기록되고(초과
    증거를 버리지 않는다), 둘째 창의 예약은 거부되어 SDK 호출이 물리적으로 가지
    않는다. 원장 파일이 남지 않으면 예약 자체가 없다는 뜻이다.
    """
    workspace = _prepare(tmp_path, _two_clip_project(tmp_path))
    _patch_boundaries(monkeypatch)
    sdk = _install_fake_sdk(monkeypatch, _FakeSdk(luna_usage=(50_000_000, 10)))

    exit_code = _analyze_online(tmp_path, workspace)

    # 가짜 SDK 경계에 실제로 도달했다는 증거가 먼저다 — 이것 없이는 빈 RED다.
    assert sdk.transcription_calls, "전사 SDK 호출이 한 번도 지나지 않았다 — 경계 증거 없음"
    assert sdk.responses_calls, "Luna SDK 호출이 한 번도 지나지 않았다 — 경계 증거 없음"
    # 1) 내구 원장이 남는다 — 예약이 곧 허가인 기록의 정본. 옛 길은 어디에도
    #    이런 기록을 남기지 않는다.
    ledger = workspace.joinpath(*LEDGER_PATH)
    assert ledger.is_file(), "유료 SDK 호출을 냈는데 내구 예약 원장이 없다"
    # 2) 상한을 넘은 뒤에는 다음 유료 호출이 가지 않는다.
    assert len(sdk.responses_calls) == 1, "실측 초과 뒤 남은 창의 SDK 호출이 막히지 않았다"
    # 3) 첫 창은 성공 판정으로 남고(실측 초과는 그 창의 실패가 아니다) 나머지는
    #    미처리로 덮인다 — 부분 실패(exit 0)로 닫는다.
    assert exit_code == 0, "성공 창이 하나 있으므로 부분 실패(exit 0)로 닫혀야 한다"
    result = _result(workspace)
    assert result.windows_total == 2
    assert result.windows_successful == 1
    assert result.windows_skipped == 1
    assert "cost_ceiling_exceeded" in result.reason_codes
    assert WorkflowJournal.open(workspace / "workflow.jsonl").state == "review_required"


def test_same_job_in_a_new_root_cannot_reuse_the_old_authorization_for_paid_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """다른 개발 루트의 같은 job이 낡은 승인으로 유료 재실행을 여는 회귀를 잡는다.

    `DevelopmentJobStager.stage`는 승인 파일을 job 루트로 스테이징하고 엔진은
    새 루트에서 `inspect`를 다시 돌린다. 추정 신원이 **작업공간 자리**와 무관하면
    새 루트의 재계산 추정이 낡은 승인과 그대로 묶여 유료 호출이 열린다 — 소비가
    조용히 0으로 리셋되는 길이다. 승인은 이전 루트의 추정에만 묶여야 한다.
    """
    input_path = _two_clip_project(tmp_path)
    root_a = tmp_path / "external-a" / "Jobs" / "job-1"
    root_b = tmp_path / "external-b" / "Jobs" / "job-1"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    workspace_a = _prepare(root_a, input_path)
    _patch_boundaries(monkeypatch)
    sdk = _install_fake_sdk(monkeypatch, _FakeSdk())
    estimate_a = project_commands._read_one(workspace_a / "analysis-estimate.jsonl")
    assert isinstance(estimate_a, AnalysisEstimate)
    authorization = tmp_path / "authorization.jsonl"
    authorization.write_text(
        encode_message(
            project_commands.AnalysisAuthorization(
                input_revision=estimate_a.input_revision,
                estimate_revision=estimate_a.estimate_revision,
                pricing_snapshot_revision=estimate_a.pricing_snapshot_revision,
                media_grant_revision=estimate_a.media_grant_revision,
                authorized_at="2026-09-28T04:00:00Z",
            )
        ),
        encoding="utf-8",
    )

    # 같은 입력, 다른 루트 — 새 workspace에서 inspect를 다시 돌린 모양.
    workspace_b = root_b / "workspace"
    assert cli.main(["project", "receive", str(input_path), "--workspace", str(workspace_b)]) == 0
    assert (
        cli.main(
            [
                "project",
                "inspect",
                str(workspace_b),
                "--project-config",
                str(_project_config(tmp_path)),
            ]
        )
        == 0
    )
    estimate_b = project_commands._read_one(workspace_b / "analysis-estimate.jsonl")
    assert isinstance(estimate_b, AnalysisEstimate)
    assert estimate_b.input_revision == estimate_a.input_revision

    exit_code = cli.main(
        [
            "project",
            "analyze",
            str(workspace_b),
            "--online",
            "--authorization",
            str(authorization),
            "--project-config",
            str(_project_config(tmp_path)),
        ]
    )

    assert exit_code == 2, "다른 루트의 작업공간이 이전 루트의 승인으로 유료 호출을 열었다"
    assert sdk.responses_calls == [], "승인 거부보다 앞서 Luna SDK 호출이 일어났다"
    assert sdk.transcription_calls == [], "승인 거부보다 앞서 전사 SDK 호출이 일어났다"
    assert WorkflowJournal.open(workspace_b / "workflow.jsonl").state != "review_required"


# ─────────────────────────────────────────────────────────────
#  단위 — 문맥·관찰자를 직전 다는 회계 계약 시험
# ─────────────────────────────────────────────────────────────


def _unit_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    workspace.mkdir(parents=True)
    return workspace


def _authorization(revision: str = "a" * 64) -> AnalysisAuthorization:
    return AnalysisAuthorization(
        input_revision=revision,
        estimate_revision="b" * 64,
        pricing_snapshot_revision=official_pricing_snapshot().snapshot_revision,
        media_grant_revision="c" * 64,
        authorized_at="2026-09-28T05:00:00Z",
    )


def _open_accounting(
    workspace: Path,
    *,
    approval: str = "10",
    settings_sha: str = "d" * 64,
    attempt_id: str = "attempt-unit",
) -> request_accounting.AnalysisAccounting:
    return request_accounting.AnalysisAccounting.open(
        workspace=workspace,
        settings=AnalysisSettings(),
        pricing=official_pricing_snapshot(),
        authorization=_authorization(),
        settings_sha256=settings_sha,
        project_config=ProjectConfig(),
        attempt_id=attempt_id,
        approval_limit=Decimal(approval),
    )


def _audio_meta(attempt: str = "a1", model: str = "whisper-1", duration: float = 12.0) -> Any:
    return RequestAttemptMeta(
        attempt_id=attempt,
        operation=TRANSCRIPTION_OPERATION,
        model=model,
        kind="whisper",
        chunk_index=0,
        chunk_offset=0.0,
        chunk_duration=duration,
        options={"model": model, "response_format": "verbose_json"},
    )


def _luna_payload(text: str = "후보를 판정한다", images: int = 2, output: int = 256) -> dict:
    content: list[dict[str, object]] = [{"type": "input_text", "text": text}]
    content.extend(
        {"type": "input_image", "detail": "low", "image_url": "data:image/jpeg;base64,QUJD"}
        for _ in range(images)
    )
    return {
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": output,
        "reasoning": {"effort": "none"},
    }


def _luna_meta(payload: dict | None = None, attempt: str = "r1") -> Any:
    body = _luna_payload() if payload is None else payload
    return RequestAttemptMeta(
        attempt_id=attempt,
        operation=RESPONSES_OPERATION,
        model=official_pricing_snapshot().luna_model,
        options={"model": official_pricing_snapshot().luna_model, **body},
    )


def _usage_rows(workspace: Path) -> list[ActualUsage]:
    return [
        row
        for row in project_commands._read_messages(workspace / USAGE_NAME)
        if isinstance(row, ActualUsage)
    ]


def _ledger_states(workspace: Path) -> list[str]:
    document = json.loads((workspace.joinpath(*LEDGER_PATH)).read_text(encoding="utf-8"))
    return [row["state"] for row in document["records"]]


def test_per_model_metering_prices_reservations_and_projects_usage(tmp_path: Path) -> None:
    """whisper·gpt·Luna 실측이 제 가격으로 확정되고 투영에 그대로 나오는지 확인한다."""
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace)
    observer = context.observer()

    ticket = observer.authorize(_audio_meta("a1", pricing.whisper_model, 60.0))
    observer.on_response(ticket, _audio_meta("a1", pricing.whisper_model, 60.0), {"duration": 60.0})
    ticket = observer.authorize(_audio_meta("a2", pricing.gpt_transcribe_model, 30.0))
    observer.on_response(
        ticket, _audio_meta("a2", pricing.gpt_transcribe_model, 30.0), {"duration": 30.0}
    )
    window = context.window_observer("window-1")
    ticket = window.authorize(_luna_meta())
    window.on_response(ticket, _luna_meta(), {"usage": {"input_tokens": 100, "output_tokens": 10}})

    rows = _usage_rows(workspace)
    assert [row.model for row in rows] == [
        pricing.whisper_model,
        pricing.gpt_transcribe_model,
        pricing.luna_model,
    ]
    assert rows[0].cost == "0.006" and rows[0].audio_seconds == 60  # 60s × $0.006/분
    assert rows[1].cost == "0.00225"  # 30s × $0.0045/분
    assert rows[2].cost == "0.000032" and rows[2].window_id == "window-1"
    snapshot = context.snapshot()
    assert snapshot.measured_actual == Decimal("0.008282")
    assert snapshot.reserved_unresolved == Decimal("0")


def test_missing_unpriced_and_token_only_metering_stay_unknown_with_evidence(
    tmp_path: Path,
) -> None:
    """결측·토큰뿐 미터링은 0원이 아니라 미지수다 — 양은 증거로만 남는다."""
    workspace = _unit_workspace(tmp_path)
    context = _open_accounting(workspace)
    observer = context.observer()
    pricing = official_pricing_snapshot()

    meta = _audio_meta("a1", pricing.whisper_model)
    ticket = observer.authorize(meta)
    observer.on_response(ticket, meta, {"segments": []})  # duration 없음
    meta = _audio_meta("a2", pricing.whisper_model)
    ticket = observer.authorize(meta)
    observer.on_response(
        ticket,
        meta,
        {"usage": {"input_tokens": 500, "output_tokens": 20, "total_tokens": 520}},
    )
    window = context.window_observer("window-2")
    meta = _luna_meta(attempt="r1")
    ticket = window.authorize(meta)
    window.on_response(ticket, meta, {"output_text": "판정"})  # usage 없음

    snapshot = context.snapshot()
    assert snapshot.measured_actual == Decimal("0")
    assert snapshot.reserved_unresolved > Decimal("0")
    assert all(row.state == "unknown" for row in snapshot.records)
    assert _usage_rows(workspace) == []
    evidence = [
        json.loads(line)
        for line in (workspace / "accounting" / "unpriced-usage.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert [row["reason_code"] for row in evidence] == [
        "missing_usage",
        "token_only_audio_usage",
        "missing_or_invalid_usage",
    ]
    assert evidence[1]["input_tokens"] == 500 and evidence[1]["total_tokens"] == 520


def test_exact_budget_fits_and_next_denial_is_budget_not_fatal(tmp_path: Path) -> None:
    """남은 상한과 꼭 맞는 예약은 지나가고, 그다음은 예산 거부다(고장이 아니다)."""
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    # gpt 60초 = $0.0045 — 승인을 정확히 그 금액으로 맞춘다.
    context = _open_accounting(workspace, approval="0.0045")
    observer = context.observer()
    meta = _audio_meta("a1", pricing.gpt_transcribe_model, 60.0)
    ticket = observer.authorize(meta)
    observer.on_response(ticket, meta, {"duration": 60.0})
    assert context.snapshot().remaining_bound == Decimal("0")

    with pytest.raises(request_accounting.AccountingDenied):
        observer.authorize(_audio_meta("a3", pricing.gpt_transcribe_model, 1.0))
    # 예산 거부는 고장이 아니다 — 문맥은 열려 있고 재개입은 거부 사실로 갈린다.
    context.ensure_healthy()
    wrapped = ObserverFailure("관찰자 authorize 훅이 실패했다")
    raised = request_accounting.AccountingDenied("상한 초과")
    try:
        raise wrapped from raised
    except ObserverFailure as exc:
        assert request_accounting.is_budget_denial(exc) is True
    assert request_accounting.is_budget_denial(ObserverFailure("다른 실패")) is False


def test_crash_reopen_restores_measured_and_outstanding_and_identity_mismatch_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """크래시 등가 재오픈은 실측과 미결 예약을 되살리고, 신원이 다르면 닫는다."""
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace, settings_sha="e" * 64)
    observer = context.observer()
    meta = _audio_meta("a1", pricing.whisper_model, 60.0)
    observer.on_response(observer.authorize(meta), meta, {"duration": 60.0})
    # 둘째 예약은 응답을 못 받고 죽은 모양 — 미결로 남는다.
    observer.authorize(_audio_meta("a2", pricing.whisper_model, 12.0))

    reopened = _open_accounting(workspace, settings_sha="e" * 64)
    snapshot = reopened.snapshot()
    assert snapshot.measured_actual == Decimal("0.006")
    assert snapshot.reserved_unresolved == Decimal("0.0012")
    assert len(snapshot.records) == 2
    assert [row.cost for row in _usage_rows(workspace)] == ["0.006"]  # 투영도 재구축

    with pytest.raises(request_accounting.AccountingError):
        _open_accounting(workspace, settings_sha="f" * 64)  # 설정 신원이 다르다
    monkeypatch.setattr(request_accounting.env, "base_url", lambda: "https://other.example/v1")
    with pytest.raises(request_accounting.AccountingError):
        _open_accounting(workspace, settings_sha="e" * 64)  # 엔드포인트가 다르다


def test_legacy_usage_or_attempts_refuse_implicit_zero_start(tmp_path: Path) -> None:
    """원장 없는 과거 기록은 0원 시작이 아니다 — 명시적 조치를 요구한다."""
    workspace = _unit_workspace(tmp_path)
    (workspace / USAGE_NAME).write_text(
        encode_message(
            ActualUsage.create(
                input_revision="a" * 64,
                attempt_id="attempt-old",
                window_id=None,
                model="whisper-1",
                input_tokens=0,
                output_tokens=0,
                cost="0.001",
            )
        ),
        encoding="utf-8",
    )
    with pytest.raises(request_accounting.LegacyAccountingAmbiguous):
        _open_accounting(workspace, attempt_id="attempt-new")

    clean = _unit_workspace(tmp_path / "second")
    attempts = clean / "analysis-attempts.jsonl"
    attempts.parent.mkdir(parents=True, exist_ok=True)
    # 방금 붙은 이번 시도는 과거가 아니다 — 신원으로 구별해 통과시킨다.
    from vlogsrt.project.workflow_protocol import AnalysisAttempt

    attempts.write_text(
        encode_message(AnalysisAttempt("a" * 64, "b" * 64, "attempt-now", "running", "t", None)),
        encoding="utf-8",
    )
    _open_accounting(clean, attempt_id="attempt-now")

    stale = _unit_workspace(tmp_path / "third")
    (stale / "analysis-attempts.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (stale / "analysis-attempts.jsonl").write_text(
        encode_message(AnalysisAttempt("a" * 64, "b" * 64, "attempt-old", "failed", "t", "x")),
        encoding="utf-8",
    )
    with pytest.raises(request_accounting.LegacyAccountingAmbiguous):
        _open_accounting(stale, attempt_id="attempt-now")


def test_request_shape_and_dimension_bounds_fail_safely_before_reservation(
    tmp_path: Path,
) -> None:
    """증명 안 된 모양·과대 차원은 예약 없이 안전하게 실패한다."""
    workspace = _unit_workspace(tmp_path)
    context = _open_accounting(workspace)
    observer = context.observer()

    def payload_with(content: list[dict], *, extra: dict | None = None) -> dict:
        body: dict = {
            "input": [{"role": "user", "content": content}],
            "max_output_tokens": 256,
            "reasoning": {"effort": "none"},
        }
        if extra is not None:
            body.update(extra)
        return body

    cases = [
        _luna_payload(text="가" * 9_000),  # 글자 바이트 상한 초과
        _luna_payload(images=7),  # 이미지 상한 초과
        payload_with([{"type": "input_image", "detail": "high", "image_url": "data:x"}]),
        payload_with(
            [
                {
                    "type": "input_image",
                    "detail": "low",
                    "image_url": "data:x",
                    "caption": "끼워 넣은 글자",  # 계약 밖 마디 칸
                }
            ]
        ),
        payload_with(
            [{"type": "input_audio", "data": "끼워 넣은 소리"}]  # 증명 안 된 content 종류
        ),
        {
            "input": [{"role": "user", "content": [], "metadata": "x" * 500}],
            "max_output_tokens": 256,
            "reasoning": {"effort": "none"},
        },
        {
            "input": [{"role": "user", "content": []}, {"role": "user", "content": []}],
            "max_output_tokens": 256,
            "reasoning": {"effort": "none"},
        },
        payload_with([], extra={"instructions": "밖의 지시 문면"}),  # 밖의 인자
        _luna_payload(output=2_000),  # 출력 상한 초과
    ]
    for payload in cases:
        with pytest.raises(request_accounting.AccountingError):
            observer.authorize(_luna_meta(payload))
        assert context.snapshot().records == (), "거부가 기록을 남겼다 — 예약이 아니라 기각이다"
    # reasoning 여유가 없는 모양도 닫는다 — 출력 토큰이 판정에만 쓰이는 증거다.
    payload = _luna_payload()
    payload["reasoning"] = {"effort": "low"}
    with pytest.raises(request_accounting.AccountingError):
        observer.authorize(_luna_meta(payload))


def test_projection_or_ledger_failure_closes_the_paid_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """투영·원장 쓰기 실패는 창 실패가 아니라 문맥 전체를 닫는다."""
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace)
    observer = context.observer()
    meta = _audio_meta("a1", pricing.whisper_model, 60.0)
    ticket = observer.authorize(meta)

    def broken_projection(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(request_accounting, "_atomic_write_bytes", broken_projection)
    with pytest.raises(request_accounting.AccountingError):
        observer.on_response(ticket, meta, {"duration": 60.0})
    with pytest.raises(request_accounting.AccountingError):
        observer.authorize(_audio_meta("a2", pricing.whisper_model, 12.0))
    with pytest.raises(request_accounting.AccountingError):
        context.ensure_healthy()

    monkeypatch.undo()
    # 원장은 정본을 이미 갖고 있다 — 재오픈하면 투영이 다시 서고 기록은 살아 있다.
    reopened = _open_accounting(workspace)
    assert reopened.snapshot().measured_actual == Decimal("0.006")
    assert [row.cost for row in _usage_rows(workspace)] == ["0.006"]


def test_repeated_reconciliation_is_idempotent(tmp_path: Path) -> None:
    """같은 티켓의 확정 재현은 멱등이다 — 이중 청구도 유실도 없다."""
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace)
    observer = context.observer()
    meta = _audio_meta("a1", pricing.whisper_model, 60.0)
    ticket = observer.authorize(meta)
    for _ in range(3):
        observer.on_response(ticket, meta, {"duration": 60.0})
    assert context.snapshot().measured_actual == Decimal("0.006")
    assert len(_usage_rows(workspace)) == 1


def test_concurrent_authorize_reconcile_lose_no_records(tmp_path: Path) -> None:
    """병렬 조각이 서로의 예약·투영 줄을 지우거나 중복하지 않는다."""
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace)
    metas = [_audio_meta(f"a{index}", pricing.whisper_model, 12.0) for index in range(8)]

    def cycle(meta: Any) -> None:
        observer = context.observer()
        observer.on_response(observer.authorize(meta), meta, {"duration": 12.0})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(cycle, metas))
    snapshot = context.snapshot()
    assert len(snapshot.records) == 8
    assert {row.state for row in snapshot.records} == {"reconciled"}
    assert snapshot.measured_actual == Decimal("0.0096")  # 8 × 12s × $0.006/분
    assert len({row.request_id for row in snapshot.records}) == 8
    assert len(_usage_rows(workspace)) == 8


# ─────────────────────────────────────────────────────────────
#  명령 경로 — 재시도·창 실패 뒤 재개의 누적 회계
# ─────────────────────────────────────────────────────────────


def _single_clip_project(tmp_path: Path) -> Path:
    media = tmp_path / "clip.mov"
    media.write_bytes(b"synthetic-accounting-media")
    src = quote(str(media))
    text = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        '<format id="r1" name="FFVideoFormat1080p30" frameDuration="1/30s"'
        ' width="1920" height="1080"/>\n    '
        f'<asset id="r2" name="clip.mov" start="0s" duration="12s" hasAudio="1" format="r1">\n'
        f'      <media-rep kind="original-media" src="file://{src}"/>\n    </asset>\n    '
        '  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="결과 프로젝트" uid="PROJECT-ACCOUNTING-1">\n'
        '        <sequence format="r1" tcStart="0s" duration="600s">\n'
        '          <spine>\n            <asset-clip ref="r2" name="클립0" offset="0s"'
        ' start="0s" duration="12s"/>\n          </spine>\n        </sequence>\n'
        "      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "accounting-one.fcpxml"
    path.write_text(text, encoding="utf-8")
    return path


def test_failed_retry_attempt_stays_unknown_while_success_reconciles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재시도의 첫 시도는 미지수로 남고, 살아난 시도만 실측으로 확정된다."""
    workspace = _prepare(tmp_path, _single_clip_project(tmp_path))
    _patch_boundaries(monkeypatch)
    sdk = _install_fake_sdk(monkeypatch, _FakeSdk(audio_failures=[_Api500("first 500")]))

    exit_code = _analyze_online(tmp_path, workspace)

    assert exit_code == 0
    assert len(sdk.transcription_calls) >= 2, "실패한 시도가 재시도로 이어졌다는 증거"
    states = _ledger_states(workspace)
    assert "unknown" in states and "reconciled" in states
    # 투영은 확정만 싣는다 — 미지수가 0원 성공으로 새는 길이 없다.
    assert len(_usage_rows(workspace)) == states.count("reconciled")


def test_window_failure_then_retry_accumulates_charges_across_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """성공한 전사 뒤 창이 죽어도 지출은 남고, 재개는 누적으로 이어진다."""
    workspace = _prepare(tmp_path, _single_clip_project(tmp_path))
    _patch_boundaries(monkeypatch)
    sdk = _install_fake_sdk(monkeypatch, _FakeSdk(luna_error="luna down"))

    assert _analyze_online(tmp_path, workspace) == 2  # 전사 성공 · 창 전부 실패
    first_states = _ledger_states(workspace)
    assert "unknown" in first_states  # 죽은 창의 예약은 묶여 있다
    audio_charges = _usage_rows(workspace)
    assert audio_charges and all(row.window_id is None for row in audio_charges)

    sdk.luna_error = None
    assert _analyze_online(tmp_path, workspace) == 0  # 같은 작업공간 재개

    rows = _usage_rows(workspace)
    luna_rows = [row for row in rows if row.window_id is not None]
    assert len(luna_rows) == 1
    assert len(sdk.responses_calls) == 2, "죽은 호출과 살아난 호출 — 두 번 다 경계에 닿았다"
    # 두 시도의 전사 실측이 함께 남는다 — 재개가 지출을 리셋하지 않는다.
    assert len(rows) > len(audio_charges)
    snapshot_rows = _ledger_states(workspace)
    assert snapshot_rows.count("reconciled") == len(rows)


def test_ledger_write_failure_denies_admission_and_closes_the_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원장 내구 쓰기 실패는 허가가 아니다 — 예약 자리에서 닫힌다."""

    class _UnwritableLedger(request_accounting.BudgetLedger):
        def reserve(self, **kwargs: object) -> object:
            raise request_accounting.LedgerUnavailable("cannot write")

    monkeypatch.setattr(request_accounting, "BudgetLedger", _UnwritableLedger)
    workspace = _unit_workspace(tmp_path)
    context = _open_accounting(workspace)
    observer = context.observer()
    with pytest.raises(request_accounting.AccountingError) as caught:
        observer.authorize(_audio_meta("a1"))
    assert not request_accounting.is_budget_denial(caught.value), "쓰기 실패는 예산 거부가 아니다"
    with pytest.raises(request_accounting.AccountingError):
        observer.authorize(_audio_meta("a2"))


def test_projection_snapshots_under_the_projection_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """투영 재구축은 잠금 **안에서** 원장을 본다 — 낡은 스냅샷 덮어쓰기 경쟁을 잡는다.

    스냅샷이 잠금 밖에서 일어나면, 스냅샷을 뽑은 뒤 잠금을 기다리는 사이 다른
    조각이 확정·발행한 새 투영을 낡은 내용으로 되덮는다. 주 스레드가 투영 잠금을
    쥔 사이 재구축이 원장 스냅샷조차 시도하지 않는지를 잠금으로 증명한다.
    """
    import threading

    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace)
    observer = context.observer()
    meta = _audio_meta("a1", pricing.whisper_model, 60.0)
    observer.on_response(observer.authorize(meta), meta, {"duration": 60.0})

    order: list[str] = []
    snapshotted = threading.Event()
    real_snapshot = request_accounting.BudgetLedger.snapshot

    def noting_snapshot(self: object) -> object:
        order.append("snapshot")
        snapshotted.set()
        return real_snapshot(self)  # type: ignore[arg-type]

    monkeypatch.setattr(request_accounting.BudgetLedger, "snapshot", noting_snapshot)
    lock_path = workspace / (USAGE_NAME + ".lock")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with request_accounting._flocked(lock_path):
            future = pool.submit(context.rebuild_projection)
            assert not snapshotted.wait(timeout=0.5), (
                "투영 잠금 밖에서 원장 스냅샷을 뽑았다 — 낡은 스냅샷이 이기는 경쟁이다"
            )
        future.result(timeout=10)
    finally:
        pool.shutdown()
    assert order == ["snapshot"]
    assert [row.cost for row in _usage_rows(workspace)] == ["0.006"]


class _UsageDuration:
    """설치된 SDK의 ``UsageDuration`` 최소 모양 — type=duration, seconds."""

    type = "duration"

    def __init__(self, seconds: float) -> None:
        self.seconds = seconds


class _DurationOnlyResponse:
    """SDK 응답 객체 — 미터링이 usage(UsageDuration)로만 오는 모양."""

    def __init__(self, seconds: float) -> None:
        self.usage = _UsageDuration(seconds)


def test_usage_duration_metering_prices_at_the_audio_rate(tmp_path: Path) -> None:
    """``usage``의 UsageDuration(type=duration) 초도 분당가 근거로 확정된다.

    설치된 SDK는 길이를 `duration` 칸이나 `usage`의 duration형 둘 중 하나로
    실는다 — 어느 모양이든 같은 분당가로 확정하고, 토큰형(type=tokens)은 여전히
    가격이 없다(지어낸 환산 금지).
    """
    workspace = _unit_workspace(tmp_path)
    pricing = official_pricing_snapshot()
    context = _open_accounting(workspace)
    observer = context.observer()

    meta = _audio_meta("a1", pricing.gpt_transcribe_model, 90.0)
    observer.on_response(observer.authorize(meta), meta, _DurationOnlyResponse(90.0))
    assert [row.cost for row in _usage_rows(workspace)] == ["0.00675"]  # 90s × $0.0045/분

    meta = _audio_meta("a2", pricing.whisper_model, 30.0)
    observer.on_response(
        observer.authorize(meta), meta, {"usage": {"type": "duration", "seconds": 30}}
    )
    assert _usage_rows(workspace)[-1].cost == "0.003"  # 30s × $0.006/분

    meta = _audio_meta("a3", pricing.whisper_model, 12.0)
    observer.on_response(
        observer.authorize(meta), meta, {"usage": {"type": "tokens", "input_tokens": 5}}
    )
    assert len(_usage_rows(workspace)) == 2  # 토큰형은 미지수 — 실측이 아니다
