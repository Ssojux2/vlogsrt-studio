"""쪽 준비는 원본마다 자리 수만큼 함께 — 화면이 기다리는 일이 자리를 먼저 얻는다(W-77, D128).

실측(원본 30개 불러오기, D127): 쪽 준비가 한 스레드에서 원본마다 차례로(썸네일 → 미리보기) 돌아
실제 169개 기록의 마지막 약 10분은 로컬 미디어 자리 셋 가운데 둘이 빈 채였다. 썸네일은 자리 밖에서
돌았다 — 프록시 셋 위에 하나 더 얹혔다. 이제

- 쪽의 원본마다 썸네일을 자리 수만큼 함께 만들고, 그 쪽의 썸네일이 다 끝난 뒤에 원본 전체 미리보기를
  같은 수만큼 함께 만든다(썸네일이 먼저 — W-24 세션 I 그대로).
- 썸네일도 자리를 잡는다 — 동시에 도는 ffmpeg는 여전히 자리 수다.
- 썸네일·미리보기는 **급한** 자리다 — 기다리는 급한 일이 있으면 분석 프록시는 빈 자리를 집지 않는다.
  사람은 불러온 영상을 먼저 보고, 프록시는 분석을 승인하기 전까지만 끝나면 된다.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from tests.raw import test_plan_probe_warm as warm
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import preprocess
from vlogsrt.project.raw import media_refs, visual_review
from vlogsrt.project.raw.models import RawWorkspaceError

NAMES, needs_ffmpeg = warm.NAMES, warm.needs_ffmpeg
library = warm.library  # 원본 넷(촬영 순 = 이름 순)을 들인 라이브러리 픽스처


# ── 자리 — 급한 일이 먼저 ──────────────────────────────────────────────────────


def _waiter(slots, urgent: bool, order: list[str], name: str) -> threading.Thread:
    def run() -> None:
        while not slots.acquire(urgent=urgent, timeout=0.05):
            pass
        order.append(name)
        slots.release()

    thread = threading.Thread(target=run)
    return thread


def test_an_urgent_waiter_takes_the_freed_slot_before_an_earlier_normal_one() -> None:
    slots = media_refs.LocalMediaSlots(1)
    assert slots.acquire(urgent=False, timeout=1)
    order: list[str] = []
    normal = _waiter(slots, False, order, "프록시")
    with slots.waiting(urgent=False):
        normal.start()
        time.sleep(0.2)  # 프록시가 먼저 줄을 섰다
        urgent = _waiter(slots, True, order, "미리보기")
        with slots.waiting(urgent=True):
            urgent.start()
            time.sleep(0.2)
            slots.release()
            urgent.join(timeout=5)
    normal.join(timeout=5)
    assert order == ["미리보기", "프록시"]


def test_a_normal_waiter_gets_a_free_slot_when_nothing_urgent_waits() -> None:
    slots = media_refs.LocalMediaSlots(1)
    with slots.waiting(urgent=False):
        assert slots.acquire(urgent=False, timeout=1)
    slots.release()


def test_the_slot_context_registers_its_wait_and_hears_nothing_new(monkeypatch) -> None:
    slots = media_refs.LocalMediaSlots(2)
    monkeypatch.setattr(media_refs, "_LOCAL_MEDIA_SLOTS", slots)
    with media_refs.local_media_slot(urgent=True), media_refs.local_media_slot():
        assert slots.free == 0
    assert slots.free == 2 and slots.urgent_waiting == 0


# ── 쪽 준비 — 원본마다 함께, 썸네일이 먼저, 썸네일도 자리를 잡는다 ─────────────────


def _counted(monkeypatch, slots: int) -> dict:
    seen: dict = {"inflight": 0, "peak": 0, "thumb_peak": 0, "preview_peak": 0, "events": []}
    lock = threading.Lock()
    monkeypatch.setattr(os, "cpu_count", lambda: 8)  # 쪽 일꾼은 자리 수(3)다
    monkeypatch.setattr(media_refs, "_LOCAL_MEDIA_SLOTS", media_refs.LocalMediaSlots(slots))

    def wrap(kind: str, real):
        def run(*args, **kwargs):
            with lock:
                seen["inflight"] += 1
                seen[f"{kind}_inflight"] = seen.get(f"{kind}_inflight", 0) + 1
                seen["peak"] = max(seen["peak"], seen["inflight"])
                seen[f"{kind}_peak"] = max(seen[f"{kind}_peak"], seen[f"{kind}_inflight"])
                seen["events"].append(f"{kind}+")
            try:
                time.sleep(0.2)
                return real(*args, **kwargs)
            finally:
                with lock:
                    seen["inflight"] -= 1
                    seen[f"{kind}_inflight"] -= 1
                    seen["events"].append(f"{kind}-")

        return run

    monkeypatch.setattr(
        preprocess, "extract_frame_index", wrap("thumb", preprocess.extract_frame_index)
    )
    monkeypatch.setattr(
        preprocess,
        "extract_visual_review_preview",
        wrap("preview", preprocess.extract_visual_review_preview),
    )
    return seen


def _prepare(service, root: Path) -> None:
    finished = _wait_terminal(service, root, service.start_source_preview(root).job_id)
    assert finished.status == "succeeded", finished.error


@needs_ffmpeg
def test_a_page_prepares_its_sources_together_thumbnails_first(library, monkeypatch) -> None:
    service, root = library
    seen = _counted(monkeypatch, slots=3)
    _prepare(service, root)
    assert seen["thumb_peak"] >= 2 and seen["preview_peak"] >= 2
    events = seen["events"]
    last_thumb_end = max(i for i, e in enumerate(events) if e == "thumb-")
    first_preview = events.index("preview+")
    assert last_thumb_end < first_preview  # 쪽의 썸네일이 다 끝난 뒤에 미리보기(W-24 세션 I)
    assert events.count("preview+") == len(NAMES)


@needs_ffmpeg
def test_thumbnails_hold_a_slot_so_ffmpeg_never_exceeds_the_slots(library, monkeypatch) -> None:
    service, root = library
    seen = _counted(monkeypatch, slots=1)
    _prepare(service, root)
    assert seen["peak"] == 1
    assert seen["events"].count("thumb+") >= len(NAMES)


def _failed(service, root: Path) -> str:
    finished = _wait_terminal(service, root, service.start_source_preview(root).job_id)
    assert finished.status == "failed"
    return finished.error or ""


@needs_ffmpeg
def test_the_earliest_source_error_is_the_answer(library, monkeypatch) -> None:
    """둘이 오류로 멈추면 차례로 돌던 예전처럼 앞선 원본의 오류가 답이다."""
    service, root = library
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    names = {a.asset_id: a.source_name for a in service.list_source_assets(root)}
    real = visual_review._chosen_source

    def choose(workspace, revision, source_id, choices, context):
        if names[source_id] in ("IMG_0002.MOV", "IMG_0004.MOV"):
            time.sleep(0.3 if names[source_id] == "IMG_0002.MOV" else 0)  # 뒤 원본이 먼저 멈춘다
            raise RawWorkspaceError(f"선택 실패: {names[source_id]}")
        return real(workspace, revision, source_id, choices, context)

    monkeypatch.setattr(visual_review, "_chosen_source", choose)
    error = _failed(service, root)
    assert "IMG_0002.MOV" in error and "IMG_0004.MOV" not in error


@needs_ffmpeg
def test_failures_are_listed_in_source_order(library, monkeypatch) -> None:
    service, root = library
    monkeypatch.setattr(os, "cpu_count", lambda: 8)
    real = preprocess.extract_frame_index

    def frame(path, *args, **kwargs):
        name = Path(path).name
        if name in ("IMG_0001.MOV", "IMG_0003.MOV"):
            time.sleep(0.3 if name == "IMG_0001.MOV" else 0)  # 뒤 원본이 먼저 실패한다
            raise RuntimeError(f"썸네일 실패: {name}")
        return real(path, *args, **kwargs)

    monkeypatch.setattr(preprocess, "extract_frame_index", frame)
    error = _failed(service, root)
    assert error.index("IMG_0001.MOV") < error.index("IMG_0003.MOV")
