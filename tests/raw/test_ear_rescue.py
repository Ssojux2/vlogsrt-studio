"""W-29b — 「다른 귀 기준 재전사」: GPT-Transcribe와 어긋난 청크만 whisper에 다시 묻는다(D78).

실데이터에서 whisper가 소음 위에 「시청해주셔서 감사합니다!」 한 줄을 24초 깔았고, 같은 WAV를 들은
다른 귀는 실제 말을 받아 적었지만 D37 조각 게이트가 거절했다. 유료 호출은 없다(가짜 제공자). 시험이
못 박는 것:

- 판정(순수, `ear_rescue`): 다른 귀 글자가 있고 whisper 청크가 조각 게이트를 못 지나면(또는
  whisper가 그 청크에서 아무것도 못 들었으면) 다시 묻는다. 다른 귀가 아무것도 못 들은 청크는 묻지
  않는다(「말이 아니다」 판정 금지).
- 다시 묻기: 엔진 `repair_*` 온도(0.8 → 1.0) 차례로, 다른 귀와 맞는 첫 답을 채택한다 — 시각은
  whisper 답 그대로, 1차 세그먼트와 시도는 기록에 남는다. 모두 어긋나면 1차 그대로(비우지 않는다).
  맞는 청크는 한 번도 묻지 않는다.
- 온도 손잡이가 없는 제공자(재생)는 묻지 않는다(D28 — 조용히 기본 온도로 회복을 묻지 않는다).
- 캐시: 같은 시도는 다시 사지 않는다. 기록은 다시 세워도 같은 JSON이다.
- 돈 문: 교차 확인이 켜진 견적만 「다시 묻기」 최대 횟수·금액을 싣고 digest에 접는다. 승인의 예산을
  넘으면 보내지 않는다(그 청크는 1차 그대로).
"""

from __future__ import annotations

import hashlib
import inspect
from datetime import date
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.project.raw import analysis_workflow, browser_runtime, cross_check, ear_rescue
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import parse_timing_response
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.models import AnalysisEstimate
from vlogsrt.project.raw.understanding import SampleSpan, SelectedAudio

BEFORE_SHUTDOWN = date(2026, 10, 2)
SPANS = (SampleSpan(0, 160000), SampleSpan(160000, 320000))
GPT_TEXT = "오늘은 시장에 가서 떡볶이를 먹었어요"
HALLUCINATION = [{"start": 0.0, "end": 10.0, "text": "시청해주셔서 감사합니다!"}]
HEARD = [
    {"start": 0.5, "end": 3.0, "text": "오늘은 시장에 가서"},
    {"start": 3.2, "end": 6.0, "text": "떡볶이를 먹었어요"},
]
AGREEING = [{"start": 1.0, "end": 4.0, "text": "바다가 정말 예쁘다"}]


def audio_of(tmp_path: Path) -> SelectedAudio:
    samples = 20 * stage1.PCM_SAMPLE_RATE
    pcm = tmp_path / "analysis.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * 2)))
    return SelectedAudio(
        source_id="source_test",
        source_path=tmp_path / "original.mp4",
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=pcm,
        pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
        sample_rate=stage1.PCM_SAMPLE_RATE,
        channels=1,
        sample_count=samples,
        pcm_sample0_file_time=Fraction(0),
    )


class Tempered:
    """온도 손잡이가 있는 시간 제공자 가짜 — (청크, 온도)마다 준비된 세그먼트."""

    def __init__(self, replies: dict[tuple[int, float | None], list[dict[str, Any]]]) -> None:
        self.replies = replies
        self.seen: list[tuple[int, float | None, str]] = []

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
        self.seen.append((chunk.index, temperature, binding.request_id))
        segments = self.replies.get((chunk.index, temperature), self.replies[(chunk.index, None)])
        return parse_timing_response(
            {"segments": segments},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


class Untempered(Tempered):
    """재생처럼 온도 손잡이가 없는 제공자."""

    def timed_transcript(  # type: ignore[override]
        self, *, binding: Any, chunk: Any, settings: RawWorkflowConfig, on: date, **kw: Any
    ) -> Any:
        return super().timed_transcript(binding=binding, chunk=chunk, settings=settings, on=on)


def first_pass(tmp_path: Path, provider: Any, cache: Path | None = None) -> stage1.AsrEvidence:
    return stage1.transcribe_selected_audio(
        audio_of(tmp_path),
        SPANS,
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=provider,
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        cache_dir=cache,
    )


def rescue(
    tmp_path: Path,
    provider: Any,
    evidence: stage1.AsrEvidence,
    texts: dict[int, str],
    cache: Path | None = None,
) -> stage1.AsrEvidence:
    return stage1.rescue_disagreeing_chunks(
        audio_of(tmp_path),
        evidence,
        texts,
        settings=RawWorkflowConfig(),
        provider=provider,
        on=BEFORE_SHUTDOWN,
        workdir=tmp_path,
        cache_dir=cache,
    )


def local(segments: list[dict[str, Any]]) -> tuple[tuple[float, float, str], ...]:
    return tuple((s["start"], s["end"], s["text"]) for s in segments)


# ── 판정(순수) ────────────────────────────────────────────────────────────────


def test_the_temperatures_are_the_engine_repair_ladder() -> None:
    cfg = Config()
    assert ear_rescue.temperatures(cfg) == (0.8, 1.0)
    assert len(ear_rescue.temperatures(cfg)) == cfg.repair_max_attempts


def test_a_hallucinated_chunk_needs_rescue_and_a_matching_one_does_not() -> None:
    cfg = Config()
    assert ear_rescue.needs_rescue(local(HALLUCINATION), 10.0, GPT_TEXT, cfg)
    assert not ear_rescue.needs_rescue(local(HEARD), 10.0, GPT_TEXT, cfg)
    trusted, similarity = ear_rescue.agreement(local(HEARD), 10.0, GPT_TEXT, cfg)
    assert trusted and similarity >= cfg.retext_chunk_similarity_floor


def test_whisper_hearing_nothing_while_the_other_ear_heard_speech_needs_rescue() -> None:
    assert ear_rescue.needs_rescue((), 10.0, GPT_TEXT, Config())


@pytest.mark.parametrize("gpt", ["", "   ", "…"])
def test_the_other_ear_hearing_nothing_never_asks_again(gpt: str) -> None:
    assert not ear_rescue.needs_rescue(local(HALLUCINATION), 10.0, gpt, Config())


# ── 다시 묻기 ────────────────────────────────────────────────────────────────


def test_a_hallucinated_chunk_is_replaced_by_the_first_agreeing_answer(tmp_path: Path) -> None:
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    evidence = first_pass(tmp_path, provider)
    rescued = rescue(tmp_path, provider, evidence, {0: GPT_TEXT, 1: "바다가 정말 예쁘다"})
    first, second = rescued.chunks
    assert first.transcript == local(HEARD)  # 시각은 whisper 다시 묻기 답 그대로
    assert first.first_transcript == local(HALLUCINATION)
    (attempt,) = first.rescue_attempts
    assert attempt[0] == 0.8 and attempt[2] is True
    assert first.request_id == "run-1:chunk-0000:rescue-0.8"
    assert second == evidence.chunks[1]  # 맞는 청크는 그대로
    asked = [(index, temperature) for index, temperature, _ in provider.seen]
    assert asked == [(0, None), (1, None), (0, 0.8)]  # 맞는 청크는 한 번도 다시 묻지 않는다


def test_when_every_attempt_disagrees_the_first_answer_stays(tmp_path: Path) -> None:
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING})
    evidence = first_pass(tmp_path, provider)
    rescued = rescue(tmp_path, provider, evidence, {0: GPT_TEXT})
    first = rescued.chunks[0]
    assert first.transcript == local(HALLUCINATION) and first.first_transcript == ()
    assert [(t, ok) for t, _sim, ok in first.rescue_attempts] == [(0.8, False), (1.0, False)]
    assert first.request_id == "run-1:chunk-0000"


def test_a_chunk_the_other_ear_did_not_hear_is_never_asked_again(tmp_path: Path) -> None:
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING})
    evidence = first_pass(tmp_path, provider)
    assert rescue(tmp_path, provider, evidence, {0: ""}) == evidence
    assert [t for _i, t, _r in provider.seen] == [None, None]


def test_a_provider_without_a_temperature_knob_is_never_asked_again(tmp_path: Path) -> None:
    provider = Untempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    assert "temperature" not in inspect.signature(provider.timed_transcript).parameters
    evidence = first_pass(tmp_path, provider)
    assert rescue(tmp_path, provider, evidence, {0: GPT_TEXT}) == evidence
    assert len(provider.seen) == 2


def test_a_failed_attempt_keeps_the_first_answer_and_caches_nothing(tmp_path: Path) -> None:
    class Failing(Tempered):
        def timed_transcript(  # type: ignore[override]
            self, *, temperature: float | None = None, **kw: Any
        ) -> Any:
            if temperature is not None:
                self.seen.append((kw["chunk"].index, temperature, "refused"))
                raise RuntimeError("닫힘")
            return super().timed_transcript(**kw)

    cache = tmp_path / "cache"
    provider = Failing({(0, None): HALLUCINATION, (1, None): AGREEING})
    evidence = first_pass(tmp_path, provider, cache)
    before = {path.name for path in cache.glob("*.json")}
    rescued = rescue(tmp_path, provider, evidence, {0: GPT_TEXT}, cache)
    assert (0, 0.8, "refused") in provider.seen  # 실제로 물었다
    assert rescued.chunks[0].transcript == local(HALLUCINATION)
    assert rescued.chunks[0].rescue_attempts == ()  # 받은 답이 없으면 시도도 없다
    assert {path.name for path in cache.glob("*.json")} == before  # 실패는 캐시하지 않는다


def test_a_rescued_chunk_does_not_revert_when_the_other_ear_is_missing_later(
    tmp_path: Path,
) -> None:
    cache = tmp_path / "cache"
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    once = rescue(tmp_path, provider, first_pass(tmp_path, provider, cache), {0: GPT_TEXT}, cache)
    later = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING})
    again = rescue(tmp_path, later, first_pass(tmp_path, later, cache), {}, cache)  # 답이 없다
    assert again.chunks[0].transcript == local(HEARD)
    assert again.chunks[0].first_transcript == local(HALLUCINATION)
    assert [t for _i, t, _r in later.seen] == []  # 새로 묻지 않았다
    assert once.chunks[0].transcript == again.chunks[0].transcript


def test_without_a_rescue_budget_nothing_new_is_asked_but_accepted_answers_stay(
    tmp_path: Path,
) -> None:
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    evidence = first_pass(tmp_path, provider)
    held = stage1.rescue_disagreeing_chunks(
        audio_of(tmp_path),
        evidence,
        {0: GPT_TEXT},
        settings=RawWorkflowConfig(),
        provider=provider,
        on=BEFORE_SHUTDOWN,
        workdir=tmp_path,
        may_ask=False,
    )
    assert held == evidence
    assert [t for _i, t, _r in provider.seen] == [None, None]


def test_the_other_ears_text_lands_on_the_rescued_segments(tmp_path: Path) -> None:
    from vlogsrt.project.raw import source_text
    from vlogsrt.project.raw.config import SOURCE_TEXT_SETTINGS

    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    audio = audio_of(tmp_path)
    rescued = rescue(tmp_path, provider, first_pass(tmp_path, provider), {0: GPT_TEXT})
    chunk = rescued.chunks[0]
    outcome = source_text.TextChunkOutcome(
        chunk_index=0,
        sample_start=chunk.sample_start,
        sample_end=chunk.sample_end,
        wav_sha256=chunk.wav_sha256,
        key="k",
        status=source_text.STATUS_RECEIVED,
        text="오늘은 시장에 가서 떡볶이를 먹었어요.",
    )
    request = source_text.TextPassRequest(
        settings=SOURCE_TEXT_SETTINGS,
        glossary=(),
        workflow=RawWorkflowConfig(),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        cache_dir=tmp_path / "text",
        origin=source_text.ORIGIN_LIVE,
    )
    utterances = stage1.utterance_evidence(audio, rescued)
    result = source_text.apply_text_pass(utterances, [outcome], request, audio.sample_rate)
    first_chunk = [u for u in result.utterances if u.chunk_index == 0]
    assert [u.start for u in first_chunk] == [u.start for u in utterances if u.chunk_index == 0]
    assert "".join(u.text for u in first_chunk).replace(" ", "").startswith("오늘은시장에가서")
    assert all("감사합니다" not in u.text for u in first_chunk)


def test_the_approved_provider_routes_reasks_through_the_rescue_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from dataclasses import replace as dc_replace

    from vlogsrt.project.raw.analysis_approval import AnalysisApprovalError, AsrPhaseApproval

    monkeypatch.setattr(analysis_workflow, "require_live_asr_approval", lambda *a, **k: None)
    inner = Tempered({(0, None): HALLUCINATION, (0, 0.8): HEARD})
    approval = AsrPhaseApproval(
        approved=True,
        source_sha256="a" * 64,
        audio_stream_index=1,
        chunk_budget=1,
        settings_fingerprint="f",
        timing_model="whisper-1",
        approved_on="2026-10-02",
    )
    settings = dc_replace(RawWorkflowConfig(), network_mode="online")
    audio = audio_of(tmp_path)

    def build(budget: int) -> Any:
        return analysis_workflow._approved_phase_provider(
            inner,
            settings,
            RawUnderstandingSettings(),
            audio,
            dc_replace(approval, rescue_chunk_budget=budget),
        )

    assert not analysis_workflow._may_rescue(build(0))
    assert analysis_workflow._may_rescue(build(2))
    evidence = first_pass(tmp_path, inner)
    once = stage1.rescue_disagreeing_chunks(
        audio,
        evidence,
        {0: GPT_TEXT},
        settings=settings,
        provider=build(1),
        on=BEFORE_SHUTDOWN,
        workdir=tmp_path,
    )
    assert once.chunks[0].transcript == local(HEARD)
    refused = build(0)
    with pytest.raises(AnalysisApprovalError):
        refused.timed_transcript(
            binding=type("B", (), {"request_id": "r", "sample_start": 0, "sample_end": 1})(),
            chunk=None,
            settings=settings,
            on=BEFORE_SHUTDOWN,
            temperature=0.8,
        )


def test_rescue_answers_are_cached_and_never_bought_twice(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    evidence = first_pass(tmp_path, provider, cache)
    once = rescue(tmp_path, provider, evidence, {0: GPT_TEXT}, cache)
    again_provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING})
    again = rescue(
        tmp_path, again_provider, first_pass(tmp_path, again_provider, cache), {0: GPT_TEXT}, cache
    )
    assert stage1.asr_evidence_json(again) == stage1.asr_evidence_json(once)
    assert [t for _i, t, _r in again_provider.seen] == []  # 1차도 다시 묻기도 캐시에서


def test_the_record_carries_the_rescue_and_rebuilds(tmp_path: Path) -> None:
    provider = Tempered({(0, None): HALLUCINATION, (1, None): AGREEING, (0, 0.8): HEARD})
    rescued = rescue(tmp_path, provider, first_pass(tmp_path, provider), {0: GPT_TEXT})
    body = stage1.asr_evidence_json(rescued)
    first, second = body["chunks"]
    assert first["rescue"]["first_segments"] == HALLUCINATION
    assert [a["temperature"] for a in first["rescue"]["attempts"]] == [0.8]
    assert "rescue" not in second
    rebuilt = analysis_workflow._asr_from_artifact({"asr": body})
    assert stage1.asr_evidence_json(rebuilt) == body


# ── 돈 문 ───────────────────────────────────────────────────────────────────


def estimate(**changes: Any) -> AnalysisEstimate:
    base: dict[str, Any] = dict(  # noqa: C408 — 칸 이름 그대로 읽히게
        asset_id="src",
        source_order=1,
        source_name="a.mov",
        has_audio=True,
        audio_stream_index=1,
        sample_count=400 * 16000,
        video_frame_count=12000,
        extent_seconds="400",
        window_count=27,
        frame_sample_count=216,
        asr_chunk_count=3,
        asr_seconds="400",
        luna_request_count=27,
        price_status="snapshot",
        user_ceiling_total=None,
        note="",
    )
    base.update(changes)
    return AnalysisEstimate(**base)


def test_the_quote_carries_the_rescue_ceiling_only_with_the_text_pass() -> None:
    quoted = cross_check.with_rescue(estimate())
    assert quoted.rescue_chunk_count == 6  # 청크 3 × 시도 2
    assert Decimal(str(quoted.rescue_usd)) == Decimal("0.08")  # 800초 × whisper 분당 $0.006
    plain = estimate()
    assert plain.rescue_chunk_count is None and plain.rescue_usd is None
    assert browser_runtime.asr_estimate_digest([plain]) != browser_runtime.asr_estimate_digest(
        [quoted]
    )
    assert cross_check.with_rescue(estimate(has_audio=False)).rescue_chunk_count is None


def test_switching_the_cross_check_off_clears_the_rescue_ceiling() -> None:
    cleared = cross_check._without_text(cross_check.with_rescue(estimate()))
    assert cleared.rescue_chunk_count is None and cleared.rescue_usd is None


def test_the_approval_binds_the_quoted_rescue_budget() -> None:
    fields = browser_runtime._rescue_approval_fields(cross_check.with_rescue(estimate()))
    assert fields == {"rescue_chunk_budget": 6}
    assert browser_runtime._rescue_approval_fields(estimate()) == {}
