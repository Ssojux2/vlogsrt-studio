"""CLAUDE.md 불변 규칙을 코드로 못 박는다.

여기가 깨지면 설계가 무너진 것이다. 테스트를 고치지 말고 보고하라.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vlogsrt import evaluate, mapping, pipeline, refine
from vlogsrt.config import Config
from vlogsrt.models import Cue, Segment

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
ROOT = Path(__file__).resolve().parent.parent
VIDEO = Path("golden/demo.mp4")


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def test_timing_unchanged_by_mapping() -> None:
    """★ 관문 2 — 경계 매핑은 텍스트만 바꾼다. 시각을 건드리면 v2 아키텍처 위반이다."""
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, Path("/tmp/vlogsrt-invariant"), force_offline=True)
    spine_cues = mapping.spine_to_cues(result.trace.spine)
    mapped, _, _ = mapping.apply_canonical(
        spine_cues, list(result.trace.spine), result.trace.canonical, list(result.trace.words), cfg
    )
    assert [(cue.start, cue.end) for cue in mapped] == [(cue.start, cue.end) for cue in spine_cues]


def test_draft_cue_times_come_straight_from_the_spine() -> None:
    result = pipeline.run(
        VIDEO, Config(), Path("/tmp/vlogsrt-invariant"), force_offline=True, stop_after="map"
    )
    ok_segments = [s for s in result.trace.spine if s.verdict == "ok"]
    draft = [cue for cue in result.trace.cues if cue.origin != "orphan"]
    assert [(cue.start, cue.end) for cue in draft] == [(s.start, s.end) for s in ok_segments]


def test_mapping_does_not_move_cue_starts_measurably() -> None:
    """정제까지 태워도 큐 시작이 골격에서 크게 벗어나면 안 된다."""
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, Path("/tmp/vlogsrt-invariant"), force_offline=True)
    without_mapping = refine.refine(
        mapping.spine_to_cues(result.trace.spine),
        list(result.trace.words),
        cfg,
        result.trace.duration,
    )
    mae = evaluate.cue_start_mae(list(result.cues), without_mapping)
    assert mae < cfg.start_tol


def test_spine_is_immutable() -> None:
    segment = Segment(id=0, start=0.0, end=1.0, text="가")
    with pytest.raises(FrozenInstanceError):
        segment.text = "나"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Cue(start=0.0, end=1.0, lines=("가",)).start = 2.0  # type: ignore[misc]


def test_final_text_never_contains_whisper_only_wording() -> None:
    """불변 규칙 2 — 출력 문자열은 전부 gpt-transcribe 유래여야 한다."""
    result = pipeline.run(VIDEO, Config(), Path("/tmp/vlogsrt-invariant"), force_offline=True)
    body = " ".join(cue.text for cue in result.cues)
    for whisper_only in ("망언동", "블루팽귄"):
        assert whisper_only not in body


def test_offline_run_makes_no_network_call() -> None:
    """conftest가 소켓을 막아둔다. 여기까지 오면 픽스처만 쓴 것이다."""
    result = pipeline.run(VIDEO, Config(), Path("/tmp/vlogsrt-invariant"), force_offline=True)
    assert result.cues


def test_invariant_script_passes() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "check_invariants.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_only_transcribe_imports_openai() -> None:
    offenders = [
        path.name
        for path in (ROOT / "vlogsrt").glob("*.py")
        if path.name != "transcribe.py" and "import openai" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
