"""자막 입히기 입출력(W-65, D116) — 준비 · 모양 저장 · 이름 · 보고서 · 인코더 물러남.

ffmpeg는 부르지 않는다 — `preprocess`의 경계 함수 자리에 가짜가 앉는다(실측은 D116에 남겼다).
"""

from __future__ import annotations

import hashlib
import json
import os
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import burn_export, burn_in
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.burn_config import BURN_SETTINGS
from vlogsrt.project.raw.models import RawWorkspaceError

KOREAN = (
    "1\n00:00:00,500 --> 00:00:02,500\n오늘은 제주도\n\n"
    "2\n00:00:03,000 --> 00:00:05,000\n\n\n"
    "3\n00:00:05,000 --> 00:00:05,900\n바다 {예쁘다}\n"
)
ENGLISH = (
    "1\n00:00:00,500 --> 00:00:02,500\nToday, Jeju\n\n"
    "2\n00:00:03,000 --> 00:00:05,000\n\n\n"
    "3\n00:00:05,000 --> 00:00:05,900\nThe sea\n"
)
FONT_SHA = "c89bc43027dc7cde5726e96223376f8eec09302b2fc1f8147fd5b57cfc376118"
OFL_SHA = "b04538c9abec39a3db75108cf0af0fd9c77032fe8aa2cf38345b4d250e98e38e"


def _probe(rotation: int = 0, audio: str | None = "aac", transfer: str = "bt709") -> Any:
    video = SimpleNamespace(
        width=1920, height=1080, rotation_degrees=rotation, avg_frame_rate=Fraction(30),
        r_frame_rate=Fraction(30), color_transfer=transfer, codec_name="h264",
    )  # fmt: skip
    tracks = (SimpleNamespace(codec_name=audio),) if audio else ()
    return SimpleNamespace(video=video, duration=Fraction(6), audio=tracks)


def _files(folder: Path, english: str | None = ENGLISH) -> tuple[Path, Path, Path | None]:
    folder.mkdir(parents=True, exist_ok=True)
    video = folder / "draft.mp4"
    video.write_bytes(b"video")
    korean = folder / "draft.srt"
    korean.write_text(KOREAN, encoding="utf-8")
    other = None
    if english is not None:
        other = folder / "draft.en.srt"
        other.write_text(english, encoding="utf-8")
    return video, korean, other


@pytest.fixture
def probed(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    probes = [_probe()]
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path: probes[0])
    return probes


def test_the_font_and_its_licence_are_bundled() -> None:
    font = burn_export.font_file()
    assert font.name == BURN_SETTINGS.font_file
    assert hashlib.sha256(font.read_bytes()).hexdigest() == FONT_SHA
    licence = font.with_name("OFL-Pretendard.txt")
    assert hashlib.sha256(licence.read_bytes()).hexdigest() == OFL_SHA
    readme = font.with_name("README.md").read_text(encoding="utf-8")
    assert FONT_SHA in readme and OFL_SHA in readme


def test_prepare_pairs_and_measures(tmp_path: Path, probed: list[Any]) -> None:
    job = burn_export.prepare(*_files(tmp_path))
    assert (job.width, job.height, job.frame_duration, job.duration) == (
        1920, 1080, Fraction(1, 30), Fraction(6),
    )  # fmt: skip
    assert job.audio == "copy" and not job.hdr
    assert len(job.pairing.events) == 2 and job.pairing.empty == 1
    probed[0] = _probe(rotation=90, audio="pcm_s16le", transfer="arib-std-b67")
    turned = burn_export.prepare(*_files(tmp_path))
    assert (turned.width, turned.height, turned.audio, turned.hdr) == (1080, 1920, "encode", True)
    probed[0] = _probe(audio=None)
    assert burn_export.prepare(*_files(tmp_path)).audio == "none"


def test_prepare_refuses_before_any_job(tmp_path: Path, probed: list[Any]) -> None:
    video, korean, _ = _files(tmp_path)
    shifted = tmp_path / "shifted.en.srt"
    shifted.write_text(ENGLISH.replace("00:00:05,900", "00:00:06,000"), encoding="utf-8")
    with pytest.raises(RawWorkspaceError, match="3번 큐"):
        burn_export.prepare(video, korean, shifted)
    blank = tmp_path / "blank.srt"
    blank.write_text("1\n00:00:01,000 --> 00:00:02,000\n\n", encoding="utf-8")
    with pytest.raises(RawWorkspaceError, match="입힐 자막 글이 없습니다"):
        burn_export.prepare(video, blank, None)
    with pytest.raises(RawWorkspaceError, match="영상 파일"):
        burn_export.prepare(tmp_path / "없는.mp4", korean, None)


def test_english_beside_finds_the_d114_name(tmp_path: Path) -> None:
    _video, korean, english = _files(tmp_path)
    assert burn_export.english_beside(korean) == english
    assert english is not None
    english.unlink()
    assert burn_export.english_beside(korean) is None
    late = tmp_path / "draft.en.20261112-101010.srt"
    late.write_text(ENGLISH, encoding="utf-8")
    (tmp_path / "draft.en.20261111-090909.srt").write_text(ENGLISH, encoding="utf-8")
    assert burn_export.english_beside(korean) == late


def _workspace(tmp_path: Path) -> tuple[raw_service.RawWorkspaceService, Path]:
    service = raw_service.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "자막 입히기")
    return service, root


def test_the_style_is_saved_per_workspace(tmp_path: Path) -> None:
    service, root = _workspace(tmp_path)
    assert burn_export.saved_style(service, root) == burn_in.default_style()
    style = burn_in.BurnStyle("top", 72, 6, 40, "#FFCC00")
    burn_export.save_style(service, root, style)
    assert burn_export.saved_style(service, root) == style
    service._workspace(root).set_meta_value(burn_export.BURN_STYLE_KEY, '{"font_px": 9999}')
    assert burn_export.saved_style(service, root) == burn_in.default_style()


class _FakeBurn:
    """`preprocess.burn_subtitles` 자리 — 작업 폴더를 들여다보고 출력 하나를 쓴다."""

    def __init__(self, fail_codecs: tuple[str, ...] = ()) -> None:
        self.fail_codecs = fail_codecs
        self.codecs: list[str] = []
        self.ass = ""
        self.fonts: list[str] = []

    def __call__(
        self, request: Any, output: Path, target: Any, encoding: Any, **kw: Any
    ) -> preprocess.BurnedVideo:
        self.codecs.append(encoding.video_codec)
        if encoding.video_codec in self.fail_codecs:
            raise RuntimeError("VT 세션 실패")
        self.ass = (request.workdir / request.subtitle_name).read_text(encoding="utf-8")
        self.fonts = sorted(p.name for p in (request.workdir / request.fonts_name).iterdir())
        output.write_bytes(b"burned")
        return preprocess.BurnedVideo(output, Fraction(6), 1920, 1080, "h264", "aac")


def _fake_tools(monkeypatch: pytest.MonkeyPatch, fake: _FakeBurn) -> None:
    monkeypatch.setattr(preprocess, "subtitle_ffmpeg", lambda candidates: "/opt/ffmpeg-full")
    monkeypatch.setattr(preprocess, "pick_video_encoder", lambda codecs, exe=None: codecs[0])
    monkeypatch.setattr(preprocess, "burn_subtitles", fake)
    monkeypatch.setattr(preprocess, "executable_version", lambda exe: "ffmpeg version 9.0.2")


def test_burn_writes_beside_the_video_and_never_overwrites(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path)
    fake = _FakeBurn()
    _fake_tools(monkeypatch, fake)
    job = burn_export.prepare(*_files(tmp_path / "trip"))
    facts = burn_export.burn(service, root, job, burn_in.default_style())
    output = tmp_path / "trip" / "draft.burned.mp4"
    # Windows의 권한 칸은 읽기 전용 한 비트뿐이다 — 쓸 수 있는 파일은 0o666으로 보인다.
    published_mode = 0o644 if os.name == "posix" else 0o666
    assert facts["path"] == str(output) and output.stat().st_mode & 0o777 == published_mode
    assert "오늘은 제주도\\N{\\rEN}Today, Jeju" in fake.ass and "바다 \\{예쁘다\\}" in fake.ass
    assert fake.fonts == [BURN_SETTINGS.font_file]
    report = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["schema"] == burn_export.REPORT_SCHEMA and report["events"] == 2
    assert report["korean_srt"]["cues"] == 3 and report["english_srt"]["sha256"]
    assert report["ffmpeg"] == {"path": "/opt/ffmpeg-full", "version": "ffmpeg version 9.0.2"}
    again = burn_export.burn(service, root, job, burn_in.default_style())
    assert again["path"] != facts["path"] and ".burned." in Path(again["path"]).name
    assert output.read_bytes() == b"burned"


def test_hardware_failure_falls_back_to_software(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path)
    fake = _FakeBurn(fail_codecs=("h264_videotoolbox",))
    _fake_tools(monkeypatch, fake)
    job = burn_export.prepare(*_files(tmp_path / "trip", english=None))
    facts = burn_export.burn(service, root, job, burn_in.default_style())
    assert fake.codecs == ["h264_videotoolbox", "libx264"]
    assert facts["encoder"] == "libx264" and facts["english"] is False


def test_unwritable_folder_falls_back_to_the_workspace(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path)
    _fake_tools(monkeypatch, _FakeBurn())
    job = burn_export.prepare(*_files(tmp_path / "trip"))
    real_access = burn_export.os.access
    monkeypatch.setattr(
        burn_export.os,
        "access",
        lambda path, mode: Path(path) != job.video.parent and real_access(path, mode),
    )
    facts = burn_export.burn(service, root, job, burn_in.default_style())
    assert facts["fallback"] is True
    assert Path(facts["path"]).parent.parent == service._workspace(root).root / "export" / "burned"


def test_start_refuses_without_libass_before_any_job(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path)
    job = burn_export.prepare(*_files(tmp_path / "trip"))

    def missing(candidates: Any) -> str:
        raise RuntimeError("자막을 입힐 ffmpeg(libass 포함)를 찾지 못했습니다.")

    monkeypatch.setattr(preprocess, "subtitle_ffmpeg", missing)
    with pytest.raises(RawWorkspaceError, match="libass"):
        burn_export.start(service, root, job, burn_in.default_style())
    assert service.list_jobs(root) == ()
