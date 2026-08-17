"""파이프라인 전체. 합성 픽스처만 쓰고 네트워크는 건드리지 않는다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vlogsrt import pipeline
from vlogsrt.cli import main
from vlogsrt.config import Config
from vlogsrt.output import parse_srt

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
VIDEO = Path("golden/demo.mp4")


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def test_run_end_to_end(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert result.cues
    assert result.outputs["srt"].is_file()
    assert result.outputs["report"].is_file()
    assert (tmp_path / "output.review.srt").is_file()

    srt = parse_srt(result.outputs["srt"].read_text(encoding="utf-8"))
    assert len(srt) == len(result.cues)
    assert [cue.index for cue in srt] == list(range(1, len(srt) + 1))


def test_output_text_comes_from_gpt_not_whisper(tmp_path: Path) -> None:
    """불변 규칙 2 — 최종 SRT에 whisper 텍스트가 들어가면 안 된다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(cue.text for cue in result.cues)
    assert "망원동" in body and "망언동" not in body
    assert "블루펭귄" in body and "블루팽귄" not in body


def test_hallucinated_segment_is_excluded_but_its_text_survives(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    verdicts = {segment.id: segment.verdict for segment in result.trace.spine}
    assert verdicts[6] == "compression_fail"
    body = " ".join(cue.text for cue in result.cues)
    assert "감사합니다 감사합니다" not in body
    assert "분위기" in body


def test_stop_after_spine_writes_trace_only(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, stop_after="spine")
    trace = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert trace["version"] == "2.0"
    assert len(trace["spine"]) > 0
    assert all(segment["start"] < segment["end"] for segment in trace["spine"])
    assert not (tmp_path / "output.srt").exists()
    assert result.cues == ()


def test_baseline_uses_whisper_text(tmp_path: Path) -> None:
    result = pipeline.baseline(VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(cue.text for cue in result.cues)
    assert "망언동" in body  # B0는 whisper 그대로다


def test_redo_replays_without_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "no-such-dir"))
    narrow = Config(max_chars_per_line=12)
    result = pipeline.redo(tmp_path / "trace.json", narrow, tmp_path / "redo")
    assert result.cues
    assert all(len(line) <= 14 for cue in result.cues for line in cue.lines)


def test_fast_mode_drops_word_timestamps(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, fast=True)
    assert result.trace.words == ()
    assert result.cues


def test_missing_fixture_tells_the_human_what_to_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="vlogsrt record"):
        pipeline.run(Path("golden/absent.mp4"), Config(), tmp_path, force_offline=True)


def test_cli_run_and_eval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(VIDEO), "--offline", "-o", str(tmp_path)]) == 0
    produced = tmp_path / "demo.srt"
    assert produced.is_file()
    assert main(["eval", str(produced), str(produced)]) == 0
    output = capsys.readouterr().out
    assert "CER" in output and "0.0%" in output


def test_cli_config_show_respects_preset(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["config", "show", "--preset", "shorts"]) == 0
    assert "max_chars_per_line = 12" in capsys.readouterr().out
    assert main(["config", "show", "--preset", "shorts", "--max-chars", "14"]) == 0
    assert "max_chars_per_line = 14" in capsys.readouterr().out


def test_cli_reports_errors_without_a_traceback(tmp_path: Path) -> None:
    assert main(["redo", str(tmp_path / "missing.json"), "-o", str(tmp_path)]) == 1


def test_cli_rejects_offline_and_online_together(tmp_path: Path) -> None:
    assert main(["run", str(VIDEO), "--offline", "--online", "-o", str(tmp_path)]) == 1
