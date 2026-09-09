"""소스 큐의 입력 타임라인 SOURCE SRT 투영 — P3-03 계약.

계획("Source analysis, clocks, identity" ＋ "Input and output")의 정본:

- **큐의 정본은 P3-02의 정제된 큐다.** 시각은 `Fraction(str(끝점))`으로만 옮기고
  다듬지 않는다. 원점 `pcm_sample0_file_time`은 배치에서 **정확히 한 번** 더해
  파일 시계가 되고, XML 공식 `clip.offset - tcStart + asset.start + file_time -
  clip.start`이 입력 타임라인 경과 시각을 만든다.
- **밀리초 양자화는 P1 timebase가 직렬화 순간에 딱 한 번** 한다. 정확 양수 길이가
  양자화 뒤 0이 되는 큐(subms)는 직렬화가 거부하고, 조립은 증거를 지우지 않는다 —
  깃발 `subms_collapsed`·미해결 id·SRT에서는 그 장만 뺀다.
- **되풀이 인스턴스는 같은 소스 큐를 한 번씩 재생산한다.** 음소거·비활성 인스턴스와
  갭은 말을 내지 않는다. 자원 스코프 unavailable은 내용 중복 제거보다 먼저 붙는다.

합성 XML·손로 만든 트레이스만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError, dataclass
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.models import Cue
from vlogsrt.project import captions
from vlogsrt.project.captions import (
    BOUNDARY_REQUIRED,
    SUBMS_COLLAPSED,
    PlacedSourceCue,
    SourceCue,
    project_source_captions,
    serialize_srt,
    source_cues,
)
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.speech import (
    SourceAnalysis,
    SourceSegment,
    SourceTrace,
    UnavailableSourceAudio,
)
from vlogsrt.project.timebase import milliseconds_half_up

NTSC = "1001/30000s"


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어·손 트레이스
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
    start: str = "0s",
    duration: str = "600s",
    has_audio: str = "1",
) -> str:
    return (
        f'<asset id="{asset_id}" name="{asset_id}.mp4" uid="AAAA" start="{start}"'
        f' duration="{duration}" hasAudio="{has_audio}" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file:///media/{asset_id}.mp4"/></asset>'
    )


def _clip_xml(
    ref: str,
    *,
    offset: str = "0s",
    start: str = "0s",
    duration: str = "100/25s",
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
    sha: str | None = None,
) -> ResolvedMedia:
    resource = next(item for item in document.resources if item.xml_id == xml_id)
    original = Path(f"/media/{xml_id}.mp4")
    return ResolvedMedia(
        resource_scoped_id=resource.scoped_id,
        asset_content_id=content_id or f"acid-{xml_id}",
        original=original,
        image_source=original,
        original_sha256=sha or f"{xml_id}-sha",
        has_audio=resource.has_audio if has_audio is None else has_audio,
        used_proxy=False,
    )


@dataclass
class _CueSpec:
    start: float
    end: float
    lines: tuple[str, ...] = ("자막",)
    seg_ids: tuple[int, ...] = (0,)
    flags: tuple[str, ...] = ()
    index: int = 0  # 0이면 목록 순서로 1부터 매긴다


def _segment(source_id: str, position: int, origin: Fraction) -> SourceSegment:
    """골격 세그먼트 — 시각은 큐와 일부러 다르다. 투영의 정본이 큐임을 증명하는 재료."""
    audio_start = Fraction(position) + Fraction(9, 10)
    return SourceSegment(
        segment_id=f"{source_id}-seg-{position}",
        audio_start=audio_start,
        audio_end=audio_start + Fraction(9, 10),
        file_start=audio_start + origin,
        file_end=audio_start + Fraction(9, 10) + origin,
        text=f"골격 {position}",
        compression_ratio=1.0,
        avg_logprob=-0.1,
        no_speech_prob=0.0,
    )


def _trace(
    source_id: str,
    *,
    cues: list[_CueSpec] | None = None,
    origin: Fraction = Fraction(0),
    status: str = "success",
    spine_count: int | None = None,
    analysis_key: str | None = None,
    error: str | None = None,
) -> SourceTrace:
    specs = cues or []
    needed = max((max(spec.seg_ids, default=0) for spec in specs), default=0) + 1
    # 빈 성공은 골격도 없다 — needed 계산이 빈 목록에서 1을 만들지 않게 한다.
    spine_n = (needed if specs else 0) if spine_count is None else spine_count
    published = status in ("success", "empty")
    return SourceTrace(
        variant="source_trace",
        source_id=source_id,
        original_stem="demo",
        analysis_key=analysis_key or f"key-{source_id}",
        pcm_analysis_key=f"pcm-{source_id}",
        pcm_sample0_file_time=origin,
        status=status,  # type: ignore[arg-type]
        draft=(),
        settled_spine=tuple(_segment(source_id, i, origin) for i in range(spine_n)),
        repairs=(),
        cues=tuple(
            Cue(
                start=spec.start,
                end=spec.end,
                lines=spec.lines,
                seg_ids=spec.seg_ids,
                flags=spec.flags,
                index=spec.index or position,
            )
            for position, spec in enumerate(specs, start=1)
        ),
        coverage=(),
        model="fixture-model",
        config_sha256="c" * 64,
        source_srt_relative_path=f"work/{source_id}/demo.SOURCE.srt" if published else None,
        source_srt_sha256="d" * 64 if published else None,
        error=error if error is not None else ("준비된 실패" if status == "error" else None),
    )


def _unavailable(
    resource_scoped_id: str,
    source_id: str,
    sha: str,
    *,
    status: str = "no_audio",
    reason_code: str | None = None,
) -> UnavailableSourceAudio:
    return UnavailableSourceAudio(
        variant="unavailable_source_audio",
        resource_scoped_id=resource_scoped_id,
        source_id=source_id,
        original_sha256=sha,
        status=status,  # type: ignore[arg-type]
        reason_code=reason_code
        or ("declared_no_audio" if status == "no_audio" else "stream_unavailable"),  # type: ignore[arg-type]
        reason="테스트용 기록",
    )


def _main_scenario(
    tmp_path: Path,
) -> tuple[ProjectDocument, list[ResolvedMedia], list[SourceAnalysis]]:
    """NTSC 격자·반복 인스턴스·비활성 클립·갭·두 소스·원점 64ms."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2"), _asset_xml("r3")],
        items=[
            _clip_xml("r2", offset="0s", start="3003/30000s", duration="300300/30000s"),
            _clip_xml(
                "r2",
                offset="300300/30000s",
                start="60060/30000s",
                duration="300300/30000s",
                enabled="0",
            ),
            _gap_xml("600600/30000s", "3003/30000s"),
            _clip_xml("r3", offset="630630/30000s", start="0s", duration="300300/30000s"),
        ],
        frame=NTSC,
        sequence_duration="1201200/30000s",
    )
    trace_a = _trace(
        "acid-r2",
        cues=[
            _CueSpec(0.5, 1.5, lines=("첫마디",), flags=("polished",)),
            _CueSpec(1.5, 2.5, lines=("둘째마디",), seg_ids=(1,)),
        ],
        origin=Fraction(64, 1000),
    )
    trace_b = _trace("acid-r3", cues=[_CueSpec(0.25, 1.0, lines=("B소스 마디",))])
    media = [_media(document, "r2"), _media(document, "r3")]
    return document, media, [trace_a, trace_b]


# ─────────────────────────────────────────────────────────────
#  조립 — 정확 배치·재생산·순서 독립
# ─────────────────────────────────────────────────────────────


def test_places_cues_on_input_timeline_with_exact_fractions(tmp_path: Path) -> None:
    """원점을 한 번 더하고 XML 공식을 정확히 적용한다 — NTSC 격자에서도 오차 0."""
    document, media, analyses = _main_scenario(tmp_path)
    result = project_source_captions(document, media, analyses)

    assert result.input_sha256 == document.input_sha256
    clip1, clip2 = document.storyline[0], document.storyline[3]
    assert isinstance(clip1, ClipInstance) and isinstance(clip2, ClipInstance)
    trace_a = analyses[0]
    assert isinstance(trace_a, SourceTrace)
    source_a = source_cues(trace_a)
    assert len(result.placed_cues) == 3

    first, second, third = result.placed_cues
    assert (first.cue_id, second.cue_id) == (source_a[0].cue_id, source_a[1].cue_id)
    assert first.instance_id == clip1.instance_id
    assert first.source_start == Fraction(141, 250)  # 0.5초 + 원점 64ms — 파일 시계
    assert first.source_end == Fraction(391, 250)
    assert first.timeline_start == Fraction(4639, 10000)  # 0.564 - clip.start 0.1001
    assert first.timeline_end == Fraction(14639, 10000)
    assert first.text == "첫마디"
    assert first.flags == ("polished",)
    assert second.timeline_start == first.timeline_end  # 인접 경계는 정확히 같다
    assert third.source_id == "acid-r3"
    assert third.instance_id == clip2.instance_id
    assert third.timeline_start == Fraction(21271, 1000)  # 630630/30000 + 1/4
    assert third.timeline_end == Fraction(22021, 1000)
    assert result.boundary_instance_ids == ()
    assert result.unresolved_cue_ids == ()
    assert result.unavailable_audio == ()
    assert serialize_srt(result.placed_cues) == result.srt_text


def test_source_srt_bytes_are_deterministic_half_up(tmp_path: Path) -> None:
    """양자화는 직렬화에서 한 번 — 기대 문자열과 바이트까지 같다."""
    document, media, analyses = _main_scenario(tmp_path)
    result = project_source_captions(document, media, analyses)
    assert result.srt_text == (
        "1\n"
        "00:00:00,464 --> 00:00:01,464\n"
        "첫마디\n"
        "\n"
        "2\n"
        "00:00:01,464 --> 00:00:02,464\n"
        "둘째마디\n"
        "\n"
        "3\n"
        "00:00:21,271 --> 00:00:22,021\n"
        "B소스 마디\n"
    )


def test_source_cues_keep_provenance_text_and_flags(tmp_path: Path) -> None:
    """세그먼트 사상·글자·깃발은 트레이스 그대로 — 골격 시각을 따르지 않는다."""
    document, _media_list, analyses = _main_scenario(tmp_path)
    assert document.resources[0].xml_id == "r2"
    trace = analyses[0]
    assert isinstance(trace, SourceTrace)
    cues = source_cues(trace)
    assert len(cues) == 2
    assert cues[0].source_id == "acid-r2"
    assert cues[0].start == Fraction(1, 2)  # Fraction(str(0.5)) — 오디오 국소 0기반
    assert cues[0].end == Fraction(3, 2)
    assert cues[0].segment_ids == ("acid-r2-seg-0",)
    assert cues[1].segment_ids == ("acid-r2-seg-1",)
    assert cues[0].flags == ("polished",)


def test_multiline_cue_text_is_retained_verbatim(tmp_path: Path) -> None:
    """정제가 만든 줄바꿈은 글자 그대로 산다 — 다시 조판하지 않는다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2")],
        items=[_clip_xml("r2", offset="0s", start="0s", duration="100/25s")],
    )
    trace = _trace("acid-r2", cues=[_CueSpec(1.0, 2.0, lines=("두 줄", "자막"))])
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    assert result.source_cues[0].text == "두 줄\n자막"
    assert result.placed_cues[0].text == "두 줄\n자막"
    assert "두 줄\n자막" in result.srt_text


def test_shuffled_media_and_trace_orders_give_identical_result(tmp_path: Path) -> None:
    """묶음은 신원으로 한다 — 입력 순서를 뒤집어도 결과가 같다."""
    document, media, analyses = _main_scenario(tmp_path)
    ordered = project_source_captions(document, media, analyses)
    shuffled = project_source_captions(document, list(reversed(media)), list(reversed(analyses)))
    assert shuffled == ordered


def test_repeated_instance_reproduces_the_same_cue_identity(tmp_path: Path) -> None:
    """같은 소스 큐는 인스턴스마다 같은 cue_id로 한 번씩 재생산된다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", duration="600s")],
        items=[
            _clip_xml("r2", offset="0s", start="0s", duration="100/25s"),
            _clip_xml("r2", offset="100/25s", start="0s", duration="100/25s"),
        ],
    )
    trace = _trace("acid-r2", cues=[_CueSpec(1.0, 2.0, lines=("반복",))])
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    first, second = result.placed_cues
    assert first.cue_id == second.cue_id
    assert first.instance_id != second.instance_id
    assert first.source_start == second.source_start == Fraction(1)
    assert second.timeline_start - first.timeline_start == Fraction(100, 25)
    assert result.source_cues == source_cues(trace)


def test_records_are_frozen(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    result = project_source_captions(document, media, analyses)
    with pytest.raises(FrozenInstanceError):
        result.srt_text = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.placed_cues[0].text = "x"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.source_cues[0].text = "x"  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────
#  음소거·비활성·에포크·빈 성공
# ─────────────────────────────────────────────────────────────


def test_muted_and_disabled_instances_and_gaps_emit_no_speech(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2")],
        items=[
            _clip_xml("r2", offset="0s", duration="100/25s", src_enable="video"),
            _clip_xml("r2", offset="100/25s", duration="100/25s", enabled="0"),
            _gap_xml("200/25s", "100/25s"),
        ],
    )
    trace = _trace("acid-r2", cues=[_CueSpec(1.0, 2.0)])
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    assert result.placed_cues == ()
    assert result.srt_text == ""
    assert result.source_cues == source_cues(trace)  # 소스 기록은 그대로 주소 가능


def test_nonzero_epochs_negative_origin_and_empty_trace(tmp_path: Path) -> None:
    """tcStart·asset.start·clip.start가 모두 3600s여도 경과는 0기반이다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", start="90000/25s"), _asset_xml("r3")],
        items=[
            _clip_xml("r2", offset="90000/25s", start="90000/25s", duration="250/25s"),
            _clip_xml("r3", offset="90250/25s", start="0s", duration="100/25s"),
        ],
        tc_start="90000/25s",
    )
    trace = _trace(
        "acid-r2", cues=[_CueSpec(1.0, 2.0, lines=("에포크",))], origin=Fraction(-64, 1000)
    )
    empty = _trace("acid-r3", status="empty")
    result = project_source_captions(
        document, [_media(document, "r2"), _media(document, "r3")], [trace, empty]
    )
    (placed,) = result.placed_cues
    assert placed.timeline_start == Fraction(936, 1000)  # 1.0 - 0.064
    assert placed.timeline_end == Fraction(1936, 1000)
    assert result.source_cues == source_cues(trace)  # 빈 성공은 큐를 안 더한다
    assert result.srt_text == "1\n00:00:00,936 --> 00:00:01,936\n에포크\n"
    assert result.unavailable_audio == ()


# ─────────────────────────────────────────────────────────────
#  저변 잘림 — 발표만 묶고 원본 기록은 지키며 P4에 인스턴스를 남긴다
# ─────────────────────────────────────────────────────────────


def test_input_edge_clamp_flags_and_retains_source_record(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", duration="10s")],
        items=[_clip_xml("r2", offset="1/25s", start="1/25s", duration="100/25s")],
    )
    trace = _trace(
        "acid-r2",
        cues=[
            _CueSpec(0.01, 0.02, lines=("앞쪽",)),  # 클립 시작(0.04s) 앞 — 배치 안 함
            _CueSpec(0.02, 0.05, lines=("입구 잘림",)),  # 앞변에서 잘림
            _CueSpec(3.9, 4.1, lines=("출구 잘림",)),  # 뒷변(4.04s)에서 잘림
            _CueSpec(5.0, 6.0, lines=("뒤쪽",)),  # 클립 끝 뒤 — 배치 안 함
        ],
    )
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    (clip,) = document.storyline
    assert isinstance(clip, ClipInstance)
    entrance, exit_cut = result.placed_cues
    assert entrance.timeline_start == Fraction(1, 25)
    assert entrance.timeline_end == Fraction(1, 20)
    assert entrance.source_start == Fraction(1, 50)  # 원본 기록은 잘리지 않는다
    assert entrance.source_end == Fraction(1, 20)
    assert entrance.flags == (BOUNDARY_REQUIRED,)
    assert exit_cut.timeline_start == Fraction(39, 10)
    assert exit_cut.timeline_end == Fraction(101, 25)
    assert exit_cut.source_end == Fraction(41, 10)
    assert exit_cut.flags == (BOUNDARY_REQUIRED,)
    assert result.boundary_instance_ids == (clip.instance_id,)  # P4를 위한 인스턴스
    assert result.srt_text == (
        "1\n00:00:00,040 --> 00:00:00,050\n입구 잘림\n"
        "\n2\n00:00:03,900 --> 00:00:04,040\n출구 잘림\n"
    )


# ─────────────────────────────────────────────────────────────
#  밀리초 표현 가능성 — 정확 양수여도 반올림 뒤 0이 될 수 있다
# ─────────────────────────────────────────────────────────────


def test_exact_edge_collapses_under_direct_serialization() -> None:
    """133/8000s → +1/24000s. half-up 양쪽 다 17ms — 직렬화는 거부한다."""
    start = Fraction(16625, 1000000)
    end = start + Fraction(1, 24000)
    assert start == Fraction(133, 8000)
    assert (milliseconds_half_up(start), milliseconds_half_up(end)) == (17, 17)
    cue = SourceCue(
        cue_id="edge-17ms",
        source_id="acid-x",
        segment_ids=(),
        start=start,
        end=end,
        text="한 프레임도 못 미치는 말",
    )
    with pytest.raises(ValueError) as excinfo:
        serialize_srt([cue])
    message = str(excinfo.value)
    assert "edge-17ms" in message
    assert "133/8000" in message
    assert "1/60" in message


def test_subms_cue_collapses_in_builder_without_clamp(tmp_path: Path) -> None:
    """조립은 큐를 지우지 않는다 — 깃발·미해결 id·SRT에서 그 장만 뺀다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", duration="10s")],
        items=[_clip_xml("r2", offset="0s", start="0s", duration="100/25s")],
    )
    trace = _trace("acid-r2", cues=[_CueSpec(0.016625, 1.0 / 60, lines=("반올림에 눌린 말",))])
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    (placed,) = result.placed_cues
    assert placed.timeline_start == Fraction(133, 8000)
    assert placed.flags == (SUBMS_COLLAPSED,)
    assert result.unresolved_cue_ids == (placed.cue_id,)
    assert result.srt_text == ""  # 그 장만 뺀다 — 큐 자체는 남는다
    assert result.source_cues[0].flags == ()  # 원본 소스 큐는 무결하다


def test_authored_edge_clamp_can_cause_the_same_collapse(tmp_path: Path) -> None:
    """잘림이 표현 가능성을 깨뜨릴 수도 있다 — 잘림 깃발과 함께 남는다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", duration="10s")],
        items=[_clip_xml("r2", offset="1/25s", start="1/25s", duration="100/25s")],
    )
    trace = _trace("acid-r2", cues=[_CueSpec(0.03, 0.04002, lines=("경계에 걸친 말",))])
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    (placed,) = result.placed_cues
    assert placed.timeline_start == Fraction(1, 25)
    assert placed.timeline_end == Fraction(2001, 50000)
    assert placed.flags == (BOUNDARY_REQUIRED, SUBMS_COLLAPSED)
    assert result.unresolved_cue_ids == (placed.cue_id,)
    assert result.srt_text == ""


def test_tiny_cue_across_half_ms_boundary_is_representable(tmp_path: Path) -> None:
    """반 밀리초 경계를 건너는 아주 짧은 큐는 살아 나온다 — 17ms → 18ms."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", duration="10s")],
        items=[_clip_xml("r2", offset="0s", start="0s", duration="100/25s")],
    )
    trace = _trace("acid-r2", cues=[_CueSpec(0.0174999, 0.0175001, lines=("경계 건너",))])
    result = project_source_captions(document, [_media(document, "r2")], [trace])
    assert result.unresolved_cue_ids == ()
    assert result.srt_text == "1\n00:00:00,017 --> 00:00:00,018\n경계 건너\n"


def test_adjacent_clips_serialize_identical_boundary_strings(tmp_path: Path) -> None:
    """연결 클립에서 앞 큐의 끝과 뒤 큐의 시작은 같은 문자열이 된다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", duration="600s"), _asset_xml("r3", duration="600s")],
        items=[
            _clip_xml("r2", offset="0s", start="0s", duration="100/25s"),
            _clip_xml("r3", offset="100/25s", start="0s", duration="100/25s"),
        ],
    )
    trace_a = _trace("acid-r2", cues=[_CueSpec(3.0, 4.0, lines=("앞 클립 끝",))])
    trace_b = _trace("acid-r3", cues=[_CueSpec(0.0, 2.0, lines=("뒤 클립 시작",))])
    result = project_source_captions(
        document, [_media(document, "r2"), _media(document, "r3")], [trace_a, trace_b]
    )
    assert result.boundary_instance_ids == ()  # 두 큐 모두 온전히 들어맞는다
    assert "00:00:03,000 --> 00:00:04,000" in result.srt_text
    assert "00:00:04,000 --> 00:00:06,000" in result.srt_text


# ─────────────────────────────────────────────────────────────
#  실패·무음 — 조용한 성공이 아니라 상태로 남는다
# ─────────────────────────────────────────────────────────────


def test_error_and_unavailable_sources_keep_valid_draft(tmp_path: Path) -> None:
    """실패 소스는 큐를 못 내고, 남은 소스는 명시적 초안에 그대로 산다."""
    document = _document(
        tmp_path,
        assets=[
            _asset_xml("rA"),
            _asset_xml("rB"),
            _asset_xml("rC"),
            _asset_xml("rD", has_audio="0"),
        ],
        items=[
            _clip_xml("rA", offset="0s", duration="100/25s"),
            _clip_xml("rB", offset="100/25s", duration="100/25s"),
            _clip_xml("rC", offset="200/25s", duration="100/25s"),
            _clip_xml("rD", offset="300/25s", duration="100/25s"),
        ],
    )
    media = [_media(document, ref) for ref in ("rA", "rB", "rC", "rD")]
    error_trace = _trace(
        "acid-rB", cues=[_CueSpec(1.0, 2.0)], status="error", error="SRT 출판 충돌"
    )
    pre_pcm = _unavailable(
        media[2].resource_scoped_id, "acid-rC", media[2].original_sha256, status="error"
    )
    no_audio = _unavailable(media[3].resource_scoped_id, "acid-rD", media[3].original_sha256)
    valid = _trace("acid-rA", cues=[_CueSpec(1.0, 2.0, lines=("살아남은 말",))])
    result = project_source_captions(document, media, [error_trace, pre_pcm, no_audio, valid])
    assert [cue.text for cue in result.placed_cues] == ["살아남은 말"]
    assert result.unavailable_audio == (pre_pcm, no_audio)  # 문서 자원 순서
    assert result.unresolved_cue_ids == ()
    with pytest.raises(ValueError):
        source_cues(error_trace)  # error 큐는 큐가 아니라 상태다


def test_equal_content_resources_keep_separate_dispositions(tmp_path: Path) -> None:
    """같은 내용이라 선언이 다르면 중복 제거가 해석을 덮지 못한다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2"), _asset_xml("r3", has_audio="0")],
        items=[
            _clip_xml("r2", offset="0s", duration="100/25s"),
            _clip_xml("r3", offset="100/25s", duration="100/25s"),
        ],
    )
    audible = _media(document, "r2", content_id="acid-same")
    silent = _media(document, "r3", content_id="acid-same")
    trace = _trace("acid-same", cues=[_CueSpec(1.0, 2.0, lines=("공유 내용",))])
    no_audio = _unavailable(silent.resource_scoped_id, "acid-same", silent.original_sha256)
    result = project_source_captions(document, [audible, silent], [trace, no_audio])
    assert [cue.text for cue in result.placed_cues] == ["공유 내용"]
    assert [cue.instance_id for cue in result.placed_cues] == [document.storyline[0].instance_id]
    assert result.unavailable_audio == (no_audio,)


# ─────────────────────────────────────────────────────────────
#  묶음 거부 — 빠짐·중복·모호·모순은 조용히 지나가지 않는다
# ─────────────────────────────────────────────────────────────


def test_missing_analysis_for_audible_media_is_rejected(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    with pytest.raises(ValueError, match="acid-r3"):
        project_source_captions(document, media, [analyses[0]])


def test_ambiguous_duplicate_traces_are_rejected(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    twin = _trace("acid-r2", cues=[_CueSpec(0.5, 1.5)], analysis_key="key-other")
    with pytest.raises(ValueError, match="acid-r2"):
        project_source_captions(document, media, [analyses[0], twin, analyses[1]])


def test_duplicate_media_resource_is_rejected(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    with pytest.raises(ValueError, match="중복"):
        project_source_captions(document, [*media, media[0]], analyses)


def test_forged_media_audibility_against_authored_resource_is_rejected(tmp_path: Path) -> None:
    """가청 선언의 정본은 문서의 asset이다 — 미디어 행이 거짓말하면 말이 나온다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", has_audio="0")],
        items=[_clip_xml("r2", offset="0s", duration="100/25s")],
    )
    forged = _media(document, "r2", has_audio=True)  # 문서는 hasAudio="0"이다
    trace = _trace("acid-r2", cues=[_CueSpec(1.0, 2.0, lines=("지어낸 말",))])
    with pytest.raises(ValueError, match="hasAudio"):
        project_source_captions(document, [forged], [trace])


def test_silenced_media_against_authored_audible_resource_is_rejected(tmp_path: Path) -> None:
    """거꾸로도 같다 — 문서가 가청이라면 no_audio 기록으로 덮을 수 없다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2")],
        items=[_clip_xml("r2", offset="0s", duration="100/25s")],
    )
    silenced = _media(document, "r2", has_audio=False)  # 문서는 hasAudio="1"이다
    record = _unavailable(silenced.resource_scoped_id, "acid-r2", silenced.original_sha256)
    with pytest.raises(ValueError, match="hasAudio"):
        project_source_captions(document, [silenced], [record])


def test_duplicate_unavailable_per_resource_is_rejected(tmp_path: Path) -> None:
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2", has_audio="0")],
        items=[_clip_xml("r2", offset="0s", duration="100/25s")],
    )
    media = [_media(document, "r2")]
    record = _unavailable(media[0].resource_scoped_id, "acid-r2", media[0].original_sha256)
    with pytest.raises(ValueError):
        project_source_captions(document, media, [record, record])


def test_unavailable_for_unknown_resource_is_rejected(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    stranger = _unavailable("unknown-resource", "acid-r9", "r9-sha")
    with pytest.raises(ValueError, match="unknown-resource"):
        project_source_captions(document, media, [*analyses, stranger])


def test_unavailable_identity_mismatch_is_rejected(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    impostor = _unavailable(media[1].resource_scoped_id, "acid-r9", media[1].original_sha256)
    with pytest.raises(ValueError):
        project_source_captions(document, [media[0], media[1]], [analyses[0], impostor])


def test_has_audio_contradictions_are_rejected(tmp_path: Path) -> None:
    """선언된 삼태와 어긋나는 기록은 전부 거부한다 — 지어내지 않는다."""
    document = _document(
        tmp_path,
        assets=[_asset_xml("r2"), _asset_xml("r3", has_audio="0")],
        items=[
            _clip_xml("r2", offset="0s", duration="100/25s"),
            _clip_xml("r3", offset="100/25s", duration="100/25s"),
        ],
    )
    audible, silent = _media(document, "r2"), _media(document, "r3")
    trace = _trace("acid-r2", cues=[_CueSpec(1.0, 2.0)])
    silent_trace = _trace("acid-r3", cues=[_CueSpec(1.0, 2.0)])
    declared = _unavailable(audible.resource_scoped_id, "acid-r2", audible.original_sha256)
    silent_record = _unavailable(silent.resource_scoped_id, "acid-r3", silent.original_sha256)
    stream_error = _unavailable(
        silent.resource_scoped_id, "acid-r3", silent.original_sha256, status="error"
    )
    with pytest.raises(ValueError, match="has_audio"):
        project_source_captions(document, [audible, silent], [declared, silent_record])
    with pytest.raises(ValueError, match="has_audio"):
        project_source_captions(document, [audible, silent], [trace, stream_error])
    with pytest.raises(ValueError, match="has_audio"):
        project_source_captions(document, [audible, silent], [trace, silent_trace])


def test_media_for_unknown_resource_is_rejected(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    original = Path("/media/stranger.mp4")
    stranger = ResolvedMedia(
        resource_scoped_id="unknown-resource",
        asset_content_id="acid-stranger",
        original=original,
        image_source=original,
        original_sha256="stranger-sha",
        has_audio=True,
        used_proxy=False,
    )
    with pytest.raises(ValueError):
        project_source_captions(document, [*media, stranger], analyses)


def test_extra_unused_trace_is_ignored(tmp_path: Path) -> None:
    document, media, analyses = _main_scenario(tmp_path)
    extra = _trace("acid-elsewhere", cues=[_CueSpec(1.0, 2.0)])
    with_extra = project_source_captions(document, media, [*analyses, extra])
    assert with_extra == project_source_captions(document, media, analyses)


# ─────────────────────────────────────────────────────────────
#  직렬화기·소스 큐 — 문 단위 계약
# ─────────────────────────────────────────────────────────────


def _source_cue(
    cue_id: str,
    start: Fraction,
    end: Fraction,
    text: str = "텍스트",
) -> SourceCue:
    return SourceCue(
        cue_id=cue_id,
        source_id="acid-x",
        segment_ids=("seg-a",),
        start=start,
        end=end,
        text=text,
        flags=(),
    )


def test_serialize_srt_formats_and_numbers_once() -> None:
    cues = [
        _source_cue("a", Fraction(1, 2), Fraction(3, 2)),
        _source_cue("b", Fraction(3, 2), Fraction(2)),
    ]
    assert serialize_srt(cues) == (
        "1\n00:00:00,500 --> 00:00:01,500\n텍스트\n\n2\n00:00:01,500 --> 00:00:02,000\n텍스트\n"
    )
    assert serialize_srt([]) == ""


def test_serialize_srt_rounds_half_ms_up_exactly_once() -> None:
    """정확 0.0005초는 올림한다 — float였다면 0ms가 된다."""
    assert serialize_srt([_source_cue("h", Fraction(1, 2000), Fraction(3, 2000))]) == (
        "1\n00:00:00,001 --> 00:00:00,002\n텍스트\n"
    )


def test_serialize_srt_uses_placed_timeline_fields() -> None:
    placed = PlacedSourceCue(
        cue_id="p",
        instance_id="inst",
        source_id="acid-x",
        source_start=Fraction(9),
        source_end=Fraction(10),
        timeline_start=Fraction(1),
        timeline_end=Fraction(2),
        text="표시",
        flags=(),
    )
    assert serialize_srt([placed]) == "1\n00:00:01,000 --> 00:00:02,000\n표시\n"


def test_serialize_srt_rejects_nonpositive_cue() -> None:
    with pytest.raises(ValueError, match="rev"):
        serialize_srt([_source_cue("rev", Fraction(1), Fraction(1))])


def test_serialize_srt_rejects_overlap() -> None:
    cues = [
        _source_cue("a", Fraction(0), Fraction(1)),
        _source_cue("b", Fraction(1, 2), Fraction(2)),
    ]
    with pytest.raises(ValueError, match="b"):
        serialize_srt(cues)


def test_source_cues_rejects_broken_trace_records() -> None:
    out_of_range = _trace("acid-x", cues=[_CueSpec(1.0, 2.0, seg_ids=(3,))], spine_count=1)
    with pytest.raises(ValueError, match="seg"):
        source_cues(out_of_range)
    reversed_cue = _trace("acid-x", cues=[_CueSpec(2.0, 1.0)])
    with pytest.raises(ValueError):
        source_cues(reversed_cue)
    duplicated_identity = _trace(
        "acid-x",
        cues=[_CueSpec(1.0, 2.0, index=7), _CueSpec(1.0, 2.0, index=7)],
        spine_count=1,
    )
    with pytest.raises(ValueError):
        source_cues(duplicated_identity)


def test_source_cues_empty_trace_is_an_explicit_empty() -> None:
    assert source_cues(_trace("acid-x", status="empty")) == ()


def test_module_does_not_touch_legacy_typesetting_layers() -> None:
    """조립은 조판 계층을 부르지 않는다 — 큐는 이미 정제된 상태로 들어온다."""
    source = inspect.getsource(captions)
    for forbidden in ("refine", "polish", "output", "verify", "layout"):
        assert f"import {forbidden}" not in source
        assert f"from vlogsrt import {forbidden}" not in source
        assert f"from .{forbidden}" not in source
        assert f"vlogsrt.{forbidden}" not in source
