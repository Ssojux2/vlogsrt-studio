"""근접 CFR 원본은 명목 격자에 맞춰 받는다(W-27b, D69).

실데이터(새 라이브러리 아이폰 34개): 15개는 균일, 10개는 time_base 1/600에서 패킷이 명목 슬롯
(20틱)에서 1–6틱 흔들릴 뿐이다. 반 프레임 미만이면 가장 가까운 슬롯이 하나뿐이라 Final Cut처럼
명목 격자에 맞춰 받는다 — `probe_extraction_clock` 한 자리만. 빈 슬롯·속도 전환은 그대로 지원
밖(D68)이고, 캡션 엔진·썸네일 찾기의 증명은 엄격 그대로다. 균일 원본은 시계 값·추출 argv가
바이트까지 예전 그대로다. 유료 호출 0회 — 가짜 ffprobe 출력과 합성 ffmpeg 미디어만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_native_media_probe import _fake_ffprobe, _matrix_clock, _matrix_probe
from vlogsrt import preprocess
from vlogsrt.preprocess import GridConformPolicy

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

IPHONE_TB = "1/600"
IPHONE_RATE = "30/1"
T = 20  # 30fps를 1/600에 담은 프레임 길이(틱)


def _packets(deltas: list[int], *, start: int = 0) -> list[int]:
    pts = [start]
    for delta in deltas:
        pts.append(pts[-1] + delta)
    return pts


def _stream_json(
    pts: list[int], *, rate: str = IPHONE_RATE, tb: str = IPHONE_TB, avg: str | None = None
) -> str:
    """아이폰 mov처럼 선언 길이는 마지막 패킷 + 한 프레임까지다(지터가 그대로 실린다)."""
    ticks = Fraction(rate) ** -1 / Fraction(tb)
    stream = {
        "index": 0,
        "codec_type": "video",
        "r_frame_rate": rate,
        "avg_frame_rate": avg or rate,
        "time_base": tb,
        "nb_frames": str(len(pts)),
        "start_pts": pts[0],
        "duration_ts": pts[-1] - pts[0] + int(ticks) if ticks.denominator == 1 else pts[-1],
    }
    return json.dumps({"streams": [stream], "packets": [{"pts": value} for value in pts]})


def _clock(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, stdout: str, **kwargs: object):
    _fake_ffprobe(monkeypatch, stdout)
    return preprocess.probe_extraction_clock(tmp_path / "clip.mov", **kwargs)  # type: ignore[arg-type]


def _one_off(offset: int, *, at: int = 300, frames: int = 600) -> list[int]:
    pts = [T * n for n in range(frames)]
    pts[at] += offset
    return pts


# ── 받는다 — 반 프레임 미만, 빈 슬롯 없음 ─────────────────────────────────────


def test_the_iphone_8844_shape_is_snapped_onto_the_nominal_grid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMG_8844: 간격 19×1 · 20×844 · 21×2 — 848패킷이 슬롯에서 많아야 한 틱 벗어난다."""
    deltas = [20] * 100 + [21] + [20] * 300 + [19] + [20] * 300 + [21] + [20] * 144
    pts = _packets(deltas)
    assert len(pts) == 848

    clock = _clock(monkeypatch, tmp_path, _stream_json(pts))

    assert clock.uniform_grid is True
    assert (clock.conform_kind, clock.max_deviation_ticks) == ("snap", 1)
    assert clock.frame_count == 848  # 패킷 수
    assert clock.frame_duration == Fraction(1, 30)
    assert clock.presentation_origin == 0
    assert clock.exact_end == Fraction(848, 30)  # 명목 끝 — 마지막 패킷의 지터가 아니다
    assert clock.last_pts == 848 * T - T + 1  # 측정값은 그대로 남는다
    assert "격자 맞춤: 최대 어긋남 1틱" in clock.grid_note


def test_a_six_tick_jitter_is_accepted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """실데이터의 가장 큰 어긋남(IMG_8851·8859·8863) — 앞뒤로 6틱."""
    pts = _one_off(6)
    pts[400] -= 6
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts))
    assert clock.uniform_grid and clock.conform_kind == "snap"
    assert clock.max_deviation_ticks == 6
    assert clock.frame_count == 600 and clock.exact_end == Fraction(600, 30)


@pytest.mark.parametrize("offset", [9, -9])
def test_nine_ticks_is_still_under_half_a_frame(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    clock = _clock(monkeypatch, tmp_path, _stream_json(_one_off(offset)))
    assert clock.uniform_grid and clock.max_deviation_ticks == 9


@pytest.mark.parametrize("offset", [10, -10])
def test_ten_ticks_is_half_a_frame_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: int
) -> None:
    """반 프레임이면 가장 가까운 슬롯이 둘이다 — 맞추지 않는다(floor((T−1)/2) = 9틱이 끝)."""
    clock = _clock(monkeypatch, tmp_path, _stream_json(_one_off(offset)))
    assert clock.uniform_grid is False
    assert (clock.conform_kind, clock.max_deviation_ticks) == ("uniform", 0)  # 뜻 없는 기본값
    assert "격자를 벗어났습니다" in clock.grid_note  # 엄격한 증명의 말은 그대로
    assert "격자 맞춤 불가" in clock.grid_note and "허용 10틱 미만" in clock.grid_note


# ── 받지 않는다 — 빈 슬롯·속도 전환(D68 지원 밖 그대로) ────────────────────────


def test_one_dropped_frame_is_refused_naming_the_empty_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMG_8877·8871 모양: 지터 끝에 한 슬롯을 건너뛴다(패킷 411개가 슬롯 412칸을 덮는다)."""
    deltas = [20] * 410 + [31] + [20] * 40  # 순번 411이 +11틱 → 가장 가까운 슬롯은 412
    clock = _clock(monkeypatch, tmp_path, _stream_json(_packets(deltas)))
    assert clock.uniform_grid is False
    assert "슬롯 411이 비었다" in clock.grid_note

    dropped = _packets([20] * 200 + [40] + [20] * 50)  # 프레임 하나를 통째로 떨어뜨림
    clock = _clock(monkeypatch, tmp_path, _stream_json(dropped))
    assert clock.uniform_grid is False and "슬롯 201이 비었다" in clock.grid_note


def test_the_8858_rate_switch_shape_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMG_8858: r=24(T=25)인데 뒤가 30fps(20틱)다 — 두 패킷이 한 슬롯에 떨어진다."""
    pts = _packets([25] * 150 + [20] * 80)
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="24/1"))
    assert clock.uniform_grid is False
    assert "앞 패킷과 같은 슬롯" in clock.grid_note


def test_the_8850_120fps_declaration_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IMG_8850: r=120(T=5)인데 패킷은 20틱 간격 — 슬롯 1·2·3이 비었다."""
    pts = _packets([20] * 188)
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="120/1", avg="30/1"))
    assert clock.uniform_grid is False
    assert "슬롯 1이 비었다" in clock.grid_note


def test_a_quantized_clock_keeps_the_strict_one_tick_rule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30000/1001 @ 1/1000은 프레임 길이가 정수 틱이 아니다 — 맞춤이 닿지 않는다."""
    rate = "30000/1001"
    pts = [round(Fraction(1001, 30) * n) for n in range(120)]
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate=rate, tb="1/1000"))
    assert clock.uniform_grid and clock.conform_kind == "uniform"
    assert clock.max_deviation_ticks == 0

    pts[60] += 3
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate=rate, tb="1/1000"))
    assert clock.uniform_grid is False
    assert "격자 맞춤" not in clock.grid_note  # 엄격한 판정 그대로


def test_a_uniform_clock_is_byte_for_byte_what_it_was(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D69 앞(0811c7f)의 열 칸 그대로 — 새 두 칸은 기본값이다."""
    pts = [256 + 512 * n for n in range(100)]
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="25/1", tb="1/12800"))
    assert clock == preprocess.RawVideoClock(
        stream_index=0,
        time_base=Fraction(1, 12800),
        first_pts=256,
        last_pts=256 + 512 * 99,
        presentation_origin=Fraction(256, 12800),
        frame_duration=Fraction(1, 25),
        frame_count=100,
        exact_end=Fraction(256, 12800) + 4,
        uniform_grid=True,
        grid_note="",
    )
    assert (clock.max_deviation_ticks, clock.conform_kind) == (0, "uniform")


# ── 정책 — 설정에서 오고, 이 판은 snap만 안다 ─────────────────────────────────


def test_the_policy_is_frozen_config_and_rejects_impossible_bounds() -> None:
    expected = GridConformPolicy(
        max_dropped_slots=0, half_frame_bound=Fraction(1, 2), allow_rate_remap=False
    )
    assert expected == preprocess.RAW_GRID_CONFORM
    with pytest.raises(ValueError):
        GridConformPolicy(half_frame_bound=Fraction(3, 4))  # 슬롯이 둘이 된다
    with pytest.raises(ValueError):
        GridConformPolicy(half_frame_bound=Fraction(0))


def test_a_tighter_policy_refuses_and_a_map_policy_is_not_silently_used(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = _stream_json(_one_off(3))
    tight = GridConformPolicy(half_frame_bound=Fraction(1, 10))  # 2틱 미만만
    assert _clock(monkeypatch, tmp_path, stdout, policy=tight).uniform_grid is False
    with pytest.raises(RuntimeError, match="map 갈래"):
        _clock(monkeypatch, tmp_path, stdout, policy=GridConformPolicy(max_dropped_slots=1))


def test_caption_and_thumbnail_provers_stay_strict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """맞춤은 원본 경계 하나뿐이다 — 캡션 엔진 증명과 썸네일 빠른 찾기는 한 틱도 받지 않는다."""
    stdout = _stream_json(_one_off(1))
    _fake_ffprobe(monkeypatch, stdout)
    with pytest.raises(RuntimeError, match="격자를 벗어났습니다"):
        preprocess.probe_video_grid(tmp_path / "clip.mov", 0)
    with pytest.raises(RuntimeError, match="격자를 벗어났습니다"):
        preprocess._seek_plan(stdout, tmp_path / "clip.mov", 0, Fraction(0))


# ── 지원 매트릭스 — 선언 창은 패킷 증거로 ───────────────────────────────────


def test_the_declared_window_is_compared_with_packet_evidence_for_a_snapped_clock() -> None:
    """명목 끝은 마지막 패킷이 이르면 선언 창을 몇 틱 넘는다 — 그 원본을 거짓으로 막지 않는다."""
    tb = Fraction(1, 600)
    snapped = _matrix_clock(
        time_base=tb,
        first_pts=0,
        last_pts=59 * T - 3,  # 마지막 패킷이 3틱 이르다
        frame_duration=Fraction(1, 30),
        frame_count=60,
        exact_end=Fraction(2),
        max_deviation_ticks=3,
        conform_kind="snap",
    )
    probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30), avg_frame_rate=Fraction(30))
    short = replace(probe, duration=Fraction(2) - 3 * tb)
    preprocess._assert_raw_supported(short, snapped)  # 죽지 않는다
    beyond = replace(snapped, last_pts=59 * T + 3)
    with pytest.raises(preprocess.RawMediaUnsupported, match="선언 창"):
        preprocess._assert_raw_supported(short, beyond)


def _img_8863(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, late: int = 6):
    """IMG_8863 모양: tb 1/600 · T=20 · 717패킷 · 마지막 패킷이 `late`틱 늦다 · 선언 14345틱."""
    pts = [T * n for n in range(717)]
    pts[200] -= 6
    pts[-1] += late
    stdout = json.loads(_stream_json(pts))
    stdout["streams"][0]["duration_ts"] = 14345  # 실측 선언(마지막 패킷 + 19틱)
    return _clock(monkeypatch, tmp_path, json.dumps(stdout))


def _img_8863_probe() -> preprocess.RawMediaProbe:
    """실측 format: start 0 · duration은 µs로 내려 적힌 23.908333초(참값 14345/600초)."""
    tb = Fraction(1, 600)
    probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30), avg_frame_rate=Fraction(30))
    return replace(probe, duration=Fraction("23.908333"))


def test_a_late_last_frame_keeps_the_nominal_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = _img_8863(monkeypatch, tmp_path)
    assert (clock.conform_kind, clock.max_deviation_ticks) == ("snap", 6)
    assert clock.frame_count == 717
    assert clock.last_pts == 716 * T + 6  # 측정 그대로
    assert clock.exact_end == Fraction(717, 30)  # 끝은 명목 — 마지막 패킷의 지터가 아니다


def test_img_8863_is_extractable_despite_the_microsecond_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증자 회귀: 패킷 끝 2391/100초 = 참 선언 끝 14345/600초 + 한 틱인데, 선언이 µs로 내려
    적혀(23.908333) 1/3000000초 넘친 것처럼 보여 추출만 거부했다 — 계획·검토·보내기는 받았다."""
    clock = _img_8863(monkeypatch, tmp_path)
    assert clock.last_pts * clock.time_base + clock.frame_duration == Fraction(2391, 100)
    preprocess._assert_raw_supported(_img_8863_probe(), clock)  # 죽지 않는다


def test_the_window_guard_still_refuses_a_packet_beyond_the_declaration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """µs 눈금 몫은 선언의 몫일 뿐이다 — 한 틱 더 늦은 마지막 패킷은 여전히 막는다."""
    clock = _img_8863(monkeypatch, tmp_path, late=7)
    assert clock.max_deviation_ticks == 7
    with pytest.raises(preprocess.RawMediaUnsupported, match="선언 창"):
        preprocess._assert_raw_supported(_img_8863_probe(), clock)


def test_a_uniform_clock_keeps_the_one_tick_window(tmp_path: Path) -> None:
    """균일 시계의 허용은 예전 한 틱 그대로다 — µs 눈금 몫이 붙지 않는다(판정 바이트 그대로)."""
    tb = Fraction(1, 600)
    probe = replace(_img_8863_probe(), duration=Fraction(717, 30) - tb - Fraction(1, 10**6))
    uniform = _matrix_clock(
        time_base=tb,
        last_pts=716 * T,
        frame_duration=Fraction(1, 30),
        frame_count=717,
        exact_end=Fraction(717, 30),
    )
    with pytest.raises(preprocess.RawMediaUnsupported, match="선언 창"):
        preprocess._assert_raw_supported(probe, uniform)


# ── 추출 argv — 균일 원본은 바이트 그대로, 맞춘 원본만 탐색·setpts ───────────────


def _argv(clock: preprocess.RawVideoClock, probe: preprocess.RawMediaProbe) -> list[str]:
    span = preprocess.clock_frame_range(clock, 15, 45)
    plan = preprocess._plan_raw_media(clock, span, preprocess.RawClipHandles(), ())
    monkey = Path("/stage/out.part")
    return preprocess._raw_extract_command(
        Path("/src/clip.mov"), monkey, probe, preprocess.RAW_SOURCE_MATCHED, plan
    )


def test_uniform_extraction_argv_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """D69 앞(0811c7f)의 `_raw_extract_command`가 낸 argv 그대로 — 한 글자도 바뀌지 않는다."""
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    tb = Fraction(1, 600)
    clock = _matrix_clock(
        time_base=tb,
        last_pts=59 * T,
        frame_duration=Fraction(1, 30),
        frame_count=60,
        exact_end=Fraction(2),
    )
    probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30), avg_frame_rate=Fraction(30))
    assert _argv(clock, probe) == [
        "ffmpeg", "-hide_banner", "-nostdin", "-y",
        "-seek_timestamp", "1", "-ss", "0.498333", "-t", "1.000000",
        "-i", "/src/clip.mov", "-map", "0:0",
        "-c:v", "libx264", "-preset", "medium", "-crf", "17",
        "-pix_fmt", "yuv420p", "-vf", "select=lt(n\\,30)", "-fps_mode", "passthrough",
        "-c:a", "copy",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
        "-color_range", "tv", "-f", "mp4", "/stage/out.part",
    ]  # fmt: skip


def test_snapped_extraction_seeks_early_and_lays_frames_on_their_slots(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    tb = Fraction(1, 600)
    clock = _matrix_clock(
        time_base=tb,
        last_pts=59 * T,
        frame_duration=Fraction(1, 30),
        frame_count=60,
        exact_end=Fraction(2),
        max_deviation_ticks=6,
        conform_kind="snap",
    )
    probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30), avg_frame_rate=Fraction(30))
    argv = _argv(clock, probe)
    assert argv[argv.index("-ss") + 1] == "0.490000"  # 슬롯 15(300틱)에서 6틱 앞
    assert argv[argv.index("-vf") + 1] == "select=lt(n\\,30),setpts=round((PTS-6)/20)*20"
    span = preprocess.clock_frame_range(clock, 15, 45)
    plan = preprocess._plan_raw_media(clock, span, preprocess.RawClipHandles(), ())
    assert (plan.common_shift_ticks, plan.seek) == (6, Fraction(294, 600))
    assert any("격자 맞춤(D69)" in warning for warning in plan.warnings)


# ── 진짜 ffmpeg — 지터를 넣은 쌍둥이(재인코딩 없음) ───────────────────────────


def _ffmpeg(*args: str) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-400:]


# 30프레임마다 순번 15는 6틱 늦고 순번 25는 6틱 이르며, 10프레임마다 순번 3은 한 틱 늦다 —
# 실데이터의 가장 큰 어긋남(6틱)이다. 드문 지터라야 ffprobe가 r_frame_rate를 30으로 읽는다
# (짝수 틱만 흔들면 300fps로 읽는다). 6틱 + 탐색 시프트 6틱은 반 프레임(10틱)을 넘으므로 `setpts`
# 없이는 인코더의 1/30 눈금이 그 프레임을 이웃 슬롯으로 반올림한다.
JITTER = "6*eq(mod(N\\,30)\\,15)-6*eq(mod(N\\,30)\\,25)+eq(mod(N\\,10)\\,3)"


def make_plain(path: Path, *, seconds: str = "3") -> Path:
    """30fps(1/600 = 20틱) H.264 + AAC — 아이폰과 같은 time_base. B프레임 없음(pts = dts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=30:duration={seconds}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "64k", "-video_track_timescale", "600", str(path),
    )  # fmt: skip
    return path


def make_twin(plain: Path, path: Path, *, rotate: bool = False) -> Path:
    """같은 부호화 프레임에 pts만 흔든 쌍둥이 — `setts`로 스트림 복사(재인코딩 없음)."""
    rotation = ["-display_rotation", "90"] if rotate else []
    path.parent.mkdir(parents=True, exist_ok=True)
    _ffmpeg(
        *rotation, "-i", str(plain), "-map", "0", "-c", "copy",
        "-bsf:v", f"setts=pts=PTS+{JITTER}:dts=DTS+{JITTER}",
        "-video_track_timescale", "600", str(path),
    )  # fmt: skip
    clock = preprocess.probe_extraction_clock(path)
    assert (clock.conform_kind, clock.max_deviation_ticks) == ("snap", 6)  # 재료가 정말 지터다
    return path


@pytest.fixture(scope="module")
def pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    folder = tmp_path_factory.mktemp("grid_conform")
    plain = make_plain(folder / "plain.mov")
    return plain, make_twin(plain, folder / "twin.mov")


def _frame_hashes(path: Path) -> list[str]:
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-map", "0:v:0", "-f", "framemd5",
         "-"],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr[-300:]
    return [
        line.rsplit(",", 1)[-1].strip() for line in completed.stdout.splitlines() if line[:1] == "0"
    ]


@needs_ffmpeg
def test_the_twin_conforms_to_the_plain_clock(pair: tuple[Path, Path]) -> None:
    plain, twin = (preprocess.probe_extraction_clock(path) for path in pair)
    assert plain.conform_kind == "uniform"
    for field in ("time_base", "first_pts", "presentation_origin", "frame_duration"):
        assert getattr(twin, field) == getattr(plain, field), field
    assert (twin.frame_count, twin.exact_end) == (plain.frame_count, plain.exact_end)


@needs_ffmpeg
@pytest.mark.parametrize(("first", "after"), [(25, 61), (0, 30), (55, 90)])
def test_twin_extraction_matches_the_plain_extraction(
    pair: tuple[Path, Path], tmp_path: Path, first: int, after: int
) -> None:
    """6틱 이른 프레임(25·55)에서 시작해도 잃지 않고, 출력은 정확히 균일하며 화면·소리가 같다.

    한 틱 일찍 찾던 예전 탐색은 6틱 이른 첫 프레임을 버렸다(R1). 6틱 늦은 프레임(45·75)은
    `setpts` 없이는 다음 슬롯으로 반올림된다. 쌍둥이는 같은 부호화 프레임이라 출력 프레임마다
    디코드 해시가 같아야 한다.
    """
    _assert_same_extraction(pair, tmp_path, first, after)


def _assert_same_extraction(
    pair: tuple[Path, Path], tmp_path: Path, first: int, after: int
) -> None:
    plain, twin = pair
    outcomes = []
    for source in (plain, twin):
        clock = preprocess.probe_extraction_clock(source)
        span = preprocess.clock_frame_range(clock, first, after)
        target = tmp_path / f"{source.stem}-{first}.mp4"
        outcomes.append(
            preprocess.extract_raw_clip(source, target, span, preprocess.RAW_SOURCE_MATCHED)
        )
    left, right = outcomes
    assert left.frame_count == right.frame_count == after - first
    out_clock = preprocess.probe_extraction_clock(right.path)
    assert (out_clock.conform_kind, out_clock.max_deviation_ticks) == ("uniform", 0)
    assert out_clock.presentation_origin == 0
    hashes = _frame_hashes(left.path)
    assert len(hashes) == after - first
    assert hashes == _frame_hashes(right.path)
    assert left.audio_local_starts == right.audio_local_starts
    assert left.media_range == right.media_range  # 명목 좌표 그대로
    assert any("격자 맞춤(D69)" in warning for warning in right.warnings)
    assert not any("격자 맞춤" in warning for warning in left.warnings)


@pytest.fixture(scope="module")
def late_pair(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """76프레임 — 마지막 프레임(순번 75)이 6틱 늦다. 패킷 끝이 선언 끝과 한 틱 차로 붙는다."""
    folder = tmp_path_factory.mktemp("grid_conform_late")
    plain = make_plain(folder / "plain.mov", seconds="2.51")
    twin = make_twin(plain, folder / "twin.mov")
    clock = preprocess.probe_extraction_clock(twin)
    assert (clock.frame_count, clock.last_pts) == (76, 75 * T + 6)  # 재료가 정말 늦게 끝난다
    return plain, twin


@needs_ffmpeg
@pytest.mark.parametrize(("first", "after"), [(40, 46), (60, 76)])
def test_twin_extraction_that_ends_on_a_late_frame(
    late_pair: tuple[Path, Path], tmp_path: Path, first: int, after: int
) -> None:
    """6틱 늦은 프레임(45 · 원본의 마지막 75)에서 끝나는 구간 — 지원 매트릭스와 `-t`가 그
    프레임을 잃지 않는다(검증자 지적: 늦은 마지막 프레임을 덮는 시험이 없었다)."""
    preprocess._assert_raw_supported(
        preprocess.probe_raw_media(late_pair[1]),
        preprocess.probe_extraction_clock(late_pair[1]),
    )
    _assert_same_extraction(late_pair, tmp_path, first, after)


@needs_ffmpeg
def test_frame_samples_of_the_twin_are_the_plain_frames(pair: tuple[Path, Path], tmp_path: Path):
    """분석 표본(`select=eq(n,…)`)은 패킷 순번 = 슬롯이라 그대로 맞다 — 빈 슬롯을 받지 않는 까닭."""
    plain, twin = pair
    for index in (0, 15, 25, 89):
        shots = []
        for source in (plain, twin):
            target = tmp_path / f"{source.stem}-{index}.jpg"
            preprocess.extract_frame_index(source, 0, index, target, long_edge=160)
            shots.append(target.read_bytes())
        assert shots[0] == shots[1], index
