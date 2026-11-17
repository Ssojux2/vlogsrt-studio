"""촬영 시각 순 맞춤의 경합 — 사람 결정은 조용히 사라지지 않는다(W-13, D53).

검증자가 찾은 길을 실제 시험으로 옮겼다(2026-10-09). 순서 맞춤·등급판 옮기기는 원본을 다시
해시하느라 오래 걸리고(실데이터 수 분), 입양은 사람 결정을 편집으로 다시 적는다(실데이터 137번 ·
약 14초). 그 사이 사람의 편집·다른 작업의 새 기본 출판·두 번째 정리 작업이 끼어든다. 어느 쪽이든
결과는 둘 중 하나여야 한다 — 결정이 새 머리에 실리거나, 시끄럽게 거절·미뤄진다.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from tests.raw import test_creation_order as co
from tests.raw.test_review_workspace import _mid_split_pts, _wait_terminal
from vlogsrt import log
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    SELECTION_EXCLUDED,
    StaleRevisionError,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.service import UPKEEP_JOB_KIND, RawWorkspaceService

needs_ffmpeg = co.needs_ffmpeg
SEMANTIC = "semantic_analysis"


def _pure_reorder(tmp_path: Path, *, human: bool = True):
    service, root = co.reverse_imported_project(tmp_path, human=human)
    before = ReviewWorkspaceFacade(root).load_head().revision
    decisions = co._decisions(service, root, before)
    assert service.refresh_capture_times(root) == ()
    assert service.ensure_source_order(root) is True
    return service, root, before, decisions


def _evidence_level(decisions: dict) -> dict:
    """다시 세운 기계 제안의 근거에는 새 조각 ID가 실린다 — 그 칸만 빼고 견준다."""
    normalized = {}
    for key, (selection, notes, override, unreviewed) in decisions.items():
        if notes.startswith("자동 제외: "):
            provenance = json.loads(notes.removeprefix("자동 제외: "))
            provenance.pop("segment_id", None)
            notes = json.dumps(provenance, sort_keys=True)
        normalized[key] = (selection, notes, override, unreviewed)
    return normalized


def _plan_basis(service: RawWorkspaceService, root: Path, head) -> str:
    return service.segment_plan_revision(root, head.base_plan_id).basis


@needs_ffmpeg
def test_edit_during_adoption_waits_then_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """입양이 사람 결정을 다시 적는 동안(실데이터 약 14초) 화면이 옛 머리에 편집을 보낸다.

    예전엔 그 편집이 옛 기본 계획에 내려앉아 새 머리에서 조용히 사라졌다. 이제 편집은 입양이
    끝날 때까지 기다렸다가 '새 계획으로 옮겨졌다'로 거절된다 — 화면이 새로 그려 다시 하게 한다.
    """
    service, root, before, decisions = _pure_reorder(tmp_path)
    real_replay = ReviewWorkspaceFacade._replay_metadata
    outcome: list[object] = []
    editor: list[threading.Thread] = []

    def person_edits_old_base() -> None:
        target = next(s for s in before.segments if s.selection == "included")
        try:
            ReviewWorkspaceFacade(root).set_selection(
                before.base_plan_id, target.segment_id, SELECTION_EXCLUDED
            )
            outcome.append("saved")
        except StaleRevisionError as error:
            outcome.append(error)

    def replay_while_a_person_edits(self, new_base_plan_id, old_revision, mapping):
        if not editor:
            editor.append(threading.Thread(target=person_edits_old_base))
            editor[0].start()
            editor[0].join(timeout=0.5)
            assert editor[0].is_alive(), "편집이 입양을 기다리지 않고 옛 머리에 저장됐다"
        return real_replay(self, new_base_plan_id, old_revision, mapping)

    monkeypatch.setattr(ReviewWorkspaceFacade, "_replay_metadata", replay_while_a_person_edits)
    assert service.align_review_order(root) is True
    editor[0].join(timeout=30)
    assert len(outcome) == 1 and isinstance(outcome[0], StaleRevisionError)
    assert "새로고침" in str(outcome[0])
    facade = ReviewWorkspaceFacade(root)
    assert facade.load_head(before.base_plan_id).revision == before  # 옛 머리에 몰래 쌓이지 않았다
    with pytest.raises(StaleRevisionError, match="새로고침"):  # 옛 화면에서 늦게 온 편집도
        facade.set_notes(before.base_plan_id, before.segments[0].segment_id, "옛 화면의 메모")
    visible = facade.load_head().revision
    assert co._head_names(service, root, visible) == co.SHOT_ORDER
    assert co._decisions(service, root, visible) == decisions


@needs_ffmpeg
def test_align_never_adopts_a_superseded_head(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """다시 해시하는 동안 다른 작업(분석 게시·등급판 옮기기)이 새 기본을 냈으면 옛 머리를 입양하지
    않는다 — 새 기본과 그 결정이 사라지고 옛 제안이 되살아나던 길이다. 경고하고 미룬다."""
    service, root, before, decisions = _pure_reorder(tmp_path)
    real_plan = ReviewWorkspaceFacade.plan_from_review
    published: list[str] = []
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)

    def plan_while_a_newer_base_is_published(self, base_plan_id=None, **kwargs):
        planned = real_plan(self, base_plan_id, **kwargs)
        if not published:
            revision = service.analysis_overview(root).revision.revision_id
            plan = service.plan_segmentation_from_analysis(root, revision, use_current_order=True)
            newer = ReviewWorkspaceFacade(root).initialize_review(plan.revision_id, revision)
            published.append(newer.base_plan_id)
        return planned

    monkeypatch.setattr(
        ReviewWorkspaceFacade, "plan_from_review", plan_while_a_newer_base_is_published
    )
    assert service.align_review_order(root) is False
    assert any("다음 갱신으로 미룬다" in w for w in warnings)
    visible = ReviewWorkspaceFacade(root).load_head().revision
    assert visible.base_plan_id == published[0]
    assert co._head_names(service, root, visible) == co.SHOT_ORDER
    assert co._decisions(service, root, visible) == decisions  # 사람 결정은 새 기본에 실려 있다
    assert service.align_review_order(root) is False  # 새 기본은 이미 지금 순서 위다


@needs_ffmpeg
def test_preview_keeps_the_head_when_align_defers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """다시 해시하는 동안 사람이 고쳐 순서 맞춤이 미뤄지면, 미리보기는 머리를 다시 깔지 않는다.

    같은 영상 묶음이면 머리를 원본마다 한 장으로 다시 까는 길은 없다 — 순서는 다음 갱신이 맞춘다.
    """
    service, root, before, decisions = _pure_reorder(tmp_path)
    monkeypatch.setattr(log, "warn", lambda message: None)
    real_plan = ReviewWorkspaceFacade.plan_from_review
    memo: list[str] = []

    def plan_while_a_person_edits(self, base_plan_id=None, **kwargs):
        planned = real_plan(self, base_plan_id, **kwargs)
        if not memo:
            head = self.load_head(base_plan_id).revision
            self.set_notes(head.base_plan_id, head.segments[0].segment_id, "옮기는 동안 쓴 메모")
            memo.append(head.segments[0].segment_id)
        return planned

    monkeypatch.setattr(ReviewWorkspaceFacade, "plan_from_review", plan_while_a_person_edits)
    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert len(head.segments) == len(before.segments)
    assert _plan_basis(service, root, head) != "deterministic_tiling"
    after = co._decisions(service, root, head)
    assert "옮기는 동안 쓴 메모" in {notes for _sel, notes, _o, _u in after.values()}
    changed = {key for key in after if after[key] != decisions.get(key)}
    assert len(changed) == 1  # 메모 한 칸 말고는 그대로다


@needs_ffmpeg
def test_cleanup_reads_the_head_after_another_aligner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """다른 작업(정리 작업·미리보기)이 먼저 순서를 맞췄으면, 자동 정리는 옛 머리를 들고 다시
    세우지 않는다 — 맞춘 뒤의 머리를 다시 읽어 이어 쓴다."""
    service, root, before, decisions = _pure_reorder(tmp_path)
    real_align = service.align_review_order

    def another_actor_already_aligned(root_: Path) -> bool:
        assert real_align(root_) is True  # 다른 작업이 옮겼다
        return False  # 이 호출은 할 일이 없었다

    monkeypatch.setattr(service, "align_review_order", another_actor_already_aligned)
    cleanup = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert cleanup.status == "succeeded", cleanup.error
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert co._head_names(service, root, head) == co.SHOT_ORDER
    assert co._decisions(service, root, head) == decisions


def _legacy_head(service: RawWorkspaceService, root: Path, monkeypatch: pytest.MonkeyPatch):
    """D51 이전 모양의 손대지 않은 머리 — 등급판 표지가 없다."""
    facade = ReviewWorkspaceFacade(root)
    with monkeypatch.context() as patch:
        patch.setattr(tiering, "red_decisions", lambda report: [])
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        legacy = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    assert facade.is_untouched(legacy) and not facade.has_tier_marker(legacy)
    return legacy


@needs_ffmpeg
def test_second_upkeep_is_not_submitted_while_one_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """정리 작업이 도는 동안 화면 틱(2초)이 다시 불러도 두 번째 작업은 없다 — 등급판을 두 번
    다시 세우거나(실데이터: 같은 계획 둘) 손댄 머리에 등급을 겹쳐 달다 거짓 실패를 내던 길이다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    _legacy_head(service, root, monkeypatch)
    reopened = RawWorkspaceService()
    real = reopened._migrate_unedited_head
    gate = threading.Event()
    calls: list[int] = []

    def slow_rehash(*args):
        calls.append(1)
        gate.wait(30)
        return real(*args)

    monkeypatch.setattr(reopened, "_migrate_unedited_head", slow_rehash)
    first = reopened.start_upkeep(root)
    assert first is not None and "다시 세웁니다" in first
    deadline = time.monotonic() + 30
    while not calls and time.monotonic() < deadline:
        time.sleep(0.02)
    assert calls, "정리 작업이 등급판 단계에 닿지 못했다"
    for _ in range(3):  # 작업이 스스로 바꾼 순서 때문에 할 일 열쇠가 달라져도
        assert reopened.start_upkeep(root) is not None
    jobs = [j for j in reopened.list_jobs(root) if j.kind == UPKEEP_JOB_KIND]
    assert len(jobs) == 1
    gate.set()
    assert _wait_terminal(reopened, root, jobs[0].job_id).status == "succeeded"
    assert len(calls) == 1
    assert co._order_names(reopened, root) == co.SHOT_ORDER
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert co._head_names(reopened, root, head) == co.SHOT_ORDER
    assert reopened.start_upkeep(root) is None


@needs_ffmpeg
def test_untouched_head_reorder_rebuilds_from_evidence(tmp_path: Path):
    """잃을 사람 결정이 없는 머리는 입양(편집 되풀이)하지 않고 저장 증거로 새 순서 위에 다시
    세운다 — 미리보기도 자동 정리도 원본마다 한 장으로 다시 깔지 않고, 미검토 제안은 그대로다."""
    service, root, before, decisions = _pure_reorder(tmp_path, human=False)
    assert len(unreviewed_auto_exclusions(before)) == 1
    preview = service.start_source_preview(root)
    assert _wait_terminal(service, root, preview.job_id).status == "succeeded"
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    assert _plan_basis(service, root, head) == SEMANTIC  # 결정적 타일(다시 깔기)이 아니다
    assert head.sequence == 0 and facade.is_untouched(head)
    assert co._head_names(service, root, head) == co.SHOT_ORDER
    assert _evidence_level(co._decisions(service, root, head)) == _evidence_level(decisions)
    plan = service.segment_plan_revision(root, head.base_plan_id)
    assert plan.order_revision_id == service.latest_global_source_order(root).revision_id

    cleanup = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert cleanup.status == "succeeded", cleanup.error
    after = facade.load_head().revision
    assert _plan_basis(service, root, after) != "deterministic_tiling"
    assert len(unreviewed_auto_exclusions(after)) == 1
    assert co._head_names(service, root, after) == co.SHOT_ORDER
    assert _evidence_level(co._decisions(service, root, after)) == _evidence_level(decisions)


@needs_ffmpeg
def test_machine_only_replayed_head_still_counts_as_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """기계가 다시 적은 편집(입양의 되풀이)은 사람 결정이 아니다 — sequence가 아니라 내용으로 본다.

    실데이터 머리가 이 모양이다(입양 뒤 sequence 136, 사람 결정 0). sequence로 보면 영원히 '손댄
    머리'라, 다음 분석 게시가 그 머리를 통째로 붙잡아 옛 제안이 새 기본에 실려 다녔다.
    """
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    plan, mapping = facade.plan_from_review(head.base_plan_id)
    adopted = facade.adopt_plan_review(plan, mapping, old_revision=head)
    assert adopted.sequence > 0  # 미검토 제외·근거 메모를 편집으로 다시 적었다
    assert facade.is_untouched(adopted)
    assert len(unreviewed_auto_exclusions(adopted)) == 1
    revision = service.analysis_overview(root).revision.revision_id
    with monkeypatch.context() as patch:  # 새 증거에는 빨강이 없다(예: 자막을 고쳐 다시 분석)
        patch.setattr(tiering, "red_decisions", lambda report: [])
        plan = service.plan_segmentation_from_analysis(root, revision, use_current_order=True)
        republished = facade.initialize_review(plan.revision_id, revision)
    # 옛 머리를 통째로 붙잡지 않는다 — 기계 제안은 새 증거를 따른다.
    assert unreviewed_auto_exclusions(republished) == ()


@needs_ffmpeg
def test_human_split_survives_adoption_as_a_human_edit(tmp_path: Path):
    """사람이 나눈 경계는 입양 뒤 계획(`planned`)에 실려도 사람 결정이다 — 다시 세우지 않는다."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    segment = max(head.segments, key=lambda s: s.end_pts - s.start_pts)
    split_pts = _mid_split_pts(head.clock_for(segment.source_id), segment)
    split = facade.split_segment(head.base_plan_id, segment.segment_id, split_pts)
    assert not facade.is_untouched(split)
    plan, mapping = facade.plan_from_review(split.base_plan_id)
    adopted = facade.adopt_plan_review(plan, mapping, old_revision=split)
    assert not facade.is_untouched(adopted)


@needs_ffmpeg
@pytest.mark.parametrize("evidence_gap", [False, True])
def test_marked_head_with_stale_v1_proposals_is_rebuilt_on_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, evidence_gap: bool
):
    """실데이터 지금 모양 — 등급판 v2 표지는 있는데 옛 판(v1) 미검토 제외가 실려 다니는 머리.

    D53 첫 실행의 경합(옛 머리 입양 + 그 머리를 붙잡은 게시)이 남긴 모양이다. 앱을 다시 열면
    정리 작업이 증거로 다시 세워 옛 제안을 거두고 지금 판 제안만 남긴다(유료 호출 0회).
    `evidence_gap`: 지금 판정 증거가 없는 원본(실데이터 일부)은 옛 머리의 조각을 그대로 들고
    오는데, 그 위의 옛 제안도 거둔다 — 남기면 머리가 늘 '옮길 머리'라 다시 세우기(원본 재해시
    74초)가 앱을 열 때마다 끝없이 되풀이됐다(스냅숏 실측).
    """
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    stale = {
        "start": {"num": 12, "den": 1},
        "end": {"num": 14, "den": 1},
        "reason": tiering.RED_SILENCE,
        "version": "raw-tier/v1",
        "evidence": ["pcm:-48.0dBFS"],
    }
    with monkeypatch.context() as patch:
        real_red = tiering.red_decisions
        patch.setattr(tiering, "red_decisions", lambda report: [*real_red(report), stale])
        plan = service.plan_segmentation_from_analysis(root)
        planted = facade.initialize_review(plan.revision_id)
    assert facade.has_tier_marker(planted)
    assert facade.has_stale_tier_proposals(planted)
    plan, mapping = facade.plan_from_review(planted.base_plan_id)
    replayed = facade.adopt_plan_review(plan, mapping, old_revision=planted)
    assert replayed.sequence > 0 and facade.is_untouched(replayed)
    if evidence_gap:
        gap = co._ids(service, root)["DJI_0001.mov"]
        real_rows = ReviewWorkspaceFacade.tier_evidence_rows
        monkeypatch.setattr(
            ReviewWorkspaceFacade,
            "tier_evidence_rows",
            lambda self: {k: v for k, v in real_rows(self).items() if k != gap},
        )

    reopened = RawWorkspaceService()
    message = reopened.start_upkeep(root) or ""
    assert "다시 세웁니다" in message
    job = next(j for j in reopened.list_jobs(root) if j.kind == UPKEEP_JOB_KIND)
    assert _wait_terminal(reopened, root, job.job_id).status == "succeeded"
    head = facade.load_head().revision
    assert not facade.has_stale_tier_proposals(head)
    assert head.sequence == 0 and facade.is_untouched(head)
    assert len(unreviewed_auto_exclusions(head)) == 1  # 지금 판의 빨강 하나만
    assert reopened.start_upkeep(root) is None
    assert [j.kind for j in reopened.list_jobs(root)].count(UPKEEP_JOB_KIND) == 1


@needs_ffmpeg
def test_analysis_plan_uses_the_current_order(tmp_path: Path):
    """분석 결과로 계획을 세울 때 '지금 순서'를 쓰라고 하면 계획도 지금 순서 위에 선다."""
    service, root, _before, _decisions = _pure_reorder(tmp_path)
    revision = service.analysis_overview(root).revision.revision_id
    plan = service.plan_segmentation_from_analysis(root, revision, use_current_order=True)
    latest = service.latest_global_source_order(root)
    assert plan.order_revision_id == latest.revision_id
    names = co._names(service, root)
    ordered = sorted(plan.entries, key=lambda e: (e.source_order, e.segment_order))
    assert list(dict.fromkeys(names[e.asset_id] for e in ordered)) == co.SHOT_ORDER
    old = service.plan_segmentation_from_analysis(root, revision)
    assert old.order_revision_id == service.analysis_overview(root).revision.order_revision_id


@needs_ffmpeg
def test_prefixed_human_memo_keeps_the_head_touched_through_adoption(tmp_path: Path):
    """미리 채워진 메모 칸에 덧붙인 사람 메모는 기계 메모 머리를 그대로 단다 — 내용만으로는 사람
    손인지 모른다. 편집 기록이 말한다: 그 머리는 입양을 거쳐도 손댄 머리다(다시 세우면 메모가
    사라진다)."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    assert facade.is_untouched(head)
    target = head.segments[0]
    memo = facade.set_notes(head.base_plan_id, target.segment_id, "확인 필요: 사람이 덧붙인 말")
    assert not facade.has_human_decisions(memo)  # 내용으로는 기계 메모처럼 보인다
    assert not facade.is_untouched(memo)
    plan, mapping = facade.plan_from_review(memo.base_plan_id)
    adopted = facade.adopt_plan_review(plan, mapping, old_revision=memo)
    assert adopted.edit is not None and adopted.edit.notes.startswith("입양 결정 다시 적용 ← ")
    assert not facade.is_untouched(adopted)
    retiered = facade.retier(adopted.base_plan_id)  # 기계 스냅숏은 손을 더하지도 덜지도 않는다
    assert not facade.is_untouched(retiered)
