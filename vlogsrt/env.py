"""의존성 없는 `.env` 로더.

`python-dotenv`를 쓰지 않는다 (불변 규칙 7). `KEY=value` 한 줄 형식만 지원한다.
"""

from __future__ import annotations

import os
from pathlib import Path

TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_ENV_PATH = Path(".env")

_loaded_from: Path | None = None


def parse_env_text(text: str) -> dict[str, str]:
    """`.env` 본문을 파싱한다. 파일 입출력이 없어 테스트하기 쉽다."""
    parsed: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            continue
        parsed[key] = _strip_quotes(value.strip())
    return parsed


def _strip_quotes(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def load_env(path: Path = DEFAULT_ENV_PATH) -> dict[str, str]:
    """`.env`를 읽어 `os.environ`에 채운다. 이미 있는 키는 덮어쓰지 않는다(셸 우선)."""
    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"{path} 를 읽지 못했습니다: {exc}") from exc

    parsed = parse_env_text(text)
    for key, value in parsed.items():
        os.environ.setdefault(key, value)
    return parsed


def ensure_loaded(path: Path = DEFAULT_ENV_PATH) -> None:
    """프로세스당 한 번만 `.env`를 적용한다."""
    global _loaded_from
    if _loaded_from == path:
        return
    load_env(path)
    _loaded_from = path


def get(key: str, default: str = "") -> str:
    ensure_loaded()
    return os.environ.get(key, default)


def get_int(key: str, default: int) -> int:
    raw = get(key, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{key} 는 정수여야 합니다. 현재 값: {raw!r}") from exc


def is_offline() -> bool:
    """`VLOGSRT_OFFLINE`이 참 값이면 True. 설정이 없으면 True(오프라인이 기본)."""
    raw = get("VLOGSRT_OFFLINE", "1").strip().lower()
    return raw in TRUTHY


def fixture_dir() -> Path:
    return Path(get("VLOGSRT_FIXTURE_DIR", "tests/fixtures/api"))


def work_dir() -> Path:
    return Path(get("VLOGSRT_WORK_DIR", ".vlogsrt-cache"))


def golden_dir() -> Path:
    return Path(get("VLOGSRT_GOLDEN_DIR", "golden"))


def max_workers() -> int:
    return max(1, get_int("VLOGSRT_MAX_WORKERS", 8))


def cost_guard_seconds() -> float:
    return float(get_int("VLOGSRT_COST_GUARD_SECONDS", 3600))


def require_api_key() -> str:
    """실제 호출 직전에만 부른다. 오프라인이면 거부한다."""
    if is_offline():
        raise RuntimeError(
            "오프라인 모드입니다(VLOGSRT_OFFLINE=1). 실제 API 호출은 차단됩니다.\n"
            "픽스처가 없다면 사람이 직접 `vlogsrt record <video>`를 실행해야 합니다."
        )
    key = get("OPENAI_API_KEY").strip()
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY가 없습니다.\n"
            "  cp .env.example .env  후 키를 채우세요. "
            "이 파일은 .gitignore에 있으며 커밋되지 않습니다."
        )
    return key


def base_url() -> str | None:
    raw = get("OPENAI_BASE_URL").strip()
    return raw or None
