"""A7 작업공간 오케스트레이션 — 등록→계획→승인→실행→검토→확정의 재사용 흐름(§16 A7).

받아들여진 A7-2 엔진(`final_caption`)·A7-3 검토 라이브러리(`caption_review`)를
**호출로만** 연결한다 — 어느 쪽도 한 줄 고치지 않는다. 이 계층이 더하는 것은
지속성과 문 뿐이다: 등록 행(재시작 뒤 재적재), 호출 없는 추정, 지문에 묶인
명시적 승인, `JobContext`를 받는 실행 본문, 같은 run·지문에서만 재생하는 응답
캐시, 검토 편집 래퍼, 전체 큐 명시 확인의 확정.

지키는 선:

- **상태는 정직하다.** 오디오 없는 영상은 `no_audio` 상태로 등록에 남는다 —
  무발화 확정도 빈 SRT도 아니다(§11.11). 오디오가 여럿이면 선택 확정 전까지
  `audio_selection_required`다. 어느 쪽도 제공자를 부르지 않는다.
- **승인이 실행보다 앞선다.** 오프라인 합성 실행에서도 저장된 승인이 계획의
  지문과 정확히 같아야 첫 호출이 나간다 — 어긋나면 유료 호출 0회로 거부다.
- **포장이 문을 숨기지 않는다.** 엔진의 실전송 게이트는 받은 제공자의 겉모습을
  보므로, 캐시 포장을 입히기 **전에** 원본 제공자를 같은 문에 세운다(루트 재검):
  알려진 실전송 어댑터는 `network_mode=online`에서만 새 호출을 하고, 닫힌 문
  아래에서는 저장된 응답의 재생으로만 이어진다 — 재생은 호출이 아니므로.
- **재개는 같은 신원에서만.** 응답 캐시는 (run, 지문, 요청 ID, 오디오 해시·샘플
  경계)가 전부 같을 때만 재생하고, 나머지는 실제 제공자에게 새로 묻는다(§14.2).
  이미 동결된 실행의 재제출은 제공자를 전혀 안 부른다.
- **증거는 호출 직후 굳는다.** 제공자가 응답을 돌려준 즉시 캐시 행으로 남으므로
  서버가 죽어도 들은 구간은 다시 묻지 않는다(§14.1).
- **미디어 등록은 안 한다.** 확정 결과는 경로와 함께 공개 서비스로 돌아갈
  뿐이다 — 브라우저 제공은 W1의 권한이다.
"""

from __future__ import annotations

import array
import hashlib
import inspect
import json
import os
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import cast

from vlogsrt import env, preprocess
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.project.raw import capabilities
from vlogsrt.project.raw import caption_review as review
from vlogsrt.project.raw.caption_artifacts import publish_caption_artifacts
from vlogsrt.project.raw.caption_config import DEFAULT_DRAFT_LANGUAGE, FinalCaptionSettings
from vlogsrt.project.raw.caption_manual import (
    AutoIdentity,
    ManualRevision,
    add_cue,
    allow_finding,
    edit_text,
    edit_time,
    merge_cues,
    parse_revision,
    record_no_speech,
    resolve_unplaced,
    split_cue,
)
from vlogsrt.project.raw.caption_provider import (
    AudioChunkBinding,
    RescueTimingProvider,
    TextTranscript,
    TextTranscriptProvider,
    TimedTranscript,
    TimingProvider,
    TransportTextProvider,
    TransportTimingProvider,
    parse_text_response,
    parse_timing_response,
)
from vlogsrt.project.raw.caption_workspace_store import CaptionWorkspaceStore
from vlogsrt.project.raw.caption_workspace_types import (
    REGISTRATION_AUDIO_SELECTION_REQUIRED,
    REGISTRATION_NO_AUDIO,
    REGISTRATION_READY,
    RESPONSE_ORIGIN_INJECTED,
    RESPONSE_ORIGIN_LIVE,
    RUN_CANCELLED,
    RUN_FAILED,
    RUN_SUCCEEDED,
    ApprovalNotFoundError,
    ApprovalRecord,
    CachedResponse,
    CaptionRunRecord,
    CaptionWorkspaceError,
    ConfirmedExport,
    FinalVideoRegistration,
    FreshSendBlockedError,
    ManualHeadConflict,
    ModelLifecycle,
    RegistrationNotFoundError,
    RegistrationNotReadyError,
    RunEstimate,
    RunIdentity,
    RunNotFoundError,
    WaveformBins,
)
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.final_caption import (
    STATUS_PARTIAL_FAILURE,
    CaptionApproval,
    CaptionProviderMissing,
    CaptionRunCancelled,
    CaptionRunPlan,
    FinalCaptionResult,
    expected_approval,
    plan_caption_run,
    require_approval,
    run_final_captions,
)
from vlogsrt.project.raw.final_video import (
    AmbiguousAudioSelection,
    FinalVideoInput,
    FinalVideoNoAudio,
    register_final_video,
)
from vlogsrt.project.raw.jobs import JobCancelledError, JobContext
from vlogsrt.project.raw.understanding import SelectedAudio
from vlogsrt.project.raw.workspace import RawWorkspace

CAPTION_MEDIA_REL = "final/captions"
"""검증 사본·선택 오디오가 사는 자리 — `final/` 이름공간 안의 내부 작업 디렉터리."""

CAPTION_DRAFT_REL = "subtitles"
"""초안 게시물의 자리 — API 원응답을 포함하므로 브라우저 제공 대상이 아니다(§4.4)."""

CAPTION_REVIEW_REL = "review/captions"
"""A7-3 검토 저장소의 뿌리 — automatic·manual·final이 신원마다 갈라진다."""

CAPTION_CACHE_REL = "cache/captions"
"""청크 WAV의 자리 — 재실행이 같은 바이트를 다시 쓰지 않게 하는 작업 공간."""

_WAVEFORM_PEAK_SCALE = 1 << 15
"""s16 PCM의 최대 진폭 — 피크 빈의 척도다(진폭 외에는 아무것도 만들지 않는다)."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _today() -> date:
    return datetime.now(UTC).date()


def new_registration_id() -> str:
    """등록 행의 전역 유일 불변 ID — 미디어·자산 ID와 같은 규칙(128비트)."""
    return f"creg_{secrets.token_hex(8)}"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def open_caption_workspace(workspace: RawWorkspace) -> CaptionWorkspace:
    """작업공간 하나의 자막 백엔드를 연다 — 덧붙이기 표는 여기서 보장된다."""
    return CaptionWorkspace(workspace)


@dataclass(frozen=True, slots=True)
class ReviewBundle:
    """검토 화면의 불변 자료 — 동결 스냅샷·상태·머리 리비전을 한데 모은다(§11.7)."""

    snapshot: review.AutomaticSnapshot
    state: review.ReviewState
    head: ManualRevision | None


@dataclass(frozen=True, slots=True)
class _ProviderPair:
    timing: CachedTimingProvider
    text: CachedTextProvider


def _cache_matches(cached: CachedResponse, binding: AudioChunkBinding, role: str) -> bool:
    """캐시 행이 이 요청과 같은 청크를 들었는가 — 신원 넷이 전부 같아야 한다."""
    return (
        cached.role == role
        and cached.audio_sha256 == binding.audio_sha256
        and cached.sample_start == binding.sample_start
        and cached.sample_end == binding.sample_end
        and cached.sample_rate == binding.sample_rate
    )


def _replay_guard(cached: CachedResponse | None, binding: AudioChunkBinding, role: str) -> bool:
    return cached is not None and _cache_matches(cached, binding, role)


class CachedTimingProvider:
    """재생·기록 시간 제공자 — 같은 신원의 응답만 재생하고 증거는 즉시 굳힌다.

    재생은 캐시가 **들었던 그 청크**(해시·샘플 경계)에만 붙는다. 빗나가면 실제
    제공자에게 새로 묻는다 — 낡은 응답을 새 오디오에 얹는 길이 여기서 닫힌다.
    `replays_only`일 때는 그 새 묻기 자체가 금지다: 오프라인 문이 닫힌 실행에서
    캐시를 빗나가면 예외로 끝내고 절대 전송하지 않는다(§14.3). 방금 굳힌 행은
    실행 안 캐시에도 남으므로, 같은 실행 안에서 같은 요청 ID가 다시 오면
    다시 묻지 않고 재생한다.
    """

    def __init__(
        self,
        inner: TimingProvider,
        cache: dict[str, CachedResponse],
        store: CaptionWorkspaceStore,
        identity: RunIdentity,
        *,
        replays_only: bool = False,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._store = store
        self._identity = identity
        self._replays_only = replays_only
        self._origin = (
            RESPONSE_ORIGIN_LIVE
            if isinstance(inner, TransportTimingProvider)
            else RESPONSE_ORIGIN_INJECTED
        )
        self.fresh_calls = 0
        self.replays = 0

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
        return self._respond(binding, chunk, settings, on, stem, source, None)

    def _respond(
        self,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str,
        source: str,
        temperature: float | None,
    ) -> TimedTranscript:
        cached = self._cache.get(binding.request_id)
        if _replay_guard(cached, binding, capabilities.ROLE_TIMING):
            assert cached is not None
            self.replays += 1
            return parse_timing_response(
                cached.payload,
                binding=binding,
                model=settings.models.timing_model,
                provider=settings.caption.timing_provider,
                chunk_duration=chunk.duration,
                chunk_offset=chunk.offset,
            )
        if self._replays_only:
            raise FreshSendBlockedError(
                f"재생 전용 실행인데 캐시에 없는 요청이다: {binding.request_id!r} —"
                " 오프라인 문이 닫혀 있어 새 전송은 못 나간다(§14.3)"
            )
        self.fresh_calls += 1
        transcript = self._ask_inner(binding, chunk, settings, on, stem, source, temperature)
        row = _cached_of(
            self._identity, binding, capabilities.ROLE_TIMING, transcript.response, self._origin
        )
        self._store.record_responses([row])
        self._cache[binding.request_id] = row
        return transcript

    def _ask_inner(
        self,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str,
        source: str,
        temperature: float | None,
    ) -> TimedTranscript:
        return self._inner.timed_transcript(
            binding=binding, chunk=chunk, settings=settings, on=on, stem=stem, source=source
        )


class TemperedCachedTimingProvider(CachedTimingProvider):
    """온도 손잡이를 가진 원본의 포장 — 재전사 회복 온도를 그대로 넘긴다(D28).

    엔진은 제공자의 `timed_transcript` 서명에 `temperature`가 있는지 보고 뜨거운
    재전사를 걸지 결정한다. 포장이 그 서명을 지워버리면 실전송의 회복 능력이
    조용히 사라지므로, 원본이 손잡이를 가질 때만 이 갈래를 쓴다 — 서명이 곧
    능력의 증거다.
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
        temperature: float | None = None,
    ) -> TimedTranscript:
        return self._respond(binding, chunk, settings, on, stem, source, temperature)

    def _ask_inner(
        self,
        binding: AudioChunkBinding,
        chunk: Chunk,
        settings: RawWorkflowConfig,
        on: date,
        stem: str,
        source: str,
        temperature: float | None,
    ) -> TimedTranscript:
        rescue = cast(
            "RescueTimingProvider", self._inner
        )  # _wrap_timing의 서명 검사가 이 계약을 보증한다
        return rescue.timed_transcript(
            binding=binding,
            chunk=chunk,
            settings=settings,
            on=on,
            stem=stem,
            source=source,
            temperature=temperature,
        )


class CachedTextProvider:
    """재생·기록 문구 제공자 — 시간 제공자와 대칭이다."""

    def __init__(
        self,
        inner: TextTranscriptProvider,
        cache: dict[str, CachedResponse],
        store: CaptionWorkspaceStore,
        identity: RunIdentity,
        *,
        replays_only: bool = False,
    ) -> None:
        self._inner = inner
        self._cache = cache
        self._store = store
        self._identity = identity
        self._replays_only = replays_only
        self._origin = (
            RESPONSE_ORIGIN_LIVE
            if isinstance(inner, TransportTextProvider)
            else RESPONSE_ORIGIN_INJECTED
        )
        self.fresh_calls = 0
        self.replays = 0

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
        cached = self._cache.get(binding.request_id)
        if _replay_guard(cached, binding, capabilities.ROLE_TEXT):
            assert cached is not None
            self.replays += 1
            return parse_text_response(
                cached.payload, binding=binding, model=settings.models.text_model
            )
        if self._replays_only:
            raise FreshSendBlockedError(
                f"재생 전용 실행인데 캐시에 없는 요청이다: {binding.request_id!r} —"
                " 오프라인 문이 닫혀 있어 새 전송은 못 나간다(§14.3)"
            )
        self.fresh_calls += 1
        transcript = self._inner.text_transcript(
            binding=binding,
            chunk=chunk,
            settings=settings,
            on=on,
            prompt=prompt,
            stem=stem,
            source=source,
        )
        row = _cached_of(
            self._identity, binding, capabilities.ROLE_TEXT, transcript.response, self._origin
        )
        self._store.record_responses([row])
        self._cache[binding.request_id] = row
        return transcript


def _cached_of(
    identity: RunIdentity, binding: AudioChunkBinding, role: str, payload: object, origin: str
) -> CachedResponse:
    """응답 하나의 캐시 행 — 신원·원 payload·출처를 함께 굳힌다(§11.3)."""
    document = payload if isinstance(payload, Mapping) else {}
    return CachedResponse(
        run_id=identity.run_id,
        fingerprint=identity.fingerprint,
        request_id=binding.request_id,
        role=role,
        chunk_index=binding.chunk_index,
        audio_sha256=binding.audio_sha256,
        sample_start=binding.sample_start,
        sample_end=binding.sample_end,
        sample_rate=binding.sample_rate,
        payload=document,
        created_at=_now(),
        origin=origin,
    )


class CaptionWorkspace:
    """작업공간 하나의 자막 백엔드 — 상태는 DB·검토 저장소가 정본이다."""

    def __init__(self, workspace: RawWorkspace) -> None:
        self._workspace = workspace
        self._store = CaptionWorkspaceStore(workspace.root)
        self._review = review.open_review_store(workspace.root / CAPTION_REVIEW_REL)

    @property
    def root(self) -> Path:
        return self._workspace.root

    @property
    def workspace(self) -> RawWorkspace:
        return self._workspace

    @property
    def review_store(self) -> review.ReviewStore:
        return self._review

    @property
    def store(self) -> CaptionWorkspaceStore:
        """덧붙이기 표의 저장소 — 조회·감사용 공개 창구(W1이 함께 읽는다)."""
        return self._store

    # ── 등록 — 정직한 상태로 남는다 ─────────────────────────────

    def register_final_video(
        self, source: Path, *, source_id: str, audio_ordinal: int | None = None
    ) -> FinalVideoRegistration:
        """외부 최종 영상을 `final/` 이름공간에 등록한다 — 재등록은 멱등이다.

        오디오가 없으면 `no_audio` 상태 행으로, 여럿인데 선택이 없으면
        `audio_selection_required` 상태 행으로 남는다 — 예외로 지우지 않는다.
        """
        try:
            video = register_final_video(
                source, self._media_root(), source_id=source_id, audio_ordinal=audio_ordinal
            )
        except FinalVideoNoAudio:
            return self._register_state(source, source_id, REGISTRATION_NO_AUDIO)
        except AmbiguousAudioSelection:
            return self._register_state(source, source_id, REGISTRATION_AUDIO_SELECTION_REQUIRED)
        return self._register_ready(video, source, audio_ordinal)

    def _media_root(self) -> Path:
        media = self._workspace.root / CAPTION_MEDIA_REL
        media.mkdir(parents=True, exist_ok=True)
        return media

    def _register_ready(
        self, video: FinalVideoInput, source: Path, ordinal: int | None
    ) -> FinalVideoRegistration:
        registration = FinalVideoRegistration(
            registration_id=new_registration_id(),
            source_id=video.source_id,
            final_video_revision=video.final_video_revision,
            status=REGISTRATION_READY,
            audio_ordinal=0 if ordinal is None else ordinal,
            audio_stream_count=video.audio_stream_count,
            external_source=str(source),
            audio_stream_index=video.audio.audio_stream_index,
            pcm_sha256=video.audio.pcm_sha256,
            sample_rate=video.audio.sample_rate,
            channels=video.audio.channels,
            sample_count=video.audio.sample_count,
            origin=video.audio.pcm_sample0_file_time,
            video_duration=video.video_duration,
            verified_copy_rel=self._rel(video.verified_copy_path),
            pcm_rel=self._rel(video.audio.pcm_path),
            manifest_rel=self._rel(video.manifest_path),
            created_at=_now(),
        )
        return self._persist_registration(registration)

    def _register_state(self, source: Path, source_id: str, status: str) -> FinalVideoRegistration:
        """준비되지 않은 상태의 등록 행 — 해시와 스트림 수만 잰다(호출은 없다)."""
        registration = FinalVideoRegistration(
            registration_id=new_registration_id(),
            source_id=source_id,
            final_video_revision=_sha256_file(source),
            status=status,
            audio_ordinal=None,
            audio_stream_count=preprocess.probe_audio_stream_count(source),
            external_source=str(source),
            audio_stream_index=None,
            pcm_sha256=None,
            sample_rate=None,
            channels=None,
            sample_count=None,
            origin=None,
            video_duration=None,
            verified_copy_rel=None,
            pcm_rel=None,
            manifest_rel=None,
            created_at=_now(),
        )
        return self._persist_registration(registration)

    def _persist_registration(self, registration: FinalVideoRegistration) -> FinalVideoRegistration:
        existing = self._store.registration(
            registration.final_video_revision, registration.audio_ordinal
        )
        if existing is not None:
            return _same_registration_or_conflict(existing, registration)
        self._store.save_registration(registration)
        return registration

    def registration(
        self, final_video_revision: str, audio_ordinal: int | None = None
    ) -> FinalVideoRegistration | None:
        return self._store.registration(final_video_revision, audio_ordinal)

    def select_registration(self, registration: FinalVideoRegistration) -> None:
        """Persist the selected final render independently of historical caption runs."""
        if self.registration(registration.final_video_revision, registration.audio_ordinal) is None:
            raise RegistrationNotReadyError("등록되지 않은 최종 영상입니다")
        target = self.root / "final" / "active_video.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".active-video-", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "revision": registration.final_video_revision,
                        "audio_ordinal": registration.audio_ordinal,
                    },
                    handle,
                )
            os.replace(temporary, target)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def active_registration(self) -> FinalVideoRegistration | None:
        target = self.root / "final" / "active_video.json"
        if not target.exists():
            rows = self.registrations()
            return rows[-1] if rows else None
        try:
            selected = json.loads(target.read_text(encoding="utf-8"))
            return self.registration(selected["revision"], selected["audio_ordinal"])
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise RegistrationNotReadyError("최종 영상 선택 기록을 읽을 수 없습니다") from error

    def registrations(self) -> tuple[FinalVideoRegistration, ...]:
        return self._store.registrations()

    def video_input(self, registration: FinalVideoRegistration) -> FinalVideoInput:
        """등록 행을 다시 `FinalVideoInput`으로 — 재시작 뒤 같은 신원이다(§9.2)."""
        if not registration.ready:
            raise RegistrationNotReadyError(
                f"등록이 {registration.status!r}다 — 오디오 선택이 확정된 등록만 실행된다"
                f" (렌더 {registration.final_video_revision[:12]}…)"
            )
        external = Path(registration.external_source)
        original = external if external.is_file() else self._abs(registration.verified_copy_rel)
        origin = registration.origin
        if origin is None:
            raise CaptionWorkspaceError("준비된 등록에 원점이 없다 — 손상된 행이다")
        return FinalVideoInput(
            source_id=registration.source_id,
            final_video_revision=registration.final_video_revision,
            original_path=original,
            verified_copy_path=self._abs(registration.verified_copy_rel),
            verified_copy_sha256=registration.final_video_revision,
            manifest_path=self._abs(registration.manifest_rel),
            audio=SelectedAudio(
                source_id=registration.source_id,
                source_path=self._abs(registration.verified_copy_rel),
                source_sha256=registration.final_video_revision,
                audio_stream_index=_required_int(
                    registration.audio_stream_index, "audio_stream_index"
                ),
                pcm_path=self._abs(registration.pcm_rel),
                pcm_sha256=_required_text(registration.pcm_sha256, "pcm_sha256"),
                sample_rate=_required_int(registration.sample_rate, "sample_rate"),
                channels=_required_int(registration.channels, "channels"),
                sample_count=_required_int(registration.sample_count, "sample_count"),
                pcm_sample0_file_time=origin,
            ),
            video_duration=_required_float(registration.video_duration, "video_duration"),
            audio_stream_count=registration.audio_stream_count,
        )

    # ── 계획·추정·승인 — 호출 없이 굳는다 ────────────────────────

    def plan(
        self,
        registration: FinalVideoRegistration,
        run_id: str,
        *,
        settings: FinalCaptionSettings | None = None,
        workflow: RawWorkflowConfig | None = None,
        engine: Config | None = None,
    ) -> CaptionRunPlan:
        """실행 계획 — 이 시점에 지문·청크·비용이 전부 결정된다(§11.6·§14.3)."""
        return plan_caption_run(
            self.video_input(registration),
            run_id=run_id,
            settings=settings,
            workflow=workflow,
            engine=engine,
        )

    def estimate(
        self,
        registration: FinalVideoRegistration,
        run_id: str,
        *,
        settings: FinalCaptionSettings | None = None,
        workflow: RawWorkflowConfig | None = None,
        engine: Config | None = None,
    ) -> RunEstimate:
        """호출 없는 실행 추정 — 모델·수명주기·청크·비용을 사람에게 먼저 보여준다."""
        plan = self.plan(registration, run_id, settings=settings, workflow=workflow, engine=engine)
        audio = plan.video.audio
        return RunEstimate(
            identity=self.identity_of(plan),
            text_model=plan.workflow.models.text_model,
            timing_model=plan.workflow.models.timing_model,
            lifecycle=_lifecycle(),
            chunk_count=len(plan.spans),
            sample_count=audio.sample_count,
            sample_rate=audio.sample_rate,
            channels=audio.channels,
            origin=audio.pcm_sample0_file_time,
            extent_seconds=plan.extent_seconds,
            chunk_seconds=tuple(
                (Fraction(span.start, audio.sample_rate), Fraction(span.end, audio.sample_rate))
                for span in plan.spans
            ),
            estimated_cost_usd=str(plan.estimated_cost_usd),
            reserved_cost_usd=str(plan.reserved_cost_usd),
        )

    def approve(self, plan: CaptionRunPlan, *, note: str = "") -> ApprovalRecord:
        """계획에 정확히 묶인 승인을 저장한다 — 실행은 이 문을 먼저 지난다."""
        expected = expected_approval(plan)
        record = ApprovalRecord(
            identity=self.identity_of(plan),
            audio_stream_index=expected.audio_stream_index,
            sample_start=expected.sample_start,
            sample_end=expected.sample_end,
            text_model=expected.text_model,
            timing_model=expected.timing_model,
            settings_fingerprint=expected.settings_fingerprint,
            budget_usd=str(expected.budget_usd),
            note=note,
            created_at=_now(),
        )
        self._store.save_approval(record)
        return record

    def identity_of(self, plan: CaptionRunPlan) -> RunIdentity:
        return RunIdentity(
            final_video_revision=plan.video.final_video_revision,
            run_id=plan.run_id,
            fingerprint=plan.fingerprint,
        )

    # ── 실행 — 작업 본문·동기 경로 ──────────────────────────────

    def run_body(
        self,
        registration: FinalVideoRegistration,
        run_id: str,
        *,
        timing_provider: TimingProvider,
        text_provider: TextTranscriptProvider,
        settings: FinalCaptionSettings | None = None,
        workflow: RawWorkflowConfig | None = None,
        engine: Config | None = None,
        on: date | None = None,
        verify_enabled: bool = True,
    ) -> Callable[[JobContext], None]:
        """`JobManager`에 실을 실행 본문 — JobContext의 취소·진행을 그대로 쓴다."""
        workspace = self

        def body(context: JobContext) -> None:
            workspace._execute(
                registration,
                run_id,
                timing_provider,
                text_provider,
                settings=settings,
                workflow=workflow,
                engine=engine,
                on=on,
                verify_enabled=verify_enabled,
                gate=_job_gate(context),
                progress=_job_progress(context),
            )

        return body

    def run_now(
        self,
        registration: FinalVideoRegistration,
        run_id: str,
        *,
        timing_provider: TimingProvider,
        text_provider: TextTranscriptProvider,
        settings: FinalCaptionSettings | None = None,
        workflow: RawWorkflowConfig | None = None,
        engine: Config | None = None,
        on: date | None = None,
        gate: Callable[[], None] | None = None,
        verify_enabled: bool = True,
    ) -> CaptionRunRecord:
        """동기 실행 — 시험·공개 서비스가 작업 관리자 없이 도는 같은 길이다."""
        return self._execute(
            registration,
            run_id,
            timing_provider,
            text_provider,
            settings=settings,
            workflow=workflow,
            engine=engine,
            on=on,
            verify_enabled=verify_enabled,
            gate=gate,
            progress=None,
        )

    def run_record(self, identity: RunIdentity) -> CaptionRunRecord | None:
        return self._store.run(identity)

    def runs(self, final_video_revision: str) -> tuple[CaptionRunRecord, ...]:
        return self._store.runs(final_video_revision)

    def _execute(
        self,
        registration: FinalVideoRegistration,
        run_id: str,
        timing: TimingProvider,
        text: TextTranscriptProvider,
        *,
        settings: FinalCaptionSettings | None,
        workflow: RawWorkflowConfig | None,
        engine: Config | None,
        on: date | None,
        verify_enabled: bool,
        gate: Callable[[], None] | None,
        progress: Callable[[str, int], None] | None,
    ) -> CaptionRunRecord:
        plan = self.plan(registration, run_id, settings=settings, workflow=workflow, engine=engine)
        identity = self.identity_of(plan)
        self._store.ensure_run(identity)
        _report(progress, "total", 3)
        try:
            return self._run_core(identity, plan, timing, text, on, verify_enabled, gate, progress)
        except JobCancelledError:
            self._store.update_run(identity, status=RUN_CANCELLED)
            raise
        except Exception as error:  # noqa: BLE001 - 실행 행에 오류 본문을 남긴다
            self._store.update_run(identity, status=RUN_FAILED, error=str(error)[:400])
            raise

    def _run_core(
        self,
        identity: RunIdentity,
        plan: CaptionRunPlan,
        timing: TimingProvider,
        text: TextTranscriptProvider,
        on: date | None,
        verify_enabled: bool,
        gate: Callable[[], None] | None,
        progress: Callable[[str, int], None] | None,
    ) -> CaptionRunRecord:
        approval = self._stored_approval(identity)
        require_approval(plan, _engine_approval(approval))
        if review.find_snapshot(self._review, _lookup_identity(identity)) is not None:
            return self._store.update_run(identity, status=RUN_SUCCEEDED, error=None)
        _require_providers(timing, text)
        _report(progress, "current", 1)
        replays_only = _validate_original_providers(plan, timing, text, on or _today())
        if replays_only:
            _require_full_cache(self._store, identity, plan)
        pair = self._cached_providers(identity, timing, text, replays_only)
        outcome = run_final_captions(
            plan,
            on=on or _today(),
            timing_provider=pair.timing,
            text_provider=pair.text,
            workdir=self._chunk_workdir(identity),
            verify_enabled=verify_enabled,
            cancellation=gate,
            approval=_engine_approval(approval),
        )
        _report(progress, "current", 2)
        return self._settle(identity, outcome, plan, _run_origin(timing, text))

    def _stored_approval(self, identity: RunIdentity) -> ApprovalRecord:
        approval = self._store.approval(identity)
        if approval is None:
            raise ApprovalNotFoundError(
                f"실행 {identity.run_id!r}에 묶인 승인이 없다 — 추정을 보여주고 승인부터"
                " 만들어야 첫 호출이 나간다(§14.3)"
            )
        return approval

    def _cached_providers(
        self,
        identity: RunIdentity,
        timing: TimingProvider,
        text: TextTranscriptProvider,
        replays_only: bool,
    ) -> _ProviderPair:
        cache = {
            item.request_id: item
            for item in self._store.responses(identity.run_id, identity.fingerprint)
        }
        return _ProviderPair(
            timing=_wrap_timing(timing, cache, self._store, identity, replays_only),
            text=CachedTextProvider(text, cache, self._store, identity, replays_only=replays_only),
        )

    def _settle(
        self,
        identity: RunIdentity,
        outcome: FinalCaptionResult,
        plan: CaptionRunPlan,
        origin: str,
    ) -> CaptionRunRecord:
        """실행 결과를 정착시킨다 — 증거·초안·동결은 각자 불변 자리에.

        실패한 청크가 하나라도 있으면(`partial_failure`) 초안·증거는 보존하되
        실행 행은 실패로 남긴다 — 문구 없는 결과를 최신 반영 성공이라 부르는
        길이 없다(§11.3).
        """
        self._store.record_responses(_evidence_rows(identity, outcome, origin))
        if outcome.cancellation_reason:
            return self._store.update_run(
                identity, status=RUN_CANCELLED, cancellation_reason=outcome.cancellation_reason
            )
        publish_caption_artifacts(self._draft_dir(identity), outcome, plan)
        snapshot = review.freeze_automatic(self._review, outcome, plan)
        review.save_revision(self._review, review.initial_revision(snapshot))
        status = RUN_FAILED if outcome.status == STATUS_PARTIAL_FAILURE else RUN_SUCCEEDED
        error = (
            "partial_failure — 실패한 청크가 있는 초안이다(증거·초안은 보존했다)"
            if status == RUN_FAILED
            else None
        )
        return self._store.update_run(
            identity,
            status=status,
            error=error,
            draft_rel=self._draft_rel(identity),
            auto_content_sha256=snapshot.identity.auto_content_sha256,
        )

    # ── 검토 — 불러오기·편집 래퍼·확정 ──────────────────────────

    def review_bundle(self, identity: RunIdentity) -> ReviewBundle:
        frozen = review.find_snapshot(self._review, _lookup_identity(identity))
        if frozen is None:
            raise RunNotFoundError(
                f"동결된 자동 스냅샷이 없다: run {identity.run_id!r} — 검토는 실행 뒤에 열린다"
            )
        head = None
        stored = self._review.manual_head(
            final_video_revision=identity.final_video_revision,
            run_id=identity.run_id,
            fingerprint=identity.fingerprint,
            auto_content_sha256=frozen.identity.auto_content_sha256,
        )
        if stored is not None:
            head = parse_revision(stored[1])
        return ReviewBundle(snapshot=frozen, state=review.review_state(frozen, head), head=head)

    def _apply_head(
        self,
        identity: RunIdentity,
        operation: Callable[[ManualRevision], ManualRevision],
        expected_head: int,
    ) -> ManualRevision:
        """머리 리비전 위에 편집을 얹는다 — 호출자가 본 머리가 지금 머리여야 한다.

        기대 머리 번호가 저장된 머리와 다르면 남의 편집 위에 덮는 길이다 —
        `ManualHeadConflict`로 닫는다(갱신 손실 방지, §11.7).
        """
        bundle = self.review_bundle(identity)
        head = bundle.head
        if head is None:
            head = review.save_revision(self._review, review.initial_revision(bundle.snapshot))
        if head.sequence != expected_head:
            raise ManualHeadConflict(
                f"기대한 머리 리비전 {expected_head}이(가) 지금 머리 {head.sequence}와"
                " 다르다 — 앞선 편집 위에 덮지 못한다. 다시 불러서 다시 편집해야 한다"
            )
        return review.save_revision(self._review, operation(head))

    def edit_cue_text(
        self, identity: RunIdentity, cue_id: str, text: str, *, expected_head: int
    ) -> ManualRevision:
        return self._apply_head(identity, lambda head: edit_text(head, cue_id, text), expected_head)

    def edit_cue_time(
        self,
        identity: RunIdentity,
        cue_id: str,
        *,
        start: Fraction | int | None = None,
        end: Fraction | int | None = None,
        expected_head: int,
    ) -> ManualRevision:
        return self._apply_head(
            identity, lambda head: edit_time(head, cue_id, start=start, end=end), expected_head
        )

    def split_cue(
        self,
        identity: RunIdentity,
        cue_id: str,
        at: Fraction | int,
        left_text: str,
        right_text: str,
        *,
        expected_head: int,
    ) -> ManualRevision:
        return self._apply_head(
            identity,
            lambda head: split_cue(head, cue_id, at, left_text, right_text),
            expected_head,
        )

    def merge_cues(
        self,
        identity: RunIdentity,
        left_id: str,
        right_id: str,
        text: str | None = None,
        *,
        expected_head: int,
    ) -> ManualRevision:
        return self._apply_head(
            identity, lambda head: merge_cues(head, left_id, right_id, text), expected_head
        )

    def add_manual_cue(
        self,
        identity: RunIdentity,
        start: Fraction | int,
        end: Fraction | int,
        text: str,
        *,
        note: str = "",
        expected_head: int,
    ) -> ManualRevision:
        return self._apply_head(
            identity, lambda head: add_cue(head, start, end, text, note=note), expected_head
        )

    def allow_warning(
        self, identity: RunIdentity, key: str, reason: str, *, expected_head: int
    ) -> ManualRevision:
        return self._apply_head(
            identity, lambda head: allow_finding(head, key, reason), expected_head
        )

    def resolve_unplaced_item(
        self,
        identity: RunIdentity,
        index: int,
        text: str,
        *,
        placed_cue_id: str | None = None,
        dismissed_reason: str | None = None,
        expected_head: int,
    ) -> ManualRevision:
        return self._apply_head(
            identity,
            lambda head: resolve_unplaced(
                head,
                index,
                text,
                placed_cue_id=placed_cue_id,
                dismissed_reason=dismissed_reason,
            ),
            expected_head,
        )

    def record_no_speech(
        self, identity: RunIdentity, *, note: str = "", expected_head: int
    ) -> ManualRevision:
        return self._apply_head(
            identity, lambda head: record_no_speech(head, note=note), expected_head
        )

    def confirm(
        self,
        identity: RunIdentity,
        *,
        expected_head: int,
        cue_ids: tuple[str, ...],
        note: str = "",
        language: str = DEFAULT_DRAFT_LANGUAGE,
    ) -> ConfirmedExport:
        """기대 머리를 검토자가 짚은 큐 ID 전부와 함께 확정 게시한다(§11.7·§11.11).

        `cue_ids`는 검토자가 **명시적으로** 넘긴 확인 목록이다 — 게이트가 머리의
        큐 전부와 정확히 같은지 다시 잰다(빠짐·중복·낡은 ID는 거부). 기대 머리가
        어긋나면 확정 자체를 시작하지 않는다.
        """
        bundle = self.review_bundle(identity)
        if bundle.head is None:
            raise CaptionWorkspaceError("확정할 머리 리비전이 없다 — 실행·동결이 먼저다")
        if bundle.head.sequence != expected_head:
            raise ManualHeadConflict(
                f"기대한 머리 리비전 {expected_head}이(가) 지금 머리"
                f" {bundle.head.sequence}와 다르다 — 다시 불러서 다시 확인해야 한다"
            )
        registration = self._registration_for(bundle.snapshot)
        export = review.confirm_final(
            self._review,
            self.video_input(registration),
            bundle.head,
            review.ReviewConfirmation(cue_ids=cue_ids, note=note),
            language=language,
        )
        run = self._store.run(identity)
        if run is None:
            raise RunNotFoundError(f"실행 기록이 없다: run {identity.run_id!r}")
        return self._export_bundle(identity, registration, run, export, language)

    def _export_bundle(
        self,
        identity: RunIdentity,
        registration: FinalVideoRegistration,
        run: CaptionRunRecord,
        export: review.FinalExport,
        language: str,
    ) -> ConfirmedExport:
        """확정 결과 꾸러미 — 공개 서비스가 받는 경로들을 한데 묶는다."""
        draft_dir = self._draft_dir(identity)
        return ConfirmedExport(
            registration=registration,
            run=run,
            export=export,
            draft_dir=draft_dir,
            draft_srt=draft_dir / f"draft.{language}.srt",
            draft_vtt=draft_dir / f"draft.{language}.vtt",
        )

    def _registration_for(self, snapshot: review.AutomaticSnapshot) -> FinalVideoRegistration:
        """동결 스냅샷이 들은 선택 오디오의 등록 행 — 스냅샷이 결정한다(§11.1)."""
        for registration in self._store.registrations():
            if (
                registration.ready
                and registration.final_video_revision == snapshot.identity.final_video_revision
                and registration.pcm_sha256 == snapshot.pcm_sha256
                and registration.origin == snapshot.origin
                and registration.audio_stream_index == snapshot.audio_stream_index
            ):
                return registration
        raise RegistrationNotFoundError(
            "동결 스냅샷의 선택 오디오와 같은 등록이 없다 — 원점·스트림까지 같아야 한다"
        )

    # ── 파형 — 로컬 PCM의 진폭 빈 ──────────────────────────────

    def waveform(
        self, registration: FinalVideoRegistration, *, bin_seconds: float = 0.05
    ) -> WaveformBins:
        """등록 PCM에서 피크 빈을 잰다 — 진폭만 있고 발화·시각은 만들지 않는다."""
        if not registration.ready:
            raise RegistrationNotReadyError(
                f"등록이 {registration.status!r}다 — PCM이 없으면 파형도 없다"
            )
        rate = _required_int(registration.sample_rate, "sample_rate")
        channels = _required_int(registration.channels, "channels")
        sample_count = _required_int(registration.sample_count, "sample_count")
        bin_frames = max(1, round(bin_seconds * rate))
        return WaveformBins(
            pcm_sha256=_required_text(registration.pcm_sha256, "pcm_sha256"),
            sample_rate=rate,
            channels=channels,
            sample_count=sample_count,
            bin_frames=bin_frames,
            bin_duration=Fraction(bin_frames, rate),
            peaks=_peak_bins(self._abs(registration.pcm_rel), sample_count, channels, bin_frames),
            peak_scale=_WAVEFORM_PEAK_SCALE,
        )

    # ── 자리 ────────────────────────────────────────────────────

    def _rel(self, path: Path) -> str:
        return path.resolve().relative_to(self._workspace.root).as_posix()

    def _abs(self, relative: str | None) -> Path:
        if relative is None:
            raise CaptionWorkspaceError("등록 행에 경로가 없다 — 준비되지 않은 상태다")
        return self._workspace.root / relative

    def _key(self, identity: RunIdentity) -> str:
        return self._review.store_key(
            identity.final_video_revision, identity.run_id, identity.fingerprint
        )

    def _draft_rel(self, identity: RunIdentity) -> str:
        return f"{CAPTION_DRAFT_REL}/{self._key(identity)}"

    def _draft_dir(self, identity: RunIdentity) -> Path:
        draft = self._workspace.root / self._draft_rel(identity)
        draft.mkdir(parents=True, exist_ok=True)
        return draft

    def _chunk_workdir(self, identity: RunIdentity) -> Path:
        work = self._workspace.root / CAPTION_CACHE_REL / self._key(identity)
        work.mkdir(parents=True, exist_ok=True)
        return work


def _job_gate(context: JobContext) -> Callable[[], None]:
    """엔진 취소 게이트 — 작업 취소를 유효 결과 보존 취소로 옮긴다(§14.1)."""

    def gate() -> None:
        try:
            context.check_cancel()
        except JobCancelledError:
            raise CaptionRunCancelled("작업 취소 요청 — 끝난 유효 결과는 보존한다") from None

    return gate


def _job_progress(context: JobContext) -> Callable[[str, int], None]:
    def report(kind: str, value: int) -> None:
        if kind == "total":
            context.set_total(value)
        else:
            context.set_progress(value)

    return report


def _report(progress: Callable[[str, int], None] | None, kind: str, value: int) -> None:
    if progress is not None:
        progress(kind, value)


def _require_providers(timing: object, text: object) -> None:
    """제공자가 없는 실행은 0 호출로 닫는다 — 승인의 부재가 성공이 되는 금지."""
    if timing is None or text is None:
        raise CaptionProviderMissing(
            "시간·문구 제공자가 모두 주입되어야 한다 — 없는 제공자로는 호출도 계획도 없다"
        )


def _validate_original_providers(
    plan: CaptionRunPlan,
    timing: TimingProvider,
    text: TextTranscriptProvider,
    on: date,
) -> bool:
    """포장 **전에** 원본 제공자를 오프라인 문에 세운다(§14.3, 루트 재검).

    엔진의 실전송 게이트는 받은 제공자의 겉모습만 본다 — 캐시 포장이 실전송
    어댑터를 숨기면 유효한 승인으로도 닫힌 설정을 여는 길이 생긴다. 그래서
    작업공간은 원본에 같은 문을 먼저 세운다. 환경·등록표 검사는 받아들여진
    함수를 그대로 부른다 — 판정을 다시 쓰지 않는다.

    반환값이 참이면 **재생 전용**이다: 새 전송은 못 나가고 저장된 응답의
    재생으로만 실행이 이어진다.
    """
    live = isinstance(timing, TransportTimingProvider) or isinstance(text, TransportTextProvider)
    if not live:
        return False
    if plan.workflow.network_mode != "online":
        # 설정이 승인보다 앞선다 — 유효한 승인도 오프라인 설정을 못 연다(§14.3).
        return True
    if env.is_offline():
        raise FreshSendBlockedError(
            "VLOGSRT_OFFLINE이 켜져 있다 — 실전송 제공자의 새 호출은 못 나간다(§14.3)"
        )
    capabilities.preflight_online_transcription(
        plan.workflow, on, roles=(capabilities.ROLE_TEXT, capabilities.ROLE_TIMING)
    )
    return False


def _require_full_cache(
    store: CaptionWorkspaceStore, identity: RunIdentity, plan: CaptionRunPlan
) -> None:
    """재생 전용 실행의 전제 — 확정적 요청 ID 전부가 캐시에 있어야 한다.

    청크의 시간·문구 요청 ID는 계획에서 정해진다. 하나라도 없으면 새 전송이
    필요하다는 뜻이므로 오프라인 문이 닫힌 지금은 0 호출로 거부한다. 재전사·
    심판 ID는 여기서 모른다 — 실행 중 재생이 빗나가면 그 실행은 실패로 남는다
    (빈 성공이 아니다).
    """
    cached = {row.request_id: row for row in store.responses(identity.run_id, identity.fingerprint)}
    missing = [
        request_id
        for index, span in enumerate(plan.spans)
        for request_id in (
            f"{identity.run_id}:timing:{index:04d}",
            f"{identity.run_id}:text:{index:04d}",
        )
        if request_id not in cached
        or cached[request_id].sample_start != span.start
        or cached[request_id].sample_end != span.end
    ]
    if missing:
        raise FreshSendBlockedError(
            f"오프라인 문이 닫혀 새 전송을 못 하는데 캐시가 부족하다 — {len(missing)}개"
            f" 요청({missing[0]}…)이 저장되지 않았다. 저장된 응답으로는 이 실행을"
            " 이어갈 수 없다(§14.3)"
        )


def _wrap_timing(
    inner: TimingProvider,
    cache: dict[str, CachedResponse],
    store: CaptionWorkspaceStore,
    identity: RunIdentity,
    replays_only: bool,
) -> CachedTimingProvider:
    """원본의 온도 손잡이 보유를 포장 뒤에도 드러낸다 — 엔진 재전사 판정이 참으로 남는다."""
    tempered = "temperature" in inspect.signature(inner.timed_transcript).parameters
    maker = TemperedCachedTimingProvider if tempered else CachedTimingProvider
    return maker(inner, cache, store, identity, replays_only=replays_only)


def _run_origin(timing: TimingProvider, text: TextTranscriptProvider) -> str:
    """이 실행의 증거 출처 — 실전송이 하나라도 끼어 있으면 실전송 실행이다."""
    live = isinstance(timing, TransportTimingProvider) or isinstance(text, TransportTextProvider)
    return RESPONSE_ORIGIN_LIVE if live else RESPONSE_ORIGIN_INJECTED


def _engine_approval(record: ApprovalRecord) -> CaptionApproval:
    return CaptionApproval(
        final_video_sha256=record.identity.final_video_revision,
        audio_stream_index=record.audio_stream_index,
        sample_start=record.sample_start,
        sample_end=record.sample_end,
        text_model=record.text_model,
        timing_model=record.timing_model,
        settings_fingerprint=record.settings_fingerprint,
        budget_usd=Decimal(record.budget_usd),
    )


def _lookup_identity(identity: RunIdentity) -> AutoIdentity:
    """조회용 신원 — 내용 해시는 조회에 쓰이지 않는다(찾은 뒤 스냅샷이 정본)."""
    return AutoIdentity(
        final_video_revision=identity.final_video_revision,
        run_id=identity.run_id,
        fingerprint=identity.fingerprint,
        auto_content_sha256="",
    )


def _evidence_rows(
    identity: RunIdentity, outcome: FinalCaptionResult, origin: str
) -> list[CachedResponse]:
    return [
        CachedResponse(
            run_id=identity.run_id,
            fingerprint=identity.fingerprint,
            request_id=evidence.request_id,
            role=evidence.role,
            chunk_index=evidence.chunk_index,
            audio_sha256=evidence.audio_sha256,
            sample_start=evidence.sample_start,
            sample_end=evidence.sample_end,
            sample_rate=evidence.sample_rate,
            payload=dict(evidence.payload),
            created_at=_now(),
            origin=origin,
        )
        for evidence in outcome.evidence
    ]


def _lifecycle() -> tuple[ModelLifecycle, ...]:
    """등록표의 수명주기 요약 — 판정은 하지 않고 확인된 값만 싣는다(§11.9)."""
    return tuple(
        ModelLifecycle(
            role=snapshot.role,
            model_id=snapshot.model_id,
            registry_revision=snapshot.registry_revision,
            response_format=snapshot.requested_response_format,
            deprecation_notice_date=_date_text(snapshot.deprecation_notice_date),
            shutdown_date=_date_text(snapshot.shutdown_date),
        )
        for snapshot in capabilities.confirmed_capability_snapshots()
    )


def _date_text(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _required_int(value: int | None, field: str) -> int:
    if value is None:
        raise CaptionWorkspaceError(f"준비된 등록에 {field}가 없다 — 손상된 행이다")
    return value


def _required_text(value: str | None, field: str) -> str:
    if value is None:
        raise CaptionWorkspaceError(f"준비된 등록에 {field}가 없다 — 손상된 행이다")
    return value


def _required_float(value: float | None, field: str) -> float:
    if value is None:
        raise CaptionWorkspaceError(f"준비된 등록에 {field}가 없다 — 손상된 행이다")
    return value


def _peak_bins(pcm: Path, sample_count: int, channels: int, bin_frames: int) -> tuple[int, ...]:
    """PCM을 빈마다 읽어 최대 진폭을 남긴다 — 샘플 좌표의 등분할일 뿐이다."""
    frame_bytes = channels * 2
    peaks: list[int] = []
    with pcm.open("rb") as handle:
        remaining = sample_count
        while remaining > 0:
            frames = min(bin_frames, remaining)
            block = handle.read(frames * frame_bytes)
            if len(block) < frames * frame_bytes:
                missing = frames - len(block) // frame_bytes
                raise CaptionWorkspaceError(
                    f"PCM이 등록보다 짧다: {pcm} — {remaining}프레임 중 {missing}개가 없다"
                )
            samples = array.array("h")
            samples.frombytes(block)
            if sys.byteorder == "big":
                samples.byteswap()
            peaks.append(max((abs(sample) for sample in samples), default=0))
            remaining -= frames
    return tuple(peaks)


def _same_registration_or_conflict(
    existing: FinalVideoRegistration, given: FinalVideoRegistration
) -> FinalVideoRegistration:
    """같은 (렌더, 오디오 선택)의 재등록 — 값이 같으면 옛 행을, 다르면 거부다."""
    stable = (
        "source_id",
        "final_video_revision",
        "status",
        "audio_ordinal",
        "audio_stream_count",
        "audio_stream_index",
        "pcm_sha256",
        "sample_rate",
        "channels",
        "sample_count",
        "origin",
        "video_duration",
        "verified_copy_rel",
        "pcm_rel",
        "manifest_rel",
    )
    changed = [name for name in stable if getattr(existing, name) != getattr(given, name)]
    if changed:
        raise CaptionWorkspaceError(
            f"같은 렌더·오디오 선택의 재등록이 기록과 다르다: {changed} —"
            " 바꿔치기가 아니라 새 등록이어야 한다"
        )
    return existing
