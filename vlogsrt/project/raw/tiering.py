"""3단 등급 — 자막(소리)과 영상을 **따로** 판정한 뒤 표 하나로 합친다(W-11, D51).

순수 함수만 산다: 프로세스·DB·서비스·파일 읽기가 없다. 입력은 저장된 증거 문서와,
호출자가 미리 잰 값(공백 RMS·프레임 화질)이다. 같은 입력이면 같은 보고서가 나온다 —
유료 호출 0회로 언제든 다시 계산한다(분석 지문에 들어가지 않는다).

지키는 선:

- **빨강은 제안이다.** 측정된 무음과 측정된 영상 결함만 빨강이 된다. 빨강은 자동 제외
  후보로만 쓰이고, 사람이 확인하기 전에는 출력이 닫힌다(D35, 규칙 9).
- **모름은 결함이 아니다.** 프레임이 없거나(추출 실패) 판정이 실패했거나 아직 없으면
  노랑 + 판정 보류다. 전사 실패 구간은 절대 무음이 아니다.
- **구간은 빈틈없이 덮는다.** 보고서의 구간은 `[0, extent]`를 겹침·틈 없이 타일한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from fractions import Fraction
from typing import Any

from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    RawTierSettings,
    validate_tier_settings,
)
from vlogsrt.project.raw.suitability import rational

TIER_RED = "red"
TIER_YELLOW = "yellow"
TIER_GREEN = "green"
TIERS = (TIER_GREEN, TIER_YELLOW, TIER_RED)

RED_SILENCE = "tier_silence"
RED_VIDEO_DEFECT = "tier_video_defect"

AUDIO_SPEECH_DENSE = "speech_dense"
AUDIO_SPEECH_SPARSE = "speech_sparse"
AUDIO_AMBIENT = "ambient_no_speech"
AUDIO_QUIET = "quiet_silence"
AUDIO_UNCONFIRMED = "unconfirmed_empty"
AUDIO_FAILED = "analysis_failed"
AUDIO_ABSENT = "audio_absent"
AUDIO_UNMEASURED = "unmeasured"
AUDIO_STATUSES = (
    AUDIO_SPEECH_DENSE,
    AUDIO_SPEECH_SPARSE,
    AUDIO_AMBIENT,
    AUDIO_QUIET,
    AUDIO_UNCONFIRMED,
    AUDIO_FAILED,
    AUDIO_ABSENT,
    AUDIO_UNMEASURED,
)

VIDEO_GOOD = "good"
VIDEO_USABLE = "usable"
VIDEO_FLAGGED = "flagged"
VIDEO_DEFECT = "defect"
VIDEO_NO_FRAMES = "no_frames"
VIDEO_FAILED = "failed"
VIDEO_UNANALYZED = "unanalyzed"
VIDEO_STATUSES = (
    VIDEO_GOOD,
    VIDEO_USABLE,
    VIDEO_FLAGGED,
    VIDEO_UNANALYZED,
    VIDEO_NO_FRAMES,
    VIDEO_FAILED,
    VIDEO_DEFECT,
)
"""심각도 오름차순 — 한 구간에 창이 여럿이면 가장 나쁜 쪽이 이긴다."""

VIDEO_UNKNOWN = frozenset({VIDEO_NO_FRAMES, VIDEO_FAILED, VIDEO_UNANALYZED})

AUDIO_LABELS = {
    AUDIO_SPEECH_DENSE: "발화 많음",
    AUDIO_SPEECH_SPARSE: "발화 적음",
    AUDIO_AMBIENT: "현장음",
    AUDIO_QUIET: "무음(측정)",
    AUDIO_UNCONFIRMED: "발화 없음(미측정)",
    AUDIO_FAILED: "전사 실패",
    AUDIO_ABSENT: "소리 없음",
    AUDIO_UNMEASURED: "측정 전",
}
VIDEO_LABELS = {
    VIDEO_GOOD: "좋음",
    VIDEO_USABLE: "사용 가능",
    VIDEO_FLAGGED: "확인 권고",
    VIDEO_DEFECT: "결함(측정)",
    VIDEO_NO_FRAMES: "프레임 없음",
    VIDEO_FAILED: "판정 실패",
    VIDEO_UNANALYZED: "판정 전",
}
TIER_LABELS = {TIER_GREEN: "추천", TIER_YELLOW: "보통", TIER_RED: "제외 제안"}

TIER_TAG_PREFIX = "등급: "
AUDIO_TAG_PREFIX = "자막: "
VIDEO_TAG_PREFIX = "영상: "
MARKER_PREFIX = "등급판 "
LEGACY_GRADE_PREFIX = "Luna 평가: "
GRADE_TAG_PREFIXES = (
    LEGACY_GRADE_PREFIX,
    TIER_TAG_PREFIX,
    AUDIO_TAG_PREFIX,
    VIDEO_TAG_PREFIX,
    MARKER_PREFIX,
)

_REASON_SEPARATOR = " · "
_ANNOTATION_ACCEPTED = "accepted"
_AUDIO_ABSENT_STATUS = "audio_absent"
_SCENE_CANDIDATE_KIND = "verified_scene"
_FRAMES_MISSING_ERROR = "frames_missing"
"""프레임이 한 장도 없어 Luna를 부르지 않은 창의 실패 코드 — 결함이 아니라 '모름'이다."""


def tier_marker(settings: RawTierSettings = TIER_SETTINGS) -> str:
    """이 등급판으로 매긴 조각의 표지 — 표지가 없는 검토 머리는 옮겨야 한다(Q8)."""
    return MARKER_PREFIX + settings.tier_version


def strip_grade_tags(tags: Sequence[str]) -> tuple[str, ...]:
    """옛 Luna 등급과 등급판 태그를 걷는다 — 다시 매기기 전에 한 번만 부른다."""
    return tuple(tag for tag in tags if not tag.startswith(GRADE_TAG_PREFIXES))


def _rational_json(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


# ── 판정 값 ───────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AudioJudgment:
    """자막(소리) 쪽 판정 — 밀도는 발화가 덮은 길이 ÷ 구간 길이다."""

    status: str
    density: Fraction
    rms_dbfs: float | None
    reason: str
    evidence: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "label": AUDIO_LABELS[self.status],
            "reason": self.reason,
            "density": _rational_json(self.density),
            "rms_dbfs": self.rms_dbfs,
            "evidence": list(self.evidence),
        }


@dataclass(frozen=True)
class VideoJudgment:
    """영상 쪽 판정 — Luna의 구조화된 칸과 로컬 화질 측정에서만 온다."""

    status: str
    reason: str
    evidence: tuple[str, ...]
    windows: tuple[str, ...]

    def document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "label": VIDEO_LABELS[self.status],
            "reason": self.reason,
            "evidence": list(self.evidence),
            "windows": list(self.windows),
        }


@dataclass(frozen=True)
class TierSpan:
    """보고서의 구간 하나 — 두 판정과 표가 낸 등급을 함께 싣는다."""

    start: Fraction
    end: Fraction
    tier: str
    rule: str
    audio: AudioJudgment
    video: VideoJudgment
    red_reason: str | None

    @property
    def hold(self) -> bool:
        """판정 보류 — 증거가 없어 노랑인 구간(점선으로 보인다, Q4·Q6)."""
        return self.tier == TIER_YELLOW and (
            self.video.status in VIDEO_UNKNOWN or self.audio.status == AUDIO_FAILED
        )

    @property
    def recommendation(self) -> str:
        return span_recommendation(self)

    def document(self) -> dict[str, object]:
        return {
            "start": _rational_json(self.start),
            "end": _rational_json(self.end),
            "tier": self.tier,
            "rule": self.rule,
            "hold": self.hold,
            "red_reason": self.red_reason,
            "recommendation": self.recommendation,
            "reason": f"자막: {self.audio.reason} · 영상: {self.video.reason}",
            "audio": self.audio.document(),
            "video": self.video.document(),
        }


@dataclass(frozen=True)
class TierReport:
    """원본 하나의 등급 보고서 — 설정과 근거 문서의 해시를 함께 묶는다."""

    tier_version: str
    settings: RawTierSettings
    document_sha256: str
    spans: tuple[TierSpan, ...]
    unmaterialized_red: int = 0

    def document(self) -> dict[str, object]:
        return {
            "tier_version": self.tier_version,
            "settings": asdict(self.settings),
            "document_sha256": self.document_sha256,
            "spans": [span.document() for span in self.spans],
        }


def span_recommendation(span: TierSpan) -> str:
    """구간 하나의 닫힌 권고 집합 값 — 검토 조각 등급과 같은 규칙이다."""
    if span.tier == TIER_GREEN:
        return "recommended"
    if span.video.status in (VIDEO_FAILED, VIDEO_UNANALYZED) or span.audio.status == AUDIO_FAILED:
        return "review_required"
    return "normal"


# ── 증거 정리 ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Window:
    window_id: str
    start: Fraction
    end: Fraction
    frame_ids: tuple[str, ...]
    annotation: Mapping[str, Any] | None


@dataclass(frozen=True)
class SourceEvidence:
    """원본 하나의 판정 재료 — 문서를 한 번만 읽어 정리한 값이다."""

    extent: Fraction
    audio_state: str
    speech: tuple[tuple[Fraction, Fraction, str], ...]
    failed: tuple[tuple[Fraction, Fraction, str], ...]
    gaps: Mapping[tuple[Fraction, Fraction], float] | None
    windows: tuple[_Window, ...]
    frames_known: bool
    present_frames: frozenset[str]
    local_video: Mapping[str, Mapping[str, float]]
    settings: RawTierSettings


def extent_of(document: Mapping[str, Any]) -> Fraction:
    """덮을 길이 — 창 계획이 정한 공통 길이(진행 문서에도 있다)를 먼저 읽는다."""
    for key in ("windows", "segmentation"):
        section = document.get(key)
        if isinstance(section, Mapping) and isinstance(section.get("extent"), Mapping):
            return rational(section["extent"])
    cores = [rational(w["core_end"]) for w in window_rows(document)]
    return max(cores, default=Fraction(0))


def window_rows(document: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    section = document.get("windows")
    rows = section.get("windows", []) if isinstance(section, Mapping) else []
    return [row for row in rows if isinstance(row, Mapping)]


def _audio_state(document: Mapping[str, Any]) -> str:
    audio = document.get("audio")
    if not isinstance(audio, Mapping):
        return "unknown"
    return "absent" if audio.get("status") == _AUDIO_ABSENT_STATUS else "present"


def _spoken(document: Mapping[str, Any]) -> tuple[tuple[Fraction, Fraction, str], ...]:
    rows = []
    for row in document.get("utterances", []) or []:
        if not isinstance(row, Mapping) or not str(row.get("text", "")).strip():
            continue
        start, end = rational(row["start"]), rational(row["end"])
        if end > start:
            rows.append((start, end, str(row.get("utterance_id", ""))))
    return tuple(sorted(rows))


def _sample_rate(document: Mapping[str, Any]) -> int:
    for key in ("asr", "audio"):
        section = document.get(key)
        if isinstance(section, Mapping) and isinstance(section.get("sample_rate"), int):
            return int(section["sample_rate"])
    return 0


def failed_chunks(
    document: Mapping[str, Any], *, audio_start: Fraction = Fraction(0)
) -> tuple[tuple[Fraction, Fraction, str], ...]:
    """실패한 ASR 청크의 공통 시계 구간 — 이 구간은 절대 무음으로 읽지 않는다."""
    asr = document.get("asr")
    rate = _sample_rate(document)
    if not isinstance(asr, Mapping) or rate < 1:
        return ()
    rows = []
    for chunk in asr.get("chunks", []) or []:
        if isinstance(chunk, Mapping) and chunk.get("failure"):
            start = audio_start + Fraction(int(chunk["sample_start"]), rate)
            end = audio_start + Fraction(int(chunk["sample_end"]), rate)
            rows.append((start, end, f"asr:chunk-{int(chunk.get('chunk_index', 0)):04d}"))
    return tuple(sorted(rows))


def _frames(document: Mapping[str, Any]) -> tuple[bool, frozenset[str]]:
    """프레임 증거 — 진행 문서처럼 칸 자체가 없으면 '모름'이지 '없음'이 아니다."""
    section = document.get("frames")
    if not isinstance(section, Mapping):
        return False, frozenset()
    failed = {str(row.get("frame_id")) for row in section.get("failures", []) or []}
    present = {
        str(row.get("frame_id"))
        for row in section.get("frames", []) or []
        if isinstance(row, Mapping)
    }
    return True, frozenset(present - failed)


def _windows(document: Mapping[str, Any]) -> tuple[_Window, ...]:
    annotations = {
        str(row.get("window_id")): row
        for row in document.get("annotations", []) or []
        if isinstance(row, Mapping)
    }
    return tuple(
        _Window(
            window_id=str(row["window_id"]),
            start=rational(row["core_start"]),
            end=rational(row["core_end"]),
            frame_ids=tuple(str(f) for f in row.get("frame_ids", []) or []),
            annotation=annotations.get(str(row["window_id"])),
        )
        for row in window_rows(document)
    )


def evidence_of(
    document: Mapping[str, Any],
    *,
    gap_levels: Mapping[tuple[Fraction, Fraction], float] | None,
    local_video: Mapping[str, Mapping[str, float]] | None = None,
    settings: RawTierSettings = TIER_SETTINGS,
    audio_start: Fraction = Fraction(0),
) -> SourceEvidence:
    """판정 재료를 한 번에 정리한다 — `gap_levels=None`은 '소리를 재지 않았다'이다."""
    frames_known, present = _frames(document)
    return SourceEvidence(
        extent=extent_of(document),
        audio_state=_audio_state(document),
        speech=_spoken(document),
        failed=failed_chunks(document, audio_start=audio_start),
        gaps=None if gap_levels is None else dict(gap_levels),
        windows=_windows(document),
        frames_known=frames_known,
        present_frames=present,
        local_video=dict(local_video or {}),
        settings=settings,
    )


def _merged_speech(
    speech: Sequence[tuple[Fraction, Fraction, str]],
) -> list[tuple[Fraction, Fraction]]:
    merged: list[tuple[Fraction, Fraction]] = []
    for start, end, _ in sorted(speech):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def speech_gaps(
    document: Mapping[str, Any],
    settings: RawTierSettings = TIER_SETTINGS,
    *,
    audio_start: Fraction = Fraction(0),
) -> tuple[tuple[Fraction, Fraction], ...]:
    """긴 발화 공백(`silence_min_seconds` 이상) — 발화 쪽만 여유를 남긴 범위.

    머리·꼬리 공백도 공백이다. 여유는 발화를 지키려는 것이므로 원본 끝 쪽은 깎지 않는다.
    이보다 짧은 쉼은 발화 구역 안의 쉼이다 — 밀도에만 들어가고 구간을 자르지 않는다.
    """
    state = _audio_state(document)
    if state == "absent":
        return ()
    low, high = (
        _audio_coverage(document, audio_start)
        if state == "present"
        else (Fraction(0), extent_of(document))
    )
    handle = Fraction(str(settings.speech_handle_seconds))
    minimum = Fraction(str(settings.silence_min_seconds))
    gaps: list[tuple[Fraction, Fraction]] = []
    cursor, after_speech = low, False
    for start, end in [*_merged_speech(_spoken(document)), (high, high)]:
        gap_end = min(start, high)
        if gap_end - cursor >= minimum:
            left = cursor + (handle if after_speech else 0)
            gaps.append((left, gap_end - (handle if start < high else 0)))
        cursor, after_speech = max(cursor, end), True
    return tuple(gaps)


def quiet_gap_candidates(
    document: Mapping[str, Any],
    settings: RawTierSettings = TIER_SETTINGS,
    *,
    audio_start: Fraction = Fraction(0),
) -> tuple[tuple[Fraction, Fraction], ...]:
    """PCM으로 잴 공백 — 소리가 있고, 실패 청크에 닿지 않는 긴 공백만이다."""
    if _audio_state(document) != "present":
        return ()
    failed = failed_chunks(document, audio_start=audio_start)
    return tuple(
        (start, end)
        for start, end in speech_gaps(document, settings, audio_start=audio_start)
        if not any(a < end and b > start for a, b, _ in failed)
    )


def _audio_coverage(
    document: Mapping[str, Any], audio_start: Fraction
) -> tuple[Fraction, Fraction]:
    """소리가 실제로 있는 공통 시계 범위 — 원본 길이 안으로 자른다."""
    audio = document.get("audio")
    length = Fraction(0)
    if isinstance(audio, Mapping) and isinstance(audio.get("extent"), Mapping):
        length = rational(audio["extent"])
    extent = extent_of(document)
    return max(Fraction(0), audio_start), min(extent, audio_start + length)


# ── 경계 ─────────────────────────────────────────────────────────────────


def atomic_boundaries(
    document: Mapping[str, Any],
    settings: RawTierSettings = TIER_SETTINGS,
    *,
    gap_levels: Mapping[tuple[Fraction, Fraction], float] | None = None,
    audio_start: Fraction = Fraction(0),
) -> tuple[Fraction, ...]:
    """구간을 자를 자리 — 긴 공백의 가장자리(발화 끝·시작 ± 여유)·창 중심·검증 장면·
    잰 공백·실패 청크 가장자리.

    발화 하나하나의 끝은 자르지 않는다 — 자르면 모든 원자 구간의 밀도가 0 아니면 1이 되어
    '띄엄띄엄 말한 구역'을 알아볼 수 없다. 저장된 `recommendation_boundaries`도 쓰지
    않는다 — 이 판정의 재료가 아닌 경계가 섞인다.
    """
    extent = extent_of(document)
    points = {Fraction(0), extent}
    gaps = speech_gaps(document, settings, audio_start=audio_start)
    points.update(p for gap in (*gaps, *(gap_levels or {})) for p in gap)
    points.update(p for w in _windows(document) for p in (w.start, w.end))
    points.update(
        p
        for start, end, _ in failed_chunks(document, audio_start=audio_start)
        for p in (start, end)
    )
    section = document.get("windows")
    candidates = section.get("boundary_candidates", []) if isinstance(section, Mapping) else []
    points.update(
        rational(row["position"])
        for row in candidates or []
        if isinstance(row, Mapping) and row.get("kind") == _SCENE_CANDIDATE_KIND
    )
    return tuple(sorted(p for p in points if 0 <= p <= extent))


# ── 자막(소리) 판정 ───────────────────────────────────────────────────────


def _percent(value: Fraction) -> int:
    return round(value * 100)


def _audio_reason(
    status: str, density: Fraction, rms: float | None, settings: RawTierSettings
) -> str:
    level = "" if rms is None else f" {rms:.1f} dBFS"
    return {
        AUDIO_SPEECH_DENSE: f"발화가 구간의 {_percent(density)}%를 덮는다",
        AUDIO_SPEECH_SPARSE: f"발화가 구간의 {_percent(density)}%뿐이다",
        AUDIO_AMBIENT: f"발화 없음 · 현장음{level}",
        AUDIO_QUIET: f"발화 없음 · 무음{level} ≤ {settings.silence_rms_dbfs:g} dBFS",
        AUDIO_UNCONFIRMED: "발화 없음 · 소리 미측정",
        AUDIO_FAILED: "전사 실패 구간 — 무음으로 보지 않는다",
        AUDIO_ABSENT: "오디오 트랙이 없다",
        AUDIO_UNMEASURED: "소리를 아직 재지 않았다(분석 중)",
    }[status]


def _audio(
    status: str,
    settings: RawTierSettings,
    *,
    density: Fraction = Fraction(0),
    rms: float | None = None,
    evidence: tuple[str, ...] = (),
) -> AudioJudgment:
    return AudioJudgment(
        status, density, rms, _audio_reason(status, density, rms, settings), evidence
    )


def _speech_cover(
    evidence: SourceEvidence, start: Fraction, end: Fraction
) -> tuple[Fraction, tuple[str, ...]]:
    touching = [row for row in evidence.speech if row[0] < end and row[1] > start]
    clipped = [(max(a, start), min(b, end), uid) for a, b, uid in touching]
    covered = sum((b - a for a, b in _merged_speech(clipped)), Fraction(0))
    return covered, tuple(uid for _, _, uid in touching)


def judge_audio(evidence: SourceEvidence, start: Fraction, end: Fraction) -> AudioJudgment:
    """구간 하나의 소리 판정 — 순서가 곧 우선순위다(부재 → 실패 → 발화 → 잰 공백)."""
    settings = evidence.settings
    if evidence.audio_state == "absent":
        return _audio(AUDIO_ABSENT, settings)
    failed = tuple(label for a, b, label in evidence.failed if a < end and b > start)
    if failed:
        return _audio(AUDIO_FAILED, settings, evidence=failed)
    covered, ids = _speech_cover(evidence, start, end)
    if covered > 0:
        density = covered / (end - start)
        dense = density >= Fraction(str(settings.speech_density_green))
        status = AUDIO_SPEECH_DENSE if dense else AUDIO_SPEECH_SPARSE
        return _audio(status, settings, density=density, evidence=ids)
    if evidence.audio_state != "present" or evidence.gaps is None:
        return _audio(AUDIO_UNMEASURED, settings)
    return _gap_judgment(evidence, start, end)


def _gap_judgment(evidence: SourceEvidence, start: Fraction, end: Fraction) -> AudioJudgment:
    settings = evidence.settings
    shortest = Fraction(str(settings.silence_min_seconds)) - 2 * Fraction(
        str(settings.speech_handle_seconds)
    )
    for (a, b), rms in sorted((evidence.gaps or {}).items()):
        if a <= start and end <= b:
            quiet = rms <= settings.silence_rms_dbfs and b - a >= shortest
            return _audio(
                AUDIO_QUIET if quiet else AUDIO_AMBIENT,
                settings,
                rms=rms,
                evidence=(f"gap:{a}-{b}", f"pcm:rms={rms:.1f}dBFS"),
            )
    return _audio(AUDIO_UNCONFIRMED, settings)


# ── 영상 판정 ────────────────────────────────────────────────────────────


def _video(status: str, reason: str, evidence: tuple[str, ...], window: str) -> VideoJudgment:
    return VideoJudgment(status, reason, evidence, (window,))


def _local_defect(
    evidence: SourceEvidence, frames: Sequence[str]
) -> tuple[str, tuple[str, ...]] | None:
    """저장 프레임의 로컬 측정 — 검은 화면·흐림이 창의 일정 비율 이상일 때만 결함이다."""
    settings = evidence.settings
    measured = [(f, evidence.local_video[f]) for f in frames if f in evidence.local_video]
    if not measured:
        return None
    black = [f for f, row in measured if float(row["yavg"]) <= settings.black_luma_max]
    if Fraction(len(black), len(measured)) >= Fraction(str(settings.black_frame_fraction)):
        return f"검은 화면 {len(black)}/{len(measured)}장", tuple(f"black:{f}" for f in black)
    if settings.blur_min > 0:
        blurry = [f for f, row in measured if float(row.get("blur", 0.0)) >= settings.blur_min]
        if Fraction(len(blurry), len(measured)) >= Fraction(str(settings.blur_frame_fraction)):
            return f"흐림 {len(blurry)}/{len(measured)}장", tuple(f"blur:{f}" for f in blurry)
    return None


def _luna_notes(annotation: Mapping[str, Any], partial: bool) -> str:
    """Luna의 불확실성·검토 요청은 등급을 내리지 않고 이유에만 붙는다(D51)."""
    notes = [str(item) for item in annotation.get("uncertainties", []) or []]
    text = ""
    if notes:
        text += " · 확인 메모: " + "; ".join(notes)
    if annotation.get("needs_review"):
        text += " · Luna 검토 요청"
    if partial:
        text += " · 프레임 일부 누락"
    return text


def _window_judgment(evidence: SourceEvidence, window: _Window) -> VideoJudgment:
    row = window.annotation
    wid = window.window_id
    if row is None:
        return _video(VIDEO_UNANALYZED, "Luna 판정 전", (), wid)
    if row.get("error_code") == _FRAMES_MISSING_ERROR:
        reason = "프레임 증거 없음 · 재분석 전까지 판정 보류"
        return _video(VIDEO_NO_FRAMES, reason, (f"window:{wid}",), wid)
    if row.get("status") != _ANNOTATION_ACCEPTED:
        reason = str(row.get("defect") or row.get("error_code") or "Luna 판정 실패")
        return _video(VIDEO_FAILED, reason, (f"window:{wid}",), wid)
    if not row.get("suitability_evaluated", "suitability" in row):
        return _video(VIDEO_UNANALYZED, "사용성 평가가 없는 판정", (f"window:{wid}",), wid)
    frames = window.frame_ids
    if evidence.frames_known:
        frames = tuple(f for f in window.frame_ids if f in evidence.present_frames)
        if not frames:
            reason = "프레임 증거 없음 · 재분석 전까지 판정 보류"
            return _video(VIDEO_NO_FRAMES, reason, (f"window:{wid}",), wid)
    defect = _local_defect(evidence, frames)
    if defect is not None:
        return _video(VIDEO_DEFECT, defect[0], defect[1], wid)
    status = {"recommended": VIDEO_GOOD, "usable": VIDEO_USABLE}.get(
        str(row.get("suitability")), VIDEO_FLAGGED
    )
    reason = str(row.get("suitability_reason") or VIDEO_LABELS[status])
    reason += _luna_notes(row, len(frames) < len(window.frame_ids))
    return _video(status, reason, (f"window:{wid}",), wid)


def judge_video(evidence: SourceEvidence, start: Fraction, end: Fraction) -> VideoJudgment:
    """구간을 덮는 창들의 판정 — 가장 나쁜 창이 이긴다."""
    covering = [w for w in evidence.windows if w.start < end and w.end > start]
    if not covering:
        return VideoJudgment(VIDEO_UNANALYZED, "Luna 창 밖", (), ())
    judged = [_window_judgment(evidence, window) for window in covering]
    worst = max(judged, key=lambda j: VIDEO_STATUSES.index(j.status))
    return replace(worst, windows=tuple(w.window_id for w in covering))


# ── 합치기 ───────────────────────────────────────────────────────────────


def combine(audio: AudioJudgment, video: VideoJudgment) -> tuple[str, str, str | None]:
    """두 판정 → (등급, 규칙, 빨강 이유). 영상 결함이 무음보다 먼저다(둘 다면 영상)."""
    if video.status == VIDEO_DEFECT:
        return TIER_RED, "V-defect", RED_VIDEO_DEFECT
    if audio.status == AUDIO_QUIET:
        return TIER_RED, "A-quiet", RED_SILENCE
    if video.status == VIDEO_GOOD and audio.status == AUDIO_SPEECH_DENSE:
        return TIER_GREEN, "both-good", None
    if video.status == VIDEO_GOOD and audio.status == AUDIO_ABSENT:
        return TIER_GREEN, "video-only-good", None
    return TIER_YELLOW, _yellow_rule(audio.status, video.status), None


def _yellow_rule(audio: str, video: str) -> str:
    if audio == AUDIO_FAILED:
        return "audio-failed"
    if video in VIDEO_UNKNOWN:
        return "video-unknown"
    by_audio = {
        AUDIO_SPEECH_SPARSE: "speech-sparse",
        AUDIO_AMBIENT: "scenery-no-speech",
        AUDIO_UNCONFIRMED: "speech-unconfirmed",
        AUDIO_UNMEASURED: "audio-unmeasured",
    }
    return by_audio.get(audio, f"video-{video}")


def _span(evidence: SourceEvidence, start: Fraction, end: Fraction) -> TierSpan:
    audio = judge_audio(evidence, start, end)
    video = judge_video(evidence, start, end)
    tier, rule, red = combine(audio, video)
    return TierSpan(start, end, tier, rule, audio, video, red)


def _dedupe(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _join_reasons(*reasons: str) -> str:
    parts = [part for reason in reasons for part in reason.split(_REASON_SEPARATOR)]
    return _REASON_SEPARATOR.join(dict.fromkeys(parts))


def _grow(host: TierSpan, other: TierSpan, settings: RawTierSettings, *, absorb: bool) -> TierSpan:
    """두 이웃 구간을 하나로 — 흡수면 판정 상태는 host의 것, 근거는 합친다."""
    first, second = sorted((host, other), key=lambda span: span.start)
    left, right = host.end - host.start, other.end - other.start
    density = (host.audio.density * left + other.audio.density * right) / (left + right)
    levels = [v for v in (host.audio.rms_dbfs, other.audio.rms_dbfs) if v is not None]
    rms = max(levels) if levels else None
    audio_evidence = (
        host.audio.evidence if absorb else _dedupe(first.audio.evidence, second.audio.evidence)
    )
    audio = AudioJudgment(
        host.audio.status,
        density,
        rms,
        _audio_reason(host.audio.status, density, rms, settings),
        audio_evidence,
    )
    video = VideoJudgment(
        host.video.status,
        host.video.reason if absorb else _join_reasons(first.video.reason, second.video.reason),
        host.video.evidence if absorb else _dedupe(first.video.evidence, second.video.evidence),
        _dedupe(first.video.windows, second.video.windows),
    )
    return replace(host, start=first.start, end=second.end, audio=audio, video=video)


def _key(span: TierSpan) -> tuple[str, str, str]:
    return span.tier, span.audio.status, span.video.status


def _merge(spans: Sequence[TierSpan], settings: RawTierSettings) -> list[TierSpan]:
    merged: list[TierSpan] = []
    for span in spans:
        if merged and _key(merged[-1]) == _key(span):
            merged[-1] = _grow(merged[-1], span, settings, absorb=False)
        else:
            merged.append(span)
    return merged


def _host_index(spans: Sequence[TierSpan], index: int) -> int | None:
    """짧은 조각을 받을 이웃 — 앞이 먼저, 첫 조각은 뒤. 빨강과 비빨강은 서로 못 먹는다."""
    is_red = spans[index].tier == TIER_RED
    for host in (index - 1, index + 1):
        if 0 <= host < len(spans) and (spans[host].tier == TIER_RED) == is_red:
            return host
    return None


def _absorb(spans: Sequence[TierSpan], settings: RawTierSettings) -> list[TierSpan]:
    minimum = Fraction(str(settings.min_span_seconds))
    items = list(spans)
    changed = True
    while changed:
        changed = False
        for index, span in enumerate(items):
            if span.end - span.start >= minimum:
                continue
            host = _host_index(items, index)
            if host is None:
                continue
            items[min(index, host)] = _grow(items[host], span, settings, absorb=True)
            del items[max(index, host)]
            changed = True
            break
    return items


def _require_tiling(spans: Sequence[TierSpan], extent: Fraction) -> None:
    if extent <= 0:
        if spans:
            raise ValueError("길이 0인 원본에 등급 구간이 생겼다")
        return
    edges = [spans[0].start] + [s.end for s in spans]
    contiguous = all(a.end == b.start for a, b in zip(spans, spans[1:], strict=False))
    if spans[0].start != 0 or spans[-1].end != extent or not contiguous or edges != sorted(edges):
        raise ValueError("등급 구간이 [0, extent]를 빈틈없이 덮지 못했다")


def _document_sha256(document: Mapping[str, Any]) -> str:
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def classify(
    document: Mapping[str, Any],
    *,
    gap_levels: Mapping[tuple[Fraction, Fraction], float] | None,
    local_video: Mapping[str, Mapping[str, float]] | None = None,
    settings: RawTierSettings = TIER_SETTINGS,
    audio_start: Fraction = Fraction(0),
    document_sha256: str | None = None,
) -> TierReport:
    """저장된 증거 → 등급 보고서. 결정적이고 설정을 함께 싣는다."""
    validate_tier_settings(settings)
    evidence = evidence_of(
        document,
        gap_levels=gap_levels,
        local_video=local_video,
        settings=settings,
        audio_start=audio_start,
    )
    points = atomic_boundaries(document, settings, gap_levels=gap_levels, audio_start=audio_start)
    spans = [_span(evidence, a, b) for a, b in zip(points, points[1:], strict=False)]
    spans = _merge(_absorb(_merge(spans, settings), settings), settings)
    _require_tiling(spans, evidence.extent)
    return TierReport(
        tier_version=settings.tier_version,
        settings=settings,
        document_sha256=document_sha256 or _document_sha256(document),
        spans=tuple(spans),
    )


# ── 소비자 ───────────────────────────────────────────────────────────────


def red_decisions(report: TierReport) -> list[dict[str, object]]:
    """빨강 구간을 자동 제외 후보 모양(`auto_decisions[]`)으로 — 제안일 뿐이다."""
    return [
        {
            "start": _rational_json(span.start),
            "end": _rational_json(span.end),
            "reason": span.red_reason,
            "evidence": list(
                _dedupe(
                    (
                        f"tier:{span.rule}",
                        f"audio:{span.audio.status}",
                        f"video:{span.video.status}",
                    ),
                    span.audio.evidence,
                    span.video.evidence,
                )
            ),
            "version": report.tier_version,
        }
        for span in report.spans
        if span.tier == TIER_RED
    ]


def _significant_spans(report: TierReport, start: Fraction, end: Fraction) -> list[TierSpan]:
    def overlap(span: TierSpan) -> Fraction:
        return min(end, span.end) - max(start, span.start)

    touching = [span for span in report.spans if overlap(span) > 0]
    floor = min(Fraction(str(report.settings.min_span_seconds)), end - start)
    significant = [span for span in touching if overlap(span) >= floor]
    return significant or sorted(touching, key=overlap)[-1:]


def segment_grade(
    report: TierReport, start: Fraction, end: Fraction
) -> tuple[str, tuple[str, ...]]:
    """검토 조각 하나의 권고와 등급 태그 — 권고는 닫힌 집합(recommended/normal/review_required).

    `min_span_seconds`보다 짧게 겹친 구간은 보지 않는다 — 빨강 끝을 프레임 안쪽으로 붙이면
    이웃 조각에 0.02초짜리 빨강 자투리가 남는데, 그것이 녹색 조각을 보통으로 끌어내리면 안
    된다. 조각이 그보다 짧거나 넉넉히 겹친 구간이 없으면 가장 많이 겹친 구간 하나로 본다.
    """
    rows = _significant_spans(report, start, end)
    tiers = {span.tier for span in rows}
    if rows and tiers == {TIER_GREEN}:
        recommendation, tier = "recommended", TIER_GREEN
    else:
        unknown = any(span.recommendation == "review_required" for span in rows)
        recommendation = "review_required" if unknown or not rows else "normal"
        tier = TIER_RED if tiers == {TIER_RED} else TIER_YELLOW
    audio = _join_reasons(*(span.audio.reason for span in rows)) or "분석 없음"
    video = _join_reasons(*(span.video.reason for span in rows)) or "분석 없음"
    return recommendation, (
        TIER_TAG_PREFIX + TIER_LABELS[tier],
        AUDIO_TAG_PREFIX + audio,
        VIDEO_TAG_PREFIX + video,
        MARKER_PREFIX + report.tier_version,
    )


def red_ranges(report: TierReport) -> tuple[tuple[Fraction, Fraction], ...]:
    return tuple((span.start, span.end) for span in report.spans if span.tier == TIER_RED)


def unmaterialized_red(
    ranges: Sequence[tuple[Fraction, Fraction]], edges: set[Fraction], frame: Fraction
) -> int:
    """현재 조각 경계로 아직 떨어지지 않은 빨강 수 — 프레임 안쪽으로 붙인 끝을 본다."""
    count = 0
    for start, end in ranges:
        first = -((-start) // frame) * frame
        last = (end // frame) * frame
        if first < last and not (first in edges and last in edges):
            count += 1
    return count


# ── 보고서 문서 왕복 ───────────────────────────────────────────────────────


def _audio_from(row: Mapping[str, Any]) -> AudioJudgment:
    rms = row.get("rms_dbfs")
    return AudioJudgment(
        str(row["status"]),
        rational(row["density"]),
        None if rms is None else float(rms),
        str(row["reason"]),
        tuple(str(e) for e in row.get("evidence", []) or []),
    )


def _video_from(row: Mapping[str, Any]) -> VideoJudgment:
    return VideoJudgment(
        str(row["status"]),
        str(row["reason"]),
        tuple(str(e) for e in row.get("evidence", []) or []),
        tuple(str(w) for w in row.get("windows", []) or []),
    )


def report_from_document(body: Mapping[str, Any]) -> TierReport:
    """보관한 보고서(`tier_report.json`)를 값으로 — 모양이 어긋나면 KeyError·TypeError다."""
    spans = tuple(
        TierSpan(
            start=rational(row["start"]),
            end=rational(row["end"]),
            tier=str(row["tier"]),
            rule=str(row["rule"]),
            audio=_audio_from(row["audio"]),
            video=_video_from(row["video"]),
            red_reason=None if row.get("red_reason") is None else str(row["red_reason"]),
        )
        for row in body["spans"]
    )
    return TierReport(
        tier_version=str(body["tier_version"]),
        settings=RawTierSettings(**body["settings"]),
        document_sha256=str(body["document_sha256"]),
        spans=spans,
    )
