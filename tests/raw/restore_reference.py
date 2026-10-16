"""「자동 제외 전체 복원」의 옛 걸음 그대로(W-24 세션 G 이전, 353c544) — 바이트 대조 시험의 기준.

세션 G는 사슬 걸음을 한 질의(`revision_lineage`)로 바꿨다. 같은 작업공간 두 벌에 하나는 지금
`restore_automatic`, 하나는 이 옛 걸음(리비전마다 연결을 열고 문서 전체를 푼다)을 돌려 새 머리의
문서 바이트가 같은지 본다. 시험 전용이다 — 제품 코드는 이것을 쓰지 않는다.
"""

from __future__ import annotations

from dataclasses import replace

from vlogsrt.project.raw import review_workspace as review_module
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import ReviewRevision, confirm_tags, parse_revision


def reference_restore(facade: ReviewWorkspaceFacade) -> ReviewRevision:
    """353c544의 `restore_automatic` 본문 그대로 — 걸음마다 리비전 문서를 읽어 푼다."""
    head = facade.load_head().revision
    manually_selected: set[str] = set()
    cursor = head
    legacy = facade._legacy_replay(head)
    while cursor.parent_revision_id is not None:
        if legacy is not None and 0 < cursor.sequence <= legacy.end:
            if facade._replay_matches(head.base_plan_id, legacy):
                break
            legacy = None
        label = (cursor.edit.notes or "") if cursor.edit and cursor.edit.kind == "note" else ""
        if label.startswith(review_module.ADOPTION_REPLAY_LABEL + " ← "):
            break
        if cursor.edit and cursor.edit.kind == "selection":
            manually_selected.update(cursor.edit.segment_ids)
        document, _ = facade._store.revision_document(
            facade._project_id,
            head.base_plan_id,
            facade._sequence_of_revision_id(cursor.parent_revision_id),
        )
        cursor = parse_revision(document)
    auto = review_module._AUTO_NOTE_PREFIX
    segments = tuple(
        replace(s, selection="included", visual_tags=confirm_tags(s.visual_tags))
        if s.notes.startswith(auto) and s.segment_id not in manually_selected
        else s
        for s in head.segments
    )
    return facade._publish_snapshot(head, segments, "자동 제외 전체 복원")


def head_document(facade: ReviewWorkspaceFacade) -> tuple[str, str]:
    """지금 머리의 저장된 (문서 JSON, 지문) — 두 벌의 바이트를 견줄 값."""
    head = facade.load_head().revision
    return facade._store.revision_document(facade._project_id, head.base_plan_id, head.sequence)
