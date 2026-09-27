"""`.env` 로더. 실제 `.env`는 읽지 않는다 — 전부 임시 파일이다."""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest

from tests._dotenv_guard import (
    CANARY_KEY,
    CANARY_VALUE,
    GuardedEnvAccess,
    allow_dotenv_under,
    clear_violations,
    guard_installed,
    protected_spelling,
    stage_canary_standin,
    stage_synthetic_dotenv,
    violations,
)
from vlogsrt.env import (
    PACKAGE_ROOT,
    api_key_source,
    find_env_file,
    has_api_key,
    is_offline,
    load_env,
    parse_env_text,
    require_api_key,
    reset_cache,
    resolve_under_root,
)


def test_parses_simple_pairs() -> None:
    assert parse_env_text("A=1\nB=two\n") == {"A": "1", "B": "two"}


def test_ignores_comments_and_blanks() -> None:
    assert parse_env_text("# 주석\n\nA=1\n") == {"A": "1"}


def test_strips_surrounding_quotes() -> None:
    assert parse_env_text("A='1'\nB=\"two\"\n") == {"A": "1", "B": "two"}


def test_keeps_equals_inside_value() -> None:
    assert parse_env_text("URL=https://x/?a=b\n") == {"URL": "https://x/?a=b"}


def test_missing_file_is_quiet(tmp_path: Path) -> None:
    assert load_env(tmp_path / "absent") == {}


def test_shell_wins_over_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    allow_dotenv_under(tmp_path)  # 합성 fixture 뿌리만이 읽기를 허락받는다
    path = tmp_path / ".env"
    path.write_text("VLOGSRT_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("VLOGSRT_TEST_KEY", "from_shell")
    load_env(path)
    import os

    assert os.environ["VLOGSRT_TEST_KEY"] == "from_shell"


def test_offline_is_the_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLOGSRT_OFFLINE", raising=False)
    assert is_offline() is True


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_truthy_values(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", value)
    assert is_offline() is True


def test_explicit_zero_turns_it_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    assert is_offline() is False


def test_api_key_refused_while_offline(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    with pytest.raises(RuntimeError, match="오프라인"):
        require_api_key()


def test_force_lets_the_human_pay_on_purpose(monkeypatch: pytest.MonkeyPatch) -> None:
    """`record`와 `--online`은 VLOGSRT_OFFLINE 값과 무관하게 동작해야 한다."""
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    assert require_api_key(force=True) == "sk-test"


def test_force_still_requires_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "1")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_api_key(force=True)


def test_api_key_missing_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_OFFLINE", "0")
    monkeypatch.setenv("OPENAI_API_KEY", "")
    with pytest.raises(RuntimeError, match="OPENAI_API_KEY"):
        require_api_key()


# ─────────────────────────────────────────────────────────────
#  실행 위치에 흔들리지 않기
# ─────────────────────────────────────────────────────────────


def test_env_is_found_from_a_subdirectory(tmp_path: Path) -> None:
    """저장소 안 어디서 실행하든 `.env`를 찾아야 한다."""
    allow_dotenv_under(tmp_path)  # 조상 탐색이 sandbox 밖으로 새는 것만 막는다
    (tmp_path / ".env").write_text("VLOGSRT_TEST_MARK=1\n", encoding="utf-8")
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_env_file(deep) == tmp_path / ".env"


def test_env_lookup_gives_up_outside_any_project(tmp_path: Path) -> None:
    allow_dotenv_under(tmp_path)  # tmp 안은 실제 답, 그 위 조상은 보호가 숨긴다
    found = find_env_file(tmp_path)
    # 저장소 밖이면 None이다 — 진짜 저장소 fallback은 아래 합성 시험이 맡는다.
    assert found is None


def test_env_lookup_falls_back_to_the_package_root_dotenv(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """조상 탐색이 빈 경우의 fallback은 PACKAGE_ROOT다 — 합성 뿌리로 실제 계약을 지킨다."""
    from vlogsrt import env as env_module

    synthetic_root = tmp_path / "installed-package"
    synthetic_root.mkdir()
    dotenv = stage_synthetic_dotenv(synthetic_root)
    monkeypatch.setattr(env_module, "PACKAGE_ROOT", synthetic_root)
    assert find_env_file(tmp_path) == dotenv  # 없던 fallback이 합성 .env로 떨어진다


def test_relative_config_paths_resolve_under_the_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`VLOGSRT_FIXTURE_DIR=tests/fixtures/api` 가 실행 위치를 따라다니면 안 된다."""
    allow_dotenv_under(tmp_path)  # 합성 fixture 뿌리 — 루트 계산도 이 안에서 끝난다
    (tmp_path / ".env").write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    reset_cache()
    # 절대 경로는 뿌리가 있어야 한다 — POSIX는 `/`, 윈도우는 `C:\` 처럼 드라이브까지다.
    absolute = Path(tmp_path.anchor, "absolute", "stays")
    try:
        assert resolve_under_root("tests/fixtures/api") == tmp_path / "tests/fixtures/api"
        assert resolve_under_root(str(absolute)) == absolute
        # 드라이브 없는 `/…`는 윈도우에서 현재 드라이브를 타므로 그대로 돌려주면 안 된다.
        # 무엇이 들어오든 나가는 것은 실행 위치에 흔들리지 않는 경로여야 한다.
        assert resolve_under_root("/absolute/stays").is_absolute()
    finally:
        reset_cache()


def test_key_state_helpers_never_expose_the_value(monkeypatch: pytest.MonkeyPatch) -> None:
    secret = "sk-proj-SECRET-VALUE"
    monkeypatch.setenv("OPENAI_API_KEY", secret)
    assert has_api_key() is True
    assert secret not in api_key_source()

    monkeypatch.setenv("OPENAI_API_KEY", "  ")
    assert has_api_key() is False
    assert api_key_source() == "없음"


def test_cost_guard_defaults_to_two_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("VLOGSRT_COST_GUARD_SECONDS", raising=False)
    from vlogsrt.env import cost_guard_seconds

    assert cost_guard_seconds() == 7200.0


# ─────────────────────────────────────────────────────────────
#  STAB-TEST-ISOLATION — 테스트 전용 dotenv 보호 경계
# ─────────────────────────────────────────────────────────────


def test_guard_is_installed_for_collection_and_fixtures() -> None:
    """conftest 수입 시각에 보호가 붙어 있다 — 이 절을 떼면 아래 전부가 실패한다."""
    assert guard_installed() is True


def test_unregistered_dotenv_is_hidden_and_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """등록되지 않은 보호 철자는 탐색이 빠진 것으로 숨고 읽기는 막힌다.

    보호가 없다면(RED) 로더가 stand-in을 찾아내 카나리가 환경에 들어온다 —
    이 시험은 그 부정을 증명한다. 카나리 값은 어떤 출력에도 없다.
    """
    monkeypatch.delenv(CANARY_KEY, raising=False)
    sealed = tmp_path / "sealed"
    standin = stage_canary_standin(sealed)
    assert protected_spelling(standin) is True
    assert find_env_file(sealed) is None  # 보호가 없으면 stand-in 경로가 돌아온다
    assert load_env(standin) == {}
    assert CANARY_KEY not in os.environ  # 보호가 없으면 카나리가 들어온다
    with pytest.raises(GuardedEnvAccess), open(standin, encoding="utf-8"):
        pass
    # io.open 직접 참조도 내장 open과 같은 경계다 — 고의적인 우회 경로 시험이다.
    with pytest.raises(GuardedEnvAccess), io.open(standin, encoding="utf-8"):  # noqa: UP020
        pass
    with pytest.raises(GuardedEnvAccess):
        standin.read_text(encoding="utf-8")
    with pytest.raises(GuardedEnvAccess):
        standin.read_bytes()


def test_guard_covers_relative_and_absolute_spellings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """상대·절대 표기가 같은 철자 판정을 받는다 — 우회 경로가 아니라 같은 경계다."""
    monkeypatch.chdir(tmp_path)
    standin = stage_canary_standin(tmp_path / "sealed")
    for spelling in (standin, str(standin), "sealed/.env", Path("sealed") / ".env"):
        assert protected_spelling(spelling) is True
        assert Path(spelling).is_file() is False
        with pytest.raises(GuardedEnvAccess):
            Path(spelling).read_text(encoding="utf-8")
    with pytest.raises(GuardedEnvAccess), open("sealed/.env", encoding="utf-8"):
        pass


def test_guard_covers_env_example_and_probe_apis(tmp_path: Path) -> None:
    """`.env.example`과 탐색·stat도 같은 경계 안에 있다.

    stat은 '파일이 없다'로 숨는다(FileNotFoundError) — pathlib 내부가 이 예외를
    잡아 False로 돌리므로 수집 도구가 깨지지 않는다. 존재 신탁도 함께 닫힌다.
    """
    example = stage_canary_standin(tmp_path / "sealed", name=".env.example")
    assert protected_spelling(example) is True
    with pytest.raises(FileNotFoundError):
        example.stat()
    with pytest.raises(FileNotFoundError):
        os.stat(example)
    with pytest.raises(FileNotFoundError):
        os.lstat(example)
    with pytest.raises(GuardedEnvAccess):
        os.open(example, os.O_RDONLY)
    assert example.is_file() is False
    assert os.path.exists(example) is False
    assert os.path.isfile(example) is False
    assert os.path.lexists(example) is False


def test_guard_covers_byte_and_pathlike_spellings(tmp_path: Path) -> None:
    """bytes·PathLike 표기도 같은 철자 판정을 받는다 — 경로 인코딩 우회가 아니다."""
    standin = stage_canary_standin(tmp_path / "sealed")
    raw = os.fsencode(standin)

    class Wrapper:
        """__fspath__만 제공하는 우회 표기 — Path가 아니어도 같은 경계다."""

        def __fspath__(self) -> bytes:
            return raw

    assert protected_spelling(raw) is True
    assert protected_spelling(Wrapper()) is True
    assert os.path.exists(raw) is False
    with pytest.raises(GuardedEnvAccess), open(raw, "rb"):
        pass


def test_guard_classifies_without_filesystem_access(tmp_path: Path) -> None:
    """판정은 존재와 무관하다 — 없는 철자도 보호다(존재 확인이 판정 전제가 아니다)."""
    absent = tmp_path / "nowhere" / ".env"
    absent_example = tmp_path / "nowhere" / ".env.example"
    assert protected_spelling(absent) is True
    assert protected_spelling(absent_example) is True
    assert protected_spelling(tmp_path / "dotenv") is False
    assert protected_spelling(tmp_path / ".envrc") is False  # 로더가 읽지 않는 철자다
    assert absent.is_file() is False


def test_registered_synthetic_fixture_keeps_real_loader_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """등록된 합성 fixture 안에서는 실제 로더의 탐색·파싱이 그대로 돈다."""
    dotenv = stage_synthetic_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    reset_cache()
    try:
        assert find_env_file() == dotenv.resolve()  # cwd 조상 탐색이 여기서 멈춘다
        assert load_env(dotenv) == {"VLOGSRT_OFFLINE": "1"}
    finally:
        reset_cache()


def test_real_repository_dotenv_is_out_of_reach_in_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """실행 위치가 어디든 진짜 저장소 `.env`(PACKAGE_ROOT fallback 포함)는 닿지 않는다."""
    monkeypatch.chdir(tmp_path)
    assert find_env_file() is None  # 보호가 없으면 실제 저장소 철자가 드러날 수 있다(RED)
    package_env = PACKAGE_ROOT / ".env"
    assert package_env.is_file() is False
    assert package_env.exists() is False


def test_guard_survives_monkeypatch_cycles(tmp_path: Path) -> None:
    """보호는 conftest가 직접 붙였다 — monkeypatch 왕복에 풀리지 않는다."""
    standin = stage_canary_standin(tmp_path / "sealed")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("VLOGSRT_UNRELATED", "1")
    with pytest.raises(GuardedEnvAccess):
        standin.read_text(encoding="utf-8")


def test_blocked_attempts_are_recorded_without_content(tmp_path: Path) -> None:
    """차단 시도는 종류와 경로 철자만 남긴다 — 내용·카나리는 기록에 없다."""
    clear_violations()
    standin = stage_canary_standin(tmp_path / "sealed")
    assert standin.is_file() is False
    recorded = " ".join(spelling for _, spelling in violations())
    assert str(standin) in recorded
    assert CANARY_KEY not in recorded
    assert CANARY_VALUE not in recorded


# ─────────────────────────────────────────────────────────────
#  역할 자격 증명 (S2A-2 단위2) — 이름 검사·권위적 배정·가림
# ─────────────────────────────────────────────────────────────


def test_legal_vision_role_names_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본 이름과 정당한 사용자 지정 이름은 통과한다 — 직접 CLI 계약은 그대로다."""
    from vlogsrt.env import validate_role_env_names

    validate_role_env_names("GOOGLE_API_KEY", "GEMINI_BASE_URL")
    validate_role_env_names("MY_VISION_KEY", "MY_VISION_URL")


@pytest.mark.parametrize(
    ("key_name", "url_name"),
    [
        ("SAME_NAME", "SAME_NAME"),  # 키와 URL 이름이 같다
        ("OPENAI_API_KEY", "MY_URL"),  # STT 키 이름 재사용
        ("MY_KEY", "OPENAI_API_KEY"),
        ("OPENAI_BASE_URL", "MY_URL"),  # STT base URL 이름 재사용
        ("MY_KEY", "OPENAI_BASE_URL"),
        ("PATH", "MY_URL"),  # 실행 민감 이름
        ("MY_KEY", "HOME"),
        ("MY_KEY", "BASH_ENV"),
        ("MY_KEY", "ZDOTDIR"),
        ("PYTHONPATH", "MY_URL"),  # 보호 접두사
        ("MY_KEY", "DYLD_INSERT_LIBRARIES"),
        ("MY_KEY", "LD_LIBRARY_PATH"),
        ("VLOGSRT_OFFLINE", "MY_URL"),
    ],
)
def test_ambiguous_vision_role_names_are_rejected(key_name: str, url_name: str) -> None:
    """모호한 역할 이름은 닫는다 — 배정 순간에만 검사한다(설정 해석이 아니다)."""
    from vlogsrt.env import validate_role_env_names

    with pytest.raises(RuntimeError):
        validate_role_env_names(key_name, url_name)


def test_apply_role_credentials_is_authoritative(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """세 값은 setdefault가 아니라 배정이다 — 상속 낡은 값은 대체된다."""
    from vlogsrt.env import apply_role_credentials

    monkeypatch.chdir(tmp_path)  # 프로젝트 설정 없음 — 코드 기본 이름
    monkeypatch.setenv("OPENAI_API_KEY", "stale-stt")
    monkeypatch.setenv("GOOGLE_API_KEY", "stale-vision")
    monkeypatch.setenv("GEMINI_BASE_URL", "https://stale.example/v1")

    apply_role_credentials("synthetic-stt", "synthetic-vision", "https://vision.example/v1")

    assert os.environ["OPENAI_API_KEY"] == "synthetic-stt"
    assert os.environ["GOOGLE_API_KEY"] == "synthetic-vision"
    assert os.environ["GEMINI_BASE_URL"] == "https://vision.example/v1"


def test_apply_role_credentials_empty_values_block_dotenv_revival(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """빈 세 값은 권위적 부재다 — dotenv의 setdefault가 낡은 키를 되살리지 못한다."""
    from vlogsrt.env import apply_role_credentials, ensure_loaded, reset_cache

    allow_dotenv_under(tmp_path)
    (tmp_path / ".env").write_text(
        "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    reset_cache()
    apply_role_credentials("", "", "")

    assert os.environ["OPENAI_API_KEY"] == ""  # 지운 것이 아니라 빈 채로 막는다
    ensure_loaded()
    assert os.environ["OPENAI_API_KEY"] == ""
    assert os.environ["GOOGLE_API_KEY"] == ""
    assert os.environ["GEMINI_BASE_URL"] == ""
    reset_cache()


def test_apply_role_credentials_rejects_illegal_resolved_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """이름 검사는 배정 순간에 일어난다 — 이미 풀린 이름이 모호하면 닫는다.

    프로젝트 설정 해석 자체의 실패 주장(깨진 설정 = 닫힘)은 이름을 푸는 경계,
    엔진 진입점 시험이 담당한다 — 하위층 적용은 풀린 이름만 받는다.
    """
    from vlogsrt.env import apply_role_credentials

    monkeypatch.setenv("OPENAI_API_KEY", "untouched")
    for key_name, url_name in [("OPENAI_API_KEY", "GEMINI_BASE_URL"), ("A", "A"), ("PATH", "X")]:
        with pytest.raises(RuntimeError):
            apply_role_credentials(
                "a", "b", "c", vision_key_name=key_name, vision_base_url_name=url_name
            )
    assert os.environ["OPENAI_API_KEY"] == "untouched"


def test_redact_secrets_knows_the_managed_vision_value_without_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """가림의 기본 후보는 관리 값 둘 — 설정 해석과 무관하게 비전 키를 안다."""
    from vlogsrt.env import redact_secrets

    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-engine-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")

    text = redact_secrets("engine=synthetic-engine-key vision=synthetic-vision-key")

    assert "synthetic-engine-key" not in text
    assert "synthetic-vision-key" not in text
    assert redact_secrets("ffprobe not found") == "ffprobe not found"
