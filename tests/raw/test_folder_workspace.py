"""폴더 작업공간 · 원본 복사 없는 불러오기 · 불러올 때 4K 프록시(W-41, D100 §2).

합성 lavfi 원본(4K 하나 · 1080p 하나 · 2초)과 임시 폴더만 쓴다 — 유료 호출 0회. 촬영 시각은
이름 순과 거꾸로 둔다(D53). 결제 열쇠(`media_refs.fingerprint`)가 `original-only/v1` 그대로인지,
25% 분석 프록시가 4K에만 생기고 다시 쓰이는지, 4K 미리보기가 긴 변 960인지 본다.
"""

from __future__ import annotations

import errno
import hashlib
import shutil
import stat
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import log, preprocess
from vlogsrt.project.raw import ingest, media_refs
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_PROXY,
    REVIEW_MEDIA,
    ReviewMediaSettings,
    validate_review_media_settings,
)
from vlogsrt.project.raw.config import (
    FOLDER_WORKSPACE,
    FolderWorkspaceSettings,
    LocalMediaConcurrency,
)
from vlogsrt.project.raw.jobs import JobCancelledError
from vlogsrt.project.raw.models import IngestError, SourceNestingError
from vlogsrt.project.raw.service import ANALYSIS_PROXY_JOB_KIND, RawWorkspaceService
from vlogsrt.project.raw.workspace import WORKSPACE_LAYOUT
from vlogsrt.task_runtime import cancellation_scope

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

UHD_NAME = "A_uhd.mp4"
HD_NAME = "B_hd.mp4"


def _encode(path: Path, size: str, creation_time: str, *, seconds: int = 2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate=30:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-threads", "2",
            "-c:a", "aac", "-metadata", f"creation_time={creation_time}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


def _identity(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


@dataclass(frozen=True)
class Imported:
    service: RawWorkspaceService
    folder: Path
    root: Path
    before: dict[str, tuple[str, int]]
    logs: tuple[str, ...]


def _finish_import(service: RawWorkspaceService, root: Path, job_id: str) -> None:
    record = _wait_terminal(service, root, job_id)
    assert record.status == "succeeded", record.error
    for job in service.recent_jobs(root, [ANALYSIS_PROXY_JOB_KIND], None):
        done = _wait_terminal(service, root, job.job_id)
        assert done.status == "succeeded", done.error


@pytest.fixture(scope="module")
def imported(tmp_path_factory: pytest.TempPathFactory) -> Imported:
    """한 번 불러온 폴더 — 4K는 늦게, 1080p는 먼저 찍었다(이름 순과 거꾸로)."""
    folder = tmp_path_factory.mktemp("trip") / "제주 여행"
    _encode(folder / UHD_NAME, "3840x2160", "2026-09-01T10:05:00Z")
    _encode(folder / HD_NAME, "1920x1080", "2026-09-01T10:00:00Z")
    _encode(folder / ".hidden" / "copy.mp4", "64x36", "2026-09-01T09:00:00Z", seconds=1)
    before = {name: _identity(folder / name) for name in (UHD_NAME, HD_NAME)}
    service = RawWorkspaceService()
    logs: list[str] = []
    real_info = log.info
    # 모듈 범위 픽스처라 monkeypatch를 못 쓴다 — 불러오기가 끝날 때까지만 갈아 끼운다.
    log.info = logs.append  # type: ignore[assignment]
    try:
        status, handle = service.start_folder_import(folder)
        root = Path(status.workspace_root)
        _finish_import(service, root, handle.job_id)
    finally:
        log.info = real_info  # type: ignore[assignment]
    return Imported(service, folder, root, before, tuple(logs))


def _assets(imported: Imported) -> dict[str, object]:
    return {
        asset.source_name: asset for asset in imported.service.list_source_assets(imported.root)
    }


# ── 자리 · 스캔 ───────────────────────────────────────────────────────────


def test_the_workspace_lives_inside_the_folder(imported: Imported) -> None:
    assert imported.root == imported.folder.resolve() / FOLDER_WORKSPACE.workspace_dirname
    for directory in WORKSPACE_LAYOUT:
        assert (imported.root / directory).is_dir()
    status, created = imported.service.managed_folder_workspace(imported.folder)
    assert not created and Path(status.workspace_root) == imported.root  # 다시 열기는 만들지 않는다


def test_the_rescan_skips_the_workspace_and_dot_folders(imported: Imported) -> None:
    """작업공간 안의 프록시·미리보기 MP4도, 점 폴더의 영상도 새 원본이 아니다."""
    assert list(imported.root.rglob("*.mp4"))  # 작업공간 안에 영상이 실제로 있다
    proposal = imported.service.scan_sources(imported.root, imported.folder)
    assert sorted(item.source_relative_path for item in proposal.order) == [UHD_NAME, HD_NAME]
    assert ingest._pruned_name("_vlogsrt") and ingest._pruned_name("_VLOGSRT")
    assert not ingest._pruned_name("_vlogsrt2") and not ingest._pruned_name("vlogsrt")


def test_the_order_is_capture_time_not_name(imported: Imported) -> None:
    order = imported.service.global_order_proposal(imported.root)
    assert [asset.source_name for asset in order] == [HD_NAME, UHD_NAME]  # D53


def test_the_import_copies_nothing_and_leaves_the_originals_untouched(imported: Imported) -> None:
    workspace = imported.service._workspace(imported.root)
    assert [p for p in (imported.root / "originals").rglob("*") if p.is_file()] == []
    for name, asset in _assets(imported).items():
        assert asset.status == "ready"
        assert workspace.source_media_path(asset) == imported.folder.resolve() / name
        assert _identity(imported.folder / name) == imported.before[name]
        assert asset.content_sha256 == imported.before[name][0]


def test_the_import_tells_each_step_in_the_log(imported: Imported) -> None:
    """스캔은 요청 안에서 동기로 돈다 — 파일마다 한 줄, 이어지는 조용하던 단계도 한 줄씩(D101)."""
    logs = list(imported.logs)
    scan = [line for line in logs if line.startswith("원본 확인 ")]
    assert scan == [
        f"원본 확인 1/2 · {UHD_NAME}",
        f"원본 확인 2/2 · {HD_NAME}",
    ]  # 점 폴더는 안 센다
    steps = [
        f"작업공간 만듦: {imported.root.parent.name}/{imported.root.name}",
        "촬영 시각 순 정렬: 2개",
        f"분석 프록시 만들기 시작: {UHD_NAME} (25%)",
        f"분석 프록시 검증: {UHD_NAME}",
    ]
    places = [logs.index(step) for step in steps]
    assert places == sorted(places) and logs.index(scan[-1]) < places[0]
    ready = [i for i, line in enumerate(logs) if line.startswith(f"분석 프록시 준비: {UHD_NAME}")]
    assert ready and ready[0] > places[-1]


def test_a_proxy_job_counts_one_step(imported: Imported) -> None:
    """프록시 작업은 0/0으로 머물지 않는다 — 막대가 0에서 1/1로 간다(D101)."""
    jobs = imported.service.recent_jobs(imported.root, [ANALYSIS_PROXY_JOB_KIND], None)
    assert jobs and all((j.progress_current, j.progress_total) == (1, 1) for j in jobs)


# ── 4K 분석 프록시 ─────────────────────────────────────────────────────────


def _proxy_dir(imported: Imported, asset) -> Path:
    workspace = imported.service._workspace(imported.root)
    return media_refs._proxy_directory(workspace, asset.content_sha256, ANALYSIS_PROXY)


def test_the_proxy_is_built_for_the_4k_source_only(imported: Imported) -> None:
    assets = _assets(imported)
    uhd_proxy = _proxy_dir(imported, assets[UHD_NAME]) / "proxy.mp4"
    assert uhd_proxy.is_file()
    assert not _proxy_dir(imported, assets[HD_NAME]).exists()
    assert len(list((imported.root / "cache" / "analysis-proxy").glob("*/proxy.mp4"))) == 1
    probe = preprocess.probe_raw_media(uhd_proxy)
    assert (probe.video.width, probe.video.height) == (960, 540) and probe.audio == ()
    original = preprocess.probe_extraction_clock(imported.folder / UHD_NAME)
    assert preprocess.probe_extraction_clock(uhd_proxy).frame_count == original.frame_count


def test_the_proxy_is_reused_and_non_4k_frames_come_from_the_original(
    imported: Imported, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("다시 만들면 안 된다")

    monkeypatch.setattr(preprocess, "build_analysis_proxy", refuse)
    workspace = imported.service._workspace(imported.root)
    assets = _assets(imported)
    proxy = _proxy_dir(imported, assets[UHD_NAME]) / "proxy.mp4"
    stamp = proxy.stat().st_mtime_ns
    assert media_refs.resolve(workspace, assets[UHD_NAME], "frames") == proxy
    assert proxy.stat().st_mtime_ns == stamp
    hd_original = imported.folder.resolve() / HD_NAME
    assert media_refs.resolve(workspace, assets[HD_NAME], "frames") == hd_original


def test_the_paid_fingerprint_stays_original_only(imported: Imported) -> None:
    """결제 열쇠는 프록시 전후 그대로다 — 미디어 연결 기록이 생기지 않는다(`connect` 없음)."""
    workspace = imported.service._workspace(imported.root)
    tagged = f"{media_refs.ORIGINAL_ONLY_FINGERPRINT}+{ANALYSIS_PROXY.tag()}"
    for name, asset in _assets(imported).items():
        assert media_refs.load(workspace, asset.asset_id) is None
        assert media_refs.fingerprint(workspace, asset.asset_id) == "original-only/v1"
        frames = media_refs.frames_fingerprint(workspace, asset.asset_id)
        assert frames == (tagged if name == UHD_NAME else "original-only/v1")


def test_a_second_proxy_job_reuses_the_ready_proxy(imported: Imported) -> None:
    proxy = _proxy_dir(imported, _assets(imported)[UHD_NAME]) / "proxy.mp4"
    stamp = proxy.stat().st_mtime_ns
    handles = imported.service.start_analysis_proxies(imported.root)
    assert len(handles) == 1  # 4K 하나
    record = _wait_terminal(imported.service, imported.root, handles[0].job_id)
    assert record.status == "succeeded" and record.kind == ANALYSIS_PROXY_JOB_KIND
    assert proxy.stat().st_mtime_ns == stamp


# ── 4K 미리보기 ───────────────────────────────────────────────────────────


def test_the_4k_preview_is_960_with_sound(imported: Imported) -> None:
    overview = imported.service.visual_review_query(imported.root)
    names = {
        asset.asset_id: asset.source_name
        for asset in imported.service.list_source_assets(imported.root)
    }
    sizes = {}
    for source in overview.sources:
        assert source.registered_file_id, source
        path = imported.service.resolve_media(imported.root, source.registered_file_id).path
        probe = preprocess.probe_raw_media(path)
        sizes[names[source.source_id]] = (probe.video.width, probe.video.height, len(probe.audio))
    assert sizes[UHD_NAME] == (REVIEW_MEDIA.preview_long_edge_4k, 540, 1)
    assert sizes[HD_NAME] == (1920, 1080, 1)


def test_the_4k_preview_edge_is_validated() -> None:
    assert REVIEW_MEDIA.preview_long_edge_4k == 960
    with pytest.raises(ValueError, match="검토 보조 미디어 설정"):
        validate_review_media_settings(ReviewMediaSettings(preview_long_edge_4k=961))


# ── 순수 판정 · 설정 ───────────────────────────────────────────────────────


def test_the_proxy_rule_is_the_4k_long_edge() -> None:
    rule = media_refs.needs_analysis_proxy
    assert rule(3840, 2160, FOLDER_WORKSPACE) and rule(2160, 3840, FOLDER_WORKSPACE)
    assert rule(4096, 2160, FOLDER_WORKSPACE)
    assert not rule(1920, 1080, FOLDER_WORKSPACE) and not rule(3839, 2160, FOLDER_WORKSPACE)
    assert rule(None, None, FOLDER_WORKSPACE)  # 크기를 모르는 옛 기록은 예전처럼(D47)


def test_local_media_limit_comes_from_the_cpu_count() -> None:
    limit = LocalMediaConcurrency().limit
    assert [limit(n) for n in (None, 0, 1, 2, 4, 6, 8, 64)] == [1, 1, 1, 1, 2, 3, 3, 3]
    with pytest.raises(ValueError):
        LocalMediaConcurrency(ceiling=0)


@pytest.mark.parametrize("name", [".vlogsrt", "", "a/b", "a\\b", "C:x"])
def test_folder_workspace_names_refuse_hidden_and_path_spellings(name: str) -> None:
    with pytest.raises(ValueError, match="폴더 작업공간 설정"):
        FolderWorkspaceSettings(workspace_dirname=name)


def test_a_waiting_local_media_slot_hears_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_refs, "_LOCAL_MEDIA_SLOTS", threading.BoundedSemaphore(1))
    outcome: list[str] = []
    asked: list[int] = []

    def cancelled() -> None:
        asked.append(1)
        if len(asked) > 1:  # 첫 물음은 범위에 들어갈 때다 — 기다리는 동안의 물음에 취소한다
            raise JobCancelledError("취소")

    def waiter() -> None:
        try:
            with cancellation_scope(cancelled), media_refs.local_media_slot():
                outcome.append("들어감")
        except JobCancelledError:
            outcome.append("취소")

    with media_refs.local_media_slot():
        thread = threading.Thread(target=waiter)
        thread.start()
        thread.join(timeout=5)
    assert outcome == ["취소"]


# ── 원본 루트 검사 ─────────────────────────────────────────────────────────


def test_only_the_one_folder_workspace_nesting_is_allowed(tmp_path: Path) -> None:
    folder = tmp_path / "card"
    (folder / "sub").mkdir(parents=True)
    workspace = folder / FOLDER_WORKSPACE.workspace_dirname
    workspace.mkdir()
    assert ingest._safe_source_root(folder, workspace) == folder.resolve()
    for other in (folder / "other", folder / "sub" / "_vlogsrt", folder / "_vlogsrt2"):
        with pytest.raises(SourceNestingError):
            ingest._safe_source_root(folder, other)
    with pytest.raises(SourceNestingError):
        ingest._safe_source_root(tmp_path, workspace)  # 두 겹 위의 폴더
    with pytest.raises(SourceNestingError):
        ingest._safe_source_root(workspace, workspace)  # 작업공간 그 자체
    (workspace / "originals").mkdir()
    with pytest.raises(SourceNestingError):
        ingest._safe_source_root(workspace / "originals", workspace)


# ── 쓸 수 없는 폴더 ────────────────────────────────────────────────────────


def test_a_read_only_folder_falls_back_to_the_sibling(tmp_path: Path) -> None:
    folder = tmp_path / "SDCARD"
    _encode(folder / HD_NAME, "320x180", "2026-09-01T10:00:00Z", seconds=1)
    mode = folder.stat().st_mode
    folder.chmod(stat.S_IRUSR | stat.S_IXUSR)
    try:
        service = RawWorkspaceService()
        status, created = service.managed_folder_workspace(folder)
        root = Path(status.workspace_root)
        assert created and root == tmp_path.resolve() / "SDCARD-vlogsrt"
        assert not (folder / FOLDER_WORKSPACE.workspace_dirname).exists()
        assert [item.source_name for item in service.scan_sources(root, folder).order] == [HD_NAME]
        again, created_again = RawWorkspaceService().managed_folder_workspace(folder)
        assert not created_again and Path(again.workspace_root) == root
    finally:
        folder.chmod(mode)


def test_other_create_failures_are_not_mistaken_for_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "card"
    folder.mkdir()
    service = RawWorkspaceService()

    def full(root: Path, name: str):
        raise OSError(errno.ENOSPC, "디스크가 가득 찼다")

    monkeypatch.setattr(service, "create_workspace", full)
    with pytest.raises(OSError, match="가득"):
        service.managed_folder_workspace(folder)
    assert not (tmp_path / "card-vlogsrt").exists()


def test_a_folder_without_videos_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "note.txt").write_text("메모", encoding="utf-8")
    with pytest.raises(IngestError, match="불러올 영상이 없다"):
        RawWorkspaceService().start_folder_import(folder)


# ── 4K 기준 이전에 1단계를 마친 비4K 원본 ─────────────────────────────────────


def test_a_stage_one_recorded_proxy_source_is_read_again_for_non_4k(tmp_path: Path) -> None:
    """4K 기준이 생기기 전 1단계가 프록시에서 장면을 쟀다면 2단계도 같은 프록시를 읽는다.

    출처가 원본으로 바뀌면 2단계 실측 지문이 1단계와 어긋나 유료 1단계를 다시 돌려야 한다(D47).
    """
    folder = tmp_path / "old"
    _encode(folder / HD_NAME, "320x180", "2026-09-01T10:00:00Z", seconds=1)
    service = RawWorkspaceService()
    status, handle = service.start_folder_import(folder, prepare_preview=False)
    root = Path(status.workspace_root)
    _finish_import(service, root, handle.job_id)
    workspace = service._workspace(root)
    (asset,) = service.list_source_assets(root)
    original = folder.resolve() / HD_NAME
    recorded = f"{media_refs.ORIGINAL_ONLY_FINGERPRINT}+{ANALYSIS_PROXY.tag()}"
    path, source = media_refs.frames_decode_source(workspace, asset, recorded)
    assert path != original and path.name == "proxy.mp4" and source == recorded
    assert media_refs.frames_decode_source(workspace, asset, "original-only/v1") == (
        original,
        "original-only/v1",
    )
    assert media_refs.frames_decode_source(workspace, asset, None) == (original, "original-only/v1")
    assert media_refs.fingerprint(workspace, asset.asset_id) == "original-only/v1"
