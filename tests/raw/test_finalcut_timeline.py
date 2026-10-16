"""전체 타임라인 하나로 Final Cut에 보내는 순수 빌더의 계약 (W-16, D56).

입력은 값뿐이다(원본 사실·검토 조각 상태·등급 보고서·자막 큐). 파일·프로세스·서비스를
부르지 않으므로 재료는 전부 여기서 만든다. 지키는 선:

- 아무것도 지우지 않는다 — 원본마다 클립 길이 합이 원본 길이다.
- 사람이 확정한 제외만 끈다(`enabled="0"` + `제외 확정`). 미검토 빨강은 켠 채 키워드 둘.
- 키워드는 `tiering.segment_keyword`와 한 규칙이다. 평가는 브라우저 클립의 초록에만, reject 없음.
- 캡션은 프레임에 맞추고 컷에서 나누며, 빈 글·겹침·줄·꼬리를 **세기만** 한다.
- 시각은 실측 타임코드에서: 0008 `12:56:29;10` → `1397678282/30000s`.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
import xml.etree.ElementTree as ET
from dataclasses import replace
from datetime import datetime
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.fcp_tables import FRAME_RATE_TAGS
from vlogsrt.project.raw import finalcut_timeline as ft
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.analysis_config import TIER_SETTINGS, RawTierSettings
from vlogsrt.project.xml_validation import validate_xml

ROOT = Path(__file__).resolve().parents[2]
INSTALLED_DTD = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework"
    "/Versions/A/Resources/FCPXMLv1_14.dtd"
)
INSTALLED_DTD_SHA256 = "33bb44530790be145d87ed2d5c347166aab945c90f1cfbc03c8be545aaf73cb5"
needs_xmllint = pytest.mark.skipif(shutil.which("xmllint") is None, reason="xmllint이 없는 환경")
needs_installed_dtd = pytest.mark.skipif(
    not INSTALLED_DTD.is_file(), reason="설치된 Final Cut Pro 1.14 DTD가 없는 환경"
)

F = Fraction
NTSC = F(1001, 30000)
BT709 = ("bt709", "bt709", "bt709")
BUNDLE = Path("/Users/ssojux2/Movies/test_dir/260810_wangbang.fcpbundle")
ORIGINALS = BUNDLE / "clips" / "Original Media"

# 실측(읽기 전용) — 라이브러리 `FFMIORADAsset`의 mediaIdentifier·mediaRange와 원본 타임코드.
ID_0008 = "52889BE5F969239B5AC1E14037397BBA"
TC_0008 = 1396282  # 12:56:29;10 → mediaRange 시작 1397678282/30000
TC_0014 = 1435575  # 13:18:20;13
TC_0019 = 1686673  # 15:37:58;21


# ─── 재료 ────────────────────────────────────────────────────────────────


def media(
    name: str = "DJI_20260811125046_0008_D",
    *,
    frames: int = 1168,
    tc: int = TC_0008,
    drop: bool = True,
    ident: str | None = ID_0008,
    fd: Fraction = NTSC,
    size: tuple[int, int] = (3840, 2160),
    color: tuple[str, str, str] = BT709,
    audio: tuple[int, int] | None = (2, 48000),
    folder: Path = ORIGINALS,
) -> ft.SourceMedia:
    return ft.SourceMedia(
        source_id=f"asset-{name}",
        name=name,
        path=folder / f"{name}.mov",
        content_sha256=hashlib.sha256(name.encode()).hexdigest(),
        media_identifier=ident,
        frame_duration=fd,
        frame_count=frames,
        timecode_frames=tc,
        drop_frame=drop,
        width=size[0],
        height=size[1],
        color=color,
        audio_channels=None if audio is None else audio[0],
        audio_rate=None if audio is None else audio[1],
    )


def seg(a: int, b: int, *, excluded: bool = False, unreviewed: bool = False) -> ft.SegmentState:
    return ft.SegmentState(start_frame=a, end_frame=b, excluded=excluded, unreviewed=unreviewed)


_STATUSES = {
    "green": (tiering.AUDIO_SPEECH_DENSE, tiering.VIDEO_GOOD),
    "yellow": (tiering.AUDIO_SPEECH_SPARSE, tiering.VIDEO_USABLE),
    "hold": (tiering.AUDIO_SPEECH_SPARSE, tiering.VIDEO_NO_FRAMES),
    "red": (tiering.AUDIO_SILENT, tiering.VIDEO_FLAGGED),
    "red2": (tiering.AUDIO_SILENT, tiering.VIDEO_USABLE),
}


def span(a: Fraction, b: Fraction, kind: str) -> tiering.TierSpan:
    audio, video = _STATUSES[kind]
    tier = {"green": tiering.TIER_GREEN, "red": tiering.TIER_RED, "red2": tiering.TIER_RED}.get(
        kind, tiering.TIER_YELLOW
    )
    return tiering.TierSpan(
        start=F(a),
        end=F(b),
        tier=tier,
        rule=kind,
        audio=tiering.AudioJudgment(audio, F(0), "자막", ()),
        video=tiering.VideoJudgment(video, "영상", (), ()),
        red_reason=tiering.RED_SILENCE if tier == tiering.TIER_RED else None,
    )


NO_MINIMUM = replace(TIER_SETTINGS, recommended_min_seconds=0.0)
"""추천 최소 3초(D60 규칙 2)를 끈 설정 — 손으로 만든 1–2초짜리 녹색 구간으로 스냅·합치기·
즐겨찾기를 보는 시험이 그 규칙과 섞이지 않게 한다(규칙 2의 효과는 따로 단언한다)."""


def report(
    *spans: tiering.TierSpan, settings: RawTierSettings = TIER_SETTINGS
) -> tiering.TierReport:
    return tiering.TierReport(
        tier_version=settings.tier_version,
        settings=settings,
        document_sha256="0" * 64,
        spans=tuple(spans),
    )


def at(frame: int) -> Fraction:
    """프레임 경계의 초 — 등급 보고서와 자막 큐가 쓰는 원본 시계."""
    return frame * NTSC


def real_like_sources() -> list[ft.SourceInput]:
    """§2 골격을 실물 숫자로 — 0008(분석·라이브러리 신원·DF), 0014(확정 제외·미검토 빨강),
    0019(분석 전·검토 머리 밖)."""
    first = ft.SourceInput(
        media=media(),
        segments=(seg(0, 600), seg(600, 1168)),
        report=report(span(at(0), at(600), "yellow"), span(at(600), at(1168), "green")),
        cues=(ft.Cue(start=F(1001, 1000), end=F(25025, 10000), text="안녕하세요"),),
    )
    second = ft.SourceInput(
        media=media("DJI_20260811131238_0014_D", frames=604, tc=TC_0014, ident=None),
        segments=(
            seg(0, 300, excluded=True),
            seg(300, 450, excluded=True, unreviewed=True),
            seg(450, 604),
        ),
        report=report(
            span(at(0), at(300), "yellow"),
            span(at(300), at(450), "red"),
            span(at(450), at(604), "hold"),
        ),
        cues=(ft.Cue(start=at(280), end=at(320), text="다음 장소로"),),
    )
    third = ft.SourceInput(
        media=media("DJI_20260811153216_0019_D", frames=1064, tc=TC_0019, ident=None)
    )
    return [first, second, third]


def real_like_document() -> ft.TimelineDocument:
    return ft.plan(
        real_like_sources(),
        title="vlogsrt · 260810_wangbang · 2026-10-09 18:30",
        library_bundle=BUNDLE,
    )


def parsed(document: ft.TimelineDocument) -> ET.Element:
    return ET.fromstring(ft.render(document))


def spine_clips(root: ET.Element) -> list[ET.Element]:
    spine = root.find("./library/event/project/sequence/spine")
    assert spine is not None
    return list(spine)


def t(value: str) -> Fraction:
    return F(value.removesuffix("s"))


# ─── 1·2. DTD와 자식 순서 ───────────────────────────────────────────────


def cue_at(a: Fraction | int, b: Fraction | int, text: str, fd: Fraction = NTSC) -> ft.Cue:
    return ft.Cue(F(a) * fd, F(b) * fd, text)


def frames_report(
    fd: Fraction, *rows: tuple[Fraction | int, Fraction | int, str]
) -> tiering.TierReport:
    return report(*(span(F(a) * fd, F(b) * fd, kind) for a, b, kind in rows))


def edge_mixed() -> ft.TimelineDocument:
    """크기·색이 섞이고(모르는 색 포함) 분석 전 원본과 1프레임 원본이 있으며, 꺼진 클립 밑 캡션·
    세 클립 걸친 큐·0프레임·겹침·꼬리·영상 밖·빈 글·세 줄·이모지·XML 특수 문자가 다 들어 있다."""
    first = ft.SourceInput(
        media=media("A 한글 이름", frames=300),
        segments=(seg(0, 100), seg(100, 200, excluded=True),
                  seg(200, 250, excluded=True, unreviewed=True), seg(250, 300)),
        report=frames_report(NTSC, (0, 90, "green"), (90, 200, "yellow"), (200, 250, "red"),
                             (250, 300, "hold")),
        cues=(
            cue_at(10, 20, "앞 & <뒤> \"인용\" '홑'"), cue_at(95, 110, "컷을 넘는다"),
            cue_at(150, 150, "길이0"), cue_at(160, 180, "겹침1"), cue_at(170, 190, "겹침2"),
            cue_at(199, 251, "세 클립 걸침"), cue_at(290, 400, "꼬리"), cue_at(500, 600, "완전 밖"),
            cue_at(30, 40, "  "), cue_at(41, 50, "첫 줄\n둘째 줄\n셋째"),
            cue_at(51, 60, "😀 emoji"),
        ),
    )  # fmt: skip
    unanalysed = ft.SourceInput(
        media=media("B", frames=120, size=(1920, 1080), tc=0, drop=False, ident=None)
    )
    unknown = ft.SourceInput(
        media=media("C", frames=60, ident=None, color=("", "", "")),
        segments=(seg(0, 20), seg(20, 40), seg(40, 60, excluded=True)),
    )
    hlg = ("bt2020", "arib-std-b67", "bt2020nc")
    single = ft.SourceInput(
        media=media("D", frames=1, ident=None, audio=None, color=hlg),
        segments=(seg(0, 1),),
        report=frames_report(NTSC, (0, 1, "green")),
        cues=(cue_at(0, 0, "x"),),
    )
    bundle = Path("/Users/me/Movies/My Lib.fcpbundle")
    return ft.plan(
        [first, unanalysed, unknown, single], title="vlogsrt · 테스트 · 2026-10-09 18:30",
        library_bundle=bundle,
    )  # fmt: skip


def edge_pal() -> ft.TimelineDocument:
    """25p NDF · 타임코드 없음 · 소리 없음 · 라이브러리 밖."""
    pal = F(1, 25)
    source = ft.SourceInput(
        media=media("pal", frames=250, fd=pal, tc=0, drop=False, ident=None, audio=None),
        segments=(seg(0, 125), seg(125, 250, excluded=True)),
        report=frames_report(pal, (0, 125, "green"), (125, 250, "red")),
        cues=(cue_at(10, 140, "pal cut", pal),),
    )
    return ft.plan([source], title="t", library_bundle=None)


def edge_hfr() -> ft.TimelineDocument:
    """59.94 DF · 6채널 96k(시퀀스 오디오 철자가 없다)."""
    hfr = F(1001, 60000)
    source = ft.SourceInput(
        media=media("hfr", frames=600, fd=hfr, tc=32368, drop=True, ident=None, audio=(6, 96000)),
        segments=(seg(0, 300), seg(300, 600)),
        report=frames_report(hfr, (0, 600, "yellow")),
        cues=(cue_at(299, 301, "edge", hfr),),
    )
    return ft.plan([source], title="t", library_bundle=None)


def edge_film() -> ft.TimelineDocument:
    """23.976 NDF · 모노 44.1k · 분석 전."""
    film = F(1001, 24000)
    source = ft.SourceInput(
        media=media("film", frames=100, fd=film, tc=86400, drop=False, ident=None, audio=(1, 44100))
    )
    return ft.plan([source], title="t", library_bundle=None)


def edge_red() -> ft.TimelineDocument:
    """전부 미검토 빨강 + 1프레임도 안 되는 초록 자투리(브라우저에서 버려진다)."""
    source = ft.SourceInput(
        media=media("red", frames=100, ident=None),
        segments=(seg(0, 100, excluded=True, unreviewed=True),),
        report=frames_report(NTSC, (0, F(1, 3), "green"), (F(1, 3), 100, "red")),
        cues=(),
    )
    return ft.plan([source], title="t", library_bundle=None)


EDGE_DOCUMENTS = {
    "real_like": real_like_document,
    "mixed": edge_mixed,
    "pal": edge_pal,
    "hfr": edge_hfr,
    "film": edge_film,
    "red": edge_red,
}


@needs_xmllint
@needs_installed_dtd
@pytest.mark.parametrize("name", sorted(EDGE_DOCUMENTS))
def test_documents_pass_the_installed_fcpxml_1_14_dtd(tmp_path: Path, name: str) -> None:
    digest = hashlib.sha256(INSTALLED_DTD.read_bytes()).hexdigest()
    assert digest == INSTALLED_DTD_SHA256, f"설치 DTD가 바뀌었다: {digest}"
    target = tmp_path / f"{name}.fcpxml"
    target.write_bytes(ft.render(EDGE_DOCUMENTS[name]()))
    result = validate_xml(target, dtd_path=INSTALLED_DTD)
    assert result.dtd_checked is True
    assert result.valid is True, result.diagnostics


def _check_spine_clip(clip: ET.Element) -> None:
    keywords = [k.attrib["value"] for k in clip.findall("keyword")]
    assert (clip.get("enabled") == "0") == (ft.KEYWORD_CONFIRMED_EXCLUSION in keywords)
    if ft.KEYWORD_UNREVIEWED in keywords:
        assert clip.get("enabled") is None and ft.KEYWORD_SUGGESTED_EXCLUSION in keywords
    assert clip.find("rating") is None
    begin = t(clip.attrib["start"])
    end = begin + t(clip.attrib["duration"])
    for caption in clip.findall("caption"):
        offset, length = t(caption.attrib["offset"]), t(caption.attrib["duration"])
        assert begin <= offset and offset + length <= end and length > 0
        assert caption.get("enabled") == clip.get("enabled")
        assert caption.attrib["name"] == caption.findtext("text")


@pytest.mark.parametrize("name", sorted(EDGE_DOCUMENTS))
def test_documents_keep_coverage_states_and_identity(name: str) -> None:
    document = EDGE_DOCUMENTS[name]()
    raw = ft.render(document)
    assert raw == ft.render(EDGE_DOCUMENTS[name]())
    root = ET.fromstring(raw)
    assets = {a.attrib["id"]: a for a in root.findall("./resources/asset")}
    used: dict[str, Fraction] = {}
    cursor = F(0)
    for clip in spine_clips(root):
        ref = clip.attrib["ref"]
        assert t(clip.attrib["offset"]) == cursor
        assert t(clip.attrib["start"]) == t(assets[ref].attrib["start"]) + used.get(ref, F(0))
        used[ref] = used.get(ref, F(0)) + t(clip.attrib["duration"])
        cursor += t(clip.attrib["duration"])
        _check_spine_clip(clip)
    assert used == {key: t(a.attrib["duration"]) for key, a in assets.items()}
    sequence = root.find(".//sequence")
    assert sequence is not None and t(sequence.attrib["duration"]) == cursor
    assert all("," not in k.attrib["value"] for k in root.iter("keyword"))
    assert all(r.attrib["value"] == "favorite" for r in root.iter("rating"))
    for asset in assets.values():
        rep = asset.find("media-rep")
        assert rep is not None
        assert (
            not re.fullmatch(r"[0-9A-F]+", asset.attrib["uid"])
            or rep.get("sig") == asset.attrib["uid"]
        )


def test_edge_documents_report_what_they_could_not_carry() -> None:
    mixed = ft.document_counts(edge_mixed())
    assert mixed["unknown_color"] == ["C"] and mixed["formats"] == 4
    assert mixed["captions"] == {
        "cues": 12, "placed": 10, "pieces": 13, "empty": 1, "widened": 2, "inverted": 0,
        "overlaps": 1, "line_issues": 1, "tails": 1, "outside": 1, "split": 2,
    }  # fmt: skip
    hfr = parsed(edge_hfr()).find(".//sequence")
    assert hfr is not None and "audioLayout" not in hfr.attrib and hfr.attrib["tcFormat"] == "DF"
    pal = parsed(edge_pal())
    asset = pal.find("./resources/asset")
    assert asset is not None and asset.attrib["start"] == "0s" and asset.attrib["hasAudio"] == "0"
    red_root = parsed(edge_red())
    (clip,) = spine_clips(red_root)
    assert "enabled" not in clip.attrib
    assert [k.attrib["value"] for k in clip.findall("keyword")] == [
        "제외 제안",
        "제외 제안(미검토)",
    ]
    browser = red_root.find("./library/event/asset-clip")
    assert browser is not None
    assert [k.attrib["value"] for k in browser] == ["제외 제안"]  # 초록 자투리는 0프레임이다


@needs_xmllint
@needs_installed_dtd
def test_reversed_child_order_is_what_the_dtd_rejects(tmp_path: Path) -> None:
    """caption(anchor)은 keyword/rating(marker)보다 앞이어야 한다 — 뒤집으면 거부된다."""
    root = parsed(real_like_document())
    clip = spine_clips(root)[0]
    caption = clip.find("caption")
    assert caption is not None
    clip.remove(caption)
    clip.append(caption)
    target = tmp_path / "reversed.fcpxml"
    target.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root))
    assert validate_xml(target, dtd_path=INSTALLED_DTD).valid is False


def test_captions_come_before_keywords_and_keywords_before_ratings() -> None:
    root = parsed(real_like_document())
    rank = {"caption": 0, "keyword": 1, "rating": 2}
    clips = root.findall(".//asset-clip")
    assert any(clip.find("caption") is not None for clip in clips)
    assert any(clip.find("rating") is not None for clip in clips)
    for clip in clips:
        order = [rank[child.tag] for child in clip]
        assert order == sorted(order)


def test_document_skeleton_matches_the_validated_shape() -> None:
    root = parsed(real_like_document())
    assert root.tag == "fcpxml" and root.attrib == {"version": "1.14"}
    assert [child.tag for child in root] == ["import-options", "resources", "library"]
    event = root.find("./library/event")
    assert event is not None
    assert event.attrib == {"name": "vlogsrt · 260810_wangbang · 2026-10-09 18:30"}
    assert [child.tag for child in event] == [
        "asset-clip", "asset-clip", "keyword-collection", "keyword-collection",
        "keyword-collection", "keyword-collection", "project",
    ]  # fmt: skip
    project = event.find("project")
    assert project is not None and project.attrib == {"name": event.attrib["name"]}
    raw = ft.render(real_like_document())
    assert raw.startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n')


# ─── 3·4. 순서와 빈틈 없는 덮개 ──────────────────────────────────────────


def test_spine_follows_given_shot_order_then_segment_start() -> None:
    late = ft.SourceInput(media=media("late", frames=10, tc=0, ident=None, drop=False))
    early = ft.SourceInput(
        media=media("early", frames=10, tc=0, ident=None, drop=False),
        segments=(seg(5, 10), seg(0, 5)),
        report=report(span(at(0), at(10), "green")),  # 분석된 원본은 조각마다 클립이다
    )
    document = ft.plan([late, early], title="t", library_bundle=None)
    root = parsed(document)
    names = {a.attrib["id"]: a.attrib["name"] for a in root.findall("./resources/asset")}
    clips = spine_clips(root)
    assert [names[c.attrib["ref"]] for c in clips] == ["late", "early", "early"]
    assert [t(c.attrib["start"]) for c in clips] == [F(0), F(0), 5 * NTSC]


def test_every_source_is_covered_end_to_end_and_offsets_telescope() -> None:
    root = parsed(real_like_document())
    assets = {a.attrib["id"]: a for a in root.findall("./resources/asset")}
    totals: dict[str, Fraction] = {}
    cursor = F(0)
    for clip in spine_clips(root):
        asset = assets[clip.attrib["ref"]]
        assert t(clip.attrib["offset"]) == cursor
        expected_start = t(asset.attrib["start"]) + totals.get(clip.attrib["ref"], F(0))
        assert t(clip.attrib["start"]) == expected_start
        totals[clip.attrib["ref"]] = totals.get(clip.attrib["ref"], F(0)) + t(
            clip.attrib["duration"]
        )
        cursor += t(clip.attrib["duration"])
    assert totals == {key: t(a.attrib["duration"]) for key, a in assets.items()}
    sequence = root.find(".//sequence")
    assert sequence is not None and t(sequence.attrib["duration"]) == cursor
    assert cursor == (1168 + 604 + 1064) * NTSC


@pytest.mark.parametrize(
    "segments",
    [
        (seg(0, 5), seg(6, 10)),  # 틈
        (seg(0, 6), seg(5, 10)),  # 겹침
        (seg(0, 5), seg(5, 9)),  # 끝까지 못 감
        (seg(1, 10),),  # 머리가 빔
        (seg(0, 5), seg(5, 5), seg(5, 10)),  # 길이 0
        (),
    ],
)
def test_segments_that_do_not_tile_the_whole_file_are_refused(
    segments: tuple[ft.SegmentState, ...],
) -> None:
    source = ft.SourceInput(media=media("gap", frames=10, ident=None), segments=segments)
    with pytest.raises(ft.FinalCutTimelineError, match="gap"):
        ft.plan_source(source)


# ─── 5·6. 켜고 끄기와 키워드 ─────────────────────────────────────────────


def test_only_human_confirmed_exclusions_are_disabled() -> None:
    plan = ft.plan_source(real_like_sources()[1])
    excluded, unreviewed, kept = plan.clips
    assert excluded.enabled is False
    assert excluded.keywords == (ft.KEYWORD_NORMAL, ft.KEYWORD_CONFIRMED_EXCLUSION)
    assert unreviewed.enabled is True
    assert unreviewed.keywords == (ft.KEYWORD_SUGGESTED_EXCLUSION, ft.KEYWORD_UNREVIEWED)
    assert kept.enabled is True and kept.keywords == (ft.KEYWORD_HOLD,)
    clips = spine_clips(parsed(real_like_document()))[2:5]
    assert [c.attrib.get("enabled") for c in clips] == ["0", None, None]
    assert [[k.attrib["value"] for k in c.findall("keyword")] for c in clips] == [
        ["보통", "제외 확정"],
        ["제외 제안", "제외 제안(미검토)"],
        ["판정 보류"],
    ]


def test_restored_red_segment_stays_enabled_with_only_its_tier_keyword() -> None:
    source = ft.SourceInput(
        media=media("restored", frames=60, ident=None),
        segments=(seg(0, 60),),
        report=report(span(at(0), at(60), "red")),
    )
    (clip,) = ft.plan_source(source).clips
    assert clip.enabled is True and clip.keywords == (ft.KEYWORD_SUGGESTED_EXCLUSION,)


def test_unreviewed_mark_on_an_included_segment_is_refused() -> None:
    source = ft.SourceInput(
        media=media("odd", frames=10, ident=None), segments=(seg(0, 10, unreviewed=True),)
    )
    with pytest.raises(ft.FinalCutTimelineError, match="odd"):
        ft.plan_source(source)


def test_timeline_keyword_is_segment_keyword_and_all_hold_is_hold() -> None:
    spans = (
        span(at(0), at(40), "green"),
        span(at(40), at(70), "hold"),
        span(at(70), at(100), "yellow"),
        span(at(100), at(160), "hold"),
    )
    segments = (seg(0, 40), seg(40, 70), seg(70, 160), seg(160, 200))
    for tiers, first in ((report(*spans, settings=NO_MINIMUM), "추천"), (report(*spans), "보통")):
        source = ft.SourceInput(
            media=media("tiers", frames=200, ident=None), segments=segments, report=tiers
        )
        plan = ft.plan_source(source)
        expected = [
            tiering.segment_keyword(tiers, at(s.start_frame), at(s.end_frame)) for s in segments
        ]
        assert [clip.keywords[0] for clip in plan.clips] == expected
        # 40프레임(1.33초) 녹색 조각은 3초보다 짧아 보통이다(D60 규칙 2) — 끄면 추천.
        assert expected == [first, "판정 보류", "보통", "판정 보류"]


def test_analysed_source_outside_the_review_head_is_one_whole_file_clip() -> None:
    source = ft.SourceInput(
        media=media("loose", frames=90, ident=None), report=report(span(at(0), at(90), "green"))
    )
    (clip,) = ft.plan_source(source).clips
    assert (clip.start_frame, clip.end_frame, clip.enabled) == (0, 90, True)
    assert clip.keywords == (ft.KEYWORD_RECOMMENDED,)


# ─── 7. 브라우저 클립 범위 ────────────────────────────────────────────────


def test_browser_ranges_snap_tier_spans_to_the_nearest_frame() -> None:
    source = media("browse", frames=100, ident=None)
    spans = (
        span(F(0), F(1234, 1000), "yellow"),  # 36.98프레임 → 37
        span(F(1234, 1000), F(124, 100), "green"),  # 37 → 37: 0프레임 자투리는 버린다
        span(F(124, 100), F(3), "green"),  # 37 → 90
        span(F(3), F(35, 10), "red"),  # 90 → 105 → 100으로 자른다
        span(F(35, 10), F(4), "red2"),  # 같은 키워드 → 앞과 합친다
    )
    assert ft.browser_ranges(report(*spans, settings=NO_MINIMUM), source) == (
        ft.BrowserRange(0, 37, "보통", False),
        ft.BrowserRange(37, 90, "추천", True),
        ft.BrowserRange(90, 100, "제외 제안", False),
    )
    # 1.76초 녹색은 3초보다 짧아 보통이다(D60 규칙 2) — 앞 보통과 합쳐지고 즐겨찾기가 없다.
    assert ft.browser_ranges(report(*spans), source) == (
        ft.BrowserRange(0, 90, "보통", False),
        ft.BrowserRange(90, 100, "제외 제안", False),
    )


def test_browser_clips_carry_keyword_ranges_and_favorites_only_on_green() -> None:
    root = parsed(real_like_document())
    browser = root.findall("./library/event/asset-clip")
    assert [b.attrib["name"] for b in browser] == [
        "DJI_20260811125046_0008_D",
        "DJI_20260811131238_0014_D",
    ]  # 분석 전 0019는 브라우저 클립이 없다
    first = browser[0]
    assert "offset" not in first.attrib
    assert [(k.attrib["start"], k.attrib["duration"], k.attrib["value"]) for k in first] == [
        ("1397678282/30000s", "600600/30000s", "보통"),
        ("1398278882/30000s", "568568/30000s", "추천"),
        ("1398278882/30000s", "568568/30000s", "favorite"),
    ]
    ratings = [r.attrib["value"] for r in root.iter("rating")]
    assert ratings == ["favorite"] and "reject" not in ft.render(real_like_document()).decode()
    collections = [c.attrib["name"] for c in root.iter("keyword-collection")]
    assert collections == ["추천", "보통", "제외 제안", "판정 보류"]


# ─── 묻힌 추천(D60 20b-final) ─────────────────────────────────────────────


def buried_source(
    segments: tuple[ft.SegmentState, ...] = (seg(0, 300), seg(300, 600)),
    rows: tuple[tuple[Fraction | int, Fraction | int, str], ...] = (
        (0, 100, "yellow"),
        (100, 200, "green"),
        (200, 600, "yellow"),
    ),
) -> ft.SourceInput:
    """600프레임 원본 — 녹색 100–200프레임(3.34초)이 첫 조각(0–300, 보통) 안에 묻힌다."""
    return ft.SourceInput(
        media=media("buried", frames=600, ident=None),
        segments=segments,
        report=frames_report(NTSC, *rows),
        cues=(cue_at(120, 150, "묻힌 추천 속 말"),),
    )  # fmt: skip


def buried_document(source: ft.SourceInput) -> ft.TimelineDocument:
    return ft.plan([source], title="t", library_bundle=None)


def clip_keyword_ranges(clip: ET.Element) -> list[tuple[Fraction, Fraction, str]]:
    return [
        (t(k.attrib["start"]), t(k.attrib["duration"]), k.attrib["value"])
        for k in clip.findall("keyword")
    ]


def test_buried_recommended_range_gets_one_extra_keyword_range() -> None:
    source = buried_source()
    document = buried_document(source)
    first, second = document.sources[0].clips
    assert first.keywords == ("보통",) and first.recommended == ((100, 200),)
    assert second.recommended == ()  # 녹색과 겹치지 않는다
    root = parsed(document)
    clip, other = spine_clips(root)
    tc = 1396282
    assert clip_keyword_ranges(clip) == [
        (tc * NTSC, 300 * NTSC, "보통"),
        ((tc + 100) * NTSC, 100 * NTSC, "추천"),
    ]
    assert [child.tag for child in clip] == ["caption", "keyword", "keyword"]
    assert clip_keyword_ranges(other) == [((tc + 300) * NTSC, 300 * NTSC, "보통")]
    # 컷·켜짐·등급 키워드·캡션·브라우저 클립은 그대로다
    base = ft.timeline_clips(source)
    assert tuple(replace(c, recommended=()) for c in document.sources[0].clips) == base
    assert document.sources[0].captions == ft.caption_pieces(source.cues or (), base, NTSC)[0]
    assert document.sources[0].browser == ft.browser_ranges(source.report, source.media)
    assert ft.document_counts(document)["keywords"] == {"보통": 2}


def test_buried_ranges_are_cut_at_the_clip_edges_and_one_per_overlap() -> None:
    """녹색 하나가 두 클립에 걸치면 클립마다 제 겹침 하나씩 — 겹침이 짧은 쪽은 없다."""
    source = buried_source(
        segments=(seg(0, 150), seg(150, 400), seg(400, 600)),
        rows=((0, 50, "yellow"), (50, 260, "green"), (260, 330, "yellow"),
              (330, 500, "green"), (500, 600, "yellow")),
    )  # fmt: skip
    clips = buried_document(source).sources[0].clips
    assert [c.keywords[0] for c in clips] == ["보통", "보통", "보통"]
    assert [c.recommended for c in clips] == [((50, 150),), ((150, 260),), ((400, 500),)]
    # 두 번째 클립의 330–400(70프레임 = 2.34초)은 3초가 못 되어 없다


def test_recommended_clip_gets_no_extra_range() -> None:
    source = buried_source(segments=(seg(0, 300), seg(300, 600)), rows=((0, 600, "green"),))
    document = buried_document(source)
    assert [c.keywords[0] for c in document.sources[0].clips] == ["추천", "추천"]
    assert all(c.recommended == () for c in document.sources[0].clips)
    for clip in spine_clips(parsed(document)):
        assert [k.attrib["value"] for k in clip.findall("keyword")] == ["추천"]


def test_disabled_clip_gets_no_extra_range() -> None:
    """사람이 확정한 제외는 끈 채 그대로 — 그 안의 추천을 다시 알리지 않는다."""
    source = buried_source(segments=(seg(0, 300, excluded=True), seg(300, 600)))
    document = buried_document(source)
    first = document.sources[0].clips[0]
    assert (first.enabled, first.recommended) == (False, ())
    clip = spine_clips(parsed(document))[0]
    assert [k.attrib["value"] for k in clip.findall("keyword")] == ["보통", "제외 확정"]


def test_unreviewed_red_clip_is_enabled_and_still_shows_buried_recommended() -> None:
    """미검토 제외 제안은 켠 클립이다 — 그 안의 추천도 보인다(키워드만, 켜짐 그대로)."""
    source = buried_source(
        segments=(seg(0, 300, excluded=True, unreviewed=True), seg(300, 600)),
        rows=((0, 100, "red"), (100, 200, "green"), (200, 300, "red"), (300, 600, "yellow")),
    )
    first = buried_document(source).sources[0].clips[0]
    assert first.enabled and first.keywords == ("보통", ft.KEYWORD_UNREVIEWED)
    assert first.recommended == ((100, 200),)


@pytest.mark.parametrize(("length", "kept"), [(89, False), (90, True)])
def test_overlap_under_three_seconds_gets_none_and_the_minimum_rounds_up(
    length: int, kept: bool
) -> None:
    """3초 = 89.91프레임 → 올려 90프레임(3.003초)부터 — 89프레임(2.97초)은 모자란다.

    손으로 만든 녹색 구간은 길어서 브라우저에서는 「추천」이다 — 겹침의 길이만 본다.
    """
    source = buried_source(
        rows=((0, 100, "yellow"), (100, 300 + length, "green"), (300 + length, 600, "yellow")),
    )
    first, second = buried_document(source).sources[0].clips
    assert (first.keywords[0], second.keywords[0]) == ("보통", "보통")
    assert first.recommended == ((100, 300),)
    assert second.recommended == (((300, 300 + length),) if kept else ())


def test_minimum_frames_round_up_per_frame_rate() -> None:
    assert ft._minimum_frames(3.0, NTSC) == 90
    assert ft._minimum_frames(3.0, F(1, 25)) == 75
    assert ft._minimum_frames(3.0, F(1001, 60000)) == 180
    assert ft._minimum_frames(3.0, F(1001, 24000)) == 72
    assert ft._minimum_frames(0.0, NTSC) == 1  # 규칙을 끄면 1프레임 이상의 겹침 모두


@needs_xmllint
@needs_installed_dtd
def test_buried_ranges_pass_the_installed_dtd(tmp_path: Path) -> None:
    source = buried_source(
        segments=(seg(0, 150), seg(150, 400), seg(400, 600)),
        rows=((0, 50, "yellow"), (50, 260, "green"), (260, 330, "yellow"),
              (330, 500, "green"), (500, 600, "yellow")),
    )  # fmt: skip
    document = ft.plan(
        [source], title="t", library_bundle=None, caption_timing=frozenset({"asset-buried"})
    )
    raw = ft.render(document)
    spine = [clip_keyword_ranges(clip) for clip in spine_clips(ET.fromstring(raw))]
    assert [sum(value == "추천" for *_, value in rows) for rows in spine] == [1, 1, 1]
    target = tmp_path / "buried.fcpxml"
    target.write_bytes(raw)
    result = validate_xml(target, dtd_path=INSTALLED_DTD)
    assert result.dtd_checked is True and result.valid is True, result.diagnostics


@pytest.mark.parametrize("name", sorted(EDGE_DOCUMENTS))
def test_edge_documents_have_nothing_buried(name: str) -> None:
    """빌더 시험의 경계 문서에는 묻힌 추천이 없다 — 바이트는 W-16 지문 시험이 잇는다."""
    document = EDGE_DOCUMENTS[name]()
    assert all(clip.recommended == () for s in document.sources for clip in s.clips)


def test_buried_ranges_add_only_their_own_keyword_lines() -> None:
    """묻힌 추천을 걷은 문서의 바이트 = 묻힌 추천 줄만 뺀 바이트 — 그 밖은 한 바이트도 같다."""
    document = buried_document(buried_source())
    stripped = replace(
        document,
        sources=tuple(
            replace(s, clips=tuple(replace(c, recommended=()) for c in s.clips))
            for s in document.sources
        ),
    )
    # 같은 줄이 브라우저 클립(추천 범위)과 스파인 클립(묻힌 추천)에 하나씩 — 스파인은 뒤에 온다
    extra = '<keyword start="1397778382/30000s" duration="100100/30000s" value="추천" />'
    lines = ft.render(document).decode().splitlines(keepends=True)
    found = [index for index, line in enumerate(lines) if line.strip() == extra]
    assert len(found) == 2
    kept = "".join(line for index, line in enumerate(lines) if index != found[-1])
    assert ft.render(stripped).decode() == kept


# ─── 8·9. 캡션 ──────────────────────────────────────────────────────────


def test_captions_snap_split_at_cuts_and_inherit_disabled_state() -> None:
    source = ft.SourceInput(
        media=media("cap", frames=60, tc=100, drop=False, ident=None),
        segments=(seg(0, 30), seg(30, 60, excluded=True)),
        cues=(
            ft.Cue(F(5, 10), F(8, 10), "앞"),  # 14.99 → 15 · 23.98 → 24
            ft.Cue(F(9, 10), F(12, 10), "걸침"),  # 26.97 → 27 · 35.96 → 36 — 컷 30을 넘는다
        ),
    )
    plan = ft.plan_source(source)
    assert plan.captions == (
        ft.CaptionPiece(0, 15, 24, "앞", True),
        ft.CaptionPiece(0, 27, 30, "걸침", True),
        ft.CaptionPiece(1, 30, 36, "걸침", False),
    )
    assert plan.caption_counts is not None and plan.caption_counts.split == 1
    clips = spine_clips(parsed(ft.plan([source], title="t", library_bundle=None)))
    first, second = clips
    for clip in clips:
        begin = t(clip.attrib["start"])
        end = begin + t(clip.attrib["duration"])
        for caption in clip.findall("caption"):
            assert begin <= t(caption.attrib["offset"]) < end
            assert t(caption.attrib["offset"]) + t(caption.attrib["duration"]) <= end
    caption = first.findall("caption")[1]
    assert caption.attrib == {
        "lane": "1",
        "offset": f"{(100 + 27) * 1001}/30000s",
        "name": "걸침",  # Final Cut 자신의 내보내기처럼 캡션 이름이 곧 글이다
        "start": "3600s",
        "duration": f"{3 * 1001}/30000s",
        "role": "iTT?captionFormat=ITT.ko",
    }
    assert [x.text for x in caption] == ["걸침"]
    (tail,) = second.findall("caption")
    assert tail.attrib["enabled"] == "0" and tail.attrib["offset"] == f"{130 * 1001}/30000s"


def test_caption_problems_are_counted_but_never_altered() -> None:
    """세는 칸은 서로 겹치지 않는다: 큐 = 빈 글 + 영상 밖 + 넣은 큐. 넣은 큐 가운데 넓힌 것
    (0프레임)과 뒤집힌 것은 따로 세고, 꼬리는 일부가 잘린 넣은 큐만이다. 겹침·줄은 넣은 큐의
    성질이다."""
    clips = (ft.ClipPlan(0, 60, True, ("보통",)),)
    cues = (
        ft.Cue(F(1, 10), F(11, 100), "짧음"),  # 3.0 → 3, 3.3 → 3: 0프레임 → 1프레임
        ft.Cue(F(5, 10), F(8, 10), "겹침 앞"),
        ft.Cue(F(6, 10), F(9, 10), "겹침 뒤"),
        ft.Cue(F(10, 10), F(11, 10), "   "),
        ft.Cue(F(11, 10), F(12, 10), "첫 줄\n둘째 줄\n셋째 줄"),
        ft.Cue(F(12, 10), F(13, 10), "앞\n\n뒤"),
        ft.Cue(F(14, 10), F(13, 10), "뒤집힘"),  # 끝이 시작보다 앞 → 시작 프레임에 1프레임
        ft.Cue(F(19, 10), F(25, 10), "꼬리"),  # 57 → 75: 60에서 자른다
        ft.Cue(F(2), F(2), "끝점"),  # 60 → 61: 넓혀도 영상 밖이다 — 밖으로만 센다
        ft.Cue(F(3), F(35, 10), "밖"),  # 90 → 105: 영상 밖 전부
    )
    pieces, counts = ft.caption_pieces(cues, clips, NTSC)
    assert counts == ft.CaptionCounts(
        cues=10, placed=7, pieces=7, empty=1, widened=1, inverted=1, overlaps=1, line_issues=2,
        tails=1, outside=2, split=0,
    )  # fmt: skip
    assert counts.cues == counts.empty + counts.outside + counts.placed
    assert pieces[0] == ft.CaptionPiece(0, 3, 4, "짧음", True)
    assert [p.text for p in pieces] == [
        "짧음", "겹침 앞", "겹침 뒤", "첫 줄\n둘째 줄\n셋째 줄", "앞\n\n뒤", "뒤집힘", "꼬리",
    ]  # fmt: skip
    assert (pieces[1].start_frame, pieces[1].end_frame) == (15, 24)
    assert (pieces[2].start_frame, pieces[2].end_frame) == (18, 27)  # 겹침은 그대로 둔다
    assert (pieces[5].start_frame, pieces[5].end_frame) == (42, 43)
    assert (pieces[-1].start_frame, pieces[-1].end_frame) == (57, 60)


def test_source_without_transcript_has_no_captions_and_says_so() -> None:
    plan = ft.plan_source(real_like_sources()[2])
    assert plan.captions == () and plan.caption_counts is None
    counts = ft.document_counts(real_like_document())
    assert counts["sources_without_captions"] == ["DJI_20260811153216_0019_D"]


# ─── 10. 분석 전 원본 ───────────────────────────────────────────────────


def test_unanalysed_source_without_review_head_is_one_unanalysed_clip() -> None:
    plan = ft.plan_source(real_like_sources()[2])
    assert plan.clips == (ft.ClipPlan(0, 1064, True, (ft.KEYWORD_UNANALYZED,)),)
    assert plan.browser is None and plan.analysed is False
    clip = spine_clips(parsed(real_like_document()))[-1]
    assert [k.attrib["value"] for k in clip.findall("keyword")] == ["미분석"]
    assert clip.findall("caption") == [] and clip.findall("rating") == []


def test_unanalysed_identical_tiles_merge_but_human_exclusions_stay_apart() -> None:
    source = ft.SourceInput(
        media=media("plain", frames=40, ident=None),
        segments=(seg(0, 10), seg(10, 20), seg(20, 30, excluded=True), seg(30, 40)),
    )
    assert ft.plan_source(source).clips == (
        ft.ClipPlan(0, 20, True, ("미분석",)),
        ft.ClipPlan(20, 30, False, ("미분석", "제외 확정")),
        ft.ClipPlan(30, 40, True, ("미분석",)),
    )


# ─── 11. 포맷 ───────────────────────────────────────────────────────────


def test_mixed_resolution_gets_two_formats_and_the_longest_use_sets_the_sequence() -> None:
    small = ft.SourceInput(media=media("small", frames=300, size=(1920, 1080), ident=None))
    large = ft.SourceInput(media=media("large", frames=100, ident=None))
    unknown = ft.SourceInput(media=media("odd", frames=10, ident=None, color=("", "", "")))
    document = ft.plan([large, small, unknown], title="t", library_bundle=None)
    root = parsed(document)
    formats = root.findall("./resources/format")
    assert [f.attrib for f in formats] == [
        {
            "id": "r1",
            "name": f"FFVideoFormat3840x2160p{FRAME_RATE_TAGS[NTSC]}",
            "frameDuration": "1001/30000s",
            "width": "3840",
            "height": "2160",
            "colorSpace": "1-1-1 (Rec. 709)",
        },
        {
            "id": "r2",
            "name": "FFVideoFormat1920x1080p2997",
            "frameDuration": "1001/30000s",
            "width": "1920",
            "height": "1080",
            "colorSpace": "1-1-1 (Rec. 709)",
        },
        {
            "id": "r3",
            "name": "FFVideoFormat3840x2160p2997",
            "frameDuration": "1001/30000s",
            "width": "3840",
            "height": "2160",
        },
    ]
    sequence = root.find(".//sequence")
    assert sequence is not None and sequence.attrib["format"] == "r2"
    assert [c.attrib["format"] for c in spine_clips(root)] == ["r1", "r2", "r3"]
    assert ft.document_counts(document)["unknown_color"] == ["odd"]


def test_mixed_frame_rates_are_refused_naming_the_files() -> None:
    ntsc = ft.SourceInput(media=media("ntsc_clip", frames=10, ident=None))
    pal = ft.SourceInput(media=media("pal_clip", frames=10, fd=F(1, 25), drop=False, ident=None))
    with pytest.raises(ft.FinalCutTimelineError) as caught:
        ft.plan([ntsc, pal], title="t", library_bundle=None)
    assert "ntsc_clip" in str(caught.value) and "pal_clip" in str(caught.value)


def test_sequence_audio_and_timecode_format_follow_the_longest_use() -> None:
    root = parsed(real_like_document())
    sequence = root.find(".//sequence")
    assert sequence is not None
    assert {k: sequence.attrib[k] for k in ("tcStart", "tcFormat", "audioLayout", "audioRate")} == {
        "tcStart": "0s",
        "tcFormat": "DF",
        "audioLayout": "stereo",
        "audioRate": "48k",
    }
    silent = ft.SourceInput(media=media("mute", frames=10, ident=None, audio=None, drop=False))
    root = parsed(ft.plan([silent], title="t", library_bundle=None))
    sequence = root.find(".//sequence")
    assert sequence is not None and "audioLayout" not in sequence.attrib
    assert sequence.attrib["tcFormat"] == "NDF"
    (clip,) = spine_clips(root)
    assert "audioRole" not in clip.attrib
    asset = root.find("./resources/asset")
    assert asset is not None and asset.attrib["hasAudio"] == "0"


# ─── 12·13. 시작 타임코드와 신원 ─────────────────────────────────────────


def test_asset_and_clip_start_come_from_the_drop_frame_timecode() -> None:
    root = parsed(real_like_document())
    asset = root.find("./resources/asset")
    assert asset is not None
    assert asset.attrib == {
        "id": "a1",
        "name": "DJI_20260811125046_0008_D",
        "uid": ID_0008,
        "start": "1397678282/30000s",
        "duration": "1169168/30000s",
        "hasVideo": "1",
        "format": "r1",
        "videoSources": "1",
        "hasAudio": "1",
        "audioSources": "1",
        "audioChannels": "2",
        "audioRate": "48000",
    }
    first = spine_clips(root)[0]
    assert first.attrib == {
        "ref": "a1",
        "offset": "0s",
        "name": "DJI_20260811125046_0008_D",
        "start": "1397678282/30000s",
        "duration": "600600/30000s",
        "format": "r1",
        "tcFormat": "DF",
        "audioRole": "dialogue",
    }
    (caption,) = first.findall("caption")
    assert caption.attrib["offset"] == "1397708312/30000s"
    assert caption.attrib["duration"] == "45045/30000s"
    ndf = ft.SourceInput(media=media("ndf", frames=10, tc=0, drop=False, ident=None))
    (clip,) = spine_clips(parsed(ft.plan([ndf], title="t", library_bundle=None)))
    assert clip.attrib["tcFormat"] == "NDF" and clip.attrib["start"] == "0s"


def test_drop_frame_on_a_rate_without_drop_frame_is_refused() -> None:
    bad = ft.SourceInput(media=media("pal_df", frames=10, fd=F(1, 25), drop=True, ident=None))
    with pytest.raises(ft.FinalCutTimelineError, match="pal_df"):
        ft.plan_source(bad)


def test_library_media_uses_its_identifier_and_others_get_a_prefixed_lowercase_uid() -> None:
    root = parsed(real_like_document())
    assets = root.findall("./resources/asset")
    library_asset = assets[0]
    rep = library_asset.find("media-rep")
    assert rep is not None
    assert library_asset.attrib["uid"] == rep.attrib["sig"] == ID_0008
    assert rep.attrib["kind"] == "original-media"
    assert rep.attrib["src"] == (
        "file:///Users/ssojux2/Movies/test_dir/260810_wangbang.fcpbundle/clips/"
        "Original%20Media/DJI_20260811125046_0008_D.mov"
    )
    for asset, name in zip(
        assets[1:], ("DJI_20260811131238_0014_D", "DJI_20260811153216_0019_D"), strict=True
    ):
        sha = hashlib.sha256(name.encode()).hexdigest()
        assert asset.attrib["uid"] == f"com.vlogsrt.source.{sha}"
        assert not re.fullmatch(r"[0-9A-F]+", asset.attrib["uid"])  # Apple이 FCP용으로 예약
        rep = asset.find("media-rep")
        assert rep is not None and "sig" not in rep.attrib


@pytest.mark.parametrize("ident", ["52889be5f969239b5ac1e14037397bba", "52889BE5", "X" * 32, ""])
def test_malformed_library_identifier_is_refused(ident: str) -> None:
    with pytest.raises(ft.FinalCutTimelineError, match="ident_bad"):
        ft.plan_source(ft.SourceInput(media=media("ident_bad", frames=10, ident=ident)))


def test_relative_media_path_is_refused() -> None:
    source = ft.SourceInput(media=media("rel", frames=10, ident=None, folder=Path("clips")))
    with pytest.raises(ft.FinalCutTimelineError, match="rel"):
        ft.plan_source(source)


def test_text_xml_cannot_carry_is_refused_naming_the_source_not_rewritten() -> None:
    source = ft.SourceInput(
        media=media("ctl", frames=60, ident=None),
        cues=(ft.Cue(F(3, 2), F(2), "벨\x07소리"),),
    )
    with pytest.raises(ft.FinalCutTimelineError) as caught:
        ft.plan_source(source)
    # 보이지 않는 문자다 — 사람이 찾아갈 수 있게 원본과 큐 시각을 함께 말한다.
    assert "ctl" in str(caught.value) and "00:00:01,500" in str(caught.value)
    with pytest.raises(ft.FinalCutTimelineError):
        ft.plan_source(ft.SourceInput(media=media("bad\x0bname", frames=10, ident=None)))


# ─── 14. import-options ─────────────────────────────────────────────────


def test_import_options_link_media_and_point_at_the_original_library() -> None:
    """번들 URI는 디렉터리 꼴(끝 `/`)이다 — Final Cut 자신의 내보내기가 그렇게 적는다
    (이 기기 실물: `<library location="file:///Volumes/SubSSD/EU.fcpbundle/">`)."""
    root = parsed(real_like_document())
    uri = "file:///Users/ssojux2/Movies/test_dir/260810_wangbang.fcpbundle/"
    options = [o.attrib for o in root.findall("./import-options/option")]
    assert options == [
        {"key": "library location", "value": uri},
        {"key": "copy assets", "value": "0"},
        {"key": "suppress warnings", "value": "0"},
    ]
    library = root.find("library")
    assert library is not None and library.attrib == {"location": uri}


def test_without_a_library_the_options_still_link_but_name_no_location() -> None:
    loose = ft.SourceInput(media=media("loose", frames=10, ident=None, folder=Path("/tmp/x")))
    root = parsed(ft.plan([loose], title="t", library_bundle=None))
    options = [o.attrib for o in root.findall("./import-options/option")]
    assert options == [
        {"key": "copy assets", "value": "0"},
        {"key": "suppress warnings", "value": "0"},
    ]
    library = root.find("library")
    assert library is not None and library.attrib == {}


# ─── 15·16. 결정성과 키워드 철자 ─────────────────────────────────────────


def test_same_input_gives_byte_identical_output() -> None:
    first = ft.render(real_like_document())
    assert first == ft.render(real_like_document())
    assert real_like_document() == real_like_document()


def test_keywords_never_contain_a_comma_and_the_w15_hook_is_not_wired() -> None:
    labels = (
        ft.KEYWORD_RECOMMENDED,
        ft.KEYWORD_NORMAL,
        ft.KEYWORD_SUGGESTED_EXCLUSION,
        ft.KEYWORD_HOLD,
        ft.KEYWORD_UNANALYZED,
        ft.KEYWORD_UNREVIEWED,
        ft.KEYWORD_CONFIRMED_EXCLUSION,
        ft.KEYWORD_CAPTION_TIMING,
    )
    assert labels == (
        "추천", "보통", "제외 제안", "판정 보류", "미분석", "제외 제안(미검토)", "제외 확정",
        "자막 시각 확인 필요",
    )  # fmt: skip
    assert labels == ft.KEYWORDS
    assert not any("," in label for label in labels)
    values = [k.attrib["value"] for k in parsed(real_like_document()).iter("keyword")]
    assert values and not any("," in value for value in values)
    assert ft.KEYWORD_CAPTION_TIMING not in values


# ─── 보고와 이름 ─────────────────────────────────────────────────────────


def test_document_counts_summarise_what_was_sent() -> None:
    counts = ft.document_counts(real_like_document())
    assert counts["sources"] == 3 and counts["analysed"] == 2
    assert counts["spine_clips"] == 6
    assert counts["disabled_clips"] == 1 and counts["unreviewed_clips"] == 1
    assert counts["frames"] == 1168 + 604 + 1064
    assert counts["keywords"] == {
        "보통": 2, "추천": 1, "제외 확정": 1, "제외 제안": 1, "제외 제안(미검토)": 1,
        "판정 보류": 1, "미분석": 1,
    }  # fmt: skip
    assert counts["browser_ranges"] == 5 and counts["favorites"] == 1
    assert counts["captions"] == {
        "cues": 2, "placed": 2, "pieces": 3, "empty": 0, "widened": 0, "inverted": 0,
        "overlaps": 0, "line_issues": 0, "tails": 0, "outside": 0, "split": 1,
    }  # fmt: skip


def test_export_title_names_workspace_and_minute() -> None:
    moment = datetime(2026, 10, 9, 18, 30, 59)
    assert ft.export_title("260810_wangbang", moment) == (
        "vlogsrt · 260810_wangbang · 2026-10-09 18:30"
    )


def test_empty_source_list_and_blank_title_are_refused() -> None:
    with pytest.raises(ft.FinalCutTimelineError):
        ft.plan([], title="t", library_bundle=None)
    with pytest.raises(ft.FinalCutTimelineError):
        ft.plan(real_like_sources(), title=" ", library_bundle=None)


# ─── 순수성: 새 프로세스에서 import해도 ffmpeg 경계가 따라오지 않는다 ─────


def test_importing_the_builder_loads_no_process_or_media_boundary() -> None:
    """직접 import 검사는 `clips`처럼 한 다리 건넌 경계를 못 본다 — 실제 모듈 표를 본다."""
    probe = (
        "import json, sys\n"
        "import vlogsrt.project.raw.finalcut_timeline as built\n"
        "names = ('vlogsrt.preprocess', 'vlogsrt.project.media', 'vlogsrt.project.clips',"
        " 'subprocess')\n"
        "print(json.dumps({'file': built.__file__,"
        " 'loaded': [name for name in names if name in sys.modules]}))\n"
    )
    environment = {**os.environ, "PYTHONPATH": str(ROOT)}
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    report = json.loads(completed.stdout)
    assert Path(report["file"]).is_relative_to(ROOT)
    assert report["loaded"] == []


def test_builder_source_spells_invisible_characters_as_escapes() -> None:
    """정규식 안의 보이지 않는 문자(사용 영역·미할당·제어 문자)는 리뷰에서 읽을 수 없다."""
    source = (ROOT / "vlogsrt" / "project" / "raw" / "finalcut_timeline.py").read_text("utf-8")
    hidden = [
        (number, hex(ord(char)))
        for number, line in enumerate(source.splitlines(), start=1)
        for char in line
        if unicodedata.category(char) in {"Cc", "Cf", "Co", "Cs", "Cn"} or char == "\ufffd"
    ]
    assert hidden == []
