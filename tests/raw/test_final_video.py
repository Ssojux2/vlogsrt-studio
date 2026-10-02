"""A7-2 최종 영상 입력 — 신원·검증 사본·청크 계획·시각 환산의 계약 검사.

오프라인 시험은 합성 PCM 위의 `FinalVideoInput`을 직접 짓고, 실추출 시험은
ffmpeg 합성 영상을 만들어 `register_final_video`의 공개 경계를 실제로 돈다.
네트워크는 없다 — 제공자는 전부 가짜고 유료 호출은 이 문서의 범위 밖이다.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw.caption_config import FinalCaptionSettings
from vlogsrt.project.raw.final_video import (
    AmbiguousAudioSelection,
    FinalVideoChanged,
    FinalVideoCopyFailed,
    FinalVideoError,
    FinalVideoInput,
    caption_chunk,
    pcm_span_cutter,
    plan_caption_chunks,
    register_final_video,
    require_unchanged,
)
from vlogsrt.project.raw.understanding import PCM_SAMPLE_RATE, SampleSpan

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def make_input(
    tmp_path: Path,
    *,
    seconds: float = 40.0,
    channels: int = 1,
    revision: str = "a" * 64,
    origin: Fraction = Fraction(-1, 50),
    video_duration: float = 40.5,
) -> FinalVideoInput:
    """합성 PCM 위의 최종 영상 입력 — 잰 원점은 음수도 되는 실측값의 하나다."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    samples = int(seconds * PCM_SAMPLE_RATE)
    pcm = tmp_path / "caption.pcm"
    pcm.write_bytes(bytes((index * 7 + 3) % 256 for index in range(samples * channels * 2)))
    from vlogsrt.project.raw.understanding import SelectedAudio

    source = tmp_path / "final.mp4"
    body = b"synthetic-final-video"
    source.write_bytes(body)
    digest = revision if revision != "a" * 64 else hashlib.sha256(body).hexdigest()
    return FinalVideoInput(
        source_id="final_test",
        final_video_revision=digest,
        original_path=source,
        verified_copy_path=source,
        verified_copy_sha256=digest,
        manifest_path=tmp_path / "final-video.json",
        audio=SelectedAudio(
            source_id="final_test",
            source_path=source,
            source_sha256=digest,
            audio_stream_index=1,
            pcm_path=pcm,
            pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
            sample_rate=PCM_SAMPLE_RATE,
            channels=channels,
            sample_count=samples,
            pcm_sample0_file_time=origin,
        ),
        video_duration=video_duration,
        audio_stream_count=1,
    )


# ─────────────────────────────────────────────────────────────
#  청크 계획 — 전체 덮음, 꼬리 포함, 동결 상한
# ─────────────────────────────────────────────────────────────


def test_chunk_plan_covers_whole_audio_including_tail(tmp_path: Path) -> None:
    """단일 패스 커버리지 — 빈틈·겹침 없음, 마지막 청크는 남은 만큼 짧다."""
    video = make_input(tmp_path, seconds=40.0)
    short = FinalCaptionSettings(chunk_max_seconds=15.0)
    spans = plan_caption_chunks(video, short)
    assert [(span.start, span.end) for span in spans] == [
        (0, 240000),
        (240000, 480000),
        (480000, 640000),
    ]
    assert spans[-1].end - spans[-1].start == 160000  # 꼬리 — 늘리지 않는다


def test_chunk_plan_ceiling_is_frozen(tmp_path: Path) -> None:
    """180초·24MB 동결 상한을 넘는 설정은 만들 수 없다(D26·D28)."""
    video = make_input(tmp_path)
    with pytest.raises(ValueError, match="동결 상한"):
        plan_caption_chunks(video, FinalCaptionSettings(chunk_max_seconds=181.0))
    with pytest.raises(ValueError, match="동결 상한"):
        plan_caption_chunks(video, FinalCaptionSettings(chunk_max_bytes=24_000_001))


def test_chunk_offset_is_exact_sample_arithmetic(tmp_path: Path) -> None:
    """청크 오프셋은 `샘플/주파수`의 정확한 유리수다 — 가정한 길이가 아니다(§11.6)."""
    video = make_input(tmp_path, seconds=40.0)
    span = SampleSpan(240000, 480000)
    chunk = caption_chunk(video, span, 1, tmp_path / "chunk.wav")
    assert chunk.offset == 15.0
    assert chunk.duration == 15.0
    assert video.chunk_time(span) == Fraction(15)


# ─────────────────────────────────────────────────────────────
#  시각 환산 — 원점은 한 번만
# ─────────────────────────────────────────────────────────────


def test_final_time_applies_measured_origin_exactly_once(tmp_path: Path) -> None:
    """`final = 원점 + 청크 시작 샘플/주파수 + 세그먼트 시각` — 유리수로 한 번(§11.6)."""
    video = make_input(tmp_path, origin=Fraction(5, 2))  # 오디오가 영상보다 2.5초 늦게 시작
    span = SampleSpan(160000, 480000)  # 10초 지점에서 시작하는 청크
    provider_time = Fraction(3, 2)
    expected = Fraction(5, 2) + video.chunk_time(span) + provider_time
    assert video.to_final_time(video.chunk_time(span) + provider_time) == expected
    assert expected == Fraction(5, 2) + Fraction(10) + Fraction(3, 2) == Fraction(28, 2)


def test_negative_origin_keeps_leading_silence(tmp_path: Path) -> None:
    """앞무음은 보존한다 — 음수 원점을 0으로 몰아붙이지 않는다(§11.6)."""
    video = make_input(tmp_path, origin=Fraction(-1, 50))
    assert video.to_final_time(Fraction(0)) == Fraction(-1, 50)


def test_input_is_immutable(tmp_path: Path) -> None:
    """입력은 불변이다 — 리비전이 곧 신원이므로 도중 수정은 문법적으로 막힌다."""
    video = make_input(tmp_path)
    with pytest.raises(FrozenInstanceError):
        video.final_video_revision = "b" * 64  # type: ignore[misc]


# ─────────────────────────────────────────────────────────────
#  재전사 커터 — 저장 PCM의 같은 바이트에서 다시 자른다
# ─────────────────────────────────────────────────────────────


def test_pcm_span_cutter_returns_paired_exact_slices(tmp_path: Path) -> None:
    """구간 수와 조각 수가 같고, 좌표가 정확히 샘플로 되돌아간다(§11.6)."""
    video = make_input(tmp_path, seconds=40.0)
    workdir = tmp_path / "repair"
    cut = pcm_span_cutter(video, workdir)
    chunks = cut([(2.0, 6.5), (10.0, 13.0)])
    assert [chunk.index for chunk in chunks] == [0, 1]
    assert chunks[0].offset == pytest.approx(2.0)
    assert chunks[0].duration == pytest.approx(4.5)
    assert chunks[1].offset == pytest.approx(10.0)
    assert all((workdir / f"repair-{index:04d}.wav").is_file() for index in (0, 1))


def test_require_unchanged_detects_replaced_copy(tmp_path: Path) -> None:
    """등록 뒤 사본이 바뀌면 낡은 신원이다 — 실행 직전 검사가 닫는다."""
    video = make_input(tmp_path)
    require_unchanged(video)
    video.verified_copy_path.write_bytes(b"tampered")
    with pytest.raises(FinalVideoChanged):
        require_unchanged(video)


# ─────────────────────────────────────────────────────────────
#  실추출 등록 — ffmpeg 합성 최종 영상
# ─────────────────────────────────────────────────────────────


def _run_ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-y", *args],
        capture_output=True,
        check=True,
    )


@pytest.fixture
def synthetic_final(tmp_path: Path) -> Path:
    """6초 합성 최종 영상 — 정현파 오디오(뒤 3초는 조용하다)."""
    path = tmp_path / "final.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=6",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=3",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=44100:cl=mono:duration=3",
        "-filter_complex",
        "[1:a][2:a]concat=n=2:v=0:a=1",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


@pytest.fixture
def delayed_audio_final(tmp_path: Path) -> Path:
    """오디오가 영상보다 2초 늦게 시작하는 합성 최종 영상 — 원점이 양수다(§11.6)."""
    path = tmp_path / "delayed.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=320x180:rate=25:duration=8",
        "-f",
        "lavfi",
        "-itsoffset",
        "2",
        "-i",
        "sine=frequency=440:duration=4",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        str(path),
    )
    return path


@needs_ffmpeg
def test_register_final_video_verifies_copy_and_manifest(
    synthetic_final: Path, tmp_path: Path
) -> None:
    """등록은 해시 신원·검증 사본·신원 기록을 남긴다 — 원본은 손대지 않는다."""
    before = synthetic_final.read_bytes()
    video = register_final_video(synthetic_final, tmp_path / "work", source_id="final_1")
    assert video.final_video_revision == hashlib.sha256(before).hexdigest()
    assert video.verified_copy_path.is_file()
    assert video.verified_copy_sha256 == video.final_video_revision
    manifest = tmp_path / "work" / "final-video" / "final-video.json"
    assert manifest.is_file()
    assert "final_video_revision" in manifest.read_text(encoding="utf-8")
    assert video.audio.sample_rate == PCM_SAMPLE_RATE
    assert video.audio.sample_count > 5 * PCM_SAMPLE_RATE  # 거의 전체 길이
    assert synthetic_final.read_bytes() == before  # 원본 불가침


@needs_ffmpeg
def test_register_reuses_verified_copy_without_duplicates(
    synthetic_final: Path, tmp_path: Path
) -> None:
    """같은 해시의 사본이 이미 있으면 다시 복사하지 않는다(§11.1)."""
    first = register_final_video(synthetic_final, tmp_path / "work", source_id="final_1")
    copied = first.verified_copy_path.read_bytes()
    second = register_final_video(synthetic_final, tmp_path / "work", source_id="final_1")
    assert second.verified_copy_path == first.verified_copy_path
    assert second.verified_copy_path.read_bytes() == copied  # 중복 복사 없음


@needs_ffmpeg
def test_reregistration_after_overwrite_is_a_new_revision(
    synthetic_final: Path, tmp_path: Path
) -> None:
    """같은 파일명의 새 영상은 해시가 다르므로 새 리비전이다(§11.1)."""
    first = register_final_video(synthetic_final, tmp_path / "work", source_id="final_1")
    synthetic_final.write_bytes(synthetic_final.read_bytes() + b"-v2")
    second = register_final_video(synthetic_final, tmp_path / "work2", source_id="final_1")
    assert second.final_video_revision != first.final_video_revision


@needs_ffmpeg
def test_stale_verified_copy_candidate_is_rejected(synthetic_final: Path, tmp_path: Path) -> None:
    """해시가 다른 사본 후보 위에는 등록하지 않는다 — 반쪽 사본은 신원이 못 된다."""

    revision = hashlib.sha256(synthetic_final.read_bytes()).hexdigest()
    copy_dir = tmp_path / "work" / "final-video"
    copy_dir.mkdir(parents=True)
    stale = copy_dir / f"{revision}.mp4"
    stale.write_bytes(b"leftover-from-another-render")
    with pytest.raises(FinalVideoCopyFailed):
        register_final_video(synthetic_final, tmp_path / "work", source_id="final_1")
    assert not (copy_dir / "final-video.json").exists()  # 실패는 기록을 남기지 않는다


@needs_ffmpeg
def test_no_audio_video_is_a_distinct_state(tmp_path: Path) -> None:
    """오디오 없는 최종 영상은 `no_audio` — 무발화·실패와 다른 갈래다(§11.11)."""
    silent = tmp_path / "video_only.mp4"
    _run_ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=size=160x120:rate=25:duration=2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-an",
        str(silent),
    )
    with pytest.raises(FinalVideoError) as raised:
        register_final_video(silent, tmp_path / "work", source_id="final_silent")
    assert type(raised.value).__name__ == "FinalVideoNoAudio"
    assert "no_audio" in str(raised.value)


@needs_ffmpeg
def test_multi_track_requires_explicit_selection(synthetic_final: Path, tmp_path: Path) -> None:
    """오디오 트랙이 여럿이면 묵시적 첫 트랙이 없다 — 선택을 요구한다(§11.1)."""
    dual = tmp_path / "dual.mp4"
    _run_ffmpeg(
        "-i",
        str(synthetic_final),
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=880:duration=6",
        "-map",
        "0:v",
        "-map",
        "0:a",
        "-map",
        "1:a",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        str(dual),
    )
    with pytest.raises(AmbiguousAudioSelection):
        register_final_video(dual, tmp_path / "work", source_id="final_dual")
    chosen = register_final_video(dual, tmp_path / "work", source_id="final_dual", audio_ordinal=1)
    assert chosen.audio.audio_stream_index >= 2  # 전역 번호 — 영상(0)·오디오 둘 다 지나침


@needs_ffmpeg
def test_delayed_audio_origin_is_measured_positive(
    delayed_audio_final: Path, tmp_path: Path
) -> None:
    """늦게 시작하는 오디오의 원점은 실측 양수다 — 환산에 정확히 한 번만 들어간다."""
    video = register_final_video(delayed_audio_final, tmp_path / "work", source_id="final_d")
    origin = video.audio.pcm_sample0_file_time
    assert origin > 1  # 컨테이너가 민 지연을 디코드 원점이 잰다
    normalized = Fraction(0)
    assert video.to_final_time(normalized) == origin


@needs_ffmpeg
def test_missing_source_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(FinalVideoError, match="없다"):
        register_final_video(tmp_path / "absent.mp4", tmp_path, source_id="final_x")
