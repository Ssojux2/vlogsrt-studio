"""W-15 버그 둘(D55) — 원본 경로 1차 전사의 온도와 청크 끝을 살짝 넘은 세그먼트.

1. 원본 경로의 1차 whisper 요청이 온도를 보내지 않아 API 기본 0으로 들렸다(D28 위반).
   분석 내부 전사와 최종 자막 1차 모두 `transcribe_temperature`(0.6)를 보낸다 — 캐시
   열쇠·승인 지문은 기본 설정에서 그대로다(재전사·재승인·유료 호출 0).
2. 세그먼트 하나가 청크 끝을 1초 이하로 넘거나 길이 0이면 청크 **전체**를 버렸다(규칙 9).
   관용 안의 넘침은 청크 끝으로 닫고(경고), 길이 0은 글자째 남긴다. 관용 밖만 거부한다.

네트워크는 없다 — 전송 경계는 요청을 기록하는 가짜다.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import log
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.analysis_approval import asr_settings_fingerprint
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.caption_provider import (
    MalformedCaptionResponse,
    TransportTimingProvider,
    audio_chunk_binding,
    parse_timing_response,
)
from vlogsrt.project.raw.config import TIMING_RESPONSE_TOLERANCE, RawWorkflowConfig
from vlogsrt.project.raw.final_caption import plan_caption_run, run_final_captions
from vlogsrt.project.raw.final_video import FinalVideoInput
from vlogsrt.project.raw.understanding import SampleSpan, SelectedAudio
from vlogsrt.transcribe import AudioTranscriptionRequest

BEFORE_SHUTDOWN = date(2026, 10, 2)
ENGINE_TEMPERATURE = Config().transcribe_temperature

# 기본 설정의 캐시 열쇠·승인 지문 — 고치기 전 코드(14ac197)에서 잰 값 그대로다. 온도를
# 보내게 고친 뒤에도 같아야 이미 받은 전사가 그대로 쓰인다(재전사·재승인 0).
ASR_CACHE_KEY_BEFORE_FIX = "9f3b6ade8829a8727ff2f21832f5fc7d5736d68590548dc22f5f0833bb853abc"
ASR_APPROVAL_FP_BEFORE_FIX = "06a193d29b7550982c69b44d88f9a6e5a93effd67978b19714ab03bc60dec41f"
CAPTION_FINGERPRINT_BEFORE_FIX = "5da889d54193d51618725e5b639e365a5f4898b453dfb65be9c3792dc234414e"


class RecordingSender:
    """전송 경계의 가짜 — 요청을 기록하고 준비된 시간 응답을 돌려준다."""

    def __init__(self, segments: list[dict[str, Any]] | None = None) -> None:
        self.requests: list[AudioTranscriptionRequest] = []
        self.segments = segments or [{"text": " 안녕", "start": 0.1, "end": 0.9}]

    def send_request(
        self,
        request: AudioTranscriptionRequest,
        cfg: Config,
        *,
        kind: str,
        stem: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        self.requests.append(request)
        return {"segments": self.segments}


def deterministic_audio(tmp_path: Path, seconds: int = 20) -> SelectedAudio:
    samples = seconds * stage1.PCM_SAMPLE_RATE
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


def chunk_of(tmp_path: Path, duration: float) -> tuple[Chunk, Any]:
    wav = tmp_path / "chunk.wav"
    wav.write_bytes(b"synthetic-wav")
    chunk = Chunk(path=wav, offset=0.0, duration=duration, index=0)
    binding = audio_chunk_binding(
        chunk,
        sample_start=0,
        sample_end=round(duration * 16000),
        sample_rate=16000,
        request_id="req",
    )
    return chunk, binding


def parse(tmp_path: Path, segments: list[dict[str, Any]], duration: float) -> Any:
    chunk, binding = chunk_of(tmp_path, duration)
    return parse_timing_response(
        {"segments": segments},
        binding=binding,
        model="whisper-1",
        provider="whisper_verbose_json",
        chunk_duration=chunk.duration,
    )


# ─────────────────────────────────────────────────────────────
#  버그 1 — 1차 전사가 온도를 보낸다
# ─────────────────────────────────────────────────────────────


def test_transport_first_pass_sends_engine_temperature(tmp_path: Path) -> None:
    """온도를 받지 않은 호출은 엔진의 1차 온도로 나간다 — API 기본 0이 아니다(D28)."""
    sender = RecordingSender()
    provider = TransportTimingProvider(sender=sender, cfg=Config())
    chunk, binding = chunk_of(tmp_path, 1.0)
    provider.timed_transcript(
        binding=binding, chunk=chunk, settings=RawWorkflowConfig(), on=BEFORE_SHUTDOWN
    )
    assert sender.requests[-1].temperature == ENGINE_TEMPERATURE == 0.6
    provider.timed_transcript(
        binding=binding,
        chunk=chunk,
        settings=RawWorkflowConfig(),
        on=BEFORE_SHUTDOWN,
        temperature=0.8,
    )
    assert sender.requests[-1].temperature == 0.8  # 재전사의 뜨거운 온도는 그대로다


def test_analysis_asr_first_pass_sends_temperature(tmp_path: Path) -> None:
    """분석 내부 전사의 청크마다 0.6이 실린다 — 원본 경로의 1차 전사도 D28을 지킨다."""
    sender = RecordingSender()
    audio = deterministic_audio(tmp_path)
    stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000), SampleSpan(160000, 320000)),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=sender, cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
    )
    assert [request.temperature for request in sender.requests] == [ENGINE_TEMPERATURE] * 2


def test_asr_cache_key_and_approval_fingerprint_unchanged(tmp_path: Path) -> None:
    """온도를 보내도 내부 전사 캐시 열쇠·1단계 승인 지문은 기본 설정에서 그대로다."""
    audio = deterministic_audio(tmp_path)
    cache_dir = tmp_path / "cache"
    stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000),),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=RecordingSender(), cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        cache_dir=cache_dir,
    )
    assert [path.stem for path in cache_dir.glob("*.json")] == [ASR_CACHE_KEY_BEFORE_FIX]
    fingerprint = asr_settings_fingerprint(RawUnderstandingSettings(), RawWorkflowConfig())
    assert fingerprint == ASR_APPROVAL_FP_BEFORE_FIX


def final_video(tmp_path: Path) -> FinalVideoInput:
    samples = 40 * stage1.PCM_SAMPLE_RATE
    pcm = tmp_path / "caption.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * 2)))
    source = tmp_path / "final.mp4"
    source.write_bytes(b"synthetic-final-video")
    digest = hashlib.sha256(b"synthetic-final-video").hexdigest()
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
            sample_rate=stage1.PCM_SAMPLE_RATE,
            channels=1,
            sample_count=samples,
            pcm_sample0_file_time=Fraction(-1, 50),
        ),
        video_duration=40.5,
        audio_stream_count=1,
    )


class TemperatureTiming:
    """온도 손잡이가 있는 시간 제공자 가짜 — 요청 ID마다 받은 온도를 남긴다."""

    def __init__(self) -> None:
        self.temperatures: dict[str, float | None] = {}

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
        self.temperatures[binding.request_id] = temperature
        return parse_timing_response(
            {"segments": [{"text": " 안녕하세요", "start": 0.5, "end": 2.0}]},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


class SilentText:
    def text_transcript(self, *, binding: Any, **_: Any) -> Any:
        from vlogsrt.project.raw.caption_provider import parse_text_response

        return parse_text_response({"text": "안녕하세요"}, binding=binding, model="m")


def test_final_caption_first_pass_sends_approved_temperature(tmp_path: Path) -> None:
    """최종 자막 1차 전사는 승인 지문이 얼린 엔진 온도를 그대로 보낸다."""
    video = final_video(tmp_path)
    plan = plan_caption_run(
        video, run_id="run-t", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    timing = TemperatureTiming()
    run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=timing,
        text_provider=SilentText(),
        workdir=tmp_path / "work",
    )
    first_pass = {key: value for key, value in timing.temperatures.items() if ":timing:" in key}
    assert len(first_pass) == len(plan.spans) == 3
    assert set(first_pass.values()) == {plan.engine.transcribe_temperature}


def test_caption_fingerprint_unchanged_for_default_config(tmp_path: Path) -> None:
    """최종 자막 승인 지문도 그대로 — 지문은 이미 0.6을 기록하고 있었다(보내지만 않았다)."""
    plan = plan_caption_run(final_video(tmp_path), run_id="run-f")
    assert plan.fingerprint == CAPTION_FINGERPRINT_BEFORE_FIX


# ─────────────────────────────────────────────────────────────
#  버그 3 — 청크 끝 넘침·길이 0이 청크 전체를 버리지 않는다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("start", "end", "duration"),
    [
        (176.2, 181.0, 180.0),  # 0024 · 720–900초 조각 — 1.0초 넘침
        (36.4, 39.0, 38.932),  # 0008 · 꼬리 조각 — 0.068초 넘침
        (178.5, 180.06, 180.0),  # 0025 · 0–180초 조각 — 0.06초 넘침
        (180.02, 180.06, 180.0),  # 시작까지 넘친 모양 — 둘 다 청크 끝으로
    ],
)
def test_overrun_within_tolerance_is_closed_at_chunk_end(
    tmp_path: Path, start: float, end: float, duration: float, monkeypatch: pytest.MonkeyPatch
) -> None:
    """관용 안의 넘침은 청크 끝으로 닫는다 — 다른 세그먼트와 글자는 그대로 남는다(경고)."""
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    timed = parse(
        tmp_path,
        [
            {"text": " 앞 문장", "start": 1.0, "end": 3.0},
            {"text": " 넘친 문장", "start": start, "end": end},
        ],
        duration,
    )
    assert [segment.text for segment in timed.segments] == [" 앞 문장", " 넘친 문장"]
    clamped = timed.segments[1]
    assert clamped.end == duration
    assert clamped.start == min(start, duration)
    assert timed.segments[0].start == 1.0 and timed.segments[0].end == 3.0  # 다른 시각은 그대로
    assert timed.clamped_segments == (1,)
    assert timed.response is not None
    assert timed.response["segments"][1]["end"] == end  # 원 응답은 증거로 그대로
    assert len(warnings) == 1 and "청크 끝" in warnings[0]


def test_zero_length_segment_keeps_its_text(tmp_path: Path) -> None:
    """0019 · 28.44=28.44 — 길이 0 세그먼트도 글자째 남는다(규칙 9). 청크를 버리지 않는다."""
    timed = parse(
        tmp_path,
        [
            {"text": " 먼저", "start": 20.0, "end": 24.0},
            {"text": " 길이 영", "start": 28.44, "end": 28.44},
        ],
        35.5,
    )
    assert [(seg.start, seg.end, seg.text) for seg in timed.segments] == [
        (20.0, 24.0, " 먼저"),
        (28.44, 28.44, " 길이 영"),
    ]
    assert timed.clamped_segments == ()


def test_overrun_beyond_tolerance_still_rejects(tmp_path: Path) -> None:
    """관용 밖의 넘침은 여전히 소유하지 않은 시각이다 — 지어내지 않고 실패한다."""
    beyond = 180.0 + TIMING_RESPONSE_TOLERANCE.chunk_end_overrun_seconds + 0.5
    with pytest.raises(MalformedCaptionResponse):
        parse(tmp_path, [{"text": " 너무 멀다", "start": 178.0, "end": beyond}], 180.0)
    with pytest.raises(MalformedCaptionResponse):
        parse(tmp_path, [{"text": " 뒤집힘", "start": 4.0, "end": 3.0}], 180.0)


def test_clamped_segment_lands_at_chunk_end_sample(tmp_path: Path) -> None:
    """닫힌 세그먼트는 발화 증거에서 정확히 청크 끝 표본에 선다 — 이웃 청크로 새지 않는다."""
    audio = deterministic_audio(tmp_path, seconds=20)
    sender = RecordingSender([{"text": " 끝 문장", "start": 9.0, "end": 10.5}])
    evidence = stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000), SampleSpan(160000, 320000)),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=sender, cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
    )
    assert all(chunk.succeeded for chunk in evidence.chunks)
    utterances = stage1.utterance_evidence(audio, evidence)
    assert [u.end_sample for u in utterances] == [160000, 320000]
    assert all(u.text == " 끝 문장" for u in utterances)


def test_tolerance_lives_outside_the_workflow_config() -> None:
    """관용은 RawWorkflowConfig 밖이다 — 내부 전사 캐시 열쇠가 그 사전을 해시한다."""
    keys = json.dumps(sorted(RawWorkflowConfig.__dataclass_fields__))
    assert "overrun" not in keys and "tolerance" not in keys
    assert TIMING_RESPONSE_TOLERANCE.chunk_end_overrun_seconds > 1.0  # 실측 최대 1.0초 + 여유


# ── 검증자 재검(D55) — 맞춘 줄은 기록으로 남고, 각 경로는 실제로 한 일을 말한다 ──────


def test_clamped_positions_survive_into_the_stage_one_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """분석 경로: 청크 끝에 맞춘 세그먼트 위치가 1단계 기록에 남는다 — 없으면 칸도 없다."""
    from vlogsrt.project.raw.analysis_workflow import _asr_from_artifact

    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    audio = deterministic_audio(tmp_path, seconds=20)
    sender = RecordingSender([{"text": " 끝 문장", "start": 9.0, "end": 10.5}])
    evidence = stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000),),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=sender, cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
    )
    assert evidence.chunks[0].clamped_segments == (0,)
    written = stage1.asr_evidence_json(evidence)
    assert written["chunks"][0]["clamped_segments"] == [0]
    assert _asr_from_artifact({"asr": written}).chunks[0].clamped_segments == (0,)
    assert any("1줄을 청크 끝에 맞췄다" in line for line in warnings), warnings
    plain = RecordingSender([{"text": " 안쪽", "start": 1.0, "end": 2.0}])
    (tmp_path / "calm").mkdir()
    calm = stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000),),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=plain, cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-2",
        workdir=tmp_path / "calm",  # 같은 입력의 내부 전사 캐시를 피한다
    )
    assert "clamped_segments" not in stage1.asr_evidence_json(calm)["chunks"][0]


def test_a_rejected_chunk_is_a_warning_not_a_debug_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """뒤집힌 세그먼트 하나가 청크를 버리면 그 구간 자막이 사라진다 — 경고로 알린다."""
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    audio = deterministic_audio(tmp_path, seconds=20)
    sender = RecordingSender([{"text": " 뒤집힘", "start": 4.0, "end": 3.0}])
    evidence = stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000),),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=sender, cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
    )
    assert not evidence.chunks[0].succeeded
    assert any("청크 0" in line and "실패" in line for line in warnings), warnings


class OverrunTiming(TemperatureTiming):
    """첫 청크의 마지막 세그먼트가 청크 끝(15초)을 1초 넘는 시간 제공자 가짜."""

    def timed_transcript(self, *, binding: Any, chunk: Any, settings: Any, **kwargs: Any) -> Any:
        self.temperatures[binding.request_id] = kwargs.get("temperature")
        segments = [{"text": " 안녕하세요", "start": 0.5, "end": 2.0}]
        if chunk.index == 0 and ":timing:" in binding.request_id:
            segments.append({"text": " 넘친 말", "start": 13.0, "end": 16.0})
            segments.append({"text": " 길이 영", "start": 14.0, "end": 14.0})
        return parse_timing_response(
            {"segments": segments},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


def test_final_captions_record_that_whisper_times_were_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """최종 자막: 청크를 버리지 않고 whisper 시각을 그대로 쓴다 — 그 사실을 검토 항목으로 남긴다.

    길이 0 큐의 읽기 속도는 `inf자/초`로 나가지 않는다.
    """
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    video = final_video(tmp_path)
    plan = plan_caption_run(
        video, run_id="run-o", settings=FinalCaptionSettings(chunk_max_seconds=15.0)
    )
    result = run_final_captions(
        plan,
        on=BEFORE_SHUTDOWN,
        timing_provider=OverrunTiming(),
        text_provider=SilentText(),
        workdir=tmp_path / "work",
    )
    assert result.chunks[0].timing_ok
    kept = [item for item in result.findings if item.code == "timing_overrun_kept"]
    assert len(kept) == 1 and "whisper 시각 그대로" in kept[0].message
    assert any(cue.text.strip() == "넘친 말" for cue in result.cues)
    assert all("inf" not in item.message for item in result.findings)
    assert any("원 응답은 그대로" in line for line in warnings), warnings


def test_asr_cache_entry_without_overrun_keeps_the_pre_fix_bytes(tmp_path: Path) -> None:
    """넘침이 없는 캐시 항목은 W-15 이전 모양 그대로다 — 옛 빌드가 읽어도 놓침·재전송이 없다."""
    audio = deterministic_audio(tmp_path)
    cache_dir = tmp_path / "cache"
    stage1.transcribe_selected_audio(
        audio,
        (SampleSpan(0, 160000),),
        settings=RawWorkflowConfig(),
        understanding=RawUnderstandingSettings(),
        provider=TransportTimingProvider(sender=RecordingSender(), cfg=Config()),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        cache_dir=cache_dir,
    )
    (entry,) = cache_dir.glob("*.json")
    wav = hashlib.sha256((tmp_path / "asr-chunk-0000.wav").read_bytes()).hexdigest()
    before_fix = {  # 14ac197의 asdict(AsrChunkOutcome) — 필드 일곱, 이 순서
        "chunk_index": 0,
        "sample_start": 0,
        "sample_end": 160000,
        "wav_sha256": wav,
        "request_id": "run-1:chunk-0000",
        "transcript": [[0.1, 0.9, " 안녕"]],
        "failure": "",
    }
    expected = json.dumps({"fingerprint": entry.stem, "outcome": before_fix}, ensure_ascii=False)
    assert entry.read_text(encoding="utf-8") == expected
