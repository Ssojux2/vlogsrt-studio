"""A7-3 검토·확정 게시 — 자동 스냅샷 동결·검토 상태 노출·확정 게이트·final 게시.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.7·§11.11·§13·§16 A7-3. 자동 실행
결과를 **동결 스냅샷**으로 못박고, 사람의 수동 리비전(`caption_manual`)을 그 위에
얹고, 명시적 검토 확인을 거쳐 final SRT/VTT를 **새 리비전 디렉터리**에 게시한다.
UI·서비스 연동은 없다 — W1이 이 선 위에 얹는다.

지키는 선:

- **자동 결과는 불변이다.** 스냅샷은 렌더 해시·run·지문·자동 내용 해시 넷에 묶여
  동결되고(§11.1), 같은 신원에 다른 바이트를 다시 못박는 길은 없다. 수동 편집과
  확정 게시는 스냅샷·A7-2 게시물을 한 바이트도 고치지 않는다.
- **구조 경색은 허용으로 못 뚫는다.** 원본·PCM 변경, 신원 불일치, 낡은 자동
  근거, 미처리 오디오, 무효 시각은 확정을 막는다(§11.11). 같은 렌더 해시의
  **다른 선택 오디오**도 막는다 — 스냅샷은 선택 PCM의 정체와 기하에 함께 묶인다.
  시각 문제와 미배치는 **명시적 감사 기록을 가진 수동 정정**으로만 풀린다.
  경고 허용은 표시된 경로에만 붙고, 실패한 문구 모델을 성공으로 바꾸지 못한다(§11.3).
- **빈 성공은 사람 확정이다.** 전부 정상이고 전부 비어 있는 실행은 무발화
  미확정으로 남고, 독립적인 사용자 결정이 있을 때만 final이 나온다(§11.11).
  오류가 섞인 0바이트 SRT 성공은 없다. `no_audio`는 등록 단계의 별도 상태라
  스냅샷에 애초에 오지 않는다.
- **게시는 원자적 no-replace다.** 확정마다 새 디렉터리를 받고, 예전 final·초안은
  그대로 산다(§14.1). SRT/VTT는 재파싱 검사를 통과한 것만 나간다(§11.7).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import TypeGuard

from vlogsrt.output import parse_srt, write_srt
from vlogsrt.project.raw.caption_artifacts import (
    ArtifactVerificationError,
    parse_vtt,
    to_engine_cues,
    verify_serialization,
    write_vtt,
)
from vlogsrt.project.raw.caption_config import DEFAULT_DRAFT_LANGUAGE
from vlogsrt.project.raw.caption_manual import (
    Allowance,
    AutoIdentity,
    ManualRevision,
    ReviewCue,
    UnplacedResolution,
    content_fingerprint,
    cue_document,
    initial_manual_revision,
    serialize_revision,
)
from vlogsrt.project.raw.caption_store import (
    FrozenAutomatic,
    ReviewStore,
    canonical_json,
    sha256_text,
)
from vlogsrt.project.raw.final_caption import (
    BASIS_UNCONFIRMED_EMPTY,
    STATUS_REVIEW_REQUIRED,
    CaptionCue,
    CaptionRunPlan,
    FinalCaptionResult,
)
from vlogsrt.project.raw.final_video import (
    FinalVideoInput,
    require_audio_unchanged,
    require_unchanged,
)

FINAL_MANIFEST_SCHEMA = "raw-caption-final/v1"
"""확정 manifest 문서의 스키마 — 초안 게시와 자리·이름이 갈라진다(§11.7)."""

_HANDLED_FINDINGS = frozenset(
    {
        "unmapped_text",  # 미대응 해결 기록으로 닫는다(D37)
        "empty_run_unconfirmed",  # 무발화 결정으로 닫는다(§11.11)
        "timing_chunk_failed",  # 구조 경색 — 미처리 오디오다
        "out_of_final_range",  # 큐 값에서 다시 잰다 — 낡은 값 경고는 못박지 않는다
        "short_display",
        "reading_speed",
        "overlap",
    }
)
"""경고 목록에 그대로 못박지 않는 자동 항목 — 각자 갈라진 처리나 현재 값 재계산으로 닫힌다.

자동 실행이 남긴 항목 자체는 스냅샷에 얼어 있어 그대로 보존된다(§11.11). 여기서
거르는 것은 **확정 게이트의 열린 경고 목록**뿐이다: 시각을 고치면 범위 이탈은
사라지고, 문구를 고치면 읽기 속도는 다시 잰다.
"""


class ReviewConfirmError(RuntimeError):
    """확정 게이트의 오류 — 구조 경색은 이 계열로 닫힌다(§11.11)."""


class AutoBasisMissing(ReviewConfirmError):
    """자동 스냅샷이 없다 — 확정은 얼어 붙은 자동 근거 위에서만 가능하다."""


class StaleAutoBasis(ReviewConfirmError):
    """머리 리비전이 가리키는 자동 내용이 지금 저장소의 것과 다르다."""


class IdentityMismatch(ReviewConfirmError):
    """머리 리비전의 신원이 이 최종 영상이 아니다 — 새 실행은 새 자리다(§11.1)."""


class StaleManualHead(ReviewConfirmError):
    """겨냥한 리비전이 저장된 머리가 아니다 — 앞선 편집 위에 덮지 못한다."""


class IncompleteCoverage(ReviewConfirmError):
    """청크 계획이 오디오 전체를 덮지 않는다 — 구멍은 확정이 아니라 재실행이다."""


class UnprocessedAudio(ReviewConfirmError):
    """시간 요청이 실패한 청크가 있다 — 듣지 못한 구간의 확정은 없다(§11.11)."""


class InvalidCueTimes(ReviewConfirmError):
    """무효 큐 시각 — 음수·뒤집힘은 수동 시각 정정으로만 풀린다(§11.11)."""


class UnresolvedUnplaced(ReviewConfirmError):
    """미해결 미대응 문구가 있다 — 살리거나 버린 이유를 남겨야 한다(D37)."""


class UnallowedWarnings(ReviewConfirmError):
    """허용되지 않은 경고가 남아 있다 — 풀거나 이유와 함께 허용해야 한다(§11.11)."""


class EmptyRunUndecided(ReviewConfirmError):
    """빈 실행의 무발화가 미확정이다 — 독립적인 사용자 결정이 필요하다(§11.11)."""


class ConfirmationIncomplete(ReviewConfirmError):
    """검토 확인이 완전하지 않다 — 모든 큐를 짚은 명시적 확인이어야 한다."""


class FinalSerializationError(ReviewConfirmError):
    """final SRT/VTT가 재파싱 검사를 통과하지 못한다 — 게시되지 않는다(§11.7)."""


class AudioBasisMismatch(ReviewConfirmError):
    """같은 렌더 해시의 다른 선택 오디오 — 동결된 자동 근거는 낡은 것이다(§11.1).

    검증 사본과 PCM 파일이 제 나름의 신원과 일치해도(각각 유효해도), 동결된
    스냅샷이 들은 것과 **같은 선택**이어야 한다. 새 PCM·새 원점·새 스트림은
    새 실행이고 새 동결이다.
    """


class MismatchedRunBasis(ReviewConfirmError):
    """결과와 계획이 한 실행이 아니다 — 동결은 같은 실행의 증거끼리만(§11.1).

    스냅샷 본문은 결과의 신원 아래 계획에서 잰 기하(청크·샘플 경계)를 얼어 붙인다.
    남의 계획 기하를 남의 신원으로 못박으면 근거가 섞인 문서가 태어난다 —
    동결보다 먼저 닫는다.
    """


# ─────────────────────────────────────────────────────────────
#  자동 스냅샷 — 실행 결과를 얼어 붙인 증거
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ChunkGate:
    """청크 하나의 문 두 개 — 시간·문구 요청의 성패(§11.11 coverage)."""

    chunk_index: int
    sample_start: int
    sample_end: int
    timing_failure: str
    text_failure: str

    @property
    def timing_ok(self) -> bool:
        return not self.timing_failure

    @property
    def text_ok(self) -> bool:
        return not self.text_failure


@dataclass(frozen=True, slots=True)
class UnplacedItem:
    """미대응 문구 — 원문과 **탐색 구간**을 함께 실는다(§11.7 화면 4 이동 재생)."""

    index: int
    text: str
    chars: int
    chunk_index: int
    seek_start: Fraction
    seek_end: Fraction


@dataclass(frozen=True, slots=True)
class RunFinding:
    """자동 실행이 남긴 검토 항목 — 상태는 보존되고 확정 때 다시 잰다(§11.11)."""

    severity: str
    code: str
    message: str
    seg_id: int | None
    chunk_index: int | None


@dataclass(frozen=True, slots=True)
class ReviewThresholds:
    """경고 재계산의 얼어 붙은 경계 — 새 임계값을 만들지 않는다(규칙 8)."""

    min_duration: Fraction
    reading_speed_max_cps: float


@dataclass(frozen=True, slots=True)
class AutomaticSnapshot:
    """동결된 자동 결과 — 신원 넷에 묶이고 타이핑된 검토 자료를 함께 낸다.

    `audio_*`·`origin`·나머지 기하는 **선택 오디오의 정체**다 — 확정 때 지금
    `FinalVideoInput`의 선택과 같은지 다시 잰다(같은 렌더 해시의 다른 PCM 거부).
    """

    identity: AutoIdentity
    status: str
    speech_basis: str
    video_duration: Fraction
    sample_count: int
    sample_rate: int
    channels: int
    pcm_sha256: str
    origin: Fraction
    audio_stream_index: int
    audio_source_sha256: str
    audio_stream_count: int
    spans: tuple[tuple[int, int], ...]
    chunks: tuple[ChunkGate, ...]
    thresholds: ReviewThresholds
    findings: tuple[RunFinding, ...]
    unplaced: tuple[UnplacedItem, ...]
    cues: tuple[ReviewCue, ...]
    path: Path
    body: str


def open_review_store(root: Path) -> ReviewStore:
    """호출자 소유의 root를 연다 — 그 밖의 파일은 절대 건드리지 않는다(§4.4)."""
    root.mkdir(parents=True, exist_ok=True)
    return ReviewStore(root)


def freeze_automatic(
    store: ReviewStore, result: FinalCaptionResult, plan: CaptionRunPlan
) -> AutomaticSnapshot:
    """실행 결과를 동결한다 — 같은 신원·같은 바이트는 멱등, 다른 바이트는 거부."""
    frozen = store.freeze_automatic(
        final_video_revision=result.final_video_revision,
        run_id=result.run_id,
        fingerprint=result.fingerprint,
        content=automatic_content(result, plan),
    )
    return snapshot_from_frozen(frozen)


def automatic_content(result: FinalCaptionResult, plan: CaptionRunPlan) -> dict[str, object]:
    """스냅샷 본문 — 큐·골격·수리·교정·검증·항목·미대응까지 전부 값으로(§11.11).

    큐에는 안정 ID(`a####`)를 순서대로 붙인다 — 같은 자동 내용은 같은 ID를 낳는다.
    본문을 짓기 **전에** 결과와 계획이 한 실행인지 잰다 — 남의 계획 기하를 이
    결과의 신원으로 얼어 붙이는 길이 여기서 끊긴다.
    """
    _require_matching_basis(result, plan)
    audio = plan.video.audio
    return {
        "status": result.status,
        "speech_basis": result.speech_basis,
        "video_duration": _fraction(plan.video.video_duration),
        "sample_count": audio.sample_count,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "pcm_sha256": audio.pcm_sha256,
        "origin": _fraction(audio.pcm_sample0_file_time),
        "audio_stream_index": audio.audio_stream_index,
        "audio_source_sha256": audio.source_sha256,
        "audio_stream_count": plan.video.audio_stream_count,
        "spans": [[span.start, span.end] for span in plan.spans],
        "thresholds": {
            "min_duration": _fraction(plan.engine.min_duration),
            "reading_speed_max_cps": plan.settings.reading_speed_max_cps,
        },
        "chunks": _chunks_body(result),
        "findings": [asdict(finding) for finding in result.findings],
        "unplaced": _unplaced_body(result, plan),
        "cues": [
            dict(cue_document(_auto_review_cue(index, cue)), cue_id=f"a{index:04d}")
            for index, cue in enumerate(result.cues)
        ],
        "spine": [asdict(segment) for segment in result.spine],
        "repairs": [asdict(repair) for repair in result.repairs],
        "retext": result.retext.to_dict(),
        "verify_rows": [asdict(row) for row in result.verify_rows],
    }


def _require_matching_basis(result: FinalCaptionResult, plan: CaptionRunPlan) -> None:
    """결과와 계획이 한 실행인가 — 신원 셋과 청크·샘플 묶음을 잰다(§11.1).

    본문은 결과의 신원 아래 계획의 기하를 얼어 붙이므로, 짝이 다르면 근거가 섞인
    문서가 된다. run_id·지문·렌더 해시와 청크의 샘플 경계가 계획의 조각과 정확히
    맞물려야 동결이 시작된다.
    """
    mismatches: list[str] = []
    if result.run_id != plan.run_id:
        mismatches.append(f"run_id {result.run_id!r}≠{plan.run_id!r}")
    if result.fingerprint != plan.fingerprint:
        mismatches.append(f"fingerprint {result.fingerprint[:12]}…≠{plan.fingerprint[:12]}…")
    if result.final_video_revision != plan.video.final_video_revision:
        mismatches.append("final_video_revision")
    if len(result.chunks) != len(plan.spans):
        mismatches.append(f"청크 수 {len(result.chunks)}≠{len(plan.spans)}")
    else:
        for position, (record, span) in enumerate(zip(result.chunks, plan.spans, strict=True)):
            if record.chunk_index != position or (record.sample_start, record.sample_end) != (
                span.start,
                span.end,
            ):
                mismatches.append(f"청크 {position}의 샘플 경계")
                break
    if mismatches:
        raise MismatchedRunBasis(
            f"결과와 계획이 한 실행이 아니다: {', '.join(mismatches)} — 남의 계획 기하를"
            " 이 결과의 신원으로 동결할 수 없다"
        )


def _chunks_body(result: FinalCaptionResult) -> list[dict[str, object]]:
    """청크 기록 — 문 두 개(시간·문구)의 성패만 남긴다(§11.11 coverage)."""
    return [
        {
            "chunk_index": record.chunk_index,
            "sample_start": record.sample_start,
            "sample_end": record.sample_end,
            "timing_failure": record.timing_failure,
            "text_failure": record.text_failure,
        }
        for record in result.chunks
    ]


def _unplaced_body(result: FinalCaptionResult, plan: CaptionRunPlan) -> list[dict[str, object]]:
    """미대응 문구 — 원점·청크 샘플 범위로 잰 **탐색 구간**을 함께 얼어 붙인다(§11.7)."""
    origin = plan.video.audio.pcm_sample0_file_time
    rate = plan.video.audio.sample_rate
    return [
        {
            "index": index,
            "text": item.text,
            "chars": item.chars,
            "chunk_index": item.chunk_index,
            "seek_start": _fraction(_seek(plan, item.chunk_index, origin, rate, True)),
            "seek_end": _fraction(_seek(plan, item.chunk_index, origin, rate, False)),
        }
        for index, item in enumerate(result.retext.unplaced)
    ]


def _seek(
    plan: CaptionRunPlan, chunk_index: int, origin: Fraction, rate: int, first: bool
) -> Fraction:
    """탐색 구간의 한쪽 끝 — `원점 + 청크 샘플 경계/주파수`의 정확한 유리수(§13.3)."""
    span = plan.spans[chunk_index]
    sample = span.start if first else span.end
    return origin + Fraction(sample, rate)


def _auto_review_cue(index: int, cue: CaptionCue) -> ReviewCue:
    return ReviewCue(
        cue_id=f"a{index:04d}",
        start=cue.start,
        end=cue.end,
        text=cue.text,
        text_basis="auto",
        auto_text=cue.text,
        whisper_text=cue.whisper_text,
        timing_segment_ids=cue.timing_segment_ids,
        timing_evidence_id=cue.timing_evidence_id,
        text_evidence_ids=cue.text_evidence_ids,
        correction_status=cue.correction_status,
        flags=cue.flags,
    )


def find_snapshot(store: ReviewStore, identity: AutoIdentity) -> AutomaticSnapshot | None:
    """스냅샷을 찾는다 — 없으면 None(새 실행은 아직 동결 전이다)."""
    frozen = store.find_automatic(
        final_video_revision=identity.final_video_revision,
        run_id=identity.run_id,
        fingerprint=identity.fingerprint,
    )
    return None if frozen is None else snapshot_from_frozen(frozen)


def snapshot_from_frozen(frozen: FrozenAutomatic) -> AutomaticSnapshot:
    """동결 본문을 타이핑된 스냅샷으로 — 모르는 키·타입은 못 들어온다(§13)."""
    content = frozen.content
    return AutomaticSnapshot(
        identity=AutoIdentity(
            final_video_revision=frozen.final_video_revision,
            run_id=frozen.run_id,
            fingerprint=frozen.fingerprint,
            auto_content_sha256=frozen.auto_content_sha256,
        ),
        status=_str(content, "status"),
        speech_basis=_str(content, "speech_basis"),
        video_duration=_fraction_value(_any(content, "video_duration")),
        sample_count=_int(content, "sample_count"),
        sample_rate=_int(content, "sample_rate"),
        channels=_int(content, "channels"),
        pcm_sha256=_str(content, "pcm_sha256"),
        origin=_fraction_value(_any(content, "origin")),
        audio_stream_index=_int(content, "audio_stream_index"),
        audio_source_sha256=_str(content, "audio_source_sha256"),
        audio_stream_count=_int(content, "audio_stream_count"),
        spans=_spans(content),
        chunks=tuple(
            ChunkGate(
                chunk_index=_int(chunk, "chunk_index"),
                sample_start=_int(chunk, "sample_start"),
                sample_end=_int(chunk, "sample_end"),
                timing_failure=_str(chunk, "timing_failure"),
                text_failure=_str(chunk, "text_failure"),
            )
            for chunk in _list(content, "chunks")
        ),
        thresholds=_thresholds(content),
        findings=_findings(content),
        unplaced=_unplaced(content),
        cues=_cues(content),
        path=frozen.path,
        body=frozen.body,
    )


def _thresholds(content: dict[str, object]) -> ReviewThresholds:
    values = _any(content, "thresholds")
    if not isinstance(values, dict):
        raise ValueError("thresholds는 객체여야 한다")
    speed = values.get("reading_speed_max_cps")
    if isinstance(speed, bool) or not isinstance(speed, (int, float)):
        raise ValueError("reading_speed_max_cps는 숫자여야 한다")
    return ReviewThresholds(
        min_duration=_fraction_value(values.get("min_duration")),
        reading_speed_max_cps=float(speed),
    )


def _findings(content: dict[str, object]) -> tuple[RunFinding, ...]:
    return tuple(
        RunFinding(
            severity=_str(finding, "severity"),
            code=_str(finding, "code"),
            message=_str(finding, "message"),
            seg_id=_nullable_int(finding, "seg_id"),
            chunk_index=_nullable_int(finding, "chunk_index"),
        )
        for finding in _list(content, "findings")
    )


def initial_revision(snapshot: AutomaticSnapshot) -> ManualRevision:
    """스냅샷 위의 기본 리비전(0) — 자동 큐를 그대로, 편집 없이."""
    return initial_manual_revision(
        snapshot.identity,
        snapshot.cues,
        video_duration=snapshot.video_duration,
        sample_count=snapshot.sample_count,
    )


def save_revision(store: ReviewStore, revision: ManualRevision) -> ManualRevision:
    """리비전을 저장소에 no-replace로 못박는다 — 연쇄 검증을 통과하고 돌아온다."""
    store.save_manual(
        final_video_revision=revision.identity.final_video_revision,
        run_id=revision.identity.run_id,
        fingerprint=revision.identity.fingerprint,
        auto_content_sha256=revision.identity.auto_content_sha256,
        revision=revision.sequence,
        body=serialize_revision(revision),
        parent_sha256=revision.parent_sha256,
    )
    return revision


# ─────────────────────────────────────────────────────────────
#  검토 상태 — 세 겹 글자 비교·미대응·경고·파형 자료
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class CueRow:
    """큐 행 — 최신 글자(`cue`)와 읽기 속도를 함께(§11.7 화면 4)."""

    cue: ReviewCue
    chars_per_second: float


@dataclass(frozen=True, slots=True)
class WarningItem:
    """열린 경고 — key가 허용 장부와 정확히 맞물린다(정확한 key만, 흐릿한 대응 없음)."""

    key: str
    code: str
    message: str
    cue_id: str | None


@dataclass(frozen=True, slots=True)
class UnplacedView:
    """미대응 문구와 그 처리 — 살렸으면 가리킨 큐가 레코드에 있다(D37)."""

    item: UnplacedItem
    resolution: UnplacedResolution | None


@dataclass(frozen=True, slots=True)
class PlaybackData:
    """파형·반복 재생 자료 — 기하와 범위뿐, 재생 제어는 UI 소유다(§11.7)."""

    sample_rate: int
    sample_count: int
    channels: int
    duration: Fraction
    pcm_sha256: str
    cue_spans: tuple[tuple[Fraction, Fraction], ...]
    chunk_boundaries: tuple[tuple[Fraction, Fraction], ...]


@dataclass(frozen=True, slots=True)
class ReviewState:
    """검토 화면의 불변 자료 — 보기 상태(CaptionViewState)는 여기 없다."""

    identity: AutoIdentity
    status: str
    speech_basis: str
    head_revision: int | None
    head_fingerprint: str | None
    rows: tuple[CueRow, ...]
    unplaced: tuple[UnplacedView, ...]
    warnings: tuple[WarningItem, ...]
    allowances: tuple[Allowance, ...]
    auto_findings: tuple[RunFinding, ...]
    text_model_complete: bool
    timing_model_complete: bool
    playback: PlaybackData


def review_state(snapshot: AutomaticSnapshot, head: ManualRevision | None) -> ReviewState:
    """머리 리비전(없으면 기본) 위의 검토 상태 — 비교 행·미대응·경고·파형."""
    cues = snapshot.cues if head is None else head.cues
    identity = snapshot.identity
    return ReviewState(
        identity=identity,
        status=snapshot.status,
        speech_basis=snapshot.speech_basis,
        head_revision=None if head is None else head.sequence,
        head_fingerprint=None if head is None else content_fingerprint(head),
        rows=tuple(CueRow(cue=cue, chars_per_second=_cps(cue)) for cue in cues),
        unplaced=tuple(
            UnplacedView(
                item=item,
                resolution=_resolution_of(head, item.index) if head else None,
            )
            for item in snapshot.unplaced
        ),
        warnings=current_warnings(cues, snapshot),
        allowances=head.allowances if head else (),
        auto_findings=snapshot.findings,
        text_model_complete=all(chunk.text_ok for chunk in snapshot.chunks),
        timing_model_complete=all(chunk.timing_ok for chunk in snapshot.chunks),
        playback=PlaybackData(
            sample_rate=snapshot.sample_rate,
            sample_count=snapshot.sample_count,
            channels=snapshot.channels,
            duration=snapshot.video_duration,
            pcm_sha256=snapshot.pcm_sha256,
            cue_spans=tuple((cue.start, cue.end) for cue in cues),
            chunk_boundaries=_chunk_boundaries(snapshot),
        ),
    )


def _resolution_of(head: ManualRevision, index: int) -> UnplacedResolution | None:
    for record in head.unplaced_resolutions:
        if record.index == index:
            return record
    return None


def _chunk_boundaries(snapshot: AutomaticSnapshot) -> tuple[tuple[Fraction, Fraction], ...]:
    """청크 경계의 최종 영상 시각 — 원점 환산은 여기서 한 번(§13.3)."""
    origin = snapshot.origin
    return tuple(
        (
            origin + Fraction(span[0], snapshot.sample_rate),
            origin + Fraction(span[1], snapshot.sample_rate),
        )
        for span in snapshot.spans
    )


def current_warnings(
    cues: tuple[ReviewCue, ...], snapshot: AutomaticSnapshot
) -> tuple[WarningItem, ...]:
    """지금 큐가 남긴 경고 — 자동 항목에 지금 상태를 다시 얹는다(§11.11).

    시각을 고치면 범위 이탈·겹침은 사라지고, 문구를 고치면 읽기 속도가 다시 잰다.
    임계값은 스냅샷에 얼어 붙은 실행 설정이다 — 새 숫자를 만들지 않는다(규칙 8).
    """
    items: list[WarningItem] = []
    for cue in cues:
        items.extend(_cue_warnings(cue, snapshot))
    items.extend(_overlap_warnings(cues))
    items.extend(
        WarningItem(
            key=_finding_key(finding),
            code=finding.code,
            message=finding.message,
            cue_id=None,
        )
        for finding in snapshot.findings
        if finding.code not in _HANDLED_FINDINGS
    )
    return tuple(items)


def _cue_warnings(cue: ReviewCue, snapshot: AutomaticSnapshot) -> list[WarningItem]:
    found: list[WarningItem] = []
    duration = cue.duration
    if cue.end > snapshot.video_duration or cue.start < 0:
        found.append(_warn(cue, "out_of_final_range", "큐가 최종 영상 범위를 벗어났다"))
    if duration < snapshot.thresholds.min_duration:
        found.append(_warn(cue, "short_display", f"표시가 짧다: {float(duration):.2f}초"))
    if duration > 0 and _cps(cue) > snapshot.thresholds.reading_speed_max_cps:
        found.append(_warn(cue, "reading_speed", f"읽기 속도가 빠르다: {_cps(cue):.1f}자/초"))
    return found


def _overlap_warnings(cues: tuple[ReviewCue, ...]) -> list[WarningItem]:
    return [
        WarningItem(
            key=f"overlap:{earlier.cue_id}:{later.cue_id}",
            code="overlap",
            message=(f"큐가 겹친다: {float(earlier.end):.2f}초 > {float(later.start):.2f}초"),
            cue_id=later.cue_id,
        )
        for earlier, later in zip(cues, cues[1:], strict=False)
        if earlier.end > later.start
    ]


def _warn(cue: ReviewCue, code: str, message: str) -> WarningItem:
    return WarningItem(
        key=f"cue:{cue.cue_id}:{code}", code=code, message=message, cue_id=cue.cue_id
    )


def _finding_key(finding: RunFinding) -> str:
    locator = finding.seg_id if finding.seg_id is not None else finding.chunk_index
    suffix = "-" if locator is None else str(locator)
    return f"run:{finding.code}:{suffix}"


def _cps(cue: ReviewCue) -> float:
    duration = cue.duration
    return float(len(cue.text) / duration) if duration > 0 else float("inf")


# ─────────────────────────────────────────────────────────────
#  확정 게이트 — 구조 경색은 허용으로 못 뚫는다
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ReviewConfirmation:
    """명시적 검토 확인 — 짚은 큐 ID 전부를 실어야 확정이 문을 연다."""

    cue_ids: tuple[str, ...]
    note: str = ""


@dataclass(frozen=True, slots=True)
class FinalExport:
    """확정 게시 결과 — 새 리비전 디렉터리의 final SRT/VTT·manifest."""

    identity: AutoIdentity
    manual_revision: int
    directory: Path
    srt: Path
    vtt: Path
    manifest: Path
    export_sha256: str
    srt_sha256: str
    vtt_sha256: str


def confirm_final(
    store: ReviewStore,
    video: FinalVideoInput,
    head: ManualRevision,
    confirmation: ReviewConfirmation,
    *,
    language: str = DEFAULT_DRAFT_LANGUAGE,
) -> FinalExport:
    """머리 리비전을 확정 게시한다 — 게이트를 전부 통과해야 final이 나온다.

    구조 경색(소스 변경·신원 불일치·낡은 근거·미처리 오디오·무효 시각)은 어떤
    허용으로도 열리지 않는다. 시각 문제는 수동 시각 정정 감사로, 미배치는 수동
    큐·처리 기록으로 풀어야 게이트가 다시 열린다(§11.11).
    """
    snapshot = _require_fresh_basis(store, video, head)
    _require_stored_head(store, head)
    _require_coverage(snapshot)
    _require_valid_times(head)
    if not head.cues:
        _require_no_speech_decision(snapshot, head)
    _require_unplaced_resolved(snapshot, head)
    _require_warnings_allowed(head, snapshot)
    _require_confirmation(head, confirmation)
    return _publish_final(store, head, snapshot, confirmation, language)


def _require_fresh_basis(
    store: ReviewStore, video: FinalVideoInput, head: ManualRevision
) -> AutomaticSnapshot:
    """자동 근거·소스 신원 — 낡았거나 바뀐 자리 위에 확정은 없다."""
    frozen = store.find_automatic(
        final_video_revision=head.identity.final_video_revision,
        run_id=head.identity.run_id,
        fingerprint=head.identity.fingerprint,
    )
    if frozen is None:
        raise AutoBasisMissing(
            f"자동 스냅샷이 없다: run {head.identity.run_id!r} — 동결 없이는 확정할 수 없다"
        )
    if frozen.auto_content_sha256 != head.identity.auto_content_sha256:
        raise StaleAutoBasis("머리 리비전의 자동 내용 해시가 저장소와 다르다 — 낡은 자동 근거다")
    if video.final_video_revision != head.identity.final_video_revision:
        raise IdentityMismatch(
            f"머리 리비전은 렌더 {head.identity.final_video_revision[:12]}…의 것이다 —"
            f" 지금 영상은 {video.final_video_revision[:12]}…다"
        )
    require_unchanged(video)  # 검증 사본 SHA — 바뀌었으면 FinalVideoChanged
    require_audio_unchanged(video)  # PCM 기하·해시 — 같은 길이 변조도 닫는다
    snapshot = snapshot_from_frozen(frozen)
    _require_audio_basis(snapshot, video)  # 같은 렌더 해시의 다른 선택도 닫는다
    return snapshot


def _require_audio_basis(snapshot: AutomaticSnapshot, video: FinalVideoInput) -> None:
    """동결된 선택 오디오·영상 폭과 지금 입력이 같은가(§11.1·§11.11).

    `require_audio_unchanged`는 지금 입력이 **제 선언과** 맞는지만 본다 — 선언째
    바꿔치면 통과한다. 이 문은 얼어 붙은 자동 근거가 들은 선택 그 자체와 비교한다:
    같은 렌더 해시의 다른 PCM·다른 원점·다른 스트림은 확정이 아니라 재실행이다.
    """
    audio = video.audio
    frozen = {
        "pcm_sha256": snapshot.pcm_sha256,
        "sample_count": snapshot.sample_count,
        "sample_rate": snapshot.sample_rate,
        "channels": snapshot.channels,
        "origin": snapshot.origin,
        "audio_stream_index": snapshot.audio_stream_index,
        "audio_source_sha256": snapshot.audio_source_sha256,
        "audio_stream_count": snapshot.audio_stream_count,
        "video_duration": snapshot.video_duration,
    }
    current = {
        "pcm_sha256": audio.pcm_sha256,
        "sample_count": audio.sample_count,
        "sample_rate": audio.sample_rate,
        "channels": audio.channels,
        "origin": audio.pcm_sample0_file_time,
        "audio_stream_index": audio.audio_stream_index,
        "audio_source_sha256": audio.source_sha256,
        "audio_stream_count": video.audio_stream_count,
        "video_duration": _exact(video.video_duration),
    }
    changed = sorted(name for name, value in frozen.items() if current[name] != value)
    if changed:
        raise AudioBasisMismatch(
            f"선택 오디오·영상 폭이 동결된 자동 근거와 다르다: {changed} — 같은 렌더"
            " 해시의 다른 선택이다. 새 PCM에는 새 실행과 새 동결이 필요하다"
        )


def _require_stored_head(store: ReviewStore, head: ManualRevision) -> None:
    """머리 리비전은 저장된 지금 것이어야 한다 — 앞선 편집 위에 덮지 못한다."""
    stored = store.manual_head(
        final_video_revision=head.identity.final_video_revision,
        run_id=head.identity.run_id,
        fingerprint=head.identity.fingerprint,
        auto_content_sha256=head.identity.auto_content_sha256,
    )
    if stored is None or stored[0] != head.sequence or stored[1] != serialize_revision(head):
        raise StaleManualHead(
            f"리비전 {head.sequence}이(가) 저장된 머리가 아니다 — 머리 리비전만 확정할 수 있다"
        )


def _require_coverage(snapshot: AutomaticSnapshot) -> None:
    """청크 계획이 [0, 샘플 수]를 빈틈없이 덮는가 — 구멍은 재실행이다(§11.11)."""
    covered = sorted(snapshot.spans)
    if not covered or covered[0][0] != 0 or covered[-1][1] != snapshot.sample_count:
        raise IncompleteCoverage(f"청크 계획이 오디오 [0, {snapshot.sample_count})를 덮지 않는다")
    for earlier, later in zip(covered, covered[1:], strict=False):
        if earlier[1] != later[0]:
            raise IncompleteCoverage(
                f"청크 사이가 맞닿지 않다: {earlier} → {later} — 구멍·겹침은 재실행이다"
            )
    failed = [chunk.chunk_index for chunk in snapshot.chunks if not chunk.timing_ok]
    if failed or len(snapshot.chunks) != len(snapshot.spans):
        raise UnprocessedAudio(
            f"시간 요청을 못 받은 청크가 있다: {failed or '기록 부족'} — 듣지 못한 구간이다"
        )


def _require_valid_times(head: ManualRevision) -> None:
    """무효 시각 — 음수·뒤집힘. 범위 이탈(끝>길이)은 경로가 다르다(경고)."""
    for cue in head.cues:
        if cue.start < 0 or cue.start >= cue.end:
            raise InvalidCueTimes(
                f"큐 {cue.cue_id!r}: 시각이 무효다 [{cue.start}, {cue.end}) —"
                " 수동 시각 정정으로 풀어야 한다"
            )


def _require_no_speech_decision(snapshot: AutomaticSnapshot, head: ManualRevision) -> None:
    """빈 실행의 확정 — 전부 정상·전부 빈·사람 결정, 셋 다 필요하다(§11.11)."""
    if snapshot.speech_basis != BASIS_UNCONFIRMED_EMPTY:
        raise EmptyRunUndecided(
            f"빈 결과의 근거가 {snapshot.speech_basis!r}다 — 무발화 확정이 아니다"
        )
    if snapshot.status != STATUS_REVIEW_REQUIRED or not all(
        chunk.timing_ok and chunk.text_ok for chunk in snapshot.chunks
    ):
        raise EmptyRunUndecided(
            "오류가 섞였거나 미확정 상태가 아닌 빈 실행이다 — 0바이트 SRT 성공은 없다"
        )
    if not head.no_speech:
        raise EmptyRunUndecided(
            "무발화가 아직 미확정이다 — 독립적인 사용자 결정(record_no_speech)이 필요하다"
        )


def _require_unplaced_resolved(snapshot: AutomaticSnapshot, head: ManualRevision) -> None:
    """미대응 문구는 하나씩 처리된다 — 살렸거나 버린 이유가 남았거나(D37·§11.5)."""
    resolved = {record.index: record for record in head.unplaced_resolutions}
    for item in snapshot.unplaced:
        record = resolved.get(item.index)
        if record is None or record.text != item.text:
            raise UnresolvedUnplaced(
                f"미대응 문구 #{item.index} {item.text[:12]}…의 처리 기록이 없다 —"
                " 수동 큐로 살리거나 버린 이유를 남겨야 한다"
            )
        if record.placed_cue_id is not None and record.placed_cue_id not in {
            cue.cue_id for cue in head.cues
        }:
            raise UnresolvedUnplaced(
                f"미대응 #{item.index}이(가) 가리킨 큐 {record.placed_cue_id!r}가 없다"
            )


def _require_warnings_allowed(head: ManualRevision, snapshot: AutomaticSnapshot) -> None:
    """열린 경고는 정확한 key의 허용으로만 닫힌다 — 흐릿한 대응은 없다(§11.11)."""
    allowed = {allowance.key for allowance in head.allowances}
    missing = [
        warning.key
        for warning in current_warnings(head.cues, snapshot)
        if warning.key not in allowed
    ]
    if missing:
        raise UnallowedWarnings(
            f"허용되지 않은 경로가 남아 있다: {missing} — 풀거나 이유와 함께 허용해야 한다"
        )


def _require_confirmation(head: ManualRevision, confirmation: ReviewConfirmation) -> None:
    """확인은 전부 짚고 중복 없이 — 큐 하나하나를 본 증거다(§11.7 검토 확정)."""
    cues = {cue.cue_id for cue in head.cues}
    confirmed = set(confirmation.cue_ids)
    if confirmed != cues or len(confirmation.cue_ids) != len(cues):
        raise ConfirmationIncomplete(
            f"검토 확인이 완전하지 않다 — 확인 {len(confirmed)}/{len(cues)} 큐,"
            " 빠짐·중복·낡은 ID가 없어야 한다"
        )


# ─────────────────────────────────────────────────────────────
#  final 게시 — 새 리비전 디렉터리, 원자적 no-replace
# ─────────────────────────────────────────────────────────────


def _publish_final(
    store: ReviewStore,
    head: ManualRevision,
    snapshot: AutomaticSnapshot,
    confirmation: ReviewConfirmation,
    language: str,
) -> FinalExport:
    """final SRT/VTT·manifest를 새 디렉터리에 게시 — manifest가 마지막이다."""
    srt_body, vtt_body = _final_bodies(head.cues)
    srt_sha = sha256_text(srt_body)
    vtt_sha = sha256_text(vtt_body)
    export_sha = sha256_text(canonical_json({"srt": srt_body, "vtt": vtt_body}))
    token = f"r{head.sequence:04d}-{export_sha[:12]}"
    manifest = _manifest_body(head, snapshot, confirmation, language, srt_sha, vtt_sha, export_sha)
    directory = store.publish_final(
        final_video_revision=head.identity.final_video_revision,
        run_id=head.identity.run_id,
        fingerprint=head.identity.fingerprint,
        token=token,
        files=[
            (f"final.{language}.srt", srt_body),
            (f"final.{language}.vtt", vtt_body),
            ("manifest.json", canonical_json(manifest)),
        ],
    )
    return FinalExport(
        identity=head.identity,
        manual_revision=head.sequence,
        directory=directory,
        srt=directory / f"final.{language}.srt",
        vtt=directory / f"final.{language}.vtt",
        manifest=directory / "manifest.json",
        export_sha256=export_sha,
        srt_sha256=srt_sha,
        vtt_sha256=vtt_sha,
    )


def _final_bodies(cues: tuple[ReviewCue, ...]) -> tuple[str, str]:
    """final 본문 — 재파싱 검사를 통과한 SRT/VTT만 돌아온다(§11.7)."""
    shells = tuple(_shell(cue) for cue in cues)
    srt_body = write_srt(to_engine_cues(shells))
    vtt_body = write_vtt(shells)
    try:
        verify_serialization(srt_body, vtt_body)
    except ArtifactVerificationError as error:
        raise FinalSerializationError(f"final 직렬화 검사 실패: {error}") from error
    _assert_reparse(srt_body, vtt_body, cues)
    return srt_body, vtt_body


def _shell(cue: ReviewCue) -> CaptionCue:
    """최종 직렬화용 껍데기 — 시각은 밀리초 격자로, 근거 칸은 비운다(비교용이 아니다).

    SRT·VTT가 표현할 수 있는 시각은 정수 밀리초다(§11.6). 격자에 얹는 양자화는
    이 **직렬화 경계에서만** 일어난다 — 리비전·스냅샷의 유리수 시각은 그대로다.
    먼저 격자에 얹은 뒤 코덱에 주면 float 이중 반올림으로 1밀리초 어긋날 길이
    아예 없다.
    """
    return CaptionCue(
        start=Fraction(_ms_int(cue.start), 1000),
        end=Fraction(_ms_int(cue.end), 1000),
        text=cue.text,
        whisper_text="",
        timing_segment_ids=(),
        timing_evidence_id="",
        text_evidence_ids=(),
        text_source="",
        timing_source="",
        correction_status="",
    )


def _assert_reparse(srt_body: str, vtt_body: str, cues: tuple[ReviewCue, ...]) -> None:
    """직렬화한 final을 다시 읽어 큐 자료와 같은지 본다(§11.7·A7-3 완료 기준).

    SRT 재독기는 초와 밀리초를 float **합**으로 되돌린다 — 3.183초 같은 값은
    정확한 유리수와 1ulp 어긋난다. 그래서 판정은 형식의 격자인 **정수 밀리초**로
    정규화해서 한다: 되읽은 float에서 직렬된 밀리초를 정확히 되찾고(코덱 합의
    오차는 1e-9초 이하), 원본 유리수의 half-up 반올림과 같은지 본다. 오차 허용도
    도메인 양자화도 아니다 — 격자 위에서의 정확한 일치다.
    """
    reparsed = parse_srt(srt_body)
    if len(reparsed) != len(cues):
        raise FinalSerializationError(
            f"final SRT 재파싱이 큐 수와 다르다: {len(reparsed)} vs {len(cues)}"
        )
    for parsed, cue in zip(reparsed, cues, strict=True):
        if (
            parsed.text != _flat_text(cue.text)
            or _serialized_ms(parsed.start) != _ms_int(cue.start)
            or _serialized_ms(parsed.end) != _ms_int(cue.end)
        ):
            raise FinalSerializationError(
                f"final SRT 재파싱이 큐 {cue.cue_id!r}와 같지 않다 — 게시되지 않는다"
            )
    vtt_cues = parse_vtt(vtt_body)
    if [(_ms_int(start), _ms_int(end), text) for start, end, text in vtt_cues] != [
        (_ms_int(cue.start), _ms_int(cue.end), _flat_text(cue.text)) for cue in cues
    ]:
        raise FinalSerializationError("final VTT 재파싱이 큐 자료와 같지 않다")


def _flat_text(text: str) -> str:
    """SRT 재독기의 줄 이음 규칙 — 여러 줄 글자는 공백으로 이어 다시 읽힌다."""
    return " ".join(text.splitlines())


def _serialized_ms(seconds: float) -> int:
    """재독기 float에서 직렬된 밀리초를 되찾는다 — 코덱 합의 오차는 1e-9초다."""
    return round(seconds * 1000)


def _ms_int(value: Fraction) -> int:
    """유리수를 밀리초 정수로 반올림(half-up) — 직렬화 격자의 유일한 양자화(§11.6)."""
    return (value.numerator * 1000 + value.denominator // 2) // value.denominator


def _manifest_body(
    head: ManualRevision,
    snapshot: AutomaticSnapshot,
    confirmation: ReviewConfirmation,
    language: str,
    srt_sha: str,
    vtt_sha: str,
    export_sha: str,
) -> dict[str, object]:
    """확정 기록 — 실패한 문구 모델을 성공으로 바꾸지 않는다(§11.3·§11.11)."""
    return {
        "schema": FINAL_MANIFEST_SCHEMA,
        "identity": {
            "final_video_revision": head.identity.final_video_revision,
            "run_id": head.identity.run_id,
            "fingerprint": head.identity.fingerprint,
            "auto_content_sha256": head.identity.auto_content_sha256,
        },
        "language": language,
        "manual_revision": head.sequence,
        "manual_fingerprint": content_fingerprint(head),
        "run_status": snapshot.status,
        "speech_basis": snapshot.speech_basis,
        "text_model_complete": all(chunk.text_ok for chunk in snapshot.chunks),
        "timing_model_complete": all(chunk.timing_ok for chunk in snapshot.chunks),
        "video_duration": _fraction(snapshot.video_duration),
        "sample_count": snapshot.sample_count,
        "cue_count": len(head.cues),
        "cues": [cue_document(cue) for cue in head.cues],
        **_audit_document(head, confirmation),
        "srt_sha256": srt_sha,
        "vtt_sha256": vtt_sha,
        "export_sha256": export_sha,
    }


def _audit_document(head: ManualRevision, confirmation: ReviewConfirmation) -> dict[str, object]:
    """확정의 감사 부분 — 허용·미배치 처리·무발화 결정·검토 확인(§11.11)."""
    return {
        "allowances": [{"key": a.key, "reason": a.reason} for a in head.allowances],
        "unplaced_resolutions": [
            {
                "index": r.index,
                "text": r.text,
                "placed_cue_id": r.placed_cue_id,
                "dismissed_reason": r.dismissed_reason,
            }
            for r in head.unplaced_resolutions
        ],
        "no_speech": head.no_speech,
        "no_speech_note": head.no_speech_note,
        "confirmation": {"cue_ids": list(confirmation.cue_ids), "note": confirmation.note},
    }


# ─────────────────────────────────────────────────────────────
#  스냅샷 본문 독기 — 엄격한 타입 검사(§13)
# ─────────────────────────────────────────────────────────────


def _unplaced(content: dict[str, object]) -> tuple[UnplacedItem, ...]:
    return tuple(
        UnplacedItem(
            index=_int(item, "index"),
            text=_str(item, "text"),
            chars=_int(item, "chars"),
            chunk_index=_int(item, "chunk_index"),
            seek_start=_fraction_value(_any(item, "seek_start")),
            seek_end=_fraction_value(_any(item, "seek_end")),
        )
        for item in _list(content, "unplaced")
    )


def _cues(content: dict[str, object]) -> tuple[ReviewCue, ...]:
    return tuple(
        ReviewCue(
            cue_id=_str(item, "cue_id"),
            start=_fraction_value(_any(item, "start")),
            end=_fraction_value(_any(item, "end")),
            text=_str(item, "text"),
            text_basis=_str(item, "text_basis"),
            auto_text=_str(item, "auto_text"),
            whisper_text=_str(item, "whisper_text"),
            timing_segment_ids=tuple(
                _int_scalar(segment, "timing_segment_ids[]")
                for segment in _list(item, "timing_segment_ids")
            ),
            timing_evidence_id=_str(item, "timing_evidence_id"),
            text_evidence_ids=tuple(
                _str_scalar(evidence, "text_evidence_ids[]")
                for evidence in _list(item, "text_evidence_ids")
            ),
            correction_status=_str(item, "correction_status"),
            flags=tuple(_str_scalar(flag, "flags[]") for flag in _list(item, "flags")),
            manual_note=_str(item, "manual_note"),
        )
        for item in _list(content, "cues")
    )


def _spans(content: dict[str, object]) -> tuple[tuple[int, int], ...]:
    spans = _list(content, "spans")
    parsed = []
    for span in spans:
        if not isinstance(span, list) or len(span) != 2:
            raise ValueError(f"spans 원소는 [시작, 끝] 쌍이어야 한다: {span!r}")
        start, end = span
        if not _is_int(start) or not _is_int(end):
            raise ValueError(f"spans 원소는 정수 쌍이어야 한다: {span!r}")
        parsed.append((start, end))
    return tuple(parsed)


def _int_scalar(value: object, where: str) -> int:
    if not _is_int(value):
        raise ValueError(f"{where}은(는) 정수여야 한다")
    return value


def _str_scalar(value: object, where: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{where}은(는) 문자열이어야 한다")
    return value


def _is_int(value: object) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _any(content: object, key: str) -> object:
    if not isinstance(content, dict) or key not in content:
        raise ValueError(f"스냅샷에 {key!r}이(가) 없다 — 계약 밖 문서다")
    return content[key]


def _str(item: object, key: str) -> str:
    value = _any(item, key)
    if not isinstance(value, str):
        raise ValueError(f"{key!r}은(는) 문자열이어야 한다")
    return value


def _int(item: object, key: str) -> int:
    value = _any(item, key)
    if not _is_int(value):
        raise ValueError(f"{key!r}은(는) 정수여야 한다")
    return value


def _nullable_int(item: object, key: str) -> int | None:
    if not isinstance(item, dict) or item.get(key) is None:
        return None
    return _int(item, key)


def _list(item: object, key: str) -> list[object]:
    value = _any(item, key)
    if not isinstance(value, list):
        raise ValueError(f"{key!r}은(는) 배열이어야 한다")
    return value


def _fraction_value(item: object) -> Fraction:
    if not isinstance(item, dict) or set(item) != {"num", "den"}:
        raise ValueError(f"유리수는 {{num, den}} 쌍이어야 한다: {item!r}")
    num, den = item["num"], item["den"]
    if not _is_int(num) or not _is_int(den) or den <= 0:
        raise ValueError(f"유리수 분자·분모가 계약 밖이다: {item!r}")
    return Fraction(num, den)


def _fraction(value: Fraction | float) -> dict[str, int]:
    """유리수의 정본 표기 — float 길이는 문자열로 한 번 유리수화 한다(§13.3)."""
    exact = _exact(value)
    return {"num": exact.numerator, "den": exact.denominator}


def _exact(value: Fraction | float) -> Fraction:
    """float를 흔들지 않는 정확 유리수 — 같은 float는 같은 값이 된다."""
    return value if isinstance(value, Fraction) else Fraction(str(value))
