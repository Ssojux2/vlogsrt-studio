"""엔진 자격 증명 운반의 회귀 시험 — 사적 운반자가 상속 키를 이긴다(STAB-S2A-2).

앱이 저장한 키는 자식에게 **운반자**로만 흘러간다: argv·파일·부모 환경 전역
뮤테이션은 없다. 이 모듈은 운반 계약의 세 가지 면을 못 박는다.

1. 운반자가 말하는 키는 이미 상속된 낡은 키를 **이긴다** — `setdefault`로
   덮으면 앱에서 키를 갈아끼워도 자식이 계속 낡은 키로 유료 호출을 한다.
2. "키 없음" 운반은 상속된 낡은 키를 **부활시키지 않는다** — 앱에 키가 없는데
   셸에서 물려받은 키로 유료 호출이 열리면 진실한 사전 검사가 거짓말이 된다.
3. 운반자가 없는 낡은 직접 호출(번들 3/4-인자 계약)은 상속 환경을 그대로
   둔다 — 레거시 계약을 바꾸지 않는다.

소비 증명도 여기서 한다: 운반된 키는 SDK 공장(`env.require_api_key`)이 실제로
읽는 이름에 도달해야 한다 — 호출 객체 안에만 있는 키는 아무것도 증명하지 않는다.
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

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_CARRIER_ENV = "VLOGSRT_ENGINE_API_KEY"
_ENGINE_KEY_NAME = "OPENAI_API_KEY"


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

    engine._carry_api_key()

    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-submitted-key"
    assert _CARRIER_ENV not in os.environ  # 운반자 이름은 두 번 흐르지 않는다


def test_env_carrier_without_key_does_not_resurrect_a_stale_inherited_key(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """빈 운반자는 '키 없음'이다 — 상속된 낡은 키가 유료 호출을 열지 못한다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setenv(_CARRIER_ENV, "")

    engine._carry_api_key()

    assert not env.has_api_key()  # 빈 값은 없음이다 — 부활이 아니다
    assert _CARRIER_ENV not in os.environ


def test_absent_carrier_keeps_the_inherited_environment(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """운반자가 없으면 상속 환경을 그대로 둔다 — 낡은 직접 호출 계약이다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "inherited-legacy-key")

    engine._carry_api_key()

    assert os.environ[_ENGINE_KEY_NAME] == "inherited-legacy-key"


def test_carried_key_reaches_the_sdk_credential_reader(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """운반된 키는 SDK 공장이 읽는 이름에 도달한다 — 객체 안 머무름이 아니라 사용이다."""
    monkeypatch.setenv(_CARRIER_ENV, "synthetic-carried-key")

    engine._carry_api_key()

    assert env.require_api_key(force=True) == "synthetic-carried-key"
    # 상태·표시 표면에는 값이 실리지 않는다 — 있는지만 말한다.
    assert "synthetic-carried-key" not in env.api_key_source()


def test_carried_key_reaches_the_stt_sdk_constructor(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """운반된 키는 실제 STT 공장(LiveTranscriber→openai.OpenAI)까지 간다.

    호출 객체가 키를 **담고만** 있는 것은 증명이 아니다 — SDK 생성자가 받은
    인자로 증명한다(합성 경계, 네트워크 없음).
    """
    import openai

    import vlogsrt.transcribe as transcribe_module

    seen: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv(_CARRIER_ENV, "synthetic-factory-key")

    engine._carry_api_key()
    transcriber = transcribe_module.get_transcriber(online=True)
    transcriber._client()

    assert seen["api_key"] == "synthetic-factory-key"


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

    carried = engine._read_carrier_envelope()

    assert carried == "synthetic-envelope-key"
    engine._carry_api_key(carried)
    assert os.environ[_ENGINE_KEY_NAME] == "synthetic-envelope-key"


def test_stdin_envelope_length_zero_is_an_explicit_no_key(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """길이 0 봉투는 '키 없음'이다 — 상속 키를 내려놓고 끝난다."""
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed("")))

    carried = engine._read_carrier_envelope()

    assert carried == ""
    engine._carry_api_key(carried)
    assert not env.has_api_key()


def test_stdin_envelope_framing_counts_bytes_not_characters(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """길이는 바이트 수다 — 한글 키가 문자 수로 세여 잘리거나 남지 않는다."""
    payload = "합성-열쇠-값"
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed(payload)))

    assert engine._read_carrier_envelope() == payload


def test_stdin_envelope_accepts_a_maximal_payload(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """상한 안 최대 페이로드는 통과한다 — Swift 쪽 상한과 정확히 같은 값이다."""
    payload = "k" * engine._CARRIER_MAX_BYTES
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed(payload)))

    assert engine._read_carrier_envelope() == payload


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

    assert engine._read_carrier_envelope() == payload
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
    assert engine._parse_carrier_envelope(frame) == payload


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
        b"VLOGSRT-ENGINE-CARRIER 2\n3\nabc\n",  # 버전을 모른다
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
        assert first.result(timeout=10) == "synthetic-first-key"
        assert second.result(timeout=10) == "synthetic-second-key"


def test_envelope_key_replaces_a_stale_env_and_dotenv_value(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType, tmp_path: Path
) -> None:
    """봉투의 키는 상속 환경 **와** dotenv의 낡은 값을 둘 다 이긴다.

    dotenv의 적재는 setdefault다 — 봉투가 값을 먼저 얹으면 낡은 dotenv 값은
    다시 살아날 자리가 없다. 소비 시점(require_api_key)의 값이 곧 봉투의 값이다.
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
    carried = engine._read_carrier_envelope()
    engine._carry_api_key(carried)

    assert env.require_api_key(force=True) == "synthetic-envelope-wins"


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
    carried = engine._read_carrier_envelope()
    engine._carry_api_key(carried)

    assert not env.has_api_key()
    assert env.get(_ENGINE_KEY_NAME) == ""
    with pytest.raises(RuntimeError):
        env.require_api_key(force=True)


def test_envelope_key_reaches_the_stt_sdk_constructor(
    monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """봉투의 키는 실제 STT 공장(LiveTranscriber→openai.OpenAI)까지 간다.

    호출 객체가 키를 담고만 있는 것은 증명이 아니다 — SDK 생성자가 받은 인자로
    증명한다(합성 경계, 네트워크 없음).
    """
    import openai

    import vlogsrt.transcribe as transcribe_module

    seen: dict[str, object] = {}

    class _FakeOpenAI:
        def __init__(self, **kwargs: object) -> None:
            seen.update(kwargs)

    monkeypatch.setattr(openai, "OpenAI", _FakeOpenAI)
    monkeypatch.setenv(_ENGINE_KEY_NAME, "stale-inherited-key")
    monkeypatch.setattr("sys.stdin", _pipe_stdin(_framed("synthetic-factory-key")))

    carried = engine._read_carrier_envelope()
    engine._carry_api_key(carried)
    transcriber = transcribe_module.get_transcriber(online=True)
    transcriber._client()

    assert seen["api_key"] == "synthetic-factory-key"


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
    assert engine.main(["analyze", "job-1", str(job_dir), "--stdin-carrier"]) == 2
    captured = capsys.readouterr()
    assert "abcEXTRA" not in captured.out + captured.err
    assert "abc" not in captured.out + captured.err


# ─────────────────────────────────────────────────────────────
#  실패 문면의 가림 — SDK 오류가 키를 되뇌어도 기록·stderr에 실리지 않는다
# ─────────────────────────────────────────────────────────────


def test_captured_transcription_failures_redact_the_engine_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """speech·boundary 포획 기록은 값을 지우고 모양만 남긴다(S2A-2)."""
    from vlogsrt.project import boundary, speech

    monkeypatch.setenv(_ENGINE_KEY_NAME, "synthetic-redact-key")
    message = RuntimeError("SDK 401 rejected key synthetic-redact-key")

    speech_capture = speech._Capture()
    speech_capture.record("전사 호출", message)
    assert "synthetic-redact-key" not in speech_capture.describe()
    assert env.REDACTED_MARK in speech_capture.describe()

    boundary_capture = boundary._BoundaryCapture()
    boundary_capture.record("재전사 호출", message)
    assert "synthetic-redact-key" not in boundary_capture.describe()


def test_sdk_400_retry_warning_redacts_the_engine_key_before_stderr(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    """400 재시도 경고는 인쇄 표면 중 가장 앞이다 — 포획 기록보다 먼저 가린다.

    인자 해석(_rejected)은 원문으로 한다(재시도 의미가 가려지 없이 유지) —
    인쇄만 가린다. 재시도·부가 인자 감축 계약 자체는 그대로 도는 것도 본다.
    """
    from vlogsrt import transcribe as transcribe_module
    from vlogsrt.config import Config
    from vlogsrt.preprocess import Chunk

    submitted = "synthetic-echoed-key"
    monkeypatch.setenv(_ENGINE_KEY_NAME, submitted)
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "WARNING")

    class _Echo400(Exception):
        status_code = 400

        def __str__(self) -> str:
            return f"invalid request: key {submitted} rejected for param language"

    dropped: list[dict] = []

    class _Transcriptions:
        def create(self, **kwargs: object) -> object:
            dropped.append(dict(kwargs))
            raise _Echo400()

    class _Audio:
        transcriptions = _Transcriptions()

    class _Client:
        audio = _Audio()

    audio = tmp_path / "clip.mp3"
    audio.write_bytes(b"synthetic-audio")
    transcriber = transcribe_module.LiveTranscriber(api_key=submitted)

    capfd.readouterr()  # 이전 시험 출력과 갈라 놓는다
    with pytest.raises(_Echo400):
        transcriber._one_chunk(_Client(), Chunk(audio, 0.0, 1.0, 0), Config(), {"language": "ko"})
    captured = capfd.readouterr()

    assert submitted not in captured.err  # 첫 인쇄 표면이 이미 가렸다
    assert env.REDACTED_MARK in captured.err
    # 계약 보존 — 경고 뒤에도 부가 인자를 떼고 다시 부른다(두 번째 호출).
    assert len(dropped) == 2
    assert "language" in dropped[0] and "language" not in dropped[1]


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


def test_paid_attempt_record_never_persists_the_submitted_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    """SDK가 키를 되뇌며 실패해도 시도 기록(attempts)과 stderr에 키가 오지 않는다."""
    import importlib.util

    from tests.project.test_engine_entrypoint import (
        _patch_paid_gates,
        _stage_job,
        _write_authorization,
    )

    spec = importlib.util.spec_from_file_location("vlogsrt_engine_redaction", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    engine = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(engine)

    monkeypatch.chdir(tmp_path)
    job_dir = _stage_job(tmp_path)
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 0
    _write_authorization(job_dir)
    _patch_paid_gates(monkeypatch)

    # SDK 경계가 키를 되뇌며 죽는다 — 유료 호출은커녕 클라이언트 생성에서 실패한다.
    submitted = "synthetic-echoed-key"
    monkeypatch.setenv(_ENGINE_KEY_NAME, submitted)

    import vlogsrt.transcribe as transcribe_module

    def echoing_client(**kwargs: object) -> object:
        raise RuntimeError(f"401 unauthorized: api key {submitted} rejected")

    monkeypatch.setattr(transcribe_module, "create_vision_client", echoing_client)

    capfd.readouterr()  # 이전 시험 출력과 갈라 놓는다
    assert engine.main(["analyze", "job-1", str(job_dir)]) == 1
    captured = capfd.readouterr()

    attempts = (job_dir / "analysis-attempts.jsonl").read_text(encoding="utf-8")
    assert submitted not in attempts
    assert env.REDACTED_MARK in attempts
    # stderr·stdout·직접 쓴 기록 어디에도 제출값은 없다.
    assert submitted not in captured.out + captured.err
    for record in job_dir.iterdir():
        if record.is_file():
            assert submitted not in record.read_text(encoding="utf-8"), record.name
