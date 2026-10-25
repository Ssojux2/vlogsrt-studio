"""W-30c1 — 분석 창마다 Gemini가 볼 짧은 영상 클립(그림 + 소리)을 로컬에서 자른다(D85).

무료·로컬 ffmpeg 한 번이다(유료 호출 0회). 시험이 못 박는 것:

- 클립은 원본의 한 구간(파일 시각)이고 고른 소리 트랙 하나를 싣는다(소리 없는 원본은 영상만).
- 긴 변만 조리법 한도로 줄이고(짝수 치수, 키우지 않음) 크기 상한을 넘으면 실패로 닫는다.
- 같은 원본·구간·조리법이면 **같은 바이트**다 — 요청 신원(`luna_request_sha256`)이 바이트를
  해시하므로 다시 자른 클립이 달라지면 이미 산 판정을 다시 사게 된다.
- 표본기는 캐시를 쓴다 — 두 번째 요청은 ffmpeg을 부르지 않고, 망가진 캐시는 다시 자른다.
- 조리법 이름(tag)은 바이트를 정하는 칸만 싣는다(크기 상한·시간 한도는 싣지 않는다).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import media_refs
from vlogsrt.project.raw.analysis_config import ANALYSIS_CLIP, AnalysisClipRecipe

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


def synthetic(tmp_path: Path, *, size: str = "1280x720", audio: bool = True) -> Path:
    source = tmp_path / f"source-{size}-{int(audio)}.mov"
    command = [
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={size}:rate=25:duration=6",
    ]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=6"]
    command += ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "pcm_s16le"]
    command += ["-t", "6", str(source)]
    subprocess.run(command, check=True, capture_output=True)
    return source


def cut(
    source: Path, out: Path, *, audio: int | None, recipe: AnalysisClipRecipe = ANALYSIS_CLIP
) -> Path:
    probe = preprocess.probe_raw_media(source)
    return preprocess.extract_analysis_clip(
        source,
        out,
        video_stream_index=probe.video.stream_index,
        audio_stream_index=audio,
        start=Fraction(1),
        seconds=Fraction(2),
        encoding=media_refs.clip_encoding(recipe),
        timeout_seconds=recipe.timeout(Fraction(2)),
    )


def audio_index(source: Path) -> int:
    return preprocess.probe_raw_media(source).audio[0].stream_index


# ── 조리법 ───────────────────────────────────────────────────────────────


def test_the_recipe_tag_names_only_what_decides_the_bytes() -> None:
    assert ANALYSIS_CLIP.tag() == (
        "analysis-clip/v1/edge=640/libx264/veryfast/crf30/yuv420p/aac/64k/ac1"
    )
    assert replace(ANALYSIS_CLIP, max_bytes=1).tag() == ANALYSIS_CLIP.tag()
    assert replace(ANALYSIS_CLIP, timeout_floor_seconds=1.0).tag() == ANALYSIS_CLIP.tag()
    assert replace(ANALYSIS_CLIP, crf=28).tag() != ANALYSIS_CLIP.tag()


@pytest.mark.parametrize(
    "changes",
    [
        {"long_edge": 0},
        {"crf": -1},
        {"audio_channels": 0},
        {"max_bytes": 0},
        {"audio_bitrate_kbps": 0},
    ],
)
def test_a_recipe_outside_its_contract_is_refused(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        replace(ANALYSIS_CLIP, **changes)  # type: ignore[arg-type]


def test_the_clip_timeout_grows_with_the_clip_and_has_a_floor() -> None:
    assert ANALYSIS_CLIP.timeout(Fraction(2)) == ANALYSIS_CLIP.timeout_floor_seconds
    assert ANALYSIS_CLIP.timeout(Fraction(1000)) == 1000 * ANALYSIS_CLIP.timeout_media_ratio


# ── 자르기 ───────────────────────────────────────────────────────────────


@needs_ffmpeg
def test_a_clip_is_the_window_range_with_one_mono_aac_track_and_a_small_long_edge(
    tmp_path: Path,
) -> None:
    source = synthetic(tmp_path)
    clip = cut(source, tmp_path / "clip.mp4", audio=audio_index(source))
    probe = preprocess.probe_raw_media(clip)
    assert (probe.video.width, probe.video.height) == (640, 360)
    assert len(probe.audio) == 1 and probe.audio[0].codec_name == "aac"
    assert probe.audio[0].channels == 1
    duration = float(probe.duration)
    assert 1.9 <= duration <= 2.1
    assert clip.stat().st_size < ANALYSIS_CLIP.max_bytes


@needs_ffmpeg
def test_cutting_twice_gives_the_same_bytes(tmp_path: Path) -> None:
    source = synthetic(tmp_path)
    first = cut(source, tmp_path / "a.mp4", audio=audio_index(source))
    second = cut(source, tmp_path / "b.mp4", audio=audio_index(source))
    assert first.read_bytes() == second.read_bytes()


@needs_ffmpeg
def test_a_window_after_the_sound_ends_is_a_video_only_clip(tmp_path: Path) -> None:
    """소리가 영상보다 일찍 끝나는 원본 — 그 뒤의 창 클립은 소리 없이 성공한다(그 구간에 소리가
    없다)."""
    source = tmp_path / "short-sound.mov"
    subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=8",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(source),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    probe = preprocess.probe_raw_media(source)
    clip = preprocess.extract_analysis_clip(
        source,
        tmp_path / "late.mp4",
        video_stream_index=probe.video.stream_index,
        audio_stream_index=probe.audio[0].stream_index,
        start=Fraction(5),
        seconds=Fraction(2),
        encoding=media_refs.clip_encoding(),
        timeout_seconds=ANALYSIS_CLIP.timeout(Fraction(2)),
    )
    assert preprocess.probe_raw_media(clip).audio == ()


@needs_ffmpeg
def test_a_silent_source_gives_a_video_only_clip(tmp_path: Path) -> None:
    source = synthetic(tmp_path, audio=False)
    clip = cut(source, tmp_path / "clip.mp4", audio=None)
    assert preprocess.probe_raw_media(clip).audio == ()


@needs_ffmpeg
def test_a_portrait_source_keeps_its_orientation_and_even_edges(tmp_path: Path) -> None:
    source = synthetic(tmp_path, size="721x1281", audio=False)
    probe = preprocess.probe_raw_media(cut(source, tmp_path / "clip.mp4", audio=None))
    assert probe.video.height == 640 and probe.video.width < probe.video.height
    assert probe.video.width % 2 == 0 and probe.video.height % 2 == 0


@needs_ffmpeg
def test_a_clip_over_the_size_cap_fails_and_leaves_nothing(tmp_path: Path) -> None:
    source = synthetic(tmp_path)
    out = tmp_path / "clip.mp4"
    with pytest.raises(RuntimeError, match="크기 상한"):
        cut(source, out, audio=audio_index(source), recipe=replace(ANALYSIS_CLIP, max_bytes=1000))
    assert not out.exists()
    assert not list(tmp_path.glob(".clip.mp4.*"))


@needs_ffmpeg
def test_an_existing_output_is_never_overwritten(tmp_path: Path) -> None:
    source = synthetic(tmp_path)
    out = tmp_path / "clip.mp4"
    out.write_bytes(b"keep")
    with pytest.raises(preprocess.RawStagingError):
        cut(source, out, audio=audio_index(source))
    assert out.read_bytes() == b"keep"


# ── 표본기 캐시 ───────────────────────────────────────────────────────────


@needs_ffmpeg
def test_the_clip_sampler_caches_by_range_and_recuts_a_broken_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = synthetic(tmp_path)
    calls: list[tuple[Fraction, Fraction]] = []
    real = preprocess.extract_analysis_clip

    def counting(*args: object, **kwargs: object) -> Path:
        calls.append((kwargs["start"], kwargs["seconds"]))  # type: ignore[arg-type]
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(preprocess, "extract_analysis_clip", counting)
    sampler = media_refs.ClipSampler(
        source=source,
        video_stream_index=preprocess.probe_raw_media(source).video.stream_index,
        audio_stream_index=audio_index(source),
        cache_directory=tmp_path / "cache",
    )
    first = sampler.clip(start=Fraction(1), seconds=Fraction(2))
    again = sampler.clip(start=Fraction(1), seconds=Fraction(2))
    assert first == again and calls == [(Fraction(1), Fraction(2))]
    other = sampler.clip(start=Fraction(3), seconds=Fraction(2))
    assert other != first and len(calls) == 2
    first.write_bytes(b"broken")  # 원장(sha256)과 다르다 — 다시 자른다
    recut = sampler.clip(start=Fraction(1), seconds=Fraction(2))
    assert len(calls) == 3 and recut.read_bytes() != b"broken"


def test_the_clip_cache_folder_is_keyed_by_source_recipe_and_audio_track(tmp_path: Path) -> None:
    key = media_refs.clip_cache_key
    base = key("a" * 64, audio_stream_index=1)
    assert base == key("a" * 64, audio_stream_index=1)
    assert base != key("b" * 64, audio_stream_index=1)
    assert base != key("a" * 64, audio_stream_index=2)
    assert base != key("a" * 64, audio_stream_index=None)
    assert base != key("a" * 64, audio_stream_index=1, recipe=replace(ANALYSIS_CLIP, crf=28))
