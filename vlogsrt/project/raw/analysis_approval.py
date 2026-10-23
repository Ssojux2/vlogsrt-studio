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
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from vlogsrt.env import ANALYSIS_CLAUDE_KEY_ENV
from vlogsrt.project.raw.analysis_config import (
    TRANSCRIPTION_IDENTITY_PROMPT_REVISION,
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
    validate_understanding_settings,
)
from vlogsrt.project.raw.config import (
    ANALYSIS_PROVIDER_ANTHROPIC,
    CLAUDE_ANALYSIS_PROFILES,
    RawWorkflowConfig,
    analysis_model_label,
    analysis_provider,
    transcription_identity_view,
    validate_raw_workflow_config,
)
from vlogsrt.project.raw.understanding_luna import AnalysisApprovalError, online_env_blocked

ASR_APPROVAL_SCHEMA = "raw-analysis-asr-approval/v2"
"""1단계 승인 기록의 schema — v2(D59 19d)는 「다른 귀로 교차 확인」의 글자 요청 세 칸을 더했다."""

ASR_SETTINGS_FINGERPRINT_SCHEMA = "raw-analysis-asr-approval/v1"
"""1단계 승인이 묶는 **설정 지문**의 schema — 기록이 v2가 돼도 지문 바이트는 v1 그대로다.

설정 지문은 whisper 전사의 설정만 묶는다. 글자 요청(19d)은 승인의 다른 칸이 따로 묶으므로 이 값을
올리면 이미 만든 승인·기존 시험의 지문만 흔들린다 — 그래서 고정한다."""


@dataclass(frozen=True, slots=True)
class AsrPhaseApproval:
    """1단계(내부 ASR) 전용 사람 승인 — 전사 전에 알 수 있는 값만 묶는다(§14.3).

    `chunk_budget`은 예약 상한이다: 실행 청크 수가 이 값을 넘으면 전송 **전에**
    닫힌다. 실측 샘플 수·원점은 1단계 기록에 남고 2단계 지문이 묶는다 — 이 승인이
    미리 알 수 없는 값을 묶는 척하지 않는다. `approved_on`은 승인이 만들어진
    서버 날짜로, 시간 제공자 종료 예정일 검사(`capabilities`)가 이 날짜를 본다.

    v2(D59 19d)의 글자 칸 셋은 「다른 귀로 교차 확인」이 켜져 있고 견적이 글자 분량을 보였을 때만
    채워진다. 비어 있으면(기본) 글자 요청은 한 번도 나가지 않는다 — 캐시에 있는 답만 쓴다.
    """

    approved: bool
    source_sha256: str
    audio_stream_index: int
    chunk_budget: int
    settings_fingerprint: str
    timing_model: str
    approved_on: str
    text_model: str | None = None
    """승인이 묶은 글자(두 번째 귀) 모델 — None이면 글자 요청을 보내지 않는다."""
    text_identity: str | None = None
    """글자 요청의 신원(`source_text.text_identity` — 문맥·언어·용어집 sha). 보낼 때 다시 잰다."""
    text_chunk_budget: int = 0
    """글자 요청 상한 — 견적이 센 「아직 답이 없는 청크」 수. whisper 예산과 따로 센다."""
    text_chunk_keys: tuple[str, ...] | None = None
    """견적이 센 바로 그 청크들의 글자 캐시 열쇠 — 있으면 이 청크만 보낸다(더 긴 다른 청크로
    상한을 채우지 못한다). None은 1단계 기록이 없던 원본 — 견적이 그 오디오의 청크 전부를 셌다."""
    rescue_chunk_budget: int = 0
    """「다른 귀 기준 재전사」(W-29b, D78)의 whisper 다시 묻기 상한 — 0이면 다시 묻지 않는다. 1차
    전사 예산(`chunk_budget`)과 따로 센다."""

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
    의미 분석 모델은 전사 전용 보기(`transcription_identity_view`)로 고정한다(D54) —
    전사는 그 모델을 듣지 않고, 기본 설정의 지문은 W-14 이전과 같다. 안내문 리비전도 같은
    이유로 고정값이다(D58 18b) — 안내문을 고쳐도 전사 승인은 그대로다.
    """
    validate_understanding_settings(settings)
    validate_raw_workflow_config(workflow)
    workflow = transcription_identity_view(workflow)
    parts = [
        ASR_SETTINGS_FINGERPRINT_SCHEMA,
        UNDERSTANDING_SCHEMA_VERSION,
        str(settings.window_seconds),
        str(settings.context_seconds),
        str(settings.max_images_per_window),
        str(settings.image_long_edge),
        TRANSCRIPTION_IDENTITY_PROMPT_REVISION,
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


def require_text_approval(
    approval: AsrPhaseApproval,
    *,
    workflow: RawWorkflowConfig,
    identity: str,
    on: date,
) -> None:
    """글자(두 번째 귀) 요청의 문(D59 19d) — 승인이 글자 모델·요청 신원·상한을 묶었는가.

    1단계 승인 문(`require_live_asr_approval`)을 지난 뒤에 본다. 지어낸 모델·빠진 모델·승인 뒤에
    바뀐 용어집이나 문맥은 보내기 **전에** 닫는다. 문구 역할의 수명주기(확인된 모델)도 여기서 본다 —
    부르는 쪽은 이 문이 닫히면 글자를 묻지 않고 whisper 글자를 둔다(D39: 글자 실패는 전사를 막지
    않는다)."""
    if approval.text_model is None:
        raise AnalysisApprovalError("1단계 승인이 글자 모델을 묶지 않았다 — 글자 요청은 없다")
    if approval.text_model != workflow.models.text_model:
        raise AnalysisApprovalError("승인이 본 글자 모델과 설정 모델이 다르다")
    if approval.text_identity != identity:
        raise AnalysisApprovalError(
            "승인이 본 글자 요청(문맥·언어·용어집)과 지금이 다르다 — 새 견적과 새 승인을 받아라"
        )
    budget = approval.text_chunk_budget
    if isinstance(budget, bool) or not isinstance(budget, int) or budget < 1:
        raise AnalysisApprovalError(f"글자 요청 상한은 1 이상 정수다: {budget!r}")
    keys = approval.text_chunk_keys
    if keys is not None and len(set(keys)) != budget:
        raise AnalysisApprovalError("승인이 묶은 글자 청크와 요청 상한이 어긋난다")
    from vlogsrt.project.raw.capabilities import ROLE_TEXT, preflight_online_transcription

    try:
        preflight_online_transcription(workflow, on, roles=(ROLE_TEXT,))
    except Exception as error:  # noqa: BLE001 - 수명주기 실패도 승인 문의 오류로 닫는다
        raise AnalysisApprovalError(f"글자 요청의 수명주기 문이 닫혔다: {error}") from error


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
    claude_clients: Mapping[str, object] = field(default_factory=dict)
    """Claude 모델 → 그 모델의 프로파일로 만든 클라이언트(D54·D58).

    `ANTHROPIC_API_KEY`가 있을 때만 채워진다. 엔드포인트·버전·시간 한도가 모델마다 다를 수
    있으므로 클라이언트 하나를 모든 Claude 모델에 돌려 쓰지 않는다."""

    def semantic_client(self, model: str) -> object:
        """의미 분석 모델에 맞는 클라이언트 — 없으면 유료 호출이 시작되기 **전에** 닫는다.

        Claude 모델인데 키가 없으면 전사(1단계)조차 시작하지 않는다 — 전사를 사 놓고
        의미 분석에서 막히는 길을 남기지 않는다. 등록되지 않은 Claude 이름은 다른 모델의
        클라이언트로 대신하지 않는다.
        """
        if analysis_provider(model) != ANALYSIS_PROVIDER_ANTHROPIC:
            return self.client
        if model not in CLAUDE_ANALYSIS_PROFILES:
            raise AnalysisApprovalError(
                f"등록되지 않은 Claude 모델입니다: {model!r} — 유료 호출은 시작하지 않았습니다"
            )
        client = self.claude_clients.get(model)
        if client is None:
            raise AnalysisApprovalError(
                f"{analysis_model_label(model)} 분석에는 {ANALYSIS_CLAUDE_KEY_ENV}가 필요합니다 — "
                ".env에 키를 넣고 앱을 다시 띄우세요. 유료 호출은 시작하지 않았습니다"
            )
        return client


__all__ = [
    "ASR_APPROVAL_SCHEMA",
    "ASR_SETTINGS_FINGERPRINT_SCHEMA",
    "AsrPhaseApproval",
    "LiveAnalysisTransport",
    "asr_settings_fingerprint",
    "require_live_asr_approval",
    "require_text_approval",
]
