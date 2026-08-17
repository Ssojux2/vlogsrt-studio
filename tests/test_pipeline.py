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

    srt = parse_srt(result.outputs["srt"].read_text(encoding="utf-8"))
    assert len(srt) == len(result.cues)
    assert [cue.index for cue in srt] == list(range(1, len(srt) + 1))


def test_output_text_comes_straight_from_whisper(tmp_path: Path) -> None:
    """불변 규칙 1 — 모든 글자는 whisper 세그먼트에서 온다. 오타까지 그대로다.

    whisper의 오기("망언동")를 gpt가 바로잡는 일은 없다 — gpt는 검증 플래그만 낸다.
    """
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(cue.text for cue in result.cues)
    assert "망언동" in body and "망원동" not in body
    assert "블루팽귄" in body and "블루펭귄" not in body


def test_hallucinated_segment_is_dropped(tmp_path: Path) -> None:
    """환청 세그먼트는 큐가 되지 않는다. 그 시간대는 정직한 공백이다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    verdicts = {segment.id: segment.verdict for segment in result.trace.spine}
    assert verdicts[6] == "compression_fail"
    body = " ".join(cue.text for cue in result.cues)
    assert "감사합니다 감사합니다" not in body
    assert all(6 not in cue.seg_ids for cue in result.cues)


def test_full_run_returns_a_verification(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert result.verification is not None
    assert result.verification.cue_total == len(result.cues)


def test_stop_after_output_never_calls_gpt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`--stop-after output` = whisper 비용만. gpt 호출이 한 번이라도 있으면 실패다."""
    from vlogsrt import transcribe as transcribe_module

    calls: list[str] = []
    original = transcribe_module.FixtureTranscriber.gpt

    def counting(self: object, job: object, cfg: object) -> list[object]:
        calls.append("gpt")
        return original(self, job, cfg)  # type: ignore[arg-type]

    monkeypatch.setattr(transcribe_module.FixtureTranscriber, "gpt", counting)
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, stop_after="output")
    assert calls == []
    assert result.verification is None
    assert result.outputs["srt"].is_file()


def test_missing_gpt_fixture_still_writes_the_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """불변 규칙 4 — 검증 실패는 SRT 산출을 막지 못한다."""
    from vlogsrt import transcribe as transcribe_module

    def broken(self: object, job: object, cfg: object) -> list[object]:
        raise RuntimeError("픽스처가 없습니다")

    monkeypatch.setattr(transcribe_module.FixtureTranscriber, "gpt", broken)
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert result.outputs["srt"].is_file()
    assert result.cues
    assert result.verification is None


def test_stop_after_spine_writes_trace_only(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, stop_after="spine")
    trace = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert trace["version"] == "2.0"
    assert len(trace["spine"]) > 0
    assert all(segment["start"] < segment["end"] for segment in trace["spine"])
    assert not (tmp_path / "output.srt").exists()
    assert result.cues == ()


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


def test_record_reuses_what_was_already_paid_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """한쪽 모델만 실패했을 때, 재시도가 성공한 쪽까지 다시 결제하면 안 된다."""
    from vlogsrt import transcribe as transcribe_module

    fixtures = tmp_path / "api"
    fixtures.mkdir()
    (fixtures / "demo.whisper.json").write_text('{"segments": [], "words": []}', encoding="utf-8")
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(fixtures))
    monkeypatch.setattr(
        pipeline,
        "prepare",
        lambda video, cfg, **kwargs: pipeline.Prepared("demo", 10.0, (), (), str(video)),
    )
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")

    called: list[str] = []

    class OnlyGpt:
        def whisper(self, job: object, cfg: object) -> list[object]:
            called.append("whisper")
            return []

        def gpt(self, job: object, cfg: object) -> list[object]:
            called.append("gpt")
            return [transcribe_module.ChunkResponse(0, 0.0, {"text": "가"})]

    monkeypatch.setattr(pipeline, "LiveTranscriber", lambda **kwargs: OnlyGpt())
    outputs = pipeline.record_fixtures(Path("demo.mp4"), Config())

    assert called == ["gpt"]  # whisper는 다시 부르지 않았다
    assert outputs["whisper"].name == "demo.whisper.json"
    assert outputs["gpt"].is_file()


# ─────────────────────────────────────────────────────────────
#  기록을 남기지 않는 실행
# ─────────────────────────────────────────────────────────────


def test_artifacts_off_writes_only_the_srt(tmp_path: Path) -> None:
    """SRT 하나만 남긴다. trace·리포트·검토본·output.srt 별칭은 만들지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, artifacts=False)
    assert result.cues
    assert result.outputs["srt"].is_file()
    assert set(result.outputs) == {"srt"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.srt"]


def test_artifacts_on_still_writes_everything(tmp_path: Path) -> None:
    pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    produced = {path.name for path in tmp_path.iterdir()}
    assert {"trace.json", "demo.srt", "output.srt", "demo.report.md"} <= produced


def test_no_cache_run_reuses_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`reuse_cache=False`면 지문 폴더도 만들지 않고 오디오를 매번 새로 뽑는다."""
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "no-meta"))
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"\x00")
    calls: list[Path] = []

    def fake_extract(src: Path, dst: Path, cfg: Config) -> Path:
        calls.append(dst)
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"\x00")
        return dst

    monkeypatch.setattr(pipeline.preprocess, "extract_audio", fake_extract)
    monkeypatch.setattr(pipeline.preprocess, "probe_duration", lambda path: 30.0)
    monkeypatch.setattr(pipeline.preprocess, "detect_silence", lambda *a, **k: [])
    monkeypatch.setattr(
        pipeline.preprocess,
        "make_chunks",
        lambda audio, cfg, workdir, silences=None: [],
    )

    work = tmp_path / "scratch"
    for _ in range(2):
        pipeline.prepare(video, Config(), offline=False, workdir=work, reuse_cache=False)
    assert len(calls) == 2  # 두 번 다 새로 뽑았다
    assert not (work / "silences.json").exists()  # 캐시 파일을 남기지 않는다
    assert [path.name for path in work.iterdir()] == ["demo.mp3"]
