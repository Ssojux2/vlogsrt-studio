"""전처리 — ffmpeg 경계. 순수 로직은 항상 돌고, 실제 호출만 건너뛴다."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.models import Chunk
from vlogsrt.preprocess import (
    cut_batches,
    detect_silence,
    extract_audio,
    probe_duration,
    warn_about_short_cuts,
)
from vlogsrt.spans import Batch


def test_extraction_applies_no_audio_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D20 — 오디오는 손대지 않고 뽑는다. 필터는 전부 제거됐다."""
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


# ─────────────────────────────────────────────────────────────
#  묶음 조각 만들기
# ─────────────────────────────────────────────────────────────


def _fake_ffmpeg(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[list[str]]:
    commands: list[list[str]] = []

    def fake_run(command: list[str]) -> object:
        commands.append(command)
        Path(command[-1]).write_bytes(b"\x00")

        class Done:
            stdout = ""
            stderr = ""

        return Done()

    monkeypatch.setattr(preprocess, "_run", fake_run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffmpeg")
    monkeypatch.setattr(preprocess, "probe_duration", lambda path: 20.0)
    return commands


BATCHES = [
    Batch(start=0.0, end=10.0, index=0, spans=((0.5, 4.0), (5.0, 9.5))),
    Batch(start=10.0, end=20.0, index=1, spans=((11.0, 14.0),)),
]


def test_each_batch_becomes_its_own_audio_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    commands = _fake_ffmpeg(monkeypatch, tmp_path)
    chunks = cut_batches(tmp_path / "audio.mp3", BATCHES, tmp_path, Config())
    assert len(chunks) == 2
    assert len({chunk.path for chunk in chunks}) == 2
    assert len(commands) == 2


def test_the_cut_carries_the_spans_into_the_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """규칙 1 — 조각이 자기 발화 구간을 들고 다녀야 배분이 시각을 갖는다."""
    _fake_ffmpeg(monkeypatch, tmp_path)
    chunks = cut_batches(tmp_path / "audio.mp3", BATCHES, tmp_path, Config())
    assert chunks[0].spans == ((0.5, 4.0), (5.0, 9.5))
    assert chunks[1].spans == ((11.0, 14.0),)


def test_the_offset_is_the_batch_start(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """offset이 틀리면 자막 전체가 밀린다(D13)."""
    _fake_ffmpeg(monkeypatch, tmp_path)
    chunks = cut_batches(tmp_path / "audio.mp3", BATCHES, tmp_path, Config())
    assert [chunk.offset for chunk in chunks] == [0.0, 10.0]


def test_seeking_happens_after_the_input(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """-ss가 -i 앞에 오면 탐색이 부정확해지고 offset이 어긋난다(D13)."""
    commands = _fake_ffmpeg(monkeypatch, tmp_path)
    cut_batches(tmp_path / "audio.mp3", BATCHES, tmp_path, Config())
    for command in commands:
        assert command.index("-i") < command.index("-ss")
        assert command.index("-ss") < command.index("-to")


def test_no_batches_means_no_ffmpeg_call(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    commands = _fake_ffmpeg(monkeypatch, tmp_path)
    assert cut_batches(tmp_path / "audio.mp3", [], tmp_path, Config()) == []
    assert commands == []


# ─────────────────────────────────────────────────────────────
#  손실은 멈추지 않고 경고한다 (규칙 9)
# ─────────────────────────────────────────────────────────────


def _chunks_of(batches: list[Batch]) -> list[Chunk]:
    return [
        Chunk(path=Path(f"b{b.index}.mp3"), offset=b.start, duration=b.duration, index=b.index)
        for b in batches
    ]


def test_a_short_cut_is_reported_but_does_not_stop_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preprocess, "probe_duration", lambda path: 1.0)
    complaints = warn_about_short_cuts(_chunks_of(BATCHES), BATCHES, Config())
    assert len(complaints) == 2
    assert "0" in complaints[0]


def test_encoder_padding_is_within_tolerance(monkeypatch: pytest.MonkeyPatch) -> None:
    """mp3 패딩은 조각을 늘리지 줄이지 않는다. 오차 허용치 안이면 조용하다."""
    cfg = Config()
    monkeypatch.setattr(
        preprocess, "probe_duration", lambda path: 10.0 - cfg.chunk_duration_tolerance / 2
    )
    assert warn_about_short_cuts(_chunks_of(BATCHES), BATCHES, cfg) == []


# ─────────────────────────────────────────────────────────────
#  실제 ffmpeg
# ─────────────────────────────────────────────────────────────


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
    """톤 2초 + 무음 3초. 뒤쪽 무음이 잡혀야 한다."""
    silences = detect_silence(tone, Config())
    assert silences
    assert silences[-1][0] == pytest.approx(2.0, abs=0.3)


@needs_ffmpeg
def test_cut_batches_real(tone: Path, tmp_path: Path) -> None:
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    batch = Batch(start=0.0, end=2.0, index=0, spans=((0.0, 2.0),))
    (chunk,) = cut_batches(audio, [batch], tmp_path / "cuts", Config())
    assert chunk.path.is_file()
    assert probe_duration(chunk.path) == pytest.approx(2.0, abs=0.3)
