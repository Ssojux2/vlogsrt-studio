"""최종 편집 입출력(W-66, D117) — 초안 폴더의 `edit/`에만 쓰고, 초안 파일은 바이트 그대로다.

읽기는 쓰지 않고, 저장은 `final.srt` → `final-edit.json`(SRT sha) 차례이며, 리비전이 다르면 덮지
않는다. 초안 지문이 바뀌면 낡은 편집으로 거부하고, 3단계 입구(`load_for_burn`)는 어긋난 쌍을
받지 않는다.
"""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt.project.raw import final_edit, final_edit_io
from vlogsrt.project.raw.final_edit import FinalEditError
from vlogsrt.project.raw.workspace import RawWorkspace

SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\n앞\n\n"
    "2\n00:00:03,000 --> 00:00:03,500\n안\n\n"
    "3\n00:00:06,000 --> 00:00:07,000\n뒤\n"
)
MOMENT = datetime(2026, 11, 12, 1, 2, 3, tzinfo=UTC)


def _workspace(tmp_path: Path) -> RawWorkspace:
    return RawWorkspace.create(tmp_path / "ws", "편집")


def _plant(workspace: RawWorkspace, stamp: str = "20261112T010203Z", srt: str = SRT) -> Path:
    folder = workspace.root / "export" / stamp
    folder.mkdir(parents=True)
    (folder / "draft.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(64))
    (folder / "draft.srt").write_text(srt, encoding="utf-8")
    report = {
        "schema": "vlogsrt-render-export/v1",
        "sequence": {"frame_duration": {"num": 1, "den": 30}},
        "total_frames": 300,
        "total_seconds": {"num": 10, "den": 1},
        "parts": [],
    }
    (folder / "render_report.json").write_text(json.dumps(report), encoding="utf-8")
    workspace.register_media_file(folder / "draft.mp4", "artifact")
    return folder


def _bytes(folder: Path) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in folder.iterdir() if path.is_file()}


def _save(workspace: RawWorkspace, model: dict, doc: dict, base: int, **kwargs: object) -> dict:
    payload = {**doc, "seedSrt": model["seedSrt"]}
    return final_edit_io.save_edit(
        workspace, model["exportId"], base, payload, now=lambda: MOMENT, **kwargs
    )


def test_open_seeds_from_newest_srt_without_writing(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    folder = _plant(workspace)
    before = _bytes(folder)
    model = final_edit_io.open_edit(workspace)
    assert model["exportId"] == folder.name and model["revision"] == 0
    assert model["seedSrt"] == "draft.srt" and model["newerSeed"] is False
    assert model["doc"]["clips"] == [[0, 300]]
    assert [cue[0] for cue in model["doc"]["cues"]] == ["c1", "c2", "c3"]
    assert model["mediaFileId"] and model["frameDuration"] == {"num": 1, "den": 30}
    assert not (folder / "edit").exists() and _bytes(folder) == before


def test_save_writes_srt_then_record_and_keeps_draft_bytes(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    folder = _plant(workspace)
    before = _bytes(folder)
    model = final_edit_io.open_edit(workspace)
    draft = final_edit.DraftFacts(Fraction(1, 30), 300)
    document = final_edit.from_payload(model["doc"], draft)
    cut = final_edit.ripple_delete(document, 90, 120)  # 3초–4초를 지운다 → c2가 가려진다
    result = _save(workspace, model, final_edit.to_payload(cut), 0)
    assert result["revision"] == 1
    assert result["summary"]["hidden_by_cut"] == ["c2"]
    assert result["summary"]["removed_ranges"] == [[90, 120]]
    srt = (folder / "edit" / "final.srt").read_bytes()
    assert srt.decode("utf-8") == (
        "1\n00:00:01,000 --> 00:00:02,000\n앞\n\n2\n00:00:05,000 --> 00:00:06,000\n뒤\n"
    )
    record = json.loads((folder / "edit" / "final-edit.json").read_text(encoding="utf-8"))
    assert record["srt"]["sha256"] == hashlib.sha256(srt).hexdigest()
    assert (
        record["seed_srt"]["name"] == "draft.srt" and record["saved_at"] == "2026-11-12T01:02:03Z"
    )
    assert _bytes(folder) == before  # 초안 파일은 바이트 그대로
    reopened = final_edit_io.open_edit(workspace)
    assert reopened["revision"] == 1 and reopened["doc"] == final_edit.to_payload(cut)


def test_stale_revision_is_a_conflict_unless_forced(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _plant(workspace)
    model = final_edit_io.open_edit(workspace)
    assert _save(workspace, model, model["doc"], 0)["revision"] == 1
    assert _save(workspace, model, model["doc"], 0) == {"conflict": True, "revision": 1}
    assert _save(workspace, model, model["doc"], 0, force=True)["revision"] == 2


def test_server_revalidates_the_whole_document(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    _plant(workspace)
    model = final_edit_io.open_edit(workspace)
    broken = {**model["doc"], "clips": [[0, 999]]}
    with pytest.raises(FinalEditError):
        _save(workspace, model, broken, 0)
    with pytest.raises(FinalEditError, match="초안 SRT"):
        final_edit_io.save_edit(
            workspace, model["exportId"], 0, {**model["doc"], "seedSrt": "../x"}
        )


def test_changed_draft_makes_the_edit_stale(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    folder = _plant(workspace)
    model = final_edit_io.open_edit(workspace)
    _save(workspace, model, model["doc"], 0)
    report = folder / "render_report.json"
    report.write_text(report.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(FinalEditError, match="낡은 편집"):
        final_edit_io.open_edit(workspace)
    with pytest.raises(FinalEditError, match="낡은 편집"):
        final_edit_io.load_for_burn(folder)


def test_newer_seed_and_reseed_keep_clips(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    folder = _plant(workspace)
    model = final_edit_io.open_edit(workspace)
    cut = {**model["doc"], "clips": [[0, 90], [120, 300]]}
    _save(workspace, model, cut, 0)
    (folder / "draft-20261113T000000Z.srt").write_text(
        "1\n00:00:00,500 --> 00:00:01,500\n새 자막\n", encoding="utf-8"
    )
    again = final_edit_io.open_edit(workspace, folder.name, reseed=True)
    assert again["newerSeed"] is True and again["doc"]["clips"] == [[0, 90], [120, 300]]
    assert again["reseedCues"] == [["c1", 500, 1500, "새 자막"]]
    assert again["reseedSrt"] == "draft-20261113T000000Z.srt"


def test_exports_list_and_older_edits(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    old = _plant(workspace, "20261111T000000Z")
    old_model = final_edit_io.open_edit(workspace, old.name)
    _save(workspace, old_model, old_model["doc"], 0)
    new = _plant(workspace, "20261112T000000Z")
    model = final_edit_io.open_edit(workspace)
    assert model["exportId"] == new.name
    assert [row["id"] for row in model["exports"]] == [new.name, old.name]
    assert model["olderEdits"] == [old.name]
    with pytest.raises(FinalEditError, match="없다"):
        final_edit_io.open_edit(workspace, "../escape")


def test_no_export_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FinalEditError, match="초안이 없다"):
        final_edit_io.open_edit(_workspace(tmp_path))


def test_load_for_burn_contract(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    folder = _plant(workspace)
    with pytest.raises(FinalEditError, match="저장한 최종 편집이 없다"):
        final_edit_io.load_for_burn(folder)
    assert final_edit_io.saved_edit_srt(folder) is None
    model = final_edit_io.open_edit(workspace)
    _save(workspace, model, {**model["doc"], "clips": [[0, 90], [120, 300]]}, 0)
    burn = final_edit_io.load_for_burn(folder)
    assert burn.video == folder / "draft.mp4" and burn.srt == folder / "edit" / "final.srt"
    assert final_edit_io.saved_edit_srt(folder) == burn.srt
    assert final_edit_io.draft_of_edit_srt(burn.srt) == folder
    assert final_edit_io.draft_of_edit_srt(folder / "draft.srt") is None
    assert final_edit_io.draft_of_edit_srt(tmp_path / "edit" / "final.srt") is None
    assert burn.srt_sha256 == hashlib.sha256(burn.srt.read_bytes()).hexdigest()
    assert burn.keep == ((0, 90, Fraction(0), Fraction(3)), (120, 300, Fraction(4), Fraction(10)))
    assert burn.cue_count == 2 and burn.hidden_count == 1 and burn.revision == 1
    assert burn.total_frames == 300
    (folder / "edit" / "final.srt").write_text("고친 자막", encoding="utf-8")
    with pytest.raises(FinalEditError, match="다르다"):
        final_edit_io.load_for_burn(folder)
    _save(workspace, model, {**model["doc"], "clips": []}, 1)
    with pytest.raises(FinalEditError, match="남은 클립이 없다"):
        final_edit_io.load_for_burn(folder)


def test_waveform_is_measured_once_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = _workspace(tmp_path)
    folder = _plant(workspace)
    calls: list[tuple[float, float, int, int]] = []

    def fake(path: Path, start: float, length: float, cfg: object) -> list[float]:
        calls.append((start, length, cfg.cut_probe_rate, cfg.cut_quiet_window_ms))  # type: ignore[attr-defined]
        assert path == folder / "draft.mp4"
        return [0.0, 0.5, 1.0]

    monkeypatch.setattr(final_edit_io.preprocess, "amplitudes", fake)
    first = final_edit_io.waveform(workspace, folder.name)
    assert base64.b64decode(first["peaks_b64"]) == bytes([0, 128, 255])
    assert first["count"] == 3 and first["bin_ms"] == 20
    assert final_edit_io.waveform(workspace, folder.name) == first
    assert calls == [(0.0, 10.0, 4000, 20)]
    assert (folder / "edit" / "waveform.json").is_file()


def test_write_atomic_refuses_names_outside_edit_files(tmp_path: Path) -> None:
    edit = tmp_path / "edit"
    edit.mkdir()
    with pytest.raises(FinalEditError):
        final_edit_io._write_atomic(edit, "draft.srt", b"x")
    with pytest.raises(FinalEditError):
        final_edit_io._write_atomic(tmp_path, "final.srt", b"x")
