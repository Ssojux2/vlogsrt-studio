"""A6 캐시 정리 정책 — 지울 수 있는 것은 `cache/`와 `previews/`뿐이다(§9.2·16-A6).

`docs/plans/2026-10-02-raw-video-browser-v2.md` §9.2: `cache/`·`previews/`는 지울 수
있지만 `originals/`·확정된 `clips/`·`final/`·`subtitles/`은 일반 캐시 정리 대상이
아니다. 이 모듈은 그 **정책만** 산다 — 계획(plan)과 실행(execute)을 나눠서, 실행
직전에 지워도 되는 길인지를 다시 검사한다.

지키는 선:

- **보호 이름은 위치를 못 속인다.** originals·clips·final·subtitles·review·
  analysis·jobs·config·project DB·`confirmed_*`는 `cache/` 안에 있더라도(잘못
  붙은 이름이라도) 지우지 않고 거부 목록에 이유를 남긴다.
- **심볼릭 링크 안쪽으로 따라가지 않는다.** 링크 자체만 지운다 — 링크가 가리키는
  원본·확정 미디어는 그대로 남는다(§9.4).
- **암묵적 제거는 없다.** 폴더를 훑아 알아서 지우는 길은 계획 단계에도 없다.
  작업공간 뿌리에서 `cache/`·`previews/` 두 디렉터리만 본다.
- **확정 미디어는 캐시가 아니**므로 이 정리와 무관하게 살아남고, 검증은
  `confirmed_package.verify_confirmed_export`가 폴더 안 해시로 한다.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISDIR, S_ISLNK, S_ISREG

__all__ = [
    "CachePolicyError",
    "CachePurgePlan",
    "CachePurgeResult",
    "RefusedPath",
    "execute_cache_purge",
    "plan_cache_purge",
]

PURGEABLE_DIR_NAMES: tuple[str, ...] = ("cache", "previews")
"""지울 수 있는 작업공간 최상위 디렉터리 — 이 둘뿐이다(§9.2)."""

PROTECTED_DIR_NAMES: tuple[str, ...] = (
    "originals",
    "clips",
    "final",
    "subtitles",
    "review",
    "analysis",
    "jobs",
    "config",
)
"""보호 디렉터리 이름 — cache 안에 있어도(잘못 붙은 이름) 지우지 않는다."""

PROTECTED_FILE_NAMES: tuple[str, ...] = ("project.toml", "project.sqlite3")
"""보호 파일 이름 — 작업공간의 설정·DB 정본이다."""

CONFIRMED_NAME_PREFIX = "confirmed_"
"""확정 내보내기 이름 — `clips/confirmed_*`는 물론 어디에 있어도 보호한다."""


class CachePolicyError(RuntimeError):
    """캐시 정리 계약 위반 — 사유를 사람에게 그대로 보여준다."""


@dataclass(frozen=True, slots=True)
class RefusedPath:
    """지우지 않기로 한 경로 하나 — 이유까지 남긴다(조용히 넘기지 않는다)."""

    relative_path: str
    reason: str


@dataclass(frozen=True, slots=True)
class CachePurgePlan:
    """캐시 정리 계획 — 지울 경로(깊은 것부터)와 거부된 경로의 묶음."""

    workspace_root: str
    purge_paths: tuple[str, ...]
    refused: tuple[RefusedPath, ...]


@dataclass(frozen=True, slots=True)
class CachePurgeResult:
    """캐시 정리 실행 결과 — 지운 파일 수·내보낸 바이트·거부 목록."""

    removed_files: int
    removed_bytes: int
    refused: tuple[RefusedPath, ...]


def plan_cache_purge(workspace_root: Path) -> CachePurgePlan:
    """작업공간의 캐시를 훑아 계획을 짠다 — 이 단계에서는 아무것도 지우지 않는다."""
    root = _require_real_root(workspace_root)
    purge: list[str] = []
    refused: list[RefusedPath] = []
    for name in PURGEABLE_DIR_NAMES:
        top = root / name
        if not os.path.lexists(top):
            continue
        if top.is_symlink():
            refused.append(RefusedPath(name, "캐시 뿌리가 심볼릭 링크다"))
            continue
        if not top.is_dir():
            refused.append(RefusedPath(name, "캐시 뿌리가 디렉터리가 아니다"))
            continue
        _collect_under(root, top, purge, refused)
    deepest_first = sorted(purge, key=lambda rel: (-rel.count("/"), rel))
    return CachePurgePlan(
        workspace_root=str(root), purge_paths=tuple(deepest_first), refused=tuple(refused)
    )


def _collect_under(root: Path, top: Path, purge: list[str], refused: list[RefusedPath]) -> None:
    """캐시 뿌리 한 개 아래를 훑는다 — 링크는 잎으로만 보고 안쪽을 따라가지 않는다."""
    for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
        here = Path(dirpath)
        for name in dirnames + filenames:
            relative = (here / name).relative_to(root).as_posix()
            reason = _refusal_reason(relative)
            if reason is None:
                purge.append(relative)
            else:
                refused.append(RefusedPath(relative, reason))


def execute_cache_purge(workspace_root: Path, plan: CachePurgePlan) -> CachePurgeResult:
    """계획을 실행한다 — 지우기 직전에 같은 검문을 다시 통과시킨다."""
    root = _require_real_root(workspace_root)
    if plan.workspace_root != str(root):
        raise CachePolicyError(
            f"다른 작업공간의 계획이다: {plan.workspace_root} ≠ {str(root)} — 다시 계획하세요"
        )
    removed_files = 0
    removed_bytes = 0
    refused = list(plan.refused)
    for relative in plan.purge_paths:
        reason = _refusal_reason(relative)
        if reason is not None:
            raise CachePolicyError(f"계획이 변조됐다(지울 수 없는 경로): {relative} — {reason}")
        path = root.joinpath(*relative.split("/"))
        _require_real_parents(root, relative)
        facts = path.lstat()
        if S_ISLNK(facts.st_mode):
            os.unlink(path)  # 링크만 지운다 — 가리킨 대상은 모른 척한다.
            removed_files += 1
        elif S_ISDIR(facts.st_mode):
            try:
                os.rmdir(path)
            except OSError as error:
                if error.errno != errno.ENOTEMPTY:
                    raise
                # 채워진 디렉터리는 지우지 않고 이유만 남긴다 — 경합이면 다시 계획한다.
                refused.append(RefusedPath(relative, "비어 있지 않다 — 다시 계획하세요"))
        elif S_ISREG(facts.st_mode):
            removed_bytes += facts.st_size
            os.unlink(path)
            removed_files += 1
        else:
            refused.append(RefusedPath(relative, "일반 파일이 아니다"))
    return CachePurgeResult(
        removed_files=removed_files, removed_bytes=removed_bytes, refused=tuple(refused)
    )


def _refusal_reason(relative_path: str) -> str | None:
    """이 경로를 지우면 안 되는 이유 — 없으면(None) 지워도 된다."""
    parts = relative_path.split("/")
    if os.path.isabs(relative_path) or ".." in parts or "." in parts or "" in parts:
        return "경로가 상대 경로 계약을 벗어났다"
    if parts[0] not in PURGEABLE_DIR_NAMES:
        return f"{parts[0]!r}은(는) 지울 수 있는 캐시 뿌리가 아니다"
    for part in parts[1:]:
        if part in PROTECTED_DIR_NAMES or part in PROTECTED_FILE_NAMES:
            return f"보호 이름 {part!r}은(는) cache 안에 있어도 지우지 않는다"
        if part.startswith(CONFIRMED_NAME_PREFIX):
            return f"확정 내보내기 {part!r}은(는) cache 안에 있어도 지우지 않는다"
    return None


def _require_real_parents(root: Path, relative_path: str) -> None:
    """지울 경로의 이음초가 전부 실제 디렉터리인가 — 링크 경유 삭제를 막는다."""
    walked = root
    for part in relative_path.split("/")[:-1]:
        walked = walked / part
        if walked.is_symlink() or not walked.is_dir():
            raise CachePolicyError(f"지울 경로의 중간이 실제 디렉터리가 아니다: {walked}")


def _require_real_root(workspace_root: Path) -> Path:
    """작업공간 뿌리 — 심볼릭 링크 뿌리는 아예 받지 않는다(경로 혼동의 원천)."""
    if workspace_root.is_symlink():
        raise CachePolicyError(f"작업공간 뿌리가 심볼릭 링크다: {workspace_root}")
    if not workspace_root.is_dir():
        raise CachePolicyError(f"작업공간 뿌리가 디렉터리가 아니다: {workspace_root}")
    return workspace_root.resolve()
