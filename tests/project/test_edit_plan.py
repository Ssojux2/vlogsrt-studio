"""P6-02 편집 계획 — 판정 적용·리플·기계 검증 (계약).

브리프(p6-02-edit-plan.md)의 정본:

- **해시 선검사가 첫 관문이다.** 블록을 하나도 읽기 전에 문서·설정 해시가
  파티션과 같아야 한다 — 어긋나면 섞인 리비전이라 raise다.
- **판정은 정본 투영으로 증명한다.** 적용 직전에 `vision_request_for` 투영의
  `request_sha256`이 판정의 것과 같은지 본다 — 다르면 섞인 리비전이다.
- **적용은 `status`로만 한다.** 세 조건의 교집합이고 재검사는 좁히기만 한다.
  `confidence`·`reason`에서 승인을 다시 유도하지 않는다(NaN 우회 금지).
- **보호 프레임은 절대 지우지 않는다.** 스냅이 보호와 겹치면 그 판정만
  keep_all로 물러나고 리뷰에 남는다(규칙 9).
- **검증은 규칙 9를 기계로 지킨다.** 프레임 보존·출력 타일링·보호·잠금·부재·
  최소 1프레임 — 비어 있지 않으면 P6-04가 렌더를 거부한다.

합성 XML·손 미디어·손 guard·손 판정만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.boundary import UnavailableInstance
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.edit import (
    BlockPartition,
    EditBlock,
    RemovedInterval,
    RetainedGap,
    RetainedInterval,
    ReviewIssue,
    build_edit_plan,
    partition_blocks,
    validate_edit_plan,
    vision_request_for,
)
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.timebase import FrameInterval, format_fcpx_time
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vision.policy import VisionDecision, request_sha256

FD25 = Fraction(1, 25)


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어·손 guard·손 판정
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
    status: str = "success",
    protected_by_clip: dict[int, tuple[FrameInterval, ...]] | None = None,
) -> tuple[InstanceGuard, ...]:
    """문서의 클립마다 guard 하나 — 스토리라인 순서, 보호는 소스 격자 프레임."""
    content = {row.resource_scoped_id: row.asset_content_id for row in media}
    protected_by_clip = protected_by_clip or {}
    return tuple(
        InstanceGuard(
            instance_id=clip.instance_id,
            source_id=content[clip.resource_scoped_id],
            status=status,  # type: ignore[arg-type]
            protected_frames=protected_by_clip.get(position, ()),
            review=(),
        )
        for position, clip in enumerate(_clips(document))
    )


def _partition(
    document: ProjectDocument,
    media: tuple[ResolvedMedia, ...],
    guards: tuple[InstanceGuard, ...],
    *,
    locked: frozenset[str] = frozenset(),
    unavailable: tuple[UnavailableInstance, ...] = (),
    cfg: ProjectConfig | None = None,
) -> BlockPartition:
    return partition_blocks(document, media, guards, locked, unavailable, cfg or ProjectConfig())


def _decision(
    block: EditBlock,
    choice: str,
    *,
    status: str = "accepted",
    confidence: float = 0.9,
    request: str | None = None,
    reason: str = "같은 내용의 단순 되풀이 B-roll이다",
) -> VisionDecision:
    """손 판정 — 요청 신원은 기본으로 정본 투영에서 만든다."""
    return VisionDecision(
        block_id=block.block_id,
        request_sha256=(
            request if request is not None else request_sha256(vision_request_for(block, ""))
        ),
        choice=choice,
        reason=reason,
        evidence_frame_ids=(),
        confidence=confidence,
        status=status,  # type: ignore[arg-type]
    )


def _front(block: EditBlock) -> VisionDecision:
    """블록의 앞 후보를 고른 승인 판정."""
    return _decision(block, block.keep_candidates[0].candidate_id)


def _scene(
    tmp_path: Path,
    clips: tuple[_ClipSpec | _GapSpec, ...],
    *,
    assets: tuple[_AssetSpec, ...] = (_AssetSpec("a1"),),
    has_audio: bool = True,
    tc_start_frames: int = 0,
    content: dict[str, str] | None = None,
) -> tuple[ProjectDocument, tuple[ResolvedMedia, ...]]:
    document = _project(
        tmp_path,
        tuple(
            replace(asset, has_audio=has_audio) if asset.xml_id == "a1" else asset
            for asset in assets
        ),
        clips,
        tc_start_frames=tc_start_frames,
    )
    return document, _media(document, content)


def _standard(
    tmp_path: Path,
) -> tuple[ProjectDocument, tuple[InstanceGuard, ...], BlockPartition]:
    """기본 장면 — 25fps 500프레임 클립, 블록 [0,250)·[250,500), 보호 없음."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media)
    return document, guards, _partition(document, media, guards)


def _plan(
    document: ProjectDocument,
    partition: BlockPartition,
    guards: tuple[InstanceGuard, ...],
    decisions: tuple[VisionDecision, ...] = (),
    *,
    cfg: ProjectConfig | None = None,
):
    return build_edit_plan(document, partition, guards, decisions, cfg or ProjectConfig())


def _unavailable(clip: ClipInstance, document: ProjectDocument, reason: str) -> UnavailableInstance:
    resource = next(r for r in document.resources if r.scoped_id == clip.resource_scoped_id)
    return UnavailableInstance(
        resource_scoped_id=resource.scoped_id,
        instance_id=clip.instance_id,
        reason_code=reason,  # type: ignore[arg-type]
    )


# ─────────────────────────────────────────────────────────────
#  리비전 관문 — 해시가 먼저다
# ─────────────────────────────────────────────────────────────


def test_stale_partition_input_hash_raises(tmp_path: Path) -> None:
    """다른 문서의 파티션은 섞인 리비전이다 — 블록을 읽기 전에 닫는다."""
    document, guards, partition = _standard(tmp_path)
    other, _ = _scene(tmp_path / "other", clips=(_ClipSpec("a1", duration_frames=550),))
    with pytest.raises(ValueError, match="리비전"):
        build_edit_plan(other, partition, guards, (), ProjectConfig())


def test_stale_config_hash_raises(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    with pytest.raises(ValueError, match="리비전"):
        build_edit_plan(document, partition, guards, (), ProjectConfig(keep_seconds=Fraction(5, 2)))


def test_request_identity_mismatch_raises(tmp_path: Path) -> None:
    """승인 판정의 요청 신원이 이 블록 투영과 다르면 섞인 리비전이다."""
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    stale = _decision(block, block.keep_candidates[0].candidate_id, request="0" * 64)
    with pytest.raises(ValueError, match="요청 신원"):
        _plan(document, partition, guards, (stale,))


# ─────────────────────────────────────────────────────────────
#  판정 연결 — 모르는 아이디는 raise다
# ─────────────────────────────────────────────────────────────


def test_unknown_block_id_raises(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    alien = replace(_front(partition.blocks[0]), block_id="f" * 64)
    with pytest.raises(ValueError, match="없는 블록"):
        _plan(document, partition, guards, (alien,))


def test_choice_from_another_block_raises(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    foreign = _decision(partition.blocks[0], partition.blocks[1].keep_candidates[0].candidate_id)
    with pytest.raises(ValueError, match="남의 블록"):
        _plan(document, partition, guards, (foreign,))


def test_unknown_choice_raises(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    bogus = _decision(partition.blocks[0], "candidate-that-never-was")
    with pytest.raises(ValueError, match="알려진 후보"):
        _plan(document, partition, guards, (bogus,))


# ─────────────────────────────────────────────────────────────
#  적용 — 세 조건의 교집합
# ─────────────────────────────────────────────────────────────


def test_accepted_front_candidate_removes_the_rest(tmp_path: Path) -> None:
    """앞 후보 승인 — 후보는 유지, 블록 나머지는 제거, 출력은 리플로 당겨진다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    clip = _clips(document)[0]
    assert plan.retained == (
        RetainedInterval(
            clip.instance_id,
            FrameInterval(0, 75),
            FrameInterval(0, 75),
            FrameInterval(0, 75),
            "kept_candidate",
        ),
        RetainedInterval(
            clip.instance_id,
            FrameInterval(250, 500),
            FrameInterval(250, 500),
            FrameInterval(75, 325),
            "kept_block",
        ),
    )
    assert plan.removed == (
        RemovedInterval(clip.instance_id, FrameInterval(75, 250), FrameInterval(75, 250), 75),
    )
    assert plan.gaps == ()
    assert validate_edit_plan(plan, document, guards) == ()


def test_accepted_middle_candidate_removes_both_sides(tmp_path: Path) -> None:
    """중앙 후보 — 머리·꼬리 둘 다 제거되고 되돌릴 자리는 각자 출력 커서다."""
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    middle = _decision(block, block.keep_candidates[1].candidate_id)
    plan = _plan(document, partition, guards, (middle,))
    clip = _clips(document)[0]
    assert plan.removed == (
        RemovedInterval(clip.instance_id, FrameInterval(0, 88), FrameInterval(0, 88), 0),
        RemovedInterval(clip.instance_id, FrameInterval(163, 250), FrameInterval(163, 250), 75),
    )
    assert plan.retained[0].source_frames == FrameInterval(88, 163)
    assert plan.retained[0].reason == "kept_candidate"


def test_two_blocks_of_one_clip_ripple_together(tmp_path: Path) -> None:
    """한 클립의 두 블록에 각각 적용 — 인접한 제거 둘은 합쳐지지 않고 각자 남는다."""
    document, guards, partition = _standard(tmp_path)
    first = _front(partition.blocks[0])  # 앞 후보: 제거 [75,250)
    last = _decision(
        partition.blocks[1], partition.blocks[1].keep_candidates[2].candidate_id
    )  # 뒤 후보: 제거 [250,425)
    plan = _plan(document, partition, guards, (first, last))
    clip = _clips(document)[0]
    assert plan.removed == (
        RemovedInterval(clip.instance_id, FrameInterval(75, 250), FrameInterval(75, 250), 75),
        RemovedInterval(clip.instance_id, FrameInterval(250, 425), FrameInterval(250, 425), 75),
    )
    assert [row.source_frames for row in plan.retained] == [
        FrameInterval(0, 75),
        FrameInterval(425, 500),
    ]
    assert [row.output_timeline_frames for row in plan.retained] == [
        FrameInterval(0, 75),
        FrameInterval(75, 150),
    ]


def test_single_block_clip_never_loses_whole_clip(tmp_path: Path) -> None:
    """블록 하나짜리 클립 — 후보를 적용해도 클립은 한 프레임 이상 남는다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=150),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    (block,) = partition.blocks
    end_choice = _decision(block, block.keep_candidates[2].candidate_id)
    plan = _plan(document, partition, guards, (end_choice,))
    clip = _clips(document)[0]
    assert [row.source_frames for row in plan.retained] == [FrameInterval(75, 150)]
    assert plan.removed == (
        RemovedInterval(clip.instance_id, FrameInterval(0, 75), FrameInterval(0, 75), 0),
    )
    assert validate_edit_plan(plan, document, guards) == ()


def test_low_confidence_decision_keeps_block(tmp_path: Path) -> None:
    """파싱에서 이미 keep_all로 정규화된 낮은 신뢰도 — 계획은 그대로 둔다."""
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    low = _decision(
        block,
        block.keep_candidates[0].candidate_id,
        status="keep_all",
        confidence=0.5,
    )
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (low, settled))
    assert plan.removed == ()
    assert plan.review == ()
    assert [row.reason for row in plan.retained] == ["kept_block", "kept_block"]


def test_error_decision_keeps_block_with_failed_review(tmp_path: Path) -> None:
    """오류 판정은 블록을 남기고 리뷰로 남는다 — 최종 수용은 게시 관문이 막는다."""
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    error = _decision(block, "keep_all", status="error", confidence=0.0)
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (error, settled))
    assert plan.removed == ()
    assert [issue.code for issue in plan.review] == ["decision_error"]
    assert plan.review[0].instance_id is None and plan.review[0].source_frame is None
    assert [row.reason for row in plan.retained] == ["kept_block", "kept_block"]


def test_missing_decisions_keep_everything_with_pending_review(tmp_path: Path) -> None:
    """판정이 없는 블록은 통째 유지 — 없음은 무권한이고 대기 증거는 리뷰에 남는다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards)
    assert plan.removed == ()
    assert [issue.code for issue in plan.review] == ["decision_missing", "decision_missing"]
    assert all(issue.instance_id is None for issue in plan.review)
    assert [row.source_frames for row in plan.retained] == [
        FrameInterval(0, 250),
        FrameInterval(250, 500),
    ]
    assert validate_edit_plan(plan, document, guards) == ()


def test_plan_time_threshold_narrows_without_review(tmp_path: Path) -> None:
    """계획 시점 한도가 파싱 시점보다 높으면 좁혀질 뿐 — 리뷰가 아니라 유지다."""
    cfg = ProjectConfig(vision_apply_confidence=0.9)
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards, cfg=cfg)
    block = partition.blocks[0]
    borderline = _decision(block, block.keep_candidates[0].candidate_id, confidence=0.85)
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (borderline, settled), cfg=cfg)
    assert plan.removed == ()
    assert plan.review == ()
    assert validate_edit_plan(plan, document, guards) == ()


# ─────────────────────────────────────────────────────────────
#  모순 판정 — keep_all + decision_inconsistent 리뷰
# ─────────────────────────────────────────────────────────────


def test_accepted_with_keep_all_choice_is_inconsistent(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    inconsistent = _decision(partition.blocks[0], "keep_all")
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (inconsistent, settled))
    assert plan.removed == ()
    assert [issue.code for issue in plan.review] == ["decision_inconsistent"]
    assert plan.review[0].instance_id is None
    assert plan.review[0].source_frame is None


def test_accepted_with_uncertain_choice_is_inconsistent(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    inconsistent = _decision(partition.blocks[0], "uncertain")
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (inconsistent, settled))
    assert [issue.code for issue in plan.review] == ["decision_inconsistent"]


def test_duplicate_decisions_for_one_block_are_inconsistent(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (_front(block), _front(block), settled))
    assert plan.removed == ()
    assert [issue.code for issue in plan.review] == ["decision_inconsistent"]


@pytest.mark.parametrize("status", ["accepted", "keep_all"])
def test_non_finite_confidence_is_inconsistent(tmp_path: Path, status: str) -> None:
    """NaN은 비교를 무력화한다 — 승인 재유도 없이 모순으로 유지된다."""
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    nan = _decision(
        block,
        block.keep_candidates[0].candidate_id if status == "accepted" else "keep_all",
        status=status,
        confidence=float("nan"),
    )
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    plan = _plan(document, partition, guards, (nan, settled))
    assert plan.removed == ()
    assert [issue.code for issue in plan.review] == ["decision_inconsistent"]


# ─────────────────────────────────────────────────────────────
#  guard 충돌 — 보호 프레임은 절대 지우지 않는다
# ─────────────────────────────────────────────────────────────


def test_guard_collision_rejects_decision_to_keep_all(tmp_path: Path) -> None:
    """새 guard 세대의 보호가 블록 안으로 들어오면 그 판정만 물러난다."""
    document, guards, partition = _standard(tmp_path)
    stricter = _guards(
        document, _media(document), protected_by_clip={0: (FrameInterval(100, 200),)}
    )
    settled = _decision(partition.blocks[1], "keep_all", status="keep_all")
    collided = build_edit_plan(
        document, partition, stricter, (_front(partition.blocks[0]), settled), ProjectConfig()
    )
    clip = _clips(document)[0]
    assert collided.removed == ()
    assert [row.reason for row in collided.retained] == ["kept_block", "kept_block"]
    assert [issue.code for issue in collided.review] == ["guard_overlap"]
    assert collided.review[0].instance_id == clip.instance_id
    assert collided.review[0].source_frame == 100
    assert validate_edit_plan(collided, document, stricter) == ()


def test_guard_collision_on_tail_piece_names_first_frame(tmp_path: Path) -> None:
    """뒤 후보의 꼬리 제거가 보호와 겹치는 첫 프레임을 짚는다."""
    document, guards, partition = _standard(tmp_path)
    stricter = _guards(
        document, _media(document), protected_by_clip={0: (FrameInterval(160, 200),)}
    )
    block = partition.blocks[0]
    end_choice = _decision(block, block.keep_candidates[2].candidate_id)
    plan = build_edit_plan(document, partition, stricter, (end_choice,), ProjectConfig())
    # 뒤 후보 [175,250)의 제거는 [0,175) — 겹침은 160에서 시작한다.
    assert plan.removed == ()
    assert plan.review[0].code == "guard_overlap"
    assert plan.review[0].source_frame == 160


# ─────────────────────────────────────────────────────────────
#  기하 — 리플·갭·순서·원점
# ─────────────────────────────────────────────────────────────


def test_gap_keeps_duration_and_output_position(tmp_path: Path) -> None:
    """갭 사이 두 클립이 짧아져도 갭은 길이·순서 그대로 출력에 선다."""
    clips: tuple[_ClipSpec | _GapSpec, ...] = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=300),
        _GapSpec(offset_frames=300, duration_frames=100),
        _ClipSpec("a1", offset_frames=400, start_frames=300, duration_frames=300),
    )
    document, media = _scene(tmp_path, clips=clips)
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    first, second = _clips(document)
    decisions = (
        _front(partition.blocks[0]),
        _front(partition.blocks[2]),
    )
    plan = _plan(document, partition, guards, decisions)
    assert plan.gaps == (
        RetainedGap(
            document.storyline[1].instance_id,
            FrameInterval(300, 400),
            FrameInterval(225, 325),
        ),
    )
    assert plan.removed == (
        RemovedInterval(first.instance_id, FrameInterval(75, 150), FrameInterval(75, 150), 75),
        RemovedInterval(second.instance_id, FrameInterval(375, 450), FrameInterval(475, 550), 400),
    )
    assert [(row.instance_id, row.output_timeline_frames) for row in plan.retained] == [
        (first.instance_id, FrameInterval(0, 75)),
        (first.instance_id, FrameInterval(75, 225)),
        (second.instance_id, FrameInterval(325, 400)),
        (second.instance_id, FrameInterval(400, 550)),
    ]
    assert validate_edit_plan(plan, document, guards) == ()


def test_protected_prefix_yields_outside_interval(tmp_path: Path) -> None:
    """보호로 블록 앞이 뚫리면 그 앞구간은 '블록 밖 유지'로 명시된다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, protected_by_clip={0: (FrameInterval(0, 100),)})
    partition = _partition(document, media, guards)
    assert [block.source_frames for block in partition.blocks] == [
        FrameInterval(100, 300),
        FrameInterval(300, 500),
    ]
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    assert [(row.source_frames, row.reason) for row in plan.retained] == [
        (FrameInterval(0, 100), "kept_outside_blocks"),
        (FrameInterval(100, 175), "kept_candidate"),
        (FrameInterval(300, 500), "kept_block"),
    ]
    assert [row.output_timeline_frames for row in plan.retained] == [
        FrameInterval(0, 100),
        FrameInterval(100, 175),
        FrameInterval(175, 375),
    ]


def test_nonzero_origins_flow_into_plan(tmp_path: Path) -> None:
    """애셋 시작 2500f·tcStart 100f — 소스·입력 타임라인이 각자 앵커를 지킨다."""
    document, media = _scene(
        tmp_path,
        clips=(_ClipSpec("a1", offset_frames=50, start_frames=100, duration_frames=500),),
        assets=(_AssetSpec("a1", start_frames=2500),),
        tc_start_frames=100,
    )
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    assert plan.removed == (
        RemovedInterval(
            _clips(document)[0].instance_id,
            FrameInterval(175, 350),
            FrameInterval(125, 300),
            75,
        ),
    )
    assert [(row.source_frames, row.input_timeline_frames) for row in plan.retained] == [
        (FrameInterval(100, 175), FrameInterval(50, 125)),
        (FrameInterval(350, 600), FrameInterval(300, 550)),
    ]


# ─────────────────────────────────────────────────────────────
#  잠금·부재 — 제거 권한이 없는 인스턴스
# ─────────────────────────────────────────────────────────────


def test_locked_instance_is_carried_whole(tmp_path: Path) -> None:
    """잠긴 인스턴스는 블록도 없고 통째 유지다 — 집합은 계획에 그대로 흐른다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media)
    clip = _clips(document)[0]
    partition = _partition(document, media, guards, locked=frozenset({clip.instance_id}))
    plan = _plan(document, partition, guards)
    assert plan.locked_instance_ids == frozenset({clip.instance_id})
    assert [(row.source_frames, row.reason) for row in plan.retained] == [
        (FrameInterval(0, 500), "kept_locked"),
    ]
    assert plan.removed == ()
    assert plan.frame_duration == document.frame_duration
    assert validate_edit_plan(plan, document, guards) == ()


def test_guard_failure_keeps_whole_clip_with_reason(tmp_path: Path) -> None:
    """keep_all guard — 분석 실패는 제거 권한이 없다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, status="keep_all")
    partition = _partition(document, media, guards)
    plan = _plan(document, partition, guards)
    assert [(row.source_frames, row.reason) for row in plan.retained] == [
        (FrameInterval(0, 500), "kept_guard"),
    ]


def test_short_run_clip_keeps_whole_clip_with_reason(tmp_path: Path) -> None:
    """적격 런이 없는 클립 — 블록 없음도 사유로 남는다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=100),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    plan = _plan(document, partition, guards)
    assert [(row.source_frames, row.reason) for row in plan.retained] == [
        (FrameInterval(0, 100), "kept_no_blocks"),
    ]


@pytest.mark.parametrize(
    ("reason", "has_audio", "guard_status"),
    [
        ("declared_no_audio", False, "inaudible"),
        ("stream_unavailable", True, "keep_all"),
        ("pcm_unavailable", True, "keep_all"),
    ],
)
def test_unavailable_records_carry_through_typed(
    tmp_path: Path, reason: str, has_audio: bool, guard_status: str
) -> None:
    """세 사유 전부 — 기록은 타입 그대로 흐르고 인스턴스는 통째 유지다."""
    document, media = _scene(
        tmp_path, clips=(_ClipSpec("a1", duration_frames=500),), has_audio=has_audio
    )
    guards = _guards(document, media, status=guard_status)
    clip = _clips(document)[0]
    record = _unavailable(clip, document, reason)
    partition = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
    )
    plan = _plan(document, partition, guards)
    assert plan.unavailable_instances == (record,)
    assert [(row.source_frames, row.reason) for row in plan.retained] == [
        (FrameInterval(0, 500), "kept_unavailable"),
    ]
    assert plan.removed == ()
    assert validate_edit_plan(plan, document, guards) == ()


def test_unavailable_review_issues_are_unlocated_and_coded(tmp_path: Path) -> None:
    """부재 리뷰는 기록마다 하나, 위치 없음 — 선언된 무음과 재시도는 코드가 갈린다."""
    codes: dict[str, str] = {}
    for reason, has_audio, guard_status in (
        ("declared_no_audio", False, "inaudible"),
        ("stream_unavailable", True, "keep_all"),
        ("pcm_unavailable", True, "keep_all"),
    ):
        document, media = _scene(
            tmp_path / reason, clips=(_ClipSpec("a1", duration_frames=500),), has_audio=has_audio
        )
        guards = _guards(document, media, status=guard_status)
        clip = _clips(document)[0]
        record = _unavailable(clip, document, reason)
        partition = _partition(
            document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
        )
        plan = _plan(document, partition, guards)
        (issue,) = plan.review
        assert issue.code == f"unavailable_{reason}"
        assert issue.instance_id is None
        assert issue.source_frame is None
        codes[reason] = issue.code
    assert len(set(codes.values())) == 3


def test_forged_partition_with_block_for_locked_instance_raises(tmp_path: Path) -> None:
    """잠긴 인스턴스에 블록이 있는 파티션은 위조다 — build가 닫는다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media)
    clip = _clips(document)[0]
    unlocked = _partition(document, media, guards)
    locked = _partition(document, media, guards, locked=frozenset({clip.instance_id}))
    forged = replace(locked, blocks=unlocked.blocks)
    with pytest.raises(ValueError, match="잠긴·부재"):
        _plan(document, forged, guards, (_front(unlocked.blocks[0]),))


def test_forged_partition_with_block_for_unavailable_raises(tmp_path: Path) -> None:
    """부재 인스턴스에 블록이 있는 파티션도 위조다 — 같은 문서로 증명한다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, status="keep_all")
    clip = _clips(document)[0]
    record = _unavailable(clip, document, "pcm_unavailable")
    sick = _partition(
        document,
        media,
        guards,
        locked=frozenset({clip.instance_id}),
        unavailable=(record,),
    )
    # 같은 문서·설정의 잠기지 않은 파티션에서 블록을 가져와 얹는다.
    open_guards = _guards(document, media)
    unlocked = _partition(document, media, open_guards)
    forged = replace(sick, blocks=unlocked.blocks)
    with pytest.raises(ValueError, match="잠긴·부재"):
        _plan(document, forged, open_guards, (_front(unlocked.blocks[0]),))


def test_forged_unavailable_without_lock_raises(tmp_path: Path) -> None:
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media)
    clip = _clips(document)[0]
    record = _unavailable(clip, document, "pcm_unavailable")
    partition = _partition(document, media, guards)
    forged = replace(partition, unavailable_instances=(record,))
    with pytest.raises(ValueError, match="잠금"):
        _plan(document, forged, guards)


def test_forged_duplicate_unavailable_records_raise(tmp_path: Path) -> None:
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, status="keep_all")
    clip = _clips(document)[0]
    record = _unavailable(clip, document, "stream_unavailable")
    partition = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
    )
    with pytest.raises(ValueError, match="중복"):
        _plan(document, replace(partition, unavailable_instances=(record, record)), guards)


def test_forged_unknown_unavailable_resource_raises(tmp_path: Path) -> None:
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, status="keep_all")
    clip = _clips(document)[0]
    record = UnavailableInstance(
        resource_scoped_id="sha256-nonexistent",
        instance_id=clip.instance_id,
        reason_code="pcm_unavailable",
    )
    partition = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=()
    )
    with pytest.raises(ValueError, match="자원"):
        _plan(document, replace(partition, unavailable_instances=(record,)), guards)


def test_equal_content_healthy_sibling_stays_editable(tmp_path: Path) -> None:
    """같은 내용 해시의 두 자원 — 부재 쪽은 못 자르고 건강한 쪽은 자른다."""
    clips = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
        _ClipSpec("a2", offset_frames=600, start_frames=0, duration_frames=500),
    )
    document, media = _scene(
        tmp_path,
        clips,
        assets=(_AssetSpec("a1"), _AssetSpec("a2")),
        content={"a1": "acid-shared", "a2": "acid-shared"},
    )
    healthy, sick = _clips(document)
    record = _unavailable(sick, document, "pcm_unavailable")
    # guard가 클립마다 하나여야 하므로 상태만 갈아끼운 짝을 만든다.
    mixed = (
        InstanceGuard(
            instance_id=healthy.instance_id,
            source_id="acid-shared",
            status="success",
            protected_frames=(),
            review=(),
        ),
        InstanceGuard(
            instance_id=sick.instance_id,
            source_id="acid-shared",
            status="keep_all",
            protected_frames=(),
            review=(),
        ),
    )
    partition = _partition(
        document,
        media,
        mixed,
        locked=frozenset({sick.instance_id}),
        unavailable=(record,),
    )
    plan = _plan(document, partition, mixed, (_front(partition.blocks[0]),))
    assert [row.instance_id for row in plan.removed] == [healthy.instance_id]
    assert [
        (row.instance_id, row.reason) for row in plan.retained if row.reason == "kept_unavailable"
    ] == [(sick.instance_id, "kept_unavailable")]
    assert plan.unavailable_instances == (record,)
    assert validate_edit_plan(plan, document, mixed) == ()


# ─────────────────────────────────────────────────────────────
#  리뷰 증거 — 위치는 짝이다
# ─────────────────────────────────────────────────────────────


def test_half_populated_review_issue_raises() -> None:
    with pytest.raises(ValueError, match="짝"):
        ReviewIssue("some_code", "설명", "instance", None)
    with pytest.raises(ValueError, match="짝"):
        ReviewIssue("some_code", "설명", None, 120)


def test_located_review_outside_clip_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    clip = _clips(document)[0]
    stray = ReviewIssue("guard_overlap", "경계 밖", clip.instance_id, 999_999)
    findings = validate_edit_plan(replace(plan, review=plan.review + (stray,)), document, guards)
    assert any("클립 밖" in finding for finding in findings)


def test_located_review_for_unknown_instance_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    stray = ReviewIssue("guard_overlap", "모르는 인스턴스", "ghost", 10)
    findings = validate_edit_plan(replace(plan, review=plan.review + (stray,)), document, guards)
    assert any("인스턴스" in finding for finding in findings)


# ─────────────────────────────────────────────────────────────
#  검증 — 규칙 9의 기계 점검
# ─────────────────────────────────────────────────────────────


def test_frame_conservation_violation_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    broken = replace(plan, retained=plan.retained[:1])
    findings = validate_edit_plan(broken, document, guards)
    assert any("프레임 보존" in finding for finding in findings)


def test_zero_retained_frames_is_a_finding(tmp_path: Path) -> None:
    """클립 통째 삭제의 기계적 표현 — 보존이 맞아도 한 프레임은 남아야 한다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards)
    clip = _clips(document)[0]
    emptied = replace(
        plan,
        retained=(),
        removed=(
            RemovedInterval(clip.instance_id, FrameInterval(0, 500), FrameInterval(0, 500), 0),
        ),
    )
    findings = validate_edit_plan(emptied, document, guards)
    assert any("한 프레임도" in finding for finding in findings)


def test_removed_outside_clip_bounds_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards)
    clip = _clips(document)[0]
    stray = replace(
        plan,
        retained=(
            RetainedInterval(
                clip.instance_id,
                FrameInterval(0, 300),
                FrameInterval(0, 300),
                FrameInterval(0, 300),
                "kept_block",
            ),
        ),
        removed=(
            RemovedInterval(
                clip.instance_id, FrameInterval(450, 650), FrameInterval(450, 650), 300
            ),
        ),
    )
    findings = validate_edit_plan(stray, document, guards)
    assert any("경계 밖" in finding for finding in findings)


def test_output_hole_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    holed = replace(
        plan,
        retained=(
            plan.retained[0],
            replace(plan.retained[1], output_timeline_frames=FrameInterval(300, 550)),
        ),
    )
    findings = validate_edit_plan(holed, document, guards)
    assert any("구멍" in finding for finding in findings)


def test_output_overlap_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    overlapped = replace(
        plan,
        retained=(
            plan.retained[0],
            replace(plan.retained[1], output_timeline_frames=FrameInterval(50, 300)),
        ),
    )
    findings = validate_edit_plan(overlapped, document, guards)
    assert any("겹친다" in finding for finding in findings)


def test_flipped_input_order_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards)
    flipped = replace(
        plan,
        retained=(
            replace(plan.retained[0], output_timeline_frames=FrameInterval(250, 500)),
            replace(plan.retained[1], output_timeline_frames=FrameInterval(0, 250)),
        ),
    )
    findings = validate_edit_plan(flipped, document, guards)
    assert any("순서" in finding for finding in findings)


def test_changed_gap_duration_is_a_finding(tmp_path: Path) -> None:
    clips: tuple[_ClipSpec | _GapSpec, ...] = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=300),
        _GapSpec(offset_frames=300, duration_frames=100),
        _ClipSpec("a1", offset_frames=400, start_frames=300, duration_frames=300),
    )
    document, media = _scene(tmp_path, clips=clips)
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    stretched = replace(
        plan,
        gaps=(replace(plan.gaps[0], output_timeline_frames=FrameInterval(225, 335)),),
    )
    findings = validate_edit_plan(stretched, document, guards)
    assert any("갭 길이" in finding for finding in findings)


def test_removed_in_locked_instance_is_a_finding(tmp_path: Path) -> None:
    clips = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
        _ClipSpec("a1", offset_frames=600, start_frames=0, duration_frames=300),
    )
    document, media = _scene(tmp_path, clips=clips)
    guards = _guards(document, media)
    editable, locked = _clips(document)
    partition = _partition(document, media, guards, locked=frozenset({locked.instance_id}))
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    trimmed = replace(
        plan,
        retained=tuple(row for row in plan.retained if row.instance_id != locked.instance_id)
        + (
            RetainedInterval(
                locked.instance_id,
                FrameInterval(0, 150),
                FrameInterval(600, 750),
                FrameInterval(325, 475),
                "kept_locked",
            ),
        ),
        removed=plan.removed
        + (
            RemovedInterval(
                locked.instance_id, FrameInterval(150, 300), FrameInterval(750, 900), 475
            ),
        ),
    )
    findings = validate_edit_plan(trimmed, document, guards)
    assert any("잠긴" in finding for finding in findings)


def test_removed_in_unavailable_instance_is_a_finding(tmp_path: Path) -> None:
    clips = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
        _ClipSpec("a1", offset_frames=600, start_frames=0, duration_frames=300),
    )
    document, media = _scene(tmp_path, clips=clips)
    guards = _guards(document, media)
    healthy, sick = _clips(document)
    record = _unavailable(sick, document, "pcm_unavailable")
    healthy_partition = _partition(document, media, guards)
    plan = _plan(document, healthy_partition, guards, (_front(healthy_partition.blocks[0]),))
    # 같은 문서·같은 설정이므로 해시는 통과한다 — 부재 기록만 얹고 짜본다.
    trimmed = replace(
        plan,
        locked_instance_ids=frozenset({sick.instance_id}),
        unavailable_instances=(record,),
        retained=tuple(row for row in plan.retained if row.instance_id != sick.instance_id)
        + (
            RetainedInterval(
                sick.instance_id,
                FrameInterval(0, 150),
                FrameInterval(600, 750),
                FrameInterval(325, 475),
                "kept_unavailable",
            ),
        ),
        removed=plan.removed
        + (
            RemovedInterval(
                sick.instance_id, FrameInterval(150, 300), FrameInterval(750, 900), 475
            ),
        ),
    )
    findings = validate_edit_plan(trimmed, document, guards)
    assert any("부재" in finding for finding in findings)


def test_removed_intersecting_protection_is_a_finding(tmp_path: Path) -> None:
    """검증 시점의 guard가 더 많이 보호하면 이미 만든 계획도 거부된다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    stricter = _guards(
        document, _media(document), protected_by_clip={0: (FrameInterval(100, 200),)}
    )
    findings = validate_edit_plan(plan, document, stricter)
    assert any("보호와 겹친다" in finding for finding in findings)


def test_missing_duplicate_and_extra_guards_are_findings(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    assert any("guard가 없다" in finding for finding in validate_edit_plan(plan, document, ()))
    assert any(
        "둘 이상" in finding for finding in validate_edit_plan(plan, document, guards + guards)
    )
    extra = InstanceGuard(
        instance_id="ghost-instance",
        source_id="acid-a1",
        status="success",
        protected_frames=(),
        review=(),
    )
    assert any(
        "문서에 없는" in finding
        for finding in validate_edit_plan(plan, document, guards + (extra,))
    )


def test_identity_mismatch_is_a_finding(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    stale_hash = replace(plan, input_sha256="0" * 64)
    assert any("해시" in finding for finding in validate_edit_plan(stale_hash, document, guards))
    wrong_grid = replace(plan, frame_duration=Fraction(1, 30))
    assert any(
        "프레임 길이" in finding for finding in validate_edit_plan(wrong_grid, document, guards)
    )


def test_unavailable_record_defects_are_findings(tmp_path: Path) -> None:
    document, media = _scene(
        tmp_path, clips=(_ClipSpec("a1", duration_frames=500),), has_audio=False
    )
    guards = _guards(document, media, status="inaudible")
    clip = _clips(document)[0]
    record = _unavailable(clip, document, "declared_no_audio")
    partition = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
    )
    plan = _plan(document, partition, guards)
    duplicated = replace(plan, unavailable_instances=(record, record))
    assert any("중복" in finding for finding in validate_edit_plan(duplicated, document, guards))
    bogus = UnavailableInstance(
        resource_scoped_id=document.resources[0].scoped_id,
        instance_id=clip.instance_id,
        reason_code="mystery_reason",  # type: ignore[arg-type]
    )
    relabelled = replace(plan, unavailable_instances=(bogus,))
    assert any("사유" in finding for finding in validate_edit_plan(relabelled, document, guards))


# ─────────────────────────────────────────────────────────────
#  결정성 — 같은 저장 입력은 같은 계획
# ─────────────────────────────────────────────────────────────


def test_identical_inputs_yield_identical_plan_and_revision(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    decisions = (_front(partition.blocks[0]),)
    first = _plan(document, partition, guards, decisions)
    second = _plan(document, partition, guards, decisions)
    assert first == second
    assert first.revision == second.revision


def test_revision_binds_decisions(tmp_path: Path) -> None:
    document, guards, partition = _standard(tmp_path)
    applied = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    withheld = _plan(document, partition, guards)
    assert applied.revision != withheld.revision
    assert applied.decisions == (_front(partition.blocks[0]),)


def test_revision_binds_unavailable_metadata(tmp_path: Path) -> None:
    """부재 기록이 리비전에 박힌다 — 같은 문서·설정에서도 갈라진다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, status="keep_all")
    clip = _clips(document)[0]
    record = _unavailable(clip, document, "stream_unavailable")
    with_record = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
    )
    without_record = _partition(document, media, guards, locked=frozenset({clip.instance_id}))
    assert (
        _plan(document, with_record, guards).revision
        != _plan(document, without_record, guards).revision
    )


def test_plan_actually_shortens(tmp_path: Path) -> None:
    """안전한 합성 장면은 실제로 짧아진다 — no-op 계획은 인수가 아니다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    assert plan.removed
    assert all(row.source_frames.end_frame > row.source_frames.start_frame for row in plan.removed)
    output_end = max(row.output_timeline_frames.end_frame for row in (*plan.retained, *plan.gaps))
    input_end = max(
        row.input_timeline_frames.end_frame for row in (*plan.retained, *plan.gaps, *plan.removed)
    )
    assert output_end < input_end


# ═════════════════════════════════════════════════════════════
#  P6-02b — 조용한 통과 경로를 닫는다 (Fable 재현 A·B·C·D·E + α)
# ═════════════════════════════════════════════════════════════


def test_retained_output_shorter_than_source_is_a_finding(tmp_path: Path) -> None:
    """재현 A — 보존은 소스를 세고 타일링은 출력을 세므로 둘을 묶는다.

    소스 [0,75)를 출력 [0,50)으로 내보내고 다음 타일을 [50,325)로 붙이면
    타일링은 깨끗하다 — 유지 구간마다 출력 길이 == 소스 길이가 그 구멍을 닫는다.
    """
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    shrunk = replace(
        plan,
        retained=(
            replace(plan.retained[0], output_timeline_frames=FrameInterval(0, 50)),
            replace(plan.retained[1], output_timeline_frames=FrameInterval(50, 325)),
        ),
    )
    findings = validate_edit_plan(shrunk, document, guards)
    assert any("출력 길이가 소스와" in finding for finding in findings)


def test_zero_length_output_tile_is_a_finding(tmp_path: Path) -> None:
    """재현 B — 길이 0 타일은 건너뛰지 않고 finding이다.

    소스 75프레임 유지 구간의 출력을 [0,0)으로 낸다: 유지 구간 길이 검사와
    타일링의 길이 0 검사가 둘 다 말해야 한다.
    """
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    emptied = replace(
        plan,
        retained=(
            replace(plan.retained[0], output_timeline_frames=FrameInterval(0, 0)),
            replace(plan.retained[1], output_timeline_frames=FrameInterval(0, 250)),
        ),
    )
    findings = validate_edit_plan(emptied, document, guards)
    assert any("출력 길이가 소스와" in finding for finding in findings)
    assert any("길이가 0" in finding for finding in findings)


def test_wrong_restore_frame_is_a_finding(tmp_path: Path) -> None:
    """재현 C — restore_at_output_frame은 다음 타일의 출력 시작과 같아야 한다.

    9999를 넣어도 보존·타일링은 그대로다 — 되돌릴 자리만 거짓이 된다.
    """
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    wrong = replace(plan, removed=(replace(plan.removed[0], restore_at_output_frame=9999),))
    findings = validate_edit_plan(wrong, document, guards)
    assert any("되돌릴" in finding for finding in findings)


def test_keep_all_guard_with_empty_protection_and_blocks_raises(tmp_path: Path) -> None:
    """재현 D — keep_all guard는 빈 protected여도 제거 권한이 없다.

    `protected=()`를 "보호할 게 없다"로 읽으면 `protected=[]`가 옷만 갈아입은
    것이다(P5-03b 계보). 성공 guard로 지은 파티션에 keep_all guard를 내밀면
    블록 검증이 닫는다.
    """
    document, guards, partition = _standard(tmp_path)
    demoted = _guards(document, _media(document), status="keep_all")
    with pytest.raises(ValueError, match="제거 권한"):
        build_edit_plan(
            document, partition, demoted, (_front(partition.blocks[0]),), ProjectConfig()
        )


def test_inaudible_guard_on_forged_video_only_partition_raises(tmp_path: Path) -> None:
    """재현 E — 비디오 전용 클립의 위조 파티션 + 빈 inaudible guard도 같은 줄이다."""
    document, media = _scene(
        tmp_path, clips=(_ClipSpec("a1", duration_frames=500, src_enable="video"),)
    )
    guards = _guards(document, media, status="inaudible")
    partition = _partition(document, media, guards)
    forged_block = EditBlock(
        block_id="forged-block",
        instance_id=_clips(document)[0].instance_id,
        source_id=media[0].asset_content_id,
        timeline_frames=FrameInterval(0, 250),
        source_frames=FrameInterval(0, 250),
        keep_candidates=(),
        sample_requests=(),
    )
    forged = replace(partition, blocks=(forged_block,))
    with pytest.raises(ValueError, match="제거 권한"):
        _plan(document, forged, guards, ())


def test_removal_under_non_success_guard_is_a_finding(tmp_path: Path) -> None:
    """검증 시점에 guard가 keep_all로 강등되면 이미 만든 제거도 거부된다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    demoted = _guards(document, _media(document), status="keep_all")
    findings = validate_edit_plan(plan, document, demoted)
    assert any("제거 권한" in finding for finding in findings)


def test_non_accepted_decision_with_stale_request_hash_raises(tmp_path: Path) -> None:
    """재현 I — keep_all 판정의 쓰레기 해시도 섞인 리비전이다.

    적용 경로에서만 신원을 보면 승인되지 않은 판정의 위조가 기록과 리비전에
    그대로 들어간다 — 연결 단계에서 모든 판정을 증명한다.
    """
    document, guards, partition = _standard(tmp_path)
    block = partition.blocks[0]
    stale = _decision(
        block,
        block.keep_candidates[0].candidate_id,
        status="keep_all",
        confidence=0.5,
        request="0" * 64,
    )
    with pytest.raises(ValueError, match="요청 신원"):
        _plan(document, partition, guards, (stale,))


def test_below_threshold_decision_with_stale_request_hash_raises(tmp_path: Path) -> None:
    """재현 J — 임계 미만 승인 판정의 쓰레기 해시도 같은 줄이다."""
    cfg = ProjectConfig(vision_apply_confidence=0.9)
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards, cfg=cfg)
    block = partition.blocks[0]
    stale = _decision(
        block,
        block.keep_candidates[0].candidate_id,
        confidence=0.85,
        request="f" * 64,
    )
    with pytest.raises(ValueError, match="요청 신원"):
        _plan(document, partition, guards, (stale,), cfg=cfg)


def test_uncut_clip_yields_one_run(tmp_path: Path) -> None:
    """판정 없는 500프레임 클립 — 블록 경계마다 쪼개지지 않고 run 하나다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards)
    clip = _clips(document)[0]
    assert len(plan.runs) == 1
    (run,) = plan.runs
    assert run.instance_id == clip.instance_id
    assert run.source_frames == FrameInterval(0, 500)
    assert run.input_timeline_frames == FrameInterval(0, 500)
    assert run.output_timeline_frames == FrameInterval(0, 500)
    assert run.reasons == ("kept_block", "kept_block")


def test_protected_prefix_yields_one_run(tmp_path: Path) -> None:
    """보호 접두 100프레임 — 유지 조각 셋이 run 하나로 합친다(전부 shift 0)."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, protected_by_clip={0: (FrameInterval(0, 100),)})
    partition = _partition(document, media, guards)
    plan = _plan(document, partition, guards)
    assert [row.reason for row in plan.retained] == [
        "kept_outside_blocks",
        "kept_block",
        "kept_block",
    ]
    assert len(plan.runs) == 1
    (run,) = plan.runs
    assert run.source_frames == FrameInterval(0, 500)
    assert run.output_timeline_frames == FrameInterval(0, 500)
    assert run.reasons == ("kept_outside_blocks", "kept_block", "kept_block")


def test_one_removal_yields_two_runs_with_different_shifts(tmp_path: Path) -> None:
    """제거 하나 — shift 0인 run과 밀린 run 둘로 갈라진다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards, (_front(partition.blocks[0]),))
    clip = _clips(document)[0]
    assert len(plan.runs) == 2
    first, second = plan.runs
    assert (first.instance_id, second.instance_id) == (clip.instance_id, clip.instance_id)
    assert first.source_frames == FrameInterval(0, 75)
    assert first.output_timeline_frames == FrameInterval(0, 75)
    assert first.reasons == ("kept_candidate",)
    assert second.source_frames == FrameInterval(250, 500)
    assert second.output_timeline_frames == FrameInterval(75, 325)
    assert second.reasons == ("kept_block",)


def test_tampered_runs_are_a_finding(tmp_path: Path) -> None:
    """runs는 retained의 병합과 일치해야 한다 — 손으로 고쳤으면 finding이다."""
    document, guards, partition = _standard(tmp_path)
    plan = _plan(document, partition, guards)
    findings = validate_edit_plan(replace(plan, runs=()), document, guards)
    assert any("run" in finding for finding in findings)


def test_decision_order_does_not_change_revision(tmp_path: Path) -> None:
    """판정 순서는 리비전을 바꾸지 않는다 — 해시는 파티션 블록 순서로 정규화한다."""
    document, guards, partition = _standard(tmp_path)
    first = _front(partition.blocks[0])
    second = _decision(partition.blocks[1], partition.blocks[1].keep_candidates[2].candidate_id)
    forward = _plan(document, partition, guards, (first, second))
    backward = _plan(document, partition, guards, (second, first))
    assert forward.revision == backward.revision
    # 기록물은 입력 순서 그대로다 — 정규화는 해시 재료에만 적용된다.
    assert forward.decisions == (first, second)
    assert backward.decisions == (second, first)
