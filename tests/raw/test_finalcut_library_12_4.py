"""12.4 라이브러리(`FFAsset`)의 원본을 Final Cut으로 보낸다 (W-28, D72).

실물 261018_reelog: 아이폰 세로 HEVC 34개를 Final Cut Pro 12.4에서 가져왔더니 자산 기록이
`FFAsset`이라 보내기가 34개 모두 「FFMIORADAsset 기록이 없다」로 막혔다. 지키는 선:

- `FFAsset`의 길이는 영상 머리 정보의 실제 길이(실측 848.05·189.95프레임)라, 선언 프레임 수는 가장
  가까운 프레임(half-up — 904.5 → 905)이고 측정 프레임 수와 **정확히** 같아야 한다. 맞춘 원본의
  어긋남 몫(D69·D70)을 그 위에 더 얹지 않는다.
- `FFMIORADAsset` 선언은 예전 그대로다(격자에 떨어져야 하고, 어긋남 몫은 맞춘 원본에서만).
- 세로(90° 회전) 원본은 재생 화면 크기의 포맷으로, 켠 길이가 많으면 세로 프로젝트로 간다(W-26).
- 균일 원본의 편집 목록이 시작은 그대로 두고 **끝만** 반 프레임 미만 자르면 받고(실물 IMG_8874,
  사용자 결정), 리포트·결과 글·CLI가 그 원본과 몫을 이름으로 말한다. 반 프레임 이상 · 시작을
  옮긴 편집 목록은 예전 문구로 거부한다.
"""

from __future__ import annotations

import dataclasses
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.project.library_fixtures import ASSET, NSNULL, MediaSpec, make_bundle
from tests.raw.test_finalcut_export import (
    _assert_valid,
    _encode,
    _parsed,
    _parser,
    _report,
    _rotated,
    _sequence_size,
    _spine,
    needs_ffmpeg,
)
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_tier_review_gate import tier_project
from vlogsrt import preprocess
from vlogsrt.project.library import ASSET as LIBRARY_ASSET
from vlogsrt.project.library import RAD_ASSET, LibraryMedia
from vlogsrt.project.raw import commands as media_commands
from vlogsrt.project.raw import finalcut_export
from vlogsrt.project.raw.service import RawWorkspaceService

pytestmark = [
    needs_ffmpeg,
]
F = Fraction
RATE = F(1, 25)
NO_SHIFT = F(0)


def _spec(
    name: str, frames: int, length_frames: Fraction | None = None, **kwargs: Any
) -> MediaSpec:
    """12.4가 새로 가져온 원본의 기록 — 길이를 주면 그 프레임어치를 `mediaRange` 길이로 적는다."""
    fields: dict[str, Any] = {
        "name": name,
        "frames": frames,
        "frame_duration": "1/25",
        "format": NSNULL,
        "drop_frame": 0,
        "record": ASSET,
    }
    if length_frames is not None:
        fields["duration_override"] = str(length_frames * RATE)
    fields.update(kwargs)
    return MediaSpec(**fields)


def _reelog(
    tmp_path: Path, portrait: MediaSpec, landscape: MediaSpec
) -> tuple[RawWorkspaceService, Path, Path]:
    """세로 하나 + 가로 하나가 든 합성 12.4 라이브러리를 참조로 가져온다 — 복사 없음."""
    bundle = make_bundle(
        tmp_path, media={"raw_vid": (portrait, landscape)}, name="reelog.fcpbundle"
    )
    originals = bundle / "raw_vid" / "Original Media"
    _rotated(originals / f"{portrait.name}.mov", seconds=str(portrait.frames * RATE))
    _encode(originals / f"{landscape.name}.mov", seconds=str(landscape.frames * RATE))
    service, root = _reference_import(bundle)
    return service, root, bundle.resolve()


def _reference_import(bundle: Path) -> tuple[RawWorkspaceService, Path]:
    service = RawWorkspaceService()
    state, _ = service.managed_fcp_workspace(bundle)
    root = Path(state.workspace_root)
    proposal = service.scan_fcp_library(root, bundle, originals_only=True).proposal
    service.remember_fcp_library(root, bundle)
    handle = service.start_reference_import(root, proposal, prepare_preview=False)
    finished = _wait_terminal(service, root, handle.job_id)
    assert finished.status == "succeeded", finished.error
    return service, root


def _assets(path: Path) -> dict[str, tuple[ET.Element, ET.Element]]:
    """이름 → (asset, 그 asset의 format)."""
    document = _parsed(path)
    formats = {f.attrib["id"]: f for f in document.iter("format")}
    return {
        a.attrib["name"]: (a, formats[a.attrib["format"]])
        for a in document.findall("./resources/asset")
    }


@pytest.mark.parametrize(
    "length_frames",
    [F(10001, 100), F(199, 2), F(100)],
    ids=["848.05-style", "half-up-tie-99.5", "exact"],
)
def test_a_12_4_library_with_a_rotated_portrait_and_a_landscape_clip_exports(
    tmp_path: Path, length_frames: Fraction
) -> None:
    """세로 4초(100프레임) + 가로 2초 — 세로 프로젝트, 원본마다 제 재생 화면 크기, 신원은
    FFAsset."""
    portrait = _spec("IMG_8844", 100, length_frames, rotation=-90.0, display=(90, 160))
    landscape = _spec("IMG_9000", 50, F(4999, 100), display=(160, 90))  # 49.99 → 50
    service, root, bundle = _reelog(tmp_path, portrait, landscape)
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    assets = _assets(path)
    tall, tall_format = assets["IMG_8844"]
    wide, wide_format = assets["IMG_9000"]
    assert (tall.attrib["uid"], wide.attrib["uid"]) == (portrait.identity, landscape.identity)
    assert (tall.attrib["start"], tall.attrib["duration"]) == ("0s", "100/25s")
    assert wide.attrib["duration"] == "50/25s"
    assert (tall_format.attrib["width"], tall_format.attrib["height"]) == ("90", "160")
    assert (wide_format.attrib["width"], wide_format.attrib["height"]) == ("160", "90")
    assert _sequence_size(path) == ("90", "160")
    assert [clip.attrib["name"] for clip in _spine(path)] == ["IMG_8844", "IMG_9000"]
    report = _report(path)
    assert report["counts"]["orientation"] == "portrait"
    assert report["library"]["bundle"] == str(bundle)
    assert report["library"]["warnings"] == []


@pytest.mark.parametrize(
    "length_frames",
    [F(201, 2), F(101), F(9949, 100)],
    ids=["half-up-tie-100.5-is-101", "one-frame-long", "99.49-is-99"],
)
def test_a_12_4_length_whose_nearest_frame_differs_is_refused_by_name(
    tmp_path: Path, length_frames: Fraction
) -> None:
    portrait = _spec("IMG_8844", 100, length_frames, rotation=-90.0, display=(90, 160))
    landscape = _spec("IMG_9000", 50, display=(160, 90))
    service, root, _bundle = _reelog(tmp_path, portrait, landscape)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    message = str(caught.value)
    assert "IMG_8844.mov" in message and "프레임 수" in message
    assert "IMG_9000" not in message


def test_a_12_4_start_that_disagrees_with_the_file_is_refused(tmp_path: Path) -> None:
    portrait = _spec("IMG_8844", 100, rotation=-90.0, display=(90, 160), start_frames=1)
    service, root, _bundle = _reelog(tmp_path, portrait, _spec("IMG_9000", 50))
    with pytest.raises(finalcut_export.FinalCutExportError, match="IMG_8844.mov"):
        service.export_finalcut_timeline(root)


# ─── 선언 프레임 수의 규칙 — 시계 종류와 어긋남 몫의 상호작용 ──────────────────


def _declared(duration: Fraction, record_type: str) -> LibraryMedia:
    return LibraryMedia(
        name="IMG_8877",
        original=Path("IMG_8877.mov"),
        proxy=None,
        media_identifier="id",
        frame_duration=F(1, 30),
        duration=duration,
        format_name=None,
        has_video=True,
        has_audio=True,
        audio_rate=48000,
        audio_channels=2,
        media_start=F(0),
        drop_frame=False,
        record_type=record_type,
    )


def test_ffasset_frames_are_the_nearest_frame_whatever_the_clock() -> None:
    """균일(0) · snap(1/20) · map(19/20) 어느 시계든 FFAsset은 가장 가까운 프레임 하나다."""
    for deviation in (F(0), F(1, 20), F(19, 20)):
        grid = finalcut_export._Grid(F(1, 30), 452, deviation)
        assert finalcut_export._declared_frames(_declared(F(9030, 600), LIBRARY_ASSET), grid) == 452
        # 452.7프레임: map의 어긋남 몫(19/20) 안이지만 가장 가까운 프레임은 453 — 몫을
        # 더 얹지 않는다.
        wide = _declared(F(4527, 300), LIBRARY_ASSET)
        assert finalcut_export._declared_frames(wide, grid) == 453


def test_ffmioradasset_frames_keep_the_old_rule() -> None:
    """12.3 기록은 예전 그대로 — 균일 원본은 격자에 떨어져야 하고, 어긋남 몫은 맞춘 원본에서만."""
    exact = finalcut_export._Grid(F(1, 30), 190)
    with pytest.raises(Exception, match="프레임 격자"):
        finalcut_export._declared_frames(_declared(F(3799, 600), RAD_ASSET), exact)  # 189.95
    assert finalcut_export._declared_frames(_declared(F(3799, 600), LIBRARY_ASSET), exact) == 190
    mapped = finalcut_export._Grid(F(1, 30), 452, F(19, 20))
    assert finalcut_export._declared_frames(_declared(F(4527, 300), RAD_ASSET), mapped) == 452


# ─── 끝만 반 프레임 미만 자른 편집 목록(IMG_8874 모양, 사용자 결정 — D72) ──────────────


def _trim_tail(path: Path, *, trim: Fraction, start: Fraction = NO_SHIFT) -> Path:
    """영상 트랙의 첫 `elst` 항목을 고친다 — 길이를 `trim + start` 프레임어치 줄이고, `start`면
    미디어 시작도 그만큼 민다. 다시 인코딩하지 않으니 패킷은 그대로 균일하다(실물 8874처럼)."""
    data = bytearray(path.read_bytes())
    movie = data.find(b"mvhd")
    scale = int.from_bytes(data[movie + 16 : movie + 20], "big")
    edit = data.find(b"elst")  # 영상 트랙이 먼저 들어간다(`_encode`의 첫 입력)
    duration = int.from_bytes(data[edit + 12 : edit + 16], "big")
    media_time = int.from_bytes(data[edit + 16 : edit + 20], "big")
    cut = (trim + start) * RATE * scale
    track_ticks = RATE / F(1, 12800) * start  # mov 25fps의 트랙 눈금 1/12800
    assert cut.denominator == 1 and track_ticks.denominator == 1
    data[edit + 12 : edit + 16] = (duration - cut.numerator).to_bytes(4, "big")
    data[edit + 16 : edit + 20] = (media_time + track_ticks.numerator).to_bytes(4, "big")
    path.write_bytes(bytes(data))
    return path


def _plain(tmp_path: Path, name: str, *, trim: Fraction, start: Fraction = NO_SHIFT) -> Any:
    """편집 목록을 고친 원본 하나를 복사 가져오기로 — 검토 머리 없음(패킷을 훑어 잰다)."""
    source = _trim_tail(_encode(tmp_path / "card" / f"{name}.mov"), trim=trim, start=start)
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "끝 자름")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    return service, root


def test_a_uniform_tail_trimmed_by_less_than_half_a_frame_is_sent_and_reported(tmp_path: Path):
    service, root = _plain(tmp_path, "tail", trim=F(1, 4))
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    asset = _parsed(path).find("./resources/asset")
    assert asset is not None and asset.attrib["duration"] == "50/25s"  # 패킷 50개 그대로
    report = _report(path)
    assert report["counts"]["tail_trimmed_sources"] == [
        {"name": "tail", "frames": "1/4", "seconds": "1/100"}
    ]
    assert report["sources"][0]["tail_trim_frames"] == "1/4"


@pytest.mark.parametrize(
    ("trim", "start"),
    [(F(1, 2), F(0)), (F(3, 4), F(0)), (F(1), F(0)), (F(0), F(1, 2))],
    ids=["half-frame", "three-quarters", "whole-frame", "start-moved"],
)
def test_a_tail_trim_of_half_a_frame_or_more_or_a_moved_start_is_refused(
    tmp_path: Path, trim: Fraction, start: Fraction
):
    service, root = _plain(tmp_path, "cut", trim=trim, start=start)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    message = str(caught.value)
    assert "cut.mov" in message and "편집 목록" in message and "다시 저장" in message


def test_an_untrimmed_source_reports_no_tail_trim(tmp_path: Path):
    service, root = _plain(tmp_path, "whole", trim=F(0))
    report = _report(service.export_finalcut_timeline(root))
    assert report["counts"]["tail_trimmed_sources"] == []
    assert report["sources"][0]["tail_trim_frames"] == "0"


def test_a_head_clock_accepts_a_one_tick_tail_trim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """검토 머리 길도 같은 규칙이다 — 예전 「머리 정보 한 틱 짧음」 재료가 이제 받는 쪽이다."""
    service, root = tier_project(tmp_path)
    real = preprocess.probe_raw_media

    def shorter(path: Path, **kwargs: Any) -> Any:
        probe = real(path, **kwargs)
        assert probe.video.duration_ts is not None
        video = dataclasses.replace(probe.video, duration_ts=probe.video.duration_ts - 1)
        return dataclasses.replace(probe, video=video)

    monkeypatch.setattr(preprocess, "probe_raw_media", shorter)
    (row,) = _report(service.export_finalcut_timeline(root))["counts"]["tail_trimmed_sources"]
    assert (row["name"], row["frames"]) == ("tier", "1/512")


def test_snap_sources_keep_their_own_tolerance_without_the_tail_allowance() -> None:
    """맞춘 원본은 D69·D70의 몫만 — 끝 자름 갈래를 더해 넓히지 않는다."""
    video = SimpleNamespace(duration_ts=None, time_base=F(1, 600))
    probe: Any = SimpleNamespace(video=video)
    asset: Any = SimpleNamespace(source_name="snap.mov")
    video.duration_ts = 848 * 20 - 8  # 0.4프레임 짧다 — 몫 1/20 밖, 반 프레임 안
    with pytest.raises(finalcut_export.FinalCutExportError, match="편집 목록"):
        finalcut_export._require_plain_timeline(asset, 0, 848, F(1, 30), probe, F(1, 20))
    uniform = finalcut_export._require_plain_timeline(asset, 0, 848, F(1, 30), probe)
    assert uniform == F(2, 5)  # 균일 원본이면 끝 자름으로 받는다


def test_a_12_4_portrait_with_a_tail_trim_is_sent_like_img_8874(tmp_path: Path) -> None:
    """IMG_8874 모양 — 패킷 100개 균일, `elst`가 끝을 1/4프레임 자름, FFAsset 길이 = 머리 정보."""
    portrait = _spec("IMG_8874", 100, F(399, 4), rotation=-90.0, display=(90, 160))
    landscape = _spec("IMG_9000", 50, display=(160, 90))
    bundle = make_bundle(
        tmp_path, media={"raw_vid": (portrait, landscape)}, name="reelog.fcpbundle"
    )
    originals = bundle / "raw_vid" / "Original Media"
    _trim_tail(_rotated(originals / "IMG_8874.mov", seconds="4"), trim=F(1, 4))
    _encode(originals / "IMG_9000.mov", seconds="2")
    service, root = _reference_import(bundle)
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    tall, tall_format = _assets(path)["IMG_8874"]
    assert tall.attrib["duration"] == "100/25s" and tall_format.attrib["height"] == "160"
    counts = _report(path)["counts"]
    assert counts["tail_trimmed_sources"] == [
        {"name": "IMG_8874", "frames": "1/4", "seconds": "1/100"}
    ]
    assert counts["orientation"] == "portrait"


def test_the_send_text_and_cli_name_tail_trimmed_sources(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from ui.workspace_service import _finalcut_sent_text

    counts = {"sources": 1, "spine_clips": 1, "disabled_clips": 0, "unreviewed_clips": 0}
    trimmed = [{"name": "IMG_8874", "frames": "1/20", "seconds": "1/600"}]
    sent = {"path": "/x/v.fcpxml", "counts": {**counts, "tail_trimmed_sources": trimmed}}
    text = _finalcut_sent_text({**sent, "not_exported": 0})
    assert "편집 목록이 끝을 반 프레임보다 짧게 자른 영상 1개(IMG_8874(1/600초))" in text
    assert "편집 목록" not in _finalcut_sent_text({**sent, "counts": counts, "not_exported": 0})
    service, root = _plain(tmp_path, "tail", trim=F(1, 4))
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root)])
    assert media_commands.media_command(args) == 0
    printed = "".join(capsys.readouterr().out.split())
    assert "끝을반프레임보다짧게자른편집목록:tail—1/100초(1/4프레임),시작은그대로" in printed
