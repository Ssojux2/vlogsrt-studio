"""원본 영상 묶음을 FCPXML로 합성하는 입구의 계약 — R-08이 여기서 얼어 붙는다.

합성기는 **타임라인을 저작한다**. 그래서 두 가지를 동시에 증명해야 한다: 만든
문서가 기존 파서의 좁은 부분집합 안에 떨어지는 것과, 섞인 입력을 맞추지 않고
거부하는 것. 실물 export가 29.97 소스를 30p 타임라인에 얹은 것 하나로 거부됐고
그대로 진행했다면 가장 긴 클립 끝에서 1.36초가 밀렸다 — 그 사고를 다시 부르지
않기 위해 리샘플링 대신 거부를 시험한다.

여기 있는 클립 실측값은 전부 손으로 만든 것이다. ffmpeg를 부르지 않는다.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.clips import (
    ClipProbe,
    collect_clip_paths,
    probe_clips,
    synthesize_fcpxml,
)
from vlogsrt.project.fcpxml import parse_project
from vlogsrt.project.models import UnsupportedProject

NTSC = Fraction(1001, 30000)


def _probe(
    name: str,
    *,
    frames: int,
    frame_duration: Fraction = NTSC,
    width: int = 3840,
    height: int = 2160,
    color_space: str = "1-1-1 (Rec. 709)",
    has_audio: bool = True,
    audio_channels: int | None = 2,
    audio_rate: int | None = 48000,
) -> ClipProbe:
    return ClipProbe(
        path=Path(f"/media/{name}.mov"),
        frame_duration=frame_duration,
        frame_count=frames,
        width=width,
        height=height,
        color_space=color_space,
        has_audio=has_audio,
        audio_channels=audio_channels,
        audio_rate=audio_rate,
        content_sha256=hashlib.sha256(name.encode()).hexdigest(),
    )


def _parse(raw: bytes, tmp_path: Path):  # type: ignore[no-untyped-def]
    destination = tmp_path / "input.fcpxml"
    destination.write_bytes(raw)
    return parse_project(destination)


def test_synthesized_timeline_keeps_clip_order_and_frame_counts(tmp_path: Path) -> None:
    clips = (_probe("a", frames=1168), _probe("b", frames=1940), _probe("c", frames=604))
    document = _parse(synthesize_fcpxml(clips, project_name="wangbang"), tmp_path)

    assert document.frame_duration == NTSC
    assert document.sequence_tc_start == 0
    assert len(document.storyline) == 3
    offset = Fraction(0)
    for clip, item in zip(clips, document.storyline, strict=True):
        assert item.offset == offset
        assert item.duration == clip.frame_count * NTSC
        offset += item.duration


def test_source_in_points_start_at_the_asset_origin(tmp_path: Path) -> None:
    """클립은 소스 전체를 쓴다 — 트림이 없으므로 in점은 애셋 시작이다."""
    document = _parse(synthesize_fcpxml((_probe("a", frames=30),), project_name="p"), tmp_path)
    asset = document.resources[0]
    clip = document.storyline[0]
    assert asset.start == 0
    assert clip.start == asset.start  # type: ignore[union-attr]
    assert clip.duration == asset.duration


def test_time_strings_keep_the_frame_rate_denominator() -> None:
    """1168프레임 @ 1001/30000 → `1169168/30000s`. Final Cut이 적는 모양 그대로다."""
    raw = synthesize_fcpxml((_probe("a", frames=1168),), project_name="p").decode()
    assert 'duration="1169168/30000s"' in raw
    assert 'frameDuration="1001/30000s"' in raw
    assert 'name="FFVideoFormat3840x2160p2997"' in raw


def test_synthesis_is_byte_stable() -> None:
    clips = (_probe("a", frames=10), _probe("b", frames=20))
    assert synthesize_fcpxml(clips, project_name="p") == synthesize_fcpxml(clips, project_name="p")


def test_project_uid_follows_content_not_order_of_calls() -> None:
    """같은 묶음이면 같은 uid — 리비전이 같은 것을 가리켜야 한다."""
    clips = (_probe("a", frames=10), _probe("b", frames=20))
    first = synthesize_fcpxml(clips, project_name="p")
    other = synthesize_fcpxml((clips[1], clips[0]), project_name="p")
    assert first != other


def test_mixed_frame_duration_is_rejected_by_file_name() -> None:
    clips = (_probe("a", frames=10), _probe("b", frames=20, frame_duration=Fraction(1, 30)))
    with pytest.raises(UnsupportedProject) as caught:
        synthesize_fcpxml(clips, project_name="p")
    assert caught.value.location == "/media/b.mov"
    assert "프레임 길이" in caught.value.reason


def test_mixed_resolution_is_rejected() -> None:
    clips = (_probe("a", frames=10), _probe("b", frames=10, width=1920, height=1080))
    with pytest.raises(UnsupportedProject) as caught:
        synthesize_fcpxml(clips, project_name="p")
    assert "가로 크기" in caught.value.reason


def test_mixed_audio_presence_is_rejected() -> None:
    clips = (
        _probe("a", frames=10),
        _probe("b", frames=10, has_audio=False, audio_channels=None, audio_rate=None),
    )
    with pytest.raises(UnsupportedProject) as caught:
        synthesize_fcpxml(clips, project_name="p")
    assert "오디오 유무" in caught.value.reason


def test_unsupported_audio_rate_is_rejected() -> None:
    clips = (_probe("a", frames=10, audio_rate=32000),)
    with pytest.raises(UnsupportedProject) as caught:
        synthesize_fcpxml(clips, project_name="p")
    assert "샘플레이트" in caught.value.reason


def test_silent_clips_declare_no_audio(tmp_path: Path) -> None:
    clips = (_probe("a", frames=10, has_audio=False, audio_channels=None, audio_rate=None),)
    raw = synthesize_fcpxml(clips, project_name="p")
    assert b'hasAudio="0"' in raw
    assert b"audioRole" not in raw
    assert b"audioLayout" not in raw
    document = _parse(raw, tmp_path)
    assert document.resources[0].has_audio is False


def test_empty_clip_list_is_rejected() -> None:
    with pytest.raises(UnsupportedProject):
        synthesize_fcpxml((), project_name="p")
    with pytest.raises(UnsupportedProject):
        probe_clips(())


def test_collect_sorts_by_name_and_skips_hidden_and_other_suffixes(tmp_path: Path) -> None:
    for name in ("b.mov", "a.mp4", ".hidden.mov", "notes.txt", "c.MKV"):
        (tmp_path / name).write_bytes(b"")
    assert [path.name for path in collect_clip_paths(tmp_path)] == ["a.mp4", "b.mov", "c.MKV"]


def test_directory_without_clips_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "notes.txt").write_bytes(b"")
    with pytest.raises(UnsupportedProject) as caught:
        collect_clip_paths(tmp_path)
    assert "영상 파일이 없다" in caught.value.reason


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsupportedProject):
        collect_clip_paths(tmp_path / "없는곳")
