"""자막 시각 확인 보고서는 원본마다 가장 새 몇 개만 남긴다(W-15F ⑧, D165).

리비전마다 하나씩 쌓이던 `cache/sync-check/reports/<원본>/`를 쓸 때마다 정리한다 — 방금 쓴
보고서는 언제나 남고, 다른 원본의 폴더와 보고서가 아닌 파일은 건드리지 않는다. 기준은 확인
기준(`SyncCheckSettings`)의 칸이 아니어서 보고서 열쇠(`settings8`)를 바꾸지 않는다.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from vlogsrt.project.config import (
    SYNC_CHECK_SETTINGS,
    SYNC_REPORT_RETENTION,
    SyncCheckSettings,
    SyncReportRetention,
)
from vlogsrt.project.raw import sync_report


def _document(source_id: str, revision: str) -> dict[str, object]:
    return {"source": {"source_id": source_id, "transcript_revision": revision}}


def _write(cache: Path, source_id: str, revision: str, when: int, keep: int) -> Path:
    path = sync_report.write_report(
        cache,
        _document(source_id, revision),
        SYNC_CHECK_SETTINGS,
        retention=SyncReportRetention(keep_per_source=keep),
    )
    # 쓴 차례를 수정 시각으로 못 박는다(파일 시스템 눈금에 기대지 않는다).
    os.utime(path, (when, when))
    return path


def test_only_the_newest_reports_of_a_source_stay(tmp_path: Path) -> None:
    paths = [_write(tmp_path, "s1", f"r{index}", 1_000 + index, keep=3) for index in range(5)]
    left = sorted((tmp_path / "reports" / "s1").glob("*.json"))
    assert left == sorted(paths[-3:])


def test_the_report_just_written_survives_even_with_an_older_clock(tmp_path: Path) -> None:
    for index in range(3):
        _write(tmp_path, "s1", f"r{index}", 2_000 + index, keep=2)
    # 시계가 뒤로 간 기기 — 방금 쓴 보고서가 가장 낡아 보여도 지우지 않는다.
    newest = sync_report.write_report(
        tmp_path,
        _document("s1", "back"),
        SYNC_CHECK_SETTINGS,
        retention=SyncReportRetention(keep_per_source=2),
    )
    os.utime(newest, (1, 1))
    sync_report.prune_reports(tmp_path, "s1", newest, SyncReportRetention(keep_per_source=2))
    assert newest.exists()
    assert len(list((tmp_path / "reports" / "s1").glob("*.json"))) == 2


def test_other_sources_and_other_files_are_not_touched(tmp_path: Path) -> None:
    other = _write(tmp_path, "s2", "r0", 500, keep=1)
    note = tmp_path / "reports" / "s1" / "notes.txt"
    note.parent.mkdir(parents=True)
    note.write_text("사람 메모", encoding="utf-8")
    for index in range(3):
        _write(tmp_path, "s1", f"r{index}", 1_000 + index, keep=1)
    assert other.exists() and note.exists()
    assert len(list((tmp_path / "reports" / "s1").glob("*.json"))) == 1


def test_retention_is_not_part_of_the_check_settings_key() -> None:
    """보관 수를 바꿔도 보고서 열쇠(`settings8`)와 낡음 판정이 그대로다."""
    names = {field.name for field in dataclasses.fields(SyncCheckSettings)}
    assert "keep_per_source" not in names
    assert SYNC_REPORT_RETENTION.keep_per_source >= 2  # 지금 리비전 + 되돌릴 바로 앞 리비전


@pytest.mark.parametrize("keep", [0, -1, True])
def test_retention_refuses_broken_values(keep: object) -> None:
    with pytest.raises(ValueError):
        SyncReportRetention(keep_per_source=keep)  # type: ignore[arg-type]
