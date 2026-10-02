"""최종 자막 A7-1의 역할별 요청 어댑터 — 문구와 시간을 타입으로 갈라놓는다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.4·§13.1의 `TextTranscript`·
`TimedTranscript` 계약을 코드로 세운다. 문구 경로는 시각 칸이 아예 없는 형이고
시간 경로는 세그먼트 시각의 원천이다 — 한 형이 다른 형의 칸을 얻는 순간 근거
없는 cue가 생긴다.

실제 호출은 없다. 네트워크는 `vlogsrt/transcribe.py`의 공개 전송 경계
(`TranscriptionSender`)를 통해서만 나가고, 조합 확인은 `capabilities.py`의
기능 등록표가 호출 전에 끝낸다. 문구 힌트(languages·keywords·prompt)가 시간
요청에 실리는 일은 어댑터 구조 자체가 막는다 — 시간 요청 빌더에는 힌트 인자가
없다(규칙 6, §11.4).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol, TypeGuard

from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.transcribe import (
    CAPTION_TEXT_KIND,
    CAPTION_TIMING_KIND,
    AudioTranscriptionRequest,
    TranscriptionSender,
)

TIMED_TIME_UNIT = "seconds"
"""세그먼트 시각의 원래 단위 — SRT 직렬화에서만 밀리초로 반올림한다(§13.3)."""


class MalformedCaptionResponse(RuntimeError):
    """모델 응답 꼴이 계약을 어긴다 — cue를 만들어 내지 않고 실패로 끝난다.

    시각을 몰래 0으로 메우거나 글자 수에 비례해 cue를 지어내는 길은 없다
    (§11.4·§11.11).
    """


class InvalidChunkBinding(ValueError):
    """청크 신원이 계약을 어긴다 — 샘플 범위는 `[start, end)` 반개구간이다(§13.3)."""


class ChunkBindingMismatch(InvalidChunkBinding):
    """신원과 청크가 어긋난다 — 낡은 신원으로는 전송하지 않는다(루트 재검).

    `InvalidChunkBinding`의 한 갈래다: 신원을 **만들 수 없는** 경우와 마찬가지로
    신원이 **더 사실이 아닌** 경우도 같은 문에서 닫힌다 — 호출자는 한 예외형으로
    샘플 좌표·해시·번호의 어긋남을 모두 받는다.
    """


@dataclass(frozen=True, slots=True)
class AudioChunkBinding:
    """같은 오디오를 들은 두 응답을 묶는 신원 — 청크 해시·샘플 범위·요청 ID(§11.3).

    문구·시간 두 경로가 이 값을 공유하므로 "두 응답이 같은 청크를 들었다"가
    기록으로 증명된다. 샘플 범위는 추출된 오디오의 좌표다 — 최종 영상 원점으로의
    환산은 소비 계층의 산술이다(§11.6).
    """

    chunk_index: int
    audio_sha256: str
    sample_start: int
    sample_end: int
    sample_rate: int
    request_id: str


def _exact_int(value: object) -> bool:
    """정수 좌표만 받는다 — bool은 int의 서브클래스지만 샘플 좌표가 될 수 없다."""
    return isinstance(value, int) and not isinstance(value, bool)


def audio_chunk_binding(
    chunk: Chunk,
    *,
    sample_start: int,
    sample_end: int,
    sample_rate: int,
    request_id: str,
) -> AudioChunkBinding:
    """청크 파일에서 SHA-256을 계산해 신원을 만든다 — 네트워크 호출은 없다.

    해시는 파일 내용에서 다시 계산하므로 요청 조각과 응답을 잇는 열쇠가 코드가
    아니라 데이터다. 좌표는 정확한 정수만 받는다 — 런타임에 bool·float가 새어
    들어와 계약이 무너지는 일은 이 문에서 막는다.
    """
    for name, value in (
        ("sample_start", sample_start),
        ("sample_end", sample_end),
        ("sample_rate", sample_rate),
    ):
        if not _exact_int(value):
            raise InvalidChunkBinding(f"{name}은 정수여야 한다: {value!r}")
    if sample_start < 0 or sample_end <= sample_start:
        raise InvalidChunkBinding(
            f"샘플 범위는 [start, end) 반개구간이어야 한다: [{sample_start}, {sample_end})"
        )
    if sample_rate <= 0:
        raise InvalidChunkBinding(f"샘플 레이트는 양수여야 한다: {sample_rate}")
    if not request_id.strip():
        raise InvalidChunkBinding("요청 ID가 비어 있다")
    audio_sha256 = hashlib.sha256(Path(chunk.path).read_bytes()).hexdigest()
    return AudioChunkBinding(
        chunk_index=chunk.index,
        audio_sha256=audio_sha256,
        sample_start=sample_start,
        sample_end=sample_end,
        sample_rate=sample_rate,
        request_id=request_id,
    )


def verify_chunk_binding(binding: AudioChunkBinding, chunk: Chunk) -> None:
    """전송 직전에 신원과 청크가 아직 같은지 다시 본다.

    묶은 뒤 파일 내용이 바뀌었거나 번호가 어긋난 신원은 **낡은 신원**이다 — 그
    응답을 새 청크에 붙이면 다른 오디오를 들었다고 기록하게 된다. 보내기 전에
    해시를 다시 계산해 거짓말의 길을 닫는다.
    """
    if binding.chunk_index != chunk.index:
        raise ChunkBindingMismatch(
            f"신원의 청크 번호({binding.chunk_index})가 요청({chunk.index})과 다르다"
        )
    digest = hashlib.sha256(Path(chunk.path).read_bytes()).hexdigest()
    if digest != binding.audio_sha256:
        raise ChunkBindingMismatch(
            f"청크 {chunk.index}의 내용이 신원을 만든 뒤 바뀌었다 — 낡은 신원으로는 보내지 않는다"
        )


@dataclass(frozen=True, slots=True)
class TextTranscript:
    """문구 경로의 결과 — **시각 칸이 하나도 없다**(§11.4·§13.1).

    JSON 응답에 `text`가 있다는 사실은 단어·세그먼트 시각이 있다는 뜻이 아니다.
    응답 도착 시각도 발화 시각으로 쓰지 않는다. 이 형에 시각 칸을 추가하는
    순간 근거 없는 cue가 생긴다 — 그래서 `TimedTranscript`와 타입으로 갈라져
    있다. 감지 언어·사용량은 모델이 실어 준 것만 담는다.
    """

    binding: AudioChunkBinding
    model: str
    text: str
    detected_language: str | None
    usage: Mapping[str, object] | None
    response: Mapping[str, object]
    # 문서화된 객체 배열([{"code": "ko"}, ...])에서 정규화한 코드 원터플(루트 재검).
    # 원 배열 그대로는 response 칸에 보존된다. 없거나 빈 배열은 언어를 확정 못 했다는
    # 뜻일 뿐 무발화가 아니다. 지어내는 칸은 없다.
    languages: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TimedSegment:
    """시간 제공자 세그먼트 하나 — 시각은 제공자 좌표 그대로다.

    청크 오프셋·최종 영상 원점의 환산은 소비 계층의 산술이고 여기서 하지
    않는다(§11.6). 시각은 `0 ≤ start < end ≤ 청크 길이`로 검사를 마친 값이다.
    """

    index: int
    start: float
    end: float
    text: str
    evidence_id: str


@dataclass(frozen=True, slots=True)
class TimedTranscript:
    """시간 경로의 결과 — 세그먼트 시각의 유일한 원천이다(§11.10·§13.1).

    `chunk_offset`은 ffmpeg가 실제로 잘랐다고 알려준 청크 시작 시각이다. 이 값을
    세그먼트 시각에 더하는 것이 시각에 가해지는 유일한 계산이다(규칙 1) — 더하는
    곳은 소비 계층이다.
    """

    binding: AudioChunkBinding
    model: str
    provider: str
    segments: tuple[TimedSegment, ...]
    chunk_offset: float
    time_unit: str = TIMED_TIME_UNIT
    response: Mapping[str, object] | None = None

    @property
    def unconfirmed_empty(self) -> bool:
        """빈 세그먼트 목록은 **미확인 빈 골격**이다 — 무발화의 증명이 아니다(§11.11).

        무음에 cue가 없는 것은 정상이지만, `confirmed_no_speech`와 구분되는 상태로
        소비 계층에 넘어가야 한다. 이 칸이 그 구분의 표식이다.
        """
        return not self.segments


def _finite_number(value: object) -> TypeGuard[float]:
    """숫자이면서 유한한 값만 시각이 될 수 있다 — bool·NaN·무한대는 거짓이다."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    return math.isfinite(float(value))


def parse_text_response(
    payload: object, *, binding: AudioChunkBinding, model: str
) -> TextTranscript:
    """문구 응답을 `TextTranscript`로 — 꼴이 어긋나면 지어내지 않고 실패한다.

    `text`가 빈 문자열인 것은 위반이 아니다(§11.11) — 빈 문자열 하나만으로
    무발화를 확정하지도 않는다. 판정은 소비 계층의 몫이다. 복수형 `languages`
    배열은 코드 원터플로 정규화하고 원 배열은 `response`에 그대로 남긴다(루트
    재검).
    """
    if not isinstance(payload, Mapping):
        raise MalformedCaptionResponse(f"문구 응답이 JSON 객체가 아니다: {type(payload).__name__}")
    if "text" not in payload or not isinstance(payload["text"], str):
        raise MalformedCaptionResponse("문구 응답에 문자열 text가 없다")
    language = payload.get("language")
    if language is not None and not isinstance(language, str):
        raise MalformedCaptionResponse(f"감지 언어가 문자열이 아니다: {language!r}")
    usage = payload.get("usage")
    if usage is not None and not isinstance(usage, Mapping):
        raise MalformedCaptionResponse(f"사용량이 JSON 객체가 아니다: {usage!r}")
    return TextTranscript(
        binding=binding,
        model=model,
        text=payload["text"],
        detected_language=language,
        usage=usage,
        response=payload,
        languages=_response_languages(payload),
    )


def _response_languages(payload: Mapping[object, object]) -> tuple[str, ...]:
    """복수형 languages 배열을 코드 원터플로 정규화한다(루트 재검).

    SDK 3.8.0의 문서화된 모양은 객체 배열이다 — `Transcription.languages`가
    `Optional[List[TranscriptionLanguage]]`이고 `TranscriptionLanguage.code`가
    문자열이다. `model_dump()`를 거치면 `[{"code": "ko"}, ...]`로 온다. 문자열
    배열은 합성 픽스처·낡은 기록을 위한 **명시적 관용**일 뿐 문서화된 모양이
    아니다. 둘을 섞은 배열·`code`가 없거나 문자열이 아닌 객체는 거짓말의 길이므로
    거부한다. 없거나 빈 배열은 언어를 확정 못 했다는 뜻일 뿐 무발화가 아니다.
    """
    raw = payload.get("languages")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MalformedCaptionResponse(f"languages가 배열이 아니다: {raw!r}")
    codes: list[str] = []
    kinds: set[str] = set()
    for item in raw:
        if isinstance(item, Mapping):
            code = item.get("code")
            if not isinstance(code, str) or not code.strip():
                raise MalformedCaptionResponse(f"languages 항목에 문자열 code가 없다: {item!r}")
            kinds.add("object")
            codes.append(code)
        elif isinstance(item, str):
            if not item.strip():
                raise MalformedCaptionResponse("languages에 빈 문자열이 있다")
            kinds.add("string")
            codes.append(item)
        else:
            raise MalformedCaptionResponse(f"languages 항목을 이해할 수 없다: {item!r}")
    if len(kinds) > 1:
        raise MalformedCaptionResponse(
            f"languages가 두 모양을 섞었다 — 어느 쪽 해석도 확정할 수 없다: {raw!r}"
        )
    return tuple(codes)


def parse_timing_response(
    payload: object,
    *,
    binding: AudioChunkBinding,
    model: str,
    provider: str,
    chunk_duration: float,
    chunk_offset: float = 0.0,
) -> TimedTranscript:
    """시간 응답을 `TimedTranscript`로 — 세그먼트가 없으면 그대로 없다고 남긴다.

    `segments`가 아예 없는 응답은 계약 위반이다. 시각은 청크가 소유한 범위
    안에서 `0 ≤ start < end ≤ chunk_duration`을 지켜야 한다(§11.11 — 음수
    없음·start<end·범위 안). 어긋난 시각을 몰래 접거나 메우지 않는다 —
    cue를 지어내는 길을 여는 순간 가짜 SRT가 돌아온다. 시각을 고치는 것도
    추측이므로 여기서는 하지 않는다.
    """
    if not isinstance(payload, Mapping):
        raise MalformedCaptionResponse(f"시간 응답이 JSON 객체가 아니다: {type(payload).__name__}")
    if not _finite_number(chunk_offset) or chunk_offset < 0:
        raise MalformedCaptionResponse(f"청크 오프셋이 유한한 0 이상이 아니다: {chunk_offset!r}")
    if not _finite_number(chunk_duration) or chunk_duration <= 0:
        raise MalformedCaptionResponse(f"청크 길이가 유한한 양수가 아니다: {chunk_duration!r}")
    if "segments" not in payload:
        raise MalformedCaptionResponse(
            f"시간 응답에 segments가 없다 — 받은 필드: {sorted(payload)}"
        )
    records = payload["segments"]
    if not isinstance(records, list):
        raise MalformedCaptionResponse(f"segments가 리스트가 아니다: {type(records).__name__}")
    segments = tuple(
        _timed_segment(record, position, binding.request_id, chunk_duration)
        for position, record in enumerate(records)
    )
    return TimedTranscript(
        binding=binding,
        model=model,
        provider=provider,
        segments=segments,
        chunk_offset=chunk_offset,
        response=payload,
    )


def _timed_segment(
    record: object, position: int, request_id: str, chunk_duration: float
) -> TimedSegment:
    """세그먼트 하나의 형태 검사 — 시각·글자가 없으면 지어내지 않고 실패한다.

    소유한 청크 범위를 벗어나거나 뒤집힌 시각은 고쳐 쓰지 않고 실패로 돌린다
    (루트 리뷰) — 이 층의 계약은 §11.11이고, 뒤집힌 시각을 접어 살리는 것은
    엔진 골격(규칙 9)의 규율이다.
    """
    where = f"세그먼트 {position}"
    if not isinstance(record, Mapping):
        raise MalformedCaptionResponse(f"{where}이(가) JSON 객체가 아니다")
    text = record.get("text")
    if not isinstance(text, str):
        raise MalformedCaptionResponse(f"{where}의 text가 문자열이 아니다: {text!r}")
    start, end = record.get("start"), record.get("end")
    if not _finite_number(start) or not _finite_number(end):
        raise MalformedCaptionResponse(
            f"{where}의 시각이 유한 숫자가 아니다: start={start!r}, end={end!r}"
        )
    if float(start) < 0 or not float(start) < float(end):
        raise MalformedCaptionResponse(
            f"{where}의 시각이 0 ≤ start < end를 어긴다: start={start!r}, end={end!r}"
        )
    if float(end) > chunk_duration:
        raise MalformedCaptionResponse(
            f"{where}의 끝({end!r})이 청크 길이({chunk_duration!r})를 벗어난다 — "
            "소유하지 않은 시각이다"
        )
    return TimedSegment(
        index=position,
        start=float(start),
        end=float(end),
        text=text,
        evidence_id=f"{request_id}:{position}",
    )


def _text_features(prompt: str, settings: RawWorkflowConfig) -> tuple[str, ...]:
    """요청에 실제로 실리는 힌트만 확인 받는다 — 빈 힌트는 필드째 빠진다(§11.4)."""
    contract = settings.caption
    features = [capabilities.FEATURE_JSON]
    if contract.languages:
        features.append(capabilities.FEATURE_LANGUAGES)
    if contract.keywords:
        features.append(capabilities.FEATURE_KEYWORDS)
    if prompt:
        features.append(capabilities.FEATURE_PROMPT)
    return tuple(features)


def text_transcription_request(
    chunk: Chunk, *, settings: RawWorkflowConfig, prompt: str = ""
) -> AudioTranscriptionRequest:
    """문구 경로 요청 — 확인된 힌트는 languages·keywords·prompt 뿐이다(§11.4).

    `prompt`는 사용자가 승인한 문맥만 받는다 — 장면 요약의 자동 주입은 이
    인자로 들어오지 않는다. srt·vtt·verbose_json·timestamp_granularities·
    logprobs는 이 요청에 칸 자체가 없다. 입자 칸(timestamp_granularities)은
    시간 계약의 것이므로 여기서 읽지도 않는다 — 문구 요청에 입자를 얹는
    무단 조합은 전송 경계(입자는 verbose_json과만 짝한다)와 등록표가 닫는다.
    """
    contract, models = settings.caption, settings.models
    capabilities.confirm_request_combo(
        role=capabilities.ROLE_TEXT,
        model=models.text_model,
        response_format=contract.text_response_format,
        features=_text_features(prompt, settings),
    )
    return AudioTranscriptionRequest(
        chunk=chunk,
        model=models.text_model,
        response_format=contract.text_response_format,
        languages=contract.languages,
        keywords=contract.keywords,
        prompt=prompt,
    )


def timing_transcription_request(
    chunk: Chunk, *, settings: RawWorkflowConfig, temperature: float | None = None
) -> AudioTranscriptionRequest:
    """시간 경로 요청 — 문구 힌트가 **실리지 않는다**(규칙 6, §11.4).

    힌트 인자가 아예 없으므로 문구 설정의 languages·keywords가 넘어올 길이
    없다. `temperature`는 되풀이 대응의 손잡이(D28)로 호출자가 값을 정한다 —
    기본은 필드를 아예 보내지 않는 것이다.
    """
    contract, models = settings.caption, settings.models
    capabilities.confirm_request_combo(
        role=capabilities.ROLE_TIMING,
        model=models.timing_model,
        response_format=contract.timing_response_format,
        features=_granularity_features(contract.timestamp_granularities),
    )
    return AudioTranscriptionRequest(
        chunk=chunk,
        model=models.timing_model,
        response_format=contract.timing_response_format,
        timestamp_granularities=contract.timestamp_granularities,
        temperature=temperature,
    )


def _granularity_features(granularities: tuple[str, ...]) -> tuple[str, ...]:
    """계약의 입자를 확인받는다 — 세그먼트만 확인됐고 word는 별도 계약이다(§11.3)."""
    return tuple(f"timestamp_granularities:{granularity}" for granularity in granularities)


class TimingProvider(Protocol):
    """시간 골격 제공자의 교체 경계(§11.10) — 직접 SDK 호출은 금지다.

    입력은 검증된 오디오 청크·시간 대응·승인된 설정이고 출력은 시간 범위·글자·
    증거 ID가 있는 `TimedTranscript`다. 현재 어댑터는 whisper_verbose_json뿐이며
    미검증 대체 모델을 자동 도입하지 않는다.
    """

    def timed_transcript(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> TimedTranscript: ...


class TextTranscriptProvider(Protocol):
    """문구 경로의 주입 경계 — `TimingProvider`와 대칭이다."""

    def text_transcript(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        prompt: str = "",
        stem: str = "",
        source: str = "",
    ) -> TextTranscript: ...


@dataclass(frozen=True, slots=True)
class TransportTimingProvider:
    """확인된 verbose_json 어댑터 — 공개 전송 경계를 통해서만 부른다.

    수명주기 검사가 끝난 뒤에야 전송이 일어난다. 실패는 여기서 감추지 않고
    그대로 올라간다 — 바운드된 재시도는 전송 경계의 `_retry`가 정한다.
    """

    sender: TranscriptionSender
    cfg: Config

    def timed_transcript(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> TimedTranscript:
        capabilities.preflight_online_transcription(settings, on, roles=(capabilities.ROLE_TIMING,))
        request = timing_transcription_request(chunk, settings=settings)
        verify_chunk_binding(binding, chunk)
        payload = self.sender.send_request(
            request, self.cfg, kind=CAPTION_TIMING_KIND, stem=stem, source=source
        )
        return parse_timing_response(
            payload,
            binding=binding,
            model=request.model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


@dataclass(frozen=True, slots=True)
class TransportTextProvider:
    """확인된 문구 어댑터 — 공개 전송 경계를 통해서만 부른다."""

    sender: TranscriptionSender
    cfg: Config

    def text_transcript(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        prompt: str = "",
        stem: str = "",
        source: str = "",
    ) -> TextTranscript:
        capabilities.preflight_online_transcription(settings, on, roles=(capabilities.ROLE_TEXT,))
        request = text_transcription_request(chunk, settings=settings, prompt=prompt)
        verify_chunk_binding(binding, chunk)
        payload = self.sender.send_request(
            request, self.cfg, kind=CAPTION_TEXT_KIND, stem=stem, source=source
        )
        return parse_text_response(payload, binding=binding, model=request.model)
