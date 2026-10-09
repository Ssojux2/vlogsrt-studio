"""자막 시각 확인의 미디어 경로(D55) — 합성 톤 덩어리 MOV·가짜 에너지 VAD로 끝까지 돈다.

SRT는 읽기만 한다(바이트 그대로). 곁파일은 SRT 옆 하나뿐이고 `--no-write`면 그것도 없다.
무음이면 측정 불가(회색), 오디오 원점이 0이 아니면 기록하고, 원점을 빼먹은 SRT는 노랑이다.
저장 PCM이 원본과 어긋나면 표본 대조가 잡아 새로 디코드한다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from tests._sync_fakes import (
    bursts,
    energy_factory,
    install_fake_numpy,
    make_media,
    numpy_or_fake,
    srt_text,
)
from vlogsrt import cli, preprocess
from vlogsrt.project import sync_check
from vlogsrt.project.raw import sync_report

needs_media = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

pytestmark = needs_media


@pytest.fixture(scope="module")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    folder = tmp_path_factory.mktemp("sync_media")
    make_media(folder / "tone.mov")
    make_media(folder / "silent.mov", silent=True)
    make_media(folder / "delayed.mov", audio_delay="0.5")
    return folder


@pytest.fixture(autouse=True)
def fake_vad(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    numpy_or_fake(monkeypatch)  # VAD extra 없는 개발 환경에서도 같은 단언이 돈다


def files_under(folder: Path) -> set[Path]:
    return {path for path in folder.rglob("*") if path.is_file()}


def run_cli(*args: str) -> int:
    return cli.main(["project", "sync-check", *args])


def test_shifted_srt_is_red_aligned_is_green_and_srt_never_changes(
    media_dir: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    aligned = tmp_path / "aligned.srt"
    aligned.write_text(srt_text(bursts()), encoding="utf-8")
    early = tmp_path / "early.srt"
    early.write_text(srt_text(bursts("-2")), encoding="utf-8")
    hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in (aligned, early)}
    before = files_under(media_dir)
    assert run_cli("--media", str(media_dir / "tone.mov"), "--srt", str(aligned), "--json") == 0
    good = json.loads(capsys.readouterr().out)
    assert good["verdict"] == sync_check.LEVEL_GREEN, good["reasons"]
    assert run_cli("--media", str(media_dir / "tone.mov"), "--srt", str(early), "--json") == 0
    bad = json.loads(capsys.readouterr().out)
    assert bad["verdict"] == sync_check.LEVEL_RED
    offset = Fraction(bad["metrics"]["offset"]["seconds"])
    assert abs(offset - 2) <= Fraction(1, 10)  # + = 자막이 이르다
    assert {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes} == hashes
    assert files_under(tmp_path) == {
        aligned,
        early,
        sync_report.sidecar_path(aligned),
        sync_report.sidecar_path(early),
    }  # 곁파일 둘 말고는 아무것도 생기지 않는다
    assert files_under(media_dir) == before  # 원본 쪽에는 쓰지 않는다
    sidecar = json.loads(sync_report.sidecar_path(early).read_text(encoding="utf-8"))
    assert sidecar["verdict"] == "red" and sidecar["source"]["transcript_revision"] == hashes[early]


def test_no_write_writes_nothing(media_dir: Path, tmp_path: Path) -> None:
    srt = tmp_path / "cues.srt"
    srt.write_text(srt_text(bursts()), encoding="utf-8")
    before = files_under(tmp_path) | files_under(media_dir)
    args = ("--media", str(media_dir / "tone.mov"), "--srt", str(srt), "--no-write")
    assert run_cli(*args) == 0
    assert files_under(tmp_path) | files_under(media_dir) == before


def test_silent_audio_is_skip_never_green(media_dir: Path, tmp_path: Path) -> None:
    srt = tmp_path / "cues.srt"
    srt.write_text(srt_text(bursts()), encoding="utf-8")
    document = sync_report.measure_srt(media_dir / "silent.mov", srt, scratch=tmp_path / "s")
    assert document["verdict"] == sync_check.LEVEL_SKIP
    assert sync_report.badge(document["verdict"]) == ("측정 불가", "grey")


def test_nonzero_origin_is_recorded_and_audio_time_srt_is_flagged(
    media_dir: Path, tmp_path: Path
) -> None:
    """오디오가 0.5초 늦게 시작하는 원본 — 영상 시계 SRT는 초록, 오디오 표본 0 기준 SRT는 노랑."""
    video_time = tmp_path / "video.srt"
    video_time.write_text(srt_text(bursts("0.5")), encoding="utf-8")
    audio_time = tmp_path / "audio.srt"
    audio_time.write_text(srt_text(bursts()), encoding="utf-8")
    media = media_dir / "delayed.mov"
    good = sync_report.measure_srt(media, video_time, scratch=tmp_path / "a")
    assert Fraction(good["pcm"]["audio_start"]) > Fraction(4, 10)  # 잰 원점이 기록된다
    assert good["verdict"] == sync_check.LEVEL_GREEN, good["reasons"]
    flagged = sync_report.measure_srt(media, audio_time, scratch=tmp_path / "b")
    assert flagged["verdict"] == sync_check.LEVEL_AMBER
    assert "origin" in {reason["code"] for reason in flagged["reasons"]}


def test_window_decode_matches_full_extraction(media_dir: Path, tmp_path: Path) -> None:
    source = media_dir / "tone.mov"
    extraction = preprocess.extract_pcm_s16le(source, tmp_path / "full.pcm", stream_index=0)
    frame = 2 * extraction.stream.channels
    start = Fraction(10)
    stored = (tmp_path / "full.pcm").read_bytes()[160000 * frame : 480000 * frame]
    fresh = preprocess.pcm_window_s16le(
        source,
        stream_index=extraction.stream.stream_index,
        start=extraction.pcm_sample0_file_time + start,
        seconds=Fraction(20),
        channels=extraction.stream.channels,
    )
    hop = 160
    reference = sync_check.pcm_envelope(stored, extraction.stream.channels, hop)
    probe = sync_check.pcm_envelope(fresh, extraction.stream.channels, hop)
    lag, score = sync_check.envelope_alignment(reference, probe, 100)
    assert lag == 0 and score > 0.99


def stage1_record(
    folder: Path, media: Path, *, shift_samples: int = 0, failed: bool = False
) -> sync_report.Stage1Audio:
    """1단계 기록 흉내 — 원본을 디코드해 저장 PCM을 두고(밀 수 있다) asr_phase.json을 쓴다."""
    folder.mkdir(parents=True, exist_ok=True)
    extraction = preprocess.extract_pcm_s16le(media, folder / "fresh.pcm", stream_index=0)
    data = (folder / "fresh.pcm").read_bytes()
    frame = 2 * extraction.stream.channels
    if shift_samples:
        data = (b"\x00" * shift_samples * frame + data)[: len(data)]
    (folder / "analysis.pcm").write_bytes(data)
    (folder / "fresh.pcm").unlink()
    chunk = {"chunk_index": 0, "sample_start": 0, "sample_end": extraction.sample_count}
    body = {
        "source_sha256": "f" * 64,
        "created_at": "2026-10-09T00:00:00+00:00",
        "video": {"presentation_origin": {"num": 0, "den": 1}},
        "audio": {
            "pcm_sha256": hashlib.sha256(data).hexdigest(),
            "audio_stream_index": extraction.stream.stream_index,
            "sample_rate": 16000,
            "channels": extraction.stream.channels,
            "sample_count": extraction.sample_count,
            "pcm_sample0_file_time": sync_check.fraction_text(extraction.pcm_sample0_file_time),
        },
        "asr": {"chunks": [{**chunk, "failure": "Malformed: 넘침" if failed else ""}]},
    }
    (folder / "asr_phase.json").write_text(json.dumps(body), encoding="utf-8")
    found = sync_report.stage1_audio(folder.parents[2], "src", "f" * 64)
    assert found is not None
    return found


def source_of(media: Path, stage1: sync_report.Stage1Audio | None) -> sync_report.SyncSource:
    cues = tuple(
        sync_check.Cue(f"u{i}", start, end, "말") for i, (start, end) in enumerate(bursts())
    )
    return sync_report.SyncSource(
        source_id="src",
        source_name=media.name,
        source_sha256="f" * 64,
        original=media,
        original_note="",
        transcript_revision="rev-1",
        cues=cues,
        stage1=stage1,
    )


def test_stored_pcm_is_used_when_it_matches_the_original(media_dir: Path, tmp_path: Path) -> None:
    stage1 = stage1_record(tmp_path / "analysis" / "anal_1" / "000001_src", media_dir / "tone.mov")
    document = sync_report.measure_source(
        source_of(media_dir / "tone.mov", stage1), cache_root=tmp_path / "cache"
    )
    assert document["pcm"]["basis"] == "stored"
    assert document["verdict"] == sync_check.LEVEL_GREEN, document["reasons"]
    spot = next(c for c in document["checks"] if c["name"] == "저장 PCM ↔ 원본 표본")
    assert spot["status"] == sync_check.STATUS_PASS
    origin = next(c for c in document["checks"] if c["name"] == "오디오 원점")
    assert origin["status"] == sync_check.STATUS_INFO  # 대조할 다른 기록이 없으면 통과가 아니다


def test_shifted_stored_pcm_fails_spot_check_and_is_decoded_fresh(
    media_dir: Path, tmp_path: Path
) -> None:
    """저장 PCM이 원본보다 0.5초 밀렸다 — 표본 대조가 잡고 원본을 새로 디코드해 잰다."""
    stage1 = stage1_record(
        tmp_path / "analysis" / "anal_1" / "000001_src",
        media_dir / "tone.mov",
        shift_samples=8000,
        failed=True,
    )
    document = sync_report.measure_source(
        source_of(media_dir / "tone.mov", stage1), cache_root=tmp_path / "cache"
    )
    assert document["pcm"]["basis"] == "decoded"
    spot = next(c for c in document["checks"] if c["name"] == "저장 PCM ↔ 원본 표본")
    assert spot["status"] == sync_check.STATUS_WARN and "새로 디코드" in spot["detail"]
    assert document["verdict"] == sync_check.LEVEL_GREEN, document["reasons"]
    assert document["failed_chunks"] and "failed_chunks" in {r["code"] for r in document["reasons"]}


def test_measurement_runs_without_real_numpy(
    media_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """numpy가 없는 개발 환경 — 최소 가짜 numpy로도 같은 판정이 나온다(extra에 묶이지 않는다)."""
    install_fake_numpy(monkeypatch)
    srt = tmp_path / "cues.srt"
    srt.write_text(srt_text(bursts("-2")), encoding="utf-8")
    document = sync_report.measure_srt(media_dir / "tone.mov", srt, scratch=tmp_path / "s")
    assert document["verdict"] == sync_check.LEVEL_RED
    assert abs(Fraction(document["metrics"]["offset"]["seconds"]) - 2) <= Fraction(1, 10)
