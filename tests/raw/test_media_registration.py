"""A1 미디어 등록·해석 — 안전한 루트 상대 ID의 경계(계획 §4.4·§16 A1).

제공은 등록된 ID로만 한다. 로그·설정·DB·API 응답 자리는 등록 단계에서 거부되고,
등록 뒤 바뀐 자리는 다시 등록하라는 상태로 거부된다. 합성 파일만 쓴다.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest

from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import (
    MediaNotAllowedError,
    MediaNotFoundError,
    MediaTargetChangedError,
)
from vlogsrt.project.raw.workspace import DB_NAME

FILE_ID_PATTERN = re.compile(r"^media_[0-9a-f]{16}$")


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


@pytest.fixture
def clip(workspace: Path) -> Path:
    """originals/ 아래의 합성 미디어 파일 — 내용은 아무 바이트나 상관없다."""
    target = workspace / "originals" / "import_000001" / "DJI_0001.MP4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x00\x00\x00 ftypisom" + b"x" * 90)
    return target


def test_register_gives_an_opaque_root_relative_id(workspace: Path, clip: Path) -> None:
    """브라우저에 나갈 ID는 경로가 아니다 — 128비트 무작위 형태의 불투명 ID다."""
    registration = raw.RawWorkspaceService().register_media(workspace, clip, "original")
    assert FILE_ID_PATTERN.match(registration.file_id)
    assert registration.relative_path == "originals/import_000001/DJI_0001.MP4"
    assert registration.status == "ready"


def test_register_and_resolve_roundtrip(workspace: Path, clip: Path) -> None:
    service = raw.RawWorkspaceService()
    registration = service.register_media(workspace, clip, "original")
    resolved = service.resolve_media(workspace, registration.file_id)
    assert resolved.path == clip
    assert resolved.size_bytes == clip.stat().st_size
    assert resolved.content_type == "video/mp4"
    assert [item.file_id for item in service.list_media(workspace)] == [registration.file_id]
    assert [item.file_id for item in service.list_media(workspace, kind="preview")] == []


@pytest.mark.parametrize(
    ("relative", "body"),
    [
        ("jobs/run.log", b"log"),
        ("subtitles/run/responses/resp.json", b"{}"),
        ("analysis/source/rev/media_probe.json", b"{}"),
        ("cache/audio.wav", b"w"),
        ("review/rev/user_edits.jsonl", b"{}"),
    ],
)
def test_protected_areas_never_register(workspace: Path, relative: str, body: bytes) -> None:
    """로그·API 원응답·분석 증거·캐시·검토 기록은 미디어 제공 대상이 아니다(§4.4)."""
    target = workspace / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(body)
    with pytest.raises(MediaNotAllowedError, match="아래만 등록할 수 있다|보호"):
        raw.RawWorkspaceService().register_media(workspace, target, "original")


def test_the_database_and_project_file_never_register(workspace: Path) -> None:
    """설정·DB를 통째로 내주는 길이 없다 — 원본 폴더 안이라도 마찬가지다."""
    service = raw.RawWorkspaceService()
    for name in (DB_NAME, "project.toml"):
        copy = workspace / "originals" / name
        copy.write_bytes((workspace / name).read_bytes())
        with pytest.raises(MediaNotAllowedError):
            service.register_media(workspace, copy, "original")


def test_paths_outside_the_workspace_never_register(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "밖.mp4"
    outside.write_bytes(b"outside")
    with pytest.raises(MediaNotAllowedError, match="작업공간 밖"):
        raw.RawWorkspaceService().register_media(workspace, outside, "original")


@pytest.mark.parametrize(
    "relative",
    [
        "originals/../project.sqlite3",
        "originals/../../탈출.mp4",
        "../VlogProject/project.toml",
    ],
)
def test_traversal_never_registers(workspace: Path, relative: str) -> None:
    """`..`는 정규화 뒤에도 보호 경계를 넘지 못한다."""
    target = workspace / relative
    with pytest.raises(MediaNotAllowedError):
        raw.RawWorkspaceService().register_media(workspace, target, "original")


def test_symlinks_never_register(workspace: Path, clip: Path, tmp_path: Path) -> None:
    """심볼릭 링크 — 작업공간 안을 가리켜도, 밖을 가리켜도 등록이 안 된다."""
    inside_link = workspace / "originals" / "안쪽-링크.mp4"
    inside_link.symlink_to(clip)
    outside = tmp_path / "밖.mp4"
    outside.write_bytes(b"outside")
    escape_link = workspace / "originals" / "탈출-링크.mp4"
    escape_link.symlink_to(outside)
    service = raw.RawWorkspaceService()
    with pytest.raises(MediaNotAllowedError):
        service.register_media(workspace, inside_link, "original")
    with pytest.raises(MediaNotAllowedError):
        service.register_media(workspace, escape_link, "original")


@pytest.mark.parametrize(
    "relative",
    [
        "originals/.env",  # 루트 재현 사례 — 숨김·자격 증명 철자
        "originals/private.txt",  # 루트 재현 사례 — 미디어가 아닌 확장자
        "originals/.env.backup.mp4",  # 숨김 접두가 붙으면 확장자가 맞아도 안 된다
        "originals/.hidden/clip.mp4",  # 숨김 디렉터리 아래
        "originals/notes.json",  # JSON(응답·기록)은 미디어가 아니다
        "originals/run.log",
        "originals/project.toml",
        "originals/확장자없음",
        "originals/sub/clip.MP4x",
    ],
)
def test_secret_and_non_media_spellings_never_register(workspace: Path, relative: str) -> None:
    """숨김·비밀·기록 철자와 미디어가 아닌 확장자는 어휘 단계에서 거절한다.

    파일을 실제로 만들지 않고도 거절해야 한다 — 보호 철자는 stat도 일으키지
    않는다(코디네이터 재결, 루트 독립 재현).
    """
    target = workspace / relative
    with pytest.raises(MediaNotAllowedError):
        raw.RawWorkspaceService().register_media(workspace, target, "original")


def test_kind_and_directory_must_match(workspace: Path, clip: Path) -> None:
    """종류↔자리 짝 — 원본은 originals/에, 미리보기는 previews/에만 등록된다."""
    service = raw.RawWorkspaceService()
    with pytest.raises(MediaNotAllowedError, match="아래에 있어야 한다"):
        service.register_media(workspace, clip, "preview")
    preview = workspace / "previews" / "clip.mp4"
    preview.write_bytes(b"preview")
    service.register_media(workspace, preview, "preview")
    with pytest.raises(MediaNotAllowedError, match="아래에 있어야 한다"):
        service.register_media(workspace, preview, "original")


def test_directories_and_missing_files_never_register(workspace: Path) -> None:
    service = raw.RawWorkspaceService()
    with pytest.raises(MediaNotAllowedError):
        service.register_media(workspace, workspace / "originals", "original")
    with pytest.raises(MediaNotAllowedError):
        service.register_media(workspace, workspace / "originals" / "없음.mp4", "original")


def test_unknown_kind_is_rejected(workspace: Path, clip: Path) -> None:
    with pytest.raises(ValueError, match="알 수 없는 미디어 종류"):
        raw.RawWorkspaceService().register_media(workspace, clip, "api-response")


def test_duplicate_registration_is_refused(workspace: Path, clip: Path) -> None:
    """같은 경로를 두 번 등록하지 않는다 — ID가 두 개가 되는 순간 추적이 깨진다."""
    service = raw.RawWorkspaceService()
    service.register_media(workspace, clip, "original")
    with pytest.raises(MediaNotAllowedError, match="이미 등록된 경로"):
        service.register_media(workspace, clip, "original")


@pytest.mark.parametrize(
    "file_id",
    [
        "media_999999",
        "originals/import_000001/DJI_0001.MP4",
        "../project.sqlite3",
        "media_000001/../../project.sqlite3",
        "",
    ],
)
def test_resolution_is_id_only(workspace: Path, clip: Path, file_id: str) -> None:
    """경로 모양·탈출 모양·빈 값은 전부 '없는 ID'다 — 해석 자체를 하지 않는다."""
    raw.RawWorkspaceService().register_media(workspace, clip, "original")
    with pytest.raises(MediaNotFoundError):
        raw.RawWorkspaceService().resolve_media(workspace, file_id)


def test_a_changed_registered_target_is_rejected(workspace: Path, clip: Path) -> None:
    """등록 때의 지문(size·mtime·inode)과 어긋난 자리는 다시 등록해야 한다."""
    service = raw.RawWorkspaceService()
    registration = service.register_media(workspace, clip, "original")
    clip.write_bytes("전혀 다른 내용으로 바뀌었다 — 크기도 다르다".encode())
    with pytest.raises(MediaTargetChangedError, match="다시 등록"):
        service.resolve_media(workspace, registration.file_id)


def test_a_replaced_symlink_is_rejected(workspace: Path, clip: Path, tmp_path: Path) -> None:
    """등록된 자리가 링크로 바뀌면 거부한다 — 링크가 가리키는 걸 몰래 내주지 않는다."""
    service = raw.RawWorkspaceService()
    registration = service.register_media(workspace, clip, "original")
    other = tmp_path / "다른-파일.mp4"
    other.write_bytes(clip.read_bytes())
    clip.unlink()
    clip.symlink_to(other)
    with pytest.raises(MediaTargetChangedError):
        service.resolve_media(workspace, registration.file_id)


def test_a_deleted_target_is_not_found(workspace: Path, clip: Path) -> None:
    service = raw.RawWorkspaceService()
    registration = service.register_media(workspace, clip, "original")
    clip.unlink()
    with pytest.raises(MediaNotFoundError, match="등록된 파일이 없다"):
        service.resolve_media(workspace, registration.file_id)


def test_a_not_ready_status_is_not_servable(workspace: Path, clip: Path) -> None:
    """복사 중(A2가 만들 상태) 등록 행은 ready가 아니면 제공하지 않는다."""
    service = raw.RawWorkspaceService()
    registration = service.register_media(workspace, clip, "original")
    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.execute(
            "UPDATE media_files SET status = 'copying' WHERE file_id = ?",
            (registration.file_id,),
        )
        connection.commit()
    with pytest.raises(raw.MediaNotReadyError):
        service.resolve_media(workspace, registration.file_id)


def test_media_ids_are_globally_unique_across_workspaces(tmp_path: Path) -> None:
    """루트 재현 — 독립 작업공간 둘의 첫 등록이 같은 ID를 만들지 않는다.

    예전의 `media_000001` 국소 번호는 두 프로젝트에서 같은 표기를 가져, 서버가
    다른 작업공간을 가리키는 순간 남의 파일로 조용히 이어졌다.
    """
    service = raw.RawWorkspaceService()
    ids = set()
    for name in ("a", "b"):
        root = tmp_path / name
        service.create_workspace(root, name)
        clip = root / "originals" / f"{name}.mp4"
        clip.write_bytes(name.encode() * 20)
        ids.add(service.register_media(root, clip, "original").file_id)
    assert len(ids) == 2
    assert all(FILE_ID_PATTERN.match(file_id) for file_id in ids)


def test_a_foreign_media_id_is_not_resolved(tmp_path: Path) -> None:
    """A의 미디어 ID를 B에서 물으면 '없는 ID'다 — B의 파일로 이어지지 않는다."""
    service = raw.RawWorkspaceService()
    roots = []
    for name in ("a", "b"):
        root = tmp_path / name
        service.create_workspace(root, name)
        clip = root / "originals" / f"{name}.mp4"
        clip.write_bytes(name.encode() * 20)
        registration = service.register_media(root, clip, "original")
        roots.append((root, registration.file_id))
    (a, ida), (b, _idb) = roots
    with pytest.raises(MediaNotFoundError):
        service.resolve_media(b, ida)
    # 소유 작업공간에서는 여전히 열린다 — URL은 소유자에게 보존된다.
    assert service.resolve_media(a, ida).path == a / "originals" / "a.mp4"


def test_listing_keeps_registration_order(workspace: Path) -> None:
    """ID가 무작위라도 목록은 등록 순서다 — 삽입 행(rowid)이 근거다."""
    service = raw.RawWorkspaceService()
    expected = []
    for index in range(3):
        clip = workspace / "originals" / f"clip{index}.mp4"
        clip.write_bytes(b"c" * (index + 1))
        expected.append(service.register_media(workspace, clip, "original").relative_path)
    assert [item.relative_path for item in service.list_media(workspace)] == expected
