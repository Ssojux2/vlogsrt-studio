"""P6-01 블록 분할 — eligible unprotected runs의 편집 블록화 (계약).

브리프(p6-01-block-partition.md)의 정본:

- **프레임이 정본이고 초는 파생이다.** `KeepCandidate.source_frames`가 소스 격자
  절대 좌표고 `vision.keep_start/keep_end`는 그 프레임에서 유도한 블록 상대 초다.
- **신원은 내용에 묶인다.** 서수 id가 아니라 브리프 공식의 해시다 — 다른 설정·
  다른 문서 리비전에서 만든 판정이 "모르는 id" 검사를 통과해 엉뚱한 프레임을
  지우는 일이 없게 한다.
- **레일이 먼저다.** 설정 결함은 호출자 잘못이라 `raise`고 keep+review로 세탁하지
  않는다. 프레임 환산 뒤 관계와 교차 레일은 생성자가 볼 수 없는 P6 고유다.
- **guard 연결은 엄격하다.** 문서의 클립 인스턴스마다 guard가 정확히 하나고
  `instance_id`·`source_id` 둘 다 맞아야 한다. "guard 없음"이 조용히
  "보호 없음"이 되면 `protected=[]`가 옷만 갈아입은 것이다.
- **여집합만 자른다.** 이 계층은 기하만 하고 오디오·전사·VAD·`review` 산문을
  보지 않는다(규칙 4). 짧은 잔여는 명시적으로 유지된다(규칙 9).

합성 XML·손 미디어·손 guard만 쓴다. 픽스처·골든·네트워크는 없다.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.boundary import UnavailableInstance
from vlogsrt.project.config import ProjectConfig, ProjectConfigError
from vlogsrt.project.edit import BlockPartition, EditBlock, partition_blocks
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.models import ClipInstance, ProjectDocument
from vlogsrt.project.timebase import (
    SUPPORTED_FRAME_DURATIONS,
    FrameInterval,
    format_fcpx_time,
    frame_ceil,
    frame_floor,
    frame_nearest,
)
from vlogsrt.project.vad.guard import InstanceGuard
from vlogsrt.project.vision.sampling import FrameSampleRequest

FD25 = Fraction(1, 25)
FD2997 = Fraction(1001, 30000)
"""계약 시간표의 대표 두 레이트 — 정수 레이트와 1001계 레이트."""

ALL_RATES = sorted(SUPPORTED_FRAME_DURATIONS)
RUN_SECONDS = (6, 10, 11, 12, 17, 20, 22)
"""브리프가 명시한 일곱 개 런 길이 — balanced/greedy/경계 전부 나온다."""


# ─────────────────────────────────────────────────────────────
#  재료 — 합성 XML·손 미디어·손 guard
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


def _project(
    tmp_path: Path,
    rate: Fraction,
    assets: tuple[_AssetSpec, ...],
    clips: tuple[_ClipSpec, ...],
    *,
    tc_start_frames: int = 0,
) -> ProjectDocument:
    """격자가 정확한 합성 문서 — 파서가 요구하는 앵커·프레임 정수를 전부 지킨다."""
    tc = Fraction(tc_start_frames) * rate
    spans = [clip.offset_frames + clip.duration_frames for clip in clips]
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
    clips_xml = "\n    ".join(_clip_xml(clip, tc, rate, asset_starts[clip.ref]) for clip in clips)
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


def _scene(
    tmp_path: Path,
    *,
    rate: Fraction = FD25,
    clips: tuple[_ClipSpec, ...],
    asset_start_frames: int = 0,
    asset_duration_frames: int = 400_000,
    has_audio: bool = True,
    tc_start_frames: int = 0,
) -> tuple[ProjectDocument, tuple[ResolvedMedia, ...]]:
    assets = (_AssetSpec("a1", asset_start_frames, asset_duration_frames, has_audio),)
    document = _project(tmp_path, rate, assets, clips, tc_start_frames=tc_start_frames)
    return document, _media(document)


def _block_sizes(partition: BlockPartition) -> list[int]:
    return [
        block.source_frames.end_frame - block.source_frames.start_frame
        for block in partition.blocks
    ]


def _sample_grid_indices(block: EditBlock, fd: Fraction = FD25) -> list[int]:
    """블록 표본의 소스 격자 인덱스 — source_time에서 되돌린 정렬 목록."""
    return sorted(int(request.source_time / fd) for request in block.sample_requests)


def _expected_blocks(length: int, min_frames: int, max_frames: int) -> tuple[list[int], int]:
    """브리프 규칙의 독립 재구현 — balanced 먼저, 아니면 greedy + 명시적 잔여."""
    count = -(-length // max_frames)
    base, extra = divmod(length, count)
    sizes = [base + 1] * extra + [base] * (count - extra)
    if min(sizes) >= min_frames:
        return sizes, 0
    sizes = []
    left = length
    while left >= min_frames:
        take = min(max_frames, left)
        sizes.append(take)
        left -= take
    return sizes, left


def _limits(rate: Fraction) -> tuple[int, int, int]:
    return (
        frame_ceil(Fraction(6), rate),
        frame_floor(Fraction(10), rate),
        frame_nearest(Fraction(3), rate),
    )


# ─────────────────────────────────────────────────────────────
#  레일 — 설정 결함은 raise다 (표의 각 칸)
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"keep_seconds": Fraction(0)}, "후보가 비어 블록 전체가 제거된다"),
        ({"keep_seconds": Fraction(7)}, "후보가 블록보다 길어진다"),
        ({"vision_sample_fps": Fraction(0)}, "0으로 나누거나 표본이 0개가 된다"),
    ],
)
def test_constructor_level_config_defects_raise(overrides: dict[str, object], why: str) -> None:
    """P5-06 생성자 레일 — 이 셋은 객체가 만들어지기 전에 닫힌다."""
    with pytest.raises(ProjectConfigError):
        ProjectConfig(**overrides)  # type: ignore[arg-type]


def _healthy_scene(
    tmp_path: Path,
) -> tuple[ProjectDocument, tuple[ResolvedMedia, ...], tuple[InstanceGuard, ...]]:
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    return document, media, _guards(document, media)


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        (
            {"block_min_seconds": Fraction(1, 1000), "keep_seconds": Fraction(1, 10_000)},
            "1프레임 런이 블록이 된다 — keep_frames=0이므로 프레임 환산 레일에서 닫힌다",
        ),
        (
            {"block_max_seconds": Fraction(600)},
            "10분을 이미지 16장으로 판정해 597초를 제거한다 — 교차 레일",
        ),
        ({"vision_max_images": 4}, "끝점 2 + 중앙 3을 담을 수 없다 — int >= 5 레일"),
        (
            {
                "block_min_seconds": Fraction(5997, 1000),
                "block_max_seconds": Fraction(5999, 1000),
            },
            "초로는 순서가 맞지만 프레임 환산 뒤 min(150) > max(149)가 된다",
        ),
    ],
)
def test_partition_level_config_defects_raise_before_any_block(
    tmp_path: Path, overrides: dict[str, object], why: str
) -> None:
    """생성자가 볼 수 없는 관계 — partition_blocks의 `_require_config`이 닫는다."""
    document, media, guards = _healthy_scene(tmp_path)
    cfg = ProjectConfig(**overrides)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="설정"):
        partition_blocks(document, media, guards, frozenset(), (), cfg)


def test_healthy_config_yields_real_blocks(tmp_path: Path) -> None:
    """ "전부 막혔다"와 "모듈이 망가졌다"를 가르는 대조군 — 정상 설정은 정상 블록."""
    document, media, guards = _healthy_scene(tmp_path)
    partition = _partition(document, media, guards)
    assert _block_sizes(partition) == [250, 250]


# ─────────────────────────────────────────────────────────────
#  guard 연결 — 없음·중복·남음·불일치는 전부 연결 오류
# ─────────────────────────────────────────────────────────────


def test_missing_guard_raises(tmp_path: Path) -> None:
    """ "guard 없음"이 조용히 "보호 없음"이 되는 것을 막는다."""
    document, media, _guards = _healthy_scene(tmp_path)
    with pytest.raises(ValueError, match="guard"):
        _partition(document, media, ())


def test_duplicate_guard_raises(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    with pytest.raises(ValueError, match="하나"):
        _partition(document, media, guards + guards)


def test_extra_guard_for_unknown_instance_raises(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    extra = InstanceGuard(
        instance_id="nonexistent-instance",
        source_id=media[0].asset_content_id,
        status="success",
        protected_frames=(),
        review=(),
    )
    with pytest.raises(ValueError, match="문서에 없는"):
        _partition(document, media, guards + (extra,))


def test_guard_source_id_mismatch_raises(tmp_path: Path) -> None:
    """인스턴스 짝은 맞아도 내용 신원이 다르면 위조 연결이다."""
    document, media, guards = _healthy_scene(tmp_path)
    forged = InstanceGuard(
        instance_id=guards[0].instance_id,
        source_id="acid-something-else",
        status="success",
        protected_frames=(),
        review=(),
    )
    with pytest.raises(ValueError, match="source_id"):
        _partition(document, media, (forged,))


@pytest.mark.parametrize(
    "clip",
    [
        _ClipSpec("a1", duration_frames=500, enabled=False),
        _ClipSpec("a1", duration_frames=500, src_enable="video"),
    ],
    ids=["muted", "video-only"],
)
def test_success_guard_on_inaudible_clip_raises(tmp_path: Path, clip: _ClipSpec) -> None:
    """가청은 문서에서 계산한다 — 불가청 인스턴스의 success guard는 연결 오류다."""
    document, media = _scene(tmp_path, clips=(clip,))
    guards = _guards(document, media, status="success")
    with pytest.raises(ValueError, match="불가청"):
        _partition(document, media, guards)


@pytest.mark.parametrize("status", ["inaudible", "keep_all"])
def test_ineligible_statuses_on_inaudible_clips_stay_silent(tmp_path: Path, status: str) -> None:
    """보수 방향의 모순은 견딘다 — 제거가 좁아질 뿐이므로 블록만 없다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500, enabled=False),))
    guards = _guards(document, media, status=status)
    partition = _partition(document, media, guards)
    assert partition.blocks == ()
    assert partition.kept_short_remainders == ()


def test_keep_all_guard_yields_no_blocks(tmp_path: Path) -> None:
    """분석 실패(keep_all)는 제거 권한이 없다 — 전체가 보호다."""
    document, media, guards = _healthy_scene(tmp_path)
    partition = _partition(document, media, _guards(document, media, status="keep_all"))
    assert partition.blocks == ()


def test_media_linkage_is_strict(tmp_path: Path) -> None:
    """미디어의 가청 선언이 문서와 어긋나면 조용한 무음이 되므로 거부한다."""
    document, media, guards = _healthy_scene(tmp_path)
    forged = (replace(media[0], has_audio=not media[0].has_audio),)
    with pytest.raises(ValueError, match="has_audio"):
        _partition(document, forged, guards)


# ─────────────────────────────────────────────────────────────
#  unavailable — 타입 있는 부재의 검증과 반영
# ─────────────────────────────────────────────────────────────


def _unavailable(clip: ClipInstance, document: ProjectDocument, reason: str) -> UnavailableInstance:
    resource = next(r for r in document.resources if r.scoped_id == clip.resource_scoped_id)
    return UnavailableInstance(
        resource_scoped_id=resource.scoped_id,
        instance_id=clip.instance_id,
        reason_code=reason,  # type: ignore[arg-type]
    )


def test_declared_no_audio_instance_is_carried_and_silent(tmp_path: Path) -> None:
    """선언된 무음 — 저작된 불가청이고 guard도 inaudible다. 기록은 그대로 흐른다."""
    document, media = _scene(
        tmp_path,
        clips=(_ClipSpec("a1", duration_frames=500),),
        has_audio=False,
    )
    guards = _guards(document, media, status="inaudible")
    clip = _clips(document)[0]
    record = _unavailable(clip, document, "declared_no_audio")
    partition = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
    )
    assert partition.blocks == ()
    assert partition.unavailable_instances == (record,)


@pytest.mark.parametrize("reason", ["stream_unavailable", "pcm_unavailable"])
def test_retryable_pre_pcm_failures_are_carried_and_silent(tmp_path: Path, reason: str) -> None:
    """선언된 가청 + pre-PCM 실패 — 명시적 부재 기록과 잠금으로 제외된다."""
    document, media = _scene(tmp_path, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, status="keep_all")
    clip = _clips(document)[0]
    record = _unavailable(clip, document, reason)
    partition = _partition(
        document, media, guards, locked=frozenset({clip.instance_id}), unavailable=(record,)
    )
    assert partition.blocks == ()
    assert partition.unavailable_instances == (record,)


def test_unavailable_instance_without_lock_raises(tmp_path: Path) -> None:
    """부재는 잠금과 함께 온다 — 아니면 계획이 모순이다."""
    document, media, guards = _healthy_scene(tmp_path)
    record = _unavailable(_clips(document)[0], document, "pcm_unavailable")
    with pytest.raises(ValueError, match="잠금"):
        _partition(document, media, guards, unavailable=(record,))


def test_duplicate_unavailable_records_raise(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    record = _unavailable(_clips(document)[0], document, "pcm_unavailable")
    with pytest.raises(ValueError, match="중복"):
        _partition(
            document,
            media,
            guards,
            locked=frozenset({record.instance_id}),
            unavailable=(record, record),
        )


def test_unknown_unavailable_resource_raises(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    clip = _clips(document)[0]
    record = UnavailableInstance(
        resource_scoped_id="sha256-nonexistent-resource",
        instance_id=clip.instance_id,
        reason_code="pcm_unavailable",
    )
    with pytest.raises(ValueError, match="자원"):
        _partition(
            document,
            media,
            guards,
            locked=frozenset({clip.instance_id}),
            unavailable=(record,),
        )


def test_unknown_unavailable_instance_raises(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    record = UnavailableInstance(
        resource_scoped_id=document.resources[0].scoped_id,
        instance_id="nonexistent-instance",
        reason_code="pcm_unavailable",
    )
    with pytest.raises(ValueError, match="인스턴스"):
        _partition(
            document,
            media,
            guards,
            locked=frozenset({"nonexistent-instance"}),
            unavailable=(record,),
        )


def test_unknown_unavailable_reason_raises(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    record = _unavailable(_clips(document)[0], document, "mystery_reason")
    with pytest.raises(ValueError, match="사유"):
        _partition(
            document,
            media,
            guards,
            locked=frozenset({record.instance_id}),
            unavailable=(record,),
        )


def test_resource_scoped_unavailability_wins_over_equal_content_healthy_guard(
    tmp_path: Path,
) -> None:
    """같은 내용 해시의 두 자원 — 건강한 쪽은 자르고, 부재 쪽은 못 자른다."""
    clips = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
        _ClipSpec("a2", offset_frames=600, start_frames=0, duration_frames=500),
    )
    document = _project(tmp_path, FD25, (_AssetSpec("a1"), _AssetSpec("a2")), clips)
    shared = "acid-shared-content"
    media = _media(document, {"a1": shared, "a2": shared})
    guards = _guards(document, media, status="success")
    sick = _clips(document)[1]
    record = _unavailable(sick, document, "pcm_unavailable")
    partition = _partition(
        document,
        media,
        guards,
        locked=frozenset({sick.instance_id}),
        unavailable=(record,),
    )
    healthy = _clips(document)[0]
    assert [block.instance_id for block in partition.blocks] == [
        healthy.instance_id,
        healthy.instance_id,
    ]
    assert partition.unavailable_instances == (record,)


# ─────────────────────────────────────────────────────────────
#  기하 — 여덟 레이트·일곱 길이의 balanced/greedy
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("rate", ALL_RATES)
@pytest.mark.parametrize("seconds", RUN_SECONDS)
def test_rule_shaped_partition_at_every_rate_and_length(
    tmp_path: Path, rate: Fraction, seconds: int
) -> None:
    """브리프 규칙의 성질 — 블록은 [min,max] 안에서 타일하고 잔여는 명시적이다."""
    run_frames = frame_nearest(Fraction(seconds), rate)
    document, media = _scene(
        tmp_path, rate=rate, clips=(_ClipSpec("a1", duration_frames=run_frames),)
    )
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    min_frames, max_frames, _ = _limits(rate)
    sizes, remainder = _expected_blocks(run_frames, min_frames, max_frames)
    assert _block_sizes(partition) == sizes
    covered = sum(sizes)
    assert all(min_frames <= size <= max_frames for size in sizes)
    cursor = 0
    for block in partition.blocks:
        assert block.source_frames.start_frame == cursor
        cursor = block.source_frames.end_frame
    if remainder:
        assert partition.kept_short_remainders == (
            (_clips(document)[0].instance_id, FrameInterval(covered, run_frames)),
        )
    else:
        assert partition.kept_short_remainders == ()
        assert cursor == run_frames


def test_exact_partition_shapes_at_25fps(tmp_path: Path) -> None:
    """25fps의 손계산 값 — min=150, max=250이므로 11초만 greedy가 낀다."""
    expected: dict[int, tuple[list[int], int]] = {
        150: ([150], 0),
        250: ([250], 0),
        275: ([250], 25),
        300: ([150, 150], 0),
        425: ([213, 212], 0),
        500: ([250, 250], 0),
        550: ([184, 183, 183], 0),
    }
    for run_frames, (sizes, remainder) in expected.items():
        document, media = _scene(
            tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=run_frames),)
        )
        guards = _guards(document, media)
        partition = _partition(document, media, guards)
        assert _block_sizes(partition) == sizes, f"{run_frames}프레임 런"
        assert len(partition.kept_short_remainders) == (1 if remainder else 0)


def test_exact_partition_shapes_at_2997fps(tmp_path: Path) -> None:
    """1001계 레이트 — min=180, max=299다. 11초(330f)는 greedy, 22초(659f)는 balanced."""
    cases = {
        330: ([299], 31),  # ceil(330/299)=2 → 165,165 < 180 → greedy
        659: ([220, 220, 219], 0),  # ceil(659/299)=3 → 220,220,219 ≥ 180
    }
    for run_frames, (sizes, remainder) in cases.items():
        document, media = _scene(
            tmp_path, rate=FD2997, clips=(_ClipSpec("a1", duration_frames=run_frames),)
        )
        guards = _guards(document, media)
        partition = _partition(document, media, guards)
        assert _block_sizes(partition) == sizes, f"{run_frames}프레임 런"
        assert len(partition.kept_short_remainders) == (1 if remainder else 0)


def test_short_remainder_names_its_instance_and_interval(tmp_path: Path) -> None:
    document, media = _scene(
        tmp_path, rate=FD25, clips=(_ClipSpec("a1", start_frames=40, duration_frames=275),)
    )
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    clip = _clips(document)[0]
    assert partition.kept_short_remainders == ((clip.instance_id, FrameInterval(290, 315)),)


def test_runs_shorter_than_minimum_are_left_alone_not_recorded(tmp_path: Path) -> None:
    """6초 미만 런은 적격 자체가 아니다 — 잔여 기록도 없이 그대로 둔다."""
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=100),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    assert partition.blocks == ()
    assert partition.kept_short_remainders == ()


def test_guard_holes_split_runs(tmp_path: Path) -> None:
    """보호 구멍 사이의 eligible 런만 블록이 된다."""
    protected = (FrameInterval(100, 150), FrameInterval(350, 400))
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=500),))
    guards = _guards(document, media, protected_by_clip={0: protected})
    partition = _partition(document, media, guards)
    assert [block.source_frames for block in partition.blocks] == [FrameInterval(150, 350)]


def test_whole_slice_protection_intersects_each_clip_before_complement(
    tmp_path: Path,
) -> None:
    """합쳐진 두 칼날 슬라이스의 whole-slice 보호 — 클립 구간과 교집합한 뒤 여집합."""
    clips = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=500),
        _ClipSpec("a1", offset_frames=500, start_frames=500, duration_frames=500),
    )
    document, media = _scene(tmp_path, rate=FD25, clips=clips)
    whole_slice = (FrameInterval(250, 750),)
    guards = _guards(document, media, protected_by_clip={0: whole_slice, 1: whole_slice})
    partition = _partition(document, media, guards)
    assert [block.source_frames for block in partition.blocks] == [
        FrameInterval(0, 250),
        FrameInterval(750, 1000),
    ]


def test_blocks_never_cross_clip_or_gap_boundaries(tmp_path: Path) -> None:
    """갭으로 갈라진 두 클립 — 각자 자기 구간 안에서만 블록이 된다."""
    clips = (
        _ClipSpec("a1", offset_frames=0, start_frames=0, duration_frames=300),
        _ClipSpec("a1", offset_frames=400, start_frames=300, duration_frames=300),
    )
    document, media = _scene(tmp_path, rate=FD25, clips=clips)
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    assert _block_sizes(partition) == [150, 150, 150, 150]
    first, second = _clips(document)
    for block in partition.blocks[:2]:
        assert block.instance_id == first.instance_id
        assert 0 <= block.source_frames.start_frame < block.source_frames.end_frame <= 300
        assert block.timeline_frames == FrameInterval(
            block.source_frames.start_frame, block.source_frames.end_frame
        )
    for block in partition.blocks[2:]:
        assert block.instance_id == second.instance_id
        assert 300 <= block.source_frames.start_frame < block.source_frames.end_frame <= 600
        # 타임라인은 갭만큼 밀려 있다 — 소스 프레임 + 100
        assert block.timeline_frames == FrameInterval(
            block.source_frames.start_frame + 100, block.source_frames.end_frame + 100
        )


def test_locked_instances_yield_no_blocks_and_stay_carried(tmp_path: Path) -> None:
    document, media, guards = _healthy_scene(tmp_path)
    clip = _clips(document)[0]
    partition = _partition(document, media, guards, locked=frozenset({clip.instance_id}))
    assert partition.blocks == ()
    assert partition.locked_instance_ids == frozenset({clip.instance_id})


# ─────────────────────────────────────────────────────────────
#  프레임 정본 — 원점·경계·끝점
# ─────────────────────────────────────────────────────────────


def test_nonzero_asset_and_timeline_origins_flow_into_frames(tmp_path: Path) -> None:
    """애셋 시작 2500f·tcStart 100f — 소스 격자와 타임라인 격자가 각자 앵커를 지킨다."""
    document, media = _scene(
        tmp_path,
        rate=FD25,
        clips=(_ClipSpec("a1", offset_frames=50, start_frames=100, duration_frames=500),),
        asset_start_frames=2500,
        tc_start_frames=100,
    )
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    first, second = partition.blocks
    assert first.source_frames == FrameInterval(100, 350)
    assert first.timeline_frames == FrameInterval(50, 300)
    assert second.source_frames == FrameInterval(350, 600)
    assert second.timeline_frames == FrameInterval(300, 550)
    # 격자 인덱스 100의 파일 시계 시각은 4초다 — 애셋 시계(104초)가 아니다.
    first_sample = min(partition.blocks[0].sample_requests, key=lambda request: request.source_time)
    assert first_sample.source_time == Fraction(100, 25)


def test_asset_last_block_endpoint_is_end_minus_one(tmp_path: Path) -> None:
    """남은 XML 경계가 격자 끝과 같아도 끝점 표본은 end-1이다.

    `end_frame`은 애셋 프레임 수와 같을 수 있으므로 표본이 될 수 없다 — 마지막
    유효 프레임은 언제나 `end_frame − 1`이다.
    """
    document, media = _scene(
        tmp_path,
        rate=FD25,
        clips=(_ClipSpec("a1", start_frames=400, duration_frames=600),),
        asset_start_frames=0,
        asset_duration_frames=1000,
    )
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    # 600f 런은 balanced 200×3 — 마지막 블록이 남은 XML 격자 끝(1000)과 정확히 닿는다.
    assert partition.blocks[-1].source_frames == FrameInterval(800, 1000)
    indices = _sample_grid_indices(partition.blocks[-1])
    assert indices[-1] == 999
    assert 1000 not in indices


# ─────────────────────────────────────────────────────────────
#  후보 — 프레임이 정본, 초는 파생
# ─────────────────────────────────────────────────────────────


def test_candidates_are_exactly_keep_frames_at_three_positions(tmp_path: Path) -> None:
    """10초 블록, 3초 유지 — 앞/중앙/뒤, 중앙 시작은 half-up(프레임 기준)."""
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=250),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    (block,) = partition.blocks
    assert [candidate.source_frames for candidate in block.keep_candidates] == [
        FrameInterval(0, 75),  # 앞
        FrameInterval(88, 163),  # 중앙: (250-75+1)//2 = 88
        FrameInterval(175, 250),  # 뒤
    ]
    middle = block.keep_candidates[1]
    assert middle.vision.keep_start == Fraction(88, 25)
    assert middle.vision.keep_end == Fraction(163, 25)
    assert middle.timeline_frames == FrameInterval(88, 163)


def test_candidate_dedup_collapses_overlapping_windows(tmp_path: Path) -> None:
    """keep=150일 때 블록 151f — 중앙과 뒤가 같은 구간이면 한 번만 낸다."""
    document, media = _scene(
        tmp_path, rate=FD25, clips=(_ClipSpec("a1", start_frames=0, duration_frames=301),)
    )
    guards = _guards(document, media)
    partition = _partition(document, media, guards, cfg=ProjectConfig(keep_seconds=Fraction(6)))
    sizes = _block_sizes(partition)
    assert sizes == [151, 150]
    first, second = partition.blocks
    assert [c.source_frames for c in first.keep_candidates] == [
        FrameInterval(0, 150),
        FrameInterval(1, 151),  # 중앙 == 뒤 — 하나로 겹친다
    ]
    assert [c.source_frames for c in second.keep_candidates] == [
        FrameInterval(151, 301),  # 블록 길이 == keep — 셋이 전부 같은 구간
    ]


def test_full_block_candidate_keeps_everything(tmp_path: Path) -> None:
    """keep == min이면 블록과 후보가 같은 길이다 — "전체 삭제 후보"는 없다."""
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=150),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards, cfg=ProjectConfig(keep_seconds=Fraction(6)))
    (block,) = partition.blocks
    (candidate,) = block.keep_candidates
    assert candidate.source_frames == block.source_frames == FrameInterval(0, 150)
    assert candidate.vision.keep_start == Fraction(0)
    assert candidate.vision.keep_end == Fraction(6)


# ─────────────────────────────────────────────────────────────
#  표본 — 1fps 명목 + 끝점 + 후보 중앙, 결정적 축소
# ─────────────────────────────────────────────────────────────


def test_sample_grid_at_25fps_is_exact(tmp_path: Path) -> None:
    """25fps 10초 블록 — 명목 1fps는 격자 위, 끝점과 중앙 셋은 필수다."""
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=250),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    (block,) = partition.blocks
    assert _sample_grid_indices(block) == [
        0,
        25,
        38,
        50,
        75,
        100,
        125,
        126,
        150,
        175,
        200,
        213,
        225,
        249,
    ]
    for request in block.sample_requests:
        assert isinstance(request, FrameSampleRequest)
        assert request.source_id == media[0].asset_content_id
        assert request.source_time == Fraction(int(request.source_time * 25), 25)
        assert request.frame_id == f"{media[0].asset_content_id}:{int(request.source_time * 25)}"


def test_sample_grid_at_2997_never_sends_off_grid_seconds(tmp_path: Path) -> None:
    """29.97 — 명목 정수 초는 격자 밖이다. snapped 인덱스의 시각만 나간다."""
    document, media = _scene(tmp_path, rate=FD2997, clips=(_ClipSpec("a1", duration_frames=299),))
    guards = _guards(document, media)
    partition = _partition(document, media, guards)
    (block,) = partition.blocks
    assert _sample_grid_indices(block, FD2997) == [
        0,
        30,
        45,
        60,
        90,
        120,
        150,
        180,
        210,
        240,
        254,
        270,
        298,
    ]
    for request in block.sample_requests:
        # P5-04가 요구하는 격자 정수성 — (source_time / frame_duration)이 정수다.
        assert (request.source_time / FD2997).denominator == 1
        # 문장 그대로의 1초(=1s)는 격자 밖이므로 절대 나가지 않는다.
        assert request.source_time != Fraction(1)
    by_index = {int(request.source_time / FD2997): request for request in block.sample_requests}
    assert by_index[30].source_time == Fraction(1001, 1000)


def test_sample_cap_keeps_mandatory_samples(tmp_path: Path) -> None:
    """한도 5 — 끝점 2 + 중앙 3은 지키고 명목 1fps만 결정적으로 줄인다(여기선 전부)."""
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=250),))
    guards = _guards(document, media)
    cfg = ProjectConfig(vision_max_images=5, vision_sample_fps=Fraction(1, 2))
    partition = _partition(document, media, guards, cfg=cfg)
    (block,) = partition.blocks
    assert _sample_grid_indices(block) == [0, 38, 126, 213, 249]


def test_sample_cap_thins_nominal_samples_by_stride(tmp_path: Path) -> None:
    """한도 6 — 명목은 격자 간격으로 줄인다: 남은 4개 중 첫 원소 1개만 산다."""
    document, media = _scene(tmp_path, rate=FD25, clips=(_ClipSpec("a1", duration_frames=250),))
    guards = _guards(document, media)
    cfg = ProjectConfig(vision_max_images=6, vision_sample_fps=Fraction(1, 2))
    partition = _partition(document, media, guards, cfg=cfg)
    (block,) = partition.blocks
    assert _sample_grid_indices(block) == [0, 38, 50, 126, 213, 249]


# ─────────────────────────────────────────────────────────────
#  신원 — 내용에 묶인 해시
# ─────────────────────────────────────────────────────────────


def test_identity_is_stable_and_content_bound(tmp_path: Path) -> None:
    """같은 입력은 같은 id — keep_seconds가 다르면 후보 신원과 설정 해시가 달라진다."""
    document, media, guards = _healthy_scene(tmp_path)
    first = _partition(document, media, guards)
    second = _partition(document, media, guards)
    assert [b.block_id for b in first.blocks] == [b.block_id for b in second.blocks]
    assert first.blocks[0].keep_candidates == second.blocks[0].keep_candidates
    assert first.input_sha256 == document.input_sha256
    assert first.config_sha256 == ProjectConfig().sha256()

    other_keep = _partition(document, media, guards, cfg=ProjectConfig(keep_seconds=Fraction(5, 2)))
    assert other_keep.config_sha256 != first.config_sha256
    assert (
        other_keep.blocks[0].keep_candidates[0].candidate_id
        != first.blocks[0].keep_candidates[0].candidate_id
    )
    # 블록 기하가 같으면 block_id는 같다 — 후보와 설정 해시가 리비전을 가른다.
    assert other_keep.blocks[0].block_id == first.blocks[0].block_id


def test_identity_changes_with_document_revision(tmp_path: Path) -> None:
    """클립이 달라지면 문서 해시가 달라지고 block_id도 따라간다."""
    document, media, guards = _healthy_scene(tmp_path)
    first = _partition(document, media, guards)
    longer, longer_media = _scene(
        tmp_path / "longer", clips=(_ClipSpec("a1", duration_frames=550),)
    )
    second = _partition(longer, longer_media, _guards(longer, longer_media))
    assert first.input_sha256 != second.input_sha256
    assert first.blocks[0].block_id != second.blocks[0].block_id


def test_candidate_ids_bind_to_their_block(tmp_path: Path) -> None:
    """candidate_id는 block_id와 후보 프레임에서 나온다 — 블록끼리 겹치지 않는다."""
    document, media, guards = _healthy_scene(tmp_path)
    partition = _partition(document, media, guards)
    ids = {
        candidate.candidate_id for block in partition.blocks for candidate in block.keep_candidates
    }
    assert len(ids) == 6
    vision_ids = {
        candidate.vision.candidate_id
        for block in partition.blocks
        for candidate in block.keep_candidates
    }
    assert vision_ids == ids
