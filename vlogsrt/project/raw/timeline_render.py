"""원본 범위 타임라인에서 렌더(W-71e, D130) — 클립 그대로 원본에서 한 번 렌더해 초안 폴더에 낸다.

「타임라인으로 보내기」(D126)는 렌더하지 않고 재생 범위만 넘겼다. 사람이 재생기·원본 띠에서 고친
타임라인을 여기서 **한 번만** 원본에서 렌더한다 — 초안과 같은 길(`render_export.export_plan`)로
`export/<시각>/`에 `draft.mp4`·`draft.srt`·`render_report.json`을 낸다. 그래서 결과 카드·「영어
SRT로 변환」·자막 입히기·초안 편집(D117)이 그대로 이 폴더를 쓴다.

- 계획은 등급으로 고르지 않는다 — 클립(원본 프레임 구간)과 문서에 고정된 시퀀스 포맷 그대로
  (`render_plan.plan_from_clips`, 배치는 화면과 같은 `sequence_edges`).
- 자막은 타임라인 문서의 것이다(`timeline_edit.render_cues` — SRT는 `srt_text` 바이트 그대로).
- 뺀 구간은 원본마다(클립이 없는 원본은 통째로) `timeline_cut`으로, 가린 자막은 수로 보고서에
  남는다(규칙 9).
- 화면이 본 리비전이 지금 파일의 리비전이 아니면, 그리고 원본 격자(프레임 길이·수)가 파일의 사실과
  다르면 렌더하지 않는다 — 클립의 프레임 번호가 다른 그림을 가리키게 된다.

쓰는 곳은 `render_export.export_plan` 하나다(이 모듈은 파일을 직접 쓰지 않는다).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

from vlogsrt.project.raw import (
    finalcut_export,
    render_export,
    render_plan,
    timeline_edit,
    timeline_edit_io,
)
from vlogsrt.project.raw.jobs import JobContext
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.raw.timeline_edit import TimelineDocument, TimelineEditError


def _utc_now() -> datetime:
    return datetime.now(UTC)


def export_timeline(
    service: RawWorkspaceService,
    root: Path,
    timeline_id: str,
    revision: int,
    context: JobContext | None = None,
    *,
    now: Callable[[], datetime] = _utc_now,
) -> render_export.ExportResult:
    """타임라인 `timeline_id`의 리비전 `revision`을 렌더한다 — 리비전이 다르면 거부한다."""
    name, current, document = timeline_edit_io.read_document(root, timeline_id)
    if current != revision:
        raise TimelineEditError(
            f"타임라인 `{name}`이 리비전 {current}이다 — 화면이 본 리비전 {revision}이 아니라"
            " 렌더하지 않았다. 새로고침하고 다시 누르세요"
        )
    if not document.clips:
        raise TimelineEditError(f"타임라인 `{name}`에 클립이 없다 — 렌더할 것이 없다")
    finalcut_export._require_no_import(service, root)
    source_ids = list(dict.fromkeys(clip.source_id for clip in document.clips))
    gathered = render_export.gather_for_timeline(service, root, source_ids, context)
    _require_same_grid(document, gathered)
    plan = _plan(document, gathered)
    origin = {render_export.TIMELINE_REPORT_KEY: {"id": name, "revision": current}}
    return render_export.export_plan(service, root, gathered, plan, context, now=now, origin=origin)


def _require_same_grid(
    document: TimelineDocument, gathered: list[render_export.GatheredSource]
) -> None:
    """원본의 지금 격자가 타임라인을 만들 때의 사실과 같은가 — 다르면 이름을 대고 거부한다."""
    facts = {source.source_id: source for source in document.facts.sources}
    changed = []
    for item in gathered:
        fact = facts[item.source.source_id]
        clock = item.source.clock
        if (clock.frame_duration, clock.frame_count) != (fact.frame_duration, fact.frame_count):
            changed.append(
                f"{item.asset.source_name}(타임라인 {fact.frame_count}프레임 ×"
                f" {fact.frame_duration} · 지금 {clock.frame_count}프레임 × {clock.frame_duration})"
            )
    if changed:
        raise render_export.RenderExportError(
            "원본 프레임 격자가 타임라인을 만들 때와 다르다 — 클립 번호가 다른 그림을 가리킨다: "
            + " / ".join(changed)
            + ". 타임라인으로 다시 보내세요"
        )


def _plan(
    document: TimelineDocument, gathered: list[render_export.GatheredSource]
) -> render_plan.RenderPlan:
    cues, counts = timeline_edit.render_cues(document)
    dropped = tuple(
        render_plan.DroppedRange(
            source_id,
            start * _frame_duration(document, source_id),
            end * _frame_duration(document, source_id),
            render_plan.DROP_TIMELINE_CUT,
            render_plan.REVIEW_NONE,
            None,
        )
        for source_id, start, end in timeline_edit.removed_ranges(document)
    )
    clips = [(clip.source_id, clip.start_frame, clip.end_frame) for clip in document.clips]
    try:
        return render_plan.plan_from_clips(
            [item.source for item in gathered],
            clips,
            document.facts.sequence,
            cues,
            counts,
            dropped,
        )
    except render_plan.RenderPlanError as error:
        raise render_export.RenderExportError(f"타임라인 계획을 세우지 못했다 — {error}") from error


def _frame_duration(document: TimelineDocument, source_id: str) -> Fraction:
    return next(s.frame_duration for s in document.facts.sources if s.source_id == source_id)
