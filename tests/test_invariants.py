"""CLAUDE.md 불변 규칙을 코드로 못 박는다.

여기가 깨지면 설계가 무너진 것이다. 테스트를 고치지 말고 보고하라.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from vlogsrt import pipeline
from vlogsrt.config import Config
from vlogsrt.hangul import strip_for_compare
from vlogsrt.models import VERDICT_OK, Cue, Segment

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
ROOT = Path(__file__).resolve().parent.parent
VIDEO = Path("golden/demo.mp4")


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def test_draft_cue_times_come_straight_from_the_spine(tmp_path: Path) -> None:
    """규칙 1 — 정제 전 큐의 시각은 ok 세그먼트 원본값과 정확히 같다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    ok_segments = [s for s in result.trace.spine if s.verdict == VERDICT_OK]
    draft = pipeline.spine_to_cues(list(result.trace.spine))
    assert [(cue.start, cue.end) for cue in draft] == [(s.start, s.end) for s in ok_segments]


def test_refined_cue_times_stay_within_refine_bounds(tmp_path: Path) -> None:
    """규칙 3 — refine의 이동량은 lead_in/lead_out 한도 안이다."""
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, tmp_path, force_offline=True)
    segments = {segment.id: segment for segment in result.trace.spine}
    epsilon = 1e-6
    for cue in result.cues:
        assert cue.seg_ids, f"큐 {cue.index}가 seg_ids 없이 나왔다 — 역추적 불가(규칙 2)"
        first = segments[cue.seg_ids[0]]
        last = segments[cue.seg_ids[-1]]
        assert cue.start >= first.start - cfg.lead_in - epsilon
        assert cue.end <= last.end + cfg.lead_out + cfg.min_duration + epsilon


def test_srt_text_is_exactly_the_ok_spine_text(tmp_path: Path) -> None:
    """규칙 1 — 출력 글자 전부가 ok 세그먼트에서 오고, 한 글자도 새지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = strip_for_compare(" ".join(cue.text for cue in result.cues))
    spine_text = strip_for_compare(
        " ".join(s.text for s in result.trace.spine if s.verdict == VERDICT_OK)
    )
    assert body == spine_text


def test_verify_on_off_produces_identical_srt_bytes(tmp_path: Path) -> None:
    """규칙 4 — 검증을 껐다 켜도 SRT는 바이트 단위로 같다."""
    with_verify = pipeline.run(VIDEO, Config(), tmp_path / "on", force_offline=True)
    without = pipeline.run(
        VIDEO, Config(), tmp_path / "off", force_offline=True, stop_after="output"
    )
    assert with_verify.outputs["srt"].read_bytes() == without.outputs["srt"].read_bytes()


def test_spine_is_immutable() -> None:
    segment = Segment(id=0, start=0.0, end=1.0, text="가")
    with pytest.raises(FrozenInstanceError):
        segment.text = "나"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        Cue(start=0.0, end=1.0, lines=("가",)).start = 2.0  # type: ignore[misc]


def test_offline_run_makes_no_network_call(tmp_path: Path) -> None:
    """conftest가 소켓을 막아둔다. 여기까지 오면 픽스처만 쓴 것이다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
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
