"""자막 입히기의 ffmpeg 경계(W-65, D116) — 가짜 실행기로 argv·찾기·게시를 잰다."""

from __future__ import annotations

import os
import subprocess
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt import preprocess

FILTERS_WITH_LIBASS = """Filters:
  T.. = Timeline support
 ... = ...
 TS acompressor       A->A       Audio compressor.
 .. subtitles         V->V       Render text subtitles onto input video using the libass library.
 .. ass               V->V       Render ASS subtitles onto input video using the libass library.
"""
FILTERS_WITHOUT = """Filters:
 TS overlay           VV->V      Overlay a video source on top of the input.
 .. scale             V->V       Scale the input video size and/or convert the image format.
"""


@pytest.fixture(autouse=True)
def _fresh_caches() -> Any:
    preprocess.reset_process_caches()
    yield
    preprocess.reset_process_caches()


def _completed(stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, stdout, "")


def _encoding(codec: str = "libx264") -> preprocess.RenderEncoding:
    return preprocess.RenderEncoding(
        video_codec=codec, crf=20, preset="medium", hardware_bits_per_pixel=Fraction(1, 10),
        keyframe_seconds=Fraction(2), audio_codec="aac", audio_rate=48_000, audio_channels=2,
        audio_bitrate_kbps=192, pad_color="black", track_timescale_min=10_000,
        timeout_floor_seconds=1800.0, timeout_media_ratio=6.0, priority=10, threads=4,
    )  # fmt: skip


TARGET = preprocess.RenderTarget(1920, 1080, Fraction(1, 30))


def _request(tmp_path: Path) -> preprocess.BurnRequest:
    source = tmp_path / "제주 여행" / "draft.mp4"
    source.parent.mkdir()
    source.write_bytes(b"video")
    work = tmp_path / "work"
    work.mkdir()
    return preprocess.BurnRequest("/opt/ffmpeg-full", source, work, "burn.ass", "fonts")


def test_filters_are_parsed_per_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        return _completed(FILTERS_WITH_LIBASS if "full" in command[0] else FILTERS_WITHOUT)

    monkeypatch.setattr(preprocess, "_run", run)
    assert "subtitles" in preprocess.available_filters("/x/ffmpeg-full")
    assert preprocess.available_filters("/x/ffmpeg") == frozenset({"overlay", "scale"})
    preprocess.available_filters("/x/ffmpeg-full")
    assert len(seen) == 2  # 실행 파일마다 한 번


def test_subtitle_ffmpeg_prefers_path_then_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    full = tmp_path / "ffmpeg-full"
    full.write_text("#!/bin/sh\n")
    full.chmod(0o755)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "/usr/bin/ffmpeg")
    listing = {"/usr/bin/ffmpeg": FILTERS_WITHOUT, str(full): FILTERS_WITH_LIBASS}
    monkeypatch.setattr(preprocess, "_run", lambda command, **_: _completed(listing[command[0]]))
    missing = str(tmp_path / "없는-ffmpeg")
    assert preprocess.subtitle_ffmpeg((missing, str(full))) == str(full)
    preprocess.reset_process_caches()
    listing["/usr/bin/ffmpeg"] = FILTERS_WITH_LIBASS
    assert preprocess.subtitle_ffmpeg((str(full),)) == "/usr/bin/ffmpeg"


def test_subtitle_ffmpeg_names_the_install(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": "/usr/bin/ffmpeg")
    monkeypatch.setattr(preprocess, "_run", lambda command, **_: _completed(FILTERS_WITHOUT))
    monkeypatch.setattr(preprocess.sys, "platform", "darwin")
    with pytest.raises(RuntimeError, match="brew install ffmpeg-full"):
        preprocess.subtitle_ffmpeg(())


def test_burn_command_uses_relative_filter_names(tmp_path: Path) -> None:
    request = _request(tmp_path)
    staged = tmp_path / "out" / ".x.part"
    command = preprocess._burn_command(request, staged, TARGET, _encoding(), "copy")
    assert command[0] == "/opt/ffmpeg-full"
    assert command[command.index("-i") + 1] == os.path.abspath(request.source)
    assert command[command.index("-vf") + 1] == "subtitles=filename=burn.ass:fontsdir=fonts"
    assert command[command.index("-c:a") + 1] == "copy"
    assert command[command.index("0:v:0") + 1 : command.index("0:v:0") + 3] == ["-map", "0:a:0"]
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    assert command[-1] == os.path.abspath(staged) and "-movflags" in command
    encoded = preprocess._burn_command(request, staged, TARGET, _encoding(), "encode")
    assert encoded[encoded.index("-c:a") + 1] == "aac" and "192k" in encoded
    silent = preprocess._burn_command(request, staged, TARGET, _encoding(), "none")
    assert "-c:a" not in silent and "0:a:0" not in silent
    hardware = preprocess._burn_command(
        request, staged, TARGET, _encoding("h264_videotoolbox"), "copy"
    )
    assert "-b:v" in hardware and "-crf" not in hardware


def test_filter_names_must_need_no_escaping(tmp_path: Path) -> None:
    for name in ("my subs.ass", "a:b.ass", "x,y", "../burn.ass", ""):
        bad = preprocess.BurnRequest("ffmpeg", tmp_path, tmp_path, name, "fonts")
        with pytest.raises(ValueError):
            bad.video_filter()


def _probe(width: int = 1920, height: int = 1080, duration: str = "6", audio: bool = True) -> Any:
    video = SimpleNamespace(width=width, height=height, codec_name="h264")
    tracks = (SimpleNamespace(codec_name="aac"),) if audio else ()
    return SimpleNamespace(video=video, duration=Fraction(duration), audio=tracks)


def test_burn_runs_in_the_work_folder_and_publishes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    output = request.source.parent / "draft.burned.mp4"
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(options)
        Path(command[-1]).write_bytes(b"burned")
        return _completed()

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path: _probe())
    burned = preprocess.burn_subtitles(
        request, output, TARGET, _encoding(), duration=Fraction(6), audio="copy"
    )
    assert burned.path == output and output.read_bytes() == b"burned"
    assert calls[0]["cwd"] == request.workdir and calls[0]["priority"] == 10
    assert not list(output.parent.glob("*.part"))
    with pytest.raises(preprocess.RawStagingError):
        preprocess.burn_subtitles(
            request, output, TARGET, _encoding(), duration=Fraction(6), audio="copy"
        )


def test_a_burn_that_differs_is_not_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    output = request.source.parent / "draft.burned.mp4"

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        Path(command[-1]).write_bytes(b"short")
        return _completed()

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path: _probe(duration="5"))
    with pytest.raises(preprocess.RenderVerificationError, match="길이"):
        preprocess.burn_subtitles(
            request, output, TARGET, _encoding(), duration=Fraction(6), audio="copy"
        )
    assert not output.exists() and not list(output.parent.glob("*.part"))
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path: _probe(audio=False))
    with pytest.raises(preprocess.RenderVerificationError, match="소리"):
        preprocess.burn_subtitles(
            request, output, TARGET, _encoding(), duration=Fraction(6), audio="copy"
        )


def test_preview_frame_reads_and_removes_the_jpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _request(tmp_path)
    seen: list[list[str]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command)
        (options["cwd"] / command[-1]).write_bytes(b"\xff\xd8jpeg")
        return _completed()

    monkeypatch.setattr(preprocess, "_run", run)
    picture = preprocess.burn_preview_frame(
        request, Fraction(3, 2), long_edge=960, quality=3, timeout=60.0
    )
    assert picture == b"\xff\xd8jpeg"
    (command,) = seen
    assert command[command.index("-ss") + 1] == "1.500"
    assert command[command.index("-vf") + 1].startswith(
        "subtitles=filename=burn.ass:fontsdir=fonts,"
    )
    assert not (request.workdir / "preview.jpg").exists()


def test_encoder_pick_can_target_another_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []

    def run(command: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
        seen.append(command[0])
        listing = " ------\n V....D h264_videotoolbox VT\n V....D libx264 x264\n"
        return _completed(listing if "-encoders" in command else "")

    monkeypatch.setattr(preprocess, "_run", run)
    assert preprocess.pick_video_encoder(("h264_videotoolbox", "libx264"), "/opt/full") == (
        "h264_videotoolbox"
    )
    assert set(seen) == {"/opt/full"}


# ── 최종 편집의 컷과 함께 입히기(W-67, D118) ──────────────────────────────

NTSC = Fraction(1001, 30000)
CUT_GRAPH = (
    "[0:v:0]split=2[vs0][vs1];[0:a:0]asplit=2[as0][as1];"
    "[vs0]trim=start_frame=0:end_frame=60,setpts=PTS-STARTPTS[v0];"
    "[as0]atrim=start=0.000000:end=2.002000,asetpts=PTS-STARTPTS[a0];"
    "[vs1]trim=start_frame=150:end_frame=240,setpts=PTS-STARTPTS[v1];"
    "[as1]atrim=start=5.005000:end=8.008000,asetpts=PTS-STARTPTS[a1];"
    "[v0][a0][v1][a1]concat=n=2:v=1:a=1[cv][ca];"
    "[cv]subtitles=filename=burn.ass:fontsdir=fonts[vout]"
)


def _cut_request(tmp_path: Path, inline_chars: int = 24_000) -> preprocess.BurnRequest:
    plain = _request(tmp_path)
    cut = preprocess.BurnCut(NTSC, ((0, 60), (150, 240)), inline_chars)
    return preprocess.BurnRequest(
        plain.executable, plain.source, plain.workdir, plain.subtitle_name, plain.fonts_name, cut
    )


def test_a_cut_burn_trims_joins_and_burns_in_one_graph(tmp_path: Path) -> None:
    request = _cut_request(tmp_path)
    staged = tmp_path / "out" / ".x.part"
    command = preprocess._burn_command(request, staged, TARGET, _encoding(), "encode")
    assert command[command.index("-filter_complex") + 1] == CUT_GRAPH
    assert "-vf" not in command and "0:v:0" not in command[: command.index("-filter_complex")]
    maps = [command[index + 1] for index, part in enumerate(command) if part == "-map"]
    assert maps == ["[vout]", "[ca]"]
    assert command[command.index("-c:a") + 1] == "aac"
    assert command[command.index("-fps_mode") + 1] == "passthrough"
    with pytest.raises(ValueError, match="소리"):
        preprocess._burn_command(request, staged, TARGET, _encoding(), "copy")


def test_a_cut_burn_without_sound_has_no_audio_branch(tmp_path: Path) -> None:
    request = _cut_request(tmp_path)
    command = preprocess._burn_command(request, tmp_path / "x.part", TARGET, _encoding(), "none")
    graph = command[command.index("-filter_complex") + 1]
    assert "asplit" not in graph and "atrim" not in graph
    assert "[v0][v1]concat=n=2:v=1:a=0[cv];" in graph
    maps = [command[index + 1] for index, part in enumerate(command) if part == "-map"]
    assert maps == ["[vout]"] and "-c:a" not in command


def test_a_long_cut_graph_goes_to_a_file_on_ffmpeg_7(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _cut_request(tmp_path, inline_chars=100)
    versions = ["ffmpeg version 9.0.2 Copyright"]
    monkeypatch.setattr(preprocess, "executable_version", lambda program: versions[0])
    command = preprocess._burn_command(request, tmp_path / "x.part", TARGET, _encoding(), "encode")
    assert command[command.index("-/filter_complex") + 1] == "cut.graph"
    assert "-filter_complex" not in command
    assert (request.workdir / "cut.graph").read_text(encoding="utf-8") == CUT_GRAPH
    versions[0] = "ffmpeg version N-118000-gdeadbeef Copyright"
    preprocess._burn_command(request, tmp_path / "x.part", TARGET, _encoding(), "encode")
    versions[0] = "ffmpeg version 6.1.1-3ubuntu5 Copyright"
    with pytest.raises(RuntimeError, match="ffmpeg 7"):
        preprocess._burn_command(request, tmp_path / "x.part", TARGET, _encoding(), "encode")


def test_a_cut_burn_waits_for_the_whole_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = _cut_request(tmp_path)
    output = request.source.parent / "final.burned.mp4"
    calls: list[dict[str, Any]] = []

    def run(command: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(options)
        Path(command[-1]).write_bytes(b"burned")
        return _completed()

    monkeypatch.setattr(preprocess, "_run", run)
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path: _probe(duration="5.005"))
    burned = preprocess.burn_subtitles(
        request,
        output,
        TARGET,
        _encoding(),
        duration=Fraction(5005, 1000),
        audio="encode",
        timeout_basis=Fraction(600),
    )
    assert burned.path == output
    assert calls[0]["timeout"] == 3600.0
