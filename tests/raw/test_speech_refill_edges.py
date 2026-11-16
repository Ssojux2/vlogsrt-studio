"""D59 19f 검증 뒤 고침 — 말소리 빈칸 재전사의 가장자리(독립 검증의 적대 시험을 접어 넣었다).

유료 호출은 없다 — 두 귀는 요청을 세는 가짜 전송이고, 환경 문은 시험 안에서만 가짜로 연다
(`tests/raw/test_speech_refill.py`와 같은 재료). 시험이 못 박는 것:

- 다시 듣기와 빈칸 재전사는 서로를 헛돈으로 만들지 않는다 — 빈칸 재전사가 도는 동안 다시 듣기는
  거절되고, 그래도 넣을 때 다시 듣기가 기다리고 있으면 넣지 않는다(받은 답은 캐시에 남아 $0).
- 넣은 뒤 기록을 쓰기 전에 멈춰도 같은 소리를 다시 사지 않는다(넣기 전에 남긴 줄 표지).
- 견적 문구의 말소리는 이번에 묻는 조각 안의 것만 센다.
- 실행마다 기록이 따로 남고, 사이 글자의 앞뒤 줄 아이디는 넣은 줄의 아이디다.
- 시각은 whisper 세그먼트 + 조각 오프셋뿐이다(어색한 소수·조각 끝 넘침·길이 0 포함, 규칙 1).
- 견적 뒤에 무엇이든 바뀌면 0송신이고, 두 탭이 함께 눌러도 작업은 하나다.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests._sync_fakes import energy_factory, numpy_or_fake
from tests.raw.test_speech_refill import (
    RATE,
    RefillSender,
    _online,
    _quote,
    context,
    gapped_workspace,
    transport,
    wait,
    wait_sync,
)
from vlogsrt import log
from vlogsrt.project.raw import asr_epoch, source_transcript, speech_refill, sync_report
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.transcribe import CAPTION_TEXT_KIND, CAPTION_TIMING_KIND

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")


@pytest.fixture
def gapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    return gapped_workspace(tmp_path)


def _inserted(service: Any, root: Path, asset_id: str) -> list[dict[str, Any]]:
    doc = service.source_transcript(root, asset_id)
    return [r for r in doc["utterances"] if r.get(source_transcript.REFILL_FIELD)]


def _seconds(quote: Any) -> list[Fraction]:
    return [Fraction(b - a, RATE) for a, b in quote.pieces]


class Blocking(RefillSender):
    """첫 요청에서 멈춰 서는 가짜 전송 — 작업이 도는 동안의 경합을 재현한다."""

    def __init__(self) -> None:
        super().__init__()
        self.release = threading.Event()
        self.entered = threading.Event()

    def send_request(self, request: Any, cfg: Any, *, kind: str, stem: str, source: str) -> Any:
        self.entered.set()
        assert self.release.wait(30)
        return super().send_request(request, cfg, kind=kind, stem=stem, source=source)


# ── 1. 다시 듣기와 빈칸 재전사의 순서 ─────────────────────────────────────────────


def test_relisten_is_refused_while_a_refill_runs(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    sender = Blocking()
    quote = _quote(service, root, asset_id)
    handle = service.refill_start(root, asset_id, quote, transport(sender))
    assert sender.entered.wait(30)
    with pytest.raises(RawWorkspaceError) as refused:
        service.relisten_source(root, asset_id)
    assert str(refused.value) == raw.RELISTEN_REFILL_RUNNING_REFUSAL
    sender.release.set()
    assert wait(service, root, handle.job_id).status == "succeeded"
    state = asr_epoch.state(service._workspace(root), asset_id)
    assert (state.epoch, state.pending) == (0, False)  # 차례는 오르지 않았다
    assert service.source_transcript(root, asset_id)["edited"] is True


def test_a_relisten_pending_at_settle_refuses_the_insert_and_keeps_the_answers(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """작업 밖에서 차례가 올라 버린 경우(방어선 둘째) — 넣지 않고, 산 답은 다음 견적에서 $0이다.

    작업 없이 본문을 직접 부르면 「진행 중」 거절이 닿지 않는다 — 그 틈에 다시 듣기를 눌렀다."""
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    original = speech_refill.run

    def relisten_mid_run(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        service.relisten_source(root, asset_id)  # 받은 뒤 넣기 전에 다시 듣기를 눌렀다
        return result

    monkeypatch.setattr(speech_refill, "run", relisten_mid_run)
    sender = RefillSender()
    with pytest.raises(RawWorkspaceError) as refused:
        service.refill_source(root, asset_id, quote, transport(sender), context())
    assert str(refused.value) == raw.REFILL_RELISTEN_SETTLE_REFUSAL
    doc = service.source_transcript(root, asset_id)
    assert doc["edited"] is False and not _inserted(service, root, asset_id)
    assert asr_epoch.state(service._workspace(root), asset_id).pending  # 다시 듣기는 그대로
    record = speech_refill.latest_record(root, asset_id)
    assert record is not None and record["refused"] == raw.REFILL_RELISTEN_SETTLE_REFUSAL
    assert service.refill_scope(root, asset_id) == raw.REFILL_RELISTEN_PENDING
    asr_epoch.withdraw(service._workspace(root), asset_id)  # 사람이 다시 듣기를 거뒀다면
    again = _quote(service, root, asset_id)
    assert again.pieces == quote.pieces  # 산 답은 캐시 — 다시 사지 않는다
    assert again.whisper_due == () and again.text_due == ()
    assert len(sender.whisper) == 2 and len(sender.text) == 2


# ── 3. 넣은 뒤 기록 전에 멈춰도 다시 사지 않는다 ─────────────────────────────────


def test_a_crash_between_insert_and_the_ledger_does_not_buy_again(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)

    def boom(*_args: Any, **_kwargs: Any) -> None:
        raise OSError("asked.json을 쓰는 중에 디스크가 찼다")

    monkeypatch.setattr(speech_refill, "mark_asked", boom)
    with pytest.raises(OSError):
        service.refill_source(root, asset_id, quote, transport(RefillSender()), context())
    assert len(_inserted(service, root, asset_id)) == 2  # 줄은 들어갔고 장부는 못 썼다
    monkeypatch.undo()
    _online(monkeypatch)
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    assert service.start_sync_checks(root, (asset_id,)) is not None
    wait_sync(service, root)
    again = _quote(service, root, asset_id)
    assert again.pieces == () and again.asked  # 넣은 줄의 표지가 장부를 대신한다


# ── 4·8. 견적의 말소리 · 실행 기록 · 사이 글자 ─────────────────────────────────────


class FailSecond(RefillSender):
    """둘째 조각(청크 차례 1)의 whisper 요청만 실패한다 — 조각 하나는 받고 하나는 못 받는다.

    조각은 함께 보낸다(P-5, D159) — 「두 번째로 온 요청」이 아니라 둘째 조각을 실패시킨다."""

    def send_request(self, request: Any, cfg: Any, *, kind: str, stem: str, source: str) -> Any:
        if kind == CAPTION_TIMING_KIND and request.chunk.index == 1:
            self.whisper.append(request)
            raise RuntimeError("네트워크가 끊겼다")
        return super().send_request(request, cfg, kind=kind, stem=stem, source=source)


def test_partial_failure_requotes_only_the_failed_piece_and_counts_only_its_speech(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    record = service.refill_source(root, asset_id, quote, transport(FailSecond()), context())
    assert len(record["inserted"]) == 1 and len(record["failed"]) == 1
    wait_sync(service, root)
    again = _quote(service, root, asset_id)
    assert again.pieces == (quote.pieces[1],)
    assert again.whisper_due == again.pieces and again.text_due == again.pieces
    # 이번에 묻는 조각 안의 말소리만 센다 — 이미 물은 조각 안의 나머지는 뺀다.
    asking = again.asking_speech_seconds
    assert 0 < asking < again.speech_seconds
    sender = RefillSender()
    service.refill_source(root, asset_id, again, transport(sender), context())
    assert len(sender.whisper) == 1 and len(sender.text) == 1
    assert len(_inserted(service, root, asset_id)) == 2


def test_every_run_keeps_its_own_record(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 견적을 다시 승인해도(앞 실행이 모두 실패) 앞 기록을 덮어쓰지 않는다."""
    service, root, asset_id = gapped
    quote = _quote(service, root, asset_id)
    offline = service.refill_source(root, asset_id, quote, transport(RefillSender()), context())
    assert len(offline["failed"]) == 2  # 환경 문이 닫혀 0송신
    _online(monkeypatch)
    service.refill_source(root, asset_id, quote, transport(RefillSender()), context())
    runs = sorted((root / "cache" / speech_refill.REFILL_DIRNAME / asset_id / "runs").glob("*"))
    assert [p.name for p in runs] == [f"{quote.refill_id}-001.json", f"{quote.refill_id}-002.json"]
    bodies = [json.loads(p.read_text(encoding="utf-8")) for p in runs]
    assert [len(b["inserted"]) for b in bodies] == [0, 2]
    assert speech_refill.latest_record(root, asset_id) == bodies[1]


def test_text_between_segments_is_recorded_with_the_inserted_row_ids(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)

    class Between(RefillSender):
        def send_request(self, request: Any, cfg: Any, *, kind: str, stem: str, source: str) -> Any:
            if kind == CAPTION_TEXT_KIND:
                self.text.append(request)
                text = "앞에 하는 말 가운데 끼어든 아주 긴 말 뒤에 하는 말"
                return {"text": text, "languages": [{"code": "ko"}]}
            return super().send_request(request, cfg, kind=kind, stem=stem, source=source)

    sender = Between(
        [
            {"start": 0.1, "end": 0.6, "text": "앞에 하는 말"},
            {"start": 1.3, "end": 1.8, "text": "뒤에 하는 말"},
        ]
    )
    record = service.refill_source(root, asset_id, quote, transport(sender), context())
    rows = _inserted(service, root, asset_id)
    assert not any("가운데" in r["text"] or "끼어든" in r["text"] for r in rows)  # 규칙 1
    unplaced = record["asr_text"]["unplaced"]
    assert unplaced and any("끼어든" in item["text"] for item in unplaced)
    ids = {r["utterance_id"] for r in rows}
    for item in unplaced:
        for side in (item["before"], item["after"]):
            assert side is None or side in ids  # 넣은 줄의 아이디(refill_…)를 가리킨다


# ── 규칙 1 — 시각의 모양 ───────────────────────────────────────────────────────────


def test_awkward_float_segments_map_exactly_to_ticks_plus_the_slice_offset(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    service.set_cross_check(root, False)
    quote = _quote(service, root, asset_id)
    segment = (0.1234567, 1.0000312)
    sender = RefillSender([{"start": segment[0], "end": segment[1], "text": "소수"}])
    service.refill_source(root, asset_id, quote, transport(sender), context())
    for row, (start, _end) in zip(_inserted(service, root, asset_id), quote.pieces, strict=True):
        head = start + math.floor(segment[0] * RATE + 0.5)
        tail = start + math.floor(segment[1] * RATE + 0.5)
        assert (row["start_sample"], row["end_sample"]) == (head, tail)
        assert source_transcript.rational(row["start"]) == Fraction(head, RATE) + quote.audio_start
        assert source_transcript.rational(row["end"]) == Fraction(tail, RATE) + quote.audio_start


def test_a_small_overrun_is_closed_at_the_slice_end_with_a_warning(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    service.set_cross_check(root, False)
    quote = _quote(service, root, asset_id)
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    over = float(min(_seconds(quote))) + 1.0  # 관용(2초) 안의 넘침
    sender = RefillSender([{"start": 0.5, "end": over, "text": "넘친 말"}])
    service.refill_source(root, asset_id, quote, transport(sender), context())
    rows = _inserted(service, root, asset_id)
    assert len(rows) == 2
    for row, (start, end) in zip(rows, quote.pieces, strict=True):
        assert source_transcript.rational(row["end"]) <= Fraction(end, RATE) + quote.audio_start
        expected = Fraction(start, RATE) + quote.audio_start + Fraction(1, 2)
        assert source_transcript.rational(row["start"]) == expected
    assert any("청크 끝" in line for line in warnings), warnings


def test_a_far_overrun_fails_the_piece_and_the_same_quote_can_be_asked_again(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    service.set_cross_check(root, False)
    quote = _quote(service, root, asset_id)
    far = float(max(_seconds(quote))) + 5.0
    sender = RefillSender([{"start": 0.5, "end": far, "text": "너무 넘친 말"}])
    record = service.refill_source(root, asset_id, quote, transport(sender), context())
    assert record["inserted"] == [] and len(record["failed"]) == 2
    again = _quote(service, root, asset_id)  # 표시하지 않았고 실패는 캐시되지 않는다
    assert again.pieces == quote.pieces and again.whisper_due == quote.pieces
    assert again.digest == quote.digest


def test_a_zero_length_segment_is_inserted_and_the_transcript_stays_editable(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    service.set_cross_check(root, False)
    quote = _quote(service, root, asset_id)
    sender = RefillSender([{"start": 0.5, "end": 0.5, "text": "길이 영"}])
    service.refill_source(root, asset_id, quote, transport(sender), context())
    rows = _inserted(service, root, asset_id)
    assert [r["text"] for r in rows] == ["길이 영", "길이 영"]
    assert all(r["start"] == r["end"] for r in rows)  # 글자는 남는다(규칙 9)
    doc = service.source_transcript(root, asset_id)
    service.edit_source_transcript(
        root, asset_id, doc["revision"], action="edit",
        utterance_id=doc["utterances"][0]["utterance_id"], text="편집 가능",
    )  # fmt: skip


# ── 비용 문 ───────────────────────────────────────────────────────────────────────


def test_two_tabs_approving_at_once_start_one_job_and_a_late_repeat_sends_nothing(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    sender = Blocking()
    handles: list[Any] = []
    barrier = threading.Barrier(4)

    def approve() -> None:
        barrier.wait()
        handles.append(service.refill_start(root, asset_id, quote, transport(sender)))

    threads = [threading.Thread(target=approve) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len({h.job_id for h in handles}) == 1
    assert sum(1 for h in handles if not h.reattached) == 1
    sender.release.set()
    assert wait(service, root, handles[0].job_id).status == "succeeded"
    assert len(sender.whisper) == 2 and len(sender.text) == 2
    late = service.refill_start(root, asset_id, quote, transport(sender))
    assert wait(service, root, late.job_id).status == "failed"
    assert len(sender.whisper) == 2 and len(sender.text) == 2  # 낡은 승인 — 0송신


def test_toggling_cross_check_between_quote_and_approval_sends_nothing(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    service.set_cross_check(root, False)
    sender = RefillSender()
    with pytest.raises(RawWorkspaceError):
        service.refill_source(root, asset_id, quote, transport(sender), context())
    off = _quote(service, root, asset_id)
    service.set_cross_check(root, True)
    with pytest.raises(RawWorkspaceError):
        service.refill_source(root, asset_id, off, transport(sender), context())
    assert sender.whisper == [] and sender.text == []


def test_a_human_edit_between_quote_and_approval_sends_nothing(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """견적 뒤에 사람이 자막을 고쳤다 — 리비전이 견적에 묶여 있어 0송신이다."""
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    doc = service.source_transcript(root, asset_id)
    monkeypatch.setattr(service, "_queue_sync_checks", lambda *_: None)
    service.edit_source_transcript(
        root, asset_id, doc["revision"], action="edit",
        utterance_id=doc["utterances"][0]["utterance_id"], text="사람이 고침",
    )  # fmt: skip
    sender = RefillSender()
    with pytest.raises(RawWorkspaceError):
        service.refill_source(root, asset_id, quote, transport(sender), context())
    assert sender.whisper == [] and sender.text == []
    assert not _inserted(service, root, asset_id)


def test_toggling_cross_check_during_the_run_keeps_the_approved_plan(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    service.set_cross_check(root, False)
    quote = _quote(service, root, asset_id)
    sender = Blocking()
    handle = service.refill_start(root, asset_id, quote, transport(sender))
    assert sender.entered.wait(30)
    service.set_cross_check(root, True)
    sender.release.set()
    assert wait(service, root, handle.job_id).status == "succeeded"
    assert len(sender.whisper) == 2 and sender.text == []  # whisper만 승인했다


def test_cached_answers_quote_zero_and_send_nothing(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)

    def refused(*_args: Any, **_kwargs: Any) -> Any:
        raise RawWorkspaceError("원본 자막이 바뀌었습니다")

    monkeypatch.setattr(source_transcript, "insert_segments", refused)
    first = RefillSender()
    with pytest.raises(RawWorkspaceError):
        service.refill_source(root, asset_id, quote, transport(first), context())
    assert len(first.whisper) == 2 and len(first.text) == 2
    monkeypatch.undo()
    _online(monkeypatch)
    again = _quote(service, root, asset_id)
    assert again.whisper_due == () and again.text_due == ()
    second = RefillSender()
    service.refill_source(root, asset_id, again, transport(second), context())
    assert second.whisper == [] and second.text == []
    rows = _inserted(service, root, asset_id)
    assert len(rows) == 2 and all(r.get("text_source") for r in rows)


def test_a_piece_outside_the_approval_is_refused_at_the_send_gate(
    gapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """실행에 넘긴 견적의 새로 살 조각이 하나뿐이면 다른 조각은 전송 경계에서 닫힌다."""
    service, root, asset_id = gapped
    _online(monkeypatch)
    quote = _quote(service, root, asset_id)
    narrowed = replace(quote, whisper_due=(quote.pieces[0],))
    plan = service._refill_plan(root, asset_id)
    audio = service._refill_audio(service._workspace(root), asset_id, plan)
    sender = RefillSender()
    result = speech_refill.run(plan, narrowed, audio, transport(sender), on=date.today())
    assert len(sender.whisper) == 1
    assert len(result.failed) == 1
    assert "승인한 범위에 없던" in json.dumps(result.failed, ensure_ascii=False)
