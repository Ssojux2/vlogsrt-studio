"""불변식 검사기(`scripts/check_invariants.py`)를 가짜 소스 트리에 돌리는 시험 재료.

`tests/test_invariants.py`의 같은 이름 재료를 옮겼다 — 그 파일은 whisper와 함께 지운 옛 파이프라인
(`vlogsrt.pipeline`)을 머리에서 import해(D163) 등급 시험이 그 파일에 기대면 모듈째 못 읽는다.
검사기 자체는 그대로 쓴다. 진짜 트리는 건드리지 않는다 — 경로 전역만 `tmp_path`로 돌린다.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

_CHECKER_SPEC = importlib.util.spec_from_file_location(
    "check_invariants", ROOT / "scripts" / "check_invariants.py"
)
check_invariants = importlib.util.module_from_spec(_CHECKER_SPEC)
assert _CHECKER_SPEC.loader is not None
_CHECKER_SPEC.loader.exec_module(check_invariants)

TIER_SERVICE = "vlogsrt/project/raw/service.py"
CLEAN_TIERING = (
    "class TimingSignals:\n    pass\n\n\nUNKNOWN_TIMING = TimingSignals()\n\n\n"
    "def classify(document, *, timing=UNKNOWN_TIMING):\n    return document\n"
)


def fake_source_tree(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, files: dict[str, str]
) -> None:
    """검사기가 `tmp_path` 아래 가짜 트리를 보게 한다. 진짜 트리는 건드리지 않는다."""
    for rel, body in files.items():
        target = tmp_path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
    monkeypatch.setattr(check_invariants, "ROOT", tmp_path)
    monkeypatch.setattr(check_invariants, "PACKAGE", tmp_path / "vlogsrt")
    monkeypatch.setattr(check_invariants, "UI_DIR", tmp_path / "ui")
    monkeypatch.setattr(check_invariants, "PROJECT_PACKAGE", tmp_path / "vlogsrt" / "project")
    monkeypatch.setattr(
        check_invariants,
        "XML_VALIDATION_MODULE",
        tmp_path / "vlogsrt" / "project" / "xml_validation.py",
    )
    monkeypatch.setattr(
        check_invariants,
        "EDIT_MODULE_PATH",
        tmp_path / "vlogsrt" / "project" / "edit.py",
    )
