"""논리 슬라이스 도출 — 원본 칼날을 잃지 않는 이웃 합침 (P4-01 계약).

계획("Boundary transcripts" ＋ "Source analysis, clocks, identity")의 정본:

- **시계는 XML 그대로다.** 파일 시각은 `clip.start - asset.start`, 입력 경과는
  `clip.offset - sequence.tcStart`. 전체 사상
  `elapsed = clip.offset - tcStart + asset.start + file_time - clip.start`이
  클립 양 끝점에서 정확히 왕복한다.
- **가청은 저작 상태만 본다.** `media.has_audio and clip.enabled and
  clip.src_enable == "all"` — 전사·PCM·VAD의 이후 상태는 이 함수의 입력에 없다.
- **합침은 이웃 판정이다.** 같은 자원·내용·원점, 파일·타임라인 이어짐, 같은
  가청 정책일 때만 합친다. 자원 신원이 다르면 내용이 같아도 합치지 않고,
  갭은 합침을 끊으며 슬라이스가 되지 않는다. 칼날은 합친 뒤에도 전부 남는다.

합성 XML과 손 미디어만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.boundary import AuthoredBlade, LogicalSlice, logical_slices
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.timebase import SUPPORTED_FRAME_DURATIONS, format_fcpx_time

ASSET_START = "2500/25s"
"""애셋 원점 — 0이 아닌 에포크를 전제로 파일 시계의 뺄셈을 시험한다."""

CLIP_START = "2600/25s"
"""기본 클립 인점 — 원점에서 프레임 100개(4초) 뒤다."""

CLIP_DURATION = "50/25s"
"""기본 클립 길이 — 프레임 50개(2초)."""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어
# ─────────────────────────────────────────────────────────────


def _document(
    tmp_path: Path,
    assets: list[str],
    items: list[str],
    *,
    frame: str = "1/25s",
    tc_start: str = "0s",
    sequence_duration: str = "600s",
) -> ProjectDocument:
    fmt = (
        f'<format id="r1" name="FFVideoFormat1080p25" frameDuration="{frame}"'
        ' width="1920" height="1080"/>'
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        + fmt
        + "\n    "
        + "\n    ".join(assets)
        + '\n  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        f'        <sequence format="r1" tcStart="{tc_start}" duration="{sequence_duration}">\n'
        "          <spine>\n            "
        + "\n            ".join(items)
        + "\n          </spine>\n        </sequence>\n"
        "      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "project.fcpxml"
    path.write_text(body, encoding="utf-8")
    return parse_project(path)


def _asset_xml(
    asset_id: str,
    *,
    start: str = ASSET_START,
    duration: str = "500/25s",
    has_audio: str = "1",
    name: str | None = None,
) -> str:
    return (
        f'<asset id="{asset_id}" name="{name or f"{asset_id}.mp4"}" uid="AAAA" start="{start}"'
        f' duration="{duration}" hasAudio="{has_audio}" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file:///media/{asset_id}.mp4"/></asset>'
    )


def _clip_xml(
    ref: str,
    *,
    offset: str = "0s",
    start: str = CLIP_START,
    duration: str = CLIP_DURATION,
    enabled: str | None = None,
    src_enable: str | None = None,
) -> str:
    extra = ""
    if enabled is not None:
        extra += f' enabled="{enabled}"'
    if src_enable is not None:
        extra += f' srcEnable="{src_enable}"'
    return (
        f'<asset-clip ref="{ref}" name="{ref}" offset="{offset}" start="{start}"'
        f' duration="{duration}"{extra}/>'
    )


def _gap_xml(offset: str, duration: str) -> str:
    return f'<gap name="갭" offset="{offset}" start="0s" duration="{duration}"/>'


def _media(
    document: ProjectDocument,
    xml_id: str,
    *,
    content_id: str | None = None,
    has_audio: bool | None = None,
) -> ResolvedMedia:
    resource = next(item for item in document.resources if item.xml_id == xml_id)
    original = Path(f"/media/{xml_id}.mp4")
    return ResolvedMedia(
        resource_scoped_id=resource.scoped_id,
        asset_content_id=content_id or f"acid-{xml_id}",
        original=original,
        image_source=original,
        original_sha256=f"{xml_id}-sha",
        has_audio=resource.has_audio if has_audio is None else has_audio,
        used_proxy=False,
    )


def _frames(frame: Fraction, count: int) -> str:
    """프레임 수를 FCPXML 시간 문자열로 — 격자 정합 시각을 만드는 유일한 재료."""
    return format_fcpx_time(frame * count)


def _clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


# ─────────────────────────────────────────────────────────────
#  시계 — 공식이 양 끝점에서 정확히 왕복한다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("frame", sorted(SUPPORTED_FRAME_DURATIONS))
def test_clock_formula_roundtrips_at_both_endpoints_for_every_supported_fps(
    tmp_path: Path, frame: Fraction
) -> None:
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", start=_frames(frame, 2500), duration=_frames(frame, 500))],
        items=[
            _clip_xml(
                "r2",
                offset=_frames(frame, 400),
                start=_frames(frame, 2600),
                duration=_frames(frame, 50),
            )
        ],
        frame=format_fcpx_time(frame),
        tc_start=_frames(frame, 300),
        sequence_duration=_frames(frame, 2000),
    )
    resource = document.resources[0]
    (clip,) = _clips(document)
    (slice_,) = logical_slices(document, [_media(document, "r2")])

    def elapsed(file_time: Fraction) -> Fraction:
        return clip.offset - document.sequence_tc_start + resource.start + file_time - clip.start

    assert slice_.source_start == clip.start - resource.start
    assert slice_.source_end == clip.start + clip.duration - resource.start
    assert slice_.timeline_start == clip.offset - document.sequence_tc_start
    assert slice_.timeline_end == clip.offset + clip.duration - document.sequence_tc_start
    assert slice_.asset_start == resource.start
    # file_time은 파일 시계(원점 0)다 — 클립 인점·아웃점에서 양 끝이 정확히 왕복한다
    assert elapsed(slice_.source_start) == slice_.timeline_start
    assert elapsed(slice_.source_end) == slice_.timeline_end


def test_nonzero_asset_start_epoch_is_the_resource_epoch(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2", start=ASSET_START)])
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    assert slice_.asset_start == document.resources[0].start == Fraction(100)
    # 원점에서 바로 시작하는 클립의 파일 시각은 0이다 — 에포크는 빼기 때문
    assert slice_.source_start == 0
    assert slice_.blades[0].source_time == 0
    assert slice_.blades[0].timeline_time == 0


# ─────────────────────────────────────────────────────────────
#  신원 — 자원·내용·인스턴스가 저마다 다르다
# ─────────────────────────────────────────────────────────────


def test_resource_and_content_identity_join_exactly(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2")])
    (clip,) = _clips(document)
    (slice_,) = logical_slices(document, [_media(document, "r2", content_id="acid-r2")])
    assert slice_.resource_scoped_id == document.resources[0].scoped_id
    assert slice_.resource_scoped_id == clip.resource_scoped_id
    assert slice_.source_id == "acid-r2"


def test_repeated_instances_of_one_asset_stay_distinct(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="200/25s"),
        ],
    )
    first, second = logical_slices(document, [_media(document, "r2")])
    assert first.source_start == second.source_start
    assert first.source_end == second.source_end
    assert first.resource_scoped_id == second.resource_scoped_id
    assert first.source_id == second.source_id
    assert first.instance_ids != second.instance_ids
    assert first.slice_id != second.slice_id
    assert len(first.blades) == len(second.blades) == 1


def test_slice_ids_are_deterministic_and_unique(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="50/25s", start="2650/25s"),
            _clip_xml("r2", offset="200/25s"),
        ],
    )
    media = [_media(document, "r2")]
    slices = logical_slices(document, media)
    assert logical_slices(document, media) == slices
    ids = [item.slice_id for item in slices]
    assert len(set(ids)) == len(ids)


# ─────────────────────────────────────────────────────────────
#  합침 — 이웃 판정의 여섯 조건
# ─────────────────────────────────────────────────────────────


def test_continuous_blades_coalesce_and_keep_every_authored_blade(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="50/25s", start="2650/25s"),
            _clip_xml("r2", offset="100/25s", start="2700/25s"),
        ],
    )
    resource = document.resources[0]
    first, _, last = _clips(document)
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    # 이어진 칼날 셋은 슬라이스 하나다 — 경계 작업이 볼 단위가 하나뿐이다
    assert slice_.instance_ids == tuple(clip.instance_id for clip in _clips(document))
    assert slice_.source_start == first.start - resource.start
    assert slice_.source_end == last.start + last.duration - resource.start
    assert slice_.timeline_start == 0
    assert slice_.timeline_end == Fraction(150, 25)
    assert isinstance(slice_.blades[0], AuthoredBlade)
    source_times = [Fraction(4), Fraction(6), Fraction(8)]
    timeline_times = [Fraction(0), Fraction(2), Fraction(4)]
    assert [blade.source_time for blade in slice_.blades] == source_times
    assert [blade.timeline_time for blade in slice_.blades] == timeline_times
    assert [blade.instance_id for blade in slice_.blades] == list(slice_.instance_ids)


def test_resource_boundary_prevents_coalescing_across_equal_content(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r3", offset="50/25s", start="2650/25s"),
        ],
    )
    media = [
        _media(document, "r2", content_id="same-bytes"),
        _media(document, "r3", content_id="same-bytes"),
    ]
    first, second = logical_slices(document, media)
    # 내용이 같아도 자원 신원이 다르면 합치지 않는다
    assert first.source_id == second.source_id
    assert first.resource_scoped_id != second.resource_scoped_id
    assert len(first.blades) == len(second.blades) == 1


def test_source_discontinuity_prevents_coalescing(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="50/25s", start="2750/25s"),
        ],
    )
    first, second = logical_slices(document, [_media(document, "r2")])
    # 타임라인은 이어져도 파일 시계가 건너뛰면 두 슬라이스다
    assert second.timeline_start == first.timeline_end
    assert first.source_end != second.source_start
    assert len(first.blades) == len(second.blades) == 1


def test_timeline_discontinuity_prevents_coalescing(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="100/25s", start="2650/25s"),
        ],
    )
    first, second = logical_slices(document, [_media(document, "r2")])
    # 파일 시계는 이어져도 타임라인에 구멍이 있으면 두 슬라이스다
    assert second.source_start == first.source_end
    assert second.timeline_start != first.timeline_end
    assert len(first.blades) == len(second.blades) == 1


def test_gap_breaks_coalescing_and_is_not_a_slice(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _gap_xml("50/25s", "50/25s"),
            _clip_xml("r2", offset="100/25s", start="2650/25s"),
        ],
    )
    first, second = logical_slices(document, [_media(document, "r2")])
    # 갭은 합침을 끊고, 갭 자신은 슬라이스가 되지 않는다 — 문서가 표현한다
    assert second.source_start == first.source_end
    assert len(first.blades) == len(second.blades) == 1


def test_audible_policy_mismatch_prevents_coalescing(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="50/25s", start="2650/25s", enabled="0"),
        ],
    )
    first, second = logical_slices(document, [_media(document, "r2")])
    assert first.audible is True
    assert second.audible is False
    assert len(first.blades) == len(second.blades) == 1


# ─────────────────────────────────────────────────────────────
#  가청 — 저작 상태만 본다
# ─────────────────────────────────────────────────────────────


def test_disabled_clip_retains_identity_and_blades(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2", enabled="0")])
    (clip,) = _clips(document)
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    assert slice_.audible is False
    assert slice_.instance_ids == (clip.instance_id,)
    assert slice_.blades[0].instance_id == clip.instance_id


def test_video_only_clip_retains_identity_and_blades(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2", src_enable="video")])
    (clip,) = _clips(document)
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    assert slice_.audible is False
    assert slice_.instance_ids == (clip.instance_id,)


def test_declared_no_audio_resource_retains_identity_and_blades(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2", has_audio="0")], [_clip_xml("r2")])
    (clip,) = _clips(document)
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    assert slice_.audible is False
    assert slice_.resource_scoped_id == clip.resource_scoped_id
    assert slice_.blades[0].source_time == clip.start - document.resources[0].start


def test_audibility_is_authored_state_only(tmp_path: Path) -> None:
    # 전사·PCM·VAD의 상태는 이 함수의 입력에 아예 없다 — 계약을 기계로 못박는다
    assert tuple(inspect.signature(logical_slices).parameters) == ("document", "media")
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2")])
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    # 분석 어디에도 없는 선언된 오디오 — 나중에 PCM이 없어도 가청이다
    assert slice_.audible is True


# ─────────────────────────────────────────────────────────────
#  내용 신원 — 같은 내용, 다른 자원
# ─────────────────────────────────────────────────────────────


def test_same_filename_different_content_stay_distinct(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2", name="vacation.mp4"), _asset_xml("r3", name="vacation.mp4")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r3", offset="50/25s", start="2650/25s"),
        ],
    )
    media = [_media(document, "r2"), _media(document, "r3")]
    first, second = logical_slices(document, media)
    # 이름이 같아도 내용이 다르면 신원이 다르다
    assert first.source_id != second.source_id
    assert first.resource_scoped_id != second.resource_scoped_id
    assert len(first.blades) == len(second.blades) == 1


def test_same_content_equal_no_audio_declarations_stay_distinct(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2", has_audio="0"), _asset_xml("r3", has_audio="0")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r3", offset="50/25s", start="2650/25s"),
        ],
    )
    media = [
        _media(document, "r2", content_id="same-bytes"),
        _media(document, "r3", content_id="same-bytes"),
    ]
    first, second = logical_slices(document, media)
    # 내용·가청 정책이 같아도 자원 신원이 다르면 합치지 않는다
    assert first.source_id == second.source_id
    assert first.audible is False
    assert second.audible is False
    assert first.resource_scoped_id != second.resource_scoped_id


def test_same_content_different_audio_declarations_keep_authored_audibility(
    tmp_path: Path,
) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2", has_audio="1"), _asset_xml("r3", has_audio="0")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r3", offset="50/25s", start="2650/25s"),
        ],
    )
    media = [
        _media(document, "r2", content_id="same-bytes"),
        _media(document, "r3", content_id="same-bytes"),
    ]
    first, second = logical_slices(document, media)
    # 같은 내용 위에 가청·불가청이 나란히 살아도 서로를 지우지 않는다
    assert first.source_id == second.source_id
    assert first.audible is True
    assert second.audible is False
    assert len(first.blades) == len(second.blades) == 1


# ─────────────────────────────────────────────────────────────
#  묶음 — 자원 조인의 거부와 최소성
# ─────────────────────────────────────────────────────────────


def test_media_join_is_by_resource_identity_not_row_order(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3", has_audio="0"), _asset_xml("r4")],
        [
            _clip_xml("r3", offset="0s"),
            _clip_xml("r2", offset="100/25s", start="2650/25s"),
            _clip_xml("r4", offset="200/25s", start="2700/25s"),
            _clip_xml("r3", offset="300/25s", start="2750/25s"),
        ],
    )
    # 미디어 행 순서를 자원 순서(r2,r3,r4)도 스토리라인 첫 등장 순서(r3,r2,r4)도
    # 아닌 순서로 섞는다 — 조인이 위치(zip)로 묶이면 source_id·가청이 뒤섞인다
    shuffled = [_media(document, "r4"), _media(document, "r2"), _media(document, "r3")]
    appearance = [clip.resource_scoped_id for clip in _clips(document)]
    assert [row.resource_scoped_id for row in shuffled] != [
        resource.scoped_id for resource in document.resources
    ]
    assert [row.resource_scoped_id for row in shuffled] != appearance
    content_by_scoped = {row.resource_scoped_id: row.asset_content_id for row in shuffled}
    assert len(set(content_by_scoped.values())) == len(shuffled)

    slices = logical_slices(document, shuffled)
    assert len(slices) == len(_clips(document)) == 4
    for slice_, clip in zip(slices, _clips(document), strict=True):
        # 슬라이스마다 자원 신원과 그 자원의 정확한 내용 신원을 가져야 한다
        assert slice_.resource_scoped_id == clip.resource_scoped_id
        assert slice_.source_id == content_by_scoped[clip.resource_scoped_id]
        assert len(slice_.blades) == 1
    # 섞인 행이 가청까지 뒤섞지 않는다 — 선언된 무음은 r3뿐이다
    assert [slice_.audible for slice_ in slices] == [False, True, True, False]


def test_media_for_unknown_resource_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2")])
    bogus = replace(_media(document, "r2"), resource_scoped_id="문서-밖-자원")
    with pytest.raises(ValueError, match="문서에 없는 자원"):
        logical_slices(document, [bogus])


def test_duplicate_media_rejected(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2")])
    with pytest.raises(ValueError, match="미디어 자원이 중복이다"):
        logical_slices(document, [_media(document, "r2"), _media(document, "r2")])


def test_media_missing_for_referenced_resource_rejected(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r3", offset="50/25s", start="2650/25s"),
        ],
    )
    with pytest.raises(ValueError, match="미디어가 없다"):
        logical_slices(document, [_media(document, "r2")])


def test_media_audio_declaration_must_match_document(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2")])
    contradictory = _media(document, "r2", has_audio=False)
    with pytest.raises(ValueError, match="가청 선언이 문서와 다르다"):
        logical_slices(document, [contradictory])


def test_unused_resource_needs_no_media(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [_clip_xml("r2")],
    )
    # 클립이 가리키지 않는 자원은 슬라이스를 만들지 않는다 — 해석도 강제하지 않는다
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    assert slice_.instance_ids == (_clips(document)[0].instance_id,)


def test_empty_storyline_yields_no_slices(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [])
    assert logical_slices(document, [_media(document, "r2")]) == ()


# ─────────────────────────────────────────────────────────────
#  순서와 불변 — 문서 순서 그대로, 기록은 얼어 있다
# ─────────────────────────────────────────────────────────────


def test_slices_follow_storyline_order(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        [_asset_xml("r2"), _asset_xml("r3")],
        [
            _clip_xml("r2", offset="0s"),
            _clip_xml("r2", offset="50/25s", start="2650/25s"),
            _clip_xml("r3", offset="100/25s", start="2600/25s"),
            _clip_xml("r2", offset="150/25s", start="2700/25s"),
        ],
    )
    slices = logical_slices(document, [_media(document, "r2"), _media(document, "r3")])
    starts = [item.timeline_start for item in slices]
    assert starts == sorted(starts)
    assert [len(item.blades) for item in slices] == [2, 1, 1]


def test_records_are_frozen(tmp_path: Path) -> None:
    document = _document(tmp_path, [_asset_xml("r2")], [_clip_xml("r2")])
    (slice_,) = logical_slices(document, [_media(document, "r2")])
    assert isinstance(slice_, LogicalSlice)
    with pytest.raises(FrozenInstanceError):
        slice_.audible = False  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        slice_.blades[0].source_time = Fraction(1)  # type: ignore[misc]
