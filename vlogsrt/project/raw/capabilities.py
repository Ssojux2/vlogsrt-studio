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

from vlogsrt.project.raw import config as raw_config
from vlogsrt.project.raw.config import (
    CONFIRMED_REGISTRY_REVISION,
    TIMING_RESPONSE_FORMAT_WORD_INFO,
    RawGeminiTranscribeProfile,
    RawModelRegistry,
    RawModelRoles,
    RawPricingSnapshot,
    RawWorkflowConfig,
    gemini_transcribe_price_per_minute,
    validate_raw_workflow_config,
)

ROLE_TEXT = "text"
"""문구 역할 — 시각 없는 문자열만 담당한다(계획 §3.2·§11.0)."""

ROLE_TIMING = "timing"
"""시간 역할 — 세그먼트 골격만 담당한다. whisper는 과도기 제공자다(§11.10)."""

TRANSCRIPTION_ENDPOINT = "audio.transcriptions"
"""두 역할이 함께 쓰는 엔드포인트 — 전송 경계의 연산 이름과 같다."""
INTERACTIONS_ENDPOINT = "interactions"
"""Gemini 3.5 Transcribe의 엔드포인트(D92) — `POST /v1beta/interactions`."""

# 기능 이름은 `<요청 칸>:<값>` 또는 요청 칸 이름 그대로다. 역할별 목록은 모델의
# 절대 능력이 아니라 **그 역할로 쓸 때**의 확인 상태다 — 같은 모델이 다른 역할에
# 서는 확인되지 않은 기능일 수 있다.
FEATURE_JSON = "response_format:json"
FEATURE_VERBOSE_JSON = "response_format:verbose_json"
FEATURE_LANGUAGES = "languages"
FEATURE_LANGUAGE = "language"
"""whisper의 단수 언어 칸(D77) — 시간 역할만 확인했다. 복수형 `languages`(문구 힌트)와 다르다."""
FEATURE_KEYWORDS = "keywords"
FEATURE_PROMPT = "prompt"
FEATURE_SEGMENT_TIMES = "timestamp_granularities:segment"
FEATURE_WORD_TIMES = "timestamp_granularities:word"
FEATURE_SRT = "response_format:srt"
FEATURE_VTT = "response_format:vtt"
FEATURE_LOGPROBS = "logprobs"
FEATURE_VERBATIM = "mode:verbatim"
FEATURE_SMART_MODE = "mode:smart"
FEATURE_LANGUAGE_CODES = "language_codes"
"""Gemini의 언어 잠금(`ko-KR`) — 힌트가 아니다(D77·D92)."""
FEATURE_CUSTOM_VOCABULARY = "custom_vocabulary"
FEATURE_SYSTEM_INSTRUCTION = "system_instruction"
FEATURE_DIARIZATION = "speaker_diarization"

_TEXT_CONFIRMED = (FEATURE_JSON, FEATURE_LANGUAGES, FEATURE_KEYWORDS, FEATURE_PROMPT)
_TEXT_UNCONFIRMED = (
    FEATURE_LANGUAGE,
    FEATURE_VERBOSE_JSON,
    FEATURE_SRT,
    FEATURE_VTT,
    FEATURE_SEGMENT_TIMES,
    FEATURE_WORD_TIMES,
    FEATURE_LOGPROBS,
)
# 단어 입자는 whisper-1 verbose_json 문서의 `timestamp_granularities[]`가 세그먼트와 함께 준다 —
# 1단계 내부 전사만 제안·보고서 재료로 묻는다(W-25c, D73). 시각은 여전히 세그먼트에서만 온다.
_TIMING_CONFIRMED = (
    FEATURE_VERBOSE_JSON,
    FEATURE_SEGMENT_TIMES,
    FEATURE_WORD_TIMES,
    FEATURE_LANGUAGE,
)
_TIMING_UNCONFIRMED = (
    FEATURE_JSON,
    FEATURE_SRT,
    FEATURE_VTT,
    FEATURE_LANGUAGES,
    FEATURE_KEYWORDS,
    FEATURE_PROMPT,
    FEATURE_LOGPROBS,
)


# Gemini 3.5 Transcribe(D92, 2026-10-26 문서): verbatim + 단어 시각 + 언어 잠금만 확인했다.
# smart 방식과 custom_vocabulary는 공급자가 단어 시각과 함께 받지 않는다. 힌트(prompt·
# system_instruction)는 규칙 6이 막는다. 세그먼트 시각·srt·vtt는 이 모델에 없다.
_GEMINI_TIMING_CONFIRMED = (
    f"response_format:{TIMING_RESPONSE_FORMAT_WORD_INFO}",
    FEATURE_VERBATIM,
    FEATURE_WORD_TIMES,
    FEATURE_LANGUAGE_CODES,
)
_GEMINI_TIMING_UNCONFIRMED = (
    FEATURE_SMART_MODE,
    FEATURE_CUSTOM_VOCABULARY,
    FEATURE_PROMPT,
    FEATURE_SYSTEM_INSTRUCTION,
    FEATURE_DIARIZATION,
    FEATURE_SEGMENT_TIMES,
    FEATURE_SRT,
    FEATURE_VTT,
    FEATURE_VERBOSE_JSON,
    FEATURE_JSON,
    FEATURE_KEYWORDS,
    FEATURE_LANGUAGES,
    FEATURE_LANGUAGE,
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
    """2026-10-02 공식 문서로 확인된 역할별 **기본** 스냅샷 둘(OpenAI) — 모델은 설정 기본값이다.

    리터럴을 여기 두지 않기 위해 `RawModelRoles()`의 확정 기본값을 그대로 쓴다. 등록된 다른 엔진
    (Gemini 시간 엔진, D92)까지 보려면 `all_capability_snapshots`.
    """
    return _openai_snapshots()


def all_capability_snapshots() -> tuple[ModelCapabilitySnapshot, ...]:
    """기본 둘 + 등록된 Gemini 시간 엔진(2026-10-26, D92) — 요청 조합 확인은 여기서 찾는다."""
    return (*_openai_snapshots(), *map(_gemini_timing_snapshot, _gemini_profiles()))


def _gemini_profiles() -> tuple[RawGeminiTranscribeProfile, ...]:
    return tuple(raw_config.GEMINI_TRANSCRIBE_PROFILES.values())


def _gemini_timing_snapshot(profile: RawGeminiTranscribeProfile) -> ModelCapabilitySnapshot:
    pricing = RawPricingSnapshot(
        checked_at=profile.pricing_checked_at,
        timing_per_minute=profile.audio_input_per_minute + profile.text_output_per_minute,
    )
    return ModelCapabilitySnapshot(
        model_id=profile.model,
        role=ROLE_TIMING,
        endpoint=INTERACTIONS_ENDPOINT,
        requested_response_format=TIMING_RESPONSE_FORMAT_WORD_INFO,
        confirmed_supported_features=_GEMINI_TIMING_CONFIRMED,
        unsupported_or_unverified_features=_GEMINI_TIMING_UNCONFIRMED,
        docs_checked_at=profile.pricing_checked_at,
        registry_revision=profile.pricing_revision,
        pricing_snapshot=pricing,
        deprecation_notice_date=None,
        shutdown_date=None,
        source_urls=profile.source_urls,
        account_access_status=profile.account_access_status,
        integration_test_status=profile.integration_test_status,
    )


def _openai_snapshots() -> tuple[ModelCapabilitySnapshot, ...]:
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
    """역할의 **기본** 스냅샷(OpenAI) — 모르는 역할은 등록표 밖이다."""
    for snapshot in confirmed_capability_snapshots():
        if snapshot.role == role:
            return snapshot
    raise UnverifiedModelCapability(
        f"역할 {role!r}은(는) 확인된 등록표에 없다 — 역할은 문구·시간 둘뿐이다"
    )


def capability_snapshot(role: str, model: str) -> ModelCapabilitySnapshot:
    """역할·모델의 확인된 스냅샷 — 없으면 조용히 다른 모델로 넘어가지 않고 실패한다."""
    for snapshot in all_capability_snapshots():
        if snapshot.role == role and snapshot.model_id == model:
            return snapshot
    default = capability_snapshot_for_role(role)
    raise UnverifiedModelCapability(
        f"{role} 역할의 확인된 모델은 {default.model_id!r}(또는 등록된 엔진)인데"
        f" 요청은 {model!r}이다 — 조용한 모델 교체는 없다. 바꾸려면 문서를 다시 확인하고"
        " 새 등록표 리비전을 내려라"
    )


def confirm_request_combo(
    *, role: str, model: str, response_format: str, features: tuple[str, ...] = ()
) -> ModelCapabilitySnapshot:
    """요청 조합이 확인된 등록표 안에 있는지 호출 **전에** 확인한다.

    모델이 다르면 실패한다 — 옛 모델로 조용히 되돌리거나 확인 안 된 새 모델로
    넘어가는 길이 없다(§11.9). 응답 형식과 기능은 요청에 실제로 실리는 것만
    확인 받는다.
    """
    snapshot = capability_snapshot(role, model)
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
        configured = config.models.text_model if role == ROLE_TEXT else config.models.timing_model
        snapshot = capability_snapshot(role, configured)
        if role == ROLE_TIMING:
            _require_timing_lifecycle(config, snapshot, on)


def _require_timing_lifecycle(
    config: RawWorkflowConfig, snapshot: ModelCapabilitySnapshot, on: date
) -> None:
    """시간 엔진의 수명주기 — whisper는 종료 예정일, Gemini는 자기 단가 재확인 기한(D92).

    Gemini는 whisper의 종료일에 막히지 않는다. 그 대신 단가 재확인 기한이 지나면 가격 미정이라
    새 온라인 작업을 닫는다(0원으로 부르지 않는다, D84).
    """
    profile = raw_config.GEMINI_TRANSCRIBE_PROFILES.get(snapshot.model_id)
    if profile is not None:
        if gemini_transcribe_price_per_minute(profile, today=on) is None:
            raise TimingProviderUnavailable(
                f"시간 엔진({profile.label})의 단가 재확인 기한"
                f" {profile.price_valid_through.isoformat()}이 지났다(on={on.isoformat()}) —"
                " 가격표를 다시 확인해 등록표를 고치기 전까지 새 온라인 작업을 닫는다(D92·D84)"
            )
        return
    registry = config.registry
    if registry.blocks_online_timing(on):
        raise TimingProviderUnavailable(
            f"시간 제공자({config.models.timing_model})가 종료 예정일 "
            f"{registry.timing_shutdown_date.isoformat()}에 도달했다(on={on.isoformat()}) — "
            "신규 온라인 시간 작업은 차단한다(§11.10)"
        )


def price_per_minute(role: str) -> Decimal:
    """역할의 **기본**(OpenAI) 확인된 분당 요금 — 미확인 가격을 0원으로 계산하지 않기 위한 값."""
    snapshot = capability_snapshot_for_role(role)
    pricing = snapshot.pricing_snapshot
    return pricing.text_per_minute if role == ROLE_TEXT else pricing.timing_per_minute


def timing_price_per_minute(model: str, on: date) -> Decimal | None:
    """시간 엔진의 분당 요금(D92) — Gemini는 재확인 기한이 지나면 None(가격 미정, 0원이 아니다)."""
    profile = raw_config.GEMINI_TRANSCRIBE_PROFILES.get(model)
    if profile is not None:
        return gemini_transcribe_price_per_minute(profile, today=on)
    return capability_snapshot(ROLE_TIMING, model).pricing_snapshot.timing_per_minute
