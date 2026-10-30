"""사람 손의 출처 — 이어받은 기본·경계·다시 세우기에서 사람 결정이 조용히 사라지지 않는다(D53).

검증 2차가 찾은 길을 실제 시험으로 옮겼다(2026-10-09). 게시(영상 고르기·분석 완료)는 새 기본
리비전(sequence 0)을 세우며 사람이 손댄 원본의 조각을 옛 머리에서 이어받는다. 그 새 기본에는 편집
기록이 없어, 내용만으로 사람 손을 모르는 결정(통째 합친 경계·기계 메모 머리를 단 사람 메모)은
'손대지 않은 머리'로 읽혀 다음 게시·순서 맞춤이 다시 세우며 지웠다. 이제 이어받은 기본은 어느 원본을
이어받았는지 기록을 남기고, 사람 손은 원본마다 내용·경계·기록으로 판정한다.
"""

from __future__ import annotations

import time
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw import test_creation_order as co
from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt import log
from vlogsrt.project.raw.models import PlannedInterval
from vlogsrt.project.raw.review_workspace import CARRIED_DECISIONS_LABEL, ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import chronological, unreviewed_auto_exclusions
from vlogsrt.project.raw.service import UPKEEP_JOB_KIND, RawWorkspaceService

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = co.needs_ffmpeg


class _Context:
    def check_cancel(self) -> None:
        return None


def _republish(service: RawWorkspaceService, root: Path, revision_id: str | None = None):
    """영상 고르기·분석 완료가 하는 일 — (저장된) 분석을 다시 게시한다."""
    revision_id = revision_id or service.analysis_overview(root).revision.revision_id
    service.publish_selection_review(root, revision_id, _Context())
    return ReviewWorkspaceFacade(root).load_head().revision


def _source(head, source_id: str) -> list:
    return [s for s in chronological(head) if s.source_id == source_id]


@needs_ffmpeg
def test_edit_a_view_b_back_to_a_keeps_a_and_shows_b(tmp_path: Path):
    """영상별 분석(라이브러리 흐름): A를 고쳐 놓고 B를 보고 다시 A로 — A의 결정은 그대로다.

    예전엔 B를 볼 때 A의 결정이 sequence 0 새 기본으로 이어지고, 다시 A로 오면 그 새 기본을
    'sequence 0이니 손대지 않음'으로 읽어 A의 제외·메모를 버렸다. 이어받는 것은 사람이 손댄
    원본뿐이라, B는 B 자신의 분석 결과로 선다.
    """
    card = tmp_path / "card"
    card.mkdir()
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "영상별")
    for name in co.OLD_ORDER:
        co._encode(card / name, co.SECONDS[name], co.SHOT[name])
        handle = service.import_selected_video(root, card / name)
        assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    shas = {a.source_name: a.content_sha256 for a in service.list_source_assets(root)}
    _write_replay_fixtures(root, {shas[name]: co.SPEECH[name] for name in co.SPEECH})
    ids = co._ids(service, root)
    revisions: dict[str, str] = {}
    for name in ("DJI_0001.mov", "DJI_0002.mov"):
        handle = service.start_analysis(root, mode="replay", asset_ids=(ids[name],))
        assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
        revisions[name] = service.analysis_overview(root).revision.revision_id
    a, b = ids["DJI_0001.mov"], ids["DJI_0002.mov"]
    facade = ReviewWorkspaceFacade(root)
    head = _republish(service, root, revisions["DJI_0001.mov"])  # A를 고른다
    target = _source(head, a)[0]
    head = facade.set_selection(head.base_plan_id, target.segment_id, "excluded")
    head = facade.set_notes(head.base_plan_id, target.segment_id, "사람 메모 A")
    key = (target.start_pts, target.end_pts)

    viewing_b = _republish(service, root, revisions["DJI_0002.mov"])  # B를 고른다
    assert viewing_b.edit is not None and viewing_b.edit.notes.startswith(CARRIED_DECISIONS_LABEL)
    assert not facade.is_untouched(viewing_b)
    plan = service.segment_plan_revision(root, viewing_b.base_plan_id)
    planned_b = {e.segment_id for e in plan.entries if e.asset_id == b}
    assert {s.segment_id for s in _source(viewing_b, b)} == planned_b  # B는 B의 분석으로 선다

    back_to_a = _republish(service, root, revisions["DJI_0001.mov"])  # 다시 A로
    for view in (viewing_b, back_to_a):
        kept = {(s.start_pts, s.end_pts): (s.selection, s.notes) for s in _source(view, a)}
        assert kept[key] == ("excluded", "사람 메모 A")


@needs_ffmpeg
def test_publishing_twice_keeps_carried_human_decisions(tmp_path: Path):
    """같은 분석을 두 번 게시해도(영상을 두 번 고른다) 이어받은 사람 결정은 남는다."""
    service, root = co.reverse_imported_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    human = co._decisions(service, root, facade.load_head().revision)
    first = _republish(service, root)
    assert not facade.is_untouched(first)
    second = _republish(service, root)
    assert co._decisions(service, root, first) == human
    assert co._decisions(service, root, second) == human


def _merge_whole_source(facade: ReviewWorkspaceFacade, head, source_id: str):
    while len(_source(head, source_id)) > 1:
        first, second = _source(head, source_id)[:2]
        head = facade.merge_segments(head.base_plan_id, first.segment_id, second.segment_id)
    return head


@needs_ffmpeg
def test_whole_source_merge_carried_into_a_new_base_stays_touched(tmp_path: Path):
    """사람이 원본 하나를 통째로 합쳤다 — 합친 조각은 기계 근거 메모·미검토 표지를 그대로 달고, 그
    경계는 미리보기의 '영상마다 한 장' 타일과 같아 내용으로는 사람 손인지 모른다. 게시가 그 머리를
    새 기본으로 이어받으면 이어받은 기록이 남고, 순서 맞춤은 다시 세우지 않고 입양한다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    service.plan_segmentation(root, tile_seconds=Fraction(10**12))  # 미리보기의 영상마다 한 장
    facade = ReviewWorkspaceFacade(root)
    two = co._ids(service, root)["DJI_0002.mov"]
    merged = _merge_whole_source(facade, facade.load_head().revision, two)
    assert len(_source(merged, two)) == 1
    assert not facade.has_human_decisions(merged)  # 내용으로는 모른다
    carried = _republish(service, root)
    assert carried.edit is not None and carried.edit.notes.startswith(CARRIED_DECISIONS_LABEL)
    assert two in carried.edit.notes
    assert not facade.is_untouched(carried)
    assert len(_source(carried, two)) == 1
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True
    assert service.align_review_order(root) is True
    after = facade.load_head().revision
    assert co._head_names(service, root, after) == co.SHOT_ORDER
    assert len(_source(after, two)) == 1  # 다시 세우지 않았다


@needs_ffmpeg
def test_prefixed_memo_carried_by_a_publish_stays_human(tmp_path: Path):
    """기계 메모 머리를 단 사람 메모(미리 채워진 칸에 덧붙인 말)는 게시로 이어받아도 사람 것이다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    target = head.segments[-1]
    memo = "확인 필요: 사람이 덧붙인 말"
    facade.set_notes(head.base_plan_id, target.segment_id, memo)
    carried = _republish(service, root)
    assert not facade.is_untouched(carried)
    again = _republish(service, root)
    notes = {(s.source_id, s.start_pts, s.end_pts): s.notes for s in again.segments}
    assert notes[(target.source_id, target.start_pts, target.end_pts)] == memo


@needs_ffmpeg
def test_geometry_alone_marks_a_head_touched(tmp_path: Path):
    """편집 기록이 없어도 기계가 세운 계획 어디에도 없는 경계면 사람 경계다(사람 구간 계획)."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    two = co._ids(service, root)["DJI_0002.mov"]
    intervals: dict[str, list[PlannedInterval]] = {}
    for clock in head.clocks:
        spans = [
            PlannedInterval(
                start=(s.start_pts - clock.origin_pts) * clock.time_base,
                end=(s.end_pts - clock.origin_pts) * clock.time_base,
            )
            for s in _source(head, clock.source_id)
        ]
        if clock.source_id == two:  # 앞 두 조각을 사람이 정한 구간 하나로
            spans = [PlannedInterval(spans[0].start, spans[1].end), *spans[2:]]
        intervals[clock.source_id] = spans
    plan = service.plan_segmentation(root, intervals=intervals)
    planned = facade.initialize_review(plan.revision_id)
    assert planned.sequence == 0 and planned.edit is None
    assert not facade.has_human_decisions(planned)
    assert not facade.is_untouched(planned)
    assert facade.human_source_ids(planned) == frozenset({two})


@needs_ffmpeg
def test_untouched_replan_never_supersedes_a_newer_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """손대지 않은 머리를 증거로 다시 세우는 동안(원본 재해시) 새 분석이 게시되면, 다시 세운 옛
    증거로 그 위를 덮지 않는다 — 경고하고 미룬다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    assert len(unreviewed_auto_exclusions(facade.load_head().revision)) == 1
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True
    real = RawWorkspaceService.plan_segmentation_from_evidence
    newer: list = []
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)

    def rehash_while_a_new_analysis_lands(self, root_, rows):
        planned = real(self, root_, rows)
        if not newer:
            # 사람이 다시 분석해 게시한다(자막을 고친 뒤 등) — 새 기본이 지금 순서 위에 선다.
            handle = service.start_analysis(root, mode="replay")
            assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
            newer.append(_republish(service, root))
        return planned

    monkeypatch.setattr(
        RawWorkspaceService, "plan_segmentation_from_evidence", rehash_while_a_new_analysis_lands
    )
    assert service.align_review_order(root) is False
    assert any("다음 갱신으로 미룬다" in w for w in warnings)
    visible = facade.load_head().revision
    assert visible == newer[0]  # 새 게시는 다시 세운 옛 증거에 덮이지 않았다
    assert co._head_names(service, root, visible) == co.SHOT_ORDER
    assert service.align_review_order(root) is False  # 새 기본은 이미 지금 순서 위다


@needs_ffmpeg
def test_tier_step_key_stops_a_replan_loop_when_the_marker_never_sets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """등급판 표지가 끝내 붙지 않는 머리(등급 보고서를 못 읽는다)도 다시 세우기를 되풀이하지
    않는다 — 할 일 열쇠가 머리 리비전이 아니라 (판, 표지, 옛 제안 자리)라 같은 것이 남으면
    이번 실행에서는 다시 걸지 않는다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    monkeypatch.setattr(RawWorkspaceService, "tier_report", lambda self, *args: None)
    facade = ReviewWorkspaceFacade(root)
    legacy = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    assert not facade.has_tier_marker(legacy) and facade.is_untouched(legacy)
    reopened = RawWorkspaceService()
    calls: list[int] = []
    real = reopened._migrate_unedited_head

    def counting(*args, **kwargs):
        calls.append(1)
        return real(*args, **kwargs)

    monkeypatch.setattr(reopened, "_migrate_unedited_head", counting)
    assert reopened.start_upkeep(root) is not None
    job = next(j for j in reopened.list_jobs(root) if j.kind == UPKEEP_JOB_KIND)
    assert _wait_terminal(reopened, root, job.job_id).status == "succeeded"
    assert not facade.has_tier_marker(facade.load_head().revision)  # 여전히 표지가 없다
    for _ in range(3):  # 화면 갱신이 거듭 불러도
        assert reopened.start_upkeep(root) is None
        time.sleep(0.05)
    assert [j.kind for j in reopened.list_jobs(root)].count(UPKEEP_JOB_KIND) == 1
    assert len(calls) == 1


@needs_ffmpeg
def test_carried_marker_lists_only_the_sources_a_person_touched(tmp_path: Path):
    """이어받은 기록은 사람이 손댄 원본만 적는다 — 다른 원본은 새 분석 결과로 선다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    three = co._ids(service, root)["DJI_0003.mov"]
    target = _source(head, three)[0]
    facade.set_override(head.base_plan_id, target.segment_id, "review_required")
    carried = _republish(service, root)
    assert carried.edit is not None
    label = carried.edit.notes
    assert label.startswith(CARRIED_DECISIONS_LABEL) and three in label
    others = {sid for sid in co._ids(service, root).values() if sid != three}
    assert not any(sid in label for sid in others)
    assert facade.human_source_ids(carried) == frozenset({three})
