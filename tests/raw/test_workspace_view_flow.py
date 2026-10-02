"""A1F2 같은 화면의 작업공간 전환 — 이벤트 출력이 서명까지 내린다(루트 브라우저 재현).

루트 브라우저 재현: A를 둔 화면에서 새로고침 없이 B를 만들었더니 Markdown만
바뀌고 서명은 A에 남아, 방금 만든 B의 작업 취소가 "선택이 이미 바뀌었다"로
실패했다. 만들기·열기 이벤트는 **같은 화면**에 작업공간 전체 보기(서명·미디어·
작업·설정)를 바로 내려야 하고, 진짜 오래된 다른 탭의 거절은 그대로 살아 있어야
한다. 이 묶음은 gradio 배선을 실제로 만들고 처리기 결과를 검사한다.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,  # 진짜 부재(ModuleNotFoundError)와 차단 수입(ImportError) 둘 다 건너뛴다
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import workspace_service  # noqa: E402
from vlogsrt.project.raw.jobs import JobContext  # noqa: E402


def _wait_until(predicate: object, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _server(tmp_path: Path) -> workspace_service.RawWorkspaceUi:
    return workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")


def _blocked_job(server: workspace_service.RawWorkspaceUi, root: Path, fingerprint: str) -> str:
    """게이트가 열릴 때까지 멈춰 있는 작업 — 취소 대상."""
    release = threading.Event()

    def blocked(context: JobContext) -> None:
        release.wait(timeout=10)

    handle = server.raw.submit_job(root, "probe", fingerprint, blocked)
    assert _wait_until(lambda: server.raw.get_job(root, handle.job_id).status == "running")
    return handle.job_id


# ── 루트 재현 시나리오 — 새로고침 없이 A에서 B로 ──────────────────────────────


def test_same_view_create_switches_the_whole_view(tmp_path: Path, local_socket: None) -> None:
    server = _server(tmp_path)
    server.build_blocks()  # 실제 배선 — 만들기·열기 출력에 서명 state가 들어가 있다

    first = server.create_workspace_view("A", str(tmp_path / "a"))
    workspace_md, signature_a, media_dd, jobs_dd, jobs_md, settings_md, ingest_md, ingest_dd = first
    assert signature_a is not None
    assert "A" in workspace_md
    assert "자막 프리셋" in settings_md  # 설정 요약도 같은 화면에 내려왔다(빈 안내 아님)
    assert "조사" in ingest_md  # 가져오기 패널도 같은 화면에 내려왔다(새 상태 안내)
    _ = media_dd, jobs_dd, jobs_md, ingest_dd

    second = server.create_workspace_view("B", str(tmp_path / "b"))
    assert second[1] is not None and second[1] != signature_a  # 서명이 B로 갱신
    assert "B" in second[0]

    # 같은 화면의 서명으로 방금 만든 B의 작업 취소 — 바로 통과해야 한다
    job_id = _blocked_job(server, tmp_path / "b", "fp-b")
    dropdown, message = server.cancel_view(second[1], job_id)
    assert "cancel_requested" in message
    assert dropdown is not None
    record = server.raw.get_job(tmp_path / "b", job_id)
    assert record.cancellation_requested  # DB에 실제로 반영되었다


def test_a_truly_stale_other_tab_is_still_refused(tmp_path: Path, local_socket: None) -> None:
    """A를 보던 **다른 탭**의 서명으로 B의 작업을 취소하면 여전히 거절된다."""
    server = _server(tmp_path)
    first = server.create_workspace_view("A", str(tmp_path / "a"))
    stale_signature = first[1]
    server.create_workspace_view("B", str(tmp_path / "b"))
    job_id = _blocked_job(server, tmp_path / "b", "fp-b")
    with pytest.raises(gr.Error, match="바뀌었다"):
        server.cancel_selected(stale_signature, job_id)
    assert not server.raw.get_job(tmp_path / "b", job_id).cancellation_requested


def test_first_creation_without_prior_workspace_then_cancel(
    tmp_path: Path, local_socket: None
) -> None:
    """처음(서명 None)에 만든 작업공간도 같은 화면에서 즉시 취소와 이어진다."""
    server = _server(tmp_path)
    server.build_blocks()
    assert server.current_signature() is None  # 상태 파일도 없다
    view = server.create_workspace_view("C", str(tmp_path / "c"))
    signature = view[1]
    assert signature is not None
    job_id = _blocked_job(server, tmp_path / "c", "fp-c")
    _, message = server.cancel_view(signature, job_id)
    assert "cancel_requested" in message


def test_open_workspace_view_refreshes_back_to_that_project(
    tmp_path: Path, local_socket: None
) -> None:
    """열기 이벤트도 같은 보기를 내린다 — B에서 A를 다시 열면 서명이 A로 돌아온다."""
    server = _server(tmp_path)
    signature_a = server.create_workspace_view("A", str(tmp_path / "a"))[1]
    server.create_workspace_view("B", str(tmp_path / "b"))
    reopened = server.open_workspace_view(str(tmp_path / "a"))
    assert reopened[1] == signature_a
    assert "A" in reopened[0]


# ── 배선 계약 — 출력 목록이 처리기 반환과 어긋나면 화면이 반쯤 갱신된다 ────────


def test_wiring_outputs_match_handler_values(tmp_path: Path, local_socket: None) -> None:
    """만들기·열기의 출력 대상 수 == 처리기 반환 수 — 이 어긋남이 루트 재현의 원형이다."""
    server = _server(tmp_path)
    server.build_blocks()
    outputs = server._selection_outputs()
    assert server._signature_state in outputs  # 서명이 전환 이벤트의 출력이다
    server.create_workspace_view("A", str(tmp_path / "a"))
    assert len(server.workspace_view_values()) == len(outputs)
    # 페이지 로드(새로고침)도 같은 서명 state를 채운다
    assert server._signature_state in server._load_outputs()


def test_bad_create_inputs_fail_without_touching_the_view(
    tmp_path: Path, local_socket: None
) -> None:
    """빈 이름·경로는 안내와 함께 거절 — 화면·서버 상태는 그대로다."""
    server = _server(tmp_path)
    with pytest.raises(gr.Error, match="이름을 입력"):
        server.create_workspace_view("  ", str(tmp_path / "a"))
    with pytest.raises(gr.Error, match="빈 폴더 경로"):
        server.create_workspace_view("A", " ")
    assert server.current_signature() is None


# ── 동시 전환 경합 — 서명 판정과 반환 루트가 같은 작업공간을 가리킨다 ─────────


def test_selection_guard_never_crosses_workspaces_under_switching(
    tmp_path: Path, local_socket: None
) -> None:
    """코디네이터 재현 — 전환이 끼어들어도 맞은 A 서명은 A 루트만 돌려준다.

    옛 구현은 서명을 읽고 루트를 다시 읽어, 그 사이 B로 바뀌면 맞은 A 서명이
    B 루트를 돌려주었다. 지금은 루트를 한 번 찍어 판정·반환 모두에 쓴다.
    """
    server = _server(tmp_path)
    signature_a = server.create_workspace_view("A", str(tmp_path / "a"))[1]
    server.create_workspace_view("B", str(tmp_path / "b"))
    stop = threading.Event()

    def flip() -> None:
        toggle = False
        while not stop.is_set():
            server._remember(tmp_path / ("a" if toggle else "b"))
            toggle = not toggle

    switcher = threading.Thread(target=flip, daemon=True)
    switcher.start()
    accepted = 0
    try:
        for _ in range(400):
            try:
                accepted_root = server._require_current_selection(signature_a)
            except gr.Error:
                continue  # 거절은 정당 — 서버가 B를 가리키는 순간이다
            assert self_signature(server, accepted_root) == signature_a
            accepted += 1
    finally:
        stop.set()
        switcher.join(timeout=5)
    assert accepted > 0  # 거절만 반복하면 이 시험은 아무 것도 보지 않았다


def test_view_values_never_span_two_workspaces(tmp_path: Path, local_socket: None) -> None:
    """한 보기의 Markdown·서명·작업 표는 **한** 작업공간에서 나온다."""
    server = _server(tmp_path)
    signature_a = server.create_workspace_view("A", str(tmp_path / "a"))[1]
    signature_b = server.create_workspace_view("B", str(tmp_path / "b"))[1]
    stop = threading.Event()

    def flip() -> None:
        toggle = False
        while not stop.is_set():
            server._remember(tmp_path / ("a" if toggle else "b"))
            toggle = not toggle

    switcher = threading.Thread(target=flip, daemon=True)
    switcher.start()
    samples = 0
    try:
        for _ in range(300):
            view = server.workspace_view_values()
            markdown, signature, ingest_md = view[0], view[1], view[6]
            # 이름을 실은 Markdown과 서명이 같은 작업공간에서 왔는지 본다.
            if signature == signature_a:
                assert "A" in markdown
            elif signature == signature_b:
                assert "B" in markdown
            else:
                pytest.fail("보기가 어느 작업공간에도 속하지 않는 서명을 실었다")
            # 가져오기 패널도 전환마다 새 상태다 — 옛 제안·확정이 남지 않는다.
            assert "확정 순서" not in ingest_md
            samples += 1
    finally:
        stop.set()
        switcher.join(timeout=5)
    assert samples > 0


def self_signature(server: workspace_service.RawWorkspaceUi, root: Path) -> str | None:
    return server._signature_for(root)
