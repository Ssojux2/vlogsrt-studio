"""전처리 — 순수 로직은 항상 돌고, 실제 ffmpeg 호출만 건너뛴다."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from vlogsrt.config import Config
from vlogsrt.preprocess import (
    audio_filter,
    chunk_count,
    detect_silence,
    extract_audio,
    parse_silence_output,
    plan_chunks,
    probe_duration,
)

SILENCE_LOG = """
[silencedetect @ 0x1] silence_start: 3.5
[silencedetect @ 0x1] silence_end: 5.25 | silence_duration: 1.75
[silencedetect @ 0x1] silence_start: 40.0
[silencedetect @ 0x1] silence_end: 41.2 | silence_duration: 1.2
"""

MB = 1_000_000


def test_silence_comes_from_stderr_format() -> None:
    assert parse_silence_output(SILENCE_LOG, 60.0) == [(3.5, 5.25), (40.0, 41.2)]


def test_unclosed_silence_runs_to_the_end() -> None:
    log = "[silencedetect @ 0x1] silence_start: 55.0\n"
    assert parse_silence_output(log, 60.0) == [(55.0, 60.0)]


def test_no_silence_lines() -> None:
    assert parse_silence_output("nothing here", 60.0) == []


def test_loudnorm_filter_is_present() -> None:
    assert audio_filter(Config()) == "loudnorm=I=-18:TP=-2:LRA=11"


def test_chunking() -> None:
    """T1.2 DoD가 이름으로 부르는 테스트다."""
    cfg = Config()
    plans = plan_chunks(300.0, 10 * MB, [], cfg)
    assert len(plans) == 1
    assert plans[0].start == 0.0
    assert plans[0].end == 300.0

    size = 3 * cfg.chunk_max_bytes
    silences = [(95.0, 105.0), (195.0, 205.0)]
    plans = plan_chunks(300.0, size, silences, cfg)
    assert len(plans) == math.ceil(size / cfg.chunk_max_bytes)
    assert [plan.start for plan in plans] == sorted(plan.start for plan in plans)
    assert sum(plan.duration for plan in plans) == pytest.approx(300.0, abs=0.1)
    assert plans[0].end == pytest.approx(100.0)
    assert plans[1].end == pytest.approx(200.0)
    assert not any(plan.forced for plan in plans)


def test_forced_split_when_no_silence_nearby() -> None:
    cfg = Config()
    plans = plan_chunks(300.0, 2 * cfg.chunk_max_bytes, [], cfg)
    assert len(plans) == 2
    assert plans[0].end == pytest.approx(150.0)
    assert plans[0].forced is True


def test_silence_outside_the_window_is_ignored() -> None:
    cfg = Config()
    # 목표는 150초, 탐색 창은 ±25%(±37.5초). 10초짜리 무음은 창 밖이다.
    plans = plan_chunks(300.0, 2 * cfg.chunk_max_bytes, [(8.0, 12.0)], cfg)
    assert plans[0].end == pytest.approx(150.0)
    assert plans[0].forced is True


def test_offsets_are_monotonic_and_cover_everything() -> None:
    cfg = Config()
    plans = plan_chunks(600.0, 5 * cfg.chunk_max_bytes, [(120.0, 124.0)], cfg)
    assert plans[0].start == 0.0
    assert plans[-1].end == 600.0
    for previous, following in zip(plans, plans[1:], strict=False):
        assert previous.end == following.start


def test_zero_duration_is_loud() -> None:
    with pytest.raises(RuntimeError):
        plan_chunks(0.0, 1000, [], Config())


# ─────────────────────────────────────────────────────────────
#  길이 한도 — 25MB에 38분이 들어가서 whisper가 무너졌다 (D13)
# ─────────────────────────────────────────────────────────────


def test_long_audio_is_split_even_when_it_fits_the_size_limit() -> None:
    """77분은 크기로는 두 조각이지만, 길이 한도가 더 잘게 나눈다."""
    cfg = Config()
    duration = 4598.0
    tiny = 1000  # 크기 한도는 전혀 건드리지 않는다
    assert chunk_count(duration, tiny, cfg) == math.ceil(duration / cfg.chunk_max_seconds)

    plans = plan_chunks(duration, tiny, [], cfg)
    assert len(plans) == chunk_count(duration, tiny, cfg)
    assert all(plan.duration <= cfg.chunk_max_seconds * 1.3 for plan in plans)
    assert plans[0].start == 0.0
    assert plans[-1].end == pytest.approx(duration)


def test_size_limit_still_wins_when_it_demands_more_pieces() -> None:
    cfg = Config()
    duration = 300.0  # 길이로는 한 조각
    size = 5 * cfg.chunk_max_bytes
    assert chunk_count(duration, size, cfg) == 5
    assert len(plan_chunks(duration, size, [], cfg)) == 5


def test_short_audio_stays_whole() -> None:
    cfg = Config()
    assert chunk_count(cfg.chunk_max_seconds - 1, 1000, cfg) == 1
    assert len(plan_chunks(cfg.chunk_max_seconds - 1, 1000, [], cfg)) == 1


def test_chunk_count_never_returns_zero() -> None:
    assert chunk_count(0.5, 0, Config()) == 1


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


@pytest.fixture
def tone(tmp_path: Path) -> Path:
    """말소리 대신 톤과 무음을 이어 붙인 합성 오디오. 골든셋 영상 없이도 돈다."""
    import subprocess

    path = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono",
            "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1,atrim=0:5",
            str(path),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return path


@needs_ffmpeg
def test_probe_duration_real(tone: Path) -> None:
    assert probe_duration(tone) == pytest.approx(5.0, abs=0.2)


@needs_ffmpeg
def test_extract_audio_real(tone: Path, tmp_path: Path) -> None:
    destination = extract_audio(tone, tmp_path / "out.mp3", Config())
    assert destination.is_file()
    assert destination.stat().st_size > 0


@needs_ffmpeg
def test_detect_silence_real(tone: Path) -> None:
    silences = detect_silence(tone, Config())
    assert any(start >= 1.5 for start, _ in silences)
