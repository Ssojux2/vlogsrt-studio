"""W-7 — 브라우저 흐름의 유료 시도마다 실측 사용량과 계산 비용을 영수증 행에 남긴다.

가짜 SDK는 **실제 호출 자리**(`LiveTranscriber`의 `_client`, `request_luna_vision`의
클라이언트)에 앉는다 — 관찰자는 그 안쪽 `RequestObserver` 이음매에서만 움직인다.
비용은 2026-09-21 가격 스냅샷으로 **계산한 값**이지 공급자의 청구서가 아니다. 유료 호출 0회.
"""

from __future__ import annotations

import sqlite3
import types
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.pricing import LUNA_MODEL, official_pricing_snapshot
from vlogsrt.project.raw import request_log, request_usage
from vlogsrt.project.raw.workspace import RawWorkspace
from vlogsrt.transcribe import (
    CAPTION_TIMING_KIND,
    AudioTranscriptionRequest,
    LiveTranscriber,
    ObserverFailure,
    request_luna_vision,
)

PRICING = official_pricing_snapshot()
WHISPER = Config().transcribe_model
GPT_TRANSCRIBE = Config().verify_model


class _FakeAudioClient:
    """`audio.transcriptions.create`가 시험이 정한 응답을 돌려준다. 네트워크 없음."""

    def __init__(self, reply: object = None, failure: Exception | None = None) -> None:
        self.reply = reply if reply is not None else {"segments": []}
        self.failure = failure
        self.calls = 0
        self.audio = self
        self.transcriptions = self

    def create(self, **kwargs: object) -> object:
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return self.reply


class _UsageObject:
    """SDK `UsageDuration` 객체의 최소 모습 — 사전이 아니라 속성으로 읽힌다."""

    type = "duration"
    seconds = 30.0


class _Timeout(Exception):
    """재시도 판정이 알아보지 못하는 시간 초과 — `provider_error`가 불확실로 읽는다."""


class _FakeResponsesClient:
    def __init__(self, reply: object) -> None:
        self.reply = reply
        self.responses = types.SimpleNamespace(create=lambda **payload: self.reply)


def _workspace(tmp_path: Path) -> RawWorkspace:
    return RawWorkspace.create(tmp_path / "ws", "사용량")


def _request(tmp_path: Path, model: str = WHISPER) -> AudioTranscriptionRequest:
    audio = tmp_path / "chunk.wav"
    audio.write_bytes(b"\x00")
    return AudioTranscriptionRequest(
        chunk=Chunk(audio, 0.0, 24.0, 0),
        model=model,
        response_format="verbose_json",
        timestamp_granularities=("segment",),
    )


def _send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: _FakeAudioClient, model: str = WHISPER
) -> dict[str, Any]:
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: client)
    live = LiveTranscriber(api_key="sk-test", observer=request_usage.observer())
    return live.send_request(
        _request(tmp_path, model), Config(), kind=CAPTION_TIMING_KIND, stem="chunk"
    )


def _rows(workspace: RawWorkspace) -> list[sqlite3.Row]:
    with workspace.connect() as db:
        return db.execute("SELECT * FROM analysis_requests ORDER BY rowid").fetchall()


def test_whisper_duration_usage_is_recorded_and_priced_per_minute(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    reply = {"segments": [], "usage": {"type": "duration", "seconds": 30}}
    with request_log.scope(workspace, "job-1", live=True):
        attempt = request_log.begin(
            "asr", "fp"
        )  # 이해 단계가 하는 그대로 — 이 행에 사용량이 붙는다
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply))
        request_log.finish(attempt, received=True)
    (row,) = _rows(workspace)
    assert (row["stage"], row["status"]) == ("asr", "response_received")
    assert row["model"] == WHISPER
    assert Decimal(row["billed_seconds"]) == Decimal(30)
    assert Decimal(row["cost_usd"]) == Decimal("0.003")  # 30초 × $0.006/분
    assert row["unpriced_reason"] is None
    assert row["pricing_revision"] == PRICING.snapshot_revision


def test_duration_as_sdk_object_is_read_the_same_way(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    reply = types.SimpleNamespace(usage=_UsageObject(), model_dump=lambda: {"segments": []})
    with request_log.scope(workspace, "job-1", live=True):
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply))
    (row,) = _rows(workspace)
    assert Decimal(row["billed_seconds"]) == Decimal(30)
    assert Decimal(row["cost_usd"]) == Decimal("0.003")


def test_caption_call_without_an_open_attempt_gets_its_own_receipt(monkeypatch, tmp_path):
    """최종 자막 경로는 `begin`을 안 부른다 — 관찰자가 호출 하나당 행 하나를 만든다."""
    workspace = _workspace(tmp_path)
    reply = {"segments": [], "usage": {"type": "duration", "seconds": 60}}
    with request_log.scope(workspace, "job-cap", live=True):
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply))
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply))
    rows = _rows(workspace)
    assert [(r["job_id"], r["stage"], r["status"]) for r in rows] == [
        ("job-cap", CAPTION_TIMING_KIND, "response_received")
    ] * 2
    assert all(Decimal(r["cost_usd"]) == Decimal("0.006") for r in rows)


def test_missing_usage_is_unpriced_never_zero(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    with request_log.scope(workspace, "job-1", live=True):
        _send(monkeypatch, tmp_path, _FakeAudioClient({"segments": []}))
    (row,) = _rows(workspace)
    assert row["status"] == "response_received"
    assert row["billed_seconds"] is None and row["cost_usd"] is None
    assert row["unpriced_reason"] == "missing_usage"
    assert request_usage.summarize(workspace).unpriced == 1


def test_token_only_audio_usage_keeps_tokens_but_no_seconds_or_cost(monkeypatch, tmp_path):
    """청 길이로 초를 지어내지 않는다 — 토큰만 온 전사는 가격 미정이다."""
    workspace = _workspace(tmp_path)
    reply = {"text": "안녕", "usage": {"type": "tokens", "input_tokens": 90, "output_tokens": 7}}
    with request_log.scope(workspace, "job-1", live=True):
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply), model=GPT_TRANSCRIBE)
    (row,) = _rows(workspace)
    assert row["billed_seconds"] is None and row["cost_usd"] is None
    assert (row["input_tokens"], row["output_tokens"]) == (90, 7)
    assert row["unpriced_reason"] == "token_only_audio_usage"


def test_unknown_audio_model_is_unpriced_but_seconds_are_kept(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    reply = {"segments": [], "usage": {"type": "duration", "seconds": 12}}
    with request_log.scope(workspace, "job-1", live=True):
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply), model="mystery-stt")
    (row,) = _rows(workspace)
    assert row["model"] == "mystery-stt"
    assert Decimal(row["billed_seconds"]) == Decimal(12)
    assert row["cost_usd"] is None and row["unpriced_reason"] == "unknown_model"


def test_luna_tokens_are_recorded_and_priced_for_the_snapshot_model(tmp_path):
    workspace = _workspace(tmp_path)
    reply = {"output_text": "{}", "usage": {"input_tokens": 1000, "output_tokens": 500}}
    with request_log.scope(workspace, "job-1", live=True):
        attempt = request_log.begin("luna", "sha")
        request_luna_vision(
            _FakeResponsesClient(reply),
            model=LUNA_MODEL,
            payload={"input": []},
            online=True,
            observer=request_usage.observer(),
        )
        request_log.finish(attempt, received=True)
    (row,) = _rows(workspace)
    assert (row["stage"], row["model"]) == ("luna", LUNA_MODEL)
    assert (row["input_tokens"], row["output_tokens"]) == (1000, 500)
    assert Decimal(row["cost_usd"]) == Decimal("0.0008")  # (1000×0.20 + 500×1.20) / 1,000,000
    assert row["billed_seconds"] is None


def test_luna_model_outside_the_snapshot_is_unpriced_with_tokens_kept(tmp_path):
    """앱 기본 Luna 모델은 가격 스냅샷에 없을 수 있다 — 0원이 아니라 가격 미정이다."""
    workspace = _workspace(tmp_path)
    reply = {"output_text": "{}", "usage": {"input_tokens": 40, "output_tokens": 2}}
    with request_log.scope(workspace, "job-1", live=True):
        request_luna_vision(
            _FakeResponsesClient(reply),
            model="not-in-the-snapshot",
            payload={"input": []},
            online=True,
            observer=request_usage.observer(),
        )
    (row,) = _rows(workspace)
    assert (row["input_tokens"], row["output_tokens"]) == (40, 2)
    assert row["cost_usd"] is None and row["unpriced_reason"] == "unknown_model"


def test_luna_response_without_usage_is_unpriced(tmp_path):
    workspace = _workspace(tmp_path)
    with request_log.scope(workspace, "job-1", live=True):
        request_luna_vision(
            _FakeResponsesClient({"output_text": "{}"}),
            model=LUNA_MODEL,
            payload={"input": []},
            online=True,
            observer=request_usage.observer(),
        )
    (row,) = _rows(workspace)
    assert row["input_tokens"] is None and row["cost_usd"] is None
    assert row["unpriced_reason"] == "missing_or_invalid_usage"


def test_uncertain_attempts_are_counted_and_never_priced(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    with request_log.scope(workspace, "job-1", live=True):
        attempt = request_log.begin("asr", "lost")  # 보냈는데 응답을 못 받았다
        request_log.finish(attempt, received=False, note="timeout")
        with pytest.raises(_Timeout):  # 최종 자막 경로의 시간 초과도 같은 불확실이다
            _send(monkeypatch, tmp_path, _FakeAudioClient(failure=_Timeout("t")))
    rows = _rows(workspace)
    assert [r["status"] for r in rows] == ["response_uncertain", "response_uncertain"]
    assert all(r["cost_usd"] is None and r["unpriced_reason"] is None for r in rows)
    summary = request_usage.summarize(workspace)
    assert (summary.attempts, summary.received, summary.uncertain) == (2, 0, 2)
    assert summary.unpriced == 0  # 불확실은 가격 미정과 따로 센다
    assert summary.cost_usd == Decimal(0)


def test_rejected_request_is_a_received_failure_not_uncertain(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)

    class _Rejected(Exception):
        status_code = 400

    with request_log.scope(workspace, "job-1", live=True), pytest.raises(_Rejected):
        _send(monkeypatch, tmp_path, _FakeAudioClient(failure=_Rejected("bad")))
    (row,) = _rows(workspace)
    assert row["status"] == "response_received" and row["note"] == "request_format"
    assert row["cost_usd"] is None


def test_old_schema_table_gains_columns_once_and_old_rows_stay_readable(tmp_path):
    workspace = _workspace(tmp_path)
    with workspace.connect() as db:
        db.execute(
            "CREATE TABLE analysis_requests (id TEXT PRIMARY KEY, job_id TEXT NOT NULL, "
            "stage TEXT NOT NULL, fingerprint TEXT NOT NULL, status TEXT NOT NULL, note TEXT, "
            "created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"
        )
        db.execute(
            "INSERT INTO analysis_requests(id,job_id,stage,fingerprint,status) "
            "VALUES('old','job-old','asr','fp','response_received')"
        )
        db.commit()
    for _ in range(2):  # 두 번 열어도 같다 — 멱등이다
        with request_log.scope(workspace, "job-new", live=True):
            pass
    with workspace.connect() as db:
        columns = {r["name"] for r in db.execute("PRAGMA table_info(analysis_requests)")}
    assert {
        "model",
        "billed_seconds",
        "input_tokens",
        "output_tokens",
        "cost_usd",
        "unpriced_reason",
        "pricing_revision",
    } <= columns
    summary = request_usage.summarize(workspace, ("job-old",))
    assert (summary.attempts, summary.received, summary.unpriced) == (1, 1, 1)
    assert summary.billed_seconds == Decimal(0) and summary.cost_usd == Decimal(0)


class _StaleColumns:
    """첫 칸 목록만 옛것을 돌려주는 연결 — 그것을 읽은 사이 다른 분석이 칸을 더했다."""

    def __init__(self, db: sqlite3.Connection, names: tuple[str, ...]) -> None:
        self._db, self._names, self.stale_reads = db, names, 1

    def execute(self, sql: str, *args: Any) -> Any:
        if sql.startswith("PRAGMA table_info") and self.stale_reads:
            self.stale_reads -= 1
            return [(index, name) for index, name in enumerate(self._names)]
        return self._db.execute(sql, *args)

    def commit(self) -> None:
        self._db.commit()


def test_two_analyses_adding_the_same_column_at_once_both_open(tmp_path):
    """W-24 세션 E에서 드러난 경합 — 둘이 같은 옛 칸 목록을 읽으면 늦은 쪽의 ADD COLUMN이
    「duplicate column name」으로 그 영상의 분석을 실패시켰다(유료 호출 전에)."""
    workspace = _workspace(tmp_path)
    base = ("id", "job_id", "stage", "fingerprint", "status", "note", "created_at")
    with workspace.connect() as db:
        request_log.ensure_schema(db)  # 먼저 연 쪽이 칸을 다 더했다
        request_log.ensure_schema(_StaleColumns(db, base))  # type: ignore[arg-type]
        columns = {r["name"] for r in db.execute("PRAGMA table_info(analysis_requests)")}
    assert {"unpriced_reason", "provider_request_id"} <= columns


def test_summary_totals_per_job_and_overall(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    asr = {"segments": [], "usage": {"type": "duration", "seconds": 60}}
    luna = {"output_text": "{}", "usage": {"input_tokens": 1000, "output_tokens": 500}}
    with request_log.scope(workspace, "job-a", live=True):
        _send(monkeypatch, tmp_path, _FakeAudioClient(asr))
        _send(monkeypatch, tmp_path, _FakeAudioClient({"segments": []}))  # 사용량 없음
    with request_log.scope(workspace, "job-b", live=True):
        request_luna_vision(
            _FakeResponsesClient(luna),
            model=LUNA_MODEL,
            payload={"input": []},
            online=True,
            observer=request_usage.observer(),
        )
        request_log.finish(request_log.begin("asr", "x"), received=False, note="timeout")
    first = request_usage.summarize(workspace, ("job-a",))
    assert (first.attempts, first.received, first.uncertain, first.unpriced) == (2, 2, 0, 1)
    assert first.billed_seconds == Decimal(60) and first.cost_usd == Decimal("0.006")
    both = request_usage.summarize(workspace)
    assert (both.attempts, both.received, both.uncertain, both.unpriced) == (4, 3, 1, 1)
    assert both.cost_usd == Decimal("0.0068")
    assert (both.luna_input_tokens, both.luna_output_tokens) == (1000, 500)
    assert request_usage.summarize(workspace, ()).attempts == 0  # 빈 집합은 빈 집계다


def test_summary_of_a_workspace_that_never_ran_live_is_empty(tmp_path):
    workspace = _workspace(tmp_path)
    summary = request_usage.summarize(workspace)
    assert summary.attempts == 0
    assert request_usage.format_usage_line(summary) == ""


def test_usage_line_is_compact_and_flags_uncertain_and_unpriced(tmp_path):
    summary = request_usage.UsageSummary(
        attempts=5,
        received=4,
        uncertain=1,
        billed_seconds=Decimal("63.6"),
        input_tokens=12345,
        output_tokens=678,
        luna_input_tokens=12345,
        luna_output_tokens=678,
        cost_usd=Decimal("0.012345"),
        unpriced=2,
    )
    assert request_usage.format_usage_line(summary) == (
        "실사용 $0.0123 · 전사 64초 · Luna 입력 12,345/출력 678 토큰 (불확실 1건·가격 미정 2건)"
    )
    capped = request_usage.UsageSummary(
        attempts=1, received=1, cost_usd=Decimal("0.003"), ceiling_usd=Decimal("0.05")
    )
    assert request_usage.format_usage_line(capped) == (
        "실사용 $0.0030 · 승인 상한 $0.05 (불확실 0건·가격 미정 0건)"
    )


def test_offline_or_unscoped_runs_write_nothing(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    with request_log.scope(workspace, "job-off", live=False):  # 오프라인·픽스처 실행의 문
        assert request_log.begin("asr", "fp") is None
    reply = {"segments": [], "usage": {"type": "duration", "seconds": 30}}
    _send(monkeypatch, tmp_path, _FakeAudioClient(reply))  # 범위 밖 호출 — 기록할 자리가 없다
    with workspace.connect() as db:
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "analysis_requests" not in tables


def test_a_broken_recorder_surfaces_as_observer_failure(monkeypatch, tmp_path):
    workspace = _workspace(tmp_path)
    reply = {"segments": [], "usage": {"type": "duration", "seconds": 30}}

    def broken(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(request_log, "record_usage", broken)
    with request_log.scope(workspace, "job-1", live=True), pytest.raises(ObserverFailure):
        _send(monkeypatch, tmp_path, _FakeAudioClient(reply))


def test_live_browser_senders_carry_the_usage_observer():
    from vlogsrt.project.raw import browser_runtime

    credentials = browser_runtime.EnvironmentCredentials(api_key="sk-test-only", base_url=None)
    transport = browser_runtime.live_transport_for_credentials(
        credentials, asr_approvals=lambda *a: {}, luna_approvals=lambda *a: {}
    )
    assert isinstance(transport.sender.observer, request_usage.UsageObserver)  # type: ignore[attr-defined]
    timing, text = browser_runtime.caption_transport_providers(credentials)
    assert isinstance(timing.sender.observer, request_usage.UsageObserver)
    assert isinstance(text.sender.observer, request_usage.UsageObserver)


def test_live_selection_records_luna_usage_and_the_status_line_shows_it(tmp_path, monkeypatch):
    """승인된 선택 분석 — 전사·Luna 영수증이 리비전 단위로 합쳐지고 화면에 한 줄이 뜬다."""
    from tests.raw.test_browser_runtime import _make_media
    from tests.raw.test_selection_analysis import SuitabilityClient, make_server, wait_until

    class UsageClient(SuitabilityClient):
        def _create(self, **payload: Any) -> dict[str, Any]:
            reply = super()._create(**payload)
            return {**reply, "usage": {"input_tokens": 1000, "output_tokens": 500}}

    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    server, sender, client = make_server(tmp_path, monkeypatch, source, UsageClient())
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    root = server.workspace_root()
    summary = server.raw.analysis_usage_summary(root, server._quick["revision"])
    assert summary.attempts == len(sender.calls) + len(client.calls) == 4
    assert (summary.received, summary.uncertain) == (4, 0)
    assert (summary.luna_input_tokens, summary.luna_output_tokens) == (3000, 1500)
    assert summary.unpriced == 4  # 앱 기본 Luna 모델은 스냅샷 밖이고 가짜 전송은 사용량이 없다
    assert server.raw.analysis_usage_summary(root, "no-such-revision").attempts == 0
    line = request_usage.format_usage_line(summary)
    assert "Luna 입력 3,000/출력 1,500 토큰" in line
    assert line in server._quick_status()


def test_final_caption_run_gets_receipts_only_when_its_providers_are_live(monkeypatch, tmp_path):
    from vlogsrt.project.raw.caption_provider import TransportTimingProvider
    from vlogsrt.project.raw.service import RawWorkspaceService

    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "자막 영수증")
    reply = {"segments": [], "usage": {"type": "duration", "seconds": 60}}
    monkeypatch.setattr(LiveTranscriber, "_client", lambda self: _FakeAudioClient(reply))
    live = LiveTranscriber(api_key="sk-test", observer=request_usage.observer())
    live_timing = TransportTimingProvider(sender=live, cfg=Config())

    def body(context: Any) -> None:
        live.send_request(_request(tmp_path), Config(), kind=CAPTION_TIMING_KIND)

    context = types.SimpleNamespace(job_id="job_caption")
    service.with_request_receipts(root, body, live_timing, object())(context)
    summary = service.job_usage_summary(root, "job_caption")
    assert (summary.attempts, summary.received, summary.cost_usd) == (1, 1, Decimal("0.006"))
    service.with_request_receipts(root, body, object(), object())(
        types.SimpleNamespace(job_id="job_fixture")
    )
    assert service.job_usage_summary(root, "job_fixture").attempts == 0


# ─────────────────────────────────────────────────────────────
#  W-14 / D54 — Claude 영수증: 공식 단가로 계산하고, 못 매기면 가격 미정이다
# ─────────────────────────────────────────────────────────────


CLAUDE = "claude-opus-5-5"


class _ClaudeReply:
    def __init__(self, body: dict[str, Any], request_id: str) -> None:
        self.status = 200
        self.headers = {"request-id": request_id}
        self._body = body

    def read(self) -> bytes:
        import json

        return json.dumps(self._body).encode()

    def __enter__(self) -> _ClaudeReply:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _claude_send(body: dict[str, Any], request_id: str = "req_synthetic_usage") -> None:
    from vlogsrt.anthropic_messages import ClaudeMessagesClient
    from vlogsrt.transcribe import request_claude_messages

    client = ClaudeMessagesClient(
        api_key="sk-ant-synthetic",
        base_url="https://api.anthropic.com",
        api_version="2023-06-01",
        timeout_seconds=30.0,
        opener=lambda request, timeout: _ClaudeReply(body, request_id),
    )
    request_claude_messages(
        client,
        model=CLAUDE,
        payload={"max_tokens": 16, "messages": []},
        online=True,
        observer=request_usage.observer(),
    )


def _claude_body(usage: dict[str, Any] | None, model: str = CLAUDE) -> dict[str, Any]:
    body: dict[str, Any] = {"type": "message", "model": model, "stop_reason": "end_turn"}
    if usage is not None:
        body["usage"] = usage
    return body


def test_claude_tokens_are_priced_at_the_official_snapshot(tmp_path):
    workspace = _workspace(tmp_path)
    with request_log.scope(workspace, "job-1", live=True):
        _claude_send(_claude_body({"input_tokens": 5000, "output_tokens": 1200}))
    (row,) = _rows(workspace)
    assert (row["stage"], row["status"], row["model"]) == ("luna", "response_received", CLAUDE)
    assert (row["input_tokens"], row["output_tokens"]) == (5000, 1200)
    assert Decimal(row["cost_usd"]) == Decimal("0.044")  # (5000×$4 + 1200×$20) / 1,000,000
    assert row["unpriced_reason"] is None
    assert row["pricing_revision"] == "anthropic-claude-opus-5-5-2026-10-09"
    assert row["provider_request_id"] == "req_synthetic_usage"


def test_claude_cache_tokens_are_priced_at_their_own_rates():
    from vlogsrt.project.raw.claude_cost import measure_claude_usage

    meta = types.SimpleNamespace(model=CLAUDE, operation="messages.create")
    usage = {
        "input_tokens": 1000,
        "output_tokens": 100,
        "cache_creation_input_tokens": 3000,
        "cache_creation": {"ephemeral_5m_input_tokens": 1000, "ephemeral_1h_input_tokens": 2000},
        "cache_read_input_tokens": 4000,
    }
    measured = measure_claude_usage(meta, _claude_body(usage))
    # 1000×$4 + 1000×$5 + 2000×$8 + 4000×$0.20 + 100×$20 = $0.0278
    assert Decimal(measured.cost_usd or "nan") == Decimal("0.0278")
    assert measured.input_tokens == 8000  # 모든 입력 범주의 합 — 캐시도 입력이다
    assert measured.output_tokens == 100

    plain = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1000}
    conservative = measure_claude_usage(meta, _claude_body(plain))
    assert Decimal(conservative.cost_usd or "nan") == Decimal("0.008")  # 분해가 없으면 1시간 값


@pytest.mark.parametrize(
    ("body", "model", "reason"),
    [
        ({"type": "message", "model": CLAUDE}, CLAUDE, "missing_or_invalid_usage"),
        (
            _claude_body({"input_tokens": -1, "output_tokens": 2}),
            CLAUDE,
            "missing_or_invalid_usage",
        ),
        (
            _claude_body({"input_tokens": "5", "output_tokens": 2}),
            CLAUDE,
            "missing_or_invalid_usage",
        ),
        (
            _claude_body({"input_tokens": 5, "output_tokens": 2}, model="claude-opus-5"),
            CLAUDE,
            "model_mismatch",
        ),
        (
            _claude_body({"input_tokens": 5, "output_tokens": 2}),
            "claude-unknown-9",
            "unknown_model",
        ),
        (  # bool은 int의 하위형이지만 토큰 수가 아니다
            _claude_body({"input_tokens": True, "output_tokens": 2}),
            CLAUDE,
            "missing_or_invalid_usage",
        ),
        (
            _claude_body({"input_tokens": 5, "output_tokens": 2, "cache_read_input_tokens": False}),
            CLAUDE,
            "missing_or_invalid_usage",
        ),
        (  # TTL 분해가 생성 합보다 크다 — 어느 쪽을 믿을지 모른다
            _claude_body(
                {
                    "input_tokens": 1,
                    "output_tokens": 1,
                    "cache_creation_input_tokens": 5,
                    "cache_creation": {"ephemeral_5m_input_tokens": 9},
                }
            ),
            CLAUDE,
            "missing_or_invalid_usage",
        ),
        (
            _claude_body({"input_tokens": 1, "output_tokens": 1, "cache_creation": 3}),
            CLAUDE,
            "missing_or_invalid_usage",
        ),
    ],
)
def test_claude_usage_that_cannot_be_priced_is_unpriced_never_zero(body, model, reason):
    from vlogsrt.project.raw.claude_cost import measure_claude_usage

    meta = types.SimpleNamespace(model=model, operation="messages.create")
    measured = measure_claude_usage(meta, body)
    assert measured.cost_usd is None
    assert measured.unpriced_reason == reason


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        ({"inference_geo": "us"}, "nonstandard_inference_geo"),  # US 전용은 1.1배 단가다
        ({"inference_geo": "eu"}, "nonstandard_inference_geo"),
        ({"service_tier": "priority"}, "nonstandard_service_tier"),
        ({"service_tier": "batch"}, "nonstandard_service_tier"),
        ({"inference_geo": "global", "service_tier": "priority"}, "nonstandard_service_tier"),
    ],
)
def test_claude_usage_outside_the_standard_rate_is_unpriced_not_underpriced(extra, reason):
    """1M 입력을 US 전용으로 받으면 실제는 $4.40이다 — 표준 $4로 적지 않고 가격 미정으로 둔다."""
    from vlogsrt.project.raw.claude_cost import measure_claude_usage

    meta = types.SimpleNamespace(model=CLAUDE, operation="messages.create")
    usage = {"input_tokens": 1_000_000, "output_tokens": 0, **extra}
    measured = measure_claude_usage(meta, _claude_body(usage))
    assert measured.cost_usd is None
    assert measured.unpriced_reason == reason
    assert measured.input_tokens == 1_000_000  # 토큰은 남긴다


@pytest.mark.parametrize(
    "extra",
    [{}, {"inference_geo": "global"}, {"service_tier": "standard"}, {"inference_geo": None}],
)
def test_claude_usage_at_the_standard_rate_is_priced(extra):
    from vlogsrt.project.raw.claude_cost import measure_claude_usage

    meta = types.SimpleNamespace(model=CLAUDE, operation="messages.create")
    usage = {"input_tokens": 5000, "output_tokens": 1200, **extra}
    measured = measure_claude_usage(meta, _claude_body(usage))
    assert Decimal(measured.cost_usd or "nan") == Decimal("0.044")
    assert measured.unpriced_reason is None


def test_a_claude_model_mismatch_keeps_the_tokens(tmp_path):
    workspace = _workspace(tmp_path)
    with request_log.scope(workspace, "job-1", live=True):
        _claude_send(_claude_body({"input_tokens": 40, "output_tokens": 2}, model="claude-opus-5"))
    (row,) = _rows(workspace)
    assert (row["input_tokens"], row["output_tokens"]) == (40, 2)
    assert row["cost_usd"] is None and row["unpriced_reason"] == "model_mismatch"


def test_the_request_id_column_is_added_once_and_luna_rows_leave_it_empty(tmp_path):
    workspace = _workspace(tmp_path)
    reply = {"output_text": "{}", "usage": {"input_tokens": 1000, "output_tokens": 500}}
    for _ in range(2):  # 두 번 열어도 같다 — 멱등이다
        with request_log.scope(workspace, "job-1", live=True):
            pass
    with request_log.scope(workspace, "job-1", live=True):
        request_luna_vision(
            _FakeResponsesClient(reply),
            model=LUNA_MODEL,
            payload={"input": []},
            online=True,
            observer=request_usage.observer(),
        )
    with workspace.connect() as db:
        columns = [r["name"] for r in db.execute("PRAGMA table_info(analysis_requests)")]
    assert columns.count("provider_request_id") == 1
    (row,) = _rows(workspace)
    assert row["provider_request_id"] is None
    assert Decimal(row["cost_usd"]) == Decimal("0.0008")  # Luna 계산은 그대로다


def test_a_second_claude_response_on_one_attempt_adds_up(tmp_path):
    workspace = _workspace(tmp_path)
    usage = {"input_tokens": 5000, "output_tokens": 1200}
    with request_log.scope(workspace, "job-1", live=True):
        attempt = request_log.begin("luna", "sha")
        _claude_send(_claude_body(usage), "req_first")
        _claude_send(_claude_body(usage), "req_second")
        request_log.finish(attempt, received=True)
    (row,) = _rows(workspace)
    assert Decimal(row["cost_usd"]) == Decimal("0.088")
    assert row["provider_request_id"] == "req_second"
