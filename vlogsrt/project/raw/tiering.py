"""3단 등급 — 자막과 영상을 **따로** 판정한 뒤 표 하나로 합친다(W-11·W-12, D51·D52).

순수 함수만 산다: 프로세스·DB·서비스·파일 읽기가 없다. 입력은 저장된 분석 문서와 값 하나다 —
AI 판정(Luna 창의 `annotations[]`·프레임 유무)과 생성 자막(`utterances[]`·ASR 청크 실패·오디오
트랙 유무), 그리고 서비스가 **지금 리비전**의 자막 시각 확인(D55) 보고서에서 만든 값
`TimingSignals`(판정 수준·「말소리를 못 찾음」 큐, D60). 소리 크기나 화면 밝기를 로컬에서 재지
않는다(D52) — 확인 계층(`sync_check`·`sync_report`)도 모른다. 같은 문서·같은 값이면 같은 보고서가
나온다 — 유료 호출 0회로 언제든 다시 계산한다(분석 지문에 들어가지 않는다).

지키는 선:

- **빨강은 제안이다.** 자막 없는 긴 공백인데 Luna가 그 화면을 풍경(`recommended`)으로 보지 않은
  자리만 빨강이 된다. 빨강은 자동 제외 후보로만 쓰이고, 사람이 확인하기 전에는 어디서도 빠지지
  않는다 — 잘라내는 확정 내보내기는 그때까지 닫히고, Final Cut 타임라인에는 켠 채
  `제외 제안(미검토)` 키워드로만 간다(D35·D56, 규칙 9). 발화가 있는 구간은 빨강이 되지 않는다.
- **모름은 결함이 아니다.** 프레임이 없거나 판정이 실패했거나 아직 없으면 노랑 + 판정 보류다.
  전사 실패 구간은 절대 무음이 아니고, 자막이 아직 없으면(진행 문서) 공백도 아니다.
- **구간은 빈틈없이 덮는다.** 보고서의 구간은 `[0, extent]`를 겹침·틈 없이 타일한다.
- **추천은 확인된 말로만(D60).** 같은 말 되풀이·느린 추임새는 밀도에서 뺀다 — 말이 있는지(빨강
  판단)는 여전히 모든 발화로 본다. 시각 확인 값은 다 정한 구간 위에서 **녹색을 노랑으로 내릴 수만**
  있다: 경계를 옮기지 않고 빨강을 만들거나 지우지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field, replace
from fractions import Fraction
from typing import Any, TypeVar

from vlogsrt.hangul import strip_for_compare, to_nfc
from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    RawTierSettings,
    validate_tier_settings,
)
from vlogsrt.project.raw.suitability import rational

_T = TypeVar("_T")

TIER_RED = "red"
TIER_YELLOW = "yellow"
TIER_GREEN = "green"
TIERS = (TIER_GREEN, TIER_YELLOW, TIER_RED)

RED_SILENCE = "tier_silence"
LEGACY_RED_REASONS = ("tier_video_defect",)
"""옛 등급판(D51)만 냈던 빨강 이유 — 지금은 만들지 않고, 낡은 제안을 풀 때만 알아본다(D52)."""
TIER_RED_REASONS = (RED_SILENCE, *LEGACY_RED_REASONS)

AUDIO_SPEECH_DENSE = "speech_dense"
AUDIO_SPEECH_SPARSE = "speech_sparse"
AUDIO_SILENT = "subtitle_silence"
AUDIO_UNCONFIRMED = "unconfirmed_empty"
AUDIO_FAILED = "analysis_failed"
AUDIO_ABSENT = "audio_absent"
AUDIO_PENDING = "subtitle_pending"
AUDIO_STATUSES = (
    AUDIO_SPEECH_DENSE,
    AUDIO_SPEECH_SPARSE,
    AUDIO_SILENT,
    AUDIO_UNCONFIRMED,
    AUDIO_FAILED,
    AUDIO_ABSENT,
    AUDIO_PENDING,
)

VIDEO_GOOD = "good"
VIDEO_USABLE = "usable"
VIDEO_FLAGGED = "flagged"
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
)
"""심각도 오름차순 — 한 구간에 창이 여럿이면 가장 나쁜 쪽이 이긴다."""

VIDEO_UNKNOWN = frozenset({VIDEO_NO_FRAMES, VIDEO_FAILED, VIDEO_UNANALYZED})

_SHORT_RED_RULE = "red-too-short"
"""빨강 이웃이 없어 노랑으로 내린 짧은 빨강의 규칙 이름 — 보통은 이웃에 흡수되어 보이지 않는다."""

_SILENCE_RED_VIDEO = frozenset({VIDEO_USABLE, VIDEO_FLAGGED})
"""자막 없는 긴 공백이 빨강이 되는 영상 판정 — Luna가 그 화면을 풍경(`recommended`)으로 보지
않았다. `usable`을 넣을지는 조정 대상이다(D52 — 실데이터에서 두 변형을 함께 쟀다). 이 값이나
판정·합치기 규칙을 바꾸면 `tier_version`을 올린다 — 올리지 않으면 이미 매긴 검토 머리가 옛
등급 그대로 남는다(옮기기는 등급판 표지로만 낡음을 안다)."""

AUDIO_LABELS = {
    AUDIO_SPEECH_DENSE: "발화 많음",
    AUDIO_SPEECH_SPARSE: "발화 적음",
    AUDIO_SILENT: "자막 없음(긴 공백)",
    AUDIO_UNCONFIRMED: "발화 없음",
    AUDIO_FAILED: "전사 실패",
    AUDIO_ABSENT: "소리 없음",
    AUDIO_PENDING: "자막 전",
}
VIDEO_LABELS = {
    VIDEO_GOOD: "좋음",
    VIDEO_USABLE: "사용 가능",
    VIDEO_FLAGGED: "확인 권고",
    VIDEO_NO_FRAMES: "프레임 없음",
    VIDEO_FAILED: "판정 실패",
    VIDEO_UNANALYZED: "판정 전",
}
VISUAL_ISSUE_LABELS = {
    "blur": "흐림",
    "shake": "흔들림",
    "dark": "어두움",
    "overexposed": "과노출",
    "obstructed": "가림",
}
"""판정의 화면 문제(D58 18b)를 보일 쉬운 말 — 영상 이유(「분석 근거」)에만 붙고 등급·규칙·합치기
열쇠에는 들어가지 않는다(등급에 쓰는 일은 W-20b). `none`과 키 없음은 아무 말도 보태지 않는다."""
TIER_LABELS = {TIER_GREEN: "추천", TIER_YELLOW: "보통", TIER_RED: "제외 제안"}
HOLD_LABEL = "판정 보류"
"""근거가 없어 노랑인 조각의 이름(화면의 점선 노랑) — Final Cut 키워드로 나간다(D56)."""

TIER_TAG_PREFIX = "등급: "
AUDIO_TAG_PREFIX = "자막: "
VIDEO_TAG_PREFIX = "영상: "
CAP_TAG_PREFIX = "추천 제한: "
MARKER_PREFIX = "등급판 "
LEGACY_GRADE_PREFIX = "Luna 평가: "
GRADE_TAG_PREFIXES = (
    LEGACY_GRADE_PREFIX,
    TIER_TAG_PREFIX,
    AUDIO_TAG_PREFIX,
    VIDEO_TAG_PREFIX,
    CAP_TAG_PREFIX,
    MARKER_PREFIX,
)

# ── 추천 기준 강화(D60) ──────────────────────────────────────────────────

TIMING_RED = "red"
"""자막 시각 확인(D55) 빨강 판정의 값 — 등급은 확인 계층을 모르므로 값만 둔다(시험이
`sync_check.LEVEL_RED`와 같음을 못 박는다)."""

UNCONFIRMED_REPEAT = "repeat"
UNCONFIRMED_SLOW = "slow_filler"
UNCONFIRMED_NO_SPEECH = "no_speech"
UNCONFIRMED_KINDS = (UNCONFIRMED_REPEAT, UNCONFIRMED_SLOW, UNCONFIRMED_NO_SPEECH)
"""밀도에서 뺀 줄의 까닭 — 한 줄에 하나, 이 순서가 우선이다. 문서의 글자에서 아는 까닭(되풀이·
느린 추임새)이 먼저라 「측정 한계일 수 있음」은 측정만으로 뺀 줄에만 붙는다."""

_COUNTED_CATEGORIES = frozenset("LN")
"""느린 추임새의 「글자」 — NFC 뒤 문자(L)·숫자(N) 범주 낱자. 한글은 음절 하나가 한 자, 라틴은
철자 하나가 한 자다(영어는 덜 걸린다 — 빼지 않는 쪽으로만 틀린다). 공백·부호·기호는 세지 않는다."""

CAP_TOO_SHORT = "too_short"
CAP_TIMING_RED = "timing_red"
_TIMING_RED_CAP = (
    CAP_TIMING_RED,
    "자막 시각 확인이 빨강이라 보통까지만 추천합니다 — 시각을 맞춘 뒤 다시 재면 풀립니다",
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
class TimingSignals:
    """자막 시각 확인(D55)이 등급에 넘기는 값 — 서비스가 **지금 리비전** 보고서에서만 만든다(D60).

    `level`은 그 보고서의 판정(red·amber·green·skip)이고, 보고서가 없거나 낡았으면 None(모름)이다.
    `no_speech`는 그 보고서가 「말소리를 못 찾음」으로 짚은 큐의 (아이디, 시작, 끝) — 공통 시계의
    정확 유리수. 분석 문서의 발화와 아이디·시작·끝이 **모두** 같은 줄에만 쓴다(다른 리비전의 줄은
    모름이다). 둘 다 녹색을 노랑으로 내릴 수만 있다.
    """

    level: str | None = None
    no_speech: frozenset[tuple[str, Fraction, Fraction]] = frozenset()


UNKNOWN_TIMING = TimingSignals()
"""시각 확인을 모른다 — 아무것도 내리지 않는다(보고서 없음·낡음·다른 리비전)."""


@dataclass(frozen=True)
class AudioJudgment:
    """자막 쪽 판정 — 밀도는 **확인된** 발화가 덮은 길이 ÷ 구간 길이다(D60).

    `excluded`는 구간에 닿았지만 밀도에서 뺀 발화의 (아이디, 까닭)이다 — 근거에는 그대로 남는다.
    """

    status: str
    density: Fraction
    reason: str
    evidence: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...] = ()

    def document(self) -> dict[str, object]:
        return {
            "status": self.status,
            "label": AUDIO_LABELS[self.status],
            "reason": self.reason,
            "density": _rational_json(self.density),
            "evidence": list(self.evidence),
            "excluded": [{"id": uid, "kind": kind} for uid, kind in self.excluded],
        }


@dataclass(frozen=True)
class VideoJudgment:
    """영상 쪽 판정 — Luna의 구조화된 칸과 프레임 유무에서만 온다."""

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
    """보고서의 구간 하나 — 두 판정과 표가 낸 등급을 함께 싣는다.

    `caps`는 표가 녹색이라 했지만 추천하지 않는 까닭(코드, 말)이다 — 최소 길이·자막 시각
    빨강(D60). 그때 규칙(`rule`)은 표의 칸 그대로이고 등급만 노랑이다.
    """

    start: Fraction
    end: Fraction
    tier: str
    rule: str
    audio: AudioJudgment
    video: VideoJudgment
    red_reason: str | None
    caps: tuple[tuple[str, str], ...] = ()

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
        capped = "".join(f"{_REASON_SEPARATOR}{CAP_TAG_PREFIX}{text}" for _, text in self.caps)
        return {
            "start": _rational_json(self.start),
            "end": _rational_json(self.end),
            "tier": self.tier,
            "rule": self.rule,
            "hold": self.hold,
            "red_reason": self.red_reason,
            "recommendation": self.recommendation,
            "reason": f"자막: {self.audio.reason} · 영상: {self.video.reason}{capped}",
            "caps": [{"code": code, "text": text} for code, text in self.caps],
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
    """원본 하나의 판정 재료 — 문서를 한 번만 읽어 정리한 값이다.

    `audio_state`는 `present`(자막 칸이 있다) · `absent`(오디오 트랙 없음) · `unknown`(진행
    문서처럼 자막 칸이 아직 없다)이다. `silent`는 무음 후보인 긴 자막 공백이다. `unconfirmed`는
    문서의 글자만으로 밀도에서 빼는 발화(같은 말 되풀이·느린 추임새, D60) — 발화 행 → 까닭.
    """

    extent: Fraction
    audio_state: str
    speech: tuple[tuple[Fraction, Fraction, str], ...]
    failed: tuple[tuple[Fraction, Fraction, str], ...]
    silent: tuple[tuple[Fraction, Fraction], ...]
    windows: tuple[_Window, ...]
    frames_known: bool
    present_frames: frozenset[str]
    settings: RawTierSettings
    unconfirmed: Mapping[tuple[Fraction, Fraction, str], str] = field(default_factory=dict)


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
    settings: RawTierSettings = TIER_SETTINGS,
    audio_start: Fraction = Fraction(0),
) -> SourceEvidence:
    """판정 재료를 한 번에 정리한다 — 저장된 문서 밖의 값은 받지 않는다(D52).

    밀도에서 뺄 줄은 문서의 글자로 아는 것(되풀이·느린 추임새)만 싣는다 — 시각 확인 값은 다 정한
    구간 위에서만 쓴다(`classify`, D60).
    """
    frames_known, present = _frames(document)
    return SourceEvidence(
        extent=extent_of(document),
        audio_state=_audio_state(document),
        speech=_spoken(document),
        failed=failed_chunks(document, audio_start=audio_start),
        silent=silent_gaps(document, settings, audio_start=audio_start),
        windows=_windows(document),
        frames_known=frames_known,
        present_frames=present,
        settings=settings,
        unconfirmed=unconfirmed_speech(document, settings),
    )


# ── 확인된 말소리(D60) ───────────────────────────────────────────────────


def _row_key(row: Mapping[str, Any]) -> tuple[Fraction, Fraction, str]:
    """발화 행의 열쇠 — `_spoken`과 같은 (시작, 끝, 아이디)."""
    return rational(row["start"]), rational(row["end"]), str(row.get("utterance_id", ""))


def _repeat_keys(
    rows: Sequence[Mapping[str, Any]], run_min: int
) -> set[tuple[Fraction, Fraction, str]]:
    """접은 글자(19e와 같은 `strip_for_compare`)가 같은 발화가 `run_min`줄 이상 잇따른 런.

    문서 순서로 잰다. 접어서 비는 줄(부호뿐)은 런을 만들지도 잇지도 않는다.
    """
    folded = [strip_for_compare(str(row.get("text", ""))) for row in rows]
    keys: set[tuple[Fraction, Fraction, str]] = set()
    start = 0
    while start < len(rows):
        end = start + 1
        while folded[start] and end < len(rows) and folded[end] == folded[start]:
            end += 1
        if folded[start] and end - start >= run_min:
            keys.update(_row_key(row) for row in rows[start:end])
        start = end
    return keys


def _letters(text: str) -> int:
    return sum(1 for ch in to_nfc(text) if unicodedata.category(ch)[0] in _COUNTED_CATEGORIES)


def _slow_filler(row: Mapping[str, Any], settings: RawTierSettings) -> bool:
    """`slow_filler_min_seconds` 이상이면서 글자가 초당 `slow_filler_chars_per_second`보다 적다."""
    start, end, _ = _row_key(row)
    length = end - start
    if length < Fraction(str(settings.slow_filler_min_seconds)):
        return False
    pace = Fraction(str(settings.slow_filler_chars_per_second))
    return _letters(str(row.get("text", ""))) < pace * length


def unconfirmed_speech(
    document: Mapping[str, Any],
    settings: RawTierSettings = TIER_SETTINGS,
    timing: TimingSignals = UNKNOWN_TIMING,
) -> dict[tuple[Fraction, Fraction, str], str]:
    """밀도에서 뺄 발화(`_spoken`의 행) → 까닭 — 되풀이 · 느린 추임새 · 말소리 측정상 없음 순.

    말소리 측정상 없음은 `timing.no_speech`에 아이디·시작·끝이 모두 같은 줄만이다. 줄을 지우지도
    고치지도 않는다 — 밀도의 재료에서만 뺀다(규칙 4·9).
    """
    rows = [row for row in document.get("utterances", []) or [] if isinstance(row, Mapping)]
    repeats = _repeat_keys(rows, settings.same_text_run_min)
    texts = {_row_key(row): row for row in rows if str(row.get("text", "")).strip()}
    spoken = _spoken(document)
    found: dict[tuple[Fraction, Fraction, str], str] = {}
    for key in spoken:
        if key in repeats:
            found[key] = UNCONFIRMED_REPEAT
        elif _slow_filler(texts[key], settings):
            found[key] = UNCONFIRMED_SLOW
    return _with_measured(found, spoken, timing)


def _with_measured(
    found: Mapping[tuple[Fraction, Fraction, str], str],
    spoken: Sequence[tuple[Fraction, Fraction, str]],
    timing: TimingSignals,
) -> dict[tuple[Fraction, Fraction, str], str]:
    """글자로 뺀 줄에 시각 확인이 짚은 줄을 더한다 — 아이디·시작·끝이 모두 같은 줄만(D60)."""
    merged = dict(found)
    for row in spoken:
        if row not in merged and (row[2], row[0], row[1]) in timing.no_speech:
            merged[row] = UNCONFIRMED_NO_SPEECH
    return merged


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


def silent_gaps(
    document: Mapping[str, Any],
    settings: RawTierSettings = TIER_SETTINGS,
    *,
    audio_start: Fraction = Fraction(0),
) -> tuple[tuple[Fraction, Fraction], ...]:
    """무음 후보 — 자막 칸이 있고, 실패 청크에 닿지 않는 긴 자막 공백만이다.

    자막이 아직 없거나(진행 문서) 오디오 트랙이 없으면 후보가 없다. 전사가 실패한 자리는
    자막이 없어도 말이 있었을 수 있다 — 공백으로 치지 않는다.
    """
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
    audio_start: Fraction = Fraction(0),
) -> tuple[Fraction, ...]:
    """구간을 자를 자리 — 긴 공백의 가장자리(발화 끝·시작 ± 여유)·창 중심·검증 장면·
    실패 청크 가장자리.

    발화 하나하나의 끝은 자르지 않는다 — 자르면 모든 원자 구간의 밀도가 0 아니면 1이 되어
    '띄엄띄엄 말한 구역'을 알아볼 수 없다. 저장된 `recommendation_boundaries`도 쓰지
    않는다 — 이 판정의 재료가 아닌 경계가 섞인다.
    """
    extent = extent_of(document)
    points = {Fraction(0), extent}
    gaps = speech_gaps(document, settings, audio_start=audio_start)
    points.update(p for gap in gaps for p in gap)
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


# ── 자막 판정 ─────────────────────────────────────────────────────────────


def _percent(value: Fraction) -> int:
    return round(value * 100)


_EXCLUDED_NOTES = {
    UNCONFIRMED_REPEAT: "같은 말이 되풀이된 줄 {}개를 밀도에서 뺐습니다",
    UNCONFIRMED_SLOW: "느린 추임새 {}개를 밀도에서 뺐습니다",
    UNCONFIRMED_NO_SPEECH: ("말소리 측정상 없는 줄 {}개를 밀도에서 뺐습니다(측정 한계일 수 있음)"),
}
"""밀도에서 뺀 줄의 말(D60) — 측정으로 뺀 줄에는 D55의 단서를 그대로 단다. 「VAD」라고 쓰지
않는다(D57·D59 19e)."""


def _excluded_notes(excluded: Sequence[tuple[str, str]]) -> tuple[str, ...]:
    """밀도에서 뺀 줄의 말 — 까닭마다 한 마디, 정해진 순서로(D60)."""
    counts = {kind: sum(1 for _, k in excluded if k == kind) for kind in UNCONFIRMED_KINDS}
    return tuple(
        _EXCLUDED_NOTES[kind].format(counts[kind]) for kind in UNCONFIRMED_KINDS if counts[kind]
    )


def _audio_reason(
    status: str,
    density: Fraction,
    settings: RawTierSettings,
    excluded: Sequence[tuple[str, str]] = (),
) -> str:
    base = {
        AUDIO_SPEECH_DENSE: f"발화가 구간의 {_percent(density)}%를 덮는다",
        AUDIO_SPEECH_SPARSE: f"발화가 구간의 {_percent(density)}%뿐이다",
        AUDIO_SILENT: f"발화 없음 · 자막 없는 공백 {settings.silence_min_seconds:g}초 이상",
        AUDIO_UNCONFIRMED: "발화 없음 · 무음 후보 아님(짧은 공백·전사 실패 근처·소리 범위 밖)",
        AUDIO_FAILED: "전사 실패 구간 — 무음으로 보지 않는다",
        AUDIO_ABSENT: "오디오 트랙이 없다",
        AUDIO_PENDING: "자막을 아직 만들지 않았다(분석 중)",
    }[status]
    return _REASON_SEPARATOR.join((base, *_excluded_notes(excluded)))


def _audio(
    status: str,
    settings: RawTierSettings,
    *,
    density: Fraction = Fraction(0),
    evidence: tuple[str, ...] = (),
    excluded: tuple[tuple[str, str], ...] = (),
) -> AudioJudgment:
    reason = _audio_reason(status, density, settings, excluded)
    return AudioJudgment(status, density, reason, evidence, excluded)


@dataclass(frozen=True)
class _Cover:
    """구간 하나의 발화 덮음 — 모든 발화(말이 있는가)와 확인된 발화(밀도)."""

    spoken: Fraction
    confirmed: Fraction
    ids: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]


def _speech_cover(
    evidence: SourceEvidence,
    start: Fraction,
    end: Fraction,
    unconfirmed: Mapping[tuple[Fraction, Fraction, str], str] | None = None,
) -> _Cover:
    """덮음을 잰다 — 뺄 줄(`unconfirmed`, 없으면 문서의 글자로 아는 것)은 밀도에만 빠진다."""
    skip = evidence.unconfirmed if unconfirmed is None else unconfirmed
    touching = [row for row in evidence.speech if row[0] < end and row[1] > start]
    clipped = [(max(a, start), min(b, end), uid) for a, b, uid in touching]
    kept = [clip for clip, row in zip(clipped, touching, strict=True) if row not in skip]
    return _Cover(
        spoken=sum((b - a for a, b in _merged_speech(clipped)), Fraction(0)),
        confirmed=sum((b - a for a, b in _merged_speech(kept)), Fraction(0)),
        ids=tuple(uid for _, _, uid in touching),
        excluded=tuple((row[2], skip[row]) for row in touching if row in skip),
    )


def _speech_judgment(
    cover: _Cover, start: Fraction, end: Fraction, settings: RawTierSettings
) -> AudioJudgment:
    """말이 있는 구간의 판정 — 밀도는 확인된 발화로만 잰다(D60)."""
    density = cover.confirmed / (end - start)
    dense = density >= Fraction(str(settings.speech_density_green))
    status = AUDIO_SPEECH_DENSE if dense else AUDIO_SPEECH_SPARSE
    return _audio(status, settings, density=density, evidence=cover.ids, excluded=cover.excluded)


def judge_audio(evidence: SourceEvidence, start: Fraction, end: Fraction) -> AudioJudgment:
    """구간 하나의 자막 판정 — 순서가 곧 우선순위다(부재 → 실패 → 발화 → 자막 전 → 공백).

    말이 있는지는 **모든** 발화로 본다 — 밀도에서 뺀 줄도 말이 있던 자리라 공백(빨강의 재료)이
    되지 않는다(규칙 4·9, D60).
    """
    settings = evidence.settings
    if evidence.audio_state == "absent":
        return _audio(AUDIO_ABSENT, settings)
    failed = tuple(label for a, b, label in evidence.failed if a < end and b > start)
    if failed:
        return _audio(AUDIO_FAILED, settings, evidence=failed)
    cover = _speech_cover(evidence, start, end)
    if cover.spoken > 0:
        return _speech_judgment(cover, start, end, settings)
    if evidence.audio_state != "present":
        return _audio(AUDIO_PENDING, settings)
    for a, b in evidence.silent:
        if a <= start and end <= b:
            return _audio(AUDIO_SILENT, settings, evidence=(f"subtitle_gap:{a}-{b}",))
    return _audio(AUDIO_UNCONFIRMED, settings)


# ── 영상 판정 ────────────────────────────────────────────────────────────


def _video(status: str, reason: str, evidence: tuple[str, ...], window: str) -> VideoJudgment:
    return VideoJudgment(status, reason, evidence, (window,))


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


def _visual_issue_note(annotation: Mapping[str, Any]) -> str:
    """판정이 적은 화면 문제를 쉬운 말로 — 표시만 한다(등급을 바꾸지 않는다)."""
    labels = [
        VISUAL_ISSUE_LABELS[item]
        for item in annotation.get("visual_issues") or []
        if isinstance(item, str) and item in VISUAL_ISSUE_LABELS
    ]
    return " · 화면 문제: " + "·".join(labels) if labels else ""


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
    status = {"recommended": VIDEO_GOOD, "usable": VIDEO_USABLE}.get(
        str(row.get("suitability")), VIDEO_FLAGGED
    )
    reason = str(row.get("suitability_reason") or VIDEO_LABELS[status])
    reason += _luna_notes(row, len(frames) < len(window.frame_ids)) + _visual_issue_note(row)
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
    """두 판정 → (등급, 규칙, 빨강 이유).

    빨강은 자막 없는 긴 공백 × Luna가 풍경으로 보지 않은 화면뿐이다. 풍경으로 본 화면이면
    말이 없어도 노랑이고(풍경 컷), Luna 판정이 없으면 보류다 — AI 근거 없이는 빨강이 없다.
    """
    if audio.status == AUDIO_SILENT and video.status in _SILENCE_RED_VIDEO:
        return TIER_RED, "silent-not-scenery", RED_SILENCE
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
        AUDIO_SILENT: "scenery-no-speech",
        AUDIO_UNCONFIRMED: "speech-unconfirmed",
        AUDIO_PENDING: "audio-pending",
    }
    return by_audio.get(audio, f"video-{video}")


def _span(evidence: SourceEvidence, start: Fraction, end: Fraction) -> TierSpan:
    audio = judge_audio(evidence, start, end)
    video = judge_video(evidence, start, end)
    tier, rule, red = combine(audio, video)
    return TierSpan(start, end, tier, rule, audio, video, red)


def _dedupe(*groups: Sequence[_T]) -> tuple[_T, ...]:
    return tuple(dict.fromkeys(item for group in groups for item in group))


def _join_reasons(*reasons: str) -> str:
    parts = [part for reason in reasons for part in reason.split(_REASON_SEPARATOR)]
    return _REASON_SEPARATOR.join(dict.fromkeys(parts))


def _grow(host: TierSpan, other: TierSpan, settings: RawTierSettings, *, absorb: bool) -> TierSpan:
    """두 이웃 구간을 하나로 — 흡수면 판정 상태는 host의 것, 근거는 합친다."""
    first, second = sorted((host, other), key=lambda span: span.start)
    left, right = host.end - host.start, other.end - other.start
    density = (host.audio.density * left + other.audio.density * right) / (left + right)
    audio_evidence = (
        host.audio.evidence if absorb else _dedupe(first.audio.evidence, second.audio.evidence)
    )
    excluded = (
        host.audio.excluded if absorb else _dedupe(first.audio.excluded, second.audio.excluded)
    )
    audio = AudioJudgment(
        host.audio.status,
        density,
        _audio_reason(host.audio.status, density, settings, excluded),
        audio_evidence,
        excluded,
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


def _demoted(span: TierSpan) -> TierSpan:
    """붙을 빨강 이웃이 없는 짧은 빨강을 노랑으로 내린다(D52) — 대개 다음 바퀴에 이웃이 먹지만,
    짧은 노랑을 먼저 먹어 자라면 이 규칙 이름을 단 노랑으로 남는다. 어느 쪽이든 제안은 아니다.
    """
    return replace(span, tier=TIER_YELLOW, rule=_SHORT_RED_RULE, red_reason=None)


def _absorb(spans: Sequence[TierSpan], settings: RawTierSettings) -> list[TierSpan]:
    """`min_span_seconds`보다 짧은 조각을 이웃에 붙인다 — 빨강은 빨강에만, 비빨강은 비빨강에만.

    빨강 이웃이 없는 짧은 빨강은 제안하지 않는다(D52): 긴 공백이 Luna 창 경계를 1초 못 되게
    넘어 생긴 자투리가 사람의 확인을 기다리며 출력을 막으면 안 된다. 빨강은 자라지 않는다.
    """
    minimum = Fraction(str(settings.min_span_seconds))
    items = list(spans)
    changed = True
    while changed:
        changed = False
        for index, span in enumerate(items):
            if span.end - span.start >= minimum:
                continue
            host = _host_index(items, index)
            if host is None and span.tier == TIER_RED:
                items[index] = _demoted(span)
            elif host is None:
                continue
            else:
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


def _measured_density(
    span: TierSpan,
    evidence: SourceEvidence,
    unconfirmed: Mapping[tuple[Fraction, Fraction, str], str],
) -> TierSpan:
    """말소리 측정상 없는 줄을 녹색 구간의 밀도에서도 뺀다 — 녹색만, 경계는 그대로(D60).

    그 줄이 닿지 않은 녹색은 그대로다. 밀도가 기준 밑으로 내려가면 표의 칸(발화 적음 × 그 화면)이
    다시 정한다 — 발화가 있으니 빨강은 나올 수 없다(빨강은 공백 × 의심 화면뿐).
    """
    if span.tier != TIER_GREEN or span.audio.status != AUDIO_SPEECH_DENSE:
        return span
    cover = _speech_cover(evidence, span.start, span.end, unconfirmed)
    if not any(kind == UNCONFIRMED_NO_SPEECH for _, kind in cover.excluded):
        return span
    judged = _speech_judgment(cover, span.start, span.end, evidence.settings)
    audio = replace(judged, evidence=span.audio.evidence)
    tier, rule, _ = combine(audio, span.video)
    return replace(span, tier=tier, rule=rule, audio=audio)


def _capped(span: TierSpan, settings: RawTierSettings, timing: TimingSignals) -> TierSpan:
    """녹색이지만 추천하지 않는 까닭 — 최소 길이·자막 시각 빨강(D60). 녹색만 노랑으로 내린다."""
    if span.tier != TIER_GREEN:
        return span
    caps = []
    if span.end - span.start < Fraction(str(settings.recommended_min_seconds)):
        caps.append(_too_short_cap(settings))
    if timing.level == TIMING_RED:
        caps.append(_TIMING_RED_CAP)
    return replace(span, tier=TIER_YELLOW, caps=tuple(caps)) if caps else span


def _too_short_cap(settings: RawTierSettings) -> tuple[str, str]:
    return CAP_TOO_SHORT, f"{settings.recommended_min_seconds:g}초보다 짧아 추천하지 않습니다"


def classify(
    document: Mapping[str, Any],
    *,
    settings: RawTierSettings = TIER_SETTINGS,
    audio_start: Fraction = Fraction(0),
    document_sha256: str | None = None,
    timing: TimingSignals = UNKNOWN_TIMING,
) -> TierReport:
    """저장된 분석 문서 → 등급 보고서. 결정적이고 설정을 함께 싣는다.

    로컬 측정(공백 RMS·프레임 화질)은 받지 않는다 — AI 판정과 생성 자막만이 재료다(D52). 자막
    시각 확인 값(`timing`, D60)은 다 정한 구간 위에서 녹색을 노랑으로 내리는 데만 쓴다 — 구간
    경계·빨강·자동 제외 후보는 그 값과 무관하다(서비스만 넘긴다, `check_invariants`).
    """
    validate_tier_settings(settings)
    evidence = evidence_of(document, settings=settings, audio_start=audio_start)
    points = atomic_boundaries(document, settings, audio_start=audio_start)
    spans = [_span(evidence, a, b) for a, b in zip(points, points[1:], strict=False)]
    spans = _merge(_absorb(_merge(spans, settings), settings), settings)
    measured = _with_measured(evidence.unconfirmed, evidence.speech, timing)
    spans = [_measured_density(span, evidence, measured) for span in spans]
    spans = [_capped(span, settings, timing) for span in spans]
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


@dataclass(frozen=True)
class _SegmentTier:
    """검토 조각 하나가 볼 구간 · 그 등급 · 추천하지 않는 까닭(코드, 말)."""

    rows: list[TierSpan]
    tier: str
    caps: tuple[tuple[str, str], ...]


def _segment_tier(report: TierReport, start: Fraction, end: Fraction) -> _SegmentTier:
    """검토 조각 하나가 볼 구간과 그 등급 — `segment_grade`와 `segment_keyword`의 한 규칙.

    `min_span_seconds`보다 짧게 겹친 구간은 보지 않는다 — 빨강 끝을 프레임 안쪽으로 붙이면
    이웃 조각에 0.02초짜리 빨강 자투리가 남는데, 그것이 녹색 조각을 보통으로 끌어내리면 안
    된다. 조각이 그보다 짧거나 넉넉히 겹친 구간이 없으면 가장 많이 겹친 구간 하나로 본다.
    전부 녹색이어야 녹색, 전부 빨강이어야 빨강이고 나머지는 노랑이다. 다만 `recommended_min_
    seconds`보다 짧은 조각은 녹색 구간 위에 있어도 보통까지다 — 짧은 조각이 이웃(또는 품은)
    녹색에게서 추천을 물려받지 않는다(D60).
    """
    rows = _significant_spans(report, start, end)
    tiers = {span.tier for span in rows}
    caps = _dedupe(*(span.caps for span in rows))
    if rows and tiers == {TIER_GREEN}:
        if end - start < Fraction(str(report.settings.recommended_min_seconds)):
            return _SegmentTier(rows, TIER_YELLOW, (_too_short_cap(report.settings),))
        return _SegmentTier(rows, TIER_GREEN, caps)
    return _SegmentTier(rows, TIER_RED if tiers == {TIER_RED} else TIER_YELLOW, caps)


def segment_keyword(report: TierReport, start: Fraction, end: Fraction) -> str:
    """검토 조각 하나의 Final Cut 키워드 — 추천·보통·제외 제안·판정 보류(D56).

    등급은 `segment_grade`와 같은 규칙에서 온다. 노랑 가운데 볼 구간이 **전부** 보류이거나
    볼 구간이 아예 없으면 판정 보류다 — 판정된 구간이 하나라도 섞이면 보통이다(녹색·빨강이
    '전부'일 때만 그 등급인 것과 같은 모양이다).
    """
    judged = _segment_tier(report, start, end)
    if judged.tier == TIER_YELLOW and all(span.hold for span in judged.rows):
        return HOLD_LABEL
    return TIER_LABELS[judged.tier]


def segment_grade(
    report: TierReport, start: Fraction, end: Fraction
) -> tuple[str, tuple[str, ...]]:
    """검토 조각 하나의 권고와 등급 태그 — 권고는 닫힌 집합(recommended/normal/review_required).

    볼 구간과 등급은 `_segment_tier`가 정한다(`segment_keyword`와 한 규칙). 추천하지 않는 까닭이
    있으면 `추천 제한: …` 태그로 단다(「분석 근거」에 보인다, D60).
    """
    judged = _segment_tier(report, start, end)
    rows, tier = judged.rows, judged.tier
    if tier == TIER_GREEN:
        recommendation = "recommended"
    else:
        unknown = any(span.recommendation == "review_required" for span in rows)
        recommendation = "review_required" if unknown or not rows else "normal"
    audio = _join_reasons(*(span.audio.reason for span in rows)) or "분석 없음"
    video = _join_reasons(*(span.video.reason for span in rows)) or "분석 없음"
    return recommendation, (
        TIER_TAG_PREFIX + TIER_LABELS[tier],
        AUDIO_TAG_PREFIX + audio,
        VIDEO_TAG_PREFIX + video,
        *(CAP_TAG_PREFIX + text for _, text in judged.caps),
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
