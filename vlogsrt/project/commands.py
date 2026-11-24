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

비용 길이 안전장치(레거시 `_confirm_cost`에 해당하는 것)는 없다 — 비용 추정도 없다
(D110). `inspect`는 승인이 묶을 값(입력·설정 지문·미디어 권한)만 인쇄한다.
`run`은 임시 명령이다: 최종 표면은 `inspect | cut | approve | caption`이고
`run`을 두지 않는다(사람 게이트 우회).

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
from datetime import UTC, datetime
from pathlib import Path

from vlogsrt import env, log, oscompat
from vlogsrt.config import load_config
from vlogsrt.project import request_accounting, topology
from vlogsrt.project.clips import collect_clip_paths, probe_clips, synthesize_fcpxml
from vlogsrt.project.config import load_project_config
from vlogsrt.project.fcpxml import element_tree, parse_project
from vlogsrt.project.media import (
    MediaOverride,
    ResolvedMedia,
    load_media_map,
    resolve_media,
)
from vlogsrt.project.review import (
    build_review_plan,
    render_approved_roughcut,
)
from vlogsrt.project.timebase import parse_fcpx_time
from vlogsrt.project.workflow_analysis import (
    AnalysisSettings,
    build_analysis_estimate,
    effective_analysis_settings_sha256,
)
from vlogsrt.project.workflow_protocol import (
    AnalysisEstimate,
    CancellationRequest,
    CutCandidate,
    EditPlan,
    ProjectRevision,
    ProtocolMessage,
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
_ENGINE_INVOCATION_ENV = "VLOGSRT_ENGINE_INVOCATION_ID"


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
    """단계형 명령의 단일 CLI 경계 — whisper로만 돌던 analyze·caption·run은 지웠다(D163)."""
    handlers = {
        "receive": _project_receive,
        "inspect": _project_inspect,
        "review": _project_review,
        "roughcut": _project_roughcut,
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
    """네트워크 없이 준비를 확인하고 승인이 묶을 범위를 인쇄한다 — 상태·파일은 그대로다(D110).

    비용·가격은 재지 않는다. 인쇄한 범위 기록의 입력 리비전·설정 지문·미디어 권한 리비전이
    `AnalysisAuthorization`이 묶을 값이다."""
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state not in {"received", "estimate_ready"}:
        raise ValueError(f"inspect는 received 상태에서만 가능하다: {journal.state}")
    # 키 없이도 실제 준비를 잰다(S2A-2) — 오디오 위상과 권한 문서 커버리지.
    scope = _current_analysis_estimate(args, workspace, journal.revision, verify_prerequisites=True)
    _protocol_out(scope)
    return 0


def _current_analysis_estimate(
    args: argparse.Namespace,
    workspace: Path,
    revision: ProjectRevision,
    *,
    verify_prerequisites: bool = False,
) -> AnalysisEstimate:
    """지금 잰 분석 범위 — `verify_prerequisites`면 실제 준비를 잰다(S2A-2). 비용은 없다(D110).

    실제 원본의 오디오 위상과(위상 관문) 스테이징된 권한 문서의 루트 안
    커버리지를 같이 잴 수 있다. 둘 다 키가 없어도 돈다(읽기 전용) — 지원하지 않는
    구성을 드러내는 것이 이 매개변수의 목적이다. 유료 실행의 재개도 이 자리를 다시
    지나므로, 저장된 범위가 있어도 실행 때 다시 잰다.

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


# ─────────────────────────────────────────────────────────────
#  STAB-S4A — 공유 받기로 시작한 자막 job: 추정과 실행이 갈린다
# ─────────────────────────────────────────────────────────────


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
