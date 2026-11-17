"""D59 19f — 말소리 빈칸 재전사: 넣기 · 범위와 승인 · 규칙 1·6 탐침 · 다음 1단계 심기.

유료 호출은 없다 — 시간 엔진(Gemini 3.5 Transcribe)은 요청을 세는 가짜 opener(`GeminiEcho`)가
받고, 환경 문은 시험 안에서만 `vlogsrt.env.is_offline`을 가짜로 연다(다른 흐름 시험과 같은 선).
재료는 톤 덩어리 원본 하나를 Gemini 응답 재생으로 분석해 원본 자막을 심되 **덩어리 둘을 일부러
빼서** 자막 없는 말소리를 만든다. 자막 시각 확인은 가짜 에너지 VAD로 잰다.

whisper·gpt-transcribe는 은퇴했다(D163) — 빈칸 재전사는 Gemini 시간 엔진 원본만 묻고, 다른 귀(gpt
글자)는 없다. whisper로 들은 원본은 0송신으로 거절한다.

시험이 못 박는 것:

- 기존 줄은 바이트까지 그대로, 겹치는 줄은 넣지 않고 WARN·기록, 리비전이 다르면 거절한다.
- 넣은 자막은 고친 자막(`edited=True`)이 되어 **다음 1단계 심기가 지우지 않는다**.
- 넣은 시각 = 엔진 단어 시각 + 그 조각의 실제 표본 오프셋(+ 확인과 같은 원점) — 정확
  유리수(규칙 1).
- 시간 엔진 요청에는 힌트가 없다(규칙 6) — 용어집을 저장해도 요청·범위에 실리지 않는다.
- 범위 digest는 구간·자막 리비전에 따라 바뀌고, 낡은 승인은 0송신으로 닫힌다.
"""

from __future__ import annotations

import base64
import hashlib
import json
import shutil
import time
import types
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests._sync_fakes import bursts, energy_factory, make_media, numpy_or_fake
from tests.raw.gemini_echo import GeminiEcho, interaction_body
from vlogsrt import log
from vlogsrt.config import Config
from vlogsrt.project.raw import (
    asr_epoch,
    glossary,
    source_transcript,
    speech_refill,
    sync_report,
    timing_engine,
)
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport
from vlogsrt.project.raw.config import (
    GEMINI_TRANSCRIBE_PROFILE,
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINE_WHISPER,
)
from vlogsrt.project.raw.models import RawWorkspaceError

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
REPLAY_ANALYSIS_MODEL = "gpt-6-luna"  # 재생 의미 판정 픽스처의 응답 모양(test_sync_report와 같다)
DROPPED = (2, 9)  # 원본 자막에서 일부러 뺀 덩어리 — 5.9–8.0초 · 25.0–27.2초
RATE = 16000
SEGMENT = (0.5, 1.25)  # 조각 안 엔진 단어 시각 — 표본으로 정확히 떨어진다(8000·20000)
ENGINE_TEXT = "빈칸 말"


class RefillSender(GeminiEcho):
    """Gemini 시간 엔진의 가짜 opener — 요청 본문을 모으고 정해진 세그먼트를 단어로 돌려준다.

    세그먼트 하나의 낱말은 모두 그 세그먼트의 시각을 가진다(`GeminiEcho`) — 유료 호출은 없다."""

    def __init__(self, segments: list[dict[str, Any]] | None = None) -> None:
        default = [{"start": SEGMENT[0], "end": SEGMENT[1], "text": ENGINE_TEXT}]
        super().__init__(segments or default)


def sent_audio(body: dict[str, Any]) -> bytes:
    """보낸 요청 본문의 인라인 오디오(조각 WAV 바이트 그대로)."""
    (part,) = body["input"]
    assert part["type"] == "audio"
    return base64.b64decode(part["data"])


def sent_pieces(sender: GeminiEcho, root: Path, asset_id: str) -> list[tuple[int, int]]:
    """보낸 조각(표본 범위) — 인라인 오디오의 해시를 잘라 둔 조각의 WAV 해시와 맞춘다.

    조각은 함께 보낸다(P-5, D159) — 보낸 차례가 아니라 보낸 조각을 본다."""
    folder = root / "cache" / speech_refill.REFILL_DIRNAME / asset_id
    slices = json.loads((folder / "slices.json").read_text(encoding="utf-8"))
    by_sha: dict[str, tuple[int, int]] = {}
    for key, sha in slices.items():
        _pcm, start, end = str(key).split(":")
        by_sha[str(sha)] = (int(start), int(end))
    return sorted(by_sha[hashlib.sha256(sent_audio(body)).hexdigest()] for body in sender.calls)


def wait(service: raw.RawWorkspaceService, root: Path, job_id: str) -> Any:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    raise AssertionError(f"작업이 끝나지 않았다: {job_id}")


def wait_sync(service: raw.RawWorkspaceService, root: Path) -> None:
    latest = service.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
    assert latest is not None
    assert wait(service, root, latest.job_id).status == "succeeded"


def transport(sender: GeminiEcho) -> LiveAnalysisTransport:
    """가짜 전송 — 시간 엔진은 `timing_client`(Gemini 가짜)로만 간다."""
    return LiveAnalysisTransport(
        sender=sender,
        cfg=Config(),
        client=None,
        asr_approvals=lambda *_: {},
        luna_approvals=lambda *_: {},
        timing_client=sender.client(),
    )


def context() -> Any:
    return types.SimpleNamespace(job_id="refill-test", check_cancel=lambda: None)


def pin_gemini(service: raw.RawWorkspaceService, root: Path, asset_id: str) -> None:
    """손으로 심은 원본 자막을 Gemini 시간 엔진 원본으로 고정한다(없으면 whisper로 읽힌다)."""
    workspace = service._workspace(root)
    epoch = asr_epoch.state(workspace, asset_id).epoch
    timing_engine.pin(workspace, asset_id, epoch, TIMING_ENGINE_GEMINI)
    assert timing_engine.resolve(workspace, asset_id) == TIMING_ENGINE_GEMINI


def _replay_fixtures(root: Path, sha: str, *, dropped: tuple[int, ...]) -> None:
    """재생 픽스처 — 원본 자막은 Gemini 응답 선언(청크 0)에서, 의미 판정은 Luna 모양에서 온다."""
    replay = root / "analysis" / "replay"
    replay.mkdir(parents=True, exist_ok=True)
    spoken = [
        {"start": str(start), "end": str(end), "text": f"말 {index}"}
        for index, (start, end) in enumerate(bursts())
        if index not in dropped
    ]
    words = {"sources": {sha: {"0": interaction_body(spoken)}}}
    (replay / "word-text-fixtures.json").write_text(
        json.dumps(words, ensure_ascii=False), encoding="utf-8"
    )
    luna = {f"window_{i:06d}": _luna(f"window_{i:06d}") for i in range(12)}
    (replay / "luna-fixtures.json").write_text(json.dumps({"windows": luna}), "utf-8")


def _luna(window_id: str) -> dict[str, Any]:
    return {
        "window_id": window_id,
        "speech_summary": "말한다",
        "visual_summary": "회색 화면",
        "scene_tags": [],
        "evidence_ids": [window_id],
        "suggested_boundary_ids": [],
        "editing_notes": [],
        "needs_review": False,
        "uncertainties": [],
    }


def gapped_workspace(
    tmp_path: Path, audio_delay: str = "0"
) -> tuple[raw.RawWorkspaceService, Path, str]:
    """톤 원본 하나를 Gemini 재생으로 분석(덩어리 둘을 뺀 자막)하고 자막 시각 확인까지 끝낸 곳.

    `audio_delay`는 오디오 원점이다 — 0이 아니면 공통 시계가 PCM 시계보다 그만큼 늦다."""
    card = tmp_path / "card"
    make_media(card / "tone.mov", audio_delay=audio_delay)
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "빈칸")
    workflow = service.settings(root)
    models = replace(workflow.models, luna_model=REPLAY_ANALYSIS_MODEL)
    service.update_settings(root, replace(workflow, models=models))
    proposal = service.scan_sources(root, card)
    confirmation = service.confirm_ingest(root, proposal)
    copied = wait(service, root, service.start_ingest_copy(root, confirmation).job_id)
    assert copied.status == "succeeded"
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    _replay_fixtures(root, asset.content_sha256 or "", dropped=DROPPED)
    assert wait(service, root, service.start_analysis(root).job_id).status == "succeeded"
    wait_sync(service, root)
    workspace = service._workspace(root)
    assert timing_engine.resolve(workspace, asset.asset_id) == TIMING_ENGINE_GEMINI
    return service, root, asset.asset_id


@pytest.fixture
def gapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    return gapped_workspace(tmp_path)


def _online(monkeypatch: pytest.MonkeyPatch) -> None:
    """환경 문을 시험 안에서만 연다 — 전송은 가짜다(유료 호출 없음)."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)


def _quote(service: raw.RawWorkspaceService, root: Path, asset_id: str) -> Any:
    quote = service.refill_scope(root, asset_id)
    assert isinstance(quote, speech_refill.RefillScope), quote
    return quote


# ── 넣기 — 기존 줄 바이트 그대로 · 겹침은 WARN·기록 · 리비전 대조 ─────────────────────


def _row(row_id: str, start: str, end: str, text: str = "새 줄") -> dict[str, Any]:
    return {
        "utterance_id": row_id,
        "text": text,
        "start": source_transcript.encoded(Fraction(start)),
        "end": source_transcript.encoded(Fraction(end)),
        source_transcript.REFILL_FIELD: "r",
    }


def _seeded(tmp_path: Path) -> tuple[Any, str, dict[str, Any]]:
    service = raw.RawWorkspaceService()
    root = tmp_path / "plain"
    service.create_workspace(root, "넣기")
    workspace = service._workspace(root)
    rows = [_row("a", "1", "2", "첫째"), _row("b", "5", "6", "둘째")]
    for row in rows:
        del row[source_transcript.REFILL_FIELD]
    document = source_transcript.seed(workspace, "asset_x", rows, Fraction(10), "sha")
    return workspace, "asset_x", document


def test_insert_keeps_existing_rows_byte_identical_and_marks_the_transcript_edited(
    tmp_path: Path,
) -> None:
    workspace, asset_id, before = _seeded(tmp_path)
    old_bytes = [json.dumps(row, ensure_ascii=False) for row in before["utterances"]]
    outcome = source_transcript.insert_segments(
        workspace, asset_id, before["revision"], [_row("n2", "7", "8"), _row("n1", "3", "4")]
    )
    after = source_transcript.load(workspace, asset_id)
    assert after is not None and outcome.document == after
    assert outcome.inserted == ("n1", "n2") and not outcome.overlapped
    assert [row["utterance_id"] for row in after["utterances"]] == ["a", "n1", "b", "n2"]
    kept = [
        json.dumps(row, ensure_ascii=False)
        for row in after["utterances"]
        if row["utterance_id"] in "ab"
    ]
    assert kept == old_bytes  # 기존 줄은 바이트까지 그대로
    assert after["edited"] is True and after["sequence"] == before["sequence"] + 1
    assert after["revision"] != before["revision"]
    assert after["dirty_ranges"] == [
        {"start": {"num": 3, "den": 1}, "end": {"num": 4, "den": 1}},
        {"start": {"num": 7, "den": 1}, "end": {"num": 8, "den": 1}},
    ]


def test_overlapping_and_outside_rows_are_recorded_never_dropped_silently(tmp_path: Path) -> None:
    workspace, asset_id, before = _seeded(tmp_path)
    rows = [
        _row("touch", "2", "3"),  # 맞닿기만 한다 — 넣는다
        _row("over", "5.5", "7", "겹친 말"),  # 둘째 줄과 겹친다
        _row("twin", "2.5", "2.8", "새 줄끼리 겹침"),  # 먼저 받은 새 줄과 겹친다
        _row("late", "9.5", "11", "범위 밖"),  # 원본 길이(10초) 밖
    ]
    outcome = source_transcript.insert_segments(workspace, asset_id, before["revision"], rows)
    assert outcome.inserted == ("touch",)
    assert [(item["utterance_id"], item["with"]) for item in outcome.overlapped] == [
        ("twin", "touch"),
        ("over", "b"),
    ]
    assert outcome.overlapped[1]["text"] == "겹친 말"  # 글자째 남는다(규칙 9)
    assert [item["utterance_id"] for item in outcome.out_of_range] == ["late"]


def test_insert_refuses_a_stale_revision_and_nothing_changes(tmp_path: Path) -> None:
    workspace, asset_id, before = _seeded(tmp_path)
    with pytest.raises(RawWorkspaceError, match="원본 자막이 바뀌었습니다"):
        source_transcript.insert_segments(
            workspace, asset_id, "old-revision", [_row("n", "3", "4")]
        )
    assert source_transcript.load(workspace, asset_id) == before


def test_nothing_inserted_means_no_new_revision(tmp_path: Path) -> None:
    workspace, asset_id, before = _seeded(tmp_path)
    outcome = source_transcript.insert_segments(
        workspace, asset_id, before["revision"], [_row("over", "1.5", "1.8")]
    )
    assert outcome.inserted == () and len(outcome.overlapped) == 1
    assert source_transcript.load(workspace, asset_id) == before  # 고친 자막도 되지 않았다


def test_the_next_stage1_seed_keeps_the_inserted_rows(tmp_path: Path) -> None:
    """다음 1단계 심기가 삽입 세그먼트를 지우지 않는다 — `edited=True`라 심기가 덮지 않는다."""
    workspace, asset_id, before = _seeded(tmp_path)
    source_transcript.insert_segments(
        workspace, asset_id, before["revision"], [_row("n1", "3", "4")]
    )
    filled = source_transcript.load(workspace, asset_id)
    fresh = [_row("w1", "1", "2", "다시 들은 첫째")]
    del fresh[0][source_transcript.REFILL_FIELD]
    reseeded = source_transcript.seed(workspace, asset_id, fresh, Fraction(10), "sha")
    assert reseeded == filled and any(row["utterance_id"] == "n1" for row in reseeded["utterances"])
    assert source_transcript.human_edited(reseeded)


# ── 범위 — 구간 · digest(견적·금액 문구는 없다, D110) ─────────────────────────────


def test_the_scope_asks_exactly_the_dropped_bursts_with_the_timing_engine_alone(
    gapped: tuple[Any, Path, str],
) -> None:
    service, root, asset_id = gapped
    quote = _quote(service, root, asset_id)
    assert len(quote.spans) == 2 and len(quote.pieces) == 2
    for span, index in zip(quote.spans, DROPPED, strict=True):
        start, end = bursts()[index]
        assert span.start <= start * RATE and end * RATE <= span.end  # 덩어리를 통째로 묻는다
    assert quote.whisper_due == quote.pieces  # 처음이라 모두 새로 산다
    assert quote.timing_model == GEMINI_TRANSCRIBE_PROFILE.model
    # Gemini 원본에는 다른 귀가 없다(D104·D163) — 글자 요청 칸이 비어 있다.
    assert (quote.text_model, quote.text_identity, quote.text_due) == (None, None, ())
    assert not hasattr(quote, "usd") and not hasattr(quote, "text")  # 금액·문구가 없다(D110)
    assert service.refill_scope(root, asset_id).digest == quote.digest  # 결정적이다


def test_the_digest_moves_with_the_transcript_revision_not_with_the_glossary(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    first = _quote(service, root, asset_id)
    glossary.save(service._workspace(root), "왕방\n백숙")
    # 용어집은 시간 엔진에 가지 않는다(규칙 6) — 범위가 묶을 요청 신원이 없다.
    assert _quote(service, root, asset_id) == first
    transcript = service.source_transcript(root, asset_id)
    first_row = transcript["utterances"][0]
    queued: list[tuple[str, ...]] = []
    # 고친 뒤의 확인을 붙잡아 두고 본다 — 확인 작업(VAD 캐시)이 범위보다 먼저 끝나는 경합을 없앤다.
    monkeypatch.setattr(service, "_queue_sync_checks", lambda _root, ids: queued.append(tuple(ids)))
    service.edit_source_transcript(
        root, asset_id, transcript["revision"], action="edit",
        utterance_id=first_row["utterance_id"], text="고친 말",
    )  # fmt: skip
    assert queued == [(asset_id,)]
    assert service.refill_scope(root, asset_id) == raw.REFILL_WAITING  # 새 리비전은 아직 안 쟀다
    assert service.start_sync_checks(root, (asset_id,)) is not None
    wait_sync(service, root)
    moved = _quote(service, root, asset_id)
    assert moved.transcript_revision != first.transcript_revision
    assert moved.pieces == first.pieces and moved.digest != first.digest


def test_relisten_pending_blocks_the_refill(gapped: tuple[Any, Path, str]) -> None:
    service, root, asset_id = gapped
    service.relisten_source(root, asset_id)
    assert service.refill_scope(root, asset_id) == raw.REFILL_RELISTEN_PENDING


def test_an_unmeasurable_report_says_so_instead_of_waiting(gapped: tuple[Any, Path, str]) -> None:
    """지금 리비전을 잰 보고서가 측정 불가(PCM 없음)면 기다리라고 하지 않고 이유 칸을 가리킨다."""
    service, root, asset_id = gapped
    workspace = service._workspace(root)
    source = sync_report.gather_source(workspace, workspace.source_asset(asset_id))
    assert source is not None
    failed = sync_report.error_document(
        source, sync_report.SYNC_CHECK_SETTINGS, RuntimeError("오디오를 읽지 못함")
    )
    sync_report.write_report(root / sync_report.SYNC_CACHE, failed, sync_report.SYNC_CHECK_SETTINGS)
    assert service.refill_scope(root, asset_id) == raw.REFILL_UNMEASURED


def test_a_source_heard_by_whisper_is_refused_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """whisper로 들은 원본은 빈칸을 새로 묻지 않는다(D163) — 범위도 실행도 같은 이유로 닫힌다."""
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    service, root, _ = _whisper_heard_workspace(tmp_path)
    asset_id = service.list_source_assets(root)[0].asset_id
    workspace = service._workspace(root)
    assert timing_engine.resolve(workspace, asset_id) == TIMING_ENGINE_WHISPER
    assert service.refill_scope(root, asset_id) == raw.REFILL_WHISPER_RETIRED
    _online(monkeypatch)
    sender = RefillSender()
    forged = types.SimpleNamespace(digest="0" * 64)
    with pytest.raises(RawWorkspaceError) as refused:
        service.refill_source(root, asset_id, forged, transport(sender), context())
    assert str(refused.value) == raw.REFILL_WHISPER_RETIRED
    handle = service.refill_start(root, asset_id, forged, transport(sender))
    job = wait(service, root, handle.job_id)
    assert job.status == "failed" and raw.REFILL_WHISPER_RETIRED in str(job.error)
    assert sender.calls == []  # 0송신
    doc = service.source_transcript(root, asset_id)
    assert doc["edited"] is False
    assert not any(row.get(source_transcript.REFILL_FIELD) for row in doc["utterances"])
    assert speech_refill.latest_record(root, asset_id) is None


def _whisper_heard_workspace(tmp_path: Path) -> tuple[Any, Path, str]:
    """덩어리 둘을 뺀 원본 자막을 whisper 모양 재생으로 들은 작업공간 — 옛 whisper 원본(D163).

    새로 듣는 엔진은 Gemini뿐이라, 그 원본의 차례를 whisper로 고정해 은퇴 전에 들은 원본을 만든다.
    whisper 차례의 재생은 whisper 모양 발화 선언(`asr-fixtures.json`)을 읽는다.
    """
    card = tmp_path / "card"
    make_media(card / "tone.mov")
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "whisper 원본")
    workflow = service.settings(root)
    models = replace(workflow.models, luna_model=REPLAY_ANALYSIS_MODEL)
    service.update_settings(root, replace(workflow, models=models))
    proposal = service.scan_sources(root, card)
    copied = service.start_ingest_copy(root, service.confirm_ingest(root, proposal))
    assert wait(service, root, copied.job_id).status == "succeeded"
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    replay = root / "analysis" / "replay"
    replay.mkdir(parents=True, exist_ok=True)
    spoken = [
        {"start": float(start), "end": float(end), "text": f"말 {index}"}
        for index, (start, end) in enumerate(bursts())
        if index not in DROPPED
    ]
    sources = {"sources": {asset.content_sha256 or "": spoken}}
    (replay / "asr-fixtures.json").write_text(json.dumps(sources), "utf-8")
    luna = {f"window_{i:06d}": _luna(f"window_{i:06d}") for i in range(12)}
    (replay / "luna-fixtures.json").write_text(json.dumps({"windows": luna}), "utf-8")
    timing_engine.pin(service._workspace(root), asset.asset_id, 0, TIMING_ENGINE_WHISPER)
    assert wait(service, root, service.start_analysis(root).job_id).status == "succeeded"
    wait_sync(service, root)
    return service, root, asset.asset_id


# ── 실행 — 규칙 1·6 탐침 · 넣기 · 다음 범위 ────────────────────────────────────


def _assert_engine_times(inserted: list[dict[str, Any]], quote: Any) -> None:
    """규칙 1 — 시각은 엔진 단어 시각 + 조각의 실제 표본 오프셋(+ 확인과 같은 원점)뿐이다."""
    for row, (piece_start, _end) in zip(inserted, quote.pieces, strict=True):
        offset = Fraction(piece_start, RATE) + quote.audio_start
        assert source_transcript.rational(row["start"]) == offset + Fraction(1, 2)
        assert source_transcript.rational(row["end"]) == offset + Fraction(5, 4)
        assert (row["start_sample"], row["end_sample"]) == (piece_start + 8000, piece_start + 20000)


def test_refill_inserts_engine_times_plus_the_slice_offset_and_sends_no_hints(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈칸 재전사의 시간 요청은 Gemini 가짜로 간다 — 오디오·언어 잠금·단어 시각뿐이다(규칙 6)."""
    service, root, asset_id = gapped
    _online(monkeypatch)
    glossary.save(service._workspace(root), "왕방\n백숙")
    before = service.source_transcript(root, asset_id)
    quote = _quote(service, root, asset_id)
    sender = RefillSender()
    record = service.refill_source(root, asset_id, quote, transport(sender), context())
    assert len(sender.calls) == 2
    for body in sender.calls:
        assert body["model"] == GEMINI_TRANSCRIBE_PROFILE.model
        assert set(body) == {"model", "store", "input", "generation_config"}
        config = body["generation_config"]
        assert set(config) == {"transcription_config"}
        assert set(config["transcription_config"]) == {"language_codes", "mode"}
        assert config["transcription_config"]["language_codes"] == ["ko-KR"]  # 언어 잠금뿐
        text = json.dumps(body, ensure_ascii=False)
        assert "왕방" not in text and "백숙" not in text  # 용어집은 가지 않는다
    assert sent_pieces(sender, root, asset_id) == sorted(quote.pieces)
    after = service.source_transcript(root, asset_id)
    inserted = [r for r in after["utterances"] if r.get(source_transcript.REFILL_FIELD)]
    assert [r["utterance_id"] for r in inserted] == record["inserted"]
    assert len(inserted) == 2 and after["edited"] is True
    _assert_engine_times(inserted, quote)
    for row in inserted:
        assert row["text"] == ENGINE_TEXT
        assert "text_source" not in row  # 다른 귀가 없다 — 엔진 글자 그대로
    assert record["asr_text"] is None
    old = [json.dumps(r, ensure_ascii=False) for r in before["utterances"]]
    kept = [
        json.dumps(r, ensure_ascii=False)
        for r in after["utterances"]
        if not r.get(source_transcript.REFILL_FIELD)
    ]
    assert kept == old  # 기존 줄은 바이트까지 그대로


def test_inserted_times_follow_the_measured_audio_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """오디오가 영상보다 늦게 시작하면 넣은 시각도 확인이 잰 그 원점만큼 늦다 — 지어낸 값이 없다."""
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    service, root, asset_id = gapped_workspace(tmp_path, audio_delay="0.5")
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    report = service.sync_report(root, asset_id)
    assert quote.audio_start == Fraction(report["pcm"]["audio_start"]) != 0
    service.refill_source(root, asset_id, quote, transport(RefillSender()), context())
    after = service.source_transcript(root, asset_id)
    inserted = [r for r in after["utterances"] if r.get(source_transcript.REFILL_FIELD)]
    assert len(inserted) == 2
    _assert_engine_times(inserted, quote)


def test_a_stale_approval_sends_nothing(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    sender = RefillSender()
    forged = replace(quote, digest="0" * 64)  # 본 적 없는 범위
    with pytest.raises(RawWorkspaceError, match="빈칸이 바뀌었습니다"):
        service.refill_source(root, asset_id, forged, transport(sender), context())
    assert sender.calls == []
    assert service.source_transcript(root, asset_id)["edited"] is False


def test_the_run_never_trusts_the_approved_objects_fields_only_its_digest(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """승인 객체의 조각을 바꿔도 실행은 다시 잰 범위(digest가 같은 것)의 조각만 묻는다."""
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    forged = replace(quote, pieces=((0, RATE),), whisper_due=((0, RATE),))
    sender = RefillSender()
    service.refill_source(root, asset_id, forged, transport(sender), context())
    sent = sent_pieces(sender, root, asset_id)
    assert sent == sorted(quote.pieces) and all(start != 0 for start, _end in sent)


def test_the_offline_gate_closes_every_send(gapped: tuple[Any, Path, str]) -> None:
    """환경이 오프라인이면 승인한 범위여도 0송신이다 — 조각은 실패로 남고 아무것도 넣지 않는다."""
    service, root, asset_id = gapped
    quote = _quote(service, root, asset_id)
    sender = RefillSender()
    record = service.refill_source(root, asset_id, quote, transport(sender), context())
    assert sender.calls == []
    assert record["inserted"] == [] and len(record["failed"]) == 2
    assert "VLOGSRT_OFFLINE" in record["failed"][0]["failure"]
    assert service.source_transcript(root, asset_id)["edited"] is False


def test_overlapping_answers_are_warned_and_recorded(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    # 문장 끝 부호 뒤에서 줄이 끊긴다 — 엔진 단어 시각 그대로라 두 줄이 겹친다(D92 — 순서는 그대로).
    sender = RefillSender(
        [
            {"start": 0.5, "end": 1.25, "text": "앞 말."},
            {"start": 1.0, "end": 1.5, "text": "겹친 말"},
        ]
    )
    record = service.refill_source(
        root, asset_id, _quote(service, root, asset_id), transport(sender), context()
    )
    assert len(record["inserted"]) == 2 and len(record["overlapped"]) == 2
    assert {item["text"] for item in record["overlapped"]} == {"겹친 말"}
    assert any("겹친 줄 2개" in line for line in warnings), warnings
    runs = list((root / "cache" / speech_refill.REFILL_DIRNAME / asset_id / "runs").glob("*.json"))
    stored = json.loads(runs[0].read_text(encoding="utf-8"))
    assert [item["text"] for item in stored["overlapped"]] == ["겹친 말", "겹친 말"]


def test_a_transcript_edited_during_the_run_is_not_overwritten(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    original = speech_refill.run

    def edit_while_asking(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        doc = service.source_transcript(root, asset_id)
        service.edit_source_transcript(
            root, asset_id, doc["revision"], action="edit",
            utterance_id=doc["utterances"][0]["utterance_id"], text="사람이 고침",
        )  # fmt: skip
        return result

    monkeypatch.setattr(speech_refill, "run", edit_while_asking)
    with pytest.raises(RawWorkspaceError, match="원본 자막이 바뀌었습니다"):
        service.refill_source(root, asset_id, quote, transport(RefillSender()), context())
    doc = service.source_transcript(root, asset_id)
    assert not any(row.get(source_transcript.REFILL_FIELD) for row in doc["utterances"])
    record = speech_refill.latest_record(root, asset_id)
    assert record is not None and "원본 자막이 바뀌었습니다" in str(record["refused"])
    wait_sync(service, root)  # 고친 리비전을 다시 잰다 — 받은 답은 캐시에 남아 다시 사지 않는다
    again = _quote(service, root, asset_id)
    assert again.pieces == quote.pieces and again.whisper_due == () and again.text_due == ()


def test_after_a_refill_the_asked_pieces_are_not_bought_again(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    sender = RefillSender()
    service.refill_source(
        root, asset_id, _quote(service, root, asset_id), transport(sender), context()
    )
    wait_sync(service, root)  # 넣은 뒤의 새 리비전을 잰다(VAD는 캐시)
    again = service.refill_scope(root, asset_id)
    assert isinstance(again, speech_refill.RefillScope)
    assert again.pieces == () and again.whisper_due == () and again.text_due == ()
    assert again.asked  # 이미 다시 물어 넣은 조각 안의 빈칸 — 다시 사지 않는다


def test_the_next_stage1_run_keeps_the_refilled_rows(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """다음 1단계 심기가 삽입 세그먼트를 지우지 않는다 — 실제 1단계 작업(재생)을 다시 돌려 본다."""
    service, root, asset_id = gapped
    _online(monkeypatch)
    service.refill_source(
        root, asset_id, _quote(service, root, asset_id), transport(RefillSender()), context()
    )
    filled = service.source_transcript(root, asset_id)
    wait_sync(service, root)
    monkeypatch.undo()  # 다시 오프라인 — 재생 분석은 유료 호출이 없다
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    assert wait(service, root, service.start_analysis(root).job_id).status == "succeeded"
    assert service.source_transcript(root, asset_id) == filled


def test_the_job_wiring_runs_the_same_body(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    sender = RefillSender()
    handle = service.refill_start(
        root, asset_id, _quote(service, root, asset_id), transport(sender)
    )
    record = wait(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    assert record.kind == raw.REFILL_JOB_KIND and len(sender.calls) == 2
    assert not service.refill_running(root, asset_id)
    last = service.refill_last(root, asset_id)
    assert last is not None and len(last["inserted"]) == 2
