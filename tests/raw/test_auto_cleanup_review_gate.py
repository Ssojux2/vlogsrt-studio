"""자동 제외는 사람이 보기 전까지 미검토다 — 잘라 내는 확정 내보내기는 그 상태로 나가지
못한다(D35·W-5). Final Cut 전체 타임라인은 지우지 않으므로 관문이 없고, 미검토 제외는 키워드와
함께 보낸다 — 등급이 빨강이면 처음부터, 아니면 사람이 확인한 뒤부터 꺼진 채 간다(D56·D80).

순수 층(도메인 편집·설정)은 손으로 만든 리비전으로, 통합 층(조립·재분석·출력 관문)은
`test_capcut_flow`와 같은 합성 ffmpeg 미디어로 잰다. 유료 호출 0회.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_auto_cleanup import document
from tests.raw.test_confirmed_package import make_review
from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt.project.raw.analysis_config import (
    RawAutoCleanupSettings,
    validate_auto_cleanup_settings,
)
from vlogsrt.project.raw.auto_cleanup import decisions
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    merge_segments,
    set_selection,
    split_segment,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.service import RawWorkspaceError, RawWorkspaceService

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def _segment(review, segment_id: str):
    return next(s for s in review.segments if s.segment_id == segment_id)


def _pending(review, *segment_ids: str):
    """자동 제외 미검토로 만든 리비전 — 조립 단계가 붙이는 모양 그대로."""
    return replace(
        review,
        segments=tuple(
            replace(
                s,
                selection="excluded",
                visual_tags=(*s.visual_tags, AUTO_EXCLUDED_UNREVIEWED_TAG),
                notes="자동 제외: {}",
            )
            if s.segment_id in segment_ids
            else s
            for s in review.segments
        ),
    )


def test_explicit_selection_clears_marker_for_that_segment_only():
    review = _pending(make_review(), "seg_a1", "seg_a2")
    assert {s.segment_id for s in unreviewed_auto_exclusions(review)} == {"seg_a1", "seg_a2"}
    for choice in ("included", "excluded"):
        edited = set_selection(review, "seg_a1", choice)
        assert [s.segment_id for s in unreviewed_auto_exclusions(edited)] == ["seg_a2"]
        assert _segment(edited, "seg_a1").selection == choice


def test_split_and_merge_never_launder_the_marker():
    review = _pending(make_review(), "seg_a1")
    clock = review.clocks[0]
    first = _segment(review, "seg_a1")
    mid = clock.origin_pts + clock.frame_ticks * 10
    assert first.start_pts < mid < first.end_pts
    split = split_segment(review, "seg_a1", mid)
    assert len(unreviewed_auto_exclusions(split)) == 2
    merged = merge_segments(split, "seg_a1#1", "seg_a1#2")
    assert [s.segment_id for s in unreviewed_auto_exclusions(merged)] == ["seg_a1#1"]


def test_included_segment_is_never_counted_even_with_a_stale_tag():
    review = make_review()
    tagged = replace(
        review,
        segments=(
            replace(review.segments[0], visual_tags=(AUTO_EXCLUDED_UNREVIEWED_TAG,)),
            *review.segments[1:],
        ),
    )
    assert unreviewed_auto_exclusions(tagged) == ()


def test_auto_cleanup_defaults_reproduce_the_previous_decisions(tmp_path: Path):
    doc = document()
    pcm = tmp_path / "synthetic.pcm"
    pcm.write_bytes(bytes(20 * 16000 * 2))
    implicit = decisions(doc, source_id="s", revision_id="a", pcm_path=pcm)
    explicit = decisions(
        doc, source_id="s", revision_id="a", pcm_path=pcm, settings=RawAutoCleanupSettings()
    )
    assert implicit == explicit
    silent = next(d for d in explicit if d.reason == "internal_silence")
    assert (silent.start, silent.end) == (Fraction(33, 10), Fraction(67, 10))
    assert silent.evidence[:2] == ("pcm:peak<=-50dBFS", "speech_handle:3/10")
    assert RawAutoCleanupSettings().peak_sample_limit() == 103


def test_auto_cleanup_settings_change_the_decisions(tmp_path: Path):
    doc = document()
    pcm = tmp_path / "synthetic.pcm"
    pcm.write_bytes(bytes(20 * 16000 * 2))

    def silences(settings: RawAutoCleanupSettings):
        return [
            d
            for d in decisions(doc, source_id="s", revision_id="a", pcm_path=pcm, settings=settings)
            if d.reason == "internal_silence"
        ]

    # 공백은 4초(3→7)다 — 더 긴 기준이면 후보가 아니다.
    assert silences(RawAutoCleanupSettings(silence_seconds=4.5)) == []
    wider = silences(RawAutoCleanupSettings(speech_handle_seconds=0.5))
    assert [(d.start, d.end) for d in wider] == [(Fraction(7, 2), Fraction(13, 2))]
    pcm.write_bytes(b"\xff\x1f" * 20 * 16000)  # 8191 — 기본 기준으로는 소리다
    assert silences(RawAutoCleanupSettings()) == []
    assert silences(RawAutoCleanupSettings(silence_peak_dbfs=-10.0))


@pytest.mark.parametrize(
    "settings",
    [
        RawAutoCleanupSettings(silence_seconds=0.0),
        RawAutoCleanupSettings(speech_handle_seconds=-0.1),
        RawAutoCleanupSettings(silence_seconds=0.5, speech_handle_seconds=0.3),
        RawAutoCleanupSettings(silence_peak_dbfs=3.0),
        RawAutoCleanupSettings(silence_peak_dbfs=float("nan")),
    ],
)
def test_auto_cleanup_settings_reject_contract_violations(settings: RawAutoCleanupSettings):
    with pytest.raises(ValueError):
        validate_auto_cleanup_settings(settings)
    with pytest.raises(ValueError):
        decisions(document(), source_id="s", revision_id="a", settings=settings)


# ── 통합: 합성 미디어로 조립 → 출력 관문 ─────────────────────────


def _synthetic_project(tmp_path: Path) -> tuple[RawWorkspaceService, Path]:
    source = tmp_path / "synthetic.mov"
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=12",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", "12", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-c:a", "pcm_s16le", str(source),
        ],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "자동 제외 관문")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    _write_replay_fixtures(
        root,
        {
            asset.content_sha256: [
                {"start": 0.5, "end": 1.5, "text": "오늘은 여행을 갑니다."},
                {"start": 5, "end": 6, "text": "오늘은 여행을 갑니다."},
                {"start": 8, "end": 9, "text": "다음 장소로 갑니다."},
            ]
        },
    )
    luna_path = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna_path.read_text())
    fixtures["windows"]["window_000000"]["scene_tags"] = ["dialogue"]
    luna_path.write_text(json.dumps(fixtures))
    analysis = service.start_analysis(root, mode="replay")
    assert _wait_terminal(service, root, analysis.job_id).status == "succeeded"
    cleanup = service.start_cleanup(root)
    finished = _wait_terminal(service, root, cleanup.job_id)
    assert finished.status == "succeeded", finished.error
    return service, root


@pytest.fixture
def d80_export(monkeypatch: pytest.MonkeyPatch) -> None:
    """보통·판정 보류도 켜던 D80 보내기 설정 — 이 시험은 제외 규칙(확정·미검토)을 본다. 추천만 켜는
    지금 보내기(D93)는 `tests/raw/test_finalcut_green_only.py`가 본다."""
    from vlogsrt.project.raw import finalcut_export
    from vlogsrt.project.raw.config import FinalCutTimelineSettings

    monkeypatch.setattr(finalcut_export, "FINALCUT_TIMELINE", FinalCutTimelineSettings())


def _disabled(xml_path: Path) -> int:
    clips = ET.parse(xml_path).findall(".//spine/asset-clip")
    return sum(clip.get("enabled") == "0" for clip in clips)


def _package(service: RawWorkspaceService, root: Path) -> str:
    return service.package_overview(root).package.package_dir


@needs_ffmpeg
def test_materialization_marks_exclusions_and_only_the_cutting_export_is_gated(
    tmp_path: Path, d80_export: None
):
    service, root = _synthetic_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    excluded = [s for s in head.segments if s.selection == "excluded"]
    assert len(excluded) >= 2
    assert all(s.notes.startswith("자동 제외: ") for s in excluded)
    assert {s.segment_id for s in unreviewed_auto_exclusions(head)} == {
        s.segment_id for s in excluded
    }
    with pytest.raises(RawWorkspaceError, match=f"자동 제외 {len(excluded)}개를 먼저 확인"):
        facade.export_confirmed(_package(service, root))
    assert _disabled(service.export_finalcut_timeline(root)) == 0  # 보내기는 막지 않는다
    confirmed = facade.confirm_automatic_exclusions()
    assert confirmed.sequence == head.sequence + 1
    assert unreviewed_auto_exclusions(confirmed) == ()
    assert [s.selection for s in confirmed.segments] == [s.selection for s in head.segments]
    assert [s.notes for s in confirmed.segments] == [s.notes for s in head.segments]
    outcome = facade.export_confirmed(_package(service, root))
    manifest = json.loads((root / outcome.destination_dir / "confirmed_manifest.json").read_text())
    assert not {s.segment_id for s in excluded} & {c["segment_id"] for c in manifest["clips"]}
    assert _disabled(service.export_finalcut_timeline(root)) == len(excluded)
    assert (
        facade.confirm_automatic_exclusions() == confirmed
    )  # 할 일이 없으면 리비전을 만들지 않는다


@needs_ffmpeg
def test_explicit_include_clears_one_segment_and_other_stays_gated(tmp_path: Path):
    service, root = _synthetic_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    first, *rest = [s for s in head.segments if s.selection == "excluded"]
    changed = facade.set_selection(head.base_plan_id, first.segment_id, "included")
    assert {s.segment_id for s in unreviewed_auto_exclusions(changed)} == {
        s.segment_id for s in rest
    }
    with pytest.raises(RawWorkspaceError, match=f"자동 제외 {len(rest)}개를 먼저 확인"):
        facade.export_confirmed(_package(service, root))
    manual = _segment(changed, first.segment_id)
    assert manual.selection == "included" and manual.notes.startswith("수동 선택 · ")


@needs_ffmpeg
def test_reanalysis_neither_clears_a_pending_marker_nor_undoes_a_confirmation(tmp_path: Path):
    service, root = _synthetic_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    pending = {s.segment_id for s in unreviewed_auto_exclusions(head)}
    assert pending
    # 사람이 아무것도 안 했으면 재조립은 같은 근거로 다시 미검토를 만든다.
    plan = service.plan_segmentation_from_analysis(root)
    again = facade.initialize_review(plan.revision_id)
    assert {s.segment_id for s in unreviewed_auto_exclusions(again)} == pending
    with pytest.raises(RawWorkspaceError, match="먼저 확인"):
        facade.export_confirmed(_package(service, root))
    confirmed = facade.confirm_automatic_exclusions(plan.revision_id)
    replan = service.plan_segmentation_from_analysis(root)
    retained = facade.initialize_review(replan.revision_id)
    assert unreviewed_auto_exclusions(retained) == ()
    assert [(s.start_pts, s.end_pts, s.selection) for s in retained.segments] == [
        (s.start_pts, s.end_pts, s.selection) for s in confirmed.segments
    ]


@needs_ffmpeg
def test_plan_adoption_replay_keeps_pending_exclusions_pending(tmp_path: Path):
    service, root = _synthetic_project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    pending = unreviewed_auto_exclusions(head)
    assert pending
    plan, mapping = facade.plan_from_review(head.base_plan_id)
    adopted = facade.adopt_plan_review(plan, mapping, old_revision=head)
    assert adopted.base_plan_id != head.base_plan_id
    new_ids = {pair.old_segment_id: pair.new_segment_id for pair in mapping.pairs}
    assert {s.segment_id for s in unreviewed_auto_exclusions(adopted)} == {
        new_ids[s.segment_id] for s in pending
    }
    with pytest.raises(RawWorkspaceError, match="먼저 확인"):
        facade.export_confirmed(_package(service, root))


@needs_ffmpeg
def test_ui_shows_pending_count_sends_without_a_gate_and_confirms_in_one_click(
    tmp_path: Path, d80_export: None
):
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)

    from ui.workspace_service import FINALCUT_SEND_JOB_KIND, RawWorkspaceUi

    service, root = _synthetic_project(tmp_path)
    opened: list[Path] = []
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json", finalcut_opener=opened.append)
    signature = server.open_workspace_view(str(root))[1]
    pending = ReviewWorkspaceFacade(root).unreviewed_auto_exclusion_count()
    assert pending >= 2
    server.finalcut_send_view(signature)  # 미검토가 있어도 보낸다 — 빨강이 아니면 켠 채(D56·D80)
    job = next(j for j in server.raw.list_jobs(root) if j.kind == FINALCUT_SEND_JOB_KIND)
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    (sent,) = opened
    assert _disabled(sent) == 0
    message, _shell, workbench = server.capcut_refresh_view(signature)
    # 수와 '켠 채' 안내는 그릴 때마다 다시 세는 제외 제안 대기열이 싣는다(W-17 C 고침 3) — 답에
    # 실은 수는 그 순간의 값이라 확인·실행 취소 뒤 화면에 남아 대기열과 어긋났다.
    from tests.raw.test_visual_workbench_ui import _model_of

    queue = _model_of(workbench)["queue"]
    assert queue["open"] == queue["confirmable"] == pending
    assert "미검토 제외 제안" not in message
    message, _shell, _workbench = server.capcut_confirm_auto_view(signature)
    assert f"제외 제안 {pending}개를 확인했습니다" in message
    assert "미검토" not in message
    assert ReviewWorkspaceFacade(root).unreviewed_auto_exclusion_count() == 0
    assert "확인할 제외 제안이 없습니다" in server.capcut_confirm_auto_view(signature)[0]


def test_confirm_source_sits_next_to_the_send_button_with_the_old_handler(
    tmp_path: Path, local_socket: None
):
    """일괄 확인(끈 채 보내기)은 접힌 서랍 안에 두지 않는다 — 옛 처리기를 단 원천 단추
    (`vl-queue-confirm-src`)는 보내기 단추 곁 명령 줄에 숨어 있다. node 없이도 늘 돈다."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    import gradio as gr

    from ui import ui_facade
    from ui.workspace_service import CONFIRM_ALL_SOURCE_LABEL, RawWorkspaceUi

    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    app = server.build_blocks()
    buttons = {block.value: block for block in app.blocks.values() if isinstance(block, gr.Button)}
    source = buttons[CONFIRM_ALL_SOURCE_LABEL]
    assert source.elem_id == ui_facade.CONFIRM_ALL_SOURCE_ID == "vl-queue-confirm-src"
    assert source.visible == "hidden"
    (dep,) = [d for d in app.config["dependencies"] if d["targets"] == [(source._id, "click")]]
    assert app.fns[dep["id"]].fn == server.capcut_confirm_auto_view  # 옛 처리기 그대로
    # 명령 줄의 숨은 원천 — 보내기 단추는 W-46a에 서랍의 「고급 · Final Cut」으로 갔다.
    assert source.parent is buttons["추천만 이어붙여 내보내기"].parent
    node = source.parent
    while node is not None:
        assert not isinstance(node, (gr.Accordion, gr.Sidebar))
        node = getattr(node, "parent", None)


@pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")
def test_the_visible_queue_button_presses_the_hidden_source(tmp_path: Path) -> None:
    """화면에 **보이는** 단추는 제외 제안 대기열의 「남은 M개 모두 제외 유지」다(W-17 C) — 누르면
    원천 단추가 눌리고 서버 함수는 부르지 않는다. 실행본을 돌려야 하므로 node가 있어야 한다."""
    pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
    from tests.raw.test_proposal_queue import _run_queue_harness

    report = _run_queue_harness(tmp_path)
    visible = 'data-vl-act="queue-confirm">남은 2개 모두 제외 유지</button>'
    assert visible in report["queuePanel"]  # 대기열에 그려진, 보이는 단추
    assert report["bulk"] == {"clicks": 1, "calls": 0}  # 누르면 원천 단추가 눌린다
