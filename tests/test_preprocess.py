"""전처리 — ffmpeg 경계. 순수 로직은 항상 돌고, 실제 호출만 건너뛴다."""

from __future__ import annotations

import array
import json
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.preprocess import (
    amplitudes,
    cut_spans,
    extract_audio,
    extract_pcm_s16le,
    find_quiet_point,
    make_chunks,
    probe_audio_stream,
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


# ─────────────────────────────────────────────────────────────
#  원본 PCM 추출 — 잰 시계의 원점 (P2-01)
# ─────────────────────────────────────────────────────────────


class _Completed:
    """`_run`이 돌려주는 것과 같은 모양. stdout·stderr만 있으면 충분하다."""

    def __init__(self, stdout: str, stderr: str) -> None:
        self.stdout = stdout
        self.stderr = stderr


def _fake_ffmpeg_ffprobe(
    monkeypatch: pytest.MonkeyPatch, probe_json: str, ashowinfo: str, pcm_bytes: int
) -> list[list[str]]:
    """ffprobe(JSON)와 ffmpeg(ashowinfo) 호출을 흉내 낸다. 호출된 argv를 모은다."""
    commands: list[list[str]] = []

    def run(command: list[str]) -> object:
        commands.append(command)
        if command[0] == "ffprobe":
            return _Completed(probe_json, "")
        Path(command[-1]).write_bytes(b"\x7f" * pcm_bytes)
        return _Completed("", ashowinfo)

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    return commands


def _probe_json(
    start_time: str | float = "2.000000",
    sample_rate: str = "16000",
    channels: int = 2,
    index: int | bool | None = 1,
    codec_type: str = "audio",
) -> str:
    entry: dict[str, object] = {
        "start_time": start_time,
        "sample_rate": sample_rate,
        "channels": channels,
    }
    if index is not None:
        entry["index"] = index
    if codec_type is not None:
        entry["codec_type"] = codec_type
    return json.dumps({"streams": [entry]})


def _ashowinfo(n: int, pts: int, *, channels: int = 2, rate: int = 16000, nb: int = 16000) -> str:
    """ashowinfo가 남기는 변환 후 프레임 한 줄. pts는 1/rate 단위 정수다."""
    return (
        f"[Parsed_ashowinfo_3 @ 0x1] n:{n} pts:{pts} pts_time:{pts / rate if rate else 0} fmt:s16 "
        f"channels:{channels} chlayout:stereo rate:{rate} nb_samples:{nb} "
        f"checksum:00000000 plane_checksums: [ 00000000 ]"
    )


def test_probe_reads_stream_times_as_exact_fractions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """시각은 부동소수로 읽지 않는다 — `Fraction(str(value))`로 정확히."""
    for raw, expected in [
        ("-0.264000", Fraction(-33, 125)),
        ("1.936000", Fraction(242, 125)),
        (1.936, Fraction(242, 125)),  # ffprobe가 숫자로 줄 때도 str을 거치면 정확하다
    ]:
        commands = _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(start_time=raw), "", 0)
        stream = probe_audio_stream(Path("demo.mp4"), 0)
        assert stream == preprocess.AudioStreamInfo(
            stream_index=1, start_time=expected, sample_rate=16000, channels=2
        )
        assert commands[0][commands[0].index("-of") + 1] == "json"


@pytest.mark.parametrize("bad", [-1, True, False, 1.0, "0"])
def test_probe_rejects_an_ordinal_that_is_not_a_count(
    monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """순번은 0 이상 정수다 — bool·float·문자열은 세는 수가 아니다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), "", 0)
    with pytest.raises(RuntimeError, match="순번"):
        probe_audio_stream(Path("demo.mp4"), bad)  # type: ignore[arg-type]


def test_probe_selects_by_ordinal_and_returns_the_global_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """물어보는 것은 오디오 순번, 돌려주는 것은 전역 번호 — 영상이 앞서면 어긋난다."""
    # 영상 0번 + 오디오 둘(1, 2): a:1을 물으면 ffprobe는 전역 2번만 알려준다
    commands = _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(index=2), "", 0)
    stream = probe_audio_stream(Path("demo.mp4"), 1)
    assert stream.stream_index == 2
    (command,) = commands
    assert command[command.index("-select_streams") + 1] == "a:1"
    assert (
        "index,codec_type,start_time,sample_rate,channels"
        in command[command.index("-show_entries") + 1]
    )


def test_probe_fails_loudly_when_the_stream_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_ffmpeg_ffprobe(monkeypatch, json.dumps({"streams": []}), "", 0)
    with pytest.raises(RuntimeError, match="오디오 스트림"):
        probe_audio_stream(Path("demo.mp4"), 7)


def test_probe_reports_out_of_range_selection_distinctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """빈 목록은 '못 찾음'과 다른 말이어야 한다 — 범위를 벗어난 선택이다."""
    _fake_ffmpeg_ffprobe(monkeypatch, json.dumps({"streams": []}), "", 0)
    with pytest.raises(RuntimeError, match="벗어난"):
        probe_audio_stream(Path("demo.mp4"), 7)


def test_probe_rejects_json_without_a_streams_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """streams 키가 없거나 목록이 아니면 고르기 전에 형태가 깨진 것이다."""
    for broken in ('{"format": {}}', '{"streams": "한 줄"}', "[1, 2]"):
        _fake_ffmpeg_ffprobe(monkeypatch, broken, "", 0)
        with pytest.raises(RuntimeError, match="streams"):
            probe_audio_stream(Path("demo.mp4"), 0)


def test_probe_rejects_ambiguous_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    """a:N은 스트림 하나를 골라야 한다 — 둘이 오면 어느 시계인지 알 수 없다."""
    entry = {
        "codec_type": "audio",
        "start_time": "2.0",
        "sample_rate": "16000",
        "channels": 2,
    }
    _fake_ffmpeg_ffprobe(
        monkeypatch, json.dumps({"streams": [dict(entry, index=1), dict(entry, index=2)]}), "", 0
    )
    with pytest.raises(RuntimeError, match="모호"):
        probe_audio_stream(Path("demo.mp4"), 0)


def test_probe_rejects_a_malformed_single_entry(monkeypatch: pytest.MonkeyPatch) -> None:
    """항목이 딕셔너리가 아니면 필드를 읽을 수조차 없다."""
    _fake_ffmpeg_ffprobe(monkeypatch, json.dumps({"streams": ["audio"]}), "", 0)
    with pytest.raises(RuntimeError, match="깨졌"):
        probe_audio_stream(Path("demo.mp4"), 0)


def test_probe_fails_loudly_on_broken_json(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ffmpeg_ffprobe(monkeypatch, "ffprobe가 반쯤 죽으며 흘린 출력", "", 0)
    with pytest.raises(RuntimeError, match="JSON"):
        probe_audio_stream(Path("demo.mp4"), 0)


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"index": 1, "codec_type": "audio", "sample_rate": "16000", "channels": 2}, "start_time"),
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "N/A",
                "sample_rate": "16000",
                "channels": 2,
            },
            "start_time",
        ),
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "1/0",
                "sample_rate": "16000",
                "channels": 2,
            },
            "start_time",
        ),
        ({"index": 1, "codec_type": "audio", "start_time": "2.0", "channels": 2}, "sample_rate"),
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "shuffle",
                "channels": 2,
            },
            "sample_rate",
        ),
        (
            {"index": 1, "codec_type": "audio", "start_time": "2.0", "sample_rate": "16000"},
            "channels",
        ),
        (
            {"codec_type": "audio", "start_time": "2.0", "sample_rate": "16000", "channels": 2},
            "index",
        ),
        (
            {
                "index": -1,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "16000",
                "channels": 2,
            },
            "index",
        ),
        (
            {
                "index": True,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "16000",
                "channels": 2,
            },
            "index",
        ),
    ],
)
def test_probe_names_the_field_it_could_not_read(
    monkeypatch: pytest.MonkeyPatch, entry: dict[str, object], field: str
) -> None:
    """어떤 필드가 못 읽었는지 메시지가 말해준다 — 조용한 기본값은 없다."""
    _fake_ffmpeg_ffprobe(monkeypatch, json.dumps({"streams": [entry]}), "", 0)
    with pytest.raises(RuntimeError, match=field):
        probe_audio_stream(Path("demo.mp4"), 0)


def test_probe_rejects_a_stream_that_is_not_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(codec_type="video"), "", 0)
    with pytest.raises(RuntimeError, match="오디오가 아닙니다|codec_type"):
        probe_audio_stream(Path("demo.mp4"), 0)
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(codec_type=None), "", 0)
    with pytest.raises(RuntimeError, match="오디오가 아닙니다|codec_type"):
        probe_audio_stream(Path("demo.mp4"), 0)


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "0",
                "channels": 2,
            },
            "sample_rate",
        ),
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "-16000",
                "channels": 2,
            },
            "sample_rate",
        ),
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "16000",
                "channels": 0,
            },
            "channels",
        ),
        (
            {
                "index": 1,
                "codec_type": "audio",
                "start_time": "2.0",
                "sample_rate": "16000",
                "channels": -2,
            },
            "channels",
        ),
    ],
)
def test_probe_rejects_nonpositive_clock_fields(
    monkeypatch: pytest.MonkeyPatch, entry: dict[str, object], field: str
) -> None:
    """주파수·채널은 시계의 단위다 — 0이나 음수면 길이를 셀 수 없다."""
    _fake_ffmpeg_ffprobe(monkeypatch, json.dumps({"streams": [entry]}), "", 0)
    with pytest.raises(RuntimeError, match="양수"):
        probe_audio_stream(Path("demo.mp4"), 0)


def test_extraction_command_preserves_the_measured_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`-copyts`로 시계를 유지하고 변환 후 프레임을 보고받는다. argv는 이 하나다.

    매핑은 ffprobe가 알려준 **전역 번호**다 — 순번 a:1로 물었지만 스트림이
    전역 5번이면 `0:5`를 짚는다.
    """
    commands = _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(index=5), _ashowinfo(0, 32000), 64000)
    extract_pcm_s16le(tmp_path / "in.mp4", tmp_path / "out.pcm", stream_index=1)

    assert commands[-1] == [
        "ffmpeg", "-hide_banner", "-loglevel", "info", "-nostdin", "-y",
        "-copyts",
        "-i", str(tmp_path / "in.mp4"),
        "-map", "0:5",
        "-vn",
        "-af", "aresample=16000,aformat=sample_fmts=s16,asettb=1/16000,ashowinfo",
        "-c:a", "pcm_s16le",
        "-f", "s16le",
        str(tmp_path / "out.pcm"),
    ]  # fmt: skip
    assert "-start_at_zero" not in commands[-1]
    assert "asetpts" not in commands[-1][commands[-1].index("-af") + 1]


@pytest.mark.parametrize("bad", [8000, 44100, 48000, 0, -16000, True, 16000.0])
def test_extraction_rejects_any_rate_but_the_contract_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: object
) -> None:
    """이 단계의 계약 주파수는 16 kHz 하나다 — 원본 44.1/48 kHz는 **리샘플로** 들어온다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), _ashowinfo(0, 0), 64000)
    with pytest.raises(RuntimeError, match="16000"):
        extract_pcm_s16le(
            tmp_path / "in.mov",
            tmp_path / "out.pcm",
            stream_index=0,
            sample_rate=bad,  # type: ignore[arg-type]
        )


def test_extraction_reads_the_first_converted_pts_as_the_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PCM 0번 프레임의 파일 시각 = 변환 후 첫 프레임의 정수 pts / rate."""
    stderr = "\n".join([_ashowinfo(0, -3200, nb=1024), _ashowinfo(1, -2176, nb=1024)])
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(start_time="-0.264000"), stderr, 8192)
    extraction = extract_pcm_s16le(tmp_path / "in.mkv", tmp_path / "out.pcm", stream_index=0)

    assert extraction.pcm_sample0_file_time == Fraction(-1, 5)
    # start_time은 −33/125(−0.264초)로 **다르다** — 이 64ms가 계약의 핵심이다
    assert extraction.stream.start_time == Fraction(-33, 125)
    assert extraction.pcm_sample0_file_time != extraction.stream.start_time


def test_extraction_keeps_retained_priming_as_the_epoch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AAC/MOV의 프라이밍은 출력에 남는다 — 원점은 실측 242/125지 작성값 2가 아니다."""
    stderr = "\n".join(_ashowinfo(n, 30976 + n * 1024, nb=1024) for n in range(17))
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(start_time="1.936000"), stderr, 69632)
    extraction = extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)

    assert extraction.pcm_sample0_file_time == Fraction(242, 125)
    assert extraction.sample_count == 17408


def test_extraction_counts_frames_per_channel_from_exact_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`sample_count`는 채널별 프레임 수다 — 64000바이트 스테레오는 16000프레임."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(channels=2), _ashowinfo(0, 0, channels=2), 64000)
    extraction = extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)

    assert extraction.sample_width == 2
    assert extraction.sample_count == 16000
    assert extraction.stream.channels == 2


def test_extraction_fails_without_converted_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ashowinfo 기록이 없으면 원점을 잰 것이 아니다 — 조용히 0으로 두지 않는다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), "[in @ 0x1] Input #0, s16le …", 64000)
    with pytest.raises(RuntimeError, match="ashowinfo"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


def test_extraction_fails_when_a_frame_has_no_integer_pts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    broken = _ashowinfo(0, 0).replace("pts:0 ", "pts:N/A ", 1)
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), broken, 64000)
    with pytest.raises(RuntimeError, match="pts"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


@pytest.mark.parametrize(
    ("mangle", "field"),
    [
        ("pts:0 ", "pts:1.5 "),
        ("pts:0 ", "pts:1/2 "),
        ("channels:2 ", "channels:2.5 "),
        ("rate:16000 ", "rate:16.000 "),
        ("nb_samples:16000 ", "nb_samples:10.5 "),
        ("nb_samples:16000 ", "nb_samples:16000/2 "),
    ],
)
def test_extraction_rejects_partial_number_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mangle: str, field: str
) -> None:
    """`pts:1.5`를 `1`로 슬쩍 읽으면 원점이 소리 없이 틀어진다 — 토큰 전체가 정수여야 한다."""
    broken = _ashowinfo(0, 0).replace(mangle, field, 1)
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), broken, 64000)
    name = field.strip().split(":")[0]
    with pytest.raises(RuntimeError, match=name):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


@pytest.mark.parametrize(
    "line",
    [
        _ashowinfo(0, 0, channels=0),
        _ashowinfo(0, 0, rate=0),
        _ashowinfo(0, 0, nb=0),
    ],
)
def test_extraction_rejects_nonpositive_converted_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, line: str
) -> None:
    """채널·주파수·프레임 길이가 0이면 시계가 아니다 — pts만 음수를 허용한다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), line, 0)
    with pytest.raises(RuntimeError, match="양수"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


def test_extraction_rejects_discontinuous_frame_timing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프레임 시계에 구멍이 있으면 파일 전체를 하나의 시계로 믿지 않는다."""
    stderr = "\n".join([_ashowinfo(0, 0, nb=8000), _ashowinfo(1, 12800, nb=8000)])
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), stderr, 32000)
    with pytest.raises(RuntimeError, match="불연속"):
        extract_pcm_s16le(tmp_path / "in.mkv", tmp_path / "out.pcm", stream_index=0)


def test_extraction_rejects_a_partial_channel_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """바이트 수가 채널 프레임을 반쯤 남겼다 — 샘플 수를 지어내지 않는다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), _ashowinfo(0, 0), 64001)
    with pytest.raises(RuntimeError, match="프레임"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


def test_extraction_cross_checks_the_frame_record_against_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ashowinfo가 설명하는 샘플 수와 파일 크기가 어긋나면 시계 증거가 아니다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), _ashowinfo(0, 0, nb=8000), 64000)
    with pytest.raises(RuntimeError, match="설명"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


def test_extraction_rejects_a_channel_or_rate_change_after_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프로브가 본 채널·요청한 주파수가 아니면 갈아끼운 것이다 — 못 믿는다."""
    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(channels=2), _ashowinfo(0, 0, channels=1), 32000)
    with pytest.raises(RuntimeError, match="채널"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)

    _fake_ffmpeg_ffprobe(monkeypatch, _probe_json(), _ashowinfo(0, 0, rate=44100), 64000)
    with pytest.raises(RuntimeError, match="주파수"):
        extract_pcm_s16le(tmp_path / "in.mov", tmp_path / "out.pcm", stream_index=0)


# ─────────────────────────────────────────────────────────────
#  원본 PCM 추출 — 합성 미디어로 잰 원점 (P2-01)
# ─────────────────────────────────────────────────────────────
#
# argv 흉내만으로는 원점 계약을 증명하지 못한다. 1초짜리 스테레오 PCM에
# 프레임 4000(=0.25초)마다 좌우 다른 임펄스를 싣고 PTS를 옮겨 담은 뒤,
# 어느 PCM 프레임이 그 임펄스를 말하는지로 원점을 잰다(Astra 실측 재현).
# 미디어는 전부 tmp_path 안에서만 만든다.


def _impulse_pcm_bytes(rate: int, left: int = 20000, right: int = -12000) -> bytes:
    """1초 스테레오 s16. 프레임 rate//4에만 소리가 있다(왼쪽 양수, 오른쪽 음수)."""
    samples = array.array("h", [0, 0] * rate)
    samples[(rate // 4) * 2] = left
    samples[(rate // 4) * 2 + 1] = right
    return samples.tobytes()


def _mux_shifted_media(
    directory: Path,
    name: str,
    *,
    codec: str,
    offset: Fraction,
    rate: int = 16000,
    shift_video: bool = False,
) -> Path:
    """임펄스 오디오 + 16x16 합성 영상. 오디오 PTS를 `offset`만큼 옮겨 담는다."""
    raw = directory / f"impulse-{rate}.s16"
    raw.write_bytes(_impulse_pcm_bytes(rate))
    shift = f"({offset.numerator}/{offset.denominator})/TB"
    chains = f"[0:a]asetpts=PTS+{shift}[a]"
    maps = ["-map", "1:v", "-map", "[a]"]
    if shift_video:
        chains += f";[1:v]setpts=PTS+{shift}[v]"
        maps = ["-map", "[v]", "-map", "[a]"]
    media = directory / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "s16le", "-ar", str(rate), "-ac", "2", "-i", str(raw),
            "-f", "lavfi", "-i", "testsrc=size=16x16:rate=25:duration=1",
            "-filter_complex", chains,
            *maps,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", codec, "-avoid_negative_ts", "disabled",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return media


def _loudest_left_frame(extraction: preprocess.PcmExtraction) -> tuple[int, int, int]:
    """(왼쪽 채널이 가장 큰 프레임 번호, 왼쪽 값, 오른쪽 값)."""
    data = extraction.path.read_bytes()
    samples = array.array("h")
    samples.frombytes(data[: len(data) - len(data) % samples.itemsize])
    channels = extraction.stream.channels
    frame = max(range(len(samples) // channels), key=lambda f: abs(samples[f * channels]))
    return frame, samples[frame * channels], samples[frame * channels + 1]


def _stream_start_time(media: Path, selector: str) -> str:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", selector,
            "-show_entries", "stream=start_time",
            "-of", "default=noprint_wrappers=1:nokey=1", str(media),
        ],
        capture_output=True,
        text=True,
        check=True,
    )  # fmt: skip
    return completed.stdout.strip()


@needs_ffmpeg
def test_measured_origins_match_the_committed_table_real(tmp_path: Path) -> None:
    """Astra 실측 표를 그대로 재현한다(docs/orchestration/reviews/astra-pcm-origin-evidence.md).

    결정적인 행은 AAC/MKV다 — ffprobe `stream.start_time`은 −33/125(−0.264초)인데
    디코드 원점은 −1/5(−0.2초)다. 둘을 같은 것으로 쓰면 64ms가 자막 전체에 실린다.
    AAC/MOV은 반대편 증거다: 프라이밍이 출력에 남아 원점이 작성값 2가 아니라
    242/125(1.936초)고, 임펄스는 프레임 5024에서 말한다. 보정 상수는 없다 —
    원점은 첫 프레임의 실측 pts 그 자체다.
    """
    # name, codec, offset, start_time, origin, frames, peak, peak_time
    # fmt: off
    cases = [
        ("pcm_plus.mov", "pcm_s16le", Fraction(2), Fraction(2), Fraction(2),
         16000, 4000, Fraction(9, 4)),
        ("aac_plus.mov", "aac", Fraction(2), Fraction(242, 125), Fraction(242, 125),
         17408, 5024, Fraction(9, 4)),
        ("aac_zero.mov", "aac", Fraction(0), Fraction(0), Fraction(0),
         16000, 4000, Fraction(1, 4)),
        ("pcm_minus.mkv", "pcm_s16le", Fraction(-1, 5), Fraction(-1, 5), Fraction(-1, 5),
         16000, 4000, Fraction(1, 20)),
        ("aac_minus.mkv", "aac", Fraction(-1, 5), Fraction(-33, 125), Fraction(-1, 5),
         16384, 4000, Fraction(1, 20)),
    ]
    # fmt: on
    for name, codec, offset, start, origin, frames, peak, peak_time in cases:
        media = _mux_shifted_media(tmp_path, name, codec=codec, offset=offset)
        extraction = extract_pcm_s16le(media, tmp_path / f"{name}.pcm", stream_index=0)

        assert extraction.stream.start_time == start, name
        assert extraction.stream.channels == 2, name  # 스테레오가 그대로 살아 있다
        assert extraction.sample_width == 2, name
        assert extraction.pcm_sample0_file_time == origin, name
        assert extraction.sample_count == frames, name

        got_peak, left, right = _loudest_left_frame(extraction)
        assert got_peak == peak, name
        # 원점 + 프레임/rate == 임펄스를 심은 파일 시각. 이 등식이 원점의 증명이다.
        assert extraction.pcm_sample0_file_time + Fraction(got_peak, 16000) == peak_time, name
        if codec == "pcm_s16le":  # 무손실 — 자극값까지 그대로다
            assert (left, right) == (20000, -12000), name
        else:  # AAC은 손실 — 자리와 부호·크기만 본다
            assert left > 10000 and right < -10000, name


@needs_ffmpeg
def test_resampling_from_44100_and_48000_keeps_the_epoch_real(tmp_path: Path) -> None:
    """44.1/48 kHz 원본도 원점은 pts 그대로다. 임펄스 자리는 리샘플 위상만큼 움직인다."""
    cases = [
        ("res48.mkv", 48000, Fraction(-1, 5), Fraction(1, 20)),
        ("res441.mov", 44100, Fraction(2), Fraction(9, 4)),
    ]  # fmt: skip
    for name, rate, offset, authored_peak_time in cases:
        media = _mux_shifted_media(tmp_path, name, codec="pcm_s16le", offset=offset, rate=rate)
        extraction = extract_pcm_s16le(media, tmp_path / f"{name}.pcm", stream_index=0)

        assert extraction.stream.sample_rate == rate, name
        assert extraction.pcm_sample0_file_time == offset, name
        assert extraction.sample_count == 16000, name  # 1초 × 16000 — 딱
        peak_time = extraction.pcm_sample0_file_time + Fraction(
            _loudest_left_frame(extraction)[0], 16000
        )
        assert abs(peak_time - authored_peak_time) <= Fraction(1, 250), name


@needs_ffmpeg
def test_the_epoch_is_the_shared_video_timeline_real(tmp_path: Path) -> None:
    """영상도 같이 +2초로 옮겨 담으면 오디오 원점은 영상 원점과 같은 2초다."""
    media = _mux_shifted_media(
        tmp_path, "both_plus.mov", codec="pcm_s16le", offset=Fraction(2), shift_video=True
    )
    assert Fraction(_stream_start_time(media, "v:0")) == 2

    extraction = extract_pcm_s16le(media, tmp_path / "out.pcm", stream_index=0)
    assert extraction.pcm_sample0_file_time == 2
    peak_time = extraction.pcm_sample0_file_time + Fraction(
        _loudest_left_frame(extraction)[0], 16000
    )
    assert peak_time == Fraction(9, 4)


@needs_ffmpeg
def test_a_pts_hole_in_the_source_is_rejected_real(tmp_path: Path) -> None:
    """0.5초 + 0.3초 구멍 + 0.5초: 디코드 프레임 시계가 불연속이다 — 하나의
    시계로 믿고 원점 하나를 내주면 안 된다."""
    first = tmp_path / "half-a.s16"
    second = tmp_path / "half-b.s16"
    for path, impulse_frame in ((first, 2000), (second, 6000)):
        samples = array.array("h", [0, 0] * 8000)
        samples[impulse_frame * 2] = 20000
        samples[impulse_frame * 2 + 1] = -12000
        path.write_bytes(samples.tobytes())
        subprocess.run(
            [
                "ffmpeg", "-v", "error", "-y",
                "-f", "s16le", "-ar", "16000", "-ac", "2", "-i", str(path),
                "-c:a", "pcm_s16le", str(path.with_suffix(".wav")),
            ],
            capture_output=True,
            check=True,
        )  # fmt: skip
    listing = tmp_path / "halves.txt"
    listing.write_text(
        f"file '{tmp_path / 'half-a.wav'}'\nfile '{tmp_path / 'half-b.wav'}'\n",
        encoding="utf-8",
    )
    media = tmp_path / "hole.mkv"
    # N >= 8000(=뒤쪽 0.5초)부터 +0.3초: 프레임 pts가 8192 → 12800으로 뛴다
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "concat", "-safe", "0", "-i", str(listing),
            "-f", "lavfi", "-i", "testsrc=size=16x16:rate=25:duration=1.3",
            "-filter_complex", "[0:a]asetpts=PTS+(3/10)/TB*gte(N\\,8000)[a]",
            "-map", "1:v", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le", "-avoid_negative_ts", "disabled",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip

    with pytest.raises(RuntimeError, match="불연속"):
        extract_pcm_s16le(media, tmp_path / "out.pcm", stream_index=0)


@needs_ffmpeg
def test_extraction_maps_the_second_audio_stream_exactly_real(tmp_path: Path) -> None:
    """영상 0번 + 오디오 둘(1, 2): 순번 a:1은 전역 2번이다 — `0:2`를 정확히 짚는다.

    오디오마다 원점을 다르게 심었다(+2초 / −1/5초). 매핑이 순번을 전역 번호로
    착각하면 엉뚱한 스트림의 원점이 돌아와 바로 드러난다.
    """
    first = tmp_path / "impulse-16000.s16"
    second = tmp_path / "distractor-16000.s16"
    first.write_bytes(_impulse_pcm_bytes(16000))
    second.write_bytes(_impulse_pcm_bytes(16000))
    media = tmp_path / "two-audio.mkv"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "s16le", "-ar", "16000", "-ac", "2", "-i", str(first),
            "-f", "s16le", "-ar", "16000", "-ac", "2", "-i", str(second),
            "-f", "lavfi", "-i", "testsrc=size=16x16:rate=25:duration=1",
            "-filter_complex",
            "[0:a]asetpts=PTS+(2/1)/TB[a0];[1:a]asetpts=PTS+(-1/5)/TB[a1]",
            "-map", "2:v", "-map", "[a0]", "-map", "[a1]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le", "-avoid_negative_ts", "disabled",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip

    streams = probe_audio_stream(media, 1)
    assert streams.stream_index == 2  # 전역 번호 — a:1은 두 번째 오디오다
    extraction = extract_pcm_s16le(media, tmp_path / "second.pcm", stream_index=1)

    assert extraction.stream.stream_index == 2
    assert extraction.pcm_sample0_file_time == Fraction(-1, 5)  # a:1의 원점
    peak_time = extraction.pcm_sample0_file_time + Fraction(
        _loudest_left_frame(extraction)[0], 16000
    )
    assert peak_time == Fraction(1, 20)
