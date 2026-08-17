"""의존성 없는 `.env` 로더.

`python-dotenv`를 쓰지 않는다 (불변 규칙 7). `KEY=value` 한 줄 형식만 지원한다.
"""

from __future__ import annotations

import os
from pathlib import Path

TRUTHY = frozenset({"1", "true", "yes", "on"})
DEFAULT_ENV_PATH = Path(".env")
# `vlogsrt/env.py` → `vlogsrt/` → 저장소 루트
PACKAGE_ROOT = Path(__file__).resolve().parent.parent

_loaded_from: Path | None = None
_root: Path | None = None


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


def find_env_file(start: Path | None = None) -> Path | None:
    """현재 위치에서 위로 올라가며 `.env`를 찾는다.

    `cd ~/Movies && vlogsrt record vlog.mp4` 처럼 저장소 밖에서 실행해도
    키를 찾을 수 있어야 한다. 못 찾으면 패키지가 설치된 저장소 루트를 본다.
    """
    here = (start or Path.cwd()).resolve()
    for directory in (here, *here.parents):
        candidate = directory / DEFAULT_ENV_PATH.name
        if candidate.is_file():
            return candidate
    fallback = PACKAGE_ROOT / DEFAULT_ENV_PATH.name
    return fallback if fallback.is_file() else None


def project_root() -> Path:
    """상대 경로 설정의 기준점. `.env`가 있는 디렉토리다."""
    ensure_loaded()
    return _root or Path.cwd()


def resolve_under_root(value: str) -> Path:
    """설정의 상대 경로를 저장소 기준으로 푼다. 실행 위치에 흔들리지 않게."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else project_root() / path


def ensure_loaded(path: Path | None = None) -> None:
    """프로세스당 한 번만 `.env`를 적용한다."""
    global _loaded_from, _root
    target = path if path is not None else find_env_file()
    if _loaded_from is not None and _loaded_from == target:
        return
    if target is not None:
        load_env(target)
        _root = target.parent
    else:
        _root = PACKAGE_ROOT if (PACKAGE_ROOT / "pyproject.toml").is_file() else Path.cwd()
    _loaded_from = target


def reset_cache() -> None:
    """테스트에서 `.env` 탐색을 다시 시키고 싶을 때만 쓴다."""
    global _loaded_from, _root
    _loaded_from = None
    _root = None


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
    return resolve_under_root(get("VLOGSRT_FIXTURE_DIR", "tests/fixtures/api"))


def work_dir() -> Path:
    return resolve_under_root(get("VLOGSRT_WORK_DIR", ".vlogsrt-cache"))


def golden_dir() -> Path:
    return resolve_under_root(get("VLOGSRT_GOLDEN_DIR", "golden"))


def max_workers() -> int:
    return max(1, get_int("VLOGSRT_MAX_WORKERS", 8))


def cost_guard_seconds() -> float:
    """이 길이를 넘는 오디오는 확인 없이 전사하지 않는다.

    비용 임계값이지 품질 임계값이 아니다. 쓰는 사람이 정하는 값이라
    `.env` 에서 자유롭게 올린다. 기본 7200초 = 2시간 ≈ $1.44.
    """
    return float(get_int("VLOGSRT_COST_GUARD_SECONDS", 7200))


def has_api_key() -> bool:
    """키가 있는지만 알려준다. **값은 절대 돌려주지 않는다.**"""
    return bool(get("OPENAI_API_KEY").strip())


def api_key_source() -> str:
    """키를 어디서 읽었는지. 화면과 로그에 쓰며 값은 담지 않는다."""
    if not has_api_key():
        return "없음"
    env_file = find_env_file()
    return f"{env_file}" if env_file else "셸 환경변수"


def require_api_key(force: bool = False) -> str:
    """실제 호출 직전에만 부른다. 오프라인이면 거부한다.

    `force`는 사람이 `record`나 `--online`으로 **명시적으로** 유료 호출을 요청한
    경우에만 참이다. 그 둘은 `VLOGSRT_OFFLINE` 값과 무관하게 동작해야 한다
    (.env.example 참조). 나머지 경로는 전부 오프라인 기본값에 막힌다.
    """
    if not force and is_offline():
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
