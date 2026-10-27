#!/usr/bin/env python3
"""폴더 작업대의 로컬 단계가 띄우는 외부 프로세스 수와 벽시계 시간을 잰다(W-45, D100 §6).

유료 호출 0회.

    python scripts/measure_speed.py             # 합성 폴더(lavfi 2초 셋 — 4K 하나)를 만들어 잰다
    python scripts/measure_speed.py --keep      # 임시 폴더를 남긴다(경로를 적는다)

잰 것(한 프로세스 안에서 차례로 — 서버처럼 앞 단계의 기억이 뒤 단계에 남는다):
  1. 폴더 불러오기(`start_folder_import`, 미리보기 없이) + 4K 분석 프록시 작업이 끝날 때까지
  2. 원본 전체 미리보기 준비(`start_source_preview`)가 끝날 때까지
  3. 승인 견적(`selection_quote`, 원본 셋) 두 번 — 같은 원본을 다시 재는 길

외부 프로세스는 `subprocess.Popen`을 감싸 도구 이름(argv[0]의 줄기)별로 센다 — `subprocess.run`도
그 길을 지난다. 시험 원본을 만드는 ffmpeg는 세기 전에 돈다. `make check`에는 들지 않는다.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
os.environ.setdefault("VLOGSRT_OFFLINE", "1")

from tests.raw.test_review_workspace import _wait_terminal  # noqa: E402
from vlogsrt.project.raw.service import ANALYSIS_PROXY_JOB_KIND, RawWorkspaceService  # noqa: E402

CLIPS = (
    # (이름, 크기, 레이트, 촬영 시각) — 촬영 시각 순은 이름 순과 다르다(D53)
    ("A_uhd.mp4", "3840x2160", 30, "2026-09-01T10:05:00Z"),
    ("B_hd.mp4", "1920x1080", 30, "2026-09-01T10:00:00Z"),
    ("C_vertical.mp4", "1080x1920", 24, "2026-09-01T10:10:00Z"),
)
CLIP_SECONDS = 2


class _Spawns:
    """`subprocess.Popen`을 감싸 띄운 프로세스를 도구별로 센다 — 스레드에서 띄워도 센다."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self._lock = threading.Lock()
        self._original = subprocess.Popen

    def install(self) -> None:
        spawns = self

        class _Counted(self._original):  # type: ignore[misc, valid-type]
            def __init__(self, args: Any, *rest: Any, **kwargs: Any) -> None:
                with spawns._lock:
                    spawns.counts[_kind(args)] += 1
                super().__init__(args, *rest, **kwargs)

        subprocess.Popen = _Counted  # type: ignore[misc]

    def snapshot(self) -> Counter[str]:
        with self._lock:
            return Counter(self.counts)


_QUERY_FLAGS = ("-version", "-encoders", "-buildconf", "-hwaccels")
"""도구에 무엇이 있는지 묻는 실행 — 미디어를 읽지 않는다. 따로 센다(캐시가 줄이는 몫)."""


def _kind(args: Any) -> str:
    words = [args] if isinstance(args, (str, bytes, os.PathLike)) else [*args]
    tool = Path(os.fsdecode(words[0])).stem.lower()
    for flag in _QUERY_FLAGS:
        if flag in words:
            return f"{tool}{flag}"
    return tool


def _encode(path: Path, size: str, rate: int, creation_time: str) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={CLIP_SECONDS}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={CLIP_SECONDS}",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-metadata", f"creation_time={creation_time}",
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


@contextmanager
def _phase(
    name: str, spawns: _Spawns, rows: list[tuple[str, float, Counter[str]]]
) -> Iterator[None]:
    before = spawns.snapshot()
    started = time.perf_counter()
    yield
    seconds = time.perf_counter() - started
    rows.append((name, seconds, spawns.snapshot() - before))


def _wait(service: RawWorkspaceService, root: Path, job_id: str) -> None:
    record = _wait_terminal(service, root, job_id)
    if record.status != "succeeded":
        raise RuntimeError(f"작업 {job_id}이(가) 끝나지 않았다: {record.status} {record.error}")


def _import(service: RawWorkspaceService, folder: Path) -> Path:
    status, handle = service.start_folder_import(folder, prepare_preview=False)
    root = Path(status.workspace_root)
    _wait(service, root, handle.job_id)
    for job in service.recent_jobs(root, [ANALYSIS_PROXY_JOB_KIND], None):
        _wait(service, root, job.job_id)
    return root


def _preview(service: RawWorkspaceService, root: Path) -> None:
    _wait(service, root, service.start_source_preview(root).job_id)


def _quote(service: RawWorkspaceService, root: Path) -> Callable[[], None]:
    ids = tuple(asset.asset_id for asset in service.list_source_assets(root))

    def run() -> None:
        service.selection_quote(root, ids)

    return run


def _report(rows: list[tuple[str, float, Counter[str]]]) -> None:
    print(f"{'단계':<24} {'초':>7} {'프로세스':>8}  도구별")
    for name, seconds, counts in rows:
        tools = " ".join(f"{tool}={count}" for tool, count in sorted(counts.items()))
        print(f"{name:<24} {seconds:7.2f} {sum(counts.values()):8d}  {tools}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="임시 폴더를 남긴다")
    args = parser.parse_args(argv)
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        print("ffmpeg/ffprobe가 없다", file=sys.stderr)
        return 2
    scratch = Path(tempfile.mkdtemp(prefix="vlogsrt-speed-"))
    folder = scratch / "trip"
    folder.mkdir()
    for name, size, rate, creation in CLIPS:
        _encode(folder / name, size, rate, creation)
    spawns = _Spawns()
    spawns.install()
    rows: list[tuple[str, float, Counter[str]]] = []
    service = RawWorkspaceService()
    try:
        with _phase("1 폴더 불러오기+프록시", spawns, rows):
            root = _import(service, folder)
        with _phase("2 미리보기 준비", spawns, rows):
            _preview(service, root)
        quote = _quote(service, root)
        for attempt in (1, 2):
            with _phase(f"3 승인 견적 #{attempt}", spawns, rows):
                quote()
    finally:
        if args.keep:
            print(f"임시 폴더: {scratch}")
        else:
            shutil.rmtree(scratch, ignore_errors=True)
    _report(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
