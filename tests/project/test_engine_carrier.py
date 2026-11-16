"""엔진 자격 증명 운반의 회귀 시험 — 사적 운반자가 상속 키를 이긴다(STAB-S2A-2).

앱이 저장한 키는 자식에게 **운반자**로만 흘러간다: argv·파일·부모 환경 전역
뮤테이션은 없다. 이 모듈은 운반 계약의 세 가지 면을 못 박는다.

1. 운반자가 말하는 키는 이미 상속된 낡은 키를 **이긴다** — `setdefault`로
   덮으면 앱에서 키를 갈아끼워도 자식이 계속 낡은 키로 유료 호출을 한다.
2. "키 없음" 운반은 상속된 낡은 키를 **부활시키지 않는다** — 앱에 키가 없는데
   셸에서 물려받은 키로 유료 호출이 열리면 진실한 사전 검사가 거짓말이 된다.
3. 운반자가 없는 낡은 직접 호출(번들 3/4-인자 계약)은 상속 환경을 그대로
   둔다 — 레거시 계약을 바꾸지 않는다.

소비 증명도 여기서 한다: 운반된 키는 환경 읽기(`env.get`, dotenv 적재 포함)가 실제로
읽는 이름에 도달해야 한다 — 호출 객체 안에만 있는 키는 아무것도 증명하지 않는다.
whisper STT 공장·분석 문은 D163으로 지웠다 — 그 소비 증명도 함께 지웠다.
"""

from __future__ import annotations

import io
import os
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import ModuleType

import pytest

from tests._dotenv_guard import stage_synthetic_dotenv
from vlogsrt import env

# 개발 엔진(`macos/scripts/`)은 POSIX 전용이다 — `fcntl` 잠금 · zsh 설치 스크립트. Windows에는 그
# 엔진이 없다(작업대는 `vlogsrt/filelock.py`의 msvcrt 길을 쓴다, W-84a).
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 전용 개발 엔진(macos/scripts)")

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_CARRIER_ENV = "VLOGSRT_ENGINE_API_KEY"
_ROLE_CARRIER_ENV = "VLOGSRT_ENGINE_ROLE_CREDENTIALS"
_ENGINE_KEY_NAME = "OPENAI_API_KEY"
_VISION_KEY_ENV = "GOOGLE_API_KEY"
_VISION_URL_ENV = "GEMINI_BASE_URL"


def _carry_stt(module: ModuleType, payload: str) -> None:
    """V1 stdin 봉투 값 하나를 해석·적용까지 돌린다 — 낡은 계약의 짧은 길."""
    module._carry_credentials(module._resolve_carried_credentials(("1", payload)))


def _carry_env(module: ModuleType) -> None:
    """환경 운반자만으로 해석·적용까지 돌린다 — 인자는 시험이 미리 얹는다."""
    module._carry_credentials(module._resolve_carried_credentials(None))


def _load_engine() -> ModuleType:
    import importlib.util

    spec = importlib.util.spec_from_file_location("vlogsrt_engine_carrier", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """키 이름 둘을 비우고 dotenv도 합성물로 고정한다 — 상속 시뮬레이션이 새지 않게."""
    monkeypatch.delenv(_ENGINE_KEY_NAME, raising=False)
    monkeypatch.delenv(_CARRIER_ENV, raising=False)
    stage_synthetic_dotenv(tmp_path)
    yield tmp_path


def test_env_carrier_overrides_a_stale_inherited_key(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """환경 운반자의 키는 이미 있던 상속 키를 대체한다 — setdefault면 결함이다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setenv(_CARRIER_ENV, "synthetic-submitted-key")

    _carry_env(engine)

    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-submitted-key"
    assert _CARRIER_ENV not in os.environ  # 운반자 이름은 두 번 흐르지 않는다


def test_env_carrier_without_key_does_not_resurrect_a_stale_inherited_key(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """빈 운반자는 '키 없음'이다 — 상속된 낡은 키가 유료 호출을 열지 못한다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setenv(_CARRIER_ENV, "")

    _carry_env(engine)

    assert env.get(_ENGINE_KEY_NAME).strip() == ""  # 빈 값은 없음이다 — 부활이 아니다
    assert _CARRIER_ENV not in os.environ


def test_absent_carrier_keeps_the_inherited_environment(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """운반자가 없으면 상속 환경을 그대로 둔다 — 낡은 직접 호출 계약이다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "inherited-legacy-key")

    _carry_env(engine)

    assert os.environ[_ENGINE_KEY_NAME] == "inherited-legacy-key"


def test_guard_provenance_rejects_sibling_prefixes() -> None:
    """수입 출처 판정은 구성요소 경계로 잰다 — 형제 접두사는 안쪽이 아니다.

    startswith 판정은 `/x/vlogsrt-evil`을 `/x/vlogsrt` 안으로 받아들인다.
    사전확인(preflight)이 쓰는 경계 검사의 음성 회귀다.
    """
    from tests._dotenv_guard import _is_within

    assert _is_within("/tmp/x/vlogsrt/__init__.py", "/tmp/x/vlogsrt")
    assert _is_within("/tmp/x/vlogsrt", "/tmp/x/vlogsrt")
    assert not _is_within("/tmp/x/vlogsrt-evil/__init__.py", "/tmp/x/vlogsrt")
    assert not _is_within("/tmp/x/vlogsrt2/__init__.py", "/tmp/x/vlogsrt")
    assert not _is_within("/tmp/other/vlogsrt/__init__.py", "/tmp/x/vlogsrt")


@pytest.fixture()
def engine() -> ModuleType:
    return _load_engine()


# ─────────────────────────────────────────────────────────────
#  사적 stdin 봉투 — NSUserUnixTask 환경 운반자가 없는 개발 경로의 통로
#
#  봉투 읽기는 **진짜 fd 위에서** 마감을 두고 돈다 — 그래서 stdin 모양도 진짜
#  파이프로 만든다(BytesIO에는 fd가 없다). 조각 전달은 쓰기 스레드가 나눠
#  흘려본다.
# ─────────────────────────────────────────────────────────────

_OPEN_WRITE_ENDS: list[int] = []


@pytest.fixture(autouse=True)
def _close_held_pipe_ends() -> Iterator[None]:
    """hold-open 시험이 쥔 쓰기 끝을 시험 뒤에 닫는다 — fd 누수 방지."""
    yield
    while _OPEN_WRITE_ENDS:
        os.close(_OPEN_WRITE_ENDS.pop())


def _pipe_stdin(payload: bytes = b"", *, hold_open: bool = False) -> io.TextIOWrapper:
    """봉투 바이트가 흘러 있는 진짜 stdin — 파이프 읽기 끝을 감싼다."""
    read_fd, write_fd = os.pipe()
    if payload:
        os.write(write_fd, payload)
    if hold_open:
        _OPEN_WRITE_ENDS.append(write_fd)
    else:
        os.close(write_fd)
    return io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0), encoding="utf-8")


def _framed(payload: str) -> bytes:
    """봉투 바이트 — 마법줄·버전·바이트 길이·본문·끝표식."""
    return f"VLOGSRT-ENGINE-CARRIER 1\n{len(payload.encode('utf-8'))}\n{payload}\n".encode()


def test_stdin_envelope_carries_the_submitted_key(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """봉투의 키는 상속 키를 대체한다 — 개발 실행기의 사적 1회 통로다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed("synthetic-envelope-key")))

    version, payload = engine._read_carrier_envelope()

    assert version == "1"
    assert payload == "synthetic-envelope-key"
    _carry_stt(engine, payload)
    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-envelope-key"


def test_stdin_envelope_length_zero_is_an_explicit_no_key(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """길이 0 봉투는 '키 없음'이다 — 상속 키를 내려놓고 끝난다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed("")))

    version, payload = engine._read_carrier_envelope()

    assert version == "1"
    assert payload == ""
    _carry_stt(engine, payload)
    assert env.get(_ENGINE_KEY_NAME).strip() == ""


def test_stdin_envelope_framing_counts_bytes_not_characters(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """길이는 바이트 수다 — 한글 키가 문자 수로 세여 잘리거나 남지 않는다."""
    payload = "합성-열쇠-값"
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed(payload)))

    assert engine._read_carrier_envelope() == ("1", payload)


def test_stdin_envelope_accepts_a_maximal_payload(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """상한 안 최대 페이로드는 통과한다 — Swift 쪽 상한과 정확히 같은 값이다."""
    payload = "k" * engine._CARRIER_MAX_BYTES
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed(payload)))

    assert engine._read_carrier_envelope() == ("1", payload)


def test_stdin_envelope_survives_fragmented_delivery(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """머릿줄·본문이 조각으로 흘러와도 읽는다 — 여러 번 읽기 반복문의 모양.

    조각 **경계** 자체의 결정적 증명은 아래 파서 전계(sweep) 시험이 든다 —
    여기는 진짜 파이프에서 읽기가 여러 번으로 나뉘어도 끝까지 간다는 통합 모양.
    """
    payload = "synthetic-fragmented-key"
    frame = _framed(payload)
    half = len(frame) // 3
    read_fd, write_fd = os.pipe()
    writer = threading.Thread(
        target=lambda: (
            os.write(write_fd, frame[:half]),
            time.sleep(0.01),
            os.write(write_fd, frame[half : 2 * half]),
            time.sleep(0.01),
            os.write(write_fd, frame[2 * half :]),
            os.close(write_fd),
        )
    )
    writer.start()
    monkeypatch.setattr(
        "sys.stdin",
        io.TextIOWrapper(os.fdopen(read_fd, "rb", buffering=0), encoding="utf-8"),
    )

    assert engine._read_carrier_envelope() == ("1", payload)
    writer.join(timeout=5)


def test_envelope_parser_waits_at_every_fragment_boundary() -> None:
    """온전한 프레임의 모든 앞부분은 미완(None)이다 — 경계마다 결정적으로.

    쓰기 합쳐짐에 기대지 않는다: 파서를 전체 프레임의 **모든** 진짜 앞부분으로
    직접 부른다. 온전한 마법줄 + 미완 길이 줄이 예외가 아니라 미완이라는 것이
    회귀의 핵이다(옛 판독기는 마법줄 줄바꿈이 오자마자 오판으로 닫았다). 마지막
    바이트가 오기 전까지 그 어떤 앞부분도 페이로드가 되지 않는다.
    """
    engine = _load_engine()
    payload = "synthetic-fragmented-key"
    frame = _framed(payload)

    for size in range(1, len(frame)):
        assert engine._parse_carrier_envelope(frame[:size]) is None, size
    assert engine._parse_carrier_envelope(frame) == ("1", payload)


def test_stdin_envelope_deadline_expires_on_a_silent_held_open_pipe(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """말이 없는 열린 파이프 앞에서 영원히 기다리지 않는다 — 마감이 끝낸다.

    무제한 버퍼 읽기(EOF 기다림)의 회귀다: 쓰기 끝이 닫히지 않으면 옛 판독기는
    블록에 갇혔다. 마감을 짧게 줄여 실측한다.
    """
    monkeypatch.setattr(engine, "_CARRIER_READ_DEADLINE_SECONDS", 0.3)
    started = time.monotonic()
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._read_carrier_envelope(stream=_pipe_stdin(hold_open=True))
    assert time.monotonic() - started < 5.0


def test_complete_frame_still_requires_eof_within_the_deadline(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """끝표식이 온전해도 EOF가 와야 한다 — 열린 파이프를 조용히 받지 않는다.

    쓰는 쪽은 execute 앞에서 쓰기 끝을 닫는다(EOF는 정해져 있다). 끝표식 뒤
    마감 안에 EOF가 오지 않으면 실패로 닫는다 — 엄격한 EOF의 다른 면이다.
    """
    monkeypatch.setattr(engine, "_CARRIER_READ_DEADLINE_SECONDS", 0.3)
    started = time.monotonic()
    # 끝표식까지 온전하지만 쓰기 끝을 닫지 않는다(EOF 없음).
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._read_carrier_envelope(
            stream=_pipe_stdin(_framed("synthetic-eof-key"), hold_open=True)
        )
    assert time.monotonic() - started < 5.0


def test_malformed_envelopes_fail_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """깨진 봉투는 닫힌다 — 마법줄·길이·잔여 바이트·과대 어떤 모양도 키로 못 박는다."""
    magic = f"VLOGSRT-ENGINE-CARRIER {engine._CARRIER_VERSION}".encode()
    malformed = [
        b"",  # 아무것도 없다(빈 입력)
        b"NOT-THE-MAGIC 1\n3\nabc\n",  # 마법줄이 다르다
        b"VLOGSRT-ENGINE-CARRIER 3\n3\nabc\n",  # 버전을 모른다(2는 이제 아는 버전이다)
        b"VLOGSRT-ENGINE-CARRIER 1\nabc\nxxx\n",  # 길이가 숫자가 아니다
        b"VLOGSRT-ENGINE-CARRIER 1\n-1\n\n",  # 음수 길이
        b"VLOGSRT-ENGINE-CARRIER 1\n3\nab\n",  # 본문이 길이보다 짧다
        b"VLOGSRT-ENGINE-CARRIER 1\n3\nabcEXTRA\n",  # 끝표식 자리에 다른 글자
        b"VLOGSRT-ENGINE-CARRIER 1\n999999\n" + b"a" * 64,  # 길이 선언이 과대
        b"VLOGSRT-ENGINE-CARRIER 1\n",  # 마법줄만 있고 끝났다
        magic + b"\n" + b"9" * 60,  # 머릿줄 상한 안에 길이 줄이 오지 않는다
        b"VLOGSRT-ENGINE-CARRIER 1\n0\n\ntrailing-garbage",  # 끝표식 뒤 잔여 입력
    ]
    for raw in malformed:
        monkeypatch.setattr("sys.stdin", _pipe_stdin(raw))
        with pytest.raises(engine._CarrierEnvelopeError):
            engine._read_carrier_envelope()


def test_completed_overlong_length_header_fails_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """완결된 머릿줄도 상한 안에 있어야 한다 — 모두 0인 긴 길이 줄의 회귀.

    길이 줄이 60자리 0이면 값은 0(상한 안)이고 본문은 곧장 끝표식이라 옛
    판독기는 이를 '빈 키 없음' 봉투로 받아들였다 — 머릿줄 상한을 완결 뒤에도
    재지 않으면 빠져나가는 길이다.
    """
    frame = b"VLOGSRT-ENGINE-CARRIER 1\n" + b"0" * 60 + b"\n\n"
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))

    with pytest.raises(engine._CarrierEnvelopeError):
        engine._read_carrier_envelope()


def test_nul_bytes_in_the_payload_fail_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """본문의 NUL은 거절한다 — C 문자열 경계를 넘는 값은 키가 아니다."""
    payload = b"synthetic\x00nul"
    frame = f"VLOGSRT-ENGINE-CARRIER 1\n{len(payload)}\n".encode() + payload + b"\n"
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))

    with pytest.raises(engine._CarrierEnvelopeError):
        engine._read_carrier_envelope()


def test_oversized_envelope_fails_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """상한을 넘는 봉투는 읽다가 닫는다 — 실제 크기로도 증명한다(선언만이 아니다)."""
    limit = engine._CARRIER_MAX_BYTES
    declared = f"VLOGSRT-ENGINE-CARRIER 1\n{limit + 1}\n".encode() + b"a" * 32
    monkeypatch.setattr("sys.stdin", _pipe_stdin(declared))
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._read_carrier_envelope()

    actual = f"VLOGSRT-ENGINE-CARRIER 1\n{limit}\n".encode() + b"k" * (limit + 1)
    monkeypatch.setattr("sys.stdin", _pipe_stdin(actual))
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._read_carrier_envelope()


def test_concurrent_envelope_reads_do_not_mix() -> None:
    """동시에 열린 두 봉투는 서로의 값을 모른다 — 호출마다 제 파이프가 있다."""
    engine = _load_engine()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(
            engine._read_carrier_envelope, _pipe_stdin(_framed("synthetic-first-key"))
        )
        second = pool.submit(
            engine._read_carrier_envelope, _pipe_stdin(_framed("synthetic-second-key"))
        )
        assert first.result(timeout=10) == ("1", "synthetic-first-key")
        assert second.result(timeout=10) == ("1", "synthetic-second-key")


def test_envelope_key_replaces_a_stale_env_and_dotenv_value(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """봉투의 키는 상속 환경 **와** dotenv의 낡은 값을 둘 다 이긴다.

    dotenv의 적재는 setdefault다 — 봉투가 값을 먼저 얹으면 낡은 dotenv 값은
    다시 살아날 자리가 없다. 소비 시점(`env.get`)의 값이 곧 봉투의 값이다.
    """
    from tests._dotenv_guard import allow_dotenv_under

    allow_dotenv_under(tmp_path)
    (tmp_path / ".env").write_text(
        "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-key\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    env.reset_cache()

    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed("synthetic-envelope-wins")))
    _, payload = engine._read_carrier_envelope()
    _carry_stt(engine, payload)

    assert env.get(_ENGINE_KEY_NAME) == "synthetic-envelope-wins"


def test_empty_envelope_suppresses_stale_env_and_dotenv_values(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """빈 봉투는 상속·dotenv의 낡은 키를 둘 다 누른다 — 부활이 없다.

    빈 값을 **지우지 않고 빈 채로** 두는 것이 요점이다: 키가 '있음(빈)'이면
    dotenv의 setdefault가 그 자리를 다시 채우지 못한다.
    """
    from tests._dotenv_guard import allow_dotenv_under

    allow_dotenv_under(tmp_path)
    (tmp_path / ".env").write_text(
        "VLOGSRT_OFFLINE=1\nOPENAI_API_KEY=stale-dotenv-key\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    env.reset_cache()

    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed("")))
    _, payload = engine._read_carrier_envelope()
    _carry_stt(engine, payload)

    assert env.get(_ENGINE_KEY_NAME) == ""


def test_envelope_failures_never_echo_the_payload(
    monkeypatch: pytest.MonkeyPatch,
    engine: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """봉투 거절 문면은 페이로드를 절대 되뇌지 않는다 — 모양·길이만 말한다."""
    job_dir = tmp_path / "carrier-job"
    job_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    frame = b"VLOGSRT-ENGINE-CARRIER 1\n9\nabcEXTRA"
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))
    assert engine.main(["roughcut", "job-1", str(job_dir), "--stdin-carrier"]) == 2
    captured = capsys.readouterr()
    assert "abcEXTRA" not in captured.out + captured.err
    assert "abc" not in captured.out + captured.err


# ─────────────────────────────────────────────────────────────
#  실패 문면의 가림 — SDK 오류가 키를 되뇌어도 기록·stderr에 실리지 않는다
# ─────────────────────────────────────────────────────────────


def test_project_failure_text_redacts_engine_and_vision_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """분석 실패 문면은 엔진 키와 설정이 고른 비전 키 둘 다 지운다.

    비전 키 이름은 프로젝트 설정의 것이다 — 엔진 키를 비전에 얹는 별칭이
    아니라, 각자의 값을 각자 지우는 것이다(P5-00 계약 보존).
    """
    import argparse as argparse_module

    from vlogsrt.project import commands as project_commands

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "synthetic-engine-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "synthetic-vision-key")
    args = argparse_module.Namespace(project_config=None)

    text = project_commands._redact_failure_text(
        args, "boom engine=synthetic-engine-key vision=synthetic-vision-key"
    )

    assert "synthetic-engine-key" not in text
    assert "synthetic-vision-key" not in text
    # 비밀이 없는 문면은 그대로 — 지움이 진단을 죽이지 않는다.
    assert project_commands._redact_failure_text(args, "ffprobe not found") == "ffprobe not found"


def _fake_supported_topology(monkeypatch: pytest.MonkeyPatch) -> None:
    """실제 진입점을 지나는 시험의 새 전제 — 합성 미디어는 1오디오 스트림이다."""
    from vlogsrt.project import topology

    monkeypatch.setattr(topology, "probe_audio_stream_count", lambda source: 1)


def _stage_valid_grant(job_dir: Path) -> None:
    """V2 적용 실행에 필요한 유효한 권한 문서 — 루트와 지문이 묶여 있다."""
    import json as _json

    from vlogsrt.project import commands as project_commands

    roots = [str(job_dir.parent)]
    (job_dir / "media-grant.json").write_text(
        _json.dumps(
            {
                "grant_revision": project_commands._swift_grant_revision(roots),
                "roots": roots,
            }
        ),
        encoding="utf-8",
    )


# ─────────────────────────────────────────────────────────────
#  V2 역할 운반자 (S2A-2 단위2) — 세 필드 JSON, 역할마다 독립 부재
# ─────────────────────────────────────────────────────────────


def _framed_roles(roles: dict[str, str]) -> bytes:
    """V2 봉투 바이트 — 마법줄·버전 2·바이트 길이·JSON 본문·끝표식."""
    import json

    payload = json.dumps(roles, sort_keys=True, separators=(",", ":"))
    return f"VLOGSRT-ENGINE-CARRIER 2\n{len(payload.encode('utf-8'))}\n{payload}\n".encode()


@pytest.fixture(autouse=True)
def _reset_applied_role_names() -> Iterator[None]:
    """역할 이름 기억은 시험 사이에 지운다 — 가림 기본값이 새어 넘어가지 않게."""
    env._applied_vision_key_name = None
    yield
    env._applied_vision_key_name = None


def _read_roles(
    engine: ModuleType, monkeypatch: pytest.MonkeyPatch, frame: bytes
) -> dict[str, str]:
    """V2 stdin 봉투를 읽어 해석·적용까지 돌린다 — 적용된 세 값을 돌려준다."""
    import json

    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))
    version, payload = engine._read_carrier_envelope()
    assert version == "2"
    carried = engine._resolve_carried_credentials((version, payload))
    assert carried.roles is not None
    engine._carry_credentials(carried)
    return json.loads(payload)


def test_role_envelope_applies_all_three_roles_over_stale_values(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """V2의 세 값은 제 이름에 권위적으로 얹힌다 — 상속 낡은 값은 대체된다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-stt")
    monkeypatch.setenv(_VISION_KEY_ENV, "stale-vision")
    monkeypatch.setenv(_VISION_URL_ENV, "https://stale.example/v1")

    applied = _read_roles(
        engine,
        monkeypatch,
        _framed_roles(
            {
                "stt_key": "synthetic-stt-key",
                "vision_key": "synthetic-vision-key",
                "vision_base_url": "https://vision.example/v1",
            }
        ),
    )

    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-stt-key"
    assert os.environ[_VISION_KEY_ENV] == "synthetic-vision-key"
    assert os.environ[_VISION_URL_ENV] == "https://vision.example/v1"
    # 프레임은 세 필드를 통째로 실었다
    assert applied["stt_key"] == "synthetic-stt-key"
    # 운반자 이름은 소비 뒤 지워진다 — 값이 두 이름으로 흐르지 않는다.
    assert _CARRIER_ENV not in os.environ
    assert _ROLE_CARRIER_ENV not in os.environ


def test_role_envelope_empty_fields_are_authoritative_absence_per_role(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """역할마다 독립 부재 — 한 역할이 비어도 다른 역할의 값은 그대로 간다.

    빈 값을 지우지 않고 빈 채로 두므로 dotenv의 setdefault가 낡은 비전 키를
    되살릴 길도 막힌다(명시적 부재 계약의 세 역할 확장).
    """
    from tests._dotenv_guard import allow_dotenv_under

    allow_dotenv_under(tmp_path)
    (tmp_path / ".env").write_text(
        "VLOGSRT_OFFLINE=1\nGOOGLE_API_KEY=stale-dotenv-vision\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-stt")
    monkeypatch.setenv(_VISION_KEY_ENV, "stale-vision")
    env.reset_cache()

    _read_roles(
        engine,
        monkeypatch,
        _framed_roles({"stt_key": "synthetic-stt", "vision_key": "", "vision_base_url": ""}),
    )

    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-stt"  # STT는 산다
    assert env.get(_VISION_KEY_ENV) == ""  # 비전 키 부재 — dotenv 부활 없음
    assert env.get(_VISION_URL_ENV) == ""


def test_role_envelope_resolves_names_from_the_public_project_config(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """비전 역할 이름은 작업이 쓰는 공개 설정이 정한다 — 정당한 사용자 지정은 산다."""
    (tmp_path / "vlogsrt.project.toml").write_text(
        "schema_version = 1\n"
        'vision_api_key_env = "MY_VISION_KEY"\n'
        'vision_base_url_env = "MY_VISION_URL"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    _read_roles(
        engine,
        monkeypatch,
        _framed_roles(
            {
                "stt_key": "synthetic-stt",
                "vision_key": "synthetic-vision",
                "vision_base_url": "https://vision.example/v1",
            }
        ),
    )

    assert os.environ["MY_VISION_KEY"] == "synthetic-vision"
    assert os.environ["MY_VISION_URL"] == "https://vision.example/v1"
    assert env._applied_vision_key_name == "MY_VISION_KEY"


def test_ambiguous_role_names_reject_before_any_assignment(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """모호한 역할 이름은 배정 전에 닫는다 — 부분 적용은 부분 병합이다."""
    (tmp_path / "vlogsrt.project.toml").write_text(
        "schema_version = 1\n"
        'vision_api_key_env = "SHARED_ROLE_NAME"\n'
        'vision_base_url_env = "SHARED_ROLE_NAME"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "untouched-stt")

    with pytest.raises(RuntimeError, match="같다"):
        _read_roles(
            engine,
            monkeypatch,
            _framed_roles({"stt_key": "synthetic-stt", "vision_key": "v", "vision_base_url": "u"}),
        )

    assert os.environ[_ENGINE_KEY_NAME] == "untouched-stt"  # STT도 건드리지 않았다
    assert "SHARED_ROLE_NAME" not in os.environ


def test_role_env_carrier_applies_and_is_consumed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """번들의 V2 환경 운반자 — 같은 JSON 계약, 소비 뒤 이름은 지운다."""
    import json

    monkeypatch.setenv(
        _ROLE_CARRIER_ENV,
        json.dumps(
            {
                "stt_key": "synthetic-bundled-stt",
                "vision_key": "synthetic-bundled-vision",
                "vision_base_url": "https://vision.example/v1",
            }
        ),
    )

    _carry_env(engine)

    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-bundled-stt"
    assert os.environ[_VISION_KEY_ENV] == "synthetic-bundled-vision"
    assert os.environ[_VISION_URL_ENV] == "https://vision.example/v1"
    assert _ROLE_CARRIER_ENV not in os.environ


@pytest.mark.parametrize(
    "payload",
    [
        '{"stt_key":"a","vision_key":"b"}',  # 필드가 하나 비었다
        '{"stt_key":"a","vision_key":"b","vision_base_url":"c","extra":"d"}',  # 불명 필드
        '{"stt_key":"a","vision_key":"b","vision_base_url":1}',  # 문자열 아닌 값
        '{"stt_key":"a","vision_key":["b"],"vision_base_url":"c"}',  # 배열 값
        '["stt_key","vision_key","vision_base_url"]',  # 객체가 아니다
        '"stt_key"',  # 문자열 페이로드
        "not-json-at-all",  # JSON이 아니다
        '{"stt_key":"a","stt_key":"b","vision_key":"c","vision_base_url":"d"}',  # 중복 필드
    ],
)
def test_role_payload_schema_violations_fail_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, payload: str
) -> None:
    """V2 페이로드 계약 위반은 닫는다 — 중복·불명·결실·타열·비객체 전부."""
    frame = f"VLOGSRT-ENGINE-CARRIER 2\n{len(payload.encode('utf-8'))}\n{payload}\n".encode()
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))

    version, body = engine._read_carrier_envelope()  # 프레임 자체는 성립한다
    assert version == "2"
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._resolve_carried_credentials((version, body))


def test_role_payload_nul_inside_a_value_fails_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """값 안의 NUL(탈출형)도 닫는다 — C 문자열 경계를 넘는 값은 키가 아니다."""
    payload = '{"stt_key":"a\\u0000b","vision_key":"","vision_base_url":""}'
    frame = f"VLOGSRT-ENGINE-CARRIER 2\n{len(payload.encode())}\n{payload}\n".encode()
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))

    _, body = engine._read_carrier_envelope()
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._resolve_carried_credentials(("2", body))


@pytest.mark.parametrize(
    "env_setup",
    [
        {_CARRIER_ENV: "legacy"},
        {_ROLE_CARRIER_ENV: '{"stt_key":"","vision_key":"","vision_base_url":""}'},
    ],
    ids=["with-legacy-env", "with-role-env"],
)
def test_a_v2_stdin_envelope_never_merges_with_another_carrier(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, env_setup: dict[str, str]
) -> None:
    """V2는 호출의 자격 증명 전부다 — 다른 운반자와 나란히 오면 충돌로 닫는다."""
    for name, value in env_setup.items():
        monkeypatch.setenv(name, value)
    with pytest.raises(engine._CarrierEnvelopeError, match="conflicting"):
        engine._resolve_carried_credentials(
            ("2", '{"stt_key":"a","vision_key":"","vision_base_url":""}')
        )


def test_role_and_legacy_env_carriers_together_fail_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """V2 환경 운반자도 V1 환경 운반자와 섞이지 않는다 — 부분 병합 금지."""
    monkeypatch.setenv(_CARRIER_ENV, "legacy-key")
    monkeypatch.setenv(
        _ROLE_CARRIER_ENV,
        '{"stt_key":"","vision_key":"","vision_base_url":""}',
    )
    with pytest.raises(engine._CarrierEnvelopeError, match="conflicting"):
        engine._resolve_carried_credentials(None)


def test_v2_env_carrier_oversize_fails_closed(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """환경 운반자도 같은 상한 — 8192바이트(부호화 포함)를 넘으면 닫는다."""
    monkeypatch.setenv(_ROLE_CARRIER_ENV, "x" * (engine._CARRIER_MAX_BYTES + 1))
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._resolve_carried_credentials(None)


def test_role_payload_rejections_never_echo_the_payload(
    monkeypatch: pytest.MonkeyPatch,
    engine: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """V2 거절 문면은 페이로드를 절대 되뇌지 않는다 — 종료 코드 2로 닫는다."""
    job_dir = tmp_path / "carrier-job"
    job_dir.mkdir()
    monkeypatch.chdir(tmp_path)
    payload = '{"stt_key":"SYNTHETIC-PAYLOAD-KEY","extra":"x"}'
    frame = f"VLOGSRT-ENGINE-CARRIER 2\n{len(payload.encode())}\n{payload}\n".encode()
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))
    assert engine.main(["roughcut", "job-1", str(job_dir), "--stdin-carrier"]) == 2
    captured = capsys.readouterr()
    assert "SYNTHETIC-PAYLOAD-KEY" not in captured.out + captured.err


def test_engine_reports_a_malformed_config_as_a_controlled_failure(
    monkeypatch: pytest.MonkeyPatch,
    engine: ModuleType,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """V2 + 못 쓰는 프로젝트 설정은 통제된 1이다 — traceback도 제출값 반향도 없다.

    제출값 둘(STT·비전)을 봉투 페이로드와 못 쓰는 설정 파일 **안에** 실제로 넣고,
    어떤 인쇄·기록 표면에도 나오지 않음을 본다. 개발 실행기의 상태 기록 계약도
    여기서 실측한다 — 전달 성공으로 프로세스는 0, 안쪽 실패(1)는 기록 한 줄로 나간다.
    같은 job의 재시도는 잠금이 풀려 있어 중복(3)이 아니다. 분석 문(`analyze`)은
    D163으로 지웠다 — 운반 적용은 러프컷 실행 앞에서 같은 문을 지난다.
    """
    submitted_stt = "SYNTHETIC-CONFIG-FAIL-STT-KEY"
    submitted_vision = "SYNTHETIC-CONFIG-FAIL-VISION-KEY"
    config = tmp_path / "vlogsrt.project.toml"
    config.write_text(
        "schema_version = 1\n"
        f'broken_key = "mixed {submitted_stt} and {submitted_vision}"\n'  # 못 쓰는 toml
        "unterminated = \n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    job_dir = tmp_path / "config-fail-job"
    job_dir.mkdir()
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-managed-stt")
    roles = {
        "stt_key": submitted_stt,
        "vision_key": submitted_vision,
        "vision_base_url": "https://vision.example/v1",
    }

    def _status_path(token: str) -> Path:
        directory = tmp_path / f"vlogsrt-status-{token}"
        directory.mkdir()
        return directory / f"{token}.status"

    capsys.readouterr()  # 이전 시험 출력과 갈라 놓는다
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed_roles(roles)))
    first_status = _status_path("config-fail-first")
    delivered = engine.main(
        ["roughcut", "job-1", str(job_dir), str(first_status), "--stdin-carrier"]
    )
    captured = capsys.readouterr()

    # 상태 기록 계약 — 프로세스는 전달 성공(0), 안쪽 실패(1)는 기록 한 줄이다.
    assert delivered == 0
    assert first_status.read_text(encoding="utf-8") == "VLOGSRT-ENGINE-STATUS config-fail-first 1\n"
    # 통제된 실패 문면 — traceback도 제출값 반향도 없다.
    assert "cannot apply carried credentials" in captured.err
    assert submitted_stt not in captured.out + captured.err
    assert submitted_vision not in captured.out + captured.err
    assert "Traceback" not in captured.out + captured.err

    # 재시도 — 잠금은 이미 풀려 있다. 설정을 고치면 같은 job이 운반 적용을 지나
    # 다시 소유권을 얻는다(중복 3이 아니다 — 입력이 없어 러프컷 자체는 1로 끝난다).
    config.write_text("schema_version = 1\n", encoding="utf-8")
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed_roles(roles)))
    second_status = _status_path("config-fail-retry")
    assert (
        engine.main(["roughcut", "job-1", str(job_dir), str(second_status), "--stdin-carrier"]) == 0
    )
    retried = capsys.readouterr()
    assert second_status.read_text(encoding="utf-8").startswith(
        "VLOGSRT-ENGINE-STATUS config-fail-retry "
    )
    assert "already running" not in retried.err
    assert "cannot apply carried credentials" not in retried.err
    assert (job_dir / "workspace").is_dir()  # 소유권을 얻고 실행까지 갔다

    # 지속 표면 어디에도 제출값은 없다 — 상태 기록·job 산물 전부.
    for record in tmp_path.rglob("*"):
        if record.is_file():
            content = record.read_text(encoding="utf-8", errors="ignore")
            assert submitted_stt not in content, record.name
            assert submitted_vision not in content, record.name


def test_failure_text_redacts_both_managed_values_when_config_parsing_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """설정 해석이 실패해도 두 관리 값을 안다 — 이름은 기억되었거나 기본이다."""
    import argparse as argparse_module

    from vlogsrt.project import commands as project_commands
    from vlogsrt.project.config import ProjectConfigError, load_project_config

    broken = tmp_path / "broken.project.toml"
    broken.write_text("schema_version = 1\nthis is not toml =\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "synthetic-engine-key")
    monkeypatch.setenv(_VISION_KEY_ENV, "synthetic-vision-key")
    args = argparse_module.Namespace(project_config=broken)

    # 정말로 설정 해석 오류가 나는 길이다 — str 전달로 다른 예외를 재는 게 아니다.
    with pytest.raises(ProjectConfigError):
        load_project_config(args.project_config)

    text = project_commands._redact_failure_text(
        args, "boom engine=synthetic-engine-key vision=synthetic-vision-key"
    )
    assert "synthetic-engine-key" not in text
    assert "synthetic-vision-key" not in text


def test_redaction_removes_overlapping_managed_values_longest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """겹치는 값은 긴 쪽부터 지운다 — 짧은 키를 먼저 지우면 긴 키의 꼬리가 드러난다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "synthetic-long-key-with-tail")
    monkeypatch.setenv(_VISION_KEY_ENV, "synthetic-long-key")

    text = env.redact_secrets("rejected synthetic-long-key-with-tail at the gate")

    assert "with-tail" not in text
    assert env.REDACTED_MARK in text


def test_vision_base_url_validation_closes_ports_delimiters_and_encoding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """URL 검증의 좁은 구멍 — 포트·빈 구분자·퍼센트 부호형·해석 불가 모양."""
    from vlogsrt.env import validate_vision_base_url

    monkeypatch.delenv(_ENGINE_KEY_NAME, raising=False)
    monkeypatch.delenv(_VISION_KEY_ENV, raising=False)
    # 정상 모양은 통과한다 — 포트가 있어도 범위 안이면 산다.
    validate_vision_base_url("https://vision.example:8443/v1")
    rejected = [
        "https://provider.invalid:bad/v1",  # 포트가 숫자가 아니다
        "https://provider.invalid:99999/v1",  # 포트가 범위를 넘는다
        "https://provider.invalid:-1/v1",  # 음수 포트
        "https://provider.invalid/v1?",  # 빈 쿼리 구분자
        "https://provider.invalid/v1#",  # 빈 프래그먼트 구분자
        "https://provider.invalid/v%20path",  # 퍼센트 부호형 공백
        "https://provider.invalid/v%00path",  # 퍼센트 부호형 NUL
        "https://provider.invalid/v%0Apath",  # 퍼센트 부호형 제어 문자
        "https://[::1/v1",  # 못 여는 대괄호 — 해석기가 거부한다
    ]
    for endpoint in rejected:
        with pytest.raises(RuntimeError) as caught:
            validate_vision_base_url(endpoint)
        # 오류 문면은 URL을 절대 되뇌지 않는다.
        assert endpoint not in str(caught.value)


def test_invalid_role_endpoint_rejects_before_any_assignment(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """V2의 잘못된 엔드포인트는 배정 전에 닫는다 — 환경에는 아무 값도 안 얹힌다."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "untouched-stt")
    monkeypatch.setenv(_VISION_KEY_ENV, "untouched-vision")
    endpoint = "http://synthetic-endpoint.example/v1"

    with pytest.raises(RuntimeError, match="https"):
        _read_roles(
            engine,
            monkeypatch,
            _framed_roles(
                {
                    "stt_key": "synthetic-stt",
                    "vision_key": "synthetic-vision",
                    "vision_base_url": endpoint,
                }
            ),
        )

    assert os.environ[_ENGINE_KEY_NAME] == "untouched-stt"
    assert os.environ[_VISION_KEY_ENV] == "untouched-vision"
    assert _VISION_URL_ENV not in os.environ or endpoint not in os.environ[_VISION_URL_ENV]


def test_redaction_covers_the_effective_trimmed_value_of_each_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """공백이 붙은 키도 지운다 — SDK는 strip한 값으로 부르므로 반향은 다듬은 모양이다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "  synthetic-stt-key-with-space\n")
    monkeypatch.setenv(_VISION_KEY_ENV, "\tsynthetic-vision-key-with-space ")

    stt_text = env.redact_secrets("SDK 401 rejected key synthetic-stt-key-with-space")
    vision_text = env.redact_secrets("vision refused synthetic-vision-key-with-space")

    assert "synthetic-stt-key-with-space" not in stt_text
    assert env.REDACTED_MARK in stt_text
    assert "synthetic-vision-key-with-space" not in vision_text
    assert env.REDACTED_MARK in vision_text
    # 비밀 없는 진단은 그대로 — 다듬은 값 후보가 문장을 죽이지 않는다.
    assert env.redact_secrets("ffprobe not found") == "ffprobe not found"


def test_endpoint_material_detection_cannot_be_defeated_by_pasted_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """키 재료 검사도 다듬은 값을 본다 — 붙여 넣은 공백이 우회가 되지 않는다."""
    from vlogsrt.env import validate_vision_base_url

    padded = "  synthetic-padded-secret \n"
    with pytest.raises(RuntimeError, match="자격증명"):
        validate_vision_base_url("https://h.example/v1/synthetic-padded-secret", secrets=(padded,))
    with pytest.raises(RuntimeError, match="자격증명"):
        validate_vision_base_url(
            "https://h.example/v1%2Fsynthetic%2Dpadded%2Dsecret", secrets=(padded,)
        )


def test_role_payload_escaped_lone_surrogate_fails_closed_before_assignment(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """탈출된 외로운 서러게이트는 배정 전에 닫는다 — 반쪽 배정이 없다.

    JSON 문면은 UTF-8로 성립하지만 해석된 값은 부호화 불가능한 글자를 품는다.
    환경 배정이 그 값에서 죽으면 STT만 얹힌 뒤에 죽는다 — 해석 단계에서 닫는다.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "old-stt-value")
    monkeypatch.setenv(_VISION_KEY_ENV, "old-vision-value")
    payload = '{"stt_key":"synthetic-stt","vision_key":"\\ud800","vision_base_url":""}'
    frame = f"VLOGSRT-ENGINE-CARRIER 2\n{len(payload.encode())}\n{payload}\n".encode()
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))

    _, body = engine._read_carrier_envelope()  # 프레임 자체는 성립한다
    with pytest.raises(engine._CarrierEnvelopeError):
        engine._resolve_carried_credentials(("2", body))

    # 어떤 역할도 바뀌지 않았다 — 반쪽 배정이 없다는 증명.
    assert os.environ[_ENGINE_KEY_NAME] == "old-stt-value"
    assert os.environ[_VISION_KEY_ENV] == "old-vision-value"

    # main 계약으로도 닫는다 — 2, 문면은 페이로드를 모른다.
    job_dir = tmp_path / "surrogate-job"
    job_dir.mkdir()
    monkeypatch.setattr("sys.stdin", _pipe_stdin(frame))
    assert engine.main(["roughcut", "job-1", str(job_dir), "--stdin-carrier"]) == 2
    assert not (job_dir / "workspace").exists()  # 소유권·실행 흔적이 없다


def test_engine_boundary_fails_closed_on_a_broken_role_name_config(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """역할 이름을 푸는 경계는 엔진이다 — 깨진 설정은 조용한 기본 드리움 없이 닫는다.

    하위층(env)은 프로젝트 설정을 수입하지 못하므로 이 주장의 주인은 이제
    이 경계다(불변식 위반 수정, S2A-2 마지막 단위). 배정 전에 닫힌다.
    """
    from vlogsrt.project.config import ProjectConfigError

    (tmp_path / "vlogsrt.project.toml").write_text(
        "schema_version = 1\nbroken =\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "untouched")

    with pytest.raises(ProjectConfigError):
        engine._resolve_vision_role_names()

    # V2 운반 적용도 같은 문에서 닫는다 — 어떤 배정도 일어나기 전에.
    carried = engine._resolve_carried_credentials(
        ("2", '{"stt_key": "a", "vision_key": "b", "vision_base_url": ""}')
    )
    with pytest.raises(ProjectConfigError):
        engine._carry_credentials(carried)
    assert os.environ[_ENGINE_KEY_NAME] == "untouched"
