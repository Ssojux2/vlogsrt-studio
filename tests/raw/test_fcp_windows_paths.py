"""링크 풀기의 어휘 부분은 앵커(드라이브)에서 다시 짓는다(D100 §1).

예전에는 `Path("/", *칸)`으로 다시 지어 Windows의 `C:\\`를 잃었다 — 폴더 불러오기가 전부
실패한다. Windows는 여기서 돌려 볼 수 없으므로 `PureWindowsPath`로 어휘 계산만 본다.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath, PureWindowsPath

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
