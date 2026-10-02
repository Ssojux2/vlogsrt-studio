"""A7 작업공간 백엔드의 순수 값형 — 등록·추정·승인·실행·캐시·파형(계획 §16 A7).

`caption_workspace.py`가 오케스트레이션을, `caption_workspace_store.py`가 SQLite
선로를 소유한다. 이 모듈은 그 둘이 주고받는 **값**만 정의한다 — I/O가 없고,
임계값·모델 문자열을 새로 만들지 않는다(모델은 `RawModelRoles`, 한도는
`FinalCaptionSettings`가 정본이다).

상태의 정직함이 이 계층의 첫 계약이다: 오디오 없는 최종 영상은 `no_audio`
**상태**로 남고(무발화가 아니다, §11.11), 오디오가 여럿이면 선택 확정 전까지
`audio_selection_required`다. 어느 쪽도 빈 성공으로 바꾸지 않는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vlogsrt.project.raw.caption_review import FinalExport

REGISTRATION_READY = "ready"
"""선택 오디오까지 잰 등록 — 실행·추정·확정이 열려 있다."""

REGISTRATION_NO_AUDIO = "no_audio"
"""오디오 스트림이 아예 없는 최종 영상 — 무발화와 다른 별개 상태다(§11.11)."""

REGISTRATION_AUDIO_SELECTION_REQUIRED = "audio_selection_required"
"""오디오 트랙이 여럿인데 어느 트랙/믹스인지 확정하지 않았다(§11.1)."""

RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_CANCELLED = "cancelled"
RUN_FAILED = "failed"


class CaptionWorkspaceError(RuntimeError):
    """작업공간 백엔드의 오류 — 모든 갈래의 부모."""


class RegistrationNotFoundError(CaptionWorkspaceError):
    """그 렌더·오디오 선택의 등록이 없다."""


class RegistrationNotReadyError(CaptionWorkspaceError):
    """등록이 준비되지 않은 상태다 — no_audio·선택 미확정은 실행·추정·확정이 못 연다."""


class RunNotFoundError(CaptionWorkspaceError):
    """그 신원의 실행 기록이 없다."""


class ApprovalNotFoundError(CaptionWorkspaceError):
    """그 실행에 묶인 승인이 없다 — 지문 결속 승인이 실행의 전제다."""


class ManualHeadConflict(CaptionWorkspaceError):
    """기대한 머리 리비전이 지금 머리가 아니다 — 앞선 편집 위에 덮지 못한다(§11.7).

    편집·확정 호출자는 자기가 본 머리 번호를 명시해야 한다. 어긋나면 갱신 손실의
    모양이므로 조용히 병합하지 않고 다시 불러서 다시 편집하게 한다.
    """


@dataclass(frozen=True, slots=True)
class RunIdentity:
    """실행의 신원 셋 — 등록 해시·run_id·설정 지문(§11.1)."""

    final_video_revision: str
    run_id: str
    fingerprint: str

    def triple(self) -> tuple[str, str, str]:
        return (self.final_video_revision, self.run_id, self.fingerprint)


@dataclass(frozen=True, slots=True)
class FinalVideoRegistration:
    """최종 영상 등록 행 — 재시작 뒤에도 같은 신원으로 되살아난다.

    `external_source`는 출처 표기일 뿐 재적재의 원천이 아니다 — 실행은 언제나
    작업공간 안의 검증 사본(`verified_copy_rel`)과 잰 PCM(`pcm_rel`)으로 돈다.
    `no_audio`·`audio_selection_required` 상태에서는 오디오 칸들이 None이다.
    """

    registration_id: str
    source_id: str
    final_video_revision: str
    status: str
    audio_ordinal: int | None
    audio_stream_count: int
    external_source: str
    audio_stream_index: int | None
    pcm_sha256: str | None
    sample_rate: int | None
    channels: int | None
    sample_count: int | None
    origin: Fraction | None
    video_duration: float | None
    verified_copy_rel: str | None
    pcm_rel: str | None
    manifest_rel: str | None
    created_at: str

    @property
    def ready(self) -> bool:
        return self.status == REGISTRATION_READY


@dataclass(frozen=True, slots=True)
class ModelLifecycle:
    """역할 하나의 수명주기 요약 — 등록표에서 읽은 값만 담는다(§11.9).

    종료 예정·폐지 예고는 등록표의 확인된 값이다. 여기서 판정하지 않는다 —
    온라인 문은 엔진·등록표의 `preflight_online_transcription`이 닫는다.
    """

    role: str
    model_id: str
    registry_revision: str
    response_format: str
    deprecation_notice_date: str | None
    shutdown_date: str | None


@dataclass(frozen=True, slots=True)
class RunEstimate:
    """호출 없이 잰 실행 추정 — 모델·수명주기·청크·비용(§11.8·§14.3).

    비용은 `Decimal` 문자열로 실는다(왕복이 손실 없어야 한다). 청크 시각은
    정규화 시간축의 정확한 유리수다 — 원점 환산은 실행·검토 계층의 산술이다.
    """

    identity: RunIdentity
    text_model: str
    timing_model: str
    lifecycle: tuple[ModelLifecycle, ...]
    chunk_count: int
    sample_count: int
    sample_rate: int
    channels: int
    origin: Fraction
    extent_seconds: Fraction
    chunk_seconds: tuple[tuple[Fraction, Fraction], ...]
    estimated_cost_usd: str
    reserved_cost_usd: str


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    """지문에 묶인 명시적 승인의 저장 행 — 승인 값은 계획과 정확히 같아야 한다."""

    identity: RunIdentity
    audio_stream_index: int
    sample_start: int
    sample_end: int
    text_model: str
    timing_model: str
    settings_fingerprint: str
    budget_usd: str
    note: str
    created_at: str


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """응답 캐시 한 행 — 재개가 같은 청크를 다시 묻지 않게 하는 증거(§14.2).

    재생 조건은 신원이 전부 같아야 한다: run·지문·요청 ID·오디오 해시·샘플
    경계·주파수. 하나라도 다르면 캐시는 못 쓴다 — 낡은 응답을 새 오디오에
    붙이는 길이 여기서 닫힌다.
    """

    run_id: str
    fingerprint: str
    request_id: str
    role: str
    chunk_index: int
    audio_sha256: str
    sample_start: int
    sample_end: int
    sample_rate: int
    payload: Mapping[str, object]
    created_at: str


@dataclass(frozen=True, slots=True)
class CaptionRunRecord:
    """실행 하나의 상태 행 — 증거 자체는 파일·캐시 표에 있다."""

    identity: RunIdentity
    status: str
    cancellation_reason: str
    error: str | None
    draft_rel: str | None
    auto_content_sha256: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class WaveformBins:
    """로컬 PCM에서 잰 파형 피크 빈 — 진폭만 있고 시각·발화는 없다(§11.7).

    빈은 샘플 좌표의 등분할이다. 말이 있는지·언제인지는 여기서 만들지 않는다 —
    그 판정은 모델 응답의 몫이고, 파형은 사람이 눈으로 보는 근거일 뿐이다.
    """

    pcm_sha256: str
    sample_rate: int
    channels: int
    sample_count: int
    bin_frames: int
    bin_duration: Fraction
    peaks: tuple[int, ...]
    peak_scale: int


@dataclass(frozen=True, slots=True)
class ConfirmedExport:
    """확정 결과 꾸러미 — 공개 서비스가 받아 미디어로 여는 값들.

    브라우저 제공은 이 계층이 하지 않는다: final SRT/VTT·manifest는 검토 저장소의
    불변 자리에 있고, 서비스가 `final/` 이름공간의 검증 사본과 함께 등록해
    연다(등록은 공개 서비스의 권한이다).
    """

    registration: FinalVideoRegistration
    run: CaptionRunRecord
    export: FinalExport
    draft_dir: Path
    draft_srt: Path
    draft_vtt: Path
