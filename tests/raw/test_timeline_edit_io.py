"""원본 범위 타임라인 만들기·열기·저장(W-71b, D126) — 렌더하지 않고 `timeline/<시각>/`에만 쓴다.

초안 내보내기와 같은 재료·같은 계획에서 추천 조각을 원본 프레임 구간 클립으로 심는다 — 손대지 않은
타임라인의 배치·SRT가 렌더 계획과 같다. 저장은 리비전이 다르면 덮지 않고, 심은 리비전은 파일의
것만이며, 열기는 바뀐 원본 자막을 알릴 뿐 쓰지 않는다. 지원 밖 원본·심지 않은 원본은 요약에 이름이
남고, 자막 상한을 넘으면 수와 원본을 대고 거부한다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_render_export import (
    UTTERANCES,
    Folder,
    _encode,
    _identity,
    _trip,
)
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import preprocess
from vlogsrt.project.raw import (
    render_export,
    render_plan,
    timeline_edit,
    timeline_edit_io,
)
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.config import RENDER_SETTINGS
from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS
from vlogsrt.project.raw.timeline_edit import TimelineEditError

F = Fraction
MOMENT = datetime(2026, 11, 13, 1, 2, 3, tzinfo=UTC)


@pytest.fixture(scope="module")
def trip(tmp_path_factory: pytest.TempPathFactory) -> Folder:
    import shutil

    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        pytest.skip("ffmpeg/ffprobe가 없는 환경")
    folder = tmp_path_factory.mktemp("timeline_trip") / "제주 여행"
    _encode(folder / "A_uhd.mp4", "3840x2160", 30, 2, created="2026-09-01T10:05:00Z")
    _encode(folder / "B_hd.mp4", "1920x1080", 30, 9, created="2026-09-01T10:00:00Z")
    _encode(folder / "C_tall.mp4", "1080x1920", 24, 2, created="2026-09-01T09:55:00Z")
    originals = {name: _identity(folder / name) for name in UTTERANCES}
    return _trip(folder, originals)


def _tree(folder: Path) -> dict[str, tuple[int, int]]:
    """폴더의 파일 전부(크기·mtime) — 작업공간 `timeline/`과 DB 파일만 뺀다."""
    rows: dict[str, tuple[int, int]] = {}
    for path in folder.rglob("*"):
        relative = path.relative_to(folder).as_posix()
        if not path.is_file() or relative.startswith("_vlogsrt/timeline/"):
            continue
        if ".sqlite3" in path.name:
            continue
        facts = path.stat()
        rows[relative] = (facts.st_size, facts.st_mtime_ns)
    return rows


def _no_render(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("타임라인으로 보내기가 렌더를 불렀다")

    for name in ("render_part", "concat_parts"):
        monkeypatch.setattr(preprocess, name, refuse)
    monkeypatch.setattr(render_export, "render_pieces", refuse)


def _plan(trip: Folder, settings: Any = RENDER_SETTINGS) -> render_plan.RenderPlan:
    gathered = render_export.gather_sources(trip.service, trip.root)
    return render_export._build(gathered, settings)


def _create(trip: Folder, moment: datetime = MOMENT, **kwargs: Any) -> Any:
    return timeline_edit_io.create_timeline(trip.service, trip.root, now=lambda: moment, **kwargs)


def _names(trip: Folder) -> dict[str, str]:
    return {a.source_name: a.asset_id for a in trip.service.list_source_assets(trip.root)}


def test_sending_renders_nothing_and_writes_only_one_timeline_folder(
    trip: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_render(monkeypatch)
    before = _tree(trip.folder)
    created = _create(trip, datetime(2026, 11, 13, 0, 0, 0, tzinfo=UTC))
    assert _tree(trip.folder) == before
    assert created.folder.parent == trip.root / timeline_edit_io.TIMELINE_FOLDER
    assert sorted(p.name for p in created.folder.iterdir()) == [timeline_edit_io.TIMELINE_NAME]
    for name, (sha, mtime) in trip.originals.items():
        assert _identity(trip.folder / name) == (sha, mtime)


def test_the_untouched_timeline_is_the_render_plan(trip: Folder) -> None:
    plan = _plan(trip)
    created = _create(trip, datetime(2026, 11, 13, 0, 1, 0, tzinfo=UTC))
    document = created.document
    assert [(c.source_id, c.start_frame, c.end_frame) for c in document.clips] == [
        (p.source_id, p.start_frame, p.end_frame) for p in plan.parts
    ]
    spans = timeline_edit.layout(document)
    assert [(s.out_start, s.out_frames) for s in spans] == [
        (p.output_start_frame, p.output_frames) for p in plan.parts
    ]
    assert timeline_edit.edited_frames(document) == plan.total_frames
    assert document.facts.sequence == plan.sequence
    # 같은 원본의 뺀 구간을 걸친 자막이 없는 픽스처라 초안 SRT와 바이트까지 같다(D125).
    assert timeline_edit.srt_text(document) == render_plan.srt_text(plan)


def test_the_file_carries_facts_seeded_revisions_and_the_summary(trip: Folder) -> None:
    created = _create(trip, datetime(2026, 11, 13, 0, 2, 0, tzinfo=UTC))
    record = json.loads((created.folder / timeline_edit_io.TIMELINE_NAME).read_text("utf-8"))
    assert record["schema"] == timeline_edit_io.FILE_SCHEMA
    assert record["revision"] == 1
    names = _names(trip)
    sources = {row["name"]: row for row in record["facts"]["sources"]}
    assert set(sources) == set(UTTERANCES)
    assert sources["C_tall.mp4"]["frame_duration"] == {"num": 1, "den": 24}
    seeded = record["document"]["seeded"]
    for name in UTTERANCES:
        current = trip.service.source_transcript(trip.root, names[name])
        assert current is not None and seeded[names[name]] == current["revision"]
    assert record["seed"]["unsupported"] == [] and record["seed"]["unseeded"] == []
    assert record["seed"]["empty"] == 0 and record["seed"]["clamped"] == 0
    removed = record["summary"]["removed_ranges"]
    assert {row[0] for row in removed} <= set(names.values())


def test_opening_writes_nothing_and_names_changed_transcripts(trip: Folder) -> None:
    created = _create(trip, datetime(2026, 11, 13, 0, 3, 0, tzinfo=UTC))
    path = created.folder / timeline_edit_io.TIMELINE_NAME
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    model = trip.service.timeline_open(trip.root)
    assert model["timelineId"] == created.folder.name and model["revision"] == 1
    assert model["changedSources"] == []
    assert model["settings"]["maxCues"] == FINAL_EDIT_SETTINGS.max_cues
    asset_id = _names(trip)["B_hd.mp4"]
    current = trip.service.source_transcript(trip.root, asset_id)
    assert current is not None
    first = current["utterances"][0]
    trip.service.edit_source_transcript(
        trip.root,
        asset_id,
        current["revision"],
        action="edit",
        utterance_id=first["utterance_id"],
        text="바다가 정말 보여요.",
    )
    model = trip.service.timeline_open(trip.root)
    assert [row["name"] for row in model["changedSources"]] == ["B_hd.mp4"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_saving_bumps_the_revision_and_a_stale_base_conflicts(trip: Folder) -> None:
    created = _create(trip, datetime(2026, 11, 13, 0, 4, 0, tzinfo=UTC))
    timeline_id = created.folder.name
    path = created.folder / timeline_edit_io.TIMELINE_NAME
    blade = timeline_edit.blade(created.document, 10)
    payload = timeline_edit.to_payload(blade)
    saved = trip.service.timeline_save(trip.root, timeline_id, 1, payload)
    assert saved["revision"] == 2 and saved["summary"]["clips"] == len(blade.clips)
    record = json.loads(path.read_text("utf-8"))
    assert record["created_at"] == "2026-11-13T00:04:00Z"
    assert record["facts"] == json.loads(json.dumps(created.record["facts"]))
    stale = trip.service.timeline_save(trip.root, timeline_id, 1, payload)
    assert stale == {"conflict": True, "revision": 2}
    forced = trip.service.timeline_save(trip.root, timeline_id, 1, payload, force=True)
    assert forced["revision"] == 3
    reopened = trip.service.timeline_open(trip.root, timeline_id)
    assert reopened["revision"] == 3 and reopened["doc"] == payload


def test_saving_refuses_changed_seeded_revisions_and_broken_documents(trip: Folder) -> None:
    created = _create(trip, datetime(2026, 11, 13, 0, 5, 0, tzinfo=UTC))
    timeline_id = created.folder.name
    path = created.folder / timeline_edit_io.TIMELINE_NAME
    before = path.read_bytes()
    payload = timeline_edit.to_payload(created.document)
    forged = {**payload, "seeded": dict.fromkeys(payload["seeded"], "다른-리비전")}  # type: ignore[union-attr]
    with pytest.raises(TimelineEditError, match="seeded"):
        trip.service.timeline_save(trip.root, timeline_id, 1, forged)
    clips = payload["clips"]
    assert isinstance(clips, list)
    broken = {**payload, "clips": [[clips[0][0], 5, 2], *clips[1:]]}
    with pytest.raises(TimelineEditError):
        trip.service.timeline_save(trip.root, timeline_id, 1, broken)
    assert path.read_bytes() == before


def test_sending_again_makes_a_new_folder_and_keeps_the_edited_one(trip: Folder) -> None:
    moment = datetime(2026, 11, 13, 0, 6, 0, tzinfo=UTC)
    first = _create(trip, moment)
    trip.service.timeline_save(
        trip.root, first.folder.name, 1, timeline_edit.to_payload(first.document)
    )
    second = _create(trip, moment)
    assert second.folder.name == f"{first.folder.name}_2"
    assert trip.service.timeline_open(trip.root)["timelineId"] == second.folder.name
    assert trip.service.timeline_open(trip.root, first.folder.name)["revision"] == 2
    assert trip.service.has_timeline(trip.root)


def test_an_unsupported_source_is_left_out_and_named_in_the_summary(
    trip: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = preprocess.probe_extraction_clock

    def mapped(path: Path, **kwargs: Any) -> preprocess.RawVideoClock:
        clock = real(path, **kwargs)
        return (
            dataclasses.replace(clock, conform_kind="map") if path.name == "C_tall.mp4" else clock
        )

    monkeypatch.setattr(preprocess, "probe_extraction_clock", mapped)
    created = _create(trip, datetime(2026, 11, 13, 0, 7, 0, tzinfo=UTC))
    unsupported = created.record["seed"]["unsupported"]
    assert [(row["name"], row["reason"]) for row in unsupported] == [
        ("C_tall.mp4", render_plan.DROP_UNSUPPORTED_GRID)
    ]
    tall = _names(trip)["C_tall.mp4"]
    assert tall not in {s.source_id for s in created.document.facts.sources}
    assert tall not in dict(created.document.seeded)
    model = trip.service.timeline_open(trip.root)
    assert model["seed"]["unsupported"][0]["name"] == "C_tall.mp4"


def test_sources_without_clips_are_not_seeded_and_are_named(trip: Folder) -> None:
    plan = _plan(trip)
    longest = max(plan.parts, key=lambda part: part.end - part.start)
    strict = dataclasses.replace(RENDER_SETTINGS, min_part_seconds=longest.end - longest.start)
    created = _create(trip, datetime(2026, 11, 13, 0, 8, 0, tzinfo=UTC), settings=strict)
    clipped = {clip.source_id for clip in created.document.clips}
    assert set(dict(created.document.seeded)) == clipped
    every = {source.source_id for source in created.document.facts.sources}
    unseeded = {row["source_id"] for row in created.record["seed"]["unseeded"]}
    assert unseeded == every - clipped and unseeded
    assert {row["name"] for row in created.record["seed"]["unseeded"]} <= set(UTTERANCES)
    # 클립이 없는 원본도 사실에 남아 통째로 뺀 구간으로 보인다(D35).
    removed = {row[0] for row in created.record["summary"]["removed_ranges"]}
    assert unseeded <= removed


def test_too_many_cues_are_refused_with_the_count_and_the_largest_sources(
    trip: Folder,
) -> None:
    base = trip.root / timeline_edit_io.TIMELINE_FOLDER
    before = sorted(p.name for p in base.iterdir()) if base.is_dir() else []
    tight = dataclasses.replace(FINAL_EDIT_SETTINGS, max_cues=2)
    with pytest.raises(TimelineEditError, match=r"6줄로 상한 2줄") as caught:
        # Gemini 단어 묶기는 문장 끝 부호에서 줄을 나눠 A의 두 문장이 두 줄이다(D163).
        _create(trip, datetime(2026, 11, 13, 0, 9, 0, tzinfo=UTC), edit_settings=tight)
    assert "B_hd.mp4 3줄" in str(caught.value)
    after = sorted(p.name for p in base.iterdir()) if base.is_dir() else []
    assert after == before


def test_the_writer_refuses_any_other_place(tmp_path: Path) -> None:
    folder = tmp_path / "export" / "20261113T000000Z"
    folder.mkdir(parents=True)
    with pytest.raises(TimelineEditError, match="쓸 수 없는 자리"):
        timeline_edit_io._write_atomic(folder, timeline_edit_io.TIMELINE_NAME, b"{}")
    inside = tmp_path / timeline_edit_io.TIMELINE_FOLDER / "20261113T000000Z"
    inside.mkdir(parents=True)
    with pytest.raises(TimelineEditError, match="쓸 수 없는 자리"):
        timeline_edit_io._write_atomic(inside, "draft.srt", b"")
    assert list(inside.iterdir()) == [] and list(folder.iterdir()) == []


def test_opening_without_a_timeline_says_to_send_one(tmp_path: Path) -> None:
    with pytest.raises(TimelineEditError, match="타임라인으로 보내기"):
        timeline_edit_io.open_timeline(raw_service.RawWorkspaceService(), tmp_path)


def test_the_service_job_creates_a_timeline_and_the_activity_label_names_it(
    trip: Folder,
) -> None:
    from ui import workspace_service

    handle = trip.service.start_timeline_export(trip.root)
    record = _wait_terminal(trip.service, trip.root, handle.job_id)
    assert record.status == "succeeded", record.error
    assert record.kind == raw_service.TIMELINE_EXPORT_JOB_KIND == "timeline_export"
    assert trip.service.has_timeline(trip.root)
    assert workspace_service.ACTIVITY_JOB_LABELS["timeline_export"] == "타임라인으로 보내기"
    assert "timeline_export" in workspace_service.CLOSE_CANCELS_JOB_KINDS


# ── 재생 미디어(W-71c) — 클립이 있는 원본마다 원본 전체 미리보기 ─────────────────────────


def test_timeline_media_names_each_clipped_sources_ready_preview(trip: Folder) -> None:
    """원본마다 등록 미리보기(`/media/<id>`)와 브라우저 시각 원점 — 읽기만 하고 쓰지 않는다."""
    created = _create(trip, MOMENT.replace(minute=30))
    opened = trip.service.timeline_open(trip.root, created.folder.name)
    clipped = {clip[0] for clip in opened["doc"]["clips"]}
    before = _tree(trip.folder)
    media = trip.service.timeline_media(trip.root, created.folder.name)
    assert _tree(trip.folder) == before
    assert media["timelineId"] == created.folder.name
    assert set(media["sources"]) == clipped
    for source_id, row in media["sources"].items():
        assert row["state"] == "ready", source_id
        assert row["origin"] == "0"
        resolved = trip.service.resolve_media(trip.root, row["fileId"])
        assert resolved.kind == "preview"
        assert resolved.path.suffix == ".mp4"
        assert row["width"] > 0 and row["height"] > 0


def test_a_source_without_a_ready_preview_says_so_and_gives_no_media(
    trip: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """미리보기가 없으면 상태만 — 등록 ID·원점을 지어내지 않는다(화면은 「준비 안 됨」)."""
    from vlogsrt.project.raw import visual_review

    created = _create(trip, MOMENT.replace(minute=31))
    real = visual_review.source_previews

    def missing(workspace: Any, source_ids: Any, active: Any) -> Any:
        found = real(workspace, source_ids, active)
        first = sorted(found)[0]
        found[first] = dataclasses.replace(
            found[first], state="missing", registered_file_id=None, browser_origin_seconds=None
        )
        return found

    monkeypatch.setattr(visual_review, "source_previews", missing)
    media = trip.service.timeline_media(trip.root, created.folder.name)
    first = sorted(media["sources"])[0]
    assert media["sources"][first] == {
        "state": "missing",
        "fileId": None,
        "origin": None,
        "width": None,
        "height": None,
        "rotation": None,
    }


# ── 범위 조정의 등급 띠(W-71d) — 표시만 ────────────────────────────────────────────


def test_timeline_tiers_give_each_clipped_source_its_tier_spans_on_the_source_clock(
    trip: Folder,
) -> None:
    """원본마다 등급 구간(원본 시계 초, 원점 0) — 렌더 계획이 쓰는 같은 보고서다. 쓰지 않는다."""
    created = _create(trip, MOMENT.replace(minute=32))
    _, sources = timeline_edit_io.clipped_sources(trip.root, created.folder.name)
    before = _tree(trip.folder)
    tiers = trip.service.timeline_tiers(trip.root, sources)
    assert _tree(trip.folder) == before
    assert set(tiers) == set(sources)
    gathered = render_export.gather_sources(trip.service, trip.root)
    by_id = {item.source.source_id: item.source for item in gathered}
    for source_id, rows in tiers.items():
        expected = [
            [str(piece.start), str(piece.end), piece.tier] for piece in by_id[source_id].tiers or ()
        ]
        assert rows == expected, source_id
        assert rows and all(F(row[0]) < F(row[1]) for row in rows)


# ─── 원본 다시 넣기(W-71g) — 처음 넣는 원본은 지금 원본 자막을 심고 새 리비전으로 쓴다 ─────


def _strict(trip: Folder, minute: int) -> Any:
    """클립이 없는 원본이 생기게 — 가장 긴 조각만 남는 최소 길이로 만든다."""
    plan = _plan(trip)
    longest = max(plan.parts, key=lambda part: part.end - part.start)
    strict = dataclasses.replace(RENDER_SETTINGS, min_part_seconds=longest.end - longest.start)
    return _create(trip, datetime(2026, 11, 14, 0, minute, 0, tzinfo=UTC), settings=strict)


def _record_of(created: Any) -> dict[str, Any]:
    return json.loads((created.folder / "timeline.json").read_text(encoding="utf-8"))


def test_adding_a_never_clipped_source_seeds_its_transcript_and_writes_a_revision(
    trip: Folder,
) -> None:
    created = _strict(trip, 1)
    clipped = {clip.source_id for clip in created.document.clips}
    source = next(s for s in created.document.facts.sources if s.source_id not in clipped)
    payload = timeline_edit.to_payload(created.document)
    got = trip.service.timeline_add_source(
        trip.root, created.folder.name, 1, payload, source.source_id
    )
    assert got["revision"] == 2
    _, revision, document = timeline_edit_io.read_document(trip.root, created.folder.name)
    assert revision == 2 and timeline_edit.to_payload(document) == got["doc"]
    assert (source.source_id, 0, source.frame_count) in [
        (c.source_id, c.start_frame, c.end_frame) for c in document.clips
    ]
    transcript = trip.service.source_transcript(trip.root, source.source_id)
    assert transcript is not None
    assert dict(document.seeded)[source.source_id] == str(transcript["revision"])
    lines = [r for r in transcript["utterances"] if render_plan.caption_text(r["text"]).strip()]
    mine = [c for c in document.cues if c.source_id == source.source_id]
    assert len(mine) == got["cues"] == len(lines) > 0
    ids = [c.cue_id for c in document.cues]
    assert len(ids) == len(set(ids))
    # 넣은 뒤 평범한 저장이 지나간다(심은 리비전이 파일과 같다).
    saved = timeline_edit_io.save_timeline(trip.root, created.folder.name, 2, got["doc"])
    assert saved["revision"] == 3


def test_a_source_dropped_from_the_list_lands_where_it_was_dropped(trip: Folder) -> None:
    """W-111(D185) — 끌어 놓은 원본은 촬영 차례 자리가 아니라 놓은 자리(여기서는 끝)에
    원본 전체로 선다."""
    created = _strict(trip, 1)
    clipped = {clip.source_id for clip in created.document.clips}
    source = next(s for s in created.document.facts.sources if s.source_id not in clipped)
    payload = timeline_edit.to_payload(created.document)
    trip.service.timeline_add_source(
        trip.root, created.folder.name, 1, payload, source.source_id, at=len(created.document.clips)
    )
    _, revision, document = timeline_edit_io.read_document(trip.root, created.folder.name)
    last = document.clips[-1]
    assert revision == 2 and len(document.clips) == len(created.document.clips) + 1
    assert (last.source_id, last.start_frame, last.end_frame) == (
        source.source_id,
        0,
        source.frame_count,
    )


def test_adding_a_source_refuses_a_stale_revision_or_a_clipped_source_and_writes_nothing(
    trip: Folder,
) -> None:
    created = _strict(trip, 2)
    clipped = {clip.source_id for clip in created.document.clips}
    other = next(s.source_id for s in created.document.facts.sources if s.source_id not in clipped)
    payload = timeline_edit.to_payload(created.document)
    before = (created.folder / "timeline.json").read_bytes()
    stale = trip.service.timeline_add_source(trip.root, created.folder.name, 9, payload, other)
    assert stale == {"conflict": True, "revision": 1}
    with pytest.raises(TimelineEditError, match="이미 클립"):
        trip.service.timeline_add_source(
            trip.root, created.folder.name, 1, payload, next(iter(clipped))
        )
    assert (created.folder / "timeline.json").read_bytes() == before
    assert _record_of(created)["revision"] == 1


def test_edited_cues_are_saved_reopened_and_carried_into_the_srt(trip: Folder) -> None:
    """W-71i — 타임라인 화면의 자막 편집(글·나누기·넣기)은 저장의 자막이다 — 심은 리비전 그대로."""
    created = _create(trip, datetime(2026, 11, 13, 0, 9, 0, tzinfo=UTC))
    timeline_id = created.folder.name
    document = created.document
    first = timeline_edit.project(document).cues[0]
    cue = next(c for c in document.cues if c.cue_id == first.cue_id)
    edited = timeline_edit.cue_set_text(document, cue.cue_id, "고친 글", FINAL_EDIT_SETTINGS)
    middle = (cue.start_ms + cue.end_ms) // 2
    edited = timeline_edit.cue_split(edited, cue.cue_id, middle, "앞 반", "뒤 반")
    source_id, at = timeline_edit.source_ms_at(edited, 0, timeline_edit.SIDE_START)
    edited = timeline_edit.cue_insert(edited, source_id, at, at + 10, "새 자막")
    saved = trip.service.timeline_save(trip.root, timeline_id, 1, timeline_edit.to_payload(edited))
    assert saved["revision"] == 2
    reopened = trip.service.timeline_open(trip.root, timeline_id)
    assert reopened["doc"]["seeded"] == timeline_edit.to_payload(document)["seeded"]
    back = timeline_edit.from_payload(reopened["doc"], document.facts)
    assert back == edited
    srt = timeline_edit.srt_text(back)
    assert "앞 반" in srt and "뒤 반" in srt and "고친 글" not in srt
    assert "새 자막" in srt


def test_the_revision_srt_for_english_is_that_revisions_text(trip: Folder) -> None:
    """W-99(D174) — 「영어 만들기」의 재료는 저장한 리비전의 SRT다(렌더한 `draft.srt`와 같다)."""
    created = _create(trip, datetime(2026, 11, 19, 0, 0, 0, tzinfo=UTC))
    name = created.folder.name
    path = timeline_edit_io.korean_srt(trip.root, name, 1)
    assert path == created.folder / "korean-r1.srt"
    assert path.read_bytes() == timeline_edit.srt_text(created.document).encode("utf-8")
    assert timeline_edit_io.korean_srt(trip.root, name, 1) == path  # 같은 리비전은 그대로
    assert timeline_edit_io.korean_srts(trip.root, name) == [path]
    with pytest.raises(TimelineEditError):
        timeline_edit_io.korean_srt(trip.root, name, 2)  # 화면이 본 리비전이 아니다
