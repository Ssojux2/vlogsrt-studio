"""이어받기의 범위와 비용 — 사람 손은 이어받고, 손대지 않은 영상의 분석은 지키고, 길이와 무관(D53).

검증 3차가 찾은 길을 실제 시험으로 옮겼다(2026-10-09).
- 이어받은 표지를 따라 옛 머리의 손을 다시 계산하던 걸음은 게시마다 한 층씩 깊어져(라이브러리
  에서 영상 하나 고를 때마다) 490번째 게시부터 `RecursionError`로 작업공간이 영원히 멈췄다. 표지는
  그 앞을 요약한다 — 걸음은 가장 가까운 표지에서 멈춘다.
- 새 영상을 가져오면 미리보기가 머리를 다시 깔며 손대지 않은 영상의 분석 조각·미검토 제안을 지웠다.
- 분석 전에 한 편집(미리보기 카드 제외·메모)은 이어받지 않았다.
"""

from __future__ import annotations

import inspect
import sys
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw import test_creation_order as co
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt.project.raw.review_workspace import CARRIED_DECISIONS_LABEL, ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import chronological, unreviewed_auto_exclusions
from vlogsrt.project.raw.service import RawWorkspaceService

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = co.needs_ffmpeg


class _Context:
    def check_cancel(self) -> None:
        return None


def _republish(service: RawWorkspaceService, root: Path, revision_id: str):
    service.publish_selection_review(root, revision_id, _Context())
    return ReviewWorkspaceFacade(root).load_head().revision


def _per_source(head, source_id: str) -> list:
    return [s for s in chronological(head) if s.source_id == source_id]


def _counting_loads(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    loads: list[str] = []
    real = ReviewWorkspaceFacade._revision_by_id

    def counting(self, revision_id):
        loads.append(str(revision_id))
        return real(self, revision_id)

    monkeypatch.setattr(ReviewWorkspaceFacade, "_revision_by_id", counting)
    return loads


@needs_ffmpeg
def test_long_publish_chains_keep_a_constant_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """사람 결정을 실은 머리를 수십 번 다시 게시해도(라이브러리 영상 고르기) 사람 손 판정의 걸음은
    길어지지 않는다 — 가장 가까운 이어받음 표지가 그 앞을 요약한다. 재귀 한도를 낮춰도 판정한다."""
    service, root = co.reverse_imported_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    human = co._decisions(service, root, facade.load_head().revision)
    revision = service.analysis_overview(root).revision.revision_id
    loads = _counting_loads(monkeypatch)
    walks: dict[int, int] = {}
    for count in range(1, 41):
        head = _republish(service, root, revision)
        if count in (3, 40):
            loads.clear()
            assert len(facade.human_source_ids(head)) == 3
            walks[count] = len(loads)
    assert walks[3] == walks[40]  # 사슬이 길어져도 읽는 리비전 수가 같다
    assert co._decisions(service, root, head) == human
    depth = len(inspect.stack())
    limit = sys.getrecursionlimit()
    sys.setrecursionlimit(depth + 60)  # 옛 재귀 걸음은 표지마다 두 층 — 40층이면 넘친다
    try:
        assert len(facade.human_source_ids(facade.load_head().revision)) == 3
    finally:
        sys.setrecursionlimit(limit)


@needs_ffmpeg
def test_importing_a_video_keeps_untouched_videos_analysis(tmp_path: Path):
    """사람이 DJI_0001만 만졌는데 DJI_0004를 가져왔다 — 미리보기는 새 영상만 한 장으로 깔고, 손대지
    않은 DJI_0002의 분석 조각(3개)과 미검토 빨강 제안(1개)은 그대로다. 출력 관문도 그대로 묻는다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    ids = co._ids(service, root)
    one = _per_source(head, ids["DJI_0001.mov"])[0]
    head = facade.set_override(head.base_plan_id, one.segment_id, "review_required")
    before = {name: len(_per_source(head, ids[name])) for name in ids}
    assert len(unreviewed_auto_exclusions(head)) == 1
    card = tmp_path / "card"
    co._encode(card / "DJI_0004.mov", 15, "2026-08-11T12:00:00+00:00")
    handle = service.import_selected_video(root, card / "DJI_0004.mov")
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    after = facade.load_head().revision
    ids = co._ids(service, root)
    assert {name: len(_per_source(after, ids[name])) for name in before} == before
    assert len(_per_source(after, ids["DJI_0004.mov"])) == 1  # 새 영상만 한 장
    assert len(unreviewed_auto_exclusions(after)) == 1
    assert any(
        s.override_recommendation == "review_required"
        for s in _per_source(after, ids["DJI_0001.mov"])
    )
    assert after.edit is not None and after.edit.notes.startswith(CARRIED_DECISIONS_LABEL)


@needs_ffmpeg
def test_cleanup_reinit_keeps_videos_the_analysis_did_not_cover(tmp_path: Path):
    """새 분석이 일부 영상만 다뤘으면 자동 정리의 다시 세우기는 나머지 영상의 조각·제안을 지킨다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    ids = co._ids(service, root)
    two = ids["DJI_0002.mov"]
    before = len(_per_source(head, two))
    assert before == 3 and len(unreviewed_auto_exclusions(head)) == 1
    handle = service.start_analysis(root, mode="replay", asset_ids=(ids["DJI_0001.mov"],))
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    cleanup = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert cleanup.status == "succeeded", cleanup.error
    after = facade.load_head().revision
    assert len(_per_source(after, two)) == before
    assert [s.source_id for s in unreviewed_auto_exclusions(after)] == [two]


@needs_ffmpeg
def test_edits_before_any_analysis_are_carried(tmp_path: Path):
    """분석 전에 미리보기 카드를 빼고 메모를 달았다 — 새 영상을 가져와도, 새 기본으로 다시 세워도
    그 결정은 이어받는다(이어받음 표지와 함께)."""
    card = tmp_path / "card"
    card.mkdir()
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "분석 전")
    for name in ("DJI_0001.mov", "DJI_0002.mov"):
        co._encode(card / name, co.SECONDS[name], co.SHOT[name])
        handle = service.import_selected_video(root, card / name)
        assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    ids = co._ids(service, root)
    tile = _per_source(head, ids["DJI_0001.mov"])[0]
    head = facade.set_selection(head.base_plan_id, tile.segment_id, "excluded")
    head = facade.set_notes(head.base_plan_id, tile.segment_id, "사람: 흔들려서 뺀다")
    decided = [("excluded", "사람: 흔들려서 뺀다")]
    # 새 기본으로 다시 세운다(보존 목록 없이) — 분석이 없어도 사람 손은 이어받는다.
    plan = service.plan_segmentation(root, tile_seconds=Fraction(10**12))
    rebuilt = facade.initialize_review(plan.revision_id)
    assert rebuilt.edit is not None and rebuilt.edit.notes.startswith(CARRIED_DECISIONS_LABEL)
    assert ids["DJI_0001.mov"] in rebuilt.edit.notes
    assert [(s.selection, s.notes) for s in _per_source(rebuilt, ids["DJI_0001.mov"])] == decided
    co._encode(card / "DJI_0003.mov", co.SECONDS["DJI_0003.mov"], co.SHOT["DJI_0003.mov"])
    handle = service.import_selected_video(root, card / "DJI_0003.mov")
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    after = facade.load_head().revision
    assert [(s.selection, s.notes) for s in _per_source(after, ids["DJI_0001.mov"])] == decided


@needs_ffmpeg
def test_a_broken_history_record_counts_every_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """편집 기록이 끊겼으면(리비전을 못 읽는다) 모든 영상을 사람 손으로 본다(다시 세우지 않는다)."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    three = co._ids(service, root)["DJI_0003.mov"]
    target = _per_source(head, three)[0]
    edited = facade.set_notes(head.base_plan_id, target.segment_id, "확인 필요: 사람 말")
    assert facade.human_source_ids(edited) == frozenset({three})
    monkeypatch.setattr(ReviewWorkspaceFacade, "_revision_by_id", lambda self, revision_id: None)
    assert facade.human_source_ids(edited) == frozenset(c.source_id for c in edited.clocks)
    assert not facade.is_untouched(edited)


@needs_ffmpeg
def test_marker_less_sequence_zero_human_head_is_still_carried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """D53 이전 모양 — 사람 결정을 싣고 표지 없이 sequence 0으로 선 머리. 다음 게시는 sequence가
    아니라 내용으로 보고 그 결정을 이어받는다(예전 문은 `sequence == 0`이라 버렸다)."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    three = co._ids(service, root)["DJI_0003.mov"]
    target = _per_source(head, three)[0]
    facade.set_override(head.base_plan_id, target.segment_id, "review_required")
    revision = service.analysis_overview(root).revision.revision_id
    with monkeypatch.context() as patch:  # 표지를 쓰지 않던 시절의 이어받기
        patch.setattr(
            ReviewWorkspaceFacade,
            "_publish_snapshot_locked",
            lambda self, head, segments, label: head,
        )
        seeded = _republish(service, root, revision)
    assert seeded.sequence == 0 and seeded.edit is None
    assert facade.has_human_decisions(seeded)
    again = _republish(service, root, revision)
    kept = {(s.start_pts, s.end_pts): s.override_recommendation for s in _per_source(again, three)}
    assert kept[(target.start_pts, target.end_pts)] == "review_required"
