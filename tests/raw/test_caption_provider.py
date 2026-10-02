"""최종 자막 역할별 어댑터(A7-1) — 문구와 시간이 타입으로 갈라져 있는지 본다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.4·§13.1. 네트워크 없이
가짜 전송 경계(요청 기록·준비된 응답)만으로 돈다 — 실제 호출은 살지 않는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields, replace
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.capabilities import (
    TimingProviderUnavailable,
    UnverifiedModelCapability,
)
from vlogsrt.project.raw.caption_provider import (
    AudioChunkBinding,
    TextTranscript,
    audio_chunk_binding,
    parse_text_response,
    parse_timing_response,
    text_transcription_request,
    timing_transcription_request,
)
from vlogsrt.project.raw.config import (
    RawCaptionContract,
    RawModelRoles,
    RawWorkflowConfig,
)
from vlogsrt.transcribe import (
    CAPTION_TEXT_KIND,
    CAPTION_TIMING_KIND,
    AudioTranscriptionRequest,
)

BEFORE_SHUTDOWN = date(2026, 10, 2)
SHUTDOWN_DAY = date(2027, 2, 26)
TEXT_PAYLOAD: dict[str, Any] = {"text": "망원동 카페", "language": "ko"}
TIMING_PAYLOAD: dict[str, Any] = {
    "segments": [
        {"text": " 망원동", "start": 0.0, "end": 1.5},
        {"text": " 카페", "start": 2.0, "end": 3.5},
    ]
}


class RecordingSender:
    """전송 경계의 가짜 구현 — (작업 종류, 요청)을 기록하고 준비된 응답을 돌려준다."""

    def __init__(self, *, text: dict[str, Any], timing: dict[str, Any]) -> None:
        self.sent: list[tuple[str, AudioTranscriptionRequest]] = []
        self.replies = {
            CAPTION_TEXT_KIND: text,
            CAPTION_TIMING_KIND: timing,
        }

    def send_request(
        self,
        request: AudioTranscriptionRequest,
        cfg: Config,
        *,
        kind: str,
        stem: str = "",
        source: str = "",
    ) -> dict[str, Any]:
        self.sent.append((kind, request))
        return self.replies[kind]


def chunk(tmp_path: Path) -> Chunk:
    audio = tmp_path / "final.mp3"
    audio.write_bytes(b"synthetic-audio-bytes")
    return Chunk(audio, 120.0, 12.0, 3)


def binding_of(target: Chunk) -> AudioChunkBinding:
    return audio_chunk_binding(
        target,
        sample_start=480000,
        sample_end=528000,
        sample_rate=4000,
        request_id="run-1",
    )


def providers(sender: RecordingSender) -> tuple[Any, Any]:
    from vlogsrt.project.raw.caption_provider import (
        TransportTextProvider,
        TransportTimingProvider,
    )

    return (
        TransportTimingProvider(sender=sender, cfg=Config()),
        TransportTextProvider(sender=sender, cfg=Config()),
    )


# ─────────────────────────────────────────────────────────────
#  청크 신원 — 두 경로가 같은 오디오를 들었다는 증명
# ─────────────────────────────────────────────────────────────


def test_binding_hashes_the_actual_chunk_bytes(tmp_path: Path) -> None:
    """해시는 파일 내용에서 온다 — 청크가 바뀌면 신원도 바뀐다."""
    target = chunk(tmp_path)
    binding = binding_of(target)
    assert binding.audio_sha256 == hashlib.sha256(b"synthetic-audio-bytes").hexdigest()
    (tmp_path / "final.mp3").write_bytes(b"different-bytes")
    assert (
        audio_chunk_binding(
            target, sample_start=0, sample_end=10, sample_rate=1, request_id="run-2"
        ).audio_sha256
        != binding.audio_sha256
    )


@pytest.mark.parametrize(
    ("sample_start", "sample_end", "sample_rate", "request_id"),
    [
        (10, 10, 4000, "r"),
        (-1, 10, 4000, "r"),
        (0, 10, 0, "r"),
        (0, 10, 4000, "  "),
        # 좌표는 정확한 정수만 — bool·float은 애노테이션을 넘어 새어 들어온다(루트 리뷰)
        (True, 10, 4000, "r"),
        (0, 10.5, 4000, "r"),
        (0, 10, False, "r"),
    ],
)
def test_binding_rejects_broken_sample_ranges(
    tmp_path: Path,
    sample_start: int,
    sample_end: int,
    sample_rate: int,
    request_id: str,
) -> None:
    """샘플 범위는 [start, end) 반개구간의 정수 좌표다(§13.3) — 깨진 신원은 못 만든다."""
    from vlogsrt.project.raw.caption_provider import InvalidChunkBinding

    with pytest.raises(InvalidChunkBinding):
        audio_chunk_binding(
            chunk(tmp_path),
            sample_start=sample_start,  # type: ignore[arg-type]
            sample_end=sample_end,  # type: ignore[arg-type]
            sample_rate=sample_rate,  # type: ignore[arg-type]
            request_id=request_id,
        )


def test_provider_rejects_a_stale_binding_before_sending(tmp_path: Path) -> None:
    """묶은 뒤 청크 내용이 바뀌면 낡은 신원이다 — 전송은 한 번도 나가지 않는다(루트 리뷰).

    `InvalidChunkBinding`으로 잡히는지도 본다(루트 재검) — 신원을 못 만드는 경우와
    신원이 더 사실이 아닌 경우가 한 예외형으로 닫혀야 호출자가 샘플 좌표·해시·
    번호의 어긋남을 같은 문에서 받는다.
    """
    from vlogsrt.project.raw.caption_provider import ChunkBindingMismatch, InvalidChunkBinding

    sender = RecordingSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD)
    timing_provider, text_provider = providers(sender)
    target = chunk(tmp_path)
    binding = binding_of(target)
    (tmp_path / "final.mp3").write_bytes(b"rewritten-bytes")  # 신원을 만든 뒤 바뀌었다
    with pytest.raises(ChunkBindingMismatch, match="바뀌었다"):
        timing_provider.timed_transcript(
            binding=binding, chunk=target, settings=RawWorkflowConfig(), on=BEFORE_SHUTDOWN
        )
    with pytest.raises(InvalidChunkBinding):  # 부모형으로도 잡힌다(루트 독립 검사 모양)
        text_provider.text_transcript(
            binding=binding, chunk=target, settings=RawWorkflowConfig(), on=BEFORE_SHUTDOWN
        )
    assert sender.sent == []


def test_provider_rejects_a_binding_from_another_chunk(tmp_path: Path) -> None:
    """번호가 어긋난 신원도 낡은 신원이다 — 다른 청크에 붙이지 않는다(루트 리뷰)."""
    from vlogsrt.models import Chunk as EngineChunk
    from vlogsrt.project.raw.caption_provider import ChunkBindingMismatch

    sender = RecordingSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD)
    timing_provider, _ = providers(sender)
    target = chunk(tmp_path)
    stranger = EngineChunk(target.path, target.offset, target.duration, index=target.index + 1)
    with pytest.raises(ChunkBindingMismatch, match="번호"):
        timing_provider.timed_transcript(
            binding=binding_of(target),
            chunk=stranger,
            settings=RawWorkflowConfig(),
            on=BEFORE_SHUTDOWN,
        )
    assert sender.sent == []


def test_both_paths_bind_the_same_identity(tmp_path: Path) -> None:
    """문구·시간 응답이 같은 신원을 나른다 — 요청 ID와 청크 해시가 짝을 증명한다(§11.3)."""
    sender = RecordingSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD)
    timing_provider, text_provider = providers(sender)
    target = chunk(tmp_path)
    binding = binding_of(target)
    settings = RawWorkflowConfig()
    timed = timing_provider.timed_transcript(
        binding=binding, chunk=target, settings=settings, on=BEFORE_SHUTDOWN
    )
    text = text_provider.text_transcript(
        binding=binding, chunk=target, settings=settings, on=BEFORE_SHUTDOWN
    )
    assert timed.binding == binding and text.binding == binding
    assert timed.segments[0].evidence_id == "run-1:0"
    assert [kind for kind, _ in sender.sent] == [CAPTION_TIMING_KIND, CAPTION_TEXT_KIND]


# ─────────────────────────────────────────────────────────────
#  결과형 — 시각 칸의 유무가 타입으로 갈라진다
# ─────────────────────────────────────────────────────────────


def test_text_transcript_has_no_time_fields() -> None:
    """문구 형에는 시각 칸이 하나도 없다 — 칸을 추가하려는 순간 계약 위반이다."""
    assert {field.name for field in fields(TextTranscript)} == {
        "binding",
        "model",
        "text",
        "detected_language",
        "usage",
        "response",
        "languages",
    }


def test_timed_transcript_carries_provider_coordinates(tmp_path: Path) -> None:
    """세그먼트 시각은 제공자 좌표 그대로 — 오프셋은 곱하지 않고 함께 실린다(§11.6)."""
    binding = binding_of(chunk(tmp_path))
    timed = parse_timing_response(
        TIMING_PAYLOAD,
        binding=binding,
        model="m",
        provider="p",
        chunk_duration=12.0,
        chunk_offset=120.0,
    )
    assert [(segment.start, segment.end) for segment in timed.segments] == [
        (0.0, 1.5),
        (2.0, 3.5),
    ]
    assert timed.chunk_offset == 120.0
    assert timed.time_unit == "seconds"
    assert TIMING_PAYLOAD["segments"][0]["start"] == 0.0  # 원응답은 그대로다


def test_text_request_type_cannot_smuggle_time_fields(tmp_path: Path) -> None:
    """전송 요청형에도 logprobs 칸은 없다 — 보낼 수 없는 인자는 받지도 않는다."""
    with pytest.raises(TypeError):
        AudioTranscriptionRequest(  # type: ignore[call-arg]
            chunk=chunk(tmp_path),
            model="m",
            response_format="json",
            logprobs=True,
        )


# ─────────────────────────────────────────────────────────────
#  응답 해석 — 꼴이 어긋나면 지어내지 않고 실패한다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "text",
        {},
        {"language": "ko"},
        {"text": 3},
        {"text": "a", "language": 5},
        {"text": "a", "usage": "x"},
    ],
)
def test_parse_text_response_rejects_malformed(payload: object, tmp_path: Path) -> None:
    from vlogsrt.project.raw.caption_provider import MalformedCaptionResponse

    with pytest.raises(MalformedCaptionResponse):
        parse_text_response(payload, binding=binding_of(chunk(tmp_path)), model="m")


def test_parse_text_response_normalizes_the_sdk_language_object_array(tmp_path: Path) -> None:
    """SDK 3.8.0의 문서화된 모양은 객체 배열이다 — 실제 모델 model_dump로 재현한다(루트 재검).

    핸드메이드 픽스처가 아니라 설치된 SDK의 `Transcription`을 합성 값으로 만들어
    `model_dump()`한 것을 파서에 통과시킨다. 코드 원터플로 정규화하고 원 배열은
    `response`에 그대로 남는다.
    """
    from openai.types.audio.transcription import Transcription
    from openai.types.audio.transcription_language import TranscriptionLanguage

    dumped = Transcription(
        text="정확한 문구",
        languages=[TranscriptionLanguage(code="ko"), TranscriptionLanguage(code="en")],
    ).model_dump()
    assert dumped["languages"] == [{"code": "ko"}, {"code": "en"}]  # 객체 배열이 맞는지 먼저 확인
    transcript = parse_text_response(dumped, binding=binding_of(chunk(tmp_path)), model="m")
    assert transcript.text == "정확한 문구"
    assert transcript.languages == ("ko", "en")
    assert transcript.response["languages"] == [{"code": "ko"}, {"code": "en"}]  # 원본 보존
    single = parse_text_response(
        {"text": "x", "languages": [{"code": "ko"}]},
        binding=binding_of(chunk(tmp_path)),
        model="m",
    )
    assert single.languages == ("ko",)  # 루트 독립 검사의 기대 모양 그대로


def test_parse_text_response_languages_empty_or_absent_never_means_no_speech(
    tmp_path: Path,
) -> None:
    """빈 배열·없는 칸은 '언어를 확정 못 했다'일 뿐 무발화가 아니다(SDK 문서문)."""
    from openai.types.audio.transcription import Transcription

    empty = Transcription(text="어쨌든 말은 있다", languages=[]).model_dump()
    transcript = parse_text_response(empty, binding=binding_of(chunk(tmp_path)), model="m")
    assert transcript.languages == () and transcript.text == "어쨌든 말은 있다"
    absent = parse_text_response({"text": "x"}, binding=binding_of(chunk(tmp_path)), model="m")
    assert absent.languages == ()


def test_parse_text_response_tolerates_string_arrays_as_explicit_legacy(tmp_path: Path) -> None:
    """문자열 배열은 합성 픽스처·낡은 기록을 위한 명시적 관용이다 — 문서화된 모양이 아니다."""
    legacy = parse_text_response(
        {"text": "안녕", "languages": ["ko", "en"]}, binding=binding_of(chunk(tmp_path)), model="m"
    )
    assert legacy.languages == ("ko", "en")


@pytest.mark.parametrize(
    "languages",
    [
        "ko",  # 배열이 아니다
        [{"lang": "ko"}],  # code가 없다
        [{"code": 5}],
        [{"code": None}],
        [{"code": "  "}],
        ["ko", {"code": "en"}],  # 두 모양을 섞으면 애매하다 — 거짓말의 길이다
    ],
)
def test_parse_text_response_rejects_malformed_language_metadata(
    tmp_path: Path, languages: object
) -> None:
    from vlogsrt.project.raw.caption_provider import MalformedCaptionResponse

    with pytest.raises(MalformedCaptionResponse):
        parse_text_response(
            {"text": "안녕", "languages": languages},
            binding=binding_of(chunk(tmp_path)),
            model="m",
        )


def test_parse_text_response_keeps_empty_text(tmp_path: Path) -> None:
    """빈 문자열은 위반이 아니다 — 무발화의 증거도 아니다(§11.11)."""
    binding = binding_of(chunk(tmp_path))
    empty = parse_text_response({"text": ""}, binding=binding, model="m")
    assert empty.text == "" and empty.detected_language is None and empty.usage is None
    full = parse_text_response(TEXT_PAYLOAD, binding=binding, model="m")
    assert full.text == "망원동 카페" and full.detected_language == "ko"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "segments",
        {},
        {"segments": "x"},
        {"segments": [{}]},
        {"segments": [{"text": "a"}]},
        {"segments": [{"text": "a", "start": 0.0}]},
        {"segments": [{"text": "a", "start": 0.0, "end": float("nan")}]},
        {"segments": [{"text": "a", "start": True, "end": 1.0}]},
        # §11.11 — 음수·뒤집힘·길이 0·소유 범위 밖은 고쳐 쓰지 않고 실패다(루트 리뷰)
        {"segments": [{"text": "음수", "start": -0.5, "end": 1.0}]},
        {"segments": [{"text": "뒤집힘", "start": 4.0, "end": 3.0}]},
        {"segments": [{"text": "길이 0", "start": 1.0, "end": 1.0}]},
        {"segments": [{"text": "범위 밖", "start": 0.0, "end": 12.5}]},
        {"text": "시각 없는 덩어리"},  # json 문구 응답을 시간 응답으로 끼우는 경우
    ],
)
def test_parse_timing_response_rejects_malformed(payload: object, tmp_path: Path) -> None:
    """세그먼트·시각·글자가 빠지거나 소유 범위를 어기면 0으로 메우지 않는다."""
    from vlogsrt.project.raw.caption_provider import MalformedCaptionResponse

    with pytest.raises(MalformedCaptionResponse):
        parse_timing_response(
            payload,
            binding=binding_of(chunk(tmp_path)),
            model="m",
            provider="p",
            chunk_duration=12.0,
        )


@pytest.mark.parametrize(
    ("chunk_offset", "chunk_duration"),
    [(-0.1, 12.0), (float("inf"), 12.0), (0.0, 0.0), (0.0, float("nan"))],
)
def test_parse_timing_rejects_broken_chunk_coordinates(
    tmp_path: Path, chunk_offset: float, chunk_duration: float
) -> None:
    """청크 좌표 자체가 깨진 해석은 시작하지 않는다(루트 리뷰)."""
    from vlogsrt.project.raw.caption_provider import MalformedCaptionResponse

    with pytest.raises(MalformedCaptionResponse):
        parse_timing_response(
            TIMING_PAYLOAD,
            binding=binding_of(chunk(tmp_path)),
            model="m",
            provider="p",
            chunk_duration=chunk_duration,
            chunk_offset=chunk_offset,
        )


def test_parse_timing_response_allows_silence(tmp_path: Path) -> None:
    """빈 segments는 **미확인 빈 골격**이다 — 무발화의 증명으로 바꾸지 않는다(§11.11)."""
    timed = parse_timing_response(
        {"segments": []},
        binding=binding_of(chunk(tmp_path)),
        model="m",
        provider="p",
        chunk_duration=12.0,
    )
    assert timed.segments == ()
    assert timed.unconfirmed_empty is True
    assert timed.binding.audio_sha256  # 신원은 그대로 묶여 있다


# ─────────────────────────────────────────────────────────────
#  요청 빌더 — 지원 조합만 만들 수 있다
# ─────────────────────────────────────────────────────────────


def test_text_request_builder_shape(tmp_path: Path) -> None:
    """문구 요청은 json + 확인된 힌트 세 칸뿐이다(§11.4)."""
    settings = RawWorkflowConfig()
    request = text_transcription_request(
        chunk(tmp_path), settings=settings, prompt="망원동, 아페로"
    )
    assert request.model == settings.models.text_model
    assert request.response_format == "json"
    assert request.languages == ("ko",) and request.keywords == ()
    assert request.prompt == "망원동, 아페로"
    assert request.timestamp_granularities == ()


def test_timing_builder_never_carries_text_hints(tmp_path: Path) -> None:
    """설정에 힌트가 있어도 시간 요청에는 실리지 않는다 — 인자가 아예 없다(규칙 6)."""
    settings = replace(
        RawWorkflowConfig(),
        caption=replace(RawCaptionContract(), keywords=("블루펭귄",), languages=("ko", "en")),
    )
    request = timing_transcription_request(chunk(tmp_path), settings=settings)
    assert request.languages == () and request.keywords == () and request.prompt == ""
    assert request.timestamp_granularities == ("segment",)
    assert request.response_format == "verbose_json"


@pytest.mark.parametrize(
    "caption",
    [
        replace(RawCaptionContract(), text_response_format="verbose_json"),
        replace(RawCaptionContract(), text_response_format="srt"),
        replace(RawCaptionContract(), text_response_format="vtt"),
    ],
)
def test_text_builder_rejects_unauthorized_combos(
    tmp_path: Path, caption: RawCaptionContract
) -> None:
    """gpt-transcribe + srt/vtt/verbose_json 조합은 만들어지기 전에 막힌다(A7-1 완료 기준)."""
    settings = replace(RawWorkflowConfig(), caption=caption)
    with pytest.raises(UnverifiedModelCapability):
        text_transcription_request(chunk(tmp_path), settings=settings)


def test_text_request_never_carries_time_fields_at_any_layer(tmp_path: Path) -> None:
    """문구 역할에 입자를 얹는 무단 조합은 두 겹으로 막힌다 — 등록표와 전송 경계.

    빌더가 만든 요청에는 입자 칸이 아예 없고(아래 단언), 손으로 얹은 요청은
    전송 경계의 형태 검사가 거부한다(입자는 verbose_json과만 짝한다).
    """
    request = text_transcription_request(chunk(tmp_path), settings=RawWorkflowConfig())
    assert request.timestamp_granularities == ()
    smuggled = replace(request, timestamp_granularities=("segment",))
    from vlogsrt.transcribe import UnsupportedTranscriptionRequest, validate_transport_request

    with pytest.raises(UnsupportedTranscriptionRequest, match="verbose_json과만"):
        validate_transport_request(smuggled)
    with pytest.raises(UnverifiedModelCapability):
        capabilities.confirm_request_combo(
            role=capabilities.ROLE_TEXT,
            model=RawWorkflowConfig().models.text_model,
            response_format="json",
            features=(capabilities.FEATURE_SEGMENT_TIMES,),
        )


@pytest.mark.parametrize(
    "caption",
    [
        replace(RawCaptionContract(), timing_response_format="json"),
        replace(RawCaptionContract(), timestamp_granularities=("word",)),
    ],
)
def test_timing_builder_rejects_unauthorized_combos(
    tmp_path: Path, caption: RawCaptionContract
) -> None:
    settings = replace(RawWorkflowConfig(), caption=caption)
    with pytest.raises(UnverifiedModelCapability):
        timing_transcription_request(chunk(tmp_path), settings=settings)


# ─────────────────────────────────────────────────────────────
#  공급자 — 수명주기 검사가 전송보다 먼저다
# ─────────────────────────────────────────────────────────────


def test_timing_provider_blocks_on_shutdown_day_without_calling(tmp_path: Path) -> None:
    """종료일 도달 — 전송은 한 번도 나가지 않고 실패로 끝난다(§11.10)."""
    sender = RecordingSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD)
    timing_provider, _ = providers(sender)
    target = chunk(tmp_path)
    with pytest.raises(TimingProviderUnavailable):
        timing_provider.timed_transcript(
            binding=binding_of(target),
            chunk=target,
            settings=RawWorkflowConfig(),
            on=SHUTDOWN_DAY,
        )
    assert sender.sent == []


def test_text_provider_survives_the_timing_shutdown(tmp_path: Path) -> None:
    """문구 모델은 시간 제공자의 종료일과 무관하다 — 역할이 다르다(§11.10)."""
    sender = RecordingSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD)
    _, text_provider = providers(sender)
    target = chunk(tmp_path)
    text = text_provider.text_transcript(
        binding=binding_of(target), chunk=target, settings=RawWorkflowConfig(), on=SHUTDOWN_DAY
    )
    assert text.text == "망원동 카페"
    assert len(sender.sent) == 1


def test_provider_rejects_swapped_models_before_any_call(tmp_path: Path) -> None:
    """확인되지 않은 모델 조합은 사전에 닫힌다 — 조용한 fallback이 없다(§11.9)."""
    sender = RecordingSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD)
    timing_provider, text_provider = providers(sender)
    swapped = replace(
        RawWorkflowConfig(), models=replace(RawModelRoles(), timing_model="other-timing")
    )
    target = chunk(tmp_path)
    with pytest.raises(UnverifiedModelCapability):
        timing_provider.timed_transcript(
            binding=binding_of(target), chunk=target, settings=swapped, on=BEFORE_SHUTDOWN
        )
    with pytest.raises(UnverifiedModelCapability):
        text_provider.text_transcript(
            binding=binding_of(target),
            chunk=target,
            settings=replace(
                RawWorkflowConfig(), models=replace(RawModelRoles(), text_model="other-text")
            ),
            on=BEFORE_SHUTDOWN,
        )
    assert sender.sent == []


def test_provider_failure_propagates(tmp_path: Path) -> None:
    """전송 실패는 감추지 않고 그대로 올라간다 — 빈 성공이 없다(§11.11)."""

    class BrokenSender(RecordingSender):
        def send_request(
            self,
            request: AudioTranscriptionRequest,
            cfg: Config,
            *,
            kind: str,
            stem: str = "",
            source: str = "",
        ) -> dict[str, Any]:
            raise RuntimeError("전송 경계가 깨졌다")

    timing_provider, _ = providers(BrokenSender(text=TEXT_PAYLOAD, timing=TIMING_PAYLOAD))
    target = chunk(tmp_path)
    with pytest.raises(RuntimeError, match="깨졌다"):
        timing_provider.timed_transcript(
            binding=binding_of(target),
            chunk=target,
            settings=RawWorkflowConfig(),
            on=BEFORE_SHUTDOWN,
        )


def test_offline_history_needs_no_lifecycle_gate(tmp_path: Path) -> None:
    """과거 응답의 재해석은 온라인 문을 지나지 않는다 — 종료일 뒤에도 열린다(§11.10)."""
    binding = binding_of(chunk(tmp_path))
    timed = parse_timing_response(
        TIMING_PAYLOAD,
        binding=binding,
        model="m",
        provider="p",
        chunk_duration=12.0,
        chunk_offset=0.0,
    )
    assert len(timed.segments) == 2  # 수명주기 가드는 온라인 문에만 있다 — 이 길엔 없다
