"""P6-03 EDIT 자막 — 편집 계획을 통과하는 큐 투영 (계약).

브리프(p6-03-edit-captions.md)와 P6-02 방향 검토(fable-p6-02-direction.md)의 정본:

- **큐 부착은 run 단위다.** `retained`는 제거가 없는 블록 경계마다 쪼개진다 —
  조각 하나 안에 들어가라고 요구하면 자르지 않은 경계를 건너는 큐가 잘못
  실패한다. 소스와 출력이 둘 다 연속인 `EditPlan.runs`가 정본이고 이동량은
  `run.output.start − run.input.start` 프레임이다.
- **EDIT 신원은 (cue_id, instance_ids)다.** 내부 칼날을 걸친 소스 큐는 인스턴스마다
  한 장이고 여러 칼날을 덮는 경계 큐는 하나다 — cue_id로 합치면 전자가 뭉개진다.
- **제거를 건너는 큐는 하드 실패다.** 부분 큐를 합성·재전사로 지어내지 않는다.
- **접힘은 EDIT 시계에서 다시 잰다.** 상류 깃발은 증거로 남고 중복하지 않는다.

합성 XML·손 미디어·손 guard·손 판정·손 큐만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.boundary import (
    BoundaryProjection,
    ProjectCue,
    UnavailableInstance,
)
from vlogsrt.project.captions import SUBMS_COLLAPSED, project_edit_captions
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.edit import (
    EditBlock,
    EditPlan,
    RetainedRun,
    build_edit_plan,
    partition_blocks,
    vision_request_for,
)
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.timebase import FrameInterval, format_fcpx_time
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vision.policy import VisionDecision, request_sha256

FD25 = Fraction(1, 25)
FDNTSC = Fraction(1001, 30000)


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어·손 guard·손 판정 (test_edit_plan과 같은 자)
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _AssetSpec:
    """애셋 하나 — 시각은 전부 프레임 수로 주고 XML 시각은 격자 위에서 만든다."""

    xml_id: str
    start_frames: int = 0
    duration_frames: int = 400_000
    has_audio: bool = True


@dataclass(frozen=True)
class _ClipSpec:
    """칼날 하나 — offset은 tcStart, start는 asset.start에 앵커된 프레임 수다."""

    ref: str
    offset_frames: int = 0
    start_frames: int = 0
    duration_frames: int = 500
    enabled: bool = True
    src_enable: str = "all"


@dataclass(frozen=True)
class _GapSpec:
    """갭 하나 — offset은 tcStart에 앵커된 프레임 수다."""

    offset_frames: int
    duration_frames: int


def _project(
    tmp_path: Path,
    assets: tuple[_AssetSpec, ...],
    items: tuple[_ClipSpec | _GapSpec, ...],
    *,
    tc_start_frames: int = 0,
    rate: Fraction = FD25,
) -> ProjectDocument:
    """격자가 정확한 합성 문서 — 파서가 요구하는 앵커·프레임 정수를 전부 지킨다."""
    tc = Fraction(tc_start_frames) * rate
    spans = [item.offset_frames + item.duration_frames for item in items]
    sequence_frames = max(spans, default=0) + 10_000
    assets_xml = "\n    ".join(
        f'<asset id="{asset.xml_id}" name="{asset.xml_id}.mp4" uid="AAAA"'
        f' start="{format_fcpx_time(Fraction(asset.start_frames) * rate)}"'
        f' duration="{format_fcpx_time(Fraction(asset.duration_frames) * rate)}"'
        f' hasAudio="{int(asset.has_audio)}" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file:///media/{asset.xml_id}.mp4"/></asset>'
        for asset in assets
    )
    asset_starts = {asset.xml_id: Fraction(asset.start_frames) * rate for asset in assets}
    clips_xml = "\n    ".join(
        _gap_xml(item, tc, rate)
        if isinstance(item, _GapSpec)
        else _clip_xml(item, tc, rate, asset_starts[item.ref])
        for item in items
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        f'<format id="r1" name="FFVideoFormat1080p25" frameDuration="{format_fcpx_time(rate)}"'
        ' width="1920" height="1080"/>\n    '
        + assets_xml
        + '\n  </resources>\n  <library>\n    <event name="이벤트">\n'
        '      <project name="프로젝트">\n'
        f'        <sequence format="r1" tcStart="{format_fcpx_time(tc)}"'
        f' duration="{format_fcpx_time(Fraction(sequence_frames) * rate)}">\n'
        "          <spine>\n            "
        + clips_xml
        + "\n          </spine>\n        </sequence>\n"
        "      </project>\n    </event>\n  </library>\n</fcpxml>\n"
    )
    path = tmp_path / "project.fcpxml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return parse_project(path)


def _gap_xml(gap: _GapSpec, tc: Fraction, rate: Fraction) -> str:
    """갭 XML — offset은 tcStart에 앵커된 프레임 수다."""
    return (
        f'<gap name="갭" offset="{format_fcpx_time(tc + Fraction(gap.offset_frames) * rate)}"'
        f' duration="{format_fcpx_time(Fraction(gap.duration_frames) * rate)}"/>'
    )


def _clip_xml(clip: _ClipSpec, tc: Fraction, rate: Fraction, asset_start: Fraction) -> str:
    """칼날 XML — start는 asset.start에, offset은 tcStart에 앵커된 프레임 수로."""
    return (
        f'<asset-clip ref="{clip.ref}" name="{clip.ref}"'
        f' offset="{format_fcpx_time(tc + Fraction(clip.offset_frames) * rate)}"'
        f' start="{format_fcpx_time(asset_start + Fraction(clip.start_frames) * rate)}"'
        f' duration="{format_fcpx_time(Fraction(clip.duration_frames) * rate)}"/>'
    )


def _media(
    document: ProjectDocument, content: dict[str, str] | None = None
) -> tuple[ResolvedMedia, ...]:
    """손 미디어 — 내용 신원은 기본으로 xml_id마다 하나, 같은 내용 시험엔 명시한다."""
    return tuple(
        ResolvedMedia(
            resource_scoped_id=resource.scoped_id,
            asset_content_id=(content or {}).get(resource.xml_id, f"acid-{resource.xml_id}"),
            original=Path(f"/media/{resource.xml_id}.mp4"),
            image_source=Path(f"/media/{resource.xml_id}.mp4"),
            original_sha256=f"{resource.xml_id}-sha",
            has_audio=resource.has_audio,
            used_proxy=False,
        )
        for resource in document.resources
    )


def _clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


def _guards(
    document: ProjectDocument,
    media: tuple[ResolvedMedia, ...],
    *,
    status_by_clip: dict[int, str] | None = None,
) -> tuple[InstanceGuard, ...]:
    """문서의 클립마다 guard 하나 — 기본 success, 불가청·부재 클립은 따로 준다."""
    content = {row.resource_scoped_id: row.asset_content_id for row in media}
    statuses = status_by_clip or {}
    return tuple(
        InstanceGuard(
            instance_id=clip.instance_id,
            source_id=content[clip.resource_scoped_id],
            status=statuses.get(position, "success"),  # type: ignore[arg-type]
            protected_frames=(),
            review=(),
        )
        for position, clip in enumerate(_clips(document))
    )


def _plan(
    document: ProjectDocument,
    media: tuple[ResolvedMedia, ...],
    guards: tuple[InstanceGuard, ...],
    decisions: tuple[VisionDecision, ...] = (),
    *,
    locked: frozenset[str] = frozenset(),
    unavailable: tuple[UnavailableInstance, ...] = (),
) -> EditPlan:
    partition = partition_blocks(document, media, guards, locked, unavailable, ProjectConfig())
    return build_edit_plan(document, partition, guards, decisions, ProjectConfig())


def _decision(block: EditBlock, choice: str) -> VisionDecision:
    """손 판정 — 요청 신원은 정본 투영에서 만든다."""
    return VisionDecision(
        block_id=block.block_id,
        request_sha256=request_sha256(vision_request_for(block, "")),
        choice=choice,
        reason="같은 내용의 단순 되풀이 B-roll이다",
        evidence_frame_ids=(),
        confidence=0.9,
        status="accepted",
    )


def _front(block: EditBlock) -> VisionDecision:
    return _decision(block, block.keep_candidates[0].candidate_id)


def _back(block: EditBlock) -> VisionDecision:
    return _decision(block, block.keep_candidates[-1].candidate_id)


def _unavailable(clip: ClipInstance, document: ProjectDocument, reason: str) -> UnavailableInstance:
    resource = next(r for r in document.resources if r.scoped_id == clip.resource_scoped_id)
    return UnavailableInstance(
        resource_scoped_id=resource.scoped_id,
        instance_id=clip.instance_id,
        reason_code=reason,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────
#  재료 — 손 큐·손 출처 투영
# ─────────────────────────────────────────────────────────────


def _cue(
    cue_id: str,
    instances: tuple[str, ...],
    start: Fraction,
    end: Fraction,
    *,
    text: str = "자막",
    provenance: str = "source",
    flags: tuple[str, ...] = (),
    source: tuple[Fraction, Fraction] | None = None,
) -> ProjectCue:
    """손 큐 — 파일 시계 원본은 기본으로 배치 상수 0(소스==타임라인) 세계를 따른다.

    `source`를 주면 저변 잘림 세계의 **잘리지 않은 원본 기록**을 실는다(P3-03과
    같은 비대칭 — 발표만 묶이고 원본은 그대로다).
    """
    source_start, source_end = source if source is not None else (start, end)
    return ProjectCue(
        cue_id=cue_id,
        slice_id=f"slice-{cue_id}",
        instance_ids=instances,
        source_id="acid-a1",
        source_start=source_start,
        source_end=source_end,
        timeline_start=start,
        timeline_end=end,
        text=text,
        provenance=provenance,  # type: ignore[arg-type]
        flags=flags,
    )


def _projection(
    document: ProjectDocument,
    cues: tuple[ProjectCue, ...],
    *,
    locked: frozenset[str] = frozenset(),
    unavailable: tuple[UnavailableInstance, ...] = (),
    input_sha256: str | None = None,
) -> BoundaryProjection:
    """손 출처 투영 — 계획과 같은 문서·잠금·부재를 말하는 정상적인 입력."""
    return BoundaryProjection(
        input_sha256=input_sha256 or document.input_sha256,
        cues=cues,
        source_srt="1\n00:00:01,000 --> 00:00:01,500\n자막\n",
        locked_instance_ids=locked,
        unavailable_instances=unavailable,
        unresolved_request_ids=(),
        unresolved_cue_ids=(),
    )


def _run_of(plan: EditPlan, instance_id: str, containing: Fraction) -> RetainedRun:
    """그 인스턴스의 run 가운데 입력 타임라인 시각 `containing`(초)을 담은 run."""
    for run in plan.runs:
        if run.instance_id != instance_id:
            continue
        window = run.input_timeline_frames
        if window.start_frame <= containing * 25 < window.end_frame:
            return run
    raise AssertionError(f"run이 없다: instance={instance_id!r}, at={containing}")


def _scene25(
    tmp_path: Path, items: tuple[_ClipSpec | _GapSpec, ...]
) -> tuple[ProjectDocument, tuple[ResolvedMedia, ...], tuple[InstanceGuard, ...]]:
    """25fps 한 자원 세계 — a1 하나, 클립 배치 상수는 전부 0이다."""
    document = _project(tmp_path, (_AssetSpec("a1"),), items)
    media = _media(document)
    return document, media, _guards(document, media)


# ─────────────────────────────────────────────────────────────
#  네 발치 테스트 — 브리프 0단계, 먼저 빨간불로 박는다
# ─────────────────────────────────────────────────────────────


def test_uncut_block_boundary_cue_survives(tmp_path: Path) -> None:
    """자르지 않은 블록 경계를 건너는 큐는 살아남는다 — run이 정본이다.

    판정 없는 500프레임 클립의 `retained`는 [0,250)·[250,500) 둘이지만 실제로
    자른 곳은 없다. 조각 하나 안에 들어가라고 요구하면 이 큐가 잘못 실패한다.
    """
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    assert [row.reason for row in plan.retained] == ["kept_block", "kept_block"]
    assert len(plan.runs) == 1, "제거가 없으면 run 하나다 — 이것이 부착 단위다"

    cue = _cue("straddler", (_clips(document)[0].instance_id,), Fraction(19, 2), Fraction(21, 2))
    result = project_edit_captions(plan, _projection(document, (cue,)))

    assert result.plan_revision == plan.revision
    (only,) = result.cues
    assert (only.timeline_start, only.timeline_end) == (Fraction(19, 2), Fraction(21, 2))
    assert result.srt_text == "1\n00:00:09,500 --> 00:00:10,500\n자막\n"


def test_cue_intersecting_a_removal_hard_fails(tmp_path: Path) -> None:
    """제거를 건너는 큐는 하드 실패다 — 부분 큐를 합성하지 않는다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    plan = _plan(document, media, guards, (_front(partition.blocks[0]),))
    # 앞 후보 [0,75)가 남고 [75,250) — 초로 [3,10) — 이 제거된다
    cue = _cue("crosser", (_clips(document)[0].instance_id,), Fraction(2), Fraction(8))

    with pytest.raises(ValueError, match="제거 구간과 겹친다") as excinfo:
        project_edit_captions(plan, _projection(document, (cue,)))
    assert "crosser" in str(excinfo.value)


def test_straddling_source_cue_stays_two_placements_adjacent_after_ripple(
    tmp_path: Path,
) -> None:
    """내부 칼날을 걸친 소스 큐는 앞 인스턴스의 제거 뒤에도 두 장으로 남는다.

    같은 자원의 이어진 두 칼날 A|B — 소스 큐 하나가 A의 끝과 B의 앞에 걸쳐
    각각 클립 가장자리로 잘려 두 장으로 배치된다(P3-03). A의 머리가 잘려 나가도
    두 장은 같은 이동량으로 밀려 리플 뒤 정확히 인접한다. cue_id로 합치면
    안 된다 — 신원은 (cue_id, instance_ids)다.
    """
    document, media, guards = _scene25(
        tmp_path,
        (
            _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=250),
            _ClipSpec("a1", offset_frames=250, start_frames=250, duration_frames=250),
        ),
    )
    first, second = _clips(document)
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    # A의 블록에서 뒤 후보 [175,250)만 남긴다 — 머리 [0,175)이 잘려 나간다
    plan = _plan(document, media, guards, (_back(partition.blocks[0]),))
    original = (Fraction(49, 5), Fraction(52, 5))  # 파일 시계 원본 [9.8, 10.4)
    head = _cue(
        "straddler",
        (first.instance_id,),
        Fraction(49, 5),
        Fraction(10),
        text="칼날 마디",
        source=original,
    )
    tail = _cue(
        "straddler",
        (second.instance_id,),
        Fraction(10),
        Fraction(52, 5),
        text="칼날 마디",
        source=original,
    )

    result = project_edit_captions(plan, _projection(document, (head, tail)))

    assert [cue.cue_id for cue in result.cues] == ["straddler", "straddler"]
    assert result.cues[0].instance_ids == (first.instance_id,)
    assert result.cues[1].instance_ids == (second.instance_id,)
    # 이동량 −175프레임 = −7초: [9.8,10)→[2.8,3), [10,10.4)→[3,3.4)
    assert result.cues[0].timeline_end == Fraction(3)
    assert result.cues[1].timeline_start == Fraction(3)
    assert result.cues[0].timeline_start == Fraction(14, 5)
    assert result.cues[1].timeline_end == Fraction(17, 5)
    assert result.cues[0].provenance == "source" and result.cues[1].provenance == "source"
    assert result.srt_text.count("칼날 마디") == 2
    assert "00:00:02,800 --> 00:00:03,000" in result.srt_text
    assert "00:00:03,000 --> 00:00:03,400" in result.srt_text


def test_boundary_cue_over_two_locked_blades_stays_one_cue(tmp_path: Path) -> None:
    """잠긴 두 칼날을 덮는 경계 큐는 한 장으로 남는다 — 쪼개지도 합쳐지지도 않는다."""
    document, media, guards = _scene25(
        tmp_path,
        (
            _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=250),
            _ClipSpec("a1", offset_frames=250, start_frames=250, duration_frames=250),
        ),
    )
    first, second = _clips(document)
    locked = frozenset({first.instance_id, second.instance_id})
    plan = _plan(document, media, guards, locked=locked)
    assert len(plan.runs) == 2, "잠긴 칼날마다 통째 유지 run 하나다"

    cue = _cue(
        "boundary-1",
        (first.instance_id, second.instance_id),
        Fraction(49, 5),
        Fraction(52, 5),
        text="경계 마디",
        provenance="boundary",
    )
    result = project_edit_captions(plan, _projection(document, (cue,), locked=locked))

    (only,) = result.cues
    assert only.cue_id == "boundary-1"
    assert only.instance_ids == (first.instance_id, second.instance_id)
    assert only.provenance == "boundary"
    assert (only.timeline_start, only.timeline_end) == (Fraction(49, 5), Fraction(52, 5))
    assert only.source_start == Fraction(49, 5) and only.source_end == Fraction(52, 5)
    assert "경계 마디" in result.srt_text


# ─────────────────────────────────────────────────────────────
#  리플 — 블록 여럿·갭·정확한 끝점·인접
# ─────────────────────────────────────────────────────────────


def test_ripple_across_two_blocks_and_a_gap_maps_exactly(tmp_path: Path) -> None:
    """두 블록의 제거와 갭 하나를 건너는 큐 — 누적 이동량이 정확하다."""
    document, media, guards = _scene25(
        tmp_path,
        (
            _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
            _GapSpec(offset_frames=500, duration_frames=100),
            _ClipSpec("a1", offset_frames=600, start_frames=600, duration_frames=300),
        ),
    )
    clip1, clip2 = _clips(document)
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    # 첫 클립의 두 블록 모두 앞 후보 — [75,250)과 [325,500)이 나간다(175+175프레임)
    plan = _plan(
        document,
        media,
        guards,
        (_front(partition.blocks[0]), _front(partition.blocks[1])),
    )
    cues = (
        _cue("c1", (clip1.instance_id,), Fraction(1, 2), Fraction(1), text="첫 마디"),
        _cue("c2", (clip1.instance_id,), Fraction(10), Fraction(13), text="가운데 마디"),
        _cue("c3", (clip2.instance_id,), Fraction(25), Fraction(26), text="끝 마디"),
    )

    result = project_edit_captions(plan, _projection(document, cues))

    assert [(cue.timeline_start, cue.timeline_end) for cue in result.cues] == [
        (Fraction(1, 2), Fraction(1)),
        (Fraction(3), Fraction(6)),
        (Fraction(11), Fraction(12)),
    ]
    # c2는 run [250,325)의 입력 창 그 자체다 — 출력 끝점 150프레임 = 6초와 정확히 같다
    run = _run_of(plan, clip1.instance_id, Fraction(10))
    assert Fraction(run.output_timeline_frames.end_frame) * FD25 == Fraction(6)
    assert result.cues[1].timeline_end == Fraction(run.output_timeline_frames.end_frame) * FD25
    assert result.srt_text == (
        "1\n00:00:00,500 --> 00:00:01,000\n첫 마디\n"
        "\n2\n00:00:03,000 --> 00:00:06,000\n가운데 마디\n"
        "\n3\n00:00:11,000 --> 00:00:12,000\n끝 마디\n"
    )


def test_clip_end_cue_ends_at_the_exact_retained_endpoint(tmp_path: Path) -> None:
    """클립 끝에 닿는 큐 — 남은 조각의 끝점 그대로이고 프레임 틈을 지어내지 않는다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    plan = _plan(document, media, guards, (_front(partition.blocks[0]),))
    (clip,) = _clips(document)
    # 입력 [19,20)은 남은 run [250,500)의 마지막 순간 — 끝은 클립 끝 20초와 정확히 같다
    cue = _cue("tail", (clip.instance_id,), Fraction(19), Fraction(20), text="끝 마디")

    result = project_edit_captions(plan, _projection(document, (cue,)))

    (only,) = result.cues
    run = _run_of(plan, clip.instance_id, Fraction(19))
    assert Fraction(run.output_timeline_frames.end_frame) * FD25 == Fraction(13)
    assert only.timeline_end == Fraction(run.output_timeline_frames.end_frame) * FD25
    assert only.timeline_end == Fraction(13)
    assert "00:00:12,000 --> 00:00:13,000" in result.srt_text


def test_cues_touching_a_removal_become_adjacent_after_ripple(tmp_path: Path) -> None:
    """제거 양옆에 닿던 큐 둘은 리플 뒤 정확히 인접한다 — 닿는 경계는 같은 밀리초다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    plan = _plan(document, media, guards, (_front(partition.blocks[0]),))
    (clip,) = _clips(document)
    # 제거 [75,250) = [3,10)초 — 앞 큐는 3초에, 뒤 큐는 10초에 닿는다
    head = _cue("head", (clip.instance_id,), Fraction(2), Fraction(3), text="앞 마디")
    tail = _cue("tail", (clip.instance_id,), Fraction(10), Fraction(11), text="뒤 마디")

    result = project_edit_captions(plan, _projection(document, (head, tail)))

    assert result.cues[0].timeline_end == Fraction(3)
    assert result.cues[1].timeline_start == Fraction(3)
    assert result.srt_text == (
        "1\n00:00:02,000 --> 00:00:03,000\n앞 마디\n\n2\n00:00:03,000 --> 00:00:04,000\n뒤 마디\n"
    )


def test_partial_removal_overlap_also_hard_fails(tmp_path: Path) -> None:
    """제거에 걸치기만 해도 실패다 — 시작이 run에 있어도 끝이 제거 안에 있으면 안 된다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    plan = _plan(document, media, guards, (_front(partition.blocks[0]),))
    (clip,) = _clips(document)
    cue = _cue("half-in", (clip.instance_id,), Fraction(9), Fraction(21, 2))

    with pytest.raises(ValueError, match="제거 구간과 겹친다"):
        project_edit_captions(plan, _projection(document, (cue,)))


def test_forged_run_covering_a_removal_still_hard_fails(tmp_path: Path) -> None:
    """위조 플랜의 run이 제거 위를 덮어도 제거 검사가 먼저 닫는다 — P6-02 안전 문장의 기계 박기.

    `project_edit_captions`은 시그니처에 문서도 guard도 없어 `validate_edit_plan`을
    부를 수 없다. 그래서 `runs`와 `removed`가 서로소라고 가정하지 않고 제거를
    모든 큐에 대해 run 포함보다 먼저 본다. 정직한 계획의 `runs`를 클립 전체
    run 하나로 바꾼 위조 플랜은 run 포함에 성공하지만, 그 큐는 계획 자신의
    제거 기록과 겹치므로 조용히 내보내지 않고 하드 실패한다.
    """
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    plan = _plan(document, media, guards, (_front(partition.blocks[0]),))
    assert [row.input_timeline_frames for row in plan.removed] == [FrameInterval(75, 250)]
    (clip,) = _clips(document)
    forged = replace(
        plan,
        runs=(
            RetainedRun(
                instance_id=clip.instance_id,
                source_frames=FrameInterval(0, 500),
                input_timeline_frames=FrameInterval(0, 500),
                output_timeline_frames=FrameInterval(0, 500),
                reasons=("kept_block",),
            ),
        ),
    )
    # [2,8)초 = 프레임 [50,200) — 위조 run [0,500) 안에 온전히 있지만 제거 [75,250)과 겹친다
    cue = _cue("forged-crosser", (clip.instance_id,), Fraction(2), Fraction(8))

    with pytest.raises(ValueError, match="제거 구간과 겹친다") as excinfo:
        project_edit_captions(forged, _projection(document, (cue,)))
    assert "forged-crosser" in str(excinfo.value)


def test_repeated_content_instances_project_independently(tmp_path: Path) -> None:
    """같은 내용을 다시 쓰는 인스턴스 — 같은 cue_id의 배치 둘은 각자 밀린다.

    B-roll 재사용: 첫·끝 클립이 같은 소스 구간을 쓰고 가운데 클립의 제거가
    끝 배치만 민다. cue_id가 같다고 한 장으로 합치지 않는다.
    """
    document, media, guards = _scene25(
        tmp_path,
        (
            _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=250),
            _ClipSpec("a1", offset_frames=250, start_frames=500, duration_frames=250),
            _ClipSpec("a1", offset_frames=500, start_frames=0, duration_frames=250),
        ),
    )
    first, middle, last = _clips(document)
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    middle_block = next(
        block for block in partition.blocks if block.instance_id == middle.instance_id
    )
    plan = _plan(document, media, guards, (_back(middle_block),))
    reuse_head = _cue("reuse-1", (first.instance_id,), Fraction(1), Fraction(2), text="재사용 마디")
    # 끝 클립은 입력 타임라인 [20,30)초에 산다 — 같은 소스 구간 [1,2)초의 재배치다
    reuse_tail = _cue(
        "reuse-1", (last.instance_id,), Fraction(21), Fraction(22), text="재사용 마디"
    )

    result = project_edit_captions(plan, _projection(document, (reuse_head, reuse_tail)))

    assert [cue.cue_id for cue in result.cues] == ["reuse-1", "reuse-1"]
    assert result.cues[0].instance_ids == (first.instance_id,)
    assert result.cues[1].instance_ids == (last.instance_id,)
    # 가운데 클립의 머리 [500,675) 프레임이 나가므로 끝 배치만 −175프레임 밀린다
    assert (result.cues[0].timeline_start, result.cues[0].timeline_end) == (
        Fraction(1),
        Fraction(2),
    )
    assert (result.cues[1].timeline_start, result.cues[1].timeline_end) == (
        Fraction(14),
        Fraction(15),
    )
    assert result.srt_text.count("재사용 마디") == 2


# ─────────────────────────────────────────────────────────────
#  신원 — 섞인 리비전·부재 기록·불변성
# ─────────────────────────────────────────────────────────────


def test_mixed_revision_inputs_are_rejected(tmp_path: Path) -> None:
    """문서 해시·잠금이 어긋나면 섞인 리비전이다 — 큐를 읽기 전에 닫는다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    (clip,) = _clips(document)
    cue = _cue("ok", (clip.instance_id,), Fraction(1), Fraction(2))

    with pytest.raises(ValueError, match="문서 해시가 출처와 다르다"):
        project_edit_captions(plan, _projection(document, (cue,), input_sha256="stale"))
    with pytest.raises(ValueError, match="잠긴 인스턴스가 출처와 다르다"):
        project_edit_captions(
            plan, _projection(document, (cue,), locked=frozenset({clip.instance_id}))
        )


def test_unavailable_metadata_must_match_exactly(tmp_path: Path) -> None:
    """부재 기록은 순서까지 정확히 같아야 한다 — stale·누락·재정렬은 섞인 리비전이다."""
    document = _project(
        tmp_path,
        (_AssetSpec("a1"), _AssetSpec("a2", has_audio=False), _AssetSpec("a3", has_audio=False)),
        (
            _ClipSpec("a1", duration_frames=500),
            _ClipSpec("a2", offset_frames=500, duration_frames=500),
            _ClipSpec("a3", offset_frames=1000, duration_frames=500),
        ),
    )
    media = _media(document)
    guards = _guards(document, media, status_by_clip={1: "inaudible", 2: "inaudible"})
    healthy, quiet2, quiet3 = _clips(document)
    records = (
        _unavailable(quiet2, document, "declared_no_audio"),
        _unavailable(quiet3, document, "declared_no_audio"),
    )
    locked = frozenset({quiet2.instance_id, quiet3.instance_id})
    plan = _plan(document, media, guards, locked=locked, unavailable=records)
    cue = _cue("ok", (healthy.instance_id,), Fraction(1), Fraction(2))

    # 정확히 같은 기록은 통과하고 리뷰에 타입 코드가 순서 그대로 남는다
    result = project_edit_captions(
        plan, _projection(document, (cue,), locked=locked, unavailable=records)
    )
    assert [issue.code for issue in result.review] == [
        "unavailable_declared_no_audio",
        "unavailable_declared_no_audio",
    ]

    stale = _projection(document, (cue,), locked=locked)
    with pytest.raises(ValueError, match="부재 기록이 출처와"):
        project_edit_captions(plan, stale)
    missing = _projection(document, (cue,), locked=locked, unavailable=records[:1])
    with pytest.raises(ValueError, match="부재 기록이 출처와"):
        project_edit_captions(plan, missing)
    reordered = _projection(document, (cue,), locked=locked, unavailable=(records[1], records[0]))
    with pytest.raises(ValueError, match="부재 기록이 출처와"):
        project_edit_captions(plan, reordered)


def test_declared_no_audio_completes_with_typed_review(tmp_path: Path) -> None:
    """선언된 무음은 완결이다 — 큐를 지어내지 않고 타입 리뷰만 흘려보낸다."""
    document = _project(
        tmp_path,
        (_AssetSpec("a1"), _AssetSpec("a2", has_audio=False)),
        (
            _ClipSpec("a1", duration_frames=500),
            _ClipSpec("a2", offset_frames=500, duration_frames=500),
        ),
    )
    media = _media(document)
    guards = _guards(document, media, status_by_clip={1: "inaudible"})
    healthy, quiet = _clips(document)
    records = (_unavailable(quiet, document, "declared_no_audio"),)
    locked = frozenset({quiet.instance_id})
    plan = _plan(document, media, guards, locked=locked, unavailable=records)
    cue = _cue("ok", (healthy.instance_id,), Fraction(1), Fraction(2))

    result = project_edit_captions(
        plan, _projection(document, (cue,), locked=locked, unavailable=records)
    )

    (issue,) = result.review
    assert issue.code == "unavailable_declared_no_audio"
    assert issue.instance_id is None and issue.source_frame is None
    assert all(quiet.instance_id not in cue.instance_ids for cue in result.cues)
    assert result.srt_text == "1\n00:00:01,000 --> 00:00:02,000\n자막\n"


def test_retryable_unavailability_carries_blocking_code(tmp_path: Path) -> None:
    """되풀이 가능한 부재는 수용을 막는 코드로 남는다 — 판정은 P6-04가 코드로 한다."""
    document = _project(
        tmp_path,
        (_AssetSpec("a1"), _AssetSpec("a2")),
        (
            _ClipSpec("a1", duration_frames=500),
            _ClipSpec("a2", offset_frames=500, duration_frames=500),
        ),
    )
    media = _media(document)
    guards = _guards(document, media, status_by_clip={1: "keep_all"})
    healthy, broken = _clips(document)
    records = (_unavailable(broken, document, "stream_unavailable"),)
    locked = frozenset({broken.instance_id})
    plan = _plan(document, media, guards, locked=locked, unavailable=records)
    cue = _cue("ok", (healthy.instance_id,), Fraction(1), Fraction(2))

    result = project_edit_captions(
        plan, _projection(document, (cue,), locked=locked, unavailable=records)
    )

    (issue,) = result.review
    assert issue.code == "unavailable_stream_unavailable"
    assert issue.instance_id is None and issue.source_frame is None


def test_source_projection_is_untouched(tmp_path: Path) -> None:
    """SOURCE 기록은 이 함수를 지나도 그대로다 — 투영은 읽기만 한다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    (clip,) = _clips(document)
    cues = (
        _cue("plain", (clip.instance_id,), Fraction(1), Fraction(2), text="보통 마디"),
        _cue(
            "from-boundary",
            (clip.instance_id,),
            Fraction(3),
            Fraction(4),
            text="경계 마디",
            provenance="boundary",
            flags=("boundary_unresolved",),
        ),
    )
    source = _projection(document, cues)
    before_cues = source.cues
    before_srt = source.source_srt

    result = project_edit_captions(plan, source)

    assert source.cues == before_cues
    assert source.source_srt == before_srt
    assert source.input_sha256 == document.input_sha256
    assert source.locked_instance_ids == frozenset()
    assert source.unavailable_instances == ()
    # 글자·출처·원본 기록·깃발은 그대로 — 시각만 리플 이동량이 더해진 새 객체다
    assert result.cues[1].text == "경계 마디"
    assert result.cues[1].provenance == "boundary"
    assert result.cues[1].flags == ("boundary_unresolved",)
    assert result.cues[1].source_start == Fraction(3)
    assert result.cues[1].slice_id == "slice-from-boundary"


def test_plan_review_is_preserved_and_edit_issues_append(tmp_path: Path) -> None:
    """계획의 구조화 리뷰는 글자 그대로·순서 그대로 먼저 온다 — EDIT 증거는 뒤에 붙는다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    repeated = _front(partition.blocks[0])
    plan = _plan(document, media, guards, (repeated, repeated))
    assert [issue.code for issue in plan.review] == ["decision_inconsistent"]
    (clip,) = _clips(document)
    healthy = _cue("healthy", (clip.instance_id,), Fraction(1), Fraction(3, 2), text="온전한 마디")
    start = Fraction(133, 8000)
    collapsed = _cue(
        "tiny",
        (clip.instance_id,),
        start,
        start + Fraction(1, 24000),
        text="한 프레임 마디",
        flags=(SUBMS_COLLAPSED,),
    )

    result = project_edit_captions(plan, _projection(document, (healthy, collapsed)))

    assert result.review[: len(plan.review)] == plan.review
    assert [issue.code for issue in result.review] == [
        "decision_inconsistent",
        SUBMS_COLLAPSED,
    ]
    assert result.review[0] == plan.review[0]


def test_zero_cue_projection_stays_zero(tmp_path: Path) -> None:
    """큐가 없으면 EDIT도 없다 — 빈 성공이 아니라 빈 그대로다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)

    result = project_edit_captions(plan, _projection(document, ()))

    assert result.cues == ()
    assert result.srt_text == ""
    assert result.review == plan.review == ()


# ─────────────────────────────────────────────────────────────
#  접힘 — EDIT 시계에서 다시 잰다
# ─────────────────────────────────────────────────────────────


def test_exact_seventeen_ms_collapse_at_edit(tmp_path: Path) -> None:
    """정확 `133/8000`초 → `+1/24000`초 — 양 끝이 모두 17ms로 반올림된다.

    큐는 정확 길이 양수로 남고 깃발이 붙고 SRT에서 그 장만 빠지고 미위치
    리뷰가 남는다 — 마커 좌표가 없으므로 P6-04는 최종 수용을 거부한다.
    """
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    (clip,) = _clips(document)
    start = Fraction(133, 8000)
    healthy = _cue("healthy", (clip.instance_id,), Fraction(1), Fraction(3, 2), text="온전한 마디")
    collapsed = _cue(
        "tiny",
        (clip.instance_id,),
        start,
        start + Fraction(1, 24000),
        text="한 프레임 마디",
        flags=(SUBMS_COLLAPSED,),
    )

    result = project_edit_captions(plan, _projection(document, (healthy, collapsed)))

    flagged = next(cue for cue in result.cues if cue.cue_id == "tiny")
    assert flagged.timeline_start == Fraction(133, 8000)
    assert flagged.timeline_end == Fraction(1, 60)
    assert flagged.flags.count(SUBMS_COLLAPSED) == 1, "상류 깃발을 중복하지 않는다"
    assert "한 프레임 마디" not in result.srt_text
    assert result.srt_text == "1\n00:00:01,000 --> 00:00:01,500\n온전한 마디\n"
    (issue,) = [row for row in result.review if row.code == SUBMS_COLLAPSED]
    assert "tiny" in issue.message
    assert "133/8000" in issue.message and "1/60" in issue.message
    assert issue.instance_id is None and issue.source_frame is None


def test_collapse_introduced_only_by_the_ripple(tmp_path: Path) -> None:
    """29.97 프레임 밀림이 접힘을 만든다 — 리플 전에는 표현 가능했던 큐가 접힌다.

    같은 세계에서 반대편도 본다: 상류에서 접힌 큐가 리플 뒤 표현 가능해지면
    깃발은 증거로 남고(그 장은 여전히 빠진다) 새 리뷰는 생기지 않는다.
    """
    document = _project(
        tmp_path / "ntsc",
        (_AssetSpec("a1"),),
        (_ClipSpec("a1", duration_frames=500),),
        rate=FDNTSC,
    )
    media = _media(document)
    guards = _guards(document, media)
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    # 앞 블록의 뒤 후보 [160,250)만 남긴다 — 머리 [0,160)이 나가 런이 −160프레임 밀린다
    plan = _plan(document, media, guards, (_back(partition.blocks[0]),))
    (clip,) = _clips(document)
    assert Fraction(-160) * FDNTSC == Fraction(-2002, 375)
    shrinks = _cue(
        "shrinks",
        (clip.instance_id,),
        Fraction(18001, 3000),
        Fraction(7201, 1200),
        text="리플에 접히는 마디",
    )
    recovers = _cue(
        "recovers",
        (clip.instance_id,),
        Fraction(7201, 1200),
        Fraction(4501, 750),
        text="상류에서 접힌 마디",
        flags=(SUBMS_COLLAPSED,),
    )

    result = project_edit_captions(plan, _projection(document, (shrinks, recovers)))

    shrunk, recovered = result.cues
    # 리플 이동량 −2002/375초: [18001/3000, 7201/1200) → [397/600, 3973/6000)
    assert (shrunk.timeline_start, shrunk.timeline_end) == (
        Fraction(397, 600),
        Fraction(3973, 6000),
    )
    assert SUBMS_COLLAPSED in shrunk.flags
    assert (recovered.timeline_start, recovered.timeline_end) == (
        Fraction(3973, 6000),
        Fraction(497, 750),
    )
    assert recovered.flags == (SUBMS_COLLAPSED,), "상류 증거는 그대로다"
    assert result.srt_text == "", "접힘 깃발이 있는 장은 모두 빠진다"
    assert [row.code for row in result.review] == [SUBMS_COLLAPSED], "새 리뷰는 하나뿐이다"
    (issue,) = result.review
    assert "shrinks" in issue.message
    assert "397/600" in issue.message and "3973/6000" in issue.message


# ─────────────────────────────────────────────────────────────
#  경계 큐의 실패 길 — 연속·같은 이동량·통째 유지
# ─────────────────────────────────────────────────────────────


def _three_blades(
    tmp_path: Path,
) -> tuple[ProjectDocument, tuple[ResolvedMedia, ...], tuple[InstanceGuard, ...]]:
    """같은 자원의 이어진 칼날 셋 — 잠긴 A|C 사이 가운데 B가 편집된다."""
    document = _project(
        tmp_path,
        (_AssetSpec("a1"),),
        (
            _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=250),
            _ClipSpec("a1", offset_frames=250, start_frames=250, duration_frames=250),
            _ClipSpec("a1", offset_frames=500, start_frames=500, duration_frames=250),
        ),
    )
    media = _media(document)
    return document, media, _guards(document, media)


def test_boundary_cue_over_nonconsecutive_whole_clips_fails(tmp_path: Path) -> None:
    """사이의 칼날이 편집된 경계 큐 — 통째 유지 run들이 출력에서 연속이 아니다."""
    document, media, guards = _three_blades(tmp_path)
    first, middle, last = _clips(document)
    locked = frozenset({first.instance_id, last.instance_id})
    partition = partition_blocks(document, media, guards, locked, (), ProjectConfig())
    middle_block = next(
        block for block in partition.blocks if block.instance_id == middle.instance_id
    )
    plan = _plan(document, media, guards, (_back(middle_block),), locked=locked)
    cue = _cue(
        "spanner",
        (first.instance_id, last.instance_id),
        Fraction(49, 5),
        Fraction(25),
        provenance="boundary",
    )

    with pytest.raises(ValueError, match="출력에서 연속이 아니다"):
        project_edit_captions(plan, _projection(document, (cue,), locked=locked))


def test_boundary_cue_with_edited_instance_fails(tmp_path: Path) -> None:
    """경계 큐의 인스턴스가 제거로 run 둘로 쪼개졌다 — 통째 유지 run이 아니다."""
    document = _project(
        tmp_path,
        (_AssetSpec("a1"),),
        (
            _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
            _ClipSpec("a1", offset_frames=500, start_frames=500, duration_frames=250),
        ),
    )
    media = _media(document)
    guards = _guards(document, media)
    first, second = _clips(document)
    partition = partition_blocks(document, media, guards, frozenset(), (), ProjectConfig())
    first_block = next(
        block for block in partition.blocks if block.instance_id == first.instance_id
    )
    plan = _plan(document, media, guards, (_front(first_block),))
    assert len([run for run in plan.runs if run.instance_id == first.instance_id]) == 2
    cue = _cue(
        "over-edited",
        (first.instance_id, second.instance_id),
        Fraction(1),
        Fraction(21),
        provenance="boundary",
    )

    with pytest.raises(ValueError, match="통째 유지 run 하나가 아니다"):
        project_edit_captions(plan, _projection(document, (cue,)))


def test_cue_on_unknown_instance_fails(tmp_path: Path) -> None:
    """계획에 없는 인스턴스의 큐 — 지어낼 run이 없으므로 닫는다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    cue = _cue("ghost", ("instance-that-is-not"), Fraction(1), Fraction(2))

    with pytest.raises(ValueError, match="계획에 없다"):
        project_edit_captions(plan, _projection(document, (cue,)))


# ─────────────────────────────────────────────────────────────
#  잔여 방어 — 계약 위반은 조용한 성공이 아니다
# ─────────────────────────────────────────────────────────────


def test_overlapping_edit_cues_reject(tmp_path: Path) -> None:
    """리플 뒤 겹치는 큐는 직렬화할 수 없다 — 위조 입력은 그 자리에서 닫는다.

    정상 상류는 겹침을 내지 않는다(이동량은 단조롭다). 손으로 지어 넣은
    역전은 계획이 아니라 입력이 깨진 것이므로 하드 실패다.
    """
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    (clip,) = _clips(document)
    late = _cue("late", (clip.instance_id,), Fraction(4), Fraction(5))
    early = replace(late, timeline_start=Fraction(1), timeline_end=Fraction(9, 2))

    with pytest.raises(ValueError, match="서로 겹친다"):
        project_edit_captions(plan, _projection(document, (early, late)))


def test_zero_length_edit_cue_rejects(tmp_path: Path) -> None:
    """정확 길이 0 이하인 큐는 받지 않는다 — 상류가 이미 막는 선을 다시 지킨다."""
    document, media, guards = _scene25(tmp_path, (_ClipSpec("a1", duration_frames=500),))
    plan = _plan(document, media, guards)
    (clip,) = _clips(document)
    cue = replace(
        _cue("flat", (clip.instance_id,), Fraction(1), Fraction(2)),
        timeline_end=Fraction(1),
    )

    with pytest.raises(ValueError, match="길이가 0 이하다"):
        project_edit_captions(plan, _projection(document, (cue,)))
