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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar, Protocol, TypeGuard

from vlogsrt import log
from vlogsrt.env import ANALYSIS_GEMINI_KEY_ENV
from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities, word_cues
from vlogsrt.project.raw.config import (
    GEMINI_TRANSCRIBE_PROFILES,
    TIMING_RESPONSE_TOLERANCE,
    WORD_CUE_SETTINGS,
    RawWorkflowConfig,
    WordCueSettings,
    gemini_transcribe_language_codes,
)
from vlogsrt.project.raw.gemini_transcribe_parse import (
    FALLBACK,
    ParsedTranscript,
    parse_transcription,
)
from vlogsrt.transcribe import (
    GeminiTranscriptionRequest,
    request_gemini_transcription,
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
    않는다(§11.6). 시각은 `0 ≤ start ≤ end ≤ 청크 길이`로 검사를 마친 값이다 —
    길이 0은 글자째 남고, 청크 끝을 관용 안에서 넘은 시각은 청크 끝으로 닫혀 온다(D55).
    """

    index: int
    start: float
    end: float
    text: str
    evidence_id: str
    # whisper의 세그먼트 신뢰도(W-25c, D73) — 응답에 유한 숫자로 있을 때만. 표시·보고서 재료다.
    no_speech_prob: float | None = None
    avg_logprob: float | None = None


@dataclass(frozen=True, slots=True)
class TimedWord:
    """단어 시각 하나(W-25c, D73) — 청크 국소 초. **제안·보고서 재료일 뿐** 자막 시각이 아니다."""

    start: float
    end: float
    word: str


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
    # 청크 끝으로 닫은 세그먼트의 위치(D55) — 원 응답은 `response`에 그대로 있다.
    clamped_segments: tuple[int, ...] = ()
    # 단어 시각(W-25c, D73) — 세그먼트와 따로 산다. 단어를 세그먼트에 나눠 붙이지 않는다.
    words: tuple[TimedWord, ...] = ()
    # Gemini 원본의 줄마다 센 것(W-52b1) — (세그먼트 위치, `word_cues.WORD_NOTES`의 이름).
    # 「확인 필요」 표시의 재료일 뿐이다.
    word_notes: tuple[tuple[int, str], ...] = ()

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


def parse_timing_response(
    payload: object,
    *,
    binding: AudioChunkBinding,
    model: str,
    provider: str,
    chunk_duration: float,
    chunk_offset: float = 0.0,
    overrun_tolerance: float = TIMING_RESPONSE_TOLERANCE.chunk_end_overrun_seconds,
) -> TimedTranscript:
    """시간 응답을 `TimedTranscript`로 — 세그먼트가 없으면 그대로 없다고 남긴다.

    `segments`가 없는 응답은 계약 위반이다. 시각은 `0 ≤ start ≤ end ≤ chunk_duration`
    (§11.11)이고 어긋난 시각을 몰래 메우지 않는다 — cue를 지어내면 가짜 SRT가 돌아온다.
    예외 하나(D55): 청크 끝을 `overrun_tolerance` 이하로 넘은 시각은 **청크 끝으로 닫고**
    경고한다 — 그 하나로 청크째 버리면 말이 조용히 사라진다(규칙 9, 실측 4/38). 길이 0은
    글자째 남는다. 원 응답은 `response`에 그대로 남는다.
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
    segments, clamped = _timed_segments(
        payload["segments"], binding.request_id, chunk_duration, overrun_tolerance
    )
    return TimedTranscript(
        binding=binding,
        model=model,
        provider=provider,
        segments=segments,
        chunk_offset=chunk_offset,
        response=payload,
        clamped_segments=clamped,
        words=_timed_words(payload.get("words"), chunk_duration, overrun_tolerance),
    )


def _timed_segments(
    records: object, request_id: str, chunk_duration: float, tolerance: float
) -> tuple[tuple[TimedSegment, ...], tuple[int, ...]]:
    """세그먼트 전부와 청크 끝으로 닫힌 위치(D55) — 하나라도 꼴이 어긋나면 실패한다."""
    if not isinstance(records, list):
        raise MalformedCaptionResponse(f"segments가 리스트가 아니다: {type(records).__name__}")
    if not _finite_number(tolerance) or tolerance < 0:
        raise MalformedCaptionResponse(f"넘침 관용이 유한한 0 이상이 아니다: {tolerance!r}")
    segments = tuple(
        _timed_segment(record, position, request_id, chunk_duration, tolerance)
        for position, record in enumerate(records)
    )
    clamped = tuple(
        segment.index
        for segment, record in zip(segments, records, strict=True)
        if isinstance(record, Mapping) and segment.end != float(record["end"])
    )
    return segments, clamped


def _timed_segment(
    record: object, position: int, request_id: str, chunk_duration: float, tolerance: float
) -> TimedSegment:
    """세그먼트 하나의 형태 검사 — 시각·글자가 없으면 지어내지 않고 실패한다.

    음수·뒤집힌 시각, 청크 끝을 관용 밖으로 넘은 시각은 고쳐 쓰지 않고 실패로
    돌린다(루트 리뷰·§11.11). 길이 0은 지나가고, 관용 안의 넘침은 청크 끝으로
    닫힌다(D55 — `_close_at_chunk_end`).
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
    if float(start) < 0 or float(start) > float(end):
        raise MalformedCaptionResponse(
            f"{where}의 시각이 0 ≤ start ≤ end를 어긴다: start={start!r}, end={end!r}"
        )
    closed_start, closed_end = _close_at_chunk_end(
        where, float(start), float(end), chunk_duration, tolerance
    )
    return TimedSegment(
        index=position,
        start=closed_start,
        end=closed_end,
        text=text,
        evidence_id=f"{request_id}:{position}",
        no_speech_prob=_score(record, "no_speech_prob"),
        avg_logprob=_score(record, "avg_logprob"),
    )


def _score(record: Mapping[object, object], name: str) -> float | None:
    """세그먼트 신뢰도 하나 — 유한 숫자가 아니면 없는 것으로 둔다(지어내지 않는다, D73)."""
    value = record.get(name)
    return float(value) if _finite_number(value) else None


def _timed_words(records: object, chunk_duration: float, tolerance: float) -> tuple[TimedWord, ...]:
    """단어 시각 전부(W-25c, D73) — 없으면 빈 튜플이다.

    단어는 시각의 원천이 아니라 재료라서 꼴이 어긋나도 청크를 버리지 않는다 — 세그먼트는
    그대로 두고 **단어만** 비우며 경고한다(조용한 손실은 없다). 청크 끝을 관용 안에서 넘은
    단어는 세그먼트와 같은 선(D55)으로 청크 끝에 닫고 그 수를 경고한다."""
    if records is None:
        return ()
    try:
        if not isinstance(records, list):
            raise MalformedCaptionResponse(f"words가 리스트가 아니다: {type(records).__name__}")
        words = tuple(_timed_word(record, position) for position, record in enumerate(records))
        closed = tuple(_close_word(word, chunk_duration, tolerance) for word in words)
    except MalformedCaptionResponse as error:
        log.warn(f"단어 시각을 싣지 않는다 — 세그먼트는 그대로다 (D73): {error}")
        return ()
    moved = sum(1 for before, after in zip(words, closed, strict=True) if before != after)
    if moved:
        log.warn(f"단어 시각 {moved}개의 끝이 청크 끝을 넘어 청크 끝으로 닫았다 (D55·D73)")
    return closed


def _timed_word(record: object, position: int) -> TimedWord:
    where = f"단어 {position}"
    if not isinstance(record, Mapping) or not isinstance(record.get("word"), str):
        raise MalformedCaptionResponse(f"{where}에 문자열 word가 없다: {record!r}")
    start, end = record.get("start"), record.get("end")
    if not _finite_number(start) or not _finite_number(end):
        raise MalformedCaptionResponse(f"{where}의 시각이 유한 숫자가 아니다: {record!r}")
    if float(start) < 0 or float(start) > float(end):
        raise MalformedCaptionResponse(f"{where}의 시각이 0 ≤ start ≤ end를 어긴다: {record!r}")
    return TimedWord(start=float(start), end=float(end), word=str(record["word"]))


def _close_word(word: TimedWord, chunk_duration: float, tolerance: float) -> TimedWord:
    over = word.end - chunk_duration
    if over <= 0:
        return word
    if over > tolerance:
        raise MalformedCaptionResponse(
            f"단어 {word.word!r}의 끝({word.end!r})이 청크 길이({chunk_duration!r})를 관용 밖으로"
            " 벗어난다"
        )
    return TimedWord(min(word.start, chunk_duration), chunk_duration, word.word)


def _close_at_chunk_end(
    where: str, start: float, end: float, chunk_duration: float, tolerance: float
) -> tuple[float, float]:
    """청크 끝을 관용 안에서 넘은 시각만 청크 끝으로 닫는다 — 시각에 가하는 유일한 손질.

    오디오가 끝난 자리 너머의 시각은 들은 소리가 없으니 청크 끝이 유일한 사실이다.
    관용 밖은 소유하지 않은 시각이라 실패다. 닫았으면 반드시 경고를 남긴다(규칙 9 —
    조용한 손질은 없다).
    """
    over = end - chunk_duration
    if over <= 0:
        return start, end
    if over > tolerance:
        raise MalformedCaptionResponse(
            f"{where}의 끝({end!r})이 청크 길이({chunk_duration!r})를 관용({tolerance}초) 밖으로"
            " 벗어난다 — 소유하지 않은 시각이다"
        )
    log.warn(
        f"{where}의 끝 {end!r}초가 청크 길이 {chunk_duration!r}초를 {over:.3f}초 넘었다 — 청크는"
        " 버리지 않는다. 해석 시각은 청크 끝으로 닫고 원 응답은 그대로 둔다 (D55)"
    )
    return min(start, chunk_duration), chunk_duration


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


def gemini_timing_request(
    chunk: Chunk, *, settings: RawWorkflowConfig
) -> GeminiTranscriptionRequest:
    """Gemini 시간 엔진 요청(D92) — 오디오·언어 잠금·verbatim·단어 시각뿐이다(규칙 6).

    등록된 Gemini 전사 엔진이 아닌 설정은 보내기 전에 닫는다(whisper 설정이 이 문에 오지 않는다).
    언어 잠금은 계약의 예상 언어(`ko`)를 등록표로 옮긴 값(`ko-KR`)이다 — 힌트가 아니다.
    """
    contract, model = settings.caption, settings.models.timing_model
    profile = GEMINI_TRANSCRIBE_PROFILES.get(model)
    if profile is None:
        raise capabilities.UnverifiedModelCapability(
            f"시간 모델 {model!r}은(는) 등록된 Gemini 전사 엔진이 아니다 — 이 문으로 보내지 않는다"
        )
    capabilities.confirm_request_combo(
        role=capabilities.ROLE_TIMING,
        model=model,
        response_format=contract.timing_response_format,
        features=(
            capabilities.FEATURE_VERBATIM,
            *(f"timestamp_granularities:{item}" for item in contract.timestamp_granularities),
            capabilities.FEATURE_LANGUAGE_CODES,
        ),
    )
    return GeminiTranscriptionRequest(
        model=model,
        chunk=chunk,
        language_codes=gemini_transcribe_language_codes(contract.languages, profile),
        max_audio_seconds=profile.max_audio_seconds_with_word_times,
        max_request_bytes=profile.inline_request_max_bytes,
        mode=profile.mode,
        granularities=contract.timestamp_granularities,
    )


def gemini_timed_transcript(
    payload: object,
    *,
    binding: AudioChunkBinding,
    model: str,
    provider: str,
    chunk: Chunk,
    cue_settings: WordCueSettings = WORD_CUE_SETTINGS,
) -> TimedTranscript:
    """Gemini 응답 → whisper와 같은 모양의 `TimedTranscript`(D92).

    줄은 단어 경계에서만 묶고(`word_cues`), 줄 시각은 엔진 단어 시각 그대로다. 받아 줄 수 없는
    응답은 `MalformedCaptionResponse`(청크 하나의 보이는 실패)다. 센 것(자리 없는 글자·시각 없는
    단어·닫은 끝·순서 어긋남·정렬 실패)은 경고로 남긴다 — 조용한 손질은 없다(규칙 9).
    """
    try:
        parsed = parse_transcription(payload, chunk_duration=chunk.duration)
        grouped = word_cues.group(parsed.words, cue_settings)
    except ValueError as error:
        raise MalformedCaptionResponse(f"Gemini 전사 응답을 받아 줄 수 없다: {error}") from error
    _warn_counts(chunk.index, parsed, grouped)
    segments = tuple(
        TimedSegment(
            index=index,
            start=float(cue.start),
            end=float(cue.end),
            text=cue.text,
            evidence_id=f"{binding.request_id}:{index}",
        )
        for index, cue in enumerate(grouped.cues)
    )
    edge = Fraction(repr(float(chunk.duration)))
    return TimedTranscript(
        binding=binding,
        model=model,
        provider=provider,
        segments=segments,
        chunk_offset=chunk.offset,
        response=payload if isinstance(payload, Mapping) else None,
        clamped_segments=tuple(
            index
            for index, cue in enumerate(grouped.cues)
            if parsed.clamped_words and cue.end == edge
        ),
        words=tuple(
            TimedWord(start=float(word.start), end=float(word.end), word=word.text)
            for word in parsed.words
            if word.start is not None and word.end is not None
        ),
        word_notes=word_cues.word_notes(grouped, parsed.alignment),
    )


def _warn_counts(index: int, parsed: ParsedTranscript, grouped: word_cues.CueGroup) -> None:
    counted = [
        (parsed.unplaced_chars, "단어가 덮지 못해 싣지 않은 글자"),
        (parsed.untimed_words, "시각 없이 이웃 줄에 남긴 단어"),
        (parsed.clamped_words, "청크 끝으로 닫은 단어 끝"),
        (grouped.out_of_order, "앞 단어보다 일찍 시작한 단어"),
    ]
    notes = [f"{label} {count}개" for count, label in counted if count]
    if parsed.alignment == FALLBACK:
        notes.append("단어를 전체 글에서 차례로 찾지 못해 띄어 이었다")
    if notes:
        log.warn(f"Gemini 전사 조각 {index}: " + " · ".join(notes) + " (D92)")


@dataclass(frozen=True, slots=True)
class GeminiTranscribeTimingProvider:
    """Gemini 3.5 Transcribe 시간 어댑터(D92) — 공개 문(`request_gemini_transcription`)으로만.

    사전 검사 → 확인된 조합 → 청크 신원 재확인 → 문 → 해석 → 단어 묶기. 온도 손잡이가 없다 — 다시
    묻기(D78)는 이 엔진에 없다(`RescueTimingProvider`가 아니다). 관찰자·취소는 whisper 전송과 같은
    것을 받는다(`timing_provider_for`가 그 전송에서 옮겨 준다).
    """

    CACHE_ORIGIN: ClassVar[str] = "GeminiTranscribeTimingProvider"
    """청크 캐시 열쇠의 제공자 칸(P-6, D160) — 클래스 이름이 아니라 이 고정 값이다. 지금 이름과
    같아 열쇠 바이트가 그대로다 — 클래스 이름을 바꿔도 이 값은 그대로 둔다."""

    client: Any
    observer: Any = None
    cancellation: Callable[[], None] | None = None
    cue_settings: WordCueSettings = WORD_CUE_SETTINGS

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
        request = gemini_timing_request(chunk, settings=settings)
        verify_chunk_binding(binding, chunk)
        payload = request_gemini_transcription(
            self.client,
            request=request,
            online=True,
            observer=self.observer,
            cancellation=self.cancellation,
            stem=stem,
            source=source,
        )
        return gemini_timed_transcript(
            payload,
            binding=binding,
            model=request.model,
            provider=settings.caption.timing_provider,
            chunk=chunk,
            cue_settings=self.cue_settings,
        )


WHISPER_RETIRED = (
    "whisper-1·gpt-transcribe 전사는 은퇴했다(D163) — 이 원본은 「다시 듣기」로 Gemini 3.5"
    " Transcribe에 다시 들으세요(이미 들은 자막은 그대로 읽고 고칠 수 있다)"
)


class TranscriptionRetired(capabilities.TimingProviderUnavailable):
    """whisper·gpt-transcribe에 새로 묻는 길 — 유료 호출 전에 닫는다(D163)."""


def timing_provider_for(
    workflow: RawWorkflowConfig, *, sender: Any, cfg: Any, gemini_client: Any
) -> TimingProvider:
    """설정의 시간 모델에 맞는 실제 제공자 — Gemini 문(D92)뿐이다. whisper는 은퇴했다(D163).

    Gemini 클라이언트가 없으면 유료 호출 전에 키 이름을 대고 닫는다. 관찰자·취소는 실제 호출 운반자
    (`sender`)가 가진 것을 그대로 옮긴다(영수증·취소가 한 길이다)."""
    del cfg
    if workflow.models.timing_model not in GEMINI_TRANSCRIBE_PROFILES:
        raise TranscriptionRetired(WHISPER_RETIRED)
    if gemini_client is None:
        raise capabilities.TimingProviderUnavailable(
            f"Gemini 전사 엔진의 키가 없다 — .env에 {ANALYSIS_GEMINI_KEY_ENV}(결제 연결 키)를"
            " 넣고 앱을 다시 켜세요"
        )
    return GeminiTranscribeTimingProvider(
        client=gemini_client,
        observer=getattr(sender, "observer", None),
        cancellation=getattr(sender, "cancellation", None),
    )
