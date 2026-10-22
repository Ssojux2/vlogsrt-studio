"""W-25e — 모든 영상 시각 맞춤 일괄 적용(D75).

유료 호출은 없다(적용은 무료다). 시험이 못 박는 것:

- 확인(`apply_snap_all_preview`)은 아무것도 쓰지 않고, 영상마다 적용할 수 있는지(제안 sha·바뀔
  줄 수)나 그 까닭(다시 듣기 대기 · 이미 적용함 · 바꿀 시각 없음)을 보인다. 묶음 토큰은 적용할
  영상과 그 sha를 묶는다.
- 적용(`apply_snap_all`)은 확인 때의 토큰이 지금과 같을 때만 쓴다 — 다르면 아무 영상도 쓰지
  않는다. 같으면 적용할 수 있는 영상마다 그 sha로 `apply_snap`(19h)을 부른다 — 막힘·한 걸음·
  sha 대조가 그대로다.
  화면이 붙잡아 달라고 한 영상(분석 중)은 건너뛰고 까닭을 남긴다.
- 적용한 영상마다 되돌리기가 그대로다.
"""

from __future__ import annotations

import shutil
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests._sync_fakes import energy_factory, make_media, numpy_or_fake
from tests.raw.test_sync_snap_apply import _wait, snap_rows, wait_sync
from vlogsrt.project.raw import asr_epoch, source_transcript, sync_report
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import RawWorkspaceError

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


@pytest.fixture
def two(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, list[str]]:
    """이른 원본 자막을 심고 시각 확인까지 끝낸 톤 덩어리 원본 둘."""
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    card = tmp_path / "card"
    make_media(card / "tone_a.mov")
    make_media(card / "tone_b.mov", audio_delay="0.5")
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "모든 영상 시각 맞춤")
    proposal = service.scan_sources(root, card)
    copied = service.start_ingest_copy(root, service.confirm_ingest(root, proposal))
    assert _wait(service, root, copied.job_id).status == "succeeded"
    service.confirm_global_source_order(root)
    workspace = service._workspace(root)
    ids: list[str] = []
    for asset, origin in zip(
        service.list_source_assets(root), (Fraction(0), Fraction(1, 2)), strict=True
    ):
        rows = snap_rows(origin)
        source_transcript.seed(workspace, asset.asset_id, rows, Fraction(40), asset.content_sha256)
        ids.append(asset.asset_id)
    assert service.start_sync_checks(root, tuple(ids)) is not None
    wait_sync(service, root)
    return service, root, ids


def _sequences(service: Any, root: Path, ids: list[str]) -> list[int]:
    workspace = service._workspace(root)
    return [source_transcript.load(workspace, asset_id)["sequence"] for asset_id in ids]


def test_the_preview_lists_every_ready_video_and_writes_nothing(
    two: tuple[Any, Path, list[str]],
) -> None:
    service, root, ids = two
    preview = service.apply_snap_all_preview(root)
    assert [row.asset_id for row in preview.rows] == ids
    for row in preview.rows:
        single = service.apply_snap_preview(root, row.asset_id)
        assert row.sha256 == single.sha256 and row.changed == single.changes > 0
        assert row.reason is None
    assert [row.asset_id for row in preview.ready] == ids
    assert service.apply_snap_all_preview(root).token == preview.token  # 결정적이다
    assert _sequences(service, root, ids) == [0, 0]


def test_apply_all_writes_one_snap_revision_per_video(two: tuple[Any, Path, list[str]]) -> None:
    service, root, ids = two
    preview = service.apply_snap_all_preview(root)
    result = service.apply_snap_all(root, preview.token)
    assert [item.asset_id for item in result.applied] == ids
    assert [item.changed for item in result.applied] == [row.changed for row in preview.rows]
    assert result.skipped == ()
    workspace = service._workspace(root)
    for asset_id in ids:
        document = source_transcript.load(workspace, asset_id)
        assert source_transcript.snap_applied(document) is not None
    again = service.apply_snap_all_preview(root)
    assert again.ready == ()
    assert {row.reason for row in again.rows} == {raw.SNAP_APPLIED_ONCE}  # 한 걸음
    for asset_id in ids:  # 되돌리기는 영상마다 그대로다
        current = source_transcript.load(workspace, asset_id)
        service.revert_snap(root, asset_id, current["revision"])
        assert source_transcript.load(workspace, asset_id)["edited"] is False
    wait_sync(service, root)


def test_a_video_waiting_to_relisten_is_shown_and_left_alone(
    two: tuple[Any, Path, list[str]],
) -> None:
    service, root, ids = two
    asr_epoch.bump(service._workspace(root), ids[1])
    preview = service.apply_snap_all_preview(root)
    assert [row.asset_id for row in preview.ready] == [ids[0]]
    waiting = {row.asset_id: row for row in preview.rows}[ids[1]]
    assert waiting.sha256 is None and waiting.reason == raw.SNAP_RELISTEN_PENDING
    result = service.apply_snap_all(root, preview.token)
    assert [item.asset_id for item in result.applied] == [ids[0]]
    assert _sequences(service, root, ids) == [1, 0]
    wait_sync(service, root)


def test_a_changed_proposal_after_the_preview_writes_nothing(
    two: tuple[Any, Path, list[str]],
) -> None:
    service, root, ids = two
    preview = service.apply_snap_all_preview(root)
    single = service.apply_snap_preview(root, ids[0])
    service.apply_snap(root, ids[0], single.sha256)  # 그사이 한 영상을 따로 적용했다
    with pytest.raises(RawWorkspaceError) as refused:
        service.apply_snap_all(root, preview.token)
    assert str(refused.value) == f"{raw.SNAP_ALL_CHANGED} {raw.SNAP_NOTHING_CHANGED}"
    assert _sequences(service, root, ids) == [1, 0]  # 다른 영상은 그대로다
    wait_sync(service, root)


def test_videos_the_screen_holds_are_skipped_with_the_reason(
    two: tuple[Any, Path, list[str]],
) -> None:
    service, root, ids = two
    preview = service.apply_snap_all_preview(root)
    result = service.apply_snap_all(root, preview.token, held={ids[0]: raw.SNAP_ANALYSIS_RUNNING})
    assert [item.asset_id for item in result.applied] == [ids[1]]
    assert [(item.asset_id, item.reason) for item in result.skipped] == [
        (ids[0], raw.SNAP_ANALYSIS_RUNNING)
    ]
    assert _sequences(service, root, ids) == [0, 1]
    wait_sync(service, root)


def test_a_refusal_in_the_middle_skips_that_video_and_keeps_going(
    two: tuple[Any, Path, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, ids = two
    preview = service.apply_snap_all_preview(root)
    original = service.apply_snap

    def refuse_first(root_: Path, asset_id: str, sha256: str) -> dict:
        if asset_id == ids[0]:
            raise RawWorkspaceError(raw.SNAP_CHANGED)
        return original(root_, asset_id, sha256)

    monkeypatch.setattr(service, "apply_snap", refuse_first)
    result = service.apply_snap_all(root, preview.token)
    assert [item.asset_id for item in result.applied] == [ids[1]]
    assert [(item.asset_id, item.reason) for item in result.skipped] == [(ids[0], raw.SNAP_CHANGED)]
    wait_sync(service, root)


def test_untranscribed_videos_are_not_listed(tmp_path: Path) -> None:
    service = raw.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "빈 작업공간")
    preview = service.apply_snap_all_preview(root)
    assert preview.rows == () and preview.ready == ()
    assert service.apply_snap_all(root, preview.token).applied == ()
