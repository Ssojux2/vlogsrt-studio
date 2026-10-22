"""맞춘 원본이 라이브러리에 든다 — 다시 계획의 계기와 Final Cut 보내기(W-27b, D69).

W-27a(D68)는 격자를 증명하지 못한 원본을 계획의 `unsupported`에 기록했다. 그 기록은 그때의
측정이라, 맞춤 규칙(D69)이 생긴 뒤에도 옛 머리를 쓰는 한 「분석 지원 밖」으로 남았다. 손댄
머리에서 순서 옮기기가 일어나면 새 계획이 그 원본의 조각을 내어 매핑이 실패하고 미리보기 작업이
매번 실패했다(D68 (e), 검증 지적). 여기서는 W-27a 판 측정을 정책 하나로 흉내 낸다 — 같은 원본을
아주 좁은 맞춤 정책으로 재면 「지원 밖」으로 기록된다. 그 뒤 기본 정책으로 앱을 다시 연다(새
서비스).
유료 호출 0회 — 합성 ffmpeg 미디어만 쓴다.
"""

from __future__ import annotations

import json
import shutil
import xml.etree.ElementTree as ET
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_grid_conform import make_plain, make_twin
from tests.raw.test_review_workspace import _ingest_card, _wait_terminal
from tests.raw.test_vfr_library import make_cfr
from vlogsrt import preprocess
from vlogsrt.preprocess import GridConformPolicy
from vlogsrt.project.library import RAD_ASSET, LibraryMedia
from vlogsrt.project.raw import finalcut_export
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import SELECTION_EXCLUDED
from vlogsrt.project.raw.service import UPKEEP_JOB_KIND, RawWorkspaceService

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

W27A_LIKE = GridConformPolicy(half_frame_bound=Fraction(1, 1000))
"""W-27a 판처럼 한 틱의 지터도 받지 않는 정책 — 맞춤 이전의 기록을 만든다."""

FIRST, TWIN, LAST = "01_first.mov", "02_iphone.mov", "03_last.mov"
MEMO = "사람 메모 — 첫 장면"


def _card(tmp_path: Path) -> Path:
    card = tmp_path / "card"
    make_cfr(card / FIRST, "2")
    make_twin(make_plain(tmp_path / "work" / "plain.mov"), card / TWIN)
    make_cfr(card / LAST, "2")
    return card


def _preview(service: RawWorkspaceService, root: Path, asset_id: str) -> None:
    record = _wait_terminal(service, root, service.start_source_preview(root, (asset_id,)).job_id)
    assert record.status == "succeeded", record.error


def _recorded_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, touched: bool
) -> tuple[Path, dict[str, str]]:
    """W-27a 판의 머리 — 지터 원본은 「지원 밖」으로 기록됐다. 손댔으면 첫 원본에 사람 결정."""
    card = _card(tmp_path)
    monkeypatch.setattr(preprocess, "RAW_GRID_CONFORM", W27A_LIKE)
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "격자 맞춤")
    _ingest_card(service, root, card)
    service.ensure_source_order(root)
    ids = {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}
    _preview(service, root, ids[FIRST])
    assert [u.asset_id for u in service.review_unsupported_sources(root)] == [ids[TWIN]]
    if touched:
        facade = ReviewWorkspaceFacade(root)
        head = facade.load_head().revision
        first = next(s for s in head.segments if s.source_id == ids[FIRST])
        facade.set_selection(head.base_plan_id, first.segment_id, SELECTION_EXCLUDED)
        facade.set_notes(head.base_plan_id, first.segment_id, MEMO)
    monkeypatch.undo()  # 맞춤 규칙이 생긴 판으로 앱을 다시 연다
    return root, ids


def _assert_covered(service: RawWorkspaceService, root: Path, ids: dict[str, str]) -> Any:
    head = ReviewWorkspaceFacade(root).load_head().revision
    assert {c.source_id for c in head.clocks} == set(ids.values())
    assert service.review_unsupported_sources(root) == ()
    twin = [s for s in head.segments if s.source_id == ids[TWIN]]
    assert len(twin) == 1  # 새로 든 영상처럼 한 장
    clock = preprocess.probe_extraction_clock(
        service._workspace(root).source_media_path(service._workspace(root).source_asset(ids[TWIN]))
    )
    assert (twin[0].start_pts, twin[0].end_pts) == (
        clock.first_pts,
        clock.first_pts + clock.frame_count * 20,  # 명목 끝
    )
    return head


def _human_kept(head: Any, ids: dict[str, str]) -> None:
    (first,) = [s for s in head.segments if s.source_id == ids[FIRST]]
    assert (first.selection, first.notes) == (SELECTION_EXCLUDED, MEMO)


# ── 다시 계획의 계기 — 기록된 지원 밖 원본이 지금 덮인다 ──────────────────────────


def test_an_untouched_head_relays_when_the_recorded_source_is_now_coverable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ids = _recorded_library(tmp_path, monkeypatch, touched=False)
    service = RawWorkspaceService()

    _preview(service, root, ids[FIRST])

    _assert_covered(service, root, ids)
    plans = service.list_segment_plan_revisions(root)
    _preview(service, root, ids[TWIN])  # 이제 그 영상도 미리보기가 선다 — 다시 계획하지 않는다
    assert service.list_segment_plan_revisions(root) == plans


def test_a_touched_head_relays_and_keeps_the_human_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ids = _recorded_library(tmp_path, monkeypatch, touched=True)
    service = RawWorkspaceService()

    _preview(service, root, ids[LAST])

    _human_kept(_assert_covered(service, root, ids), ids)


def test_a_touched_head_with_a_reorder_adopts_the_newly_covered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D68 (e): 순서 옮기기의 새 계획이 그 원본의 조각을 내도 매핑이 받는다 — 미리보기가 산다."""
    root, ids = _recorded_library(tmp_path, monkeypatch, touched=True)
    service = RawWorkspaceService()
    order = [ids[LAST], ids[TWIN], ids[FIRST]]
    latest = service.confirm_global_source_order(root, order)
    facade = ReviewWorkspaceFacade(root)
    before = facade.load_head().revision

    _, mapping = facade.plan_from_review(before.base_plan_id, order_revision_id=latest.revision_id)
    assert mapping.covered_sources == (ids[TWIN],)  # 짝 없이 적힌 새로 덮인 원본
    assert len(mapping.pairs) == len(before.segments)  # 옛 조각은 모두 짝이 있다
    assert json.loads(mapping.as_document())["covered_sources"] == [ids[TWIN]]
    assert service.align_review_order(root) is True

    head = _assert_covered(service, root, ids)
    _human_kept(head, ids)
    seen = [s.source_id for s in ReviewWorkspaceFacade(root).chronological(head)]
    assert list(dict.fromkeys(seen)) == order  # 새 순서 위에
    _preview(service, root, ids[TWIN])  # 미리보기 작업이 실패하지 않는다


def test_a_mapping_without_newly_covered_sources_keeps_its_document_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ids = _recorded_library(tmp_path, monkeypatch, touched=True)
    service = RawWorkspaceService()
    _preview(service, root, ids[FIRST])  # 이제 다 덮었다
    facade = ReviewWorkspaceFacade(root)
    _, mapping = facade.plan_from_review(facade.load_head().revision.base_plan_id)
    assert mapping.covered_sources == ()
    assert "covered_sources" not in json.loads(mapping.as_document())


def test_upkeep_lays_the_newly_covered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, ids = _recorded_library(tmp_path, monkeypatch, touched=True)
    service = RawWorkspaceService()

    pending = service._upkeep_pending(root)
    assert pending[raw_service.UPKEEP_COVER].endswith(ids[TWIN])
    message = service.start_upkeep(root)
    assert message is not None and "흔들리던 영상" in message
    job = next(j for j in service.list_jobs(root) if j.kind == UPKEEP_JOB_KIND)
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"

    _human_kept(_assert_covered(service, root, ids), ids)
    assert raw_service.UPKEEP_COVER not in service._upkeep_pending(root)


def test_a_source_that_is_still_refused_does_not_replan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """기록과 지금 측정이 같은 판정이면(정책을 되돌리지 않음) 다시 계획하지 않는다."""
    card = _card(tmp_path)
    monkeypatch.setattr(preprocess, "RAW_GRID_CONFORM", W27A_LIKE)
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "그대로")
    _ingest_card(service, root, card)
    service.ensure_source_order(root)
    ids = {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}
    _preview(service, root, ids[FIRST])
    plans = service.list_segment_plan_revisions(root)

    _preview(service, root, ids[LAST])

    assert service.list_segment_plan_revisions(root) == plans
    assert raw_service.UPKEEP_COVER not in service._upkeep_pending(root)


# ── Final Cut 보내기 — 회전 + 지터 원본 ──────────────────────────────────────


def _rotated_twin(tmp_path: Path) -> Path:
    """세로(90°) 아이폰 모양 — 마지막 패킷이 6틱 늦어 머리 정보가 명목보다 6틱 길다."""
    plain = make_plain(
        tmp_path / "work" / "plain.mov", seconds="2.51"
    )  # 76프레임 — 마지막(순번 75)이 6틱 늦다
    return make_twin(plain, tmp_path / "card" / "portrait.mov", rotate=True)


def _sent(service: RawWorkspaceService, root: Path) -> tuple[ET.Element, dict[str, Any]]:
    path = service.export_finalcut_timeline(root)
    report = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    return ET.parse(path).getroot(), report


@pytest.mark.parametrize("with_head", [False, True])
def test_a_rotated_jittered_source_exports(tmp_path: Path, with_head: bool) -> None:
    source = _rotated_twin(tmp_path)
    probe = preprocess.probe_raw_media(source)
    clock = preprocess.probe_extraction_clock(source, probe=probe)
    header = Fraction(probe.video.duration_ts or 0) / 20
    assert header != clock.frame_count  # 머리 정보가 명목 길이와 다르다 — 허용이 닿는 자리
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "세로 지터")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    (asset,) = service.list_source_assets(root)
    if with_head:
        service.ensure_source_order(root)
        _preview(service, root, asset.asset_id)  # 검토 머리의 시계로 보낸다

    document, report = _sent(service, root)

    assert report["not_exported"] == []
    (fmt,) = document.findall("./resources/format")
    assert (fmt.attrib["width"], fmt.attrib["height"]) == ("90", "160")
    (media,) = document.findall("./resources/asset")
    assert media.attrib["duration"] == f"{clock.frame_count}/30s"  # 명목 프레임 수


def test_a_uniform_source_header_must_still_match_exactly(tmp_path: Path) -> None:
    """맞춤 몫은 맞춘 원본에만 — 균일 원본의 머리 정보는 한 틱도 어긋날 수 없다."""
    probe = preprocess.probe_raw_media(make_cfr(tmp_path / "a.mov", "2"))
    longer = replace(probe.video, duration_ts=(probe.video.duration_ts or 0) + 1)
    probe = replace(probe, video=longer)
    asset = _asset("a.mov")
    with pytest.raises(finalcut_export.FinalCutExportError, match="편집 목록"):
        finalcut_export._require_plain_timeline(asset, 0, 50, Fraction(1, 25), probe)


def _asset(name: str) -> Any:
    from types import SimpleNamespace

    return SimpleNamespace(source_name=name)


def _declared(duration: Fraction) -> LibraryMedia:
    return LibraryMedia(
        name="IMG_8844",
        original=Path("IMG_8844.mov"),
        proxy=None,
        media_identifier="id",
        frame_duration=Fraction(1, 30),
        duration=duration,
        format_name="FFVideoFormat1080p30",
        has_video=True,
        has_audio=True,
        audio_rate=48000,
        audio_channels=2,
        media_start=Fraction(0),
        drop_frame=False,
        record_type=RAD_ASSET,  # 12.3 기록 — 길이가 프레임 수 선언이다(D72 이전 그대로)
    )


def test_a_library_declaration_may_carry_the_snapped_deviation_only() -> None:
    """Final Cut은 IMG_8844를 848.05프레임으로 적는다 — 어긋남 한 틱(1/20프레임) 안이면 848이다."""
    snapped = finalcut_export._Grid(Fraction(1, 30), 848, Fraction(1, 20))
    exact = finalcut_export._Grid(Fraction(1, 30), 848)
    declared = _declared(Fraction(16961, 600))
    assert finalcut_export._declared_frames(declared, snapped) == 848
    with pytest.raises(Exception, match="프레임 격자"):
        finalcut_export._declared_frames(declared, exact)  # 균일 원본은 예전처럼 거부
    beyond = _declared(Fraction(16962, 600))  # 두 틱 — 측정한 어긋남 밖
    with pytest.raises(Exception, match="프레임 격자"):
        finalcut_export._declared_frames(beyond, snapped)
