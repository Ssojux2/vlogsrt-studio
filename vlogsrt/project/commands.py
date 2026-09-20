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
from pathlib import Path

from vlogsrt import env, log, transcribe
from vlogsrt.config import load_config
from vlogsrt.project import runner
from vlogsrt.project.config import ProjectConfig, load_project_config
from vlogsrt.project.fcpxml import element_tree, parse_project
from vlogsrt.project.final_caption import create_caption_request, generate_final_captions
from vlogsrt.project.media import MediaOverride, load_media_map
from vlogsrt.project.review import build_review_plan, render_approved_roughcut
from vlogsrt.project.timebase import parse_fcpx_time
from vlogsrt.project.workflow_protocol import (
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
_CANDIDATES = "candidates.jsonl"
_PLAN = "edit-plan.jsonl"
_SOURCE = "source.json"


def project_command(args: argparse.Namespace) -> int:
    """단계형 명령의 단일 CLI 경계. 진단용 run만 기존 실행기를 유지한다."""
    if args.project_command == "run":
        return project_run(args)
    handlers = {
        "receive": _project_receive,
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
    project = root.find("library/event/project")
    sequence = root.find("library/event/project/sequence")
    if project is None or sequence is None:
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


def _project_analyze(args: argparse.Namespace) -> int:
    workspace = args.workspace
    journal = WorkflowJournal.open(workspace / _JOURNAL)
    if journal.state not in {"received", "analyzing"}:
        raise ValueError(f"analyze는 received/analyzing 상태에서만 가능하다: {journal.state}")
    candidates = tuple(_read_candidates(args.candidates, journal.revision.revision))
    if journal.state == "received":
        # 외부 작업 프로세스 결과를 전부 검사·저장한 뒤에만 상태를 움직인다.
        _write_messages(workspace / _CANDIDATES, candidates)
        journal.transition(journal.revision.revision, "analyzing")
    else:
        stored = tuple(_read_candidates(workspace / _CANDIDATES, journal.revision.revision))
        if stored != candidates:
            raise ValueError("재개한 분석 후보가 저장된 analyzing 결과와 다르다")
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
    if journal.state != "roughcut_imported":
        raise ValueError(f"caption은 roughcut_imported 상태에서만 가능하다: {journal.state}")
    request = create_caption_request(
        journal.revision.revision, args.render_audio, args.final_fcpxml
    )
    delivery = generate_final_captions(
        request,
        load_config(),
        args.out,
        online=bool(args.online),
        confirmed_cost=bool(args.yes),
    )
    _write_messages(workspace / "caption-request.jsonl", (request,))
    journal.transition(journal.revision.revision, "caption_requested")
    _write_messages(workspace / "caption-result.jsonl", (delivery.result,))
    journal.transition(journal.revision.revision, "caption_ready")
    if delivery.result.srt_path is None:
        log.out("자막 상태: 발화 없음")
    else:
        log.out(f"srt: {delivery.result.srt_path}")
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
        _CANDIDATES,
        _PLAN,
        "caption-request.jsonl",
        "caption-result.jsonl",
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
