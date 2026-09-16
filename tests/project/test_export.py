"""P6-04 원자적 패키지 내보내기 — FCPXML·EDIT SRT·SOURCE SRT·review·manifest (계약).

브리프(p6-04-package-export.md)와 P6-02·P6-03 방향 검토(Fable)의 정본:

- **스토리라인은 조회만 한다.** `asset-clip`은 `plan.runs`마다 하나(`retained`마다가
  아니라 — 제거 없는 블록 경계마다 쪼개져 through-edit을 낸다). 갭은 `plan.gaps`,
  오프셋은 `m × frame_duration`, `tcStart=0` — 리플 산수를 다시 하지 않는다.
- **마커 소유자는 내보낸 run이고 반개 구간이다.** 마커 시계는
  `asset.start + source_frame × frame_duration`(애셋 시작 앵커) — 잘린 클립 시작을
  빼서 0상대 시각을 만들지 않는다. 자리가 지워졌으면 마커 없이 review에 남는다.
- **원본만 링크한다.** 프록시가 이미지 원본이어도 출력의 original-media는
  `ResolvedMedia.original`이다. 낡은 `a1` id는 문서 안 참조일 뿐 재묶음 키가 아니다.
- **패키지는 하나의 리비전으로 나온다.** 다섯 파일이 불변 리비전 디렉터리에 함께
  쓰이고 `current` 심링크의 원자적 교체로 공개된다. 실패는 포인터 이전에 멈춘다.
- **`final_accepted`는 여기서만 계산한다.** SOURCE에서 접힌 큐는 EDIT 이슈를
  남기지 않으므로 `source.unresolved_cue_ids`가 유일한 차단 증거다 — 반드시 본다.

합성 XML·손 미디어·손 guard·손 판정·손 출처 투영만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Literal

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
    ReviewIssue,
    build_edit_plan,
    partition_blocks,
    validate_edit_plan,
    vision_request_for,
)
from vlogsrt.project.export import (
    ExportPackage,
    edit_captions_record,
    plan_record,
    publish_package,
    render_edit_fcpxml,
)
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.timebase import FrameInterval, format_fcpx_time
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vision.policy import VisionDecision, request_sha256
from vlogsrt.project.xml_validation import XmlValidationUnavailable

DecisionStatus = Literal["accepted", "keep_all", "error"]

FD25 = Fraction(1, 25)

# 검사기 로드 — tests/test_invariants.py와 같은 방식(정확 경로 로드).
_ROOT = Path(__file__).resolve().parent.parent.parent
_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_invariants_export", _ROOT / "scripts" / "check_invariants.py"
)
check_invariants = importlib.util.module_from_spec(_CHECKER_SPEC)
assert _CHECKER_SPEC.loader is not None
_CHECKER_SPEC.loader.exec_module(check_invariants)

INSTALLED_DTD = Path(
    "/Applications/Final Cut Pro.app/Contents/Frameworks/Interchange.framework"
    "/Versions/A/Resources/FCPXMLv1_14.dtd"
)
needs_installed_dtd = pytest.mark.skipif(
    not INSTALLED_DTD.is_file(), reason="설치된 Final Cut Pro 1.14 DTD가 없는 환경"
)

FCPXML_NAME = "VLOG_AI_EDIT.fcpxml"
EDIT_SRT_NAME = "VLOG_AI_EDIT.srt"
SOURCE_SRT_NAME = "VLOG_AI_SOURCE.srt"
TRACE_NAME = "project_trace.json"
REVIEW_NAME = "review.json"
PACKAGE_NAMES = {FCPXML_NAME, EDIT_SRT_NAME, SOURCE_SRT_NAME, TRACE_NAME, REVIEW_NAME}
SOURCE_SRT_TEXT = "1\n00:00:01,000 --> 00:00:01,500\n자막\n"


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
class CLIP:
    """칼날 하나 — offset은 tcStart, start는 asset.start에 앵커된 프레임 수다."""

    ref: str
    offset_frames: int = 0
    start_frames: int = 0
    duration_frames: int = 500
    enabled: bool = True
    src_enable: str = "all"


@dataclass(frozen=True)
class GAP:
    """갭 하나 — offset은 tcStart에 앵커된 프레임 수다."""

    offset_frames: int
    duration_frames: int


@dataclass(frozen=True)
class _Bundle:
    """한 장면의 전부 — 문서·미디어·guard·계획·잠금·부재."""

    document: ProjectDocument
    media: tuple[ResolvedMedia, ...]
    guards: tuple[InstanceGuard, ...]
    plan: EditPlan
    locked: frozenset[str]
    unavailable: tuple[UnavailableInstance, ...]


def _project(
    tmp_path: Path,
    assets: tuple[_AssetSpec, ...],
    items: tuple[CLIP | GAP, ...],
    *,
    tc_start_frames: int = 0,
) -> ProjectDocument:
    """격자가 정확한 합성 문서 — 파서가 요구하는 앵커·프레임 정수를 전부 지킨다."""
    rate = FD25
    tc = Fraction(tc_start_frames) * rate
    spans = [item.offset_frames + item.duration_frames for item in items]
    sequence_frames = max(spans, default=0) + 10_000
    assets_xml = "\n    ".join(
        f'<asset id="{asset.xml_id}" name="{asset.xml_id}.mp4" uid="AAAA"'
        f' start="{format_fcpx_time(Fraction(asset.start_frames) * rate)}"'
        f' duration="{format_fcpx_time(Fraction(asset.duration_frames) * rate)}"'
        f' hasAudio="{int(asset.has_audio)}" hasVideo="1" format="r1">'
        f'<media-rep kind="original-media" src="file:///media/{asset.xml_id}.mp4"/>'
        f'<media-rep kind="proxy-media" src="file:///proxy/{asset.xml_id}.mov"/></asset>'
        for asset in assets
    )
    asset_starts = {asset.xml_id: Fraction(asset.start_frames) * rate for asset in assets}
    clips_xml = "\n    ".join(
        _gap_xml(item, tc, rate)
        if isinstance(item, GAP)
        else _clip_xml(item, tc, rate, asset_starts[item.ref])
        for item in items
    )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14">\n  <resources>\n    '
        f'<format id="r1" name="FFVideoFormat1080p25" frameDuration="{format_fcpx_time(rate)}"'
        ' width="1920" height="1080"/>\n    '
        + assets_xml
        + '\n  </resources>\n  <library>\n    <event name="이벤트" uid="OLD-EVENT">\n'
        '      <project name="프로젝트" uid="OLD-PROJECT">\n'
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


def _gap_xml(gap: GAP, tc: Fraction, rate: Fraction) -> str:
    return (
        f'<gap name="갭" offset="{format_fcpx_time(tc + Fraction(gap.offset_frames) * rate)}"'
        f' duration="{format_fcpx_time(Fraction(gap.duration_frames) * rate)}"/>'
    )


def _clip_xml(clip: CLIP, tc: Fraction, rate: Fraction, asset_start: Fraction) -> str:
    attributes = ""
    if not clip.enabled:
        attributes += ' enabled="0"'
    if clip.src_enable == "video":
        attributes += ' srcEnable="video"'
    return (
        f'<asset-clip ref="{clip.ref}" name="{clip.ref}"'
        f' offset="{format_fcpx_time(tc + Fraction(clip.offset_frames) * rate)}"'
        f' start="{format_fcpx_time(asset_start + Fraction(clip.start_frames) * rate)}"'
        f' duration="{format_fcpx_time(Fraction(clip.duration_frames) * rate)}"'
        f"{attributes}/>"
    )


def _media(document: ProjectDocument, media_dir: str = "media") -> tuple[ResolvedMedia, ...]:
    """손 미디어 — 원본은 media_dir 아래 경로, 프록시는 이미지 원본 후보일 뿐이다."""
    return tuple(
        ResolvedMedia(
            resource_scoped_id=resource.scoped_id,
            asset_content_id=f"acid-{resource.xml_id}",
            original=Path(f"/{media_dir}/{resource.xml_id}.mp4"),
            image_source=Path(f"/proxy/{resource.xml_id}.mov"),
            original_sha256=f"{resource.xml_id}-sha",
            has_audio=resource.has_audio,
            used_proxy=True,
        )
        for resource in document.resources
    )


def _clips(document: ProjectDocument) -> tuple[ClipInstance, ...]:
    return tuple(item for item in document.storyline if isinstance(item, ClipInstance))


def _guards(
    document: ProjectDocument,
    media: tuple[ResolvedMedia, ...],
    protected: tuple[tuple[int, int], ...] = (),
) -> tuple[InstanceGuard, ...]:
    """문서의 클립마다 guard 하나 — 가청은 success, 아니면 keep_all/inaudible.

    `protected`는 **계획 세대**의 보호다(첫 클립에만 단다). 파티션 세대는 늘 빈
    보호로 주고 계획에서 보호를 키우면 guard_overlap 이슈가 난다 — 다른 guard 세대가
    만든 판정을 적용하는 정상적인 이음매다.
    """
    has_audio = {row.resource_scoped_id: row.has_audio for row in media}
    spans = (FrameInterval(low, high) for low, high in protected)
    guards: list[InstanceGuard] = []
    for position, clip in enumerate(_clips(document)):
        status: Literal["success", "keep_all", "inaudible"]
        if not has_audio[clip.resource_scoped_id]:
            status = "inaudible"
        elif not clip.enabled or clip.src_enable != "all":
            status = "keep_all"
        else:
            status = "success"
        guards.append(
            InstanceGuard(
                instance_id=clip.instance_id,
                source_id=f"acid-{_xml_id_of(document, clip)}",
                status=status,
                protected_frames=tuple(spans) if position == 0 else (),
                review=(),
            )
        )
    return tuple(guards)


def _xml_id_of(document: ProjectDocument, clip: ClipInstance) -> str:
    for resource in document.resources:
        if resource.scoped_id == clip.resource_scoped_id:
            return resource.xml_id
    raise AssertionError("클립이 가리키는 자원이 없다")


def _decision(
    block: EditBlock, choice: str, *, status: DecisionStatus = "accepted"
) -> VisionDecision:
    return VisionDecision(
        block_id=block.block_id,
        request_sha256=request_sha256(vision_request_for(block, "")),
        choice=choice,
        reason="같은 내용의 단순 되풀이 B-roll이다",
        evidence_frame_ids=(),
        confidence=0.9,
        status=status,
    )


def build_plan(
    tmp_path: Path,
    items: tuple[CLIP | GAP, ...],
    *,
    assets: tuple[_AssetSpec, ...] | None = None,
    tc_start_frames: int = 0,
    media_dir: str = "media",
    protected: tuple[tuple[int, int], ...] = (),
    front_on_first_block: bool = False,
    middle_on_second_block: bool = False,
    inconsistent_on_first_block: bool = False,
    error_on_all_blocks: bool = False,
    keep_all_on_remaining_blocks: bool = False,
    lock_second: bool = False,
    unavailable_second_asset: str | None = None,
) -> _Bundle:
    """한 장면을 문서→파티션→계획까지 만든다 — 판정·보호는 깃발로 고른다."""
    asset_rows = assets
    if asset_rows is None:
        first = _AssetSpec("a1", start_frames=0)
        asset_rows = (
            (first, _AssetSpec("a2", has_audio=False))
            if (unavailable_second_asset is not None)
            else (first,)
        )
    document = _project(tmp_path, asset_rows, items, tc_start_frames=tc_start_frames)
    media = _media(document, media_dir)
    clips = _clips(document)
    partition_guards = _guards(document, media)
    locked = frozenset({clips[1].instance_id}) if lock_second else frozenset()
    unavailable: tuple[UnavailableInstance, ...] = ()
    if unavailable_second_asset is not None:
        second = clips[1]
        unavailable = (
            UnavailableInstance(
                resource_scoped_id=second.resource_scoped_id,
                instance_id=second.instance_id,
                reason_code=unavailable_second_asset,  # type: ignore[arg-type]
            ),
        )
    partition = partition_blocks(
        document, media, partition_guards, locked, unavailable, ProjectConfig()
    )
    decisions: list[VisionDecision] = []
    if inconsistent_on_first_block:
        decisions.append(_decision(partition.blocks[0], "keep_all"))
    if front_on_first_block:
        first = partition.blocks[0]
        decisions.append(_decision(first, first.keep_candidates[0].candidate_id))
    if middle_on_second_block:
        second = partition.blocks[1]
        decisions.append(_decision(second, second.keep_candidates[1].candidate_id))
    if error_on_all_blocks:
        decisions.extend(_decision(block, "keep_all", status="error") for block in partition.blocks)
    if keep_all_on_remaining_blocks:
        decided = {decision.block_id for decision in decisions}
        decisions.extend(
            _decision(block, "keep_all", status="keep_all")
            for block in partition.blocks
            if block.block_id not in decided
        )
    build_guards = _guards(document, media, protected)
    plan = build_edit_plan(document, partition, build_guards, tuple(decisions), ProjectConfig())
    return _Bundle(
        document=document,
        media=media,
        guards=build_guards,
        plan=plan,
        locked=locked,
        unavailable=unavailable,
    )


def cue(document: ProjectDocument, cue_id: str, start: Fraction, end: Fraction) -> ProjectCue:
    """손 큐 — 배치 상수 0 세계(소스==타임라인), 단일 인스턴스, 출처는 소스다."""
    instance = _clips(document)[0].instance_id
    return ProjectCue(
        cue_id=cue_id,
        slice_id=f"slice-{cue_id}",
        instance_ids=(instance,),
        source_id="acid-a1",
        source_start=start,
        source_end=end,
        timeline_start=start,
        timeline_end=end,
        text=f"자막 {cue_id}",
        provenance="source",
        flags=(),
    )


def build_projection(
    document: ProjectDocument,
    cues: tuple[ProjectCue, ...] = (),
    *,
    locked: frozenset[str] = frozenset(),
    unavailable: tuple[UnavailableInstance, ...] = (),
    unresolved_requests: tuple[str, ...] = (),
    unresolved_cues: tuple[str, ...] = (),
) -> BoundaryProjection:
    """손 출처 투영 — 계획과 같은 문서·잠금·부재를 말하는 정상적인 입력."""
    return BoundaryProjection(
        input_sha256=document.input_sha256,
        cues=cues,
        source_srt=SOURCE_SRT_TEXT,
        locked_instance_ids=locked,
        unavailable_instances=unavailable,
        unresolved_request_ids=unresolved_requests,
        unresolved_cue_ids=unresolved_cues,
    )


def _captions(bundle: _Bundle, cues: tuple[ProjectCue, ...] = ()):
    return project_edit_captions(
        bundle.plan,
        build_projection(
            bundle.document, cues, locked=bundle.locked, unavailable=bundle.unavailable
        ),
    )


# ─────────────────────────────────────────────────────────────
#  XML 탐색 도우미
# ─────────────────────────────────────────────────────────────


def _root_of(text: str) -> ET.Element:
    return ET.fromstring(text)


def _sequence(root: ET.Element) -> ET.Element:
    return root.find("library/event/project/sequence")


def _spine(root: ET.Element) -> ET.Element:
    return _sequence(root).find("spine")


def _asset_clips(root: ET.Element) -> list[ET.Element]:
    return [child for child in _spine(root) if child.tag == "asset-clip"]


def _assets(root: ET.Element) -> list[ET.Element]:
    return [child for child in root.find("resources") if child.tag == "asset"]


def _markers(root: ET.Element) -> list[ET.Element]:
    return [marker for clip in _asset_clips(root) for marker in clip.findall("marker")]


def _rendered(bundle: _Bundle, plan: EditPlan | None = None, captions=None) -> str:
    return render_edit_fcpxml(
        bundle.document,
        bundle.media,
        plan if plan is not None else bundle.plan,
        captions if captions is not None else _captions(bundle),
        bundle.guards,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ─────────────────────────────────────────────────────────────
#  렌더 — 관문
# ─────────────────────────────────────────────────────────────


def test_render_refuses_a_plan_with_validator_findings(tmp_path: Path) -> None:
    """구조 관문이 findings를 내면 렌더하지 않는다 — 유지 출력 길이 ≠ 소스 길이."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),))
    broken = replace(
        bundle.plan,
        retained=(
            replace(
                bundle.plan.retained[0],
                output_timeline_frames=FrameInterval(0, 50),
            ),
        ),
    )
    assert validate_edit_plan(broken, bundle.document, bundle.guards)
    with pytest.raises(ValueError, match="검증"):
        _rendered(bundle, plan=broken)


def test_render_refuses_mixed_caption_revision(tmp_path: Path) -> None:
    """자막의 plan_revision이 계획과 다르면 섞인 리비전이다."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),))
    captions = replace(_captions(bundle), plan_revision="f" * 64)
    with pytest.raises(ValueError, match="리비전"):
        render_edit_fcpxml(bundle.document, bundle.media, bundle.plan, captions, bundle.guards)


def test_render_refuses_empty_storyline(tmp_path: Path) -> None:
    """빈 스토리라인은 내보낼 수 없다 — 0길이 시퀀스는 곧 무효 문서다."""
    bundle = build_plan(tmp_path, ())
    assert bundle.plan.retained == () and bundle.plan.gaps == ()
    with pytest.raises(ValueError, match="빈 스토리라인"):
        _rendered(bundle)


def test_render_leaves_the_input_document_untouched(tmp_path: Path) -> None:
    """렌더는 원본 문서 파일도 raw_xml도 한 바이트 못 바꾼다."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),))
    before_bytes = bundle.document.input_path.read_bytes()
    before_raw = bundle.document.raw_xml
    _rendered(bundle)
    assert bundle.document.input_path.read_bytes() == before_bytes
    assert bundle.document.raw_xml == before_raw


# ─────────────────────────────────────────────────────────────
#  렌더 — 문서 신원·원본 링크·정적 속성
# ─────────────────────────────────────────────────────────────


def test_render_creates_new_identity_and_zero_tcstart(tmp_path: Path) -> None:
    """새 문서는 새 event/project 이름과 tcStart=0을 가진다 — 낡은 uid는 못 밸다."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),), tc_start_frames=3600)
    root = _root_of(_rendered(bundle))
    event = root.find("library/event")
    project = root.find("library/event/project")
    assert event.get("name") == "VLOG_AI_EDIT"
    assert project.get("name") == "VLOG_AI_EDIT"
    assert "uid" not in event.attrib and "uid" not in project.attrib
    assert _sequence(root).get("tcStart") == "0s"


def test_render_links_original_never_proxy(tmp_path: Path) -> None:
    """프록시가 이미지 원본이어도 출력의 original-media는 원본 경로 하나다."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),))
    reps = _assets(_root_of(_rendered(bundle)))[0].findall("media-rep")
    assert len(reps) == 1
    assert reps[0].get("kind") == "original-media"
    assert reps[0].get("src") == "file:///media/a1.mp4"


def test_render_rebinds_by_resource_identity_not_raw_ids(tmp_path: Path) -> None:
    """같은 낡은 id를 쓰는 무관한 문서 둘은 서로의 미디어로 묶이지 않는다."""
    first = build_plan(tmp_path / "one", (CLIP("a1", duration_frames=500),), media_dir="media-one")
    second = build_plan(tmp_path / "two", (CLIP("a1", duration_frames=400),), media_dir="media-two")
    xml_one = first.document.resources[0].xml_id
    assert xml_one == second.document.resources[0].xml_id, "낡은 id가 같다"
    assert first.document.input_sha256 != second.document.input_sha256
    src_one = _assets(_root_of(_rendered(first)))[0].find("media-rep").get("src")
    src_two = _assets(_root_of(_rendered(second)))[0].find("media-rep").get("src")
    assert src_one == "file:///media-one/a1.mp4"
    assert src_two == "file:///media-two/a1.mp4"
    # 남의 문서 미디어는 같은 낡은 id(a1)를 가리켜도 거부된다 — 스코프 신원이 다르다.
    with pytest.raises(ValueError, match="문서에 없는 자원"):
        render_edit_fcpxml(first.document, second.media, first.plan, _captions(first), first.guards)


def test_render_preserves_repeats_gaps_disabled_and_order(tmp_path: Path) -> None:
    """반복 애셋·갭·비활성 클립·소스 순서가 그대로 산다."""
    items = (
        CLIP("a1", duration_frames=500),
        GAP(offset_frames=500, duration_frames=100),
        CLIP("a1", offset_frames=600, duration_frames=250, enabled=False, src_enable="video"),
    )
    bundle = build_plan(tmp_path, items, front_on_first_block=True)
    spine = list(_spine(_root_of(_rendered(bundle))))
    assert [child.tag for child in spine] == [
        "asset-clip",
        "asset-clip",
        "gap",
        "asset-clip",
    ]
    first, second, gap, disabled = spine
    # 첫 클립: 앞 후보 [0,75) 적용 → run 둘. 갭은 리플 뒤 13s에 선다.
    assert first.get("ref") == "a1"
    assert (first.get("offset"), first.get("duration")) == ("0s", "3s")
    assert (second.get("start"), second.get("offset")) == ("10s", "3s")
    assert (gap.get("offset"), gap.get("duration")) == ("13s", "4s")
    # 비활성 클립은 통째 유지 — 길이가 그대로고 정적 속성도 산다.
    assert disabled.get("ref") == "a1"
    assert (disabled.get("offset"), disabled.get("duration")) == ("17s", "10s")
    assert disabled.get("enabled") == "0" and disabled.get("srcEnable") == "video"


def test_render_emits_one_clip_per_run_not_per_retained(tmp_path: Path) -> None:
    """제거가 없으면 블록 경계가 retained를 쪼개도 클립은 하나다."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),))
    assert len(bundle.plan.retained) == 2, "블록 경계에서 retained는 둘으로 쪼개진다"
    assert len(bundle.plan.runs) == 1
    clips = _asset_clips(_root_of(_rendered(bundle)))
    assert len(clips) == 1
    assert clips[0].get("duration") == "20s"


def test_render_cuts_at_exact_plan_frames(tmp_path: Path) -> None:
    """칼은 정확히 계획 프레임 경계에만 선다 — 앞 후보 [0,75) 적용."""
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),), front_on_first_block=True)
    root = _root_of(_rendered(bundle))
    first, second = _asset_clips(root)
    assert (first.get("start"), first.get("duration"), first.get("offset")) == (
        "0s",
        "3s",
        "0s",
    )
    assert (second.get("start"), second.get("duration"), second.get("offset")) == (
        "10s",
        "10s",
        "3s",
    )
    assert _sequence(root).get("duration") == "13s", "안전 픽스처가 실제로 짧아진다"


def test_render_offsets_rebase_from_zero(tmp_path: Path) -> None:
    """입력 tcStart가 3600프레임이어도 출력 오프셋은 0에서 다시 시작한다."""
    items = (
        CLIP("a1", duration_frames=500),
        GAP(offset_frames=500, duration_frames=100),
    )
    bundle = build_plan(tmp_path, items, tc_start_frames=3600, front_on_first_block=True)
    spine = list(_spine(_root_of(_rendered(bundle))))
    assert spine[0].get("offset") == "0s"
    assert spine[1].get("offset") == "3s"


# ─────────────────────────────────────────────────────────────
#  렌더 — 마커
# ─────────────────────────────────────────────────────────────


def test_marker_owner_is_the_later_slice_at_a_cut_boundary(tmp_path: Path) -> None:
    """칼 경계 프레임 250의 마커는 반개 구간상 뒤 조각(시작 10s)이 가진다."""
    bundle = build_plan(
        tmp_path,
        (CLIP("a1", duration_frames=500),),
        protected=((250, 260),),
        front_on_first_block=True,
        middle_on_second_block=True,
    )
    located = [issue for issue in bundle.plan.review if issue.instance_id is not None]
    assert [issue.source_frame for issue in located] == [250]
    clips = _asset_clips(_root_of(_rendered(bundle)))
    assert [clip.get("start") for clip in clips] == ["0s", "10s"]
    owners = [clip for clip in clips if clip.findall("marker")]
    assert len(owners) == 1 and owners[0].get("start") == "10s"
    assert owners[0].find("marker").get("start") == "10s"


def test_marker_clock_stays_asset_anchored(tmp_path: Path) -> None:
    """asset.start≠0에서 마커 시각은 애셋 앵커 시계 그대로다 — 잘린 클립 0상대가 아니다."""
    bundle = build_plan(
        tmp_path,
        (CLIP("a1", duration_frames=500),),
        assets=(_AssetSpec("a1", start_frames=100),),
        protected=((300, 360),),
        front_on_first_block=True,
        middle_on_second_block=True,
    )
    clips = _asset_clips(_root_of(_rendered(bundle)))
    # run 둘: 소스 격자 [0,75)·[250,500) — XML start는 애셋 시작 100프레임을 더한 시계.
    assert [clip.get("start") for clip in clips] == ["4s", "14s"]
    marker = clips[1].find("marker")
    assert marker.get("start") == "16s", "asset.start(100f) + 300f = 16s — 앵커 증명"
    assert marker.get("duration") == "1/25s"


def test_deleted_marker_location_never_relocates(tmp_path: Path) -> None:
    """제거된 프레임의 이슈는 마커를 옮기지 않는다 — 살아있는 자리만 낸다."""
    bundle = build_plan(
        tmp_path,
        (CLIP("a1", duration_frames=500),),
        protected=((300, 360),),
        front_on_first_block=True,
        middle_on_second_block=True,
    )
    instance = _clips(bundle.document)[0].instance_id
    forged = replace(
        bundle.plan,
        review=(
            *bundle.plan.review,
            ReviewIssue("guard_overlap", "제거된 자리", instance, 200),
        ),
    )
    markers = _markers(_root_of(_rendered(bundle, plan=forged)))
    assert [marker.get("start") for marker in markers] == ["12s"]


def test_unlocated_issues_never_become_markers(tmp_path: Path) -> None:
    """위치 없는 이슈는 마커로 지어내지 않는다 — 산문 파싱 금지의 기계 검사."""
    bundle = build_plan(
        tmp_path, (CLIP("a1", duration_frames=500),), inconsistent_on_first_block=True
    )
    assert bundle.plan.review and all(issue.instance_id is None for issue in bundle.plan.review)
    assert _markers(_root_of(_rendered(bundle))) == []


def test_marker_shape_follows_the_installed_dtd(tmp_path: Path) -> None:
    """marker는 EMPTY, start/duration/value만 달고 asset-clip의 직속 자식이다."""
    bundle = build_plan(
        tmp_path,
        (CLIP("a1", duration_frames=500),),
        protected=((300, 360),),
        front_on_first_block=True,
        middle_on_second_block=True,
    )
    marker = _markers(_root_of(_rendered(bundle)))[0]
    assert marker.tag == "marker" and len(list(marker)) == 0
    assert set(marker.attrib) == {"start", "duration", "value"}
    assert marker.get("value").startswith("VLOG_AI_REVIEW:")
    assert marker.get("duration") == "1/25s"


# ─────────────────────────────────────────────────────────────
#  렌더 — 부재 기록과 리뷰 짝
# ─────────────────────────────────────────────────────────────


def _unavailable_bundle(tmp_path: Path, reason: str) -> _Bundle:
    items = (
        CLIP("a1", duration_frames=500),
        CLIP("a2", offset_frames=500, duration_frames=250),
    )
    return build_plan(
        tmp_path,
        items,
        front_on_first_block=True,
        unavailable_second_asset=reason,
        lock_second=True,
    )


def test_render_accepts_matching_unavailable_review(tmp_path: Path) -> None:
    bundle = _unavailable_bundle(tmp_path, "declared_no_audio")
    assert "asset-clip" in _rendered(bundle)


def test_render_refuses_omitted_unavailable_review(tmp_path: Path) -> None:
    """부재 기록이 있는데 리뷰가 그 이슈를 빼면 거짓 기록이다."""
    bundle = _unavailable_bundle(tmp_path, "declared_no_audio")
    captions = _captions(bundle)
    stripped = replace(
        captions,
        review=tuple(
            issue for issue in captions.review if not issue.code.startswith("unavailable_")
        ),
    )
    with pytest.raises(ValueError, match="부재"):
        render_edit_fcpxml(bundle.document, bundle.media, bundle.plan, stripped, bundle.guards)


def test_render_refuses_contradictory_unavailable_review(tmp_path: Path) -> None:
    """리뷰가 기록과 다른 부재 사유를 말하면 모순이다."""
    bundle = _unavailable_bundle(tmp_path, "declared_no_audio")
    captions = _captions(bundle)
    swapped = replace(
        captions,
        review=tuple(
            replace(issue, code="unavailable_stream_unavailable")
            if issue.code == "unavailable_declared_no_audio"
            else issue
            for issue in captions.review
        ),
    )
    with pytest.raises(ValueError, match="부재"):
        render_edit_fcpxml(bundle.document, bundle.media, bundle.plan, swapped, bundle.guards)


# ─────────────────────────────────────────────────────────────
#  공개 기록 인코더 — P7-01이 읽는 유일한 형태
# ─────────────────────────────────────────────────────────────


def test_plan_record_encodes_fractions_and_frames(tmp_path: Path) -> None:
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),), front_on_first_block=True)
    record = plan_record(bundle.plan)
    assert record["revision"] == bundle.plan.revision
    assert record["frame_duration"] == "1/25"
    removed = record["removed"][0]
    assert removed["source_frames"] == [75, 250]
    assert removed["restore_at_output_frame"] == 75
    run = record["runs"][0]
    assert run["source_frames"] == [0, 75]
    assert run["output_timeline_frames"] == [0, 75]
    assert run["reasons"] == ["kept_candidate"]
    assert set(record["decisions"][0]) == {
        "block_id",
        "request_sha256",
        "choice",
        "reason",
        "evidence_frame_ids",
        "confidence",
        "status",
    }


def test_edit_captions_record_encodes_cues(tmp_path: Path) -> None:
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),))
    captions = _captions(bundle, (cue(bundle.document, "c1", Fraction(1), Fraction(2)),))
    record = edit_captions_record(captions)
    assert record["plan_revision"] == bundle.plan.revision
    assert record["cues"][0]["timeline_start"] == "1/1"
    assert record["cues"][0]["flags"] == []
    assert record["srt_text"] == captions.srt_text
    assert set(record) == {"plan_revision", "cues", "srt_text", "review"}


# ─────────────────────────────────────────────────────────────
#  게시 — 다섯 파일·한 리비전
# ─────────────────────────────────────────────────────────────


def _publish(
    tmp_path: Path,
    destination: Path | None = None,
    *,
    front_on_first_block: bool = True,
    unavailable_second_asset: str | None = None,
    **kwargs,
) -> ExportPackage:
    dtd_path = kwargs.pop("dtd_path", None)
    default_items = (
        (
            CLIP("a1", duration_frames=500),
            CLIP("a2", offset_frames=500, duration_frames=250),
        )
        if unavailable_second_asset is not None
        else (CLIP("a1", duration_frames=500),)
    )
    bundle = build_plan(
        tmp_path,
        kwargs.pop("items", default_items),
        front_on_first_block=front_on_first_block,
        unavailable_second_asset=unavailable_second_asset,
        lock_second=unavailable_second_asset is not None,
        **kwargs,
    )
    cues = (
        cue(bundle.document, "c1", Fraction(1), Fraction(2)),
        cue(bundle.document, "c2", Fraction(260, 25), Fraction(11)),
    )
    projection = build_projection(
        bundle.document, cues, locked=bundle.locked, unavailable=bundle.unavailable
    )
    captions = project_edit_captions(bundle.plan, projection)
    destination = destination if destination is not None else tmp_path / "export"
    return publish_package(
        destination,
        bundle.document,
        bundle.media,
        bundle.plan,
        captions,
        projection,
        bundle.guards,
        dtd_path=dtd_path,
    )


def _revision_dir(destination: Path) -> Path:
    return (destination / "current").resolve()


def test_first_publish_creates_managed_root_and_five_names(tmp_path: Path) -> None:
    destination = tmp_path / "export"
    package = _publish(tmp_path, destination)
    revision = _revision_dir(destination)
    assert (destination / "current").is_symlink()
    assert os.readlink(destination / "current") == f"revisions/{revision.name}"
    assert package.plan_revision == revision.name
    assert {entry.name for entry in revision.iterdir()} == PACKAGE_NAMES
    for path in (
        package.fcpxml,
        package.edit_srt,
        package.source_srt,
        package.project_trace,
        package.review,
    ):
        assert path.parent == revision and path.is_file()


def test_published_srt_texts_come_from_the_projection(tmp_path: Path) -> None:
    package = _publish(tmp_path)
    assert package.edit_srt.read_text(encoding="utf-8").startswith(
        "1\n00:00:01,000 --> 00:00:02,000\n"
    )
    assert package.source_srt.read_text(encoding="utf-8") == SOURCE_SRT_TEXT


def test_structured_artifacts_share_one_revision(tmp_path: Path) -> None:
    package = _publish(tmp_path)
    trace = _load_json(package.project_trace)
    review = _load_json(package.review)
    assert trace["plan_revision"] == review["plan_revision"] == package.plan_revision
    assert trace["kind"].startswith("vlogsrt-")
    assert review["kind"].startswith("vlogsrt-")
    artifacts = trace["artifacts"]
    assert set(artifacts) == PACKAGE_NAMES - {TRACE_NAME}
    for name, digest in artifacts.items():
        body = (package.fcpxml.parent / name).read_bytes()
        assert hashlib.sha256(body).hexdigest() == digest["sha256"]


def test_trace_records_removed_ranges_and_restore_points(tmp_path: Path) -> None:
    package = _publish(tmp_path)
    removed = _load_json(package.project_trace)["plan"]["removed"]
    assert [row["source_frames"] for row in removed] == [[75, 250]]
    assert removed[0]["restore_at_output_frame"] == 75


def test_unavailable_records_are_exact_in_both_artifacts(tmp_path: Path) -> None:
    """부재 기록은 두 JSON이 하나의 타입 그대로 싣는다 — 신원 셋이 정확하다."""
    package = _publish(tmp_path, unavailable_second_asset="stream_unavailable")
    trace = _load_json(package.project_trace)
    review = _load_json(package.review)
    records = trace["plan"]["unavailable_instances"]
    assert records == review["unavailable_instances"]
    assert records[0]["reason_code"] == "stream_unavailable"
    assert set(records[0]) == {"resource_scoped_id", "instance_id", "reason_code"}
    assert trace["source"]["unavailable_instances"] == records


def test_final_accepted_true_when_clean(tmp_path: Path) -> None:
    review = _load_json(_publish(tmp_path, keep_all_on_remaining_blocks=True).review)
    assert review["final_accepted"] is True
    assert review["final_blockers"] == []


def test_final_accepted_blocked_by_missing_decisions(tmp_path: Path) -> None:
    """블록이 있는데 판정이 하나도 없다 — 분석이 일어나지 않은 패키지는 승인이 아니다."""
    package = _publish(tmp_path, front_on_first_block=False)
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_missing"]
    assert [row["code"] for row in review["review"]] == [
        "decision_missing",
        "decision_missing",
    ], "실패는 사람이 읽는 파일에도 명시돼야 한다"


def test_final_accepted_blocked_by_error_decisions(tmp_path: Path) -> None:
    """모든 판정이 오류다 — 429·뒤틀린 응답만 남은 패키지도 승인이 아니다."""
    package = _publish(tmp_path, front_on_first_block=False, error_on_all_blocks=True)
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_error"]


def test_final_accepted_blocked_by_inconsistent_decisions(tmp_path: Path) -> None:
    """모순 판정은 모델 계약 위반이다 — 나머지가 전부 완결이어도 막는다."""
    package = _publish(
        tmp_path, inconsistent_on_first_block=True, keep_all_on_remaining_blocks=True
    )
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["decision_inconsistent"]


def test_final_accepted_blocked_by_guard_overlap(tmp_path: Path) -> None:
    """보호 겹침 마커가 XML에 있는 '최종'은 모순이다 — 판정은 물러나고 승인도 막힌다."""
    package = _publish(
        tmp_path,
        protected=((300, 360),),
        front_on_first_block=True,
        middle_on_second_block=True,
    )
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["guard_overlap"]


def test_final_accepted_blocked_by_source_unresolved_cues(tmp_path: Path) -> None:
    """SOURCE에서 접힌 큐는 EDIT 이슈를 남기지 않는다 — 출처 목록이 유일한 증거다."""
    destination = tmp_path / "export"
    bundle = build_plan(
        tmp_path,
        (CLIP("a1", duration_frames=500),),
        front_on_first_block=True,
        keep_all_on_remaining_blocks=True,
    )
    projection = build_projection(
        bundle.document, (cue(bundle.document, "c1", Fraction(1), Fraction(2)),)
    )
    collapsed = replace(projection, unresolved_cue_ids=("cue-gone",))
    captions = project_edit_captions(bundle.plan, collapsed)
    assert not [issue for issue in captions.review if issue.code == SUBMS_COLLAPSED], (
        "EDIT 이슈 목록은 비어 있다 — 이 함정을 잡는 게 이 테스트다"
    )
    package = publish_package(
        destination,
        bundle.document,
        bundle.media,
        bundle.plan,
        captions,
        collapsed,
        bundle.guards,
    )
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert review["final_blockers"] == ["unresolved_source_cues"], (
        "빈 EDIT 이슈 목록만 보고 승인하면 SOURCE에서 접힌 큐가 조용히 사라진다"
    )
    assert review["unresolved_cue_ids"] == ["cue-gone"]


def test_final_accepted_blocked_by_unresolved_requests(tmp_path: Path) -> None:
    destination = tmp_path / "export"
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),), front_on_first_block=True)
    projection = replace(
        build_projection(bundle.document, ()),
        unresolved_request_ids=("req-9",),
    )
    captions = project_edit_captions(bundle.plan, projection)
    package = publish_package(
        destination,
        bundle.document,
        bundle.media,
        bundle.plan,
        captions,
        projection,
        bundle.guards,
    )
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert "unresolved_boundary_requests" in review["final_blockers"]


def test_final_accepted_blocked_by_edit_collapse_issues(tmp_path: Path) -> None:
    destination = tmp_path / "export"
    bundle = build_plan(tmp_path, (CLIP("a1", duration_frames=500),), front_on_first_block=True)
    shrinking = cue(bundle.document, "c1", Fraction(2, 5), Fraction(2001, 5000))
    projection = build_projection(bundle.document, (shrinking,))
    captions = project_edit_captions(bundle.plan, projection)
    assert any(issue.code == SUBMS_COLLAPSED for issue in captions.review)
    package = publish_package(
        destination,
        bundle.document,
        bundle.media,
        bundle.plan,
        captions,
        projection,
        bundle.guards,
    )
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert "subms_collapsed_edit_cues" in review["final_blockers"]
    collapsed = review["collapsed_cues"][0]
    assert collapsed["cue_id"] == "c1"
    assert collapsed["timeline_start"] == "2/5"


def test_declared_no_audio_alone_still_reaches_final(tmp_path: Path) -> None:
    """선언된 무음은 완결된 기록이다 — 단독으로는 최종 승인을 막지 않는다."""
    package = _publish(
        tmp_path,
        unavailable_second_asset="declared_no_audio",
        keep_all_on_remaining_blocks=True,
    )
    review = _load_json(package.review)
    assert review["final_accepted"] is True
    assert review["final_blockers"] == []
    assert review["unavailable_instances"][0]["reason_code"] == "declared_no_audio"


def test_retryable_unavailable_blocks_final(tmp_path: Path) -> None:
    package = _publish(tmp_path, unavailable_second_asset="pcm_unavailable")
    review = _load_json(package.review)
    assert review["final_accepted"] is False
    assert "retryable_unavailable_instances" in review["final_blockers"]


# ─────────────────────────────────────────────────────────────
#  게시 — 실패 경로와 원자성
# ─────────────────────────────────────────────────────────────


def test_invalid_xml_refusal_keeps_prior_revision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증 실패는 포인터 이전에 멈춘다 — 옛 리비전과 포인터는 온전하다."""
    from vlogsrt.project import xml_validation

    destination = tmp_path / "export"
    first = _publish(tmp_path / "scene", destination, front_on_first_block=False)
    before = os.readlink(destination / "current")
    before_bytes = first.fcpxml.read_bytes()

    def refuse(path: Path, *, dtd_path: Path | None = None) -> xml_validation.XmlValidation:
        return xml_validation.XmlValidation(
            valid=False, diagnostics=("가짜 진단 — 게시되면 안 된다",), dtd_checked=False
        )

    monkeypatch.setattr(xml_validation, "validate_xml", refuse)
    with pytest.raises(ValueError, match="가짜 진단"):
        _publish(tmp_path / "scene2", destination, front_on_first_block=True)
    assert os.readlink(destination / "current") == before
    assert first.fcpxml.read_bytes() == before_bytes
    assert {entry.name for entry in (destination / "revisions").iterdir()} == {
        before.removeprefix("revisions/")
    }
    assert not (destination / ".vlogsrt-publish.lock").exists()


def test_missing_xmllint_cannot_publish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """판정을 못 내리는 환경은 성공 패키지를 낼 수 없다 — 예외가 그대로 샌다."""
    destination = tmp_path / "export"
    monkeypatch.setenv("PATH", "")
    with pytest.raises(XmlValidationUnavailable):
        _publish(tmp_path, destination)
    assert not (destination / "current").exists()
    assert list((destination / "revisions").iterdir()) == []
    assert not (destination / ".vlogsrt-publish.lock").exists()


def _refuse_destination(tmp_path: Path, destination: Path) -> None:
    with pytest.raises(ValueError):
        _publish(tmp_path, destination)


def test_unmanaged_file_destination_is_untouched(tmp_path: Path) -> None:
    destination = tmp_path / "user-file"
    destination.write_bytes(b"user content\n")
    _refuse_destination(tmp_path, destination)
    assert destination.read_bytes() == b"user content\n"


def test_unmanaged_directory_is_untouched(tmp_path: Path) -> None:
    destination = tmp_path / "user-dir"
    destination.mkdir()
    (destination / "user.txt").write_text("사용자 파일", encoding="utf-8")
    _refuse_destination(tmp_path, destination)
    assert (destination / "user.txt").read_text(encoding="utf-8") == "사용자 파일"
    assert list(destination.iterdir()) == [destination / "user.txt"]


def test_invalid_pointer_layout_is_refused(tmp_path: Path) -> None:
    destination = tmp_path / "export"
    (destination / "revisions").mkdir(parents=True)
    (destination / "current").write_text("not a symlink", encoding="utf-8")
    _refuse_destination(tmp_path, destination)
    assert (destination / "current").read_text(encoding="utf-8") == "not a symlink"


def test_dangling_pointer_is_refused(tmp_path: Path) -> None:
    destination = tmp_path / "export"
    (destination / "revisions").mkdir(parents=True)
    (destination / "current").symlink_to("revisions/ghost")
    _refuse_destination(tmp_path, destination)
    assert (destination / "current").is_symlink()
    assert os.readlink(destination / "current") == "revisions/ghost"


def test_stale_lock_is_never_stolen_by_age(tmp_path: Path) -> None:
    """오래된 잠금도 나이만으로 훔치지 않는다 — 명시적 복구를 기다린다."""
    destination = tmp_path / "export"
    destination.mkdir()
    (destination / "revisions").mkdir()
    lock = destination / ".vlogsrt-publish.lock"
    lock.write_text("죽은 작성자", encoding="utf-8")
    old = lock.stat().st_atime - 86_400
    os.utime(lock, (old, old))
    _refuse_destination(tmp_path, destination)
    assert lock.read_text(encoding="utf-8") == "죽은 작성자"
    assert not (destination / "current").exists()
    assert list((destination / "revisions").iterdir()) == []


def test_live_lock_conflict_leaves_pointer_untouched(tmp_path: Path) -> None:
    """잠금을 쥔 다른 작성자가 있으면 결정적으로 실패한다 — current를 못 건드린다."""
    destination = tmp_path / "export"
    first = _publish(tmp_path / "scene", destination, front_on_first_block=False)
    before = os.readlink(destination / "current")
    lock = destination / ".vlogsrt-publish.lock"
    lock.write_text("다른 작성자", encoding="utf-8")
    with pytest.raises(ValueError, match="잠금"):
        _publish(tmp_path / "scene2", destination, front_on_first_block=True)
    assert os.readlink(destination / "current") == before
    assert first.fcpxml.is_file()
    assert lock.read_text(encoding="utf-8") == "다른 작성자"


def test_same_revision_conflict_is_deterministic(tmp_path: Path) -> None:
    """같은 리비전의 재게시는 배타 생성에서 실패한다 — 덮어쓰지 않는다."""
    destination = tmp_path / "export"
    first = _publish(tmp_path / "scene", destination)
    before = first.fcpxml.read_bytes()
    with pytest.raises(ValueError, match="리비전"):
        _publish(tmp_path / "scene", destination)
    assert first.fcpxml.read_bytes() == before
    assert os.readlink(destination / "current").endswith(first.plan_revision)


def test_reader_consistency_across_a_pointer_switch(tmp_path: Path) -> None:
    """포인터가 갈아끼워지는 동안 독자는 한 리비전만 본다 — 옛 길은 그대로 산다."""
    destination = tmp_path / "export"
    first = _publish(tmp_path / "scene", destination, front_on_first_block=False)
    second = _publish(tmp_path / "scene2", destination, front_on_first_block=True)
    assert first.plan_revision != second.plan_revision
    resolved = _revision_dir(destination)
    assert resolved.name == second.plan_revision
    assert {entry.name for entry in resolved.iterdir()} == PACKAGE_NAMES
    # 옛 리비전의 경로는 여전히 옛 내용을 준다 — 불변 디렉터리다.
    assert first.fcpxml.is_file()
    assert first.fcpxml.read_bytes() != second.fcpxml.read_bytes()
    assert _load_json(first.project_trace)["plan_revision"] == first.plan_revision


def test_published_fcpxml_reparses_and_shortens(tmp_path: Path) -> None:
    """패키지의 FCPXML은 지원 구조로 다시 읽히고 실제로 짧아졌다."""
    package = _publish(tmp_path)
    reparsed = parse_project(package.fcpxml)
    assert reparsed.sequence_tc_start == 0
    clips = [item for item in reparsed.storyline if isinstance(item, ClipInstance)]
    assert [(clip.offset, clip.duration) for clip in clips] == [
        (Fraction(0), Fraction(3)),
        (Fraction(3), Fraction(10)),
    ]
    assert clips[1].start == Fraction(10)
    trace = _load_json(package.project_trace)
    assert (
        trace["artifacts"][FCPXML_NAME]["sha256"]
        == hashlib.sha256(package.fcpxml.read_bytes()).hexdigest()
    )


@needs_installed_dtd
def test_published_fcpxml_passes_the_installed_dtd(tmp_path: Path) -> None:
    """설치된 1.14 DTD 대조 — 마커 자식 위치·속성이 국소 규칙을 지킨다."""
    from vlogsrt.project.xml_validation import validate_xml

    package = _publish(tmp_path, tmp_path / "export", dtd_path=INSTALLED_DTD)
    result = validate_xml(package.fcpxml, dtd_path=INSTALLED_DTD)
    assert result.valid is True, result.diagnostics


# ─────────────────────────────────────────────────────────────
#  검사기 확장 — 20번째 검사는 export.py까지 본다
# ─────────────────────────────────────────────────────────────


def _fake_tree(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str]) -> None:
    for rel, body in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(check_invariants, "ROOT", tmp_path)
    monkeypatch.setattr(check_invariants, "PACKAGE", tmp_path / "vlogsrt")
    monkeypatch.setattr(check_invariants, "UI_DIR", tmp_path / "ui")
    monkeypatch.setattr(check_invariants, "PROJECT_PACKAGE", tmp_path / "vlogsrt" / "project")


def test_invariant_covers_export_module(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """내보내기 계층이 오디오를 import하면 20번째 검사가 잡는다."""
    _fake_tree(
        monkeypatch,
        tmp_path,
        {
            "vlogsrt/project/edit.py": "",
            "vlogsrt/project/export.py": "from vlogsrt.project.pcm import PcmReference\n",
        },
    )
    failures = check_invariants.check_edit_layer_ignores_audio()
    assert len(failures) == 1, failures
    assert "vlogsrt/project/export.py" in failures[0]


def test_invariant_passes_the_real_export_module() -> None:
    """진짜 export.py는 확장된 검사를 통과한다."""
    assert check_invariants.check_edit_layer_ignores_audio() == []
