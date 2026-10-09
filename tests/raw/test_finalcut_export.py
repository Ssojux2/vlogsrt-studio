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
from vlogsrt.project.raw import finalcut_export, media_refs, review_workspace
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


def _encode(target: Path, *extra: str, seconds: str = "2") -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=160x90:rate=25:duration={seconds}",
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
    assert any("videoFormatName" in line and broken.identity in line for line in warnings)


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
