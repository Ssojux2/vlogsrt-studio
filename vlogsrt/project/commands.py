"""PROTO-5 — `vlogsrt project run`의 실행기. 이 프로토타입의 비용 경계가 사는 자리.

CLI(`cli.py`)는 파싱과 출력 조립만 하고, 이 모듈이 설정을 읽고 자격증명을 풀고
클라이언트를 만들어 러너 세 단계(`prepare_audio` → `analyze_audio` →
`publish_project`)를 순서대로 부른다. 유료 호출로 가는 문은 정확히 두 개다:

- 전사기는 `get_transcriber(cfg, online=online, force_offline=not online)`로 만든다.
  `force_offline`을 빼면 셸의 `VLOGSRT_OFFLINE=0`이 `--offline`을 뚫고
  LiveTranscriber를 만든다(transcribe.py의 오프라인 판정식) — 레거시 `run`이 같은
  이유로 같은 선을 긋는다.
- 비전 재생기는 `_vision_responder`가 고른다(셋 중 하나). 실제(`LiveVisionResponder`)
  는 `--online`일 때만 만들고, 자격증명은 `vlogsrt.project.toml`이 지정한 환경변수
  **이름**에서만 푼다. base URL 값이 비면 `create_vision_client`가 공개 엔드포인트로
  가므로 Google 키가 다른 공급자로 전송된다 — 파이프라인 시작 전에 멈춘다. 오류
  문구에는 이름만 있고 값은 없다. 재생(`--vision-fixtures`)은 네트워크를 모르는
  `FixtureVisionResponder`로, 무플래그는 닫힌 재생기로 — 블록마다 오류 판정이 남는
  지금의 기본값 그대로다.

비용 길이 안전장치(레거시 `_confirm_cost`에 해당하는 것)는 없다 — 비용 추정은
`inspect`의 몫이다. `run`은 임시 명령이다: 최종 표면은 `inspect | cut | approve |
caption`이고 `run`을 두지 않는다(사람 게이트 우회).

`run`의 산출은 언제나 **초안**이다(D35): `EditPlan`에 사람 검토 상태가 없으므로
`accepted` 판정이 임계값을 넘으면 사람 확인 없이 컷이 적용된다. 출력은 그 사실을
"차단 없음"과 구분해 말한다 — `final_accepted`는 승인이 아니라 차단 부재다. 이
모듈은 `cli.py`를 import하지 못한다(계약 §1).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import uuid
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vlogsrt import env, log, transcribe
from vlogsrt.config import Config, load_config
from vlogsrt.project import runner
from vlogsrt.project.config import ProjectConfig, load_project_config
from vlogsrt.project.fcpxml import element_tree, parse_project
from vlogsrt.project.final_caption import (
    build_caption_estimate,
    caption_settings_sha256,
    create_caption_request,
    generate_final_captions,
    measure_caption_request,
)
from vlogsrt.project.media import (
    MediaOverride,
    ResolvedMedia,
    load_media_map,
    resolve_media,
)
from vlogsrt.project.models import ClipInstance
from vlogsrt.project.pricing import official_pricing_snapshot
from vlogsrt.project.review import (
    CandidateAnalysis,
    build_review_plan,
    generate_silence_candidates,
    render_approved_roughcut,
)
from vlogsrt.project.timebase import parse_fcpx_time
from vlogsrt.project.vision.sampling import (
    FrameSampleRequest,
    establish_correspondence,
    sample_frames,
)
from vlogsrt.project.workflow_analysis import (
    AnalysisSettings,
    CandidateSeed,
    CandidateWindowPlan,
    LunaFrame,
    LunaWindow,
    TranscriptEvidence,
    UsageLedger,
    build_analysis_estimate,
    build_luna_payload,
    effective_analysis_settings_sha256,
    eligible_transcript_evidence,
    finalize_candidate,
    luna_cache_key,
    luna_request_sha256,
    luna_usage,
    normalize_luna_response,
    plan_candidate_windows,
    project_transcript_evidence,
    safe_transcript_candidates,
    select_frame_times,
    transcription_usage,
)
from vlogsrt.project.workflow_protocol import (
    AnalysisAttempt,
    AnalysisAuthorization,
    AnalysisEstimate,
    CancellationRequest,
    CaptionAuthorization,
    CaptionEstimate,
    CaptionRequest,
    CutCandidate,
    EditPlan,
    PricingSnapshot,
    ProjectRevision,
    ProtocolMessage,
    RationalRange,
    VisionWindowDecision,
    WorkflowJournal,
    decode_message,
    encode_message,
)

_JOURNAL = "workflow.jsonl"
_INPUT = "input.fcpxml"
_REVISION = "revision.jsonl"
_PRICING = "pricing-snapshot.jsonl"
_ESTIMATE = "analysis-estimate.jsonl"
_ATTEMPTS = "analysis-attempts.jsonl"
_USAGE = "analysis-usage.jsonl"
_CANDIDATES = "candidates.jsonl"
_PLAN = "edit-plan.jsonl"
_SOURCE = "source.json"
_CANCEL = "cancellation.jsonl"
_CAPTION_REQUEST = "caption-request.jsonl"
_CAPTION_ESTIMATE = "caption-estimate.jsonl"
_CAPTION_RESULT = "caption-result.jsonl"


class WorkflowCancelled(RuntimeError):
    """앱이 기록한 CancellationRequest를 단계 경계에서 만났다 — 남은 호출을 하지 않는다."""

    def __init__(self, cancellation: CancellationRequest) -> None:
        super().__init__(
            f"취소 요청({cancellation.reason}, {cancellation.requested_at})을 만났다 — "
            "분석을 중단한다"
        )
        self.cancellation = cancellation


def _check_cancellation(workspace: Path, input_revision: str, attempt_id: str) -> None:
    """API·전사·ffmpeg 단계 사이에만 확인한다 — 취소 뒤 새 호출은 없다.

    앱은 `cancellation.jsonl`에 CancellationRequest를 기록한다(App Group 작업
    폴더와 같은 내용). 앱이 실행 중인 시도 ID를 모를 때 — 사용자 취소 버튼·활성
    프로젝트 변경 — 는 `attempt_id="*"`로 기록하고 그 입력 리비전의 모든 시도에
    적용한다. 캐시는 그냥 둔다 — 취소는 지금 실행만 멈춘다.
    """
    path = workspace / _CANCEL
    if not path.is_file():
        return
    for message in _read_messages(path):
        if not isinstance(message, CancellationRequest):
            raise ValueError(f"취소 기록에 CancellationRequest가 아닌 메시지가 있다: {path}")
        if message.input_revision != input_revision:
            continue
        if message.attempt_id != attempt_id and message.attempt_id != "*":
            continue
        raise WorkflowCancelled(message)


def _media_grant_revision(
    media: Sequence[ResolvedMedia], app_grant_revision: str | None = None
) -> str:
    """허용된 미디어 루트(북마크)와 실제 미디어 해시가 함께 묶인 권한 리비전.

    앱이 스테이징한 `media-grant.json`의 grant revision(북마크 루트의 지문)을
    함께 묶는다 — 같은 FCPXML·미디어라도 앱 쪽 허용 루트가 바뀌면 다른 권한이고,
    비용 추정·승인이 그대로 드리프트로 잡힌다.
    """
    rows = sorted(
        (
            {
                "original_sha256": row.original_sha256,
                "resource": row.resource_scoped_id,
            }
            for row in media
        ),
        key=lambda row: (row["resource"], row["original_sha256"]),
    )
    payload: dict[str, object]
    if app_grant_revision is not None:
        payload = {"app_grant_revision": app_grant_revision, "media": rows}
    else:
        payload = {"media": rows}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _staged_app_grant_revision(workspace: Path) -> str | None:
    """앱이 스테이징한 media-grant.json의 grant revision — 없으면 None(순수 CLI 실행)."""
    path = workspace / "media-grant.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"media-grant.json을 해석할 수 없다: {path}") from exc
    revision = document.get("grant_revision") if isinstance(document, dict) else None
    if not isinstance(revision, str):
        raise ValueError("media-grant.json에 grant_revision이 없다")
    if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("media-grant.json의 grant_revision이 SHA-256이 아니다")
    return revision


def project_command(args: argparse.Namespace) -> int:
    """단계형 명령의 단일 CLI 경계. 진단용 run만 기존 실행기를 유지한다."""
    if args.project_command == "run":
        return project_run(args)
    handlers = {
        "receive": _project_receive,
        "inspect": _project_inspect,
        "analyze": _project_analyze,
        "review": _project_review,
        "roughcut": _project_roughcut,
        "caption": _project_caption,
    }
    try:
        return handlers[args.project_command](args)
    except Exception as exc:  # noqa: BLE001 - CLI는 traceback 없이 단계 실패를 보고한다
        log.error(str(exc))
        return 2


def _project_receive(args: argparse.Namespace) -> int:
    document = parse_project(args.input)
    root = element_tree(document.raw_xml, str(document.info_path))
    projects = root.findall("library/event/project")
    if len(projects) != 1:
        # 한 작업공간은 한 프로젝트다 — 여러 개가 섞이면 승인·리비전이 어느
        # 프로젝트의 것인지 말할 수 없다.
        raise ValueError(f"FCPXML에는 프로젝트가 정확히 하나여야 한다: {len(projects)}개")
    project = projects[0]
    sequence = root.find("library/event/project/sequence")
    if sequence is None:
        raise ValueError("FCPXML에 project/sequence가 없다")
    name = project.get("name", "").strip()
    if not name:
        raise ValueError("FCPXML 프로젝트 이름이 비어 있다")
    uid = (
        project.get("uid", "").strip()
        or hashlib.sha256(f"{document.input_sha256}:project".encode()).hexdigest()
    )
    duration_text = sequence.get("duration")
    if duration_text is None:
        raise ValueError("FCPXML sequence.duration이 없다")
    revision = ProjectRevision(
        input_sha256=document.input_sha256,
        project_uid=uid,
        project_name=name,
        frame_duration=document.frame_duration,
        duration=parse_fcpx_time(duration_text),
    )
    workspace = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / _JOURNAL).exists():
        raise ValueError(f"이미 수신된 작업공간이다: {workspace}")
    shutil.copyfile(document.info_path, workspace / _INPUT)
    _write_json(
        workspace / _SOURCE,
        {
            "input_path": str(document.input_path.resolve()),
            "info_path": str(document.info_path.resolve()),
        },
    )
    WorkflowJournal.create(workspace / _JOURNAL, revision)
    _write_messages(workspace / _REVISION, (revision,))
    _protocol_out(revision)
    return 0


def _project_inspect(args: argparse.Namespace) -> int:
    """네트워크를 열지 않고 공식 가격 스냅샷과 비용 상한을 고정한다."""
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state != "received":
        raise ValueError(f"inspect는 received 상태에서만 가능하다: {journal.state}")
    pricing = official_pricing_snapshot(AnalysisSettings().model)
    estimate = _current_analysis_estimate(args, workspace, journal.revision, pricing)
    _write_messages(workspace / _PRICING, (pricing,))
    _write_messages(workspace / _ESTIMATE, (estimate,))
    _protocol_out(pricing)
    _protocol_out(estimate)
    journal.transition(journal.revision.revision, "estimate_ready")
    return 0


def _current_analysis_estimate(
    args: argparse.Namespace,
    workspace: Path,
    revision: ProjectRevision,
    pricing: PricingSnapshot,
) -> AnalysisEstimate:
    document = parse_project(workspace / _INPUT)
    cfg = load_config()
    project_config = load_project_config(args.project_config)
    media_map = getattr(args, "media_map", None)
    overrides: tuple[MediaOverride, ...] = (
        load_media_map(media_map) if media_map is not None else ()
    )
    media = resolve_media(document, overrides)
    override_payload = [
        {
            "original": str(row.original) if row.original is not None else None,
            "proxy": str(row.proxy) if row.proxy is not None else None,
            "resource_id": row.resource_id,
        }
        for row in overrides
    ]
    override_sha256 = hashlib.sha256(
        json.dumps(override_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    settings = AnalysisSettings()
    fingerprint = effective_analysis_settings_sha256(
        settings,
        engine_config=cfg.to_dict(),
        project_config=project_config.to_dict(),
        media_overrides_sha256=override_sha256,
        source_sha256s=tuple(row.original_sha256 for row in media),
    )
    return build_analysis_estimate(
        document,
        revision,
        settings,
        pricing=pricing,
        media_grant_revision=_media_grant_revision(media, _staged_app_grant_revision(workspace)),
        settings_sha256=fingerprint,
    )


def _project_analyze(args: argparse.Namespace) -> int:
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state not in {"estimate_ready", "authorized", "analyzing"}:
        raise ValueError(
            f"analyze는 estimate_ready/authorized/analyzing 상태에서만 가능하다: {journal.state}"
        )
    pricing = official_pricing_snapshot(AnalysisSettings().model)
    stored_pricing = _read_one(workspace / _PRICING)
    if not isinstance(stored_pricing, PricingSnapshot):
        raise ValueError("pricing-snapshot.jsonl의 메시지가 PricingSnapshot이 아니다")
    if stored_pricing != pricing:
        raise ValueError("기록된 가격 스냅샷이 현재 공식 가격과 다르다 — inspect를 다시 돌린다")
    estimate_message: AnalysisEstimate | None = None
    if bool(args.online) or args.vision_fixtures is not None:
        loaded_estimate = _read_one(workspace / _ESTIMATE)
        if not isinstance(loaded_estimate, AnalysisEstimate):
            raise ValueError("analysis-estimate.jsonl의 메시지가 AnalysisEstimate가 아니다")
        estimate_message = loaded_estimate
        if estimate_message.input_revision != journal.revision.revision:
            raise ValueError("비용 추정의 입력 리비전이 현재 작업과 다르다")
        if estimate_message.pricing_snapshot_revision != pricing.snapshot_revision:
            raise ValueError("비용 추정이 현재 가격 스냅샷에 묶이지 않았다")
        current_estimate = _current_analysis_estimate(args, workspace, journal.revision, pricing)
        if current_estimate != estimate_message:
            raise ValueError("현재 분석 설정·미디어가 승인 전 비용 추정과 다르다")
    if bool(args.online):
        assert estimate_message is not None
        if args.authorization is None:
            raise ValueError("온라인 분석은 --authorization JSONL이 필요하다")
        authorization = _read_one(args.authorization)
        if not isinstance(authorization, AnalysisAuthorization):
            raise ValueError("승인 JSONL이 AnalysisAuthorization이 아니다")
        authorization.require_matches(
            journal.revision.revision,
            estimate_message.estimate_revision,
            pricing.snapshot_revision,
            estimate_message.media_grant_revision,
        )
        if estimate_message.cost_upper_bound is None:
            raise ValueError("온라인 분석 비용 상한이 미설정이다")
    if journal.state == "estimate_ready":
        # 유료 호출은 승인 파일이, 픽스처 재생은 깃발이 분석 착수를 승인한다.
        journal.transition(journal.revision.revision, "authorized")
    attempt_id: str | None = None
    if args.candidates is not None:
        if bool(args.online):
            raise ValueError("외부 --candidates는 온라인 승인 분석에 사용할 수 없다")
        candidates = tuple(_read_candidates(args.candidates, journal.revision.revision))
    else:
        if estimate_message is None:
            raise ValueError("project inspect로 비용 추정을 먼저 고정해야 한다")
        attempt_id = f"attempt-{uuid.uuid4()}"
        started_at = _utc_now()
        _append_message(
            workspace / _ATTEMPTS,
            AnalysisAttempt(
                journal.revision.revision,
                estimate_message.estimate_revision,
                attempt_id,
                "pending",
                started_at,
                None,
            ),
        )
        try:
            _append_message(
                workspace / _ATTEMPTS,
                AnalysisAttempt(
                    journal.revision.revision,
                    estimate_message.estimate_revision,
                    attempt_id,
                    "running",
                    started_at,
                    None,
                ),
            )
            if args.vision_fixtures is not None and not bool(args.online):
                candidates = _replay_analysis(
                    Path(args.vision_fixtures), journal.revision, estimate_message
                )
            else:
                candidates = _produce_analysis_candidates(
                    args, workspace, journal.revision, estimate_message, attempt_id, pricing
                )
        except KeyboardInterrupt:
            _append_message(
                workspace / _ATTEMPTS,
                AnalysisAttempt(
                    journal.revision.revision,
                    estimate_message.estimate_revision,
                    attempt_id,
                    "cancelled",
                    started_at,
                    None,
                ),
            )
            journal.transition(journal.revision.revision, "cancelled")
            raise
        except WorkflowCancelled:
            _append_message(
                workspace / _ATTEMPTS,
                AnalysisAttempt(
                    journal.revision.revision,
                    estimate_message.estimate_revision,
                    attempt_id,
                    "cancelled",
                    started_at,
                    None,
                ),
            )
            journal.transition(journal.revision.revision, "cancelled")
            raise
        except Exception as exc:
            _append_message(
                workspace / _ATTEMPTS,
                AnalysisAttempt(
                    journal.revision.revision,
                    estimate_message.estimate_revision,
                    attempt_id,
                    "failed",
                    started_at,
                    " ".join(str(exc).split()) or exc.__class__.__name__,
                ),
            )
            raise
    if journal.state == "authorized":
        # 외부 작업 프로세스 결과를 전부 검사·저장한 뒤에만 상태를 움직인다.
        _write_messages(workspace / _CANDIDATES, candidates)
        journal.transition(journal.revision.revision, "analyzing")
    else:
        stored = tuple(_read_candidates(workspace / _CANDIDATES, journal.revision.revision))
        if stored != candidates:
            raise ValueError("재개한 분석 후보가 저장된 analyzing 결과와 다르다")
    if attempt_id is not None and estimate_message is not None:
        _append_message(
            workspace / _ATTEMPTS,
            AnalysisAttempt(
                journal.revision.revision,
                estimate_message.estimate_revision,
                attempt_id,
                "succeeded",
                started_at,
                None,
            ),
        )
    journal.transition(journal.revision.revision, "review_required")
    log.out(f"컷 후보 {len(candidates)}건 · 사람 검토 필요")
    return 0


def _project_review(args: argparse.Namespace) -> int:
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state != "review_required":
        raise ValueError(f"review는 review_required 상태에서만 가능하다: {journal.state}")
    candidates = tuple(_read_candidates(workspace / _CANDIDATES, journal.revision.revision))
    plan = build_review_plan(
        journal.revision, candidates, approved_candidate_ids=tuple(args.approve)
    )
    _write_messages(workspace / _PLAN, (plan,))
    _protocol_out(plan)
    return 0


def _project_roughcut(args: argparse.Namespace) -> int:
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state != "review_required":
        raise ValueError(f"roughcut은 review_required 상태에서만 가능하다: {journal.state}")
    candidates = tuple(_read_candidates(workspace / _CANDIDATES, journal.revision.revision))
    plan_message = _read_one(workspace / _PLAN)
    if not isinstance(plan_message, EditPlan):
        raise ValueError("edit-plan.jsonl의 메시지가 EditPlan이 아니다")
    document = parse_project(workspace / _INPUT)
    _require_safe_roughcut_output(workspace, args.out)
    rendered = render_approved_roughcut(document, journal.revision, candidates, plan_message)
    _write_bytes(args.out, rendered.xml_bytes)
    report = args.out.with_suffix(args.out.suffix + ".review.json")
    _write_json(
        report,
        {
            "input_revision": journal.revision.revision,
            "plan_revision": plan_message.plan_revision,
            "project_name": rendered.project_name,
            "removed_candidate_ids": list(rendered.removed_candidate_ids),
        },
    )
    journal.transition(journal.revision.revision, "roughcut_imported")
    log.out(f"roughcut: {args.out}")
    log.out(f"review: {report}")
    return 0


def _project_caption(args: argparse.Namespace) -> int:
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state not in {"roughcut_imported", "caption_estimate_ready"}:
        raise ValueError(
            f"caption은 roughcut_imported/caption_estimate_ready 상태에서만 가능하다: "
            f"{journal.state}"
        )
    # 승인 파일은 상태를 움직이기 전에 검사한다 — 실패가 저널에 흔적을 남기지 않는다.
    authorization: CaptionAuthorization | None = None
    if bool(args.online):
        if args.caption_authorization is None:
            raise ValueError("온라인 자막은 --caption-authorization JSONL이 필요하다")
        loaded = _read_one(args.caption_authorization)
        if not isinstance(loaded, CaptionAuthorization):
            raise ValueError("자막 승인 JSONL이 CaptionAuthorization이 아니다")
        authorization = loaded
    request = create_caption_request(
        journal.revision.revision, args.render_audio, args.final_fcpxml
    )
    cfg = load_config()
    pricing = official_pricing_snapshot()
    audio_seconds = measure_caption_request(request)
    estimate = build_caption_estimate(
        request,
        audio_seconds=audio_seconds,
        pricing=pricing,
        settings_sha256=caption_settings_sha256(cfg),
    )
    if journal.state == "roughcut_imported":
        _write_messages(workspace / _CAPTION_REQUEST, (request,))
        journal.transition(journal.revision.revision, "caption_received")
        _write_messages(workspace / "pricing-caption.jsonl", (pricing,))
        _write_messages(workspace / _CAPTION_ESTIMATE, (estimate,))
        journal.transition(journal.revision.revision, "caption_estimate_ready")
    else:
        # 드리프트 재시도: 이미 고정된 요청·추정과 지금 입력이 같은지만 본다.
        stored_request = _read_one(workspace / _CAPTION_REQUEST)
        if not isinstance(stored_request, CaptionRequest) or stored_request != request:
            raise ValueError("자막 요청이 고정된 추정과 다르다 — 새 작업공간으로 시작한다")
        stored_estimate = _read_one(workspace / _CAPTION_ESTIMATE)
        if not isinstance(stored_estimate, CaptionEstimate) or stored_estimate != estimate:
            raise ValueError("현재 자막 추정이 고정된 추정과 다르다 — 새 작업공간으로 시작한다")
    if authorization is not None:
        authorization.require_matches(estimate)
    journal.transition(journal.revision.revision, "caption_authorized")
    delivery = generate_final_captions(
        request,
        cfg,
        args.out,
        online=bool(args.online),
        confirmed_cost=bool(args.yes),
    )
    _write_messages(workspace / _CAPTION_RESULT, (delivery.result,))
    journal.transition(journal.revision.revision, "caption_ready")
    if delivery.result.srt_path is None:
        log.out("자막 상태: 발화 없음")
    else:
        log.out(f"srt: {delivery.result.srt_path}")
        log.out(f"finder: {delivery.result.srt_path.parent}")
    # 자동 import는 하지 않는다 — 사람이 Finder에서 확인하고 직접 넣는다.
    log.out("Final Cut Pro: File > Import > Captions → Relative to Timeline")
    return 0


def _read_candidates(path: Path, revision: str) -> list[CutCandidate]:
    messages = _read_messages(path)
    candidates: list[CutCandidate] = []
    for message in messages:
        if not isinstance(message, CutCandidate):
            raise ValueError(
                f"후보 JSONL에 CutCandidate 아닌 메시지가 있다: {type(message).__name__}"
            )
        if message.input_revision != revision:
            raise ValueError("후보 JSONL에 다른 입력 리비전이 섞였다")
        candidates.append(message)
    return candidates


def _read_messages(path: Path) -> list[ProtocolMessage]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        raise ValueError(f"JSONL을 읽을 수 없다: {path}") from exc
    return [decode_message(line) for line in lines]


def _read_one(path: Path) -> ProtocolMessage:
    messages = _read_messages(path)
    if len(messages) != 1:
        raise ValueError(f"JSONL에는 메시지가 정확히 하나여야 한다: {path}")
    return messages[0]


def _write_messages(path: Path, messages: tuple[ProtocolMessage, ...]) -> None:
    text = "".join(encode_message(message) for message in messages)
    _write_bytes(path, text.encode())


def _append_message(path: Path, message: ProtocolMessage) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(encode_message(message))
        stream.flush()
        os.fsync(stream.fileno())


def _utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _protocol_out(message: ProtocolMessage) -> None:
    """JSONL은 Rich wrapping·markup을 거치지 않고 stdout에 그대로 쓴다."""
    sys.stdout.write(encode_message(message))
    sys.stdout.flush()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _write_bytes(path, text.encode())


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(payload)
    os.replace(temporary, path)


def _require_safe_roughcut_output(workspace: Path, output: Path) -> None:
    target = output.resolve()
    protected_names = (
        _INPUT,
        _JOURNAL,
        _SOURCE,
        _REVISION,
        _PRICING,
        _ESTIMATE,
        _ATTEMPTS,
        _USAGE,
        _CANDIDATES,
        _PLAN,
        _CANCEL,
        _CAPTION_REQUEST,
        _CAPTION_ESTIMATE,
        _CAPTION_RESULT,
        "pricing-caption.jsonl",
        "vision-decisions.jsonl",
    )
    protected = {(workspace / name).resolve() for name in protected_names}
    source_path = workspace / _SOURCE
    try:
        source = json.loads(source_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"원본 경로 기록을 읽을 수 없다: {source_path}") from exc
    for key in ("input_path", "info_path"):
        value = source.get(key)
        if not isinstance(value, str):
            raise ValueError(f"원본 경로 기록의 {key}가 문자열이 아니다")
        protected.add(Path(value).resolve())
    if target in protected:
        raise ValueError(f"러프컷 출력이 보호된 원본·작업 파일과 같다: {target}")


def _replay_analysis(
    fixtures: Path, revision: ProjectRevision, estimate: AnalysisEstimate
) -> tuple[CutCandidate, ...]:
    """1.1 픽스처의 후보가 같은 창 판정에 정확히 묶였을 때만 재생한다."""
    if estimate.input_revision != revision.revision:
        raise ValueError("픽스처 재생의 비용 추정이 현재 입력과 다르다")
    candidate_path = fixtures / "candidates.jsonl"
    decision_path = fixtures / "decisions.jsonl"
    candidates = tuple(_read_candidates(candidate_path, revision.revision))
    decisions: dict[str, VisionWindowDecision] = {}
    for message in _read_messages(decision_path):
        if not isinstance(message, VisionWindowDecision):
            raise ValueError("판정 픽스처에 VisionWindowDecision이 아닌 메시지가 있다")
        if message.input_revision != revision.revision:
            raise ValueError("판정 픽스처에 다른 입력 리비전이 섞였다")
        if message.window_id in decisions:
            raise ValueError(f"판정 픽스처 창이 중복이다: {message.window_id}")
        decisions[message.window_id] = message
    for candidate in candidates:
        decision = decisions.get(candidate.window_id)
        if decision is None:
            raise ValueError(f"후보의 Luna 판정이 없다: {candidate.candidate_id}")
        if (
            decision.choice != "propose_cut"
            or decision.candidate_id != candidate.candidate_id
            or decision.decision_revision != candidate.decision_revision
        ):
            raise ValueError(f"후보가 Luna 판정과 다르다: {candidate.candidate_id}")
    build_review_plan(revision, candidates, approved_candidate_ids=())
    return candidates


def _produce_analysis_candidates(
    args: argparse.Namespace,
    workspace: Path,
    revision: ProjectRevision,
    estimate: AnalysisEstimate,
    attempt_id: str,
    pricing: PricingSnapshot,
) -> tuple[CutCandidate, ...]:
    """기존 소스 전사/VAD를 재사용하고 안전 seed만 Luna에 제공한다."""
    settings = AnalysisSettings()
    if estimate.model != settings.model:
        raise ValueError("승인된 비용 추정과 현재 Luna 모델이 다르다")
    if bool(args.online) == (args.vision_fixtures is not None):
        raise ValueError("분석은 --online 또는 --vision-fixtures 하나만 선택해야 한다")
    cfg = load_config()
    project_config = load_project_config(args.project_config)
    overrides: tuple[MediaOverride, ...] = (
        load_media_map(args.media_map) if args.media_map is not None else ()
    )
    ledger = UsageLedger(Decimal(estimate.cost_upper_bound or "0"))
    transcriber = _BudgetedTranscriber(
        transcribe.get_transcriber(
            cfg, online=bool(args.online), force_offline=not bool(args.online)
        ),
        estimate.transcription_seconds,
    )
    _check_cancellation(workspace, revision.revision, attempt_id)
    prepared = runner.prepare_audio(
        workspace / _INPUT,
        workspace / "artifacts",
        overrides=overrides,
        config=cfg,
        project_config=project_config,
    )
    if prepared.document.input_sha256 != revision.input_sha256:
        raise ValueError("분석 준비 문서가 승인된 입력 리비전과 다르다")
    _check_cancellation(workspace, revision.revision, attempt_id)
    analyzed = runner.analyze_audio(prepared, transcriber=transcriber)
    if transcriber.used > 0:
        # 실제로 보낸 whisper 오디오만 기록한다 — 예측이 아니라 측정값이다.
        usage = transcription_usage(
            input_revision=revision.revision,
            attempt_id=attempt_id,
            model=pricing.whisper_model,
            audio_seconds=transcriber.used,
            per_minute=pricing.whisper_per_minute,
        )
        _append_message(workspace / _USAGE, usage)
        ledger.record(usage)
    seeds = _analysis_seeds(analyzed, revision, settings)
    clip_ranges = {
        item.instance_id: RationalRange(item.offset, item.offset + item.duration)
        for item in prepared.document.storyline
        if isinstance(item, ClipInstance)
    }
    windows = plan_candidate_windows(
        seeds,
        input_revision=revision.revision,
        clip_ranges=clip_ranges,
        window_seconds=settings.window_seconds,
    )
    if len(windows) > estimate.max_windows:
        raise ValueError("실제 Luna 창 수가 승인된 상한을 넘는다")
    client = None
    if bool(args.online):
        client = transcribe.create_vision_client(
            api_key=env.require_api_key(force=True), base_url=env.base_url()
        )
    decisions: list[VisionWindowDecision] = []
    candidates: list[CutCandidate] = []
    used_images = 0
    for planned in windows:
        _check_cancellation(workspace, revision.revision, attempt_id)
        decision, image_count = _decide_window(
            planned,
            analyzed,
            revision.revision,
            settings,
            project_config,
            estimate.settings_sha256,
            workspace,
            client=client,
            fixtures=Path(args.vision_fixtures) if args.vision_fixtures is not None else None,
            attempt_id=attempt_id,
            pricing=pricing,
            ledger=ledger,
        )
        used_images += image_count
        if used_images > estimate.max_images:
            raise ValueError("실제 Luna 이미지 수가 승인된 상한을 넘는다")
        decisions.append(decision)
        for seed in planned.candidates:
            candidate = finalize_candidate(seed, decision)
            if candidate is not None:
                candidates.append(candidate)
    _write_messages(workspace / "vision-decisions.jsonl", tuple(decisions))
    return tuple(candidates)


class _BudgetedTranscriber:
    def __init__(self, delegate: transcribe.Transcriber, limit: Fraction) -> None:
        self.delegate = delegate
        self.limit = limit
        self.used = Fraction()

    def transcribe(
        self, job: transcribe.TranscriptionJob, cfg: Config
    ) -> list[transcribe.ChunkResponse]:
        requested = sum((Fraction(str(chunk.duration)) for chunk in job.chunks), start=Fraction())
        if self.used + requested > self.limit:
            raise RuntimeError("전사 요청 길이가 승인된 비용 상한을 넘는다")
        self.used += requested
        return self.delegate.transcribe(job, cfg)


def _analysis_seeds(
    analyzed: runner.AnalyzedAudio, revision: ProjectRevision, settings: AnalysisSettings
) -> tuple[CandidateSeed, ...]:
    document = analyzed.prepared.document
    guard_analysis = {
        guard.instance_id: CandidateAnalysis(
            transcript_status="success" if guard.status == "success" else "error",
            vision_status="success",
            unique_speech_frames=(),
            motion_continuity_frames=(),
            confidence="0",
        )
        for guard in analyzed.instance_guards
    }
    silence = generate_silence_candidates(
        document,
        revision,
        analyzed.instance_guards,
        locked_instance_ids=frozenset(analyzed.locked_instance_ids),
        analysis=guard_analysis,
        config=analyzed.prepared.project_config,
    )
    seeds = [
        CandidateSeed(
            row.candidate_id,
            row.window_id,
            row.clip_instance_id,
            row.source_range,
            row.timeline_range,
            row.reason,
            tuple(item for item in row.evidence if not item.startswith("vision:")),
        )
        for row in silence.candidates
    ]
    clips = {
        item.instance_id: item for item in document.storyline if isinstance(item, ClipInstance)
    }
    resources = {row.scoped_id: row for row in document.resources}
    guards = {row.instance_id: row for row in analyzed.instance_guards}
    locked = set(analyzed.locked_instance_ids) | {
        row.instance_id for row in analyzed.unavailable_instances
    }
    unresolved = set(analyzed.source_captions.unresolved_cue_ids)
    transcript_rows: list[TranscriptEvidence] = []
    for cue in analyzed.source_captions.placed_cues:
        clip = clips.get(cue.instance_id)
        guard = guards.get(cue.instance_id)
        if clip is None or guard is None or guard.status != "success" or cue.instance_id in locked:
            continue
        resource = resources[clip.resource_scoped_id]
        evidence = project_transcript_evidence(
            utterance_id=cue.cue_id,
            clip_instance_id=cue.instance_id,
            source_range=RationalRange(cue.source_start, cue.source_end),
            text=cue.text,
            complete=cue.cue_id not in unresolved,
            clip_source_start=clip.start,
            clip_timeline_start=clip.offset,
            resource_start=resource.start,
            frame_duration=document.frame_duration,
        )
        if evidence is not None and evidence.source_range.end < clip.start + clip.duration:
            transcript_rows.append(evidence)
    transcript = eligible_transcript_evidence(
        transcript_rows,
        successful_instance_ids={
            row.instance_id for row in analyzed.instance_guards if row.status == "success"
        },
        locked_instance_ids=locked,
    )
    seeds.extend(safe_transcript_candidates(transcript, revision.revision, settings))
    valid = [
        seed
        for seed in seeds
        if (clip := clips.get(seed.clip_instance_id)) is not None
        and clip.start
        < seed.source_range.start
        < seed.source_range.end
        < clip.start + clip.duration
    ]
    by_id: dict[str, CandidateSeed] = {}
    for seed in valid:
        by_id.setdefault(seed.candidate_id, seed)
    return tuple(by_id.values())


def _decide_window(
    planned: CandidateWindowPlan,
    analyzed: runner.AnalyzedAudio,
    input_revision: str,
    settings: AnalysisSettings,
    project_config: ProjectConfig,
    cache_settings_sha256: str,
    workspace: Path,
    *,
    client: object | None,
    fixtures: Path | None,
    attempt_id: str,
    pricing: PricingSnapshot,
    ledger: UsageLedger,
) -> tuple[VisionWindowDecision, int]:
    document = analyzed.prepared.document
    clip = next(
        item
        for item in document.storyline
        if isinstance(item, ClipInstance) and item.instance_id == planned.clip_instance_id
    )
    media = next(
        row for row in analyzed.prepared.media if row.resource_scoped_id == clip.resource_scoped_id
    )
    resource = next(row for row in document.resources if row.scoped_id == clip.resource_scoped_id)
    window_start = planned.timeline_range.start
    window_end = planned.timeline_range.end
    source_window = RationalRange(
        clip.start + window_start - clip.offset,
        clip.start + window_end - clip.offset,
    )
    correspondence = establish_correspondence(media)
    file_origin = correspondence.original.presentation_origin
    file_window = RationalRange(
        file_origin + source_window.start - resource.start,
        file_origin + source_window.end - resource.start,
    )
    file_candidates = tuple(
        RationalRange(
            file_origin + row.source_range.start - resource.start,
            file_origin + row.source_range.end - resource.start,
        )
        for row in planned.candidates
    )
    times = select_frame_times(
        file_window,
        file_candidates,
        frame_duration=document.frame_duration,
        grid_anchor=file_origin,
        max_images=settings.max_images,
    )
    requests = tuple(
        FrameSampleRequest(
            hashlib.sha256(f"{planned.window_id}\0{time}".encode()).hexdigest()[:24],
            media.asset_content_id,
            time,
        )
        for time in times
    )
    samples = sample_frames(
        media,
        requests,
        analyzed.prepared.frames_dir,
        replace(project_config, vision_long_edge=settings.image_long_edge),
    )
    frames = tuple(
        LunaFrame(
            sample.frame_id,
            sample.image_path.read_bytes(),
            sample.image_sha256,
            source_time=sample.source_time,
            timeline_time=window_start + (sample.source_time - file_window.start),
        )
        for sample in samples
    )
    window = LunaWindow(
        input_revision=input_revision,
        window_id=planned.window_id,
        candidate_ids=tuple(row.candidate_id for row in planned.candidates),
        frame_ids=tuple(frame.frame_id for frame in frames),
        transcript_context=_window_transcript(analyzed, window_start, window_end),
        source_hashes=(media.original_sha256,),
    )
    payload = build_luna_payload(
        window, planned.candidates, frames, max_output_tokens=settings.window_output_tokens
    )
    request_sha256 = luna_request_sha256(window, planned.candidates, frames)
    key = luna_cache_key(
        prompt_revision=settings.prompt_revision,
        model=settings.model,
        settings_sha256=cache_settings_sha256,
        request_sha256=request_sha256,
    )
    cache_path = workspace / "artifacts" / "luna-cache" / f"{key}.json"
    from_cache = cache_path.is_file()
    raw: object
    if from_cache:
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    elif fixtures is not None:
        fixture = fixtures / f"{planned.window_id}.luna.json"
        if not fixture.is_file():
            raise ValueError(f"Luna 픽스처가 없다: {fixture}")
        raw = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        try:
            raw = transcribe.request_luna_vision(
                client, model=settings.model, payload=payload, online=True
            )
        except Exception as exc:  # noqa: BLE001 - API 오류는 이 창을 keep으로 닫는다
            raw = exc
    if isinstance(raw, dict) and not from_cache:
        _write_json(cache_path, raw)
        usage = luna_usage(window=window, raw=raw, pricing=pricing, attempt_id=attempt_id)
        if usage is not None:
            # 실 Responses 사용량을 먼저 남기고, 상한을 넘었으면 다음 호출을 끊는다.
            _append_message(workspace / _USAGE, usage)
            ledger.record(usage)
    return (
        normalize_luna_response(window, raw if isinstance(raw, (dict, BaseException)) else {}),
        len(frames),
    )


def _window_transcript(analyzed: runner.AnalyzedAudio, start: Fraction, end: Fraction) -> str:
    rows = [
        cue.text
        for cue in analyzed.source_captions.placed_cues
        if analyzed.prepared.document.sequence_tc_start + cue.timeline_start < end
        and start < analyzed.prepared.document.sequence_tc_start + cue.timeline_end
    ]
    return " / ".join(rows) if rows else "전사 없음(VAD 무음 후보)"


def project_run(args: argparse.Namespace) -> int:
    """`vlogsrt project run` — 세 단계를 잇고 출판 기록으로 종료 코드를 정한다.

    실패는 한 줄 오류와 종료 코드 2로 닫는다. 레거시 `main()`의 RuntimeError→1에
    흘러가면 막힌 패키지(1)와 실패(2)의 구분이 사라진다 — 집행 지점은 여기다.
    `KeyboardInterrupt`는 잡지 않는다.
    """
    try:
        return _run_pipeline(args)
    except Exception as exc:  # noqa: BLE001 - 실패는 2의 한 줄 로그로 닫는다
        log.error(str(exc))
        return 2


def _run_pipeline(args: argparse.Namespace) -> int:
    cfg = load_config()
    project_config = load_project_config(args.project_config)
    overrides: tuple[MediaOverride, ...] = (
        load_media_map(args.media_map) if args.media_map is not None else ()
    )
    online = bool(args.online)
    transcriber = transcribe.get_transcriber(cfg, online=online, force_offline=not online)
    vision = _vision_responder(args, project_config)
    prepared = runner.prepare_audio(
        args.input,
        args.out / "artifacts",
        overrides=overrides,
        config=cfg,
        project_config=project_config,
    )
    analyzed = runner.analyze_audio(prepared, transcriber=transcriber)
    published = runner.publish_project(
        analyzed,
        vision=vision,
        destination=args.out / "package",
        dtd_path=args.dtd,
    )
    return _report(published, dtd_path=args.dtd, vision_fixtures=fixtures_of(args))


def _vision_responder(
    args: argparse.Namespace, project_config: ProjectConfig
) -> transcribe.VisionResponder:
    """재생기 선택 — 비전 비용 경계는 이 문 하나다(V1).

    `--online`이면 실제(유료), `--vision-fixtures`면 픽스처 재생, 둘 다 없으면
    닫힘 — 블록마다 오류 판정으로 막히는 기본값 그대로다. 동시 지정은 파이프라인
    시작 전에 닫는다: 재생 판정이 유료 호출과 섞이면 어느 쪽 기록도 믿을 수 없다.
    """
    fixtures = getattr(args, "vision_fixtures", None)
    if args.online and fixtures is not None:
        raise RuntimeError(
            "--online과 --vision-fixtures는 함께 쓸 수 없다 — 유료 호출과 재생이 섞이면"
            " 기록을 믿을 수 없다"
        )
    if args.online:
        return transcribe.LiveVisionResponder(client=_vision_client(project_config))
    if fixtures is not None:
        return transcribe.FixtureVisionResponder(namespace=Path(fixtures))
    return transcribe.ClosedVisionResponder()


def _vision_client(project_config: ProjectConfig) -> object:
    """비전 클라이언트 — 자격증명은 설정이 지정한 이름에서만 푼다."""
    api_key, base_url = _vision_credentials(project_config)
    return transcribe.create_vision_client(api_key=api_key, base_url=base_url)


def _vision_credentials(project_config: ProjectConfig) -> tuple[str, str]:
    """`--online`의 비전 자격증명 — STT(`env.require_api_key`)와 갈라진 이름에서.

    base URL 값이 비면 `create_vision_client`가 공개 엔드포인트로 간다 — 기본
    모델이 Gemini이므로 Google 키가 다른 공급자로 전송된다. 키와 마찬가지로
    파이프라인 시작 전에 멈춘다. 문구에 값은 없다.
    """
    key_name = project_config.vision_api_key_env
    url_name = project_config.vision_base_url_env
    api_key = env.get(key_name).strip()
    if not api_key:
        raise RuntimeError(
            f"비전 키 환경변수가 비어 있다: {key_name} — --online은 키 값이 필요하다"
        )
    base_url = env.get(url_name).strip()
    if not base_url:
        raise RuntimeError(
            f"비전 base URL 환경변수가 비어 있다: {url_name} — 값이 없으면"
            " 공개 엔드포인트로 가고 키가 다른 공급자로 전송된다"
        )
    return api_key, base_url


def fixtures_of(args: argparse.Namespace) -> Path | None:
    """재생 픽스처 디렉터리 — 보고가 쓸 값만 꺼낸다(선택 로직은 `_vision_responder`)."""
    value = getattr(args, "vision_fixtures", None)
    return Path(value) if value is not None else None


def _report(
    published: runner.PublishedProject, *, dtd_path: Path | None, vision_fixtures: Path | None
) -> int:
    """출판된 기록에서 종료 코드를 정한다 — `final_accepted`는 review.json의 것이다.

    `ExportPackage`·`PublishedProject`에는 그 값이 없다(경로뿐). `export`의 내부
    함수로 재유도하지 않는다 — 출판된 기록이 정본이다. 재생기가 쓰였으면 그 사실을
    결과보다 먼저 한 줄로 남긴다 — 진짜 판정으로 읽히면 안 되기 때문이다.
    """
    review = json.loads(published.package.review.read_text(encoding="utf-8"))
    final_accepted = bool(review["final_accepted"])
    blockers = list(review["final_blockers"])
    removed = len(published.plan.removed)
    if vision_fixtures is not None:
        log.out(f"비전 판정 재생(픽스처 {vision_fixtures}) — 진짜 판정이 아니고 유료 호출은 0회다")
    if removed:
        # 진단(warn)이 아니라 실행 결과다 — 로그 레벨에 억제되면 안 되므로 stdout으로.
        log.out(f"자동 컷 {removed}건 — 사람 검토 없음(초안)")
    if final_accepted:
        log.out("최종 판정: 차단 없음")
    else:
        log.error(f"최종 판정: 차단 — {blockers}")
    _report_analysis_failures(review)
    log.out(f"리비전: {published.package.fcpxml.parent}")
    log.out(f"판정 {len(published.decisions)}개 · 제거 구간 {removed}건")
    if dtd_path is None:
        log.out("DTD 대조 안 함")
    return 0 if final_accepted else 1


def _report_analysis_failures(review: dict) -> None:
    """분석 실패를 눈에 띄게 — 개수와 각 이유를 log.out으로 낸다(PROTO-6b).

    `analysis_failures`가 있는 판정이 '성공, 자막 0장'으로 읽히면 안 된다 —
    초안 경고와 같은 이유로 억제 불가 채널(stdout)로 나간다. 문장의 줄바꿈은
    한 줄로 접는다 — 원인 문장의 정본은 review.json에 있다.
    """
    failures = review.get("analysis_failures") or []
    if not failures:
        return
    log.out(f"분석 실패 {len(failures)}건 — 이유:")
    for row in failures:
        sentences = (*row.get("review", ()), *row.get("source_review", ()))
        folded = [" ".join(sentence.split()) for sentence in sentences]
        shown = " / ".join(folded) or "(이유 문장 없음)"
        log.out(f"  · {row['instance_id'][:12]}… ({row['source_id'][:12]}…): {shown}")
