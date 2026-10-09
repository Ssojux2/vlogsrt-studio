"""A6 확정 내보내기 라이브러리의 계약 — 진짜 복사·원자 게시·멱등·경로 검문.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §9·화면 3·§14.4·16-A6. ffmpeg·API
없이 합성 미디어 바이트로 온전한 작업공간을 만들어, 확정 폴더가 (1) 검증된 것만
복사하고 (2) 옛 것을 건드리지 않으며 (3) 실패를 빈 성공으로 위장하지 않는지를
증명한다. 완성 기준(16-A6): 앱을 종료하거나 캐시를 지워도 확정한 조각이 살아 있다.
"""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import os
import shutil
import threading
from dataclasses import replace
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw import cache_policy
from vlogsrt.project.raw import confirmed_package as confirmed
from vlogsrt.project.raw.models import ClipArtifact, PackageRevision, SourceAsset
from vlogsrt.project.raw.package import CSV_FIELDS
from vlogsrt.project.raw.segment_review import (
    SELECTION_INCLUDED,
    ReviewRevision,
    ReviewSegment,
    make_source_clock,
)

NOW = "2026-10-02T00:00:00+00:00"
DESTINATION = "clips/confirmed_000001"
# 합성 시나리오: source_a(순서 1)가 a1(0~100틱=4초)·a2(100~250틱=6초)를, source_b가
# b1(0~75틱=3초)를 가진다. 미디어 바이트는 조각마다 다르다 — 복사가 아니면 못 맞춘다.
PAYLOADS: dict[str, bytes] = {
    "seg_a1": b"A1-" * 40,
    "seg_a2": b"A2-" * 55,
    "seg_b1": b"B1-" * 30,
}
RELATIVE_PATHS: dict[str, str] = {
    "seg_a1": "clips/package_000001/000001_여행 소스/seg_a1.mp4",
    "seg_a2": "clips/package_000001/000001_여행 소스/seg_a2.mp4",
    "seg_b1": "clips/package_000001/000002_도시 소스/seg_b1.mp4",
}
CORES: dict[str, tuple[int, int]] = {
    "seg_a1": (0, 100),
    "seg_a2": (100, 250),
    "seg_b1": (0, 75),
}
SOURCE_ORDERS: dict[str, tuple[str, int, int]] = {
    "seg_a1": ("source_a", 1, 1),
    "seg_a2": ("source_a", 1, 2),
    "seg_b1": ("source_b", 2, 1),
}


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
    for segment_id, payload in PAYLOADS.items():
        path = root / RELATIVE_PATHS[segment_id]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)


def _asset(asset_id: str, name: str, sha: str) -> SourceAsset:
    return SourceAsset(
        asset_id=asset_id,
        batch_dir="import_000001",
        relative_path=f"originals/import_000001/{name}",
        status="ready",
        size_bytes=1,
        source_root="/card",
        source_relative_path=name,
        source_name=name,
        content_sha256=sha,
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


def default_assets() -> dict[str, SourceAsset]:
    # 원본 이름이 수식처럼 생겼다 — 확정 CSV에서도 막혀야 한다(§9.3).
    return {
        "source_a": _asset("source_a", "=여행.mp4", "a" * 64),
        "source_b": _asset("source_b", "도시 소스.mov", "b" * 64),
    }


def _artifact(segment_id: str, **overrides) -> ClipArtifact:
    source_id, source_order, segment_order = SOURCE_ORDERS[segment_id]
    start, end = CORES[segment_id]
    base = ClipArtifact(
        clip_id=f"clip_{segment_id}",
        package_dir="clips/package_000001",
        segment_id=segment_id,
        asset_id=source_id,
        source_order=source_order,
        segment_order=segment_order,
        profile_name="source_matched",
        relative_path=RELATIVE_PATHS[segment_id],
        status="published",
        content_sha256=hashlib.sha256(PAYLOADS[segment_id]).hexdigest(),
        size_bytes=len(PAYLOADS[segment_id]),
        file_id=None,
        time_base_num=1,
        time_base_den=25,
        core_start_pts=start,
        core_end_pts=end,
        media_start_pts=start,
        media_end_pts=end,
        core_local_start="0",
        core_local_end=str(Fraction(end - start, 25)),
        media_local_duration=str(Fraction(end - start, 25)),
        frame_count=end - start,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )
    return replace(base, **overrides) if overrides else base


def default_artifacts() -> tuple[ClipArtifact, ...]:
    return (_artifact("seg_a1"), _artifact("seg_a2"), _artifact("seg_b1"))


def _seg(segment_id: str) -> ReviewSegment:
    source_id, source_order, segment_order = SOURCE_ORDERS[segment_id]
    start, end = CORES[segment_id]
    return ReviewSegment(
        segment_id=segment_id,
        source_id=source_id,
        source_order=source_order,
        segment_order=segment_order,
        start_pts=start,
        end_pts=end,
        speech_status="speech_detected",
        visual_tags=(),
        recommendation="normal",
        override_recommendation=None,
        selection=SELECTION_INCLUDED,
        notes="",
    )


def make_review() -> ReviewRevision:
    clock_a = make_source_clock("source_a", "a" * 64, 1, 25, 0, 250, Fraction(1, 25))
    clock_b = make_source_clock("source_b", "b" * 64, 1, 25, 0, 75, Fraction(1, 25))
    return ReviewRevision(
        "proj",
        "splan_1",
        0,
        (clock_a, clock_b),
        (_seg("seg_a1"), _seg("seg_a2"), _seg("seg_b1")),
        None,
        None,
    )


def make_package() -> PackageRevision:
    return PackageRevision(
        package_dir="clips/package_000001",
        plan_revision_id="splan_1",
        order_revision_id="gorder_1",
        profile_name="source_matched",
        handles_before="0",
        handles_after="0",
        job_fingerprint="f" * 64,
        status="complete",
        clip_total=3,
        clip_published=3,
        error=None,
        created_at=NOW,
        updated_at=NOW,
    )


def build_request(
    *,
    export_id: str = "confirmed_000001",
    destination: str = DESTINATION,
    selected: frozenset[str] | None = None,
    review: ReviewRevision | None = None,
    artifacts: tuple[ClipArtifact, ...] | None = None,
    assets: dict[str, SourceAsset] | None = None,
    profile: str = "source_matched",
    handles_before: str = "0",
    handles_after: str = "0",
) -> confirmed.ConfirmedExportRequest:
    return confirmed.build_confirmed_request(
        export_id=export_id,
        destination_dir=destination,
        project_id="proj",
        package=make_package(),
        review=review if review is not None else make_review(),
        artifacts=artifacts if artifacts is not None else default_artifacts(),
        assets=assets if assets is not None else default_assets(),
        selected_segment_ids=selected,
        profile_name=profile,
        handles_before=handles_before,
        handles_after=handles_after,
        tool_provenance="ffmpeg-7-test",
        created_at=NOW,
    )


def tree_fingerprint(root: Path) -> dict[str, tuple[int, int, int]]:
    """무변경 증명 — inode·mtime·mode까지 묶어 찍는다(내용 해시는 따로 잰다)."""
    fingerprint: dict[str, tuple[int, int, int]] = {}
    for path in sorted(root.rglob("*")):
        facts = path.lstat()
        fingerprint[path.relative_to(root).as_posix()] = (
            facts.st_ino,
            facts.st_mtime_ns,
            facts.st_mode,
        )
    return fingerprint


def staging_leftovers(root: Path) -> list[str]:
    clips = root / "clips"
    return [entry.name for entry in clips.iterdir() if entry.name.startswith(".")]


def media_names(destination: Path) -> list[str]:
    return sorted(os.listdir(destination / "media"))


# ── 전체 확정 — 복사·동결·전달물 ──────────────────────────────────────────────


def test_full_export_copies_verified_media_and_freezes_deliverables(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    old_package = tree_fingerprint(root / "clips/package_000001")
    request = build_request()
    report = confirmed.export_confirmed_package(root, request)
    assert report.mode == "published"
    assert report.clip_count == 3
    assert report.total_duration == "13.000000"
    assert report.total_bytes == sum(len(payload) for payload in PAYLOADS.values())
    destination = root / DESTINATION
    names = media_names(destination)
    # 병렬 번호는 확정 순서(시간순) — 틱은 원본 좌표 밀리초(§9.1 모양).
    assert names[0].startswith("000001__t000000000-t000004000__c_")
    assert names[1].startswith("000002__t000004000-t000010000__c_")
    assert names[2].startswith("000003__t000000000-t000003000__c_")
    assert (destination / "media" / names[0]).read_bytes() == PAYLOADS["seg_a1"]
    assert (destination / "media" / names[2]).read_bytes() == PAYLOADS["seg_b1"]
    # 옛 package는 흔들리지 않는다 — 하드링크가 아니라 갈라진 복사다.
    assert tree_fingerprint(root / "clips/package_000001") == old_package
    # 전달물 — BOM·CRLF·§9.3 머리·수식 방지·틱에서 온 고정소수.
    csv_bytes = (destination / "clip_index.csv").read_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf") and b"\r\n" in csv_bytes
    decoded = csv_bytes.decode("utf-8-sig")
    assert decoded.splitlines()[0] == ",".join(CSV_FIELDS)
    assert ",'=여행.mp4," in decoded and ",=여행.mp4," not in decoded
    assert "4.000000" in decoded and "6.000000" in decoded and "3.000000" in decoded
    jsonl = (destination / "clips.jsonl").read_text(encoding="utf-8")
    assert '"core_range_pts": [0, 100]' in jsonl
    assert '"media_local_duration": "4"' in jsonl
    readme = (destination / "README.md").read_text(encoding="utf-8")
    assert "확정 편집용 조각 폴더" in readme and "Final Cut Pro" in readme
    manifest = json.loads((destination / "confirmed_manifest.json").read_text(encoding="utf-8"))
    assert manifest["export_fingerprint"] == confirmed.export_fingerprint(request)
    assert manifest["clip_count"] == 3
    # 얼어 붙었다 — 쓰기 비트가 없다.
    for path in destination.rglob("*"):
        assert path.stat().st_mode & 0o222 == 0


def test_selected_export_preserves_orders_without_renumbering(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    request = build_request(selected=frozenset({"seg_a1", "seg_b1"}))
    report = confirmed.export_confirmed_package(root, request)
    assert report.clip_count == 2
    records = [
        json.loads(line)
        for line in (root / DESTINATION / "clips.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    # 걸러도 원본 순번은 그대로(재번호 없음), 병렬 번호만 1부터 이어진다.
    assert [(record["source_order"], record["segment_order"]) for record in records] == [
        (1, 1),
        (2, 1),
    ]
    assert [record["export_index"] for record in records] == [1, 2]
    assert media_names(root / DESTINATION)[0].startswith("000001__")
    assert media_names(root / DESTINATION)[1].startswith("000002__")
    csv_text = (root / DESTINATION / "clip_index.csv").read_text(encoding="utf-8-sig")
    assert "clip_seg_a2" not in csv_text and "seg_a2" not in csv_text


# ── 실패·경합 — 빈 성공이 없다 ───────────────────────────────────────────────


def test_failed_export_cleans_only_own_staging_and_keeps_old(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    old = build_request(export_id="confirmed_000009", destination="clips/confirmed_000009")
    confirmed.export_confirmed_package(root, old)
    # 요청을 만든 뒤 원본이 바뀌었다 — 확정의 밑천이 아니게 된다.
    (root / RELATIVE_PATHS["seg_a2"]).write_bytes(PAYLOADS["seg_a2"] + b"TAMPER")
    with pytest.raises(confirmed.SourceMediaError):
        confirmed.export_confirmed_package(root, build_request())
    assert not (root / DESTINATION).exists()
    assert staging_leftovers(root) == []
    confirmed.verify_confirmed_export(root, "clips/confirmed_000009", expected=old)
    assert (root / RELATIVE_PATHS["seg_a1"]).read_bytes() == PAYLOADS["seg_a1"]


def test_source_changed_mid_copy_is_detected(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    real_copy = confirmed._copy_bytes

    def mutating_copy(source: Path, target: Path) -> None:
        real_copy(source, target)
        # 크기를 유지한 자리 바꿈 — 복사 뒤 검문(stat 또는 SHA)이 잡아내야 한다.
        payload = bytearray(source.read_bytes())
        payload[0] ^= 0xFF
        source.write_bytes(bytes(payload))

    monkeypatch.setattr(confirmed, "_copy_bytes", mutating_copy)
    with pytest.raises(confirmed.SourceMediaError, match="복사하는 동안 원본이 바뀌었다|복사 후"):
        confirmed.export_confirmed_package(root, build_request())
    assert not (root / DESTINATION).exists()
    assert staging_leftovers(root) == []


def test_destination_readback_corruption_fails(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    real_copy = confirmed._copy_bytes

    def corrupting_copy(source: Path, target: Path) -> None:
        real_copy(source, target)
        data = bytearray(target.read_bytes())
        data[0] ^= 0xFF
        target.write_bytes(bytes(data))

    monkeypatch.setattr(confirmed, "_copy_bytes", corrupting_copy)
    with pytest.raises(confirmed.SourceMediaError, match="목적지 재독출"):
        confirmed.export_confirmed_package(root, build_request())
    assert not (root / DESTINATION).exists()


def test_exact_retry_is_idempotent_and_mismatch_refuses(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    request = build_request()
    confirmed.export_confirmed_package(root, request)
    snapshot = tree_fingerprint(root / DESTINATION)
    again = confirmed.export_confirmed_package(root, request)
    assert again.mode == "verified_existing"
    assert tree_fingerprint(root / DESTINATION) == snapshot  # 쓰기 없이 검증만
    different = build_request(selected=frozenset({"seg_a1"}))
    with pytest.raises(confirmed.ExportExistsError, match="지문이 다르다"):
        confirmed.export_confirmed_package(root, different)
    assert tree_fingerprint(root / DESTINATION) == snapshot


def test_foreign_existing_destination_is_refused_not_overwritten(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    stray = root / DESTINATION
    stray.mkdir(parents=True)
    (stray / "stray.txt").write_text("남의 것이다", encoding="utf-8")
    with pytest.raises(confirmed.ConfirmedExportError, match="매니페스트가 없다"):
        confirmed.export_confirmed_package(root, build_request())
    assert (stray / "stray.txt").read_text(encoding="utf-8") == "남의 것이다"


def test_concurrent_reservations_do_not_clobber(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    request = build_request()
    real_copy = confirmed._copy_bytes
    barrier = threading.Barrier(2)

    def slow_copy(source: Path, target: Path) -> None:
        real_copy(source, target)
        barrier.wait(timeout=10)

    monkeypatch.setattr(confirmed, "_copy_bytes", slow_copy)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(confirmed.export_confirmed_package, root, request) for _ in range(2)]
        reports = [future.result() for future in futures]
    assert sum(report.mode == "published" for report in reports) == 1
    assert sum(report.mode == "verified_existing" for report in reports) == 1
    assert staging_leftovers(root) == []
    confirmed.verify_confirmed_export(root, DESTINATION, expected=request)
    # 다른 목적지 예약끼리는 서로 치지 않는다.
    second = build_request(export_id="confirmed_000002", destination="clips/confirmed_000002")
    third = build_request(export_id="confirmed_000003", destination="clips/confirmed_000003")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(confirmed.export_confirmed_package, root, item) for item in (second, third)
        ]
        reports = [future.result() for future in futures]
    assert [report.mode for report in reports] == ["published", "published"]


# ── 경로 검문 — 심볼릭 링크·이탈·이름공간 ────────────────────────────────────


def test_symlink_and_path_escapes_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    with pytest.raises(confirmed.PathSafetyError, match="이름공간"):
        build_request(destination="clips/package_000009")
    with pytest.raises(confirmed.PathSafetyError, match="토큰"):
        build_request(export_id="../x")
    # 원본이 심볼릭 링크면 내용이 같아도 거부한다.
    elsewhere = tmp_path / "outside.mp4"
    elsewhere.write_bytes(PAYLOADS["seg_a1"])
    source = root / RELATIVE_PATHS["seg_a1"]
    source.unlink()
    source.symlink_to(elsewhere)
    with pytest.raises(confirmed.PathSafetyError, match="심볼릭 링크"):
        confirmed.export_confirmed_package(root, build_request())
    # 게시 뒤 몰래 고친 참조·끼워 넣은 링크는 재검증을 못 통과한다.
    source.unlink()
    source.write_bytes(PAYLOADS["seg_a1"])
    request = build_request()
    confirmed.export_confirmed_package(root, request)
    destination = root / DESTINATION
    manifest_path = destination / "confirmed_manifest.json"
    original_manifest = manifest_path.read_bytes()
    manifest_path.chmod(0o644)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["clips"][0]["export_relative_path"] = "../escape.mp4"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    manifest_path.chmod(0o444)
    with pytest.raises(confirmed.PathSafetyError, match="계약을 벗어났다"):
        confirmed.verify_confirmed_export(root, DESTINATION)
    manifest_path.chmod(0o644)
    manifest_path.write_bytes(original_manifest)
    manifest_path.chmod(0o444)
    media_dir = destination / "media"
    media_dir.chmod(0o755)
    (media_dir / "link.mp4").symlink_to(elsewhere)
    with pytest.raises(confirmed.PathSafetyError, match="심볼릭 링크가 있다"):
        confirmed.verify_confirmed_export(root, DESTINATION)


# ── 낡은 core·프로파일·미검증 — 다시 뽑은 조각만 확정한다 ─────────────────────


def test_revised_boundaries_require_freshly_verified_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    review = make_review()
    shifted = ReviewRevision(
        review.project_id,
        review.base_plan_id,
        review.sequence,
        review.clocks,
        (replace(_seg("seg_a1"), end_pts=110), _seg("seg_a2"), _seg("seg_b1")),
        None,
        None,
    )
    with pytest.raises(confirmed.StaleCoreError, match="stale_core"):
        build_request(review=shifted)
    with pytest.raises(confirmed.StaleCoreError, match="artifact가 없다"):
        build_request(artifacts=(_artifact("seg_a1"), _artifact("seg_a2")))
    narrow = replace(_artifact("seg_a1"), media_start_pts=50, media_end_pts=100)
    with pytest.raises(confirmed.SourceMediaError, match="덮지 못한다"):
        build_request(artifacts=(narrow, _artifact("seg_a2"), _artifact("seg_b1")))


def test_chosen_profile_and_handles_must_match_package(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    with pytest.raises(confirmed.ProfileMismatchError, match="package와 다르다"):
        build_request(profile="edit_prores")
    with pytest.raises(confirmed.ProfileMismatchError, match="package와 다르다"):
        build_request(handles_before="1/2")
    with pytest.raises(confirmed.ConfirmedExportError, match="유리수 문자열이 아니다"):
        build_request(handles_after="x")
    reprofiled = (
        _artifact("seg_a1", profile_name="edit_prores"),
        _artifact("seg_a2"),
        _artifact("seg_b1"),
    )
    with pytest.raises(confirmed.ProfileMismatchError, match="다른 프로파일"):
        build_request(artifacts=reprofiled)


def test_unverified_empty_and_missing_sources_are_refused(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    with pytest.raises(confirmed.SourceMediaError, match="검증되지 않은"):
        build_request(
            artifacts=(
                _artifact("seg_a1", status="failed"),
                _artifact("seg_a2"),
                _artifact("seg_b1"),
            )
        )
    with pytest.raises(confirmed.SourceMediaError, match="해시가 없다"):
        build_request(
            artifacts=(
                _artifact("seg_a1", content_sha256=None),
                _artifact("seg_a2"),
                _artifact("seg_b1"),
            )
        )
    with pytest.raises(confirmed.SourceMediaError, match="0바이트"):
        build_request(
            artifacts=(_artifact("seg_a1", size_bytes=0), _artifact("seg_a2"), _artifact("seg_b1"))
        )
    with pytest.raises(confirmed.ConfirmedExportError, match="하나도 없다"):
        build_request(selected=frozenset())
    with pytest.raises(confirmed.ConfirmedExportError, match="없는 조각"):
        build_request(selected=frozenset({"seg_ghost"}))
    (root / RELATIVE_PATHS["seg_a2"]).unlink()
    with pytest.raises(confirmed.PathSafetyError, match="열지 못한다"):
        confirmed.export_confirmed_package(root, build_request())


# ── 생존 — 캐시 정리·작업공간 이동·버려진 스테이징 ───────────────────────────


def test_confirmed_media_survives_cache_purge_and_workspace_move(tmp_path: Path) -> None:
    root = tmp_path / "VlogProject"
    make_workspace(root)
    request = build_request()
    confirmed.export_confirmed_package(root, request)
    (root / "cache/audio_frames").mkdir(parents=True)
    (root / "cache/audio_frames/source_a.pcm").write_bytes(b"x" * 16)
    (root / "cache/requests/req.json").parent.mkdir(parents=True)
    (root / "cache/requests/req.json").write_text("{}", encoding="utf-8")
    (root / "previews/thumb.jpg").write_bytes(b"j")
    plan = cache_policy.plan_cache_purge(root)
    result = cache_policy.execute_cache_purge(root, plan)
    assert result.removed_files == 3
    report = confirmed.verify_confirmed_export(root, DESTINATION)
    assert report.clip_count == 3
    assert (root / DESTINATION / "media" / media_names(root / DESTINATION)[0]).read_bytes() == (
        PAYLOADS["seg_a1"]
    )
    # 작업공간을 통째로 옮겨도 폴더 안 상대 경로로 다시 검증된다(§9.2).
    moved = tmp_path / "VlogProject2"
    shutil.move(root, moved)
    report_after_move = confirmed.verify_confirmed_export(moved, DESTINATION)
    assert report_after_move.clip_count == 3
    # 버려진 스테이징 청소 — 아이디가 정확히 같은 것만 지운다.
    clips = moved / "clips"
    mine = clips / ".confirmed_000001.deadbeef.confirmed-staging"
    mine.mkdir()
    (mine / "partial.mp4").write_bytes(b"p")
    foreign = clips / ".confirmed_000002.deadbeef.confirmed-staging"
    foreign.mkdir()
    assert confirmed.clean_abandoned_staging(moved, "confirmed_000001") == 1
    assert not mine.exists() and foreign.exists()


def test_export_filters_excluded_even_with_explicit_selection():
    """확정 내보내기는 제외된 조각을 명시 선택으로도 싣지 못한다(옛 test_capcut_export에서 옮김)."""
    review = make_review()
    review = replace(
        review, segments=(replace(review.segments[0], selection="excluded"), *review.segments[1:])
    )
    assert [c.segment_id for c in build_request(review=review).clips] == ["seg_a2", "seg_b1"]
    with pytest.raises(confirmed.ConfirmedExportError, match="사용할 컷"):
        build_request(review=review, selected=frozenset({"seg_a1"}))
    restored = replace(
        review, segments=(replace(review.segments[0], selection="included"), *review.segments[1:])
    )
    assert len(build_request(review=restored).clips) == 3
