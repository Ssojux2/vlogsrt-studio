"""One bounded selection approval covers STT and Luna; completed caches need no calls."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Any

from vlogsrt.project.raw import browser_runtime, cross_check
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.models import (
    AnalysisEstimate,
    AnalysisSemanticEstimate,
    RawWorkspaceError,
)

MODE_FULL = "full"
"""전사 + 영상 분석 — 승인 하나로 두 단계가 이어진다(지금까지의 흐름 그대로)."""
MODE_TRANSCRIBE = "transcribe_only"
"""「전사만」(D59 19b) — 1단계(전사·원본 자막·장면·프레임)만 돌고 영상 분석 앞에서 멈춘다."""
MODES = (MODE_FULL, MODE_TRANSCRIBE)
TRANSCRIBED_STAGE = "전사 완료 · 분석 대기"
"""「전사만」이 끝난 영상의 단계 — 영상 분석(추천 판정)은 아직 승인하지 않았다."""
RELISTEN_STAGE = "다시 듣기 대기 · 승인 필요"
"""「다시 듣기」를 고른 영상의 단계 — 견적을 확인하고 승인해야 다시 듣는다(유료)."""


@dataclass(frozen=True)
class SelectionQuote:
    asset_ids: tuple[str, ...]
    estimates: tuple[AnalysisEstimate, ...]
    digest: str
    workflow: RawWorkflowConfig
    approved: bool = False
    mode: str = MODE_FULL
    """승인 범위 — digest에 접혀 있어 「전사만」 승인을 전체 실행으로(또는 거꾸로) 쓸 수 없다."""
    cross_tag: str | None = None
    """digest에 접힌 교차 확인 꼬리표(D59 19d, 꺼지면 None) — 승인이 묶은 글자 요청 신원이다."""


def quote(
    service: Any, root: Path, asset_ids: tuple[str, ...], mode: str = MODE_FULL
) -> SelectionQuote:
    """승인 견적 — 원본 재검증은 stat 믿기다(D62 A2).

    앱 시작·자막·용어집 편집마다 라이브러리 전체를 다시 해시하면 원본 18개에 20–35분이 걸렸다.
    digest가 묶는 것은 기록된 `content_sha256`(stat이 아니다)이고, 유료 요청 앞에서는 1단계 승인
    만들기(`mint_asr_approvals`)·실행 문(`start_analysis`)·분석 본체(`_analyze_source`)가 각각
    완전 해시한다 — stat을 그대로 둔 채 내용만 바뀐 원본도 그 자리에서 0송신으로 닫힌다.
    """
    estimates = service.analysis_estimate(root, asset_ids, trust_stat=True)
    return _assemble_quote(service, root, asset_ids, tuple(estimates), mode)


def rebound_quote(
    service: Any,
    root: Path,
    measured: SelectionQuote,
    asset_ids: tuple[str, ...],
    mode: str | None = None,
) -> SelectionQuote:
    """이미 잰 견적의 측정값으로 영상 일부(또는 전부)의 견적을 다시 묶는다 — ffprobe 없음.

    측정값 한 줄은 영상 하나의 것이라 다른 영상에 의존하지 않는다. 결속 지문(미디어
    참조·원본 자막·다시 듣기 차례)과 설정은 지금 값을 다시 읽으므로, 전부를 다시 묶어 같은
    digest가 나오면 측정 뒤로 결속이 바뀌지 않았다는 뜻이다. 측정값 자체의 변화는 실행 문
    (`execute`)이 유료 호출 전에 영상마다 다시 재어 잡는다. 범위(`mode`)를 주지 않으면 잰
    견적의 범위 그대로다 — 범위만 바꿔 묶는 데도 쓴다(측정은 범위와 무관하다).
    """
    wanted = set(asset_ids)
    estimates = tuple(e for e in measured.estimates if e.asset_id in wanted)
    missing = wanted - {e.asset_id for e in estimates}
    if missing:
        raise RawWorkspaceError(f"측정한 견적에 없는 영상입니다: {sorted(missing)}")
    return _assemble_quote(service, root, asset_ids, estimates, mode or measured.mode)


def _assemble_quote(
    service: Any,
    root: Path,
    asset_ids: tuple[str, ...],
    estimates: tuple[AnalysisEstimate, ...],
    mode: str = MODE_FULL,
) -> SelectionQuote:
    """견적 묶음 — 측정값·원본별 결속·범위를 digest 하나로 접는다.

    전체 범위·차례 0의 digest는 19b 이전 바이트 그대로다: 다시 듣기 차례는 0이 아닐 때만
    결속 줄의 넷째 칸이 되고, 범위는 「전사만」일 때만 끝에 붙는다(D59 19b). 「다른 귀로 교차
    확인」(D59 19d)이 켜져 있으면 줄마다 글자 분량(측정값 쪽)과 꼬리표(결속 쪽)가 더 붙는다 — 꺼져
    있으면 19d 이전 바이트 그대로다. 글자 분량은 측정이 아니라 지금의 기록·캐시에서 다시 잰다
    (ffprobe 없음) — 그래서 다시 묶을 때(`rebound_quote`)도 맞다.
    """
    import hashlib

    if mode not in MODES:
        raise RawWorkspaceError(f"알 수 없는 승인 범위입니다: {mode!r}")
    workspace = service._workspace(root)
    estimates = cross_check.with_text_passes(workspace, estimates)
    tag = cross_check.binding_tag(workspace)
    payload = browser_runtime.asr_estimate_digest(estimates) + json.dumps(
        [_quote_binding(workspace, a, tag) for a in asset_ids]
    )
    if mode != MODE_FULL:
        payload += "\0mode=" + mode
    digest = hashlib.sha256(payload.encode()).hexdigest()
    workflow = service.settings(root)
    return SelectionQuote(asset_ids, estimates, digest, workflow, mode=mode, cross_tag=tag)


def _quote_binding(workspace: Any, asset_id: str, cross_tag: str | None) -> tuple[str, ...]:
    """원본 하나의 견적 결속 — 미디어 참조·원본 자막, 0이 아닌 다시 듣기 차례, 교차 확인 꼬리표.

    교차 확인이 켜져 있으면 자막 지문은 두 번째 귀가 얹은 글자를 뺀 것이다
    (`source_transcript.binding_fingerprint`) — 그 글자는 꼬리표가 묶으므로, 실행이 글자를 얹어도
    다음 견적이 같은 결속을 본다(19c-5 알려진 한계를 닫는다, D59 19d)."""
    from vlogsrt.project.raw import asr_epoch, media_refs, source_transcript

    row: tuple[str, ...] = (
        asset_id,
        media_refs.fingerprint(workspace, asset_id),
        source_transcript.binding_fingerprint(workspace, asset_id, cross_tag is not None),
    )
    tag = asr_epoch.binding_tag(asr_epoch.current(workspace, asset_id))
    if tag is not None:
        row = (*row, tag)
    return row if cross_tag is None else (*row, cross_tag)


def _order_free_fingerprints(service: Any, root: Path, assets: dict[str, Any]) -> set[str]:
    """그 원본들의 분석 지문 — 어느 전체 순서 리비전 아래에서 잰 것이든 받는다(D53).

    원본마다의 분석 내용은 순서와 무관하다. 순서만 바뀌었다고(촬영 시각 순 맞춤·새 영상
    가져오기) 끝난 분석을 버리면 같은 영상에 다시 승인과 유료 호출을 요구하게 된다.
    """
    workflow = service.settings(root)
    bindings = service.source_bindings(root, assets)
    return {
        service._analysis_fingerprint(
            order.revision_id,
            assets,
            RawUnderstandingSettings(),
            workflow,
            "live",
            root=root,
            bindings=bindings,
        )
        for order in service.list_global_source_orders(root)
    }


def cached_revision(service: Any, root: Path, asset_ids: tuple[str, ...]) -> str | None:
    workspace = service._workspace(root)
    if service.latest_global_source_order(root) is None:
        return None
    assets = {a.asset_id: a for a in service.list_source_assets(root) if a.asset_id in asset_ids}
    from vlogsrt.project.raw.analysis_workflow import _require_source_unchanged

    try:
        for asset in assets.values():
            # 카드를 누를 때마다 9GB를 다시 해시하지 않는다 — stat이 같으면 기록을 믿는다(D62).
            _require_source_unchanged(
                workspace, workspace.source_media_path(asset), asset, trust_stat=True
            )
    except (OSError, RawWorkspaceError):
        return None
    expected = _order_free_fingerprints(service, root, assets)
    workflow = service.settings(root)
    store = AnalysisStore(workspace)
    for revision in reversed(store.list_revisions()):
        if _same_analysis(revision, expected, workflow) and _documents_reusable(
            service, root, store, revision.revision_id, asset_ids
        ):
            return revision.revision_id
    return None


def _same_analysis(revision: Any, expected: set[str], workflow: RawWorkflowConfig) -> bool:
    """끝났고, 지문(어느 순서 리비전이든)·설정·모델이 지금과 같은 분석 리비전인가."""
    models = {"timing": workflow.models.timing_model, "luna": workflow.models.luna_model}
    return (
        revision.status == "complete"
        and revision.fingerprint in expected
        and json.loads(revision.settings_json) == asdict(RawUnderstandingSettings())
        and json.loads(revision.models_json) == models
    )


def _documents_reusable(
    service: Any, root: Path, store: AnalysisStore, revision_id: str, asset_ids: tuple[str, ...]
) -> bool:
    rows = {row.asset_id: row for row in store.list_source_results(revision_id)}
    if not all(a in rows and rows[a].status in ("analyzed", "cached") for a in asset_ids):
        return False
    return all(_document_reusable(service, root, rows[asset_id]) for asset_id in asset_ids)


def _document_reusable(service: Any, root: Path, row: Any) -> bool:
    """완성 문서가 읽히고, 미디어·자막 결속이 지금과 같고, 창 판정이 모두 받아들여졌는가.

    자막은 결속(교차 확인이 켜지면 두 번째 귀 글자를 뺀 지문, D59 19d)이 아니라 **실제** 지문으로
    본다(`_complete_source_current`와 같은 대조) — 용어집을 되돌리면 꼬리표는 옛 리비전과 같아지지만
    지금 심긴 글자는 다른 용어집의 것이라, 그 글자로 판정한 분석이 아니면 다시 쓰지 않는다."""
    if not row.document_path:
        return False
    try:
        document = json.loads((root / row.document_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    from vlogsrt.project.raw import source_transcript
    from vlogsrt.project.raw.media_refs import ORIGINAL_ONLY_FINGERPRINT, fingerprint

    workspace = service._workspace(root)
    if document.get("media_fingerprint", ORIGINAL_ONLY_FINGERPRINT) != fingerprint(
        workspace, row.asset_id
    ):
        return False
    transcript = document.get("transcript_fingerprint", source_transcript.ORIGINAL_FINGERPRINT)
    if transcript != source_transcript.fingerprint(workspace, row.asset_id):
        return False
    return all(a.get("status") == "accepted" for a in document.get("annotations", []))


def execute(
    service: Any,
    root: Path,
    approved: SelectionQuote,
    transport: Any,
    context: Any,
    report: Callable[[str, str | None], None],
) -> str:
    """승인한 견적대로 실행한다 — 전체면 두 단계, 「전사만」이면 1단계만(D59 19b).

    견적은 승인한 범위로 다시 재어 digest를 맞춘다 — 범위가 digest에 접혀 있으므로 범위만 바꾼
    승인은 여기서 0송신으로 닫힌다. 「전사만」은 의미 분석 클라이언트를 받지 않는다(Claude 키가
    없어도 된다)."""
    if not approved.approved:
        raise RawWorkspaceError("선택 영상의 전사·Luna 분석 승인이 필요합니다")
    # 글자 요청 신원(D59 19d)은 다시 재기 **전에** 읽는다 — 그사이 용어집이 바뀌면 아래에서 닫힌다.
    identity = cross_check.request_identity(service._workspace(root))
    current = quote(service, root, approved.asset_ids, approved.mode)
    if current.digest != approved.digest or current.workflow != approved.workflow:
        raise RawWorkspaceError("선택 영상이나 설정이 바뀌었습니다. 현재 범위를 다시 확인하세요")
    text_identity = _approved_text_identity(current, identity)
    transcribe_only = approved.mode == MODE_TRANSCRIBE
    # 의미 분석 클라이언트를 먼저 받는다 — 키가 없으면 전사도 사기 전에 닫힌다(D54).
    client = (
        None if transcribe_only else transport.semantic_client(approved.workflow.models.luna_model)
    )
    context.check_cancel()
    first, revision_id = _run_transcription(
        service, root, approved, transport, context, report, text_identity
    )
    if transcribe_only:
        return revision_id
    _run_semantic(service, root, approved, transport, context, report, first, revision_id, client)
    return revision_id


def _approved_text_identity(current: SelectionQuote, identity: str) -> str | None:
    """승인한 digest가 묶은 글자 요청 신원(D59 19d) — 꺼져 있으면 None(글자 요청 없음).

    다시 잰 견적의 digest가 승인한 것과 같으므로 그 꼬리표가 승인이 본 꼬리표다. 다시 재기 전에 읽은
    신원과 다르면 그 사이 용어집·문맥이 바뀐 것이라 0송신으로 닫는다."""
    if current.cross_tag is None:
        return None
    if current.cross_tag != cross_check.TAG_PREFIX + identity:
        raise RawWorkspaceError(
            "용어집이 바뀌었습니다. 새 견적을 확인하고 다시 승인하세요 — 유료 호출은 시작하지"
            " 않았습니다"
        )
    return identity


def _run_transcription(
    service: Any,
    root: Path,
    approved: SelectionQuote,
    transport: Any,
    context: Any,
    report: Callable[[str, str | None], None],
    text_identity: str | None = None,
) -> tuple[Any, str]:
    """1단계 — 전사 승인을 만들고 전사·원본 자막·장면·프레임을 기다린다(의미 분석 0회)."""
    browser_runtime.prepare_approved_browser_network()
    service.update_settings(root, replace(approved.workflow, network_mode="online"))
    # 글자 칸(D59 19d)은 사람이 본 견적 줄과 그 digest가 묶은 신원에서만 — 지금 용어집을 다시 읽지
    # 않는다(그사이 저장된 용어집은 승인 밖이다 — 보낼 때 신원 대조가 닫는다).
    asr = browser_runtime.mint_asr_approvals(
        service,
        root,
        approved.asset_ids,
        on=transport.on,
        quoted=approved.estimates,
        text_identity=text_identity,
    )
    first = service.start_analysis(
        root,
        asset_ids=approved.asset_ids,
        mode="live",
        asr_approvals=asr,
        sender=transport.sender,
        cfg=transport.cfg,
        on=transport.on,
    )
    job = service.get_job(root, first.job_id)
    revisions = AnalysisStore(service._workspace(root)).list_revisions()
    revision = next(r for r in reversed(revisions) if r.fingerprint == job.input_fingerprint)
    report("전사·장면 준비", revision.revision_id)
    service.wait_child_job(root, first, context)
    context.check_cancel()
    return first, revision.revision_id


def _run_semantic(
    service: Any,
    root: Path,
    approved: SelectionQuote,
    transport: Any,
    context: Any,
    report: Callable[[str, str | None], None],
    first: Any,
    revision_id: str,
    client: object,
) -> None:
    """2단계 — 실측이 승인 안이면 의미 승인을 만들고 영상 분석을 기다린다."""
    estimates = service.analysis_semantic_estimate(root, revision_id)
    _require_within_approval(approved.estimates, estimates)
    luna = browser_runtime.mint_luna_approvals(
        service,
        root,
        revision_id,
        tuple(e.asset_id for e in estimates if e.asset_id in approved.asset_ids),
        on=transport.on,
    )
    report("Luna 추천 구간 분석", revision_id)
    second = service.resume_analysis_semantic(
        root, first.job_id, luna_approvals=luna, client=client, on=transport.on
    )
    service.wait_child_job(root, second, context)
    context.check_cancel()


def _require_within_approval(
    approved: Sequence[AnalysisEstimate], measured: Sequence[AnalysisSemanticEstimate]
) -> None:
    """전사 뒤 실측이 승인 범위 안인가 — 요청 수, 그리고 (Claude면) 비용 상한(D54).

    사람이 본 금액보다 큰 상한으로는 2단계 승인을 만들지 않는다. 상한이 없는 견적(Luna)은
    지금처럼 요청 수만 본다.
    """
    quoted = {e.asset_id: e for e in approved}
    for estimate in measured:
        seen = quoted.get(estimate.asset_id)
        if seen is None:
            continue
        if estimate.luna_request_count > max(1, seen.luna_request_count):
            raise RawWorkspaceError(
                "실측 분석량이 승인한 요청 상한을 넘었습니다. 새 범위를 확인하세요"
            )
        if seen.ceiling_usd is not None and (
            estimate.ceiling_usd is None
            or Decimal(estimate.ceiling_usd) > Decimal(seen.ceiling_usd)
        ):
            raise RawWorkspaceError(
                "실측 분석 비용 상한이 승인한 상한을 넘었습니다. 새 범위를 확인하세요"
            )


def publish_review(service: Any, root: Path, revision_id: str, context: Any) -> str:
    context.check_cancel()
    plan = service.plan_segmentation_from_analysis(root, revision_id, use_current_order=True)
    facade = service.review_workspace_facade(root)
    review = facade.initialize_review(
        plan.revision_id,
        revision_id,
        preserve_source_ids=service.unanalyzed_sources(root, plan, revision_id),
    )
    # Recommendations become visible immediately. Extraction belongs to export,
    # and source playback was already prepared when the video was selected.
    context.check_cancel()
    return review.revision_id


_PERSISTED_FIELDS = (
    "token",
    "paths",
    "asset_ids",
    "stage",
    "revision",
    "job_id",
    "preview_job_id",
    "review_job_id",
    "error",
    "connection_required",
    "view_source_id",
)


def save_state(service: Any, root: Path, state: dict[str, Any], *, selected: bool = True) -> None:
    payload = {key: state[key] for key in _PERSISTED_FIELDS if key in state}
    with service._workspace(root).connect() as connection:
        encoded = json.dumps(payload, ensure_ascii=False)
        keys = ["selection_task:" + str(state["token"])]
        if selected:
            keys.append("selection_analysis_state")
        for key in keys:
            connection.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, encoded),
            )
        connection.commit()


def _restored(root: Path, state: dict[str, Any], jobs: Mapping[str, Any]) -> dict[str, Any]:
    state.update(
        root=root, paths=tuple(state.get("paths", ())), asset_ids=tuple(state.get("asset_ids", ()))
    )
    job_id = state.get("job_id")
    job = jobs.get(job_id) if isinstance(job_id, str) else None
    if state.get("stage") not in (
        "추천 구간 준비 완료",
        "부분 완료 · 확인 필요",
        "분석 확인 필요",
        "취소됨",
        "자막 변경 · 갱신 필요",
        TRANSCRIBED_STAGE,
        RELISTEN_STAGE,
    ) and (job is None or job.status not in ("queued", "running")):
        state["stage"] = (
            "취소됨" if job and job.status == "cancelled" else "중단 · 재시도 승인 필요"
        )
    return state


def _restored_all(service: Any, root: Path, payloads: Sequence[str]) -> list[dict[str, Any]]:
    """저장된 상태들 — 가리키는 작업은 아이디로 한 번에 찾는다(W-24 세션 G).

    예전에는 상태마다 최근 작업 1000줄을 읽었다(실데이터: 상태 108개 ≈ 260 ms) — 그리고 작업이
    1000개를 넘으면 창 밖의 끝난 작업을 못 찾아 끝난 상태를 「중단」으로 바꿔 불렀다.
    """
    states = [json.loads(payload) for payload in payloads]
    wanted = {state["job_id"] for state in states if isinstance(state.get("job_id"), str)}
    jobs = service.jobs_by_id(root, wanted) if wanted else {}
    return [_restored(root, state, jobs) for state in states]


def load_state(service: Any, root: Path) -> dict[str, Any]:
    with service._workspace(root).connect() as connection:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='selection_analysis_state'"
        ).fetchone()
    return _restored_all(service, root, (row[0],))[0] if row else {}


def load_states(service: Any, root: Path) -> tuple[dict[str, Any], ...]:
    with service._workspace(root).connect() as connection:
        rows = connection.execute(
            "SELECT value FROM meta WHERE key LIKE 'selection_task:%' ORDER BY rowid"
        ).fetchall()
    return tuple(_restored_all(service, root, [row[0] for row in rows]))


def completion(service: Any, root: Path, revision_id: str) -> str:
    revision = next(
        r
        for r in AnalysisStore(service._workspace(root)).list_revisions()
        if r.revision_id == revision_id
    )
    if revision.status == "complete":
        return "추천 구간 준비 완료"
    if revision.status == "awaiting_semantic":
        return TRANSCRIBED_STAGE  # 「전사만」이 끝났다 — 영상 분석은 승인 전이다(D59 19b)
    return "분석 확인 필요" if revision.status == "failed" else "부분 완료 · 확인 필요"


def completion_error(service: Any, root: Path, revision_id: str) -> str | None:
    errors = []
    for row in AnalysisStore(service._workspace(root)).list_source_results(revision_id):
        if row.error:
            errors.append(row.error)
        if row.document_path:
            try:
                document = json.loads((root / row.document_path).read_text(encoding="utf-8"))
                errors.extend(
                    a["defect"] for a in document.get("annotations", []) if a.get("defect")
                )
                errors.extend(
                    c["failure"]
                    for c in document.get("asr", {}).get("chunks", [])
                    if c.get("failure")
                )
            except (OSError, ValueError):
                pass
    return " · ".join(dict.fromkeys(errors))[:600] or None
