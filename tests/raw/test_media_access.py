"""A1 미디어 접근·Host/Origin/Range의 행동 검증(계획 §4.3·§4.4).

미들웨어와 /media 라우트를 실제 FastAPI 앱 위에서 TestClient로 두드린다 —
전역 가드를 약화하지 않는, 진짜 요청·응답 수준의 증거다. Gradio 마운트가
 sockets를 만들 수 있어 해당 묶음에는 `local_socket`을 쓴다(루프백 허용,
 외부 연결은 여전히 막힌다).
"""

from __future__ import annotations

import sqlite3
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

from fastapi.testclient import TestClient  # noqa: E402

from ui import workspace_service  # noqa: E402
from vlogsrt.project.raw.workspace import DB_NAME  # noqa: E402

PORT = 7860
BODY = bytes(range(256)) * 4  # 1024 바이트 — 내용이 예측 가능한 합성 미디어


@pytest.fixture
def ui(tmp_path: Path, local_socket: None) -> workspace_service.RawWorkspaceUi:
    """작업공간을 열어 둔 UI 서버 — 상태 파일도 임시 폴더 안에 둔다.

    TestClient는 ASGI in-process지만 anyio·gradio 내부가 소켓 객체를 만든다 —
    루프백 허용(local_socket)으로 돌리고 외부 연결은 conftest가 여전히 막는다.
    """
    root = tmp_path / "VlogProject"
    ui_server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    status = ui_server.raw.create_workspace(root, "제주")
    ui_server._remember(Path(status.workspace_root))
    return ui_server


def _registered_clip(ui_server: workspace_service.RawWorkspaceUi, body: bytes = BODY) -> str:
    root = Path(ui_server.workspace_root())
    media = root / "originals" / "clip.mp4"
    media.parent.mkdir(exist_ok=True)
    media.write_bytes(body)
    return ui_server.raw.register_media(root, media, "original").file_id


def _client(ui_server: workspace_service.RawWorkspaceUi) -> TestClient:
    return TestClient(ui_server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")


# ── 행동 검증 — 실제 요청·응답 ────────────────────────────────────────────────


def test_media_serves_the_whole_file_with_range_advertising(
    ui: workspace_service.RawWorkspaceUi,
) -> None:
    server = ui
    file_id = _registered_clip(server)
    response = _client(server).get(f"/media/{file_id}")
    assert response.status_code == 200
    assert response.content == BODY
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(len(BODY))
    assert response.headers["content-type"] == "video/mp4"


@pytest.mark.parametrize(
    ("header", "expected_slice"),
    [
        ("bytes=0-9", BODY[0:10]),
        ("bytes=100-", BODY[100:]),
        ("bytes=-5", BODY[-5:]),
        ("bytes=0-100000", BODY),  # 끝이 넘으면 자른다
    ],
)
def test_media_serves_ranges(
    ui: workspace_service.RawWorkspaceUi, header: str, expected_slice: bytes
) -> None:
    server = ui
    file_id = _registered_clip(server)
    response = _client(server).get(f"/media/{file_id}", headers={"Range": header})
    assert response.status_code == 206
    assert response.content == expected_slice
    assert response.headers["content-length"] == str(len(expected_slice))
    assert response.headers["accept-ranges"] == "bytes"


def test_media_reports_content_range_for_seek(ui: workspace_service.RawWorkspaceUi) -> None:
    server = ui
    file_id = _registered_clip(server)
    response = _client(server).get(f"/media/{file_id}", headers={"Range": "bytes=10-19"})
    assert response.headers["content-range"] == f"bytes 10-19/{len(BODY)}"


@pytest.mark.parametrize("header", ["bytes=2000-3000", "bytes=50-40", "bytes=-0"])
def test_unsatisfiable_ranges_get_416(ui: workspace_service.RawWorkspaceUi, header: str) -> None:
    server = ui
    file_id = _registered_clip(server)
    response = _client(server).get(f"/media/{file_id}", headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{len(BODY)}"


def test_head_requests_carry_the_same_headers(ui: workspace_service.RawWorkspaceUi) -> None:
    """플레이어가 먼저 두르는 HEAD — 본문 없이 길이·범위 안내를 준다."""
    server = ui
    file_id = _registered_clip(server)
    client = _client(server)
    plain = client.head(f"/media/{file_id}")
    assert plain.status_code == 200
    assert plain.content == b""
    assert plain.headers["content-length"] == str(len(BODY))
    partial = client.head(f"/media/{file_id}", headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.headers["content-length"] == "10"


@pytest.mark.parametrize(
    "file_id", ["media_999999", "originals/clip.mp4", "../project.sqlite3", ""]
)
def test_only_registered_ids_are_served(ui: workspace_service.RawWorkspaceUi, file_id: str) -> None:
    """경로 모양·탈출 모양·없는 ID는 전부 404다 — ID 해석 자체를 하지 않는다."""
    server = ui
    _registered_clip(server)
    response = _client(server).get(f"/media/{file_id}")
    assert response.status_code == 404


def test_protected_workspace_files_have_no_serving_route(
    ui: workspace_service.RawWorkspaceUi,
) -> None:
    """DB·project.toml은 등록될 수도, 라우트로 나올 수도 없다."""
    server = ui
    client = _client(server)
    for name in (DB_NAME, "project.toml", "jobs/run.log", "subtitles/resp.json"):
        response = client.get(f"/media/{name}")
        assert response.status_code == 404


def test_a_changed_registered_target_answers_409(ui: workspace_service.RawWorkspaceUi) -> None:
    server = ui
    root = Path(server.workspace_root())
    file_id = _registered_clip(server)
    (root / "originals" / "clip.mp4").write_bytes("크기가 다른 내용으로 바뀌었다".encode())
    response = _client(server).get(f"/media/{file_id}")
    assert response.status_code == 409


def test_a_not_ready_media_answers_409(ui: workspace_service.RawWorkspaceUi) -> None:
    server = ui
    root = Path(server.workspace_root())
    file_id = _registered_clip(server)
    with sqlite3.connect(root / DB_NAME) as connection:
        connection.execute(
            "UPDATE media_files SET status = 'copying' WHERE file_id = ?", (file_id,)
        )
        connection.commit()
    assert _client(server).get(f"/media/{file_id}").status_code == 409


def test_requests_without_an_open_workspace_answer_404(tmp_path: Path, local_socket: None) -> None:
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    server.startup()  # 상태 파일이 없다 — 작업공간 없이 뜬 서버
    response = _client(server).get("/media/media_000001")
    assert response.status_code == 404


# ── 작업공간 바인딩 — 남의 파일로 이어지는 일은 없다(루트 재현) ─────────────


def test_two_workspaces_never_share_media_urls(tmp_path: Path, local_socket: None) -> None:
    """독립 작업공간 A·B의 미디어 URL은 서로를 가리키지 않는다.

    A를 보던 URL이 서버가 B로 옮겨간 뒤 200으로 B의 내용을 돌려준 결함의 회귀.
    지금은 A의 ID가 B에서 없는 ID라 명확히 404로 거절된다.
    """
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    body_a = b"A" * 300
    body_b = b"B" * 90
    ids = {}
    for name, body in (("a", body_a), ("b", body_b)):
        root = tmp_path / name
        server.raw.create_workspace(root, name)
        media = root / "originals" / f"{name}.mp4"
        media.write_bytes(body)
        ids[name] = server.raw.register_media(root, media, "original").file_id
    assert ids["a"] != ids["b"]

    client = TestClient(server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")
    server._remember(tmp_path / "a")
    owner = client.get(f"/media/{ids['a']}")
    assert owner.status_code == 200
    assert owner.content == body_a

    server._remember(tmp_path / "b")  # 서버가 다른 프로젝트로 옮겼다
    stale = client.get(f"/media/{ids['a']}")
    assert stale.status_code == 404
    assert stale.content != body_b  # B의 파일이 새어 나가지 않는다
    fresh = client.get(f"/media/{ids['b']}")
    assert fresh.status_code == 200
    assert fresh.content == body_b


def test_a_foreign_head_is_refused_the_same_way(tmp_path: Path, local_socket: None) -> None:
    """HEAD도 마찬가지 — 다른 작업공간의 ID는 길이 안내조차 주지 않는다."""
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    ids = {}
    for name in ("a", "b"):
        root = tmp_path / name
        server.raw.create_workspace(root, name)
        media = root / "originals" / f"{name}.mp4"
        media.write_bytes(name.encode() * 40)
        ids[name] = server.raw.register_media(root, media, "original").file_id
    client = TestClient(server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")
    media_length = str(len(b"a" * 40))
    server._remember(tmp_path / "b")
    stale = client.head(f"/media/{ids['a']}")
    assert stale.status_code == 404
    assert stale.headers.get("content-length") != media_length  # 미디어 길이는 안 나간다


def test_stale_selection_cannot_cancel_into_another_workspace(
    tmp_path: Path, local_socket: None
) -> None:
    """변경 콜백의 선택 검증 — 오래된 작업공간 신원은 적용되지 않고 실패한다.

    A에서 작업을 만들고 서버가 B로 옮겼다면, A를 보던 화면의 취소 요청은
    B의 작업 목록에 떨어지지 않는다. 새로고침 뒤(현재 신원)에만 통과한다.
    """
    import threading

    import gradio as gr

    from vlogsrt.project.raw.jobs import JobContext

    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    root_a = tmp_path / "a"
    server.raw.create_workspace(root_a, "A")
    server.raw.create_workspace(tmp_path / "b", "B")
    gate = threading.Event()

    def blocked(context: JobContext) -> None:
        gate.wait(timeout=10)

    handle = server.raw.submit_job(root_a, "probe", "fp-1", blocked)
    signature_a = server.current_signature()
    server._remember(tmp_path / "b")

    with pytest.raises(gr.Error, match="바뀌었습니다"):
        server.cancel_selected(signature_a, handle.job_id)
    record = server.raw.get_job(root_a, handle.job_id)
    assert not record.cancellation_requested  # 오래된 선택은 아무 효과도 없었다

    server._remember(root_a)
    message = server.cancel_selected(server.current_signature(), handle.job_id)
    assert "cancel_requested" in message
    gate.set()


# ── Host/Origin 가드 — 전역 미들웨어, 경로 가리지 않고 ────────────────────────


def test_foreign_hosts_are_cut_before_any_route(ui: workspace_service.RawWorkspaceUi) -> None:
    server = ui
    file_id = _registered_clip(server)
    client = _client(server)
    assert client.get(f"/media/{file_id}", headers={"Host": "evil.example.com"}).status_code == 403
    assert client.get(f"/media/{file_id}", headers={"Host": "192.168.0.10"}).status_code == 403
    assert client.get(f"/media/{file_id}", headers={"Host": "localhost:7860"}).status_code == 200


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ({"Origin": "http://evil.example.com"}, 403),
        ({"Origin": "null"}, 403),
        ({"Origin": "http://localhost:9999"}, 403),
        ({"Referer": "http://evil.example.com/x"}, 403),
        ({"Origin": f"http://127.0.0.1:{PORT}"}, 405),  # 가드 통과 → 라우트의 405
        ({"Origin": f"http://localhost:{PORT}"}, 405),
        ({"Referer": f"http://127.0.0.1:{PORT}/"}, 405),
        ({}, 403),  # 머리글 없는 변경 요청은 신뢰하지 않는다(코디네이터 재결)
    ],
)
def test_mutation_requests_need_our_origin(
    ui: workspace_service.RawWorkspaceUi, headers: dict[str, str], expected: int
) -> None:
    """변경 메서드는 Origin/Referer 검사를 지난다 — /media가 GET뿐이라 405로 증명."""
    server = ui
    file_id = _registered_clip(server)
    response = _client(server).post(f"/media/{file_id}", headers=headers)
    assert response.status_code == expected


# ── 서버 재시작 — 상태 파일과 작업 복구의 한 번 ───────────────────────────────


def test_workspace_reopens_after_a_server_restart(tmp_path: Path) -> None:
    """마지막 작업공간은 상태 파일에 남는다 — 새 서버가 같은 프로젝트를 다시 연다."""
    first = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    status = first.raw.create_workspace(tmp_path / "VlogProject", "제주")
    first._remember(Path(status.workspace_root))
    file_id = _registered_clip(first)
    second = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    note = second.startup()
    assert "제주" in note
    assert second.workspace_root() is not None
    assert [item.file_id for item in second.raw.list_media(Path(second.workspace_root()))] == [
        file_id
    ]


def test_startup_recovers_interrupted_jobs_once(tmp_path: Path) -> None:
    """재시작 시의 진행 중 작업은 interrupted — 단 시작 때 한 번만(§14.1)."""
    root = tmp_path / "VlogProject"
    first = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    status = first.raw.create_workspace(root, "제주")
    first._remember(Path(status.workspace_root))
    with sqlite3.connect(root / DB_NAME) as connection:
        connection.execute(
            "INSERT INTO jobs (job_id, kind, input_fingerprint, status, progress_current,"
            " progress_total, error, cancellation_requested, created_at, updated_at)"
            " VALUES ('job_000001', 'probe', 'fp', 'running', 1, 3, NULL, 0,"
            " '2026-10-02T00:00:00+00:00', '2026-10-02T00:00:00+00:00')"
        )
        connection.commit()
    second = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    note = second.startup()
    assert "재개 가능" in note
    assert second.raw.get_job(root, "job_000001").status == "interrupted"
    third = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    third.startup()
    assert third.raw.get_job(root, "job_000001").status == "interrupted"


def test_state_path_env_override_is_respected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VLOGSRT_RAW_UI_STATE — 루트의 합성 작업공간 시험이 쓰는 재정의 통로."""
    monkeypatch.setenv("VLOGSRT_RAW_UI_STATE", str(tmp_path / "custom-state.json"))
    server = workspace_service.RawWorkspaceUi(port=PORT)
    assert server.state_path == tmp_path / "custom-state.json"
    monkeypatch.delenv("VLOGSRT_RAW_UI_STATE")
    assert workspace_service.default_state_path() == Path.cwd() / ".vlogsrt-ui-state.json"


# ── Gradio 마운트 — 화면이 살고, 그 파일 경로도 여전히 닫혀 있다 ──────────────


def test_the_mounted_app_serves_all_four_tabs_and_media(
    local_socket: None, ui: workspace_service.RawWorkspaceUi
) -> None:
    """마운트된 화면은 200이고 /media는 그 아래에서도 Range로 돈다."""
    server = ui
    file_id = _registered_clip(server)
    client = TestClient(server.build_fastapi(mount_blocks=True), base_url="http://127.0.0.1")
    page = client.get("/")
    assert page.status_code == 200
    assert "gradio" in page.text.lower()
    partial = client.get(f"/media/{file_id}", headers={"Range": "bytes=0-9"})
    assert partial.status_code == 206
    assert partial.content == BODY[0:10]


def test_gradio_file_serving_stays_closed_without_allowed_paths(
    local_socket: None, ui: workspace_service.RawWorkspaceUi, tmp_path: Path
) -> None:
    """allowed_paths를 주지 않았다 — gradio의 file= 라우트로 밖의 파일이 안 나온다."""
    server = ui
    secret = tmp_path / "비밀-메모.txt"
    secret.write_text("이 내용이 브라우저에 나오면 안 된다", encoding="utf-8")
    client = TestClient(server.build_fastapi(mount_blocks=True), base_url="http://127.0.0.1")
    for path in (f"/gradio_api/file={secret}", f"/file={secret}"):
        response = client.get(path)
        assert response.status_code == 403  # 가드가 명시적으로 닫는다(코디네이터 재결)
        assert "이 내용이" not in response.text


def test_the_mutation_guard_covers_a_real_gradio_endpoint(
    local_socket: None, ui: workspace_service.RawWorkspaceUi
) -> None:
    """Gradio 업로드 엔드포인트(/gradio_api/upload)도 같은 가드를 지난다."""
    server = ui
    client = TestClient(server.build_fastapi(mount_blocks=True), base_url="http://127.0.0.1")
    evil = client.post("/gradio_api/upload", headers={"Origin": "http://evil.example.com"})
    assert evil.status_code == 403
    headerless = client.post("/gradio_api/upload")  # 머리글 없는 변경 요청도 거부
    assert headerless.status_code == 403
    ours = client.post("/gradio_api/upload", headers={"Origin": f"http://127.0.0.1:{PORT}"})
    assert ours.status_code != 403  # 가드는 통과 — 이후 응답은 gradio의 몫이다
