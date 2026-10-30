"""머리 전체 스냅숏(확인·복원·되돌리기)은 실제로 바뀐 영상만 사람 손으로 남긴다(D53).

검증 4차가 찾은 길을 실제 시험으로 옮겼다(2026-10-09). 「자동 제외 확인」 한 번이 모든 영상을 영원히
'손댄 영상'으로 만들어, 다음 분석 게시가 손대지 않은 영상의 새 조각·빨강 제안을 내보이지 못했다
(라이브러리 동시 분석에서 첫 영상을 확인하면 뒤 영상 결과가 모두 가려졌다). 되돌리기로 아무 결정도
남지 않은 머리도 영원히 손댄 머리였다. 이제 확인·복원은 바뀐 영상만, 되돌리기는 되돌린 자리로
건너뛰어 사이의 편집을 셈하지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.raw import test_creation_order as co
from tests.raw.test_creation_order_legacy import old_build_adopt
from tests.raw.test_review_workspace import _mid_split_pts, _wait_terminal, _write_replay_fixtures
from vlogsrt.project.raw import review_workspace as review_module
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    chronological,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.service import RawWorkspaceService

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = co.needs_ffmpeg


class _Context:
    def check_cancel(self) -> None:
        return None


def _publish(service: RawWorkspaceService, root: Path, revision_id: str | None = None):
    revision_id = revision_id or service.analysis_overview(root).revision.revision_id
    service.publish_selection_review(root, revision_id, _Context())
    return ReviewWorkspaceFacade(root).load_head().revision


def _per_source(head, source_id: str) -> list:
    return [s for s in chronological(head) if s.source_id == source_id]


@needs_ffmpeg
def test_confirm_marks_only_the_confirmed_videos(tmp_path: Path):
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    two = co._ids(service, root)["DJI_0002.mov"]
    assert [s.source_id for s in unreviewed_auto_exclusions(facade.load_head().revision)] == [two]
    confirmed = facade.confirm_automatic_exclusions()
    assert facade.human_source_ids(confirmed) == frozenset({two})


@needs_ffmpeg
def test_restore_marks_only_the_changed_videos(tmp_path: Path):
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    two = co._ids(service, root)["DJI_0002.mov"]
    restored = facade.restore_automatic()
    assert facade.human_source_ids(restored) == frozenset({two})


@needs_ffmpeg
def test_undo_back_to_the_exact_prior_state_marks_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """덮기를 했다가 되돌렸다 — 남은 결정이 없으니 손댄 영상도 없고, 다음 게시는 새 증거로 선다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    one = _per_source(head, co._ids(service, root)["DJI_0001.mov"])[0]
    facade.set_override(head.base_plan_id, one.segment_id, "review_required")
    undone = facade.undo()
    assert not facade.has_human_decisions(undone)
    assert facade.human_source_ids(undone) == frozenset()
    revision = service.analysis_overview(root).revision.revision_id
    for _ in range(3):
        with monkeypatch.context() as patch:  # 자막을 고쳐 다시 분석했더니 빨강이 없다
            patch.setattr(tiering, "red_decisions", lambda report: [])
            head = _publish(service, root, revision)
        assert unreviewed_auto_exclusions(head) == ()  # 옛 빨강이 붙잡혀 남지 않는다
        assert facade.human_source_ids(head) == frozenset()


@needs_ffmpeg
def test_undo_of_a_real_edit_after_a_kept_edit_keeps_only_the_kept_one(tmp_path: Path):
    """메모를 남기고, 덮기를 했다 되돌렸다 — 메모를 단 영상만 손댄 영상이다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    ids = co._ids(service, root)
    three = _per_source(head, ids["DJI_0003.mov"])[0]
    one = _per_source(head, ids["DJI_0001.mov"])[0]
    head = facade.set_notes(head.base_plan_id, three.segment_id, "확인 필요: 사람 말")
    facade.set_override(head.base_plan_id, one.segment_id, "review_required")
    undone = facade.undo()
    assert facade.human_source_ids(undone) == frozenset({ids["DJI_0003.mov"]})


@needs_ffmpeg
def test_confirming_one_video_does_not_hide_the_next_videos_analysis(tmp_path: Path):
    """영상별 분석(라이브러리 동시 분석): 먼저 끝난 영상의 빨강을 확인한 뒤 다음 영상의 분석이
    게시되면, 그 영상은 자기 분석의 조각과 빨강 제안으로 선다(예전엔 한 장·제안 0개로 남았다)."""
    card = tmp_path / "card"
    card.mkdir()
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "라이브러리")
    for name in ("DJI_0002.mov", "DJI_0003.mov"):
        co._encode(card / name, co.SECONDS[name], co.SHOT[name])
        handle = service.import_selected_video(root, card / name)
        assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    shas = {a.source_name: a.content_sha256 for a in service.list_source_assets(root)}
    gap = co.SPEECH["DJI_0002.mov"]  # 3→11초가 자막 없는 긴 공백
    _write_replay_fixtures(root, {shas[name]: gap for name in shas})
    luna_path = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna_path.read_text())
    fixtures["windows"]["window_000000"].update(
        suitability="usable", suitability_reason="연결 장면", evidence_ids=[co.FIRST_FRAME]
    )
    luna_path.write_text(json.dumps(fixtures, ensure_ascii=False))
    ids = co._ids(service, root)
    facade = ReviewWorkspaceFacade(root)

    def analyse_and_publish(name: str):
        handle = service.start_analysis(root, mode="replay", asset_ids=(ids[name],))
        assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
        return _publish(service, root)

    head = analyse_and_publish("DJI_0002.mov")
    assert [s.source_id for s in unreviewed_auto_exclusions(head)] == [ids["DJI_0002.mov"]]
    confirmed = facade.confirm_automatic_exclusions()  # 사람이 0002의 빨강을 확인한다
    assert facade.human_source_ids(confirmed) == frozenset({ids["DJI_0002.mov"]})
    head = analyse_and_publish("DJI_0003.mov")
    three = ids["DJI_0003.mov"]
    assert len(_per_source(head, three)) == 3
    assert [s.source_id for s in unreviewed_auto_exclusions(head)] == [three]
    kept = [s for s in _per_source(head, ids["DJI_0002.mov"]) if s.selection == "excluded"]
    assert len(kept) == 1 and AUTO_EXCLUDED_UNREVIEWED_TAG not in kept[0].visual_tags


@needs_ffmpeg
def test_restore_after_a_reorder_adoption_still_restores(tmp_path: Path):
    """순서 맞춤 입양 뒤에도 「자동 제외 전체 복원」이 듣는다 — 되풀이 선택은 사람 선택이 아니다."""
    service, root = co.reverse_imported_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True
    assert service.align_review_order(root) is True
    adopted = facade.load_head().revision
    assert len(unreviewed_auto_exclusions(adopted)) == 1
    restored = facade.restore_automatic()
    auto = [s for s in restored.segments if s.notes.startswith("자동 제외: ")]
    assert auto and all(s.selection == "included" for s in auto)
    assert unreviewed_auto_exclusions(restored) == ()
    # 사람이 직접 뺀 조각(근거 메모 없음)은 복원이 건드리지 않는다.
    human = co._decisions(service, root, adopted)
    after = co._decisions(service, root, restored)
    for key, (selection, notes, _override, _unreviewed) in human.items():
        if selection == "excluded" and not notes.startswith("자동 제외: "):
            assert after[key][0] == "excluded"


@needs_ffmpeg
def test_a_marker_without_a_video_list_counts_every_video(tmp_path: Path):
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    marked = facade._publish_snapshot(head, head.segments, "입양 결정 다시 적용 ← 옛 머리")
    assert facade.human_source_ids(marked) == frozenset(c.source_id for c in marked.clocks)


@needs_ffmpeg
def test_a_pre_d53_carried_split_survives_the_next_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """D53 이전 코드가 표지 없이 이어받은 사람 분할(분석 기반 새 기본 위) — 표지가 없으면 경계를
    모든 기계 계획과 견주어 사람 경계로 찾고, 다음 게시가 합쳐 버리지 않는다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    one = co._ids(service, root)["DJI_0001.mov"]
    segment = _per_source(head, one)[0]
    split = facade.split_segment(
        head.base_plan_id, segment.segment_id, _mid_split_pts(head.clock_for(one), segment)
    )
    pieces = len(_per_source(split, one))
    with monkeypatch.context() as patch:  # 그때 코드는 알아볼 표지를 쓰지 않았다
        patch.setattr(
            review_module, "_marker_label", lambda kind, prior, sources: "등급 재계산 (옛 빌드)"
        )
        old = _publish(service, root)
    assert len(_per_source(old, one)) == pieces
    assert facade.human_source_ids(old) == frozenset({one})
    again = _publish(service, root)
    assert len(_per_source(again, one)) == pieces


@needs_ffmpeg
def test_undo_stops_at_the_summary_marker(tmp_path: Path):
    """요약 표지(이어받음·입양)는 되돌릴 편집이 아니다 — 넘어가면 이어받은 결정과 요약이 끊긴다."""
    service, root = co.reverse_imported_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    carried = _publish(service, root)
    assert carried.edit is not None and carried.edit.notes.startswith("사람 결정 이어받음 ← ")
    with pytest.raises(review_module.ReviewWorkspaceError, match="실행 취소할 편집이 없습니다"):
        facade.undo()
    target = carried.segments[0]
    facade.set_notes(carried.base_plan_id, target.segment_id, "되돌릴 메모")
    undone = facade.undo()
    assert [s.notes for s in undone.segments] == [s.notes for s in carried.segments]
    assert facade.human_source_ids(undone) == facade.human_source_ids(carried)
    with pytest.raises(review_module.ReviewWorkspaceError, match="실행 취소할 편집이 없습니다"):
        facade.undo()


@needs_ffmpeg
def test_a_marker_less_legacy_adoption_counts_only_real_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """실데이터 지금 모양(2026-10-09 08:43) — D53 이전 앱이 표지 없이 입양한 머리.

    되풀이 편집(선택·메모를 다시 적은 것)은 사람 손이 아니다: 입양 기록으로 되풀이 길이를 다시 세어
    옛 머리로 건너뛴다. 사람이 확인한 제외가 있는 영상만 손댄 영상이다(예전 걸음은 되풀이를 사람
    편집으로 셈해 거의 모든 영상을 붙잡았다 — 실데이터 18개 중 16개, 실제 결정은 8개).
    옛 앱 그대로 되풀이 뒤에 아무 리비전도 쓰지 않는다 — 머리가 곧 되풀이의 마지막 편집이라, 창을
    개수로 셌든 내용으로 확인했든 꼬리의 기계 스냅숏이 차이를 가려 주지 않는다.
    """
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    two = co._ids(service, root)["DJI_0002.mov"]
    confirmed = facade.confirm_automatic_exclusions()  # 사람이 0002의 빨강을 확인했다
    adopted = old_build_adopt(facade, confirmed, monkeypatch)
    assert adopted.sequence > 1
    assert adopted.edit is not None and adopted.edit.notes.startswith("자동 제외: ")  # 끝 되풀이
    assert facade.human_source_ids(adopted) == frozenset({two})
    (tmp_path / "second").mkdir()
    untouched = co.reverse_imported_project(tmp_path / "second", human=False)
    other = ReviewWorkspaceFacade(untouched[1])
    head = other.load_head().revision
    replayed = old_build_adopt(other, head, monkeypatch)
    assert replayed.sequence > 1  # 미검토 제외·근거 메모를 편집으로 다시 적었다
    assert replayed.edit is not None and replayed.edit.notes == "자동 제외 미검토 유지"
    assert other.human_source_ids(replayed) == frozenset()
    assert other.is_untouched(replayed)
