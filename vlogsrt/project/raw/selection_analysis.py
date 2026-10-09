"""One bounded selection approval covers STT and Luna; completed caches need no calls."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

from vlogsrt.project.raw import browser_runtime
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.models import AnalysisEstimate, RawWorkspaceError


@dataclass(frozen=True)
class SelectionQuote:
    asset_ids: tuple[str, ...]
    estimates: tuple[AnalysisEstimate, ...]
    digest: str
    workflow: RawWorkflowConfig
    approved: bool = False


def quote(service: Any, root: Path, asset_ids: tuple[str, ...]) -> SelectionQuote:
    return _assemble_quote(
        service, root, asset_ids, tuple(service.analysis_estimate(root, asset_ids))
    )


def rebound_quote(
    service: Any, root: Path, measured: SelectionQuote, asset_ids: tuple[str, ...]
) -> SelectionQuote:
    """이미 잰 견적의 측정값으로 영상 일부(또는 전부)의 견적을 다시 묶는다 — ffprobe 없음.

    측정값 한 줄은 영상 하나의 것이라 다른 영상에 의존하지 않는다. 결속 지문(미디어
    참조·원본 자막)과 설정은 지금 값을 다시 읽으므로, 전부를 다시 묶어 같은 digest가
    나오면 측정 뒤로 결속이 바뀌지 않았다는 뜻이다. 측정값 자체의 변화는 실행 문
    (`execute`)이 유료 호출 전에 영상마다 다시 재어 잡는다.
    """
    wanted = set(asset_ids)
    estimates = tuple(e for e in measured.estimates if e.asset_id in wanted)
    missing = wanted - {e.asset_id for e in estimates}
    if missing:
        raise RawWorkspaceError(f"측정한 견적에 없는 영상입니다: {sorted(missing)}")
    return _assemble_quote(service, root, asset_ids, estimates)


def _assemble_quote(
    service: Any,
    root: Path,
    asset_ids: tuple[str, ...],
    estimates: tuple[AnalysisEstimate, ...],
) -> SelectionQuote:
    import hashlib

    from vlogsrt.project.raw import media_refs, source_transcript

    bindings = [
        (
            a,
            media_refs.fingerprint(service._workspace(root), a),
            source_transcript.fingerprint(service._workspace(root), a),
        )
        for a in asset_ids
    ]
    digest = hashlib.sha256(
        (browser_runtime.asr_estimate_digest(estimates) + json.dumps(bindings)).encode()
    ).hexdigest()
    return SelectionQuote(asset_ids, estimates, digest, service.settings(root))


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
            _require_source_unchanged(workspace.source_media_path(asset), asset)
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
    """완성 문서가 읽히고, 미디어 결속이 지금과 같고, 창 판정이 모두 받아들여졌는가."""
    if not row.document_path:
        return False
    try:
        document = json.loads((root / row.document_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    from vlogsrt.project.raw.media_refs import ORIGINAL_ONLY_FINGERPRINT, fingerprint

    if document.get("media_fingerprint", ORIGINAL_ONLY_FINGERPRINT) != fingerprint(
        service._workspace(root), row.asset_id
    ):
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
    if not approved.approved:
        raise RawWorkspaceError("선택 영상의 전사·Luna 분석 승인이 필요합니다")
    current = quote(service, root, approved.asset_ids)
    if current.digest != approved.digest or current.workflow != approved.workflow:
        raise RawWorkspaceError("선택 영상이나 설정이 바뀌었습니다. 현재 범위를 다시 확인하세요")
    context.check_cancel()
    browser_runtime.prepare_approved_browser_network()
    service.update_settings(root, replace(approved.workflow, network_mode="online"))
    asr = browser_runtime.mint_asr_approvals(service, root, approved.asset_ids, on=transport.on)
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
    estimates = service.analysis_semantic_estimate(root, revision.revision_id)
    limits = {e.asset_id: max(1, e.luna_request_count) for e in approved.estimates}
    for estimate in estimates:
        if estimate.asset_id in limits and estimate.luna_request_count > limits[estimate.asset_id]:
            raise RawWorkspaceError(
                "실측 분석량이 승인한 요청 상한을 넘었습니다. 새 범위를 확인하세요"
            )
    luna = browser_runtime.mint_luna_approvals(
        service,
        root,
        revision.revision_id,
        tuple(e.asset_id for e in estimates if e.asset_id in approved.asset_ids),
        on=transport.on,
    )
    report("Luna 추천 구간 분석", revision.revision_id)
    second = service.resume_analysis_semantic(
        root, first.job_id, luna_approvals=luna, client=transport.client, on=transport.on
    )
    service.wait_child_job(root, second, context)
    context.check_cancel()
    return revision.revision_id


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


def _restored(service: Any, root: Path, payload: str) -> dict[str, Any]:
    state = json.loads(payload)
    state.update(
        root=root, paths=tuple(state.get("paths", ())), asset_ids=tuple(state.get("asset_ids", ()))
    )
    jobs = {j.job_id: j for j in service.list_jobs(root, limit=1000)}
    job = jobs.get(state.get("job_id"))
    if state.get("stage") not in (
        "추천 구간 준비 완료",
        "부분 완료 · 확인 필요",
        "분석 확인 필요",
        "취소됨",
        "자막 변경 · 갱신 필요",
    ) and (job is None or job.status not in ("queued", "running")):
        state["stage"] = (
            "취소됨" if job and job.status == "cancelled" else "중단 · 재시도 승인 필요"
        )
    return state


def load_state(service: Any, root: Path) -> dict[str, Any]:
    with service._workspace(root).connect() as connection:
        row = connection.execute(
            "SELECT value FROM meta WHERE key='selection_analysis_state'"
        ).fetchone()
    return _restored(service, root, row[0]) if row else {}


def load_states(service: Any, root: Path) -> tuple[dict[str, Any], ...]:
    with service._workspace(root).connect() as connection:
        rows = connection.execute(
            "SELECT value FROM meta WHERE key LIKE 'selection_task:%' ORDER BY rowid"
        ).fetchall()
    return tuple(_restored(service, root, row[0]) for row in rows)


def completion(service: Any, root: Path, revision_id: str) -> str:
    revision = next(
        r
        for r in AnalysisStore(service._workspace(root)).list_revisions()
        if r.revision_id == revision_id
    )
    if revision.status == "complete":
        return "추천 구간 준비 완료"
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
