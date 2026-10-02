"""원본 미디어 경계 — 조사·시계·지원 매트릭스의 데이터 계약(A3).

`vlogsrt.preprocess`의 A3 절을 ffprobe 없이 시험한다. 파싱은 진짜 ffprobe JSON
모양의 픽스처로, 지원 매트릭스는 조립한 값으로 — 실제 추출은
`test_native_media_extract.py`가 진짜 ffmpeg로 잰다.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import preprocess


def _video_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "index": 0,
        "codec_type": "video",
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "width": 3840,
        "height": 2160,
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
        "color_range": "tv",
        "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1",
        "time_base": "1/12800",
        "start_time": "0.020000",
        "start_pts": 256,
        "duration_ts": 51200,
    }
    entry.update(overrides)
    return entry


def _audio_entry(**overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "index": 1,
        "codec_type": "audio",
        "codec_name": "aac",
        "sample_rate": "48000",
        "channels": "2",
        "time_base": "1/48000",
        "start_time": "-0.021333",
        "start_pts": -1024,
    }
    entry.update(overrides)
    return entry


def _probe_json(
    streams: list[dict[str, object]] | None = None,
    *,
    duration: str = "2.000000",
    creation_time: str | None = None,
) -> str:
    streams = streams if streams is not None else [_video_entry(), _audio_entry()]
    tags = {"creation_time": creation_time} if creation_time else {}
    return json.dumps(
        {
            "streams": streams,
            "format": {"format_name": "mov,mp4", "duration": duration, "tags": tags},
        }
    )


class _Done:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.returncode = 0


def _fake_ffprobe(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    """ffprobe 호출만 대답한다 — `-version`은 진짜 ffprobe를 믿는다."""

    def run(command: list[str]) -> object:
        if "-version" in command:
            return _Done("ffprobe version 9.0.1-test\nconfiguration: --flags\n")
        assert "-show_entries" in command, f"예상 밖의 호출: {command}"
        return _Done(stdout)

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)


# ─────────────────────────────────────────────────────────────
#  빠른 조사 — probe_raw_media
# ─────────────────────────────────────────────────────────────


def test_probe_reads_the_shooting_clock_and_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shot = "2026-10-02T09:30:00.000000+09:00"
    _fake_ffprobe(monkeypatch, _probe_json(creation_time=shot))
    probe = preprocess.probe_raw_media(tmp_path / "card.mp4")

    assert probe.container == "mov,mp4"
    assert probe.duration == Fraction(2)
    assert probe.video.bit_depth == 8
    assert probe.video.rotation_degrees == 0
    assert probe.video.time_base == Fraction(1, 12800)
    assert probe.video.r_frame_rate == Fraction(25)
    assert probe.video.start_pts == 256
    assert probe.creation_time.valid
    assert probe.creation_time.utc == datetime.fromisoformat(shot).astimezone(UTC)


def test_probe_reports_rotation_and_keeps_stored_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rotated = _video_entry(side_data_list=[{"rotation": -90}])
    _fake_ffprobe(monkeypatch, _probe_json([rotated, _audio_entry()]))
    probe = preprocess.probe_raw_media(tmp_path / "vertical.mov")

    # 저장 기하와 표시 회전이 갈라져 기록된다 — 재생 화면이 뒤바뀌는 건 회전 값이 말한다.
    assert (probe.video.width, probe.video.height) == (3840, 2160)
    assert probe.video.rotation_degrees == -90


@pytest.mark.parametrize(
    "raw", ["", "not-a-date", "2026-13-45T99:00:00Z", "1980-01-01T00:00:00+00:00"]
)
def test_creation_time_is_invalid_without_iso8601_or_saneness(
    raw: str,
) -> None:
    verdict = preprocess._parse_creation_time(raw)
    assert verdict.valid is False
    assert verdict.utc is None


def test_creation_time_rejects_naive_and_future_stamps() -> None:
    # 시간대 없는 시각은 같은 카드 안에서도 비교할 수 없다(§6.2 원본 순서).
    assert preprocess._parse_creation_time("2026-10-02T09:30:00").valid is False
    assert preprocess._parse_creation_time("9999-12-31T23:59:59+00:00").valid is False
    assert preprocess._parse_creation_time("1985-01-01T00:00:00+00:00").valid is False
    soon = (datetime.now(tz=UTC) + timedelta(hours=1)).isoformat()
    assert preprocess._parse_creation_time(soon).valid is True


def test_unknown_colors_stay_empty_instead_of_being_invented(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    silent = _video_entry(
        color_primaries="unknown",
        color_transfer=None,
        color_space="unknown",
        color_range=None,
    )
    _fake_ffprobe(monkeypatch, _probe_json([silent]))
    probe = preprocess.probe_raw_media(tmp_path / "untagged.mp4")
    assert probe.video.color_primaries == ""
    assert probe.video.color_transfer == ""
    assert probe.video.color_matrix == ""
    assert probe.video.color_range == ""


def test_audio_tracks_and_other_streams_are_all_listed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    streams = [
        _video_entry(),
        _audio_entry(),
        _audio_entry(index=2, codec_name="mp3", sample_rate="44100", channels="1"),
        {"index": 3, "codec_type": "data", "codec_name": "gpmd"},
        {"index": 4, "codec_type": "video", "codec_name": "hevc"},
    ]
    _fake_ffprobe(monkeypatch, _probe_json(streams))
    probe = preprocess.probe_raw_media(tmp_path / "dji.mp4")

    assert [(track.codec_name, track.channels, track.sample_rate) for track in probe.audio] == [
        ("aac", 2, 48000),
        ("mp3", 1, 44100),
    ]
    # 선언 start_time은 µs로 반올림된 문자열을 정확한 분수로 읽는다(-1024/48000과 같지 않다).
    assert probe.audio[0].start_time == Fraction(-21333, 1_000_000)
    assert probe.audio[0].start_pts == -1024
    # 두 번째 비디오와 데이터 트랙은 사라지지 않고 남는다 — 지원 매트릭스가 본다.
    assert probe.other_tracks == ((3, "data"), (4, "video"))


@pytest.mark.parametrize(
    "overrides",
    [
        {"codec_type": "audio"},  # 비디오가 없다
        {"r_frame_rate": "0/0"},  # 시계가 아니다
        {"start_pts": "N/A"},  # 선언이 없다 — 조용한 0이 아니라 실패다
        {"duration_ts": 0},
    ],
)
def test_probe_dies_loudly_on_broken_declarations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, overrides: dict[str, object]
) -> None:
    _fake_ffprobe(monkeypatch, _probe_json([_video_entry(**overrides)]))
    with pytest.raises(RuntimeError):
        preprocess.probe_raw_media(tmp_path / "broken.mp4")


def test_probe_rejects_broken_json(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ffprobe(monkeypatch, "ffprobe가 반쯤 죽으며 흘린 출력")
    with pytest.raises(RuntimeError):
        preprocess.probe_raw_media(tmp_path / "garbage.mp4")


@pytest.mark.parametrize(
    ("pix_fmt", "depth"),
    [
        ("yuv420p", 8),
        ("yuv420p10le", 10),
        ("yuv420p12le", 12),
        ("gray10be", 10),
        ("rgb24", 8),
        ("rgb48le", 16),
        ("gbrp10le", 10),
    ],
)
def test_bit_depth_comes_from_the_pix_fmt_name(pix_fmt: str, depth: int) -> None:
    assert preprocess._pix_fmt_bit_depth(pix_fmt) == depth


# ─────────────────────────────────────────────────────────────
#  추출 시계 — probe_extraction_clock · 범위 helpers
# ─────────────────────────────────────────────────────────────


def _clock_json(pts: list[int], *, rate: str = "25/1", tb: str = "1/12800") -> str:
    stream = {
        "index": 0,
        "codec_type": "video",
        "r_frame_rate": rate,
        "avg_frame_rate": rate,
        "time_base": tb,
        "nb_frames": str(len(pts)),
        "start_pts": pts[0],
        "duration_ts": pts[-1] - pts[0] + 512,
    }
    return json.dumps({"streams": [stream], "packets": [{"pts": value} for value in pts]})


def test_clock_measures_the_grid_and_exposes_the_vfr_fact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    uniform = [256 + 512 * n for n in range(100)]
    _fake_ffprobe(monkeypatch, _clock_json(uniform))
    clock = preprocess.probe_extraction_clock(tmp_path / "cfr.mp4")
    assert clock.uniform_grid
    assert clock.grid_note == ""
    assert clock.presentation_origin == Fraction(256, 12800)
    assert clock.frame_duration == Fraction(1, 25)
    assert clock.frame_count == 100
    assert clock.exact_end == Fraction(256, 12800) + 100 * Fraction(1, 25)

    # 30번째 프레임을 한 틱 밀면 격자 증명이 실패하고, 그 사실이 글자로 남는다.
    jittered = list(uniform)
    jittered[30] += 7
    _fake_ffprobe(monkeypatch, _clock_json(jittered))
    vfr = preprocess.probe_extraction_clock(tmp_path / "vfr.mp4")
    assert vfr.uniform_grid is False
    assert "격자를 벗어났습니다" in vfr.grid_note


def test_clock_declared_mismatch_is_a_note_not_a_vfr_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = [512 * n for n in range(50)]
    stream = {
        "index": 0,
        "codec_type": "video",
        "r_frame_rate": "25/1",
        "avg_frame_rate": "25/1",
        "time_base": "1/12800",
        "nb_frames": "49",  # 선언 개수가 디코드 증거와 다르다
        "start_pts": 0,
        "duration_ts": 512 * 50,
    }
    stdout = json.dumps({"streams": [stream], "packets": [{"pts": value} for value in pts]})
    _fake_ffprobe(monkeypatch, stdout)
    clock = preprocess.probe_extraction_clock(tmp_path / "declared.mp4")
    # PTS 격자가 균일하면 VFR이 아니다 — 선언 불일치는 기록으로만 남는다.
    assert clock.uniform_grid
    assert "선언 불일치" in clock.grid_note


def test_frame_range_is_exact_rational_ticks(tmp_path: Path) -> None:
    clock = preprocess.RawVideoClock(
        stream_index=0,
        time_base=Fraction(1, 30000),
        first_pts=37037,
        last_pts=127117,
        presentation_origin=Fraction(37037, 30000),
        frame_duration=Fraction(1001, 30000),
        frame_count=90,
        exact_end=Fraction(127127, 30000),
        uniform_grid=True,
        grid_note="",
    )
    span = preprocess.clock_frame_range(clock, 20, 70)
    assert span.time_base == Fraction(1, 30000)
    assert span.start_seconds == Fraction(57057, 30000)
    assert span.end_seconds == Fraction(107107, 30000)  # 원점 37037 + 70×1001 틱
    assert span.duration_seconds == 50 * Fraction(1001, 30000)
    with pytest.raises(RuntimeError):
        preprocess.clock_frame_range(clock, 60, 95)  # 원본 프레임 수를 넘는다
    with pytest.raises(RuntimeError):
        preprocess.clock_frame_range(clock, 40, 40)  # 빈 창


def test_range_covering_snaps_outward_to_frames(tmp_path: Path) -> None:
    clock = preprocess.RawVideoClock(
        stream_index=0,
        time_base=Fraction(1, 12800),
        first_pts=256,
        last_pts=51299,
        presentation_origin=Fraction(256, 12800),
        frame_duration=Fraction(1, 25),
        frame_count=100,
        exact_end=Fraction(256, 12800) + 4,
        uniform_grid=True,
        grid_note="",
    )
    # 1.03초는 25번째(1.00)와 26번째(1.04) 프레임 사이다 — 바깥으로 덮는다(안 잘린다).
    span = preprocess.clock_range_covering(clock, Fraction(103, 100), Fraction(203, 100))
    assert span.start_seconds == Fraction(256, 12800) + Fraction(1)
    assert span.end_seconds == Fraction(256, 12800) + Fraction(51, 25)  # ceil(50.25) 프레임
    with pytest.raises(RuntimeError):
        preprocess.clock_range_covering(clock, Fraction(10), Fraction(12))  # 원본 밖
    with pytest.raises(RuntimeError):
        preprocess.clock_range_covering(clock, Fraction(1), 1.5)  # float는 정확한 시각이 아니다


def test_us_formatting_floors_and_ceils_exactly() -> None:
    assert preprocess._format_us(Fraction(1, 3), ceil=False) == "0.333333"
    assert preprocess._format_us(Fraction(1, 3), ceil=True) == "0.333334"
    assert preprocess._format_us(Fraction(57056, 30000), ceil=False) == "1.901866"
    assert preprocess._format_us(Fraction(0), ceil=False) == "0.000000"


# ─────────────────────────────────────────────────────────────
#  지원 매트릭스 — 정밀도를 낮추지 않고 막는다
# ─────────────────────────────────────────────────────────────


def _matrix_probe(**video_overrides: object) -> preprocess.RawMediaProbe:
    video = preprocess.RawVideoMedia(
        stream_index=0,
        codec_name="h264",
        profile="High",
        pix_fmt="yuv420p",
        bit_depth=8,
        width=1920,
        height=1080,
        color_primaries="bt709",
        color_transfer="bt709",
        color_matrix="bt709",
        color_range="tv",
        rotation_degrees=0,
        time_base=Fraction(1, 15360),
        r_frame_rate=Fraction(25),
        avg_frame_rate=Fraction(25),
        start_pts=0,
        duration_ts=51200,
    )
    for field, value in video_overrides.items():
        video = replace(video, **{field: value})
    return preprocess.RawMediaProbe(
        source=Path("matrix.mp4"),
        container="mov,mp4",
        duration=Fraction(2),
        creation_time=preprocess.CreationTime(raw="", valid=False, utc=None),
        video=video,
        audio=(),
        other_tracks=(),
        ffprobe_version="ffprobe version 9-test",
    )


def _matrix_clock(**overrides: object) -> preprocess.RawVideoClock:
    fields: dict[str, object] = {
        "stream_index": 0,
        "time_base": Fraction(1, 15360),
        "first_pts": 0,
        "last_pts": 51199,
        "presentation_origin": Fraction(0),
        "frame_duration": Fraction(1, 25),
        "frame_count": 50,
        "exact_end": Fraction(2),
        "uniform_grid": True,
        "grid_note": "",
    }
    fields.update(overrides)
    return preprocess.RawVideoClock(**fields)  # type: ignore[arg-type]


def test_supported_matrix_accepts_the_normal_camera_shape() -> None:
    preprocess._assert_raw_supported(_matrix_probe(), _matrix_clock())  # 죽지 않는다


@pytest.mark.parametrize(
    "overrides",
    [
        {"codec_name": "vp9"},
        {"codec_name": "prores"},
        {"pix_fmt": "yuv422p10le"},
        {"pix_fmt": "yuv444p10le"},
        {"pix_fmt": "yuv420p12le"},
        {"pix_fmt": "yuva420p"},
        {"color_transfer": "smpte2084"},
        {"color_transfer": "arib-std-b67"},
    ],
)
def test_unsupported_precision_is_refused_not_downgraded(overrides: dict[str, object]) -> None:
    with pytest.raises(preprocess.RawMediaUnsupported) as raised:
        preprocess._assert_raw_supported(_matrix_probe(**overrides), _matrix_clock())
    assert "지원하지 않는" in str(raised.value)


def test_extra_video_stream_and_vfr_are_refused() -> None:
    probe = _matrix_probe()
    two_videos = replace(probe, other_tracks=((2, "video"),))
    with pytest.raises(preprocess.RawMediaUnsupported, match="비디오 스트림이 둘 이상"):
        preprocess._assert_raw_supported(two_videos, _matrix_clock())
    with pytest.raises(preprocess.RawMediaUnsupported, match="VFR"):
        preprocess._assert_raw_supported(
            _matrix_probe(), _matrix_clock(uniform_grid=False, grid_note="측정된 비균일")
        )


def test_packet_evidence_beyond_declared_duration_is_refused() -> None:
    # 선언 길이보다 먼 패킷은 입력 탐색 창이 잃는다 — 실측 근거의 실패 사례다.
    with pytest.raises(preprocess.RawMediaUnsupported, match="선언 길이"):
        preprocess._assert_raw_supported(_matrix_probe(), _matrix_clock(exact_end=Fraction(3)))


def test_profiles_are_frozen_constants() -> None:
    assert preprocess.RAW_SOURCE_MATCHED.name == "source_matched"
    assert preprocess.RAW_SOURCE_MATCHED.audio_codec == "copy"
    assert preprocess.RAW_SOURCE_MATCHED.pix_fmt is None  # 원본 픽셀 형식 유지
    assert preprocess.RAW_EDIT_PRORES.name == "edit_prores"
    assert preprocess.RAW_EDIT_PRORES.pix_fmt == "yuv422p10le"
    assert preprocess.RAW_EDIT_PRORES.audio_codec == "pcm_s16le"
    assert preprocess.RAW_EDIT_PRORES.container == "mov"
    with pytest.raises(FrozenInstanceError):
        preprocess.RAW_SOURCE_MATCHED.name = "keyframe_copy"  # type: ignore[misc]


def test_handles_default_to_zero_and_reject_imprecise_values() -> None:
    handles = preprocess.RawClipHandles()
    assert handles.before == 0 and handles.after == 0
    plan = preprocess._plan_raw_media(
        _matrix_clock(),
        preprocess.RawPtsRange(time_base=Fraction(1, 15360), start_pts=0, end_pts=25600),
        handles,
        (),
    )
    assert plan.core_local_start == 0
    with pytest.raises(RuntimeError, match="Fraction"):
        preprocess._plan_raw_media(
            _matrix_clock(),
            preprocess.RawPtsRange(time_base=Fraction(1, 15360), start_pts=0, end_pts=25600),
            preprocess.RawClipHandles(before=0.5),  # type: ignore[arg-type]
            (),
        )


def test_plan_maps_only_audio_with_packets_in_the_window() -> None:
    clock = _matrix_clock()
    flowing = preprocess.AudioPacketExtent(
        stream_index=1,
        time_base=Fraction(1, 48000),
        first_pts=0,
        last_pts=95999,
        packet_count=94,
        packet_ticks=1024,
    )
    late = preprocess.AudioPacketExtent(
        stream_index=2,
        time_base=Fraction(1, 48000),
        first_pts=48000 * 60,
        last_pts=48000 * 61,
        packet_count=47,
        packet_ticks=1024,
    )
    plan = preprocess._plan_raw_media(
        clock,
        preprocess.RawPtsRange(time_base=Fraction(1, 15360), start_pts=0, end_pts=25600),
        preprocess.RawClipHandles(),
        (flowing, late),
    )
    assert [extent.stream_index for extent in plan.mapped_audio] == [1]
    assert plan.unmapped == ((2, "이 구간에 오디오 패킷이 없다"),)
    assert any("매핑하지 않은 오디오" in warning for warning in plan.warnings)
