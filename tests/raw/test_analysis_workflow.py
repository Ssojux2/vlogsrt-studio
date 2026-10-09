"""A4 의미 분석 수직 통합 — 실제 ffmpeg 미디어로 재생 분석 전 과정을 증명한다.

합성 미디어만 만든다(실제 미디어·API 픽스처 불변). 이 묶음이 증명하는 것:

- A/B 카드(오디오프셋 스테레오 25fps + NTSC 무성)의 분석 창이 **공통 파일 시간축**
  전체(비디오 머리·꼬리 포함)를 덮고, 무성 원본은 이미지 증거만으로 장면을
  설명한다 — ASR 작업을 만들지 않고 빈 PCM을 지어내지도 않는다(§7.3·§7.4).
- 재생의 진실: 실행 증거는 `offline_replay`이고 유료 호출은 0회다(§14.3).
- 받아들인 경계가 하나의 비디오 격자로 조정되어 A3 분할 계획·추출로 이어지고,
  옛 계획·package는 그대로다(§6.5·§7.6).
- 창 실패는 전체 보존과 검토 표시로 남는다 — SRT는 어디에도 없다(§7.1·§7.6).
- 캐시는 성공만, 오류는 재시도 대상으로 남는다(§14.2).
- 실제 호출 문: 유효 승인은 보내고, 잘못된 승인·모델·종료일·환경은 0회 송신이다(§14.3).
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import time
import types
from dataclasses import replace
from datetime import date
from fractions import Fraction
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import analysis_workflow
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.workspace import DB_NAME, DB_SCHEMA_VERSION, RawWorkspace

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAIT_SECONDS = 300.0


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 원본 생성 실패: {completed.stderr[-400:]}"


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


def _card(tmp_path: Path, name: str, files: dict[str, Path]) -> Path:
    card = tmp_path / name
    for relative, source in files.items():
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return card


def _analyze(service: raw.RawWorkspaceService, root: Path, **kwargs):
    handle = service.start_analysis(root, **kwargs)
    return handle, _wait_terminal(service, root, handle.job_id)


def _luna_body(window_id: str, *, suggestions: tuple[str, ...] = (), review: bool = False):
    return {
        "window_id": window_id,
        "speech_summary": "도착해서 일정을 설명한다",
        "visual_summary": "실내에서 걷다가 문으로 향한다",
        "scene_tags": ["walking", "arrival"],
        "evidence_ids": [window_id],
        "suggested_boundary_ids": list(suggestions),
        "editing_notes": [],
        "needs_review": review,
        "uncertainties": [],
    }


def _write_fixtures(
    directory: Path,
    *,
    asr_sources: dict[str, list[dict[str, object]]] | None = None,
    luna_windows: dict[str, dict[str, object]] | None = None,
) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "asr-fixtures.json").write_text(
        json.dumps({"sources": asr_sources or {}}, ensure_ascii=False), encoding="utf-8"
    )
    (directory / "luna-fixtures.json").write_text(
        json.dumps({"windows": luna_windows or {}}, ensure_ascii=False), encoding="utf-8"
    )


def _document(service: raw.RawWorkspaceService, root: Path, revision_id: str, asset_id: str):
    document = service.analysis_document(root, revision_id, asset_id)
    assert document is not None
    return document


# ── 합성 미디어 — 세션마다 한 번 ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("a4_media")


def _video_input(duration: str, rate: str) -> list[str]:
    return ["-f", "lavfi", "-i", f"testsrc2=size=320x180:rate={rate}:duration={duration}"]


def _x264_output() -> list[str]:
    return [
        "-force_key_frames",
        "expr:gte(t,n_forced*3)",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
    ]


@pytest.fixture(scope="session")
def stereo_offset(media: Path) -> Path:
    """6초 25fps 스테레오 — 소리가 파일 시각 0.4초부터 5.6초동안(머리가 조용하다)."""
    path = media / "stereo_offset.mov"
    _encode(
        [
            *_video_input("6", "25"),
            "-itsoffset",
            "0.4",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=5.6",
            *_x264_output(),
            "-af",
            "aformat=channel_layouts=stereo",
            "-ac",
            "2",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def ntsc_noaudio(media: Path) -> Path:
    """30000/1001 무성 — 오디오 트랙 자체가 없다(무발화와 다른 상태)."""
    path = media / "ntsc_noaudio.mov"
    _encode([*_video_input("3.5", "30000/1001"), *_x264_output(), str(path)])
    return path


@pytest.fixture(scope="session")
def audio_tail(media: Path) -> Path:
    """영상 6초 + 오디오 7.5초 — 비디오 끝 뒤 오디오 꼬리(§8.2)."""
    path = media / "audio_tail.mp4"
    _encode(
        [
            *_video_input("6", "25"),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=7.5",
            *_x264_output(),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


# ── 두 카드 — 공통 시간축 전체 덮음·무성 원본의 이미지 증거 ────────────────────


@needs_ffmpeg
def test_two_cards_common_timeline_replay_truth(
    workspace: Path, tmp_path: Path, stereo_offset: Path, ntsc_noaudio: Path
) -> None:
    """스테레오(오디오프셋)+NTSC 무성 — 창은 파일 전체를 덮고 재생은 재생이라고 기록한다."""
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardA", {"DCIM/스테레오.mov": stereo_offset}))
    _ingest_card(service, workspace, _card(tmp_path, "cardB", {"DCIM/무성.mov": ntsc_noaudio}))
    service.confirm_global_source_order(workspace)
    assets = {a.asset_id: a for a in service.list_source_assets(workspace)}
    stereo_id = next(i for i, a in assets.items() if a.source_name == "스테레오.mov")
    silent_id = next(i for i, a in assets.items() if a.source_name == "무성.mov")

    estimates = service.analysis_estimate(workspace)
    by_name = {e.source_name: e for e in estimates}
    assert by_name["스테레오.mov"].has_audio and by_name["스테레오.mov"].asr_chunk_count == 1
    assert not by_name["무성.mov"].has_audio
    assert by_name["스테레오.mov"].price_status == "unknown"  # 미확인 단가는 0원 계산 금지

    fixtures = workspace / "analysis" / "replay"
    _write_fixtures(
        fixtures,
        asr_sources={
            assets[stereo_id].content_sha256 or "": [
                {"start": 0.8, "end": 2.0, "text": "도착해서 일정 설명"},
                {"start": 3.0, "end": 4.2, "text": "다음 장소 안내"},
            ]
        },
        luna_windows={"window_000000": _luna_body("window_000000")},
    )
    handle, record = _analyze(service, workspace)
    assert record.status == "succeeded", record.error
    summary = service.analysis_overview(workspace)
    assert summary is not None and summary.revision.status == "complete"
    assert summary.revision.mode == "replay"
    assert summary.revision.source_done == 2

    rows = {r.asset_id: r for r in summary.results}
    stereo_row, silent_row = rows[stereo_id], rows[silent_id]
    assert stereo_row.status == "analyzed" and stereo_row.speech_status == "speech_detected"
    assert silent_row.status == "analyzed" and silent_row.speech_status == "audio_absent"

    # 공통 시간축 전체 덮음 — 비디오 머리(0~0.4초 조용)도 창 밖이 아니다.
    stereo_doc = _document(service, workspace, summary.revision.revision_id, stereo_id)
    windows = stereo_doc["windows"]["windows"]
    cursor = Fraction(0)
    for window in windows:
        start = Fraction(window["core_start"]["num"], window["core_start"]["den"])
        end = Fraction(window["core_end"]["num"], window["core_end"]["den"])
        assert start == cursor
        cursor = end
    # 커서는 잰 오디오 끝(≈6.01초, AAC 프라이밍 재샘플까지)에서 닫는다 —
    # 비디오 전체(6초)를 넘어서까지 덮는다(§7.3 잰 분량 그대로).
    assert cursor >= Fraction(6)
    assert stereo_doc["audio"]["channels"] == 2  # 스테레오 보존
    assert len(stereo_doc["asr"]["chunks"]) == 1  # 선택 오디오 전체를 한 청크로
    assert all(c["failure"] == "" for c in stereo_doc["asr"]["chunks"])
    assert stereo_doc["execution_mode"] == "offline_replay"  # 재생은 재생이다(§14.3)

    # 무성 원본 — 이미지 증거만으로 장면 설명, ASR 작업·빈 PCM이 없다.
    silent_doc = _document(service, workspace, summary.revision.revision_id, silent_id)
    assert silent_doc["audio"]["status"] == "audio_absent"
    assert silent_doc["asr"]["chunks"] == []
    assert silent_doc["models"]["asr"] is None  # 시간 제공자 역할 자체가 없다
    assert silent_doc["windows"]["windows"]  # 창은 비디오 전체를 덮는다
    accepted = [a for a in silent_doc["annotations"] if a["status"] == "accepted"]
    assert accepted and accepted[0]["visual_summary"]  # 이미지 기반 장면 설명
    assert silent_doc["execution_mode"] == "offline_replay"
    # 프레임 표본은 실제 파일로 뽑혔다 — 검증 가능한 증거다.
    assert silent_row.document_path
    frames_dir = (workspace / silent_row.document_path).parent / "frames"
    assert list(frames_dir.glob("frame_*.jpg"))

    # SRT는 이 단계의 산출물이 아니다(§7.1) — analysis/ 아래 어디에도 없다.
    assert list((workspace / "analysis").rglob("*.srt")) == []


# ── 받아들인 경계 → A3 연결 — 옛 계획·package 불변 ─────────────────────────────


@needs_ffmpeg
def test_accepted_boundary_reaches_a3_extraction_old_plans_unchanged(
    workspace: Path, tmp_path: Path, stereo_offset: Path
) -> None:
    """발화 종료 경계가 비디오 격자 조각이 되어 추출까지 흐른다 — 옛 것은 그대로."""
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardC", {"제주.mov": stereo_offset}))
    service.confirm_global_source_order(workspace)
    asset = service.list_source_assets(workspace)[0]

    # 옛 결정적 계획·package — 나중에 바뀌지 않는다(§6.5).
    service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    old_handle = service.start_segment_extraction(workspace)
    old_record = _wait_terminal(service, workspace, old_handle.job_id)
    assert old_record.status == "succeeded", old_record.error
    old_summary = service.package_overview(workspace)
    assert old_summary is not None
    old_manifest = _sha256(workspace / old_summary.manifest_relative_paths[0])

    # 재생 픽스처: 발화 [0.8, 2.0]·[3.0, 4.2](오디오 시계) — 잰 원점으로 경계 아이디를
    # 계산한다(프레임 번호는 원점 실측값에서 유도된다, §13.3).
    original = workspace / asset.relative_path
    probe = preprocess.probe_raw_media(original)
    clock = preprocess.probe_extraction_clock(original, probe=probe)
    scratch = tmp_path / "a4-scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    probed_audio = analysis_workflow.stage1.select_audio(
        original, scratch, source_id="probe", audio_ordinal=0
    )
    audio_start = probed_audio.pcm_sample0_file_time - clock.presentation_origin
    cut_frame = -(-(audio_start + Fraction(21, 5)) // Fraction(1, 25))  # 4.2초 끝의 올림 프레임
    boundary_id = f"boundary_u{67200:09d}_{cut_frame:08d}"
    _write_fixtures(
        workspace / "analysis" / "replay",
        asr_sources={
            asset.content_sha256 or "": [
                {"start": 0.8, "end": 2.0, "text": "도착해서 일정 설명"},
                {"start": 3.0, "end": 4.2, "text": "다음 장소 안내"},
            ]
        },
        luna_windows={
            key: _luna_body(key, suggestions=(boundary_id,) if key == "window_000002" else ())
            for key in ("window_000000", "window_000001", "window_000002", "window_000003")
        },
    )
    settings = RawUnderstandingSettings(window_seconds=2.0, context_seconds=0.5)
    handle, record = _analyze(service, workspace, settings=settings)
    assert record.status == "succeeded", record.error

    snapshot = service.plan_segmentation_from_analysis(workspace)
    assert snapshot.basis == "semantic_analysis"
    by_source: dict[int, list] = {}
    for entry in snapshot.entries:
        by_source.setdefault(entry.source_order, []).append(entry)
    entries = by_source[1]
    assert len(entries) == 2  # 발화 종료에서 한 번 자른다 — 나머지 제안은 받아들여지지 않았다
    assert entries[0].core_after_last_frame == cut_frame
    assert entries[1].core_first_frame == cut_frame
    for left, right in zip(entries[:-1], entries[1:], strict=True):
        assert left.core_end_pts == right.core_start_pts  # 이웃 정확히 맞닿음(§6.1)

    new_handle = service.start_segment_extraction(workspace, plan_revision_id=snapshot.revision_id)
    new_record = _wait_terminal(service, workspace, new_handle.job_id)
    assert new_record.status == "succeeded", new_record.error
    new_summary = service.package_overview(workspace)
    assert new_summary is not None and new_summary.package.package_dir == "clips/package_000002"
    # 옛 package의 전달물·바이트는 그대로다.
    assert _sha256(workspace / old_summary.manifest_relative_paths[0]) == old_manifest
    assert [c.status for c in old_summary.clips] == ["published"] * len(old_summary.clips)


@needs_ffmpeg
def test_audio_tail_beyond_last_frame_belongs_to_last_clip(
    workspace: Path, tmp_path: Path, audio_tail: Path
) -> None:
    """오디오 꼬리(7.5초)까지 창이 덮지만, 조각 경계는 비디오 끝(6초)에서 닫는다."""
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardD", {"꼬리.mp4": audio_tail}))
    service.confirm_global_source_order(workspace)
    asset = service.list_source_assets(workspace)[0]
    _write_fixtures(
        workspace / "analysis" / "replay",
        asr_sources={
            asset.content_sha256 or "": [{"start": 0.5, "end": 2.0, "text": "완전한 문장입니다."}]
        },
        luna_windows={
            "window_000000": _luna_body("window_000000"),
            "window_000001": _luna_body(
                "window_000001", suggestions=("boundary_u000032000_00000050",)
            ),
            "window_000002": _luna_body("window_000002"),
            "window_000003": _luna_body("window_000003"),
        },
    )
    settings = RawUnderstandingSettings(window_seconds=2.0, context_seconds=0.5)
    handle, record = _analyze(service, workspace, settings=settings)
    assert record.status == "succeeded", record.error
    summary = service.analysis_overview(workspace)
    assert summary is not None
    document = _document(service, workspace, summary.revision.revision_id, asset.asset_id)
    extent = Fraction(document["windows"]["extent"]["num"], document["windows"]["extent"]["den"])
    assert extent == Fraction("7.5")  # 오디오 꼬리까지 증거가 덮는다(§7.3)
    # 조각은 비디오 격자에서 닫힌다 — 꼬리를 자르는 경계는 없다(§8.2).
    record_path = summary.results[0].record_path
    assert record_path is not None
    body = json.loads((workspace / record_path).read_text(encoding="utf-8"))
    last = body["intervals"][-1]
    assert Fraction(last["end"]["num"], last["end"]["den"]) == Fraction(6)

    snapshot = service.plan_segmentation_from_analysis(workspace)
    extract = service.start_segment_extraction(workspace, plan_revision_id=snapshot.revision_id)
    extract_record = _wait_terminal(service, workspace, extract.job_id)
    assert extract_record.status == "succeeded", extract_record.error
    package = service.package_overview(workspace)
    assert package is not None
    clips = sorted(package.clips, key=lambda c: c.segment_order)
    assert len(clips) == 2  # 검증된 발화 종료(2초) 하나 — 임의 길이·꼬리 경계는 없다
    internal = Fraction(clips[0].media_local_duration)
    last_clip = Fraction(clips[-1].media_local_duration)
    assert internal < Fraction("2.2")  # 내부 클립은 매체 끝에서 닫힌다
    assert last_clip > internal + Fraction(1)  # 꼬리(≈1.5초)는 마지막 조각에만


# ── 창 실패 — 전체 보존·검토 표시·SRT 없음 ─────────────────────────────────────


@needs_ffmpeg
def test_failed_window_keeps_whole_coverage_with_review(
    workspace: Path, tmp_path: Path, stereo_offset: Path
) -> None:
    """재생할 Luna 응답이 없는 창 — 실패로 남고 조각은 전체를 하나로 보존한다."""
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardE", {"제주.mov": stereo_offset}))
    service.confirm_global_source_order(workspace)
    asset = service.list_source_assets(workspace)[0]
    _write_fixtures(
        workspace / "analysis" / "replay",
        asr_sources={asset.content_sha256 or "": []},
        luna_windows={
            "window_000000": _luna_body("window_000000"),
            # window_000001 재생 응답 없음 — 실패 판정으로 기록된다.
            "window_000002": _luna_body("window_000002"),
            "window_000003": _luna_body("window_000003"),
        },
    )
    settings = RawUnderstandingSettings(window_seconds=2.0, context_seconds=0.5)
    handle, record = _analyze(service, workspace, settings=settings)
    assert record.status == "succeeded", record.error
    summary = service.analysis_overview(workspace)
    assert summary is not None
    document = _document(service, workspace, summary.revision.revision_id, asset.asset_id)
    failed = [a for a in document["annotations"] if a["status"] == "failed"]
    assert [a["window_id"] for a in failed] == ["window_000001"]
    segmentation = document["segmentation"]
    assert len(segmentation["segments"]) == 1  # 강제 절단이 아니라 보존이다(§7.6)
    first = segmentation["segments"][0]
    assert Fraction(first["end"]["num"], first["end"]["den"]) == Fraction(6)
    assert first["needs_review"]
    assert any("window_000001" in r for r in first["review_reasons"])
    assert list((workspace / "analysis").rglob("*.srt")) == []


# ── 캐시 — 성공만 재사용, 오류는 재시도 대상 ───────────────────────────────────


class _CountingFixtures:
    """호출을 세는 재생 묶음 감싸개 — 캐시 재사용이 0호출인지를 증명한다.

    `ReplayFixtures`는 frozen·slots라 상태를 못 늘린다 — 감싸개가 세기만 한다.
    """

    def __init__(self, base: analysis_workflow.ReplayFixtures) -> None:
        self._base = base
        self.asr_calls = 0
        self.luna_calls = 0

    def asr_for(self, source_sha256: str):
        provider = self._base.asr_for(source_sha256)
        outer = self

        class _Counting:
            def timed_transcript(inner, **kwargs):
                outer.asr_calls += 1
                return provider.timed_transcript(**kwargs)

        return _Counting()

    def luna_responder(self):
        base = self._base.luna_responder()
        outer = self

        class _CountingResponder:
            def respond(inner, **kwargs):
                outer.luna_calls += 1
                return base.respond(**kwargs)

        return _CountingResponder()


@needs_ffmpeg
def test_cache_reuses_success_only_and_errors_retry(
    workspace: Path, tmp_path: Path, stereo_offset: Path, ntsc_noaudio: Path, monkeypatch
) -> None:
    """같은 지문의 성공은 캐시로 0호출, 실패는 캐시되지 않고 다시 분석된다."""
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardF", {"스테레오.mov": stereo_offset}))
    _ingest_card(service, workspace, _card(tmp_path, "cardG", {"무성.mov": ntsc_noaudio}))
    service.confirm_global_source_order(workspace)
    assets = {a.source_name: a for a in service.list_source_assets(workspace)}
    stereo_sha = assets["스테레오.mov"].content_sha256 or ""
    _write_fixtures(
        workspace / "analysis" / "replay",
        asr_sources={stereo_sha: [{"start": 0.5, "end": 1.5, "text": "인사"}]},
        luna_windows={"window_000000": _luna_body("window_000000")},
    )
    settings = RawUnderstandingSettings(window_seconds=15.0)
    counting_holder: list[_CountingFixtures] = []
    real_loader = analysis_workflow.load_replay_fixtures

    def counting_loader(directory: Path) -> analysis_workflow.ReplayFixtures:
        base = real_loader(directory)
        counting = _CountingFixtures(base)
        counting_holder.append(counting)
        return counting

    monkeypatch.setattr(analysis_workflow, "load_replay_fixtures", counting_loader)

    handle1, record1 = _analyze(service, workspace, settings=settings)
    assert record1.status == "succeeded", record1.error
    first = service.analysis_overview(workspace)
    assert first is not None and [r.status for r in first.results] == ["analyzed", "analyzed"]
    assert counting_holder[-1].asr_calls == 1  # 스테레오 청크 1 — 무성은 ASR가 없다
    assert counting_holder[-1].luna_calls == 2  # 창 1 + 창 1

    # 같은 지문의 재실행 — 성공 캐시로 이어 붙는다(재생 호출 0회).
    handle2, record2 = _analyze(service, workspace, settings=settings)
    assert record2.status == "succeeded", record2.error
    second = service.analysis_overview(workspace)
    assert second is not None and [r.status for r in second.results] == ["cached", "cached"]
    assert second.results[0].cached_from is not None  # 어느 리비전에서 왔는지 말한다
    assert counting_holder[-1].asr_calls == 0 and counting_holder[-1].luna_calls == 0

    # 설정이 바뀌면 그 지문은 무효 — 다시 분석한다(§14.2).
    drifted = RawUnderstandingSettings(window_seconds=12.0)
    _analyze(service, workspace, settings=drifted)
    third = service.analysis_overview(workspace)
    assert third is not None and [r.status for r in third.results] == ["analyzed", "analyzed"]
    assert counting_holder[-1].luna_calls == 2  # 다시 판정을 얻었다

    # 원본 실패는 캐시되지 않는다 — 다음 실행이 다시 분석한다.
    real_select = analysis_workflow.stage1.select_audio

    def failing_select(source, workdir, **kwargs):
        if source.name.endswith("스테레오.mov"):
            raise RuntimeError("주입된 PCM 실패")
        return real_select(source, workdir, **kwargs)

    # 아직 성공 없는 세번째 지문에서 원본 실패를 넣는다 — 실패가 캐시되면 안 된다.
    virgin = RawUnderstandingSettings(window_seconds=10.0)
    monkeypatch.setattr(analysis_workflow.stage1, "select_audio", failing_select)
    _analyze(service, workspace, settings=virgin)
    fourth = service.analysis_overview(workspace)
    assert fourth is not None
    statuses = {r.source_name: r.status for r in fourth.results}
    assert statuses == {"스테레오.mov": "failed", "무성.mov": "analyzed"}
    # 이름으로 고른다 — D53 이후 촬영 시각 모르는 원본은 배치가 아니라 파일 이름 순이라
    # 두 번째 배치의 무성.mov가 앞에 선다(예전엔 results[0]이 첫 배치의 스테레오였다).
    failed = next(r for r in fourth.results if r.source_name == "스테레오.mov")
    assert "주입된 PCM 실패" in (failed.error or "")
    monkeypatch.setattr(analysis_workflow.stage1, "select_audio", real_select)
    _analyze(service, workspace, settings=virgin)
    fifth = service.analysis_overview(workspace)
    assert fifth is not None
    statuses = {r.source_name: r.status for r in fifth.results}
    assert statuses["스테레오.mov"] == "analyzed"  # 실패 기록은 캐시가 아니었다
    assert statuses["무성.mov"] == "cached"  # 같은 실행의 성공은 캐시다


@needs_ffmpeg
def test_interrupted_job_resumes_with_same_id_and_cached_sources(
    workspace: Path, tmp_path: Path, stereo_offset: Path, ntsc_noaudio: Path
) -> None:
    """프로세스 재시작 복구 — 같은 작업 ID로 재개하고 완료 원본은 캐시로 붙는다."""
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardH", {"스테레오.mov": stereo_offset}))
    _ingest_card(service, workspace, _card(tmp_path, "cardI", {"무성.mov": ntsc_noaudio}))
    service.confirm_global_source_order(workspace)
    stereo = next(
        a for a in service.list_source_assets(workspace) if a.source_name == "스테레오.mov"
    )
    _write_fixtures(
        workspace / "analysis" / "replay",
        asr_sources={stereo.content_sha256 or "": []},
        luna_windows={"window_000000": _luna_body("window_000000")},
    )
    handle, record = _analyze(service, workspace)
    assert record.status == "succeeded", record.error

    # 프로세스 사망 흉내 — 실행 중 표시로 되돌리고 새 서버가 복구한다(§14.1).
    from vlogsrt.project.raw.jobs import JobStore

    JobStore(RawWorkspace.open(workspace)).update(handle.job_id, status="running")
    restarted = raw.RawWorkspaceService()
    assert restarted.recover_interrupted_jobs(workspace) == 1
    assert restarted.get_job(workspace, handle.job_id).status == "interrupted"

    retry = restarted.retry_analysis(workspace, handle.job_id)
    assert retry.job_id == handle.job_id  # 같은 작업 ID
    resumed = _wait_terminal(restarted, workspace, retry.job_id)
    assert resumed.status == "succeeded", resumed.error
    summary = restarted.analysis_overview(workspace)
    assert summary is not None
    statuses = sorted(r.status for r in summary.results)
    assert statuses == ["cached", "cached"]  # 완료 원본의 캐시 재사용을 표시한다


# ── 실제 호출 문 — 두 단계 승인: 1단계(전사) → 대기 → 2단계(의미) ─────────────


class _FakeSender:
    """전사 전송 경계 가짜 — 보낸 요청을 세고 정해진 응답을 돌려준다."""

    def __init__(self, segments: list[dict[str, object]] | None = None) -> None:
        self.calls: list[object] = []
        self._segments = segments if segments is not None else []

    def send_request(self, request, cfg, *, kind, stem, source):
        self.calls.append(request)
        # 실제 전사처럼 발화를 돌려준다 — 2단계 지문은 이 발화가 만든 창 계획을
        # 묶어야 한다(빈 골격이 아니라).
        return {"segments": list(self._segments)}


class _FakeClient:
    """Responses 클라이언트 가짜 — Luna 호출을 세고 창 판정을 돌려준다."""

    def __init__(self, body: dict[str, object]) -> None:
        self._body = body
        self.calls: list[str] = []
        self.responses = types.SimpleNamespace(create=self._create)

    def _create(self, **payload):
        self.calls.append(str(payload.get("model")))
        return {"output_text": json.dumps(self._body, ensure_ascii=False)}


def _live_online(service: raw.RawWorkspaceService, root: Path, monkeypatch) -> None:
    """실제 호출 시험 준비 — 환경·작업공간 네트워크를 함께 연다(§14.3)."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    config = service.settings(root)
    service.update_settings(root, replace(config, network_mode="online"))


def _asr_approval(asset, workflow, settings, audio_stream_index: int, **overrides):
    """1단계(전사) 승인 — 견적이 알 수 있는 값만 묶는다(A4-F1)."""
    from vlogsrt.project.raw.analysis_approval import AsrPhaseApproval, asr_settings_fingerprint

    values = {
        "approved": True,
        "source_sha256": asset.content_sha256 or "",
        "audio_stream_index": audio_stream_index,
        "chunk_budget": 1,
        "settings_fingerprint": asr_settings_fingerprint(settings, workflow),
        "timing_model": workflow.models.timing_model,
        "approved_on": date.today().isoformat(),
    }
    values.update(overrides)
    return AsrPhaseApproval(**values)


def _artifact(service: raw.RawWorkspaceService, root: Path, revision_id: str, asset_id: str):
    summary = service.analysis_overview(root, revision_id)
    assert summary is not None
    row = next(item for item in summary.results if item.asset_id == asset_id)
    assert row.document_path is not None
    return json.loads((root / row.document_path).read_text(encoding="utf-8"))


@needs_ffmpeg
def test_live_two_phase_asr_stops_for_semantic_then_resumes(
    workspace: Path, tmp_path: Path, stereo_offset: Path, monkeypatch
) -> None:
    """1단계는 전사만(의미 0호출)·2단계는 전사 0재호출 — 대기 상태를 사이에 둔다."""
    from vlogsrt.project.raw.understanding_luna import AnalysisBudget, OnlineAnalysisApproval

    _live_online(raw.RawWorkspaceService(), workspace, monkeypatch)
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardJ", {"스테레오.mov": stereo_offset}))
    service.confirm_global_source_order(workspace)
    asset = service.list_source_assets(workspace)[0]
    workflow = service.settings(workspace)
    probe = preprocess.probe_raw_media(workspace / asset.relative_path)
    assert probe.audio
    settings = RawUnderstandingSettings()

    # ── 1단계(전사): 실제 발화를 받는다 — 2단계 지문은 전사 **뒤**에야 안다.
    sender = _FakeSender([{"start": 0.5, "end": 2.0, "text": "도착해서 일정 설명"}])
    client = _FakeClient(_luna_body("window_000000"))
    handle, record = _analyze(
        service,
        workspace,
        mode="live",
        asr_approvals={
            asset.asset_id: _asr_approval(asset, workflow, settings, probe.audio[0].stream_index)
        },
        sender=sender,
        cfg=types.SimpleNamespace(),
    )
    assert record.status == "succeeded", record.error
    assert len(sender.calls) == 1  # 내부 ASR 단일 청크 — 실제 전송 1회
    assert client.calls == []  # 1단계에는 Luna가 한 번도 가지 않는다
    summary = service.analysis_overview(workspace)
    assert summary is not None
    assert summary.revision.status == "awaiting_semantic"  # 의미 승인 대기
    row = next(item for item in summary.results if item.asset_id == asset.asset_id)
    assert row.status == "transcribed"
    artifact = _artifact(service, workspace, summary.revision.revision_id, asset.asset_id)
    assert artifact["schema"] == "raw-analysis-asr-phase/v1"
    assert artifact["asr"]["chunks"][0]["segments"]  # 실제 발화가 남았다
    assert artifact["phase_kind"] == "semantic"

    # ── 2단계 견적 — 사람이 볼 실측 바인딩(지문·창·이미지).
    estimates = service.analysis_semantic_estimate(workspace, summary.revision.revision_id)
    assert len(estimates) == 1
    estimate = estimates[0]
    assert estimate.semantic_fingerprint == artifact["semantic_fingerprint"]
    assert estimate.window_count == 1 and estimate.image_count > 0
    assert estimate.luna_model == workflow.models.luna_model
    assert estimate.price_status == "unknown"  # 미확인 단가 — 0원 계산 금지

    def _luna_approval(binding: dict, **overrides) -> OnlineAnalysisApproval:
        values = {
            "approved": True,
            "source_sha256": asset.content_sha256 or "",
            "audio_stream_index": binding["audio"]["audio_stream_index"],
            "sample_count": binding["audio"]["sample_count"],
            "window_count": estimate.window_count,
            "fingerprint": estimate.semantic_fingerprint,
            "asr_model": workflow.models.timing_model,
            "luna_model": workflow.models.luna_model,
            "budget": AnalysisBudget(max_luna_requests=1, max_asr_chunks=1),
        }
        values.update(overrides)
        return OnlineAnalysisApproval(**values)

    # 1단계 재시작도 전사를 다시 부르지 않는다 — 기록 재사용.
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace as _RW

    JobStore(_RW.open(workspace)).update(handle.job_id, status="running")
    service.recover_interrupted_jobs(workspace)
    phase1_again = service.retry_analysis(
        workspace,
        handle.job_id,
        asr_approvals={
            asset.asset_id: _asr_approval(asset, workflow, settings, probe.audio[0].stream_index)
        },
        sender=sender,
        cfg=types.SimpleNamespace(),
    )
    replayed = _wait_terminal(service, workspace, phase1_again.job_id)
    assert replayed.status == "succeeded", replayed.error
    assert len(sender.calls) == 1  # 1단계 기록이 있는 원본은 ASR 없이 대기 상태로
    again_summary = service.analysis_overview(workspace)
    assert again_summary is not None
    assert again_summary.revision.status == "awaiting_semantic"

    # ── 잘못된 2단계 승인 — 실측 지문이 다르면 전사도 Luna도 0송신.
    wrong_client = _FakeClient(_luna_body("window_000000"))
    with pytest.raises(raw.AnalysisError, match="실측 지문과 다르다"):
        service.resume_analysis_semantic(
            workspace,
            handle.job_id,
            luna_approvals={asset.asset_id: _luna_approval(artifact, fingerprint="f" * 64)},
            client=wrong_client,
        )
    assert wrong_client.calls == [] and len(sender.calls) == 1  # 전사 재호출도 없다
    with pytest.raises(raw.AnalysisError, match="2단계.의미. 승인이 없는 원본"):
        service.resume_analysis_semantic(
            workspace, handle.job_id, luna_approvals={}, client=wrong_client
        )
    assert wrong_client.calls == []
    # 잘못된 Luna 모델 승인 — 재개 문에서 바로 닫힌다(송신 0회).
    with pytest.raises(raw.AnalysisError, match="본 Luna 모델과 설정 모델이 다르다"):
        service.resume_analysis_semantic(
            workspace,
            handle.job_id,
            luna_approvals={asset.asset_id: _luna_approval(artifact, luna_model="luna-x")},
            client=wrong_client,
        )
    assert wrong_client.calls == []

    # ── 2단계(의미): 두 번째 승인이 실측 지문을 묶는다 — 전사 0재호출.
    resume = service.resume_analysis_semantic(
        workspace,
        handle.job_id,
        luna_approvals={asset.asset_id: _luna_approval(artifact)},
        client=client,
    )
    resumed = _wait_terminal(service, workspace, resume.job_id)
    assert resumed.status == "succeeded", resumed.error
    assert len(sender.calls) == 1  # 전사는 1단계 기록을 재사용했다(0재호출)
    assert client.calls == [workflow.models.luna_model]  # Luna 창 1 — 실제 호출
    final = service.analysis_overview(workspace)
    assert final is not None and final.revision.status == "complete"
    done = next(item for item in final.results if item.asset_id == asset.asset_id)
    assert done.status == "analyzed" and done.execution_mode == "online_live"
    document = _document(service, workspace, final.revision.revision_id, asset.asset_id)
    assert document["execution_mode"] == "online_live"
    assert document["asr"]["chunks"][0]["segments"]  # 1단계 전사가 문서에 그대로

    # ── 재시작·재개 — 전사 기록은 다시 쓴다(캐시된 전사).
    from vlogsrt.project.raw.analysis_store import AnalysisStore
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace as _RW

    # 프로세스 사망 흉내 — 2단계 작업·리비전을 실행 중 표시로 되돌리고 복구(§14.1).
    JobStore(_RW.open(workspace)).update(resume.job_id, status="running")
    AnalysisStore(_RW.open(workspace)).update_revision(
        summary.revision.revision_id, status="interrupted"
    )
    assert service.recover_interrupted_jobs(workspace) == 1
    restart_client = _FakeClient(_luna_body("window_000000"))
    retry = service.retry_analysis(
        workspace,
        resume.job_id,
        luna_approvals={asset.asset_id: _luna_approval(artifact)},
        client=restart_client,
    )
    assert retry.job_id == resume.job_id  # 같은 2단계 작업 ID로 재개
    again = _wait_terminal(service, workspace, retry.job_id)
    assert again.status == "succeeded", again.error
    assert len(sender.calls) == 1  # 어떤 재개도 전사를 다시 부르지 않는다
    assert restart_client.calls == []  # 완료 원본의 Luna도 다시 사지 않는다(§14.2)


@needs_ffmpeg
def test_live_asr_gates_refuse_wrong_approvals_before_any_send(
    workspace: Path, tmp_path: Path, stereo_offset: Path, monkeypatch
) -> None:
    """잘못된 1단계 승인·설정·종료일·환경·네트워크 — 시작 문에서 0송신으로 닫힌다."""
    _live_online(raw.RawWorkspaceService(), workspace, monkeypatch)
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardK", {"스테레오.mov": stereo_offset}))
    service.confirm_global_source_order(workspace)
    asset = service.list_source_assets(workspace)[0]
    workflow = service.settings(workspace)
    probe = preprocess.probe_raw_media(workspace / asset.relative_path)
    assert probe.audio
    settings = RawUnderstandingSettings()
    stream_index = probe.audio[0].stream_index
    sender = _FakeSender()
    cfg = types.SimpleNamespace()

    def _start(approval, **kwargs):
        return service.start_analysis(
            workspace,
            mode="live",
            asr_approvals={asset.asset_id: approval},
            sender=sender,
            cfg=cfg,
            **kwargs,
        )

    # 잘못된 원본 해시.
    with pytest.raises(raw.AnalysisError, match="본 원본과 지금의 원본이 다르다"):
        _start(_asr_approval(asset, workflow, settings, stream_index, source_sha256="f" * 64))
    # 잘못된 ASR 모델.
    with pytest.raises(raw.AnalysisError, match="본 ASR 모델과 설정 모델이 다르다"):
        _start(_asr_approval(asset, workflow, settings, stream_index, timing_model="stt-x"))
    # 설정 드리프트 — 승인이 본 설정 지문이 아니다.
    with pytest.raises(raw.AnalysisError, match="설정 지문과 지금의 설정 지문이 다르다"):
        _start(
            _asr_approval(
                asset, workflow, RawUnderstandingSettings(window_seconds=11.0), stream_index
            )
        )
    # 종료일 이후 — 수명주기 문이 어떤 전송보다 먼저 닫힌다(§11.10).
    with pytest.raises(raw.AnalysisError, match="종료 예정일"):
        _start(_asr_approval(asset, workflow, settings, stream_index), on=date(2027, 3, 1))
    assert sender.calls == []  # 위의 어떤 경우도 0송신이다

    # 환경 오프라인 — 설정이 online이어도 문은 닫혀 있다(§14.3).
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: True)
    with pytest.raises(raw.AnalysisError, match="오프라인"):
        _start(_asr_approval(asset, workflow, settings, stream_index))
    assert sender.calls == []
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)

    # 네트워크 모드 offline — 승인·경계가 있어도 시작하지 못한다.
    config = service.settings(workspace)
    service.update_settings(workspace, replace(config, network_mode="offline"))
    with pytest.raises(raw.AnalysisError, match="network_mode"):
        _start(_asr_approval(asset, workflow, settings, stream_index))
    assert sender.calls == []


@needs_ffmpeg
def test_live_noaudio_visual_phase_zero_asr(
    workspace: Path, tmp_path: Path, ntsc_noaudio: Path, monkeypatch
) -> None:
    """무성 원본의 실제 호출 — 시각 승인만, ASR는 0회(전사 승인도 필요 없다)."""
    from vlogsrt.project.raw.understanding_luna import AnalysisBudget, OnlineVisualAnalysisApproval

    _live_online(raw.RawWorkspaceService(), workspace, monkeypatch)
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardL", {"무성.mov": ntsc_noaudio}))
    service.confirm_global_source_order(workspace)
    asset = service.list_source_assets(workspace)[0]
    workflow = service.settings(workspace)

    sender = _FakeSender()  # 전사 전송 경계가 있어도 무성 원본은 부르지 않는다
    handle, record = _analyze(
        service,
        workspace,
        mode="live",
        asr_approvals={},  # 무성 — 승인할 전사가 없다
        sender=sender,
        cfg=types.SimpleNamespace(),
    )
    assert record.status == "succeeded", record.error
    assert sender.calls == []  # ASR 0송신 — 작업 자체를 지어내지 않는다(§7.3)
    summary = service.analysis_overview(workspace)
    assert summary is not None and summary.revision.status == "awaiting_semantic"
    artifact = _artifact(service, workspace, summary.revision.revision_id, asset.asset_id)
    assert artifact["phase_kind"] == "visual"
    assert artifact["audio"]["status"] == "audio_absent"
    assert artifact["frames"]["frames"]  # 실제 프레임 증거가 남았다

    estimates = service.analysis_semantic_estimate(workspace, summary.revision.revision_id)
    assert len(estimates) == 1 and estimates[0].phase == "visual"
    approval = OnlineVisualAnalysisApproval(
        approved=True,
        source_sha256=asset.content_sha256 or "",
        frame_count=artifact["video"]["frame_count"],
        window_count=estimates[0].window_count,
        fingerprint=estimates[0].semantic_fingerprint,
        luna_model=workflow.models.luna_model,
        budget=AnalysisBudget(max_luna_requests=1, max_asr_chunks=1),
    )
    client = _FakeClient(_luna_body("window_000000"))
    resume = service.resume_analysis_semantic(
        workspace, handle.job_id, luna_approvals={asset.asset_id: approval}, client=client
    )
    resumed = _wait_terminal(service, workspace, resume.job_id)
    assert resumed.status == "succeeded", resumed.error
    assert sender.calls == []  # 2단계에서도 ASR는 0회
    assert client.calls == [workflow.models.luna_model]  # 시각 Luna 호출만
    final = service.analysis_overview(workspace)
    assert final is not None and final.revision.status == "complete"
    document = _document(service, workspace, final.revision.revision_id, asset.asset_id)
    assert document["audio"]["status"] == "audio_absent"  # 없는 오디오를 지어내지 않는다
    assert document["models"]["asr"] is None
    assert document["execution_mode"] == "online_live"


@needs_ffmpeg
def test_live_without_approvals_or_sender_refuses_before_any_work(
    workspace: Path, tmp_path: Path, stereo_offset: Path, monkeypatch
) -> None:
    """승인·전송 경계가 없는 실제 호출 시작은 바로 닫힌다 — 브라우저가 만들 수 없다."""
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "cardK", {"스테레오.mov": stereo_offset}))
    service.confirm_global_source_order(workspace)
    # 기본 설정은 offline — 승인·경계가 있어도 시작하지 못한다(§14.3).
    with pytest.raises(raw.AnalysisError, match="network_mode"):
        service.start_analysis(
            workspace, mode="live", sender=object(), cfg=object(), client=object()
        )
    config = service.settings(workspace)
    service.update_settings(workspace, replace(config, network_mode="online"))
    # 전송 경계가 없어도 닫힌다.
    with pytest.raises(raw.AnalysisError, match="전송 경계"):
        service.start_analysis(workspace, mode="live")
    # 경계만 있고 1단계 승인이 없어도 닫힌다 — 사람이 만든 기록만이 문을 연다.
    with pytest.raises(raw.AnalysisError, match="승인"):
        service.start_analysis(workspace, mode="live", sender=object(), cfg=object())


# ── 마이그레이션 — v4 작업공간도 덧붙이기만으로 열린다 ─────────────────────────


def test_v4_workspace_upgrades_to_v5(workspace: Path) -> None:
    """받아들인 v4(A3) 작업공간은 v5로 올라가고 기존 행은 그대로다."""
    with sqlite3.connect(workspace / DB_NAME) as connection:
        connection.execute(
            "UPDATE meta SET value = 'raw-workspace-db/4' WHERE key = 'db_schema_version'"
        )
    reopened = raw.RawWorkspaceService().open_workspace(workspace)
    assert reopened.db_schema_version == DB_SCHEMA_VERSION
    with sqlite3.connect(workspace / DB_NAME) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert {"analysis_revisions", "analysis_source_results"} <= tables
