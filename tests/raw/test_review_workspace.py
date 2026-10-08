"""A5/A6 검토 작업공간 파사드의 계약 — 지속 리비전·재계획·측정 VTT·불변 확정.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §6·§9·§10·§12·16-A5/A6. 합성
미디어(ffmpeg lavfi)로 온전한 A3 package를 만들어, 검토 리비전이 (1) 실제 계획·
시계·분석 증거에서만 태어나고 (2) 편집이 stale-head 가드를 지키며 (3) 메타데이터
편집은 재추출 0, (4) 경계 편집은 검증된 매핑으로 새 계획·추출로 이어지며
(5) VTT 시각이 잰 발화·잰 매체 길이만 쓰며 (6) 확정이 캐시 정리·재시작 뒤에도
바이트 그대로 살아 있는지를 증명한다. 유료 호출은 0회다(재생 분석).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import review_workspace as facade_module
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import AnalysisSourceResult, PlannedInterval
from vlogsrt.project.raw.review_projection import BOUNDARY_REVIEW_NOTE, parse_review_vtt
from vlogsrt.project.raw.review_workspace_store import ReviewWorkspaceStore
from vlogsrt.project.raw.segment_review import (
    SELECTION_EXCLUDED,
    SPEECH_ANALYSIS_FAILED,
    SPEECH_AUDIO_ABSENT,
    SPEECH_DETECTED,
    SPEECH_UNCONFIRMED_EMPTY,
    StaleRevisionError,
    apply_edit,
    boundary_edit,
    content_fingerprint,
    serialize_revision,
    split_edit,
)
from vlogsrt.project.raw.workspace import DB_NAME
from vlogsrt.project.timebase import milliseconds_half_up

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAIT_SECONDS = 300.0
NOW = "2026-10-02T00:00:00+00:00"


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 미디어 생성 실패: {completed.stderr[-400:]}"


def _av_media(path: Path, seconds: str) -> None:
    """소리 있는 작은 MP4 — 재생 분석의 발화 증거가 이 원본에 묶인다."""
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=black:s=64x32:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(path),
        ]
    )


def _silent_media(path: Path, seconds: str) -> None:
    """오디오 트랙이 아예 없는 MP4 — 무성 원본은 무발화와 다른 상태다(§7.3)."""
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=black:s=64x32:r=10:d={seconds}",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ]
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _wait_terminal(service: raw.RawWorkspaceService, root: Path, job_id: str):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    return service.get_job(root, job_id)


def _ingest_card(service: raw.RawWorkspaceService, root: Path, card: Path):
    proposal = service.scan_sources(root, card)
    confirmation = service.confirm_ingest(root, proposal)
    handle = service.start_ingest_copy(root, confirmation)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    return confirmation


def _extract(service: raw.RawWorkspaceService, root: Path, **kwargs):
    handle = service.start_segment_extraction(root, **kwargs)
    return handle, _wait_terminal(service, root, handle.job_id)


def _tree_hashes(root: Path, relative: str) -> dict[str, str]:
    """디렉터리 아래 전체 파일의 해시 — 불변 증명의 밑천(상대 경로 → sha256)."""
    base = root / relative
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def _assets_by_id(service: raw.RawWorkspaceService, root: Path) -> dict[str, object]:
    return {asset.asset_id: asset for asset in service.list_source_assets(root)}


def _asset_ids_by_name(service: raw.RawWorkspaceService, root: Path) -> dict[str, str]:
    return {asset.source_name: asset.asset_id for asset in service.list_source_assets(root)}


def _luna_body(window_id: str) -> dict[str, object]:
    return {
        "window_id": window_id,
        "speech_summary": "도착해서 일정을 설명한다",
        "visual_summary": "실내에서 걷다가 문으로 향한다",
        "scene_tags": ["walking", "arrival"],
        "evidence_ids": [window_id],
        "suggested_boundary_ids": [],
        "editing_notes": [],
        "needs_review": False,
        "uncertainties": [],
    }


def _write_replay_fixtures(root: Path, asr_sources: dict[str, list[dict[str, object]]]) -> None:
    fixtures = root / "analysis" / "replay"
    fixtures.mkdir(parents=True, exist_ok=True)
    (fixtures / "asr-fixtures.json").write_text(
        json.dumps({"sources": asr_sources}, ensure_ascii=False), encoding="utf-8"
    )
    (fixtures / "luna-fixtures.json").write_text(
        json.dumps({"windows": {"window_000000": _luna_body("window_000000")}}, ensure_ascii=False),
        encoding="utf-8",
    )


def _analyze(service: raw.RawWorkspaceService, root: Path):
    handle = service.start_analysis(root)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    summary = service.analysis_overview(root)
    assert summary is not None and summary.revision.status == "complete"
    return summary


def _frame_at(clock, pts: int) -> int:
    return (pts - clock.origin_pts) // clock.frame_ticks


def _mid_split_pts(clock, segment) -> int:
    """조각 안쪽의 프레임 격자 점 — 분할 편집의 합법 지점."""
    first_frame = _frame_at(clock, segment.start_pts)
    last_frame = _frame_at(clock, segment.end_pts)
    middle = (first_frame + last_frame) // 2
    assert first_frame < middle < last_frame
    return clock.origin_pts + middle * clock.frame_ticks


@pytest.fixture(scope="session")
def spoken_media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    card = tmp_path_factory.mktemp("review_card")
    _av_media(card / "발화.mov", "4")
    _silent_media(card / "무성.mov", "3")
    return card


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    service = raw.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "검토확인")
    return root


def _prepared_workspace(
    tmp_path: Path, spoken_media: Path, *, with_analysis: bool, tile: str = "2"
):
    """카드 가져오기 → 전체 순서 → 구간 계획 → (재생 분석) → 추출까지 끝낸 작업공간."""
    service = raw.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "검토확인")
    _ingest_card(service, root, spoken_media)
    service.confirm_global_source_order(root)
    assets = _asset_ids_by_name(service, root)
    intervals = {
        assets["발화.mov"]: [
            PlannedInterval(Fraction(0), Fraction(tile)),
            PlannedInterval(Fraction(tile), Fraction(1000)),
        ],
        assets["무성.mov"]: [PlannedInterval(Fraction(0), Fraction(1000))],
    }
    plan = service.plan_segmentation(root, intervals=intervals)
    if with_analysis:
        shas = {
            asset.asset_id: (asset.content_sha256 or "")
            for asset in service.list_source_assets(root)
        }
        _write_replay_fixtures(
            root,
            {
                shas[assets["발화.mov"]]: [
                    {"start": 0.8, "end": 2.0, "text": "도착해서 일정 설명"},
                    {"start": 2.5, "end": 3.4, "text": "다음 장소 안내"},
                ]
            },
        )
        _analyze(service, root)
    _, record = _extract(service, root, plan_revision_id=plan.revision_id)
    assert record.status == "succeeded", record.error
    package = service.package_overview(root)
    assert package is not None and package.package.complete
    return service, root, plan, package.package.package_dir


# ── W1 연동 계약 — 공개 서비스가 이 파사드를 import해도 순환이 없다 ────────────


def test_no_module_level_service_import() -> None:
    """이 모듈을 먼저 import해도 service 모듈이 로드되지 않는다(순환 방지 계약)."""
    import subprocess
    import sys

    probe = (
        "import sys; import vlogsrt.project.raw.review_workspace;"
        "assert 'vlogsrt.project.raw.service' not in sys.modules, 'module-level cycle';"
        "print('clean')"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env={"PYTHONPATH": "."},
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "clean" in completed.stdout


# ── 초기화·다시 열기 ──────────────────────────────────────────────────────────


@needs_ffmpeg
def test_initialize_load_head_and_explicit_no_transcript(
    tmp_path: Path, spoken_media: Path
) -> None:
    """분석 없는 초기화 — 전사 없음이 명시적으로 남고 다시 열기는 지문을 다시 잰다."""
    service, root, plan, package_dir = _prepared_workspace(
        tmp_path, spoken_media, with_analysis=False
    )
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)

    assert revision.sequence == 0
    assert revision.base_plan_id == plan.revision_id
    assert revision.project_id == facade.project_id == service.workspace_id(root)
    assert all(segment.speech_status == SPEECH_UNCONFIRMED_EMPTY for segment in revision.segments)

    loaded = facade.load_head()
    assert loaded.revision.revision_id == revision.revision_id
    assert loaded.source_stale is False

    with pytest.raises(facade_module.ReviewWorkspaceError, match="이미 있다"):
        facade.initialize_review(plan.revision_id)

    fresh = facade_module.ReviewWorkspaceFacade(root)
    assert fresh.load_head().revision.revision_id == revision.revision_id


@needs_ffmpeg
def test_initialize_refuses_changed_original(tmp_path: Path, spoken_media: Path) -> None:
    """원본이 계획 뒤 바뀌면 초기화가 거부된다 — 낡은 증거 위에 검토를 세우지 않는다."""
    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    asset = next(iter(service.list_source_assets(root)))
    (root / asset.relative_path).write_bytes(b"tampered")
    facade = facade_module.ReviewWorkspaceFacade(root)
    with pytest.raises(facade_module.ReviewWorkspaceError):
        facade.initialize_review(plan.revision_id)


def test_profile_match_guard() -> None:
    """계획 기록과 지금 측정이 어긋나면 stale_profile — 시계값으로 직접 증명한다."""

    def clock(frame_count: int) -> preprocess.RawVideoClock:
        return preprocess.RawVideoClock(
            stream_index=0,
            time_base=Fraction(1, 90000),
            first_pts=0,
            last_pts=(frame_count - 1) * 9000,
            presentation_origin=Fraction(0),
            frame_count=frame_count,
            frame_duration=Fraction(1, 10),
            exact_end=Fraction(frame_count, 10),
            uniform_grid=True,
            grid_note="",
        )

    class _Asset:
        source_relative_path = "발화.mov"

    class _Coverage:
        asset_id = "source_a"
        time_base_num = 1
        time_base_den = 90000
        presentation_origin_pts = 0
        exact_end_pts = 360000
        source_frame_count = 40

    facade_module.ReviewWorkspaceFacade._require_profile_match(_Coverage(), clock(40), _Asset())
    with pytest.raises(facade_module.ReviewWorkspaceError, match="stale_profile"):
        facade_module.ReviewWorkspaceFacade._require_profile_match(_Coverage(), clock(39), _Asset())


# ── 발화 상태 매핑 — 실패·전사 없음이 각각 다른 값이다 ────────────────────────


def test_row_speech_status_mapping(workspace: Path) -> None:
    """분석 결과 행 → 발화 상태 — 집계를 다시 지어내지 않고 기록을 그대로 옮긴다."""
    facade = facade_module.ReviewWorkspaceFacade(workspace)

    def row(status: str, speech_status: str | None) -> AnalysisSourceResult:
        return AnalysisSourceResult(
            revision_id="anal_x",
            asset_id="source_a",
            source_order=0,
            source_name="a.mov",
            fingerprint="f",
            status=status,
            speech_status=speech_status,
            execution_mode=None,
            document_path=None,
            record_path=None,
            window_count=0,
            segment_count=0,
            cached_from=None,
            error=None,
            created_at=NOW,
            updated_at=NOW,
        )

    assert facade._row_speech_status(None) == SPEECH_UNCONFIRMED_EMPTY
    assert facade._row_speech_status(row("failed", None)) == SPEECH_ANALYSIS_FAILED
    assert facade._row_speech_status(row("analyzed", SPEECH_AUDIO_ABSENT)) == SPEECH_AUDIO_ABSENT
    assert facade._row_speech_status(row("analyzed", SPEECH_DETECTED)) == SPEECH_DETECTED
    assert facade._row_speech_status(row("pending", None)) == SPEECH_UNCONFIRMED_EMPTY


# ── 편집 — stale-head·메타데이터 0재추출·순수 조회 ────────────────────────────


@needs_ffmpeg
def test_metadata_edits_are_pure_and_zero_reextract(tmp_path: Path, spoken_media: Path) -> None:
    """선택·등급 덮기·메모는 재추출 집합이 비고 작업·package도 하나도 늘지 않는다."""
    service, root, plan, package_dir = _prepared_workspace(
        tmp_path, spoken_media, with_analysis=False
    )
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    jobs_before = len(service.list_jobs(root, limit=1000))
    packages_before = _tree_hashes(root, "clips")

    excluded = facade.chronological(revision)[1].segment_id
    after = facade.set_selection(plan.revision_id, excluded, SELECTION_EXCLUDED)
    after = facade.set_override(plan.revision_id, excluded, "review_required")
    after = facade.set_notes(plan.revision_id, excluded, "흔들림 구간")

    assert facade.reextract_ids(revision, after) == frozenset()
    assert facade.stale_media(package_dir) == ()
    assert len(service.list_jobs(root, limit=1000)) == jobs_before
    assert _tree_hashes(root, "clips") == packages_before

    filtered = facade.filter(after, selection=SELECTION_EXCLUDED)
    assert [segment.segment_id for segment in filtered] == [excluded]
    assert (
        facade.view_state(display_mode="video_with_transcript").display_mode
        == "video_with_transcript"
    )

    stale = boundary_edit(revision.revision_id, "없는조각", "없는조각2", 1)
    with pytest.raises(StaleRevisionError, match="겨냥했는데"):
        facade.apply_edit(plan.revision_id, stale)


@needs_ffmpeg
def test_split_boundary_merge_and_stale_head_guard(tmp_path: Path, spoken_media: Path) -> None:
    """경계를 움직이는 편집만 다시 뽑을 조각을 만든다 — 늦은 편집은 헤드에서 거부다."""
    service, root, plan, package_dir = _prepared_workspace(
        tmp_path, spoken_media, with_analysis=False
    )
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    by_source: dict[str, list] = {}
    for segment in facade.chronological(revision):
        by_source.setdefault(segment.source_id, []).append(segment)
    first, second = next(spans for spans in by_source.values() if len(spans) >= 2)
    assert first.source_id == second.source_id
    clock = revision.clock_for(first.source_id)
    assert clock is not None

    split = facade.split_segment(plan.revision_id, first.segment_id, _mid_split_pts(clock, first))
    assert len(facade.reextract_ids(revision, split)) == 2  # 분할이면 두 자식
    assert {item.segment_id for item in facade.stale_media(package_dir)} == set(
        facade.reextract_ids(revision, split)
    )

    ordered = [
        segment for segment in facade.chronological(split) if segment.source_id == first.source_id
    ]
    left, right = ordered[0], ordered[1]
    assert left.source_id == right.source_id
    moved = facade.adjust_boundary(
        plan.revision_id,
        left.segment_id,
        right.segment_id,
        left.end_pts + clock.frame_ticks,
    )
    assert facade.reextract_ids(split, moved) == {left.segment_id, right.segment_id}

    merged = facade.merge_segments(plan.revision_id, left.segment_id, right.segment_id)
    assert facade.reextract_ids(moved, merged) == {left.segment_id}

    stale_edit = boundary_edit(split.revision_id, left.segment_id, right.segment_id, left.end_pts)
    with pytest.raises(StaleRevisionError, match="겨냥했는데"):
        facade.apply_edit(plan.revision_id, stale_edit)


# ── 재계획·입양 — 경계 왕복 항등·신원 보존 ────────────────────────────────────


@needs_ffmpeg
def test_replan_roundtrip_preserves_identity_and_metadata(
    tmp_path: Path, spoken_media: Path
) -> None:
    """분할 → 새 계획: 그대로 둔 조각은 같은 ID, 움직인 조각은 새 ID와 검증 매핑."""
    service, root, plan, package_dir = _prepared_workspace(
        tmp_path, spoken_media, with_analysis=False
    )
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    first = facade.chronological(revision)[0]
    clock = revision.clock_for(first.source_id)
    assert clock is not None
    split = facade.split_segment(plan.revision_id, first.segment_id, _mid_split_pts(clock, first))
    child = next(segment for segment in split.segments if segment.start_pts > first.start_pts)
    edited = facade.set_selection(plan.revision_id, child.segment_id, SELECTION_EXCLUDED)
    edited = facade.set_notes(plan.revision_id, child.segment_id, "여기는 건너뛴다")

    new_snapshot, mapping = facade.plan_from_review(plan.revision_id)

    unchanged = [pair for pair in mapping.pairs if pair.old_segment_id == pair.new_segment_id]
    moved = [pair for pair in mapping.pairs if pair.old_segment_id != pair.new_segment_id]
    assert len(mapping.pairs) == len(split.segments)  # 덮개가 그대로 계획이 되었다
    assert len(unchanged) == len(split.segments) - 2  # 분할된 두 자식만 새 ID
    assert len(moved) == 2
    assert {pair.new_segment_id for pair in mapping.pairs} == {
        entry.segment_id for entry in new_snapshot.entries
    }

    adopted = facade.adopt_plan_review(new_snapshot, mapping, old_revision=edited)
    assert adopted.base_plan_id == new_snapshot.revision_id
    new_child = next(
        pair.new_segment_id for pair in mapping.pairs if pair.old_segment_id == child.segment_id
    )
    adopted_child = next(segment for segment in adopted.segments if segment.segment_id == new_child)
    assert adopted_child.selection == SELECTION_EXCLUDED
    assert adopted_child.notes == "여기는 건너뛴다"

    assert facade.load_head().revision.revision_id == adopted.revision_id
    stale = {item.segment_id for item in facade.stale_media(package_dir)}
    assert {adopted_child.segment_id} <= stale  # 움직인 경계는 낡은 매체다


# ── 측정 VTT — 저장된 발화·잰 매체 길이만 ─────────────────────────────────────


@needs_ffmpeg
def test_measured_vtt_uses_saved_utterances_and_measured_tail(
    tmp_path: Path, spoken_media: Path
) -> None:
    """재생 분석의 발화 → 클립 국소 VTT: 좌표 덧셈이 정확히 한 번씩만인지 잰다."""
    service, root, plan, package_dir = _prepared_workspace(
        tmp_path, spoken_media, with_analysis=True
    )
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    summary = service.analysis_overview(root)
    assert summary is not None
    speech_asset = next(
        row.asset_id for row in summary.results if row.speech_status == SPEECH_DETECTED
    )
    speech_segments = [
        segment for segment in facade.chronological(revision) if segment.source_id == speech_asset
    ]
    silent_asset = next(
        row.asset_id for row in summary.results if row.speech_status == SPEECH_AUDIO_ABSENT
    )
    silent_segment = next(
        segment for segment in facade.chronological(revision) if segment.source_id == silent_asset
    )

    output = facade.review_vtt(speech_segments[0].segment_id, package_dir)
    cues = output.vtt.cues
    # 첫 조각 [0, 2초) — 첫 발화만 들어오고 두째 발화(2.5~3.4초)는 창 밖이다.
    assert len(cues) == 1
    assert cues[0].lines and "도착해서 일정 설명" in cues[0].lines[0]
    assert parse_review_vtt(output.vtt.text)[0].text == "도착해서 일정 설명"

    # 독립 계산 — 문서가 잰 PCM 원점과 artifact가 잰 매체 시작으로 기대값을 만든다.
    row = next(item for item in summary.results if item.asset_id == speech_asset)
    document = json.loads((root / row.document_path).read_text(encoding="utf-8"))
    pcm_origin = Fraction(
        document["audio"]["pcm_sample0_file_time"]["num"],
        document["audio"]["pcm_sample0_file_time"]["den"],
    )
    artifact = next(
        clip
        for clip in service.list_package_clips(root, package_dir)
        if clip.segment_id == speech_segments[0].segment_id and clip.status == "published"
    )
    time_base = Fraction(artifact.time_base_num, artifact.time_base_den)
    media_start = Fraction(artifact.media_start_pts) * time_base
    expected_start = milliseconds_half_up(pcm_origin + Fraction(4, 5) - media_start)
    assert cues[0].start_ms == expected_start
    assert output.provenance.measured_media_duration == artifact.media_local_duration
    assert output.provenance.source_original_relative_path.startswith("originals/")
    assert output.provenance.extracted_preview_relative_path.startswith("clips/")
    assert (
        output.provenance.source_sha256 == _assets_by_id(service, root)[speech_asset].content_sha256
    )

    silent = facade.review_vtt(silent_segment.segment_id, package_dir)
    assert silent.vtt.cues == ()
    assert "transcript_status:audio_absent" in silent.vtt.notes


# ── 종단 — 분할 → 재계획 → 재추출 → VTT → 선택·전체 확정 → 정리·재시작 ────────


@needs_ffmpeg
def test_full_flow_split_reextract_vtt_export_purge_restart(
    tmp_path: Path, spoken_media: Path
) -> None:
    """검토 편집이 새 계획·추출로 이어지고 확정은 정리·재시작 뒤에도 바이트 그대로다."""
    service, root, plan, package_dir = _prepared_workspace(
        tmp_path, spoken_media, with_analysis=True
    )
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    summary = service.analysis_overview(root)
    assert summary is not None
    speech_asset = next(
        row.asset_id for row in summary.results if row.speech_status == SPEECH_DETECTED
    )

    first = next(
        segment for segment in facade.chronological(revision) if segment.source_id == speech_asset
    )
    clock = revision.clock_for(first.source_id)
    assert clock is not None
    split = facade.split_segment(plan.revision_id, first.segment_id, _mid_split_pts(clock, first))
    child = next(segment for segment in split.segments if segment.start_pts > first.start_pts)
    edited = facade.set_selection(plan.revision_id, child.segment_id, SELECTION_EXCLUDED)

    new_snapshot, mapping = facade.plan_from_review(plan.revision_id)
    adopted = facade.adopt_plan_review(new_snapshot, mapping, old_revision=edited)
    new_child = next(
        pair.new_segment_id for pair in mapping.pairs if pair.old_segment_id == child.segment_id
    )

    _, record = _extract(service, root, plan_revision_id=new_snapshot.revision_id)
    assert record.status == "succeeded", record.error
    new_package = service.package_overview(root, package_dir=None)
    assert new_package is not None
    new_package_dir = new_package.package.package_dir
    assert new_package_dir != package_dir
    assert facade.stale_media(new_package_dir) == ()

    old_package_hashes = _tree_hashes(root, "clips/package_000001")
    written = facade.write_review_vtts(new_package_dir)
    assert written and all(path.name.endswith(".review.vtt") for path in written)
    child_output = facade.review_vtt(new_child, new_package_dir)
    assert child_output.vtt.cues  # 잘린 발화가 자식 클립 안으로 들어온다
    assert BOUNDARY_REVIEW_NOTE in child_output.vtt.notes

    selected = frozenset(
        segment.segment_id
        for segment in facade.chronological(adopted)
        if segment.selection != SELECTION_EXCLUDED
    )
    selected_outcome = facade.export_confirmed(new_package_dir, selected_segment_ids=selected)
    assert selected_outcome.report.mode == "published"
    assert selected_outcome.report.clip_count == len(adopted.segments) - 1
    selected_media = root / selected_outcome.destination_dir / "media"
    assert len(list(selected_media.iterdir())) == selected_outcome.report.clip_count

    full_outcome = facade.export_confirmed(new_package_dir)
    assert full_outcome.report.clip_count == len(adopted.segments) - 1
    full_dir = root / full_outcome.destination_dir

    preview = facade.restore_previews(new_package_dir)
    assert preview and all((root / item.relative_path).is_file() for item in preview)
    confirmed_hashes_before = _tree_hashes(root, full_outcome.destination_dir)

    purge = facade.managed_cleanup()
    assert purge.removed_files > 0  # 미리보기를 지웠다
    assert not (root / preview[0].relative_path).exists()
    assert (root / "review" / "vtt").is_dir()  # 검토 VTT는 보호 이름 안에 살아 있다
    assert list((root / "review" / "vtt").rglob("*.vtt"))

    # 확정·옛 package는 정리 뒤에도 바이트 그대로 — 재검증이 그걸 증명한다.
    assert _tree_hashes(root, full_outcome.destination_dir) == confirmed_hashes_before
    assert _tree_hashes(root, "clips/package_000001") == old_package_hashes
    assert (
        facade.verify_export(full_outcome.destination_dir).clip_count == len(adopted.segments) - 1
    )

    # 재시작 — 새 파사드 인스턴스가 같은 헤드·같은 확정 기록을 읽는다.
    restarted = facade_module.ReviewWorkspaceFacade(root)
    assert restarted.load_head().revision.revision_id == adopted.revision_id
    assert restarted.verify_export(selected_outcome.destination_dir).clip_count == len(selected)
    assert (full_dir / "media").is_dir()

    restored = restarted.restore_previews(new_package_dir)
    assert _sha256(root / restored[0].relative_path) == restored[0].content_sha256
    assert _tree_hashes(root, full_outcome.destination_dir) == confirmed_hashes_before


# ── 출판 원자성 — 크래시·동시 편집·CAS·프로세스 즉사 ─────────────────────────


def _faulted_connect(store: ReviewWorkspaceStore, marker: str, failure: BaseException):
    """지정 SQL에서 크래시를 일으키는 저장 연결 — 루트 탐침과 같은 원리의 주입."""
    original = store._connect

    class _FaultedConnection:
        def __init__(self, connection: sqlite3.Connection) -> None:
            self._connection = connection

        def __getattr__(self, name: str) -> object:
            return getattr(self._connection, name)

        def __enter__(self) -> _FaultedConnection:
            self._connection.__enter__()
            return self

        def __exit__(self, *args: object) -> object:
            return self._connection.__exit__(*args)

        def execute(self, sql: str, *args: object) -> sqlite3.Cursor:
            if sql.strip().upper().startswith(marker):
                raise failure
            return self._connection.execute(sql, *args)

    return lambda: _FaultedConnection(original())  # type: ignore[return-value]


def _stored_sequences(root: Path, base_plan_id: str) -> tuple[int, ...]:
    """DB에 실제로 묻힌 리비전 sequence — 고아 없음의 직접 증거."""
    with sqlite3.connect(root / DB_NAME) as connection:
        rows = connection.execute(
            "SELECT sequence FROM review_workspace_revisions WHERE base_plan_id = ?"
            " ORDER BY sequence",
            (base_plan_id,),
        ).fetchall()
    return tuple(int(row[0]) for row in rows)


@needs_ffmpeg
def test_edit_publication_crash_rolls_back_and_retry_unwedges(
    tmp_path: Path, spoken_media: Path
) -> None:
    """헤드 CAS 출판이 크래시로 반쪽에 그쳐도 리비전은 함께 사라지고 재시도는 안 막힌다."""

    class Crash(Exception):
        pass

    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    segment = facade.chronological(revision)[0]
    clock = revision.clock_for(segment.source_id)
    assert clock is not None

    facade._store._connect = _faulted_connect(  # type: ignore[assignment]
        facade._store, "UPDATE REVIEW_WORKSPACE_HEADS", Crash("헤드 출판 크래시")
    )
    with pytest.raises(Crash):
        facade.split_segment(plan.revision_id, segment.segment_id, _mid_split_pts(clock, segment))

    fresh = facade_module.ReviewWorkspaceFacade(root)
    assert fresh.load_head(plan.revision_id).revision.revision_id == revision.revision_id
    assert _stored_sequences(root, plan.revision_id) == (0,)  # 고아 sequence 1은 없다
    other = _mid_split_pts(clock, segment) - clock.frame_ticks  # 격자 위의 다른 점
    assert segment.start_pts < other < segment.end_pts
    resumed = fresh.split_segment(plan.revision_id, segment.segment_id, other)
    assert resumed.sequence == revision.sequence + 1
    assert fresh.load_head(plan.revision_id).revision.revision_id == resumed.revision_id
    assert _stored_sequences(root, plan.revision_id) == (0, 1)


@needs_ffmpeg
def test_initial_publication_crash_rolls_back(tmp_path: Path, spoken_media: Path) -> None:
    """초기 리비전과 첫 헤드도 한 트랜잭션 — 헤드 삽입 실패면 리비전도 없던 일이 된다."""

    class Crash(Exception):
        pass

    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    facade = facade_module.ReviewWorkspaceFacade(root)

    facade._store._connect = _faulted_connect(  # type: ignore[assignment]
        facade._store, "INSERT INTO REVIEW_WORKSPACE_HEADS", Crash("첫 헤드 크래시")
    )
    with pytest.raises(Crash):
        facade.initialize_review(plan.revision_id)

    with sqlite3.connect(root / DB_NAME) as connection:
        revisions = connection.execute(
            "SELECT COUNT(*) FROM review_workspace_revisions"
        ).fetchone()[0]
        heads = connection.execute("SELECT COUNT(*) FROM review_workspace_heads").fetchone()[0]
    assert revisions == 0
    assert heads == 0

    fresh = facade_module.ReviewWorkspaceFacade(root)
    revision = fresh.initialize_review(plan.revision_id)  # 재시도는 막히지 않는다
    assert fresh.load_head().revision.revision_id == revision.revision_id


@needs_ffmpeg
def test_adoption_publication_crash_rolls_back(tmp_path: Path, spoken_media: Path) -> None:
    """입양 출판(리비전+첫 헤드+매핑)도 한 트랜잭션 — 매핑 실패면 전부 없던 일이 된다."""

    class Crash(Exception):
        pass

    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    first = facade.chronological(revision)[0]
    clock = revision.clock_for(first.source_id)
    assert clock is not None
    facade.split_segment(plan.revision_id, first.segment_id, _mid_split_pts(clock, first))
    new_snapshot, mapping = facade.plan_from_review(plan.revision_id)
    edited = facade.load_head(plan.revision_id).revision

    facade._store._connect = _faulted_connect(  # type: ignore[assignment]
        facade._store, "INSERT INTO REVIEW_WORKSPACE_REMAPPINGS", Crash("매핑 크래시")
    )
    with pytest.raises(Crash):
        facade.adopt_plan_review(new_snapshot, mapping, old_revision=edited)

    fresh = facade_module.ReviewWorkspaceFacade(root)
    assert _stored_sequences(root, new_snapshot.revision_id) == ()
    assert fresh._store.head_revision_id(facade.project_id, new_snapshot.revision_id) is None

    adopted = fresh.adopt_plan_review(new_snapshot, mapping)  # 재시도는 막히지 않는다
    assert fresh.load_head().revision.revision_id == adopted.revision_id
    assert fresh._store.remapping(edited.revision_id) is not None


@needs_ffmpeg
def test_head_cas_rejects_late_writer_without_orphan(tmp_path: Path, spoken_media: Path) -> None:
    """CAS와 Uniqueness가 늦은 완성을 이긴다 — 진 출판은 리비전 행을 남기지 않는다."""
    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    first = facade.chronological(revision)[0]
    clock = revision.clock_for(first.source_id)
    assert clock is not None
    winner = facade.split_segment(plan.revision_id, first.segment_id, _mid_split_pts(clock, first))
    child = next(segment for segment in winner.segments if segment.start_pts > first.start_pts)

    # CAS — 늦은 쪽 문서의 자리(sequence 2)는 비었지만 겨냥 헤드가 옛 것이다.
    late = apply_edit(
        winner, split_edit(winner.revision_id, child.segment_id, _mid_split_pts(clock, child))
    )
    with pytest.raises(StaleRevisionError, match="stale_head"):
        facade._store.publish_edit_revision(
            project_id=facade.project_id,
            base_plan_id=plan.revision_id,
            document_json=serialize_revision(late),
            content_fingerprint=content_fingerprint(late),
            expected_head_revision_id=revision.revision_id,
            new_head_revision_id=late.revision_id,
            updated_at=NOW,
        )

    # Uniqueness — 같 자리(sequence 1)를 노린 출판도 진다(동시 편집).
    duplicate = apply_edit(
        revision,
        split_edit(
            revision.revision_id,
            first.segment_id,
            _mid_split_pts(clock, first) - clock.frame_ticks,
        ),
    )
    with pytest.raises(StaleRevisionError, match="동시 편집"):
        facade._store.publish_edit_revision(
            project_id=facade.project_id,
            base_plan_id=plan.revision_id,
            document_json=serialize_revision(duplicate),
            content_fingerprint=content_fingerprint(duplicate),
            expected_head_revision_id=revision.revision_id,
            new_head_revision_id=duplicate.revision_id,
            updated_at=NOW,
        )

    assert _stored_sequences(root, plan.revision_id) == (0, 1)  # 이긴 쪽만 남는다
    note = facade.set_notes(plan.revision_id, child.segment_id, "경계 다시 봄")
    assert note.sequence == 2  # 다음 편집은 막히지 않는다


@needs_ffmpeg
def test_concurrent_edits_never_leave_orphans(tmp_path: Path, spoken_media: Path) -> None:
    """동시 편집 — 성공한 수만 리비전 행이 되고, 지는 쪽은 흔적을 남기지 않는다."""
    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)
    target = facade.chronological(revision)[1].segment_id

    def writer(note: str) -> tuple[str, object]:
        try:
            return "ok", facade.set_notes(plan.revision_id, target, note)
        except StaleRevisionError:
            return "stale", None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, ("첫 메모", "둘째 메모")))

    wins = sum(1 for tag, _ in outcomes if tag == "ok")
    assert wins >= 1
    assert all(tag in ("ok", "stale") for tag, _ in outcomes)
    assert _stored_sequences(root, plan.revision_id) == tuple(range(wins + 1))
    assert facade.load_head(plan.revision_id).revision.sequence == wins

    follow = facade.set_notes(plan.revision_id, target, "다음 메모")  # 막힘이 없다
    assert follow.sequence == wins + 1


@needs_ffmpeg
def test_process_death_mid_transaction_publishes_nothing(
    tmp_path: Path, spoken_media: Path
) -> None:
    """출판 트랜잭션 중간 프로세스 즉사 — 되돌아온 DB에는 반쪽 출판이 없다."""
    service, root, plan, _ = _prepared_workspace(tmp_path, spoken_media, with_analysis=False)
    facade = facade_module.ReviewWorkspaceFacade(root)
    revision = facade.initialize_review(plan.revision_id)

    script = (
        "import os, sqlite3, sys\n"
        "db, project_id, base_plan_id = sys.argv[1], sys.argv[2], sys.argv[3]\n"
        "connection = sqlite3.connect(db)\n"
        "connection.execute('BEGIN IMMEDIATE')\n"
        'connection.execute("INSERT INTO review_workspace_revisions (project_id,'
        " base_plan_id, sequence, document_json, content_fingerprint, created_at)"
        ' VALUES (?, ?, 1, ?, ?, ?)", (project_id, base_plan_id, "{}", "dead", "now"))\n'
        "os._exit(9)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root / DB_NAME), facade.project_id, plan.revision_id],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 9

    fresh = facade_module.ReviewWorkspaceFacade(root)
    assert _stored_sequences(root, plan.revision_id) == (0,)  # 커밋 안 된 행은 살아남지 않는다
    assert fresh.load_head(plan.revision_id).revision.revision_id == revision.revision_id
    segment = fresh.chronological(revision)[0]
    clock = revision.clock_for(segment.source_id)
    assert clock is not None
    resumed = fresh.split_segment(
        plan.revision_id, segment.segment_id, _mid_split_pts(clock, segment)
    )
    assert resumed.sequence == 1
