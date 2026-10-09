"""빨강 등급은 미검토 자동 제외로만 들어간다 — 사람이 보기 전까지 출력은 닫힌다(W-11, D51, D35).

합성 미디어(15초 · 창 하나)에 재생 분석을 돌린다. 발화는 0.5–1.5·2–3·11–12·12.5–13.5초,
3→11초가 8초짜리 자막 공백이다 — 발화 쪽 0.3초를 남긴 3.3–10.7초가 자막 없는 긴 공백이다(D52).
Luna가 그 창을 '사용 가능'(`suitability="usable"`)으로 보면 풍경이 아니므로 빨강이 되고, 프레임
격자(1/25초) 안쪽으로 붙인 3.32–10.68초 조각이 자동 제외 후보가 된다. '추천'으로 보면 풍경이라
노랑이다(제외 제안 없음). Luna는 저장 프레임 하나를 근거로 낸다. 유료 호출 0회.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.service import RawWorkspaceError, RawWorkspaceService

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

RED = (Fraction(83, 25), Fraction(267, 25))  # 3.3→3.32초, 10.7→10.68초
FIRST_FRAME = "frame_00000023"  # 15초·25fps 창 하나의 첫 표본 — 재생 분석의 결정적 격자


def _encode(source: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=15",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", "15", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-c:a", "pcm_s16le", str(source),
        ],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


def analyzed_tier_project(
    tmp_path: Path, suitability: str = "recommended"
) -> tuple[RawWorkspaceService, Path]:
    """가져오기 → 순서 → 재생 분석까지 — 검토 머리는 아직 없다. 창 하나의 Luna 판정을 고른다."""
    source = tmp_path / "tier.mov"
    _encode(source)
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "등급 관문")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    _write_replay_fixtures(
        root,
        {
            asset.content_sha256: [
                {"start": 0.5, "end": 1.5, "text": "안녕하세요."},
                {"start": 2, "end": 3, "text": "계곡에 왔어요."},
                {"start": 11, "end": 12, "text": "물이 맑네요."},
                {"start": 12.5, "end": 13.5, "text": "들어가 볼게요."},
            ]
        },
    )
    luna_path = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna_path.read_text())
    fixtures["windows"]["window_000000"].update(
        suitability=suitability,
        suitability_reason="계곡과 인물이 선명하다",
        evidence_ids=[FIRST_FRAME],
        uncertainties=["원거리 인물 식별 불확실"],
    )
    luna_path.write_text(json.dumps(fixtures, ensure_ascii=False))
    analysis = service.start_analysis(root, mode="replay")
    assert _wait_terminal(service, root, analysis.job_id).status == "succeeded"
    return service, root


def tier_project(
    tmp_path: Path, suitability: str = "recommended"
) -> tuple[RawWorkspaceService, Path]:
    """분석 뒤 자동 정리까지 — 검토 머리가 새 등급판으로 선다."""
    service, root = analyzed_tier_project(tmp_path, suitability)
    cleanup = service.start_cleanup(root)
    finished = _wait_terminal(service, root, cleanup.job_id)
    assert finished.status == "succeeded", finished.error
    return service, root


def _seconds(head, segment) -> tuple[Fraction, Fraction]:
    clock = head.clock_for(segment.source_id)
    return (
        (segment.start_pts - clock.origin_pts) * clock.time_base,
        (segment.end_pts - clock.origin_pts) * clock.time_base,
    )


def _legacy_head(service: RawWorkspaceService, root: Path, monkeypatch: pytest.MonkeyPatch):
    """W-11 이전 모양의 머리 — 빨강을 자르지 않고, 옛 Luna 등급 태그만 단다."""
    with monkeypatch.context() as patch:
        patch.setattr(tiering, "red_decisions", lambda report: [])
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        plan = service.plan_segmentation_from_analysis(root)
        head = ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)
    assert head.sequence == 0 and not ReviewWorkspaceFacade.has_tier_marker(head)
    assert unreviewed_auto_exclusions(head) == ()
    return head


@needs_ffmpeg
def test_red_silence_becomes_unreviewed_auto_exclusion_and_export_refuses_until_confirm(
    tmp_path: Path,
):
    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    pending = unreviewed_auto_exclusions(head)
    assert [_seconds(head, s) for s in pending] == [RED]
    (segment,) = pending
    provenance = json.loads(segment.notes.removeprefix("자동 제외: "))
    assert provenance["reason"] == tiering.RED_SILENCE
    assert provenance["version"] == "raw-tier/v2"
    assert provenance["evidence"][:3] == [
        "tier:silent-not-scenery",
        "audio:subtitle_silence",
        "video:usable",
    ]
    assert not any(e.startswith("pcm:") for e in provenance["evidence"])
    assert "등급: 제외 제안" in segment.visual_tags
    assert "등급판 raw-tier/v2" in segment.visual_tags
    with pytest.raises(RawWorkspaceError, match="자동 제외 1개를 먼저 확인"):
        service.export_final_cut(root)
    confirmed = facade.confirm_automatic_exclusions()
    assert unreviewed_auto_exclusions(confirmed) == ()
    assert [s.selection for s in confirmed.segments] == [s.selection for s in head.segments]
    destination, _xml = service.export_final_cut(root)
    manifest = json.loads((root / destination / "confirmed_manifest.json").read_text())
    assert segment.segment_id not in {c["segment_id"] for c in manifest["clips"]}


@needs_ffmpeg
def test_green_and_yellow_never_change_selection(tmp_path: Path):
    """Luna가 그 화면을 '추천'으로 보면 자막 없는 공백은 풍경이다 — 노랑, 제외 제안 없음(D52)."""
    service, root = tier_project(tmp_path)
    head = ReviewWorkspaceFacade(root).load_head().revision
    rows = service.selection_suitability(
        root, service.analysis_overview(root).revision.revision_id, (head.segments[0].source_id,)
    )[head.segments[0].source_id]
    assert [(row["tier"], row["rule"]) for row in rows] == [
        ("green", "both-good"),
        ("yellow", "scenery-no-speech"),
        ("green", "both-good"),
    ]
    assert rows[0]["audio"]["status"] == "speech_dense"
    assert rows[0]["video"]["status"] == "good"
    assert "확인 메모: 원거리 인물 식별 불확실" in rows[0]["video"]["reason"]
    assert (rows[1]["audio"]["status"], rows[1]["red_reason"]) == ("subtitle_silence", None)
    assert unreviewed_auto_exclusions(head) == ()
    for segment in head.segments:
        assert segment.selection == "included"
        assert tiering.tier_marker() in segment.visual_tags
        assert AUTO_EXCLUDED_UNREVIEWED_TAG not in segment.visual_tags
        assert not segment.notes.startswith("자동 제외")
        assert "등급: 제외 제안" not in segment.visual_tags


@needs_ffmpeg
def test_retier_keeps_manual_selection_override_and_notes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    legacy = _legacy_head(service, root, monkeypatch)
    first, *_ = legacy.segments
    edited = facade.set_selection(legacy.base_plan_id, first.segment_id, "excluded")
    edited = facade.set_override(edited.base_plan_id, first.segment_id, "review_required")
    edited = facade.set_notes(edited.base_plan_id, first.segment_id, "사람 메모")
    retiered = facade.retier(edited.base_plan_id)
    assert retiered.sequence == edited.sequence + 1
    assert retiered.edit is not None and retiered.edit.notes == "등급 재계산 raw-tier/v2"
    for before, after in zip(edited.segments, retiered.segments, strict=True):
        assert (before.segment_id, before.start_pts, before.end_pts) == (
            after.segment_id,
            after.start_pts,
            after.end_pts,
        )
        assert (before.selection, before.notes, before.override_recommendation) == (
            after.selection,
            after.notes,
            after.override_recommendation,
        )
        assert tiering.tier_marker() in after.visual_tags
        assert not any(t.startswith("Luna 평가: ") for t in after.visual_tags)
    kept = next(s for s in retiered.segments if s.segment_id == first.segment_id)
    assert kept.recommendation == first.recommendation  # 덮은 조각은 권고도 그대로
    assert facade.retier(retiered.base_plan_id) == retiered  # 바뀐 것이 없으면 리비전 없음


@needs_ffmpeg
def test_cleanup_on_unedited_head_materializes_red_on_edited_head_only_relabels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    legacy = _legacy_head(service, root, monkeypatch)
    assert facade.load_head().revision == legacy  # 옮길 머리(표지 없음)가 지금 머리다
    finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert finished.status == "succeeded", finished.error
    migrated = facade.load_head().revision
    assert migrated.base_plan_id != legacy.base_plan_id
    assert [_seconds(migrated, s) for s in unreviewed_auto_exclusions(migrated)] == [RED]
    assert ReviewWorkspaceFacade.has_tier_marker(migrated)
    assert service.migrate_tiers(root) is None  # 정리가 이미 옮겼다 — 앱 열기에 할 일이 없다

    # 사람이 손댄 옛 머리는 경계를 그대로 두고 등급만 다시 단다.
    legacy = _legacy_head(service, root, monkeypatch)
    touched = facade.set_selection(legacy.base_plan_id, legacy.segments[-1].segment_id, "excluded")
    finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert finished.status == "succeeded", finished.error
    relabeled = facade.load_head().revision
    # 정리는 낡은 package 때문에 같은 경계의 새 계획으로 입양할 수 있다 — 경계·선택은 같다.
    assert [(s.start_pts, s.end_pts, s.selection) for s in relabeled.segments] == [
        (s.start_pts, s.end_pts, s.selection) for s in touched.segments
    ]
    assert not any(t.startswith("Luna 평가: ") for s in relabeled.segments for t in s.visual_tags)
    assert unreviewed_auto_exclusions(relabeled) == ()
    assert ReviewWorkspaceFacade.has_tier_marker(relabeled)
    # 빨강은 아직 조각 경계로 떨어지지 않았다 — 화면이 그 수를 알린다.
    report = service.tier_report(
        root, service.analysis_overview(root).revision.revision_id, legacy.segments[0].source_id
    )
    clock = relabeled.clocks[0]
    edges = {_seconds(relabeled, s)[i] for s in relabeled.segments for i in (0, 1)}
    assert (
        tiering.unmaterialized_red(
            tiering.red_ranges(report), edges, clock.frame_ticks * clock.time_base
        )
        == 1
    )


@needs_ffmpeg
def test_migrate_tiers_rebuilds_unedited_head_in_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path, suitability="usable")
    legacy = _legacy_head(service, root, monkeypatch)
    message = service.migrate_tiers(root)
    assert message is not None and "AI 판정 + 자막" in message and "다시 세웁니다" in message
    assert "무료" not in message
    job = next(j for j in service.list_jobs(root) if j.kind == "tier_migration")
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    migrated = ReviewWorkspaceFacade(root).load_head().revision
    assert migrated.base_plan_id != legacy.base_plan_id
    assert len(unreviewed_auto_exclusions(migrated)) == 1
    # 이미 지금 판이면 옮길 것이 없다 — 작업도 리비전도 없다.
    jobs = len(service.list_jobs(root))
    assert service.migrate_tiers(root) is None
    assert len(service.list_jobs(root)) == jobs
    assert ReviewWorkspaceFacade(root).load_head().revision == migrated


@needs_ffmpeg
def test_failed_migration_is_reported_not_resubmitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """같은 머리에서 옮기기가 실패했으면 다시 걸지 않고 그 이유를 안내한다 — 조용히 쌓지 않는다."""
    service, root = tier_project(tmp_path, suitability="usable")
    legacy = _legacy_head(service, root, monkeypatch)

    def broken(*args, **kwargs):
        raise RawWorkspaceError("합성 재계획 실패")

    monkeypatch.setattr(service, "_migrate_unedited_head", broken)
    assert service.migrate_tiers(root) is not None
    job = next(j for j in service.list_jobs(root) if j.kind == "tier_migration")
    assert _wait_terminal(service, root, job.job_id).status == "failed"
    jobs = len(service.list_jobs(root))
    for _ in range(3):  # 화면 결과 갱신마다 불려도 같은 앱 실행 안에서는 다시 걸지 않는다
        assert service.migrate_tiers(root) == "등급판 옮기기 실패: 합성 재계획 실패"
    assert len(service.list_jobs(root)) == jobs
    assert ReviewWorkspaceFacade(root).load_head().revision == legacy


@needs_ffmpeg
def test_failed_migration_is_retried_once_by_a_new_app_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """실패 기록은 머리에 묶여 남지만, 앱을 다시 열면(새 서비스) 한 번 더 시도한다 — 원본 볼륨이
    잠깐 빠졌던 것 같은 일시 원인이 영구 차단이 되지 않게."""
    service, root = tier_project(tmp_path, suitability="usable")
    legacy = _legacy_head(service, root, monkeypatch)

    def broken(*args, **kwargs):
        raise RawWorkspaceError("원본 볼륨이 빠졌다")

    with monkeypatch.context() as patch:
        patch.setattr(service, "_migrate_unedited_head", broken)
        assert service.migrate_tiers(root) is not None
        job = next(j for j in service.list_jobs(root) if j.kind == "tier_migration")
        assert _wait_terminal(service, root, job.job_id).status == "failed"
    assert service.migrate_tiers(root) == "등급판 옮기기 실패: 원본 볼륨이 빠졌다"
    reopened = RawWorkspaceService()
    message = reopened.migrate_tiers(root)
    assert message is not None and "다시 세웁니다" in message
    retry = next(j for j in reopened.list_jobs(root) if j.kind == "tier_migration")
    assert retry.job_id != job.job_id
    assert _wait_terminal(reopened, root, retry.job_id).status == "succeeded"
    migrated = ReviewWorkspaceFacade(root).load_head().revision
    assert migrated.base_plan_id != legacy.base_plan_id
    assert ReviewWorkspaceFacade.has_tier_marker(migrated)
    assert reopened.migrate_tiers(root) is None


@needs_ffmpeg
def test_migrate_tiers_on_touched_head_only_relabels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path, suitability="usable")
    legacy = _legacy_head(service, root, monkeypatch)
    facade = ReviewWorkspaceFacade(root)
    touched = facade.set_notes(legacy.base_plan_id, legacy.segments[0].segment_id, "사람 메모")
    message = service.migrate_tiers(root)
    assert message is not None and "등급 표시만 다시 답니다" in message
    assert "사람이 정한 사용/제외는 그대로" in message and "무료" not in message
    job = next(j for j in service.list_jobs(root) if j.kind == "tier_migration")
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    relabeled = facade.load_head().revision
    assert relabeled.base_plan_id == touched.base_plan_id
    assert ReviewWorkspaceFacade.has_tier_marker(relabeled)
    assert [(s.start_pts, s.end_pts, s.selection, s.notes) for s in relabeled.segments] == [
        (s.start_pts, s.end_pts, s.selection, s.notes) for s in touched.segments
    ]
    assert service.migrate_tiers(root) is None


@needs_ffmpeg
def test_replan_adoption_keeps_tier_red_pending(tmp_path: Path):
    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    pending = unreviewed_auto_exclusions(head)
    assert len(pending) == 1
    plan, mapping = facade.plan_from_review(head.base_plan_id)
    adopted = facade.adopt_plan_review(plan, mapping, old_revision=head)
    new_ids = {pair.old_segment_id: pair.new_segment_id for pair in mapping.pairs}
    assert {s.segment_id for s in unreviewed_auto_exclusions(adopted)} == {
        new_ids[s.segment_id] for s in pending
    }
    # 입양은 경계만 옮긴다 — 등급 태그와 권고는 새 조각에서 다시 매겨진다.
    assert [(s.recommendation, set(s.visual_tags)) for s in adopted.segments] == [
        (s.recommendation, set(s.visual_tags)) for s in head.segments
    ]
    with pytest.raises(RawWorkspaceError, match="먼저 확인"):
        service.export_final_cut(root)
    # 사람이 아무것도 안 했으면 재조립도 같은 빨강을 다시 미검토로 만든다.
    again = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    assert [_seconds(again, s) for s in unreviewed_auto_exclusions(again)] == [RED]


@needs_ffmpeg
def test_workbench_model_carries_tiers_and_pending_counts(tmp_path: Path):
    """작업대 모델 — 카드는 등급·미검토를, 모델은 제외 제안 수를 싣는다(실백엔드)."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_visual_workbench_ui import _model_of
    from ui.workspace_service import RawWorkspaceUi

    _, root = tier_project(tmp_path, suitability="usable")
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.open_workspace_view(str(root))
    model = _model_of(server.visual_workbench_render(root)[1])
    by_tier = {card["tier"]: card for card in model["cards"]}
    assert set(by_tier) == {"yellow", "red"}
    assert by_tier["red"]["autoPending"] is True and by_tier["red"]["sel"] == "excluded"
    assert by_tier["yellow"]["autoPending"] is False
    assert model["redPending"] == 1 and model["redUnmaterialized"] == 0


def test_no_free_retier_button_or_pending_note(tmp_path: Path, local_socket: None):
    """D52 — 「등급 다시 계산(무료)」 단추와 "등급 계산 중(무료)" 안내는 지워졌다."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    import gradio as gr

    import ui.workspace_service as workspace_ui

    app = workspace_ui.RawWorkspaceUi(state_path=tmp_path / "ui-state.json").build_blocks()
    labels = [str(block.value) for block in app.blocks.values() if isinstance(block, gr.Button)]
    assert "자동 제외 확인" in labels
    assert not [label for label in labels if "등급 다시 계산" in label or "무료" in label]
    for name in ("RETIER_LABEL", "TIER_PENDING_NOTE"):
        assert not hasattr(workspace_ui, name), name
    assert not hasattr(workspace_ui.RawWorkspaceUi, "capcut_retier_view")


def _carried_human_head(service: RawWorkspaceService, root: Path, monkeypatch: pytest.MonkeyPatch):
    """sequence 0인데 사람 결정이 실려 온 옛 머리 — 보존 원본이 옛 머리의 편집 조각을 옮긴다.

    옛 판(표지 없음) 머리에서 빨강 자동 제외를 확인하고, 마지막 조각을 손으로 제외한 뒤,
    새 계획을 `preserve_source_ids`로 초기화하면 그 결정들이 새 기본 리비전에 실린다.
    """
    facade = ReviewWorkspaceFacade(root)
    with monkeypatch.context() as patch:
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        legacy = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    assert len(unreviewed_auto_exclusions(legacy)) == 1
    facade.confirm_automatic_exclusions(legacy.base_plan_id)
    facade.set_selection(legacy.base_plan_id, legacy.segments[-1].segment_id, "excluded")
    source = legacy.segments[0].source_id
    carried = facade.initialize_review(
        service.plan_segmentation_from_analysis(root).revision_id, preserve_source_ids=(source,)
    )
    assert carried.sequence == 0 and not ReviewWorkspaceFacade.has_tier_marker(carried)
    assert ReviewWorkspaceFacade.has_human_decisions(carried)
    assert not facade.is_untouched(carried)
    excluded = [s for s in carried.segments if s.selection == "excluded"]
    assert len(excluded) == 2 and unreviewed_auto_exclusions(carried) == ()
    return carried


@needs_ffmpeg
@pytest.mark.parametrize("trigger", ["migrate", "cleanup"])
def test_carried_human_decisions_survive_tier_migration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str
):
    """sequence 0이어도 사람 결정이 실린 머리는 다시 세우지 않고 등급만 단다(D35)."""
    service, root = tier_project(tmp_path, suitability="usable")
    carried = _carried_human_head(service, root, monkeypatch)
    if trigger == "migrate":
        assert service.migrate_tiers(root) is not None
        for job in [j for j in service.list_jobs(root) if j.kind == "tier_migration"]:
            assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    else:
        finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
        assert finished.status == "succeeded", finished.error
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert ReviewWorkspaceFacade.has_tier_marker(head)
    assert [(s.start_pts, s.end_pts, s.selection) for s in head.segments] == [
        (s.start_pts, s.end_pts, s.selection) for s in carried.segments
    ]
    assert unreviewed_auto_exclusions(head) == ()  # 확인한 자동 제외는 확인된 채
    assert [s.notes for s in head.segments] == [s.notes for s in carried.segments]


def test_human_decision_markers_cover_every_kind_of_edit():
    from tests.raw.test_confirmed_package import make_review

    review = make_review()
    assert not ReviewWorkspaceFacade.has_human_decisions(review)
    first = review.segments[0]

    def with_first(**changes):
        return replace(review, segments=(replace(first, **changes), *review.segments[1:]))

    machine = (
        with_first(notes="확인 필요: window_000000:검토 필요"),
        with_first(
            selection="excluded",
            visual_tags=(AUTO_EXCLUDED_UNREVIEWED_TAG,),
            notes="자동 제외: {}",
        ),
    )
    human = (
        with_first(override_recommendation="normal"),
        with_first(notes="수동 선택 · 자동 제외: {}"),
        with_first(notes="사람 메모"),
        with_first(selection="excluded"),  # 손 제외 또는 확인된 자동 제외
        with_first(notes="자동 제외: {}"),  # 「자동 제외 전체 복원」으로 사용
    )
    assert not any(ReviewWorkspaceFacade.has_human_decisions(r) for r in machine)
    assert all(ReviewWorkspaceFacade.has_human_decisions(r) for r in human)


def _proposal(start: Fraction, end: Fraction, reason: str, version: str | None) -> dict:
    row: dict[str, object] = {
        "start": {"num": start.numerator, "den": start.denominator},
        "end": {"num": end.numerator, "den": end.denominator},
        "reason": reason,
        "evidence": [f"synthetic:{reason}"],
    }
    if version is not None:
        row["version"] = version
    return row


@needs_ffmpeg
def test_retier_releases_stale_tier_proposals_only(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """D52 — 옛 등급판의 미검토 빨강 제안은 지금 등급이 빨강이 아니면 풀린다. 그 밖은 그대로다.

    Luna '사용 가능' 창이라 3.3–10.7초는 지금도 빨강이다. 옛 판(v1) 제안 다섯 자리를 심는다:
    지금도 빨강인 자리(유지) · 사람이 확인한 자리(유지) · 지금 판 표지의 자리(유지) · 옛 영상
    결함 이유의 노랑 자리(풀림) · 자동 정리(`internal_silence`) 자리(유지).
    """
    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    still_red = (Fraction(83, 25), Fraction(7))
    confirmed = (Fraction(7), Fraction(267, 25))
    current = (Fraction(267, 25), Fraction(12))
    stale = (Fraction(12), Fraction(69, 5))
    cleanup = (Fraction(69, 5), Fraction(15))
    planted = [
        _proposal(*still_red, tiering.RED_SILENCE, "raw-tier/v1"),
        _proposal(*confirmed, tiering.RED_SILENCE, "raw-tier/v1"),
        _proposal(*current, tiering.RED_SILENCE, tiering.TIER_SETTINGS.tier_version),
        _proposal(*stale, "tier_video_defect", "raw-tier/v1"),
        _proposal(*cleanup, "internal_silence", None),
    ]
    with monkeypatch.context() as patch:
        patch.setattr(tiering, "red_decisions", lambda report: planted)
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        plan = service.plan_segmentation_from_analysis(root)
        legacy = facade.initialize_review(plan.revision_id)
    spans = {_seconds(legacy, s): s for s in legacy.segments}
    assert {still_red, confirmed, current, stale, cleanup} <= set(spans)
    assert len(unreviewed_auto_exclusions(legacy)) == 5
    touched = facade.set_selection(
        legacy.base_plan_id, spans[confirmed].segment_id, "excluded"
    )  # 사람이 확인한 제외 — 미검토 표지가 빠진다
    retiered = facade.retier(touched.base_plan_id)
    after = {_seconds(retiered, s): s for s in retiered.segments}
    released = after[stale]
    assert released.selection == "included" and released.notes == ""
    assert AUTO_EXCLUDED_UNREVIEWED_TAG not in released.visual_tags
    assert "등급: 보통" in released.visual_tags
    assert {_seconds(retiered, s) for s in unreviewed_auto_exclusions(retiered)} == {
        still_red,
        current,
        cleanup,
    }
    for kept in (still_red, confirmed, current, cleanup):
        before = {_seconds(touched, s): s for s in touched.segments}[kept]
        assert (after[kept].selection, after[kept].notes) == (before.selection, before.notes)
    assert after[confirmed].selection == "excluded"
    assert after[confirmed].notes.startswith("수동 선택 · 자동 제외: ")
    assert "등급: 제외 제안" in after[still_red].visual_tags
    assert facade.retier(retiered.base_plan_id) == retiered  # 다시 해도 바뀌는 것이 없다


@needs_ffmpeg
def test_touched_head_message_names_released_stale_proposals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """손댄 머리의 안내는 실제로 일어나는 일을 말한다 — 근거 없어진 옛 제안은 사용으로 돌아간다."""
    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    stale = (Fraction(12), Fraction(69, 5))
    planted = [_proposal(*stale, tiering.RED_SILENCE, "raw-tier/v1")]
    with monkeypatch.context() as patch:
        patch.setattr(tiering, "red_decisions", lambda report: planted)
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        legacy = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    other = next(s for s in legacy.segments if _seconds(legacy, s) != stale)
    touched = facade.set_notes(legacy.base_plan_id, other.segment_id, "사람 메모")
    assert {_seconds(touched, s): s.selection for s in touched.segments}[stale] == "excluded"
    message = service.migrate_tiers(root)
    assert message is not None and "근거가 사라진 옛 제외 제안은 사용으로 되돌립니다" in message
    job = next(j for j in service.list_jobs(root) if j.kind == "tier_migration")
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    head = facade.load_head().revision
    assert {_seconds(head, s): s.selection for s in head.segments}[stale] == "included"
    assert unreviewed_auto_exclusions(head) == ()
    assert next(s for s in head.segments if s.segment_id == other.segment_id).notes == "사람 메모"


@needs_ffmpeg
@pytest.mark.parametrize("trigger", ["migrate", "cleanup"])
def test_hand_edited_proposal_note_is_left_for_a_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, trigger: str
):
    """근거 메모를 사람이 고친 옛 제안은 풀지 않는다 — 옮기기·정리는 멈추지 않고 경고만 남긴다.

    작업대 메모 칸은 자동 제외 근거로 미리 채워져 있어, 뒤에 말을 덧붙이면 JSON이 깨진다.
    메모를 지우며 풀면 사람이 쓴 말이 사라진다 — 미검토 제외로 남겨 사람이 정하게 한다.
    """
    from vlogsrt import log

    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    appended, listed = (Fraction(12), Fraction(69, 5)), (Fraction(69, 5), Fraction(15))
    planted = [
        _proposal(*appended, tiering.RED_SILENCE, "raw-tier/v1"),
        _proposal(*listed, tiering.RED_SILENCE, "raw-tier/v1"),
    ]
    with monkeypatch.context() as patch:
        patch.setattr(tiering, "red_decisions", lambda report: planted)
        patch.setattr(
            tiering, "segment_grade", lambda report, a, b: ("normal", ("Luna 평가: 옛 등급",))
        )
        legacy = facade.initialize_review(service.plan_segmentation_from_analysis(root).revision_id)
    spans = {_seconds(legacy, s): s for s in legacy.segments}
    head = facade.set_notes(
        legacy.base_plan_id, spans[appended].segment_id, spans[appended].notes + " 나중에 확인"
    )
    head = facade.set_notes(head.base_plan_id, spans[listed].segment_id, "자동 제외: [1, 2]")
    edited = {_seconds(head, s): (s.selection, s.notes) for s in head.segments}
    assert len(unreviewed_auto_exclusions(head)) == 2
    warnings: list[str] = []
    monkeypatch.setattr(log, "warn", warnings.append)
    if trigger == "migrate":
        assert service.migrate_tiers(root) is not None
        job = next(j for j in service.list_jobs(root) if j.kind == "tier_migration")
        finished = _wait_terminal(service, root, job.job_id)
    else:
        finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
    assert finished.status == "succeeded", finished.error
    after = facade.load_head().revision
    assert ReviewWorkspaceFacade.has_tier_marker(after)
    for span in (appended, listed):
        segment = next(s for s in after.segments if _seconds(after, s) == span)
        assert (segment.selection, segment.notes) == edited[span]
        assert AUTO_EXCLUDED_UNREVIEWED_TAG in segment.visual_tags
    assert len([w for w in warnings if "자동 제외 근거" in w]) >= 2
    if trigger == "migrate":
        assert service.migrate_tiers(root) is None
        finished = _wait_terminal(service, root, service.start_cleanup(root).job_id)
        assert finished.status == "succeeded", finished.error
