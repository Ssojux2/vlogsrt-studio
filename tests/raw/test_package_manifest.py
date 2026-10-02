"""A3 전달물 — 폴더·파일 이름, 수식 방지 CSV, 불변 manifest(계획 §9.1·§9.3).

ffmpeg 없이 값만으로 부린다: 이름 규칙(원본 순번 폴더·조각 순번·틱 밀리초·지문),
CSV 머리와 수식 주입 방지, utf-8-sig·CRLF, 그리고 **있으면 죽는** manifest 쓰기
(옛 package를 다시 쓰는 길이 아예 없다, §6.5).
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw import package as package_layout
from vlogsrt.project.raw.models import ClipArtifact, PackageRevision, SegmentPlanEntry


def _entry(**overrides) -> SegmentPlanEntry:
    base = SegmentPlanEntry(
        segment_id="seg_abc123",
        asset_id="source_1",
        batch_dir="import_000001",
        source_order=2,
        segment_order=3,
        time_base_num=1,
        time_base_den=90000,
        core_start_pts=1116000,
        core_end_pts=2592000,
        media_start_pts=1116000,
        media_end_pts=2592000,
        core_first_frame=31,
        core_after_last_frame=72,
        source_frame_count=150,
        recommendation="normal",
        basis="deterministic_tiling",
    )
    return replace(base, **overrides) if overrides else base


def _clip(**overrides) -> ClipArtifact:
    base = ClipArtifact(
        clip_id="clip_abc",
        package_dir="clips/package_000001",
        segment_id="seg_abc123",
        asset_id="source_1",
        source_order=2,
        segment_order=3,
        profile_name="source_matched",
        relative_path="clips/package_000001/000002_원본/000003__x.mp4",
        status="published",
        content_sha256="a" * 64,
        size_bytes=1234,
        file_id="media_1",
        time_base_num=1,
        time_base_den=90000,
        core_start_pts=1116000,
        core_end_pts=2592000,
        media_start_pts=1116000,
        media_end_pts=2592000,
        core_local_start="0",
        core_local_end="1476000/90000",
        media_local_duration="1476000/90000",
        frame_count=41,
        error=None,
        created_at="2026-10-02T00:00:00+00:00",
        updated_at="2026-10-02T00:00:00+00:00",
    )
    return replace(base, **overrides) if overrides else base


def _package() -> PackageRevision:
    return PackageRevision(
        package_dir="clips/package_000001",
        plan_revision_id="splan_1",
        order_revision_id="gorder_1",
        profile_name="source_matched",
        handles_before="0",
        handles_after="0",
        job_fingerprint="f" * 64,
        status="complete",
        clip_total=1,
        clip_published=1,
        error=None,
        created_at="2026-10-02T00:00:00+00:00",
        updated_at="2026-10-02T00:00:00+00:00",
    )


def _asset_named(name: str):
    from vlogsrt.project.raw.models import SourceAsset

    return SourceAsset(
        asset_id="source_1",
        batch_dir="import_000001",
        relative_path="originals/import_000001/x",
        status="ready",
        size_bytes=1,
        source_root="/card",
        source_relative_path=name,
        source_name=name,
        content_sha256="b" * 64,
        file_id=None,
        duplicate_of=None,
        capture_time=None,
        duration_seconds=None,
        width=None,
        height=None,
        error=None,
        source_mtime_ns=0,
        source_inode=0,
        ordinal=1,
        created_at="2026-10-02T00:00:00+00:00",
        updated_at="2026-10-02T00:00:00+00:00",
    )


# ── 이름 규칙 ────────────────────────────────────────────────────────────────


def test_source_folder_name_uses_global_order_and_sanitizes() -> None:
    assert package_layout.source_folder_name(1, "DJI_0001.MP4") == "000001_DJI_0001.MP4"
    assert package_layout.source_folder_name(12, "여행 영상.mp4") == "000012_여행 영상.mp4"
    # 경로 구분은 밑줄로, 앞 점은 떼낸다 — 폴더 이름이 경로를 만들지 못하게 한다.
    assert package_layout.source_folder_name(3, "../etc/passwd") == "000003_etc_passwd"
    assert package_layout.source_folder_name(4, "...") == "000004_source"


def test_clip_filename_carries_order_ticks_and_digest() -> None:
    entry = _entry()
    name = package_layout.clip_filename(entry, "mp4")
    # core 12.4초~28.8초 → t000012400-t000028800, 지문 4자리, 컨테이너 확장자.
    assert name.startswith("000003__t000012400-t000028800__c_")
    assert name.endswith(".mp4")
    assert name.split("__c_")[1][:4] == package_layout.clip_digest(entry)[:4]


def test_next_package_dir_never_reuses() -> None:
    assert package_layout.next_package_dir([]) == "clips/package_000001"
    taken = ["clips/package_000001", "clips/package_000002"]
    assert package_layout.next_package_dir(taken) == "clips/package_000003"
    # 중간이 비어도 다음 번호 — 건너뛰지 않는다.
    assert package_layout.next_package_dir(["clips/package_000001"]) == "clips/package_000002"


def test_clip_relative_path_layers_order_then_segment() -> None:
    entry = _entry()
    relative = package_layout.clip_relative_path(
        "clips/package_000001", entry, _asset_named("DJI_0001.MP4"), "mp4"
    )
    parts = relative.split("/")
    assert parts[:2] == ["clips", "package_000001"]
    assert parts[2] == "000002_DJI_0001.MP4"
    assert parts[3].startswith("000003__")


# ── CSV — 머리·수식 방지·인코딩 ──────────────────────────────────────────────


def test_formula_safe_cell_guards_dangerous_prefixes() -> None:
    assert package_layout.formula_safe_cell("=cmd()") == "'=cmd()"
    assert package_layout.formula_safe_cell("+1") == "'+1"
    assert package_layout.formula_safe_cell("-2") == "'-2"
    assert package_layout.formula_safe_cell("@x") == "'@x"
    assert package_layout.formula_safe_cell("\tTAB") == "'\tTAB"
    assert package_layout.formula_safe_cell("=1") == "'=1"
    # 정상 값은 그대로 — 빈 칸과 막은 칸을 구분한다.
    assert package_layout.formula_safe_cell("여행 영상.mp4") == "여행 영상.mp4"
    assert package_layout.formula_safe_cell("") == ""


def test_clip_index_csv_header_and_unicode_rows() -> None:
    """§9.3의 머리 순서, 한국어 파일명, 유리수 길이의 고정소수 표기."""
    clip = _clip()
    csv_text = package_layout.clip_index_csv([clip], {"source_1": _asset_named("여행 영상.mp4")})
    header = csv_text.splitlines()[0]
    assert header == ",".join(package_layout.CSV_FIELDS)
    assert "여행 영상.mp4" in csv_text
    assert "000002" in csv_text and "000003" not in csv_text.replace("000003__x", "")
    # core_start 1116000/90000 = 12.4초 — 틱에서 온 표기다.
    assert "12.400000" in csv_text
    assert "16.400000" in csv_text  # duration 1476000/90000


def test_clip_index_csv_escapes_formula_looking_cells() -> None:
    """파일 이름이 수식처럼 생겼으면 홑따옴표로 막는다 — 실행되지 않는다(§9.3)."""
    asset = _asset_named("=HYPERLINK(x).mp4")
    clip = _clip()
    csv_text = package_layout.clip_index_csv([clip], {"source_1": asset})
    assert ",'=HYPERLINK(x).mp4," in csv_text  # 칸 경계에서 홑따옴표로 시작한다
    assert ",=HYPERLINK" not in csv_text  # 막지 않은 칸 시작은 없다


def test_clips_jsonl_keeps_exact_integer_ticks() -> None:
    """jsonl은 정수 틱·유리수 문자열 그대로 — 밀리초 환산은 CSV에만 있다(§13.3)."""
    clip = _clip()
    payload = package_layout.clips_jsonl(_package(), {"source_1": _asset_named("a.mp4")}, [clip])
    assert '"core_range_pts": [1116000, 2592000]' in payload
    assert '"time_base": {"den": 90000, "num": 1}' in payload
    assert '"media_local_duration": "1476000/90000"' in payload


def test_readme_lists_unpublished_clips_openly() -> None:
    """실패 조각을 숨기지 않는다 — 안내 문서에도 이름과 이유가 남는다(§6.3)."""
    published = _clip()
    failed = _clip(
        clip_id="clip_bad",
        status="failed",
        error="RawClipVerificationError: 프레임 수가 다릅니다",
        content_sha256=None,
    )
    text = package_layout.readme_text(
        _package(), {"source_1": _asset_named("a.mp4")}, [published, failed]
    )
    assert "clip_bad" in text
    assert "프레임 수가 다릅니다" in text
    assert "게시되지 않은 조각" in text


def test_write_package_manifest_writes_and_refuses_rewrite(tmp_path: Path) -> None:
    """전달물 세 개를 쓰고, 같은 package에 다시 쓰려 하면 죽는다(§6.5)."""
    clip = _clip()
    package = _package()
    written = package_layout.write_package_manifest(
        tmp_path, package, {"source_1": _asset_named("a.mp4")}, [clip]
    )
    assert written == (
        "clips/package_000001/clips.jsonl",
        "clips/package_000001/clip_index.csv",
        "clips/package_000001/README.md",
    )
    csv_bytes = (tmp_path / "clips/package_000001/clip_index.csv").read_bytes()
    # utf-8-sig(BOM)·CRLF — 한국어 파일명이 스프레드시트에서 깨지지 않게 한다.
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert b"\r\n" in csv_bytes
    with pytest.raises(FileExistsError, match="덮어쓰지 않는다"):
        package_layout.write_package_manifest(
            tmp_path, package, {"source_1": _asset_named("a.mp4")}, [clip]
        )


def test_fraction_helpers_stay_exact() -> None:
    """내부 환산 — 유리수 초의 밀리초 천장·6자리 고정소수(§13.3 마지막 단계만 반올림)."""
    assert package_layout._ceil_ms(Fraction("2.0004")) == 2001
    assert package_layout._ceil_ms(Fraction(180000, 90000)) == 2000
