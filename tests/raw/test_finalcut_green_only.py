"""D93 — Final Cut 보내기는 추천(초록)만 켠다(2026-10-27 사용자 지시).

「노란색도 비활성하고 초록색만 활성해서 나가는 형식」 — 보내기 설정(`FINALCUT_TIMELINE`)이
`recommended_only`를 켠다. 시험이 못 박는 것:

- 추천 조각만 켜고, 보통·판정 보류·미분석은 끈 채 보낸다(지우지 않는다 — 클립·키워드 그대로).
- 사람이 「사용」으로 정한 조각은 등급과 무관하게 켠다(사람 결정이 이긴다 — 되살린 빨강과 같다).
- 사람이 확정한 제외·미검토 빨강은 예전처럼 끄고 키워드도 그대로다.
- 꺼진 보통 조각 안에 묻힌 추천에는 「추천」 키워드 범위를 단다(찾아서 V로 켤 수 있게) — 제외로
  꺼진 조각에는 달지 않는다.
- 빌더의 기본 설정(`FinalCutTimelineSettings()`)은 D80 그대로다 — 보내기만 바뀐다.
- 실제 보내기(서비스)의 켠 스파인 클립은 모두 추천이다.
"""

from __future__ import annotations

import shutil
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from tests.raw.test_finalcut_timeline import at, media, report, seg, span
from vlogsrt.project.raw import finalcut_timeline as ft
from vlogsrt.project.raw.config import FINALCUT_TIMELINE, FinalCutTimelineSettings

GREEN_ONLY = FINALCUT_TIMELINE


def _tiers(**segment_flags: bool) -> ft.SourceInput:
    """120프레임씩 넷 — 추천 · 보통 · 판정 보류 · 보통(조각 플래그는 둘째 조각에만)."""
    return ft.SourceInput(
        media=media("tiers", frames=480, ident=None),
        segments=(seg(0, 120), seg(120, 240, **segment_flags), seg(240, 360), seg(360, 480)),
        report=report(
            span(at(0), at(120), "green"),
            span(at(120), at(240), "yellow"),
            span(at(240), at(360), "hold"),
            span(at(360), at(480), "yellow"),
        ),
    )


def test_the_export_setting_keeps_only_recommended_clips_on() -> None:
    assert GREEN_ONLY.recommended_only is True
    assert FinalCutTimelineSettings().recommended_only is False  # 빌더 기본은 D80 그대로
    clips = ft.plan_source(_tiers(), settings=GREEN_ONLY).clips
    assert [clip.keywords[0] for clip in clips] == [
        ft.KEYWORD_RECOMMENDED,
        ft.KEYWORD_NORMAL,
        ft.KEYWORD_HOLD,
        ft.KEYWORD_NORMAL,
    ]
    assert [clip.enabled for clip in clips] == [True, False, False, False]
    # 끄기는 지우기가 아니다 — 원본을 빈틈없이 덮고 키워드는 등급 하나뿐이다
    assert [(c.start_frame, c.end_frame) for c in clips] == [
        (0, 120),
        (120, 240),
        (240, 360),
        (360, 480),
    ]
    assert all(len(clip.keywords) == 1 for clip in clips)
    d80 = ft.plan_source(_tiers()).clips
    assert [clip.enabled for clip in d80] == [True, True, True, True]


def test_a_human_use_decision_keeps_a_normal_clip_on() -> None:
    clips = ft.plan_source(_tiers(decided=True), settings=GREEN_ONLY).clips
    assert [clip.enabled for clip in clips] == [True, True, False, False]


def test_exclusions_stay_off_with_their_keywords() -> None:
    confirmed = ft.plan_source(_tiers(excluded=True, decided=True), settings=GREEN_ONLY).clips[1]
    assert confirmed.enabled is False
    assert confirmed.keywords == (ft.KEYWORD_NORMAL, ft.KEYWORD_CONFIRMED_EXCLUSION)
    source = ft.SourceInput(
        media=media("red", frames=120, ident=None),
        segments=(seg(0, 60), seg(60, 120, decided=True)),
        report=report(span(at(0), at(120), "red")),
    )
    loose, restored = ft.plan_source(source, settings=GREEN_ONLY).clips
    assert loose.enabled is False
    assert loose.keywords == (ft.KEYWORD_SUGGESTED_EXCLUSION, ft.KEYWORD_UNREVIEWED)
    assert restored.enabled is True  # 사람이 되살린 빨강은 켠다(D82 그대로)


def test_an_unanalysed_source_goes_off_whole() -> None:
    source = ft.SourceInput(media=media("loose", frames=90, ident=None))
    (clip,) = ft.plan_source(source, settings=GREEN_ONLY).clips
    assert (clip.enabled, clip.keywords) == (False, (ft.KEYWORD_UNANALYZED,))


def test_buried_recommended_is_marked_inside_a_switched_off_normal_clip_only() -> None:
    """보통 조각(0–300) 안의 4초 추천 범위 — 꺼져도 「추천」 범위가 붙는다. 확정 제외면 없다."""
    spans = (
        span(at(0), at(60), "yellow"),
        span(at(60), at(180), "green"),
        span(at(180), at(300), "yellow"),
    )
    for flags, expected in (({}, ((60, 180),)), ({"excluded": True, "decided": True}, ())):
        source = ft.SourceInput(
            media=media("buried", frames=300, ident=None),
            segments=(seg(0, 300, **flags),),
            report=report(*spans),
        )
        (clip,) = ft.plan_source(source, settings=GREEN_ONLY).clips
        assert clip.enabled is False and clip.keywords[0] == ft.KEYWORD_NORMAL
        assert clip.recommended == expected


def test_the_vote_for_orientation_and_rate_counts_only_green_clips() -> None:
    """방향·레이트 표는 켠 클립만 센다(D66·D71) — 이제 켠 클립은 추천뿐이다."""
    landscape = ft.SourceInput(
        media=media("wide", frames=600, ident=None, size=(1920, 1080)),
        segments=(seg(0, 600),),
        report=report(span(at(0), at(600), "yellow")),
    )
    portrait = ft.SourceInput(
        media=media("tall", frames=200, ident=None, size=(1080, 1920)),
        segments=(seg(0, 200),),
        report=report(span(at(0), at(200), "green")),
    )
    kwargs = {"title": "방향", "library_bundle": None}
    green = ft.plan([landscape, portrait], settings=GREEN_ONLY, **kwargs)  # type: ignore[arg-type]
    d80 = ft.plan([landscape, portrait], **kwargs)  # type: ignore[arg-type]
    assert ft.document_counts(green)["orientation"] == "portrait"
    assert ft.document_counts(d80)["orientation"] == "landscape"


# ── 실제 보내기 ──────────────────────────────────────────────────────────


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
def test_a_real_export_turns_on_only_recommended_spine_clips(tmp_path: Path) -> None:
    from tests.raw.test_auto_cleanup_review_gate import _synthetic_project

    service, root = _synthetic_project(tmp_path)
    clips = ET.parse(service.export_finalcut_timeline(root)).findall(".//spine/asset-clip")
    assert clips
    for clip in clips:
        tier = clip.findall("keyword")[0].get("value")
        assert (clip.get("enabled") != "0") == (tier == ft.KEYWORD_RECOMMENDED), tier
