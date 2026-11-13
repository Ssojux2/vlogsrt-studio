"""도구·탐침 캐시 · 미리보기 하드웨어 인코더 · 동시 실행 상한(W-45, D100 §6).

- 도구 경로(`require_ffmpeg`)는 (도구, PATH)마다, 버전(`-version`)은 찾은 경로마다 프로세스에서
  한 번이다.
- 원본 조사 기억(`probe_*_memo`)은 stat 신원·도구 버전이 같을 때만 같은 값을 돌려주고, 파일이 바뀌면
  다시 잰다. 방금 쓴 출력을 재는 검증은 기억을 쓰지 않는다.
- 미리보기는 하드웨어 인코더가 먼저다 — 고른 이름은 기록의 `encoding`에만 남고 캐시 열쇠는 그대로다.

가짜 실행기는 ffmpeg 없이 argv를 센다. 실제 ffmpeg 시험은 ffmpeg가 있을 때만 돈다. 유료 호출 0회.
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import visual_review
from vlogsrt.project.raw.analysis_config import (
    REVIEW_MEDIA,
    ReviewMediaSettings,
    validate_review_media_settings,
)
from vlogsrt.project.raw.config import LocalMediaConcurrency

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

VERSION_LINE = "ffprobe version 9.9-speed-test"


class _Done:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = 0


def _probe_json(width: int = 640, height: int = 360) -> str:
    video = {
        "index": 0, "codec_type": "video", "codec_name": "hevc", "profile": "Main 10",
        "pix_fmt": "yuv420p10le", "width": width, "height": height,
        "color_primaries": "bt709", "color_transfer": "bt709", "color_space": "bt709",
        "color_range": "tv", "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
        "time_base": "1/15360", "start_time": "0.000000", "start_pts": 0, "duration_ts": 30720,
    }  # fmt: skip
    return json.dumps({"streams": [video], "format": {"format_name": "mov,mp4", "duration": "2"}})


def _clock_json() -> str:
    stream = {
        "index": 0, "codec_type": "video", "r_frame_rate": "30/1", "avg_frame_rate": "30/1",
        "time_base": "1/30", "nb_frames": "4", "start_pts": 0, "duration_ts": 4,
    }  # fmt: skip
    packets = [{"pts": n} for n in range(4)]
    return json.dumps({"streams": [stream], "packets": packets})


class _Runner:
    """ffprobe 대역 — 부른 argv를 적는다. `-version`·조사·패킷 훑기에 대답한다."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str], **_: Any) -> _Done:
        self.calls.append(list(command))
        if "-version" in command:
            return _Done(VERSION_LINE + "\nconfiguration: --test\n")
        if "packet=pts" in command:
            return _Done(_clock_json())
        return _Done(_probe_json())

    def count(self, flag: str) -> int:
        return sum(1 for call in self.calls if flag in call)

    def probes_of(self, path: Path) -> int:
        return sum(1 for call in self.calls if call[-1] == str(path))


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> _Runner:
    fake = _Runner()
    monkeypatch.setattr(preprocess, "_run", fake)
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": f"/fake/bin/{tool}")
    return fake


def _clip(tmp_path: Path, name: str = "clip.mov", body: bytes = b"a" * 64) -> Path:
    path = tmp_path / name
    path.write_bytes(body)
    return path


# ── 도구 ──────────────────────────────────────────────────────────────────────


def test_the_version_is_read_once_per_process_across_many_probes(
    tmp_path: Path, runner: _Runner
) -> None:
    clip = _clip(tmp_path)
    for _ in range(5):
        probe = preprocess.probe_raw_media(clip)  # 기억 없는 조사 — 다섯 번 다 띄운다
        assert probe.ffprobe_version == VERSION_LINE
    assert runner.count("-version") == 1
    assert runner.probes_of(clip) == 5
    preprocess.reset_process_caches()
    preprocess.probe_raw_media(clip)
    assert runner.count("-version") == 2  # 비우면 다시 읽는다


def test_ffmpeg_provenance_spawns_nothing_after_the_first_call(
    runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preprocess, "available_encoders", lambda: frozenset({"libx264", "aac"}))
    first = preprocess.ffmpeg_provenance()
    spawned = len(runner.calls)
    assert spawned == 2  # ffmpeg -version · ffprobe -version
    assert all(preprocess.ffmpeg_provenance() == first for _ in range(4))
    assert len(runner.calls) == spawned


def test_which_is_cached_and_a_path_change_looks_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binary = _clip(tmp_path, "ffprobe", b"#!")
    looked: list[str] = []

    def which(tool: str) -> str:
        looked.append(tool)
        return str(binary)

    monkeypatch.setattr(preprocess.shutil, "which", which)
    monkeypatch.setenv("PATH", "/first")
    assert {preprocess.require_ffmpeg("ffprobe") for _ in range(4)} == {str(binary)}
    assert looked == ["ffprobe"]
    preprocess.require_ffmpeg("ffmpeg")
    assert looked == ["ffprobe", "ffmpeg"]  # 도구마다 따로
    monkeypatch.setenv("PATH", "/second")
    preprocess.require_ffmpeg("ffprobe")
    assert looked == ["ffprobe", "ffmpeg", "ffprobe"]  # PATH가 바뀌면 다시 찾는다


def test_a_missing_tool_is_not_remembered_and_a_vanished_one_is_looked_up_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found: list[str | None] = [None]
    monkeypatch.setattr(preprocess.shutil, "which", lambda tool: found[0])
    with pytest.raises(RuntimeError, match="ffprobe 를 찾지 못했습니다"):
        preprocess.require_ffmpeg("ffprobe")
    binary = _clip(tmp_path, "ffprobe", b"#!")
    found[0] = str(binary)
    assert preprocess.require_ffmpeg("ffprobe") == str(binary)  # 깔고 나면 바로 찾는다
    binary.unlink()
    found[0] = None
    with pytest.raises(RuntimeError, match="찾지 못했습니다"):
        preprocess.require_ffmpeg("ffprobe")  # 지운 실행 파일을 기억으로 내놓지 않는다


# ── 원본 조사 기억 ────────────────────────────────────────────────────────────


def test_the_memo_returns_the_same_frozen_value_for_an_unchanged_file(
    tmp_path: Path, runner: _Runner
) -> None:
    clip = _clip(tmp_path)
    first = preprocess.probe_raw_media_memo(clip)
    assert preprocess.probe_raw_media_memo(clip) is first
    clock = preprocess.probe_extraction_clock_memo(clip, probe=first)
    assert preprocess.probe_extraction_clock_memo(clip) is clock
    assert runner.probes_of(clip) == 2  # 조사 한 번 · 패킷 훑기 한 번
    for value in (first, first.video, first.creation_time, clock):
        assert type(value).__dataclass_params__.frozen  # type: ignore[attr-defined]
        with pytest.raises(dataclasses.FrozenInstanceError):
            setattr(value, dataclasses.fields(value)[0].name, None)


def _touch(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_the_memo_misses_whenever_the_file_changes(tmp_path: Path, runner: _Runner) -> None:
    clip = _clip(tmp_path)
    _touch(clip, 1_000_000_000)
    preprocess.probe_raw_media_memo(clip)
    preprocess.probe_raw_media_memo(clip)
    assert runner.probes_of(clip) == 1
    _touch(clip, 2_000_000_000)  # 수정 시각만
    preprocess.probe_raw_media_memo(clip)
    assert runner.probes_of(clip) == 2
    clip.write_bytes(b"b" * 64)  # 같은 크기로 다시 쓰고 수정 시각을 되돌린다 — 변경 시각이 잡는다
    _touch(clip, 2_000_000_000)
    preprocess.probe_raw_media_memo(clip)
    assert runner.probes_of(clip) == 3
    other = _clip(tmp_path, "next.mov", b"c" * 64)  # 같은 크기·시각의 다른 파일(inode)
    _touch(other, 2_000_000_000)
    os.replace(other, clip)
    preprocess.probe_raw_media_memo(clip)
    preprocess.probe_raw_media_memo(clip)
    assert runner.probes_of(clip) == 4


def test_the_memo_key_carries_the_tool_version_and_the_prober(
    tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = _clip(tmp_path)
    preprocess.probe_raw_media_memo(clip)
    with preprocess._VERSION_LOCK:
        preprocess._TOOL_VERSIONS["/fake/bin/ffprobe"] = "ffprobe version 10.0"  # 올린 ffprobe
    preprocess.probe_raw_media_memo(clip)
    assert runner.probes_of(clip) == 2
    calls: list[Path] = []
    real = preprocess.probe_raw_media

    def other(path: Path) -> preprocess.RawMediaProbe:
        calls.append(path)
        return real(path)

    monkeypatch.setattr(preprocess, "probe_raw_media", other)
    preprocess.probe_raw_media_memo(clip)
    assert calls == [clip]  # 조사자를 갈아 끼우면 기억을 쓰지 않는다


def test_missing_files_and_unfrozen_values_are_not_remembered(
    tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "gone.mov"
    preprocess.probe_raw_media_memo(missing)
    preprocess.probe_raw_media_memo(missing)
    assert runner.probes_of(missing) == 2
    clip = _clip(tmp_path)
    calls: list[Path] = []

    def loose(path: Path) -> object:
        calls.append(path)
        return {"video": None}  # 얼리지 않은 값 — 기억하지 않는다

    monkeypatch.setattr(preprocess, "probe_raw_media", loose)
    preprocess.probe_raw_media_memo(clip)
    preprocess.probe_raw_media_memo(clip)
    assert calls == [clip, clip]


def test_the_memo_is_bounded_by_its_setting(
    tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(preprocess, "_PROBE_MEMO", preprocess._ProbeMemo(
        preprocess.ProbeMemoSettings(entries=2)
    ))  # fmt: skip
    clips = [_clip(tmp_path, f"{n}.mov") for n in range(3)]
    for clip in clips:
        preprocess.probe_raw_media_memo(clip)
    assert len(preprocess._PROBE_MEMO) == 2
    preprocess.probe_raw_media_memo(clips[2])  # 가장 최근 — 그대로
    preprocess.probe_raw_media_memo(clips[0])  # 가장 오래된 것은 밀려났다
    assert [runner.probes_of(clip) for clip in clips] == [2, 1, 1]
    assert preprocess.PROBE_MEMO.entries == 128
    with pytest.raises(ValueError, match="조사 기억 칸 수"):
        preprocess.ProbeMemoSettings(entries=0)


def test_reserving_room_grows_the_memo_to_a_library_and_never_shrinks_it(
    tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """라이브러리가 기본 칸보다 크면 한 바퀴마다 전부 밀려나 기억이 한 번도 맞지 않았다(D121 —
    원본 169개 · 338칸 대 128칸). 라이브러리를 훑는 자리가 먼저 그만큼 자리를 잡는다."""
    monkeypatch.setattr(preprocess, "_PROBE_MEMO", preprocess._ProbeMemo(
        preprocess.ProbeMemoSettings(entries=2)
    ))  # fmt: skip
    preprocess.reserve_probe_memo(1)  # 기본보다 작으면 그대로
    assert preprocess._PROBE_MEMO.limit == 2
    clips = [_clip(tmp_path, f"{n}.mov") for n in range(3)]
    preprocess.reserve_probe_memo(3)
    for _ in range(2):
        for clip in clips:
            preprocess.probe_raw_media_memo(clip)
    assert [runner.probes_of(clip) for clip in clips] == [1, 1, 1]
    preprocess.reserve_probe_memo(1)  # 줄이지 않는다
    assert preprocess._PROBE_MEMO.limit == 3 and len(preprocess._PROBE_MEMO) == 3
    with pytest.raises(ValueError, match="조사 기억 칸 수"):
        preprocess.reserve_probe_memo(0)


def test_a_remembered_clock_still_checks_the_probe_stream(tmp_path: Path, runner: _Runner) -> None:
    clip = _clip(tmp_path)
    probe = preprocess.probe_raw_media_memo(clip)
    preprocess.probe_extraction_clock_memo(clip, probe=probe)
    moved = dataclasses.replace(probe, video=dataclasses.replace(probe.video, stream_index=3))
    with pytest.raises(RuntimeError, match="시계의 스트림이 프로브와 다릅니다"):
        preprocess.probe_extraction_clock_memo(clip, probe=moved)


def _tagged_clip(path: Path) -> None:
    """D53 촬영 순서 픽스처(`tests/raw/test_creation_order._encode`)와 같은 꼴의 실제 원본."""
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=2",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", "2", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le", "-metadata", "creation_time=2026-08-11T03:00:00+00:00",
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


@needs_ffmpeg
def test_a_remembered_probe_of_a_real_file_equals_a_fresh_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-45b 회귀 — 기억은 시간만 바꾸고 값은 바꾸지 않는다.

    `test_bulk_confirm_keeps_the_w13_rules_undo_and_restore`가 W-45 뒤로 실패했다. 기억이 다른 값을
    돌려준 것이 아니었다: 앱 열기가 건 정리(촬영 시각 순 맞춤, D53)가 기억 덕에 빨라져 시험의 첫
    편집보다 먼저 머리를 새 계획으로 옮겼다(조각 아이디가 바뀐다). 그 시험은 정리를 기다리지 않고
    옛 머리의 아이디를 쥐고 있었다 — W-45 앞 코드도 정리가 먼저 끝나게 하면 똑같이 실패한다.
    여기서는 그 원인 후보를 닫는다: 실제 원본의 기억 값(맞은 것 · 처음 잰 것)이 기억 없는 조사와
    칸마다 같고(찾은 경로로 읽은 버전 문자열 포함), 맞은 기억은 프로세스를 하나도 띄우지 않는다.
    """
    source = tmp_path / "DJI_0002.mov"
    _tagged_clip(source)
    bare = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, check=True)
    assert preprocess._tool_version("ffprobe") == bare.stdout.strip().splitlines()[0]
    fresh = preprocess.probe_raw_media(source)
    fresh_clock = preprocess.probe_extraction_clock(source, probe=fresh)
    preprocess.reset_process_caches()  # 도구 경로·버전까지 비우고 처음부터 기억을 채운다
    missed = preprocess.probe_raw_media_memo(source)
    missed_clock = preprocess.probe_extraction_clock_memo(source, probe=missed)
    assert (missed, missed_clock) == (fresh, fresh_clock)
    spawned: list[list[str]] = []
    real_run = preprocess._run

    def counting(command: list[str], *args: Any, **kwargs: Any) -> Any:
        spawned.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(preprocess, "_run", counting)
    hit = preprocess.probe_raw_media_memo(source)
    hit_clock = preprocess.probe_extraction_clock_memo(source, probe=hit)
    assert hit is missed and hit_clock is missed_clock
    assert spawned == []  # 맞은 기억은 ffprobe도 `-version`도 띄우지 않는다 — 빨라진 것뿐이다
    assert (hit, hit_clock) == (preprocess.probe_raw_media(source), fresh_clock)
    assert fresh.creation_time is not None and hit.creation_time == fresh.creation_time


def test_verification_probes_of_the_written_preview_bypass_the_memo(
    tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본 조사는 두 번째에 기억이고, 방금 쓴 스테이징 출력은 매번 다시 잰다."""
    source = _clip(tmp_path, "source.mov")
    staged_probes: list[Path] = []
    real_probe = preprocess.probe_raw_media

    def probe(path: Path) -> preprocess.RawMediaProbe:
        if path != source:
            staged_probes.append(path)
        return real_probe(path)

    monkeypatch.setattr(preprocess, "probe_raw_media", probe)
    monkeypatch.setattr(preprocess, "_run_niced", lambda command, priority, **_: None)
    monkeypatch.setattr(preprocess, "_publish_staged_clip", lambda staged, output: None)
    for n in range(2):
        out = tmp_path / "previews" / f"out{n}.mp4"
        preprocess.extract_visual_review_preview(source, out, audio_stream_index=None)
    assert runner.probes_of(source) == 1  # 원본은 한 번만 조사했다
    assert len(staged_probes) == 2  # 출력 검증은 두 번 다 조사했다
    assert sum(1 for call in runner.calls if "packet=pts" in call) == 2  # 출력의 시계도 매번


# ── 미리보기 인코더 ───────────────────────────────────────────────────────────


def _hevc_probe() -> preprocess.RawMediaProbe:
    return preprocess._parse_raw_probe(_probe_json(3840, 2160), Path("in.mov"), 0)


def _preview_args(encoding: preprocess.PreviewEncoding) -> list[str]:
    args, _, remuxed = preprocess._visual_preview_args(_hevc_probe(), None, encoding)
    assert not remuxed
    return args


def _encoding(**overrides: Any) -> preprocess.PreviewEncoding:
    values: dict[str, Any] = {
        "long_edge": 960,
        "preset": REVIEW_MEDIA.preview_x264_preset,
        "crf": REVIEW_MEDIA.preview_x264_crf,
        "video_codecs": REVIEW_MEDIA.preview_video_codecs,
        "hardware_bits_per_pixel": REVIEW_MEDIA.preview_hardware_bits_per_pixel,
    }
    values.update(overrides)
    return preprocess.PreviewEncoding(**values)


def test_the_preview_picks_the_first_listed_working_hardware_encoder(
    runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    listed = frozenset({"h264_qsv", "h264_amf", "libx264"})
    monkeypatch.setattr(preprocess, "available_encoders", lambda: listed)
    monkeypatch.setattr(preprocess, "encoder_works", lambda name: name != "h264_qsv")
    args = _preview_args(_encoding())
    assert args[args.index("-c:v") + 1] == "h264_amf"
    # 960×540 @30 × 0.1 비트/화소 = 1 555 200 b/s — crf·preset·threads는 하드웨어에 싣지 않는다
    assert args[args.index("-b:v") + 1] == "1555200"
    assert "-crf" not in args and "-preset" not in args and "-threads" not in args
    tag = preprocess._preview_tag(_encoding(), args)
    assert tag == "h264_amf/b1555200/le960"


def test_the_preview_falls_back_to_libx264_when_the_hardware_fails_its_probe(
    runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        preprocess, "available_encoders", lambda: frozenset({"h264_videotoolbox", "libx264"})
    )
    monkeypatch.setattr(preprocess, "encoder_works", lambda name: name == "libx264")
    encoding = _encoding(threads=3)
    args = _preview_args(encoding)
    assert args[args.index("-c:v") + 1] == "libx264"
    assert args[args.index("-crf") + 1] == str(REVIEW_MEDIA.preview_x264_crf)
    assert args[args.index("-threads") + 1] == "3"
    assert preprocess._preview_tag(encoding, args) == "libx264/veryfast/crf23/le960"  # 예전 이름


def test_libx264_alone_asks_no_encoder_question(
    runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    def forbidden(*_: object) -> object:
        raise AssertionError("libx264 하나뿐이면 인코더 목록을 묻지 않는다")

    monkeypatch.setattr(preprocess, "available_encoders", forbidden)
    monkeypatch.setattr(preprocess, "encoder_works", forbidden)
    encoding = preprocess.PreviewEncoding(long_edge=960, preset="veryfast", crf=23)
    args = _preview_args(encoding)
    assert args[args.index("-c:v") + 1 :][:5] == ["libx264", "-preset", "veryfast", "-crf", "23"]
    assert "-threads" not in args  # 몫을 주지 않으면 예전 argv 그대로


def test_the_preview_cache_key_does_not_see_the_encoder() -> None:
    """D62 — 열쇠·조리법 판은 그대로다(W-45 앞에서 잰 값과 바이트가 같다)."""
    assert visual_review.VISUAL_PREVIEW_RECIPE == "visual-review-preview/v1"
    assert visual_review.preview_media_key("ab" * 32, 0) == "4d20325b831dcb3d3704d252"
    assert visual_review.preview_media_key("ab" * 32, None) == "d7e85dcc742bdb156715d41e"


def test_the_workbench_preview_encoding_comes_from_the_settings(runner: _Runner) -> None:
    video = _hevc_probe().video
    encoding = visual_review._preview_encoding(video)
    assert encoding.video_codecs == REVIEW_MEDIA.preview_video_codecs
    assert (
        encoding.video_codecs[0] == "h264_videotoolbox" and encoding.video_codecs[-1] == "libx264"
    )
    assert encoding.hardware_bits_per_pixel == Fraction(1, 10)
    assert encoding.threads == LocalMediaConcurrency().encode_threads(os.cpu_count())


@pytest.mark.parametrize(
    "overrides",
    [
        {"preview_video_codecs": ()},
        {"preview_video_codecs": ("h264_videotoolbox",)},  # libx264로 끝나지 않는다
        {"preview_video_codecs": ["libx264"]},
        {"preview_hardware_bits_per_pixel": Fraction(0)},
        {"preview_hardware_bits_per_pixel": 0.1},
    ],
)
def test_preview_encoder_settings_are_validated(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="검토 보조 미디어 설정"):
        validate_review_media_settings(ReviewMediaSettings(**overrides))


def _ten_bit(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30:duration=1",
            "-f", "lavfi", "-i", "sine=duration=1",
            "-c:v", "libx264", "-profile:v", "high10", "-pix_fmt", "yuv420p10le",
            "-c:a", "aac", str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


@needs_ffmpeg
def test_the_built_preview_records_the_picked_encoder(tmp_path: Path) -> None:
    source = tmp_path / "in.mp4"
    _ten_bit(source)
    encoding = _encoding(long_edge=320)
    built = preprocess.extract_visual_review_preview(
        source, tmp_path / "out.mp4", audio_stream_index=1, encoding=encoding
    )
    picked = preprocess.pick_video_encoder(REVIEW_MEDIA.preview_video_codecs)
    assert built.encoding is not None and built.encoding.split("/")[0] == picked
    assert (built.width, built.height, built.video_codec) == (320, 180, "h264")
    assert built.audio_stream_count == 1


@needs_ffmpeg
def test_a_hardware_encoder_that_fails_on_the_source_is_retried_with_libx264(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """목록에 있고 확인을 지났지만 이 원본에서 실패하는 인코더 — libx264로 한 번 더 만든다."""
    source = tmp_path / "in.mp4"
    _ten_bit(source)
    monkeypatch.setattr(
        preprocess, "available_encoders", lambda: frozenset({"h264_fakehw", "libx264"})
    )
    monkeypatch.setattr(preprocess, "encoder_works", lambda name: True)
    encoding = _encoding(long_edge=320, video_codecs=("h264_fakehw", "libx264"))
    built = preprocess.extract_visual_review_preview(
        source, tmp_path / "out.mp4", audio_stream_index=None, encoding=encoding
    )
    assert built.encoding == "libx264/veryfast/crf23/le320"
    assert (built.width, built.height, built.audio_stream_count) == (320, 180, 0)


def test_a_timed_out_hardware_preview_is_not_retried(
    tmp_path: Path, runner: _Runner, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _clip(tmp_path, "source.mov")
    launched: list[list[str]] = []

    def stuck(command: list[str], priority: int | None, **_: Any) -> None:
        launched.append(command)
        raise preprocess.ProcessTimeout(command, 1.0, "미리보기 만들기")

    monkeypatch.setattr(preprocess, "available_encoders", lambda: frozenset({"h264_x", "libx264"}))
    monkeypatch.setattr(preprocess, "encoder_works", lambda name: True)
    monkeypatch.setattr(preprocess, "_run_niced", stuck)
    with pytest.raises(preprocess.ProcessTimeout):
        preprocess.extract_visual_review_preview(
            source,
            tmp_path / "out.mp4",
            audio_stream_index=None,
            encoding=_encoding(video_codecs=("h264_x", "libx264")),
        )
    assert len(launched) == 1


# ── 동시 실행 상한 ────────────────────────────────────────────────────────────


def test_cpu_encode_threads_share_the_cores_between_the_local_media_slots() -> None:
    share = LocalMediaConcurrency().encode_threads
    # 한도 = max(1, min(3, CPU // 2)) — 몫 = CPU // 한도, 적어도 1
    assert [share(n) for n in (None, 0, 1, 2, 4, 6, 8, 10, 64)] == [
        None, None, 1, 2, 2, 2, 2, 3, 21,
    ]  # fmt: skip
    for cpus in range(1, 65):
        slots = LocalMediaConcurrency().limit(cpus)
        assert slots * (share(cpus) or 0) <= max(cpus, slots)  # 자리가 모두 차도 코어를 넘지 않는다


def test_render_parts_carry_the_thread_share_only_for_libx264() -> None:
    target = preprocess.RenderTarget(width=1920, height=1080, frame_duration=Fraction(1, 30))

    def encoding(codec: str, threads: int | None) -> preprocess.RenderEncoding:
        return preprocess.RenderEncoding(
            video_codec=codec, crf=20, preset="medium", hardware_bits_per_pixel=Fraction(1, 10),
            keyframe_seconds=Fraction(2), audio_codec="aac", audio_rate=48_000, audio_channels=2,
            audio_bitrate_kbps=192, pad_color="black", track_timescale_min=10_000,
            timeout_floor_seconds=1800.0, timeout_media_ratio=6.0, threads=threads,
        )  # fmt: skip

    software = preprocess._render_video_args(encoding("libx264", 3), target)
    assert software[software.index("-threads") + 1] == "3"
    assert "-threads" not in preprocess._render_video_args(encoding("libx264", None), target)
    assert "-threads" not in preprocess._render_video_args(encoding("h264_videotoolbox", 3), target)
