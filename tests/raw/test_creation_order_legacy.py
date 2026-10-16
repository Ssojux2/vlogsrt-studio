"""표지 없는 옛 입양(D53 이전 앱)의 되풀이 창은 개수가 아니라 내용으로 확인한다(D53).

검증 5차가 찾은 길을 실제 시험으로 옮겼다(2026-10-09). 옛 앱은 입양 표지를 쓰지 않았고 쓰기
자물쇠도 없었다 — 되풀이 도중에 사람의 편집이 끼어들 수 있었다(사용자의 앱은 지금도 옛 빌드다).
되풀이 길이를 개수로만 다시 세어 그 창을 통째로 건너뛰면, 창 안에 낀 사람 메모(기계 메모 머리를
단 것 — 내용으로는 보이지 않는다)나, 개수가 하나 넘쳐 창에 먹힌 입양 직후의 편집이 조용히
사라졌다. 이제 창 안의 편집 하나하나가 옛 조각의 되풀이와 같은지 확인하고, 하나라도 다르면 그
기본의 편집을 모두 사람 편집으로 걸어 본다 — 확인하지 않은 편집은 건너뛰지 않는다. 진짜 옛
입양이 창 확인을 통과하는 쪽은 `test_creation_order_whole_snapshots`의 표지 없는 입양 시험이
같은 도우미로 본다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from tests.raw import test_creation_order as co
from vlogsrt.project.raw import review_workspace as review_module
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    chronological,
    unreviewed_auto_exclusions,
)

needs_ffmpeg = co.needs_ffmpeg
MEMO = "확인 필요: 사람이 덧붙인 말"  # 기계 메모 머리를 단 사람 메모 — 내용으로는 안 보인다


class _Context:
    def check_cancel(self) -> None:
        return None


def old_build_adopt(
    facade: ReviewWorkspaceFacade,
    head,
    monkeypatch: pytest.MonkeyPatch,
    *,
    keep_unreviewed_snapshot: bool = True,
    during_replay: Callable[[ReviewWorkspaceFacade, str], None] | None = None,
):
    """옛 앱처럼 입양한다 — 되풀이만 하고 요약 표지를 쓰지 않는다.

    `keep_unreviewed_snapshot=False`는 미검토 표지를 되돌리는 끝 스냅숏도 쓰지 않던 빌드다.
    `during_replay`는 자물쇠가 없던 시절 되풀이 도중에 끼어든 사람의 편집이다.
    """
    real_snapshot = ReviewWorkspaceFacade._publish_snapshot_locked
    real_set_selection = facade.set_selection
    fired: list[str] = []

    def snapshot(self, revision, segments, label):
        skipped = label.startswith(review_module.ADOPTION_REPLAY_LABEL) or (
            label == "자동 제외 미검토 유지" and not keep_unreviewed_snapshot
        )
        return revision if skipped else real_snapshot(self, revision, segments, label)

    def set_selection(base, segment_id, selection):  # 되풀이의 첫 제외 선택 바로 뒤
        revision = real_set_selection(base, segment_id, selection)
        if during_replay is not None and not fired:
            fired.append(base)
            during_replay(facade, base)
        return revision

    plan, mapping = facade.plan_from_review(head.base_plan_id)
    with monkeypatch.context() as patch:
        patch.setattr(ReviewWorkspaceFacade, "_publish_snapshot_locked", snapshot)
        patch.setattr(facade, "set_selection", set_selection)
        return facade.adopt_plan_review(plan, mapping, old_revision=head)


def _memo_survives_a_publish(service, root, source_id: str) -> bool:
    revision = service.analysis_overview(root).revision.revision_id
    service.publish_selection_review(root, revision, _Context())
    head = ReviewWorkspaceFacade(root).load_head().revision
    return any(s.notes == MEMO for s in head.segments if s.source_id == source_id)


@needs_ffmpeg
def test_an_invisible_edit_right_after_an_old_adoption_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """미검토 표지 스냅숏을 쓰지 않던 빌드라면 개수로 센 창이 하나 넘친다 — 입양 직후의 사람 메모가
    그 창에 먹혀 사라지던 길이다. 창을 내용으로 확인하면 넘친 자리에서 어긋나 메모가 보인다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    one = co._ids(service, root)["DJI_0001.mov"]
    head = facade.load_head().revision
    assert unreviewed_auto_exclusions(head)  # 옛 머리에 미검토 제외가 있다(표지 스냅숏이 셈에 든다)
    adopted = old_build_adopt(facade, head, monkeypatch, keep_unreviewed_snapshot=False)
    target = next(s for s in adopted.segments if s.source_id == one)
    memo = facade.set_notes(adopted.base_plan_id, target.segment_id, MEMO)
    assert one in facade.human_source_ids(memo)
    assert _memo_survives_a_publish(service, root, one)


@needs_ffmpeg
def test_a_human_memo_interleaved_into_an_old_replay_is_detected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """자물쇠가 없던 옛 앱 — 되풀이 도중에 사람이 메모를 달았다. 창 안의 편집을 하나하나 확인하므로
    끼어든 메모가 되풀이로 건너뛰어지지 않는다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    one = co._ids(service, root)["DJI_0001.mov"]

    def person_edits(self: ReviewWorkspaceFacade, base: str) -> None:
        current = self.load_head(base).revision
        target = next(s for s in current.segments if s.source_id == one)
        self.set_notes(base, target.segment_id, MEMO)

    adopted = old_build_adopt(
        facade, facade.load_head().revision, monkeypatch, during_replay=person_edits
    )
    assert one in facade.human_source_ids(adopted)
    assert not facade.is_untouched(adopted)
    assert _memo_survives_a_publish(service, root, one)


@needs_ffmpeg
def test_restore_after_an_old_adoption_brings_back_auto_exclusions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """표지 없는 옛 입양 뒤 「자동 제외 전체 복원」 — 자동 제외는 돌아오고 사람 제외는 남는다."""
    service, root = co.reverse_imported_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    adopted = old_build_adopt(facade, facade.load_head().revision, monkeypatch)
    assert len(unreviewed_auto_exclusions(adopted)) == 1
    two = co._ids(service, root)["DJI_0002.mov"]
    human_excluded = [
        s
        for s in chronological(adopted)
        if s.source_id == two and s.selection == "excluded" and not s.notes.startswith("자동 제외")
    ]
    assert len(human_excluded) == 1
    restored = facade.restore_automatic()
    auto = [s for s in restored.segments if s.notes.startswith("자동 제외: ")]
    assert auto and all(s.selection == "included" for s in auto)
    assert all(AUTO_EXCLUDED_UNREVIEWED_TAG not in s.visual_tags for s in auto)
    kept = next(
        s
        for s in restored.segments
        if (s.source_id, s.start_pts, s.end_pts)
        == (two, human_excluded[0].start_pts, human_excluded[0].end_pts)
    )
    assert kept.selection == "excluded"
