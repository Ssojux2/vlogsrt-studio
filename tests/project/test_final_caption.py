from __future__ import annotations

import json
import wave
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import pipeline, transcribe
from vlogsrt.config import Config
from vlogsrt.models import Cue, Trace
from vlogsrt.project.final_caption import (
    CaptionGenerationError,
    create_caption_request,
    generate_final_captions,
    load_caption_origin,
    validate_caption_request,
)


def _wav(path: Path, *, rate: int = 48_000, frames: int = 4_800) -> Path:
    with wave.open(str(path), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(rate)
        stream.writeframes(b"\0\0" * frames)
    return path


def _fcpxml(path: Path, *, name: str = "My Vlog", uid: str = "") -> Path:
    uid_attribute = f' uid="{uid}"' if uid else ""
    path.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources/>'
        f'<library><event name="Event"><project name="{name}"{uid_attribute}>'
        '<sequence duration="1s" '
        'tcStart="0s"><spine/></sequence></project></event></library></fcpxml>\n',
        encoding="utf-8",
    )
    return path


def _successful_run(calls: list[dict[str, object]]):
    def run(audio: Path, cfg: Config, outdir: Path, **kwargs: object) -> pipeline.RunResult:
        calls.append({"audio": audio, "outdir": outdir, **kwargs})
        outdir.mkdir(parents=True, exist_ok=True)
        srt = outdir / "mix.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,900\n안녕하세요\n", encoding="utf-8")
        cue = Cue(0.0, 0.9, ("안녕하세요",), seg_ids=(0,), index=1)
        return pipeline.RunResult(
            trace=Trace(source=str(audio), duration=1.0, spine=(), cues=(cue,)),
            cues=(cue,),
            outputs={"srt": srt},
        )

    return run


def test_final_caption_uses_checked_pipeline_and_revision_filename(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """최종 공유 오디오가 컷 분석용 전사를 재투영하거나 coverage 검사를 빼는 회귀를 잡는다."""
    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    xml = _fcpxml(tmp_path / "final.fcpxml")
    request = create_caption_request("a" * 64, audio, xml)
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(pipeline, "run", _successful_run(calls))

    delivery = generate_final_captions(
        request,
        Config(),
        tmp_path / "out",
        online=False,
        confirmed_cost=False,
    )

    expected = tmp_path / "out" / f"My Vlog.{request.final_revision}.ko.srt"
    assert delivery.result.srt_path == expected
    assert expected.read_text(encoding="utf-8").endswith("안녕하세요\n")
    assert delivery.result.speech_status == "speech"
    assert "coverage:complete" in delivery.result.quality
    assert calls[0]["audio"] == audio
    assert calls[0]["require_complete_coverage"] is True
    review = json.loads(delivery.review_report.read_text(encoding="utf-8"))
    assert review["final_revision"] == request.final_revision
    assert review["timeline_approval"] == "custom_share_destination"
    assert review["caption_import"] == {
        "command": "File > Import > Captions",
        "placement": "Relative to Timeline",
    }


def test_explicit_empty_coverage_reports_no_speech_without_creating_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    request = create_caption_request("a" * 64, audio, _fcpxml(tmp_path / "final.fcpxml"))

    def no_speech(*args: object, **kwargs: object) -> pipeline.RunResult:
        raise transcribe.ConfirmedNoSpeech("모든 조각이 명시적으로 비었다")

    monkeypatch.setattr(pipeline, "run", no_speech)
    delivery = generate_final_captions(
        request, Config(), tmp_path / "out", online=False, confirmed_cost=False
    )
    assert delivery.result.speech_status == "no_speech"
    assert delivery.result.srt_path is None
    assert list((tmp_path / "out").glob("*.srt")) == []


def test_zero_byte_srt_is_never_reported_as_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    request = create_caption_request("a" * 64, audio, _fcpxml(tmp_path / "final.fcpxml"))

    def empty_file(audio: Path, cfg: Config, outdir: Path, **kwargs: object) -> pipeline.RunResult:
        outdir.mkdir(parents=True, exist_ok=True)
        srt = outdir / "mix.srt"
        srt.write_bytes(b"")
        cue = Cue(0.0, 0.9, ("안녕",), seg_ids=(0,), index=1)
        return pipeline.RunResult(
            trace=Trace(source=str(audio), duration=1.0, spine=(), cues=(cue,)),
            cues=(cue,),
            outputs={"srt": srt},
        )

    monkeypatch.setattr(pipeline, "run", empty_file)
    with pytest.raises(CaptionGenerationError, match="0바이트"):
        generate_final_captions(
            request, Config(), tmp_path / "out", online=False, confirmed_cost=False
        )


def test_caption_rejects_non_48khz_pcm_and_changed_fcpxml(tmp_path: Path) -> None:
    bad_audio = _wav(tmp_path / "bad.wav", rate=44_100)
    xml = _fcpxml(tmp_path / "final.fcpxml")
    request = create_caption_request("a" * 64, bad_audio, xml)
    with pytest.raises(CaptionGenerationError, match="48000Hz PCM"):
        generate_final_captions(
            request, Config(), tmp_path / "out", online=False, confirmed_cost=False
        )

    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    request = create_caption_request("a" * 64, audio, xml)
    _fcpxml(xml, name="Changed")
    with pytest.raises(CaptionGenerationError, match="리비전"):
        generate_final_captions(
            request, Config(), tmp_path / "out2", online=False, confirmed_cost=False
        )


def test_online_caption_requires_cost_confirmation_before_pipeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request = create_caption_request(
        "a" * 64,
        _wav(tmp_path / "mix.wav", frames=48_000),
        _fcpxml(tmp_path / "final.fcpxml"),
    )
    monkeypatch.setattr(
        pipeline, "run", lambda *args, **kwargs: pytest.fail("확인 전에 파이프라인이 열렸다")
    )
    with pytest.raises(CaptionGenerationError, match="예상 비용"):
        generate_final_captions(
            request, Config(), tmp_path / "out", online=True, confirmed_cost=False
        )


def test_caption_request_binds_audio_bytes_and_timeline_duration(tmp_path: Path) -> None:
    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    xml = _fcpxml(tmp_path / "final.fcpxml")
    request = create_caption_request("a" * 64, audio, xml)
    with audio.open("r+b") as stream:
        stream.seek(-2, 2)
        stream.write(b"\1\0")
    with pytest.raises(CaptionGenerationError, match="오디오 해시"):
        generate_final_captions(
            request, Config(), tmp_path / "changed", online=False, confirmed_cost=False
        )

    short = _wav(tmp_path / "short.wav", frames=24_000)
    mismatch = create_caption_request("a" * 64, short, xml)
    with pytest.raises(CaptionGenerationError, match="길이"):
        generate_final_captions(
            mismatch, Config(), tmp_path / "mismatch", online=False, confirmed_cost=False
        )


def test_recorded_offline_fixtures_work_with_checked_caption_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fixtures = Path(__file__).parents[1] / "fixtures" / "synthetic"
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(fixtures))
    audio = _wav(tmp_path / "demo.wav", frames=48_000 * 24)
    xml = tmp_path / "final.fcpxml"
    xml.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources/><library><event name="E">'
        '<project name="Demo"><sequence duration="24s" tcStart="0s">'
        "<spine/></sequence></project></event></library></fcpxml>\n",
        encoding="utf-8",
    )
    request = create_caption_request("a" * 64, audio, xml)
    delivery = generate_final_captions(
        request, Config(), tmp_path / "out", online=False, confirmed_cost=False
    )
    assert delivery.result.speech_status == "speech"
    assert delivery.result.srt_path is not None and delivery.result.srt_path.stat().st_size > 0


# ─────────────────────────────────────────────────────────────
#  STAB-S4A — 원본 매핑 검증, 엔진 안정 이름, 전사기 주입, 경고 전달
# ─────────────────────────────────────────────────────────────


def test_validate_caption_request_binds_origin_mapping_and_duration(tmp_path: Path) -> None:
    """원본 문서가 없으면 실패고, UID·리비전·길이가 어긋나면 같은 문에서 닫힌다(S4A)."""
    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    xml = _fcpxml(tmp_path / "final.fcpxml", uid="UID-1")
    origin_path = tmp_path / "caption-origin.json"

    with pytest.raises(CaptionGenerationError, match="원본 매핑 문서가 없다"):
        load_caption_origin(origin_path)

    origin_path.write_text(
        json.dumps(
            {"origin_job_id": "roughcut-1", "origin_revision": "a" * 64, "project_uid": "UID-1"}
        ),
        encoding="utf-8",
    )
    origin = load_caption_origin(origin_path)
    request = create_caption_request(origin.origin_revision, audio, xml)
    validated = validate_caption_request(request, origin)
    assert validated.audio_seconds == Fraction(1)
    assert validated.project_uid == "UID-1"
    assert validated.project_name == "My Vlog"

    # 결과 프로젝트 UID가 매핑과 다르면 남의 타임라인에 자막을 얹을 뻔한 것이다.
    wrong_uid = tmp_path / "wrong-uid.json"
    wrong_uid.write_text(
        json.dumps(
            {"origin_job_id": "roughcut-1", "origin_revision": "a" * 64, "project_uid": "UID-2"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(CaptionGenerationError, match="프로젝트 UID"):
        validate_caption_request(request, load_caption_origin(wrong_uid))

    # 요청의 원본 리비전이 매핑 문서와 다르면 거부다.
    mismatched = create_caption_request("e" * 64, audio, xml)
    with pytest.raises(CaptionGenerationError, match="원본 리비전"):
        validate_caption_request(mismatched, origin)

    # 길이 대응도 같은 문에서 잰다 — 1초 오디오에 2초 타임라인은 매핑할 수 없다.
    long_xml = tmp_path / "long.fcpxml"
    long_xml.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE fcpxml>\n'
        '<fcpxml version="1.14"><resources/><library><event name="E">'
        '<project name="My Vlog" uid="UID-1"><sequence duration="2s" tcStart="0s">'
        "<spine/></sequence></project></event></library></fcpxml>\n",
        encoding="utf-8",
    )
    long_request = create_caption_request("a" * 64, audio, long_xml)
    with pytest.raises(CaptionGenerationError, match="길이가 다르다"):
        validate_caption_request(long_request, origin)


def test_engine_stem_and_injected_transcriber_shape_the_app_visible_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """엔진 호출의 안정 이름과 주입된 전사기가 그대로 산물을 결정한다(S4A)."""
    audio = _wav(tmp_path / "mix.wav", frames=48_000)
    xml = _fcpxml(tmp_path / "final.fcpxml")
    request = create_caption_request("a" * 64, audio, xml)
    calls: list[dict[str, object]] = []

    class _Injected:
        def transcribe(self, job: object, cfg: Config) -> list[object]:
            raise AssertionError("이 가짜는 인자만 확인한다 — 실제 호출 경로가 아니다")

    injected = _Injected()

    def run(audio: Path, cfg: Config, outdir: Path, **kwargs: object) -> pipeline.RunResult:
        calls.append({"audio": audio, **kwargs})
        outdir.mkdir(parents=True, exist_ok=True)
        srt = outdir / "mix.srt"
        srt.write_text("1\n00:00:00,000 --> 00:00:00,900\n안녕하세요\n", encoding="utf-8")
        cue = Cue(0.0, 0.9, ("안녕하세요",), seg_ids=(0,), index=1)
        return pipeline.RunResult(
            trace=Trace(source=str(audio), duration=1.0, spine=(), cues=(cue,)),
            cues=(cue,),
            outputs={"srt": srt},
        )

    monkeypatch.setattr(pipeline, "run", run)
    delivery = generate_final_captions(
        request,
        Config(),
        tmp_path / "out",
        online=False,
        confirmed_cost=False,
        transcriber=injected,
        srt_stem="My Vlog.ko",
    )
    # 주입된 전사기가 파이프라인에 그대로 흘러간다 — 엔진이 취소 게이트·회계를 붙인 위임물이다.
    assert calls[0]["transcriber"] is injected
    assert delivery.result.srt_path == tmp_path / "out" / "My Vlog.ko.srt"
    assert (tmp_path / "out" / "My Vlog.ko.review.json").is_file()


def test_caption_result_carries_pipeline_warnings_next_to_the_srt() -> None:
    """되풀이 재전사·자리 없는 교정 글자·검증 의심이 결과 경고로 나간다(S4A)."""
    from vlogsrt import retext
    from vlogsrt.models import Trace
    from vlogsrt.project.final_caption import _caption_warnings

    class _Record:
        status = retext.RETEXT_APPLIED
        unplaced = (type("Row", (), {"chars": 7})(),)

    class _Verification:
        suspects = ("cue-1", "cue-2")

    class _Run:
        repairs = ("repair-1",)
        trace = Trace(source="x", duration=1.0, spine=(), cues=(), retext=_Record())
        verification = _Verification()

    warnings = _caption_warnings(_Run())
    assert any("재전사 1" in row for row in warnings)
    assert any("7자" in row for row in warnings)
    assert any("의심 큐 2건" in row for row in warnings)

    # 교정이 적용되지 않으면 그 사실 자체가 경고다 — whisper 글자로 나갔다는 뜻이다.
    class _Skipped:
        status = retext.RETEXT_OFF
        unplaced = ()

    class _PlainRun:
        repairs = ()
        trace = Trace(source="x", duration=1.0, spine=(), cues=(), retext=_Skipped())
        verification = None

    warnings = _caption_warnings(_PlainRun())
    assert warnings and any("whisper 글자" in row for row in warnings)
