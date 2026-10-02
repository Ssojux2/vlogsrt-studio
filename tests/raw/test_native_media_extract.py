"""원본 미디어 경계 — 진짜 ffmpeg로 잰 프레임 정확 추출(A3).

합성 원본만 만들고(실제 미디어·API 픽스처 불변), `extract_raw_clip`의 구조
검증을 실측으로 통과시킨다: CFR 그리드(25fps·30000/1001), 0이 아닌 영상 PTS,
늦은/여러 오디오, GOP 중간 절단, 미지원 정밀도, 원본 해시 불변(§8, §17).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.preprocess import (
    RAW_EDIT_PRORES,
    RAW_SOURCE_MATCHED,
    RawClipHandles,
    RawClipVerificationError,
    RawMediaUnsupported,
    RawTranscodeProfile,
    clock_frame_range,
    extract_raw_clip,
    probe_audio_extents,
    probe_extraction_clock,
    probe_raw_media,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

# 이웃 프레임과 구분되는지 잰다 — 프레임끼리 차이가 이 문턱보다 작으면 시험
# 데이터가 나쁜 것이다(정확 탐색 증명의 전제).
_NEIGHBOR_LUMA_GAP = 2.0
_REENCODE_LUMA_TOLERANCE = 2.0


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 원본 생성 실패: {completed.stderr[-400:]}"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _clip(path: Path, first: int, after_last: int) -> Path:
    clock = probe_extraction_clock(path)
    return clock_frame_range(clock, first, after_last)


def _frame_gray(path: Path, frame_index: int) -> bytes:
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-frames:v",
            "1",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-300:]
    return completed.stdout


def _mean_abs_diff(left: bytes, right: bytes) -> float:
    assert len(left) == len(right) and left
    return sum(abs(a - b) for a, b in zip(left, right, strict=False)) / len(left)


# ─────────────────────────────────────────────────────────────
#  합성 원본 — 세션마다 한 번
# ─────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("native_media")


@pytest.fixture(scope="session")
def cfr25(media: Path) -> Path:
    """25fps H.264 + AAC. 키프레임은 3초마다 — 1.5초 절단은 GOP 중간이다."""
    path = media / "cfr25.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=6",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            "-force_key_frames",
            "expr:gte(t,n_forced*3)",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def ntsc(media: Path) -> Path:
    """30000/1001 — µs로 떨어지지 않는 프레임 길이의 근거."""
    path = media / "ntsc.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30000/1001:duration=3.5",
            "-force_key_frames",
            "expr:gte(t,n_forced*2)",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def late_audio(media: Path) -> Path:
    """오디오가 영상보다 3.5초 늦게 시작한다 — 공통 시프트가 깨지면 0으로 날아간다."""
    path = media / "late_audio.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=6",
            "-itsoffset",
            "3.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.5",
            "-force_key_frames",
            "expr:gte(t,n_forced*3)",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def delayed_video(media: Path) -> Path:
    """영상 PTS가 0이 아니다 — 오디오가 2초 앞서고 영상은 2초부터 시작한다."""
    path = media / "delayed_video.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=8",
            "-itsoffset",
            "2.0",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=30000/1001:duration=6",
            "-map",
            "1:v",
            "-map",
            "0:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def multi_audio(media: Path) -> Path:
    """오디오 트랙 둘(AAC 48k + MP3 44.1k) — 하나도 못 잃는다."""
    path = media / "multi_audio.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:sample_rate=44100:duration=4",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a:0",
            "aac",
            "-b:a",
            "128k",
            "-c:a:1",
            "libmp3lame",
            "-b:a",
            "96k",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def audio_tail(media: Path) -> Path:
    """오디오가 영상 끝 뒤까지 이어진다(영상 4초, 오디오 5.5초) — 꼬리 보존."""
    path = media / "audio_tail.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=5.5",
            "-af",
            "adelay=0",
            "-force_key_frames",
            "expr:gte(t,n_forced*3)",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def tenbit_hevc(media: Path) -> Path:
    path = media / "tenbit.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=2",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p10le",
            "-tag:v",
            "hvc1",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def rotated(media: Path) -> Path:
    """저장 기하 320x180 + display matrix 90° — 세로 촬영의 합성 재현."""
    path = media / "rotated.mp4"
    _encode(
        [
            "-display_rotation",
            "90",
            "-noautorotate",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def vfr(media: Path) -> Path:
    path = media / "vfr.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=30:duration=1",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-fps_mode",
            "passthrough",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def vp9(media: Path) -> Path:
    path = media / "vp9.webm"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1",
            "-c:v",
            "libvpx-vp9",
            "-b:v",
            "0",
            "-crf",
            "40",
            "-deadline",
            "realtime",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def twelve_bit_hevc(media: Path) -> Path:
    path = media / "twelvebit.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p12le",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def chroma444_h264(media: Path) -> Path:
    path = media / "chroma444.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv444p",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def hdr_tagged_hevc(media: Path) -> Path:
    path = media / "hdr_tagged.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1",
            "-vf",
            "setparams=range=tv:color_primaries=bt2020:color_trc=smpte2084:colorspace=bt2020nc",
            "-c:v",
            "libx265",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p10le",
            str(path),
        ]
    )
    return path


# ─────────────────────────────────────────────────────────────
#  CFR 프레임 정확성 — GOP 중간 절단
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_mid_gop_cut_is_frame_exact_at_25fps(cfr25: Path, tmp_path: Path) -> None:
    """키프레임 3초 간격의 1.5초 지점(=프레임 37)을 잘라도 그 프레임이 첫 장이다."""
    result = extract_raw_clip(cfr25, tmp_path / "a.mp4", _clip(cfr25, 37, 87), RAW_SOURCE_MATCHED)
    assert result.frame_count == 50
    assert result.frame_duration == Fraction(1, 25)
    assert result.core_local_start == 0

    output_frame = _frame_gray(result.path, 0)
    wanted = _frame_gray(cfr25, 37)
    neighbor = _frame_gray(cfr25, 38)
    # 재인코딩(crf 17) 오차 안에서 같은 프레임이고, 이웃은 확실히 다르다.
    assert _mean_abs_diff(wanted, output_frame) < _REENCODE_LUMA_TOLERANCE
    assert _mean_abs_diff(neighbor, output_frame) > _NEIGHBOR_LUMA_GAP
    assert _mean_abs_diff(neighbor, wanted) > _NEIGHBOR_LUMA_GAP  # 구분 가능성의 증명


@needs_ffmpeg
def test_mid_gop_cut_is_frame_exact_at_ntsc_rate(ntsc: Path, tmp_path: Path) -> None:
    """30000/1001 — µs로 안 떨어지는 길이에서 프레임을 잃거나 얻지 않는다."""
    clock = probe_extraction_clock(ntsc)
    assert clock.frame_duration == Fraction(1001, 30000)
    assert clock.uniform_grid
    # 키프레임 2초 간격: 프레임 30(≈1.0초)은 GOP 중간이다.
    result = extract_raw_clip(
        ntsc, tmp_path / "b.mp4", clock_frame_range(clock, 30, 90), RAW_SOURCE_MATCHED
    )
    assert result.frame_count == 60
    assert result.frame_duration == Fraction(1001, 30000)
    output_frame = _frame_gray(result.path, 0)
    wanted = _frame_gray(ntsc, 30)
    neighbor = _frame_gray(ntsc, 31)
    assert _mean_abs_diff(wanted, output_frame) < _REENCODE_LUMA_TOLERANCE
    assert _mean_abs_diff(neighbor, output_frame) > _NEIGHBOR_LUMA_GAP


@needs_ffmpeg
def test_clip_local_clock_starts_at_zero_with_common_shift(cfr25: Path, tmp_path: Path) -> None:
    clock = probe_extraction_clock(cfr25)
    result = extract_raw_clip(
        cfr25, tmp_path / "c.mp4", clock_frame_range(clock, 50, 120), RAW_SOURCE_MATCHED
    )
    out_clock = probe_extraction_clock(result.path)
    assert abs(out_clock.presentation_origin) <= out_clock.time_base
    assert out_clock.uniform_grid


# ─────────────────────────────────────────────────────────────
#  0이 아닌 영상 PTS · 늦은/여러/긴 오디오
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_nonzero_video_pts_stays_absolute_in_the_record(
    delayed_video: Path, tmp_path: Path
) -> None:
    clock = probe_extraction_clock(delayed_video)
    assert clock.presentation_origin > 1  # 영상 원점이 0이 아니다
    result = extract_raw_clip(
        delayed_video, tmp_path / "d.mp4", clock_frame_range(clock, 10, 160), RAW_SOURCE_MATCHED
    )
    # 절대 좌표는 원본 시계 그대로, 지역 시각은 0부터 — 두 기록이 갈라진다.
    assert result.media_range.start_seconds == clock.presentation_origin + 10 * clock.frame_duration
    assert result.core_local_start == 0
    assert result.frame_count == 150
    # 오디오는 영상보다 2초 앞서 흐르던 트랙 — 클립 안에서도 0부터 흐른다(공통 시프트).
    assert result.audio_local_starts[0][1] == Fraction(0)


@needs_ffmpeg
def test_late_audio_keeps_its_relative_offset(late_audio: Path, tmp_path: Path) -> None:
    """오디오가 3.5초에 시작하는 원본의 2.0~6.0초 조각 — 지역 1.5초에 시작해야 한다.

    영상·오디오를 따로 0으로 만드는 버그라면 오디오가 0에서 시작한다(§8.3).
    """
    extents = probe_audio_extents(late_audio)
    assert extents and extents[0].first_pts is not None
    assert extents[0].first_pts * extents[0].time_base > 3  # 실측: 늦게 시작한다

    clock = probe_extraction_clock(late_audio)
    result = extract_raw_clip(
        late_audio, tmp_path / "e.mp4", clock_frame_range(clock, 50, 150), RAW_SOURCE_MATCHED
    )
    local_start = result.audio_local_starts[0][1]
    packet = Fraction(extents[0].packet_ticks, 1) * extents[0].time_base
    assert local_start is not None
    assert abs(local_start - Fraction(3, 2)) <= packet + Fraction(1, 48000)


@needs_ffmpeg
def test_every_audio_track_survives_with_its_shape(multi_audio: Path, tmp_path: Path) -> None:
    source = probe_raw_media(multi_audio)
    result = extract_raw_clip(
        multi_audio, tmp_path / "f.mp4", _clip(multi_audio, 10, 60), RAW_SOURCE_MATCHED
    )
    assert [(t.codec_name, t.channels, t.sample_rate) for t in result.audio] == [
        (t.codec_name, t.channels, t.sample_rate) for t in source.audio
    ]
    assert result.audio_local_starts and all(
        start is not None for _, start in result.audio_local_starts
    )


@needs_ffmpeg
def test_audio_tail_beyond_video_end_is_preserved(audio_tail: Path, tmp_path: Path) -> None:
    """마지막 조각 — 영상은 1초지만 오디오는 2.5초까지 이어진다(§8.3, 코디네이터 재결)."""
    clock = probe_extraction_clock(audio_tail)
    result = extract_raw_clip(
        audio_tail, tmp_path / "g.mp4", clock_frame_range(clock, 75, 100), RAW_SOURCE_MATCHED
    )
    assert result.frame_count == 25
    assert result.media_local_end > Fraction(2)  # 영상 1초 + 오디오 꼬리
    assert any("오디오 꼬리" in warning for warning in result.warnings)


@needs_ffmpeg
def test_audio_without_packets_in_window_is_a_loud_unmapped_note(
    late_audio: Path, tmp_path: Path
) -> None:
    """오디오가 아직 시작하지 않은 앞구간 조각 — 빈 트랙이 조용히 사라지지 않는다."""
    clock = probe_extraction_clock(late_audio)
    result = extract_raw_clip(
        late_audio, tmp_path / "h.mp4", clock_frame_range(clock, 0, 50), RAW_SOURCE_MATCHED
    )
    assert result.audio == ()
    assert result.unmapped_tracks == ((1, "이 구간에 오디오 패킷이 없다"),)


# ─────────────────────────────────────────────────────────────
#  핸들 · 회전 · 비트 깊이 · 프로파일
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_handles_widen_the_media_range_and_record_core_position(
    cfr25: Path, tmp_path: Path
) -> None:
    result = extract_raw_clip(
        cfr25,
        tmp_path / "i.mp4",
        _clip(cfr25, 50, 80),
        RAW_SOURCE_MATCHED,
        handles=RawClipHandles(before=Fraction(1), after=Fraction(1)),
    )
    assert result.frame_count == 25 + 25 + 30  # 핸들 1초씩 + core 30프레임
    assert result.core_local_start == Fraction(1)
    assert result.core_local_end == Fraction(1) + Fraction(30, 25)


@needs_ffmpeg
def test_rotation_is_applied_physically_and_recorded(rotated: Path, tmp_path: Path) -> None:
    source = probe_raw_media(rotated)
    assert (source.video.width, source.video.height, source.video.rotation_degrees) == (
        320,
        180,
        90,
    )
    result = extract_raw_clip(
        rotated, tmp_path / "j.mp4", _clip(rotated, 0, 25), RAW_SOURCE_MATCHED
    )
    assert (result.video.width, result.video.height) == (180, 320)  # 표시 방향 보존
    assert result.video.rotation_degrees == 0  # 물리로 적용해 태그는 남지 않는다
    assert result.rotation_applied
    assert any("회전 90" in warning for warning in result.warnings)


@needs_ffmpeg
def test_tenbit_source_matched_keeps_the_bit_depth(tenbit_hevc: Path, tmp_path: Path) -> None:
    result = extract_raw_clip(
        tenbit_hevc, tmp_path / "k.mp4", _clip(tenbit_hevc, 5, 25), RAW_SOURCE_MATCHED
    )
    assert result.video.codec_name == "hevc"
    assert result.video.pix_fmt == "yuv420p10le"
    assert result.video.bit_depth == 10


@needs_ffmpeg
def test_edit_prores_profile_produces_a_verified_edit_master(cfr25: Path, tmp_path: Path) -> None:
    result = extract_raw_clip(cfr25, tmp_path / "l.mov", _clip(cfr25, 20, 70), RAW_EDIT_PRORES)
    assert result.profile_name == "edit_prores"
    assert result.path.suffix == ".mov"
    assert result.video.codec_name == "prores"
    assert result.video.pix_fmt == "yuv422p10le"
    assert all(track.codec_name == "pcm_s16le" for track in result.audio)
    assert any("4:2:2 업샘플링" in warning for warning in result.warnings)


@needs_ffmpeg
def test_declared_colors_survive_the_transcode(tmp_path: Path) -> None:
    path = tmp_path / "colored.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=160x90:rate=25:duration=1",
            # 색 좌표는 setparams로 프레임에 싣는다 — 출력 옵션만으로는 인코더가 버린다
            "-vf",
            "setparams=range=tv:color_primaries=bt709:color_trc=bt709:colorspace=bt709",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ]
    )
    result = extract_raw_clip(path, tmp_path / "out.mp4", _clip(path, 0, 20), RAW_SOURCE_MATCHED)
    assert result.video.color_primaries == "bt709"
    assert result.video.color_transfer == "bt709"
    assert result.video.color_matrix == "bt709"
    assert result.video.color_range == "tv"


# ─────────────────────────────────────────────────────────────
#  미지원은 정직하게 막는다
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_vfr_is_measured_and_refused(vfr: Path, tmp_path: Path) -> None:
    clock = probe_extraction_clock(vfr)
    assert clock.uniform_grid is False
    assert clock.grid_note
    with pytest.raises(RawMediaUnsupported, match="VFR"):
        extract_raw_clip(
            vfr, tmp_path / "no.mp4", clock_frame_range(clock, 0, 10), RAW_SOURCE_MATCHED
        )
    assert not (tmp_path / "no.mp4").exists()  # 호출 전에 막혔다


@needs_ffmpeg
def test_unsupported_codec_bit_depth_and_chroma_are_refused(
    vp9: Path, twelve_bit_hevc: Path, chroma444_h264: Path, tmp_path: Path
) -> None:
    for source, pattern in (
        (vp9, "코덱"),
        (twelve_bit_hevc, "픽셀 형식"),
        (chroma444_h264, "픽셀 형식"),
    ):
        with pytest.raises(RawMediaUnsupported, match=pattern):
            extract_raw_clip(source, tmp_path / "x.mp4", _clip(source, 0, 10), RAW_SOURCE_MATCHED)


@needs_ffmpeg
def test_declared_hdr_transfer_is_refused_not_downgraded(
    hdr_tagged_hevc: Path, tmp_path: Path
) -> None:
    probe = probe_raw_media(hdr_tagged_hevc)
    assert probe.video.color_transfer == "smpte2084"
    with pytest.raises(RawMediaUnsupported, match="HDR"):
        extract_raw_clip(
            hdr_tagged_hevc, tmp_path / "x.mp4", _clip(hdr_tagged_hevc, 0, 10), RAW_SOURCE_MATCHED
        )


@needs_ffmpeg
def test_a_downgrading_profile_cannot_sneak_past_verification(
    tenbit_hevc: Path, tmp_path: Path
) -> None:
    """10bit 원본에 8bit를 강요하는 변형 프로파일 — 검증이 게시를 막는다."""
    sneaky = RawTranscodeProfile(
        name="source_matched",
        audio_codec="copy",
        container="mp4",
        pix_fmt="yuv420p",
        encoder_args=(),
    )
    with pytest.raises(RawClipVerificationError, match="픽셀 형식|비트"):
        extract_raw_clip(tenbit_hevc, tmp_path / "sneaky.mp4", _clip(tenbit_hevc, 0, 10), sneaky)
    assert not (tmp_path / "published.mp4").exists()  # 성공 경로가 아니면 값이 없다


# ─────────────────────────────────────────────────────────────
#  원본 불변 · 순서 호환
# ─────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_source_files_are_unchanged_by_extraction(
    cfr25: Path, late_audio: Path, tenbit_hevc: Path, tmp_path: Path
) -> None:
    before = {path: _sha256(path) for path in (cfr25, late_audio, tenbit_hevc)}
    for index, source in enumerate((cfr25, late_audio, tenbit_hevc)):
        extract_raw_clip(
            source, tmp_path / f"u{index}.mp4", _clip(source, 5, 45), RAW_SOURCE_MATCHED
        )
    assert before == {path: _sha256(path) for path in (cfr25, late_audio, tenbit_hevc)}


@needs_ffmpeg
def test_sequential_extractions_are_repeatable(cfr25: Path, tmp_path: Path) -> None:
    """A3 러너는 순서대로 부른다 — 같은 요청은 같은 구조 검증 결과를 낸다."""
    span = _clip(cfr25, 20, 70)
    first = extract_raw_clip(cfr25, tmp_path / "p1.mp4", span, RAW_SOURCE_MATCHED)
    second = extract_raw_clip(cfr25, tmp_path / "p2.mp4", span, RAW_SOURCE_MATCHED)
    assert (
        first.frame_count,
        first.frame_duration,
        first.core_local_start,
        first.core_local_end,
    ) == (second.frame_count, second.frame_duration, second.core_local_start, second.core_local_end)
    assert first.path != second.path  # 호출자가 준 staging 경로를 그대로 쓴다


@needs_ffmpeg
def test_cached_probe_clock_and_extents_are_accepted(cfr25: Path, tmp_path: Path) -> None:
    """원본마다 한 번씩만 조사하도록 캐시 인자를 지원한다(러너 호환)."""
    probe = probe_raw_media(cfr25)
    clock = probe_extraction_clock(cfr25, probe=probe)
    extents = probe_audio_extents(cfr25)
    result = extract_raw_clip(
        cfr25,
        tmp_path / "cached.mp4",
        clock_frame_range(clock, 10, 60),
        RAW_SOURCE_MATCHED,
        probe=probe,
        clock=clock,
        audio_extents=extents,
    )
    assert result.frame_count == 50
