"""map 원본(W-27c, D70)을 진짜 ffmpeg로 — 추출이 파이썬 대응과 프레임마다 같은가.

합성 재료(1/600 시계, 아이폰과 같은 time_base): 프레임 하나를 떨어뜨린 원본(빈 슬롯 — 앞
프레임을 붙잡는다), 25fps 2초 + 30fps 1초 이어 붙이기(명목 25fps — 30fps 토막은 한 슬롯 창에 두
패킷), 한 패킷을 정확히 반 프레임 민 원본(동점은 뒤 슬롯), 30fps 재료인데 ffprobe가 r_frame_rate를
30000/1001로 읽는 원본(다시 고른 snap). 추출물의 기대값은 파이썬이 `slot_packets`대로 원본 디코드
프레임을 늘어놓아 무손실로 다시 감싼 **기준 원본**을 같은 프로파일로 균일하게 뽑은 것이다 — 인코더가
같은 프레임을 받으면 같은 바이트를 낸다(W-27b 쌍둥이 시험과 같은 근거). 유료 호출 0회.
"""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_grid_conform import _assert_same_extraction, _ffmpeg, _frame_hashes, make_plain
from vlogsrt import preprocess
from vlogsrt.project.raw import media_refs

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

WIDTH, HEIGHT = 160, 90
FRAME_BYTES = WIDTH * HEIGHT * 3 // 2  # yuv420p


def make_gap(plain: Path, path: Path) -> Path:
    """순번 5를 떨어뜨린 원본 — 순번 5(옛 6)의 pts가 120틱이라 슬롯 5가 빈다."""
    _ffmpeg(
        "-i", str(plain), "-vf", "select='not(eq(n\\,5))'", "-fps_mode", "passthrough",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "copy",
        "-video_track_timescale", "600", str(path),
    )  # fmt: skip
    return path


def make_switch(path: Path) -> Path:
    """25fps 2초 + 30fps 1초 — 프레임마다 색이 달라(hue) 이웃 프레임이 구별된다."""
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate=25:duration=2",
        "-f", "lavfi", "-i", f"testsrc2=size={WIDTH}x{HEIGHT}:rate=30:duration=1",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0,hue=h='n*37'[v]",
        "-map", "[v]", "-map", "2:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-fps_mode", "passthrough", "-c:a", "aac", "-b:a", "64k",
        "-video_track_timescale", "600", str(path),
    )  # fmt: skip
    return path


def make_shifted(plain: Path, path: Path, expression: str) -> Path:
    """같은 부호화 프레임에 pts만 옮긴 사본 — `setts`로 스트림 복사(재인코딩 없음)."""
    _ffmpeg(
        "-i", str(plain), "-map", "0", "-c", "copy",
        "-bsf:v", f"setts=pts=PTS+{expression}:dts=DTS+{expression}",
        "-video_track_timescale", "600", str(path),
    )  # fmt: skip
    return path


@pytest.fixture(scope="module")
def media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("grid_map")
    plain = make_plain(folder / "plain.mov", seconds="4")
    long_plain = make_plain(folder / "plain5.mov", seconds="5")
    return {
        "plain": plain,
        "gap": make_gap(plain, folder / "gap.mov"),
        "switch": make_switch(folder / "switch.mov"),
        "tie": make_shifted(plain, folder / "tie.mov", "10*eq(N\\,30)"),
        "plain5": long_plain,
        "ntsc": make_shifted(long_plain, folder / "ntsc.mov", "gte(N\\,45)+gte(N\\,136)"),
    }


def _clock(path: Path) -> preprocess.RawVideoClock:
    return preprocess.probe_extraction_clock(path)


def test_the_synthetic_sources_measure_as_their_real_shapes(media: dict[str, Path]) -> None:
    gap = _clock(media["gap"])
    assert (gap.conform_kind, gap.frame_count, gap.frame_duration) == ("map", 120, Fraction(1, 30))
    assert gap.slot_packets[4:7] == (4, 4, 5)  # 슬롯 5는 패킷 4를 붙잡는다(IMG_8877 모양)
    switch = _clock(media["switch"])
    assert (switch.conform_kind, switch.frame_duration, switch.frame_count) == (
        "map",
        Fraction(1, 25),
        75,
    )
    assert len(set(switch.slot_packets)) < 80  # 30fps 토막의 겹친 패킷은 가려진다(IMG_8858 모양)
    tie = _clock(media["tie"])
    assert tie.conform_kind == "map" and tie.slot_packets[29:32] == (29, 29, 31)
    ntsc = preprocess.probe_raw_media(media["ntsc"])
    assert ntsc.video.r_frame_rate == Fraction(30000, 1001)  # 실측 IMG_8861처럼 읽힌다
    remapped = _clock(media["ntsc"])
    assert (remapped.conform_kind, remapped.frame_duration, remapped.slot_packets) == (
        "snap",
        Fraction(1, 30),
        (),
    )
    for name, clock in (("gap", gap), ("switch", switch), ("tie", tie), ("ntsc", remapped)):
        preprocess._assert_raw_supported(preprocess.probe_raw_media(media[name]), clock)


# ── 기준 원본 — 파이썬 대응으로 늘어놓은 무손실 사본 ───────────────────────────────


def _decoded(path: Path) -> list[bytes]:
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0",
         "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "yuv420p", "-"],
        capture_output=True, check=True,
    )  # fmt: skip
    raw = completed.stdout
    return [raw[i : i + FRAME_BYTES] for i in range(0, len(raw), FRAME_BYTES)]


def _reference(source: Path, clock: preprocess.RawVideoClock, path: Path) -> Path:
    """슬롯마다 `slot_packets`의 프레임을 늘어놓은 균일 무손실 H.264 — 같은 1/600 시계."""
    frames = _decoded(source)
    assert len(frames) == clock.slot_packets[-1] + 1  # 패킷마다 한 프레임
    body = b"".join(frames[ordinal] for ordinal in clock.slot_packets)
    rate = 1 / clock.frame_duration
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{WIDTH}x{HEIGHT}",
         "-r", f"{rate.numerator}/{rate.denominator}", "-i", "-",
         "-c:v", "libx264", "-qp", "0", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-video_track_timescale", "600", str(path)],
        input=body, capture_output=True, check=False,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr[-400:]
    reference = _clock(path)
    assert (reference.conform_kind, reference.frame_count) == ("uniform", clock.frame_count)
    assert _decoded(path) == [frames[ordinal] for ordinal in clock.slot_packets]  # 무손실
    return path


def _extract(source: Path, first: int, after: int, target: Path) -> preprocess.RawClipExtraction:
    clock = _clock(source)
    span = preprocess.clock_frame_range(clock, first, after)
    return preprocess.extract_raw_clip(source, target, span, preprocess.RAW_SOURCE_MATCHED)


@pytest.mark.parametrize(
    ("name", "first", "after"),
    [
        ("gap", 0, 120),  # 처음부터 끝까지
        ("gap", 5, 40),  # 빈 슬롯에서 시작한다 — 붙잡은 패킷이 슬롯 4에서 처음 보인다
        ("gap", 3, 12),  # 빈 슬롯을 지난다
        ("switch", 40, 75),  # 30fps 토막을 지나 원본 끝까지
        ("switch", 10, 30),  # 25fps 안쪽
        ("tie", 25, 40),  # 반 프레임 동점을 지난다
    ],
)
def test_map_extraction_matches_the_python_map_frame_by_frame(
    media: dict[str, Path], tmp_path: Path, name: str, first: int, after: int
) -> None:
    source = media[name]
    clock = _clock(source)
    reference = _reference(source, clock, tmp_path / "reference.mov")

    mapped = _extract(source, first, after, tmp_path / "mapped.mp4")
    uniform = _extract(reference, first, after, tmp_path / "uniform.mp4")

    assert mapped.frame_count == uniform.frame_count == after - first
    assert mapped.frame_duration == clock.frame_duration
    out = _clock(mapped.path)
    assert (out.conform_kind, out.presentation_origin) == ("uniform", 0)  # 정확히 균일
    assert _frame_hashes(mapped.path) == _frame_hashes(uniform.path)
    assert mapped.media_range == uniform.media_range  # 명목 좌표 그대로
    assert any("격자 맞춤(map, D70)" in warning for warning in mapped.warnings)


@pytest.mark.parametrize(("first", "after"), [(3, 40), (60, 120)])
def test_a_map_extraction_starts_its_sound_where_the_plain_twin_does(
    media: dict[str, Path], tmp_path: Path, first: int, after: int
) -> None:
    """같은 소리를 실은 원본(떨어뜨리기 전)과 같은 슬롯 — 매체 좌표·오디오 지역 시작이 같다.

    map의 소리는 첫 슬롯의 원본 시각에서 잘린다(시프트 0). 균일 원본은 한 틱 일찍 찾지만 소리
    시작의 기대값은 같은 식(`max(0, 첫 패킷 − 탐색 점)`)이고 둘 다 0이다.
    """
    gap = _extract(media["gap"], first, after, tmp_path / "gap.mp4")
    plain = _extract(media["plain"], first, after, tmp_path / "plain.mp4")
    assert gap.media_range == plain.media_range
    assert gap.audio_local_starts == plain.audio_local_starts
    assert gap.core_local_start == plain.core_local_start
    assert gap.core_local_end == plain.core_local_end
    assert gap.frame_count == plain.frame_count == after - first
    assert abs(gap.media_local_end - plain.media_local_end) <= Fraction(1, 30)


@pytest.mark.parametrize(("first", "after"), [(10, 50), (100, 150), (0, 150)])
def test_a_remapped_snap_extracts_like_its_plain_source(
    media: dict[str, Path], tmp_path: Path, first: int, after: int
) -> None:
    """30000/1001로 읽힌 30fps 원본 — 출력은 1/30 격자이고 원본과 프레임마다 같다(D70 snap 우선)."""
    plain, ntsc = media["plain5"], media["ntsc"]
    assert _clock(ntsc).frame_count == _clock(plain).frame_count == 150
    _assert_same_extraction((plain, ntsc), tmp_path, first, after)


# ── 분석 프레임 표본 — 빈 슬롯은 앞 프레임과 같은 바이트 ─────────────────────────────


def test_a_held_slot_samples_the_same_jpeg_as_its_neighbour(
    media: dict[str, Path], tmp_path: Path
) -> None:
    gap = media["gap"]
    clock = _clock(gap)
    shots = {}
    for slot in (4, 5, 6):
        target = tmp_path / f"single-{slot}.jpg"
        preprocess.extract_frame_index(
            gap, clock.stream_index, slot, target, long_edge=64, slot_packets=clock.slot_packets
        )
        shots[slot] = target.read_bytes()
    assert shots[5] == shots[4] and shots[6] != shots[5]

    seen: list[int] = []
    batch = preprocess.extract_frame_indices(
        gap,
        clock.stream_index,
        [4, 5, 6, 119],
        tmp_path / "batch",
        long_edge=64,
        timeout_seconds=120,
        on_frame=lambda slot, _path: seen.append(slot),
        slot_packets=clock.slot_packets,
    )
    assert sorted(batch) == sorted(seen) == [4, 5, 6, 119]
    assert batch[5].name == "frame_00000005.jpg"
    assert batch[5].read_bytes() == batch[4].read_bytes() != batch[6].read_bytes()
    assert sorted(p.name for p in (tmp_path / "batch").iterdir()) == [
        "frame_00000004.jpg",
        "frame_00000005.jpg",
        "frame_00000006.jpg",
        "frame_00000119.jpg",
    ]  # 스테이징은 남지 않는다
    with pytest.raises(RuntimeError, match="map 밖"):
        preprocess.extract_frame_index(
            gap, clock.stream_index, 120, tmp_path / "x.jpg", long_edge=64,
            slot_packets=clock.slot_packets,
        )  # fmt: skip


def test_the_analysis_proxy_inherits_the_same_map(media: dict[str, Path], tmp_path: Path) -> None:
    """프록시는 원본 pts를 그대로 이어받는다 — 같은 슬롯 대응이라 원점 엄격 모드로도 잇는다."""
    built = preprocess.build_analysis_proxy(
        media["gap"],
        tmp_path / "proxy.mp4",
        scale=0.5,
        video_codec="libx264",
        preset="ultrafast",
        crf=30,
        gop=30,
        pix_fmt="yuv420p",
        timeout_seconds=120,
    )
    original, proxy = _clock(media["gap"]), _clock(built.path)
    assert proxy.conform_kind == original.conform_kind == "map"
    assert proxy.slot_packets == original.slot_packets
    assert media_refs._lattice_rejection(original, proxy, exact_origin=True) is None
    other = _clock(media["switch"])
    rejection = media_refs._lattice_rejection(original, other, exact_origin=False)
    assert rejection is not None and rejection.status == "rejected"
