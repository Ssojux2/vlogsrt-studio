"""1단계 글자 경로(D59 19c) — whisper 청크와 **같은 WAV**를 두 번째 귀(gpt-transcribe)에 묻는다.

받은 글자는 D37 세그먼트 게이트(`vlogsrt.retext.plan`)를 통과한 발화에만 얹는다. whisper가 준 시각과
경계는 그대로 두고 글자만 바꾼다 — 원본 자막이 심기기(`source_transcript.seed`) **전에** 끝난다.

지키는 선:

- **시각·아이디·발화 수는 whisper 그대로다**(규칙 1·5). 계획은 청크마다 따로 세운다 — 청크 하나에
  `ChunkText` 하나, 그 청크의 whisper 세그먼트만. 여러 청크를 한꺼번에 넘기면 `retext`가 시작
  시각으로 청크를 고르는데, 청크 끝으로 닫힌 세그먼트(180/180, D55)가 다음 청크로 넘어간다.
- **세그먼트 사이에 낀 글자는 어느 발화에도 붙이지 않는다**(규칙 1). 그 글자에는 시각이 없다 —
  청크 번호와 앞뒤 발화를 단 증거로만 남기고 WARN한다(규칙 9).
- **힌트는 두 번째 귀에게만 간다**(규칙 6). 고정 문맥·용어집·언어는 이 요청 하나에만 실린다.
- **실패는 whisper 글자 그대로 + WARN**(D37·D39). 실패는 캐시하지 않고 `asr.chunks`에도 넣지 않는다
  — 1단계 기록의 재사용 판정(`_phase_reusable`)이 글자 실패 때문에 whisper를 다시 사지 않는다.
- **온라인 글자 요청은 승인이 묶은 것만 나간다(D59 19d).** 1단계 승인(`AsrPhaseApproval` v2)이 글자
  모델·요청 신원(`text_identity`)·요청 상한을 묶어야 실제 제공자가 생기고, 청크마다 그 셋을 다시
  본다. 캐시에 이미 있는 답은 그대로 쓴다(무료) — 같은 whisper 캐시와 같은 글자 캐시는 같은 발화를
  낳는다(심기가 멱등이다).
- **접은 글자가 같으면 바꾸지 않는다(D59 19d).** 공백·부호만 다른 답은 `agreed`이고 whisper 글자를
  둔다 — 자막 지문은 글자가 실제로 바뀔 때만 바뀐다(접기는 retext 비교와 같은 `strip_for_compare`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, Protocol

from vlogsrt import log, oscompat, retext
from vlogsrt.config import Config
from vlogsrt.hangul import strip_for_compare
from vlogsrt.models import Chunk, RetextRecord, RetextRejection, RetextUnplaced, Segment
from vlogsrt.project.raw import request_log, source_transcript
from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval, require_live_asr_approval
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.caption_provider import (
    AudioChunkBinding,
    TextTranscript,
    audio_chunk_binding,
    parse_text_response,
    text_transcription_request,
)
from vlogsrt.project.raw.config import (
    TEXT_PASS_CANDIDATE,
    RawWorkflowConfig,
    SourceTextSettings,
    source_text_problems,
    timing_engine_for_model,
)
from vlogsrt.project.raw.glossary import glossary_sha, validated
from vlogsrt.project.raw.source_transcript import TEXT_SOURCE_WHISPER
from vlogsrt.project.raw.understanding import (
    TEXT_ORIGIN_FIELD,
    AsrChunkOutcome,
    AsrEvidence,
    SampleSpan,
    SelectedAudio,
    UtteranceEvidence,
    chunk_wav_path,
    utterance_text_evidence,
)
from vlogsrt.project.raw.understanding_luna import (
    ORIGIN_LIVE,
    ORIGIN_REPLAY,
    AnalysisApprovalError,
)
from vlogsrt.task_runtime import check_cancel, provider_error

TEXT_CACHE_DIRNAME = "source-asr-text"
"""글자 캐시의 자리 — `cache/source-asr-text/<원본>/<열쇠>.json`. whisper 캐시와 따로다."""

TEXT_REQUEST_STAGE = "asr-text"
"""요청 기록(`analysis_requests.stage`)의 종류 — whisper의 `asr`와 따로 센다."""

STATUS_RECEIVED = "received"  # 답을 받았다(캐시 포함)
STATUS_UNAVAILABLE = "unavailable"  # 묻다가 실패했다 — whisper 글자 그대로, WARN, 캐시하지 않음
STATUS_NOT_REQUESTED = "not_requested"  # 묻는 문이 없고 캐시에도 없다(19d 전 온라인·픽스처 없음)
STATUS_TIMING_FAILED = "timing_failed"  # whisper 청크가 실패했다 — 글자를 둘 시각이 없다

PASS_APPLIED = "applied"
PASS_PARTIAL = "partial"

REASON_ACCEPTED = "accepted"  # 게이트를 지나 글자를 바꿨다
REASON_AGREED = "agreed"  # 게이트를 지났지만 두 귀가 같은 글자를 들었다 — whisper 글자 그대로
REASON_UNDECIDED = "undecided"  # 계획에 판정이 없다(되풀이 제외 같은 자리) — whisper 글자 그대로
REASON_CANDIDATE = source_transcript.REASON_CANDIDATE  # Gemini 원본 — 후보로만 남았다(D92)
"""게이트를 지났지만 시간 엔진이 글자를 바꾸지 않는 엔진이다(Gemini, D92) — 후보(`alt_text`)로만."""


class SourceTextProvider(Protocol):
    """1단계 글자 경로의 주입 경계 — 요청 하나에만 실리는 힌트(용어집·언어)를 받는다."""

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
        keywords: tuple[str, ...] | None = None,
        languages: tuple[str, ...] | None = None,
    ) -> TextTranscript: ...


class TextReservationGate(Protocol):
    """글자 요청 예약 문 — whisper 예약과 따로 센다."""

    def reserve(self, *, count: int, key: str) -> None: ...


class ReplayTextMissing(RuntimeError):
    """재생할 글자 선언이 없다 — 그 청크는 글자 경로 실패(whisper 글자 그대로)로 남는다."""


TEXT_ORIGINS = (ORIGIN_LIVE, ORIGIN_REPLAY)
"""글자 답의 출처 — 캐시 열쇠에 실린다. 재생(픽스처) 답은 실제 실행이 결코 읽지 못한다."""


@dataclass(frozen=True, slots=True)
class TextPassRequest:
    """글자 경로 한 번의 재료 — 설정·용어집·전사 설정·날짜·요청 신원·자리·출처(실제/재생)."""

    settings: SourceTextSettings
    glossary: tuple[str, ...]
    workflow: RawWorkflowConfig
    on: date
    request_id: str
    workdir: Path
    cache_dir: Path
    origin: str

    @property
    def glossary_sha(self) -> str:
        return glossary_sha(self.glossary)


@dataclass(frozen=True, slots=True)
class TextChunkOutcome:
    """청크 하나의 글자 경로 결과 — 받은 글자이거나 그러지 못한 이유다(시각 칸은 없다)."""

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    key: str
    status: str
    text: str = ""
    languages: tuple[str, ...] = ()
    cached: bool = False
    error: str = ""


@dataclass(frozen=True, slots=True)
class TextPassResult:
    """글자를 얹은 발화와 1단계 기록에 실을 글자 경로 기록(`asr_text`)."""

    utterances: tuple[UtteranceEvidence, ...]
    record: dict[str, object]


def text_chunk_key(
    wav_sha256: str,
    settings: SourceTextSettings,
    glossary_digest: str,
    span: SampleSpan,
    origin: str,
) -> str:
    """글자 캐시 열쇠 — 출처·WAV·모델·언어·문맥·용어집·표본 범위. whisper 열쇠와 겹치지 않는다.

    출처(실제/재생)를 싣는 것은 whisper 열쇠가 제공자 이름을 싣는 것과 같은 이유다 — 재생 픽스처의
    답이 같은 자리에 캐시되면 나중의 실제 실행이 그것을 두 번째 귀의 글자로 읽어 얹는다(검증 지적).
    다시 듣기 차례(D59 19b)는 싣지 않는다 — 차례는 whisper의 온도 문제이고, 같은 WAV를 같은
    요청으로 물은 글자는 차례와 무관하다.
    """
    if origin not in TEXT_ORIGINS:
        raise ValueError(f"글자 답의 출처는 {TEXT_ORIGINS} 중 하나다: {origin!r}")
    identity = {
        "schema": settings.schema,
        "origin": origin,
        "wav": wav_sha256,
        "model": settings.model,
        "languages": list(settings.languages),
        "prompt": settings.prompt,
        "glossary": glossary_digest,
        "start": span.start,
        "end": span.end,
    }
    body = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


TEXT_IDENTITY_SCHEMA = "raw-source-text-identity/v1"


def text_identity(settings: SourceTextSettings, glossary_digest: str) -> str:
    """글자 요청의 신원(D59 19d) — schema·모델·언어·문맥·용어집 sha. WAV·범위·출처는 없다.

    1단계 승인(`AsrPhaseApproval.text_identity`)과 교차 확인 결속 꼬리표가 이 값을 묶는다 —
    용어집이나 문맥이 승인 뒤에 바뀌면 그 승인으로는 글자를 묻지 않는다."""
    identity = {
        "identity": TEXT_IDENTITY_SCHEMA,
        "schema": settings.schema,
        "model": settings.model,
        "languages": list(settings.languages),
        "prompt": settings.prompt,
        "glossary": glossary_digest,
    }
    body = json.dumps(identity, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def call_identity(
    model: str,
    prompt: str,
    keywords: tuple[str, ...] | None,
    languages: tuple[str, ...] | None,
) -> str:
    """전송 순간의 요청 재료로 다시 잰 신원 — 승인이 묶은 `text_identity`와 같아야 보낸다."""
    settings = _call_settings(model, prompt, languages)
    return text_identity(settings, glossary_sha(tuple(keywords or ())))


def call_key(
    binding: AudioChunkBinding,
    model: str,
    prompt: str,
    keywords: tuple[str, ...] | None,
    languages: tuple[str, ...] | None,
) -> str:
    """전송 순간의 청크로 다시 잰 글자 캐시 열쇠(출처 live) — 승인이 묶은 청크 집합과 대조한다."""
    span = SampleSpan(int(binding.sample_start), int(binding.sample_end))
    settings = _call_settings(model, prompt, languages)
    digest = glossary_sha(tuple(keywords or ()))
    return text_chunk_key(binding.audio_sha256, settings, digest, span, ORIGIN_LIVE)


def _call_settings(
    model: str, prompt: str, languages: tuple[str, ...] | None
) -> SourceTextSettings:
    return replace(
        SourceTextSettings(), model=model, languages=tuple(languages or ()), prompt=prompt
    )


def cached_answer_exists(cache_dir: Path, key: str) -> bool:
    """견적용 확인 — 그 열쇠의 성공 답이 캐시에 있는가. 읽지 못하면 WARN하고 없다(묻는 쪽, 상한)."""
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return False
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warn(f"글자 캐시를 읽지 못했다 — 견적은 묻는 것으로 셉니다: {path.name} ({error})")
        return False
    return isinstance(body, dict) and body.get("key") == key and isinstance(body.get("text"), str)


# ─────────────────────────────────────────────────────────────
#  묻기 — 캐시가 먼저, 문이 없으면 묻지 않는다
# ─────────────────────────────────────────────────────────────


def run_text_pass(
    audio: SelectedAudio,
    evidence: AsrEvidence,
    request: TextPassRequest,
    provider: SourceTextProvider | None,
) -> tuple[TextChunkOutcome, ...]:
    """청크마다 글자를 구한다 — 캐시, 그다음 제공자. 제공자가 없으면 아무것도 보내지 않는다."""
    problems = source_text_problems(request.settings)
    if problems:
        raise ValueError("1단계 글자 경로 설정이 계약을 어긴다: " + "; ".join(problems))
    validated(request.glossary)  # 저장된 용어집도 보내기 전에 다시 본다(<·>·줄바꿈)
    outcomes: list[TextChunkOutcome] = []
    for chunk in evidence.chunks:
        check_cancel()
        outcomes.append(_text_chunk(audio, chunk, request, provider))
    return tuple(outcomes)


def _text_chunk(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    request: TextPassRequest,
    provider: SourceTextProvider | None,
) -> TextChunkOutcome:
    span = SampleSpan(chunk.sample_start, chunk.sample_end)
    key = text_chunk_key(
        chunk.wav_sha256, request.settings, request.glossary_sha, span, request.origin
    )
    base = TextChunkOutcome(
        chunk_index=chunk.chunk_index,
        sample_start=chunk.sample_start,
        sample_end=chunk.sample_end,
        wav_sha256=chunk.wav_sha256,
        key=key,
        status=STATUS_NOT_REQUESTED,
    )
    if not chunk.succeeded:
        return replace(base, status=STATUS_TIMING_FAILED)
    cached = _cached(request.cache_dir / f"{key}.json", key)
    if cached is not None:
        return replace(
            base, status=STATUS_RECEIVED, text=cached[0], languages=cached[1], cached=True
        )
    if provider is None:
        return base
    return _ask(audio, chunk, request, provider, base)


def _ask(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    request: TextPassRequest,
    provider: SourceTextProvider,
    base: TextChunkOutcome,
) -> TextChunkOutcome:
    """제공자에게 묻는다 — 실패는 이 청크의 기록(whisper 글자 그대로)이다. 다음 청크는 묻는다."""
    attempt: str | None = None
    try:
        engine_chunk, binding = _same_wav(audio, chunk, request)
        attempt = request_log.begin(TEXT_REQUEST_STAGE, base.key)
        transcript = provider.text_transcript(
            binding=binding,
            chunk=engine_chunk,
            settings=request.workflow,
            on=request.on,
            prompt=request.settings.prompt,
            stem=engine_chunk.path.stem,
            source=audio.source_id,
            keywords=request.glossary,
            languages=request.settings.languages,
        )
        request_log.finish(attempt, received=True)
    except Exception as exc:  # noqa: BLE001 - 글자 실패는 이 청크의 기록이다(D37·D39)
        category, note, uncertain = provider_error(exc)
        request_log.finish(attempt, received=not uncertain, note=category)
        check_cancel()
        log.warn(
            f"1단계 글자 경로 청크 {chunk.chunk_index} 실패 — whisper 글자 그대로 둡니다: {note}"
        )
        return replace(base, status=STATUS_UNAVAILABLE, error=note)
    _write_cache(request.cache_dir, base.key, request.settings, transcript)
    return replace(
        base, status=STATUS_RECEIVED, text=transcript.text, languages=transcript.languages
    )


def _same_wav(
    audio: SelectedAudio, chunk: AsrChunkOutcome, request: TextPassRequest
) -> tuple[Chunk, AudioChunkBinding]:
    return same_wav(
        audio, chunk, request.workdir, f"{request.request_id}:text-{chunk.chunk_index:04d}"
    )


def same_wav(
    audio: SelectedAudio, chunk: AsrChunkOutcome, workdir: Path, request_id: str
) -> tuple[Chunk, AudioChunkBinding]:
    """whisper가 들은 **같은 WAV**의 신원 — 해시가 다르면 다른 소리라 묻지 않는다.

    Gemini 글자 경로(W-35b)도 같은 확인을 쓴다 — 요청 아이디만 다르다."""
    rate = audio.sample_rate
    engine_chunk = Chunk(
        path=chunk_wav_path(workdir, chunk.chunk_index),
        offset=float(Fraction(chunk.sample_start, rate)),
        duration=float(Fraction(chunk.sample_end - chunk.sample_start, rate)),
        index=chunk.chunk_index,
    )
    binding = audio_chunk_binding(
        engine_chunk,
        sample_start=chunk.sample_start,
        sample_end=chunk.sample_end,
        sample_rate=rate,
        request_id=request_id,
    )
    if binding.audio_sha256 != chunk.wav_sha256:
        raise ValueError("청크 WAV가 whisper가 들은 것과 다르다 — 다른 소리에 글자를 얹지 않는다")
    return engine_chunk, binding


def _cached(path: Path, key: str) -> tuple[str, tuple[str, ...]] | None:
    """성공 캐시만 있다 — 읽지 못한 항목은 없는 것으로 치고 경고한다(조용히 넘기지 않는다)."""
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("key") == key and isinstance(body.get("text"), str):
            return body["text"], tuple(str(code) for code in body.get("languages", []))
        log.warn(f"글자 캐시의 열쇠·모양이 어긋났다 — 없는 것으로 칩니다: {path.name}")
    except (OSError, ValueError, TypeError, AttributeError) as error:
        log.warn(f"글자 캐시를 읽지 못했다 — 없는 것으로 칩니다: {path.name} ({error})")
    return None


def _write_cache(
    directory: Path, key: str, settings: SourceTextSettings, transcript: TextTranscript
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{key}.json"
    temp = target.with_suffix(".tmp")
    body = {
        "key": key,
        "schema": settings.schema,
        "model": transcript.model,
        "text": transcript.text,
        "languages": list(transcript.languages),
    }
    temp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    oscompat.replace(temp, target)


# ─────────────────────────────────────────────────────────────
#  얹기 — 청크마다 따로, 게이트를 통과한 발화만
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Decision:
    """청크 하나의 계획을 발화 위치(= whisper 세그먼트 번호)로 찾게 펼친 것."""

    plan: RetextRecord
    texts: Mapping[int, str]
    rejections: Mapping[int, RetextRejection]


def apply_text_pass(
    utterances: Sequence[UtteranceEvidence],
    outcomes: Sequence[TextChunkOutcome],
    request: TextPassRequest,
    sample_rate: int,
    previous: Sequence[Mapping[str, object]] = (),
) -> TextPassResult:
    """받은 글자를 청크마다 계획해 발화에 얹는다 — 시각·아이디·순서·수는 그대로다(규칙 5).

    `previous`는 지금 심겨 있는 고치지 않은 원본 자막의 줄이다. 이번에 글자 답이 없는 청크(용어집을
    바꿔 열쇠가 달라졌다·실패)에서, **같은 whisper 답**(아이디·청크·위치·표본·증거 아이디·whisper
    글자가 모두 같다)에 이미 얹혀 있던 글자 증거는 그대로 이어 간다 — 더 나은 전사를 조용히 whisper
    글자로 되돌리지 않는다(검증 지적). whisper 답이 달라 이을 수 없는 교차 확인 줄은 WARN한다.
    """
    members: dict[int, list[UtteranceEvidence]] = {}
    for item in utterances:
        members.setdefault(item.chunk_index, []).append(item)
    decisions = {
        outcome.chunk_index: _decide(outcome, members.get(outcome.chunk_index, []), sample_rate)
        for outcome in outcomes
        if outcome.status == STATUS_RECEIVED
    }
    candidate_only = (
        timing_engine_for_model(request.workflow.models.timing_model).text_pass
        == TEXT_PASS_CANDIDATE
    )
    applied = tuple(
        _with_evidence(
            item,
            decisions.get(item.chunk_index),
            request.settings.model,
            request.origin,
            candidate_only=candidate_only,
        )
        for item in utterances
    )
    applied, carried, dropped = carry_previous(applied, set(decisions), previous, request.origin)
    require_same_skeleton(utterances, applied)
    record = _record(outcomes, decisions, members, applied, request)
    record.update(
        carried=len(carried),
        carried_replaced=sum(item.retext_reason == REASON_ACCEPTED for item in carried),
        dropped_cross_checked=dropped,
    )
    _report(record)
    return TextPassResult(utterances=applied, record=record)


def carry_previous(
    applied: tuple[UtteranceEvidence, ...],
    decided: set[int],
    previous: Sequence[Mapping[str, object]],
    origin: str,
) -> tuple[tuple[UtteranceEvidence, ...], tuple[UtteranceEvidence, ...], int]:
    """답이 없는 청크의 발화에 지금 심긴 글자 증거를 잇는다 — 같은 whisper 답·같은 출처일 때만.

    출처(D59 19d)는 그 글자 판정이 실제 답(`live`)인지 재생 답(`replay`)인지다 — 다르면 잇지 않는다.
    출처 칸이 없는 19c 줄도 잇지 않는다(어디서 온 글자인지 모른다). 돌려주는 것은 (발화, 이어 간 줄,
    이을 수 없던 교차 확인 줄 수)다."""
    rows = {str(row.get("utterance_id")): row for row in previous if "text_source" in row}
    carried: list[UtteranceEvidence] = []
    dropped = 0
    result: list[UtteranceEvidence] = []
    for item in applied:
        row = rows.get(item.utterance_id)
        if item.chunk_index in decided or row is None:
            result.append(item)
        elif _same_whisper(item, row) and row.get(TEXT_ORIGIN_FIELD) == origin:
            carried.append(_carried(item, row))
            result.append(carried[-1])
        else:
            result.append(item)
            dropped += row.get("text_source") != TEXT_SOURCE_WHISPER
    return tuple(result), tuple(carried), dropped


def _carried(item: UtteranceEvidence, row: Mapping[str, object]) -> UtteranceEvidence:
    """심긴 줄의 글자와 증거 넷을 그대로 — 시각·아이디는 이번 whisper 답의 것(같다)."""
    evidence = utterance_text_evidence(row)
    return replace(
        item,
        text=str(row["text"]),
        whisper_text=evidence["whisper_text"],
        text_source=evidence["text_source"],
        retext_reason=evidence["retext_reason"],
        alt_text=evidence["alt_text"],
        text_origin=evidence.get(TEXT_ORIGIN_FIELD),
    )


def _same_whisper(item: UtteranceEvidence, row: Mapping[str, object]) -> bool:
    """심긴 줄이 이 발화와 같은 whisper 답에서 왔는가 — 다르면 그 글자는 다른 말에 대한 판정이다."""
    return (
        row.get("chunk_index"),
        row.get("position"),
        row.get("start_sample"),
        row.get("end_sample"),
        row.get("evidence_id"),
        row.get("whisper_text"),
    ) == (
        item.chunk_index,
        item.position,
        item.start_sample,
        item.end_sample,
        item.evidence_id,
        item.text,
    )


def _decide(
    outcome: TextChunkOutcome, members: Sequence[UtteranceEvidence], sample_rate: int
) -> _Decision:
    """청크 하나 — 그 청크의 whisper 세그먼트와 그 청크의 글자 하나만 계획에 넘긴다."""
    spine = [
        Segment(id=item.position, start=float(item.start), end=float(item.end), text=item.text)
        for item in members
    ]
    piece = retext.ChunkText(
        index=outcome.chunk_index,
        offset=float(Fraction(outcome.sample_start, sample_rate)),
        text=outcome.text,
    )
    end = float(Fraction(outcome.sample_end, sample_rate))
    plan = retext.plan(spine, [piece], end, (), Config())
    return _Decision(
        plan=plan,
        texts=dict(plan.texts),
        rejections={item.seg_id: item for item in plan.rejections},
    )


def _with_evidence(
    item: UtteranceEvidence,
    decision: _Decision | None,
    model: str,
    origin: str,
    *,
    candidate_only: bool = False,
) -> UtteranceEvidence:
    """발화 하나의 글자와 증거 — whisper 글자는 언제나 남고, 거절된 후보는 `alt_text`로 남는다.

    통과한 답이라도 접은 글자가 whisper와 같으면(공백·부호만 다르다, D59 19d) 바꾸지 않고
    `agreed`다 — 그래야 부호만 고친 교차 확인이 자막 지문을 바꿔 끝난 분석을 낡게 하지 않는다.
    `candidate_only`(Gemini 원본, D92)면 통과한 글자도 자막을 바꾸지 않고 후보로만 남긴다 — 그
    엔진의 글자와 시각은 한 응답이라, 글자만 갈아 끼우면 시각을 준 단어와 어긋난다."""
    if decision is None:
        return item  # 답이 없는 청크 — 19c 이전 모양 그대로(증거 칸 없음)
    accepted = decision.texts.get(item.position)
    differs = accepted is not None and strip_for_compare(accepted) != strip_for_compare(item.text)
    if differs and candidate_only:
        return replace(
            item,
            whisper_text=item.text,
            text_source=TEXT_SOURCE_WHISPER,
            retext_reason=REASON_CANDIDATE,
            alt_text=str(accepted),
            text_origin=origin,
        )
    if accepted is not None and differs:
        return replace(
            item,
            text=accepted,
            whisper_text=item.text,
            text_source=model,
            retext_reason=REASON_ACCEPTED,
            alt_text="",
            text_origin=origin,
        )
    rejection = decision.rejections.get(item.position)
    if accepted is not None:
        reason = REASON_AGREED
    else:
        reason = REASON_UNDECIDED if rejection is None else rejection.reason
    return replace(
        item,
        whisper_text=item.text,
        text_source=TEXT_SOURCE_WHISPER,
        retext_reason=reason,
        alt_text="" if rejection is None else rejection.candidate,
        text_origin=origin,
    )


def _skeleton(item: UtteranceEvidence) -> tuple[object, ...]:
    return (
        item.utterance_id,
        item.chunk_index,
        item.position,
        item.start,
        item.end,
        item.start_sample,
        item.end_sample,
        item.evidence_id,
    )


def require_same_skeleton(
    before: Sequence[UtteranceEvidence], after: Sequence[UtteranceEvidence]
) -> None:
    """글자 경로는 시각·아이디·순서·수를 못 건드린다(규칙 5) — 어긋나면 심기 전에 멈춘다."""
    if [_skeleton(item) for item in before] != [_skeleton(item) for item in after]:
        raise RuntimeError(
            "글자 경로가 발화의 시각·아이디·수를 바꿨다 — 규칙 5 위반이라 심지 않는다"
        )


# ─────────────────────────────────────────────────────────────
#  기록 — 1단계 기록의 `asr_text` 칸(`asr.chunks`와 따로)
# ─────────────────────────────────────────────────────────────


def _record(
    outcomes: Sequence[TextChunkOutcome],
    decisions: Mapping[int, _Decision],
    members: Mapping[int, Sequence[UtteranceEvidence]],
    applied: Sequence[UtteranceEvidence],
    request: TextPassRequest,
) -> dict[str, object]:
    return {
        "schema": request.settings.schema,
        "origin": request.origin,
        "model": request.settings.model,
        "languages": list(request.settings.languages),
        "prompt": request.settings.prompt,
        "glossary_sha": request.glossary_sha,
        "glossary_terms": len(request.glossary),
        "status": pass_status(outcome.status for outcome in outcomes),
        "replaced": sum(
            item.chunk_index in decisions and item.retext_reason == REASON_ACCEPTED
            for item in applied
        ),
        "kept": sum(
            item.chunk_index in decisions and item.retext_reason != REASON_ACCEPTED
            for item in applied
        ),
        "chunks": [
            _chunk_record(outcome, decisions.get(outcome.chunk_index)) for outcome in outcomes
        ],
        "unplaced": [
            _unplaced_record(item, members.get(item.chunk_index, ()))
            for decision in decisions.values()
            for item in decision.plan.unplaced
        ],
    }


def pass_status(statuses: Iterable[str]) -> str:
    """청크 상태들 → 글자 경로 전체 상태(Gemini 글자 경로도 같은 규칙을 쓴다, W-35b)."""
    asked = {status for status in statuses if status != STATUS_TIMING_FAILED}
    if not asked or asked == {STATUS_NOT_REQUESTED}:
        return STATUS_NOT_REQUESTED
    if asked == {STATUS_RECEIVED}:
        return PASS_APPLIED
    if STATUS_RECEIVED in asked:
        return PASS_PARTIAL
    return STATUS_UNAVAILABLE


def _chunk_record(outcome: TextChunkOutcome, decision: _Decision | None) -> dict[str, object]:
    body: dict[str, object] = {
        "chunk_index": outcome.chunk_index,
        "sample_start": outcome.sample_start,
        "sample_end": outcome.sample_end,
        "wav_sha256": outcome.wav_sha256,
        "key": outcome.key,
        "status": outcome.status,
        "cached": outcome.cached,
        "error": outcome.error,
        "text": outcome.text,
        "languages": list(outcome.languages),
    }
    if decision is not None and decision.plan.chunks:
        gate = decision.plan.chunks[0]
        body.update(
            similarity=gate.similarity,
            chunk_gate=gate.accepted,
            gate_passed=gate.replaced,
            gate_rejected=gate.rejected,
        )
    return body


def _unplaced_record(
    item: RetextUnplaced, members: Sequence[UtteranceEvidence]
) -> dict[str, object]:
    """사이에 낀 글자 — 어느 발화에도 붙이지 않는다(규칙 1). 앞뒤 발화는 자리를 가리킬 뿐이다."""
    ids = {member.position: member.utterance_id for member in members}
    return {
        "chunk_index": item.chunk_index,
        "text": item.text,
        "chars": item.chars,
        "before": None if item.before is None else ids.get(item.before),
        "after": None if item.after is None else ids.get(item.after),
    }


def _report(record: Mapping[str, Any]) -> None:
    """한 줄 요약과, 자리 없는 글자가 있으면 WARN(규칙 9 — 출력하지 않는 말은 조용하지 않다)."""
    unplaced = record["unplaced"]
    if record["status"] != STATUS_NOT_REQUESTED:
        log.out(
            f"1단계 글자 경로: {record['status']} · 바꾼 줄 {record['replaced']}"
            f" · whisper 글자 그대로 {record['kept']}"
        )
    if unplaced:
        chars = sum(int(item["chars"]) for item in unplaced)
        log.warn(
            f"1단계 글자 경로: 세그먼트 사이에 낀 글자 {len(unplaced)}곳 {chars}자 — 시각이 없어"
            " 자막에 싣지 않고 기록(asr_text.unplaced)에만 남깁니다 (D37)"
        )
    if record["carried"]:
        log.out(
            f"1단계 글자 경로: 글자 답이 없는 청크에서 이미 교차 확인된 {record['carried']}줄을"
            " 그대로 둡니다(같은 whisper 답·같은 출처 — 그중 바뀐 글자"
            f" {record['carried_replaced']}줄)"
        )
    if record["dropped_cross_checked"]:
        log.warn(
            f"1단계 글자 경로: 교차 확인됐던 {record['dropped_cross_checked']}줄은 whisper 답이"
            " 달라져 이어 갈 수 없어 whisper 글자로 심습니다 — 다시 교차 확인하세요"
        )


# ─────────────────────────────────────────────────────────────
#  제공자 — 승인된 실제 전송(19d부터)과 재생
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApprovedPhaseTextProvider:
    """1단계 글자 경로의 전송 경계 — 승인이 묶은 글자 모델과 1단계 승인을 청크마다 다시 지난다.

    `analysis_workflow.ApprovedPhaseAsrProvider`와 같은 모양이다. 승인(`AsrPhaseApproval` v2,
    D59 19d)이 글자 모델·요청 신원·요청 상한을 묶어야만 만들어진다. 전송 순간마다 1단계 승인·
    모델·요청 신원(문맥·언어·용어집)·스트림·범위를 다시 보고, 예약 문(승인이 묶은 글자 요청
    상한)은 whisper와 따로 센다.
    """

    inner: SourceTextProvider
    workflow: RawWorkflowConfig
    understanding: RawUnderstandingSettings
    audio: SelectedAudio
    approval: AsrPhaseApproval
    approved_model: str
    approved_identity: str
    gate: TextReservationGate
    approved_keys: frozenset[str] | None = None
    """견적이 센 바로 그 청크들(글자 캐시 열쇠) — 있으면 그 밖의 청크는 보내지 않는다(D59 19d)."""

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
        keywords: tuple[str, ...] | None = None,
        languages: tuple[str, ...] | None = None,
    ) -> TextTranscript:
        self._require_open(binding, settings, on)
        self._require_quoted(binding, settings.models.text_model, prompt, keywords, languages)
        self.gate.reserve(count=1, key=str(binding.request_id))
        return self.inner.text_transcript(
            binding=binding,
            chunk=chunk,
            settings=settings,
            on=on,
            prompt=prompt,
            stem=stem,
            source=source,
            keywords=keywords,
            languages=languages,
        )

    def _require_quoted(
        self,
        binding: AudioChunkBinding,
        model: str,
        prompt: str,
        keywords: tuple[str, ...] | None,
        languages: tuple[str, ...] | None,
    ) -> None:
        """그 호출의 문맥·언어·용어집으로 다시 잰 신원과 청크가 승인이 본 것인가(아니면 0송신)."""
        if call_identity(model, prompt, keywords, languages) != self.approved_identity:
            raise AnalysisApprovalError(
                "승인이 본 글자 요청(문맥·언어·용어집)과 지금의 요청이 다르다 — 보내지 않는다"
            )
        if self.approved_keys is not None and (
            call_key(binding, model, prompt, keywords, languages) not in self.approved_keys
        ):
            raise AnalysisApprovalError("견적에 없던 청크다 — 승인 밖이라 보내지 않는다")

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
        if self.approved_model != settings.models.text_model:
            raise AnalysisApprovalError("승인이 본 글자 모델과 설정 모델이 다르다")
        if self.audio.audio_stream_index != self.approval.audio_stream_index:
            raise AnalysisApprovalError("승인이 본 오디오 스트림과 지금의 선택이 다르다")
        if binding.sample_start < 0 or binding.sample_end > self.audio.sample_count:
            raise AnalysisApprovalError(
                f"청크 범위가 선택 오디오 밖이다: [{binding.sample_start}, {binding.sample_end})"
                f" / {self.audio.sample_count} 샘플"
            )


@dataclass(frozen=True, slots=True)
class ReplayTextProvider:
    """재생 글자 제공자 — 선언된 글자만 돌려준다. 없으면 실패다(지어내지 않는다).

    `exact`는 WAV sha로 찾는 정밀 선언이고 앞선다. `declared`는 이 원본의 청크 번호 → 글자다.
    재생도 실제 요청과 같은 조합 확인(`text_transcription_request`)을 지난다.
    """

    exact: Mapping[str, str]
    declared: Mapping[int, str] | None = None

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
        keywords: tuple[str, ...] | None = None,
        languages: tuple[str, ...] | None = None,
    ) -> TextTranscript:
        text = self.exact.get(binding.audio_sha256)
        if text is None and self.declared is not None:
            text = self.declared.get(binding.chunk_index)
        if text is None:
            raise ReplayTextMissing(f"재생할 글자 선언이 없다 — 청크 {binding.request_id}")
        request = text_transcription_request(
            chunk, settings=settings, prompt=prompt, keywords=keywords, languages=languages
        )
        return parse_text_response({"text": text}, binding=binding, model=request.model)


__all__ = [
    "PASS_APPLIED",
    "PASS_PARTIAL",
    "REASON_ACCEPTED",
    "REASON_AGREED",
    "REASON_UNDECIDED",
    "STATUS_NOT_REQUESTED",
    "STATUS_RECEIVED",
    "STATUS_TIMING_FAILED",
    "STATUS_UNAVAILABLE",
    "TEXT_CACHE_DIRNAME",
    "TEXT_ORIGINS",
    "TEXT_IDENTITY_SCHEMA",
    "TEXT_REQUEST_STAGE",
    "ApprovedPhaseTextProvider",
    "ReplayTextMissing",
    "ReplayTextProvider",
    "SourceTextProvider",
    "TextChunkOutcome",
    "TextPassRequest",
    "TextPassResult",
    "apply_text_pass",
    "cached_answer_exists",
    "call_identity",
    "call_key",
    "carry_previous",
    "pass_status",
    "require_same_skeleton",
    "run_text_pass",
    "same_wav",
    "text_chunk_key",
    "text_identity",
]
