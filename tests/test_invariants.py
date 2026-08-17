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
from vlogsrt.models import Cue, Segment

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
ROOT = Path(__file__).resolve().parent.parent
VIDEO = Path("golden/demo.mp4")


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def test_draft_cue_times_come_straight_from_the_spine(tmp_path: Path) -> None:
    """규칙 1 — 정제 전 큐의 시각은 세그먼트 원본값과 정확히 같다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    draft = pipeline.spine_to_cues(list(result.trace.spine))
    assert [(cue.start, cue.end) for cue in draft] == [
        (segment.start, segment.end) for segment in result.trace.spine
    ]


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


def test_srt_text_is_exactly_the_spine_text(tmp_path: Path) -> None:
    """규칙 1 — 출력 글자 전부가 세그먼트에서 오고, 한 글자도 새지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = strip_for_compare(" ".join(cue.text for cue in result.cues))
    spine_text = strip_for_compare(" ".join(s.text for s in result.trace.spine))
    assert body == spine_text


def test_every_segment_reaches_the_srt(tmp_path: Path) -> None:
    """규칙 9 — 골격의 세그먼트는 하나도 빠짐없이 어떤 큐엔가 들어간다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    covered = {seg_id for cue in result.cues for seg_id in cue.seg_ids}
    assert covered == {segment.id for segment in result.trace.spine}


def test_every_word_the_model_said_reaches_the_srt(tmp_path: Path) -> None:
    """규칙 9 — 어절은 하나도 버리지 않는다. 배분에서도, 정제에서도(D24)."""
    from vlogsrt import transcribe as transcribe_module

    responses = transcribe_module.FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(
        transcribe_module.TranscriptionJob(stem="demo", chunks=()), Config()
    )
    said = [
        word
        for response in sorted(responses, key=lambda r: r.offset)
        for word in transcribe_module.parse_text(response.payload).split()
    ]
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    written = " ".join(cue.text for cue in result.cues).split()
    assert written == said


def test_the_first_and_last_seconds_survive(tmp_path: Path) -> None:
    """**초반·마지막이 날아간 사고의 회귀 테스트**(D25).

    0.0초에서 시작하는 세그먼트와 영상 끝에서 끝나는 세그먼트가 둘 다 자막으로 선다.
    구간을 골라내는 코드가 다시 생기면 여기서 걸린다(규칙 4).
    """
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, tmp_path, force_offline=True)
    assert result.cues[0].start <= cfg.lead_in + 1e-6
    assert result.cues[-1].end >= result.trace.duration - cfg.lead_out - 1e-6


def test_cues_never_overlap_and_never_exceed_capacity(tmp_path: Path) -> None:
    """0.77초에 900자 같은 큐 부류의 봉쇄. 이제 이걸 잡는 검사 단계는 없다 —
    정제가 구조적으로 보장하고, 그 보장을 여기서 못 박는다."""
    cfg = Config()
    result = pipeline.run(VIDEO, cfg, tmp_path, force_offline=True)
    for cue in result.cues:
        assert cue.end > cue.start
        assert cue.text.strip()
        assert len(cue.lines) <= cfg.max_lines
        if len(cue.text) > cfg.max_chars_per_cue:
            assert cue.flags, f"큐 {cue.index}가 표시 없이 용량을 넘었다"
    for previous, following in zip(result.cues, result.cues[1:], strict=False):
        assert following.start >= previous.end


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


def test_the_deleted_inspection_layers_stay_deleted() -> None:
    """규칙 4 — 검사·재검증 계층은 되살리지 않는다(D21)."""
    package = ROOT / "vlogsrt"
    for name in ("verify.py", "loops.py", "validate.py", "report.py", "mapping.py", "gapfill.py"):
        assert not (package / name).exists(), f"{name}이 부활했다"


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
