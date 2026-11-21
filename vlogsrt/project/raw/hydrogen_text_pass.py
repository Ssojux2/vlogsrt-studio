"""1단계 Hydrogen 글자 귀(W-114c, D187) — Transcribe가 들은 **같은 청크 WAV**를 Gemini 4 Hydrogen에
묻고, 받은 글자를 줄 아이디로 그 청크의 줄에 얹는다(`hydrogen_text.plan`).

글자 귀(`text_ear`)가 `hydrogen`인 Gemini 원본만 이 길로 온다(처음 듣거나 다시 듣는 영상). 원본
자막이 심기기 **전에** 끝난다.

지키는 선:

- **시각·아이디·줄 수·차례는 Transcribe 그대로다**(규칙 1·5). 계획(`hydrogen_text`)에 시각
  칸이 없고, 얹은 뒤 골격이 같은지 다시 본다(`source_text.require_same_skeleton`).
- **줄 밖 말(`extra`)과 「말 없음」(`speech=false`)은 기록·WARN만 한다** — 줄로 넣거나 줄을 지우지
  않는다(규칙 1·9). 「확인 필요」로 잇는 일은 W-114e다.
- **실패는 Transcribe 글자 그대로 + WARN + 까닭**(규칙 9). 받지 않은 응답·실패는 캐시하지 않는다.
  취소는 그대로 올라간다.
- **온라인 요청은 승인이 묶은 것만 나간다**(D110) — 1단계 승인이 글자 모델(Hydrogen)·요청 신원·요청
  상한을 묶어야 실제 제공자가 생기고, 보낸 수가 상한을 넘지 않는다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from decimal import ROUND_CEILING, Decimal
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Protocol

from vlogsrt import log, oscompat
from vlogsrt.project.raw import gemini_cost, hydrogen_text, request_log, source_text
from vlogsrt.project.raw import hydrogen_text_io as hio
from vlogsrt.project.raw.config import (
    GEMINI_ANALYSIS_PROFILES,
    PRICE_TOKEN_UNIT,
    HydrogenTextSettings,
)
from vlogsrt.project.raw.source_text import (
    PASS_APPLIED,
    STATUS_NOT_REQUESTED,
    STATUS_RECEIVED,
    STATUS_TIMING_FAILED,
    STATUS_UNAVAILABLE,
    TEXT_ORIGINS,
    TextPassResult,
)
from vlogsrt.project.raw.understanding import (
    AsrChunkOutcome,
    AsrEvidence,
    SelectedAudio,
    UtteranceEvidence,
)
from vlogsrt.task_runtime import check_cancel, provider_error

TEXT_CACHE_DIRNAME = "source-asr-hydrogen-text"
"""Hydrogen 글자 캐시의 자리 — `cache/source-asr-hydrogen-text/<원본>/<열쇠>.json`."""
TEXT_REQUEST_STAGE = "asr-hydrogen-text"
"""요청 기록(`analysis_requests.stage`)의 종류 — 시간 엔진 `asr`와 따로 센다."""
IDENTITY_SCHEMA = "raw-source-hydrogen-text-identity/v1"
KEY_SCHEMA = "raw-source-hydrogen-text-chunk/v1"
RECORD_SCHEMA = "raw-source-hydrogen-text/v1"
STATUS_NO_LINES = "no_lines"
"""Transcribe가 이 청크에서 줄을 하나도 내지 않았다 — 얹을 줄이 없어 묻지 않는다."""
REASON_HEARD = "hydrogen"
"""Hydrogen이 그 줄 아이디에 준 글자로 바꿨다(D187)."""
USD_STEP = Decimal("0.000001")


class HydrogenTextProvider(Protocol):
    """Hydrogen 글자 귀의 주입 경계 — generateContent 본문을 보내고 응답(JSON 객체)을 돌려준다."""

    def generate(self, payload: Mapping[str, object]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class HydrogenPassRequest:
    """Hydrogen 글자 귀 한 번의 재료 — 설정·날짜·요청 신원·자리·출처(실제/재생)."""

    settings: HydrogenTextSettings
    on: date
    request_id: str
    workdir: Path
    cache_dir: Path
    origin: str


@dataclass(frozen=True, slots=True)
class HydrogenChunkOutcome:
    """청크 하나의 결과 — 받은 계획이거나 그러지 못한 까닭이다."""

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    key: str
    status: str
    plan: hydrogen_text.LineTextPlan | None = None
    cached: bool = False
    error: str = ""


def _sha(body: object) -> str:
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def text_identity(settings: HydrogenTextSettings) -> str:
    """Hydrogen 글자 요청의 신원 — 모델·판·생각·한도·자릿수·안내문·응답 schema. WAV·줄은 없다.

    1단계 승인(`AsrPhaseApproval.text_identity`)과 결속 꼬리표(`hydrogen-text/<신원>`)가 묶는다."""
    return _sha(
        {
            "identity": IDENTITY_SCHEMA,
            "model": settings.model,
            "prompt_revision": settings.prompt_revision,
            "plan_version": settings.plan_version,
            "thinking_level": settings.thinking_level,
            "max_output_tokens": settings.max_output_tokens,
            "time_decimals": settings.time_decimals,
            "audio_mime_type": settings.audio_mime_type,
            "instruction": hio.INSTRUCTION,
            "response_schema": hio.RESPONSE_SCHEMA,
        }
    )


def chunk_key(
    wav_sha256: str,
    lines: Sequence[hio.ChunkLine],
    settings: HydrogenTextSettings,
    origin: str,
) -> str:
    """Hydrogen 글자 캐시 열쇠 — 출처·요청 신원·WAV·줄 골격과 초안(요청에 싣는 그 바이트).

    출처를 싣는 까닭은 다른 글자 경로와 같다 — 재생 답을 실제 실행이 읽지 못한다."""
    if origin not in TEXT_ORIGINS:
        raise ValueError(f"글자 답의 출처는 {TEXT_ORIGINS} 중 하나다: {origin!r}")
    return _sha(
        {
            "schema": KEY_SCHEMA,
            "origin": origin,
            "identity": text_identity(settings),
            "wav": wav_sha256,
            "lines": hio.request_text(lines, settings),
        }
    )


def ceiling_usd(
    seconds: Fraction, chunks: int, settings: HydrogenTextSettings, *, on: date
) -> str | None:
    """원본 하나를 다 묻는 보수적 상한 — 청크마다 올림 초 오디오 + 글자 여유 + schema + 부대 여유,
    출력은 한도 전부. 단가가 그날 유효하지 않으면 None(가격 미정)이다."""
    profile = GEMINI_ANALYSIS_PROFILES[settings.model]
    if chunks <= 0 or not gemini_cost.price_current(profile, on=on):
        return None
    schema = len(json.dumps(hio.RESPONSE_SCHEMA, ensure_ascii=False, separators=(",", ":")))
    per_chunk = profile.quote_text_tokens_per_window + schema + profile.quote_overhead_tokens
    audio = gemini_cost.audio_tokens(profile, seconds) + chunks * profile.audio_tokens_per_second
    input_tokens = audio + chunks * (per_chunk + len(hio.INSTRUCTION.encode("utf-8")))
    output_tokens = chunks * settings.max_output_tokens
    usd = (
        Decimal(input_tokens) * profile.input_per_mtok
        + Decimal(output_tokens) * profile.output_per_mtok
    ) / PRICE_TOKEN_UNIT
    return str(usd.quantize(USD_STEP, rounding=ROUND_CEILING))


# ─────────────────────────────────────────────────────────────
#  묻기 — 캐시가 먼저, 문이 없으면 묻지 않는다
# ─────────────────────────────────────────────────────────────


def run_pass(
    audio: SelectedAudio,
    evidence: AsrEvidence,
    utterances: Sequence[UtteranceEvidence],
    request: HydrogenPassRequest,
    provider: HydrogenTextProvider | None,
) -> tuple[HydrogenChunkOutcome, ...]:
    """청크마다 Hydrogen 글자 계획을 구한다 — 캐시, 그다음 제공자. 제공자가 없으면 보내지 않는다."""
    members: dict[int, list[UtteranceEvidence]] = {}
    for item in utterances:
        members.setdefault(item.chunk_index, []).append(item)
    outcomes: list[HydrogenChunkOutcome] = []
    for chunk in evidence.chunks:
        check_cancel()
        lines = _chunk_lines(chunk, members.get(chunk.chunk_index, []), audio.sample_rate)
        outcomes.append(_hydrogen_chunk(audio, chunk, lines, request, provider))
    return tuple(outcomes)


def _chunk_lines(
    chunk: AsrChunkOutcome, members: Sequence[UtteranceEvidence], sample_rate: int
) -> tuple[hio.ChunkLine, ...]:
    """청크 하나의 줄 — 그 청크 국소 시계(같은 WAV)의 시각과 Transcribe 초안 글자."""
    return tuple(
        hio.ChunkLine(
            line_id=item.utterance_id,
            start=Fraction(item.start_sample - chunk.sample_start, sample_rate),
            end=Fraction(item.end_sample - chunk.sample_start, sample_rate),
            draft=item.text,
        )
        for item in members
    )


def _hydrogen_chunk(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    lines: tuple[hio.ChunkLine, ...],
    request: HydrogenPassRequest,
    provider: HydrogenTextProvider | None,
) -> HydrogenChunkOutcome:
    base = HydrogenChunkOutcome(
        chunk_index=chunk.chunk_index,
        sample_start=chunk.sample_start,
        sample_end=chunk.sample_end,
        wav_sha256=chunk.wav_sha256,
        key=chunk_key(chunk.wav_sha256, lines, request.settings, request.origin),
        status=STATUS_NOT_REQUESTED,
    )
    if not chunk.succeeded:
        return replace(base, status=STATUS_TIMING_FAILED)
    if not lines:
        return replace(base, status=STATUS_NO_LINES)
    cached = cached_plan(request.cache_dir / f"{base.key}.json", base.key, lines)
    if cached is not None:
        return replace(base, status=STATUS_RECEIVED, plan=cached, cached=True)
    if provider is None:
        return base
    return _ask(audio, chunk, lines, request, provider, base)


def _ask(
    audio: SelectedAudio,
    chunk: AsrChunkOutcome,
    lines: tuple[hio.ChunkLine, ...],
    request: HydrogenPassRequest,
    provider: HydrogenTextProvider,
    base: HydrogenChunkOutcome,
) -> HydrogenChunkOutcome:
    """제공자에게 묻는다 — 실패는 이 청크의 기록(Transcribe 글자 그대로)이다. 다음 청크는 묻는다."""
    attempt: str | None = None
    try:
        engine_chunk, _binding = source_text.same_wav(
            audio, chunk, request.workdir, f"{request.request_id}:hydrogen-{chunk.chunk_index:04d}"
        )
        body = hio.payload(engine_chunk.path.read_bytes(), lines, request.settings)
        attempt = request_log.begin(TEXT_REQUEST_STAGE, base.key)
        response = provider.generate(body)
        request_log.finish(attempt, received=True)
        record_usage(response, request, attempt)
    except Exception as exc:  # noqa: BLE001 - 글자 실패는 이 청크의 기록이다(규칙 9)
        category, note, uncertain = provider_error(exc)
        request_log.finish(attempt, received=not uncertain, note=category)
        check_cancel()
        return _unavailable(base, note)
    plan = hio.read_reply(response, lines)
    if not plan.accepted:  # 받아 줄 수 없는 응답 — 캐시하지 않는다(다음 실행이 다시 묻는다)
        return _unavailable(base, f"{plan.reason}: {plan.detail}")
    write_cache(request.cache_dir, base.key, request.settings, response)
    return replace(base, status=STATUS_RECEIVED, plan=plan)


def record_usage(
    response: Mapping[str, Any], request: HydrogenPassRequest, attempt: str | None
) -> None:
    """받은 응답의 실측 사용량을 그 요청 줄에 단다 — 못 매기면 가격 미정이다(0원이 아니다)."""
    meta = SimpleNamespace(model=request.settings.model)
    usage = gemini_cost.measure_gemini_usage(meta, response, on=request.on)
    request_log.record_usage(usage, attempt)


def _unavailable(base: HydrogenChunkOutcome, note: str) -> HydrogenChunkOutcome:
    log.warn(
        f"1단계 Hydrogen 글자 귀 청크 {base.chunk_index} 실패 — Transcribe 글자 그대로 둡니다:"
        f" {note}"
    )
    return replace(base, status=STATUS_UNAVAILABLE, error=note)


def cached_plan(
    path: Path, key: str, lines: Sequence[hio.ChunkLine]
) -> hydrogen_text.LineTextPlan | None:
    """성공 캐시의 계획 — 읽지 못하거나 받지 못한 항목은 없는 것으로 치고 경고한다."""
    if not path.is_file():
        return None
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warn(f"Hydrogen 글자 캐시를 읽지 못했다 — 없는 것으로 칩니다: {path.name} ({error})")
        return None
    if not isinstance(body, dict) or body.get("key") != key:
        log.warn(f"Hydrogen 글자 캐시의 열쇠·모양이 어긋났다 — 없는 것으로 칩니다: {path.name}")
        return None
    plan = hio.read_reply(body.get("response"), lines)
    if not plan.accepted:
        log.warn(f"Hydrogen 글자 캐시를 받지 못했다 — 없는 것으로 칩니다: {path.name}")
        return None
    return plan


def write_cache(
    directory: Path, key: str, settings: HydrogenTextSettings, response: Mapping[str, Any]
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{key}.json"
    temp = target.with_suffix(".tmp")
    body = {
        "key": key,
        "schema": RECORD_SCHEMA,
        "model": settings.model,
        "prompt_revision": settings.prompt_revision,
        "plan_version": settings.plan_version,
        "response": dict(response),
    }
    temp.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
    oscompat.replace(temp, target)


# ─────────────────────────────────────────────────────────────
#  얹기 — 받은 청크만, 줄 아이디로
# ─────────────────────────────────────────────────────────────


def apply_pass(
    utterances: Sequence[UtteranceEvidence],
    outcomes: Sequence[HydrogenChunkOutcome],
    request: HydrogenPassRequest,
    previous: Sequence[Mapping[str, object]] = (),
) -> TextPassResult:
    """받은 계획을 줄에 얹는다 — 시각·아이디·순서·수는 그대로다(규칙 5).

    답이 없는 청크(실패·묻지 않음)는 지금 심긴 같은 Transcribe 답의 글자 증거를 잇는다(다른 글자
    경로와 같은 이어 가기 — 더 나은 글자를 조용히 초안으로 되돌리지 않는다)."""
    plans = {
        outcome.chunk_index: outcome.plan
        for outcome in outcomes
        if outcome.status == STATUS_RECEIVED and outcome.plan is not None
    }
    rows = {row.line_id: row for plan in plans.values() for row in plan.lines}
    placed = tuple(
        _with_text(item, rows.get(item.utterance_id), request)
        if item.chunk_index in plans
        else item
        for item in utterances
    )
    applied, carried, dropped = source_text.carry_previous(
        placed, set(plans), previous, request.origin
    )
    source_text.require_same_skeleton(utterances, applied)
    record = _record(outcomes, applied, plans, request)
    record.update(carried=len(carried), dropped_cross_checked=dropped)
    _report(record)
    return TextPassResult(utterances=applied, record=record)


def _with_text(
    item: UtteranceEvidence,
    row: hydrogen_text.PlannedLine | None,
    request: HydrogenPassRequest,
) -> UtteranceEvidence:
    """줄 하나의 글자와 증거 — Transcribe 초안은 언제나 남는다(`whisper_text` 칸 — 저장 이름)."""
    if row is not None and row.source == hydrogen_text.SOURCE_HYDROGEN:
        return replace(
            item,
            text=row.text,
            whisper_text=item.text,
            text_source=request.settings.model,
            retext_reason=REASON_HEARD,
            alt_text="",
            text_origin=request.origin,
        )
    return item  # `speech=false` — 초안 그대로, 증거 칸도 그대로(기록에만 남는다)


def _record(
    outcomes: Sequence[HydrogenChunkOutcome],
    applied: Sequence[UtteranceEvidence],
    plans: Mapping[int, hydrogen_text.LineTextPlan],
    request: HydrogenPassRequest,
) -> dict[str, object]:
    heard = [item for item in applied if item.chunk_index in plans]
    return {
        "schema": RECORD_SCHEMA,
        "origin": request.origin,
        "model": request.settings.model,
        "prompt_revision": request.settings.prompt_revision,
        "plan_version": request.settings.plan_version,
        "identity": text_identity(request.settings),
        "status": _pass_status(outcome.status for outcome in outcomes),
        "replaced": sum(item.retext_reason == REASON_HEARD for item in heard),
        "kept": sum(item.retext_reason != REASON_HEARD for item in heard),
        "no_speech": sum(row.no_speech for plan in plans.values() for row in plan.lines),
        "extra": sum(len(plan.extra) for plan in plans.values()),
        "chunks": [_chunk_record(outcome) for outcome in outcomes],
    }


def _pass_status(statuses: Any) -> str:
    """청크 상태 → 전체 상태 — 줄이 없는 청크는 시각 실패 청크처럼 세지 않는다."""
    return source_text.pass_status(
        STATUS_TIMING_FAILED if status == STATUS_NO_LINES else status for status in statuses
    )


def _chunk_record(outcome: HydrogenChunkOutcome) -> dict[str, object]:
    body: dict[str, object] = {
        "chunk_index": outcome.chunk_index,
        "sample_start": outcome.sample_start,
        "sample_end": outcome.sample_end,
        "wav_sha256": outcome.wav_sha256,
        "key": outcome.key,
        "status": outcome.status,
        "cached": outcome.cached,
        "error": outcome.error,
    }
    if outcome.plan is not None:
        body.update(
            no_speech=[row.line_id for row in outcome.plan.lines if row.no_speech],
            extra=[{"after_id": e.after_id, "text": e.text} for e in outcome.plan.extra],
        )
    return body


def _report(record: Mapping[str, Any]) -> None:
    """한 줄 요약과, 실패·말 없음·줄 밖 말이 있으면 WARN(규칙 9)."""
    if record["status"] != STATUS_NOT_REQUESTED:
        log.out(
            f"1단계 Hydrogen 글자 귀: {record['status']} · Hydrogen 글자 {record['replaced']}줄"
            f" · Transcribe 글자 그대로 {record['kept']}줄"
        )
    if record["status"] not in (STATUS_NOT_REQUESTED, PASS_APPLIED):
        failed = sum(chunk["status"] == STATUS_UNAVAILABLE for chunk in record["chunks"])
        log.warn(f"1단계 Hydrogen 글자 귀: {failed}청크는 Transcribe 글자 그대로입니다 (D187)")
    if record["no_speech"]:
        log.warn(
            f"1단계 Hydrogen 글자 귀: 말이 없다고 한 줄 {record['no_speech']}개 — 지우지 않고"
            " Transcribe 글자 그대로 둡니다 (D187)"
        )
    if record["extra"]:
        log.warn(
            f"1단계 Hydrogen 글자 귀: 줄 밖에서 들린 말 {record['extra']}개 — 시각이 없어 자막에"
            " 싣지 않고 기록(asr_text.chunks[].extra)에만 남깁니다 (규칙 1)"
        )


# ─────────────────────────────────────────────────────────────
#  제공자 — 승인이 묶은 실제 전송
# ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class LiveHydrogenProvider:
    """승인이 묶은 상한 안에서만 Gemini 문으로 보낸다 — 넘으면 보내지 않고 그 청크가 실패한다."""

    client: object
    model: str
    budget: int
    sent: int = 0

    def generate(self, payload: Mapping[str, object]) -> Mapping[str, Any]:
        from vlogsrt.project.raw.analysis_approval import AnalysisApprovalError
        from vlogsrt.transcribe import request_gemini_generate

        if self.sent >= self.budget:
            raise AnalysisApprovalError(
                f"승인이 묶은 Hydrogen 글자 요청 상한({self.budget})을 넘는다 — 보내지 않는다"
            )
        self.sent += 1
        return request_gemini_generate(
            self.client, model=self.model, payload=payload, online=True, cancellation=check_cancel
        )


__all__ = [
    "REASON_HEARD",
    "cached_plan",
    "record_usage",
    "write_cache",
    "STATUS_NO_LINES",
    "TEXT_CACHE_DIRNAME",
    "TEXT_REQUEST_STAGE",
    "HydrogenChunkOutcome",
    "HydrogenPassRequest",
    "HydrogenTextProvider",
    "LiveHydrogenProvider",
    "apply_pass",
    "ceiling_usd",
    "chunk_key",
    "run_pass",
    "text_identity",
]
