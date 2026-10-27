"""D62 B5·B6·B2 — 끊긴 만들기의 잔재 정리, 「준비 중」의 정직한 유도, 새 미리보기 조리법의 배선.

- B5: 죽은 pid의 `.part`·`.partial.jpg`만 지운다(게시본은 꼴이 달라 고르지 않는다). 끊긴 'preparing'
  기록은 닫고, 'preparing'은 그것을 쓴 **그 작업**이 활성일 때만이다.
- B6: 준비 작업은 원본을 검증(해시)하기 **전에** 맡음 표시를 쓴다 — 그동안도 「준비 중」이다.
- B2: 새 미리보기만 줄인 조리법으로 만들고 기록에 남긴다. 이미 있는 완성본은 그대로 쓴다.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_visual_review import (
    ONE_SECOND_TILE,
    _active_prepare_job,
    _encode,
    _forbidden,
    _prepared_page,
    _preview_record,
    _review_ready,
    _voiced_media,
    needs_ffmpeg,
)
from vlogsrt import preprocess
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import visual_review
from vlogsrt.project.raw.analysis_config import REVIEW_MEDIA
from vlogsrt.project.raw.workspace import RawWorkspace


@pytest.fixture()
def service() -> raw_service.RawWorkspaceService:
    return raw_service.RawWorkspaceService()


@pytest.fixture()
def workspace_root(service: raw_service.RawWorkspaceService, tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    service.create_workspace(root, "잔재")
    return root


def _dead_pid() -> int:
    """막 끝난 자식의 pid — 지금 살아 있지 않다."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


# ── B5 — 잔재 정리 ─────────────────────────────────────────────────────────────


def test_only_dead_temporaries_are_swept_and_published_files_stay(tmp_path: Path) -> None:
    workspace = RawWorkspace.create(tmp_path / "ws", "w")
    visual = workspace.root / visual_review.VISUAL_PREVIEW_ROOT
    thumbs = visual / visual_review.VISUAL_THUMB_DIRNAME
    thumbs.mkdir(parents=True)
    dead, alive = _dead_pid(), os.getpid()
    junk = {
        visual / f".k1.mp4.{dead}.0a1b2c3d.part": 5000,
        thumbs / f".t1.jpg.{dead}.deadbeef.partial.jpg": 300,
        visual / f".k1.json.{dead}.00ff00ff.partial": 20,
    }
    keep = {
        visual / f".k2.mp4.{alive}.0a1b2c3d.part": 7,  # 이 프로세스가 지금 만드는 중
        visual / "k1.mp4": 11,  # 게시본
        thumbs / "t1.jpg": 13,
        visual / f".k3.mp4.{dead}.nothex!.part": 17,  # 꼴이 아니다 — 건드리지 않는다
    }
    for path, size in {**junk, **keep}.items():
        path.write_bytes(b"x" * size)
    freed = visual_review.sweep_preview_leftovers(workspace, ())
    assert freed == sum(junk.values())
    assert not any(path.exists() for path in junk)
    assert all(path.exists() for path in keep)


def test_interrupted_preparing_records_are_closed_but_a_live_one_is_kept(tmp_path: Path) -> None:
    workspace = RawWorkspace.create(tmp_path / "ws", "w")
    _preview_record(workspace, "dead0", "preparing", "2026-10-07T00:00:00", with_file=False)
    _preview_record(workspace, "live0", "preparing", "2026-10-08T00:00:00", with_file=False)
    _preview_record(workspace, "old00", "preparing", "2026-10-09T00:00:00", with_file=False)
    _patch_record(workspace, "dead0", pid=_dead_pid(), job_id="job_000001")
    _patch_record(workspace, "live0", pid=os.getpid(), job_id="job_000002")
    # 활성 준비 작업이 있으면 pid 없는 옛 기록은 그대로 둔다 — 서버 시작(작업 없음)이 닫는다.
    visual_review.sweep_preview_leftovers(workspace, (_active_prepare_job(),))
    assert [_state(workspace, k) for k in ("dead0", "live0", "old00")] == [
        "interrupted",
        "preparing",
        "preparing",
    ]
    visual_review.sweep_preview_leftovers(workspace, ())
    assert _state(workspace, "old00") == "interrupted"
    assert _state(workspace, "live0") == "preparing"
    preview = visual_review._source_preview_state(workspace, "s1", "a" * 64, (), (0,))
    assert preview.state == "stale"  # 「미리보기 다시 준비」가 보인다 — 「준비 중」이 아니다


def test_a_preparing_record_is_backed_only_by_the_job_that_wrote_it(tmp_path: Path) -> None:
    workspace = RawWorkspace.create(tmp_path / "ws", "w")
    _preview_record(workspace, "prep0", "preparing", "2026-10-02T00:00:00", with_file=False)
    _patch_record(workspace, "prep0", pid=os.getpid(), job_id="job_000007")
    other = (_active_prepare_job("job_000008"),)
    owner = (_active_prepare_job("job_000007"),)
    assert visual_review._source_preview_state(workspace, "s1", "a" * 64, other, (0,)).state == (
        "stale"  # 다른 영상의 준비가 돈다고 이 영상이 「준비 중」이 되지 않는다
    )
    assert visual_review._source_preview_state(workspace, "s1", "a" * 64, owner, (0,)).state == (
        "preparing"
    )


def test_the_service_sweep_reads_the_active_jobs(
    service: raw_service.RawWorkspaceService, workspace_root: Path
) -> None:
    visual = workspace_root / visual_review.VISUAL_PREVIEW_ROOT
    visual.mkdir(parents=True)
    (visual / f".k.mp4.{_dead_pid()}.0a1b2c3d.part").write_bytes(b"x" * 64)
    assert service.sweep_preview_leftovers(workspace_root) == 64


def _patch_record(workspace: RawWorkspace, key: str, **values: object) -> None:
    path = workspace.root / visual_review._preview_record_relative(key)
    body = json.loads(path.read_text(encoding="utf-8"))
    body.update(values)
    path.write_text(json.dumps(body), encoding="utf-8")


def _state(workspace: RawWorkspace, key: str) -> object:
    path = workspace.root / visual_review._preview_record_relative(key)
    return json.loads(path.read_text(encoding="utf-8"))["state"]


# ── B6 — 맡음 표시 ─────────────────────────────────────────────────────────────


def test_a_claimed_source_without_a_record_reads_as_preparing(tmp_path: Path) -> None:
    workspace = RawWorkspace.create(tmp_path / "ws", "w")
    claims = visual_review._write_claims(workspace, "job_000003", {("s1", "a" * 64)})
    active = (_active_prepare_job("job_000003"),)
    claimed = visual_review._claimed_sources(workspace, active)
    assert claimed == frozenset({"a" * 64})
    state = visual_review._source_preview_state(
        workspace, "s1", "a" * 64, active, claimed="a" * 64 in claimed
    )
    assert state.state == "preparing"
    # 끝난(활성이 아닌) 작업의 표시는 아무것도 말하지 않는다
    assert visual_review._claimed_sources(workspace, (_active_prepare_job("job_9"),)) == frozenset()
    visual_review._drop_claims(claims)
    assert not any(visual_review._claim_dir(workspace).iterdir())


def test_a_dead_servers_claim_is_swept(tmp_path: Path) -> None:
    workspace = RawWorkspace.create(tmp_path / "ws", "w")
    (claim,) = visual_review._write_claims(workspace, "job_000004", {("s1", "b" * 64)})
    body = json.loads(claim.read_text(encoding="utf-8"))
    claim.write_text(json.dumps({**body, "pid": _dead_pid()}), encoding="utf-8")
    visual_review.sweep_preview_leftovers(workspace, ())
    assert not claim.exists()


@needs_ffmpeg
def test_preparing_shows_while_the_source_is_being_verified(
    service: raw_service.RawWorkspaceService,
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """원본 검증(실데이터 9GB 해시 1–2분) 동안 화면이 「아직 없음」이 아니라 「준비 중」을 본다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    assert service.visual_review_query(workspace_root, base_plan_id=base).sources[0].state == (
        "missing"
    )
    seen: list[tuple[str, tuple[str, ...]]] = []
    real = visual_review._verify_source_unchanged

    def verify(workspace: Any, asset: Any, path: Path) -> None:
        overview = service.visual_review_query(workspace_root, base_plan_id=base)
        seen.append((overview.sources[0].state, tuple(c.thumb_state for c in overview.segments)))
        real(workspace, asset, path)

    monkeypatch.setattr(visual_review, "_verify_source_unchanged", verify)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    assert seen and seen[0][0] == "preparing"
    assert set(seen[0][1]) == {"preparing"}  # 그 원본의 썸네일도
    assert not any(visual_review._claim_dir(service._workspace(workspace_root)).iterdir())
    after = service.visual_review_query(workspace_root, base_plan_id=base).sources[0]
    assert after.state == "ready"


# ── B2 — 새 미리보기만 새 조리법, 옛 완성본은 그대로 ─────────────────────────────


def _ten_bit_card(card: Path) -> None:
    _encode(
        [
            "-f", "lavfi", "-i", "testsrc2=size=64x32:rate=10:duration=3",
            "-f", "lavfi", "-i", "sine=duration=3",
            "-c:v", "libx264", "-profile:v", "high10", "-pix_fmt", "yuv420p10le",
            "-c:a", "aac", "-shortest", str(card / "십비트.mp4"),
        ]
    )  # fmt: skip


@needs_ffmpeg
def test_a_new_preview_is_built_with_the_review_recipe_and_records_it(
    service: raw_service.RawWorkspaceService,
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    card = tmp_path / "card"
    card.mkdir()
    _ten_bit_card(card)
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    captured: dict[str, Any] = {}
    real = preprocess.extract_visual_review_preview

    def spy(*args: Any, **kwargs: Any) -> preprocess.VisualPreviewExtraction:
        captured.update(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(preprocess, "extract_visual_review_preview", spy)
    # libx264만 있는 기계 — 하드웨어 인코더를 고르는 길은 `test_speed_caches`가 본다(W-45).
    monkeypatch.setattr(preprocess, "available_encoders", lambda: frozenset({"libx264"}))
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    assert captured["priority"] == REVIEW_MEDIA.process_niceness
    assert captured["timeout_seconds"] == REVIEW_MEDIA.preview_timeout(3.0)
    encoding = captured["encoding"]
    assert (encoding.long_edge, encoding.preset, encoding.crf) == (1920, "veryfast", 23)
    preview = service.visual_review_query(workspace_root, base_plan_id=base).sources[0]
    assert preview.state == "ready" and (preview.width, preview.height) == (64, 32)
    stored = json.loads(
        (workspace_root / preview.relative_path).with_suffix(".json").read_text(encoding="utf-8")
    )
    assert stored["encoding"] == "libx264/veryfast/crf23/le1920"
    assert stored["schema"] == visual_review.VISUAL_PREVIEW_RECIPE  # 열쇠 재료 그대로


@needs_ffmpeg
def test_an_existing_ready_preview_is_kept_without_reencoding(
    service: raw_service.RawWorkspaceService,
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """예전 조리법(4K 그대로)으로 만든 완성본 — 같은 열쇠로 그대로 ready, 인코더는 부르지 않는다."""
    card = tmp_path / "card"
    card.mkdir()
    _ten_bit_card(card)
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, first = _prepared_page(service, workspace_root, base, page=1)
    assert first.status == "succeeded", first.error
    preview = service.visual_review_query(workspace_root, base_plan_id=base).sources[0]
    record_path = (workspace_root / preview.relative_path).with_suffix(".json")
    old_style = json.loads(record_path.read_text(encoding="utf-8"))
    old_style.pop("encoding")  # D62 이전 기록 — 조리법 이름이 없다
    record_path.write_text(json.dumps(old_style), encoding="utf-8")
    monkeypatch.setattr(preprocess, "extract_visual_review_preview", _forbidden)
    _, again = _prepared_page(service, workspace_root, base, page=1)
    assert again.status == "succeeded", again.error
    kept = service.visual_review_query(workspace_root, base_plan_id=base).sources[0]
    assert kept.state == "ready" and kept.relative_path == preview.relative_path
