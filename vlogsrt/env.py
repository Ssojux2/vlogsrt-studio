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


def has_anthropic_api_key() -> bool:
    """Claude 의미 분석 키가 있는지만 알려준다(D54). **값은 절대 돌려주지 않는다.**"""
    return bool(get(ANALYSIS_CLAUDE_KEY_ENV).strip())


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


# ─────────────────────────────────────────────────────────────
#  역할 자격 증명 (S2A-2 단위2) — STT와 비전은 각자의 이름·값을 가진다
# ─────────────────────────────────────────────────────────────

# 엔진 STT 키의 이름 — 원본 전사 계약 그대로다.
STT_ROLE_KEY_ENV = "OPENAI_API_KEY"
# 비전 역할 이름의 기본 — 실제 이름은 작업이 쓰는 공개 프로젝트 설정이 정한다.
VISION_ROLE_DEFAULT_KEY_ENV = "GOOGLE_API_KEY"
VISION_ROLE_DEFAULT_URL_ENV = "GEMINI_BASE_URL"

# 앱이 역할 이름으로 쓸 수 없는 실행 민감 이름 — 배정 전에 닫는다(S2A-2 단위2).
# 이 이름들을 역할에 쓰면 자식의 실행 환경(PATH·임시 폴더·셸 시작 파일) 자체를
# 키 값으로 바꿔버린다.
EXECUTION_SENSITIVE_ENV_NAMES = frozenset(
    {"PATH", "HOME", "SHELL", "TMPDIR", "TMP", "TEMP", "ENV", "BASH_ENV", "ZDOTDIR"}
)
# 로더·운반자·도구가 이미 쓰는 접두사 — 우연의 겹침도 허용하지 않는다.
PROTECTED_ENV_PREFIXES = ("PYTHON", "DYLD_", "LD_", "VLOGSRT_")
# STT 계약이 이미 쓰는 이름 — 비전 역할이 물려받으면 한 키가 양쪽을 다 연다.
STT_ROLE_ENV_NAMES = frozenset({"OPENAI_API_KEY", "OPENAI_BASE_URL"})
# Claude 의미 분석 키의 이름(D54) — 값은 `.env`에서 실행 시각에만 읽힌다. 비전 역할이 이
# 이름을 물려받으면 한 키가 두 공급자를 연다.
ANALYSIS_CLAUDE_KEY_ENV = "ANTHROPIC_API_KEY"

# 적재에 성공한 마지막 역할 이름 — 설정 해석이 실패해도 가림이 두 값을 알게
# 하는 근원이다(아래 redact_secrets 참조).
_applied_vision_key_name: str | None = None


def validate_role_env_names(vision_key_name: str, vision_url_name: str) -> None:
    """앱 관리 역할 이름의 모호함을 **배정 전에** 닫는다(S2A-2 단위2).

    정당한 사용자 지정 이름은 살린다 — 이 검사는 직접 CLI 설정을 바꾸지 않고,
    운반자가 그 이름으로 값을 얹으려는 순간에만 돈다. 이름은 공개 계약이므로
    문면에 실을 수 있다(값은 아니다).
    """
    if vision_key_name == vision_url_name:
        raise RuntimeError(
            f"비전 키와 base URL의 환경변수 이름이 같다: {vision_key_name!r} — "
            "한 이름에 두 값이 얹히면 한쪽이 조용히 사라진다"
        )
    for label, name in (("키", vision_key_name), ("base URL", vision_url_name)):
        if name in STT_ROLE_ENV_NAMES:
            raise RuntimeError(
                f"비전 {label} 이름이 STT 자격증명 이름을 재사용한다: {name!r} — "
                "비전 공급자는 별도 환경변수여야 한다"
            )
        if name == ANALYSIS_CLAUDE_KEY_ENV:
            raise RuntimeError(
                f"비전 {label} 이름이 Claude 분석 키 이름을 재사용한다: {name!r} — "
                "비전 공급자는 별도 환경변수여야 한다"
            )
        if name in EXECUTION_SENSITIVE_ENV_NAMES:
            raise RuntimeError(
                f"비전 {label} 이름이 실행 민감 환경변수다: {name!r} — 역할 이름으로 쓸 수 없다"
            )
        if name.startswith(PROTECTED_ENV_PREFIXES):
            raise RuntimeError(
                f"비전 {label} 이름이 보호 접두사로 시작한다: {name!r} — "
                f"{PROTECTED_ENV_PREFIXES}로 시작하는 이름은 역할 이름으로 쓸 수 없다"
            )


def apply_role_credentials(
    stt_key: str,
    vision_key: str,
    vision_base_url: str,
    *,
    vision_key_name: str = VISION_ROLE_DEFAULT_KEY_ENV,
    vision_base_url_name: str = VISION_ROLE_DEFAULT_URL_ENV,
) -> None:
    """V2 운반자의 세 값을 제 이름에 **권위적으로** 얹는다(S2A-2 단위2).

    `os.environ[name] = value`는 setdefault가 아니다 — 이미 상속·dotenv로 있던
    낡은 값을 대체하고, 빈 값은 '없음'으로 그 자리를 막아 dotenv의 setdefault가
    다시 채우는 길을 끊는다(V1의 명시적 부재 계약과 같은 원리). 이름 검사가
    실패하면 **어떤 배정도 일어나지 않는다** — 부분 적용은 부분 병합이다.

    역할 이름은 **이미 풀린 값**으로 받는다(기본은 코드 상수). 이 모듈은
    하위층이라 프로젝트 설정을 수입하지 못한다 — 설정에서 이름을 푸는 일은
    엔진/작업 경계(`vlogsrt-engine.py`의 `_resolve_vision_role_names`)가 한다:
    운반자 적용과 작업 실행이 다른 이름을 보면 안 되므로 같은 뿌리에서 풀어
    넘긴다.
    """
    global _applied_vision_key_name
    validate_role_env_names(vision_key_name, vision_base_url_name)
    # URL 검증은 **어떤 배정보다 앞선다** — 잘못된 엔드포인트가 환경에 얹히면
    # 그 뒤 모든 소비자가 그 값을 물려받는다(S2A-2 단위2). 제출된 두 키 값도
    # 이 자리의 키 재료다.
    validate_vision_base_url(vision_base_url, secrets=(stt_key, vision_key))
    os.environ[STT_ROLE_KEY_ENV] = stt_key
    os.environ[vision_key_name] = vision_key
    os.environ[vision_base_url_name] = vision_base_url
    _applied_vision_key_name = vision_key_name


def _managed_secret_values() -> tuple[str, ...]:
    """가림이 알아야 하는 관리 값 — 엔진 STT 키, 적재된(또는 기본) 비전 키, Claude 분석 키."""
    vision_name = _applied_vision_key_name or VISION_ROLE_DEFAULT_KEY_ENV
    names = (STT_ROLE_KEY_ENV, vision_name, ANALYSIS_CLAUDE_KEY_ENV)
    return tuple(value for value in (os.environ.get(name) for name in names) if value)


def role_credentials_applied() -> bool:
    """앱 관리(V2) 운반이 이 프로세스에 적용됐는가 — 새 URL 정책의 적용 범위다.

    직접 CLI·V1·마커 없는 호출은 이 값이 거짓이다 — 그 계약의 엔드포인트
    동작(예: 사용자가 명시한 http 호환 엔드포인트)은 그대로 둔다.
    """
    return _applied_vision_key_name is not None


# 문면에서 자격 증명 값을 대체하는 표식 — 값이 어느 키였는지 말하지 않는다.
REDACTED_MARK = "∙∙∙"


def redact_secrets(text: str, secrets: tuple[str, ...] = ()) -> str:
    """자격 증명 값을 문면에서 지운다 — 지속·인쇄 표면 앞의 마지막 문이다(S2A-2).

    기본은 관리 값 셋(엔진 STT 키·비전 키·Claude 분석 키)의 현재 값이고, 다른 값은
    `secrets`로 넣는다. **이름을 대체하지 않는다** — 값만 지운다. 값은 절대
    만들어 내지 않는다: 문면에 없으면 문장은 그대로 둬서 진단을 죽이지
    않는다. 빈 값은 아무것도 지우지 않는다. 후보는 값 그대로 **와** 소비자가
    실제로 쓰는 다듬은 값(strip) 둘 다다 — SDK는 공백을 떼고 부르므로 붙여
    넣은 흔적이 남은 키도 문면에서 지워진다. 겹치는 값은 **긴 쪽부터** 지운다
    — 짧은 키를 먼저 지우면 긴 키의 나머지가 문면에 드러난다.
    """
    candidates = (*secrets, *_managed_secret_values())
    effective = dict.fromkeys(
        piece for value in candidates if value for piece in (value, value.strip()) if piece
    )
    for value in sorted(effective, key=len, reverse=True):
        text = text.replace(value, REDACTED_MARK)
    return text


def validate_vision_base_url(url: str, *, secrets: tuple[str, ...] = ()) -> None:
    """비전 base URL의 검증 — 역할 배정과 공장 생성 **앞의** 관문이다(S2A-2 단위2).

    규칙: https여야 하고 호스트가 있어야 하며, 포트는 0~65535의 숫자여야 하고,
    userinfo·쿼리·프래그먼트(빈 구분자 `?`·`#` 포함)·제어 문자·NUL·공백(퍼센트
    부호형 포함)·상대 경로·해석 불가 모양은 닫는다. 알려진 키 재료(원문·
    퍼센트해독형·퍼센트부호화형)가 URL에 들어 있으면 닫는다. 오류 문면은 정해진
    교정 글이고 **URL을 절대 되뇌지 않는다**.
    """
    import unicodedata
    from urllib.parse import quote, unquote, urlsplit

    text = url
    if not text:
        return  # 빈 값은 부재다 — 호출자가 '없음' 오류를 낸다(별도 계약).

    def _has_forbidden_character(value: str) -> bool:
        return any(
            character.isspace() or unicodedata.category(character) in {"Cc", "Cf"}
            for character in value
        )

    # 퍼센트 부호형(%20·%00 등)도 해독한 문면에서 잰다 — 부호화가 검사를 흉내
    # 내는 통로가 되지 않게.
    decoded_text = unquote(text)
    if _has_forbidden_character(text) or _has_forbidden_character(decoded_text):
        raise RuntimeError(
            "비전 base URL에 공백·제어 문자가 들어 있다 — 엔드포인트를 다시 확인하세요"
        )
    # 구분자가 **있으면** 닫는다 — `?`/`#` 뒤가 비어도 truthiness는 못 잡는다.
    if "?" in text or "#" in text:
        raise RuntimeError("비전 base URL에 쿼리·프래그먼트는 들어갈 수 없다")
    try:
        parts = urlsplit(text)
        hostname = parts.hostname
        username = parts.username
        password = parts.password
        port = parts.port  # 숫자 아님·범위 밖(0~65535)은 ValueError로 나온다.
    except ValueError as error:
        # 못 여는 대괄호 등 해석기 자체가 거부하는 모양이다.
        raise RuntimeError(
            "비전 base URL이 해석되지 않는다 — 엔드포인트를 다시 확인하세요"
        ) from error
    if parts.scheme.lower() != "https":
        raise RuntimeError(
            "비전 base URL은 https:// 로 시작해야 한다 — http·상대 경로는 쓸 수 없다"
        )
    if not parts.netloc or not hostname:
        raise RuntimeError("비전 base URL에 호스트가 없다 — 엔드포인트를 다시 확인하세요")
    if username is not None or password is not None:
        raise RuntimeError(
            "비전 base URL에 사용자 정보(userinfo)가 들어 있다 — 자격증명은 URL로 보내지 않는다"
        )
    if port is not None and not 0 <= port <= 65535:
        raise RuntimeError("비전 base URL의 포트가 범위를 넘는다 — 엔드포인트를 다시 확인하세요")
    managed = (*secrets, *_managed_secret_values())
    materials = dict.fromkeys(
        piece for value in managed if value for piece in (value, value.strip()) if piece
    )
    for material in sorted(materials, key=len, reverse=True):
        # 다듬은 값도 재료다 — 공백을 붙여 넣은 키가 URL 검사를 흉내 내게 두지 않는다.
        if material in text or material in decoded_text or quote(material, safe="") in text:
            raise RuntimeError(
                "비전 base URL에 자격증명 값이 들어 있다 — 엔드포인트에서 키를 빼세요"
            )
