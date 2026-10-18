"""map 원본이 라이브러리에 든다 — 다시 계획의 계기·썸네일·Final Cut 보내기(W-27c, D70).

W-27b(D69)의 판은 빈 슬롯·속도 전환 원본을 계획의 `unsupported`에 기록했다. 그 기록은 그때의
측정이라 정책이 열린 뒤에는 다시 계획의 계기가 된다(`_newly_coverable`) — W-27b가 놓은 길을 그대로
타는지 본다. 여기서는 W-27b 판을 정책 하나로 흉내 낸다(`GridConformPolicy()` — snap만). 유료 호출
0회 — 합성 ffmpeg 미디어만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import traceback
import xml.etree.ElementTree as ET
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_grid_conform import make_plain
from tests.raw.test_grid_map_media import make_gap, make_switch
from tests.raw.test_review_workspace import _ingest_card, _wait_terminal
from tests.raw.test_vfr_library import make_cfr
from vlogsrt import preprocess
from vlogsrt.preprocess import GridConformPolicy
from vlogsrt.project.raw import visual_review
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.service import RawWorkspaceService

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

FIRST, GAP, SWITCH = "01_first.mov", "02_gap.mov", "03_switch.mov"


def _card(tmp_path: Path) -> Path:
    card = tmp_path / "card"
    make_cfr(card / FIRST, "2")
    make_gap(make_plain(tmp_path / "work" / "plain.mov", seconds="4"), card / GAP)
    make_switch(card / SWITCH)
    return card


def _preview(service: RawWorkspaceService, root: Path, asset_id: str) -> None:
    record = _wait_terminal(service, root, service.start_source_preview(root, (asset_id,)).job_id)
    assert record.status == "succeeded", record.error


def _library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, snap_only: bool
) -> tuple[RawWorkspaceService, Path, dict[str, str]]:
    card = _card(tmp_path)
    if snap_only:
        monkeypatch.setattr(preprocess, "RAW_GRID_CONFORM", GridConformPolicy())
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "격자 map")
    _ingest_card(service, root, card)
    service.ensure_source_order(root)
    ids = {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}
    _preview(service, root, ids[FIRST])
    return service, root, ids


def _clock(service: RawWorkspaceService, root: Path, asset_id: str) -> preprocess.RawVideoClock:
    workspace = service._workspace(root)
    return preprocess.probe_extraction_clock(
        workspace.source_media_path(workspace.source_asset(asset_id))
    )


def test_recorded_map_sources_are_relaid_once_the_policy_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """W-27b 판의 머리는 두 원본을 지원 밖으로 적었다 — 다음 미리보기가 한 장씩 깐다(D69 7·D70)."""
    _, root, ids = _library(tmp_path, monkeypatch, snap_only=True)
    service = RawWorkspaceService()
    recorded = {u.asset_id for u in service.review_unsupported_sources(root)}
    assert recorded == {ids[GAP], ids[SWITCH]}
    monkeypatch.undo()  # 맞춤 규칙이 열린 판으로 앱을 다시 연다
    service = RawWorkspaceService()

    _preview(service, root, ids[FIRST])

    head = ReviewWorkspaceFacade(root).load_head().revision
    assert {c.source_id for c in head.clocks} == set(ids.values())
    assert service.review_unsupported_sources(root) == ()
    for name, frame_ticks in ((GAP, 20), (SWITCH, 24)):
        (segment,) = [s for s in head.segments if s.source_id == ids[name]]
        clock = _clock(service, root, ids[name])
        assert clock.conform_kind == "map"
        assert (segment.start_pts, segment.end_pts) == (
            clock.first_pts,
            clock.first_pts + clock.frame_count * frame_ticks,  # 명목 끝 — 슬롯 수 × T
        )
    plans = service.list_segment_plan_revisions(root)
    _preview(service, root, ids[GAP])  # 그 영상도 미리보기가 선다 — 다시 계획하지 않는다
    assert service.list_segment_plan_revisions(root) == plans


def test_a_map_source_thumbnail_is_its_slot_frame_under_the_tagged_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """준비가 원본 시계를 재 조사 기록에 맞춤 판을 남긴다 — 질의는 ffprobe 없이 같은 키를 쓴다.

    썸네일 번호는 슬롯이고 그림은 그 슬롯이 보이는 패킷이다 — 빈 슬롯 뒤의 슬롯 k는 패킷 k−1이다.
    """
    service, root, ids = _library(tmp_path, monkeypatch, snap_only=False)
    _preview(service, root, ids[GAP])
    workspace = service._workspace(root)
    head = ReviewWorkspaceFacade(root).load_head().revision
    (segment,) = [s for s in head.segments if s.source_id == ids[GAP]]
    review_clock = head.clock_for(ids[GAP])
    assert review_clock is not None
    frame_index, _, _ = visual_review._snapped_fields(
        review_clock, segment.start_pts, segment.end_pts
    )
    sha = review_clock.source_sha256
    assert visual_review._inventory_mapped(workspace, sha) is True
    tagged = visual_review.thumbnail_media_key(sha, frame_index, mapped=True)
    assert visual_review._thumb_key(workspace, sha, frame_index) == tagged
    thumb = workspace.root / visual_review._thumb_relative(tagged)
    untagged = visual_review.thumbnail_media_key(sha, frame_index)
    assert thumb.is_file()
    assert not (workspace.root / visual_review._thumb_relative(untagged)).exists()
    clock = _clock(service, root, ids[GAP])
    assert clock.slot_packets[frame_index] == frame_index - 1  # 빈 슬롯(5) 뒤
    expected = preprocess.extract_frame_index(
        workspace.source_media_path(workspace.source_asset(ids[GAP])),
        clock.stream_index,
        frame_index,
        tmp_path / "expected.jpg",
        long_edge=visual_review.VISUAL_THUMB_LONG_EDGE,
        slot_packets=clock.slot_packets,
    )
    assert thumb.read_bytes() == expected.read_bytes()
    first_sha = workspace.source_asset(ids[FIRST]).content_sha256
    assert first_sha is not None and visual_review._inventory_mapped(workspace, first_sha) is False


def _count_preparation_scans(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """준비(`_decode_slots`)가 부른 패킷 훑기만 센다 — 계획의 측정은 D70 앞에도 있었다."""
    real = preprocess.probe_extraction_clock
    scans: list[str] = []

    def counted(path: Path, **kwargs: Any) -> preprocess.RawVideoClock:
        if any(frame.name == "_decode_slots" for frame in traceback.extract_stack()):
            scans.append(Path(path).name)
        return real(path, **kwargs)

    monkeypatch.setattr(preprocess, "probe_extraction_clock", counted)
    return scans


def test_preparing_a_uniform_source_scans_no_packets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """균일·snap 원본의 준비는 D70 앞과 같은 ffprobe만 — 계획이 남긴 표지가 「map 아님」이다.

    map 원본만 슬롯 대응을 얻으려고 훑는다. 표지가 지워지면(D70 앞의 머리·캐시 정리) 한 번 다시
    재고 표지를 남겨 다음 준비는 다시 훑지 않는다.
    """
    scans = _count_preparation_scans(monkeypatch)
    service, root, ids = _library(tmp_path, monkeypatch, snap_only=False)
    assert scans == []  # 첫 원본(균일)의 미리보기 준비
    _preview(service, root, ids[GAP])
    assert scans == [GAP]  # map은 대응을 얻으려고 훑는다
    workspace = service._workspace(root)
    first_sha = workspace.source_asset(ids[FIRST]).content_sha256
    assert first_sha is not None
    marker = visual_review._grid_marker(root, first_sha)
    assert json.loads(marker.read_text(encoding="utf-8"))["mapped"] is False
    marker.unlink()
    scans.clear()
    for _ in range(2):
        visual_review._select_source(
            workspace,
            ReviewWorkspaceFacade(root).load_head().revision,
            ids[FIRST],
            explicit=False,
            ordinal=None,
        )
    assert scans == [FIRST]  # 표지가 없으면 한 번 재고 남긴다
    assert marker.is_file()


def _sent(service: RawWorkspaceService, root: Path) -> tuple[ET.Element, dict[str, Any]]:
    path = service.export_finalcut_timeline(root)
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    return ET.parse(path).getroot(), report


def _seconds(value: str) -> Fraction:
    assert value.endswith("s"), value
    return Fraction(value[:-1])


def _rate_card(tmp_path: Path, name: str) -> Path:
    """한 레이트의 카드 — Final Cut 타임라인은 섞인 프레임레이트를 거부한다(D56, 맞추지 않는다)."""
    card = tmp_path / "card"
    if name == GAP:
        plain = make_plain(tmp_path / "work" / "plain.mov", seconds="4")
        first = make_plain(tmp_path / "work" / "first.mov", seconds="2")  # 30fps CFR
        shutil.copyfile(first, _mkdir(card) / FIRST)
        make_gap(plain, card / GAP)
    else:
        make_cfr(card / FIRST, "2")  # 25fps — 이어 붙이기의 명목과 같다
        make_switch(card / SWITCH)
    return card


def _mkdir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


@pytest.mark.parametrize("name", [GAP, SWITCH])
@pytest.mark.parametrize("with_head", [False, True])
def test_map_sources_export_with_their_nominal_length(
    tmp_path: Path, with_head: bool, name: str
) -> None:
    """빈 슬롯·속도 전환 원본도 보낸다 — 자산 길이는 명목(슬롯 수 × 명목 프레임 길이)이다."""
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "격자 map")
    _ingest_card(service, root, _rate_card(tmp_path, name))
    ids = {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}
    if with_head:
        service.ensure_source_order(root)
        _preview(service, root, ids[name])  # 검토 머리의 시계로 보낸다

    document, report = _sent(service, root)

    assert report["not_exported"] == []
    clock = _clock(service, root, ids[name])
    assert clock.conform_kind == "map"
    assets = document.findall("./resources/asset")
    (asset,) = [a for a in assets if Path(name).stem in a.attrib["name"]]
    assert _seconds(asset.attrib["duration"]) == clock.frame_count * clock.frame_duration
