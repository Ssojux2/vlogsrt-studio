"""작업대 그리기 비용을 재는 합성 작업공간(W-23, D63) — 분석이 끝난 원본 18개.

`tests/raw/test_tier_review_gate.py::tier_project`의 분석 원본(15초 · 창 하나 · 3→11초 자막
공백 · Luna 「사용 가능」)을 18벌 만든다. 원본마다 촬영 시각(`creation_time`)만 1분씩 달라 바이트와
촬영 순서가 갈린다 — 재생 픽스처는 내용 해시마다 같은 발화를 준다. 가져오기 → 순서 → 재생 분석 →
자동 정리까지 실제 길로 가고, 유료 호출은 0회다. 시험(`test_render_cost.py`)과 측정 스크립트
(`scripts/bench_workbench.py`)가 같은 작업공간을 쓴다.
"""

from __future__ import annotations

import functools
import json
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from tests.raw.test_review_workspace import _ingest_card, _wait_terminal, _write_replay_fixtures
from tests.raw.test_tier_review_gate import FIRST_FRAME, UTTERANCES
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.project.raw import source_transcript, sync_report, workspace
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.service import RawWorkspaceService

SOURCE_COUNT = 18
"""실데이터 작업공간(18개 원본)과 같은 수 — 원본 수에 비례하는 읽기가 드러나는 크기다."""


def encode_card(card: Path, count: int = SOURCE_COUNT) -> tuple[Path, ...]:
    """15초 합성 원본 `count`개 — 촬영 시각만 1분씩 다르다(바이트·순서가 갈린다)."""
    card.mkdir(parents=True, exist_ok=True)
    paths = []
    for index in range(count):
        path = card / f"clip_{index + 1:02d}.mov"
        result = subprocess.run(
            [
                "ffmpeg", "-v", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=25:duration=15",
                "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono",
                "-t", "15", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-threads", "1",
                "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
                "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
                "-c:a", "pcm_s16le",
                "-metadata", f"creation_time=2026-09-01T10:{index:02d}:00Z",
                str(path),
            ],
            capture_output=True,
        )  # fmt: skip
        if result.returncode != 0:
            raise RuntimeError(result.stderr.decode("utf-8", "replace"))
        paths.append(path)
    return tuple(paths)


def build_workspace(base: Path, count: int = SOURCE_COUNT) -> Path:
    """분석·자동 정리까지 끝난 작업공간 루트 — 검토 머리가 등급판으로 선다."""
    encode_card(base / "card", count)
    service = RawWorkspaceService()
    root = base / "project"
    service.create_workspace(root, "그리기 비용")
    _ingest_card(service, root, base / "card")
    service.confirm_global_source_order(root)
    shas = [asset.content_sha256 for asset in service.list_source_assets(root)]
    _write_replay_fixtures(root, {sha: [dict(row) for row in UTTERANCES] for sha in shas})
    luna_path = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna_path.read_text(encoding="utf-8"))
    fixtures["windows"]["window_000000"].update(
        suitability="usable",
        suitability_reason="계곡과 인물이 선명하다",
        evidence_ids=[FIRST_FRAME],
        uncertainties=["원거리 인물 식별 불확실"],
    )
    luna_path.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    _finish(service, root, service.start_analysis(root, mode="replay").job_id)
    _finish(service, root, service.start_cleanup(root).job_id)
    return root


def _finish(service: RawWorkspaceService, root: Path, job_id: str) -> None:
    record = _wait_terminal(service, root, job_id)
    if record.status != "succeeded":
        raise RuntimeError(f"{record.kind} {record.status}: {record.error}")


def settle(service: RawWorkspaceService, root: Path) -> None:
    """정리 고리 둘(작업공간 정리·자막 시각 확인)이 더 걸 것이 없고 도는 작업도 없을 때까지.

    그리기 값은 시각 확인 보고서가 있느냐에 따라 달라진다 — 다 잰 상태에서만 바이트를 잠근다.
    """
    deadline = time.monotonic() + SETTLE_SECONDS
    while time.monotonic() < deadline:
        started = service.start_upkeep(root) or service.start_sync_upkeep(root)
        active = service.all_active_jobs(root)
        for job in active:
            _wait_terminal(service, root, job.job_id)
        if not started and not active:
            return
    raise RuntimeError("작업공간이 가라앉지 않았다 — 정리 고리가 계속 작업을 건다")


SETTLE_SECONDS = 300.0

_HEX_ID = re.compile(r"[0-9a-f]{16,64}")


def normalize(text: str, base: Path) -> str:
    """임의 아이디·임시 경로를 처음 나온 차례의 자리표로 — 같은 작업공간 모양이면 같은 글이다.

    작업공간·원본·조각·미디어 아이디와 그리기마다 새로 뽑는 `epoch`·화면 수명 토큰은 무작위다.
    차례는 그리기 순서(촬영 시각 순)가 정하므로 두 번 만든 작업공간도 같은 자리표를 받는다.
    """
    text = text.replace(str(base.resolve()), "<TMP>").replace(str(base), "<TMP>")
    seen: dict[str, str] = {}

    def placeholder(match: re.Match[str]) -> str:
        return seen.setdefault(match.group(0), f"<ID{len(seen):03d}>")

    return _HEX_ID.sub(placeholder, text)


# ── 부른 수 세기 — 시험은 monkeypatch.setattr, 측정 스크립트는 되돌리는 패처를 넘긴다 ──────

Patch = Callable[[object, str, object], None]

COUNTED_FUNCTIONS: tuple[tuple[str, Callable[..., Any]], ...] = (
    ("open_db", workspace.open_db),
    ("source_transcript.load", source_transcript.load),
    ("sync_report.gather_source", sync_report.gather_source),
)
"""모듈 함수 — 이름으로 가져간 모듈(`from … import open_db`)까지 같은 함수를 갈아 끼운다."""

COUNTED_METHODS: tuple[tuple[str, type, str], ...] = (
    ("ReviewWorkspaceFacade()", ReviewWorkspaceFacade, "__init__"),
    ("load_head", ReviewWorkspaceFacade, "load_head"),
    ("analysis_overview", RawWorkspaceService, "analysis_overview"),
    ("visual_workbench_render", RawWorkspaceUi, "visual_workbench_render"),
    ("start_upkeep", RawWorkspaceService, "start_upkeep"),
    ("start_sync_upkeep", RawWorkspaceService, "start_sync_upkeep"),
)

COUNTED_NAMES: tuple[str, ...] = tuple(name for name, _ in COUNTED_FUNCTIONS) + tuple(
    name for name, _, _ in COUNTED_METHODS
)


def install_counters(patch: Patch) -> Counter[str]:
    """세는 함수로 갈아 끼우고 그 수를 돌려준다 — 작업 스레드의 부름도 센다(잠금)."""
    counts: Counter[str] = Counter()
    lock = threading.Lock()

    def counted(name: str, original: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(original)
        def call(*args: Any, **kwargs: Any) -> Any:
            with lock:
                counts[name] += 1
            return original(*args, **kwargs)

        return call

    for name, original in COUNTED_FUNCTIONS:
        replacement = counted(name, original)
        for module in list(sys.modules.values()):
            module_name = getattr(module, "__name__", "") or ""
            if not module_name.startswith(("vlogsrt", "ui.")):
                continue
            for attribute, value in list(vars(module).items()):
                if value is original:
                    patch(module, attribute, replacement)
    for name, owner, attribute in COUNTED_METHODS:
        patch(owner, attribute, counted(name, getattr(owner, attribute)))
    return counts


class SteppedJob:
    """진행 수만 바꾸는 작업 하나 — 부르는 쪽이 한 걸음씩 민다(잠자기 없음)."""

    KIND = "render_cost_progress"

    def __init__(self, service: RawWorkspaceService, root: Path) -> None:
        self._service, self._root = service, root
        self._steps: queue.Queue[int | None] = queue.Queue()
        self._acks: queue.Queue[int] = queue.Queue()

        def body(context: Any) -> None:
            context.set_total(1000)
            while (value := self._steps.get(timeout=STEP_SECONDS)) is not None:
                context.set_progress(value)
                self._acks.put(value)

        self.job_id = service.submit_job(root, self.KIND, uuid.uuid4().hex, body).job_id

    def step(self, value: int) -> None:
        """진행을 `value`로 — 작업이 DB에 적을 때까지 기다린다."""
        self._steps.put(value)
        assert self._acks.get(timeout=STEP_SECONDS) == value

    def finish(self) -> None:
        self._steps.put(None)
        record = _wait_terminal(self._service, self._root, self.job_id)
        assert record.status == "succeeded", record.error


STEP_SECONDS = 30.0
