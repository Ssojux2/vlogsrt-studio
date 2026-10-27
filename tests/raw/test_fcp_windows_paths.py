"""링크 풀기의 어휘 부분은 앵커(드라이브)에서 다시 짓는다(D100 §1).

예전에는 `Path("/", *칸)`으로 다시 지어 Windows의 `C:\\`를 잃었다 — 폴더 불러오기가 전부
실패한다. Windows는 여기서 돌려 볼 수 없으므로 `PureWindowsPath`로 어휘 계산만 본다.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

import pytest

from vlogsrt.project.raw import fcp


@pytest.mark.parametrize(
    ("path", "anchor", "parts"),
    [
        (PureWindowsPath(r"C:\Users\me\clip.mov"), "C:\\", ["Users", "me", "clip.mov"]),
        (PureWindowsPath(r"\\nas\share\trip\a.mov"), "\\\\nas\\share\\", ["trip", "a.mov"]),
        (PurePosixPath("/Volumes/SD/a.mov"), "/", ["Volumes", "SD", "a.mov"]),
        (PurePosixPath("//Volumes/a.mov"), "/", ["Volumes", "a.mov"]),  # 예전처럼 `/`
        (PurePosixPath("relative/a.mov"), "", ["relative", "a.mov"]),
    ],
)
def test_the_anchor_is_split_off_and_kept(path: object, anchor: str, parts: list[str]) -> None:
    assert fcp._split_anchored(path) == (anchor, parts)  # type: ignore[arg-type]


def _walk(anchor: str, parts: list[str]) -> tuple[list[str], list[str]]:
    """조회 없이 어휘 단계만 — (후보 경로들, 사유들)."""
    resolved: list[str] = []
    candidates: list[str] = []
    reasons: list[str] = []
    for part in parts:
        candidate, reason = fcp._lexical_step(PureWindowsPath, anchor, resolved, part)
        if candidate is None:
            continue
        candidates.append(str(candidate))
        reasons.append(reason)
        if reason:
            break
        resolved.append(part)
    return candidates, reasons


def test_candidates_keep_the_drive() -> None:
    anchor, parts = fcp._split_anchored(PureWindowsPath(r"D:\Trip\clip.mov"))
    candidates, reasons = _walk(anchor, parts)
    assert candidates == [r"D:\Trip", r"D:\Trip\clip.mov"]
    assert reasons == ["", ""]


def test_dot_dot_steps_back_but_never_above_the_drive() -> None:
    candidates, _ = _walk("E:\\", ["..", "..", "Trip", ".", "", "..", "Day2", "a.mov"])
    assert candidates == [r"E:\Trip", r"E:\Day2", r"E:\Day2\a.mov"]


@pytest.mark.parametrize("hidden", [".hidden", ".env", "credentials.json"])
def test_hidden_or_protected_components_are_refused_before_any_lookup(hidden: str) -> None:
    candidates, reasons = _walk("C:\\", ["Users", hidden, "a.mov"])
    assert candidates[-1] == rf"C:\Users\{hidden}"
    assert reasons[-1] == "링크 대상이 숨김·보호 경로다 — 가져올 수 없다"


def test_protection_checks_every_component_but_not_the_anchor() -> None:
    assert not fcp._protected_target_path(PureWindowsPath(r"C:\Users\me\a.mov"))
    assert not fcp._protected_target_path(PureWindowsPath(r"\\nas\share\a.mov"))
    assert fcp._protected_target_path(PureWindowsPath(r"C:\a\.cache\a.mov"))
    assert fcp._protected_target_path(PurePosixPath("/a/.cache/a.mov"))
    assert not fcp._protected_target_path(PurePosixPath("/a/b/a.mov"))


def test_posix_resolution_is_unchanged(tmp_path: Path) -> None:
    """실제 파일 시스템 위의 풀기 — 앵커가 `/`인 경로는 예전과 같은 값을 낸다."""
    real = tmp_path / "media" / "a.mov"
    real.parent.mkdir()
    real.write_bytes(b"x")
    alias = tmp_path / "alias"
    alias.symlink_to("media")
    target, reason, probed = fcp._resolve_path_safely(alias / ".." / "alias" / "a.mov")
    assert reason == "" and probed is not None
    assert target == real.resolve()
    hidden = tmp_path / ".secret"
    hidden.mkdir()
    (tmp_path / "pointer").symlink_to(hidden)
    target, reason, probed = fcp._resolve_path_safely(tmp_path / "pointer" / "a.mov")
    assert reason == "링크 대상이 숨김·보호 경로다 — 가져올 수 없다" and probed is None


@pytest.mark.parametrize(
    ("start", "target", "refused"),
    [
        ("C:\\", "\\\\attacker\\share\\", True),  # 로컬 → 새 공유: 조회 전에 끊는다
        ("\\\\nas\\share\\", "\\\\other\\share\\", True),
        ("\\\\nas\\share\\", "\\\\NAS\\Share\\", False),  # 같은 공유(대소문자 무시)
        ("C:\\", "D:\\", False),  # 다른 로컬 드라이브는 받는다
        ("/", "/", False),  # POSIX `//a`도 `/` 앵커라 해당 없다
    ],
)
def test_a_link_hop_to_a_new_unc_share_is_refused(start: str, target: str, refused: bool) -> None:
    assert fcp._hops_to_new_share(start, target) is refused


# ── Windows 링크 표기(D100 §1b) ──────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("\\\\?\\C:\\Media\\a.mov", "C:\\Media\\a.mov"),
        ("\\\\?\\c:\\Media", "c:\\Media"),
        ("\\??\\D:\\Trip", "D:\\Trip"),  # 연결 지점의 NT 표기
        ("\\\\?\\UNC\\nas\\share\\trip\\a.mov", "\\\\nas\\share\\trip\\a.mov"),
        ("\\\\?\\unc\\nas\\share", "\\\\nas\\share"),
        ("\\\\?\\Volume{1234}\\a.mov", "\\\\?\\Volume{1234}\\a.mov"),  # 장치 — 그대로
        ("C:\\plain", "C:\\plain"),
        ("relative\\a.mov", "relative\\a.mov"),
    ],
)
def test_the_extended_length_prefix_is_removed(text: str, expected: str) -> None:
    assert fcp._without_verbatim_prefix(text) == expected


def _hop(text: str, anchor: str = "C:\\", resolved: tuple[str, ...] = ("Lib", "Media")) -> str:
    return str(fcp._link_target(PureWindowsPath, text, anchor, list(resolved)))


@pytest.mark.parametrize(
    ("text", "anchor", "expected"),
    [
        ("\\\\?\\C:\\Media\\a.mov", "C:\\", "C:\\Media\\a.mov"),
        ("\\\\?\\E:\\Other\\a.mov", "C:\\", "E:\\Other\\a.mov"),
        ("\\\\?\\UNC\\nas\\share\\a.mov", "\\\\nas\\share\\", "\\\\nas\\share\\a.mov"),
        ("\\foo\\a.mov", "D:\\", "D:\\foo\\a.mov"),  # 루트 상대 — 링크의 드라이브
        ("\\foo", "\\\\nas\\share\\", "\\\\nas\\share\\foo"),  # 공유 위의 루트 상대
        ("D:foo\\a.mov", "D:\\", "D:\\Lib\\Media\\foo\\a.mov"),  # 같은 드라이브 — 링크 폴더
        ("d:foo", "D:\\", "D:\\Lib\\Media\\foo"),
        ("E:foo\\a.mov", "D:\\", "E:\\foo\\a.mov"),  # 다른 드라이브 — 그 루트(작업 폴더 아님)
        ("..\\Other\\a.mov", "C:\\", "C:\\Lib\\Other\\a.mov"),
        ("C:\\x\\..\\y", "C:\\", "C:\\y"),
    ],
)
def test_windows_link_targets_never_depend_on_the_working_directory(
    text: str, anchor: str, expected: str
) -> None:
    assert _hop(text, anchor) == expected


def test_posix_link_targets_are_unchanged() -> None:
    def hop(text: str) -> str:
        return str(fcp._link_target(PurePosixPath, text, "/", ["Lib", "Media"]))

    assert hop("/Volumes/SD/a.mov") == "/Volumes/SD/a.mov"
    assert hop("../Other/a.mov") == "/Lib/Other/a.mov"
    assert hop("\\\\?\\C:\\x") == "/Lib/Media/\\\\?\\C:\\x"  # POSIX에서는 그냥 이름이다


@pytest.mark.parametrize(
    ("text", "start", "refused"),
    [
        ("\\\\?\\C:\\Media\\a.mov", "C:\\", False),  # 예전: `\\\\?\\C:\\`가 새 공유로 끊겼다
        ("\\\\?\\D:\\Media\\a.mov", "C:\\", False),
        ("\\\\?\\UNC\\nas\\share\\a.mov", "\\\\nas\\share\\", False),
        ("\\\\?\\UNC\\NAS\\Share\\a.mov", "\\\\nas\\share\\", False),  # 같은 공유
        ("\\\\?\\UNC\\attacker\\share\\a.mov", "C:\\", True),
        ("\\\\?\\UNC\\other\\share\\a.mov", "\\\\nas\\share\\", True),
        ("\\\\?\\Volume{1234}\\a.mov", "C:\\", True),
    ],
)
def test_share_checks_see_the_normalised_anchor(text: str, start: str, refused: bool) -> None:
    anchor, _parts = fcp._split_anchored(fcp._link_target(PureWindowsPath, text, start, []))
    assert fcp._hops_to_new_share(start, anchor) is refused


def test_device_hops_have_their_own_reason() -> None:
    assert "장치 경로" in fcp._hop_reason("\\\\?\\Volume{1234}\\")
    assert "UNC" in fcp._hop_reason("\\\\attacker\\share\\")


def test_the_workspace_guard_compares_normalised_paths() -> None:
    target = fcp._plain(PureWindowsPath("\\\\?\\C:\\Work\\_vlogsrt\\originals\\a.mov"))
    workspace = fcp._plain(PureWindowsPath("C:\\work\\_VLOGSRT"))
    assert target.is_relative_to(workspace)  # 접두·대소문자에 속지 않는다
    assert fcp._plain(PurePosixPath("/a/b")) == PurePosixPath("/a/b")


# ── Windows 연결 지점(junction) ─────────────────────────────────────────────


class _FakeStat:
    def __init__(self, mode: int, tag: int = 0) -> None:
        self.st_mode = mode
        self.st_reparse_tag = tag


def test_a_junction_counts_as_a_link_and_a_plain_directory_does_not() -> None:
    import stat

    junction = _FakeStat(stat.S_IFDIR | 0o755, 0xA0000003)
    assert fcp._is_link(junction)  # type: ignore[arg-type]
    assert not fcp._is_link(_FakeStat(stat.S_IFDIR | 0o755))  # type: ignore[arg-type]
    assert fcp._is_link(_FakeStat(stat.S_IFLNK | 0o777))  # type: ignore[arg-type]
    other_reparse = _FakeStat(stat.S_IFREG | 0o644, 0x8000001B)  # 앱 실행 별칭 등
    assert not fcp._is_link(other_reparse)  # type: ignore[arg-type]


def _fake_junction(monkeypatch: pytest.MonkeyPatch, junction: Path, target_text: str) -> list[str]:
    """`junction` 칸만 연결 지점으로 보이게 한다 — 나머지 칸은 진짜 lstat. 읽은 표기를 센다."""
    import stat

    read: list[str] = []
    real_lstat, real_readlink = os.lstat, os.readlink

    def lstat(path: Any) -> Any:
        if Path(path) == junction:
            return _FakeStat(stat.S_IFDIR | 0o755, 0xA0000003)
        return real_lstat(path)

    def readlink(path: Any) -> str:
        if Path(path) == junction:
            read.append(str(path))
            return target_text
        return real_readlink(path)

    monkeypatch.setattr(fcp.os, "lstat", lstat)
    monkeypatch.setattr(fcp.os, "readlink", readlink)
    return read


def test_a_junction_is_followed_hop_by_hop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    real = tmp_path / "media" / "a.mov"
    real.parent.mkdir()
    real.write_bytes(b"x")
    junction = tmp_path / "junction"
    junction.mkdir()  # 진짜로는 빈 폴더 — 연결 지점이면 따라가야 a.mov가 보인다
    read = _fake_junction(monkeypatch, junction, str(tmp_path / "media"))
    target, reason, probed = fcp._resolve_path_safely(junction / "a.mov")
    assert (reason, target) == ("", real)
    assert probed is not None and read == [str(junction)]


def test_a_junction_into_a_hidden_folder_is_refused_before_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    junction = tmp_path / "junction"
    junction.mkdir()
    _fake_junction(monkeypatch, junction, str(tmp_path / ".secret"))
    _target, reason, probed = fcp._resolve_path_safely(junction / "a.mov")
    assert reason == "링크 대상이 숨김·보호 경로다 — 가져올 수 없다" and probed is None


def test_walks_skip_junctions_only_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import stat
    import sys

    (tmp_path / "real").mkdir()
    (tmp_path / "junction").mkdir()
    names = ["junction", "real"]
    fcp._skip_junctions(str(tmp_path), names)
    assert names == ["junction", "real"]  # POSIX — 조회도 더하지 않는다
    real_lstat = os.lstat

    def lstat(path: Any) -> Any:
        if Path(path).name == "junction":
            return _FakeStat(stat.S_IFDIR | 0o755, 0xA0000003)
        return real_lstat(path)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(fcp.os, "lstat", lstat)
    fcp._skip_junctions(str(tmp_path), names)
    assert names == ["real"]
    assert fcp._is_link_path(tmp_path / "junction")
    assert not fcp._is_link_path(tmp_path / "real")
