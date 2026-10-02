"""A2 원본 순서 — 자연 정렬·제안·불변 리비전의 계약(계획 §6.2).

자연 정렬의 이유는 하나다: 카메라의 연속 파일명을 사전순으로 놓으면
`DJI_0010`이 `DJI_0002`보다 앞에 온다. 확정은 언제나 사람의 명시적 확인이고,
한 번 만든 리비전은 다음 확정이 생겨도 그대로다.
"""

from __future__ import annotations

import re

import pytest

from vlogsrt.project.raw import source_order
from vlogsrt.project.raw.models import (
    ORDER_BASIS_USER_CONFIRMED,
    SourceAsset,
    SourceOrderEntry,
)

ASSET_ID_PATTERN = re.compile(r"^(source|order)_[0-9a-f]{16}$")


def _asset(
    asset_id: str,
    relative: str,
    capture_time: str | None = None,
) -> SourceAsset:
    return SourceAsset(
        asset_id=asset_id,
        batch_dir="import_000001",
        relative_path=f"originals/import_000001/{relative}",
        status="pending",
        size_bytes=10,
        source_root="/tmp/card",
        source_relative_path=relative,
        source_name=relative.rsplit("/", 1)[-1],
        content_sha256=None,
        file_id=None,
        duplicate_of=None,
        capture_time=capture_time,
        duration_seconds=None,
        width=None,
        height=None,
        error=None,
        source_mtime_ns=1,
        source_inode=1,
        ordinal=0,
        created_at="2026-10-02T00:00:00+00:00",
        updated_at="2026-10-02T00:00:00+00:00",
    )


def test_natural_key_orders_numbers_by_value_not_spelling() -> None:
    """`2`가 `10`보다 앞에 선다 — 사전식 `0010 < 0002`를 뒤집는다(§6.2)."""
    names = ["DJI_0010.MP4", "DJI_0002.MP4", "DJI_0001.MP4"]
    ordered = sorted(names, key=source_order.natural_key)
    assert ordered == ["DJI_0001.MP4", "DJI_0002.MP4", "DJI_0010.MP4"]
    assert sorted(["clip10.mp4", "clip2.mp4"], key=source_order.natural_key) == [
        "clip2.mp4",
        "clip10.mp4",
    ]


def test_propose_order_with_unknown_capture_times_is_natural_path_order() -> None:
    """촬영 시각을 모르면(현재 A2) 자연 번호·경로 정렬이 제안이다."""
    candidates = [
        _asset("source_b", "DCIM/100MEDIA/DJI_0010.MP4"),
        _asset("source_a", "DCIM/100MEDIA/DJI_0002.MP4"),
        _asset("source_c", "DCIM/099MEDIA/DJI_0001.MP4"),
    ]
    ordered = source_order.propose_order(candidates)
    assert [item.asset_id for item in ordered] == ["source_c", "source_a", "source_b"]


def test_propose_order_known_capture_times_come_first() -> None:
    """촬영 시각을 아는 항목이 시각순으로 앞서고, 모르는 항목은 그 뒤에 자연 정렬된다."""
    candidates = [
        _asset("unknown", "DJI_0001.MP4"),
        _asset("later", "DJI_0009.MP4", capture_time="2026-10-02T09:00:00+00:00"),
        _asset("earlier", "DJI_0008.MP4", capture_time="2026-10-02T08:00:00+00:00"),
    ]
    ordered = source_order.propose_order(candidates)
    assert [item.asset_id for item in ordered] == ["earlier", "later", "unknown"]


def test_basis_note_distinguishes_uninspected_from_known() -> None:
    """미조사는 미조사라고 적는다 — 빈값이나 지어낸 시각과 다르다(§6.2)."""
    assert source_order.capture_basis_note(None) == source_order.CAPTURE_BASIS_UNKNOWN
    assert (
        source_order.capture_basis_note("2026-10-02T08:00:00+00:00")
        == "capture_time:2026-10-02T08:00:00+00:00"
    )


def test_revision_entries_number_the_confirmed_proposal() -> None:
    """확정 순번은 제안 그대로 1부터 붙는다 — 자동 재배열은 없다."""
    ordered = source_order.propose_order(
        [_asset("source_b", "DJI_0010.MP4"), _asset("source_a", "DJI_0002.MP4")]
    )
    entries = source_order.revision_entries(ordered)
    assert [(entry.asset_id, entry.ordinal) for entry in entries] == [
        ("source_a", 1),
        ("source_b", 2),
    ]
    assert entries[0].basis_note == source_order.CAPTURE_BASIS_UNKNOWN


def test_user_confirmed_revision_is_a_plain_immutable_record() -> None:
    """리비전은 값 기록이다 — 만든 뒤 고치는 경로가 존재하지 않는다."""
    ordered = source_order.propose_order([_asset("source_a", "a.MP4")])
    revision = source_order.user_confirmed_revision(
        ordered, "order_0123456789abcdef", "import_000001", "2026-10-02T00:00:00+00:00"
    )
    assert revision.basis == ORDER_BASIS_USER_CONFIRMED
    assert revision.entries == (SourceOrderEntry("source_a", 1, revision.entries[0].basis_note),)
    with pytest.raises(Exception):  # noqa: B017 - frozen 기록은 고칠 수 없다
        revision.entries[0].ordinal = 5  # type: ignore[misc]


def test_workspace_id_shapes_stay_opaque() -> None:
    """자산·리비전 ID 표기는 불투명 128비트 형태다 — 파일명·제목이 ID가 아니다."""
    from vlogsrt.project.raw.workspace import new_order_revision_id, new_source_asset_id

    assert ASSET_ID_PATTERN.match(new_source_asset_id())
    assert ASSET_ID_PATTERN.match(new_order_revision_id())
