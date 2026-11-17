"""D62 B1·B2·B4 — 원본 전체 미리보기의 길이 비례 한도·검토용 크기·낮은 OS 우선순위.

미리보기는 검토 보조다(Final Cut은 원본을 쓴다). 4K를 그대로 다시 인코딩하던 것을 긴 변 1920
이하·빠른 preset으로 줄이되, 크기 필터는 시각을 바꾸지 않는다 — 브라우저 원점 측정이 그대로다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw.analysis_config import (
    REVIEW_MEDIA,
    ReviewMediaSettings,
    validate_review_media_settings,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


@pytest.fixture(autouse=True)
def progress_off() -> Any:
    preprocess.disable_process_progress()
    yield
    preprocess.disable_process_progress()


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 미디어 생성 실패: {completed.stderr[-400:]}"


def _ten_bit(path: Path, size: str, *, lead_audio: bool = False) -> None:
    """브라우저가 그대로 못 트는 10bit H.264 — 미리보기는 다시 인코딩한다(DJI HEVC 10bit의 자리)."""
    video = ["-itsoffset", "0.6"] if lead_audio else []
    _encode(
        [
            *video, "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30000/1001:duration=3",
            "-f", "lavfi", "-i", "sine=duration=4",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "libx264", "-profile:v", "high10", "-pix_fmt", "yuv420p10le",
            "-c:a", "aac", "-video_track_timescale", "30000",
            str(path),
        ]
    )  # fmt: skip


def _build(
    source: Path, out: Path, encoding: preprocess.PreviewEncoding | None
) -> preprocess.VisualPreviewExtraction:
    probe = preprocess.probe_raw_media(source)
    track = probe.audio[0].stream_index if probe.audio else None
    return preprocess.extract_visual_review_preview(
        source, out, audio_stream_index=track, encoding=encoding
    )


# ── 설정 — 값은 설정에서, 캐시 열쇠 밖 ──────────────────────────────────────────


def test_the_preview_limit_grows_with_the_source_and_keeps_the_old_floor() -> None:
    assert REVIEW_MEDIA.preview_timeout(None) == 1800.0
    assert REVIEW_MEDIA.preview_timeout(35.0) == 1800.0  # 짧은 원본은 예전 그대로
    assert REVIEW_MEDIA.preview_timeout(1359.0) == 8154.0  # 0018 — 고정 1800초에 걸렸다
    assert REVIEW_MEDIA.preview_timeout(float("nan")) == 1800.0


@pytest.mark.parametrize(
    "broken",
    [
        ReviewMediaSettings(preview_long_edge=1921),
        ReviewMediaSettings(preview_long_edge=0),
        ReviewMediaSettings(preview_x264_crf=52),
        ReviewMediaSettings(process_niceness=21),
        ReviewMediaSettings(preview_x264_preset="very fast"),
        ReviewMediaSettings(preview_timeout_floor_seconds=0.0),
        ReviewMediaSettings(thumb_seek_margin_seconds=float("inf")),
    ],
)
def test_broken_review_media_settings_are_refused(broken: ReviewMediaSettings) -> None:
    with pytest.raises(ValueError, match="검토 보조 미디어 설정"):
        validate_review_media_settings(broken)


def test_the_cache_recipes_did_not_move() -> None:
    """이미 만든 미리보기 33GB와 썸네일을 그대로 쓴다 — 조리법 판(열쇠 재료)은 그대로다."""
    from vlogsrt.project.raw import visual_review

    assert visual_review.VISUAL_PREVIEW_RECIPE == "visual-review-preview/v1"
    assert visual_review.VISUAL_THUMB_RECIPE == "visual-review-thumb/v1"
    assert visual_review.preview_media_key("a" * 64, 0) == visual_review.preview_media_key(
        "a" * 64, 0
    )
    assert len(visual_review.thumbnail_media_key("a" * 64, 7)) == 24


# ── 크기 — 긴 변만, 키우지 않고, 짝수로, 방향 그대로 ────────────────────────────


@needs_ffmpeg
@pytest.mark.parametrize(
    ("size", "edge", "expected"),
    [
        ("640x360", 320, (320, 180)),  # 줄인다
        ("200x112", 320, (200, 112)),  # 키우지 않는다
        ("360x640", 320, (180, 320)),  # 세로 — 긴 변이 높이
    ],
)
def test_the_reencoded_preview_fits_the_long_edge(
    tmp_path: Path, size: str, edge: int, expected: tuple[int, int]
) -> None:
    source = tmp_path / "in.mp4"
    _ten_bit(source, size)
    encoding = preprocess.PreviewEncoding(long_edge=edge, preset="veryfast", crf=23)
    built = _build(source, tmp_path / "out.mp4", encoding)
    assert (built.width, built.height) == expected
    assert built.video_codec == "h264" and built.encoding == "libx264/veryfast/crf23/le" + str(edge)


@needs_ffmpeg
def test_a_rotated_source_keeps_its_display_orientation(tmp_path: Path) -> None:
    plain = tmp_path / "plain.mp4"
    _ten_bit(plain, "640x360")
    rotated = tmp_path / "rotated.mov"
    _encode(["-display_rotation", "90", "-i", str(plain), "-c", "copy", str(rotated)])
    assert preprocess.probe_raw_media(rotated).video.rotation_degrees == 90
    encoding = preprocess.PreviewEncoding(long_edge=320, preset="veryfast", crf=23)
    built = _build(rotated, tmp_path / "out.mp4", encoding)
    # 회전을 물리로 적용한 뒤 줄였다 — 세로 화면, 회전 선언 없음(옛 조리법과 같은 방향)
    assert (built.width, built.height, built.rotation_degrees) == (180, 320, 0)
    old = _build(rotated, tmp_path / "old.mp4", None)
    assert (old.width, old.height, old.rotation_degrees) == (360, 640, 0)


@needs_ffmpeg
def test_scaling_does_not_move_a_single_timestamp(tmp_path: Path) -> None:
    """원점이 0이 아닌(소리가 먼저) 원본 — 줄인 미리보기와 옛 크기 그대로 미리보기의 시각이 같다."""
    source = tmp_path / "lead.mp4"
    _ten_bit(source, "640x360", lead_audio=True)
    encoding = preprocess.PreviewEncoding(long_edge=320, preset="veryfast", crf=23)
    small = _build(source, tmp_path / "small.mp4", encoding)
    full = _build(source, tmp_path / "full.mp4", None)
    assert (small.width, full.width) == (320, 640)
    assert small.video_origin == full.video_origin and small.video_origin > 0
    assert small.audio_origin == full.audio_origin
    assert small.duration == full.duration
    clocks = [preprocess.probe_extraction_clock(path) for path in (small.path, full.path)]
    assert clocks[0].first_pts * clocks[0].time_base == clocks[1].first_pts * clocks[1].time_base
    assert (clocks[0].frame_count, clocks[0].frame_duration) == (
        clocks[1].frame_count,
        clocks[1].frame_duration,
    )
    assert clocks[0].uniform_grid and clocks[0].frame_count == 90
    assert clocks[0].frame_duration == Fraction(1001, 30000)


@needs_ffmpeg
def test_a_browser_friendly_source_is_still_copied_untouched(tmp_path: Path) -> None:
    source = tmp_path / "plain.mp4"
    _encode(
        [
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25:duration=1",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", str(source),
        ]
    )  # fmt: skip
    encoding = preprocess.PreviewEncoding(long_edge=320, preset="veryfast", crf=23)
    built = _build(source, tmp_path / "out.mp4", encoding)
    assert built.remuxed and (built.width, built.height) == (640, 360) and built.encoding is None


# ── 한도·우선순위가 ffmpeg 호출까지 간다 ─────────────────────────────────────────


def test_the_preview_passes_its_limit_and_priority_to_the_runner(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: dict[str, Any] = {}

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs, argv=command)
        raise RuntimeError("멈춤")

    probe = _fake_probe()
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path, **_: probe)
    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    encoding = preprocess.PreviewEncoding(long_edge=1920, preset="veryfast", crf=23)
    with pytest.raises(RuntimeError, match="멈춤"):
        preprocess.extract_visual_review_preview(
            tmp_path / "in.mov",
            tmp_path / "out.mp4",
            audio_stream_index=None,
            encoding=encoding,
            timeout_seconds=8154.0,
            priority=10,
        )
    assert seen["timeout"] == 8154.0 and seen["priority"] == 10
    argv = seen["argv"]
    assert "-ss" not in argv and "-copyts" not in argv  # 원본 시각 그대로 흐른다
    assert argv[argv.index("-preset") + 1] == "veryfast" and argv[argv.index("-crf") + 1] == "23"
    assert argv[argv.index("-vf") + 1] == encoding.scale_filter()
    assert not list(tmp_path.glob(".*.part"))  # 실패한 스테이징은 남지 않는다


def _fake_probe() -> Any:
    from types import SimpleNamespace

    video = SimpleNamespace(stream_index=0, codec_name="hevc", pix_fmt="yuv420p10le")
    return SimpleNamespace(video=video, audio=())


# ── B4 — nice는 띄운 바로 뒤에, 등록표·진행 파일은 그대로 ─────────────────────────


_REPORT_NICE = "import os, time; time.sleep(0.4); print(os.getpriority(os.PRIO_PROCESS, 0))"
_REPORT_CLASS = (
    "import ctypes, time; time.sleep(0.4); k = ctypes.windll.kernel32;"
    " print(k.GetPriorityClass(k.GetCurrentProcess()))"
)
_BELOW_NORMAL_CLASS = 0x4000
"""Windows에는 nice가 없다 — 「보통보다 낮음」 등급이 같은 양보다(`preprocess._launch`, D100 §1)."""


def _own_priority() -> int:
    """이 프로세스의 우선순위 — POSIX는 nice, Windows는 우선순위 등급."""
    if sys.platform == "win32":
        import ctypes

        kernel = ctypes.windll.kernel32  # type: ignore[attr-defined]
        return int(kernel.GetPriorityClass(kernel.GetCurrentProcess()))
    return os.getpriority(os.PRIO_PROCESS, 0)


def _child_priority(**options: Any) -> int:
    report = _REPORT_CLASS if sys.platform == "win32" else _REPORT_NICE
    completed = preprocess.run_process(
        [sys.executable, "-c", report], capture_output=True, text=True, **options
    )
    assert completed.returncode == 0, completed.stderr
    return int(completed.stdout.strip())


def test_a_prioritized_process_really_runs_at_the_lower_priority() -> None:
    if sys.platform == "win32":
        expected = _BELOW_NORMAL_CLASS
    else:
        expected = max(_own_priority(), 10)
    assert _child_priority(priority=10) == expected


def test_without_priority_the_fast_path_and_the_niceness_are_unchanged() -> None:
    assert _child_priority() == _own_priority()


def test_priority_takes_the_popen_path_and_keeps_registry_and_progress(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    preprocess.enable_process_progress(tmp_path / "progress")
    calls: list[tuple[int, int]] = []
    launched: list[list[str]] = []
    real_popen = subprocess.Popen

    def popen(argv: list[str], **kwargs: Any) -> subprocess.Popen[Any]:
        launched.append(list(argv))
        assert preprocess.active_processes()  # 띄우는 동안 이미 등록표에 있다
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(preprocess.subprocess, "Popen", popen)
    monkeypatch.setattr(sys, "platform", "darwin")  # nice 길 — Windows는 등급 플래그다(위 시험)
    monkeypatch.setattr(preprocess, "_lower_priority", lambda pid, nice: calls.append((pid, nice)))
    monkeypatch.setattr(
        preprocess.subprocess, "run", lambda *a, **k: pytest.fail("nice는 Popen 길이다")
    )
    argv = ["ffmpeg", "-hide_banner", "-i", "/media/in.mov", "/out/x.jpg"]
    preprocess.run_process(argv, capture_output=True, text=True, priority=10, step="썸네일 추출")
    progress = launched[0][2]
    assert launched[0][1] == "-progress" and progress.startswith(
        f"file:{tmp_path / 'progress'}{os.sep}"
    )
    assert launched[0][3:] == argv[1:]  # 진행 파일 말고는 받은 argv 그대로
    assert len(calls) == 1 and calls[0][1] == 10
    assert preprocess.active_processes() == ()  # 끝나면 내려간다


def test_lowering_a_finished_child_is_quiet(monkeypatch: pytest.MonkeyPatch) -> None:
    def gone(*_args: object) -> int:
        raise ProcessLookupError

    monkeypatch.setattr(preprocess.os, "getpriority", gone, raising=False)
    monkeypatch.setattr(preprocess.os, "setpriority", lambda *_: None, raising=False)
    monkeypatch.setattr(preprocess.os, "PRIO_PROCESS", 0, raising=False)
    preprocess._lower_priority(999_999, 10)  # 예외가 새지 않는다


def test_lowering_never_raises_the_priority(monkeypatch: pytest.MonkeyPatch) -> None:
    set_calls: list[int] = []
    monkeypatch.setattr(preprocess.os, "getpriority", lambda *_: 15, raising=False)
    monkeypatch.setattr(
        preprocess.os, "setpriority", lambda *a: set_calls.append(a[2]), raising=False
    )
    monkeypatch.setattr(preprocess.os, "PRIO_PROCESS", 0, raising=False)
    preprocess._lower_priority(1234, 10)
    assert set_calls == []  # 이미 더 낮다 — 높이지 않는다
