"""A4-F1 2단계 실제 호출 승인 — 형식이 정해진 기록과 주입 경계(계획 §14.3).

실제 호출은 두 개의 사람 승인으로만 열린다:

- **1단계(내부 ASR)** 승인은 전사가 일어나기 **전에** 알 수 있는 값만 묶는다 —
  원본 내용 해시·오디오 스트림·청크 예산·설정 지문·시간 모델·승인 날짜. 전사
  **뒤에야** 알 수 있는 값(실측 샘플 수·발화·창·실측 지문)은 2단계 승인의 몫이다.
- **2단계(의미 Luna)** 승인은 1단계가 남긴 불변 전사 기록의 **실측 지문**을 묶는다
  — 오디오 원본은 라이브러리 `OnlineAnalysisApproval`(호환 보존), 무성 원본은
  라이브러리의 시각(visual) overloads(`understanding_luna`).

이 모듈은 1단계 승인 레코드와 그 검사, 그리고 UI가 실제 호출 재료를 **사람의
명시적 주입**으로만 받는 전송 경계(`LiveAnalysisTransport`)를 소유한다. 어느
승인도 환경(`VLOGSRT_OFFLINE`)·작업공간 `network_mode`보다 강하지 않다 — 둘 중
하나라도 닫혀 있으면 승인이 있어도 0송신이다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from vlogsrt.project.raw.analysis_config import (
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
    validate_understanding_settings,
)
from vlogsrt.project.raw.config import RawWorkflowConfig, validate_raw_workflow_config
from vlogsrt.project.raw.understanding_luna import AnalysisApprovalError, online_env_blocked

ASR_APPROVAL_SCHEMA = "raw-analysis-asr-approval/v1"
"""1단계 승인이 묶는 값의 schema — 설정 지문이 이 값으로 갈라진다."""


@dataclass(frozen=True, slots=True)
class AsrPhaseApproval:
    """1단계(내부 ASR) 전용 사람 승인 — 전사 전에 알 수 있는 값만 묶는다(§14.3).

    `chunk_budget`은 예약 상한이다: 실행 청크 수가 이 값을 넘으면 전송 **전에**
    닫힌다. 실측 샘플 수·원점은 1단계 기록에 남고 2단계 지문이 묶는다 — 이 승인이
    미리 알 수 없는 값을 묶는 척하지 않는다. `approved_on`은 승인이 만들어진
    서버 날짜로, 시간 제공자 종료 예정일 검사(`capabilities`)가 이 날짜를 본다.
    """

    approved: bool
    source_sha256: str
    audio_stream_index: int
    chunk_budget: int
    settings_fingerprint: str
    timing_model: str
    approved_on: str

    def approved_on_date(self) -> date:
        """승인 날짜의 date — 모양이 어긋나면 승인 자체가 계약 위반이다."""
        try:
            return date.fromisoformat(self.approved_on)
        except ValueError as error:
            raise AnalysisApprovalError(
                f"1단계 승인의 승인 날짜가 ISO 날짜가 아니다: {self.approved_on!r}"
            ) from error


def asr_settings_fingerprint(
    settings: RawUnderstandingSettings, workflow: RawWorkflowConfig
) -> str:
    """1단계 승인이 묶는 설정 지문 — 결과에 영향을 주는 설정·모델만(§14.2).

    원본 해시·실측 격자는 승인 레코드의 다른 칸이 묶으므로 여기 없다. 같은 설정·
    같은 모델이면 같은 값이 나온다 — 승인은 이 값으로 설정 드리프트를 잡는다.
    """
    validate_understanding_settings(settings)
    validate_raw_workflow_config(workflow)
    parts = [
        ASR_APPROVAL_SCHEMA,
        UNDERSTANDING_SCHEMA_VERSION,
        str(settings.window_seconds),
        str(settings.context_seconds),
        str(settings.max_images_per_window),
        str(settings.image_long_edge),
        str(settings.prompt_revision),
        str(settings.asr_chunk_max_seconds),
        str(settings.asr_chunk_max_bytes),
        workflow.models.timing_model,
        workflow.models.luna_model,
    ]
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def require_live_asr_approval(
    approval: AsrPhaseApproval,
    *,
    workflow: RawWorkflowConfig,
    understanding: RawUnderstandingSettings,
    source_sha256: str,
    on: date,
) -> None:
    """1단계 승인의 문 — 환경·설정·출처·모델·설정 지문·수명주기를 본다.

    이 문을 지난 뒤에도 전송 순간마다 `ApprovedPhaseAsrProvider`가 같은 검사를
    다시 한다(§14.3). 여기는 빠른 실패 — 유료 호출이 시작되기 전에 닫는다.
    """
    if not isinstance(approval, AsrPhaseApproval):
        raise AnalysisApprovalError(
            f"1단계 승인 레코드가 AsrPhaseApproval이 아니다: {type(approval).__name__}"
        )
    if not approval.approved:
        raise AnalysisApprovalError("승인되지 않은 1단계 실행이다")
    blocked = online_env_blocked()
    if blocked is not None:
        raise AnalysisApprovalError(blocked)
    if workflow.network_mode != "online":
        raise AnalysisApprovalError(
            f"네트워크 모드가 {workflow.network_mode!r}다 — 실제 호출 문이 닫혀 있다"
        )
    if approval.source_sha256 != source_sha256:
        raise AnalysisApprovalError("승인이 본 원본과 지금의 원본이 다르다")
    if approval.timing_model != workflow.models.timing_model:
        raise AnalysisApprovalError("승인이 본 ASR 모델과 설정 모델이 다르다")
    expected = asr_settings_fingerprint(understanding, workflow)
    if approval.settings_fingerprint != expected:
        raise AnalysisApprovalError(
            "승인이 본 설정 지문과 지금의 설정 지문이 다르다 — 새 추정과 새 승인을 받아라"
        )
    if isinstance(approval.chunk_budget, bool) or approval.chunk_budget < 1:
        raise AnalysisApprovalError(f"1단계 청크 예산은 1 이상 정수다: {approval.chunk_budget!r}")
    from vlogsrt.project.raw.capabilities import (
        ROLE_TIMING,
        preflight_online_transcription,
    )

    try:
        preflight_online_transcription(workflow, on, roles=(ROLE_TIMING,))
    except Exception as error:  # noqa: BLE001 - 수명주기 실패도 승인 문의 오류로 닫는다
        raise AnalysisApprovalError(
            f"1단계 실제 호출의 수명주기 문이 닫혔다 — 신규 온라인 전사는 시작하지 않는다: {error}"
        ) from error


@dataclass(frozen=True, slots=True)
class LiveAnalysisTransport:
    """실제 호출 재료의 주입 경계 — UI는 자격증명을 스스로 찾지 않는다.

    사람이 서버를 띄울 때 명시적으로 주입하는 재료만 담는다: 전사 전송 경계
    (sender·cfg), Luna 클라이언트, 그리고 승인 공급자. 승인 공급자는 (작업공간,
    선택)을 받아 원본별 승인을 돌려주는 호출 가능체다 — 화면이 승인을 만들지
    않는다. 주입이 없으면 화면의 실제 호출 단추는 정직한 오류로 끝난다(§14.3).
    """

    sender: object
    cfg: object
    client: object
    asr_approvals: Callable[[Path, Sequence[str] | None], Mapping[str, AsrPhaseApproval]]
    luna_approvals: Callable[[Path, str, Sequence[str]], Mapping[str, object]]
    on: date | None = None


__all__ = [
    "ASR_APPROVAL_SCHEMA",
    "AsrPhaseApproval",
    "LiveAnalysisTransport",
    "asr_settings_fingerprint",
    "require_live_asr_approval",
]
