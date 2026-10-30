"""추천 기준 강화(D60) — 서비스가 **지금 리비전**의 자막 시각 확인 값을 등급에 한 자리에서 넘긴다.

합성 미디어(15초 · 창 하나)에 재생 분석을 돌리고, 확인 보고서는 손으로 심는다(재지 않는다 —
`sync_report.report_document`와 같은 모양). 보는 것:

- 값은 지금(자막 리비전·1단계 PCM·기준이 같은) 보고서에서만 온다 — 없음·낡음·다른 리비전은 모름.
- 화면 등급 · 검토 머리 · 자동 제외 후보 · Final Cut 키워드가 **같은** 등급을 본다.
- 빨강 판정이면 추천이 보통까지 — 다시 재 초록이면 정리 고리가 검토 머리의 등급을 다시 단다(사용/
  제외·메모·경계는 그대로), 보내기 키워드도 따른다.
- 빨강(제외 제안)·자동 제외 후보·컷 자리는 신호가 있든 없든 바이트까지 같다.
- v3·v4 옮기기는 사람 결정(확인한 제외·메모·덮기)을 그대로 두고, 두 번 하면 아무것도 하지 않는다.
  v4(20b-final 규칙 5)는 화면 문제가 있는 창 위의 추천을 보통까지로 내린다.

유료 호출 0회 — 제공자 조립을 막아 두고 잰다.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_caption_timing import _put_report, _settle_sync
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_tier_review_gate import RED, _seconds, tier_project
from vlogsrt.project import sync_check
from vlogsrt.project.config import SYNC_CHECK_SETTINGS
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import sync_report, tiering
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    unreviewed_auto_exclusions,
)
from vlogsrt.project.raw.service import UPKEEP_JOB_KIND, RawWorkspaceService

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

DENSE = (
    {"start": 0.5, "end": 3, "text": "안녕하세요 오늘은 계곡에 왔어요"},
    {"start": 3, "end": 6, "text": "물이 정말 맑고 시원해 보여요"},
    {"start": 6, "end": 9, "text": "저기 바위 위에 앉아 볼게요"},
    {"start": 9, "end": 12, "text": "발을 담그니까 차갑네요"},
    {"start": 12, "end": 14.5, "text": "이제 천천히 들어가 볼게요"},
)
"""말이 끊이지 않는 15초 — 창 하나(추천)가 녹색 한 구간이고 검토 머리는 「추천」 한 조각이다."""

CAP = (
    "추천 제한: 자막 시각 확인이 빨강이라 보통까지만 추천합니다 — 시각을 맞춘 뒤 다시 재면 풀립니다"
)


def _forbid_providers(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("등급 계산이 제공자를 조립했다")

    for name in ("_analysis_providers", "_live_asr_providers", "_live_semantic_providers"):
        monkeypatch.setattr(RawWorkspaceService, name, refuse)


def _project(
    tmp_path: Path,
    suitability: str = "recommended",
    utterances=DENSE,
    visual_issues: list[str] | None = None,
):
    service, root = tier_project(
        tmp_path, suitability=suitability, utterances=utterances, visual_issues=visual_issues
    )
    _settle_sync(service, root)  # 앱이 거는 확인 작업 — 무음 합성 소리라 측정 불가(회색)로 끝난다
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    source = sync_report.gather_source(workspace, asset)
    assert source is not None and source.stage1 is not None
    return service, root, asset, workspace, source


def _flagged(cues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """심을 보고서의 「말소리를 못 찾음」 큐 — 자막 줄 그대로(공통 시계의 정확 유리수 글)."""
    return [
        {
            "id": cue["utterance_id"],
            "start": sync_check.fraction_text(_fraction(cue["start"])),
            "end": sync_check.fraction_text(_fraction(cue["end"])),
            "text": cue["text"],
            "flags": [sync_check.FLAG_NO_SPEECH],
        }
        for cue in cues
    ]


def _fraction(value: Any) -> Fraction:
    return Fraction(value["num"], value["den"]) if isinstance(value, dict) else Fraction(str(value))


def _cues(service: RawWorkspaceService, root: Path, asset_id: str) -> list[dict[str, Any]]:
    transcript = service.source_transcript(root, asset_id)
    assert transcript is not None
    return list(transcript["utterances"])


def _report(service: RawWorkspaceService, root: Path, asset_id: str) -> tiering.TierReport:
    revision = service.analysis_overview(root).revision.revision_id
    return service.tier_report(root, revision, asset_id)


def _keywords(service: RawWorkspaceService, root: Path) -> list[list[str]]:
    document = ET.fromstring(service.export_finalcut_timeline(root).read_bytes())
    return [
        [str(k.get("value")) for k in clip.findall("keyword")]
        for clip in document.iterfind("./library/event/project/sequence/spine/asset-clip")
    ]


def _head_tiers(root: Path) -> list[tuple[str, list[str]]]:
    head = ReviewWorkspaceFacade(root).load_head().revision
    return [
        (s.recommendation, [t for t in s.visual_tags if t.startswith(("등급: ", "추천 제한: "))])
        for s in head.segments
    ]


def _upkeep(service: RawWorkspaceService, root: Path) -> str | None:
    """정리 고리 한 바퀴 — 할 일이 있으면 그 작업이 끝날 때까지 기다린다."""
    message = service.start_upkeep(root)
    if message is not None:
        job = next(j for j in service.list_jobs(root) if j.kind == UPKEEP_JOB_KIND)
        assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    return message


# ── 값은 지금 보고서에서만 ───────────────────────────────────────────────


@needs_ffmpeg
def test_timing_values_come_only_from_the_current_report(tmp_path: Path):
    service, root, asset, workspace, source = _project(tmp_path)
    cues = _cues(service, root, asset.asset_id)
    flags = _flagged(cues[:2])
    keys = frozenset((f["id"], Fraction(f["start"]), Fraction(f["end"])) for f in flags)

    _put_report(workspace, source, sync_check.LEVEL_RED, cues=flags)
    assert service._tier_timing(workspace, asset.asset_id) == tiering.TimingSignals(
        level=sync_check.LEVEL_RED, no_speech=keys
    )
    _put_report(workspace, source, sync_check.LEVEL_AMBER, cues=flags)
    assert service._tier_timing(workspace, asset.asset_id).no_speech == keys
    # 측정 불가는 판정이 없다 — 「말소리를 못 찾음」 큐도 등급에는 넘기지 않는다.
    _put_report(workspace, source, sync_check.LEVEL_SKIP, cues=flags)
    assert service._tier_timing(workspace, asset.asset_id) == tiering.TimingSignals(
        level=sync_check.LEVEL_SKIP
    )
    # 낡음(1단계 PCM이 바뀐 뒤) · 다른 자막 리비전 · 없음 — 모두 모름이다.
    _put_report(workspace, source, sync_check.LEVEL_RED, cues=flags, stage1_pcm_sha256="0" * 64)
    assert service._tier_timing(workspace, asset.asset_id) == tiering.UNKNOWN_TIMING
    old = replace(source, transcript_revision="이전-리비전")
    _put_report(workspace, old, sync_check.LEVEL_RED, cues=flags)
    assert service._tier_timing(workspace, asset.asset_id) == tiering.UNKNOWN_TIMING
    shutil.rmtree(workspace.root / sync_report.SYNC_CACHE / "reports")
    assert service._tier_timing(workspace, asset.asset_id) == tiering.UNKNOWN_TIMING


@needs_ffmpeg
def test_corrupt_current_report_counts_as_missing_and_is_measured_again(tmp_path: Path):
    """깨진 보고서 파일은 없는 것과 같다 — 모름이라 상한이 없고, 확인 작업이 다시 잴 원본에 든다."""
    service, root, asset, workspace, source = _project(tmp_path)
    _put_report(workspace, source, sync_check.LEVEL_RED)
    cache_root = workspace.root / sync_report.SYNC_CACHE
    path = sync_report.report_path(
        cache_root, asset.asset_id, source.transcript_revision, SYNC_CHECK_SETTINGS
    )
    assert service._tier_timing(workspace, asset.asset_id).level == sync_check.LEVEL_RED
    path.write_text("{깨진 JSON", encoding="utf-8")
    assert service._tier_timing(workspace, asset.asset_id) == tiering.UNKNOWN_TIMING
    assert [s.tier for s in _report(service, root, asset.asset_id).spans] == ["green"]
    assert asset.asset_id in sync_report.pending_sources(workspace)


@needs_ffmpeg
def test_unreadable_report_is_unknown_and_warned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, root, asset, workspace, source = _project(tmp_path)
    _put_report(workspace, source, sync_check.LEVEL_RED)

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("보고서 JSON이 깨졌다")

    monkeypatch.setattr(sync_report, "current_report", broken)
    warnings: list[str] = []
    monkeypatch.setattr(raw_service.log, "warn", warnings.append)
    assert service._tier_timing(workspace, asset.asset_id) == tiering.UNKNOWN_TIMING
    assert [line for line in warnings if "보고서 JSON이 깨졌다" in line]
    assert _report(service, root, asset.asset_id).spans[0].tier == tiering.TIER_GREEN


# ── 빨강이면 보통까지 — 모든 소비자가 같은 등급, 다시 재면 풀린다 ─────────────


@needs_ffmpeg
def test_timing_red_caps_every_consumer_and_a_remeasure_lifts_it_through_the_head(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root, asset, workspace, source = _project(tmp_path)
    _forbid_providers(monkeypatch)
    assert _head_tiers(root) == [("recommended", ["등급: 추천"])]
    assert _keywords(service, root) == [["추천"]]
    revision = service.analysis_overview(root).revision.revision_id

    _put_report(workspace, source, sync_check.LEVEL_RED)
    report = _report(service, root, asset.asset_id)
    assert [(s.tier, s.rule, s.caps) for s in report.spans] == [
        ("yellow", "both-good", (tiering._TIMING_RED_CAP,))
    ]
    rows = service.selection_suitability(root, revision, (asset.asset_id,))[asset.asset_id]
    assert [(r["tier"], r["recommendation"]) for r in rows] == [("yellow", "normal")]
    assert rows[0]["caps"] == [{"code": "timing_red", "text": CAP.removeprefix("추천 제한: ")}]
    assert _keywords(service, root) == [["보통", "자막 시각 확인 필요"]]
    # 검토 머리는 정리 고리가 등급만 다시 단다 — 안내는 「등급 표시만」.
    message = _upkeep(service, root)
    assert message is not None and "자막 시각 확인 결과나 분석 근거가 바뀌어" in message
    assert "등급 표시만 다시 답니다" in message and "무료" not in message
    assert _head_tiers(root) == [("normal", ["등급: 보통", CAP])]
    assert _upkeep(service, root) is None  # 한 번이면 끝이다

    # 시각을 맞춘 뒤 다시 쟀다 — 초록이면 상한이 풀리고 머리·키워드가 따라온다.
    _put_report(workspace, source, sync_check.LEVEL_GREEN)
    assert _keywords(service, root) == [["추천"]]
    assert _upkeep(service, root) is not None
    assert _head_tiers(root) == [("recommended", ["등급: 추천"])]
    assert _upkeep(service, root) is None


@needs_ffmpeg
def test_regrade_keeps_human_decisions_and_override(tmp_path: Path):
    service, root, asset, workspace, source = _project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    (segment,) = head.segments
    edited = facade.set_notes(head.base_plan_id, segment.segment_id, "사람 메모")
    edited = facade.set_override(edited.base_plan_id, segment.segment_id, "review_required")
    edited = facade.set_selection(edited.base_plan_id, segment.segment_id, "excluded")
    _put_report(workspace, source, sync_check.LEVEL_RED)
    assert _upkeep(service, root) is not None
    after = facade.load_head().revision
    (kept,) = after.segments
    assert (kept.selection, kept.notes, kept.override_recommendation) == (
        "excluded",
        "사람 메모",
        "review_required",
    )
    assert kept.recommendation == segment.recommendation  # 덮은 권고는 그대로
    assert (kept.start_pts, kept.end_pts) == (segment.start_pts, segment.end_pts)
    assert "등급: 보통" in kept.visual_tags and CAP in kept.visual_tags


@needs_ffmpeg
def test_tag_order_alone_is_not_a_stale_grade(tmp_path: Path):
    """입양의 되풀이가 다른 태그를 등급 태그 뒤에 붙여도 낡은 등급이 아니다 — 정리가 돌지 않는다."""
    service, root, *_ = _project(tmp_path)
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    (segment,) = head.segments
    moved = replace(segment, visual_tags=(*segment.visual_tags, "실내"))
    head = facade._publish_snapshot(head, (moved,), "합성 — 등급 태그 뒤의 다른 태그")
    assert facade.stale_grade_segments(head) == ()
    assert service.start_upkeep(root) is None
    # 등급 태그 하나라도 다르면 낡은 것이다.
    edited = tuple(t for t in moved.visual_tags if not t.startswith("등급: ")) + ("등급: 보통",)
    head = facade._publish_snapshot(head, (replace(moved, visual_tags=edited),), "합성 — 등급")
    assert [row[0] for row in facade.stale_grade_segments(head)] == [segment.segment_id]


@needs_ffmpeg
def test_unreadable_evidence_never_breaks_the_upkeep_check_and_is_reported(tmp_path: Path):
    """다시 매길지 재다가 증거를 못 읽어도 할 일 계산(화면 갱신마다)은 터지지 않는다 — 정리 작업이
    실패로 남기고 다음 갱신이 「작업공간 정리 실패」로 알린다(조용히 넘기지 않는다)."""
    service, root, *_ = _project(tmp_path)
    (row,) = ReviewWorkspaceFacade(root).tier_evidence_rows().values()
    (root / row.document_path).write_text("{깨진 JSON", encoding="utf-8")
    message = service.start_upkeep(root)
    assert message is not None and "등급 표시만 다시 답니다" in message
    job = next(j for j in service.list_jobs(root) if j.kind == UPKEEP_JOB_KIND)
    assert _wait_terminal(service, root, job.job_id).status == "failed"
    note = service.start_upkeep(root) or ""
    assert note.startswith("작업공간 정리 실패:")


# ── 말소리 측정상 없음 — 같은 줄(아이디·시각)만 ───────────────────────────


@needs_ffmpeg
def test_no_speech_flags_lower_density_only_for_the_same_cues(tmp_path: Path):
    # `usable` 화면 — v6(D79)은 `recommended` × 발화 적음을 이미 녹색으로 보아 내림이 안 보인다.
    service, root, asset, workspace, source = _project(tmp_path, "usable")
    cues = _cues(service, root, asset.asset_id)
    # 다섯 줄 중 넷을 짚었다 — 확인된 말이 2.5초뿐이라 밀도가 0.35 밑이다.
    _put_report(workspace, source, sync_check.LEVEL_AMBER, cues=_flagged(cues[:4]))
    (span,) = _report(service, root, asset.asset_id).spans
    assert (span.tier, span.rule, span.audio.status) == ("yellow", "speech-sparse", "speech_sparse")
    assert "말소리 측정상 없는 줄 4개를 밀도에서 뺐습니다(측정 한계일 수 있음)" in span.audio.reason
    assert _keywords(service, root) == [["보통"]]
    # 다른 리비전의 줄(시각이 0.1초 다르다) — 같은 줄이 아니므로 모름이다.
    shifted = _flagged(cues[:4])
    for row in shifted:
        row["end"] = sync_check.fraction_text(Fraction(row["end"]) + Fraction(1, 10))
    _put_report(workspace, source, sync_check.LEVEL_AMBER, cues=shifted)
    (span,) = _report(service, root, asset.asset_id).spans
    assert (span.tier, span.audio.excluded) == ("green", ())
    assert _keywords(service, root) == [["추천"]]


# ── 빨강·자동 제외 후보·컷 자리는 신호와 무관하다 ─────────────────────────


@needs_ffmpeg
def test_red_and_auto_cleanup_are_identical_with_and_without_timing_signals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root, asset, workspace, source = _usable_red(tmp_path)
    cues = _cues(service, root, asset.asset_id)
    facade = ReviewWorkspaceFacade(root)
    rows = facade.tier_evidence_rows()
    row = rows[asset.asset_id]
    record = json.loads((root / row.record_path).read_text(encoding="utf-8"))

    def outputs() -> dict[str, Any]:
        report = service.tier_report(root, row.revision_id, asset.asset_id)
        candidates = facade._removal_candidates(row, record, {})
        plan = service.plan_segmentation_from_analysis(root)
        built = facade.initialize_review(plan.revision_id)
        return {
            "red_decisions": json.dumps(tiering.red_decisions(report), sort_keys=True),
            "red_ranges": tiering.red_ranges(report),
            "candidates": json.dumps(candidates, sort_keys=True, default=str),
            "cuts": sorted(service._removal_cuts(workspace, row, record)),
            "unreviewed": [_seconds(built, s) for s in unreviewed_auto_exclusions(built)],
            "selection": [(s.start_pts, s.end_pts, s.selection) for s in built.segments],
        }

    _put_report(workspace, source, sync_check.LEVEL_RED, cues=_flagged(cues))
    signalled = outputs()
    assert signalled["unreviewed"] == [RED]
    monkeypatch.setattr(
        RawWorkspaceService, "_tier_timing", lambda self, ws, asset_id: tiering.UNKNOWN_TIMING
    )
    assert outputs() == signalled


def _usable_red(tmp_path: Path):
    """빨강(자막 없는 3.3–10.7초 × 사용 가능 화면)이 있는 기본 합성 원본."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    service, root = tier_project(tmp_path, suitability="usable")
    _settle_sync(service, root)
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    source = sync_report.gather_source(workspace, asset)
    assert source is not None
    return service, root, asset, workspace, source


# ── v3·v4 옮기기 — 사람 결정은 그대로, 두 번째는 아무것도 하지 않는다 ─────────


OLD_VERSIONS = ("raw-tier/v2", "raw-tier/v3")
ISSUE_CAP = "추천 제한: 화면 문제(어두움)가 있어 보통까지만 추천합니다"


def _as_old(tags: tuple[str, ...], version: str) -> tuple[str, ...]:
    """옛 판 머리의 태그 모양 — 표지가 그 판이고, v2에는 「추천 제한」 태그가 없다(v3부터)."""
    marker = tiering.MARKER_PREFIX + version
    return tuple(
        marker if tag.startswith(tiering.MARKER_PREFIX) else tag
        for tag in tags
        if version != "raw-tier/v2" or not tag.startswith(tiering.CAP_TAG_PREFIX)
    )


def _old_head(
    service: RawWorkspaceService, root: Path, monkeypatch: pytest.MonkeyPatch, version: str
):
    """옛 판이 세운 머리 — 등급판 표지와 빨강 제안 근거의 판이 그 판이고, 화면 문제(v4 규칙 5)를
    모른다(v2·v3에는 그 상한이 없었다)."""
    facade = ReviewWorkspaceFacade(root)
    real_grade, real_red = tiering.segment_grade, tiering.red_decisions
    with monkeypatch.context() as patch:
        patch.setattr(tiering, "_span_issue_labels", lambda evidence, start, end: ())
        patch.setattr(
            tiering,
            "segment_grade",
            lambda report, a, b: (
                real_grade(report, a, b)[0],
                _as_old(real_grade(report, a, b)[1], version),
            ),
        )
        patch.setattr(
            tiering,
            "red_decisions",
            lambda report: [{**row, "version": version} for row in real_red(report)],
        )
        plan = service.plan_segmentation_from_analysis(root)
        legacy = facade.initialize_review(plan.revision_id)
    assert not ReviewWorkspaceFacade.has_tier_marker(legacy)
    return facade, legacy


@needs_ffmpeg
@pytest.mark.parametrize("version", OLD_VERSIONS)
def test_old_head_migration_keeps_confirmed_exclusions_notes_and_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, version: str
):
    service, root, *_ = _usable_red(tmp_path)
    facade, legacy = _old_head(service, root, monkeypatch, version)
    (proposal,) = unreviewed_auto_exclusions(legacy)
    confirmed = facade.confirm_automatic_exclusions()
    other = next(s for s in confirmed.segments if s.segment_id != proposal.segment_id)
    touched = facade.set_notes(confirmed.base_plan_id, other.segment_id, "사람 메모")
    _forbid_providers(monkeypatch)
    message = _upkeep(service, root)
    assert message is not None and "등급 표시만 다시 답니다" in message
    migrated = facade.load_head().revision
    assert migrated.base_plan_id == touched.base_plan_id
    assert [
        (s.segment_id, s.start_pts, s.end_pts, s.selection, s.notes) for s in migrated.segments
    ] == [(s.segment_id, s.start_pts, s.end_pts, s.selection, s.notes) for s in touched.segments]
    excluded = next(s for s in migrated.segments if s.segment_id == proposal.segment_id)
    assert (
        excluded.selection == "excluded"
        and AUTO_EXCLUDED_UNREVIEWED_TAG not in excluded.visual_tags
    )
    assert all(tiering.tier_marker() in s.visual_tags for s in migrated.segments)
    assert all(version not in t for s in migrated.segments for t in s.visual_tags)
    # 두 번째 바퀴는 할 일이 없다 — 작업도 리비전도 없다.
    jobs = len(service.list_jobs(root))
    assert service.start_upkeep(root) is None
    assert len(service.list_jobs(root)) == jobs
    assert facade.retier(migrated.base_plan_id) == migrated


@needs_ffmpeg
def test_v4_migration_caps_recommended_under_a_screen_problem_and_keeps_human_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """v3 머리의 추천 조각이 화면 문제(어두움) 창 위라면 v4 옮기기가 보통 + 「추천 제한」으로
    다시 단다 — 메모·덮기·경계·선택은 그대로, 제공자 조립 0, 두 번째 바퀴는 할 일이 없다."""
    service, root, asset, *_ = _project(tmp_path, visual_issues=["dark"])
    revision = service.analysis_overview(root).revision.revision_id
    rows = service.selection_suitability(root, revision, (asset.asset_id,))[asset.asset_id]
    assert [(r["tier"], r["rule"], r["recommendation"]) for r in rows] == [
        ("yellow", "both-good", "normal")
    ]
    assert rows[0]["caps"] == [
        {"code": tiering.CAP_VISUAL_ISSUES, "text": ISSUE_CAP.removeprefix("추천 제한: ")}
    ]
    assert "화면 문제: 어두움" in rows[0]["video"]["reason"] and rows[0]["red_reason"] is None
    assert _keywords(service, root) == [["보통"]]  # 묻힌 추천도 없다 — 녹색이 하나도 없다
    facade, legacy = _old_head(service, root, monkeypatch, "raw-tier/v3")
    (segment,) = legacy.segments
    assert (segment.recommendation, segment.visual_tags[0]) == ("recommended", "등급: 추천")
    touched = facade.set_notes(legacy.base_plan_id, segment.segment_id, "사람 메모")
    touched = facade.set_override(touched.base_plan_id, segment.segment_id, "review_required")
    _forbid_providers(monkeypatch)
    message = _upkeep(service, root)
    assert message is not None and "등급 표시만 다시 답니다" in message
    (after,) = facade.load_head().revision.segments
    assert (after.segment_id, after.start_pts, after.end_pts, after.selection) == (
        segment.segment_id,
        segment.start_pts,
        segment.end_pts,
        segment.selection,
    )
    assert (after.notes, after.override_recommendation) == ("사람 메모", "review_required")
    assert after.recommendation == segment.recommendation  # 사람이 덮은 조각은 권고도 그대로
    assert "등급: 보통" in after.visual_tags and ISSUE_CAP in after.visual_tags
    assert tiering.tier_marker() in after.visual_tags
    assert service.start_upkeep(root) is None
