"""`scripts/verify_raw_media.py`의 종료 코드 계약 — 거짓 초록이 없는가.

이 진단 도구는 인수 검증의 마지막 눈이다. 지원 밖 원본이 0으로 끝나면
"추출 가능"이라는 거짓 초록이 된다(A3P-F1 결함). 여기서는 조사·지원·추출의
모든 0이 아닌 결과가 종료 코드로 살아남는지를, 진짜 미디어 없이(헬퍼만
바꿔 끼운 가짜로) 잰다. ffmpeg·네트워크·실제 사용자 미디어는 전혀 안 쓴다.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import ModuleType

import pytest

from vlogsrt import preprocess
from vlogsrt.preprocess import (
    AudioPacketExtent,
    CreationTime,
    RawAudioTrack,
    RawClipExtraction,
    RawClipVerificationError,
    RawMediaProbe,
    RawPtsRange,
    RawVideoClock,
    RawVideoMedia,
)

ROOT = Path(__file__).resolve().parent.parent.parent


def _load_script() -> ModuleType:
    """`scripts/`는 패키지가 아니라 경로로 불러온다(verify_timing 시험의 방식)."""
    path = ROOT / "scripts" / "verify_raw_media.py"
    spec = importlib.util.spec_from_file_location("verify_raw_media", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _load_script()


def _video(**overrides: object) -> RawVideoMedia:
    base = RawVideoMedia(
        stream_index=0,
        codec_name="h264",
        profile="High",
        pix_fmt="yuv420p",
        bit_depth=8,
        width=1920,
        height=1080,
        color_primaries="bt709",
        color_transfer="bt709",
        color_matrix="bt709",
        color_range="tv",
        rotation_degrees=0,
        time_base=Fraction(1, 15360),
        r_frame_rate=Fraction(25),
        avg_frame_rate=Fraction(25),
        start_pts=0,
        duration_ts=153600,
    )
    return replace(base, **overrides) if overrides else base


def _probe(**video_overrides: object) -> RawMediaProbe:
    return RawMediaProbe(
        source=Path("synthetic.mp4"),
        container="mov,mp4",
        duration=Fraction(10),
        format_start=Fraction(0),
        creation_time=CreationTime(raw="", valid=False, utc=None),
        video=_video(**video_overrides),
        audio=(
            RawAudioTrack(
                stream_index=1,
                codec_name="aac",
                channels=2,
                sample_rate=48000,
                time_base=Fraction(1, 48000),
                start_time=Fraction(0),
                start_pts=0,
            ),
        ),
        other_tracks=(),
        ffprobe_version="ffprobe version 9-test",
    )


def _clock() -> RawVideoClock:
    return RawVideoClock(
        stream_index=0,
        time_base=Fraction(1, 15360),
        first_pts=0,
        last_pts=153599,
        presentation_origin=Fraction(0),
        frame_duration=Fraction(1, 25),
        frame_count=250,
        exact_end=Fraction(10),
        uniform_grid=True,
        grid_note="",
    )


def _extent() -> AudioPacketExtent:
    return AudioPacketExtent(
        stream_index=1,
        time_base=Fraction(1, 48000),
        first_pts=0,
        last_pts=479999,
        packet_count=235,
        packet_ticks=1024,
    )


def _stub_probes(
    monkeypatch: pytest.MonkeyPatch,
    probe: RawMediaProbe | None = None,
    clock: RawVideoClock | None = None,
) -> None:
    """조사 헬퍼를 가짜로 끼운다 — ffmpeg도 실제 미디어도 없다."""
    monkeypatch.setattr(preprocess, "ffmpeg_provenance", lambda: "provenance-stub")
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda source: probe or _probe())
    monkeypatch.setattr(
        preprocess, "probe_extraction_clock", lambda source, **kwargs: clock or _clock()
    )
    monkeypatch.setattr(preprocess, "probe_audio_extents", lambda source: (_extent(),))


def _stub_extraction(
    monkeypatch: pytest.MonkeyPatch, target: Path
) -> list[tuple[Path, Path, RawPtsRange]]:
    """extract_raw_clip을 기록만 하는 가짜로 바꾼다. 돌려줄 결과도 진짜 타입으로."""
    calls: list[tuple[Path, Path, RawPtsRange]] = []

    def fake_extract(
        source: Path, out: Path, source_range: RawPtsRange, profile: object, **kw: object
    ) -> RawClipExtraction:
        calls.append((source, out, source_range))
        return RawClipExtraction(
            path=out,
            profile_name="source_matched",
            source_range=source_range,
            media_range=source_range,
            core_first_frame=0,
            core_after_last_frame=50,
            core_local_start=Fraction(0),
            core_local_end=Fraction(2),
            media_local_end=Fraction(2),
            frame_count=50,
            frame_duration=Fraction(1, 25),
            video=_video(),
            audio=(),
            audio_local_starts=(),
            rotation_applied=False,
            unmapped_tracks=(),
            warnings=(),
            ffmpeg_version="ffmpeg version 9-test",
        )

    monkeypatch.setattr(preprocess, "extract_raw_clip", fake_extract)
    return calls


def _source_file(tmp_path: Path) -> Path:
    # 진짜 미디어가 아니라 해시 계산 대상 바이트일 뿐이다.
    path = tmp_path / "synthetic.mp4"
    path.write_bytes(b"not-real-media")
    return path


# ─────────────────────────────────────────────────────────────
#  종료 코드 — 거짓 초록 회귀
# ─────────────────────────────────────────────────────────────


def test_supported_media_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_probes(monkeypatch)
    code = script.main([str(_source_file(tmp_path))])
    assert code == 0
    out = capsys.readouterr().out
    assert "VERDICT: source_matched/edit_prores 추출 가능" in out


def test_unsupported_media_exits_nonzero_not_green(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A3P-F1 결함의 회귀 — 지원 밖(2)이 0으로 내려가면 거짓 초록이다."""
    _stub_probes(monkeypatch, probe=_probe(codec_name="vp9"))
    code = script.main([str(_source_file(tmp_path))])
    assert code == 2
    out = capsys.readouterr().out
    assert "지원 밖" in out
    assert "종료 코드 2" in out


def test_probe_failure_exits_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken(source: Path) -> RawMediaProbe:
        raise RuntimeError("ffprobe가 반쯤 죽으며 흘린 출력")

    _stub_probes(monkeypatch)
    monkeypatch.setattr(preprocess, "probe_raw_media", broken)
    code = script.main([str(_source_file(tmp_path))])
    assert code == 1
    assert "PROBE FAILED" in capsys.readouterr().out


def test_any_failure_in_a_batch_keeps_the_process_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """묶음 실행에서 하나라도 0이 아니면 전체는 0이 아니다."""
    _stub_probes(monkeypatch)
    good, bad = _source_file(tmp_path), tmp_path / "vp9.mp4"
    bad.write_bytes(b"also-not-real")
    real_probe = preprocess.probe_raw_media
    monkeypatch.setattr(
        preprocess,
        "probe_raw_media",
        lambda source: _probe(codec_name="vp9") if source == bad else real_probe(source),
    )
    assert script.main([str(good), str(bad)]) == 2
    assert script.main([str(bad), str(good)]) == 2


# ─────────────────────────────────────────────────────────────
#  추출 스포크 — 실패가 깨끗하게 보고되는가
# ─────────────────────────────────────────────────────────────


def test_extraction_smoke_success_exits_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _stub_probes(monkeypatch)
    _stub_extraction(monkeypatch, tmp_path / "out")
    code = script.main([str(_source_file(tmp_path)), "--extract", "1.0-3.0"])
    assert code == 0
    assert "clip " in capsys.readouterr().out


def test_extraction_verification_failure_reports_cleanly_and_exits_three(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """검증 실패는 줄로 보고되고 종료 코드 3으로 끝난다 — traceback이 증거가 아니다."""
    _stub_probes(monkeypatch)

    def rejected(
        source: Path, out: Path, source_range: RawPtsRange, profile: object, **kw: object
    ) -> RawClipExtraction:
        raise RawClipVerificationError("프레임 수가 다릅니다: 기대 50, 출력 33")

    monkeypatch.setattr(preprocess, "extract_raw_clip", rejected)
    code = script.main([str(_source_file(tmp_path)), "--extract", "1.0-3.0"])
    assert code == 3
    out = capsys.readouterr().out
    assert "EXTRACT FAILED" in out
    assert "프레임 수가 다릅니다" in out
    assert "Traceback" not in out


def test_extraction_rejects_unsupported_span_without_claiming_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probes(monkeypatch, probe=_probe(codec_name="vp9"))
    _stub_extraction(monkeypatch, tmp_path / "out")
    # 지원 밖이면 추출조차 시도하지 않는다 — 가짜 추출이 불리지 않는다.
    assert script.main([str(_source_file(tmp_path)), "--extract", "1.0-3.0"]) == 2


def test_source_mutation_during_extraction_is_a_clean_alarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """추출 뒤 원본 해시가 달라지면 assert 폭발이 아니라 보고+3이다."""
    _stub_probes(monkeypatch)
    source = _source_file(tmp_path)
    calls: list[tuple[Path, Path, RawPtsRange]] = []

    # 호출 시 원본을 바꾸는 가짜 — 절대 나면 안 되는 일을 흉내 낸다.
    def fake(
        source: Path, out: Path, source_range: RawPtsRange, profile: object, **kw: object
    ) -> RawClipExtraction:
        calls.append((source, out, source_range))
        source.write_bytes(b"mutated")
        return RawClipExtraction(
            path=out,
            profile_name="source_matched",
            source_range=source_range,
            media_range=source_range,
            core_first_frame=0,
            core_after_last_frame=50,
            core_local_start=Fraction(0),
            core_local_end=Fraction(2),
            media_local_end=Fraction(2),
            frame_count=50,
            frame_duration=Fraction(1, 25),
            video=_video(),
            audio=(),
            audio_local_starts=(),
            rotation_applied=False,
            unmapped_tracks=(),
            warnings=(),
            ffmpeg_version="ffmpeg version 9-test",
        )

    monkeypatch.setattr(preprocess, "extract_raw_clip", fake)
    code = script.main([str(source), "--extract", "1.0-3.0"])
    assert code == 3
    assert "원본이 바뀌었다" in capsys.readouterr().out
    assert calls  # 진짜로 추출 경로를 지나갔다


# ─────────────────────────────────────────────────────────────
#  --out — 출력 폴더를 명시할 수 있다
# ─────────────────────────────────────────────────────────────


def test_out_option_directs_the_smoke_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probes(monkeypatch)
    explicit = tmp_path / "explicit-dir"
    calls = _stub_extraction(monkeypatch, explicit)
    code = script.main(
        [str(_source_file(tmp_path)), "--extract", "1.0-3.0", "--out", str(explicit)]
    )
    assert code == 0
    assert explicit.is_dir()
    assert len(calls) == 1
    source, target, _ = calls[0]
    assert target.parent == explicit
    # 이름에 원본 해시 접두가 붙는다 — 같은 파일명의 다른 카드가 덮어쓰지 않는다(§5.2).
    assert "synthetic." in target.name and ".span-1.000-3.000.mp4" in target.name


def test_out_defaults_under_cwd_without_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_probes(monkeypatch)
    monkeypatch.chdir(tmp_path)
    calls = _stub_extraction(monkeypatch, tmp_path / "vlogsrt-raw-verify")
    assert script.main([str(_source_file(tmp_path)), "--extract", "1.0-3.0"]) == 0
    assert (tmp_path / "vlogsrt-raw-verify").is_dir()
    assert calls and calls[0][1].parent == tmp_path / "vlogsrt-raw-verify"
