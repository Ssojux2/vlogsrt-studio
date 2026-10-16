"""D62 B3 — 썸네일의 빠른 프레임 찾기는 옛 `select=eq(n,N)`과 **바이트가 같은** 그림을 낸다.

옛 길은 스트림 처음부터 디코드해 번호에 비례해 느리다(4K HEVC 썸네일 24장에 30분 넘게). 새
길은 목표보다 조금 앞에서 입력을 찾아가고 `-copyts`로 절대 PTS를 지켜 증명된 격자의 정수
PTS로 고른다. 이 시험이 실제 ffmpeg로 같은 바이트임을 증명하므로 썸네일 캐시 열쇠는 그대로다.
"""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import preprocess

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

MARGIN = Fraction(1)
LONG_EDGE = 480


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 미디어 생성 실패: {completed.stderr[-400:]}"


def _ntsc_offset(path: Path) -> None:
    """29.97fps · time_base 1/30000 · 시작 PTS 2.5초 · B프레임 · GOP 30 · 12초(360장)."""
    _encode(
        [
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=30000/1001:duration=12",
            "-c:v", "libx264", "-g", "30", "-bf", "2", "-pix_fmt", "yuv420p",
            "-video_track_timescale", "30000", "-output_ts_offset", "2.5",
            str(path),
        ]
    )  # fmt: skip


def _leading_audio_open_gop(path: Path) -> None:
    """소리가 3.2초 먼저 시작한다(컨테이너 시작 < 영상 원점) · 25fps · 열린 GOP · 20초."""
    _encode(
        [
            "-itsoffset", "3.2",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=20",
            "-f", "lavfi", "-i", "sine=duration=24",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-g", "50", "-bf", "3", "-x264-params", "open-gop=1",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-video_track_timescale", "12800",
            str(path),
        ]
    )  # fmt: skip


def _old_and_new(
    source: Path, plan: preprocess.FrameSeekPlan, frame: int, tmp_path: Path
) -> tuple[bytes, bytes]:
    old = preprocess.extract_frame_index(
        source, plan.stream_index, frame, tmp_path / f"old_{frame}.jpg", long_edge=LONG_EDGE
    )
    new = preprocess.extract_frame_index(
        source,
        plan.stream_index,
        frame,
        tmp_path / f"new_{frame}.jpg",
        long_edge=LONG_EDGE,
        seek=plan,
    )
    return old.read_bytes(), new.read_bytes()


def _recording_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = preprocess._run

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(command))
        return real(command, **kwargs)

    monkeypatch.setattr(preprocess, "_run", run)
    return seen


@needs_ffmpeg
def test_seek_bytes_equal_the_decode_from_zero_on_a_2997_grid_with_a_late_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "ntsc.mp4"
    _ntsc_offset(source)
    plan = preprocess.probe_frame_seek_plan(source, 0, margin_seconds=MARGIN)
    assert plan is not None
    assert (plan.time_base, plan.frame_ticks, plan.frame_count) == (Fraction(1, 30000), 1001, 360)
    assert plan.first_pts == 75000  # 2.5초 — 0이 아닌 시작 PTS
    seen = _recording_run(monkeypatch)
    # 처음·키프레임·키 다음(비키)·가운데·끝 바로 앞·마지막
    for frame in (0, 1, 29, 30, 31, 151, 180, 358, 359):
        old, new = _old_and_new(source, plan, frame, tmp_path)
        assert old == new, f"프레임 {frame}의 바이트가 다르다"
    fast = [argv for argv in seen if "-copyts" in argv]
    assert len(fast) == 9  # 모든 빠른 호출이 그림을 냈다 — 옛 길로 물러나지 않았다
    assert sum("-ss" in argv for argv in fast) == 6  # 1초(≈30장) 뒤의 프레임은 찾아간다


@needs_ffmpeg
def test_seek_bytes_equal_when_audio_leads_and_the_gop_is_open(tmp_path: Path) -> None:
    source = tmp_path / "lead.mp4"
    _leading_audio_open_gop(source)
    plan = preprocess.probe_frame_seek_plan(source, 0, margin_seconds=MARGIN)
    assert plan is not None and plan.frame_count == 500
    assert plan.first_pts * plan.time_base == Fraction(16, 5)  # 영상 원점 3.2초
    for frame in (0, 1, 49, 50, 51, 249, 250, 251, 498, 499):
        old, new = _old_and_new(source, plan, frame, tmp_path)
        assert old == new, f"프레임 {frame}의 바이트가 다르다"


@needs_ffmpeg
def test_the_seek_argv_differs_from_the_old_argv_only_before_the_input_and_in_select(
    tmp_path: Path,
) -> None:
    plan = preprocess.FrameSeekPlan(
        stream_index=0,
        time_base=Fraction(1, 30000),
        first_pts=75000,
        frame_ticks=1001,
        frame_count=360,
        margin_seconds=MARGIN,
    )
    old = preprocess._frame_extraction_command(
        "ffmpeg", Path("in.mp4"), 0, 151, Path("o.jpg"), LONG_EDGE
    )
    new = preprocess._seek_frame_command("ffmpeg", Path("in.mp4"), plan, 151, Path("o.jpg"), 480)
    start = Fraction(151 * 1001, 30000) - 1  # 원점 상대 목표 − 여유
    jump = ["-noaccurate_seek", "-ss", preprocess._format_us(start, ceil=False), "-copyts"]
    assert new[4:8] == jump
    old_vf = "select=eq(n\\,151)"
    new_vf = f"select=eq(pts\\,{75000 + 151 * 1001})"
    swapped = [arg.replace(new_vf, old_vf) for arg in new[:4] + new[8:]]
    assert swapped == old  # 입력 앞 찾기와 select 식 말고는 한 글자도 같다
    near = preprocess._seek_frame_command("ffmpeg", Path("in.mp4"), plan, 10, Path("o.jpg"), 480)
    assert "-ss" not in near and "-copyts" in near  # 여유 안쪽 — 처음부터 디코드


@needs_ffmpeg
def test_a_variable_grid_has_no_seek_plan(tmp_path: Path) -> None:
    """프레임 하나가 빠진 격자(VFR) — 증명이 없으니 옛 길이다."""
    plain = tmp_path / "plain.mp4"
    _ntsc_offset(plain)
    vfr = tmp_path / "vfr.mp4"
    _encode(
        [
            "-i", str(plain), "-vf", "select='not(eq(n\\,5))'", "-fps_mode", "passthrough",
            "-c:v", "libx264", "-video_track_timescale", "30000", str(vfr),
        ]
    )  # fmt: skip
    assert preprocess.probe_frame_seek_plan(vfr, 0, margin_seconds=MARGIN) is None


@needs_ffmpeg
def test_a_quantized_clock_has_no_seek_plan(tmp_path: Path) -> None:
    """29.97을 1/1000 시계에 담은 MKV — 프레임 길이가 정수 틱이 아니다."""
    source = tmp_path / "q.mkv"
    _encode(
        [
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30000/1001:duration=2",
            "-c:v", "libx264", str(source),
        ]
    )  # fmt: skip
    assert preprocess.probe_frame_seek_plan(source, 0, margin_seconds=MARGIN) is None


@needs_ffmpeg
def test_discarded_edit_list_packets_have_no_seek_plan(tmp_path: Path) -> None:
    """키프레임 사이에서 복사로 자른 MP4 — 편집 목록이 앞 패킷에 버림(D)을 단다."""
    plain = tmp_path / "plain.mp4"
    _ntsc_offset(plain)
    cut = tmp_path / "cut.mp4"
    _encode(["-ss", "0.5", "-i", str(plain), "-c", "copy", str(cut)])
    assert preprocess.probe_frame_seek_plan(cut, 0, margin_seconds=MARGIN) is None


@needs_ffmpeg
def test_a_wrong_plan_falls_back_to_the_decode_from_zero(tmp_path: Path) -> None:
    """빠른 길이 그림을 못 내면(격자에 없는 PTS) 옛 길이 같은 바이트를 낸다 — 빈 성공은 없다."""
    source = tmp_path / "ntsc.mp4"
    _ntsc_offset(source)
    plan = preprocess.probe_frame_seek_plan(source, 0, margin_seconds=MARGIN)
    assert plan is not None
    broken = preprocess.FrameSeekPlan(
        stream_index=0,
        time_base=plan.time_base,
        first_pts=plan.first_pts + 7,  # 어떤 프레임도 이 PTS에 없다
        frame_ticks=plan.frame_ticks,
        frame_count=plan.frame_count,
        margin_seconds=MARGIN,
    )
    expected = preprocess.extract_frame_index(
        source, 0, 200, tmp_path / "old.jpg", long_edge=LONG_EDGE
    )
    got = preprocess.extract_frame_index(
        source, 0, 200, tmp_path / "new.jpg", long_edge=LONG_EDGE, seek=broken
    )
    assert got.read_bytes() == expected.read_bytes()


def test_the_plan_is_ignored_for_another_stream_or_an_index_outside_the_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.append(list(command))
        Path(command[-1]).write_bytes(b"jpg")
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    plan = preprocess.FrameSeekPlan(0, Fraction(1, 30000), 0, 1001, 10, MARGIN)
    preprocess.extract_frame_index(Path("in.mp4"), 1, 3, tmp_path / "a.jpg", long_edge=8, seek=plan)
    preprocess.extract_frame_index(
        Path("in.mp4"), 0, 10, tmp_path / "b.jpg", long_edge=8, seek=plan
    )
    assert all("-copyts" not in argv for argv in seen) and len(seen) == 2
