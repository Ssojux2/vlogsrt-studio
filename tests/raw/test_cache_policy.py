"""A6 캐시 정리 정책의 계약 — cache/·previews/ 만 지우고 보호 이름은 못 속인다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §9.2·16-A6 완성 기준: 앱을 종료하거나
캐시를 지워도 확정한 조각이 정상 재생된다. 여기선 그 절반 — 캐시를 지울 때 무엇이
살아남는지 — 을 증명한다. 잘못 붙은 보호 이름(cache/originals 따위)도 지우지 않고,
심볼릭 링크 안쪽으로 따라가지 않는다.
"""

from __future__ import annotations

import hashlib
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw import cache_policy
from vlogsrt.project.raw import confirmed_package as confirmed
from vlogsrt.project.raw.models import ClipArtifact, PackageRevision, SourceAsset
from vlogsrt.project.raw.segment_review import (
    SELECTION_INCLUDED,
    ReviewRevision,
    ReviewSegment,
    make_source_clock,
)

NOW = "2026-10-02T00:00:00+00:00"


def make_workspace(root: Path) -> None:
    layout = (
        "originals",
        "analysis",
        "clips",
        "previews",
        "review",
        "final",
        "subtitles",
        "cache",
        "jobs",
    )
    for name in layout:
        (root / name).mkdir(parents=True)
    (root / "project.toml").write_text('schema = "v2"\n', encoding="utf-8")
    (root / "project.sqlite3").write_bytes(b"sqlite")


def protected_fingerprint(root: Path) -> dict[str, str]:
    """보호 구역의 내용 지문 — 정리 뒤에도 한 글자도 바뀌지 않아야 한다."""
    fingerprint: dict[str, str] = {}
    names = ("originals", "analysis", "clips", "review", "final", "subtitles", "jobs")
    for top in names:
        for path in sorted((root / top).rglob("*")):
            if path.is_file():
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                fingerprint[path.relative_to(root).as_posix()] = digest
    for name in ("project.toml", "project.sqlite3"):
        fingerprint[name] = hashlib.sha256((root / name).read_bytes()).hexdigest()
    return fingerprint


# ── 지울 수 있는 것 — cache/·previews/ 의 내용물뿐 ───────────────────────────


def test_purge_removes_only_cache_and_previews_contents(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    (root / "originals/import_000001/DJI_0001.MP4").parent.mkdir(parents=True)
    (root / "originals/import_000001/DJI_0001.MP4").write_bytes(b"ORIGINAL")
    (root / "cache/audio/frames.bin").parent.mkdir(parents=True)
    (root / "cache/audio/frames.bin").write_bytes(b"x" * 10)
    (root / "cache/req.json").write_text("{}", encoding="utf-8")
    (root / "previews/seg.mp4").write_bytes(b"p" * 20)
    before = protected_fingerprint(root)
    plan = cache_policy.plan_cache_purge(root)
    assert set(plan.purge_paths) == {
        "cache/audio",
        "cache/audio/frames.bin",
        "cache/req.json",
        "previews/seg.mp4",
    }
    assert plan.refused == ()
    result = cache_policy.execute_cache_purge(root, plan)
    assert result.removed_files == 3
    assert result.removed_bytes == 32
    # 뿌리 디렉터리는 남고 내용물만 비었다 — 작업공간 구조는 유지한다.
    assert (root / "cache").is_dir() and (root / "previews").is_dir()
    assert not list((root / "cache").rglob("*")) and not list((root / "previews").rglob("*"))
    assert protected_fingerprint(root) == before


def test_mistagged_protected_names_are_refused_even_inside_cache(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    guarded = {
        "cache/originals/keep.bin": b"1",
        "cache/clips/keep.mp4": b"2",
        "previews/final/keep.mov": b"3",
        "cache/confirmed_000001/keep.mp4": b"4",
        "cache/project.sqlite3": b"5",
    }
    for relative, payload in guarded.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    (root / "cache/normal.txt").write_text("지워도 된다", encoding="utf-8")
    plan = cache_policy.plan_cache_purge(root)
    refused = {item.relative_path: item.reason for item in plan.refused}
    # 보호 이름 디렉터리 자체도, 그 안의 파일도 각각 거부로 남는다.
    assert set(refused) == set(guarded) | {
        "cache/originals",
        "cache/clips",
        "previews/final",
        "cache/confirmed_000001",
    }
    assert set(plan.purge_paths) == {"cache/normal.txt"}
    assert all("지우지 않는다" in reason for reason in refused.values())
    assert "확정 내보내기" in refused["cache/confirmed_000001/keep.mp4"]
    cache_policy.execute_cache_purge(root, plan)
    assert not (root / "cache/normal.txt").exists()
    for relative, payload in guarded.items():
        assert (root / relative).read_bytes() == payload


def test_symlink_children_are_unlinked_without_following(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    outside_file = tmp_path / "outside" / "keep.txt"
    outside_file.parent.mkdir(parents=True)
    outside_file.write_text("바깥", encoding="utf-8")
    outside_dir = tmp_path / "outside_dir"
    outside_dir.mkdir()
    (outside_dir / "inner.bin").write_bytes(b"INNER")
    inside_target = root / "originals" / "keep.mp4"
    inside_target.write_bytes(b"INSIDE")
    (root / "cache/link.txt").symlink_to(outside_file)
    (root / "cache/dirlink").symlink_to(outside_dir)
    (root / "cache/inlink.mp4").symlink_to(inside_target)
    cache_policy.execute_cache_purge(root, cache_policy.plan_cache_purge(root))
    # 링크 자체는 사라지되, 가리킨 대상(안·밖 가리지 않고)은 그대로다.
    assert not (root / "cache/link.txt").exists()
    assert not (root / "cache/dirlink").exists()
    assert not (root / "cache/inlink.mp4").exists()
    assert outside_file.read_text(encoding="utf-8") == "바깥"
    assert (outside_dir / "inner.bin").read_bytes() == b"INNER"
    assert inside_target.read_bytes() == b"INSIDE"


def test_root_and_plan_guards_refuse_mismatches(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    (root / "cache/x.bin").write_bytes(b"x")
    other = tmp_path / "OtherProject"
    make_workspace(other)
    with pytest.raises(cache_policy.CachePolicyError, match="다른 작업공간"):
        cache_policy.execute_cache_purge(other, cache_policy.plan_cache_purge(root))
    forged = cache_policy.CachePurgePlan(
        workspace_root=str(root), purge_paths=("originals/x",), refused=()
    )
    with pytest.raises(cache_policy.CachePolicyError, match="변조"):
        cache_policy.execute_cache_purge(root, forged)
    escape = cache_policy.CachePurgePlan(
        workspace_root=str(root), purge_paths=("../escape",), refused=()
    )
    with pytest.raises(cache_policy.CachePolicyError, match="변조"):
        cache_policy.execute_cache_purge(root, escape)
    link_root = tmp_path / "link_root"
    link_root.symlink_to(root)
    with pytest.raises(cache_policy.CachePolicyError, match="심볼릭 링크"):
        cache_policy.plan_cache_purge(link_root)
    with pytest.raises(cache_policy.CachePolicyError, match="디렉터리가 아니다"):
        cache_policy.plan_cache_purge(tmp_path / "없는작업공간")


def test_empty_workspace_purges_nothing(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    (root).mkdir()
    plan = cache_policy.plan_cache_purge(root)
    assert plan.purge_paths == () and plan.refused == ()
    result = cache_policy.execute_cache_purge(root, plan)
    assert result.removed_files == 0 and result.removed_bytes == 0


# ── 확정 미디어 생존 — 캐시가 지워져도 폴더는 검증된다(16-A6 완성 기준) ────────


def _tiny_confirmed_export(root: Path) -> None:
    """확정 폴더 한 벌을 최소 형태로 게시한다 — 생존 증명의 재료."""
    payload = b"CONFIRMED-MEDIA-" * 8
    clip_path = root / "clips/package_000001/000001_소스/seg.mp4"
    clip_path.parent.mkdir(parents=True, exist_ok=True)
    clip_path.write_bytes(payload)
    asset = SourceAsset(
        asset_id="source_a",
        batch_dir="import_000001",
        relative_path="originals/import_000001/x.mp4",
        status="ready",
        size_bytes=1,
        source_root="/card",
        source_relative_path="x.mp4",
        source_name="소스.mp4",
        content_sha256="a" * 64,
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
        created_at=NOW,
        updated_at=NOW,
    )
    artifact = ClipArtifact(
        clip_id="clip_seg",
        package_dir="clips/package_000001",
        segment_id="seg",
        asset_id="source_a",
        source_order=1,
        segment_order=1,
        profile_name="source_matched",
        relative_path="clips/package_000001/000001_소스/seg.mp4",
        status="published",
        content_sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        file_id=None,
        time_base_num=1,
        time_base_den=25,
        core_start_pts=0,
        core_end_pts=100,
        media_start_pts=0,
        media_end_pts=100,
        core_local_start="0",
        core_local_end="4",
        media_local_duration="4",
        frame_count=100,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    package = PackageRevision(
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
        created_at=NOW,
        updated_at=NOW,
    )
    segment = ReviewSegment(
        segment_id="seg",
        source_id="source_a",
        source_order=1,
        segment_order=1,
        start_pts=0,
        end_pts=100,
        speech_status="speech_detected",
        visual_tags=(),
        recommendation="normal",
        override_recommendation=None,
        selection=SELECTION_INCLUDED,
        notes="",
    )
    review = ReviewRevision(
        "proj",
        "splan_1",
        0,
        (make_source_clock("source_a", "a" * 64, 1, 25, 0, 100, Fraction(1, 25)),),
        (segment,),
        None,
        None,
    )
    request = confirmed.build_confirmed_request(
        export_id="confirmed_000001",
        destination_dir="clips/confirmed_000001",
        project_id="proj",
        package=package,
        review=review,
        artifacts=(artifact,),
        assets={"source_a": asset},
        selected_segment_ids=None,
        profile_name="source_matched",
        handles_before="0",
        handles_after="0",
        tool_provenance="ffmpeg-7-test",
        created_at=NOW,
    )
    confirmed.export_confirmed_package(root, request)


def test_confirmed_export_survives_cache_purge(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    _tiny_confirmed_export(root)
    (root / "cache/audio/source_a.pcm").parent.mkdir(parents=True)
    (root / "cache/audio/source_a.pcm").write_bytes(b"x" * 32)
    (root / "previews/thumb.jpg").write_bytes(b"j")
    cache_policy.execute_cache_purge(root, cache_policy.plan_cache_purge(root))
    report = confirmed.verify_confirmed_export(root, "clips/confirmed_000001")
    assert report.clip_count == 1 and report.mode == "verified"
    media = sorted((root / "clips/confirmed_000001/media").iterdir())
    assert media[0].read_bytes() == b"CONFIRMED-MEDIA-" * 8
