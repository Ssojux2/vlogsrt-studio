"""정수 틱이 아닌 선언 레이트 — 검토 시계가 못 세우는 「균일」을 내지 않는다(W-55, D107).

실데이터(2026-11-08, 175개): 아이폰 IMG_8994.MOV(time_base 1/600 · r_frame_rate
30000/1001 · 91프레임, 3초)의 패킷은 29.97 격자를 1/600에 반올림한 것이라 엄격 증명이
한 틱 허용(`_tick_tolerance`)으로 통과했다. 그 시계는 프레임 길이 1001/30000초 = 20.02틱이라
검토 시계(`make_source_clock`)가 정수 틱이 아니라고 거부했고, 그 예외가 검토 머리 깔기
전체를 실패시켜 175개 목록이 비었다(D68 위반). 같은 카메라의 다른 29.97 클립 9개는 더
길어서 엄격 증명을 넘지 못하고 D70 재선택으로 1/30 snap에 받혔다 — 길이만 다른 같은
모양이다. 이제 원본 경계(`probe_extraction_clock`)는 엄격 증명이 통과해도 프레임 길이가
정수 틱이 아니면 D70 재선택으로 보내 형제들과 같은 시계를 낸다. 맞추지 못하면 지원 밖
(D68)이다. 정수 틱 원본은 바이트 그대로다. 유료 호출 0회 — 가짜 ffprobe 출력과 합성
ffmpeg 미디어만.
"""

from __future__ import annotations

import shutil
import subprocess
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_grid_conform import _clock, _stream_json, make_plain
from tests.raw.test_review_workspace import _ingest_card, _wait_terminal
from tests.raw.test_vfr_library import make_vfr
from vlogsrt import preprocess
from vlogsrt.project.raw.segment_review import make_source_clock
from vlogsrt.project.raw.service import RawWorkspaceService

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

NTSC = "30000/1001"
IMG_8994_PTS = [20 * n for n in range(50)] + [1001 + 20 * n for n in range(41)]
"""IMG_8994.MOV의 패킷 그대로 — 간격 20틱 89번 · 21틱 한 번(순번 50), 91개."""


def _review_clock(clock: preprocess.RawVideoClock) -> object:
    """검토가 이 시계로 정확한 프레임 경계를 세울 수 있는가 — 거부하면 예외다."""
    return make_source_clock(
        source_id="src",
        source_sha256="0" * 64,
        time_base_num=clock.time_base.numerator,
        time_base_den=clock.time_base.denominator,
        origin_pts=clock.first_pts,
        end_pts=-(-clock.exact_end // clock.time_base),
        frame_duration=clock.frame_duration,
    )


def _make_ntsc(path: Path, seconds: str) -> Path:
    """29.97fps를 1/600에 담은 합성 원본 — ffmpeg가 pts를 반올림해 정확한 양자화 격자다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
         "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={NTSC}:duration={seconds}",
         "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
         "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "64k", "-video_track_timescale", "600", str(path)],
        capture_output=True, text=True, check=False,
    )  # fmt: skip
    assert completed.returncode == 0, completed.stderr[-400:]
    probed = preprocess.probe_raw_media(path)
    assert probed.video.time_base == Fraction(1, 600)  # 재료가 정말 1/600이다
    return path


# ── 원본 경계 — 정수 틱이 아닌 균일은 없다 ───────────────────────────────────────


def test_the_img_8994_shape_is_snapped_like_its_longer_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stdout = _stream_json(IMG_8994_PTS, rate=NTSC, avg="18200/607")
    clock = _clock(monkeypatch, tmp_path, stdout)

    assert (clock.uniform_grid, clock.conform_kind) == (True, "snap")
    assert clock.frame_duration == Fraction(1, 30)
    assert (clock.frame_count, clock.max_deviation_ticks) == (91, 1)
    assert "정수 틱이 아니다" in clock.grid_note and "다시 골랐다" in clock.grid_note
    _review_clock(clock)  # 검토 시계가 선다 — 예전엔 ReviewValidationError


def test_a_quantized_clock_the_review_cannot_hold_is_left_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30000/1001 @ 1/1000 — 명목 간격(33틱)이 Final Cut 레이트가 아니다(D68 지원 밖)."""
    pts = [round(Fraction(1001, 30) * n) for n in range(120)]
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate=NTSC, tb="1/1000"))

    assert clock.uniform_grid is False
    assert "정수 틱이 아니다" in clock.grid_note
    assert "Final Cut 프레임 레이트가 아니다" in clock.grid_note


def test_a_whole_tick_ntsc_clock_stays_uniform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30000/1001 @ 1/30000(1001틱) — 정확한 격자다. 새 갈래에 들지 않는다(바이트 그대로)."""
    pts = [1001 * n for n in range(120)]
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate=NTSC, tb="1/30000"))

    assert (clock.conform_kind, clock.frame_duration) == ("uniform", Fraction(1001, 30000))
    assert clock.grid_note == ""


@needs_ffmpeg
@pytest.mark.parametrize("seconds", ["3", "20"])
def test_every_uniform_ntsc_600_clip_gives_a_review_clock(tmp_path: Path, seconds: str) -> None:
    """3초는 snap, 20초는 어긋남이 반 프레임을 넘어 map — 어느 쪽이든 1/30 정수 틱이다."""
    clock = preprocess.probe_extraction_clock(_make_ntsc(tmp_path / "ntsc.mov", seconds))

    assert clock.uniform_grid
    assert clock.conform_kind == ("snap" if seconds == "3" else "map")
    assert clock.frame_duration == Fraction(1, 30)
    _review_clock(clock)


# ── 라이브러리 — 그 원본이 있어도 검토 머리가 선다 ─────────────────────────────────


@needs_ffmpeg
def test_review_head_lays_with_the_quantized_source_beside_a_vfr_one(tmp_path: Path) -> None:
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "29.97 @ 1/600 섞임")
    card = tmp_path / "card"
    make_plain(card / "01_plain.mov")
    _make_ntsc(card / "02_ntsc.mov", "3")
    make_vfr(card / "03_vfr.mov")
    _ingest_card(service, root, card)
    service.ensure_source_order(root)
    ids = {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}

    record = _wait_terminal(
        service, root, service.start_source_preview(root, (ids["01_plain.mov"],)).job_id
    )

    assert record.status == "succeeded", record.error
    head = service.review_workspace_facade(root).load_head().revision
    assert [c.source_id for c in head.clocks] == [ids["01_plain.mov"], ids["02_ntsc.mov"]]
    assert [u.asset_id for u in service.review_unsupported_sources(root)] == [ids["03_vfr.mov"]]
