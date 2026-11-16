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


def test_only_macos_blocks_for_now() -> None:
    assert "continue-on-error: ${{ matrix.os != 'macos-latest' }}" in _text()


def test_runs_the_same_checks_as_make() -> None:
    text = _text()
    assert "python scripts/smoke_ui.py" in text
    assert "python scripts/check_all.py" in text
    assert text.index("smoke_ui.py") < text.index("check_all.py")


def test_installs_ffmpeg_and_node_everywhere() -> None:
    text = _text()
    for system in ("macOS", "Windows", "Linux"):
        assert re.search(rf"if: runner.os == '{system}'\n\s+run: .*ffmpeg", text), system
    assert "actions/setup-node" in text


def test_reads_no_untrusted_event_text() -> None:
    """푸시·PR 글(제목·본문·브랜치 이름)을 명령 줄에 넣지 않는다 — 주입 경로가 없다."""
    assert "github.event." not in _text()
    assert "github.head_ref" not in _text()
