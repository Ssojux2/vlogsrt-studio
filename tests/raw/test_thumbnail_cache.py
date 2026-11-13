"""카드 썸네일의 브라우저 캐시(W-74, D125) — 새로고침 때 같은 그림을 다시 받지 않는다.

썸네일만 `Cache-Control: private, no-cache` + ETag(크기·mtime_ns)를 싣는다. 브라우저는 그림을
간직하되 쓸 때마다 묻고(no-cache), 서버는 등록 검증(§4.4 — 없는 ID 404 · 바뀐 파일 409)을 **먼저**
지난 뒤에만 같은 ETag에 본문 없는 304로 답한다 — `max-age`처럼 문을 건너뛰지 않는다. 영상·구간
요청은 예전 그대로 `no-store`다(플레이어의 구간 읽기에 캐시 의미를 섞지 않는다).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)
pytest.importorskip("fastapi", reason="웹 UI extra(fastapi)가 없다", exc_type=ImportError)

from fastapi.testclient import TestClient  # noqa: E402

from ui import workspace_service  # noqa: E402

PORT = 7860
JPEG = b"\xff\xd8\xff\xe0" + bytes(range(200)) + b"\xff\xd9"


@pytest.fixture
def ui(tmp_path: Path, local_socket: None) -> workspace_service.RawWorkspaceUi:
    root = tmp_path / "VlogProject"
    ui_server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    status = ui_server.raw.create_workspace(root, "제주")
    ui_server._remember(Path(status.workspace_root))
    return ui_server


def _registered(ui_server: workspace_service.RawWorkspaceUi, name: str, body: bytes) -> str:
    root = Path(ui_server.workspace_root())
    media = root / "previews" / "visual" / "thumbs" / name
    media.parent.mkdir(parents=True, exist_ok=True)
    media.write_bytes(body)
    return ui_server.raw.register_media(root, media, "preview").file_id


def _client(ui_server: workspace_service.RawWorkspaceUi) -> TestClient:
    return TestClient(ui_server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")


def test_a_thumbnail_carries_a_validator_and_asks_before_reuse(
    ui: workspace_service.RawWorkspaceUi,
) -> None:
    file_id = _registered(ui, "a.jpg", JPEG)
    response = _client(ui).get(f"/media/{file_id}")
    assert response.status_code == 200
    assert response.content == JPEG
    assert response.headers["cache-control"] == "private, no-cache"
    etag = response.headers["etag"]
    assert etag.startswith('"') and etag.endswith('"')
    # 같은 파일이면 같은 검증자다 — 그리기마다 바뀌지 않는다.
    assert _client(ui).get(f"/media/{file_id}").headers["etag"] == etag


@pytest.mark.parametrize("method", ["get", "head"])
def test_the_same_validator_answers_304_without_a_body(
    ui: workspace_service.RawWorkspaceUi, method: str
) -> None:
    file_id = _registered(ui, "a.jpg", JPEG)
    client = _client(ui)
    etag = client.get(f"/media/{file_id}").headers["etag"]
    again = getattr(client, method)(f"/media/{file_id}", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.content == b""
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "private, no-cache"


@pytest.mark.parametrize("form", ["W/{tag}", '"zzz", {tag}', "*"])
def test_weak_listed_and_star_validators_match(
    ui: workspace_service.RawWorkspaceUi, form: str
) -> None:
    file_id = _registered(ui, "a.jpg", JPEG)
    client = _client(ui)
    etag = client.get(f"/media/{file_id}").headers["etag"]
    header = form.replace("{tag}", etag)
    assert client.get(f"/media/{file_id}", headers={"If-None-Match": header}).status_code == 304


def test_a_different_validator_gets_the_full_picture(ui: workspace_service.RawWorkspaceUi) -> None:
    file_id = _registered(ui, "a.jpg", JPEG)
    response = _client(ui).get(f"/media/{file_id}", headers={"If-None-Match": '"other"'})
    assert response.status_code == 200
    assert response.content == JPEG


def test_two_thumbnails_never_share_a_validator(ui: workspace_service.RawWorkspaceUi) -> None:
    client = _client(ui)
    first = _registered(ui, "a.jpg", JPEG)
    second = _registered(ui, "b.jpg", JPEG + b"\x00")
    assert (
        client.get(f"/media/{first}").headers["etag"]
        != (client.get(f"/media/{second}").headers["etag"])
    )


def test_a_changed_thumbnail_still_answers_409_even_to_its_old_validator(
    ui: workspace_service.RawWorkspaceUi,
) -> None:
    """문(§4.4)이 먼저다 — 간직한 검증자가 바뀐 파일을 304로 통과시키지 않는다."""
    file_id = _registered(ui, "a.jpg", JPEG)
    client = _client(ui)
    etag = client.get(f"/media/{file_id}").headers["etag"]
    target = Path(ui.workspace_root()) / "previews" / "visual" / "thumbs" / "a.jpg"
    stat = target.stat()
    target.write_bytes(JPEG)  # 같은 내용·크기 — mtime만 바뀐다
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    response = client.get(f"/media/{file_id}", headers={"If-None-Match": etag})
    assert response.status_code == 409


def test_videos_and_range_reads_stay_uncached(ui: workspace_service.RawWorkspaceUi) -> None:
    root = Path(ui.workspace_root())
    clip = root / "originals" / "clip.mp4"
    clip.parent.mkdir(exist_ok=True)
    clip.write_bytes(bytes(range(256)) * 4)
    video_id = ui.raw.register_media(root, clip, "original").file_id
    thumb_id = _registered(ui, "a.jpg", JPEG)
    client = _client(ui)
    video = client.get(f"/media/{video_id}", headers={"If-None-Match": "*"})
    assert video.status_code == 200
    assert video.headers["cache-control"] == "no-store"
    assert "etag" not in video.headers
    partial = client.get(f"/media/{thumb_id}", headers={"Range": "bytes=0-9", "If-None-Match": "*"})
    assert partial.status_code == 206
    assert partial.headers["cache-control"] == "no-store"
    assert "etag" not in partial.headers
