"""전처리 — 순수 로직은 항상 돌고, 실제 ffmpeg 호출만 건너뛴다."""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.preprocess import (
    chunk_count,
    extract_audio,
    plan_chunks,
    probe_duration,
    verify_coverage,
)

MB = 1_000_000


def _chunks(plans: list[preprocess.ChunkPlan]) -> list[Chunk]:
    return [
        Chunk(path=Path(f"chunk{p.index}.mp3"), offset=p.start, duration=p.duration, index=p.index)
        for p in plans
    ]


def test_a_short_chunk_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """조각이 계획보다 짧으면 그 길이만큼의 소리가 전사에 오르지 못한다(D23)."""
    plans = plan_chunks(100.0, 50 * MB, Config())
    monkeypatch.setattr(preprocess, "probe_duration", lambda path: 10.0)
    with pytest.raises(RuntimeError, match="계획보다 짧습니다"):
        verify_coverage(_chunks(plans), plans, Config())


def test_encoder_padding_is_within_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """mp3 패딩은 조각을 늘리지 줄이지 않는다. 오차 허용치 안이면 통과한다."""
    plans = plan_chunks(100.0, 50 * MB, Config())
    cfg = Config()
    short_but_ok = plans[0].duration - cfg.chunk_duration_tolerance / 2
    monkeypatch.setattr(preprocess, "probe_duration", lambda path: short_but_ok)
    verify_coverage(_chunks(plans), plans, cfg)


def test_extraction_applies_no_audio_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D20 — 오디오는 손대지 않고 뽑는다. 필터는 전부 제거됐다."""
    from vlogsrt import preprocess

    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> object:
        commands.append(command)
        (tmp_path / "out.mp3").write_bytes(b"\x00")

        class Done:
            stdout = ""
            stderr = ""

        return Done()

    monkeypatch.setattr(preprocess, "_run", fake_run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffmpeg")
    extract_audio(tmp_path / "in.mp4", tmp_path / "out.mp3", Config())

    (command,) = commands
    assert "-af" not in command
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-ac") + 1] == "1"


def test_short_audio_stays_whole() -> None:
    cfg = Config()
    plans = plan_chunks(300.0, 10 * MB, cfg)
    assert len(plans) == 1
    assert (plans[0].start, plans[0].end) == (0.0, 300.0)


def test_size_limit_splits_evenly() -> None:
    """D21 — 무음을 찾지 않는다. 한도를 넘으면 균등하게 자른다."""
    cfg = Config()
    size = 3 * cfg.chunk_max_bytes
    plans = plan_chunks(300.0, size, cfg)
    assert len(plans) == math.ceil(size / cfg.chunk_max_bytes)
    assert [plan.end for plan in plans] == pytest.approx([100.0, 200.0, 300.0])


def test_offsets_are_monotonic_and_cover_everything() -> None:
    cfg = Config()
    plans = plan_chunks(600.0, 5 * cfg.chunk_max_bytes, cfg)
    assert plans[0].start == 0.0
    assert plans[-1].end == pytest.approx(600.0)
    for previous, following in zip(plans, plans[1:], strict=False):
        assert previous.end == pytest.approx(following.start)


def test_zero_duration_is_loud() -> None:
    with pytest.raises(RuntimeError):
        plan_chunks(0.0, 1000, Config())


def test_long_audio_is_split_even_when_it_fits_the_size_limit() -> None:
    """길이 한도는 크기 한도와 독립이다. 77분은 크기로 한 조각이어도 더 나뉜다."""
    cfg = Config()
    duration = 4598.0
    tiny = 1000  # 크기 한도는 전혀 건드리지 않는다
    expected = math.ceil(duration / cfg.chunk_max_seconds)
    assert chunk_count(duration, tiny, cfg) == expected

    plans = plan_chunks(duration, tiny, cfg)
    assert len(plans) == expected
    assert all(plan.duration <= cfg.chunk_max_seconds for plan in plans)
    assert plans[0].start == 0.0
    assert plans[-1].end == pytest.approx(duration)


def test_size_limit_still_wins_when_it_demands_more_pieces() -> None:
    cfg = Config()
    duration = 300.0  # 길이로는 한 조각
    size = 5 * cfg.chunk_max_bytes
    assert chunk_count(duration, size, cfg) == 5
    assert len(plan_chunks(duration, size, cfg)) == 5


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
