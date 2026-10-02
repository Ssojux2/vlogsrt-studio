"""브라우저 실제 호출 생산 transport — 승인된 행동에서 늦게 만든다(A4 생산 갭).

주입 경계(`LiveAnalysisTransport`)는 시험·루트 가짜 브라우저의 것이다. 일반
실행(`ui.app`)은 아무것도 주입받지 않으므로 그대로는 실제 호출 단추가 전부
정직한 오류로 끝난다 — 이 모듈이 그 갭을 **사람이 승인한 행동 안에서만** 채운다:

- 자격증명은 **서버 환경만** 본다(`os.environ` 직접 읽기). `.env` 로더
  (`vlogsrt.env`)를 절대 지나지 않는다 — 파일을 열지 않고, 값이나 자리를
  화면·로그에도 내보내지 않는다(§14.3).
- 전송 경계는 이미 받아들인 정본뿐이다: 내부 ASR는 `LiveTranscriber`
  (`vlogsrt/transcribe`, API 호출의 유일한 곳), Luna는 `create_vision_client`.
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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from vlogsrt.config import Config
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    LiveAnalysisTransport,
    asr_settings_fingerprint,
)
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.models import AnalysisEstimate, AnalysisSemanticEstimate
from vlogsrt.transcribe import LiveTranscriber, create_vision_client

STT_KEY_ENV = "OPENAI_API_KEY"
"""STT·Luna가 함께 쓰는 서버 환경 키 이름 — 값은 화면·로그에 절대 나오지 않는다."""

STT_BASE_URL_ENV = "OPENAI_BASE_URL"
"""선택 공개 엔드포인트 — 비면 공개 엔드포인트 그대로(전송 경계가 못 박는다)."""


class BrowserRuntimeError(RuntimeError):
    """생산 transport를 만들 수 없다 — 사람이 고칠 수 있는 이유로만."""

    def __init__(self, message: str) -> None:
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class EnvironmentCredentials:
    """서버 **시작 환경**에서 찍은 자격증명 — 값은 화면·로그에 절대 나오지 않는다."""

    api_key: str
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
) -> LiveAnalysisTransport:
    """찍아 둔 자격증명으로 생산 transport를 만든다 — 정본 전송 경계만 쓴다.

    이 함수를 부르는 것만으로는 아무 호출도 일어나지 않는다(클라이언트 생성은
    SDK 객체일 뿐이다). 첫 전송은 승인 공급자가 돌려준 승인을 게이트가 검증한
    뒤에야 시작된다(§14.3).
    """
    return LiveAnalysisTransport(
        sender=LiveTranscriber(api_key=credentials.api_key, base_url=credentials.base_url),
        cfg=Config(),
        client=create_vision_client(api_key=credentials.api_key, base_url=credentials.base_url),
        asr_approvals=asr_approvals,
        luna_approvals=luna_approvals,
        on=on,
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
    )


def caption_transport_providers(
    credentials: EnvironmentCredentials,
) -> tuple[Any, Any]:
    """최종 자막 실제 실행의 생산 제공자 — 시간·문구가 같은 정본 전송 경계를 지난다.

    화면 4의 승인된 실행에서만 만든다. 제공자 클래스는 받아들인 A7 어댑터
    (`caption_provider`) 그대로 — 여기서 새 전송 길을 만들지 않는다. 호출 0회:
    객체를 만들 뿐이고 첫 전송은 실행 문(승인·환경·네트워크)이 연 뒤에야
    일어난다(§14.3).
    """
    from vlogsrt.project.raw.caption_provider import (
        TransportTextProvider,
        TransportTimingProvider,
    )

    sender = LiveTranscriber(api_key=credentials.api_key, base_url=credentials.base_url)
    cfg = Config()
    return (
        TransportTimingProvider(sender=sender, cfg=cfg),
        TransportTextProvider(sender=sender, cfg=cfg),
    )


# ─────────────────────────────────────────────────────────────
#  승인 주조(mint) — 화면이 잰 견적 그대로에서
# ─────────────────────────────────────────────────────────────


def asr_estimate_digest(estimates: Sequence[AnalysisEstimate]) -> str:
    """1단계 견적 표의 지문 — 사람이 본 값과 시작 때의 값이 같은지의 기준.

    표시 전용 문구(note)는 빼고, 승인이 묶는 측정값(출처·스트림·청크·창·모델
    회신 수)만 묶는다 — 문구만 바뀌었다고 승인을 다시 받게 하지 않는다.
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
        )
        for item in estimates
    )
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def semantic_estimate_digest(estimates: Sequence[AnalysisSemanticEstimate]) -> str:
    """2단계 견적 표의 지문 — 실측 지문·창·이미지·단계를 묶는다."""
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
        )
        for item in estimates
    )
    body = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def mint_asr_approvals(
    service: Any,
    root: Path,
    asset_ids: Sequence[str] | None,
    *,
    understanding: RawUnderstandingSettings | None = None,
    on: date | None = None,
) -> dict[str, AsrPhaseApproval]:
    """견적의 측정값으로 1단계 승인을 만든다 — 전사 전에 알 수 있는 값만.

    무성 원본은 승인을 만들지 않는다(ASR 작업 자체가 없다, §7.3). 청크 예산은
    견적이 잰 청크 수 그대로다 — 실행이 그보다 많이 자르면 게이트가 전송 전에
    닫는다. 전송 순간의 재검사는 `ApprovedPhaseAsrProvider`가 맡는다.
    """
    settings = understanding or RawUnderstandingSettings()
    workflow = service.settings(root)
    estimates = service.analysis_estimate(root, asset_ids, settings)
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
        approvals[estimate.asset_id] = AsrPhaseApproval(
            approved=True,
            source_sha256=asset.content_sha256,
            audio_stream_index=int(estimate.audio_stream_index or 0),
            chunk_budget=max(1, int(estimate.asr_chunk_count)),
            settings_fingerprint=asr_settings_fingerprint(settings, workflow),
            timing_model=workflow.models.timing_model,
            approved_on=(on or date.today()).isoformat(),
        )
    return approvals


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
        AnalysisBudget,
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
                f"2단계 견적이 없는 원본이다: {asset_id} — 1단계(전사)가 끝나야 한다"
            )
        budget = AnalysisBudget(max_luna_requests=max(1, estimate.window_count), max_asr_chunks=1)
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
                asr_model=workflow.models.timing_model,
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
    "EnvironmentCredentials",
    "STT_BASE_URL_ENV",
    "STT_KEY_ENV",
    "asr_estimate_digest",
    "caption_transport_providers",
    "capture_environment_credentials",
    "live_transport_for_credentials",
    "mint_asr_approvals",
    "mint_luna_approvals",
    "production_live_transport",
    "semantic_estimate_digest",
]
