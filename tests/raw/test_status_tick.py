"""1초 상태 틱 하나(W-23 세션 C, D63) — 옛 1초 타이머 일곱을 한 틱으로 합친다.

옛 배선은 1초 타이머 일곱(승인 단추 · 견적 문구 · 상태 줄 · 전문 칸 + 실사용 · 승인 범위 칸 ·
교차 확인 칸 · 용어집)과 2초 둘(작업대 다시 그리기 · 보내기 알림)이었다 — 탭 하나가 초마다 서버를
여덟 번 불렀다. 그 뒤 1초는 `status_tick` 하나였고, W-81(D141)부터는 처리기 모두가 2초 화면
틱 하나에 실린다. 견적 문구·전문 칸은 견적과 함께 지웠다(D110) — 틱은 처리기 여섯을 부른다.
시험이 못 박는 것:

- 타이머 수(1초 일곱 + 2초 둘 → 1초 하나 + 2초 둘 → 2초 하나, W-81·D141)와 화면 틱 하나에 실린
  처리기·몫의 입출력·방식(대기열·`once`) — 상태 틱의 몫은 옛 의존의 입출력 그대로다.
- 바뀐 것 없는 틱은 모든 칸을 건너뛴다. 원천 하나가 바뀌면 그 칸만 간다.
- 「마지막으로 보낸 값」은 세션마다(`gr.State`)다 — 두 탭이 서로의 갱신을 막지 않는다.
- 틱마다 처리기 여섯을 모두 한 번씩 부른다(부수 효과·주기 그대로) — 한 읽기 범위 안에서.
- 다른 사건이 칸에 쓰면 그 칸의 기억을 잊어 다음 틱이 다시 보낸다.

포트는 7993(시험용)만 쓴다. 유료 호출은 없다.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw.review_tick_parts import review_tick_dep, tick_part  # noqa: E402
from tests.raw.test_selection_analysis import wait_until  # noqa: E402
from ui import workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    LIBRARY_APPROVE_LABEL,
    REVIEW_TICK_SECONDS,
    STATUS_SENT_CELLS,
    RawWorkspaceUi,
    TickPart,
)
from vlogsrt.project.raw import read_scope  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402

OLD_ONE_SECOND_TICKS = (
    "library_analysis_button",
    "capcut_status_view",
    "usage_line_markdown",
    "approval_mode_tick",
    "cross_check_tick",
    "glossary_tick",
)
SHOWN_CELLS = ("radio", "box", "glossary", "glossary_status", "loaded")


def _server(tmp_path: Path) -> RawWorkspaceUi:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("상태 틱", str(tmp_path / "ws"))
    return ui


def _skipped(update: Any) -> bool:
    return bool(update == gr.skip())


def _shown(ui: RawWorkspaceUi) -> tuple[Any, ...]:
    """화면을 막 연 탭이 틱에 보내는 칸 값 — 범위 · 교차 확인 · 용어집 글 · 불러온 용어집."""
    loaded = ui.glossary_loaded_value()
    return (
        ui.approval_mode_value(),
        ui.cross_check_value(),
        loaded[1] if loaded else "",
        loaded,
    )


def _tick(ui: RawWorkspaceUi, sent: tuple[Any, ...]) -> tuple[dict[str, Any], tuple[Any, ...]]:
    """틱 한 번 — (칸 이름 → 보낸 값, 그 세션의 다음 기억). 건너뛴 기억은 그대로 둔다."""
    reply = ui.status_tick(ui.current_signature(), *_shown(ui), *sent)
    names = (*SHOWN_CELLS, *STATUS_SENT_CELLS)
    assert len(reply) == len(names) + len(STATUS_SENT_CELLS)
    cells = dict(zip(names, reply[: len(names)], strict=True))
    memory = reply[len(names) :]
    kept = tuple(
        before if _skipped(after) else after for before, after in zip(sent, memory, strict=True)
    )
    return cells, kept


def _fresh() -> tuple[Any, ...]:
    return (None,) * len(STATUS_SENT_CELLS)


def test_every_tick_handler_rides_one_review_tick(tmp_path: Path, local_socket: None) -> None:
    """W-81, D140 — 타이머 하나 · 의존 하나. 처리기와 그 몫의 입출력은 예전 의존 그대로다."""
    ui = _server(tmp_path)
    app = ui.build_blocks()
    timers = {b._id: b for b in app.blocks.values() if isinstance(b, gr.Timer)}
    # 옛 배선: [1, 1, 1, 1, 1, 1, 1, 2, 2](D63 앞) → [1, 2, 2](의존 다섯, D134까지) → [2].
    assert [t.value for t in timers.values()] == [REVIEW_TICK_SECONDS] == [2]
    deps = app.config["dependencies"]
    ticks = [d for d in deps if d["targets"] and d["targets"][0][1] == "tick"]
    (tick,) = (
        ticks  # 2초에 디스패치 하나 — 그라디오가 디스패치마다 모든 칸의 진행 상태를 다시 매긴다
    )
    assert app.fns[tick["id"]].fn == ui.review_tick
    assert app.fns[tick["id"]].queue is True  # 보내기 알림(`gr.Info`)은 대기열을 지나야 뜬다
    assert tick["trigger_mode"] == "once" and tick["show_progress"] == "hidden"
    names = [part.handler.__name__ for part in ui._tick_parts]
    # 초안 결과 카드(W-46a) · 최종 편집 자동 전환(D117)도 같은 틱이다.
    # 알림은 던질 수 있어 맨 앞이다 — 던지면 다른 처리기가 아무것도 쓰지 않는다.
    assert sorted(names[1:]) == [
        "capcut_tick_view",
        "render_export_card_tick",
        "status_tick",
        "switch_tick",
    ]
    assert names[0] == "finalcut_toast_view"
    assert len(set(tick["outputs"])) == len(tick["outputs"])  # 한 칸에 두 처리기가 쓰지 않는다
    status = tick_part(app, ui, ui.status_tick)
    sent = [s._id for s in ui._status_sent]
    assert all(isinstance(app.blocks[i], gr.State) for i in sent)  # 세션마다 따로 사는 기억
    assert status.inputs == [
        ui._signature_state._id,
        ui._approval_mode_radio._id,
        ui._cross_check_box._id,
        ui._glossary_box._id,
        ui._glossary_loaded_state._id,
        *sent,
    ]
    assert status.outputs == [
        ui._approval_mode_radio._id,
        ui._cross_check_box._id,
        ui._glossary_box._id,
        ui._glossary_status_md._id,
        ui._glossary_loaded_state._id,
        ui._library_approve_button._id,
        ui._capcut_progress_md._id,
        ui._usage_md._id,
        *sent,
    ]
    assert tick_part(app, ui, ui.finalcut_toast_view).inputs == [ui._signature_state._id]
    assert tick_part(app, ui, ui.finalcut_toast_view).outputs == []
    wired = {getattr(f.fn, "__name__", "") for f in app.fns.values()}
    assert not wired.intersection(OLD_ONE_SECOND_TICKS)  # 옛 처리기는 틱 안에서만 불린다
    # 처리기마다 따로 단 의존은 「내보내기」를 누른 직후 카드를 한 번 그리는 꼬리(`.then`)뿐이다.
    assert wired.intersection(names) == {"render_export_card_tick"}


def test_the_review_tick_hands_each_handler_its_inputs_in_order(
    tmp_path: Path, local_socket: None
) -> None:
    """화면 틱은 처리기마다 제 입력 몫을 넘기고 답을 출력 차례대로 잇는다.

    칸 없는 처리기는 출력 몫이 없고, 칸 수가 맞지 않는 답은 거부한다."""
    ui = _server(tmp_path)
    calls: list[tuple[str, tuple[Any, ...]]] = []

    def part(name: str, reply: Any) -> Any:
        def handler(*values: Any) -> Any:
            calls.append((name, values))
            return reply

        return handler

    ui._tick_parts = [
        TickPart(part("pair", ("a", "b")), (None, None), (None, None)),
        TickPart(part("one", "c"), (None,), (None,)),
        TickPart(part("none", None), (None,), ()),
    ]
    assert ui.review_tick(1, 2, 3, 4) == ("a", "b", "c")
    assert calls == [("pair", (1, 2)), ("one", (3,)), ("none", (4,))]
    ui._tick_parts[0] = TickPart(part("pair", ("a",)), (None, None), (None, None))
    with pytest.raises(ValueError, match="1칸"):
        ui.review_tick(1, 2, 3, 4)


def _recording(handler: Any, ran: list[str]) -> Any:
    def wrapped(*values: Any) -> Any:
        ran.append(handler.__name__)
        return handler(*values)

    return wrapped


def test_a_failed_send_toast_raises_before_any_other_handler_runs(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """알림이 오류를 던지면 그 틱의 다른 처리기는 돌지 않는다 — 한 번만 내는 답이 버려지지 않는다.

    칸마다의 기억도 그대로라 다음 틱이 모두 다시 한다. 실패 알림은 결과마다 한 번이다."""
    ui = _server(tmp_path)
    app = ui.build_blocks()
    signature = ui.current_signature()
    parts, ran = ui._tick_parts, []
    names = [part.handler.__name__ for part in parts[1:]]
    for index, part in enumerate(parts[1:], start=1):
        parts[index] = TickPart(_recording(part.handler, ran), part.inputs, part.outputs)
    values = [v for part in parts for v in (signature, *(None,) * (len(part.inputs) - 1))]
    assert len(ui.review_tick(*values)) == len(review_tick_dep(app, ui)["outputs"])
    assert ran == names
    ran.clear()
    monkeypatch.setattr(ui, "_finished_send", lambda _signature: {"error": "보내기 실패"})
    with pytest.raises(gr.Error):
        ui.review_tick(*values)
    assert ran == []
    monkeypatch.setattr(ui, "_finished_send", lambda _signature: None)  # 알렸다 — 다음 틱
    ui.review_tick(*values)
    assert ran == names


def test_an_unchanged_tick_skips_every_cell(tmp_path: Path, local_socket: None) -> None:
    ui = _server(tmp_path)
    first, sent = _tick(ui, _fresh())
    # 막 연 탭: 칸 셋은 칸 값과 같아 건너뛰고, 기억이 없는 셋은 모두 보낸다.
    assert all(_skipped(first[name]) for name in SHOWN_CELLS)
    assert not any(_skipped(first[name]) for name in STATUS_SENT_CELLS)
    button = first["button"]
    assert isinstance(button, gr.Button) and button.value == LIBRARY_APPROVE_LABEL
    assert button.interactive is False  # 영상이 없다 — 옛 처리기 그대로
    assert first["progress"] == ui.capcut_status_view(ui.current_signature())
    second, again = _tick(ui, sent)
    assert all(_skipped(value) for value in second.values())
    assert again == sent


def test_a_change_in_one_source_updates_only_that_cell(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = _server(tmp_path)
    root = ui.workspace_root()
    assert root is not None
    _, sent = _tick(ui, _fresh())
    signature = ui.current_signature()
    # 다른 탭이 범위를 바꿨다 — 이 탭의 범위 칸(아직 옛 값)만 맞춘다.
    ui.raw.set_approval_mode(root, raw_service.SELECTION_MODE_TRANSCRIBE)
    shown = _shown(ui)
    reply = ui.status_tick(signature, raw_service.SELECTION_MODE_FULL, *shown[1:], *sent)
    assert (
        isinstance(reply[0], gr.Radio) and reply[0].value == raw_service.SELECTION_MODE_TRANSCRIBE
    )
    assert all(_skipped(value) for value in reply[1:])
    # 상태 줄만 바뀌었다 — 그 칸과 그 기억만 간다.
    monkeypatch.setattr(ui, "capcut_status_view", lambda signature: "새 상태 줄")
    cells, after = _tick(ui, sent)
    changed = [name for name, value in cells.items() if not _skipped(value)]
    assert changed == ["progress"] and cells["progress"] == "새 상태 줄"
    index = STATUS_SENT_CELLS.index("progress")
    assert after[index] == "새 상태 줄"
    assert [a for i, a in enumerate(after) if i != index] == [
        s for i, s in enumerate(sent) if i != index
    ]
    # 단추가 바뀌면(누를 수 있게 됐다) 단추만 간다 — 이름이 같아도 누를 수 있음이 다르면 다르다.
    monkeypatch.setattr(
        ui,
        "library_analysis_button",
        lambda: gr.Button(value=LIBRARY_APPROVE_LABEL, visible=True, interactive=True),
    )
    cells, _ = _tick(ui, after)
    assert [name for name, value in cells.items() if not _skipped(value)] == ["button"]
    assert cells["button"].interactive is True


def test_two_sessions_do_not_suppress_each_other(tmp_path: Path, local_socket: None) -> None:
    ui = _server(tmp_path)
    _, tab_a = _tick(ui, _fresh())
    # 두 번째 탭(새 세션 — 기억이 비었다)은 첫 탭이 막 보낸 값도 모두 받는다.
    cells_b, tab_b = _tick(ui, _fresh())
    assert not any(_skipped(cells_b[name]) for name in STATUS_SENT_CELLS)
    assert tab_b == tab_a
    # 첫 탭의 다음 틱은 여전히 아무것도 보내지 않는다 — 서버에 공유 기억이 없다.
    cells_a, _ = _tick(ui, tab_a)
    assert all(_skipped(value) for value in cells_a.values())


def test_every_tick_calls_all_six_handlers_inside_one_read_scope(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    ui = _server(tmp_path)
    scopes: dict[str, list[object]] = {}
    for name in OLD_ONE_SECOND_TICKS:
        original = getattr(ui, name)

        def spy(*args: Any, _name: str = name, _original: Any = original) -> Any:
            scopes.setdefault(_name, []).append(read_scope.current())
            return _original(*args)

        monkeypatch.setattr(ui, name, spy)
    _, sent = _tick(ui, _fresh())
    cells, _ = _tick(ui, sent)
    assert all(_skipped(value) for value in cells.values())
    # 건너뛴 틱도 옛 처리기를 하나도 빼지 않는다 — 처리기의 정리(지난 승인 작업·알림 걷기)가
    # 예전처럼 초마다 돈다.
    assert sorted(scopes) == sorted(OLD_ONE_SECOND_TICKS)
    assert all(len(seen) == 2 for seen in scopes.values())
    first = {id(seen[0]) for seen in scopes.values()}
    assert len(first) == 1 and None not in (seen[0] for seen in scopes.values())
    assert read_scope.current() is None  # 범위는 틱 하나만 산다


def test_the_tick_still_retires_a_finished_approval_job(tmp_path: Path, local_socket: None) -> None:
    """부수 효과 하나를 실제로 — 끝난 승인 작업은 단추 처리기가 걷는다(건너뛴 틱에서도)."""
    ui = _server(tmp_path)
    root = ui.workspace_root()
    assert root is not None
    _, sent = _tick(ui, _fresh())
    job = ui.raw.submit_job(root, "synthetic-finished", "status-tick", lambda _context: None)
    wait_until(lambda: ui.raw.get_job(root, job.job_id).status == "succeeded", timeout=10)
    ui._approval_jobs[root] = job.job_id
    cells, _ = _tick(ui, sent)
    assert all(_skipped(value) for value in cells.values())
    assert root not in ui._approval_jobs


def test_other_writers_make_the_tick_forget_and_resend(tmp_path: Path, local_socket: None) -> None:
    ui = _server(tmp_path)
    app = ui.build_blocks()
    deps = app.config["dependencies"]
    by_id = {d["id"]: d for d in deps}
    sent = {name: ui._status_sent[i]._id for i, name in enumerate(STATUS_SENT_CELLS)}
    forgets = [d for d in deps if app.fns[d["id"]].fn == ui.forget_sent_view]

    def root_of(dep: dict[str, Any]) -> dict[str, Any]:
        while dep.get("trigger_after") is not None:
            dep = by_id[dep["trigger_after"]]
        return dep

    roots = {}
    for dep in forgets:
        assert dep["inputs"] == [] and app.fns[dep["id"]].queue is False
        target, event = root_of(dep)["targets"][0]
        roots[(target, event)] = dep["outputs"]
    buttons = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}
    glossary_save = next(b for b in buttons.values() if b.elem_id == "vl-glossary-save")
    progress = [sent["progress"]]
    assert roots == {
        (ui._approval_mode_radio._id, "input"): progress,
        (ui._cross_check_box._id, "input"): progress,
        (glossary_save._id, "click"): progress,
        (buttons[LIBRARY_APPROVE_LABEL]._id, "click"): progress,
    }
    # 잊으면 다음 틱이 그 칸을 다시 보낸다(값이 같아도 — 다른 사건이 칸을 덮었을 수 있다).
    _, memory = _tick(ui, _fresh())
    forgotten = dict(zip(STATUS_SENT_CELLS, memory, strict=True))
    forgotten["progress"] = ui.forget_sent_view()
    cells, _ = _tick(ui, tuple(forgotten[name] for name in STATUS_SENT_CELLS))
    assert [name for name, value in cells.items() if not _skipped(value)] == ["progress"]


def test_an_unchanged_tick_does_not_redo_the_button_and_status_work(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """바뀐 것 없는 틱은 승인 단추·상태 줄·실사용 줄을 다시 짓지 않는다(W-46b, D63 꼴).

    처리기 여섯은 그대로 틱마다 불린다(위 시험) — 아끼는 것은 그 안의 읽기다. 작업 상태가 바뀐
    틱(새 작업이 끝났다)은 한 번 다시 짓고, 기억의 최대 나이가 지나도 다시 짓는다.
    """
    ui = _server(tmp_path)
    root = ui.workspace_root()
    assert root is not None
    built: dict[str, int] = {}
    for name in ("_library_button_facts", "_capcut_status_text"):
        original = getattr(ui, name)

        def counted(*args: Any, _name: str = name, _original: Any = original) -> Any:
            built[_name] = built.get(_name, 0) + 1
            return _original(*args)

        monkeypatch.setattr(ui, name, counted)
    usage = {"n": 0}
    original_usage = ui._usage_line

    def counted_usage() -> str:
        usage["n"] += 1
        return original_usage()

    monkeypatch.setattr(ui, "_usage_line", counted_usage)
    _, sent = _tick(ui, _fresh())
    first = dict(built), usage["n"]
    assert set(first[0].values()) == {1}, first
    for _ in range(3):
        cells, sent = _tick(ui, sent)
        assert all(_skipped(value) for value in cells.values())
    assert (dict(built), usage["n"]) == first  # 같은 열쇠 — 다시 짓지 않았다
    job = ui.raw.submit_job(root, "synthetic-finished", "status-memo", lambda _context: None)
    wait_until(lambda: ui.raw.get_job(root, job.job_id).status == "succeeded", timeout=10)
    _tick(ui, sent)
    assert set(built.values()) == {2}, built  # 작업 상태가 바뀐 틱 — 한 번 다시 짓는다
    _tick(ui, sent)
    assert set(built.values()) == {2}, built
    monkeypatch.setattr(workspace_service, "STATUS_MEMO_MAX_AGE_SECONDS", 0.0)
    _tick(ui, sent)
    assert set(built.values()) == {3}, built  # 나이가 지나면 열쇠가 같아도 다시 짓는다


def test_a_running_job_rebuilds_only_the_live_lines(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """도는 작업이 있으면 상태 줄·실사용 줄만 틱마다 다시 짓는다 — 진행·사용량이 움직이는 때다."""
    ui = _server(tmp_path)
    root = ui.workspace_root()
    assert root is not None
    release = threading.Event()
    job = ui.raw.submit_job(root, "synthetic-running", "status-live", lambda _c: release.wait(10))
    try:
        wait_until(lambda: ui.raw.get_job(root, job.job_id).status == "running", timeout=10)
        built: dict[str, int] = {}
        for name in ("_library_button_facts", "_capcut_status_text"):
            original = getattr(ui, name)

            def counted(*args: Any, _name: str = name, _original: Any = original) -> Any:
                built[_name] = built.get(_name, 0) + 1
                return _original(*args)

            monkeypatch.setattr(ui, name, counted)
        _, sent = _tick(ui, _fresh())
        _tick(ui, sent)
        _tick(ui, sent)
        assert built == {"_library_button_facts": 1, "_capcut_status_text": 3}, built
    finally:
        release.set()
        wait_until(lambda: ui.raw.get_job(root, job.job_id).status == "succeeded", timeout=10)
