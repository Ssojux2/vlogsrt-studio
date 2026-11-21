"""브라우저 실제 호출 생산 transport — 승인된 행동에서 늦게 만든다(A4 생산 갭).

주입 경계(`LiveAnalysisTransport`)는 시험·루트 가짜 브라우저의 것이다. 일반
실행(`ui.app`)은 아무것도 주입받지 않으므로 그대로는 실제 호출 단추가 전부
정직한 오류로 끝난다 — 이 모듈이 그 갭을 **사람이 승인한 행동 안에서만** 채운다:

- 일반 앱 시작은 정본 `.env` 로더로 설정을 준비하고 자격증명을 메모리에만
  보관한다. 테스트의 명시적 환경 Mapping은 파일 로더를 거치지 않는다.
  자격증명 값은 화면·로그·프로젝트 기록에 내보내지 않는다.
- 전송 경계는 이미 받아들인 정본뿐이다: 내부 ASR는 `LiveTranscriber`
  (`vlogsrt/transcribe`, API 호출의 유일한 곳), Luna는 `create_vision_client`,
  Claude 의미 분석(D54·D58)은 `create_claude_client`(`ANTHROPIC_API_KEY`가 있을 때만,
  등록된 모델마다 그 프로파일로).
  모델 대체는 없다 — 설정 모델 그대로고 실패는 그대로 올라간다.
- 승인은 **화면이 잰 견적 그대로**에서 만든다(mint). 사람이 보지 않은 값을
  묶는 길이 없고, 만든 뒤에도 게이트(`require_live_asr_approval`·
  `_live_semantic_providers`)가 전송 순간에 다시 검사한다 — 이 모듈은
  게이트를 여는 열쇠를 만들 뿐 게이트 자체를 대신하지 않는다.

환경(`VLOGSRT_OFFLINE`)·작업공간 `network_mode`가 닫혀 있으면 여기서 만든
transport로도 0송신이다 — 승인·transport는 그 문들보다 강하지 않다(§14.3).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from vlogsrt.config import Config
from vlogsrt.env import ANALYSIS_CLAUDE_KEY_ENV, ANALYSIS_GEMINI_KEY_ENV
from vlogsrt.project.raw import request_usage
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    LiveAnalysisTransport,
    asr_settings_fingerprint,
)
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.config import (
    CLAUDE_ANALYSIS_PROFILES,
    GEMINI_ANALYSIS_PROFILES,
    GEMINI_TRANSCRIBE_PROFILE,
    RawClaudeAnalysisProfile,
    analysis_model_label,
    official_price_model,
)
from vlogsrt.project.raw.models import AnalysisEstimate, AnalysisSemanticEstimate
from vlogsrt.transcribe import (
    create_claude_client,
    create_gemini_client,
    create_vision_client,
)

STT_KEY_ENV = "OPENAI_API_KEY"
"""STT·Luna가 함께 쓰는 서버 환경 키 이름 — 값은 화면·로그에 절대 나오지 않는다."""

STT_BASE_URL_ENV = "OPENAI_BASE_URL"
"""선택 공개 엔드포인트 — 비면 공개 엔드포인트 그대로(전송 경계가 못 박는다)."""


@dataclass(frozen=True, slots=True)
class LiveRequestCarrier:
    """실제 호출 제공자가 영수증 관찰자·취소 문을 넘겨받는 자리 — 스스로는 아무것도 보내지 않는다.

    whisper 전송(`LiveTranscriber`)이 이 둘을 실어 나르던 자리다(D163 — whisper 은퇴)."""

    observer: Any = None
    cancellation: Callable[[], None] | None = None


class BrowserRuntimeError(RuntimeError):
    """생산 transport를 만들 수 없다 — 사람이 고칠 수 있는 이유로만."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EnvironmentCredentials:
    """서버 **시작 환경**에서 찍은 자격증명 — 값은 화면·로그에 절대 나오지 않는다."""

    api_key: str = field(repr=False)
    base_url: str | None


def capture_environment_credentials(
    environ: Mapping[str, str] | None = None,
) -> EnvironmentCredentials | None:
    """시작 환경의 키를 **한 번** 찍는다 — `.env` 로더를 지나지 않는다.

    게이트(`online_env_blocked`)는 검사 과정에서 `.env`를 `os.environ`에 적재할
    수 있다. 그 뒤에 `os.environ`을 다시 보면 파일에서 온 키를 환경 키로 오인한다
    — 그래서 브라우저 서버는 **띄워질 때** 이 함수로 찍어 둔다. 이때 없던 키는
    이후 어떤 적재로도 생산 transport를 열지 못한다.
    """
    source: Mapping[str, str] = os.environ if environ is None else environ
    key = (source.get(STT_KEY_ENV) or "").strip()
    if not key:
        return None
    base_url = (source.get(STT_BASE_URL_ENV) or "").strip() or None
    return EnvironmentCredentials(api_key=key, base_url=base_url)


def capture_browser_credentials() -> EnvironmentCredentials | None:
    """Normal browser startup uses the same opaque dotenv loader as the CLI."""
    from vlogsrt import env

    env.ensure_loaded()
    return capture_environment_credentials()


@dataclass(frozen=True, slots=True)
class ClaudeCredentials:
    """Claude 의미 분석 키(D54) — 메모리에만 있고 repr·화면·로그에 나오지 않는다."""

    api_key: str = field(repr=False)


def capture_claude_credentials(
    environ: Mapping[str, str] | None = None,
) -> ClaudeCredentials | None:
    """`ANTHROPIC_API_KEY`를 한 번 찍는다 — OpenAI 키를 빌리지 않는다(공급자마다 제 키).

    환경을 주지 않으면 정본 `.env` 로더를 지난 프로세스 환경을 본다(키는 사람이 `.env`에
    넣는다). 명시적 Mapping은 로더를 거치지 않는다 — 시험과 단일 명령 흐름의 길이다.
    """
    if environ is None:
        from vlogsrt import env

        env.ensure_loaded()
    source: Mapping[str, str] = os.environ if environ is None else environ
    key = (source.get(ANALYSIS_CLAUDE_KEY_ENV) or "").strip()
    return ClaudeCredentials(api_key=key) if key else None


@dataclass(frozen=True, slots=True)
class GeminiCredentials:
    """Gemini 의미 분석 키(D86) — 메모리에만 있고 repr·화면·로그에 나오지 않는다."""

    api_key: str = field(repr=False)


def capture_gemini_credentials(
    environ: Mapping[str, str] | None = None,
) -> GeminiCredentials | None:
    """`GEMINI_API_KEY`를 한 번 찍는다 — 옛 비전 역할의 `GOOGLE_API_KEY`를 빌리지 않는다(D83)."""
    if environ is None:
        from vlogsrt import env

        env.ensure_loaded()
    source: Mapping[str, str] = os.environ if environ is None else environ
    key = (source.get(ANALYSIS_GEMINI_KEY_ENV) or "").strip()
    return GeminiCredentials(api_key=key) if key else None


def browser_network_mode() -> str:
    """Effective process mode, without inspecting or returning credential values."""
    from vlogsrt import env

    return "offline" if env.is_offline() else "online"


def prepare_approved_browser_network() -> None:
    """Approval enables the absent browser default; explicit offline stays authoritative."""
    from vlogsrt import env

    env.ensure_loaded()
    os.environ.setdefault("VLOGSRT_OFFLINE", "0")


def _require_environment_credentials(
    environ: Mapping[str, str] | None,
) -> EnvironmentCredentials:
    captured = capture_environment_credentials(environ)
    if captured is None:
        raise BrowserRuntimeError(
            f"실제 호출에는 서버 환경의 {STT_KEY_ENV}가 필요하다 — 브라우저 서버를"
            " 띄운 셸에서 키를 내보내고 서버를 다시 띄우세요. 이 경로는 .env 파일을"
            " 읽지 않는다(키는 시작 환경에서만 온다)."
        )
    return captured


def live_transport_for_credentials(
    credentials: EnvironmentCredentials,
    *,
    asr_approvals: Any,
    luna_approvals: Any,
    on: date | None = None,
    claude: ClaudeCredentials | None = None,
    gemini: GeminiCredentials | None = None,
) -> LiveAnalysisTransport:
    """찍아 둔 자격증명으로 생산 transport를 만든다 — 정본 전송 경계만 쓴다.

    이 함수를 부르는 것만으로는 아무 호출도 일어나지 않는다(클라이언트 생성은
    SDK 객체일 뿐이다). 첫 전송은 승인 공급자가 돌려준 승인을 게이트가 검증한
    뒤에야 시작된다(§14.3). Claude 키가 있으면 등록된 Claude 모델마다 클라이언트를
    만든다 — 엔드포인트·버전·시간 한도는 **그 모델의** 프로파일 값이다(D54·D58).
    """
    return LiveAnalysisTransport(
        sender=LiveRequestCarrier(observer=request_usage.observer()),
        cfg=Config(),
        client=create_vision_client(api_key=credentials.api_key, base_url=credentials.base_url),
        asr_approvals=asr_approvals,
        luna_approvals=luna_approvals,
        on=on,
        claude_clients={} if claude is None else _claude_clients(claude),
        gemini_clients={} if gemini is None else _gemini_clients(gemini),
        timing_client=None if gemini is None else _gemini_timing_client(gemini),
    )


def _gemini_timing_client(gemini: GeminiCredentials) -> object:
    """시간 엔진(D92)의 클라이언트 — 전사 프로파일의 엔드포인트·시간 한도로. 보내지 않는다."""
    profile = GEMINI_TRANSCRIBE_PROFILE
    return create_gemini_client(
        api_key=gemini.api_key,
        base_url=profile.api_base_url,
        api_version=profile.api_version,
        timeout_seconds=profile.timeout_seconds,
    )


def _claude_clients(claude: ClaudeCredentials) -> dict[str, object]:
    """등록된 Claude 모델 → 그 프로파일로 만든 클라이언트. 만들기만 하고 보내지 않는다."""
    return {
        model: _claude_client(claude, profile)
        for model, profile in CLAUDE_ANALYSIS_PROFILES.items()
    }


def _gemini_clients(gemini: GeminiCredentials) -> dict[str, object]:
    """등록된 Gemini 모델 → 그 프로파일로 만든 클라이언트. 만들기만 하고 보내지 않는다."""
    return {
        model: create_gemini_client(
            api_key=gemini.api_key,
            base_url=profile.api_base_url,
            api_version=profile.api_version,
            timeout_seconds=profile.timeout_seconds,
        )
        for model, profile in GEMINI_ANALYSIS_PROFILES.items()
    }


def _claude_client(claude: ClaudeCredentials, profile: RawClaudeAnalysisProfile) -> object:
    return create_claude_client(
        api_key=claude.api_key,
        base_url=profile.api_base_url,
        api_version=profile.api_version,
        timeout_seconds=profile.timeout_seconds,
    )


def production_live_transport(
    *,
    asr_approvals: Any,
    luna_approvals: Any,
    on: date | None = None,
    environ: Mapping[str, str] | None = None,
) -> LiveAnalysisTransport:
    """지금 환경에서 transport를 만든다 — 진입점이 환경을 직접 주는 경우.

    UI 서버는 게이트가 `.env`를 적재하기 전에 찍은 `capture_environment_credentials`
    결과를 쓰는 쪽(`live_transport_for_credentials`)이 정확하다. 이 함수는
    시작·승인이 같은 순간에 일어나는 단일 명령 흐름을 위한 것이다.
    """
    return live_transport_for_credentials(
        _require_environment_credentials(environ),
        asr_approvals=asr_approvals,
        luna_approvals=luna_approvals,
        on=on,
        claude=capture_claude_credentials(os.environ if environ is None else environ),
        gemini=capture_gemini_credentials(os.environ if environ is None else environ),
    )


# ─────────────────────────────────────────────────────────────
#  승인 주조(mint) — 화면이 잰 견적 그대로에서
# ─────────────────────────────────────────────────────────────


def asr_estimate_digest(estimates: Sequence[AnalysisEstimate]) -> str:
    """1단계 견적 표의 지문 — 사람이 본 값과 시작 때의 값이 같은지의 기준.

    표시 전용 문구(note)는 빼고, 승인이 묶는 측정값(출처·스트림·청크·창·모델
    회신 수)만 묶는다 — 문구만 바뀌었다고 승인을 다시 받게 하지 않는다. 비용
    상한은 **있을 때만** 덧붙인다(D54) — Luna 견적의 지문은 W-14 이전과 같다.
    교차 확인의 글자 분량(D59 19d)도 **있을 때만**이다 — 꺼져 있으면 19d 이전 바이트 그대로다.
    """
    rows = sorted(
        (
            item.asset_id,
            item.source_order,
            item.source_name,
            bool(item.has_audio),
            item.audio_stream_index,
            item.sample_count,
            item.window_count,
            item.asr_chunk_count,
            item.luna_request_count,
            *_present(item.ceiling_usd),
            *_text_pass(item),
            *_rescue_pass(item),
            *_timing_engine_pass(item),
        )
        for item in estimates
    )
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _timing_engine_pass(item: AnalysisEstimate) -> tuple[object, ...]:
    """whisper가 아닌 시간 엔진(D92) — 견적이 실었을 때만 칸을 더한다(모델·금액)."""
    if item.timing_model is None:
        return ()
    return ("timing-engine", item.timing_model, item.timing_usd)


def _rescue_pass(item: AnalysisEstimate) -> tuple[object, ...]:
    """「다른 귀 기준 재전사」 상한(W-29b, D78) — 견적이 실었을 때만 칸을 더한다(수·금액)."""
    if item.rescue_chunk_count is None:
        return ()
    return ("rescue", item.rescue_chunk_count, item.rescue_usd)


def semantic_estimate_digest(estimates: Sequence[AnalysisSemanticEstimate]) -> str:
    """2단계 견적 표의 지문 — 실측 지문·창·이미지·단계를 묶는다(상한은 있을 때만)."""
    rows = sorted(
        (
            item.asset_id,
            item.source_order,
            item.source_name,
            item.phase,
            item.semantic_fingerprint,
            item.window_count,
            item.image_count,
            item.luna_request_count,
            *_present(item.request_ceiling_usd),
            *_present(item.ceiling_usd),
        )
        for item in estimates
    )
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _present(value: str | None) -> tuple[str, ...]:
    """값이 있을 때만 칸을 더한다 — 없는 칸이 지문에 `null`로 끼어들지 않게(D54)."""
    return () if value is None else (value,)


def _text_pass(item: AnalysisEstimate) -> tuple[object, ...]:
    """교차 확인 글자 분량(D59 19d) — 켜져 있어 견적이 센 때만 칸을 더한다(수·길이·금액)."""
    if item.text_chunk_count is None:
        return ()
    keys = () if item.text_chunk_keys is None else (list(item.text_chunk_keys),)
    # Gemini 글자 경로(W-35b)만 모델 칸이 붙는다 — gpt 다른 귀 줄은 바이트 그대로다.
    model = () if item.text_model is None else ("text-model", item.text_model)
    return ("text-pass", item.text_chunk_count, item.text_seconds, item.text_usd, *keys, *model)


def mint_asr_approvals(
    service: Any,
    root: Path,
    asset_ids: Sequence[str] | None,
    *,
    understanding: RawUnderstandingSettings | None = None,
    on: date | None = None,
    quoted: Sequence[AnalysisEstimate] = (),
    text_identity: str | None = None,
) -> dict[str, AsrPhaseApproval]:
    """견적의 측정값으로 1단계 승인을 만든다 — 전사 전에 알 수 있는 값만(무성 원본은 없다).

    글자 칸(D59 19d)은 **사람이 본 견적** 줄(`quoted`)과 그 digest가 묶은 신원(`text_identity`)
    으로만 묶는다 — 용어집을 다시 읽지 않는다. 하나라도 없으면(고급 탭) 글자 요청이 0회다."""
    settings = understanding or RawUnderstandingSettings()
    workflow = service.settings(root)
    estimates = service.analysis_estimate(root, asset_ids, settings)
    texts = _quoted_text_fields(quoted, workflow.models.text_model, text_identity)
    assets = {asset.asset_id: asset for asset in service.list_source_assets(root)}
    approvals: dict[str, AsrPhaseApproval] = {}
    for estimate in estimates:
        if not estimate.has_audio:
            continue  # 승인할 전사가 없다 — 무성 원본
        asset = assets.get(estimate.asset_id)
        if asset is None or not asset.content_sha256:
            raise BrowserRuntimeError(
                f"검증된 원본이 아니다: {estimate.source_name} — 가져오기를 먼저 끝내세요"
            )
        if estimate.timing_model is not None and estimate.timing_usd is None:
            raise BrowserRuntimeError(
                f"전사 엔진 {estimate.timing_model}의 단가가 재확인 기한을 지나 가격 미정이다:"
                f" {estimate.source_name} — 단가를 확인해 고친 뒤 다시 승인하라(D92)"
            )
        view = service.source_settings(root, estimate.asset_id)  # 그 원본의 시간 엔진(D92)
        approvals[estimate.asset_id] = AsrPhaseApproval(
            approved=True,
            source_sha256=asset.content_sha256,
            audio_stream_index=int(estimate.audio_stream_index or 0),
            chunk_budget=max(1, int(estimate.asr_chunk_count)),
            settings_fingerprint=asr_settings_fingerprint(settings, view),
            timing_model=view.models.timing_model,
            approved_on=(on or date.today()).isoformat(),
            **texts.get(estimate.asset_id, {}),
            **_quoted_rescue(quoted, estimate.asset_id),
        )
    return approvals


def _quoted_rescue(quoted: Sequence[AnalysisEstimate], asset_id: str) -> dict[str, Any]:
    """사람이 본 견적 줄의 다시 묻기 예산(D78) — 그 원본 줄이 없으면(고급 탭) 0이다."""
    row = next((item for item in quoted if item.asset_id == asset_id), None)
    return {} if row is None else _rescue_approval_fields(row)


def _rescue_approval_fields(estimate: AnalysisEstimate) -> dict[str, Any]:
    """승인의 다시 묻기 예산(W-29b, D78) — 사람이 본 견적이 상한을 실었을 때만. 아니면 0(묻지
    않는다)."""
    if not estimate.rescue_chunk_count:
        return {}
    return {"rescue_chunk_budget": int(estimate.rescue_chunk_count)}


def _quoted_text_fields(
    quoted: Sequence[AnalysisEstimate], text_model: str, identity: str | None
) -> dict[str, dict[str, Any]]:
    """사람이 본 견적 줄마다 승인의 글자 칸 — 물을 청크를 센 줄만(D59 19d).

    gpt 다른 귀 줄은 digest가 묶은 신원(`identity`)이 없으면 없다. Gemini 글자 모델 줄(Hydrogen
    글자 귀 D187 · Gemini 글자 경로 W-35b)은 신원을 그 모델에서 잰다(교차 확인 꼬리표 밖)."""
    if not quoted:
        return {}
    fields = {
        item.asset_id: _text_approval_fields(item, text_model, identity or "")
        for item in quoted
        if item.text_model is not None or identity is not None
    }
    return {asset_id: values for asset_id, values in fields.items() if values}


def _text_approval_fields(
    estimate: AnalysisEstimate, text_model: str, identity: str
) -> dict[str, Any]:
    """승인의 글자 칸(D59 19d) — 견적이 물을 청크를 셌을 때만. 아니면 빈 사전(19d 이전 모양).

    견적 줄이 Gemini 글자 경로를 셌으면(W-35b) 모델·신원은 그 경로의 것이다 — 힌트가 없어 용어집이
    신원에 들지 않는다. gpt 다른 귀 줄은 digest가 묶은 신원(`identity`) 그대로다."""
    if not estimate.text_chunk_count:
        return {}
    if estimate.text_model is not None:
        if estimate.text_usd is None:
            raise BrowserRuntimeError(
                f"글자 귀 {estimate.text_model}의 단가가 재확인 기한을 지나 가격 미정이다:"
                f" {estimate.source_name} — 단가를 확인해 고친 뒤 다시 승인하라(W-35b·D187)"
            )
        text_model, identity = estimate.text_model, _gemini_text_identity(estimate.text_model)
    return {
        "text_model": text_model,
        "text_identity": identity,
        "text_chunk_budget": int(estimate.text_chunk_count),
        "text_chunk_keys": estimate.text_chunk_keys,
    }


def _gemini_text_identity(model: str) -> str:
    """견적 줄의 글자 모델이 묻는 요청 신원 — Hydrogen 글자 귀(D187) 또는 Gemini 글자 경로(W-35b).

    힌트가 없어 용어집이 신원에 들지 않는다. 그 밖의 모델은 승인 범위가 모르는 모델이다."""
    from vlogsrt.project.raw import gemini_text, hydrogen_text_pass
    from vlogsrt.project.raw.config import GEMINI_TEXT_SETTINGS, HYDROGEN_TEXT

    if model == HYDROGEN_TEXT.model:
        return hydrogen_text_pass.text_identity(HYDROGEN_TEXT)
    if model == GEMINI_TEXT_SETTINGS.model:
        return gemini_text.text_identity(GEMINI_TEXT_SETTINGS)
    raise BrowserRuntimeError(f"승인 범위의 글자 모델을 모른다: {model!r}")


def _phase_artifact_for(service: Any, root: Path, revision_id: str, asset_id: str) -> Any:
    """원본 하나의 1단계 기록 — 행 문서가 아니면 같은 폴더의 asr_phase.json."""
    from vlogsrt.project.raw.analysis_workflow import ASR_PHASE_SCHEMA

    document = service.analysis_document(root, revision_id, asset_id)
    if document is not None and document.get("schema") == ASR_PHASE_SCHEMA:
        return document
    summary = service.analysis_overview(root, revision_id)
    if summary is None:
        raise BrowserRuntimeError(f"분석 리비전을 찾지 못했다: {revision_id}")
    row = next((item for item in summary.results if item.asset_id == asset_id), None)
    if row is None or row.document_path is None:
        raise BrowserRuntimeError(
            f"1단계 기록이 없다: {asset_id} — 먼저 1단계(전사) 실제 호출을 마쳐야 한다"
        )
    sibling = (root / row.document_path).parent / "asr_phase.json"
    try:
        body = json.loads(sibling.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise BrowserRuntimeError(f"1단계 기록을 읽지 못했다: {sibling.name} — {error}") from error
    if not isinstance(body, dict) or body.get("schema") != ASR_PHASE_SCHEMA:
        raise BrowserRuntimeError(
            f"1단계 기록이 asr_phase.json 모양이 아니다: {asset_id} — 1단계부터 다시 받아라"
        )
    return body


def _semantic_budget(model: str, estimate: AnalysisSemanticEstimate) -> Any:
    """2단계 승인의 예산 — Claude·Gemini는 견적이 잰 공식 단가 상한을 그대로 묶는다(D54·D86).

    상한 없는 견적(Gemini 단가가 그날 유효하지 않으면 가격 미정)으로는 승인을 만들지 않는다 —
    사람이 금액을 보지 않은 승인이 된다. Luna는 지금처럼 단가 미확인 예산이다.
    """
    from vlogsrt.project.raw.understanding_luna import PRICE_OFFICIAL, AnalysisBudget

    requests = max(1, estimate.window_count)
    if not official_price_model(model):
        return AnalysisBudget(max_luna_requests=requests, max_asr_chunks=1)
    if estimate.request_ceiling_usd is None:
        raise BrowserRuntimeError(
            f"{analysis_model_label(model)} 2단계 범위에 비용 상한이 없다(가격 미정):"
            f" {estimate.source_name} — 단가를 확인해 고친 뒤 다시 승인하세요"
        )
    return AnalysisBudget(
        max_luna_requests=requests,
        max_asr_chunks=1,
        luna_price_status=PRICE_OFFICIAL,
        request_ceiling_usd=Decimal(estimate.request_ceiling_usd),
    )


def mint_luna_approvals(
    service: Any,
    root: Path,
    revision_id: str,
    asset_ids: Sequence[str],
    *,
    on: date | None = None,
) -> dict[str, object]:
    """1단계 기록의 실측 지문으로 2단계 승인을 만든다 — 오디오·시각 각각.

    오디오 원본은 라이브러리 `OnlineAnalysisApproval`(선택 오디오 신원까지),
    무성 원본은 `OnlineVisualAnalysisApproval`(비디오 격자만) — 없는 오디오를
    지어내지 않는다(A4-F1). 창 수·지문은 견적 표가 실측한 값 그대로다.
    """
    from vlogsrt.project.raw.understanding_luna import (
        OnlineAnalysisApproval,
        OnlineVisualAnalysisApproval,
    )

    workflow = service.settings(root)
    estimates = service.analysis_semantic_estimate(root, revision_id)
    measured = {item.asset_id: item for item in estimates}
    approvals: dict[str, object] = {}
    for asset_id in asset_ids:
        estimate = measured.get(asset_id)
        if estimate is None:
            raise BrowserRuntimeError(
                f"2단계 범위가 없는 원본이다: {asset_id} — 1단계(전사)가 끝나야 한다"
            )
        budget = _semantic_budget(workflow.models.luna_model, estimate)
        artifact = _phase_artifact_for(service, root, revision_id, asset_id)
        audio = artifact.get("audio")
        if isinstance(audio, dict) and audio.get("status") != "audio_absent":
            approvals[asset_id] = OnlineAnalysisApproval(
                approved=True,
                source_sha256=str(audio["source_sha256"]),
                audio_stream_index=int(audio["audio_stream_index"]),
                sample_count=int(audio["sample_count"]),
                window_count=estimate.window_count,
                fingerprint=estimate.semantic_fingerprint,
                asr_model=service.source_settings(root, asset_id).models.timing_model,
                luna_model=workflow.models.luna_model,
                budget=budget,
            )
            continue
        video = artifact.get("video")
        if not isinstance(video, dict):
            raise BrowserRuntimeError(f"1단계 기록에 비디오 격자가 없다: {asset_id}")
        approvals[asset_id] = OnlineVisualAnalysisApproval(
            approved=True,
            source_sha256=str(artifact["source_sha256"]),
            frame_count=int(video["frame_count"]),
            window_count=estimate.window_count,
            fingerprint=estimate.semantic_fingerprint,
            luna_model=workflow.models.luna_model,
            budget=budget,
        )
    return approvals


__all__ = [
    "BrowserRuntimeError",
    "ClaudeCredentials",
    "EnvironmentCredentials",
    "STT_BASE_URL_ENV",
    "STT_KEY_ENV",
    "asr_estimate_digest",
    "capture_claude_credentials",
    "capture_gemini_credentials",
    "capture_environment_credentials",
    "live_transport_for_credentials",
    "mint_asr_approvals",
    "mint_luna_approvals",
    "production_live_transport",
    "semantic_estimate_digest",
]
