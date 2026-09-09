"""비전 프레임 샘플 — 원본/프록시 대응과 결정적 추출의 계약 (P5-04).

네 가지 선을 증명한다:

- **짝은 연결로만 선다.** 원본/프록시 짝은 `ResolvedMedia`가 싣는 미디어-rep
  연결뿐이고 파일 이름 유사성은 후보조차 아니다(P1-03). 프록시가 없으면 원본에서
  뽑는다 — 그건 폴백이 아니라 원래 길이다.
- **시계는 정확 유리수다.** `source_time`은 공통 파일 시계(소스 격자 인덱스 ×
  프레임 길이)이고 원본 매핑은 `(source_time − 원본 원점) / 프레임 길이`가
  `[0, 프레임 수)`의 **정확한 정수**인가 하나뿐이다. XML 애셋 시계(`asset.start`)는
  이 층의 입력이 아니다. 격자 밖·범위 밖 요청은 명시적 분석 오류이고, 원본 폴백이
  무효 요청을 구하지 못한다.
- **명목이 같아도 증명이 갈린다.** 명목 률·프레임 수가 같아도 프레임
  타임스탬프가 격자에서 뛰면 대응은 부정된다 — 원본이면 오류, 프록시면 안정
  사유와 함께 원본 폴백(아래 `*_real` 검사).
- **결정성.** 같은 요청은 같은 순서·같은 이미지 바이트·같은 해시를 낸다.
  `(source_id, source_time)`이 같은 요청은 이미지 파일 하나를 공유하고, 같은
  frame_id의 같은 시각 중복은 한 장으로 접히며, 다른 시각 중복은 거부한다.

가짜 경계 검사는 probe/extract를 sampling 이름공간에서 갈아끼워 오케스트레이션
(대응·매핑·중복)만 증명한다. 탐색 argv·스케일·격자 증명 산수는
`tests/test_preprocess.py`가 증명하고, **디코드된 이미지가 진짜 대응 프레임**인
것은 아래 합성 미디어 검사가 증명한다 — 탐색 argv만으로는 부족하다(브리프 2단계).
프록시는 원본과 같은 인코딩을 `-itsoffset`으로 옮겨 담아 프레임 바이트가 같으므로,
같은 인덱스의 추출 바이트가 일치하면 대응이 증명된다. 이것은 시각 대응의 증명이지
프록시 화소가 원본과 같다는 주장이 아니다(브리프 — 실제 4K/프록시 색 충실도는
PENDING_HUMAN).
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.preprocess import VideoStreamInfo, extract_frame_index, probe_video_stream
from vlogsrt.project.config import ProjectConfig
from vlogsrt.project.media import ResolvedMedia, sha256_file
from vlogsrt.project.vision import sampling

CFG = ProjectConfig()
ASSET = "asset-content-1"
FD25 = Fraction(1, 25)


def _info(
    origin: Fraction, *, frame_duration: Fraction = FD25, frame_count: int = 25, index: int = 0
) -> VideoStreamInfo:
    return VideoStreamInfo(
        stream_index=index,
        presentation_origin=origin,
        frame_duration=frame_duration,
        frame_count=frame_count,
    )


def _media(tmp_path: Path, *, with_proxy: bool = False, asset: str = ASSET) -> ResolvedMedia:
    """해석이 끝난 미디어 — 프록시 짝은 이 레코드의 연결(image_source)이 전부다."""
    original = tmp_path / "original.mov"
    original.write_bytes(b"original-bytes")
    proxy = tmp_path / "proxy.mov"
    proxy.write_bytes(b"proxy-bytes")
    return ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=asset,
        original=original,
        image_source=proxy if with_proxy else original,
        original_sha256="0" * 64,
        has_audio=True,
        used_proxy=with_proxy,
    )


def _request(
    frame_id: str, source_time: Fraction, *, source_id: str = ASSET
) -> sampling.FrameSampleRequest:
    return sampling.FrameSampleRequest(
        frame_id=frame_id, source_id=source_id, source_time=source_time
    )


@dataclass
class _Boundary:
    """가짜 probe/extract가 본 호출 기록 — 어느 파일을 몇 번 봤는지가 계약이다."""

    probes: list[tuple[Path, int]]
    extractions: list[tuple[Path, int, int, int]]

    def extract_paths(self) -> list[Path]:
        return [path for path, _, _, _ in self.extractions]


def _fake_boundary(
    monkeypatch: pytest.MonkeyPatch,
    media: ResolvedMedia,
    *,
    original: VideoStreamInfo,
    proxy: VideoStreamInfo | None = None,
    proxy_error: RuntimeError | None = None,
) -> _Boundary:
    """probe/extract를 흉내 낸다. 바이트는 프레임 번호에서만 온다(해시 검사용)."""
    boundary = _Boundary(probes=[], extractions=[])

    def probe(source: Path, stream_index: int = 0) -> VideoStreamInfo:
        boundary.probes.append((Path(source), stream_index))
        if Path(source) == media.original:
            return original
        if proxy_error is not None:
            raise proxy_error
        if proxy is None:
            raise AssertionError(f"프록시 아닌 파일을 프로브했다: {source}")
        return proxy

    def extract(
        source: Path,
        stream_index: int,
        frame_index: int,
        destination: Path,
        *,
        long_edge: int,
    ) -> Path:
        boundary.extractions.append((Path(source), stream_index, frame_index, long_edge))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(f"frame:{frame_index}".encode())
        return destination

    monkeypatch.setattr(sampling, "probe_video_stream", probe)
    monkeypatch.setattr(sampling, "extract_frame_index", extract)
    return boundary


# ─────────────────────────────────────────────────────────────
#  짝과 대응 — 연결만이 짝을 만든다
# ─────────────────────────────────────────────────────────────


def test_the_pair_comes_only_from_the_resolved_association(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프록시 탐색·추출은 image_source가 가리키는 파일에서만 일어난다."""
    media = _media(tmp_path, with_proxy=True)
    boundary = _fake_boundary(
        monkeypatch, media, original=_info(Fraction(5)), proxy=_info(Fraction(11))
    )

    samples = sampling.sample_frames(media, [_request("f0", Fraction(5))], tmp_path / "out", CFG)

    assert [path for path, _ in boundary.probes] == [media.original, media.image_source]
    assert boundary.extract_paths() == [media.image_source]
    assert samples[0].used_proxy


def test_an_absent_proxy_samples_the_original_without_any_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프록시가 없으면 원본이 원래 길이다 — 폴백 사유 같은 건 없다."""
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(0)))

    samples = sampling.sample_frames(
        media, [_request("f0", Fraction(2, 25))], tmp_path / "out", CFG
    )

    assert [path for path, _ in boundary.probes] == [media.original]
    assert boundary.extract_paths() == [media.original]
    assert samples[0].used_proxy is False
    assert samples[0].review == ()


def test_correspondence_reports_incompatibility_without_dropping_the_proxy_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """프로브는 됐고 호환이 안 된다 — proxy 시계는 남기고 사유만 단다."""
    media = _media(tmp_path, with_proxy=True)
    _fake_boundary(
        monkeypatch, media, original=_info(Fraction(5)), proxy=_info(Fraction(11), frame_count=24)
    )

    pair = sampling.establish_correspondence(media)

    assert pair.proxy == _info(Fraction(11), frame_count=24)
    assert pair.proxy_usable is False
    assert pair.review == ("proxy_frame_count_mismatch",)


# ─────────────────────────────────────────────────────────────
#  시계 — 정확 유리수 공통 시계 매핑
# ─────────────────────────────────────────────────────────────


def test_the_clock_example_maps_request_time_to_the_same_decoded_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """브리프의 시계 예: 원본 원점 5, 프록시 원점 11, 25fps.

    source_time 5(= 소스 격자 125 × 1/25)는 원본·프록시 **인덱스 0**이고 선택된
    프록시 PTS는 11이다. asset.start=300/clip.start=305 같은 XML 애셋 시계는 P6의
    일이다 — 이 층은 파일 시계만 받는다. 오디오 첫 PTS(예: 5.064)는 독립 원점일
    뿐 비디오 격자를 다시 세우지 않는다.
    """
    media = _media(tmp_path, with_proxy=True)
    _fake_boundary(monkeypatch, media, original=_info(Fraction(5)), proxy=_info(Fraction(11)))

    samples = sampling.sample_frames(media, [_request("f125", Fraction(5))], tmp_path / "out", CFG)

    (sample,) = samples
    assert sample.original_frame_index == 0
    assert sample.selected_frame_index == 0
    assert sample.selected_presentation_time == Fraction(11)
    assert sample.used_proxy


def test_a_three_frame_offset_lands_on_the_same_index_in_both_clocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본 원점 0, 프록시 원점 3프레임(3/25초), 요청 3/25초 → 양쪽 인덱스 3.

    선택 PTS는 프록시 자기 원점에 얹은 6/25초다 — 원점 차이를 흡수하거나
    추측하지 않는다.
    """
    media = _media(tmp_path, with_proxy=True)
    _fake_boundary(monkeypatch, media, original=_info(Fraction(0)), proxy=_info(Fraction(3, 25)))

    samples = sampling.sample_frames(
        media, [_request("f3", Fraction(3, 25))], tmp_path / "out", CFG
    )

    (sample,) = samples
    assert sample.original_frame_index == 3
    assert sample.selected_frame_index == 3
    assert sample.selected_presentation_time == Fraction(6, 25)


def test_first_and_last_frame_are_both_in_range(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """경계 프레임: 인덱스 0과 frame_count−1. 선택 PTS는 원점+인덱스×프레임 길이."""
    media = _media(tmp_path)
    _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))

    samples = sampling.sample_frames(
        media,
        [_request("first", Fraction(5)), _request("last", Fraction(5) + 24 * FD25)],
        tmp_path / "out",
        CFG,
    )

    assert [sample.original_frame_index for sample in samples] == [0, 24]
    assert samples[1].selected_presentation_time == Fraction(5) + 24 * FD25


def test_an_off_grid_request_time_is_an_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """격자 밖 요청 — 원본 매핑 자체가 무효다. 폴백이 이걸 구하지 못한다."""
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))

    with pytest.raises(sampling.FrameSamplingError, match="격자"):
        sampling.sample_frames(
            media, [_request("f", Fraction(5) + Fraction(1, 50))], tmp_path / "out", CFG
        )
    assert boundary.extractions == []


@pytest.mark.parametrize("bad_time", [Fraction(5) + 25 * FD25, Fraction(5, 1) - FD25])
def test_an_out_of_range_request_is_an_explicit_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_time: Fraction
) -> None:
    """범위 밖 — 마지막 프레임 다음(25)과 첫 프레임 앞(−1)."""
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))

    with pytest.raises(sampling.FrameSamplingError, match="범위"):
        sampling.sample_frames(media, [_request("f", bad_time)], tmp_path / "out", CFG)
    assert boundary.extractions == []


def test_source_id_must_be_the_asset_content_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """다른 내용 신원의 요청은 위조다 — 프로브보다 먼저 닫힌다."""
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(0)))

    with pytest.raises(sampling.FrameSamplingError, match="source_id"):
        sampling.sample_frames(
            media, [_request("f", Fraction(0), source_id="other-asset")], tmp_path / "out", CFG
        )
    assert boundary.probes == []


def test_a_float_request_time_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """정확 유리수 규율 — float 시각은 이 계층에 들어오지 못한다."""
    media = _media(tmp_path)
    _fake_boundary(monkeypatch, media, original=_info(Fraction(0)))

    with pytest.raises(sampling.FrameSamplingError, match="Fraction"):
        sampling.sample_frames(
            media,
            [_request("f", 0.04)],
            tmp_path / "out",
            CFG,  # type: ignore[arg-type]
        )


# ─────────────────────────────────────────────────────────────
#  증명 실패 — 원본은 오류, 프록시는 폴백
# ─────────────────────────────────────────────────────────────


def test_an_unproven_original_is_an_error_even_with_a_healthy_proxy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """원본 그리드가 증명되지 않으면 그 미디어의 프레임 시계는 없다."""
    media = _media(tmp_path, with_proxy=True)
    probes: list[Path] = []

    def broken_probe(source: Path, stream_index: int = 0) -> VideoStreamInfo:
        probes.append(Path(source))
        raise RuntimeError("디코드 프레임 기록이 없습니다")

    monkeypatch.setattr(sampling, "probe_video_stream", broken_probe)

    with pytest.raises(sampling.FrameSamplingError, match="원본"):
        sampling.sample_frames(media, [_request("f", Fraction(5))], tmp_path / "out", CFG)
    assert probes == [media.original]  # 프록시는 아예 보지 못한다 — 원본부터 닫힌다


def test_an_unproven_proxy_falls_back_with_a_stable_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """명목 률·개수가 같아도 프록시 타임스탬프가 뛰면 증명은 실패 → 원본 폴백."""
    media = _media(tmp_path, with_proxy=True)
    boundary = _fake_boundary(
        monkeypatch,
        media,
        original=_info(Fraction(5)),
        proxy_error=RuntimeError("프레임 타임스탬프가 원점+순번 격자를 벗어났습니다"),
    )

    samples = sampling.sample_frames(media, [_request("f", Fraction(5))], tmp_path / "out", CFG)

    (sample,) = samples
    assert sample.used_proxy is False
    assert sample.review == ("proxy_unproven",)
    assert boundary.extract_paths() == [media.original]
    assert sample.selected_presentation_time == Fraction(5)


def test_a_proxy_rate_mismatch_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media(tmp_path, with_proxy=True)
    boundary = _fake_boundary(
        monkeypatch,
        media,
        original=_info(Fraction(5)),
        proxy=_info(Fraction(11), frame_duration=Fraction(1, 24)),
    )

    samples = sampling.sample_frames(media, [_request("f", Fraction(5))], tmp_path / "out", CFG)

    assert samples[0].used_proxy is False
    assert samples[0].review == ("proxy_frame_duration_mismatch",)
    assert boundary.extract_paths() == [media.original]


def test_a_proxy_count_mismatch_falls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media = _media(tmp_path, with_proxy=True)
    boundary = _fake_boundary(
        monkeypatch, media, original=_info(Fraction(5)), proxy=_info(Fraction(11), frame_count=26)
    )

    samples = sampling.sample_frames(media, [_request("f", Fraction(5))], tmp_path / "out", CFG)

    assert samples[0].used_proxy is False
    assert samples[0].review == ("proxy_frame_count_mismatch",)
    assert boundary.extract_paths() == [media.original]


# ─────────────────────────────────────────────────────────────
#  중복·순서·결정성
# ─────────────────────────────────────────────────────────────


def test_one_artifact_per_source_time_and_first_seen_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 (source_id, source_time)은 이미지 하나를 공유 — 프레임 아이디마다 결과 하나."""
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))
    same, other = Fraction(5), Fraction(5) + 2 * FD25

    samples = sampling.sample_frames(
        media,
        [_request("a", same), _request("b", same), _request("c", other), _request("a", same)],
        tmp_path / "out",
        CFG,
    )

    assert [sample.frame_id for sample in samples] == ["a", "b", "c"]
    assert samples[0].image_path == samples[1].image_path
    assert samples[0].image_sha256 == samples[1].image_sha256
    assert samples[2].image_path != samples[0].image_path
    assert len(boundary.extractions) == 2  # 시각 두 개 — 추출도 두 번


def test_an_identical_duplicate_frame_id_collapses_to_one_sample(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))

    samples = sampling.sample_frames(
        media,
        [_request("a", Fraction(5)), _request("a", Fraction(5))],
        tmp_path / "out",
        CFG,
    )

    assert len(samples) == 1
    assert len(boundary.extractions) == 1


def test_a_duplicate_frame_id_with_a_different_time_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _media(tmp_path)
    boundary = _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))

    with pytest.raises(sampling.FrameSamplingError, match="같은 아이디|서로 다른 시각"):
        sampling.sample_frames(
            media,
            [_request("a", Fraction(5)), _request("a", Fraction(6, 25))],
            tmp_path / "out",
            CFG,
        )
    assert boundary.extractions == []


def test_repeated_runs_produce_identical_names_and_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 입력은 같은 순서·같은 이름·같은 해시를 낸다(브리프 수용 조건)."""
    media = _media(tmp_path)
    _fake_boundary(monkeypatch, media, original=_info(Fraction(5)))

    first = sampling.sample_frames(
        media, [_request("a", Fraction(5)), _request("b", Fraction(136, 25))], tmp_path / "one", CFG
    )
    second = sampling.sample_frames(
        media, [_request("a", Fraction(5)), _request("b", Fraction(136, 25))], tmp_path / "two", CFG
    )

    assert [sample.image_path.name for sample in first] == [
        sample.image_path.name for sample in second
    ]
    assert [sample.image_sha256 for sample in first] == [sample.image_sha256 for sample in second]


# ─────────────────────────────────────────────────────────────
#  합성 미디어 — 디코드된 프레임이 대응 프레임이다
# ─────────────────────────────────────────────────────────────
#
# 프레임마다 색(hue)이 다른 25fps 합성 영상을 만든다. 프록시는 같은 인코딩을
# `-itsoffset`으로 옮겨 담는다 — 프레임 바이트가 원본과 같고 PTS 원점만 다르다.
# 같은 인덱스의 추출 바이트가 원본 직접 추출과 일치하면(그리고 이웃 프레임과
# 다르면) 선택된 디코드 프레임이 대응 프레임임이 증명된다.

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _authored_video(directory: Path, name: str, *, origin_seconds: int) -> Path:
    """프레임 n의 색이 hue n*37°인 25fps 25프레임 영상. PTS 원점은 origin_seconds초."""
    media = directory / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:size=64x64:rate=25:duration=1",
            "-vf", f"hue=h='n*37',setpts=PTS+{origin_seconds}/TB",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return media


def _muxed_with_audio_origin(directory: Path, name: str, *, origin_seconds: int) -> Path:
    """같은 영상 + 첫 PTS가 0.064초 늦은 오디오 — 오디오 원점은 비디오 격자를 못 움직인다."""
    media = directory / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:size=64x64:rate=25:duration=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-filter_complex",
            f"[0:v]hue=h='n*37',setpts=PTS+{origin_seconds}/TB[v];"
            f"[1:a]asetpts=PTS+{origin_seconds}.064/TB[a]",
            "-map", "[v]", "-map", "[a]",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return media


def _shifted_remux(source: Path, name: str, *, origin: Fraction) -> Path:
    """같은 인코딩을 PTS 원점 origin초로 옮겨 담는다(-itsoffset은 마이크로초로 정확).

    프록시의 프레임 바이트는 원본과 같다 — 그래서 같은 인덱스의 추출 바이트가
    일치하면 대응이 증명된다. 호출부는 origin이 1µs 정수배인 값만 쓴다.
    """
    shifted = source.parent / name
    microseconds = origin * 1_000_000
    assert microseconds.denominator == 1, f"µs 정수배여야 한다: {origin}"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-itsoffset", f"{microseconds.numerator}us",
            "-i", str(source),
            "-map", "0:v:0", "-c", "copy",
            str(shifted),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return shifted


def _hole_video(directory: Path, name: str) -> Path:
    """26프레임에서 5번을 뺀 25프레임 — 개수는 같고 타임스탬프에 구멍이 났다."""
    media = directory / name
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=red:size=64x64:rate=25:duration=1.04",
            "-vf", "hue=h='n*37',select='not(eq(n,5))'",
            "-fps_mode", "passthrough",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            str(media),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip
    return media


def _direct_image_bytes(source: Path, frame_index: int, destination: Path) -> bytes:
    extract_frame_index(source, 0, frame_index, destination, long_edge=CFG.vision_long_edge)
    return destination.read_bytes()


@needs_ffmpeg
def test_measured_origins_survive_the_authored_shifts_real(tmp_path: Path) -> None:
    """프로브는 측정 원점을 그대로 돌려준다 — 원본 5초, 옮겨 담은 프록시 11초."""
    original = _authored_video(tmp_path, "orig5.mov", origin_seconds=5)
    proxy = _shifted_remux(original, "proxy11.mov", origin=Fraction(11))

    assert probe_video_stream(original) == VideoStreamInfo(
        stream_index=0, presentation_origin=Fraction(5),
        frame_duration=FD25, frame_count=25,
    )  # fmt: skip
    assert probe_video_stream(proxy) == VideoStreamInfo(
        stream_index=0, presentation_origin=Fraction(11),
        frame_duration=FD25, frame_count=25,
    )  # fmt: skip


@needs_ffmpeg
def test_the_epoch_example_selects_the_corresponding_decoded_frame_real(
    tmp_path: Path,
) -> None:
    """시계 예(300/5/11)의 디코드 증명: source_time 5 → 원본·프록시 인덱스 0.

    프록시 원점이 6초 달라도 같은 인덱스를 프록시 자기 격자에서 뽑는다. 프록시
    추출 바이트가 원본 직접 추출과 같고 이웃 프레임(인덱스 1)과 다르다 — 대응이
    증명된다.
    """
    original = _authored_video(tmp_path, "orig5.mov", origin_seconds=5)
    proxy = _shifted_remux(original, "proxy11.mov", origin=Fraction(11))
    media = ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=ASSET,
        original=original,
        image_source=proxy,
        original_sha256=sha256_file(original),
        has_audio=True,
        used_proxy=True,
    )

    samples = sampling.sample_frames(media, [_request("f125", Fraction(5))], tmp_path / "out", CFG)

    (sample,) = samples
    assert sample.used_proxy
    assert (sample.original_frame_index, sample.selected_frame_index) == (0, 0)
    assert sample.selected_presentation_time == Fraction(11)
    assert sample.image_sha256 == sha256_file(sample.image_path)
    assert sample.image_path.read_bytes() == _direct_image_bytes(
        original, 0, tmp_path / "direct0.jpg"
    )
    assert sample.image_path.read_bytes() != _direct_image_bytes(
        original, 1, tmp_path / "direct1.jpg"
    )


@needs_ffmpeg
def test_a_three_frame_offset_selects_frame_three_real(tmp_path: Path) -> None:
    """원본 원점 0, 프록시 원점 3/25초, 요청 3/25초 → 디코드 인덱스 3이 뽑힌다."""
    original = _authored_video(tmp_path, "orig0.mov", origin_seconds=0)
    proxy = _shifted_remux(original, "proxy3.mov", origin=Fraction(3, 25))
    media = ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=ASSET,
        original=original,
        image_source=proxy,
        original_sha256=sha256_file(original),
        has_audio=True,
        used_proxy=True,
    )

    samples = sampling.sample_frames(
        media, [_request("f3", Fraction(3, 25))], tmp_path / "out", CFG
    )

    (sample,) = samples
    assert sample.used_proxy
    assert sample.original_frame_index == 3
    assert sample.selected_presentation_time == Fraction(6, 25)
    assert sample.image_path.read_bytes() == _direct_image_bytes(
        original, 3, tmp_path / "direct3.jpg"
    )
    assert sample.image_path.read_bytes() != _direct_image_bytes(
        original, 2, tmp_path / "direct2.jpg"
    )


@needs_ffmpeg
def test_a_quantized_tick_cfr_stream_proves_and_samples_real(tmp_path: Path) -> None:
    """30000/1001을 time_base 1/1000에 담은 스트림 — 틱 델타가 33/34로 어긋나도 증명된다."""
    media_file = tmp_path / "quantized.mov"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:size=64x64:rate=30000/1001:duration=0.3",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-video_track_timescale", "1000",
            str(media_file),
        ],
        capture_output=True,
        check=True,
    )  # fmt: skip

    info = probe_video_stream(media_file)
    # 선언 률 자체는 ffprobe가 틱에서 계산한 값이라 여기 못 박지 않는다 — 정확한
    # 틱 목록 증명은 tests/test_preprocess.py의 가짜 ffprobe 검사가 맡는다.
    # 이 검사가 증명하는 건 **실제 파일**에서도 양자화된 틱이 증명을 통과한다는 것.
    assert info.presentation_origin == 0
    assert info.frame_count == 9

    media = ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=ASSET,
        original=media_file,
        image_source=media_file,
        original_sha256=sha256_file(media_file),
        has_audio=False,
        used_proxy=False,
    )
    samples = sampling.sample_frames(
        media,
        [_request("mid", 4 * info.frame_duration), _request("tail", 8 * info.frame_duration)],
        tmp_path / "out",
        CFG,
    )
    assert [sample.original_frame_index for sample in samples] == [4, 8]


@needs_ffmpeg
def test_a_genuine_frame_jump_splits_by_which_stream_is_unproven_real(
    tmp_path: Path,
) -> None:
    """같은 명목(25fps·25프레임)에 타임스탬프 구멍 — 원본이면 오류, 프록시면 폴백."""
    clean = _authored_video(tmp_path, "clean.mov", origin_seconds=0)
    holed = _hole_video(tmp_path, "holed.mov")

    with pytest.raises(RuntimeError, match="격자"):
        probe_video_stream(holed)

    original_only = ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=ASSET,
        original=holed,
        image_source=holed,
        original_sha256=sha256_file(holed),
        has_audio=False,
        used_proxy=False,
    )
    with pytest.raises(sampling.FrameSamplingError, match="원본"):
        sampling.sample_frames(original_only, [_request("f0", Fraction(0))], tmp_path / "a", CFG)

    with_proxy = ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=ASSET,
        original=clean,
        image_source=holed,
        original_sha256=sha256_file(clean),
        has_audio=False,
        used_proxy=True,
    )
    (sample,) = sampling.sample_frames(
        with_proxy, [_request("f0", Fraction(0))], tmp_path / "b", CFG
    )
    assert sample.used_proxy is False
    assert sample.review == ("proxy_unproven",)
    assert sample.image_path.read_bytes() == _direct_image_bytes(clean, 0, tmp_path / "c0.jpg")


@needs_ffmpeg
def test_an_audio_origin_does_not_rebase_the_video_grid_real(tmp_path: Path) -> None:
    """오디오 첫 PTS(5.064초)는 독립 원점이다 — 비디오 격자 원점은 정확히 5초다."""
    media_file = _muxed_with_audio_origin(tmp_path, "av.mov", origin_seconds=5)

    info = probe_video_stream(media_file)
    assert info.presentation_origin == Fraction(5)
    assert info.frame_duration == FD25
    assert info.frame_count == 25

    media = ResolvedMedia(
        resource_scoped_id="doc1/r1",
        asset_content_id=ASSET,
        original=media_file,
        image_source=media_file,
        original_sha256=sha256_file(media_file),
        has_audio=True,
        used_proxy=False,
    )
    (sample,) = sampling.sample_frames(
        media, [_request("f1", Fraction(5) + FD25)], tmp_path / "out", CFG
    )
    assert sample.original_frame_index == 1
