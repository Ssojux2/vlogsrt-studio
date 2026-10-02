"""모델 기능 등록표와 온라인 수명주기 검사 — A7-1의 사전 차단 계층.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.9·§11.10의
`ModelCapabilitySnapshot` 계약을 코드로 세운다. 2026-10-02 공식 문서로 확인된
조합만 등록에 들어 있고, 그 밖의 조합은 **호출 한 번 나가기 전에** 실패한다 —
조용한 구형·신형 모델 fallback은 없다.

모델 리터럴은 `config.py`의 확정 기본값에서만 온다(`scripts/check_invariants.py`
모델 리터럴 검사). 이 파일은 그 값을 **값으로만** 읽는다. 확인되지 않은 날짜형
스냅샷을 만들어 쓰지 않는다(§11.9).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from vlogsrt.project.raw.config import (
    CONFIRMED_REGISTRY_REVISION,
    RawModelRegistry,
    RawModelRoles,
    RawPricingSnapshot,
    RawWorkflowConfig,
    validate_raw_workflow_config,
)

ROLE_TEXT = "text"
"""문구 역할 — 시각 없는 문자열만 담당한다(계획 §3.2·§11.0)."""

ROLE_TIMING = "timing"
"""시간 역할 — 세그먼트 골격만 담당한다. whisper는 과도기 제공자다(§11.10)."""

TRANSCRIPTION_ENDPOINT = "audio.transcriptions"
"""두 역할이 함께 쓰는 엔드포인트 — 전송 경계의 연산 이름과 같다."""

# 기능 이름은 `<요청 칸>:<값>` 또는 요청 칸 이름 그대로다. 역할별 목록은 모델의
# 절대 능력이 아니라 **그 역할로 쓸 때**의 확인 상태다 — 같은 모델이 다른 역할에
# 서는 확인되지 않은 기능일 수 있다.
FEATURE_JSON = "response_format:json"
FEATURE_VERBOSE_JSON = "response_format:verbose_json"
FEATURE_LANGUAGES = "languages"
FEATURE_KEYWORDS = "keywords"
FEATURE_PROMPT = "prompt"
FEATURE_SEGMENT_TIMES = "timestamp_granularities:segment"
FEATURE_WORD_TIMES = "timestamp_granularities:word"
FEATURE_SRT = "response_format:srt"
FEATURE_VTT = "response_format:vtt"
FEATURE_LOGPROBS = "logprobs"

_TEXT_CONFIRMED = (FEATURE_JSON, FEATURE_LANGUAGES, FEATURE_KEYWORDS, FEATURE_PROMPT)
_TEXT_UNCONFIRMED = (
    FEATURE_VERBOSE_JSON,
    FEATURE_SRT,
    FEATURE_VTT,
    FEATURE_SEGMENT_TIMES,
    FEATURE_WORD_TIMES,
    FEATURE_LOGPROBS,
)
_TIMING_CONFIRMED = (FEATURE_VERBOSE_JSON, FEATURE_SEGMENT_TIMES)
_TIMING_UNCONFIRMED = (
    FEATURE_JSON,
    FEATURE_SRT,
    FEATURE_VTT,
    FEATURE_LANGUAGES,
    FEATURE_KEYWORDS,
    FEATURE_PROMPT,
    FEATURE_WORD_TIMES,
    FEATURE_LOGPROBS,
)


class UnverifiedModelCapability(RuntimeError):
    """요청한 모델·응답 형식·기능 조합이 확인된 등록표에 없다.

    조용한 fallback이 아니라 실패로 끝난다 — 모델을 바꾸려면 공식 문서를 다시
    확인하고 새 등록표 리비전을 내려야 한다(§11.9).
    """


class TimingProviderUnavailable(RuntimeError):
    """시간 제공자를 온라인으로 부를 수 없다(§11.10의 TIMING_PROVIDER_UNAVAILABLE).

    종료 예정일 도달·등록표 미확인이 여기 담긴다. 문자열을 시간으로 나눠 SRT를
    만드는 대안은 없다 — 텍스트만 남기려면 별도 전사 전용 실행의 승인이 필요하다.
    """


@dataclass(frozen=True, slots=True)
class ModelCapabilitySnapshot:
    """모델 하나·역할 하나의 확인된 능력 스냅샷(§11.9 필수 레코드).

    `account_access_status`·`integration_test_status`는 초기값이 미검증이다 —
    유료 사전 점검은 별도 승인이고, 미검증이 온라인 호출을 막지는 않는다(§11.9).
    모델 목록 API에 보이는 것은 접근 후보 확인일 뿐 기능 증명이 아니다.
    """

    model_id: str
    role: str
    endpoint: str
    requested_response_format: str
    confirmed_supported_features: tuple[str, ...]
    unsupported_or_unverified_features: tuple[str, ...]
    docs_checked_at: date
    registry_revision: str
    pricing_snapshot: RawPricingSnapshot
    deprecation_notice_date: date | None
    shutdown_date: date | None
    source_urls: tuple[str, ...]
    account_access_status: str
    integration_test_status: str

    def supports(self, feature: str) -> bool:
        return feature in self.confirmed_supported_features


def confirmed_capability_snapshots() -> tuple[ModelCapabilitySnapshot, ...]:
    """2026-10-02 공식 문서로 확인된 역할별 스냅샷 둘 — 모델은 설정 기본값에서 온다.

    리터럴을 여기 두지 않기 위해 `RawModelRoles()`의 확정 기본값을 그대로 쓴다.
    새 모델을 등록하려면 문서를 다시 확인하고 설정 기본값·리비전을 함께 내린다.
    """
    roles = RawModelRoles()
    registry = RawModelRegistry()
    pricing = RawPricingSnapshot()
    return (
        ModelCapabilitySnapshot(
            model_id=roles.text_model,
            role=ROLE_TEXT,
            endpoint=TRANSCRIPTION_ENDPOINT,
            requested_response_format="json",
            confirmed_supported_features=_TEXT_CONFIRMED,
            unsupported_or_unverified_features=_TEXT_UNCONFIRMED,
            docs_checked_at=registry.docs_checked_at,
            registry_revision=registry.revision,
            pricing_snapshot=pricing,
            deprecation_notice_date=None,
            shutdown_date=None,
            source_urls=registry.source_urls,
            account_access_status=registry.account_access_status,
            integration_test_status=registry.integration_test_status,
        ),
        ModelCapabilitySnapshot(
            model_id=roles.timing_model,
            role=ROLE_TIMING,
            endpoint=TRANSCRIPTION_ENDPOINT,
            requested_response_format="verbose_json",
            confirmed_supported_features=_TIMING_CONFIRMED,
            unsupported_or_unverified_features=_TIMING_UNCONFIRMED,
            docs_checked_at=registry.docs_checked_at,
            registry_revision=registry.revision,
            pricing_snapshot=pricing,
            deprecation_notice_date=registry.timing_deprecation_notice_date,
            shutdown_date=registry.timing_shutdown_date,
            source_urls=registry.source_urls,
            account_access_status=registry.account_access_status,
            integration_test_status=registry.integration_test_status,
        ),
    )


def capability_snapshot_for_role(role: str) -> ModelCapabilitySnapshot:
    """역할의 확인된 스냅샷 — 모르는 역할은 등록표 밖이다."""
    for snapshot in confirmed_capability_snapshots():
        if snapshot.role == role:
            return snapshot
    raise UnverifiedModelCapability(
        f"역할 {role!r}은(는) 확인된 등록표에 없다 — 역할은 문구·시간 둘뿐이다"
    )


def confirm_request_combo(
    *, role: str, model: str, response_format: str, features: tuple[str, ...] = ()
) -> ModelCapabilitySnapshot:
    """요청 조합이 확인된 등록표 안에 있는지 호출 **전에** 확인한다.

    모델이 다르면 실패한다 — 옛 모델로 조용히 되돌리거나 확인 안 된 새 모델로
    넘어가는 길이 없다(§11.9). 응답 형식과 기능은 요청에 실제로 실리는 것만
    확인 받는다.
    """
    snapshot = capability_snapshot_for_role(role)
    if snapshot.model_id != model:
        raise UnverifiedModelCapability(
            f"{role} 역할의 확인된 모델은 {snapshot.model_id!r}인데 요청은 {model!r}이다 — "
            "바꾸려면 문서를 다시 확인하고 새 등록표 리비전을 내려라"
        )
    wanted = (f"response_format:{response_format}", *features)
    refused = [
        feature for feature in wanted if feature in snapshot.unsupported_or_unverified_features
    ]
    if refused:
        raise UnverifiedModelCapability(
            f"{role} 역할({snapshot.model_id})에 확인되지 않은 기능 {refused}를 요청했다 — "
            "enum에 있다는 이유로 보내는 게 아니다(§11.4)"
        )
    missing = [
        feature for feature in wanted if feature not in snapshot.confirmed_supported_features
    ]
    if missing:
        raise UnverifiedModelCapability(
            f"{role} 역할({snapshot.model_id})에 확인된 기능이 아니다: {missing}"
        )
    return snapshot


def preflight_online_transcription(
    config: RawWorkflowConfig,
    on: date,
    roles: tuple[str, ...] = (ROLE_TEXT, ROLE_TIMING),
) -> None:
    """온라인 호출 직전의 공통 수명주기 검사(§11.10) — 내부 ASR와 최종 자막이 같은 문을 지난다.

    먼저 설정 전체의 동결 검사를 통과시킨다(루트 리뷰) — `dataclasses.replace`로
    지어낸 설정은 생성 시 검사를 지나지 않으므로, 종료 차단을 끄거나 날짜를 민
    설정이 이 문을 먼저 만난다. 그 다음 확인된 리비전·확인된 모델·(시간 역할만)
    종료 예정일을 본다. 과거 유효 결과 열기·오프라인 fixture 재생·수동 수정은
    이 문을 지나지 않으므로 영향이 없다. 종료일의 정확한 시각은 가정하지 않는다
    — 날짜에 도달하면 보수적으로 막는다.
    """
    try:
        validate_raw_workflow_config(config)
    except ValueError as error:
        raise UnverifiedModelCapability(
            f"설정 자체가 동결 계약을 어긴다 — 온라인 문은 닫는다: {error}"
        ) from error
    registry = config.registry
    if registry.revision != CONFIRMED_REGISTRY_REVISION:
        raise UnverifiedModelCapability(
            f"등록표 리비전 {registry.revision!r}은(는) 확인된 리비전이 아니다 — "
            f"확인된 값은 {CONFIRMED_REGISTRY_REVISION!r}이다. 리비전을 바꿔 종료일을 "
            "미루는 우회는 없다"
        )
    for role in roles:
        snapshot = capability_snapshot_for_role(role)
        configured = config.models.text_model if role == ROLE_TEXT else config.models.timing_model
        if configured != snapshot.model_id:
            raise UnverifiedModelCapability(
                f"{role} 역할이 확인된 모델({snapshot.model_id})과 다른 {configured!r}을(를) "
                "가리킨다 — 조용한 모델 교체는 없다"
            )
    if ROLE_TIMING in roles and registry.blocks_online_timing(on):
        raise TimingProviderUnavailable(
            f"시간 제공자({config.models.timing_model})가 종료 예정일 "
            f"{registry.timing_shutdown_date.isoformat()}에 도달했다(on={on.isoformat()}) — "
            "신규 온라인 시간 작업은 차단한다(§11.10)"
        )


def price_per_minute(role: str) -> Decimal:
    """역할의 확인된 분당 요금 — 미확인 가격을 0원으로 계산하지 않기 위한 값이다."""
    snapshot = capability_snapshot_for_role(role)
    pricing = snapshot.pricing_snapshot
    return pricing.text_per_minute if role == ROLE_TEXT else pricing.timing_per_minute
