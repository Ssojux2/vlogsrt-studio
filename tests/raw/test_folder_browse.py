"""A1 폴더 탐색 — 명시적 요청 한 번, 한 층, 내용은 읽지 않는다(계획 §4.2·§5.2).

점으로 시작하는 항목(.env·.env.example·자격 증명이 사는 자리)은 목록에 오르지
않고, 심볼릭 링크는 따라가지 않는다. 마운트 후보도 버튼을 눌렀을 때만 본다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import BrowseNotAllowedError, FolderListing


@pytest.fixture
def card(tmp_path: Path) -> Path:
    """SD 카드 흉내의 합성 폴더 — 숨김 파일·링크·미디어·문서를 섞어 둔다.

    `.env`·`.env.example`은 테스트 보호가 만들지도 못하게 막는다(conftest의 dotenv
    guard) — 그래서 여기선 다른 점 항목으로 숨김 규칙을 증명한다. 숨김 판정은
    철자 접두('.')라 어떤 점 항목이든 같은 길을 지난다.
    """
    root = tmp_path / "SDCARD" / "DCIM"
    media = root / "100MEDIA"
    media.mkdir(parents=True)
    (media / "DJI_0001.MP4").write_bytes(b"clip")
    (media / "DJI_0002.mov").write_bytes(b"clip")
    (root / "MISC").mkdir()
    (root / "MISC" / "note.txt").write_text("문서", encoding="utf-8")
    (root / ".DS_Store").write_bytes(b"\x00")
    (root / ".hidden-credentials").write_text("synthetic-secret", encoding="utf-8")
    (root / ".secrets").mkdir()
    (root / "README.TXT").write_text("카드 안내", encoding="utf-8")
    return root


def test_browse_lists_one_level_without_hidden_entries(card: Path) -> None:
    """한 층만 보인다 — 미디어 후보·폴더만 나오고 점 항목은 아예 오르지 않는다."""
    listing = raw.browse_folder(str(card))
    assert isinstance(listing, FolderListing)
    names = [entry.name for entry in listing.entries]
    assert "100MEDIA" in names
    assert "MISC" in names
    assert ".DS_Store" not in names
    assert ".hidden-credentials" not in names
    assert ".secrets" not in names
    assert listing.path == str(card)
    assert listing.parent == str(card.parent)


def test_browse_marks_media_candidates_without_probing(card: Path) -> None:
    """미디어 후보는 표시만 — ffprobe 조사·재귀 스캔은 일어나지 않는다(A2)."""
    listing = raw.browse_folder(str(card / "100MEDIA"))
    entries = {entry.name: entry for entry in listing.entries}
    assert set(entries) == {"DJI_0001.MP4", "DJI_0002.mov"}
    assert all(not entry.is_directory for entry in entries.values())
    assert all(entry.is_media_candidate for entry in entries.values())
    deeper = raw.browse_folder(str(card))
    assert all(
        "DJI_0001.MP4" not in entry.path or entry.is_directory is False for entry in deeper.entries
    )


def test_browse_skips_symlink_entries(card: Path, tmp_path: Path) -> None:
    """링크 항목은 목록에서 건너뛴다 — 선택한 루트 밖으로 몰래 나가지 않는다(§5.2)."""
    (card / "링크폴더").symlink_to(tmp_path / "다른-세계")
    (tmp_path / "다른-세계").mkdir()
    (card / "링크.mp4").symlink_to(card / "100MEDIA" / "DJI_0001.MP4")
    names = [entry.name for entry in raw.browse_folder(str(card)).entries]
    assert "링크폴더" not in names
    assert "링크.mp4" not in names


@pytest.mark.parametrize(
    ("raw_path", "fragment"),
    [
        ("", "폴더 경로를 입력"),
        ("   ", "폴더 경로를 입력"),
        (os.path.abspath("/없는-경로/아마도"), "없는 경로"),
        (__file__, "폴더가 아니다"),  # 어느 운영체제에나 있는 파일 — `/etc/hosts`는 POSIX뿐이다
    ],
)
def test_browse_rejects_bad_targets(raw_path: str, fragment: str) -> None:
    with pytest.raises(BrowseNotAllowedError, match=fragment):
        raw.browse_folder(raw_path)


def test_browse_accepts_quoted_paths(card: Path) -> None:
    listing = raw.browse_folder(f'  "{card}"  ')
    assert listing.path == str(card)


def test_browse_resolves_relative_paths_against_a_base(card: Path) -> None:
    listing = raw.browse_folder("DCIM", base=card.parent)
    assert listing.path == str(card)


def test_mounted_device_candidates_list_only_real_directories(tmp_path: Path) -> None:
    """후보는 진짜 폴더만 — 점 항목·링크·파일은 빠진다. 기본 경로가 없으면 비운다."""
    volumes = tmp_path / "Volumes"
    (volumes / "WORK").mkdir(parents=True)
    (volumes / "CAMERA").mkdir()
    (volumes / ".hidden-disk").mkdir()
    (volumes / "링크디스크").symlink_to(volumes / "WORK")
    (volumes / "README.TXT").write_text("문서", encoding="utf-8")
    candidates = raw.mounted_device_candidates(bases=(volumes,))
    assert [path.name for path in candidates] == ["CAMERA", "WORK"]
    assert raw.mounted_device_candidates(bases=(tmp_path / "없는-마운트",)) == ()
