"""세 운영체제 CI(`.github/workflows/check.yml`, W-84, D144) — 오프라인 · 비밀 없음 · mac만 막는다.

YAML 해석기는 의존성이 아니다(규칙 10) — 글자로 잠근다.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "check.yml"


def _text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


def test_matrix_covers_three_systems() -> None:
    assert "os: [macos-latest, windows-latest, ubuntu-latest]" in _text()


def test_runs_offline_without_secrets() -> None:
    text = _text()
    assert 'VLOGSRT_OFFLINE: "1"' in text
    assert "secrets." not in text
    assert "--online" not in text
    assert ".env" not in text.replace("env:", "")


def test_every_operating_system_blocks() -> None:
    """W-84a로 Windows·Linux가 초록이 되어 보고만 하던 자리를 막는 게이트로 올렸다."""
    assert "continue-on-error" not in _text()


def test_runs_the_same_checks_as_make() -> None:
    text = _text()
    assert "python scripts/smoke_ui.py" in text
    assert "python scripts/check_all.py" in text
    assert text.index("smoke_ui.py") < text.index("check_all.py")


def test_installs_ffmpeg_and_node_everywhere() -> None:
    text = _text()
    for system in ("macOS", "Windows", "Linux"):
        # 그 운영체제의 단계(한 줄 `run:`이든 여러 줄 `run: |`이든)가 ffmpeg를 깐다.
        steps = re.findall(rf"if: runner.os == '{system}'\n(.*?)(?=\n\s+- name:|\Z)", text, re.S)
        assert any("ffmpeg" in step for step in steps), system
    assert "actions/setup-node" in text


def test_reads_no_untrusted_event_text() -> None:
    """푸시·PR 글(제목·본문·브랜치 이름)을 명령 줄에 넣지 않는다 — 주입 경로가 없다."""
    assert "github.event." not in _text()
    assert "github.head_ref" not in _text()


def test_pins_ffmpeg_9_0_where_the_package_manager_would_drift() -> None:
    """소리 시각 시험은 ffmpeg 9.0으로 잰 값이다 — Windows·Linux는 판을 못 박는다.

    못 박지 않은 choco가 9.1.0을 깔자 test_pcm 둘이 깨지고 시험 하나가 2시간 한도까지 멈췄다.
    """
    text = _text()
    assert "choco install ffmpeg-full --version=9.0.2" in text
    assert "ffmpeg-n9.0-latest-linux64-gpl-9.0" in text
