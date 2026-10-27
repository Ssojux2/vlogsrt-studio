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
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from fractions import Fraction
from pathlib import Path

from vlogsrt import env, log, oscompat, transcribe
from vlogsrt.config import Config, load_config
from vlogsrt.project import request_accounting, runner, topology
from vlogsrt.project.clips import collect_clip_paths, probe_clips, synthesize_fcpxml
from vlogsrt.project.config import ProjectConfig, load_project_config
from vlogsrt.project.fcpxml import element_tree, parse_project
from vlogsrt.project.final_caption import (
    ValidatedCaptionInput,
    build_caption_estimate,
    caption_settings_sha256,
    create_caption_request,
    generate_final_captions,
    load_caption_origin,
    measure_caption_request,
    validate_caption_request,
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
    aggregate_analysis_outcome,
    build_analysis_estimate,
    build_luna_payload,
    effective_analysis_settings_sha256,
    eligible_transcript_evidence,
    error_window_decision,
    finalize_candidate,
    luna_cache_key,
    luna_request_sha256,
    normalize_luna_response,
    plan_candidate_windows,
    project_transcript_evidence,
    safe_transcript_candidates,
    select_frame_times,
    skipped_window_decision,
)
from vlogsrt.project.workflow_protocol import (
    AnalysisAttempt,
    AnalysisAuthorization,
    AnalysisEstimate,
    AnalysisProgress,
    AnalysisResult,
    CancellationRequest,
    CaptionAttempt,
    CaptionAttemptStatus,
    CaptionAuthorization,
    CaptionEstimate,
    CaptionJournal,
    CaptionRequest,
    CaptionResult,
    CutCandidate,
    EditPlan,
    PricingSnapshot,
    ProgressStage,
    ProgressStatus,
    ProjectRevision,
    ProtocolMessage,
    RationalRange,
    UpstreamStages,
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
# 공유 받기로 시작한 자막 job의 스테이징·기록 이름(S4A) — 러프컷 workflow와 자리가
# 다르다: input.fcpxml도 workflow.jsonl도 없고, 첫 줄이 CaptionRequest인 자막
# 원장(caption.jsonl)이 상태를 소유한다.
_CAPTION_JOURNAL = "caption.jsonl"
_CAPTION_ORIGIN = "caption-origin.json"
_CAPTION_ATTEMPTS = "caption-attempts.jsonl"
_CAPTION_AUTHORIZATION = "caption-authorization.jsonl"
_CAPTION_PRICING = "pricing-caption.jsonl"
_FINAL_AUDIO = "final.wav"
_FINAL_XML = "final.fcpxml"
_ANALYSIS_RESULT = "analysis-result.jsonl"
_DECISIONS = "vision-decisions.jsonl"
_PROGRESS = "analysis-progress.jsonl"


class WorkflowCancelled(BaseException):
    """앱이 기록한 CancellationRequest를 호출 직전 게이트에서 만났다 — 남은 외부 호출이 없다.

    `BaseException`인 이유가 중요하다: 전사 경로는 실패를 상태 기록으로 바꿔 삼키는
    `except Exception` 층들(`transcribe_source`·`transcribe_outcome`·경계 전사)을 지나
    내려가는데, 취소는 어느 소스·경계의 실패가 아니라 실행 전체를 멈추는 신호다.
    `KeyboardInterrupt`와 같은 부류로 취급한다 — 잡는 곳은 상태를 `cancelled`로
    닫는 `_project_analyze`와 종료 코드를 정하는 `project_command`뿐이다.
    """

    def __init__(self, cancellation: CancellationRequest) -> None:
        super().__init__(
            f"취소 요청({cancellation.reason}, {cancellation.requested_at})을 만났다 — "
            "분석을 중단한다"
        )
        self.cancellation = cancellation


_ENGINE_JOB_DIR = "VLOGSRT_ENGINE_JOB_DIR"
# 엔진 진입점이 잠금 소유자만 아는 호출 신원을 알린다(S2A-3) — 중복(3)으로 물러나는
# 호출은 이 값을 전혀 못 본다. 진행 스냅샷의 invocation_id와 묶여, 지는 쪽 요청의
# 릴레이가 이긴 쪽 호출의 진행을 흉내 내는 길을 닫는다.
_ENGINE_JOB_ID_ENV = "VLOGSRT_ENGINE_JOB_ID"
_ENGINE_INVOCATION_ENV = "VLOGSRT_ENGINE_INVOCATION_ID"


def _engine_job_root(workspace: Path) -> Path | None:
    """엔진이 알린 job 루트 — 없으면(직접 CLI) 진행은 workspace에만 남는다.

    루트와 job 신원 **둘 다** 와야 하고, 루트는 **이 workspace의 부모**여야 한다:
    남아 있거나 잘못된 환경값이 남의 job에 진행을 쓰는 길을 구성요소 경계로
    닫는다(무경계 전역 출력 금지, S2A-3). 비교는 정준 경로로 한다.
    """
    root = env.get(_ENGINE_JOB_DIR).strip()
    job_id = env.get(_ENGINE_JOB_ID_ENV).strip()
    if not root or not job_id:
        return None
    path = Path(root)
    if not path.is_dir() or path.resolve() != workspace.parent.resolve():
        return None
    return path


class _ProgressPublisher:
    """시도 하나의 진행 경계를 원자 스냅샷 한 장으로 남긴다(S2A-3).

    두 자리에 쓴다: workspace(기록의 정본)와 — 엔진이 job 루트를 알린 경우 —
    job 루트(앱이 실행 중 읽는 자리). 발행 실패는 시도를 망치지 않는다: 진행은
    관측이지 결과가 아니므로 경고만 남긴다. 오류 문면은 절대 실지 않는다 —
    실패의 정본은 시도 기록(attempts)이 이미 지워 담고 있다.
    """

    def __init__(
        self,
        workspace: Path,
        input_revision: str,
        estimate_revision: str,
        attempt_id: str,
    ) -> None:
        self._workspace = workspace
        self._input_revision = input_revision
        self._estimate_revision = estimate_revision
        self._attempt_id = attempt_id
        self._job_root = _engine_job_root(workspace)
        self._job_id = env.get(_ENGINE_JOB_ID_ENV).strip() or None
        self._invocation_id = env.get(_ENGINE_INVOCATION_ENV).strip() or None
        self._stage: ProgressStage = "preparation"

    def boundary(
        self,
        stage: ProgressStage,
        *,
        status: ProgressStatus = "running",
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        self._stage = stage
        self._publish(stage, status=status, completed=completed, total=total)

    def terminate(self, status: ProgressStatus) -> None:
        """실패·취소 — 지금 단계에서 종료 상태로만 닫는다(계수·문면 없이)."""
        self._publish(self._stage, status=status)

    def _publish(
        self,
        stage: ProgressStage,
        *,
        status: ProgressStatus,
        completed: int | None = None,
        total: int | None = None,
    ) -> None:
        message = AnalysisProgress.create(
            input_revision=self._input_revision,
            estimate_revision=self._estimate_revision,
            attempt_id=self._attempt_id,
            job_id=self._job_id,
            invocation_id=self._invocation_id,
            stage=stage,
            status=status,
            completed=completed,
            total=total,
            recorded_at=_utc_now(),
        )
        destinations = [self._workspace / _PROGRESS]
        if self._job_root is not None:
            destinations.append(self._job_root / _PROGRESS)
        for path in destinations:
            try:
                _write_bytes(path, encode_message(message).encode())
            except Exception:  # noqa: BLE001 - 진행 발행 실패는 실행 종료를 가리지 않는다
                log.warn(f"진행 스냅샷을 남기지 못했다: {path.name}")


def _cancellation_paths(workspace: Path) -> tuple[Path, ...]:
    """엔진이 취소 기록을 찾는 경로 — workspace와 엔진 작업 폴더 루트.

    앱(`CancellationStore`)은 App Group job 루트와 개발 외부 job 루트에 기록한다.
    엔진 진입점(`vlogsrt-engine.py` — 개발 래퍼가 위임한다)이 그 루트를
    `_ENGINE_JOB_DIR`로 알려준다. 시작할 때 한 번 복사만 하면 실행 중 기록이
    유실되므로 확인마다 둘 다 읽는다 — workspace가 생기기 전에 기록돼도 잃히지
    않는다.
    """
    paths = [workspace / _CANCEL]
    root = env.get(_ENGINE_JOB_DIR).strip()
    if root:
        staged = Path(root) / _CANCEL
        if staged != paths[0]:
            paths.append(staged)
    return tuple(paths)


def _check_cancellation(
    workspace: Path, input_revision: str, attempt_id: str, input_sha256: str = ""
) -> None:
    """API·전사·ffmpeg 단계 사이에만 확인한다 — 취소 뒤 새 호출은 없다.

    앱은 `cancellation.jsonl`에 CancellationRequest를 기록한다(App Group 작업
    폴더와 같은 내용). 앱이 실행 중인 시도 ID를 모를 때 — 사용자 취소 버튼·활성
    프로젝트 변경 — 는 `attempt_id="*"`로 기록하고 그 입력 리비전의 모든 시도에
    적용한다. 캐시는 그냥 둔다 — 취소는 지금 실행만 멈춘다.

    리비전 매칭은 두 묶음만 받는다: 완성 리비전, 아니면 엔진 `input_sha256`과 같은
    값(앱이 receive 전에 스테이징된 input.fcpxml의 해시로 남긴 묶음, S1A-R1).
    그 밖의 리비전 — all-zero 포함 — 은 이 job의 취소가 아니다. 와일드카드로
    넓혀 읽지 않는다.
    """
    for path in _cancellation_paths(workspace):
        if not path.is_file():
            continue
        for message in _read_messages(path):
            if not isinstance(message, CancellationRequest):
                raise ValueError(f"취소 기록에 CancellationRequest가 아닌 메시지가 있다: {path}")
            if message.input_revision != input_revision and message.input_revision != input_sha256:
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


def _staged_grant_document(workspace: Path) -> dict[str, object] | None:
    """앱이 스테이징한 media-grant.json — 없으면 None(순수 CLI 실행).

    낡은 camelCase 문서(`grantRevision`)도 **읽는다**(S2A-2): Swift 쪽이 그
    철자로 쓰던 시절에 남은 기록의 정본을 잃지 않게. 두 철자가 함께 있으면
    거부한다 — 읽는 쪽이 어느 것이 정본인지 말할 수 없다. 쓰는 쪽(Swift)은
    snake_case 하나로만 쓴다.
    """
    path = workspace / "media-grant.json"
    if not path.is_file():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"media-grant.json을 해석할 수 없다: {path}") from exc
    if not isinstance(document, dict):
        raise ValueError("media-grant.json이 객체가 아니다")
    if "grant_revision" in document and "grantRevision" in document:
        raise ValueError("media-grant.json에 grant_revision 철자가 둘 다 있다")
    if "grant_revision" not in document and "grantRevision" not in document:
        raise ValueError("media-grant.json에 grant_revision이 없다")
    return document


def _staged_app_grant_revision(workspace: Path) -> str | None:
    """앱이 스테이징한 media-grant.json의 grant revision — 없으면 None(순수 CLI 실행)."""
    document = _staged_grant_document(workspace)
    if document is None:
        return None
    revision = document.get("grant_revision", document.get("grantRevision"))
    if not isinstance(revision, str):
        raise ValueError("media-grant.json에 grant_revision이 없다")
    if len(revision) != 64 or any(char not in "0123456789abcdef" for char in revision):
        raise ValueError("media-grant.json의 grant_revision이 SHA-256이 아니다")
    return revision


def _within_any_root(path: Path, roots: Sequence[str]) -> bool:
    """경로가 뿌리 중 하나 **안**에 있는가 — 구성요소 경계로 잰다(접두사 아님)."""
    resolved = path.resolve()
    for root in roots:
        anchored = Path(root).resolve()
        if resolved == anchored or anchored in resolved.parents:
            return True
    return False


def _swift_grant_revision(roots: Sequence[str]) -> str:
    """루트 목록의 지문 — Swift `MediaGrantDocument.revision`과 같은 재료·모양.

    Swift JSONEncoder(sortedKeys·슬래시 무이스케이프)의 바이트와 같아야 하므로
    `ensure_ascii=False`·콤팩트 구분자로 맞춘다. 두 쪽이 같은 지문을 말하는
    것이 '문서가 루트와 함께 만들어졌다'의 증거다.
    """
    payload = json.dumps(
        {"rootPaths": sorted(roots)}, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _staged_grant_roots(document: dict[str, object]) -> list[str]:
    """문서의 루트 목록 — 절대·비지 않은 경로 문자열만 받는다.

    상대 경로·빈 칸·다른형은 권한이 아니라 깨진 문서다 — 그대로 지나가면
    어디에도 닿지 않는 '허용'으로 읽힌다. **글자는 그대로** 쓴다: 공백 정리를
    하면 끝에 공백이 있는 정당한 절대 경로가 변형되고, Swift가 계산한 지문과
    어긋난다(검사에만 strip하고 값은 원문).
    """
    roots = document.get("roots")
    if (
        not isinstance(roots, list)
        or not roots
        or not all(
            isinstance(row, str) and row.strip() and Path(row).is_absolute() for row in roots
        )
    ):
        raise ValueError("media-grant.json의 roots가 절대 경로 문자열 목록이 아니다")
    return [row for row in roots if isinstance(row, str)]


def _require_staged_grant_document(workspace: Path) -> None:
    """앱이 관리하는 실행(V2 운반 적용)에는 권한 문서가 **필수**다(S2A-2).

    마커 없는 직접 CLI는 문서 없이도 돈다 — 그 계약은 그대로 둔다. 앱 경로는
    문서 없이 열리면 안 된다: 앱의 준비를 거치지 않고 들어온 실행이기 때문이다.
    """
    if not env.role_credentials_applied():
        return
    if (workspace / "media-grant.json").is_file():
        return
    raise ValueError(
        "앱이 관리하는 실행에는 스테이징된 미디어 권한 문서(media-grant.json)이 필요하다"
        " — 앱에서 미디어 루트를 지정한 뒤 다시 실행하세요"
    )


def _require_staged_grant_coverage(media: Sequence[ResolvedMedia], workspace: Path) -> None:
    """권한 문서가 스테이징되어 있으면 원본 전부가 그 루트 안인지 잰다(S2A-2).

    앱이 검사했다고 믿는 대신 엔진이 스스로 잰다 — Swift를 거치지 않은 직접
    실행도 같은 문을 지나며, 재개 실행도 다시 잰다. 문서가 없으면(순수 CLI)
    그냥 지나간다. 루트는 절대 경로만 받고, 지문은 루트와 묶여야 한다(Swift와
    같은 재료) — 깨진 문서가 권한으로 읽히는 길이 없다.
    """
    document = _staged_grant_document(workspace)
    if document is None:
        return
    roots = _staged_grant_roots(document)
    revision = document.get("grant_revision", document.get("grantRevision"))
    if revision != _swift_grant_revision(roots):
        raise ValueError("media-grant.json의 지문이 루트 목록과 맞지 않는다")
    escaped = sorted(
        {str(row.original) for row in media if not _within_any_root(row.original, roots)}
    )
    if escaped:
        raise ValueError(
            "권한 루트 밖 미디어가 있다: " + ", ".join(escaped) + " — 미디어 루트를 확인하세요"
        )


def media_command(args: argparse.Namespace) -> int:
    """`vlogsrt media` — 원본 영상 브라우저 v2의 진단 CLI(A2 가져오기).

    조립 경계(계약 §1): cli.py는 이 모듈만 보고, 이 함수는 계층 안의
    `vlogsrt.project.raw.commands`로 갈라 보낸다. 로컬 복사만 다룬다 —
    어떤 플래그도 유료 호출을 켜지 않는다.
    """
    from vlogsrt.project.raw import commands as raw_media_commands

    return raw_media_commands.media_command(args)


def add_media_parser(sub: argparse._SubParsersAction) -> None:
    """`vlogsrt media` 하위 명령 정의를 계층 안에서 가져온다(조립 위임)."""
    from vlogsrt.project.raw import commands as raw_media_commands

    raw_media_commands.add_media_parser(sub)


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
        "caption-estimate": _project_caption_estimate,
        "caption-run": _project_caption_run,
        "sync-check": _project_sync_check,
    }
    try:
        return handlers[args.project_command](args)
    except WorkflowCancelled as exc:  # BaseException이라 Exception 절로는 안 잡힌다
        log.error(str(exc))
        return 2
    except Exception as exc:  # noqa: BLE001 - CLI는 traceback 없이 단계 실패를 보고한다
        # 이 문이 준비 진단(위상·권한·역할)의 사용자 표면이다 — 원문 그대로는
        # 나가지 않는다. ffprobe stderr 같은 자식 출력에 운반된 값이 섞여 있으면
        # 지우고, 지움 자체가 무너지면 정해진 글로 닫는다(원문 폭기 금지, S2A-2).
        log.error(_sanitize_cli_failure(args, str(exc)))
        return 2


def _project_sync_check(args: argparse.Namespace) -> int:
    """`project sync-check` — SRT 자막 시각을 원본 말소리와 대조해 표시만 한다(D55).

    SRT·원본은 읽기만 하고, 디코드한 PCM·VAD 칸은 끝나면 지워지는 임시 폴더에 둔다. 남는
    것은 SRT 옆 곁파일 `<srt>.sync.json` 하나뿐이고 `--no-write`면 그것도 없다. 유료 호출 0.
    """
    import tempfile

    from vlogsrt.project import sync_check
    from vlogsrt.project.raw import sync_report
    from vlogsrt.scratch_dirs import SYNC_SCRATCH, pid_prefix

    media = args.media.expanduser().resolve()
    srt = args.srt.expanduser().resolve()
    with tempfile.TemporaryDirectory(prefix=pid_prefix(SYNC_SCRATCH)) as scratch:
        document = sync_report.measure_srt(
            media, srt, scratch=Path(scratch), audio_ordinal=args.audio_ordinal
        )
    if args.json:
        sys.stdout.write(sync_check.report_json(document) + "\n")
    else:
        for line in sync_report.summary_lines(document):
            log.out(line)
    if not args.no_write:
        log.info(f"곁파일: {sync_report.write_sidecar(srt, document)}")
    return 0


def _project_receive(args: argparse.Namespace) -> int:
    workspace = args.workspace
    workspace.mkdir(parents=True, exist_ok=True)
    if (workspace / _JOURNAL).exists():
        raise ValueError(f"이미 수신된 작업공간이다: {workspace}")
    source, origin = _receive_source(args, workspace)
    document = parse_project(source)
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
    stored = workspace / _INPUT
    if document.info_path.resolve() != stored.resolve():
        shutil.copyfile(document.info_path, stored)
    _write_json(
        workspace / _SOURCE,
        {
            "input_path": str(document.input_path.resolve()),
            "info_path": str(document.info_path.resolve()),
            "origin": origin,
        },
    )
    WorkflowJournal.create(workspace / _JOURNAL, revision)
    _write_messages(workspace / _REVISION, (revision,))
    _protocol_out(revision)
    return 0


def _receive_source(args: argparse.Namespace, workspace: Path) -> tuple[Path, dict[str, object]]:
    """입력을 확정한다 — FCPXML은 그대로, 영상 묶음은 타임라인을 합성한다.

    합성물은 곧바로 작업공간의 `input.fcpxml`이 되고, 그 뒤로는 내보낸 XML과
    구별되지 않는다. 같은 파서·같은 거부 규율을 그대로 받는다는 뜻이다.
    `origin`은 어디서 왔는지의 기록이다 — 합성이면 어떤 파일을 어떤 순서로
    묶었는지 남겨야 리비전이 무엇을 설명하는지 말할 수 있다.
    """
    from_clips = getattr(args, "from_clips", None)
    if (args.input is None) == (from_clips is None):
        raise ValueError("입력 FCPXML과 --from-clips 중 정확히 하나를 주어야 한다")
    if from_clips is None:
        return args.input, {"kind": "fcpxml"}
    directory = Path(from_clips)
    probes = probe_clips(collect_clip_paths(directory))
    destination = workspace / _INPUT
    destination.write_bytes(synthesize_fcpxml(probes, project_name=directory.name))
    return destination, {
        "kind": "clips",
        "directory": str(directory.resolve()),
        "clips": [
            {"name": probe.path.name, "sha256": probe.content_sha256, "frames": probe.frame_count}
            for probe in probes
        ],
    }


def _project_inspect(args: argparse.Namespace) -> int:
    """네트워크를 열지 않고 공식 가격 스냅샷과 비용 상한을 고정한다."""
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state != "received":
        raise ValueError(f"inspect는 received 상태에서만 가능하다: {journal.state}")
    pricing = official_pricing_snapshot(AnalysisSettings().model)
    # 추정은 키 없이도 실제 준비를 잰다(S2A-2) — 오디오 위상과 권한 문서
    # 커버리지. 지원하지 않는 구성은 비용부터 드러난다.
    estimate = _current_analysis_estimate(
        args, workspace, journal.revision, pricing, verify_prerequisites=True
    )
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
    *,
    verify_prerequisites: bool = False,
) -> AnalysisEstimate:
    """지금 계산한 비용 추정 — `verify_prerequisites`면 실제 준비를 잰다(S2A-2).

    실제 원본의 오디오 위상과(위상 관문) 스테이징된 권한 문서의 루트 안
    커버리지를 같이 잴 수 있다. 둘 다 키가 없어도 돈다(읽기 전용) — 추정이
    지원하지 않는 구성을 드러내는 것이 이 매개변수의 목적이다. 유료 실행의
    재계산도 이 자리를 다시 지나므로, 저장된 추정이 있어도 실행 때 다시 잰다.

    순서: 앱 관리 실행의 문서 필수 검사가 **미디어를 읽기 전에** 오고, 권한
    커버리지가 위상 확인보다 먼저다 — 루트 밖 미디어를 ffprobe로 찔러 보고
    나서 거절하면, 권한 없는 경로를 탐색한 셈이 된다.
    """
    if verify_prerequisites:
        _require_staged_grant_document(workspace)
    document = parse_project(workspace / _INPUT)
    cfg = load_config()
    project_config = load_project_config(args.project_config)
    media_map = getattr(args, "media_map", None)
    overrides: tuple[MediaOverride, ...] = (
        load_media_map(media_map) if media_map is not None else ()
    )
    media = resolve_media(document, overrides)
    if verify_prerequisites:
        _require_staged_grant_coverage(media, workspace)
        topology.require_supported_audio_topology(media)
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
        # 엔드포인트·작업공간 자리도 지문에 묶는다(S2B-2b) — 다른 공급자·다른
        # 개발 루트가 같은 승인을 재사용하는 길을 추정 단계에서부터 닫는다.
        endpoint_sha256=request_accounting.endpoint_identity_sha256(project_config),
        workspace_binding_sha256=request_accounting.workspace_binding_sha256(workspace),
    )
    return build_analysis_estimate(
        document,
        revision,
        settings,
        pricing=pricing,
        media_grant_revision=_media_grant_revision(media, _staged_app_grant_revision(workspace)),
        settings_sha256=fingerprint,
    )


def _redact_failure_text(args: argparse.Namespace, text: str) -> str:
    """실패 문면에서 자격 증명 값을 지운다(S2A-2) — 엔진 STT 키와 설정이 고른 비전 키.

    비전 키 이름은 프로젝트 설정이 정한다(P5-00) — 이름을 바꾸거나 한 키를 양쪽에
    얹지 않는다. 설정을 읽지 못해도 `env.redact_secrets`가 관리 값(엔진 키와
    적재된·기본 비전 키)을 스스로 알고 긴 값부터 지우므로(S2A-2 단위2) 가림이
    한쪽으로 줄지 않는다. 지움 실패가 원본 실패를 묻게 두지 않는다.
    """
    secrets: list[str] = []
    try:
        key_name = load_project_config(args.project_config).vision_api_key_env
        vision_key = env.get(key_name)
        if vision_key:
            secrets.append(vision_key)
    except Exception:  # noqa: BLE001 - 지움 보조가 원본 실패를 대신 실패시키지 않는다
        log.warn("비전 키 이름을 확인하지 못했다 — 관리 값만으로 지운다")
    return env.redact_secrets(text, secrets=tuple(secrets))


def _sanitize_cli_failure(args: argparse.Namespace, text: str) -> str:
    """CLI 실패 문면의 마지막 문 — 지움이 무너지면 원문 대신 정해진 글.

    준비 진단(ffprobe stderr 포함)은 이 문을 지나 사용자에게 나간다. 지우개
    구성이 실패해도 원문을 폭기하지 않는다: 원문에 키가 있었는지 알 수 없으므로
    있었다고 가정하는 쪽이 안전하다.
    """
    try:
        return _redact_failure_text(args, text)
    except Exception:  # noqa: BLE001 - 지움 보조 실패는 원문 폭기로 이어지지 않는다
        return "실패 진단을 정리하지 못했습니다 — 명령을 다시 실행해 주세요"


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
    authorization: AnalysisAuthorization | None = None
    if bool(args.online) or args.vision_fixtures is not None:
        loaded_estimate = _read_one(workspace / _ESTIMATE)
        if not isinstance(loaded_estimate, AnalysisEstimate):
            raise ValueError("analysis-estimate.jsonl의 메시지가 AnalysisEstimate가 아니다")
        estimate_message = loaded_estimate
        if estimate_message.input_revision != journal.revision.revision:
            raise ValueError("비용 추정의 입력 리비전이 현재 작업과 다르다")
        if estimate_message.pricing_snapshot_revision != pricing.snapshot_revision:
            raise ValueError("비용 추정이 현재 가격 스냅샷에 묶이지 않았다")
        current_estimate = _current_analysis_estimate(
            args, workspace, journal.revision, pricing, verify_prerequisites=bool(args.online)
        )
        if current_estimate != estimate_message:
            raise ValueError("현재 분석 설정·미디어가 승인 전 비용 추정과 다르다")
    if bool(args.online):
        assert estimate_message is not None
        if args.authorization is None:
            raise ValueError("온라인 분석은 --authorization JSONL이 필요하다")
        loaded_authorization = _read_one(args.authorization)
        if not isinstance(loaded_authorization, AnalysisAuthorization):
            raise ValueError("승인 JSONL이 AnalysisAuthorization이 아니다")
        authorization = loaded_authorization
        authorization.require_matches(
            journal.revision.revision,
            estimate_message.estimate_revision,
            pricing.snapshot_revision,
            estimate_message.media_grant_revision,
        )
        if estimate_message.cost_upper_bound is None:
            raise ValueError("온라인 분석 비용 상한이 미설정이다")
        # 유료 역할은 파이프라인 시작 **전에** 전부 유효해야 한다(S2A-2 마지막
        # 단위). 전사기가 먼저 만들어지고 비전 공장에서 뒤늦게 부재를 발견하면
        # 이미 STT 호출이 열린 뒤다 — 어느 역할의 부재도 유료 호출 0회로 닫힌다.
        # 재개(authorized/analyzing)도 이 문을 다시 지난다. 직접 CLI·V1의 이름
        # 계약은 그대로다(이 검사는 값의 존재만 확인한다).
        require_paid_roles(args)
    if journal.state == "estimate_ready":
        # 유료 호출은 승인 파일이, 픽스처 재생은 깃발이 분석 착수를 승인한다.
        journal.transition(journal.revision.revision, "authorized")
    attempt_id: str | None = None
    result: AnalysisResult | None = None
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
        # 진행 발행기는 try 밖에서 만든다 — 안쪽 어느 지점에서 무너져도 종료 경계를
        # 남길 수 있어야 한다(준비 경계는 running 기록 뒤에 곧바로 간다).
        progress = _ProgressPublisher(
            workspace,
            journal.revision.revision,
            estimate_message.estimate_revision,
            attempt_id,
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
            # 새 시도가 시작됐다 — 이전 시도의 집계가 지금 시도의 결과로 읽히면 안 된다.
            _discard_file(workspace / _ANALYSIS_RESULT)
            progress.boundary("preparation")
            if args.vision_fixtures is not None and not bool(args.online):
                candidates, result = _replay_analysis(
                    Path(args.vision_fixtures),
                    workspace,
                    journal.revision,
                    estimate_message,
                    attempt_id,
                    progress,
                )
            else:
                candidates, result = _produce_analysis_outcome(
                    args,
                    workspace,
                    journal.revision,
                    estimate_message,
                    attempt_id,
                    started_at,
                    pricing,
                    progress,
                    authorization,
                )
        except KeyboardInterrupt:
            progress.terminate("cancelled")
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
            progress.terminate("cancelled")
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
        except _StageFailure as exc:
            # 집계·시도 기록은 단계 안에서 정돈되어 이미 남았다 — 중복 기록 없이 넘긴다.
            raise ValueError(str(exc)) from exc
        except Exception as exc:
            # 실패 문면은 지속 기록(attempts)과 다시 던져지는 예외 두 표면으로
            # 나간다 — 두 곳 모두 자격 증명 값을 지우고 지나간다(S2A-2).
            raw = " ".join(str(exc).split()) or exc.__class__.__name__
            reason = _redact_failure_text(args, raw)
            progress.terminate("failed")
            _append_message(
                workspace / _ATTEMPTS,
                AnalysisAttempt(
                    journal.revision.revision,
                    estimate_message.estimate_revision,
                    attempt_id,
                    "failed",
                    started_at,
                    reason,
                ),
            )
            if reason == raw:
                raise  # 비밀이 없으면 원문 진단을 그대로 둔다
            # RuntimeError여야 cli.main이 한 줄로 인쇄하고 실패(1) 경로의 진단
            # 동기화가 그대로 흐른다. `from None`은 사슬(원문 예외 문면)을 끊는다 —
            # 그 문면에 키가 있었다는 것이 이 갈림길의 조건이다.
            raise RuntimeError(reason) from None
        if result.status == "error":
            # 전체 실패 — 집계·판정·사용량은 이미 남았다. 저널은 authorized에 두므로
            # 같은 입력으로 재시도할 수 있다. 취소는 여기를 지나지 않는다 — 별개 결과다.
            progress.terminate("failed")
            _append_message(
                workspace / _ATTEMPTS,
                AnalysisAttempt(
                    journal.revision.revision,
                    estimate_message.estimate_revision,
                    attempt_id,
                    "failed",
                    started_at,
                    _result_error_text(result),
                ),
            )
            raise ValueError(_result_error_text(result))
        # partial_error는 안전 후보와 함께 정상 검토 경로로 간다 — 발행·검토 가능성이
        # 실패 증거를 가리지 않게 경고를 같이 낸다(종료 코드 0이 성공 요약을 못 박지 않는다).
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
    if result is not None and result.status == "partial_error":
        # 종료 코드 0이지만 성공이 아니다 — 부분 실패 사실을 검토 화면보다 먼저 못 박는다.
        log.out(
            f"분석 부분 실패 — 창 성공 {result.windows_successful}/실패 {result.windows_failed}"
            f"/미처리 {result.windows_skipped} · 사유: {', '.join(result.reason_codes)}"
        )
    if result is not None and result.status == "success" and result.windows_total == 0:
        # 창 0개 성공은 정상이다(무오디오·무발화) — 실패로 읽히지 않게 말로 못 박는다.
        log.out("분석 완료: 창 0개 · 발화·후보 없음(정상)")
    log.out(f"컷 후보 {len(candidates)}건 · 사람 검토 필요")
    if attempt_id is not None:
        # 마지막 경계 — 후보·시도 기록·저널이 모두 정착한 뒤에야 성공을 말한다
        # (S2A-3). 외부 후보 경로에는 시도가 없으니 진행도 없다.
        progress.boundary("publication", status="succeeded")
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
    _require_writable_output(args.out)
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
    journal.transition(journal.revision.revision, "roughcut_generated")
    log.out(f"roughcut: {args.out}")
    log.out(f"review: {report}")
    return 0


def _project_caption(args: argparse.Namespace) -> int:
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    # roughcut_imported는 낡은 저널의 같은 자리다(S2C) — 읽기 호환으로 받는다.
    if journal.state not in {
        "roughcut_generated",
        "roughcut_imported",
        "caption_estimate_ready",
    }:
        raise ValueError(
            f"caption은 roughcut_generated/caption_estimate_ready 상태에서만 가능하다: "
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
    if journal.state in {"roughcut_generated", "roughcut_imported"}:
        # 낡은 저널의 roughcut_imported도 같은 자리로 받는다(S2C 읽기 호환).
        _write_messages(workspace / _CAPTION_REQUEST, (request,))
        journal.transition(journal.revision.revision, "caption_received")
        _write_messages(workspace / _CAPTION_PRICING, (pricing,))
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


# ─────────────────────────────────────────────────────────────
#  STAB-S4A — 공유 받기로 시작한 자막 job: 추정과 실행이 갈린다
# ─────────────────────────────────────────────────────────────


def _caption_job_facts(job_dir: Path) -> tuple[CaptionRequest, ValidatedCaptionInput]:
    """스테이징된 최종 내보내기에서 요청을 다시 만들고 통째로 재검증한다(S4A).

    원본 문서(`caption-origin.json`)가 없으면 그 자체가 실패이다 — 원본 오디오로
    되돌아가는 길도, 매핑을 모른 채 진행하는 길도 없다. 이 함수는 추정 직전·실행
    직전에 같은 문으로 불리므로 승인 뒤 바뀐 오디오·XML·UID·길이는 여기서 걸린다.
    """
    origin = load_caption_origin(job_dir / _CAPTION_ORIGIN)
    request = create_caption_request(
        origin.origin_revision, job_dir / _FINAL_AUDIO, job_dir / _FINAL_XML
    )
    validated = validate_caption_request(request, origin)
    return request, validated


def _project_caption_estimate(args: argparse.Namespace) -> int:
    """최종 내보내기의 자막 비용을 고정한다 — 전사도 API 키도 없다(S4A).

    스테이징된 WAV·FCPXML·원본 문서를 그대로 잰다. 첫 실행이면 자막 원장을 만들
    고 요청·가격·추정을 고정하며, 다시 돌면 이미 고정된 것과 지금 재료가 같은지만
    본다(드리프트는 거절). 상태는 `caption_estimate_ready`에서 멈춘다 — 승인은
    실행 문(`caption-run`)이 요구한다.
    """
    job_dir: Path = args.job_dir
    request, validated = _caption_job_facts(job_dir)
    cfg = load_config()
    pricing = official_pricing_snapshot()
    estimate = build_caption_estimate(
        request,
        audio_seconds=validated.audio_seconds,
        pricing=pricing,
        settings_sha256=caption_settings_sha256(cfg),
    )
    workspace = job_dir / "workspace"
    journal_path = workspace / _CAPTION_JOURNAL
    if journal_path.is_file():
        journal = CaptionJournal.open(journal_path)
        if journal.state not in {"caption_estimate_ready", "caption_authorized"}:
            raise ValueError(
                "caption-estimate는 caption_estimate_ready/caption_authorized 상태에서만"
                f" 가능하다: {journal.state}"
            )
        stored_request = _read_one(workspace / _CAPTION_REQUEST)
        if not isinstance(stored_request, CaptionRequest) or stored_request != request:
            raise ValueError(
                "스테이징된 최종 내보내기가 고정된 자막 요청과 다르다 — 새 job으로 시작한다"
            )
        stored_estimate = _read_one(workspace / _CAPTION_ESTIMATE)
        if not isinstance(stored_estimate, CaptionEstimate) or stored_estimate != estimate:
            raise ValueError("현재 자막 추정이 고정된 추정과 다르다 — 새 job으로 시작한다")
    else:
        journal = CaptionJournal.create(journal_path, request)
        _write_messages(workspace / _CAPTION_REQUEST, (request,))
        _write_messages(workspace / _CAPTION_PRICING, (pricing,))
        _write_messages(workspace / _CAPTION_ESTIMATE, (estimate,))
        journal.transition(request.request_revision, "caption_estimate_ready")
    _protocol_out(estimate)
    log.out(
        f"자막 추정: 오디오 {float(validated.audio_seconds):.1f}초 ·"
        f" 상한 ${Decimal(estimate.cost_upper_bound):.3f} · 승인 대기"
    )
    return 0


def _project_caption_run(args: argparse.Namespace) -> int:
    """승인된 자막 실행 — 같은 요청으로 실패 뒤 재시도가 열려 있다(S4A).

    승인 문서는 자막 것(`CaptionAuthorization`)이어야 하고 지금 추정에 묶여 있어야
    한다 — 러프컷 승인은 자막을 승인하지 않는다. 시도는 pending→running→결과로
    기록에 남고, 실패해도 원장은 `caption_authorized`에 둔다(같은 유효 승인으로 다시
    실행할 수 있다). 취소만 종료 상태로 닫는다.
    """
    job_dir: Path = args.job_dir
    workspace = job_dir / "workspace"
    journal = CaptionJournal.open(workspace / _CAPTION_JOURNAL)
    if journal.state not in {"caption_estimate_ready", "caption_authorized"}:
        raise ValueError(
            "caption-run은 caption_estimate_ready/caption_authorized 상태에서만 가능하다: "
            f"{journal.state}"
        )
    authorization_path = job_dir / _CAPTION_AUTHORIZATION
    if not authorization_path.is_file():
        raise ValueError(
            f"자막 승인 문서가 없다: {authorization_path} — 승인 없이 자막을 실행하지 않는다"
        )
    authorization_message = _read_one(authorization_path)
    if not isinstance(authorization_message, CaptionAuthorization):
        # 러프컷 승인(AnalysisAuthorization)을 얹어도 여기서 닫힌다 — 승인은 종류별이다.
        raise ValueError(
            "자막 승인 문서가 CaptionAuthorization가 아니다 — 러프컷 승인은 자막을 승인하지 않는다"
        )
    # 재검증이자 드리프트 검사: 실행 직전에 승인 전과 같은 문으로 다시 잰다.
    request, validated = _caption_job_facts(job_dir)
    cfg = load_config()
    pricing = official_pricing_snapshot()
    stored_pricing = _read_one(workspace / _CAPTION_PRICING)
    if not isinstance(stored_pricing, PricingSnapshot) or stored_pricing != pricing:
        raise ValueError(
            "기록된 자막 가격 스냅샷이 현재 공식 가격과 다르다 — caption-estimate를 다시 돌린다"
        )
    estimate = build_caption_estimate(
        request,
        audio_seconds=validated.audio_seconds,
        pricing=pricing,
        settings_sha256=caption_settings_sha256(cfg),
    )
    stored_estimate = _read_one(workspace / _CAPTION_ESTIMATE)
    if not isinstance(stored_estimate, CaptionEstimate) or stored_estimate != estimate:
        raise ValueError("현재 자막 추정이 고정된 추정과 다르다 — 새 job으로 시작한다")
    authorization_message.require_matches(estimate)
    if journal.state == "caption_estimate_ready":
        journal.transition(request.request_revision, "caption_authorized")
    attempt_id = f"caption-{uuid.uuid4()}"
    started_at = _utc_now()

    def record_attempt(status: CaptionAttemptStatus, error: str | None = None) -> None:
        """시도의 내구 진행 기록 — pending→running→결과가 줄마다 남는다(S4A)."""
        _append_message(
            workspace / _CAPTION_ATTEMPTS,
            CaptionAttempt(
                request.request_revision,
                estimate.estimate_revision,
                attempt_id,
                status,
                started_at,
                error,
            ),
        )

    record_attempt("pending")
    online = bool(args.online)

    def gate() -> None:
        # 자막 job의 취소 묶음 두 개: 완성 요청 리비전, 아니면 스테이징된 최종
        # FCPXML의 해시(앱이 receive 전에 계산할 수 있는 값 — S1A-R1과 같은 자리).
        _check_cancellation(workspace, request.request_revision, attempt_id, request.final_revision)

    try:
        record_attempt("running")
        # 새 시도가 시작됐다 — 이전 시도의 결과가 지금 시도의 것으로 읽히면 안 된다.
        _discard_file(workspace / _CAPTION_RESULT)
        accounting: request_accounting.CaptionAccounting | None = None
        if online:
            accounting = request_accounting.CaptionAccounting.open(
                workspace=workspace,
                pricing=pricing,
                authorization=authorization_message,
                settings_sha256=estimate.settings_sha256,
                project_config=load_project_config(args.project_config),
                attempt_id=attempt_id,
                approval_limit=Decimal(estimate.cost_upper_bound),
            )
        # 유료 실행은 예약이 살아 있는 동안에만 키를 푼다 — 승인 문서가 곧 비용
        # 확인이다(추정의 상한에 묶였다). 픽스처 실행은 게이트만 지난다.
        transcriber = _GatedTranscriber(
            transcribe.get_transcriber(
                cfg,
                online=online,
                force_offline=not online,
                cancellation=gate,
                observer=None if accounting is None else accounting.observer(),
            ),
            gate=gate,
        )
        gate()
        delivery = generate_final_captions(
            request,
            cfg,
            args.out,
            online=online,
            confirmed_cost=True,
            transcriber=transcriber,
            srt_stem=f"{validated.project_name}.ko",
        )
        if accounting is not None:
            # 회계 기반구조의 실패는 자막 오류로 위장하지 않는다(S2B-2b와 같은 선).
            accounting.ensure_healthy()
        _write_messages(workspace / _CAPTION_RESULT, (delivery.result,))
        record_attempt("succeeded")
        journal.transition(request.request_revision, "caption_ready")
        _report_caption_delivery(delivery.result)
    except (WorkflowCancelled, KeyboardInterrupt):
        record_attempt("cancelled")
        journal.transition(request.request_revision, "cancelled")
        raise
    except Exception as exc:
        # 예산 거부는 원인 사슬 깊은 데서 관찰자 예외로 올라와 자막 실패로 한 번
        # 감싸진다 — 문면보다 종류로 갈라 본다(거부 문면에는 비밀이 없다).
        if request_accounting.is_budget_denial(exc):
            record_attempt("failed", "누적 예약이 승인 상한을 넘어 이 요청은 보내지 않았다")
            raise ValueError("자막 실행 실패: 예산 상한 — 이 요청은 보내지 않았다") from exc
        raw = " ".join(str(exc).split()) or exc.__class__.__name__
        reason = _redact_failure_text(args, raw)
        record_attempt("failed", reason)
        if reason == raw:
            raise
        raise RuntimeError(reason) from None
    return 0


def _report_caption_delivery(result: CaptionResult) -> None:
    """마지막 경계 — 결과·시도·원장이 모두 정착한 뒤에만 성공을 말한다(S4A)."""
    _protocol_out(result)
    if result.srt_path is None:
        log.out("자막 상태: 발화 없음(성공)")
    else:
        log.out(f"srt: {result.srt_path}")
        log.out(f"finder: {result.srt_path.parent}")
    if result.warnings:
        log.out(f"경고 {len(result.warnings)}건: {' · '.join(result.warnings)}")
    # 자동 import는 하지 않는다 — 사람이 Finder에서 확인하고 직접 넣는다.
    log.out("Final Cut Pro: File > Import > Captions → Relative to Timeline")


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
    oscompat.replace(temporary, path)


def _discard_file(path: Path) -> None:
    """시작을 알리는 지우개 — 없으면 아무것도 하지 않는다(조용한 성공이 아니다)."""
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _require_writable_output(destination: Path) -> None:
    """결과 자리의 쓰기 증명(S2A-2) — 렌더보다 앞서고 산물은 건드리지 않는다.

    이미 있는 읽기 전용 파일·디렉터리 자리는 교정 글로 닫는다. 없는 자리는
    상한 있는 임시 생성/제거로 '그 자리에 만들 수 있다'를 증명한다.
    """
    if destination.is_dir():
        raise ValueError(f"결과를 쓸 자리가 디렉터리다: {destination} — 파일 경로를 주세요")
    if destination.exists() and not os.access(destination, os.W_OK):
        raise ValueError(
            f"결과를 쓸 자리가 읽기 전용이다: {destination} — 쓰기 가능하게 만들거나 지우세요"
        )
    probe = destination.parent / f".vlogsrt-write-probe-{uuid.uuid4().hex}"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"결과를 쓸 자리에 만들 수 없다: {destination} ({exc})") from exc
    finally:
        probe.unlink(missing_ok=True)


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
        _DECISIONS,
        _ANALYSIS_RESULT,
        _PROGRESS,
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


def _result_error_text(result: AnalysisResult) -> str:
    """집계 실패의 사용자 보이는 요약 — 정돈된 사유 코드뿐이다."""
    reasons = ", ".join(result.reason_codes) or "(사유 없음)"
    return (
        f"분석이 완전히 성공하지 못했다: {result.status} — 창 성공 {result.windows_successful}"
        f"/실패 {result.windows_failed}/미처리 {result.windows_skipped},"
        f" 상류 실패 오디오 {result.upstream_audio_failed}"
        f"/전사 {result.upstream_transcription_failed}/VAD {result.upstream_vad_failed}"
        f" · 사유: {reasons}"
    )


def _replay_analysis(
    fixtures: Path,
    workspace: Path,
    revision: ProjectRevision,
    estimate: AnalysisEstimate,
    attempt_id: str,
    progress: _ProgressPublisher,
) -> tuple[tuple[CutCandidate, ...], AnalysisResult]:
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
        if message.status != "success":
            # 낡은(상태 없는) 기록은 legacy/unknown이다 — 성공 증거로 재생하지 않고
            # 모호하다고 재실행하지도 않는다. 사람이 픽스처를 다시 기록한다.
            raise ValueError(
                f"판정 픽스처가 결과 상태를 가지고 있지 않다(재기록 필요): {message.window_id}"
            )
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
    ordered = tuple(decisions.values())
    # 재생의 진행도 실제 창 수를 말한다 — 유료 호출이었다는 뜻은 담지 않는다(S2A-3).
    progress.boundary("vision", completed=0, total=len(ordered))
    progress.boundary("vision", completed=len(ordered), total=len(ordered))
    result = aggregate_analysis_outcome(
        input_revision=revision.revision,
        attempt_id=attempt_id,
        estimate_revision=estimate.estimate_revision,
        decisions=ordered,
        windows_total=len(ordered),
        candidate_count=len(candidates),
        upstream=UpstreamStages(),
    )
    _write_messages(workspace / _DECISIONS, ordered)
    _write_messages(workspace / _ANALYSIS_RESULT, (result,))
    progress.boundary("publication")
    return candidates, result


def _produce_analysis_outcome(
    args: argparse.Namespace,
    workspace: Path,
    revision: ProjectRevision,
    estimate: AnalysisEstimate,
    attempt_id: str,
    started_at: str,
    pricing: PricingSnapshot,
    progress: _ProgressPublisher,
    authorization: AnalysisAuthorization | None = None,
) -> tuple[tuple[CutCandidate, ...], AnalysisResult]:
    """기존 소스 전사/VAD를 재사용하고 안전 seed만 Luna에 제공한다.

    창 하나의 실패는 실행 전체를 무너뜨리지 않는다 — 판정을 error로 남기고 다음
    창으로 간다. 이미 끝난 창의 판정은 완성되는 채로 창마다 기록에 더한다(뒤 창의
    예외가 앞 증거를 지우지 않는다). 비용은 유료 실행에서 내구 원장이 요청마다
    예약·확정한다(S2B-2b) — 예약 거부는 남은 창을 미처리로 덮고, 회계 자체의
    실패는 실행 전체를 닫는다. 취소(WorkflowCancelled·BaseException)는 여기서
    잡지 않는다.
    """
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
    accounting: request_accounting.AnalysisAccounting | None = None
    if bool(args.online):
        # 유료 실행의 회계 문맥 — 원장은 같은 신원으로 다시 열려 지난 실측과 남은
        # 예약을 되살린다. 원장 없는 과거 기록은 명시적 조치 없이는 시작하지 않는다.
        assert authorization is not None
        accounting = request_accounting.AnalysisAccounting.open(
            workspace=workspace,
            settings=settings,
            pricing=pricing,
            authorization=authorization,
            settings_sha256=estimate.settings_sha256,
            project_config=project_config,
            attempt_id=attempt_id,
            approval_limit=Decimal(estimate.cost_upper_bound or "0"),
        )

    def cancellation() -> None:
        _check_cancellation(workspace, revision.revision, attempt_id, revision.input_sha256)

    transcriber = _GatedTranscriber(
        transcribe.get_transcriber(
            cfg,
            online=bool(args.online),
            force_offline=not bool(args.online),
            # 실제 호출 경로(LiveTranscriber)에는 관찰자가 조각·재시도마다 붙는다
            # — 예약·확정은 그 안에서 일어난다. 픽스처·가짜 전사기는 유료 호출이
            # 아니므로 관찰자가 없고, 위임 직전 게이트만 지난다.
            cancellation=cancellation,
            observer=None if accounting is None else accounting.observer(),
        ),
        gate=cancellation,
    )
    _check_cancellation(workspace, revision.revision, attempt_id, revision.input_sha256)
    try:
        progress.boundary("audio")
        prepared = runner.prepare_audio(
            workspace / _INPUT,
            workspace / "artifacts",
            overrides=overrides,
            config=cfg,
            project_config=project_config,
        )
    except Exception as exc:  # noqa: BLE001 - 준비 단계 실패도 정돈된 집계로 남는다
        raise _close_stage_failure(
            workspace, revision, estimate, attempt_id, started_at, "preparation_error", progress
        ) from exc
    if prepared.document.input_sha256 != revision.input_sha256:
        raise ValueError("분석 준비 문서가 승인된 입력 리비전과 다르다")
    _check_cancellation(workspace, revision.revision, attempt_id, revision.input_sha256)
    try:
        analyzed = runner.analyze_audio(prepared, transcriber=transcriber)
    except Exception as exc:  # noqa: BLE001 - 분석 단계 실패도 정돈된 집계로 남는다
        raise _close_stage_failure(
            workspace, revision, estimate, attempt_id, started_at, "analysis_stage_error", progress
        ) from exc
    if accounting is not None:
        # 회계 기반구조의 실패는 소스 오류로 위장하지 않는다 — 단계 사이에서
        # 즉시 실행 실패로 들춘다(예약 거부는 닫지 않는다: 상한이 하는 일이다).
        accounting.ensure_healthy()
    stop_reason: str | None = None
    upstream, upstream_reasons = _upstream_stages(prepared, analyzed)
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
    # 창 계획이 섰다 — 이제부터 계수를 아는데, 전체 일감의 백분율이 아니다(S2A-3).
    progress.boundary("vision", completed=0, total=len(windows))
    client = None
    if bool(args.online):
        # 비전 자격 증명은 게이트와 같은 문에서 온다(`_staged_vision_credentials`)
        # — V2 앱 운반이면 별도 역할 값, 마커 없는 직접 경로면 낡은 공유 키
        # 계약 그대로. 게이트가 통과시킨 값과 공장이 받는 값이 같다는 보장이다.
        vision_key, vision_base = _staged_vision_credentials(project_config)
        client = transcribe.create_vision_client(api_key=vision_key, base_url=vision_base)
    decisions: list[VisionWindowDecision] = []
    candidates: list[CutCandidate] = []
    used_images = 0
    # 시도마다 판정 기록을 새로 연다 — 창이 끝날 때마다 더하므로 뒤 창의 실패가
    # 앞 창의 증거를 지우지 않는다. 창이 하나도 없으면 빈 기록으로 남는다.
    _write_messages(workspace / _DECISIONS, ())
    for planned in windows:
        _check_cancellation(workspace, revision.revision, attempt_id, revision.input_sha256)
        if stop_reason is not None:
            decision = skipped_window_decision(revision.revision, planned.window_id, stop_reason)
        else:
            try:
                decision, image_count = _decide_window(
                    planned,
                    analyzed,
                    revision.revision,
                    settings,
                    project_config,
                    estimate.settings_sha256,
                    workspace,
                    client=client,
                    fixtures=(
                        Path(args.vision_fixtures) if args.vision_fixtures is not None else None
                    ),
                    attempt_id=attempt_id,
                    observer=None
                    if accounting is None
                    else accounting.window_observer(planned.window_id),
                    input_sha256=revision.input_sha256,
                )
            except transcribe.ObserverFailure as exc:
                if request_accounting.is_budget_denial(exc):
                    # 예약 거부 — 이 창의 호출은 가지 않았다. 돈이 든 다음 호출도 없다.
                    stop_reason = "cost_ceiling_exceeded"
                    decision = skipped_window_decision(
                        revision.revision, planned.window_id, stop_reason
                    )
                else:
                    # 회계 기반구조의 실패는 창 실패가 아니다 — 실행 전체를 닫는다.
                    raise
            except Exception:  # noqa: BLE001 - 창 하나의 실패가 전체 증거를 지우지 않는다
                decision = error_window_decision(revision.revision, planned.window_id)
            else:
                used_images += image_count
                for seed in planned.candidates:
                    candidate = finalize_candidate(seed, decision)
                    if candidate is not None:
                        candidates.append(candidate)
                if used_images > estimate.max_images:
                    # 상한 위반은 다음 호출 앞에서 끊는다 — 이미 받은 판정은 그대로 둔다.
                    stop_reason = "image_budget_exceeded"
        decisions.append(decision)
        _append_message(workspace / _DECISIONS, decision)
        progress.boundary("vision", completed=len(decisions), total=len(windows))
    # 발행 앞의 마지막 확인 — 마지막 소스·Luna 호출이 진행 중인 사이 남겨진 취소가
    # 성공 집계로 못 간다. 판정·사용량 기록은 그대로 두되 집계는 쓰지 않는다(S1A-R1).
    _check_cancellation(workspace, revision.revision, attempt_id, revision.input_sha256)
    result = aggregate_analysis_outcome(
        input_revision=revision.revision,
        attempt_id=attempt_id,
        estimate_revision=estimate.estimate_revision,
        decisions=tuple(decisions),
        windows_total=len(windows),
        candidate_count=len(candidates),
        upstream=upstream,
        upstream_reasons=upstream_reasons,
    )
    _write_messages(workspace / _ANALYSIS_RESULT, (result,))
    progress.boundary("publication")
    return tuple(candidates), result


class _StageFailure(Exception):
    """준비·분석 단계가 창 loop 앞에서 무너진 것 — 집계·시도 기록은 이미 남았다."""


def _close_stage_failure(
    workspace: Path,
    revision: ProjectRevision,
    estimate: AnalysisEstimate,
    attempt_id: str,
    started_at: str,
    reason_code: str,
    progress: _ProgressPublisher,
) -> _StageFailure:
    """단계 실패를 정돈된 집계·시도 기록으로 닫는다 — 원문 예외는 기록에 못 들어간다."""
    progress.terminate("failed")
    result = aggregate_analysis_outcome(
        input_revision=revision.revision,
        attempt_id=attempt_id,
        estimate_revision=estimate.estimate_revision,
        decisions=(),
        windows_total=0,
        candidate_count=0,
        upstream=UpstreamStages(),
        stage_failure=reason_code,
    )
    _write_messages(workspace / _ANALYSIS_RESULT, (result,))
    text = f"분석 단계 실패: {reason_code}"
    _append_message(
        workspace / _ATTEMPTS,
        AnalysisAttempt(
            revision.revision,
            estimate.estimate_revision,
            attempt_id,
            "failed",
            started_at,
            text,
        ),
    )
    return _StageFailure(text)


class _GatedTranscriber:
    """위임 직전 취소 게이트를 곁들인 전사기 — 1차·재전사·두 번째 모델이 모두 지난다.

    이 계층의 `transcribe`는 소스 전사·경계 재전사·글자 교정의 두 번째 모델 호출이
    공통으로 지나는 유일한 고리다. 게이트가 `WorkflowCancelled`(BaseException)를
    던지면 실패로 상태 기록을 만드는 `except Exception` 층들을 뚫고 올라와 실행
    전체를 `cancelled`로 닫는다. 예산은 더 이계층의 몫이 아니다 — 유료 호출의
    상한은 내구 원장이 요청마다 예약한다(S2B-2b).
    """

    def __init__(
        self,
        delegate: transcribe.Transcriber,
        gate: Callable[[], None] = lambda: None,
    ) -> None:
        self.delegate = delegate
        self.gate = gate

    def transcribe(
        self, job: transcribe.TranscriptionJob, cfg: Config
    ) -> list[transcribe.ChunkResponse]:
        self.gate()
        return self.delegate.transcribe(job, cfg)


def _upstream_stages(
    prepared: runner.PreparedAudio, analyzed: runner.AnalyzedAudio
) -> tuple[UpstreamStages, tuple[str, ...]]:
    """상류 단계의 시도·실패·미적용 수와 쓸 수 있는 소스 수 — 집계의 근거.

    실패는 각 단계가 남긴 error 상태만 센다. 선언된 무오디오(no_audio)와 빈 전사
    (empty)는 실패가 아니다 — 미적용·정상으로 갈라 보인다. 소스 단위 수는 자원
    기준으로 중복을 묶는다(같은 미디어를 여러 클립에 써도 소스는 하나다).
    `sources_usable`은 오디오가 열리고 전사가 error가 아니고 VAD가 성공한 소스다 —
    창이 0개여도 독립적으로 쓸 수 있는 커버리지다.
    """
    reasons: set[str] = set()
    pcm_sources: set[str] = set()
    unavailable: set[str] = set()
    for media, second in prepared.audio_inputs:
        if isinstance(second, runner.UnavailableSourceAudio):
            unavailable.add(media.resource_scoped_id)
            if second.status == "error":
                reasons.add(second.reason_code)
        else:
            pcm_sources.add(second.source_id)
    # audio_input은 클립마다 반복될 수 있다 — 자원 단위로 한 번만 센다.
    audio_failed_resources: set[str] = set()
    audio_skipped_resources: set[str] = set()
    for media, second in prepared.audio_inputs:
        if isinstance(second, runner.UnavailableSourceAudio):
            target = audio_failed_resources if second.status == "error" else audio_skipped_resources
            target.add(media.resource_scoped_id)
    traces = {
        row.source_id: row for row in analyzed.analyses if isinstance(row, runner.SourceTrace)
    }
    transcription_failed = sum(1 for row in traces.values() if row.status == "error")
    if transcription_failed:
        reasons.add("transcription_error")
    vad_failed = sum(1 for row in analyzed.vad_by_source.values() if row.status != "success")
    if vad_failed:
        reasons.add("vad_error")
    sources_total = len(pcm_sources | unavailable)
    usable = sum(
        1
        for source_id in pcm_sources
        if traces.get(source_id) is not None
        and traces[source_id].status != "error"
        and analyzed.vad_by_source.get(source_id) is not None
        and analyzed.vad_by_source[source_id].status == "success"
    )
    return (
        UpstreamStages(
            audio_total=sources_total,
            audio_failed=len(audio_failed_resources),
            audio_skipped=len(audio_skipped_resources),
            # 세 단계 모두 total은 계획한 소스 전체다 — 시도한 수가 아니다.
            transcription_total=sources_total,
            transcription_failed=transcription_failed,
            transcription_skipped=sources_total - len(traces),
            vad_total=sources_total,
            vad_failed=vad_failed,
            vad_skipped=sources_total - len(analyzed.vad_by_source),
            sources_total=sources_total,
            sources_usable=usable,
        ),
        tuple(sorted(reasons)),
    )


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
    observer: transcribe.RequestObserver | None,
    input_sha256: str = "",
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
        # 캐시 재생은 유료 호출이 아니다 — 예약도 확정도 없다(S2B-2b).
        raw = json.loads(cache_path.read_text(encoding="utf-8"))
    elif fixtures is not None:
        fixture = fixtures / f"{planned.window_id}.luna.json"
        if not fixture.is_file():
            raise ValueError(f"Luna 픽스처가 없다: {fixture}")
        raw = json.loads(fixture.read_text(encoding="utf-8"))
    else:
        # Luna 호출 직전 재확인 — 창 게이트가 지난 뒤 프레임 뽑기(ffmpeg) 동안
        # 기록된 취소까지 잡는다. 취소 뒤 새 API 호출은 없다.
        _check_cancellation(workspace, input_revision, attempt_id, input_sha256)
        try:
            raw = transcribe.request_luna_vision(
                client,
                model=settings.model,
                payload=payload,
                online=True,
                # 관찰자는 예약(호출 전)·확정(응답 직후)을 이 경계 안에서 처리한다.
                observer=observer,
                cancellation=lambda: _check_cancellation(
                    workspace, input_revision, attempt_id, input_sha256
                ),
            )
        except transcribe.ObserverFailure:
            # 회계 실패는 이 창의 API 오류가 아니다 — 창 루프가 실행 전체로 올린다.
            raise
        except Exception as exc:  # noqa: BLE001 - API 오류는 이 창을 keep으로 닫는다
            raw = exc
    if isinstance(raw, dict) and not from_cache:
        _write_json(cache_path, raw)
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


def _staged_vision_credentials(project_config: ProjectConfig) -> tuple[str, str | None]:
    """스테이징 분석의 비전 자격 증명 — V2 앱 운반이면 역할 값, 아니면 낡은 공유 경로.

    사전 게이트(`require_paid_roles`)와 실제 공장(`_produce_analysis_outcome`)이
    **같은 문**을 쓴다: 게이트가 본 값과 공장이 받는 값이 어긋나면 준비가
    거짓말이 된다. 마커 없는 직접 경로는 기본선(c96e0d5)의 소비 계약 그대로다
    — `require_api_key(force=True)` + `base_url()`(값이 없으면 공개 엔드포인트).
    V2(`role_credentials_applied`)로 증명된 앱 운반만 별도 역할 값(비전 키·
    엔드포인트, URL 정책 포함)을 쓴다 — 한 키가 양쪽 공급자를 열지 않는다.
    """
    if env.role_credentials_applied():
        return _vision_credentials(project_config)
    return env.require_api_key(force=True), env.base_url()


def require_paid_roles(args: argparse.Namespace) -> None:
    """유료 분석의 모든 역할을 **공급자 construction보다 앞서** 검증한다(S2A-2).

    STT와 비전을 한 문에서 잰다: 어느 하나라도 부재면 전사기조차 만들지 않고
    닫는다. 재개(authorized/analyzing)도 이 문을 다시 지난다. 비전은 운반 형태에
    따라 갈린다(`_staged_vision_credentials`) — V2 앱 운반이면 역할 값 전부,
    마커 없는 직접 경로면 낡은 공유 키 계약 그대로. force는 명시적 --online이
    VLOGSRT_OFFLINE=1을 이기는 기존 계약이다.
    """
    project_config = load_project_config(args.project_config)
    env.require_api_key(force=True)
    _staged_vision_credentials(project_config)


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
    파이프라인 시작 전에 멈춘다. 문구에 값은 없다. 앱 관리(V2) 운반이 적용된
    실행에서는 비지 않은 값을 공장 앞에서 다시 검증한다(S2A-2 단위2) — 직접
    CLI 계약의 엔드포인트 동작은 바꾸지 않는다.
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
    # 새 URL 정책은 **앱 관리(V2) 값에만** 붙는다 — 직접 CLI 계약의 엔드포인트
    # 동작은 그대로다(예: 사용자가 명시한 http 호환 엔드포인트). 적용 시점
    # 검증(배정 전)을 통과한 앱 값이라도 공장 앞에서 한 번 더 견준다.
    if env.role_credentials_applied():
        env.validate_vision_base_url(base_url, secrets=(api_key,))
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
