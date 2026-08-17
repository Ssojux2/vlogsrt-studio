"""전처리 — ffmpeg 경계. 순수 로직은 항상 돌고, 실제 호출만 건너뛴다."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.preprocess import (
    amplitudes,
    cut_spans,
    extract_audio,
    find_quiet_point,
    make_chunks,
    probe_duration,
    split_at,
)


def _fake_run(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    commands: list[list[str]] = []

    def run(command: list[str]) -> object:
        commands.append(command)
        target = Path(command[-1])
        if "%" not in target.name:  # 세그먼트 패턴이 아니면 실제 출력 파일이다
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"\x00")

        class Done:
            stdout = ""
            stderr = ""

        return Done()

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "ffmpeg")
    return commands


def test_extraction_applies_no_audio_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D20 — 오디오는 손대지 않고 뽑는다. 필터는 전부 제거됐다."""
    commands = _fake_run(monkeypatch)
    extract_audio(tmp_path / "in.mp4", tmp_path / "out.mp3", Config())

    (command,) = commands
    assert "-af" not in command
    assert command[command.index("-ar") + 1] == "16000"
    assert command[command.index("-ac") + 1] == "1"


# ─────────────────────────────────────────────────────────────
#  조각 나누기
# ─────────────────────────────────────────────────────────────


def test_audio_under_the_limit_is_never_split(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조각이 하나면 자르지 않는다 — 원본을 그대로 보낸다."""
    commands = _fake_run(monkeypatch)
    audio = tmp_path / "demo.mp3"
    audio.write_bytes(b"\x00" * 1000)

    chunks = make_chunks(audio, 120.0, Config(), tmp_path / "cuts")
    assert len(chunks) == 1
    assert chunks[0].path == audio
    assert chunks[0].offset == 0.0
    assert commands == []  # ffmpeg를 부르지도 않았다


def test_the_split_asks_ffmpeg_once_with_all_cut_times(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-ss`로 조각마다 탐색하면 매번 처음부터 디코딩한다. 한 번에 끝낸다."""
    commands = _fake_run(monkeypatch)
    listing = tmp_path / "cuts" / "demo.segments.csv"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(
        "demo.chunk0000.mp3,0.000000,297.384000\ndemo.chunk0001.mp3,297.384000,600.000000\n",
        encoding="utf-8",
    )
    split_at(tmp_path / "demo.mp3", [297.4], tmp_path / "cuts")

    (command,) = commands
    assert command[command.index("-f") + 1] == "segment"
    assert command[command.index("-segment_times") + 1] == "297.400000"
    assert command[command.index("-c") + 1] == "copy"  # 재인코딩 없음


def test_offsets_come_from_ffmpeg_not_from_what_we_asked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """요청 시각과 실제로 잘린 시각은 mp3 프레임만큼 다르다. 실제값을 써야 안 밀린다(D13)."""
    _fake_run(monkeypatch)
    listing = tmp_path / "cuts" / "demo.segments.csv"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(
        "demo.chunk0000.mp3,0.000000,297.384000\ndemo.chunk0001.mp3,297.384000,600.000000\n",
        encoding="utf-8",
    )
    chunks = split_at(tmp_path / "demo.mp3", [297.4], tmp_path / "cuts")

    assert [chunk.offset for chunk in chunks] == [0.0, 297.384]
    assert [chunk.index for chunk in chunks] == [0, 1]
    assert chunks[0].duration == pytest.approx(297.384)


def test_chunks_cover_the_audio_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """규칙 4 — 조각을 이으면 오디오 전체다. 사라지는 구간이 없다."""
    _fake_run(monkeypatch)
    listing = tmp_path / "cuts" / "demo.segments.csv"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text(
        "demo.chunk0000.mp3,0.000000,200.0\n"
        "demo.chunk0001.mp3,200.0,400.0\n"
        "demo.chunk0002.mp3,400.0,600.0\n",
        encoding="utf-8",
    )
    chunks = split_at(tmp_path / "demo.mp3", [200.0, 400.0], tmp_path / "cuts")

    assert chunks[0].offset == 0.0
    assert sum(chunk.duration for chunk in chunks) == pytest.approx(600.0)
    for earlier, later in zip(chunks, chunks[1:], strict=False):
        assert earlier.offset + earlier.duration == pytest.approx(later.offset)


def test_an_empty_segment_list_is_loud(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_run(monkeypatch)
    listing = tmp_path / "cuts" / "demo.segments.csv"
    listing.parent.mkdir(parents=True, exist_ok=True)
    listing.write_text("", encoding="utf-8")
    with pytest.raises(RuntimeError, match="조각 목록"):
        split_at(tmp_path / "demo.mp3", [200.0], tmp_path / "cuts")


def test_the_search_window_never_reaches_outside_the_audio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """목표가 오디오 끝 가까이면 창이 밖으로 나가면 안 된다."""
    asked: list[tuple[float, float]] = []
    monkeypatch.setattr(
        preprocess,
        "amplitudes",
        lambda path, start, length, cfg: asked.append((start, length)) or [0.5],
    )
    cfg = Config()
    find_quiet_point(tmp_path / "demo.mp3", 2.0, 600.0, cfg)
    find_quiet_point(tmp_path / "demo.mp3", 598.0, 600.0, cfg)

    for start, length in asked:
        assert start >= 0.0
        assert start + length <= 600.0


# ─────────────────────────────────────────────────────────────
#  실제 ffmpeg
# ─────────────────────────────────────────────────────────────


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


@pytest.fixture
def tone(tmp_path: Path) -> Path:
    """톤 2초 + 무음 3초 + 톤 2초. 가운데 무음이 자를 자리다."""
    import subprocess

    path = tmp_path / "tone.wav"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-nostdin", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:duration=3",
            "-f", "lavfi", "-i", "sine=frequency=660:duration=2",
            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1",
            str(path),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return path


@needs_ffmpeg
def test_probe_duration_real(tone: Path) -> None:
    assert probe_duration(tone) == pytest.approx(7.0, abs=0.2)


@needs_ffmpeg
def test_extract_audio_real(tone: Path, tmp_path: Path) -> None:
    destination = extract_audio(tone, tmp_path / "out.mp3", Config())
    assert destination.is_file()
    assert destination.stat().st_size > 0


@needs_ffmpeg
def test_amplitudes_see_the_silence(tone: Path) -> None:
    levels = amplitudes(tone, 0.0, 7.0, Config())
    assert levels
    middle = levels[len(levels) // 2]
    assert middle < max(levels) / 10  # 가운데 3초는 무음이다


@needs_ffmpeg
def test_the_cut_lands_in_the_silence(tone: Path, tmp_path: Path) -> None:
    """목표가 톤 한복판이어도 근처 무음으로 옮겨간다 — 말이 잘리지 않게."""
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    cut = find_quiet_point(audio, 2.2, 7.0, Config())
    assert 2.0 <= cut <= 5.0  # 무음 구간(2~5초) 안


@needs_ffmpeg
def test_split_at_real(tone: Path, tmp_path: Path) -> None:
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    chunks = split_at(audio, [3.5], tmp_path / "cuts")
    assert len(chunks) == 2
    assert all(chunk.path.is_file() for chunk in chunks)
    assert chunks[0].offset == 0.0
    assert chunks[1].offset == pytest.approx(3.5, abs=0.2)
    total = sum(probe_duration(chunk.path) for chunk in chunks)
    assert total == pytest.approx(probe_duration(audio), abs=0.3)


@needs_ffmpeg
def test_cut_spans_pulls_out_just_the_asked_range(tone: Path, tmp_path: Path) -> None:
    """되풀이 구간만 다시 잘라낸다. 오프셋은 ffmpeg가 알려준 실제값이다(D27)."""
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    duration = probe_duration(audio)
    chunks = cut_spans(audio, [(2.0, 5.0)], duration, tmp_path / "work")
    assert len(chunks) == 1
    assert chunks[0].path.is_file()
    assert chunks[0].offset == pytest.approx(2.0, abs=0.2)
    assert probe_duration(chunks[0].path) == pytest.approx(3.0, abs=0.3)


@needs_ffmpeg
def test_a_span_covering_everything_does_not_kill_ffmpeg(tone: Path, tmp_path: Path) -> None:
    """**되풀이가 오디오 전체일 때의 회귀 테스트.**

    자를 자리가 하나도 없으면 빈 `-segment_times`가 넘어가 ffmpeg가
    "Empty time specification"으로 죽었다. 그러면 그 실행의 되풀이가 전부 접혔다 —
    한 구간의 경계 조건이 다른 구간의 자막까지 데려가는 셈이다(D27).
    """
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    duration = probe_duration(audio)
    chunks = cut_spans(audio, [(0.0, duration)], duration, tmp_path / "work")
    assert len(chunks) == 1
    assert chunks[0].offset == 0.0
    assert chunks[0].path == audio  # 자를 자리가 없으면 원본이 곧 그 조각이다


@needs_ffmpeg
def test_cut_spans_returns_one_chunk_per_span(tone: Path, tmp_path: Path) -> None:
    """계획과 조각은 순서로 짝지어진다. 수가 어긋나면 남의 자막이 그 자리에 선다."""
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    duration = probe_duration(audio)
    spans = [(0.5, 2.0), (3.0, 4.5), (5.0, 6.5)]
    chunks = cut_spans(audio, spans, duration, tmp_path / "work")
    assert len(chunks) == len(spans)
    for chunk, (low, _high) in zip(chunks, spans, strict=True):
        assert chunk.offset == pytest.approx(low, abs=0.2)


@needs_ffmpeg
def test_touching_spans_stay_separate_chunks(tone: Path, tmp_path: Path) -> None:
    """**긴 되풀이를 쪼갠 계획들의 회귀 테스트**(D28).

    쪼갠 계획은 경계를 맞대고 있다 — 앞 계획의 끝이 곧 뒤 계획의 시작이다. 그 자리를
    한 번만 자르고 조각을 둘로 세지 못하면, 계획 수와 조각 수가 어긋나 그 실행의
    재전사가 통째로 취소된다.
    """
    audio = extract_audio(tone, tmp_path / "full.mp3", Config())
    duration = probe_duration(audio)
    spans = [(1.0, 3.0), (3.0, 5.0)]
    chunks = cut_spans(audio, spans, duration, tmp_path / "work")
    assert len(chunks) == len(spans)
    assert chunks[0].offset == pytest.approx(1.0, abs=0.2)
    assert chunks[1].offset == pytest.approx(3.0, abs=0.2)
