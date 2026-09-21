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
from typing import Any

from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.workflow_protocol import (
    AnalysisEstimate,
    CutCandidate,
    ProjectRevision,
    RationalRange,
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
    low_detail_image_tokens: int = 85
    transcription_call_multiplier: int = 4
    review_confidence: Decimal = Decimal("0.90")
    retake_horizon_seconds: Fraction = Fraction(30)
    retake_similarity: Decimal = Decimal("0.72")
    prompt_revision: str = _PROMPT_REVISION

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
        if self.transcription_call_multiplier < 1:
            raise ValueError("전사 호출 배수는 1 이상이어야 한다")
        for name in ("review_confidence", "retake_similarity"):
            value = getattr(self, name)
            if not value.is_finite() or not Decimal(0) <= value <= Decimal(1):
                raise ValueError(f"{name}는 유한한 0~1이어야 한다")

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
    source_audio_cost_per_minute: Decimal | None = None,
    vision_cost_per_image: Decimal | None = None,
    settings_sha256: str | None = None,
) -> AnalysisEstimate:
    """네트워크 없이 고유 소스 오디오와 최악의 창/이미지/토큰 사용량을 센다."""
    if document.input_sha256 != revision.input_sha256:
        raise ValueError("비용 추정의 문서와 입력 리비전이 다르다")
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
    cost: Decimal | None = None
    if source_audio_cost_per_minute is not None and vision_cost_per_image is not None:
        if source_audio_cost_per_minute < 0 or vision_cost_per_image < 0:
            raise ValueError("비용 가정은 0 이상이어야 한다")
        cost = (
            Decimal(transcription_seconds.numerator)
            / Decimal(transcription_seconds.denominator)
            / Decimal(60)
            * source_audio_cost_per_minute
            + Decimal(max_images) * vision_cost_per_image
        )
    return AnalysisEstimate.create(
        input_revision=revision.revision,
        model=settings.model,
        unique_source_audio_seconds=unique_source_audio_seconds,
        transcription_seconds=transcription_seconds,
        max_windows=max_windows,
        max_images=max_images,
        max_image_tokens=max_images * settings.low_detail_image_tokens,
        cost_upper_bound=_decimal_text(cost) if cost is not None else None,
        settings_sha256=settings_sha256 or settings.sha256(),
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
    window: LunaWindow, candidates: Sequence[CandidateSeed], frames: Sequence[LunaFrame]
) -> dict[str, object]:
    """Responses API input. 경로와 자격증명은 받지도 직렬화하지도 않는다."""
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
                "type": "input_image",
                "detail": "low",
                "image_url": "data:image/jpeg;base64,"
                + base64.b64encode(frame.jpeg_bytes).decode("ascii"),
            }
        )
    return {"input": [{"role": "user", "content": content}]}


def normalize_luna_response(
    window: LunaWindow, raw: Mapping[str, object] | BaseException
) -> VisionWindowDecision:
    """모델/API 결함은 예외 없이 keep으로 닫고 원문 해시만 남긴다."""
    response_sha256 = _response_hash(raw)
    try:
        if isinstance(raw, BaseException):
            raise ValueError(raw.__class__.__name__)
        text = raw.get("output_text")
        if not isinstance(text, str) or not text:
            raise ValueError("empty output")
        body = json.loads(text)
        if not isinstance(body, dict):
            raise ValueError("response body")
        choice = body.get("choice")
        confidence = body.get("confidence")
        evidence = body.get("evidence_frame_ids")
        if not isinstance(choice, str):
            raise ValueError("choice")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("confidence")
        confidence_float = float(confidence)
        if not math.isfinite(confidence_float) or not 0 <= confidence_float <= 1:
            raise ValueError("confidence range")
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise ValueError("evidence")
        if len(evidence) != len(set(evidence)) or not set(evidence) <= set(window.frame_ids):
            raise ValueError("evidence identity")
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
            raise ValueError("unknown candidate")
        return VisionWindowDecision.create(
            input_revision=window.input_revision,
            window_id=window.window_id,
            choice="propose_cut",
            candidate_id=choice,
            evidence_frame_ids=tuple(evidence),
            confidence=confidence_text,
            response_sha256=response_sha256,
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return VisionWindowDecision.create(
            input_revision=window.input_revision,
            window_id=window.window_id,
            choice="keep",
            candidate_id=None,
            evidence_frame_ids=(),
            confidence="0",
            response_sha256=response_sha256,
        )


def finalize_candidate(seed: CandidateSeed, decision: VisionWindowDecision) -> CutCandidate | None:
    if (
        decision.choice != "propose_cut"
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
) -> str:
    return _digest(
        {
            "analysis_settings_sha256": settings.sha256(),
            "engine_config": dict(engine_config),
            "media_overrides_sha256": media_overrides_sha256,
            "project_config": dict(project_config),
            "source_sha256s": list(source_sha256s),
        }
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


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    ).hexdigest()
