"""자막이 확인된 영상의 `usable` 구간은 추천(W-25 a, D65) — 등급판 `raw-tier/v5`.

「자막 확인됨」은 영상 하나의 값이다: **지금** 자막 리비전의 자막 시각 확인(D55) 보고서가 초록이고
(`_tier_timing`과 같은 엄격함 — 자막 리비전·1단계 PCM·기준이 같다), 그 리비전 원본 자막 줄
가운데 「확인 필요」(D59 19e) 표시가 붙은 줄의 몫이 `subtitle_verified_suspect_share`(0.10) 이하다.
그 값이 참이면 표의 칸 하나만 바뀐다 — 영상 `usable` × 자막 `speech_dense`가 노랑(`video-usable`)
대신 녹색(`subtitle-verified`)이다. 빨강·`flagged`·`speech_sparse`와 다른 칸은 그대로이고, 추천 제한
(3초 · 시각 빨강 · 화면 문제)과 검토 조각의 3초 규칙은 그 뒤에 그대로 걸린다.

보는 것:

- 순수 계층 — 칸 하나만 바뀌고, 경계·빨강·자동 제외 후보는 무작위 문서 300개에서 바이트까지 같다.
- 서비스 — 값은 `_tier_timing` 한 자리에서만, 지금 초록 보고서 + 의심 줄 몫에서만 온다.
- v4 머리 옮기기 — 사람 결정(선택·메모·덮기·경계)은 그대로, 등급 표시만 다시 단다.
- 불변식 — 「자막 확인됨」 칸을 `_tier_timing` 밖에서 채우면 잡는다.
- 작업대 — 렠 막대 제목과 구간 패널에 「추천 근거」가 보인다.

유료 호출 0회.
"""

from __future__ import annotations

import json
import random
import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_caption_timing import _put_report
from tests.raw.test_tier_timing_service import (
    _flagged,
    _forbid_providers,
    _keywords,
    _project,
    _report,
    _upkeep,
)
from tests.raw.test_tiering_v3 import _random_document, chatter, cue_key, make_document
from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, needs_node
from tests.test_invariants import CLEAN_TIERING, TIER_SERVICE, _fake_source_tree, check_invariants
from ui import ui_facade
from vlogsrt.project import sync_check
from vlogsrt.project.raw import sync_report, tiering
from vlogsrt.project.raw.analysis_config import (
    TIER_SETTINGS,
    RawTierSettings,
    validate_tier_settings,
)
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.service import RawWorkspaceService

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

F = Fraction
G, Y, R = tiering.TIER_GREEN, tiering.TIER_YELLOW, tiering.TIER_RED
RULE = "subtitle-verified"


def verified(suspects: int = 1, lines: int = 12) -> tiering.TimingSignals:
    """서비스가 초록 보고서 + 적은 의심 줄에서 만드는 값의 모양(순수 시험용)."""
    return tiering.TimingSignals(
        level=sync_check.LEVEL_GREEN,
        subtitle_verified=True,
        subtitle_suspect_lines=suspects,
        subtitle_lines=lines,
    )


def lift_text(suspects: int, lines: int) -> str:
    return f"자막 확인됨(시각 확인 초록 · 확인 필요 {suspects}/{lines}줄)"


def usable_talk(extent: int = 30) -> dict:
    """말이 끊이지 않는 `usable` 화면 — v4에서는 노랑(`video-usable`) 한 구간이다."""
    return make_document(extent, [(0, extent, "usable")], chatter(0, extent))


# ── 설정 ─────────────────────────────────────────────────────────────────


def test_v5_settings_are_the_published_values():
    validate_tier_settings(TIER_SETTINGS)
    assert TIER_SETTINGS.tier_version == "raw-tier/v5"
    assert TIER_SETTINGS.subtitle_verified_suspect_share == 0.10
    assert tiering.TIMING_GREEN == sync_check.LEVEL_GREEN


@pytest.mark.parametrize("share", [-0.01, 1.01, float("nan"), True, "0.1"])
def test_suspect_share_outside_zero_to_one_is_rejected(share: Any):
    with pytest.raises(ValueError):
        validate_tier_settings(RawTierSettings(subtitle_verified_suspect_share=share))


@pytest.mark.parametrize(
    ("level", "suspects", "lines", "expected"),
    [
        (sync_check.LEVEL_GREEN, 0, 9, True),
        (sync_check.LEVEL_GREEN, 1, 10, True),  # 정확히 10%는 통과(이하)
        (sync_check.LEVEL_GREEN, 11, 100, False),  # 11%
        (sync_check.LEVEL_GREEN, 1, 9, False),  # 11.1%
        (sync_check.LEVEL_GREEN, 0, 0, False),  # 줄이 없으면 확인할 자막도 없다
        (sync_check.LEVEL_AMBER, 0, 9, False),
        (sync_check.LEVEL_RED, 0, 9, False),
        (sync_check.LEVEL_SKIP, 0, 9, False),
        (None, 0, 9, False),
    ],
)
def test_verified_subtitles_needs_a_green_verdict_and_few_suspect_lines(
    level: str | None, suspects: int, lines: int, expected: bool
):
    assert tiering.verified_subtitles(level, suspects, lines) is expected


def test_default_timing_is_not_verified():
    assert tiering.UNKNOWN_TIMING.subtitle_verified is False
    unknown = tiering.UNKNOWN_TIMING
    assert (unknown.subtitle_suspect_lines, unknown.subtitle_lines) == (0, 0)


# ── 칸 하나 — usable × speech_dense ─────────────────────────────────────


def test_verified_usable_speech_is_green_with_the_new_rule_and_a_reason():
    doc = usable_talk()
    (plain,) = tiering.classify(doc).spans
    assert (plain.tier, plain.rule, plain.lift) == (Y, "video-usable", None)
    report = tiering.classify(doc, timing=verified(1, 12))
    (span,) = report.spans
    assert (span.tier, span.rule, span.caps) == (G, RULE, ())
    assert (span.audio.status, span.video.status) == (
        tiering.AUDIO_SPEECH_DENSE,
        tiering.VIDEO_USABLE,
    )
    assert span.recommendation == "recommended" and span.red_reason is None
    assert span.lift == lift_text(1, 12)
    assert span.document()["lift"] == lift_text(1, 12)
    assert span.document()["reason"].endswith(" · 추천 근거: " + lift_text(1, 12))
    recommendation, tags = tiering.segment_grade(report, F(0), F(30))
    assert recommendation == "recommended"
    assert tags[0] == "등급: 추천" and tags[-1] == "등급판 raw-tier/v5"
    assert "추천 근거: " + lift_text(1, 12) in tags
    assert tiering.segment_keyword(report, F(0), F(30)) == "추천"
    # 등급 태그 묶음이라 다시 매기기 전에 걷힌다.
    assert tiering.strip_grade_tags(tags) == ()


def test_combine_changes_only_the_usable_dense_cell():
    """표의 모든 칸 — 「자막 확인됨」이 바꾸는 칸은 usable × speech_dense 하나뿐이다."""
    for audio_status in tiering.AUDIO_STATUSES:
        for video_status in tiering.VIDEO_STATUSES:
            audio = tiering.AudioJudgment(audio_status, F(1), "a", ())
            video = tiering.VideoJudgment(video_status, "v", (), ())
            before = tiering.combine(audio, video)
            after = tiering.combine(audio, video, subtitle_verified=True)
            if (audio_status, video_status) == (tiering.AUDIO_SPEECH_DENSE, tiering.VIDEO_USABLE):
                assert before == (Y, "video-usable", None)
                assert after == (G, RULE, None)
            else:
                assert after == before


@pytest.mark.parametrize(
    ("windows", "rule"),
    [
        ([(0, 30, "review")], "video-flagged"),  # Luna가 확인 권고로 본 화면
        ([(0, 30, "recommended")], "both-good"),  # 이미 녹색인 칸은 규칙도 그대로
    ],
)
def test_other_video_cells_keep_their_rule(windows: list, rule: str):
    doc = make_document(30, windows, chatter(0, 30))
    plain = tiering.classify(doc)
    lifted = tiering.classify(doc, timing=verified())
    assert [(s.tier, s.rule) for s in lifted.spans] == [(s.tier, s.rule) for s in plain.spans]
    assert [s.rule for s in lifted.spans] == [rule]
    assert all(s.lift is None for s in lifted.spans)


def test_sparse_speech_on_a_usable_screen_stays_yellow():
    """1초 말 · 3초 쉼(긴 공백이 아니다) — 밀도 27%라 표의 칸은 발화 적음 × usable이다."""
    sparse = [(F(4 * i), F(4 * i + 1), f"이야기 {i}번째 줄이에요") for i in range(8)]
    doc = make_document(30, [(0, 30, "usable")], sparse)
    (span,) = tiering.classify(doc, timing=verified()).spans
    assert (span.tier, span.rule, span.audio.status) == (
        Y,
        "speech-sparse",
        tiering.AUDIO_SPEECH_SPARSE,
    )


def test_a_lifted_span_shorter_than_three_seconds_is_capped():
    """확인 권고 화면 사이의 2초 `usable` 섬 — 표는 녹색이지만 3초보다 짧아 보통까지(D60)."""
    doc = make_document(
        30, [(0, 10, "review"), (10, 12, "usable"), (12, 30, "review")], chatter(0, 30)
    )
    report = tiering.classify(doc, timing=verified())
    island = next(s for s in report.spans if s.start == 10)
    assert (island.end, island.tier, island.rule) == (F(12), Y, RULE)
    assert island.caps == ((tiering.CAP_TOO_SHORT, "3초보다 짧아 추천하지 않습니다"),)
    assert island.lift is None and "추천 근거" not in island.document()["reason"]
    _, tags = tiering.segment_grade(report, F(10), F(12))
    assert "등급: 보통" in tags and not any(t.startswith("추천 근거: ") for t in tags)


def test_a_short_review_piece_on_a_lifted_span_is_capped():
    report = tiering.classify(usable_talk(), timing=verified())
    recommendation, tags = tiering.segment_grade(report, F(5), F(7))
    assert recommendation == "normal"
    assert "추천 제한: 3초보다 짧아 추천하지 않습니다" in tags
    assert not any(t.startswith("추천 근거: ") for t in tags)


def test_a_visual_issue_still_caps_a_lifted_span():
    doc = usable_talk()
    doc["annotations"][0]["visual_issues"] = ["blur"]
    (span,) = tiering.classify(doc, timing=verified()).spans
    assert (span.tier, span.rule) == (Y, RULE)
    assert [code for code, _ in span.caps] == [tiering.CAP_VISUAL_ISSUES]


def test_no_speech_cues_can_still_drop_a_lifted_span_below_the_density_line():
    doc = usable_talk(10)
    keys = frozenset(cue_key(doc, i) for i in range(3))
    timing = replace(verified(), no_speech=keys)
    (span,) = tiering.classify(doc, timing=timing).spans
    assert (span.tier, span.rule, span.audio.status) == (
        Y,
        "speech-sparse",
        tiering.AUDIO_SPEECH_SPARSE,
    )


def test_red_cells_are_unchanged_by_verified_subtitles():
    """자막 없는 긴 공백 × usable 화면은 빨강 그대로 — 제외 제안·자동 제외 후보가 바이트째 같다."""
    talk = chatter(0, 8) + chatter(20, 30)
    doc = make_document(30, [(0, 30, "usable")], talk)
    plain = tiering.classify(doc)
    lifted = tiering.classify(doc, timing=verified())
    assert tiering.red_ranges(plain) and tiering.red_ranges(lifted) == tiering.red_ranges(plain)
    assert json.dumps(tiering.red_decisions(lifted)) == json.dumps(tiering.red_decisions(plain))
    assert [(s.start, s.end) for s in lifted.spans] == [(s.start, s.end) for s in plain.spans]
    assert [s.tier for s in lifted.spans] == [G, R, G]


def test_verified_subtitles_never_move_a_boundary_or_touch_red_on_random_documents():
    """무작위 문서 300개 — 경계·빨강·제외 후보가 같고, 바뀌는 칸은 usable × 발화 많음뿐이다."""
    rng = random.Random(65)
    for _ in range(300):
        doc = _random_document(rng)
        plain = tiering.classify(doc)
        lifted = tiering.classify(doc, timing=verified())
        assert [(s.start, s.end) for s in lifted.spans] == [(s.start, s.end) for s in plain.spans]
        for before, after in zip(plain.spans, lifted.spans, strict=True):
            assert (after.red_reason, after.video, after.audio) == (
                before.red_reason,
                before.video,
                before.audio,
            )
            if (after.tier, after.rule) != (before.tier, before.rule):
                assert (before.rule, after.rule) == ("video-usable", RULE)
                assert (after.audio.status, after.video.status) == (
                    tiering.AUDIO_SPEECH_DENSE,
                    tiering.VIDEO_USABLE,
                )
                assert after.tier == G or after.caps
            assert (before.tier == R) == (after.tier == R)
        assert json.dumps(tiering.red_decisions(lifted)) == json.dumps(tiering.red_decisions(plain))


# ── 서비스 — 값은 지금 초록 보고서와 의심 줄 몫에서만 ─────────────────────


NINE: tuple[dict[str, object], ...] = tuple(
    {"start": 0.5 + 1.5 * i, "end": 2 + 1.5 * i, "text": text}
    for i, text in enumerate(
        (
            "안녕하세요 오늘은 계곡에 왔어요",
            "물이 정말 맑고 시원해 보여요",
            "저기 바위 위에 앉아 볼게요",
            "발을 담그니까 차갑네요",
            "물고기가 지나가요 보이세요",
            "햇빛이 나뭇잎 사이로 들어와요",
            "바람이 불어서 시원하네요",
            "조금 더 위쪽으로 올라가 볼게요",
            "이제 천천히 들어가 볼게요",
        )
    )
)
"""`usable` 화면 위 아홉 줄(0.5–14초, 서로 다른 말) — 한 줄이 짚히면 확인 필요 몫이 11.1%다."""


def _tier_rows(service: RawWorkspaceService, root: Path, asset_id: str) -> list[tuple]:
    return [(s.tier, s.rule, s.lift) for s in _report(service, root, asset_id).spans]


def _head_marks(root: Path) -> list[tuple[str, list[str]]]:
    head = ReviewWorkspaceFacade(root).load_head().revision
    prefixes = ("등급: ", "추천 제한: ", "추천 근거: ", "등급판 ")
    return [
        (s.recommendation, [t for t in s.visual_tags if t.startswith(prefixes)])
        for s in head.segments
    ]


@needs_ffmpeg
def test_current_green_report_with_few_suspects_recommends_usable_speech_everywhere(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root, asset, workspace, source = _project(tmp_path, "usable", NINE)
    _forbid_providers(monkeypatch)
    assert _tier_rows(service, root, asset.asset_id) == [(Y, "video-usable", None)]
    assert _keywords(service, root) == [["보통"]]

    _put_report(workspace, source, sync_check.LEVEL_GREEN)
    assert service._tier_timing(workspace, asset.asset_id) == tiering.TimingSignals(
        level=sync_check.LEVEL_GREEN,
        subtitle_verified=True,
        subtitle_suspect_lines=0,
        subtitle_lines=9,
    )
    assert _tier_rows(service, root, asset.asset_id) == [(G, RULE, lift_text(0, 9))]
    revision = service.analysis_overview(root).revision.revision_id
    (row,) = service.selection_suitability(root, revision, (asset.asset_id,))[asset.asset_id]
    assert (row["tier"], row["rule"], row["recommendation"], row["lift"]) == (
        G,
        RULE,
        "recommended",
        lift_text(0, 9),
    )
    assert _keywords(service, root) == [["추천"]]
    assert _upkeep(service, root) is not None  # 정리 고리가 검토 머리의 등급만 다시 단다
    assert _head_marks(root) == [
        ("recommended", ["등급: 추천", "추천 근거: " + lift_text(0, 9), "등급판 raw-tier/v5"])
    ]
    assert _upkeep(service, root) is None


@needs_ffmpeg
def test_only_a_current_green_report_with_few_suspects_verifies(tmp_path: Path):
    """노랑·빨강·측정 불가·낡음·다른 리비전·없음, 그리고 초록이어도 확인 필요 11% — 모두 보통."""
    service, root, asset, workspace, source = _project(tmp_path, "usable", NINE)
    cues = list(service.source_transcript(root, asset.asset_id)["utterances"])

    def check(expected: tuple, **put: Any) -> None:
        verdict = put.pop("verdict", sync_check.LEVEL_GREEN)
        target = put.pop("source", source)
        _put_report(workspace, target, verdict, **put)
        timing = service._tier_timing(workspace, asset.asset_id)
        assert (timing.subtitle_verified, timing.subtitle_suspect_lines) == (False, 0), put
        assert _tier_rows(service, root, asset.asset_id) == [expected], (verdict, put)

    usable = (Y, "video-usable", None)
    check(usable, verdict=sync_check.LEVEL_AMBER)
    check(usable, verdict=sync_check.LEVEL_SKIP)
    check(usable, stage1_pcm_sha256="0" * 64)  # 낡음 — 1단계 PCM이 바뀐 뒤의 초록
    check(usable, source=replace(source, transcript_revision="이전-리비전"))
    check(usable, cues=_flagged(cues[:1]))  # 초록이지만 아홉 줄 중 하나 = 11.1%
    # 빨강은 표의 칸이 노랑이라 추천 제한도 붙지 않는다.
    check(usable, verdict=sync_check.LEVEL_RED)
    shutil.rmtree(workspace.root / sync_report.SYNC_CACHE / "reports")
    assert service._tier_timing(workspace, asset.asset_id) == tiering.UNKNOWN_TIMING
    assert _tier_rows(service, root, asset.asset_id) == [usable]


# ── v4 머리 옮기기 ───────────────────────────────────────────────────────


@needs_ffmpeg
def test_v4_head_migration_lifts_marks_and_keeps_human_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root, asset, workspace, source = _project(tmp_path, "usable", NINE)
    _put_report(workspace, source, sync_check.LEVEL_GREEN)
    facade = ReviewWorkspaceFacade(root)
    real_grade = tiering.segment_grade
    v4 = tiering.MARKER_PREFIX + "raw-tier/v4"
    with monkeypatch.context() as patch:  # v4가 세운 머리 — 「자막 확인됨」을 모르고 표지가 v4다
        patch.setattr(
            RawWorkspaceService,
            "_tier_timing",
            lambda self, ws, asset_id: tiering.TimingSignals(level=sync_check.LEVEL_GREEN),
        )
        patch.setattr(
            tiering,
            "segment_grade",
            lambda report, a, b: (
                real_grade(report, a, b)[0],
                tuple(
                    v4 if t.startswith(tiering.MARKER_PREFIX) else t
                    for t in real_grade(report, a, b)[1]
                ),
            ),
        )
        plan = service.plan_segmentation_from_analysis(root)
        legacy = facade.initialize_review(plan.revision_id)
    (segment,) = legacy.segments
    assert (segment.recommendation, segment.visual_tags[0]) == ("normal", "등급: 보통")
    assert v4 in segment.visual_tags and not ReviewWorkspaceFacade.has_tier_marker(legacy)
    touched = facade.set_notes(legacy.base_plan_id, segment.segment_id, "사람 메모")
    touched = facade.set_selection(touched.base_plan_id, segment.segment_id, "excluded")
    touched = facade.set_override(touched.base_plan_id, segment.segment_id, "review_required")
    _forbid_providers(monkeypatch)
    message = _upkeep(service, root)
    assert message is not None and "등급 표시만 다시 답니다" in message
    (after,) = facade.load_head().revision.segments
    assert (after.segment_id, after.start_pts, after.end_pts) == (
        segment.segment_id,
        segment.start_pts,
        segment.end_pts,
    )
    assert (after.selection, after.notes, after.override_recommendation) == (
        "excluded",
        "사람 메모",
        "review_required",
    )
    assert after.recommendation == "normal"  # 사람이 덮은 조각은 권고도 그대로
    assert "등급: 추천" in after.visual_tags and "등급: 보통" not in after.visual_tags
    assert "추천 근거: " + lift_text(0, 9) in after.visual_tags
    assert tiering.tier_marker() in after.visual_tags and v4 not in after.visual_tags
    assert service.start_upkeep(root) is None


# ── 불변식 — 「자막 확인됨」은 `_tier_timing`만 채운다 ─────────────────────

CLEAN_SERVICE = (
    "from vlogsrt.project.raw import tiering\n"
    "\n"
    "\n"
    "class RawWorkspaceService:\n"
    "    def _tier_report_for_row(self, workspace, row):\n"
    "        return tiering.classify({}, timing=self._tier_timing(workspace, row))\n"
    "\n"
    "    def _tier_timing(self, workspace, asset_id):\n"
    "        if asset_id:\n"
    "            return tiering.TimingSignals(level='green', subtitle_verified=True,\n"
    "                                         subtitle_suspect_lines=0, subtitle_lines=9)\n"
    "        return tiering.UNKNOWN_TIMING\n"
)


@pytest.mark.parametrize(
    ("rel", "body", "line", "fragment"),
    [
        # 다른 모듈이 받은 값을 고쳐 추천을 올린다.
        (
            "vlogsrt/project/raw/review_workspace.py",
            "import dataclasses\n\n\ndef lift(timing):\n"
            "    return dataclasses.replace(timing, subtitle_verified=True)\n",
            5,
            "subtitle_verified를 lift에서 채운다",
        ),
        # 서비스의 다른 메서드가 몫을 바꾼다.
        (
            TIER_SERVICE,
            CLEAN_SERVICE + "\n    def _lifted(self, timing):\n"
            "        from dataclasses import replace\n"
            "        return replace(timing, subtitle_lines=100)\n",
            16,
            "subtitle_lines를 RawWorkspaceService._lifted에서 채운다",
        ),
        # 글로 가리킨 칸 — setattr·`**{…}`.
        (
            "ui/workspace_service.py",
            "def lift(timing):\n    object.__setattr__(timing, 'subtitle_verified', True)\n",
            2,
            "subtitle_verified를 lift에서 채운다",
        ),
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "import dataclasses\n\n\ndef lift(timing):\n"
            "    return dataclasses.replace(timing, **{'subtitle_suspect_lines': 0})\n",
            5,
            "subtitle_suspect_lines를 lift에서 채운다",
        ),
        # 속성 쓰기.
        (
            "vlogsrt/project/raw/finalcut_export.py",
            "def lift(timing):\n    timing.subtitle_verified = True\n",
            2,
            "subtitle_verified를 lift에서 채운다",
        ),
    ],
)
def test_verified_subtitle_fields_are_filled_only_by_the_service_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, rel: str, body: str, line: int, fragment: str
):
    clean = {TIER_SERVICE: CLEAN_SERVICE, "vlogsrt/project/raw/tiering.py": CLEAN_TIERING}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    (tmp_path / "ui").mkdir(exist_ok=True)
    assert check_invariants.check_tier_timing_only_from_service() == []
    (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
    (tmp_path / rel).write_text(body, encoding="utf-8")
    failures = check_invariants.check_tier_timing_only_from_service()
    assert any(
        failure.startswith(f"{rel}:{line}: ") and fragment in failure for failure in failures
    ), failures
    assert all(failure.endswith("(D65)") for failure in failures), failures


def test_reading_the_field_elsewhere_is_not_a_producer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    clean = {TIER_SERVICE: CLEAN_SERVICE, "vlogsrt/project/raw/tiering.py": CLEAN_TIERING}
    _fake_source_tree(monkeypatch, tmp_path, clean)
    (tmp_path / "ui").mkdir(exist_ok=True)
    (tmp_path / "ui/view.py").write_text(
        "def shown(timing):\n    return timing.subtitle_verified\n", encoding="utf-8"
    )
    assert check_invariants.check_tier_timing_only_from_service() == []


def test_real_tree_fills_verified_subtitles_only_in_the_service_entry():
    assert check_invariants.check_tier_timing_only_from_service() == []


# ── 작업대 — 렠 막대와 구간 패널의 「추천 근거」 ───────────────────────────


_LIFT_SCRIPT = r"""
const liftModel = visualModel();
liftModel.sources.srcA.suggestions = [
  { start: '0', end: '5', tier: 'green', hold: false, rec: 'recommended', rule: 'subtitle-verified',
    reason: 'r0', redReason: null, caps: [],
    lift: '자막 확인됨(시각 확인 초록 · 확인 필요 0/9줄) <i>x</i>',
    audio: { status: 'speech_dense', label: '발화 많음', reason: '발화가 구간의 90%를 덮는다',
      density: '9/10' },
    video: { status: 'usable', label: '사용 가능', reason: '계곡이 보인다', evidence: [] } },
];
const envLift = visualEnv({}, liftModel);
const liftBars = envLift.rail.innerHTML.split('<button')
  .filter((b) => b.includes('vl-recommendation'));
envLift.rail._listeners.click.slice().forEach((fn) => fn({ target: {
  getAttribute: (k) => (k === 'data-vl-recommendation' ? '0' : null) } }));
checkpoints.push({ case: 'lift', bars: liftBars, panel: envLift.panel.innerHTML });
"""


def _run_lift(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "lift.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _LIFT_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {"visual": ui_facade.VISUAL_BOOTSTRAP_JS, "caption": ui_facade.CAPTION_BOOTSTRAP_JS}
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "lift")


@needs_node
def test_rail_bar_and_panel_name_the_lift_escaped(tmp_path: Path):
    report = _run_lift(tmp_path)
    (bar,) = report["bars"]
    assert "추천 근거: 자막 확인됨(시각 확인 초록 · 확인 필요 0/9줄)" in bar
    panel = report["panel"]
    assert '<p class="vl-kv">추천 근거 · 자막 확인됨(시각 확인 초록 · 확인 필요 0/9줄)' in panel
    assert "&lt;i&gt;x&lt;/i&gt;" in panel and "<i>x</i>" not in panel and "<i>x</i>" not in bar


def test_suggestion_row_carries_the_lift_words():
    from ui.workspace_service import RawWorkspaceUi

    row = {
        "start": {"num": 0, "den": 1},
        "end": {"num": 5, "den": 1},
        "tier": "green",
        "hold": False,
        "recommendation": "recommended",
        "rule": RULE,
        "reason": "자막: a · 영상: b",
        "caps": [],
        "lift": lift_text(0, 9),
        "red_reason": None,
        "audio": {"status": "speech_dense", "label": "발화 많음", "reason": "a"},
        "video": {"status": "usable", "label": "사용 가능", "reason": "b"},
    }
    assert RawWorkspaceUi._suggestion_row(row)["lift"] == lift_text(0, 9)
    old = {key: value for key, value in row.items() if key != "lift"}  # v4 문서 모양
    assert "lift" not in RawWorkspaceUi._suggestion_row(old)
    assert "lift" not in RawWorkspaceUi._suggestion_row({**row, "lift": None})
