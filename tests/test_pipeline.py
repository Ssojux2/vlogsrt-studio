"""파이프라인 전체. 합성 픽스처만 쓰고 네트워크는 건드리지 않는다."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vlogsrt import TRACE_VERSION, pipeline
from vlogsrt.cli import main
from vlogsrt.config import Config
from vlogsrt.models import Chunk, ChunkMark, Trace
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

    srt = parse_srt(result.outputs["srt"].read_text(encoding="utf-8"))
    assert len(srt) == len(result.cues)
    assert [cue.index for cue in srt] == list(range(1, len(srt) + 1))


def test_output_text_comes_straight_from_the_transcription(tmp_path: Path) -> None:
    """규칙 1 — 모든 글자는 모델 응답에서 온다. 아무도 고쳐 쓰지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(cue.text for cue in result.cues)
    assert "망원동" in body
    assert "블루펭귄" in body


def test_every_segment_becomes_a_cue(tmp_path: Path) -> None:
    """규칙 9 — 배분된 세그먼트는 하나도 빠짐없이 큐가 된다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    covered = {seg_id for cue in result.cues for seg_id in cue.seg_ids}
    assert covered == {segment.id for segment in result.trace.spine}


def test_chunk_offsets_survive_into_the_srt(tmp_path: Path) -> None:
    """둘째 조각의 말은 그 조각이 시작하는 시각 뒤에 자막으로 선다."""
    from vlogsrt import transcribe as transcribe_module

    responses = transcribe_module.FixtureTranscriber(fixture_dir=SYNTHETIC).transcribe(
        transcribe_module.TranscriptionJob(stem="demo", chunks=()), Config()
    )
    second = next(response.offset for response in responses if response.index == 1)

    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    late = [cue for cue in result.cues if "구독" in cue.text]
    assert late and late[0].start >= second - Config().lead_in


def test_trace_carries_the_new_version(tmp_path: Path) -> None:
    pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    trace = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert trace["version"] == TRACE_VERSION
    # 길이 0 세그먼트도 글자가 있으면 골격에 남는다(규칙 9). 뒤집히지만 않으면 된다.
    assert all(segment["start"] <= segment["end"] for segment in trace["spine"])


def test_trace_records_where_the_audio_was_cut(tmp_path: Path) -> None:
    """조각 경계는 **기록으로 남아야 한다.**

    조각 mp3는 캐시를 지우면 사라지는데, 조각 **안 위치**로 무언가를 재려면 경계가
    있어야 한다. 그게 없어서 "조각 끝에서 싱크가 무너진다"는 신고를 되짚을 수 없었다(D32).
    """
    chunks = (
        Chunk(path=tmp_path / "a.mp3", offset=0.0, duration=180.0, index=0),
        Chunk(path=tmp_path / "b.mp3", offset=180.0, duration=120.0, index=1),
    )
    trace = Trace(
        source="x.mp4",
        duration=300.0,
        spine=(),
        chunks=tuple(ChunkMark.of(chunk) for chunk in chunks),
    )
    restored = Trace.load(trace.save(tmp_path / "trace.json"))

    assert [mark.offset for mark in restored.chunks] == [0.0, 180.0]
    assert [mark.duration for mark in restored.chunks] == [180.0, 120.0]
    assert restored.chunks[-1].end == 300.0
    # 경로는 넣지 않는다 — 직렬화도 안 되고 되짚는 데 필요도 없다.
    assert "path" not in json.loads((tmp_path / "trace.json").read_text("utf-8"))["chunks"][0]


def test_a_trace_without_chunk_marks_still_loads(tmp_path: Path) -> None:
    """조각 경계는 기록일 뿐이다. 없다고 `redo`가 막히면 안 된다."""
    pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = json.loads((tmp_path / "trace.json").read_text("utf-8"))
    assert body["chunks"] == [], "메타 픽스처만 쓴 실행에는 조각이 없다"
    del body["chunks"]
    (tmp_path / "old.json").write_text(json.dumps(body), encoding="utf-8")

    assert Trace.load(tmp_path / "old.json").chunks == ()


def test_redo_replays_without_fixtures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "no-such-dir"))
    narrow = Config(max_chars_per_line=12)
    result = pipeline.redo(tmp_path / "trace.json", narrow, tmp_path / "redo")
    assert result.cues
    assert all(len(line) <= 14 for cue in result.cues for line in cue.lines)


def test_old_traces_fail_loudly(tmp_path: Path) -> None:
    (tmp_path / "trace.json").write_text('{"version": "3.0", "spine": []}', encoding="utf-8")
    with pytest.raises(RuntimeError, match="trace 버전"):
        pipeline.redo(tmp_path / "trace.json", Config(), tmp_path / "out")


def test_missing_fixture_tells_the_human_what_to_run(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "empty"))
    with pytest.raises(RuntimeError, match="vlogsrt record"):
        pipeline.run(Path("golden/absent.mp4"), Config(), tmp_path, force_offline=True)


def test_a_response_without_segment_times_stops_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """시각을 주지 않는 모델로는 SRT를 만들 수 없다(D25)."""
    from vlogsrt import transcribe as transcribe_module

    def wrong_shape(self: object, job: object, cfg: object) -> list[object]:
        return [transcribe_module.ChunkResponse(0, 0.0, {"text": "시각이 없는 덩어리"})]

    monkeypatch.setattr(transcribe_module.FixtureTranscriber, "transcribe", wrong_shape)
    with pytest.raises(RuntimeError, match="transcribe_model"):
        pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)


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
    """이미 받아둔 응답이 있으면 다시 결제하지 않는다. 77분이면 한 번에 약 $0.46이다."""
    fixtures = tmp_path / "api"
    fixtures.mkdir()
    (fixtures / "demo.whisper.json").write_text('{"segments": []}', encoding="utf-8")
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(fixtures))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        pipeline,
        "prepare",
        lambda video, cfg, **kwargs: pipeline.Prepared("demo", 10.0, (), str(video)),
    )

    called: list[str] = []

    class Counting:
        def transcribe(self, job: object, cfg: object) -> list[object]:
            called.append("transcribe")
            return []

    monkeypatch.setattr(pipeline, "LiveTranscriber", lambda **kwargs: Counting())
    outputs = pipeline.record_fixtures(Path("demo.mp4"), Config(), verify=False)

    assert called == []
    assert outputs["whisper"].name == "demo.whisper.json"
    assert outputs["meta"].is_file()


def test_record_pays_for_both_models_by_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """검증이 기본이므로 녹화도 두 종류를 받는다. 없는 쪽만 새로 부른다."""
    fixtures = tmp_path / "api"
    fixtures.mkdir()
    (fixtures / "demo.whisper.json").write_text('{"segments": []}', encoding="utf-8")
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(fixtures))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setattr(
        pipeline,
        "prepare",
        lambda video, cfg, **kwargs: pipeline.Prepared("demo", 10.0, (), str(video)),
    )
    asked: list[str] = []

    class Counting:
        def transcribe(self, job: object, cfg: object) -> list[object]:
            asked.append(getattr(job, "kind", "?"))
            return []

    monkeypatch.setattr(pipeline, "LiveTranscriber", lambda **kwargs: Counting())
    outputs = pipeline.record_fixtures(Path("demo.mp4"), Config())

    assert asked == ["gpt"]  # whisper는 이미 결제된 것이 있다
    assert set(outputs) == {"whisper", "gpt", "meta"}


# ─────────────────────────────────────────────────────────────
#  기록을 남기지 않는 실행
# ─────────────────────────────────────────────────────────────


def test_artifacts_off_writes_only_the_srt(tmp_path: Path) -> None:
    """SRT 하나만 남긴다. trace도 output.srt 별칭도 만들지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, artifacts=False)
    assert result.cues
    assert result.outputs["srt"].is_file()
    assert set(result.outputs) == {"srt"}
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.srt"]


def test_artifacts_on_still_writes_everything(tmp_path: Path) -> None:
    pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    produced = {path.name for path in tmp_path.iterdir()}
    assert {"trace.json", "demo.srt", "output.srt"} == produced


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
    monkeypatch.setattr(
        pipeline.preprocess, "make_chunks", lambda audio, duration, cfg, workdir: []
    )

    work = tmp_path / "scratch"
    for _ in range(2):
        pipeline.prepare(video, Config(), offline=False, workdir=work, reuse_cache=False)
    assert len(calls) == 2  # 두 번 다 새로 뽑았다
    assert [path.name for path in work.iterdir()] == ["demo.mp3"]
