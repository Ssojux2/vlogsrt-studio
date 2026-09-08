"""Final Cut 파생 계층의 설정 뿌리.

원본 `vlogsrt.config.Config`(whisper 파이프라인의 임계값)과는 **완전히 별개다** —
서로를 import하지도 상속하지도 않는다. 이 계층의 설정 파일은
`vlogsrt.project.toml` 하나뿐이고, P0의 유일한 키는 최상위 `schema_version`이다.
부모 테이블도, env 파일 탐색도, P5의 튜너블도 없다 — 같은 스키마/API의 확장은
별도 검토를 거친 공유 계약 태스크가 맡는다(P5-00).

읽기는 결정적이고 부작용이 없다: 파일을 읽는 것 외에 아무것도 하지 않는다.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA_VERSION = 1
DEFAULT_PROJECT_CONFIG_PATH = Path("vlogsrt.project.toml")

# P0의 전체 키 집합이다. 여기 없는 키는 전부 거부한다 — 조용히 넘기는 순간
# 오타가 기본값으로 둔갑한다.
_KNOWN_KEYS = frozenset({"schema_version"})


class ProjectConfigError(ValueError):
    """프로젝트 설정을 읽지 못했다. 메시지에 경로·키·버전을 담는다."""


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    """`vlogsrt.project.toml`의 전부. P0 필드는 `schema_version` 하나다."""

    schema_version: int = SUPPORTED_SCHEMA_VERSION


def load_project_config(path: Path | None = None) -> ProjectConfig:
    """`vlogsrt.project.toml`을 읽는다. `path=None`이면 cwd의 기본 경로를 본다.

    기본 경로가 **정말 없을 때만** 코드 기본값을 돌려준다 — 그 자리에 디렉터리가
    있거나 파일이 있는데 `schema_version`가 없으면 에러다. **명시적으로 준 경로**가
    없어도 에러다 — 조용한 성공이 더 위험하기 때문이다. 읽기·파싱·검증 전 과정에서
    파일 외의 어떤 상태도 건드리지 않는다.
    """
    if path is None:
        path = Path.cwd() / DEFAULT_PROJECT_CONFIG_PATH
        if not path.exists():
            return ProjectConfig()
        if not path.is_file():
            raise ProjectConfigError(f"프로젝트 설정 기본 경로가 파일이 아닙니다: {path}")
    elif not path.exists():
        raise ProjectConfigError(f"프로젝트 설정 파일이 없습니다: {path}")
    elif not path.is_file():
        raise ProjectConfigError(f"프로젝트 설정 경로가 파일이 아닙니다: {path}")
    return _parse(path, _read_table(path))


def _read_table(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except OSError as exc:
        raise ProjectConfigError(f"프로젝트 설정을 읽지 못했습니다: {path} ({exc})") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProjectConfigError(f"프로젝트 설정 TOML 문법 오류: {path} ({exc})") from exc


def _parse(path: Path, table: dict[str, Any]) -> ProjectConfig:
    for key in sorted(table):
        if key not in _KNOWN_KEYS:
            raise ProjectConfigError(
                f"{path}: 알 수 없는 프로젝트 설정 키 {key!r} — "
                f"이 계층의 키는 {sorted(_KNOWN_KEYS)}뿐이다"
            )
    if "schema_version" not in table:
        # 파일이 있는데 이 키가 없으면 기본값으로 둔갑한다 — 없는 파일과는 다르다.
        raise ProjectConfigError(
            f"{path}: 프로젝트 설정에 schema_version가 없다 — 이 키는 생략할 수 없다"
        )
    value = table["schema_version"]
    # bool은 int의 서브클래스다 — `true`가 1로 새어 들어가면 스키마 검사가 무력해진다.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProjectConfigError(
            f"{path}: schema_version는 정수여야 한다 (bool도 정수가 아니다): {value!r}"
        )
    if value != SUPPORTED_SCHEMA_VERSION:
        raise ProjectConfigError(
            f"{path}: 지원하지 않는 schema_version {value}다 — "
            f"이 코드는 {SUPPORTED_SCHEMA_VERSION}까지 안다"
        )
    return ProjectConfig(schema_version=value)
