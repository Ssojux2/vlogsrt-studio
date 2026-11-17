"""계획의 원본 조사는 함께 미리 잰다(W-76, D135) — 순서·결과·실패 자리는 그대로다.

분할 계획(`_planned_sources`)과 분석 → 계획(`plan_segmentation_from_analysis`)은 원본마다 차례로
ffprobe 둘(원본 확인 · 추출 시계)을 기다렸다 — 실측 원본 169개의 불러오기에서 첫 미리보기 전 5.7분,
기억이 빈 서버의 첫 게시 75초가 거의 다 이 차례 ffprobe였다. 이제 루프 앞에서 조사 기억을
로컬 미디어 자리 수만큼 함께 채운다(`_warm_source_probes`). 루프는 손대지 않는다 —
검증(해시) 차례·만드는 계획·던지는 실패는 예전 그대로이고, 기억에 맞을 뿐이다.
미리 재다 실패한 원본은 기억에 남지 않으므로 루프가 제 자리에서 다시 재 알린다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import preprocess
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.service import RawWorkspaceService

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
NAMES = ("IMG_0001.MOV", "IMG_0002.MOV", "IMG_0003.MOV", "IMG_0004.MOV")


def _encode(path: Path, seconds: int, shot: str) -> None:
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
            "-t", str(seconds), "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "pcm_s16le", "-metadata", f"creation_time={shot}", str(path),
        ],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr


@pytest.fixture
def library(tmp_path: Path) -> tuple[RawWorkspaceService, Path]:
    folder = tmp_path / "trip"
    folder.mkdir()
    for index, name in enumerate(NAMES):
        _encode(folder / name, 2 + index, f"2026-08-11T0{index}:00:00+00:00")
    service = RawWorkspaceService()
    status, handle = service.start_folder_import(folder, prepare_preview=False)
    root = Path(status.workspace_root)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    for job in service.all_active_jobs(root):
        _wait_terminal(service, root, job.job_id)
    return service, root


def _cold(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """기억을 비우고 조사자를 느린 계수기로 감싼다 — 동시에 몇이 돌았는지·몇 번 쟀는지 센다."""
    preprocess._PROBE_MEMO.clear()
    monkeypatch.setattr(os, "cpu_count", lambda: 8)  # 자리가 둘 이상인 기계 — 코어가 적은 CI도 같다
    seen = {"inflight": 0, "peak": 0, "raw": 0, "clock": 0}
    lock = threading.Lock()

    def counted(kind: str, real):
        def probe(*args, **kwargs):
            with lock:
                seen[kind] += 1
                seen["inflight"] += 1
                seen["peak"] = max(seen["peak"], seen["inflight"])
            try:
                time.sleep(0.15)
                return real(*args, **kwargs)
            finally:
                with lock:
                    seen["inflight"] -= 1

        return probe

    monkeypatch.setattr(preprocess, "probe_raw_media", counted("raw", preprocess.probe_raw_media))
    monkeypatch.setattr(
        preprocess, "probe_extraction_clock", counted("clock", preprocess.probe_extraction_clock)
    )
    return seen


@needs_ffmpeg
def test_planning_probes_sources_together_and_each_only_once(library, monkeypatch) -> None:
    service, root = library
    seen = _cold(monkeypatch)
    snapshot = service.plan_segmentation(root)
    assert len(snapshot.coverage) == len(NAMES)
    assert seen["peak"] >= 2  # 차례로만 재던 예전에는 1이었다
    assert seen["raw"] == len(NAMES) and seen["clock"] == len(NAMES)  # 루프는 기억에 맞는다


@needs_ffmpeg
def test_the_plan_is_the_same_as_measuring_one_by_one(library, monkeypatch) -> None:
    service, root = library
    _cold(monkeypatch)
    together = service.plan_segmentation(root)
    _cold(monkeypatch)
    monkeypatch.setattr(raw_service, "_warm_source_probes", lambda workspace, asset_ids: None)
    alone = service.plan_segmentation(root)
    assert together.fingerprint == alone.fingerprint
    assert together.entries == alone.entries
    assert together.coverage == alone.coverage


@needs_ffmpeg
def test_a_failing_source_is_reported_where_the_loop_meets_it(library, monkeypatch) -> None:
    """두 원본이 조사에 실패하면 예전처럼 차례가 앞선 원본의 실패가 올라온다."""
    service, root = library
    _cold(monkeypatch)
    real = preprocess.probe_extraction_clock
    broken = {"IMG_0002.MOV", "IMG_0004.MOV"}

    def failing(source, *args, **kwargs):
        if Path(source).name in broken:
            raise RuntimeError(f"조사 실패: {Path(source).name}")
        return real(source, *args, **kwargs)

    monkeypatch.setattr(preprocess, "probe_extraction_clock", failing)
    with pytest.raises(RuntimeError, match="IMG_0002.MOV"):
        service.plan_segmentation(root)


@needs_ffmpeg
def test_analysis_planning_warms_the_whole_library_before_its_loop(tmp_path, monkeypatch) -> None:
    """분석 → 계획도 같다 — 분석되지 않은 원본의 전체 한 조각을 재는 루프보다 먼저 함께 잰다."""
    from tests.raw.test_creation_order import reverse_imported_project

    service, root = reverse_imported_project(tmp_path, human=False)
    events: list[str] = []
    real_warm, real_intervals = (
        raw_service._warm_source_probes,
        RawWorkspaceService._analysis_intervals,
    )

    def warm(workspace, asset_ids):
        events.append(f"warm:{len(tuple(asset_ids))}")
        return real_warm(workspace, asset_ids)

    def intervals(self, workspace, row, asset_id):
        events.append("loop")
        return real_intervals(self, workspace, row, asset_id)

    monkeypatch.setattr(raw_service, "_warm_source_probes", warm)
    monkeypatch.setattr(RawWorkspaceService, "_analysis_intervals", intervals)
    service.plan_segmentation_from_analysis(root, use_current_order=True)
    sources = len(service.list_source_assets(root))
    assert events[0] == f"warm:{sources}"
    assert events.index("loop") > 0
