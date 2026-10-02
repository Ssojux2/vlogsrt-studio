"""A7-2 최종 자막 엔진 — 문구/시간 결합·승인·수명주기·재전사·검증 불변의 계약 검사.

제공자는 전부 주입된 가짜다 — 네트워크는 없고, 요금·호출 수는 가짜의 기록으로
잰다. 실추출 시험만 ffmpeg 합성 최종 영상을 만들어 공개 경계를 실제로 돈다.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import FrozenInstanceError, replace
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.models import REPAIR_COLLAPSED, REPAIR_REPLACED
from vlogsrt.project.raw import final_caption as engine
from vlogsrt.project.raw.caption_artifacts import (
    ArtifactRevisionMismatch,
    caption_srt_text,
    publish_caption_artifacts,
)
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.caption_provider import (
    parse_text_response,
    parse_timing_response,
    verify_chunk_binding,
)
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.final_caption import (
    CaptionApproval,
    CaptionApprovalMismatch,
    CaptionApprovalMissing,
    CaptionBudgetExhausted,
    CaptionProviderMissing,
    CaptionRunCancelled,
    CaptionRunError,
    FinalCaptionResult,
    _Ledger,
    expected_approval,
    plan_caption_run,
    run_final_captions,
)
from vlogsrt.project.raw.final_video import (
    FinalVideoInput,
    register_final_video,
)
from vlogsrt.project.raw.understanding import PCM_SAMPLE_RATE, SelectedAudio

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

BEFORE_SHUTDOWN = date(2026, 10, 2)
SHUTDOWN_DAY = date(2027, 2, 26)

CORRECTED = "망원동 카페에 왔어요"
WRONG = "망원동 커피에 왔어요"


def make_video(
    tmp_path: Path,
    *,
    seconds: float = 40.0,
    origin: Fraction = Fraction(-1, 50),
    revision_body: bytes = b"synthetic-final-video",
    video_duration: float = 40.5,
) -> FinalVideoInput:
    """합성 PCM 위의 최종 영상 입력 — 리비전은 내용 해시다."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    samples = int(seconds * PCM_SAMPLE_RATE)
    pcm = tmp_path / "caption.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * 2)))
    source = tmp_path / "final.mp4"
    source.write_bytes(revision_body)
    digest = hashlib.sha256(revision_body).hexdigest()
    return FinalVideoInput(
        source_id="final_test",
        final_video_revision=digest,
        original_path=source,
        verified_copy_path=source,
        verified_copy_sha256=digest,
        manifest_path=tmp_path / "final-video.json",
        audio=SelectedAudio(
            source_id="final_test",
            source_path=source,
            source_sha256=digest,
            audio_stream_index=1,
            pcm_path=pcm,
            pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
            sample_rate=PCM_SAMPLE_RATE,
            channels=1,
            sample_count=samples,
            pcm_sample0_file_time=origin,
        ),
        video_duration=video_duration,
        audio_stream_count=1,
    )


class FakeTiming:
    """시간 제공자 가짜 — 재전사 요청은 `:rescue:` 요청 ID로 갈라 받는다."""

    def __init__(
        self,
        segments_by_chunk: dict[int, list[dict[str, Any]]],
        *,
        rescue: list[dict[str, Any]] | None = None,
        fail: set[int] | None = None,
        malform: set[int] | None = None,
        stale: set[int] | None = None,
    ) -> None:
        self.segments_by_chunk = segments_by_chunk
        self.rescue = rescue
        self.fail = fail or set()
        self.malform = malform or set()
        self.stale = stale or set()
        self.calls: list[str] = []

    def timed_transcript(
        self,
        *,
        binding: Any,
        chunk: Any,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
        temperature: float | None = None,
    ) -> Any:
        self.calls.append(binding.request_id)
        if chunk.index in self.stale:
            chunk.path.write_bytes(chunk.path.read_bytes() + b"\x00")  # 신원 뒤 바뀐 파일
            verify_chunk_binding(binding, chunk)
        if chunk.index in self.fail:
            raise RuntimeError("시간 제공자가 닫혀 있다")
        if ":rescue:" in binding.request_id:
            if self.rescue is None:
                raise RuntimeError("재전사 대답이 준비되지 않았다")
            records = self.rescue
        elif chunk.index in self.malform:
            records = None  # 꼴이 어긋난 응답 — segments가 없다
        else:
            records = self.segments_by_chunk[chunk.index]
        payload: dict[str, Any] = {"segments": records} if records is not None else {"text": "x"}
        return parse_timing_response(
            payload,
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


class FakeText:
    """문구 제공자 가짜 — 심판 요청은 `:arbiter:` 요청 ID로 갈라 받는다."""

    def __init__(
        self,
        text_by_chunk: dict[int, str],
        *,
        fail: set[int] | None = None,
        malform: set[int] | None = None,
        arbiter_text: str = "",
    ) -> None:
        self.text_by_chunk = text_by_chunk
        self.fail = fail or set()
        self.malform = malform or set()
        self.arbiter_text = arbiter_text
        self.calls: list[str] = []

    def text_transcript(
        self,
        *,
        binding: Any,
        chunk: Any,
        settings: RawWorkflowConfig,
        on: date,
        prompt: str = "",
        stem: str = "",
        source: str = "",
    ) -> Any:
        self.calls.append(binding.request_id)
        if ":arbiter:" in binding.request_id:
            return parse_text_response(
                {"text": self.arbiter_text}, binding=binding, model=settings.models.text_model
            )
        if chunk.index in self.fail:
            raise RuntimeError("문구 제공자가 닫혀 있다")
        if chunk.index in self.malform:
            return parse_text_response(
                {"language": "ko"}, binding=binding, model=settings.models.text_model
            )
        return parse_text_response(
            {"text": self.text_by_chunk[chunk.index]},
            binding=binding,
            model=settings.models.text_model,
        )


TAIL = "세번째 꼬리"


def two_chunk_run(
    tmp_path: Path,
    *,
    timing: FakeTiming | None = None,
    text: FakeText | None = None,
    verify_enabled: bool = True,
    cancellation: Any = None,
    run_id: str = "run-1",
    origin: Fraction = Fraction(-1, 50),
    single_chunk: bool = False,
) -> tuple[FinalCaptionResult, FakeTiming, FakeText]:
    """40초 오디오를 15초 청크 셋(꼬리 포함)으로 나눈 기본 실행 — 교정이 기본 시나리오."""
    video = make_video(tmp_path, origin=origin)
    settings = (
        FinalCaptionSettings() if single_chunk else FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    timing = timing or FakeTiming(
        {
            0: [
                {"text": f" {WRONG}", "start": 0.5, "end": 2.5},
                {"text": " 다음 장면", "start": 3.0, "end": 5.0},
            ],
            1: [{"text": " 두번째 조각", "start": 1.0, "end": 3.0}],
            2: [{"text": "세번째 꼬리", "start": 0.5, "end": 2.0}],
        }
    )
    text = text or FakeText(
        {0: f"{CORRECTED} 다음 장면", 1: "두번째 조각", 2: TAIL},
    )
    plan = plan_caption_run(
        video,
        run_id=run_id,
        settings=settings,
    )
    result = run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=text,
        workdir=tmp_path / "work",
        verify_enabled=verify_enabled,
        cancellation=cancellation,
    )
    return result, timing, text


# ─────────────────────────────────────────────────────────────
#  문구 교정 — 같은 시각 위에서 글자만 갈아낀다
# ─────────────────────────────────────────────────────────────


def test_erroneous_timing_text_is_corrected_with_exact_times(tmp_path: Path) -> None:
    """틀린 whisper 글자가 대응 검증을 통과해 교정되고, 시각은 골격 그대로다(A7-2 완료 기준)."""
    result, timing, text = two_chunk_run(tmp_path)
    assert [cue.text for cue in result.cues] == [CORRECTED, "다음 장면", "두번째 조각", TAIL]
    first = result.cues[0]
    assert first.whisper_text == WRONG
    assert first.correction_status == engine.CORRECTION_ACCEPTED
    assert first.retexted
    assert first.start == Fraction(-1, 50) + Fraction(1, 2)  # 원점 + 청크 0 + 세그먼트 0.5
    assert first.end == Fraction(-1, 50) + Fraction(5, 2)
    assert first.timing_segment_ids == (0,)
    assert first.text_evidence_ids == (f"{result.run_id}:text:0000",)
    # 교정은 시각을 못 움직인다 — 모든 큐의 시각은 골격 세그먼트의 환산 그대로다
    for cue, segment in zip(result.cues, result.spine, strict=False):
        video_origin = Fraction(-1, 50)
        assert cue.start == video_origin + Fraction(str(segment.start))
        assert cue.end == video_origin + Fraction(str(segment.end))


def test_second_chunk_offset_is_applied_once(tmp_path: Path) -> None:
    """두번째 청크의 큐는 청크 시작 15초가 정확히 한 번만 더해진 시각이다(§11.6)."""
    result, _, _ = two_chunk_run(tmp_path)
    third = result.cues[2]
    assert third.text == "두번째 조각"
    assert third.start == Fraction(-1, 50) + Fraction(15) + Fraction(1)
    assert third.end == Fraction(-1, 50) + Fraction(15) + Fraction(3)
    tail = result.cues[3]
    assert tail.text == TAIL  # 꼬리 청크(10쪽)도 큐로 선다


def test_unmatched_text_creates_no_cue(tmp_path: Path) -> None:
    """세그먼트 사이에 낀 문구는 기록일 뿐 — 가짜 cue가 생기지 않는다(§11.5)."""
    extra = "그리고 나서 사진을 찍었어요"
    text = FakeText({0: f"{WRONG} {extra} 다음 장면", 1: "두번째 조각", 2: TAIL})
    result, _, fake = two_chunk_run(tmp_path, text=text)
    assert len(result.cues) == 4  # 큐 수는 골격 수와 같다
    assert result.retext.unplaced and any(item.text == extra for item in result.retext.unplaced)
    assert result.status == engine.STATUS_REVIEW_REQUIRED
    assert any(item.code == "unmapped_text" for item in result.findings)
    assert len(fake.calls) == 3  # 추가 문구 호출은 없다


def test_rejected_correction_keeps_spine_text(tmp_path: Path) -> None:
    """게이트를 통과 못 한 문구는 whisper 글자가 그대로 나간다 — 시각도 그대로(D37)."""
    alien = "이 자리에 전혀 다른 아주 긴 이야기를 끼워 넣으면 비율 게이트가 닫는다"
    text = FakeText({0: f"{alien} 다음 장면", 1: "두번째 조각", 2: TAIL})
    result, _, _ = two_chunk_run(tmp_path, text=text)
    assert result.cues[0].text == WRONG
    assert result.cues[0].correction_status == engine.CORRECTION_REJECTED
    assert result.retext.rejections
    assert result.status == engine.STATUS_REVIEW_REQUIRED


def test_result_and_spine_are_immutable(tmp_path: Path) -> None:
    result, _, _ = two_chunk_run(tmp_path)
    with pytest.raises(FrozenInstanceError):
        result.status = engine.STATUS_READY  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────
#  실패 — 숨지 않고 상태로 남는다
# ─────────────────────────────────────────────────────────────


def test_failed_text_chunk_is_partial_failure_not_success(tmp_path: Path) -> None:
    """문구 요청이 실패한 조각은 partial_failure·명시적 초안 — 최신 문구 성공이 아니다."""
    text = FakeText({0: f"{CORRECTED} 다음 장면", 1: "두번째 조각", 2: TAIL}, fail={1})
    result, _, fake = two_chunk_run(tmp_path, text=text)
    assert result.status == engine.STATUS_PARTIAL_FAILURE
    assert result.is_draft
    record = result.chunks[1]
    assert not record.text_ok and "닫혀 있다" in record.text_failure
    assert result.cues[2].correction_status == engine.CORRECTION_UNAVAILABLE
    assert result.cues[2].text == "두번째 조각"  # 시간 골격 글자가 그대로
    assert len(fake.calls) == 3  # 실패 조각도 한 번은 물었다 — 재시도 감춤은 없다


def test_failed_timing_chunk_keeps_other_chunks(tmp_path: Path) -> None:
    timing = FakeTiming(
        {
            0: [
                {"text": f" {WRONG}", "start": 0.5, "end": 2.5},
                {"text": " 다음 장면", "start": 3.0, "end": 5.0},
            ],
            1: [{"text": " 두번째 조각", "start": 1.0, "end": 3.0}],
        },
        fail={1},
    )
    result, _, _ = two_chunk_run(tmp_path, timing=timing)
    assert result.status == engine.STATUS_PARTIAL_FAILURE
    assert len(result.cues) == 2  # 실패 조각에 큐가 없다 — 빈 성공이 아니다
    assert not result.chunks[1].timing_ok


def test_malformed_responses_are_recorded_failures(tmp_path: Path) -> None:
    """꼴이 어긋난 응답은 지어내지 않고 실패로 남는다 — text/timing 역할 칸 혼동 포함."""
    timing = FakeTiming({0: [], 1: [], 2: []}, malform={0})
    text = FakeText({0: "x", 1: "y", 2: "z"}, malform={1})
    result, _, _ = two_chunk_run(tmp_path, timing=timing, text=text)
    assert result.status == engine.STATUS_PARTIAL_FAILURE
    assert "MalformedCaptionResponse" in result.chunks[0].timing_failure
    assert "MalformedCaptionResponse" in result.chunks[1].text_failure


def test_stale_binding_is_rejected_before_trust(tmp_path: Path) -> None:
    """묶은 뒤 바뀐 청크는 낡은 신원이다 — 그 응답을 믿는 길이 없다(루트 재검)."""
    timing = FakeTiming(
        {
            0: [{"text": f" {WRONG}", "start": 0.5, "end": 2.5}],
            1: [{"text": " 두번째 조각", "start": 1.0, "end": 3.0}],
            2: [{"text": "세번째 꼬리", "start": 0.5, "end": 2.0}],
        },
        stale={0},
    )
    result, _, _ = two_chunk_run(tmp_path, timing=timing)
    assert result.status == engine.STATUS_PARTIAL_FAILURE
    assert "ChunkBindingMismatch" in result.chunks[0].timing_failure


# ─────────────────────────────────────────────────────────────
#  검증 불변 — on/off가 바이트도 호출 수도 바꾸지 않는다
# ─────────────────────────────────────────────────────────────


def test_verification_toggle_changes_nothing(tmp_path: Path) -> None:
    """검증 on/off — SRT 바이트가 같고 문구 호출 수도 같다(§11.3, 규칙 5)."""
    on_result, _, on_text = two_chunk_run(tmp_path / "on", verify_enabled=True, run_id="run-on")
    off_result, _, off_text = two_chunk_run(
        tmp_path / "off", verify_enabled=False, run_id="run-off"
    )
    assert caption_srt_text(on_result) == caption_srt_text(off_result)
    assert len(on_text.calls) == len(off_text.calls)  # 문구 호출 수가 같다
    assert [call.split(":", 1)[1] for call in on_text.calls] == [
        call.split(":", 1)[1] for call in off_text.calls
    ]
    assert on_result.retext.texts == off_result.retext.texts
    assert on_result.verify_rows and on_result.verify_rows[0].chunk_index == 0


# ─────────────────────────────────────────────────────────────
#  온라인 문 — 승인·환경·수명주기가 첫 호출 앞에서 닫힌다
# ─────────────────────────────────────────────────────────────


def online_plan(tmp_path: Path, **kwargs: Any) -> tuple[Any, FakeTiming, FakeText]:
    video = make_video(tmp_path)
    workflow = replace(RawWorkflowConfig(), network_mode="online")
    plan = plan_caption_run(
        video,
        run_id=kwargs.pop("run_id", "run-online"),
        settings=FinalCaptionSettings(chunk_max_seconds=15.0),
        workflow=workflow,
    )
    timing = kwargs.pop("timing", None) or FakeTiming(
        {
            0: [{"text": " 안녕", "start": 0.5, "end": 1.5}],
            1: [{"text": " 잘가", "start": 0.5, "end": 1.5}],
            2: [{"text": " 끝", "start": 0.5, "end": 1.5}],
        }
    )
    text = kwargs.pop("text", None) or FakeText({0: "안녕", 1: "잘가", 2: "끝"})
    return plan, timing, text


def test_online_route_requires_approval_and_passes_when_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine.env, "is_offline", lambda: False)
    plan, timing, text = online_plan(tmp_path)
    with pytest.raises(CaptionApprovalMissing):
        run_final_captions(
            plan,
            on=BEFORE_SHUTDOWN,
            timing_provider=timing,
            text_provider=text,
            workdir=tmp_path / "w",
            approval=None,
        )
    assert timing.calls == [] and text.calls == []  # 승인 없이는 호출이 없다
    result = run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=text,
        workdir=tmp_path / "w",
        approval=expected_approval(plan),
    )
    assert result.status == engine.STATUS_READY
    assert len(text.calls) == 3  # 승인 뒤에는 정상 호출이 있다


def test_forged_or_stale_approval_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(engine.env, "is_offline", lambda: False)
    plan, timing, text = online_plan(tmp_path)
    good = expected_approval(plan)
    forgeries = [
        replace(good, final_video_sha256="0" * 64),
        replace(good, audio_stream_index=9),
        replace(good, sample_end=good.sample_end + 1),
        replace(good, text_model="another-model"),
        replace(good, timing_model="another-model"),
        replace(good, settings_fingerprint="0" * 64),
        replace(good, budget_usd=Decimal("0.00001")),
    ]
    for forged in forgeries:
        with pytest.raises((CaptionApprovalMismatch, CaptionApprovalMissing)):
            run_final_captions(
                plan,
                on=BEFORE_SHUTDOWN,
                timing_provider=timing,
                text_provider=text,
                workdir=tmp_path / "w",
                approval=forged,
            )
    assert timing.calls == [] and text.calls == []  # 위조 승인으로는 호출이 없다


def test_offline_env_blocks_online_route(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine.env, "is_offline", lambda: True)
    plan, timing, text = online_plan(tmp_path)
    with pytest.raises(CaptionRunError, match="VLOGSRT_OFFLINE"):
        run_final_captions(
            plan,
            on=BEFORE_SHUTDOWN,
            timing_provider=timing,
            text_provider=text,
            workdir=tmp_path / "w",
            approval=expected_approval(plan),
        )
    assert timing.calls == [] and text.calls == []


def test_live_adapter_faces_the_gate_even_under_offline_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실전송 어댑터가 끼어 있으면 offline 설정이라도 승인·환경 문을 지난다(§14.3).

    설정 문자열 하나가 유료 호출을 승인해서는 안 된다 — 문은 첫 호출 앞에서 닫힌다.
    """
    from vlogsrt.project.raw.caption_provider import TransportTimingProvider

    class DeadSender:
        def send_request(self, *args: Any, **kwargs: Any) -> dict[str, Any]:  # pragma: no cover
            raise AssertionError("승인 없는 실전송 호출은 닫혀 있어야 한다")

    monkeypatch.setattr(engine.env, "is_offline", lambda: False)
    video = make_video(tmp_path)
    plan = plan_caption_run(
        video, run_id="run-live", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    assert plan.workflow.network_mode == "offline"  # 설정은 여전히 오프라인이다
    fake_text = FakeText({0: "안녕", 1: "잘가", 2: "끝"})
    with pytest.raises(CaptionApprovalMissing):
        run_final_captions(
            plan,
            on=BEFORE_SHUTDOWN,
            timing_provider=TransportTimingProvider(
                sender=DeadSender(),
                cfg=Config(),
            ),
            text_provider=fake_text,
            workdir=tmp_path / "w",
        )
    assert fake_text.calls == []


def test_shutdown_day_blocks_new_online_timing_zero_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """종료 예정일에 도달하면 신규 온라인 시간 작업은 차단된다(§11.10)."""
    monkeypatch.setattr(engine.env, "is_offline", lambda: False)
    plan, timing, text = online_plan(tmp_path)
    with pytest.raises(Exception, match="종료 예정일") as raised:
        run_final_captions(
            plan,
            on=SHUTDOWN_DAY,
            timing_provider=timing,
            text_provider=text,
            workdir=tmp_path / "w",
            approval=expected_approval(plan),
        )
    assert type(raised.value).__name__ == "TimingProviderUnavailable"
    assert timing.calls == [] and text.calls == []


def test_absent_provider_fails_before_any_call(tmp_path: Path) -> None:
    """제공자가 없으면 호출 한 번 없이 닫힌다 — 승인의 부재가 성공이 되는 길이 없다."""
    video = make_video(tmp_path)
    plan = plan_caption_run(
        video, run_id="run-none", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    text = FakeText({0: "안녕", 1: "잘가", 2: "끝"})
    with pytest.raises(CaptionProviderMissing):
        run_final_captions(plan, on=BEFORE_SHUTDOWN, timing_provider=None, text_provider=text)
    assert text.calls == []


def test_budget_reservation_gates_next_request() -> None:
    """예약 장부 — 다음 요청의 값이 승인 예산을 넘으면 보내지 않는다(§14.3)."""
    ledger = _Ledger(
        approval=CaptionApproval(
            final_video_sha256="a" * 64,
            audio_stream_index=1,
            sample_start=0,
            sample_end=1600000,
            text_model="t",
            timing_model="w",
            settings_fingerprint="f",
            budget_usd=Decimal("0.01"),
        )
    )
    ledger.charge(Fraction(60), engine.capabilities.ROLE_TEXT)
    with pytest.raises(CaptionBudgetExhausted):
        ledger.charge(Fraction(6000), engine.capabilities.ROLE_TIMING)


# ─────────────────────────────────────────────────────────────
#  취소 — 끝난 유효 결과는 보존한다(§14.1)
# ─────────────────────────────────────────────────────────────


def test_cancellation_preserves_finished_evidence(tmp_path: Path) -> None:
    """취소는 새 호출을 멈춘다 — 이미 받은 응답 증거는 결과에 남는다."""
    gates = {"count": 0}

    def cancellation() -> None:
        gates["count"] += 1
        if gates["count"] >= 2:
            raise CaptionRunCancelled("사용자 취소")

    result, timing, text = two_chunk_run(tmp_path, cancellation=cancellation)
    assert result.status == engine.STATUS_CANCELLED
    assert result.cancellation_reason == "사용자 취소"
    assert len(timing.calls) == 1  # 두번째 청크 문 앞에서 멈췄다
    assert text.calls == []  # 문구 경로는 시작도 못 했다
    assert len(result.evidence) == 1  # 받은 응답은 보존된다
    assert "생성되지 않았다" in result.chunks[1].timing_failure


# ─────────────────────────────────────────────────────────────
#  되풀이 재전사 — 엔진 repair를 어댑터로 얹는다
# ─────────────────────────────────────────────────────────────


def echo_segments(count: int, *, text: str = "안녕하세요 반갑습니다") -> list[dict[str, Any]]:
    return [
        {"text": f" {text}", "start": float(index * 2), "end": float(index * 2 + 2)}
        for index in range(count)
    ]


def test_repetition_is_replaced_by_rescue(tmp_path: Path) -> None:
    """되풀이 구간이 재전사로 갈아끼워진다 — 시각은 여전히 세그먼트에서 온다(D27)."""
    timing = FakeTiming(
        {0: [*echo_segments(4), {"text": " 마지막 장면", "start": 30.0, "end": 32.0}]},
        rescue=[{"text": " 안녕하세요 한 번만 말했습니다", "start": 0.2, "end": 3.0}],
    )
    text = FakeText({0: "안녕하세요 한 번만 말했습니다 마지막 장면"})
    result, fake_timing, fake_text = two_chunk_run(
        tmp_path, timing=timing, text=text, run_id="run-repair", single_chunk=True
    )
    assert result.repairs and result.repairs[0].outcome == REPAIR_REPLACED
    assert any("한 번만" in cue.text for cue in result.cues)
    assert any(":rescue:" in call for call in fake_timing.calls)
    assert not any(":arbiter:" in call for call in fake_text.calls)  # 첫 시도에 회복 — 심판 없다
    assert result.status == engine.STATUS_READY


def test_unrecovered_long_repetition_collapses_with_evidence(tmp_path: Path) -> None:
    """회복 못 한 긴 되풀이는 첫 장만 남고 — 교정 제외·검토 상태로 기록된다(D27·D28)."""
    timing = FakeTiming(
        {0: echo_segments(16)},
        rescue=echo_segments(16),
    )
    text = FakeText({0: "안녕하세요 반갑습니다"}, arbiter_text="")
    result, _, fake_text = two_chunk_run(
        tmp_path, timing=timing, text=text, run_id="run-collapse", single_chunk=True
    )
    assert result.repairs and result.repairs[0].outcome == REPAIR_COLLAPSED
    assert len([cue for cue in result.cues if cue.text.strip()]) == 1  # 첫 장만 섰다
    assert result.retext.excluded  # 접힌 자리는 교정하지 않는다
    assert any(":arbiter:" in call for call in fake_text.calls)  # 심판이 답했다(없다)
    assert result.status == engine.STATUS_REVIEW_REQUIRED
    assert any(item.code == "repair_excluded" for item in result.findings)


# ─────────────────────────────────────────────────────────────
#  커버리지·상태 — 꼬리까지, 그리고 다섯 상태의 구분
# ─────────────────────────────────────────────────────────────


def test_chunk_plan_and_records_cover_the_tail(tmp_path: Path) -> None:
    """마지막 청크는 남은 만큼 짧아도 요청·기록에 빠지지 않는다(§11.6)."""
    video = make_video(tmp_path, seconds=40.0)
    plan = plan_caption_run(
        video, run_id="run-cover", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    assert [(span.start, span.end) for span in plan.spans] == [
        (0, 240000),
        (240000, 480000),
        (480000, 640000),
    ]
    timing = FakeTiming(
        {
            0: [{"text": " 하나", "start": 0.5, "end": 1.5}],
            1: [{"text": " 둘", "start": 0.5, "end": 1.5}],
            2: [{"text": " 셋", "start": 0.5, "end": 1.5}],
        }
    )
    text = FakeText({0: "하나", 1: "둘", 2: "셋"})
    result = run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=text,
        workdir=tmp_path / "work",
    )
    assert len(result.chunks) == 3
    assert all(record.timing_ok and record.text_ok for record in result.chunks)
    assert result.status == engine.STATUS_READY


def test_confirmed_no_speech_is_distinct_from_failure(tmp_path: Path) -> None:
    """전부 정상 응답했고 전부 비어 있으면 confirmed_no_speech — 실패가 아니다(§11.11)."""
    timing = FakeTiming({0: [], 1: [], 2: []})
    text = FakeText({0: "", 1: "", 2: ""})
    result, _, _ = two_chunk_run(tmp_path, timing=timing, text=text)
    assert result.status == engine.STATUS_CONFIRMED_NO_SPEECH
    assert result.speech_basis == engine.BASIS_CONFIRMED_NO_SPEECH
    assert result.cues == ()
    assert all(record.timing_ok and record.text_ok for record in result.chunks)


def test_empty_result_with_failures_stays_unconfirmed(tmp_path: Path) -> None:
    """실패가 섞인 빈 결과는 무발화 확정이 아니다 — 미확인 빈 상태로 남는다(§11.11)."""
    timing = FakeTiming({0: [], 1: []}, fail={2})
    text = FakeText({0: "", 1: "", 2: ""})
    result, _, _ = two_chunk_run(tmp_path, timing=timing, text=text)
    assert result.status == engine.STATUS_PARTIAL_FAILURE
    assert result.cues == ()
    assert result.speech_basis == engine.BASIS_UNCONFIRMED_EMPTY


def test_out_of_final_range_is_clipped_and_reported(tmp_path: Path) -> None:
    """최종 영상 범위를 벗어난 큐는 경계로 자르고 보고한다 — 지어내지 않는다(§11.6)."""
    video = make_video(tmp_path, seconds=40.0, video_duration=4.0)  # 영상은 4초뿐
    timing = FakeTiming({0: [], 1: [], 2: [{"text": " 범위 밖 말", "start": 0.5, "end": 2.5}]})
    text = FakeText({0: "", 1: "", 2: "범위 밖 말"})
    plan = plan_caption_run(
        video, run_id="run-range", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    result = run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=text,
        workdir=tmp_path / "work",
    )
    clipped = result.cues[0]
    assert engine.CUE_CLIPPED_END in clipped.flags
    assert clipped.end == Fraction(4)
    assert clipped.text == "범위 밖 말"  # 글자는 사라지지 않는다(규칙 9)
    assert any(item.code == "out_of_final_range" for item in result.findings)
    assert result.status == engine.STATUS_REVIEW_REQUIRED


# ─────────────────────────────────────────────────────────────
#  리비전 격리 — 다른 렌더의 증거는 섞이지 않는다
# ─────────────────────────────────────────────────────────────


def test_different_revisions_never_share_responses(tmp_path: Path) -> None:
    """해시가 다른 최종본은 별개 실행이다 — 같은 게시 자리에 섞이지 않는다(§11.1)."""
    first_dir = tmp_path / "publish-1"
    second_dir = tmp_path / "publish-2"
    result1, _, text1 = two_chunk_run(tmp_path / "v1", run_id="run-v1")
    set1 = publish_caption_artifacts(first_dir, result1, _plan_of(tmp_path / "v1", "run-v1"))
    assert set1.srt.is_file()
    video2 = make_video(tmp_path / "v2", revision_body=b"re-rendered-final-video")
    timing = FakeTiming({0: [{"text": " 새 렌더", "start": 0.5, "end": 1.5}], 1: [], 2: []})
    text = FakeText({0: "새 렌더", 1: "", 2: ""})
    plan2 = plan_caption_run(
        video2, run_id="run-v2", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    result2 = run_final_captions(
        plan2,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=text,
        workdir=tmp_path / "v2" / "work",
    )
    assert result2.final_video_revision != result1.final_video_revision
    publish_caption_artifacts(second_dir, result2, plan2)
    with pytest.raises(ArtifactRevisionMismatch):
        publish_caption_artifacts(first_dir, result2, plan2)  # 옛 자리에 새 리비전 금지
    assert len(text1.calls) == 3  # 첫 실행의 응답은 재사용되지 않았다 — 매 실행이 새 호출이다


def _plan_of(tmp_path: Path, run_id: str) -> Any:
    video = make_video(tmp_path)
    return plan_caption_run(
        video, run_id=run_id, settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )


# ─────────────────────────────────────────────────────────────
#  실추출 전체 경로 — ffmpeg 합성 최종 영상으로 끝까지
# ─────────────────────────────────────────────────────────────


@pytest.fixture
def delayed_final(tmp_path: Path) -> Path:
    """오디오가 2초 늦게 시작하는 8초 합성 최종 영상 — 양수 원점을 잰다(§11.6)."""
    path = tmp_path / "delayed-final.mp4"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=8",
            "-f", "lavfi", "-itsoffset", "2", "-i", "sine=frequency=440:duration=4",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            str(path),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return path


@needs_ffmpeg
def test_full_run_on_real_synthetic_final_video(delayed_final: Path, tmp_path: Path) -> None:
    """등록→계획→실행→게시까지 실제 경계로 — 교정 시각이 잰 원점과 정확히 같다."""
    workdir = tmp_path / "work"
    video = register_final_video(delayed_final, workdir, source_id="final_real")
    origin = video.audio.pcm_sample0_file_time
    assert origin > 1  # 지연이 실측 원점에 들어 있다
    plan = plan_caption_run(video, run_id="run-real")
    timing = FakeTiming(
        {
            0: [
                {"text": f" {WRONG}", "start": 0.5, "end": 2.5},
                {"text": " 다음 장면", "start": 3.0, "end": 4.0},
            ]
        }
    )
    text = FakeText({0: f"{CORRECTED} 다음 장면"})
    result = run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=text,
        workdir=workdir / "run",
    )
    assert result.cues and result.cues[0].text == CORRECTED
    # 환산이 정확히 한 번: 큐 시작 = 원점 + 청크 0 + 세그먼트 0.5
    assert result.cues[0].start == origin + Fraction(1, 2)
    artifacts = publish_caption_artifacts(tmp_path / "out", result, plan)
    body = artifacts.srt.read_text(encoding="utf-8")
    assert CORRECTED in body
    from vlogsrt.output import parse_srt

    cues = parse_srt(body)
    assert len(cues) == 2
    assert cues[0].start == pytest.approx(float(origin) + 0.5, abs=0.002)
    assert cues[1].end == pytest.approx(float(origin) + 4.0, abs=0.002)
