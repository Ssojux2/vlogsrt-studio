"""승인형 AI rough-cut의 비용 추정, 안전 후보, Luna 판정 순수 계층."""

from __future__ import annotations

import base64
import difflib
import hashlib
import json
import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, fields
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.workflow_protocol import (
    ActualUsage,
    AnalysisEstimate,
    AnalysisResult,
    AnalysisResultStatus,
    CandidatePreview,
    CutCandidate,
    PricingSnapshot,
    ProjectRevision,
    RationalRange,
    UpstreamStages,
    VisionDecisionStatus,
    VisionWindowDecision,
)

_PROMPT_REVISION = "roughcut-luna-v1"
_NG_EXPRESSION = re.compile(
    r"^\s*(?:아+[\s,]*)?(?:잠깐\s*)?(?:다시\s*(?:할게|갈게|가자|해볼게|찍을게)|엔지|ng|컷)\s*[.!?…]*\s*$",
    re.IGNORECASE,
)
_TEXT_CHARS = re.compile(r"[^0-9a-z가-힣]+")


@dataclass(frozen=True, slots=True)
class AnalysisSettings:
    model: str = "gpt-5.6-luna"
    window_seconds: Fraction = Fraction(10)
    max_images: int = 6
    image_long_edge: int = 512
    # low detail 이미지는 512×512로 내려가고 장당 308 입력 토큰이다(2026-09-21 요금).
    low_detail_image_tokens: int = 308
    window_output_tokens: int = 256
    # 전사 **작업 몫** 배수: 고유 오디오 × 이 값 = 허용 전사량(1차·재전사·심판·
    # 교정·재시도·조각을 한 데 몰아 넣은 정액 몫). 실제 호출이 몫을 넘으면 원장이
    # 이후 예약을 거부하고 실행은 cost_ceiling_exceeded로 멈춘다(S2B-2b) — 전
    # 과정 완료의 보증이 아니라 정직한 소진 경계다.
    transcription_call_multiplier: int = 4
    review_confidence: Decimal = Decimal("0.90")
    retake_horizon_seconds: Fraction = Fraction(30)
    retake_similarity: Decimal = Decimal("0.72")
    prompt_revision: str = _PROMPT_REVISION
    # ── 견적·요청 상한 정책(S2B-2b) — 추정과 실제 승인이 **같은** 상한을 쓴다 ──
    # 직렬화된 입력 글자의 보수적 토큰 상한. 바이트당 토큰 수가 1이면 "토큰이
    # 바이트보다 많다"는 방향으로 안전하다(한글 음절은 UTF-8 3바이트이고 토큰은
    # 대개 그보다 적다). 토큰 변환 비율을 발명하지 않는 유일한 결정적 상한이다.
    quote_text_bytes_per_token: int = 1
    quote_structural_overhead_tokens: int = 64
    # low detail 정액(308)의 2배 — 실제 과금이 정액과 어긋나도 예약이 모자라지
    # 않게 하는 여유분이다. base64 이미지 바이트를 글자 토큰으로 세지 않는다.
    quote_image_input_tokens: int = 616
    # 요청 **구조**의 바이트 상한 — 항목(entry)·마디(part)·이미지 겉면 각각의
    # 정액 여유다. 마디 수를 늘려 고정 여유 64토큰을 넘기는 길을 막는다: 구조
    # 바이트도 글자 바이트 상한 안에서 함께 센다(마디가 아무리 많아도 상한을
    # 넘으면 안전하게 실패한다). 겉면 값은 직렬화 최소 모양보다 크게 잡았다.
    quote_entry_overhead_bytes: int = 64
    quote_image_envelope_bytes: int = 128
    quote_structural_overhead_bytes: int = 64
    max_request_text_bytes: int = 8_192
    max_request_images: int = 6
    max_request_output_tokens: int = 1_024

    def __post_init__(self) -> None:
        if not self.model or not self.prompt_revision:
            raise ValueError("분석 모델·프롬프트 리비전은 비어 있을 수 없다")
        if self.window_seconds <= 0 or self.retake_horizon_seconds <= 0:
            raise ValueError("분석 창과 재촬영 탐색 길이는 양수여야 한다")
        if self.max_images < 1 or self.max_images > 6:
            raise ValueError("창당 이미지는 1~6장이어야 한다")
        if self.image_long_edge < 1 or self.image_long_edge > 512:
            raise ValueError("이미지 긴 변은 1~512px이어야 한다")
        if self.low_detail_image_tokens < 1:
            raise ValueError("이미지 토큰 상한은 양수여야 한다")
        if self.window_output_tokens < 1:
            raise ValueError("창 출력 토큰 상한은 양수여야 한다")
        if self.transcription_call_multiplier < 1:
            raise ValueError("전사 호출 배수는 1 이상이어야 한다")
        for name in ("review_confidence", "retake_similarity"):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"{name}는 유한한 0~1이어야 한다")
        if self.quote_text_bytes_per_token < 1:
            raise ValueError("견적 바이트당 토큰은 1 이상이어야 한다")
        if self.quote_structural_overhead_tokens < 0:
            raise ValueError("견적 구조 여유 토큰은 0 이상이어야 한다")
        if self.quote_image_input_tokens < 1:
            raise ValueError("견적 이미지 토큰 상한은 양수여야 한다")
        for name in (
            "quote_entry_overhead_bytes",
            "quote_image_envelope_bytes",
            "quote_structural_overhead_bytes",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name}은 0 이상이어야 한다")
        if self.max_request_text_bytes < 1:
            raise ValueError("요청 글자 바이트 상한은 양수여야 한다")
        if self.max_request_images < 1 or self.max_request_images > 6:
            raise ValueError("요청 이미지 상한은 1~6장이어야 한다")
        if self.max_request_output_tokens < 1:
            raise ValueError("요청 출력 토큰 상한은 양수여야 한다")
        if self.window_output_tokens > self.max_request_output_tokens:
            raise ValueError("창 출력 상한이 요청 출력 상한보다 크다 — 예약이 상한을 못 믿는다")

    def sha256(self) -> str:
        payload = {field.name: _setting_value(getattr(self, field.name)) for field in fields(self)}
        return _digest(payload)


@dataclass(frozen=True, slots=True)
class TranscriptEvidence:
    utterance_id: str
    clip_instance_id: str
    source_range: RationalRange
    timeline_range: RationalRange
    text: str
    complete: bool


@dataclass(frozen=True, slots=True)
class CandidateSeed:
    candidate_id: str
    window_id: str
    clip_instance_id: str
    source_range: RationalRange
    timeline_range: RationalRange
    reason: str
    evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LunaWindow:
    input_revision: str
    window_id: str
    candidate_ids: tuple[str, ...]
    frame_ids: tuple[str, ...]
    transcript_context: str
    source_hashes: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class LunaFrame:
    frame_id: str
    jpeg_bytes: bytes
    sha256: str
    source_time: Fraction
    timeline_time: Fraction

    def __post_init__(self) -> None:
        if not self.frame_id:
            raise ValueError("프레임 ID는 비어 있을 수 없다")
        if self.source_time < 0 or self.timeline_time < 0:
            raise ValueError("프레임 시각은 0 이상이어야 한다")


@dataclass(frozen=True, slots=True)
class CandidateWindowPlan:
    window_id: str
    clip_instance_id: str
    timeline_range: RationalRange
    candidates: tuple[CandidateSeed, ...]


def build_analysis_estimate(
    document: ProjectDocument,
    revision: ProjectRevision,
    settings: AnalysisSettings,
    *,
    pricing: PricingSnapshot,
    media_grant_revision: str,
    settings_sha256: str | None = None,
) -> AnalysisEstimate:
    """네트워크 없이 고유 소스 오디오와 최악의 창/이미지/토큰 사용량을 센다.

    비용 상한은 공식 가격 스냅샷 하나로 계산한다 — 사용자 예산 가정과 섞지
    않는다(승인은 스냅샷 리비전에 묶인다). 전사 항은 **정해진 작업 몫**이다:
    고유 오디오에 `transcription_call_multiplier`를 곱한 양을 whisper+gpt 합산
    분당가로 환산한 허용량이며, 실제 호출 조합(1차·재전사·심판·교정·재시도·
    조각)이 이를 넘으면 원장이 이후 예약을 거부해 실행은 `cost_ceiling_exceeded`
    로 정직하게 멈춘다(S2B-2b). **전 과정 완료의 보증이 아니다** — 완료를
    보증하려면 통과·재시도별 유한 상한을 따로 증명해야 한다. Luna 입력 상한은
    이미지·출력과 같은 표면에 **직렬화 글자** 상한이 함께 들어간다 — 실제
    요청의 예약이 승인보다 클 수 없어야 하기 때문이다.
    """
    if document.input_sha256 != revision.input_sha256:
        raise ValueError("비용 추정의 문서와 입력 리비전이 다르다")
    if pricing.luna_model != settings.model:
        raise ValueError("가격 스냅샷의 Luna 모델과 분석 모델이 다르다")
    unique_audio: dict[str, Fraction] = {}
    for resource in document.resources:
        if resource.has_audio:
            key = resource.uid or resource.media_reps[0].src
            unique_audio.setdefault(key, resource.duration)
    unique_source_audio_seconds = sum(unique_audio.values(), start=Fraction())
    transcription_seconds = unique_source_audio_seconds * settings.transcription_call_multiplier
    max_windows = sum(
        _ceil_fraction(item.duration / settings.window_seconds)
        for item in document.storyline
        if isinstance(item, ClipInstance)
    )
    max_images = max_windows * settings.max_images
    max_image_tokens = max_images * settings.quote_image_input_tokens
    max_text_tokens = max_windows * window_text_token_ceiling(settings)
    cost = (
        _fraction_decimal(transcription_seconds)
        / Decimal(60)
        * (Decimal(pricing.whisper_per_minute) + Decimal(pricing.gpt_transcribe_per_minute))
        + Decimal(max_image_tokens + max_text_tokens)
        * Decimal(pricing.luna_input_per_million_tokens)
        / Decimal(1_000_000)
        + Decimal(max_windows * settings.window_output_tokens)
        * Decimal(pricing.luna_output_per_million_tokens)
        / Decimal(1_000_000)
    )
    return AnalysisEstimate.create(
        input_revision=revision.revision,
        model=settings.model,
        unique_source_audio_seconds=unique_source_audio_seconds,
        transcription_seconds=transcription_seconds,
        max_windows=max_windows,
        max_images=max_images,
        max_image_tokens=max_image_tokens,
        cost_upper_bound=_decimal_text(cost),
        settings_sha256=settings_sha256 or settings.sha256(),
        pricing_snapshot_revision=pricing.snapshot_revision,
        media_grant_revision=media_grant_revision,
    )


def window_text_token_ceiling(settings: AnalysisSettings) -> int:
    """한 창 요청의 직렬화 글자 토큰 보수적 상한 — 추정과 실제 승인이 같은 값.

    바이트 상한을 바이트당 토큰으로 나눈 올림에 구조 여유를 더한다. 토큰 변환
    비율을 발명하지 않는 결정적 상한이다(토크나이저 의존 없음).
    """
    return (
        -(-settings.max_request_text_bytes // settings.quote_text_bytes_per_token)
        + settings.quote_structural_overhead_tokens
    )


def select_frame_times(
    window: RationalRange,
    candidate_ranges: Sequence[RationalRange],
    *,
    frame_duration: Fraction,
    max_images: int,
    grid_anchor: Fraction = Fraction(),
) -> tuple[Fraction, ...]:
    """후보 시작·중앙·끝을 우선하고 등간격 문맥으로 최대 장수를 채운다."""
    if frame_duration <= 0 or max_images < 1 or max_images > 6:
        raise ValueError("프레임 길이는 양수, 이미지 상한은 1~6이어야 한다")
    last = _frame_floor(window.end - frame_duration, frame_duration, grid_anchor)
    if last < window.start:
        last = _frame_ceil(window.start, frame_duration, grid_anchor)
    priority: list[Fraction] = []
    for candidate in candidate_ranges:
        start = _frame_ceil(max(window.start, candidate.start), frame_duration, grid_anchor)
        middle = _frame_floor((candidate.start + candidate.end) / 2, frame_duration, grid_anchor)
        end = _frame_floor(
            min(window.end, candidate.end) - frame_duration, frame_duration, grid_anchor
        )
        priority.extend((start, middle, end))
    context: tuple[Fraction, ...]
    if max_images == 1:
        context = (window.start,)
    else:
        span = max(Fraction(), last - window.start)
        context = tuple(
            _frame_floor(
                window.start + span * index / (max_images - 1), frame_duration, grid_anchor
            )
            for index in range(max_images)
        )
    selected: list[Fraction] = []
    for value in (*priority, *context):
        if window.start <= value < window.end and value not in selected:
            selected.append(value)
        if len(selected) == max_images:
            break
    return tuple(sorted(selected))


def safe_transcript_candidates(
    utterances: Sequence[TranscriptEvidence],
    input_revision: str,
    settings: AnalysisSettings,
) -> tuple[CandidateSeed, ...]:
    """고유 발화는 보존하고 후속 완성 발화가 확인된 재촬영/NG만 seed로 만든다."""
    ordered = sorted(utterances, key=lambda row: (row.timeline_range.start, row.utterance_id))
    candidates: list[CandidateSeed] = []
    for index, current in enumerate(ordered):
        following = tuple(
            row
            for row in ordered[index + 1 :]
            if row.complete
            and row.clip_instance_id == current.clip_instance_id
            and Fraction()
            <= row.timeline_range.start - current.timeline_range.end
            <= settings.retake_horizon_seconds
        )
        if not following:
            continue
        normalized = _normalize_text(current.text)
        repeated = next(
            (
                row
                for row in following
                if normalized
                and _similarity(normalized, _normalize_text(row.text))
                >= float(settings.retake_similarity)
            ),
            None,
        )
        is_ng = _NG_EXPRESSION.fullmatch(current.text) is not None
        if repeated is None and not is_ng:
            continue
        reason = "retake_overlap" if repeated is not None else "ng_with_followup"
        followup = repeated or following[0]
        evidence = (f"transcript:{reason}", f"followup:{followup.utterance_id}")
        window_id = _window_id(input_revision, current, settings.window_seconds)
        candidate_id = CutCandidate.identity(
            input_revision=input_revision,
            clip_instance_id=current.clip_instance_id,
            source_range=current.source_range,
            timeline_range=current.timeline_range,
            reason=reason,
            evidence=evidence,
            window_id=window_id,
        )
        candidates.append(
            CandidateSeed(
                candidate_id,
                window_id,
                current.clip_instance_id,
                current.source_range,
                current.timeline_range,
                reason,
                evidence,
            )
        )
    return tuple(candidates)


def eligible_transcript_evidence(
    utterances: Sequence[TranscriptEvidence],
    *,
    successful_instance_ids: set[str],
    locked_instance_ids: set[str],
) -> tuple[TranscriptEvidence, ...]:
    return tuple(
        row
        for row in utterances
        if row.clip_instance_id in successful_instance_ids
        and row.clip_instance_id not in locked_instance_ids
    )


def project_transcript_evidence(
    *,
    utterance_id: str,
    clip_instance_id: str,
    source_range: RationalRange,
    text: str,
    complete: bool,
    clip_source_start: Fraction,
    clip_timeline_start: Fraction,
    resource_start: Fraction,
    frame_duration: Fraction,
) -> TranscriptEvidence | None:
    """file/asset 시계의 cue를 소스 격자에 안쪽 정렬하고 timeline을 재투영한다."""
    start = _frame_ceil(source_range.start, frame_duration, resource_start)
    end = _frame_floor(source_range.end, frame_duration, resource_start)
    if not clip_source_start < start < end:
        return None
    source = RationalRange(start, end)
    timeline = RationalRange(
        clip_timeline_start + start - clip_source_start,
        clip_timeline_start + end - clip_source_start,
    )
    return TranscriptEvidence(
        utterance_id,
        clip_instance_id,
        source,
        timeline,
        text,
        complete,
    )


def plan_candidate_windows(
    seeds: Sequence[CandidateSeed],
    *,
    input_revision: str,
    clip_ranges: Mapping[str, RationalRange],
    window_seconds: Fraction,
) -> tuple[CandidateWindowPlan, ...]:
    """clip을 최대 window_seconds로 나눈 고정 창에 후보를 묶는다."""
    grouped: dict[tuple[str, int], list[CandidateSeed]] = {}
    windows: dict[tuple[str, int], RationalRange] = {}
    for seed in seeds:
        clip = clip_ranges.get(seed.clip_instance_id)
        if clip is None:
            continue
        cursor = max(seed.timeline_range.start, clip.start)
        stop = min(seed.timeline_range.end, clip.end)
        while cursor < stop:
            index = int((cursor - clip.start) // window_seconds)
            window_start = clip.start + index * window_seconds
            window_end = min(clip.end, window_start + window_seconds)
            piece_end = min(stop, window_end)
            delta_start = cursor - seed.timeline_range.start
            delta_end = piece_end - seed.timeline_range.start
            source = RationalRange(
                seed.source_range.start + delta_start,
                seed.source_range.start + delta_end,
            )
            timeline = RationalRange(cursor, piece_end)
            window_id = hashlib.sha256(
                f"vlogsrt-luna-window-v2\0{input_revision}\0{seed.clip_instance_id}"
                f"\0{window_start}\0{window_end}".encode()
            ).hexdigest()
            candidate_id = CutCandidate.identity(
                input_revision=input_revision,
                clip_instance_id=seed.clip_instance_id,
                source_range=source,
                timeline_range=timeline,
                reason=seed.reason,
                evidence=seed.evidence,
                window_id=window_id,
            )
            key = (seed.clip_instance_id, index)
            grouped.setdefault(key, []).append(
                CandidateSeed(
                    candidate_id,
                    window_id,
                    seed.clip_instance_id,
                    source,
                    timeline,
                    seed.reason,
                    seed.evidence,
                )
            )
            windows[key] = RationalRange(window_start, window_end)
            cursor = piece_end
    return tuple(
        CandidateWindowPlan(rows[0].window_id, key[0], windows[key], tuple(rows))
        for key, rows in sorted(grouped.items(), key=lambda item: (windows[item[0]].start, item[0]))
    )


def build_luna_payload(
    window: LunaWindow,
    candidates: Sequence[CandidateSeed],
    frames: Sequence[LunaFrame],
    *,
    max_output_tokens: int = 256,
) -> dict[str, object]:
    """Responses API input. 경로와 자격증명은 받지도 직렬화하지도 않는다.

    이미지마다 바로 앞에 `input_text`로 frame_id·소스 시각·타임라인 시각을 밝힌다
    — 판정의 근거 프레임이 어느 시각인지 모델과 기록이 같은 눈으로 본다. 창
    출력은 256 토큰, reasoning은 none(출력 토큰을 판정 JSON에만 쓴다).
    """
    _require_window(window, candidates, frames)
    candidate_text = "\n".join(
        f"- {row.candidate_id}: {row.reason} [{row.timeline_range.start},{row.timeline_range.end})"
        for row in candidates
    )
    instruction = (
        "안전 후보 중 하나만 제안하거나 keep을 선택한다. 새 시간 범위를 만들지 않는다. "
        "고유 발화, 분석 실패, 판단 불확실은 keep이다. "
        'JSON만 반환한다: {"choice":"keep|후보 ID","confidence":0..1,'
        '"evidence_frame_ids":["프레임 ID"]}.\n'
        f"window_id={window.window_id}\n후보:\n{candidate_text}\n"
        f"전사/VAD 문맥:\n{window.transcript_context}"
    )
    by_id = {frame.frame_id: frame for frame in frames}
    content: list[dict[str, object]] = [{"type": "input_text", "text": instruction}]
    for frame_id in window.frame_ids:
        frame = by_id[frame_id]
        content.append(
            {
                "type": "input_text",
                "text": (
                    f"frame_id={frame.frame_id} "
                    f"source_time={_fraction_decimal(frame.source_time)}s "
                    f"timeline_time={_fraction_decimal(frame.timeline_time)}s"
                ),
            }
        )
        content.append(
            {
                "type": "input_image",
                "detail": "low",
                "image_url": "data:image/jpeg;base64,"
                + base64.b64encode(frame.jpeg_bytes).decode("ascii"),
            }
        )
    return {
        "input": [{"role": "user", "content": content}],
        "max_output_tokens": max_output_tokens,
        "reasoning": {"effort": "none"},
    }


class _DecisionError(Exception):
    """정규화가 판정을 error로 닫을 때의 사유 코드 운반 — 원문은 절대 실지 않는다."""

    def __init__(self, reason_code: str) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code


def normalize_luna_response(
    window: LunaWindow, raw: Mapping[str, object] | BaseException
) -> VisionWindowDecision:
    """모델/API 결함은 상태 error인 keep으로 닫고 원문은 해시만 남긴다.

    사유는 정돈된 코드뿐이다 — 예외 문장·응답 본문·비밀이 사용자 보이는 기록에
    흐르지 않는다. error 판정은 후보를 만들지 못하므로 그 창의 영상은 전부 보존된다.
    """
    response_sha256 = _response_hash(raw)

    def closed(reason_code: str) -> VisionWindowDecision:
        return VisionWindowDecision.create(
            input_revision=window.input_revision,
            window_id=window.window_id,
            choice="keep",
            candidate_id=None,
            evidence_frame_ids=(),
            confidence="0",
            response_sha256=response_sha256,
            status="error",
            reason_code=reason_code,
        )

    try:
        if isinstance(raw, BaseException):
            raise _DecisionError("api_exception")
        text = raw.get("output_text")
        if not isinstance(text, str) or not text:
            raise _DecisionError("empty_output")
        try:
            body = json.loads(text)
        except json.JSONDecodeError as exc:
            raise _DecisionError("malformed_response") from exc
        if not isinstance(body, dict):
            raise _DecisionError("malformed_response")
        choice = body.get("choice")
        confidence = body.get("confidence")
        evidence = body.get("evidence_frame_ids")
        if not isinstance(choice, str):
            raise _DecisionError("invalid_choice")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise _DecisionError("invalid_confidence")
        confidence_float = float(confidence)
        if not math.isfinite(confidence_float) or not 0 <= confidence_float <= 1:
            raise _DecisionError("invalid_confidence")
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise _DecisionError("invalid_evidence")
        if len(evidence) != len(set(evidence)) or not set(evidence) <= set(window.frame_ids):
            raise _DecisionError("invalid_evidence_identity")
        confidence_text = _decimal_text(Decimal(str(confidence)))
        if choice == "keep":
            return VisionWindowDecision.create(
                input_revision=window.input_revision,
                window_id=window.window_id,
                choice="keep",
                candidate_id=None,
                evidence_frame_ids=tuple(evidence),
                confidence=confidence_text,
                response_sha256=response_sha256,
            )
        if choice not in window.candidate_ids:
            raise _DecisionError("unknown_candidate")
        if not evidence:
            # 후보를 댔으면 근거 프레임이 하나 이상 필요하다 — 빈 근거의 propose_cut은
            # 유효한 판정이 아니라 error로 닫는다(프로토콜 검증에 부딪히게 두지 않는다).
            raise _DecisionError("invalid_evidence")
        return VisionWindowDecision.create(
            input_revision=window.input_revision,
            window_id=window.window_id,
            choice="propose_cut",
            candidate_id=choice,
            evidence_frame_ids=tuple(evidence),
            confidence=confidence_text,
            response_sha256=response_sha256,
        )
    except _DecisionError as failure:
        return closed(failure.reason_code)


def error_window_decision(
    input_revision: str, window_id: str, reason_code: str = "window_error"
) -> VisionWindowDecision:
    """창을 끝까지 묻지 못한 실패의 기록 — 호출 전 준비가 무너진 경우 등."""
    return _unprocessed_decision(input_revision, window_id, "error", reason_code)


def skipped_window_decision(
    input_revision: str, window_id: str, reason_code: str
) -> VisionWindowDecision:
    """호출하지 않기로 결정한 창의 기록 — 예산 상한 등으로 남은 창을 덮는다."""
    return _unprocessed_decision(input_revision, window_id, "skipped", reason_code)


def _unprocessed_decision(
    input_revision: str,
    window_id: str,
    status: VisionDecisionStatus,
    reason_code: str,
) -> VisionWindowDecision:
    return VisionWindowDecision.create(
        input_revision=input_revision,
        window_id=window_id,
        choice="keep",
        candidate_id=None,
        evidence_frame_ids=(),
        confidence="0",
        response_sha256=_digest(
            {"reason_code": reason_code, "status": status, "window_id": window_id}
        ),
        status=status,
        reason_code=reason_code,
    )


def aggregate_analysis_outcome(
    *,
    input_revision: str,
    attempt_id: str,
    estimate_revision: str,
    decisions: Sequence[VisionWindowDecision],
    windows_total: int,
    candidate_count: int,
    upstream: UpstreamStages,
    upstream_reasons: Sequence[str] = (),
    stage_failure: str = "",
) -> AnalysisResult:
    """시도 하나의 집계 상태 — 성공 커버리지가 없으면 실패가 성공을 흉내 못 내게 한다.

    창 0개는 상류가 전부 성공했을 때만 success다. 시도한 창이 전부 실패하면 상류가
    성공했어도 error다. 상위 단계(전사 등)가 성공했더라도 다른 단계(VAD 등)가 실패한
    소스는 커버리지가 아니다 — "전사는 됐으니 성공"이 무발화 성공으로 읽히면 안
    된다. 창이 0개일 때 쓸 수 있는 소스(오디오·전사·VAD 전부 성공)가 하나라도 있으면
    실패가 섞여도 partial_error다. `stage_failure`는 창 loop 앞에서 무너진 단계(준비·
    분석)의 정돈된 사유다 — 커버리지가 없으므로 error로 닫는다. legacy(결과 상태 없는
    낡은 기록)는 성공도 재실행 근거도 아니다 — 실패로 세되 그 사유 코드를 드러낸다.
    """
    successful = sum(1 for row in decisions if row.status == "success")
    failed = sum(1 for row in decisions if row.status in {"error", "legacy"})
    skipped = sum(1 for row in decisions if row.status == "skipped")
    upstream_failed = upstream.audio_failed + upstream.transcription_failed + upstream.vad_failed
    any_failure = bool(stage_failure) or failed > 0 or skipped > 0 or upstream_failed > 0
    # 시도한 창이 하나라도 있으면 창이 커버리지를 말한다 — 전부 실패한 창은 상류가
    # 성공했어도 error다. 창이 0개일 때만 쓸 수 있는 소스(오디오·전사·VAD 전부
    # 성공)가 커버리지가 된다 — 좋은 무발화 소스 옆의 실패 소스는 부분 실패다.
    usable_coverage = successful > 0 or (windows_total == 0 and upstream.sources_usable > 0)
    status: AnalysisResultStatus
    if not any_failure:
        status = "success"
    elif usable_coverage:
        status = "partial_error"
    else:
        status = "error"
    reasons = {
        *(row.reason_code for row in decisions if row.status != "success"),
        *upstream_reasons,
        *({stage_failure} if stage_failure else ()),
    }
    return AnalysisResult.create(
        input_revision=input_revision,
        attempt_id=attempt_id,
        estimate_revision=estimate_revision,
        status=status,
        windows_total=windows_total,
        windows_successful=successful,
        windows_failed=failed,
        windows_skipped=skipped,
        candidate_count=candidate_count,
        upstream=upstream,
        reason_codes=tuple(reasons),
    )


def finalize_candidate(seed: CandidateSeed, decision: VisionWindowDecision) -> CutCandidate | None:
    if (
        decision.choice != "propose_cut"
        or decision.status != "success"
        or decision.candidate_id != seed.candidate_id
        or decision.window_id != seed.window_id
    ):
        return None
    return CutCandidate(
        candidate_id=seed.candidate_id,
        input_revision=decision.input_revision,
        clip_instance_id=seed.clip_instance_id,
        source_range=seed.source_range,
        timeline_range=seed.timeline_range,
        reason=seed.reason,
        evidence=(*seed.evidence, *(f"vision:{item}" for item in decision.evidence_frame_ids)),
        confidence=decision.confidence,
        window_id=seed.window_id,
        decision_revision=decision.decision_revision,
    )


def luna_request_sha256(
    window: LunaWindow,
    candidates: Sequence[CandidateSeed],
    frames: Sequence[LunaFrame],
) -> str:
    return _digest(
        {
            "candidate_ids": list(window.candidate_ids),
            "candidates": [
                {
                    "candidate_id": row.candidate_id,
                    "clip_instance_id": row.clip_instance_id,
                    "evidence": list(row.evidence),
                    "reason": row.reason,
                    "source_range": [str(row.source_range.start), str(row.source_range.end)],
                    "timeline_range": [
                        str(row.timeline_range.start),
                        str(row.timeline_range.end),
                    ],
                    "window_id": row.window_id,
                }
                for row in candidates
            ],
            "frame_ids": list(window.frame_ids),
            "frame_sha256s": [frame.sha256 for frame in frames],
            "frame_source_times": [_fraction_text(frame.source_time) for frame in frames],
            "frame_timeline_times": [_fraction_text(frame.timeline_time) for frame in frames],
            "input_revision": window.input_revision,
            "source_hashes": list(window.source_hashes),
            "transcript_context": window.transcript_context,
            "window_id": window.window_id,
        }
    )


def luna_cache_key(
    *,
    prompt_revision: str,
    model: str,
    settings_sha256: str,
    request_sha256: str,
) -> str:
    return _digest(
        {
            "model": model,
            "prompt_revision": prompt_revision,
            "request_sha256": request_sha256,
            "settings_sha256": settings_sha256,
        }
    )


def effective_analysis_settings_sha256(
    settings: AnalysisSettings,
    *,
    engine_config: Mapping[str, object],
    project_config: Mapping[str, object],
    media_overrides_sha256: str,
    source_sha256s: tuple[str, ...],
    endpoint_sha256: str,
    workspace_binding_sha256: str,
) -> str:
    """유효 분석 설정의 지문 — 엔드포인트와 작업공간 자리도 함께 묶는다(S2B-2b).

    엔드포인트가 바뀌면 같은 추정이 아니고(승인이 다른 공급자를 열지 않는다),
    작업공간 자리가 바뀌어도 같은 추정이 아니다(다른 개발 루트의 같은 job이
    낡은 승인으로 유료 재실행을 여는 길을 닫는다). 둘 다 **다이제스트만** 들어
    간다 — 값이나 경로 원문은 지문 밖에 없다.
    """
    return _digest(
        {
            "analysis_settings_sha256": settings.sha256(),
            "endpoint_sha256": endpoint_sha256,
            "engine_config": dict(engine_config),
            "media_overrides_sha256": media_overrides_sha256,
            "project_config": dict(project_config),
            "source_sha256s": list(source_sha256s),
            "workspace_binding_sha256": workspace_binding_sha256,
        }
    )


class CostCeilingExceeded(RuntimeError):
    """기록된 실제 사용량이 승인된 비용 상한을 넘었다 — 다음 호출을 시작하지 않는다."""


class UsageLedger:
    """실제 사용량의 누계가 승인 상한을 넘는 순간 닫히는 게이트.

    기록(`record`)은 언제나 남긴다 — 초과 분을 숨기면 안 되므로 UsageLedger에
    더한 뒤에 판정한다. 예외는 "다음 호출" 앞에서 일어난다(호출 루프가 멈춘다).
    """

    def __init__(self, limit: Decimal) -> None:
        if not limit.is_finite() or limit < 0:
            raise ValueError("비용 상한은 유한한 0 이상이어야 한다")
        self.limit = limit
        self.spent = Decimal(0)

    def record(self, usage: ActualUsage) -> None:
        self.spent += Decimal(usage.cost)
        if self.spent > self.limit:
            raise CostCeilingExceeded(
                f"실제 사용량 {_decimal_text(self.spent)}USD가 승인 상한 "
                f"{_decimal_text(self.limit)}USD를 넘었다 — 남은 호출을 중단한다"
            )


def luna_usage(
    *,
    window: LunaWindow,
    raw: Mapping[str, object],
    pricing: PricingSnapshot,
    attempt_id: str,
) -> ActualUsage | None:
    """Responses 응답의 `usage` 블록에서 이 창의 실제 사용량을 만든다.

    usage 블록이 없거나 숫자가 아니면 `None`이다 — 없는 사용량을 0으로 꾸미지
    않는다(기록 없음과 0USD는 다른 사실이다).
    """
    usage = raw.get("usage") if isinstance(raw, Mapping) else None
    if not isinstance(usage, Mapping):
        return None
    input_tokens = usage.get("input_tokens")
    output_tokens = usage.get("output_tokens")
    if (
        isinstance(input_tokens, bool)
        or not isinstance(input_tokens, int)
        or isinstance(output_tokens, bool)
        or not isinstance(output_tokens, int)
    ):
        return None
    cost = (
        Decimal(input_tokens) * Decimal(pricing.luna_input_per_million_tokens)
        + Decimal(output_tokens) * Decimal(pricing.luna_output_per_million_tokens)
    ) / Decimal(1_000_000)
    return ActualUsage.create(
        input_revision=window.input_revision,
        attempt_id=attempt_id,
        window_id=window.window_id,
        model=pricing.luna_model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost=_decimal_text(cost),
    )


def transcription_usage(
    *,
    input_revision: str,
    attempt_id: str,
    model: str,
    audio_seconds: Fraction,
    per_minute: str,
) -> ActualUsage:
    if audio_seconds < 0:
        raise ValueError("전사 사용량 오디오 길이는 0 이상이어야 한다")
    cost = _fraction_decimal(audio_seconds) / Decimal(60) * Decimal(per_minute)
    return ActualUsage.create(
        input_revision=input_revision,
        attempt_id=attempt_id,
        window_id=None,
        model=model,
        input_tokens=0,
        output_tokens=0,
        audio_seconds=audio_seconds,
        cost=_decimal_text(cost),
    )


def build_candidate_preview(
    candidate: CutCandidate, frame: LunaFrame, thumbnail_path: Path
) -> CandidatePreview:
    """검토 화면의 미리보기 — 후보·프레임·썸네일 해시가 한 리비전으로 묶인다."""
    return CandidatePreview.create(
        candidate_id=candidate.candidate_id,
        input_revision=candidate.input_revision,
        frame_id=frame.frame_id,
        thumbnail_path=thumbnail_path,
        thumbnail_sha256=frame.sha256,
        reason=candidate.reason,
        confidence=candidate.confidence,
        removal_duration=candidate.timeline_range.end - candidate.timeline_range.start,
    )


def _require_window(
    window: LunaWindow, candidates: Sequence[CandidateSeed], frames: Sequence[LunaFrame]
) -> None:
    if not candidates or tuple(row.candidate_id for row in candidates) != window.candidate_ids:
        raise ValueError("Luna 창 후보가 요청 후보 ID와 다르다")
    if not frames or len(frames) > 6:
        raise ValueError("Luna 프레임은 1~6장이어야 한다")
    by_id: dict[str, LunaFrame] = {}
    for frame in frames:
        if frame.frame_id in by_id or hashlib.sha256(frame.jpeg_bytes).hexdigest() != frame.sha256:
            raise ValueError("Luna 프레임 ID가 중복되었거나 바이트 해시가 다르다")
        by_id[frame.frame_id] = frame
    if tuple(by_id) != window.frame_ids:
        raise ValueError("Luna 프레임 순서/ID가 요청과 다르다")


def _window_id(input_revision: str, evidence: TranscriptEvidence, window_seconds: Fraction) -> str:
    index = evidence.timeline_range.start // window_seconds
    return hashlib.sha256(
        f"vlogsrt-luna-window-v1\0{input_revision}\0{evidence.clip_instance_id}\0{index}".encode()
    ).hexdigest()


def _response_hash(raw: Mapping[str, object] | BaseException) -> str:
    if isinstance(raw, BaseException):
        material: object = {"error_type": raw.__class__.__name__}
    else:
        material = raw
    return hashlib.sha256(
        json.dumps(material, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()


def _normalize_text(text: str) -> str:
    return _TEXT_CHARS.sub("", text.lower())


def _similarity(left: str, right: str) -> float:
    return difflib.SequenceMatcher(None, left, right, autojunk=False).ratio()


def _frame_floor(
    value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()
) -> Fraction:
    frames = (value - anchor) / frame_duration
    return anchor + (frames.numerator // frames.denominator) * frame_duration


def _frame_ceil(
    value: Fraction, frame_duration: Fraction, anchor: Fraction = Fraction()
) -> Fraction:
    frames = (value - anchor) / frame_duration
    return anchor + -(-frames.numerator // frames.denominator) * frame_duration


def _ceil_fraction(value: Fraction) -> int:
    return -(-value.numerator // value.denominator)


def _fraction_decimal(value: Fraction) -> Decimal:
    return Decimal(value.numerator) / Decimal(value.denominator)


def _decimal_text(value: Decimal) -> str:
    if value == 0:
        return "0"
    return format(value.normalize(), "f")


def _setting_value(value: object) -> object:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    if isinstance(value, Decimal):
        return _decimal_text(value)
    return value


def _fraction_text(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
