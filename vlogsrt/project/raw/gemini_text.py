"""1단계 Gemini 글자 경로(W-35b, D95) — whisper 청크와 **같은 WAV**를 Gemini 3.5 Transcribe에 묻고
받은 단어를 whisper 줄에 배정한다(`word_text.place`).

gpt 다른 귀(`source_text`, D37 게이트)의 자리를 whisper 원본에서 대신한다 — 원본마다 고정된 글자
귀(`text_ear`)가 `gemini`일 때만 이 길로 온다. 원본 자막이 심기기 **전에** 끝난다.

지키는 선:

- **시각·아이디·발화 수는 whisper 그대로다**(규칙 1·5). 단어 시각은 어느 줄의 글자인지 정하는 데만
  쓴다 — 계획(`word_text`)에 시각 칸이 없고, 얹은 뒤 골격이 같은지 다시 본다.
- **어느 줄도 담지 않은 단어와 시각 없는 단어는 붙이지 않는다**(규칙 1, D37과 같은 선) — 청크
  번호·앞뒤 발화와 함께 기록(`asr_text.unplaced`)에만 남기고 WARN한다(규칙 9).
- **힌트가 없다**(규칙 6, D92) — 요청은 시간 엔진과 같은 문(`gemini_timing_request` →
  `request_gemini_transcription`)이고 언어 잠금(`ko-KR`)뿐이다.
- **실패는 whisper 글자 그대로 + WARN**(D37·D39와 같다). 실패·받아 줄 수 없는 응답은 캐시하지
  않는다.
- **온라인 요청은 승인이 묶은 것만 나간다**(D59 19d와 같은 문) — 1단계 승인이 글자 모델(이 엔진)·
  요청 신원·요청 상한·청크 열쇠를 묶어야 실제 제공자가 생기고, 청크마다 그것을 다시 본다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from vlogsrt import log, oscompat
from vlogsrt.models import Chunk
from vlogsrt.project.raw import request_log, source_text, word_text
from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval, require_live_asr_approval
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import AudioChunkBinding
from vlogsrt.project.raw.config import (
    TIMING_ENGINE_GEMINI,
    TIMING_ENGINES,
    GeminiTextSettings,
    RawWorkflowConfig,
    gemini_text_problems,
    timing_engine_view,
)
from vlogsrt.project.raw.gemini_transcribe_parse import ParsedWord, parse_transcription
from vlogsrt.project.raw.source_text import (
    STATUS_NOT_REQUESTED,
    STATUS_RECEIVED,
    STATUS_TIMING_FAILED,
    STATUS_UNAVAILABLE,
    TEXT_ORIGINS,
    TextPassResult,
)
from vlogsrt.project.raw.source_transcript import TEXT_SOURCE_WHISPER
from vlogsrt.project.raw.understanding import (
    AsrChunkOutcome,
    AsrEvidence,
    SampleSpan,
    SelectedAudio,
    UtteranceEvidence,
)
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE, AnalysisApprovalError
from vlogsrt.task_runtime import check_cancel, provider_error

TEXT_CACHE_DIRNAME = "source-asr-gemini-text"
"""Gemini 글자 캐시의 자리 — `cache/source-asr-gemini-text/<원본>/<열쇠>.json`(gpt 글자와 따로)."""

TEXT_REQUEST_STAGE = "asr-gemini-text"
"""요청 기록(`analysis_requests.stage`)의 종류 — whisper `asr`·gpt 글자 `asr-text`와 따로 센다."""

IDENTITY_SCHEMA = "raw-source-gemini-text-identity/v1"

REASON_PLACED = "placed"
"""Gemini 단어가 이 줄에 배정됐다 — 글자는 Gemini 것이다(D95)."""
REASON_NO_WORDS = "no_words"
"""Gemini가 이 줄에서 말을 듣지 못했다 — whisper 글자 그대로, 지우지 않는다(규칙 9)."""

SOURCE_GEMINI = word_text.SOURCE_GEMINI


class GeminiWordsProvider(Protocol):
    """Gemini 글자 경로의 주입 경계 — 받은 응답(JSON 객체)을 그대로 돌려준다(해석은 이 모듈)."""

    def transcription(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> Mapping[str, Any]: ...


class WordReservationGate(Protocol):
    def reserve(self, *, count: int, key: str) -> None: ...


class ReplayWordsMissing(RuntimeError):
    """재생할 Gemini 응답 선언이 없다 — 그 청크는 글자 경로 실패(whisper 글자 그대로)로 남는다."""


@dataclass(frozen=True, slots=True)
class WordPassRequest:
    """Gemini 글자 경로 한 번의 재료 — 설정·전사 설정·날짜·요청 신원·자리·출처(실제/재생)."""

    settings: GeminiTextSettings
    workflow: RawWorkflowConfig
    on: date
    request_id: str
    workdir: Path
    cache_dir: Path
    origin: str

    @property
    def engine_workflow(self) -> RawWorkflowConfig:
        """Gemini 전사 엔진으로 듣는 설정 보기 — 요청 문(`gemini_timing_request`)이 읽는다."""
        return timing_engine_view(self.workflow, TIMING_ENGINES[TIMING_ENGINE_GEMINI])


@dataclass(frozen=True, slots=True)
class WordChunkOutcome:
    """청크 하나의 Gemini 글자 결과 — 받은 단어이거나 그러지 못한 이유다."""

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    key: str
    status: str
    words: tuple[ParsedWord, ...] = ()
    unplaced_chars: int = 0
    cached: bool = False
    error: str = ""


def text_identity(settings: GeminiTextSettings) -> str:
    """Gemini 글자 요청의 신원 — schema·모델·언어 잠금·배정 판. WAV·범위·출처는 없다.

    1단계 승인(`AsrPhaseApproval.text_identity`)과 결속 꼬리표(`gemini-text/<신원>`)가 이 값을
    묶는다."""
    identity = {
        "identity": IDENTITY_SCHEMA,
        "schema": settings.schema,
        "model": settings.model,
        "languages": list(settings.languages),
        "version": settings.version,
    }
    body = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def chunk_key(wav_sha256: str, settings: GeminiTextSettings, span: SampleSpan, origin: str) -> str:
    """Gemini 글자 캐시 열쇠 — 출처·WAV·모델·언어·배정 판·표본 범위(gpt 글자 열쇠와 겹치지 않는다).

    출처를 싣는 까닭은 gpt 글자 열쇠와 같다 — 재생 답을 실제 실행이 읽지 못한다."""
    if origin not in TEXT_ORIGINS:
        raise ValueError(f"글자 답의 출처는 {TEXT_ORIGINS} 중 하나다: {origin!r}")
    identity = {
        "schema": settings.schema,
        "origin": origin,
        "wav": wav_sha256,
        "model": settings.model,
        "languages": list(settings.languages),
        "version": settings.version,
        "start": span.start,
        "end": span.end,
    }
    body = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def call_key(binding: AudioChunkBinding, settings: GeminiTextSettings) -> str:
    """전송 순간의 청크로 다시 잰 캐시 열쇠(출처 live) — 승인이 묶은 청크 집합과 대조한다."""
    span = SampleSpan(int(binding.sample_start), int(binding.sample_end))
    return chunk_key(binding.audio_sha256, settings, span, ORIGIN_LIVE)


def cached_answer_exists(cache_dir: Path, key: str) -> bool:
    """견적용 확인 — 그 열쇠의 성공 답이 캐시에 있는가(읽지 못하면 WARN하고 없다 — 상한 쪽)."""
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return False
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warn(
            f"Gemini 글자 캐시를 읽지 못했다 — 견적은 묻는 것으로 셉니다: {path.name} ({error})"
        )
        return False
    return (
        isinstance(body, dict) and body.get("key") == key and isinstance(body.get("response"), dict)
    )


# ─────────────────────────────────────────────────────────────
#  묻기 — 캐시가 먼저, 문이 없으면 묻지 않는다
# ─────────────────────────────────────────────────────────────


def run_word_pass(
    audio: SelectedAudio,
    evidence: AsrEvidence,
    request: WordPassRequest,
    provider: GeminiWordsProvider | None,
) -> tuple[WordChunkOutcome, ...]:
    """청크마다 Gemini 단어를 구한다 — 캐시, 그다음 제공자. 제공자가 없으면 보내지 않는다."""
    problems = gemini_text_problems(request.settings)
    if problems:
        raise ValueError("Gemini 글자 경로 설정이 계약을 어긴다: " + "; ".join(problems))
    outcomes: list[WordChunkOutcome] = []
    for chunk in evidence.chunks:
        check_cancel()
        outcomes.append(_word_chunk(audio, chunk, request, provider))
    return tuple(outcomes)


def _word_chunk(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    request: WordPassRequest,
    provider: GeminiWordsProvider | None,
) -> WordChunkOutcome:
    span = SampleSpan(chunk.sample_start, chunk.sample_end)
    base = WordChunkOutcome(
        chunk_index=chunk.chunk_index,
        sample_start=chunk.sample_start,
        sample_end=chunk.sample_end,
        wav_sha256=chunk.wav_sha256,
        key=chunk_key(chunk.wav_sha256, request.settings, span, request.origin),
        status=STATUS_NOT_REQUESTED,
    )
    if not chunk.succeeded:
        return replace(base, status=STATUS_TIMING_FAILED)
    duration = Fraction(chunk.sample_end - chunk.sample_start, audio.sample_rate)
    cached = _cached(request.cache_dir / f"{base.key}.json", base.key)
    if cached is not None:
        parsed = _parsed(cached, duration, chunk.chunk_index)
        if parsed is not None:
            return replace(parsed_outcome(base, parsed), cached=True)
    if provider is None:
        return base
    return _ask(audio, chunk, request, provider, base, duration)


def _ask(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    request: WordPassRequest,
    provider: GeminiWordsProvider,
    base: WordChunkOutcome,
    duration: Fraction,
) -> WordChunkOutcome:
    """제공자에게 묻는다 — 실패는 이 청크의 기록(whisper 글자 그대로)이다. 다음 청크는 묻는다."""
    attempt: str | None = None
    try:
        engine_chunk, binding = _same_wav(audio, chunk, request)
        attempt = request_log.begin(TEXT_REQUEST_STAGE, base.key)
        payload = provider.transcription(
            binding=binding,
            chunk=engine_chunk,
            settings=request.engine_workflow,
            on=request.on,
            stem=engine_chunk.path.stem,
            source=audio.source_id,
        )
        request_log.finish(attempt, received=True)
    except Exception as exc:  # noqa: BLE001 - 글자 실패는 이 청크의 기록이다(D37·D39)
        category, note, uncertain = provider_error(exc)
        request_log.finish(attempt, received=not uncertain, note=category)
        check_cancel()
        return _unavailable(base, note)
    try:
        parsed = parse_transcription(payload, chunk_duration=duration)
    except ValueError as error:  # 받아 줄 수 없는 응답 — 캐시하지 않는다(다음 실행이 다시 묻는다)
        return _unavailable(base, f"응답을 받아 줄 수 없다: {error}")
    _write_cache(request.cache_dir, base.key, request.settings, payload)
    return parsed_outcome(base, parsed)


def _unavailable(base: WordChunkOutcome, note: str) -> WordChunkOutcome:
    log.warn(
        f"1단계 Gemini 글자 경로 청크 {base.chunk_index} 실패 — whisper 글자 그대로 둡니다: {note}"
    )
    return replace(base, status=STATUS_UNAVAILABLE, error=note)


def parsed_outcome(base: WordChunkOutcome, parsed: Any) -> WordChunkOutcome:
    return replace(
        base,
        status=STATUS_RECEIVED,
        words=tuple(parsed.words),
        unplaced_chars=int(parsed.unplaced_chars),
    )


def _parsed(payload: Mapping[str, Any], duration: Fraction, index: int) -> Any | None:
    """캐시한 응답을 다시 해석한다 — 받아 줄 수 없으면 WARN하고 없는 것으로 친다(다시 묻는다)."""
    try:
        return parse_transcription(payload, chunk_duration=duration)
    except ValueError as error:
        log.warn(f"Gemini 글자 캐시를 해석하지 못했다 — 없는 것으로 칩니다: 청크 {index} ({error})")
        return None


def _same_wav(
    audio: SelectedAudio, chunk: AsrChunkOutcome, request: WordPassRequest
) -> tuple[Chunk, AudioChunkBinding]:
    """whisper가 들은 **같은 WAV**의 신원 — gpt 글자 경로와 같은 확인이다(요청 아이디만 다르다)."""
    engine_chunk, binding = source_text.same_wav(
        audio, chunk, request.workdir, f"{request.request_id}:gemini-text-{chunk.chunk_index:04d}"
    )
    return engine_chunk, binding


def _cached(path: Path, key: str) -> Mapping[str, Any] | None:
    """성공 캐시의 응답 — 읽지 못한 항목은 없는 것으로 치고 경고한다(조용히 넘기지 않는다)."""
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("key") == key and isinstance(body.get("response"), dict):
            return dict(body["response"])
        log.warn(f"Gemini 글자 캐시의 열쇠·모양이 어긋났다 — 없는 것으로 칩니다: {path.name}")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        log.warn(f"Gemini 글자 캐시를 읽지 못했다 — 없는 것으로 칩니다: {path.name} ({error})")
    return None


def _write_cache(
    directory: Path, key: str, settings: GeminiTextSettings, payload: Mapping[str, Any]
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{key}.json"
    temp = target.with_suffix(".tmp")
    body = {
        "key": key,
        "schema": settings.schema,
        "model": settings.model,
        "version": settings.version,
        "response": dict(payload),
    }
    temp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    oscompat.replace(temp, target)


# ─────────────────────────────────────────────────────────────
#  얹기 — 청크마다 따로, 단어 가운데 자리로 줄을 고른다
# ─────────────────────────────────────────────────────────────


def apply_word_pass(
    utterances: Sequence[UtteranceEvidence],
    outcomes: Sequence[WordChunkOutcome],
    request: WordPassRequest,
    sample_rate: int,
    previous: Sequence[Mapping[str, object]] = (),
) -> TextPassResult:
    """받은 단어를 청크마다 배정해 발화에 얹는다 — 시각·아이디·순서·수는 그대로다(규칙 5).

    답이 없는 청크(실패·묻지 않음)는 지금 심긴 같은 whisper 답의 글자 증거를 잇는다(gpt 글자 경로와
    같은 이어 가기 — 더 나은 글자를 조용히 whisper 글자로 되돌리지 않는다)."""
    members: dict[int, list[UtteranceEvidence]] = {}
    for item in utterances:
        members.setdefault(item.chunk_index, []).append(item)
    plans = {
        outcome.chunk_index: _plan(outcome, members.get(outcome.chunk_index, []), sample_rate)
        for outcome in outcomes
        if outcome.status == STATUS_RECEIVED
    }
    lines = {line.line_id: line for plan in plans.values() for line in plan.lines}
    placed = tuple(
        _with_words(item, lines.get(item.utterance_id), request)
        if item.chunk_index in plans
        else item  # 답이 없는 청크 — 증거 칸 없음(이어 가기가 채울 수 있다)
        for item in utterances
    )
    applied, carried, dropped = source_text.carry_previous(
        placed, set(plans), previous, request.origin
    )
    source_text.require_same_skeleton(utterances, applied)
    record = _record(outcomes, plans, members, applied, request)
    record.update(
        carried=len(carried),
        carried_replaced=sum(item.retext_reason == REASON_PLACED for item in carried),
        dropped_cross_checked=dropped,
    )
    _report(record)
    return TextPassResult(utterances=applied, record=record)


def _plan(
    outcome: WordChunkOutcome, members: Sequence[UtteranceEvidence], sample_rate: int
) -> word_text.WordTextPlan:
    """청크 하나 — 그 청크의 whisper 줄을 청크 국소 시계(같은 WAV)로 옮겨 단어를 배정한다."""
    lines = [
        word_text.TimedLine(
            line_id=item.utterance_id,
            start=Fraction(item.start_sample - outcome.sample_start, sample_rate),
            end=Fraction(item.end_sample - outcome.sample_start, sample_rate),
            text=item.text,
        )
        for item in members
    ]
    return word_text.place(lines, outcome.words)


def _with_words(
    item: UtteranceEvidence, line: word_text.LineText | None, request: WordPassRequest
) -> UtteranceEvidence:
    """발화 하나의 글자와 증거 — whisper 글자는 언제나 남는다(`whisper_text`)."""
    if line is not None and line.source == SOURCE_GEMINI and line.text:
        return replace(
            item,
            text=line.text,
            whisper_text=item.text,
            text_source=request.settings.model,
            retext_reason=REASON_PLACED,
            alt_text="",
            text_origin=request.origin,
        )
    return replace(
        item,
        whisper_text=item.text,
        text_source=TEXT_SOURCE_WHISPER,
        retext_reason=REASON_NO_WORDS,
        alt_text="",
        text_origin=request.origin,
    )


# ─────────────────────────────────────────────────────────────
#  기록 — 1단계 기록의 `asr_text` 칸(gpt 글자 경로와 같은 자리, 모양은 이 경로의 것)
# ─────────────────────────────────────────────────────────────


def _record(
    outcomes: Sequence[WordChunkOutcome],
    plans: Mapping[int, word_text.WordTextPlan],
    members: Mapping[int, Sequence[UtteranceEvidence]],
    applied: Sequence[UtteranceEvidence],
    request: WordPassRequest,
) -> dict[str, object]:
    decided = [item for item in applied if item.chunk_index in plans]
    return {
        "schema": request.settings.schema,
        "origin": request.origin,
        "model": request.settings.model,
        "languages": list(request.settings.languages),
        "version": request.settings.version,
        "status": source_text.pass_status(outcome.status for outcome in outcomes),
        "replaced": sum(item.retext_reason == REASON_PLACED for item in decided),
        "kept": sum(item.retext_reason != REASON_PLACED for item in decided),
        "no_words": sum(item.retext_reason == REASON_NO_WORDS for item in decided),
        "chunks": [_chunk_record(outcome, plans.get(outcome.chunk_index)) for outcome in outcomes],
        "unplaced": [
            _unplaced_record(index, word)
            for index, plan in sorted(plans.items())
            for word in plan.unplaced
        ],
    }


def _chunk_record(
    outcome: WordChunkOutcome, plan: word_text.WordTextPlan | None
) -> dict[str, object]:
    body: dict[str, object] = {
        "chunk_index": outcome.chunk_index,
        "sample_start": outcome.sample_start,
        "sample_end": outcome.sample_end,
        "wav_sha256": outcome.wav_sha256,
        "key": outcome.key,
        "status": outcome.status,
        "cached": outcome.cached,
        "error": outcome.error,
        "words": [_word_record(word) for word in outcome.words],
        "unplaced_chars": outcome.unplaced_chars,
    }
    if plan is not None:
        body.update(
            placed=[
                {"utterance_id": line.line_id, "words": list(line.words)}
                for line in plan.lines
                if line.words
            ],
            untimed=plan.untimed,
        )
    return body


def _word_record(word: ParsedWord) -> dict[str, object]:
    """받은 단어 하나 — 시각은 청크 국소 초의 정확 유리수 글(없으면 null)."""
    return {
        "text": word.text,
        "display": word.display,
        "start": None if word.start is None else _fraction_text(word.start),
        "end": None if word.end is None else _fraction_text(word.end),
    }


def _unplaced_record(chunk_index: int, word: word_text.UnplacedWord) -> dict[str, object]:
    """어느 줄에도 붙지 않은 단어 — 앞뒤 발화는 자리를 가리킬 뿐이다(규칙 1)."""
    return {
        "chunk_index": chunk_index,
        "index": word.index,
        "text": word.text,
        "chars": len(word.text),
        "start": None if word.start is None else _fraction_text(word.start),
        "end": None if word.end is None else _fraction_text(word.end),
        "before": word.before,
        "after": word.after,
    }


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _report(record: Mapping[str, Any]) -> None:
    """한 줄 요약과, 붙이지 않은 단어·Gemini가 못 들은 줄이 있으면 WARN(규칙 9)."""
    if record["status"] != STATUS_NOT_REQUESTED:
        log.out(
            f"1단계 Gemini 글자 경로: {record['status']} · Gemini 글자 {record['replaced']}줄"
            f" · whisper 글자 그대로 {record['kept']}줄"
        )
    if record["no_words"]:
        log.warn(
            f"1단계 Gemini 글자 경로: Gemini가 말을 듣지 못한 줄 {record['no_words']}개 — whisper"
            " 글자 그대로 둡니다 (D95)"
        )
    unplaced = record["unplaced"]
    if unplaced:
        chars = sum(int(item["chars"]) for item in unplaced)
        log.warn(
            f"1단계 Gemini 글자 경로: 어느 줄에도 들지 않은 단어 {len(unplaced)}개 {chars}자 —"
            " 자막에 싣지 않고 기록(asr_text.unplaced)에만 남깁니다 (D95)"
        )
    if record["carried"]:
        log.out(
            f"1단계 Gemini 글자 경로: 답이 없는 청크에서 이미 얹힌 {record['carried']}줄을 그대로"
            " 둡니다(같은 whisper 답·같은 출처)"
        )
    if record["dropped_cross_checked"]:
        log.warn(
            f"1단계 Gemini 글자 경로: 글자가 얹혔던 {record['dropped_cross_checked']}줄은 whisper"
            " 답이 달라져 이어 갈 수 없어 whisper 글자로 심습니다"
        )


# ─────────────────────────────────────────────────────────────
#  제공자 — 승인된 실제 전송과 재생
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApprovedPhaseWordProvider:
    """Gemini 글자 경로의 전송 경계 — 승인이 묶은 글자 모델·신원·청크와 1단계 승인을 청크마다 본다.

    `source_text.ApprovedPhaseTextProvider`와 같은 모양이다. 예약 문(승인이 묶은 글자 요청 상한)은
    whisper와 따로 센다."""

    inner: GeminiWordsProvider
    workflow: RawWorkflowConfig
    understanding: RawUnderstandingSettings
    audio: SelectedAudio
    approval: AsrPhaseApproval
    settings: GeminiTextSettings
    approved_identity: str
    gate: WordReservationGate
    approved_keys: frozenset[str] | None = None

    def transcription(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> Mapping[str, Any]:
        self._require_open(binding, settings, on)
        self.gate.reserve(count=1, key=str(binding.request_id))
        return self.inner.transcription(
            binding=binding, chunk=chunk, settings=settings, on=on, stem=stem, source=source
        )

    def _require_open(
        self, binding: AudioChunkBinding, settings: RawWorkflowConfig, on: date
    ) -> None:
        require_live_asr_approval(
            self.approval,
            workflow=self.workflow,
            understanding=self.understanding,
            source_sha256=self.audio.source_sha256,
            on=on,
        )
        if settings.network_mode != "online":
            raise AnalysisApprovalError(
                "네트워크 모드가 online이 아니다 — 글자 경로 전송 문이 닫혀 있다"
            )
        if self.approval.text_model != self.settings.model:
            raise AnalysisApprovalError("승인이 본 글자 모델과 Gemini 글자 경로 모델이 다르다")
        if settings.models.timing_model != self.settings.model:
            raise AnalysisApprovalError("요청 설정의 전사 모델이 Gemini 글자 경로 모델이 아니다")
        if text_identity(self.settings) != self.approved_identity:
            raise AnalysisApprovalError("승인이 본 Gemini 글자 요청과 지금의 요청이 다르다")
        if (
            self.approved_keys is not None
            and call_key(binding, self.settings) not in self.approved_keys
        ):
            raise AnalysisApprovalError("견적에 없던 청크다 — 승인 밖이라 보내지 않는다")
        if self.audio.audio_stream_index != self.approval.audio_stream_index:
            raise AnalysisApprovalError("승인이 본 오디오 스트림과 지금의 선택이 다르다")
        if binding.sample_start < 0 or binding.sample_end > self.audio.sample_count:
            raise AnalysisApprovalError(
                f"청크 범위가 선택 오디오 밖이다: [{binding.sample_start}, {binding.sample_end})"
                f" / {self.audio.sample_count} 샘플"
            )


@dataclass(frozen=True, slots=True)
class ReplayWordProvider:
    """재생 Gemini 글자 제공자 — 선언된 응답만 돌려준다. 없으면 실패다(지어내지 않는다).

    `exact`는 WAV sha로 찾는 정밀 선언이고 앞선다. `declared`는 이 원본의 청크 번호 → 응답이다."""

    exact: Mapping[str, Mapping[str, Any]]
    declared: Mapping[int, Mapping[str, Any]] | None = None

    def transcription(
        self,
        *,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> Mapping[str, Any]:
        payload = self.exact.get(binding.audio_sha256)
        if payload is None and self.declared is not None:
            payload = self.declared.get(binding.chunk_index)
        if payload is None:
            raise ReplayWordsMissing(f"재생할 Gemini 응답 선언이 없다 — 청크 {binding.request_id}")
        return payload


__all__ = [
    "REASON_NO_WORDS",
    "REASON_PLACED",
    "TEXT_CACHE_DIRNAME",
    "TEXT_REQUEST_STAGE",
    "ApprovedPhaseWordProvider",
    "GeminiWordsProvider",
    "ReplayWordProvider",
    "ReplayWordsMissing",
    "WordChunkOutcome",
    "WordPassRequest",
    "apply_word_pass",
    "cached_answer_exists",
    "call_key",
    "chunk_key",
    "run_word_pass",
    "text_identity",
]
