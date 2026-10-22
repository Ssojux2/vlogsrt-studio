"""전체 타임라인 보내기의 입출력 계층 (W-16 B단계, D56).

`finalcut_timeline`(순수)이 짠 문서를 실제 작업공간에서 모아 쓰는 길을 합성 ffmpeg 미디어로
증명한다. 유료 호출 0회. 지키는 선:

- 미검토 빨강이 남아 있어도 보낸다(지우지 않으므로 관문이 없다) — 켠 채 키워드로만 알린다.
  사람이 확인한 뒤 다시 보내면 그 조각만 꺼진다(`enabled="0"`).
- 원본을 열지도, 다시 해시하지도, 복사하지도 않는다. 작업공간에서 바뀌는 것은
  `review/finalcut/` 아래 새 파일뿐이고, 다시 보내면 언제나 새 파일이다(덮어쓰지 않는다).
- 원본이 바뀌었거나 라이브러리 신원·시각이 어긋나면 그 파일 이름을 대고 거부한다.
  라이브러리에서 온 원본에 해시 uid를 지어 붙이는 대체 경로는 없다.
"""

from __future__ import annotations

import argparse
import builtins
import dataclasses
import fcntl
import hashlib
import json
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.project.library_fixtures import (
    EVENT_DB,
    NSNULL,
    MediaSpec,
    make_bundle,
    write_database,
)
from tests.raw.test_review_workspace import _wait_terminal
from tests.raw.test_tier_review_gate import RED, tier_project
from vlogsrt import preprocess
from vlogsrt.project import media
from vlogsrt.project.raw import commands as media_commands
from vlogsrt.project.raw import finalcut_export, media_refs, review_workspace, source_transcript
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.xml_validation import XmlValidation, validate_xml

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
pytestmark = needs_ffmpeg

INSTALLED_DTD = finalcut_export.INSTALLED_DTD
F = Fraction


# ─── 재료 ────────────────────────────────────────────────────────────────


def _encode(target: Path, *extra: str, seconds: str = "2", rate: str = "25") -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate={rate}:duration={seconds}",
            "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
            "-t", seconds, "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-c:a", "pcm_s16le", *extra, str(target),
        ],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    return target


def _plain_project(tmp_path: Path) -> tuple[RawWorkspaceService, Path]:
    """복사 가져오기 하나 — 분석도 검토 머리도 없다."""
    source = _encode(tmp_path / "card" / "plain.mov")
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "보내기")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    return service, root


def _library_project(
    tmp_path: Path,
    *,
    linked: bool = False,
    others: dict[str, tuple[MediaSpec, ...]] | None = None,
    **spec: Any,
) -> tuple[RawWorkspaceService, Path, Path, MediaSpec]:
    """합성 Final Cut 라이브러리 안의 실제 원본을 참조로 가져온다 — 복사 없음.

    `linked`면 Final Cut의 "제자리에 두기"처럼 `Original Media`에 라이브러리 밖 원본을 가리키는
    심볼릭 링크만 둔다 — 그때 작업공간은 링크가 아니라 바깥 원본을 기록한다.
    """
    fields: dict[str, Any] = {
        "name": "clip",
        "frames": 50,
        "frame_duration": "1/25",
        "format": "FFVideoFormat160x90p25",
        "start_frames": 900000,  # 10:00:00:00 @25
        "drop_frame": 0,
    }
    fields.update(spec)
    media_spec = MediaSpec(**fields)
    events = {"clips": (media_spec,), **(others or {})}
    bundle = make_bundle(tmp_path, media=events, name="Lib.fcpbundle")
    original = bundle / "clips" / "Original Media" / "clip.mov"
    if linked:
        outside = _encode(tmp_path / "external" / "clip.mov", "-timecode", "10:00:00:00")
        original.unlink()
        original.symlink_to(outside)
    else:
        _encode(original, "-timecode", "10:00:00:00")
    service = RawWorkspaceService()
    state, _ = service.managed_fcp_workspace(bundle)
    root = Path(state.workspace_root)
    proposal = service.scan_fcp_library(root, bundle, originals_only=True).proposal
    service.remember_fcp_library(root, bundle)  # 화면의 라이브러리 조사가 하는 일
    handle = service.start_reference_import(root, proposal, prepare_preview=False)
    finished = _wait_terminal(service, root, handle.job_id)
    assert finished.status == "succeeded", finished.error
    return service, root, bundle.resolve(), media_spec


def _parsed(path: Path) -> ET.Element:
    return ET.fromstring(path.read_bytes())


def _spine(path: Path) -> list[ET.Element]:
    spine = _parsed(path).find("./library/event/project/sequence/spine")
    assert spine is not None
    return list(spine)


def _keywords(clip: ET.Element) -> list[str]:
    return [k.attrib["value"] for k in clip.findall("keyword")]


def _report(path: Path) -> dict[str, Any]:
    return json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))


def _assert_valid(path: Path) -> None:
    if INSTALLED_DTD.is_file():
        result = validate_xml(path, dtd_path=INSTALLED_DTD)
        assert result.valid and result.dtd_checked, result.diagnostics


def _tree(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _export_files(root: Path) -> list[str]:
    folder = root / "review" / "finalcut"
    return sorted(p.name for p in folder.iterdir()) if folder.is_dir() else []


# ─── 17·22. 관문 없음 · 확인 뒤 꺼짐 · 다시 보내면 새 파일 ─────────────────────


def test_unreviewed_red_is_sent_enabled_then_disabled_after_human_confirm(tmp_path: Path):
    service, root = tier_project(tmp_path, suitability="usable")
    first = service.export_finalcut_timeline(root)
    assert first.parent == root / "review" / "finalcut" and first.suffix == ".fcpxml"
    _assert_valid(first)
    first_bytes = first.read_bytes()
    red_start, red_length = f"{RED[0] * 25}/25s", f"{(RED[1] - RED[0]) * 25}/25s"
    clips = _spine(first)
    (red,) = [c for c in clips if c.attrib["start"] == red_start]
    assert red.attrib["duration"] == red_length
    assert "enabled" not in red.attrib
    assert _keywords(red) == ["제외 제안", "제외 제안(미검토)"]
    assert not any(c.get("enabled") == "0" for c in clips)
    assert _report(first)["counts"]["unreviewed_clips"] == 1

    ReviewWorkspaceFacade(root).confirm_automatic_exclusions()
    second = service.export_finalcut_timeline(root)
    assert second != first and first.read_bytes() == first_bytes
    _assert_valid(second)
    (red,) = [c for c in _spine(second) if c.attrib["start"] == red_start]
    assert red.attrib["enabled"] == "0"
    assert _keywords(red) == ["제외 제안", "제외 확정"]
    counts = _report(second)["counts"]
    assert (counts["disabled_clips"], counts["unreviewed_clips"]) == (1, 0)
    assert _export_files(root) == sorted(
        [first.name, first.with_suffix(".json").name, second.name, second.with_suffix(".json").name]
    )


def test_report_records_counts_inputs_and_captions(tmp_path: Path):
    service, root = tier_project(tmp_path, suitability="usable")
    path = service.export_finalcut_timeline(root)
    report = _report(path)
    head = ReviewWorkspaceFacade(root).load_head().revision
    (asset,) = service.list_source_assets(root)
    assert report["schema"] == finalcut_export.REPORT_SCHEMA
    assert report["fcpxml"] == path.name
    assert report["review_head"] == head.revision_id
    assert report["counts"]["sources"] == 1 and report["counts"]["analysed"] == 1
    assert report["counts"]["spine_clips"] == len(head.segments)
    assert report["counts"]["captions"]["placed"] == 4  # 재생 ASR 발화 넷
    (row,) = report["sources"]
    assert row["asset_id"] == asset.asset_id
    assert row["content_sha256"] == asset.content_sha256
    assert row["uid"] == f"com.vlogsrt.source.{asset.content_sha256}"
    assert row["timecode"] is None and row["frames"] == 375
    assert report["validation"]["valid"] is True
    assert report["not_exported"] == []


# ─── 18. 원본을 열지도·다시 해시하지도·복사하지도 않는다 ───────────────────


def _forbid_rehash(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_: Any, **__: Any) -> Any:
        raise AssertionError("보내기는 원본을 다시 해시하지 않는다")

    monkeypatch.setattr(media, "sha256_file", refuse)
    monkeypatch.setattr(review_workspace, "_sha256_file", refuse)
    monkeypatch.setattr(media_refs, "_facts", refuse)
    monkeypatch.setattr(raw_service, "_verified_asset", refuse)


def test_export_never_opens_rehashes_or_copies_and_writes_only_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path, suitability="usable")
    (asset,) = service.list_source_assets(root)
    original = media_refs.resolve(service._workspace(root), asset, "export").resolve()
    before = _tree(root)
    original_sha = hashlib.sha256(original.read_bytes()).hexdigest()
    with monkeypatch.context() as patch:
        _forbid_rehash(patch)
        seen: list[Path] = []
        real_open, real_path_open = builtins.open, Path.open

        def recording_open(file: Any, *args: Any, **kwargs: Any) -> Any:
            if isinstance(file, (str, os.PathLike)):
                seen.append(Path(file).resolve())
            return real_open(file, *args, **kwargs)

        def recording_path_open(self: Path, *args: Any, **kwargs: Any) -> Any:
            seen.append(self.resolve())
            return real_path_open(self, *args, **kwargs)

        patch.setattr(builtins, "open", recording_open)
        patch.setattr(Path, "open", recording_path_open)
        path = service.export_finalcut_timeline(root)
    assert original not in seen
    after = _tree(root)
    changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
    assert changed == {f"review/finalcut/{path.name}", f"review/finalcut/{path.stem}.json"}
    assert hashlib.sha256(original.read_bytes()).hexdigest() == original_sha
    assert original_sha == asset.content_sha256
    rep = _parsed(path).find("./resources/asset/media-rep")
    assert rep is not None and rep.attrib["src"] == original.as_uri()


# ─── 19. 분석 전 원본도 촬영 시각 순서대로 들어간다 ──────────────────────────


def test_unanalysed_source_is_one_unanalysed_clip_in_capture_order(tmp_path: Path):
    service, root = tier_project(tmp_path)
    early = _encode(
        tmp_path / "card" / "early.mov", "-metadata", "creation_time=2020-01-01T00:00:00Z"
    )
    handle = service.import_selected_video(root, early)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    root_element = _parsed(path)
    names = {a.attrib["id"]: a.attrib["name"] for a in root_element.iter("asset")}
    clips = _spine(path)
    assert names[clips[0].attrib["ref"]] == "early"
    assert _keywords(clips[0]) == ["미분석"]
    assert clips[0].attrib["duration"] == "50/25s" and clips[0].findall("caption") == []
    assert {names[c.attrib["ref"]] for c in clips[1:]} == {"tier"}
    browser = [c.attrib["name"] for c in root_element.findall("./library/event/asset-clip")]
    assert browser == ["tier"]
    counts = _report(path)["counts"]
    assert (counts["sources"], counts["analysed"]) == (2, 1)
    assert counts["sources_without_captions"] == ["early"]


def test_plain_import_without_review_or_analysis_is_one_unanalysed_clip(tmp_path: Path):
    service, root = _plain_project(tmp_path)
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    (clip,) = _spine(path)
    assert _keywords(clip) == ["미분석"] and clip.attrib["start"] == "0s"
    assert clip.attrib["tcFormat"] == "NDF"
    report = _report(path)
    assert report["review_head"] is None and report["library"] is None


# ─── 20. 라이브러리 신원 ─────────────────────────────────────────────────


def test_library_original_uses_its_identifier_timecode_and_location(tmp_path: Path):
    service, root, bundle, spec = _library_project(tmp_path)
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    root_element = _parsed(path)
    asset = root_element.find("./resources/asset")
    assert asset is not None
    assert asset.attrib["uid"] == spec.identity
    rep = asset.find("media-rep")
    assert rep is not None and rep.attrib["sig"] == spec.identity
    assert asset.attrib["start"] == "900000/25s" and asset.attrib["duration"] == "50/25s"
    location = bundle.as_uri() + "/"
    assert root_element.find("library").attrib == {"location": location}  # type: ignore[union-attr]
    options = {o.attrib["key"]: o.attrib["value"] for o in root_element.iter("option")}
    assert options["library location"] == location and options["copy assets"] == "0"
    (clip,) = _spine(path)
    assert clip.attrib["start"] == "900000/25s" and clip.attrib["tcFormat"] == "NDF"
    report = _report(path)
    assert report["library"]["bundle"] == str(bundle)
    assert report["sources"][0]["timecode"] == {"text": "10:00:00:00", "rate": "25"}


def test_library_link_to_an_outside_original_still_uses_the_library_identity(tmp_path: Path):
    """링크 대상이 라이브러리 밖이어도 라이브러리 미디어다 — 해시 uid로 새 미디어를 만들지
    않는다."""
    service, root, bundle, spec = _library_project(tmp_path, linked=True)
    (asset,) = service.list_source_assets(root)
    assert not str(media_refs.resolve(service._workspace(root), asset, "export")).startswith(
        str(bundle)
    )
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    asset_element = _parsed(path).find("./resources/asset")
    assert asset_element is not None and asset_element.attrib["uid"] == spec.identity


def test_library_start_that_disagrees_with_the_timecode_is_refused(tmp_path: Path):
    service, root, _bundle, _spec = _library_project(tmp_path, start_frames=900001)
    with pytest.raises(finalcut_export.FinalCutExportError, match="clip.mov"):
        service.export_finalcut_timeline(root)
    assert not any(name.endswith(".fcpxml") for name in _export_files(root))


def test_library_length_that_disagrees_with_the_file_is_refused(tmp_path: Path):
    service, root, _bundle, _spec = _library_project(tmp_path, frames=51)
    with pytest.raises(finalcut_export.FinalCutExportError, match="clip.mov"):
        service.export_finalcut_timeline(root)


def test_library_parse_failure_names_the_library_and_key(tmp_path: Path):
    service, root, bundle, _spec = _library_project(tmp_path, drop_frame=None)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert bundle.name in str(caught.value)
    assert "timecodeDisplayDropFrame" in str(caught.value)


def test_library_original_missing_from_the_library_is_refused_not_hashed(tmp_path: Path):
    """라이브러리 안의 원본인데 라이브러리 기록에 없다 — 해시 uid로 대신 보내지 않는다."""
    service, root, bundle, _spec = _library_project(tmp_path)
    write_database(bundle / "clips" / EVENT_DB, [])
    with pytest.raises(finalcut_export.FinalCutExportError, match="clip.mov"):
        service.export_finalcut_timeline(root)


@pytest.mark.parametrize(
    ("label", "rate", "why"),
    [
        ("10:00:00:00", F(30000, 1001), "레이트"),  # 타임코드 트랙 레이트 ≠ 영상 프레임 레이트
        ("garbage", F(25), "타임코드"),
    ],
)
def test_timecode_that_cannot_place_the_clip_is_refused_naming_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, label: str, rate: Fraction, why: str
):
    service, root, _bundle, _spec = _library_project(tmp_path)
    monkeypatch.setattr(
        preprocess, "probe_timecode", lambda path: preprocess.SourceTimecode(label, rate)
    )
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "clip.mov" in str(caught.value) and why in str(caught.value)


# ─── 21. 바뀐 원본은 거부한다 ─────────────────────────────────────────────


def test_changed_copied_original_is_refused_by_name(tmp_path: Path):
    service, root = _plain_project(tmp_path)
    (asset,) = service.list_source_assets(root)
    with (root / asset.relative_path).open("ab") as stream:
        stream.write(b"\0")
    with pytest.raises(finalcut_export.FinalCutExportError, match="plain.mov"):
        service.export_finalcut_timeline(root)
    assert _export_files(root) == []


def test_changed_library_original_is_refused_by_name(tmp_path: Path):
    service, root, bundle, _spec = _library_project(tmp_path)
    original = bundle / "clips" / "Original Media" / "clip.mov"
    os.utime(original, ns=(original.stat().st_atime_ns, original.stat().st_mtime_ns + 10**9))
    with pytest.raises(finalcut_export.FinalCutExportError, match="clip.mov"):
        service.export_finalcut_timeline(root)


# ─── 검증에 실패하면 끝난 것처럼 보이는 파일을 남기지 않는다 ─────────────────


def test_invalid_document_leaves_no_finished_looking_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = _plain_project(tmp_path)
    monkeypatch.setattr(
        finalcut_export,
        "validate_xml",
        lambda path, dtd_path=None: XmlValidation(False, ("element bad",), True),
    )
    with pytest.raises(finalcut_export.FinalCutExportError, match="element bad"):
        service.export_finalcut_timeline(root)
    files = _export_files(root)
    assert not any(name.endswith((".fcpxml", ".json")) for name in files)
    assert all(name.endswith(".invalid.xml") for name in files)


# ─── CLI — 쓰기만 하고 Final Cut은 열지 않는다 ──────────────────────────────


def _parser() -> argparse.ArgumentParser:
    import vlogsrt.project.commands as project_commands

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    project_commands.add_media_parser(sub)
    return parser


def test_cli_writes_and_prints_the_timeline_without_opening_final_cut(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    service, root = _plain_project(tmp_path)
    real_popen = subprocess.Popen

    def guarded_popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        words = [str(word) for word in argv] if isinstance(argv, (list, tuple)) else [str(argv)]
        if Path(words[0]).name == "open" or any("Final Cut Pro" in w for w in words):
            raise AssertionError("CLI는 Final Cut을 열지 않는다")
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root)])
    assert media_commands.media_command(args) == 0
    printed = capsys.readouterr().out.replace("\n", "")  # rich가 좁은 폭에서 줄을 접는다
    (fcpxml,) = [n for n in _export_files(root) if n.endswith(".fcpxml")]
    assert fcpxml in printed and "원본 1개" in printed
    (asset,) = service.list_source_assets(root)
    with (root / asset.relative_path).open("ab") as stream:
        stream.write(b"\0")
    assert media_commands.media_command(args) == 1
    assert "plain.mov" in capsys.readouterr().err.replace("\n", "")


# ─── W-16 B 2차 — 라이브러리의 무관한 깨진 기록 · 저장 중 ───────────────────


def test_unrelated_broken_library_record_does_not_block_the_export(tmp_path: Path):
    """실물 제주 백업처럼 다른 이벤트에 `videoFormatName`이 NSNull인 기록이 있어도 보낸다."""
    broken = MediaSpec(name="other", format=NSNULL)
    service, root, _bundle, spec = _library_project(tmp_path, others={"day1": (broken,)})
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    asset = _parsed(path).find("./resources/asset")
    assert asset is not None and asset.attrib["uid"] == spec.identity
    warnings = _report(path)["library"]["warnings"]
    assert any(
        "videoFormatName" in line and broken.identity in line and "other" in line
        for line in warnings
    )  # 표시 이름까지 — 사람이 Final Cut에서 그 클립을 찾을 수 있다


def test_broken_record_of_an_exported_original_is_refused_by_name(tmp_path: Path):
    service, root, bundle, _spec = _library_project(tmp_path, format=NSNULL)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    message = str(caught.value)
    assert "clip.mov" in message and bundle.name in message and "videoFormatName" in message


def test_library_being_saved_is_refused_with_a_clear_message(tmp_path: Path):
    service, root, bundle, _spec = _library_project(tmp_path)
    (bundle / "clips" / f"{EVENT_DB}-journal").write_bytes(b"")
    with pytest.raises(finalcut_export.FinalCutExportError, match="저장하는 중"):
        service.export_finalcut_timeline(root)
    assert _export_files(root) == []


# ─── MINOR-1·4 — 프로브 실패는 이름을 댄 거부다(CLI도 traceback이 없다) ──────


def test_probe_failure_is_a_named_refusal_and_the_cli_prints_no_traceback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    service, root = _plain_project(tmp_path)

    def broken_probe(path: Path) -> Any:
        raise RuntimeError("ffprobe 실패 (코드 1)")

    monkeypatch.setattr(preprocess, "probe_timecode", broken_probe)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "plain.mov" in str(caught.value) and "ffprobe 실패" in str(caught.value)
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root)])
    assert media_commands.media_command(args) == 1
    printed = capsys.readouterr()
    assert "plain.mov" in printed.err.replace("\n", "") and "Traceback" not in printed.err


# ─── MINOR-2 — 검토 머리가 덮는 원본은 패킷을 훑지 않는다 ────────────────────


def test_covered_source_never_scans_packets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    service, root = tier_project(tmp_path)

    def no_scan(*_: Any, **__: Any) -> Any:
        raise AssertionError("검토 머리의 시계를 두고 원본 패킷을 다시 훑었다")

    monkeypatch.setattr(preprocess, "probe_extraction_clock", no_scan)
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    assert _report(path)["sources"][0]["frames"] == 375


def test_uncovered_source_is_measured_and_vfr_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = _plain_project(tmp_path)
    real = preprocess.probe_extraction_clock
    calls: list[Path] = []

    def vfr(path: Path, **kwargs: Any) -> Any:
        calls.append(path)
        return dataclasses.replace(
            real(path, **kwargs), uniform_grid=False, grid_note="pts 간격이 고르지 않다"
        )

    monkeypatch.setattr(preprocess, "probe_extraction_clock", vfr)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "plain.mov" in str(caught.value) and "VFR" in str(caught.value)
    assert len(calls) == 1


# ─── MINOR-4 — 검토 머리·자막의 거부 경로 ─────────────────────────────────


def test_stale_head_source_is_refused_by_name(tmp_path: Path):
    service, root = tier_project(tmp_path)
    (asset,) = service.list_source_assets(root)
    (root / asset.relative_path).unlink()
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "tier.mov" in str(caught.value) and "낡았다" in str(caught.value)


def test_review_clock_that_disagrees_with_the_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path)
    real = preprocess.probe_raw_media

    def other_rate(path: Path, **kwargs: Any) -> Any:
        probe = real(path, **kwargs)
        video = dataclasses.replace(probe.video, r_frame_rate=F(30))
        return dataclasses.replace(probe, video=video)

    monkeypatch.setattr(preprocess, "probe_raw_media", other_rate)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "tier.mov" in str(caught.value) and "검토 시계" in str(caught.value)


def test_head_clock_bound_to_another_content_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path)
    real = ReviewWorkspaceFacade.load_head

    def other_content(self: ReviewWorkspaceFacade, *args: Any, **kwargs: Any) -> Any:
        loaded = real(self, *args, **kwargs)
        clocks = tuple(
            dataclasses.replace(c, source_sha256="f" * 64) for c in loaded.revision.clocks
        )
        return dataclasses.replace(
            loaded, revision=dataclasses.replace(loaded.revision, clocks=clocks)
        )

    monkeypatch.setattr(ReviewWorkspaceFacade, "load_head", other_content)
    with pytest.raises(finalcut_export.FinalCutExportError, match="다른 원본 내용"):
        service.export_finalcut_timeline(root)


def test_transcript_bound_to_another_content_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = tier_project(tmp_path)
    real = RawWorkspaceService.source_transcript

    def other_content(self: RawWorkspaceService, *args: Any) -> Any:
        document = real(self, *args)
        return None if document is None else {**document, "source_sha256": "0" * 64}

    monkeypatch.setattr(RawWorkspaceService, "source_transcript", other_content)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "tier.mov" in str(caught.value) and "자막" in str(caught.value)


def test_sources_left_off_the_timeline_are_reported_and_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    service, root = _plain_project(tmp_path)
    real = RawWorkspaceService.list_source_assets

    def with_failed(self: RawWorkspaceService, *args: Any, **kwargs: Any) -> Any:
        assets = real(self, *args, **kwargs)
        broken = dataclasses.replace(
            assets[0], asset_id="source_broken", status="failed", source_name="broken.mov"
        )
        return (*assets, broken)

    monkeypatch.setattr(RawWorkspaceService, "list_source_assets", with_failed)
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root)])
    assert media_commands.media_command(args) == 0
    printed = capsys.readouterr().out.replace("\n", "")
    assert "broken.mov" in printed and "상태 failed" in printed
    (fcpxml,) = [n for n in _export_files(root) if n.endswith(".fcpxml")]
    report = _report(root / "review" / "finalcut" / fcpxml)
    assert report["not_exported"] == [
        {"asset_id": "source_broken", "name": "broken.mov", "reason": "상태 failed"}
    ]


# ─── MINOR-5 — 재는 사이에 바뀐 원본 ───────────────────────────────────────


def test_original_changed_while_probing_is_refused_by_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = _plain_project(tmp_path)
    real = preprocess.probe_raw_media

    def touching(path: Path, **kwargs: Any) -> Any:
        probe = real(path, **kwargs)
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
        return probe

    monkeypatch.setattr(preprocess, "probe_raw_media", touching)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "plain.mov" in str(caught.value) and "바뀌었다" in str(caught.value)


# ─── MINOR-3·NIT — 깨진 시도의 잔해 · 동시 보내기 · 이름 경주 ─────────────────


def test_stale_drafts_are_swept_and_published_files_are_kept(tmp_path: Path):
    service, root = _plain_project(tmp_path)
    folder = root / "review" / "finalcut"
    folder.mkdir(parents=True)
    stale = [
        ".vlogsrt_20200101T000000Z.fcpxml.partial",
        ".vlogsrt_20200101T000000Z_2.json.partial",
    ]
    kept = ["vlogsrt_20200101T000000Z.json", ".keep", "notes.partial"]
    for name in (*stale, *kept):
        (folder / name).write_bytes(b"x")
    path = service.export_finalcut_timeline(root)
    names = set(_export_files(root))
    assert not names & set(stale)
    assert set(kept) <= names and path.name in names
    assert (folder / kept[0]).read_bytes() == b"x"


def test_concurrent_export_in_the_same_workspace_is_refused(tmp_path: Path):
    service, root = _plain_project(tmp_path)
    folder = root / "review" / "finalcut"
    folder.mkdir(parents=True)
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(finalcut_export.FinalCutExportError, match="진행 중"):
            service.export_finalcut_timeline(root)
    finally:
        os.close(descriptor)
    assert _export_files(root) == []


def test_reserve_moves_on_when_a_name_appears_while_reserving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """다른 쓰는 쪽이 같은 이름을 막 냈다면 다음 번호로 — 있는 파일은 덮지 않는다."""
    from datetime import UTC, datetime

    folder = tmp_path / "finalcut"
    folder.mkdir()
    moment = datetime(2026, 10, 9, 12, 0, 0, tzinfo=UTC)
    rival = folder / "vlogsrt_20261009T120000Z.fcpxml"
    real_open = os.open

    def racing_open(path: Any, flags: int, *args: Any) -> int:
        descriptor = real_open(path, flags, *args)
        if str(path).endswith(".partial") and not rival.exists():
            rival.write_bytes(b"rival")
        return descriptor

    monkeypatch.setattr(os, "open", racing_open)
    stem, draft = finalcut_export._reserve(folder, moment)
    monkeypatch.undo()
    assert stem == "vlogsrt_20261009T120000Z_2"
    assert draft.name == f".{stem}.fcpxml.partial" and draft.exists()
    assert rival.read_bytes() == b"rival"
    assert not (folder / ".vlogsrt_20261009T120000Z.fcpxml.partial").exists()


# ─── W-16 C — 가져오는 중 · 잠금 없는 파일 시스템 · 머리 정보 프레임 수 ─────


def test_export_waits_for_a_running_import(tmp_path: Path):
    """가져오기가 도는 동안의 원본 목록은 아직 끝나지 않았다 — 반쪽 타임라인을 보내지 않는다."""
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace

    service, root = _plain_project(tmp_path)
    running, _ = JobStore(RawWorkspace.open(root)).insert_deduplicated("ingest_copy", "card", 0)
    with pytest.raises(finalcut_export.FinalCutExportError, match="가져오기") as caught:
        service.export_finalcut_timeline(root)
    assert running.job_id in str(caught.value)
    assert _export_files(root) == []


def test_a_folder_without_file_locks_is_a_named_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    service, root = _plain_project(tmp_path)

    def no_locks(descriptor: int, operation: int) -> None:
        raise OSError(45, "Operation not supported")

    monkeypatch.setattr(fcntl, "flock", no_locks)
    with pytest.raises(finalcut_export.FinalCutExportError, match="잠금"):
        service.export_finalcut_timeline(root)


def test_head_frame_count_must_match_the_header(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """W-28(D72, 사용자 결정)부터 끝만 반 프레임 **미만** 자른 균일 원본은 받는다 — 예전 한 틱
    재료는 이제 받는 쪽이라(`test_finalcut_library_12_4.py`), 이 시험은 반 프레임 짧은 머리
    정보로 거부를 지킨다."""
    service, root = tier_project(tmp_path)
    real = preprocess.probe_raw_media

    def shorter(path: Path, **kwargs: Any) -> Any:
        probe = real(path, **kwargs)
        assert probe.video.duration_ts is not None
        half = Fraction(1, 2) / probe.video.r_frame_rate / probe.video.time_base
        assert half.denominator == 1
        stated = probe.video.duration_ts - half.numerator
        video = dataclasses.replace(probe.video, duration_ts=stated)
        return dataclasses.replace(probe, video=video)

    monkeypatch.setattr(preprocess, "probe_raw_media", shorter)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    assert "프레임 수" in str(caught.value)
    _edit_list_refusal(str(caught.value), "tier.mov")  # 머리를 다시 세워도 같은 시계를 잰다


# ─── W-16 C 2차 — 편집 목록(edit list)이 있는 원본 · 오래 남은 가져오기 작업 ───────


def _trim_copy(source: Path, target: Path) -> Path:
    """스트림 복사로 앞을 자른 파일 — 첫 패킷이 0보다 앞이고 머리 정보 길이가 패킷보다 짧다."""
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-ss", "0.5", "-i", str(source), "-c", "copy", str(target)],
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    return target


def _edit_list_refusal(message: str, name: str) -> None:
    assert name in message and "편집 목록" in message and "다시 저장" in message
    assert "검토를 다시 세우세요" not in message  # 다시 세워도 같은 시계를 잰다


def test_edit_list_source_outside_the_head_is_refused_with_resave_advice(tmp_path: Path):
    source = _trim_copy(
        _encode(tmp_path / "full.mov", seconds="3"), tmp_path / "card" / "cut_copy.mov"
    )
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "편집 목록")
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    _edit_list_refusal(str(caught.value), "cut_copy.mov")
    assert _export_files(root) == []


def test_edit_list_source_inside_the_head_is_refused_with_resave_advice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """검토 머리의 원점이 0보다 앞이면 머리를 세울 때 이미 편집 목록을 쟀던 것이다."""
    service, root = tier_project(tmp_path)
    real = ReviewWorkspaceFacade.load_head

    def pre_rolled(self: ReviewWorkspaceFacade, *args: Any, **kwargs: Any) -> Any:
        loaded = real(self, *args, **kwargs)
        shift = loaded.revision.clocks[0].frame_ticks * 12
        clocks = tuple(
            dataclasses.replace(c, origin_pts=c.origin_pts - shift, end_pts=c.end_pts - shift)
            for c in loaded.revision.clocks
        )
        segments = tuple(
            dataclasses.replace(s, start_pts=s.start_pts - shift, end_pts=s.end_pts - shift)
            for s in loaded.revision.segments
        )
        revision = dataclasses.replace(loaded.revision, clocks=clocks, segments=segments)
        return dataclasses.replace(loaded, revision=revision)

    monkeypatch.setattr(ReviewWorkspaceFacade, "load_head", pre_rolled)
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    _edit_list_refusal(str(caught.value), "tier.mov")


def test_an_old_running_import_behind_newer_jobs_still_blocks_and_says_how_to_clear(
    tmp_path: Path,
):
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace

    service, root = _plain_project(tmp_path)
    store = JobStore(RawWorkspace.open(root))
    stuck, _ = store.insert_deduplicated("reference_import", "stuck", 0)
    store.update(stuck.job_id, status="running")
    for index in range(55):  # 최근 50개 창 밖으로 밀어낸다
        newer, _ = store.insert_deduplicated("source_preview", f"newer-{index}", 0)
        store.update(newer.job_id, status="succeeded")
    with pytest.raises(finalcut_export.FinalCutExportError) as caught:
        service.export_finalcut_timeline(root)
    message = str(caught.value)
    assert stuck.job_id in message and stuck.created_at in message
    assert "가져오기" in message and "앱을 다시 시작" in message
    assert _export_files(root) == []


# ─── D59 — 리포트는 실은 자막을 정직하게 센다 ─────────────────────────────────


def _later_stage1_with_a_failed_chunk(root: Path, asset: Any) -> None:
    """뒤의 실행이 전사 직후에 죽었다 — 더 최근 1단계 기록에 실패 청크(1–2초)가 있다(0018 꼴)."""
    folder = root / "analysis" / "anal_later" / f"000099_{asset.asset_id}"
    folder.mkdir(parents=True)
    body = {
        "schema": "raw-analysis-asr-outcome/v1",
        "source_sha256": asset.content_sha256,
        "created_at": "2999-01-01T00:00:00+00:00",
        "video": {"presentation_origin": {"num": 0, "den": 1}},
        "audio": {
            "pcm_sha256": "0" * 64,
            "audio_stream_index": 1,
            "sample_rate": 16000,
            "channels": 2,
            "sample_count": 16000 * 15,
            "pcm_sample0_file_time": {"num": 0, "den": 1},
        },
        "asr": {
            "chunks": [
                {"chunk_index": 0, "sample_start": 0, "sample_end": 16000, "failure": ""},
                {
                    "chunk_index": 1,
                    "sample_start": 16000,
                    "sample_end": 32000,
                    "failure": "connection: 끊김",
                },
            ]
        },
    }
    (folder / "asr_outcome.json").write_text(json.dumps(body), encoding="utf-8")


def test_report_counts_the_captions_it_sent_and_names_the_failed_spans(tmp_path: Path):
    service, root = tier_project(tmp_path, suitability="usable")
    (asset,) = service.list_source_assets(root)
    report = _report(service.export_finalcut_timeline(root))
    (row,) = report["sources"]
    assert (row["captions"], row["caption_count"], row["caption_gaps"]) == (True, 4, [])
    assert report["counts"]["sources_with_empty_captions"] == []
    assert report["sync_check"]["running"] is False
    _later_stage1_with_a_failed_chunk(root, asset)
    (row,) = _report(service.export_finalcut_timeline(root))["sources"]
    assert row["caption_gaps"] == [{"start": "1/1", "end": "2/1", "reason": "connection: 끊김"}]


def test_a_source_whose_transcript_has_no_cues_is_reported_without_captions(tmp_path: Path):
    """0008 꼴 — 원본 자막 문서는 있지만 큐가 0개다. 예전 리포트는 `captions: true`라고 했다."""
    service, root = _plain_project(tmp_path)
    (asset,) = service.list_source_assets(root)
    workspace = service._workspace(root)
    source_transcript.seed(workspace, asset.asset_id, [], F(2), asset.content_sha256 or "")
    path = service.export_finalcut_timeline(root)
    report = _report(path)
    (row,) = report["sources"]
    assert (row["captions"], row["caption_count"]) == (False, 0)
    # 자막의 두 「없음」을 따로 센다 — 문서가 없음(빌더) · 문서는 있고 큐가 0개(D59).
    assert report["counts"]["sources_without_captions"] == []
    assert report["counts"]["sources_with_empty_captions"] == ["plain"]
    assert row["caption_gaps"] is None  # 1단계 기록이 없다 — '없음'이 아니라 '모름'
    assert _parsed(path).findall(".//caption") == []


def test_caption_facts_never_change_the_document_bytes(tmp_path: Path):
    """D56 — 같은 입력이면 문서는 바이트까지 같다. 새 칸은 리포트에만 실린다(D59).

    빌더 쪽 증명은 `test_finalcut_caption_timing.W16_DOCUMENT_SHA256`이 그대로 지킨다.
    """
    service, root = tier_project(tmp_path, suitability="usable")
    (asset,) = service.list_source_assets(root)
    moment = datetime(2026, 10, 11, 0, 17, 44, tzinfo=UTC)
    flags = service.caption_timing_flags(root)
    plain = finalcut_export.export(service, root, now=lambda: moment, caption_timing=flags)
    gaps = {asset.asset_id: [{"start": "1/1", "end": "2/1", "reason": "x"}]}
    noted = finalcut_export.export(
        service, root, now=lambda: moment, caption_timing=flags, caption_gaps=gaps
    )
    assert plain.fcpxml != noted.fcpxml
    assert plain.fcpxml.read_bytes() == noted.fcpxml.read_bytes()
    assert _report(plain.fcpxml)["sources"][0]["caption_gaps"] is None
    assert _report(noted.fcpxml)["sources"][0]["caption_gaps"] == gaps[asset.asset_id]


def test_a_running_timing_check_is_recorded_so_the_screen_can_say_so(tmp_path: Path):
    """실측 09:17:44 보내기는 확인 작업이 보고서를 쓰는 도중이었다 — 그 사실이 리포트에 남는다."""
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace

    service, root = tier_project(tmp_path, suitability="usable")
    latest = service.latest_job(root, raw_service.SYNC_CHECK_JOB_KIND)
    if latest is not None:
        _wait_terminal(service, root, latest.job_id)
    store = JobStore(RawWorkspace.open(root))
    record, _ = store.insert_deduplicated(raw_service.SYNC_CHECK_JOB_KIND, "still-measuring", 0)
    store.update(record.job_id, status="running")
    section = _report(service.export_finalcut_timeline(root))["sync_check"]
    assert section["running"] is True


# ─── W-26 — 프로젝트 방향은 켠 길이가 많은 쪽 (D66) ──────────────────────────


def _rotated(target: Path, *, seconds: str = "2") -> Path:
    """가로로 저장된 160x90에 90° 회전 선언 — 아이폰 세로 촬영처럼 재생 화면은 90x160이다."""
    plain = _encode(target.with_name(target.stem + "-coded.mov"), seconds=seconds)
    result = subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-display_rotation", "90", "-i", str(plain),
         "-c:v", "copy", "-c:a", "copy", str(target)],
        capture_output=True,
    )  # fmt: skip
    assert result.returncode == 0, result.stderr
    plain.unlink()
    return target


def _import(service: RawWorkspaceService, root: Path, source: Path) -> None:
    handle = service.import_selected_video(root, source)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"


def _sequence_size(path: Path) -> tuple[str, str]:
    root_element = _parsed(path)
    sequence = root_element.find(".//sequence")
    assert sequence is not None
    fmt = root_element.find(f"./resources/format[@id='{sequence.attrib['format']}']")
    assert fmt is not None
    return fmt.attrib["width"], fmt.attrib["height"]


def test_a_rotated_source_exports_its_display_size_and_a_portrait_project(tmp_path: Path):
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "세로")
    _import(service, root, _rotated(tmp_path / "card" / "portrait.mov"))
    (asset,) = service.list_source_assets(root)
    assert (asset.width, asset.height) == (90, 160)  # 가져오기 기록도 재생 화면 크기다
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    (fmt,) = _parsed(path).findall("./resources/format")
    assert (fmt.attrib["width"], fmt.attrib["height"]) == ("90", "160")
    assert _sequence_size(path) == ("90", "160")
    assert _report(path)["counts"]["orientation"] == "portrait"
    assert b"adjust-conform" not in path.read_bytes()


@pytest.mark.parametrize(
    ("portrait_seconds", "landscape_seconds", "expected"),
    [("4", "2", ("90", "160")), ("2", "4", ("160", "90"))],
)
def test_mixed_rotated_and_plain_sources_follow_the_longer_side(
    tmp_path: Path, portrait_seconds: str, landscape_seconds: str, expected: tuple[str, str]
):
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "섞임")
    _import(service, root, _rotated(tmp_path / "card" / "portrait.mov", seconds=portrait_seconds))
    _import(service, root, _encode(tmp_path / "card" / "landscape.mov", seconds=landscape_seconds))
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    sizes = {(f.attrib["width"], f.attrib["height"]) for f in _parsed(path).iter("format")}
    assert sizes == {("90", "160"), ("160", "90")}  # 원본마다 제 화면 크기 그대로
    assert _sequence_size(path) == expected
    side = "portrait" if expected == ("90", "160") else "landscape"
    assert _report(path)["counts"]["orientation"] == side


# ─── W-27d — 프레임레이트가 섞이면 많은 쪽 레이트 하나로 (D71) ─────────────────


def test_mixed_rate_sources_export_one_project_at_the_majority_rate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    """실제 ffmpeg 원본 30fps 4초 + 24fps 2초 — 거부하지 않고 30p 프로젝트 하나, 24p 클립은 제
    포맷에 시간을 지키는 `conform-rate`(scaleEnabled="0")."""
    service = RawWorkspaceService()
    root = tmp_path / "project"
    service.create_workspace(root, "레이트")
    _import(service, root, _encode(tmp_path / "card" / "a30.mov", seconds="4", rate="30"))
    _import(service, root, _encode(tmp_path / "card" / "b24.mov", seconds="2", rate="24"))
    path = service.export_finalcut_timeline(root)
    _assert_valid(path)
    document = _parsed(path)
    rates = {f.attrib["id"]: f.attrib["frameDuration"] for f in document.iter("format")}
    assert sorted(rates.values()) == ["1/24s", "1/30s"]
    sequence = document.find(".//sequence")
    assert sequence is not None and rates[sequence.attrib["format"]] == "1/30s"
    clips = {clip.attrib["name"]: clip for clip in _spine(path)}
    assert clips["a30"].find("conform-rate") is None
    conform = clips["b24"][0]
    assert conform.tag == "conform-rate"
    assert conform.attrib == {"scaleEnabled": "0", "srcFrameRate": "24"}
    assert clips["b24"].attrib["duration"] == "60/30s"  # 2초 그대로 — 48프레임이 아니다
    report = _report(path)
    counts = report["counts"]
    assert counts["frame_rate"] == "30" and counts["conformed_sources"] == ["b24"]
    assert counts["conformed_clips"] == 1 and counts["sequence_frames"] == 120 + 60
    facts = {row["name"]: row["frame_duration"] for row in report["sources"]}
    assert facts == {"a30": "1/30", "b24": "1/24"}
    # CLI도 맞춰 재생되는 영상과 손댄 자막 조각 수를 말한다(합성 원본에는 자막이 없어 0개).
    args = _parser().parse_args(["media", "finalcut", "--workspace", str(root)])
    assert media_commands.media_command(args) == 0
    printed = "".join(capsys.readouterr().out.split())  # rich가 좁은 폭에서 줄을 접는다
    assert "프레임레이트가다른영상1개(b24)는프로젝트30fps에맞춰재생된다" in printed
    assert "한프레임보다짧게남아싣지않은조각0개" in printed
