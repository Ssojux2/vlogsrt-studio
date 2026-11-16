"""엔진 진입점의 읽기 전용 readiness 계약(S2A-2 마지막 단위).

여기서 증명하는 것:

- `readiness`는 **아무 것도 쓰지 않는다** — 실행 잠금을 얻지 않고, workspace를
  만들지 않고, 결과 동기화도 없다. 남기는 것은 호출 소유의 보고서 한 줄뿐이다.
- 보고서는 공개 데이터만 담는다 — 파이썬 실행 파일·버전, 실제 자식 환경에서
  풀린 ffmpeg·ffprobe, 이해한 운반자 버전. 환경 통째로·자격 증명 재료는 없다.
- 보고서 자리는 호출 소유 모양이어야 한다 — 상대 경로·job 안·이미 있는
  파일·어긋난 이름·어긋난 부모는 계약 위반(2)이고 남의 바이트를 안 덮쓴다.
- 운반자는 실제 적용 경로를 지난다 — V2 값이 적용되고(이름 소비), 그르른
  봉투·충돌하는 운반자는 닫힌다. 적용된 값이 보고서에 어떤 형태로도
  되뇌어지지 않는다.
- 기존 호출 계약은 그대로 산다 — 없는 job 뿌리·모르는 연산은 2다.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
from pathlib import Path
from types import ModuleType

import pytest

from tests._dotenv_guard import stage_synthetic_dotenv

# 개발 엔진(`macos/scripts/`)은 POSIX 전용이다 — `fcntl` 잠금 · zsh 설치 스크립트. Windows에는 그
# 엔진이 없다(작업대는 `vlogsrt/filelock.py`의 msvcrt 길을 쓴다, W-84a).
pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX 전용 개발 엔진(macos/scripts)")

_ENGINE_SCRIPT = Path(__file__).resolve().parents[2] / "macos" / "scripts" / "vlogsrt-engine.py"
_REPORT_NAME = "readiness.json"
_ROLE_CARRIER = "VLOGSRT_ENGINE_ROLE_CREDENTIALS"
_MANAGED_NAMES = ("OPENAI_API_KEY", "GOOGLE_API_KEY", "GEMINI_BASE_URL", _ROLE_CARRIER)


def _load_engine() -> ModuleType:
    spec = importlib.util.spec_from_file_location("vlogsrt_engine_readiness", _ENGINE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def offline_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """진입점이 참조하는 dotenv는 이 모듈이 지은 합성물뿐이다(S2A-1 격리 원리)."""
    dotenv = stage_synthetic_dotenv(tmp_path)
    monkeypatch.chdir(tmp_path)
    yield dotenv


@pytest.fixture(autouse=True)
def managed_env() -> None:
    """운반자 적용이 남기는 상태를 시험 뒤 되돌린다 — 진입점이 직접 지운다.

    환경 이름만이 아니라 `env._applied_vision_key_name`(V2 적용 표식)도 돌려
    놓는다: 이것이 남으면 뒤의 마커 없는 시험들이 '앱 관리'로 읽혀 권한 문서를
    요구한다(S2A-2 결함의 실측). test_engine_carrier의 재설정 계약과 같은 원리.
    """
    from vlogsrt import env

    saved_marker = env._applied_vision_key_name
    saved = {name: os.environ.get(name) for name in _MANAGED_NAMES}
    yield
    env._applied_vision_key_name = saved_marker
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture()
def engine() -> ModuleType:
    return _load_engine()


def _job_dir(tmp_path: Path) -> Path:
    job = tmp_path / "job"
    job.mkdir()
    (job / "input.fcpxml").write_text("<fcpxml/>\n", encoding="utf-8")
    return job


def _report_root(tmp_path: Path, label: str = "readiness") -> Path:
    """호출 소유 보고서 자리 — `<tmp>/vlogsrt-readiness-<token>/readiness.json`."""
    root = tmp_path / f"vlogsrt-{label}-synthetic-token"
    root.mkdir()
    return root / _REPORT_NAME


def _read_report(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1  # 한 줄 — 읽는 쪽이 상한 안에서 읽는다
    return json.loads(lines[0])


def test_readiness_writes_one_bounded_public_report_and_touches_nothing(
    tmp_path: Path, engine: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 키 유무는 환경이 정한다 — 같은 프로세스의 앞선 시험이 남긴 dotenv 캐시와
    # 무관하게, 이 시험의 부재 판정을 명시적으로 고정한다.
    monkeypatch.setenv("OPENAI_API_KEY", "")
    job = _job_dir(tmp_path)
    before = sorted(path.name for path in job.iterdir())
    report = _report_root(tmp_path)

    assert engine.main(["readiness", "job-1", str(job), str(report)]) == 0

    # job 상태는 그대로다 — 잠금·workspace·결과 동기화가 없다.
    assert sorted(path.name for path in job.iterdir()) == before
    assert not (job / ".vlogsrt-engine.lock").exists()
    assert not (job / "workspace").exists()

    payload = _read_report(report)
    assert set(payload) == {
        "engine",
        "readiness_version",
        "openai_key_present",
        "python",
        "tools",
        "carrier_versions",
    }
    assert payload["engine"] == "vlogsrt-engine"
    assert payload["readiness_version"] == 1
    assert payload["openai_key_present"] is False
    python = payload["python"]
    assert isinstance(python, dict)
    assert isinstance(python["executable"], str) and python["executable"]
    assert isinstance(python["version"], str) and python["version"]
    tools = payload["tools"]
    assert isinstance(tools, dict)
    for name in ("ffmpeg", "ffprobe"):
        assert name in tools
        assert tools[name] is None or isinstance(tools[name], str)
    assert payload["carrier_versions"] == ["1", "2"]
    assert report.stat().st_size <= 4096


def test_readiness_with_status_file_writes_the_report_next_to_it(
    tmp_path: Path, engine: ModuleType
) -> None:
    """개발 실행기 계약 — 넷째 인자가 상태 기록이면 보고서는 그 옆에 생긴다."""
    job = _job_dir(tmp_path)
    status_dir = tmp_path / "vlogsrt-status-synthetic-token"
    status_dir.mkdir()
    status = status_dir / "synthetic-token.status"

    assert engine.main(["readiness", "job-1", str(job), str(status)]) == 0

    assert status.read_text(encoding="utf-8") == "VLOGSRT-ENGINE-STATUS synthetic-token 0\n"
    assert _read_report(status_dir / _REPORT_NAME)["engine"] == "vlogsrt-engine"


def test_readiness_without_a_destination_is_a_contract_violation(
    tmp_path: Path, engine: ModuleType
) -> None:
    """보고서 자리도 상태 기록도 없는 readiness는 자기 계약을 어겼다 — 2."""
    job = _job_dir(tmp_path)
    assert engine.main(["readiness", "job-1", str(job)]) == 2


@pytest.mark.parametrize(
    "problem",
    ["relative", "inside-job", "already-exists", "wrong-name", "wrong-parent"],
)
def test_readiness_rejects_bad_report_destinations(
    tmp_path: Path, engine: ModuleType, problem: str
) -> None:
    job = _job_dir(tmp_path)
    if problem == "relative":
        report = Path(_REPORT_NAME)
    elif problem == "inside-job":
        report = job / _REPORT_NAME
    elif problem == "already-exists":
        report = _report_root(tmp_path)
        report.write_text("occupied\n", encoding="utf-8")
    elif problem == "wrong-name":
        root = tmp_path / "vlogsrt-readiness-synthetic-token"
        root.mkdir()
        report = root / "report.json"
    else:
        report = tmp_path / "elsewhere" / _REPORT_NAME
        report.parent.mkdir()

    assert engine.main(["readiness", "job-1", str(job), str(report)]) == 2
    if problem == "already-exists":
        assert report.read_text(encoding="utf-8") == "occupied\n"  # 덮어쓰지 않는다
    elif problem != "relative":
        assert not report.exists()


def test_readiness_reports_tools_resolved_in_the_actual_child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """도구 칸은 shutil.which의 답 그대로다 — 앱이 PATH를 추측하지 않는다."""
    job = _job_dir(tmp_path)
    report = _report_root(tmp_path)
    resolved = {
        "ffmpeg": "/synthetic/child/bin/ffmpeg",
        "ffprobe": "/synthetic/child/bin/ffprobe",
    }
    monkeypatch.setattr(shutil, "which", lambda name: resolved.get(name))
    assert engine.main(["readiness", "job-1", str(job), str(report)]) == 0
    assert _read_report(report)["tools"] == resolved

    # 반쪽 환경 — ffprobe가 없으면 그 칸은 null이다(빈 문자열이 아니다).
    missing = _report_root(tmp_path, label="readiness-second")
    ffmpeg_only = {"ffmpeg": "/synthetic/child/bin/ffmpeg"}
    monkeypatch.setattr(shutil, "which", lambda name: ffmpeg_only.get(name))
    assert engine.main(["readiness", "job-1", str(job), str(missing)]) == 0
    tools = _read_report(missing)["tools"]
    assert isinstance(tools, dict)
    assert tools["ffmpeg"] == "/synthetic/child/bin/ffmpeg"
    assert tools["ffprobe"] is None


def test_readiness_applies_the_role_carrier_and_never_echoes_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """V2 환경 운반자는 실제 적용 경로를 지난다 — 보고서에는 값이 없다."""
    job = _job_dir(tmp_path)
    report = _report_root(tmp_path)
    stt_key = "synthetic-readiness-stt-key"
    vision_key = "synthetic-readiness-vision-key"
    payload = json.dumps({"stt_key": stt_key, "vision_key": vision_key, "vision_base_url": ""})
    monkeypatch.setenv(_ROLE_CARRIER, payload)

    assert engine.main(["readiness", "job-1", str(job), str(report)]) == 0

    body = report.read_text(encoding="utf-8")
    assert stt_key not in body
    assert vision_key not in body
    # 운반자 이름은 소비 뒤 지워진다 — 키가 두 이름으로 흐르지 않게.
    assert os.environ.get(_ROLE_CARRIER) is None


def _stdin_from(tmp_path: Path, payload: bytes):
    """실제 fd를 가진 stdin — 진입점의 fd 기반 봉투 판독기가 실제로 돈다."""
    frame = tmp_path / "stdin-frame.bin"
    frame.write_bytes(payload)
    return open(frame, "rb")


def test_readiness_with_a_broken_stdin_envelope_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """마커가 있으면 봉투를 읽는다 — 그르른 봉투는 보고서 없이 2로 닫는다."""
    job = _job_dir(tmp_path)
    report = _report_root(tmp_path)
    handle = _stdin_from(tmp_path, b"garbage without a magic line\n")
    monkeypatch.setattr("sys.stdin", handle)
    try:
        assert engine.main(["readiness", "job-1", str(job), str(report), "--stdin-carrier"]) == 2
    finally:
        handle.close()
    assert not report.exists()


def test_readiness_with_conflicting_carriers_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, engine: ModuleType
) -> None:
    """V2 환경 운반자가 이미 있는데 V1 stdin 봉투가 또 오면 충돌이다 — 닫는다."""
    job = _job_dir(tmp_path)
    report = _report_root(tmp_path)
    monkeypatch.setenv(
        _ROLE_CARRIER,
        json.dumps({"stt_key": "", "vision_key": "", "vision_base_url": ""}),
    )
    frame = b"VLOGSRT-ENGINE-CARRIER 1\n5\nplain\n"
    handle = _stdin_from(tmp_path, frame)
    monkeypatch.setattr("sys.stdin", handle)
    try:
        assert engine.main(["readiness", "job-1", str(job), str(report), "--stdin-carrier"]) == 2
    finally:
        handle.close()
    assert not report.exists()


def test_readiness_rejects_unknown_job_shapes(tmp_path: Path, engine: ModuleType) -> None:
    """기존 호출 계약은 그대로 산다 — 없는 job 뿌리·모르는 연산은 2다."""
    report = _report_root(tmp_path)
    assert engine.main(["readiness", "job-1", str(tmp_path / "missing")]) == 2
    assert engine.main(["preflight", "job-1", str(_job_dir(tmp_path)), str(report)]) == 2
    assert not report.exists()


def test_readiness_reports_openai_key_presence_and_never_the_value(
    tmp_path: Path, engine: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """개발 .env의 OpenAI 키는 유무만 보고된다(패널 시대 계약) — 값은 없다."""
    job = _job_dir(tmp_path)
    report = _report_root(tmp_path, "presence")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-readiness-key")

    assert engine.main(["readiness", "job-1", str(job), str(report)]) == 0
    payload = _read_report(report)
    assert payload["openai_key_present"] is True
    # 보고서는 유무불리언 외에 키 재료를 어떤 형태로도 실어 나르지 않는다.
    assert "synthetic-readiness-key" not in report.read_text(encoding="utf-8")

    # 빈 값은 부재다 — 공백만 남은 키로 '있다'고 말하지 않는다.
    report2 = _report_root(tmp_path, "absence")
    monkeypatch.setenv("OPENAI_API_KEY", "   ")
    assert engine.main(["readiness", "job-1", str(job), str(report2)]) == 0
    assert _read_report(report2)["openai_key_present"] is False
