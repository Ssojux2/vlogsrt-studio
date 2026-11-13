"""A3 수직 통합 — 실제 조각 폴더가 생기는지를 ffmpeg로 증명한다(계획 §16 A3).

합성 미디어만 만든다(실제 미디어·API 픽스처 불변). 이 묶음이 증명하는 것:

- 같은 한국어 파일명의 서로 다른 원본 두 장(25fps 소리 있음 · NTSC 소리 없음)이
  배치를 달리해도 한 package에서 **각자 번호**로 산다(§5.2·§6.2).
- 사람이 준 구간의 전체 덮음 계획이 MP4(source_matched)·MOV(edit_prores)에서
  실제 파일이 되고, 이웃 조각의 경계 틱은 정확히 맞닿는다(§6.1·§8.1).
- A/V 시작 차이가 있는 원본에서 내부 클립은 매체 끝에서 잘리고, 원본 오디오
  꼬리는 **마지막 클립만** 보존한다(§8.2·§8.3).
- 둘째 조각 추출 실패는 거기서 멈추고, 같은 작업 재개는 게시물 해시 재검증만
  지나며 번호·ID·파일을 그대로 둔다(§6.3).
- 프로세스 재시작 복구·취소 뒤 이어하기·옛 package 불변·등록 ID 재생·작업공간
  이동 상대 경로(§9.2·§14.1).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import runner as extract_runner
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import ExtractJobError, PackageError, PlannedInterval
from vlogsrt.project.raw.workspace import RawWorkspace

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
    """진짜 조사자로 카드 하나를 가져온다 — 확정·복사까지 끝낸다."""
    proposal = service.scan_sources(root, card)
    confirmation = service.confirm_ingest(root, proposal)
    handle = service.start_ingest_copy(root, confirmation)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    return confirmation


def _extract(service: raw.RawWorkspaceService, root: Path, **kwargs):
    handle = service.start_segment_extraction(root, **kwargs)
    return handle, _wait_terminal(service, root, handle.job_id)


# ── 합성 미디어 — 세션마다 한 번 ──────────────────────────────────────────────


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("a3_media")


def _video_input(duration: str, rate: str) -> list[str]:
    """영상 입력 앞부분 — 키프레임·인코더는 출력 쪽 인자로 뒤에 붙는다."""
    return [
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size=320x180:rate={rate}:duration={duration}",
    ]


def _x264_output() -> list[str]:
    """H.264 출력 인자 — 3초마다 키프레임(경계 절단이 GOP 중간이 되게)."""
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
def k25_audio(media: Path) -> Path:
    """6초 25fps H.264 + AAC — 소리 있는 원본(카드 A의 중복 이름)."""
    path = media / "k25_audio.mov"
    _encode(
        [
            *_video_input("6", "25"),
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=6",
            *_x264_output(),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            "-metadata",
            "creation_time=2026-09-01T10:00:00Z",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def ntsc_noaudio(media: Path) -> Path:
    """30000/1001 H.264 소리 없음 — µs로 안 떨어지는 프레임 길이(카드 B의 중복 이름)."""
    path = media / "ntsc_noaudio.mov"
    _encode([*_video_input("3.5", "30000/1001"), *_x264_output(), str(path)])
    return path


@pytest.fixture(scope="session")
def audio_tail(media: Path) -> Path:
    """영상 6초 + 오디오 7.5초 — 꼬리 보존 판정의 근거(§8.2)."""
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


@pytest.fixture(scope="session")
def late_audio(media: Path) -> Path:
    """오디오가 3.5초 늦게 시작 — A/V 시작 차이 보존(§8.3)."""
    path = media / "late_audio.mp4"
    _encode(
        [
            *_video_input("6", "25"),
            "-itsoffset",
            "3.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2.5",
            *_x264_output(),
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            str(path),
        ]
    )
    return path


@pytest.fixture(scope="session")
def plain_mp4(media: Path) -> Path:
    path = media / "plain.mp4"
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=5",
            "-force_key_frames",
            "expr:gte(t,n_forced*3)",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-shortest",
            str(path),
        ]
    )
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "VlogProject"
    raw.RawWorkspaceService().create_workspace(root, "제주")
    return root


def _card(tmp_path: Path, name: str, files: dict[str, Path]) -> Path:
    card = tmp_path / name
    for relative, source in files.items():
        target = card / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return card


# ── 원본 조사 기억이 라이브러리를 담는다(D121) ──────────────────────────────────


@needs_ffmpeg
def test_planning_again_probes_nothing_even_when_the_library_outgrows_the_base_memo(
    workspace: Path, tmp_path: Path, k25_audio: Path, ntsc_noaudio: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # fmt: skip
    """카드를 누를 때마다 계획을 다시 세운다.

    원본이 기본 칸보다 많아도 두 번째 계획은 패킷을 훑지 않는다.
    """
    monkeypatch.setattr(preprocess, "_PROBE_MEMO", preprocess._ProbeMemo(
        preprocess.ProbeMemoSettings(entries=1)
    ))  # fmt: skip
    scans: list[Path] = []
    real_clock = preprocess.probe_extraction_clock

    def counted(source: Path, **kwargs: Any) -> preprocess.RawVideoClock:
        scans.append(Path(source))
        return real_clock(source, **kwargs)

    monkeypatch.setattr(preprocess, "probe_extraction_clock", counted)
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, _card(tmp_path, "card", {
        "DCIM/a.MOV": k25_audio, "DCIM/b.MOV": ntsc_noaudio,
    }))  # fmt: skip
    service.confirm_global_source_order(workspace)
    first = service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    after_first = len(scans)
    second = service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    assert after_first >= 2
    assert len(scans) == after_first  # 두 번째 계획은 원본을 다시 훑지 않았다
    assert [e.core_end_pts for e in second.entries] == [e.core_end_pts for e in first.entries]


# ── 중복 한국어 파일명 두 배치 → 한 package ──────────────────────────────────


@needs_ffmpeg
def test_duplicate_korean_filenames_two_batches_one_package(
    workspace: Path, tmp_path: Path, k25_audio: Path, ntsc_noaudio: Path
) -> None:
    """같은 이름의 다른 원본 둘 — 배치를 달리 가져와도 덮어쓰지 않고 각자 번호."""
    service = raw.RawWorkspaceService()
    card_a = _card(tmp_path, "cardA", {"DCIM/제주일차.MOV": k25_audio})
    card_b = _card(tmp_path, "cardB", {"DCIM/제주일차.MOV": ntsc_noaudio})
    path_a = card_a / "DCIM/제주일차.MOV"
    path_b = card_b / "DCIM/제주일차.MOV"
    before_a, before_b = _sha256(path_a), _sha256(path_b)
    _ingest_card(service, workspace, card_a)
    _ingest_card(service, workspace, card_b)

    order = service.confirm_global_source_order(workspace)
    assert len(order.entries) == 2
    assert {entry.batch_dir for entry in order.entries} == {"import_000001", "import_000002"}

    snapshot = service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    by_source: dict[int, list[raw.SegmentPlanEntry]] = {}
    for entry in snapshot.entries:
        by_source.setdefault(entry.source_order, []).append(entry)
    assert sorted(by_source) == [1, 2]
    assert len(by_source[1]) == 3  # 6초 / 2초 타일
    assert len(by_source[2]) == 2  # 3.5초 / 2초 타일
    for entries in by_source.values():
        for left, right in zip(entries[:-1], entries[1:], strict=True):
            assert left.core_end_pts == right.core_start_pts  # 이웃 정확히 맞닿음

    handle, record = _extract(service, workspace)
    assert record.status == "succeeded", record.error
    summary = service.package_overview(workspace)
    assert summary is not None and summary.package.complete
    assert summary.package.clip_published == 5

    folders = sorted(
        (item for item in (workspace / "clips/package_000001").iterdir() if item.is_dir()),
        key=lambda item: item.name,
    )
    assert [folder.name for folder in folders] == [
        "000001_제주일차.MOV",
        "000002_제주일차.MOV",
    ]
    first_files = sorted(
        path.name for path in folders[0].iterdir() if path.suffix in {".mp4", ".mov"}
    )
    second_files = sorted(
        path.name for path in folders[1].iterdir() if path.suffix in {".mp4", ".mov"}
    )
    # 원본마다 조각 번호는 000001부터 — 시간순이 원본 안 번호의 전부다(§6.1).
    assert [name.split("__")[0] for name in first_files] == ["000001", "000002", "000003"]
    assert [name.split("__")[0] for name in second_files] == ["000001", "000002"]

    # 카드 원본은 그대로다 — 해시 불변(§5.2). 작업공간 원본도 검증 기록과 같다.
    assert _sha256(card_a / "DCIM/제주일차.MOV") == before_a
    assert _sha256(card_b / "DCIM/제주일차.MOV") == before_b
    for asset in service.list_source_assets(workspace):
        assert asset.status == "ready"
        assert _sha256(workspace / asset.relative_path) == asset.content_sha256

    first_probe = preprocess.probe_raw_media(workspace / folders[0] / first_files[0])
    assert first_probe.video.r_frame_rate == Fraction(25)
    second_probe = preprocess.probe_raw_media(workspace / folders[1] / second_files[0])
    assert second_probe.video.r_frame_rate == Fraction(30000, 1001)


# ── 사람 구간 전체 덮음 — MP4(source_matched)·MOV(edit_prores) ────────────────


@needs_ffmpeg
def test_user_intervals_full_coverage_mp4_and_mov(
    workspace: Path, tmp_path: Path, plain_mp4: Path, k25_audio: Path
) -> None:
    card = _card(tmp_path, "cardC", {"평화.mp4": plain_mp4, "제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    assets = service.list_source_assets(workspace)
    by_name = {asset.source_name: asset for asset in assets}

    snapshot = service.plan_segmentation(
        workspace,
        intervals={
            by_name["평화.mp4"].asset_id: [
                PlannedInterval(Fraction(0), Fraction("2.5")),
                PlannedInterval(Fraction("2.5"), Fraction(5)),
            ],
            by_name["제주일차.MOV"].asset_id: [
                PlannedInterval(Fraction(0), Fraction(6)),
            ],
        },
    )
    assert snapshot.basis == "user_planned"
    spans = [entry for entry in snapshot.entries if entry.asset_id == by_name["평화.mp4"].asset_id]
    assert spans[0].core_end_pts == spans[1].core_start_pts

    handle, record = _extract(service, workspace, profile_name="edit_prores")
    assert record.status == "succeeded", record.error
    summary = service.package_overview(workspace)
    assert summary is not None and summary.package.complete
    for clip in summary.clips:
        assert clip.profile_name == "edit_prores"
        assert clip.relative_path.endswith(".mov")
        probe = preprocess.probe_raw_media(workspace / clip.relative_path)
        assert probe.video.codec_name == "prores"
        assert probe.audio and probe.audio[0].codec_name == "pcm_s16le"
    manifests = summary.manifest_relative_paths
    assert manifests == (
        "clips/package_000001/clips.jsonl",
        "clips/package_000001/clip_index.csv",
        "clips/package_000001/README.md",
    )


# ── A/V 시작 차이·오디오 꼬리 ────────────────────────────────────────────────


@needs_ffmpeg
def test_av_offset_and_whole_tail_only_on_last_clip(
    workspace: Path, tmp_path: Path, late_audio: Path, audio_tail: Path
) -> None:
    card = _card(tmp_path, "cardD", {"늦은소리.mp4": late_audio, "긴소리.mp4": audio_tail})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    handle, record = _extract(service, workspace)
    assert record.status == "succeeded", record.error
    summary = service.package_overview(workspace)
    assert summary is not None

    late = sorted(
        (clip for clip in summary.clips if "늦은소리" in clip.relative_path),
        key=lambda clip: clip.segment_order,
    )
    tail = sorted(
        (clip for clip in summary.clips if "긴소리" in clip.relative_path),
        key=lambda clip: clip.segment_order,
    )
    # 늦은 오디오(3.5~6초): 첫 클립에는 소리가 없다 — 지어내지 않고 unmapped 기록.
    first_probe = preprocess.probe_raw_media(workspace / late[0].relative_path)
    assert not first_probe.audio
    # 소리가 시작되는 클립(2~4초)의 오디오는 지역 1.5초부터 — 공통 기준 변환(§8.3).
    middle_probe = preprocess.probe_raw_media(workspace / late[1].relative_path)
    assert middle_probe.audio
    audio_start = middle_probe.audio[0].start_time
    assert Fraction("1.4") < audio_start < Fraction("1.7")
    # 영상보다 긴 오디오(7.5초): 내부 클립은 매체 끝에서, 마지막 클립만 꼬리까지.
    internal = Fraction(tail[1].media_local_duration)
    last = Fraction(tail[2].media_local_duration)
    assert Fraction("1.9") < internal < Fraction("2.3")
    assert last > Fraction("3.0")  # 2초 영상 + 1.5초 꼬리 ≈ 3.5초


# ── 실패 주입 — 거기서 멈추고, 같은 작업 재개는 재검증만 ─────────────────────


class _FailingExtractor:
    """둘째 호출부터 정해진 순서에 실패하는 감싼 추출기 — 호출 기록을 남긴다.

    진짜 추출기는 생성 때 붙잡아 둔다 — 모듞性 이름을 다시 보면 자기 자신을
    부르는 재귀가 된다(디버그 재현).
    """

    def __init__(self, fail_on: set[int]) -> None:
        self.fail_on = fail_on
        self.calls: list[Path] = []
        self._lock = threading.Lock()
        self._real: Callable = extract_runner.DEFAULT_EXTRACTOR

    def __call__(self, source, destination, *args, **kwargs):
        with self._lock:
            self.calls.append(destination)
            number = len(self.calls)
        if number in self.fail_on:
            raise preprocess.RawClipVerificationError(f"주입된 실패 #{number}")
        return self._real(source, destination, *args, **kwargs)


@needs_ffmpeg
def test_second_clip_failure_stops_publication_and_resume_keeps_numbering(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    card = _card(tmp_path, "cardE", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    plan = service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    assert len(plan.entries) == 3

    failing = _FailingExtractor(fail_on={2})
    original = extract_runner.DEFAULT_EXTRACTOR
    extract_runner.DEFAULT_EXTRACTOR = failing  # type: ignore[assignment]
    try:
        handle, record = _extract(service, workspace)
    finally:
        extract_runner.DEFAULT_EXTRACTOR = original  # type: ignore[assignment]
    assert record.status == "failed"
    assert "주입된 실패 #2" in (record.error or "")
    _ = ExtractJobError
    assert len(failing.calls) == 2  # 실패 뒤의 조각은 부르지도 않는다(§6.3)

    summary = service.package_overview(workspace)
    assert summary is not None
    assert summary.package.status == "failed"
    published = [clip for clip in summary.clips if clip.status == "published"]
    failed = [clip for clip in summary.clips if clip.status == "failed"]
    assert [clip.segment_order for clip in published] == [1]
    assert [clip.segment_order for clip in failed] == [2]
    first_file = workspace / published[0].relative_path
    first_hash, first_inode = _sha256(first_file), first_file.stat().st_ino
    ids_before = [clip.clip_id for clip in summary.clips]

    # 같은 작업 재개 — 게시물은 해시 재검증만, 번호·ID는 그대로(§6.3).
    extract_runner.DEFAULT_EXTRACTOR = original  # type: ignore[assignment]
    retry = service.retry_segment_extraction(workspace, handle.job_id)
    record = _wait_terminal(service, workspace, retry.job_id)
    assert record.status == "succeeded", record.error

    resumed = service.package_overview(workspace)
    assert resumed is not None and resumed.package.complete
    assert [clip.clip_id for clip in resumed.clips][: len(ids_before)] == ids_before
    assert [clip.segment_order for clip in resumed.clips] == [1, 2, 3]
    assert _sha256(first_file) == first_hash  # 재검증만 지났다 — 내용 불변
    assert first_file.stat().st_ino == first_inode  # 파일을 다시 쓰지도 않았다


@needs_ffmpeg
def test_tampered_published_clip_fails_resume_without_overwrite(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    """게시물이 어긋난 재개 — 지우지 않고 실패로 알린다(조용한 덮어쓰기 금지)."""
    card = _card(tmp_path, "cardF", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    service.plan_segmentation(workspace, tile_seconds=Fraction(2))

    failing = _FailingExtractor(fail_on={2})
    original = extract_runner.DEFAULT_EXTRACTOR
    extract_runner.DEFAULT_EXTRACTOR = failing  # type: ignore[assignment]
    try:
        handle, record = _extract(service, workspace)
    finally:
        extract_runner.DEFAULT_EXTRACTOR = original  # type: ignore[assignment]
    assert record.status == "failed"
    summary = service.package_overview(workspace)
    assert summary is not None
    first = next(clip for clip in summary.clips if clip.status == "published")
    tampered = workspace / first.relative_path
    tampered.write_bytes(tampered.read_bytes() + b"\x00tamper")

    retry = service.retry_segment_extraction(workspace, handle.job_id)
    record = _wait_terminal(service, workspace, retry.job_id)
    assert record.status == "failed"
    assert tampered.stat().st_size > 0  # 지우지 않았다
    assert b"tamper" in tampered.read_bytes()

    # 회귀 두 번(루트 재현): 한번 게시된 조각은 실패 상태가 된 뒤에도 **다시
    # 추출되지 않는다** — 재시도를 거듭해도 지우지·덮어쓰지·되살리지 않는다.
    counting = _FailingExtractor(fail_on=set())
    extract_runner.DEFAULT_EXTRACTOR = counting  # type: ignore[assignment]
    try:
        for _ in range(2):
            again = service.retry_segment_extraction(workspace, retry.job_id)
            record = _wait_terminal(service, workspace, again.job_id)
            assert record.status == "failed"
            assert "다시 추출하지 않는다" in (record.error or "")
    finally:
        extract_runner.DEFAULT_EXTRACTOR = original  # type: ignore[assignment]
    assert counting.calls == []  # 호출 자체가 없다 — 새 파일을 몰래 만들지 않는다
    assert b"tamper" in tampered.read_bytes()  # 내용도 그대로다


# ── 같은 계획 · 두 프로파일 — 조각 ID는 package에 묶인다(루트 재현) ────────────


@needs_ffmpeg
def test_same_plan_two_profiles_get_distinct_clip_ids_and_packages(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    """같은 분할 계획을 MP4(source_matched)·MOV(edit_prores)로 추출해도 조각 ID가
    갈라지고, 옛 package의 행·전달물·바이트는 그대로다(§6.5)."""
    card = _card(tmp_path, "cardK", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    plan = service.plan_segmentation(workspace, tile_seconds=Fraction(2))

    first_handle, first_record = _extract(service, workspace, profile_name="source_matched")
    assert first_record.status == "succeeded", first_record.error
    first = service.package_overview(workspace)
    assert first is not None and first.package.package_dir == "clips/package_000001"
    first_ids = [clip.clip_id for clip in first.clips]
    first_manifest = _sha256(workspace / first.manifest_relative_paths[0])
    first_bytes = {
        clip.relative_path: _sha256(workspace / clip.relative_path) for clip in first.clips
    }

    second_handle, second_record = _extract(
        service, workspace, plan_revision_id=plan.revision_id, profile_name="edit_prores"
    )
    assert second_record.status == "succeeded", second_record.error
    second = service.package_overview(workspace)
    assert second is not None and second.package.package_dir == "clips/package_000002"
    second_ids = [clip.clip_id for clip in second.clips]
    assert len(first_ids) == len(second_ids) == 3
    assert set(first_ids).isdisjoint(second_ids)  # 같은 계획이어도 ID는 package에 묶인다

    # 옛 package는 다시 쓰지 않는다 — 행·전달물·바이트 모두 그대로.
    assert _sha256(workspace / first.manifest_relative_paths[0]) == first_manifest
    for relative, digest in first_bytes.items():
        assert _sha256(workspace / relative) == digest
    assert [clip.status for clip in first.clips] == ["published"] * 3


# ── 루트가 준 실제 픽스처 — 스테레오·A/V 오프셋·꼬리·촬영 시각(읽기 전용) ─────

ROOT_FIXTURES = Path("/tmp/vlogsrt-raw-runner-acceptance")
"""루트가 독립적으로 만든 실측 입력 — 읽기만 한다(다시 만들지 않는다)."""

needs_root_fixtures = pytest.mark.skipif(
    not (ROOT_FIXTURES / "card-a" / "DCIM").is_dir(),
    reason="루트 실측 픽스처가 없다(/tmp/vlogsrt-raw-runner-acceptance)",
)


@needs_ffmpeg
@needs_root_fixtures
def test_root_real_fixtures_stereo_offset_tail_and_creation_binding(
    workspace: Path,
) -> None:
    """card-a(3.2초 25fps 스테레오, 소리 0.4초부터+꼬리)·card-b(2.002초 NTSC 무성).

    루트가 만든 실측 입력으로만 잰다: 촬영 시각 묶음(10:00Z/11:00Z), 채널 보존,
    A/V 상대 오프셋 보존, 영상 끝 꼬리는 마지막 클립만. 원본 해시는 루트의
    `source-hashes.json`과 대조한다.
    """
    import json as json_module

    service = raw.RawWorkspaceService()
    for name in ("card-a", "card-b"):
        _ingest_card(service, workspace, ROOT_FIXTURES / name)
    expected = {
        item["path"]: item["sha256"]
        for item in json_module.loads((ROOT_FIXTURES / "source-hashes.json").read_text())
    }
    order = service.confirm_global_source_order(workspace)
    assert [entry.ordinal for entry in order.entries] == [1, 2]

    # 촬영 시각이 유효하게 묶였다 — 배치 순서와 무관하게 시각 순이다.
    assets = {asset.asset_id: asset for asset in service.list_source_assets(workspace)}
    for asset in assets.values():
        assert asset.capture_time is not None
        assert asset.capture_time.startswith("2026-09-30T1")

    snapshot = service.plan_segmentation(workspace, tile_seconds=Fraction(1))
    by_source: dict[int, list] = {}
    for entry in snapshot.entries:
        by_source.setdefault(entry.source_order, []).append(entry)
    assert [len(by_source[1]), len(by_source[2])] == [4, 3]

    handle, record = _extract(service, workspace)
    assert record.status == "succeeded", record.error
    summary = service.package_overview(workspace)
    assert summary is not None and summary.package.complete

    stereo = sorted(
        (clip for clip in summary.clips if clip.source_order == 1),
        key=lambda clip: clip.segment_order,
    )
    silent = [clip for clip in summary.clips if clip.source_order == 2]
    # 카드 원본 해시 불변 — 루트 manifest와 대조한다(경로는 /tmp↔/private/tmp
    # 표기 차가 있으므로 카드 이름+파일 이름으로 맞춘다).
    by_card = {"/".join(key.split("/")[-3:-1]): value for key, value in expected.items()}
    for asset in assets.values():
        original = Path(asset.source_root) / "DCIM" / asset.source_name
        card_key = f"{Path(asset.source_root).name}/DCIM"
        assert by_card[card_key] == _sha256(original)
    # 스테레오 채널·A/V 오프셋 보존: 첫 클립의 소리는 지역 0.4초부터, 채널은 2.
    first_probe = preprocess.probe_raw_media(workspace / stereo[0].relative_path)
    assert first_probe.audio and first_probe.audio[0].channels == 2
    assert Fraction("0.3") < first_probe.audio[0].start_time < Fraction("0.5")
    # 무성 원본도 조각이 된다 — 소리를 지어내지 않는다.
    for clip in silent:
        probe = preprocess.probe_raw_media(workspace / clip.relative_path)
        assert not probe.audio
    # 꼬리는 마지막 클립만(영상 3.2초, 소리는 3.4초까지) — 0.8초 타일×4에서
    # 내부 클립은 ≈0.8초+반올림, 마지막 클립은 꼬리 0.2초까지 산다.
    internal = Fraction(stereo[1].media_local_duration)
    last = Fraction(stereo[3].media_local_duration)
    assert Fraction("0.75") < internal < Fraction("0.95")
    assert last > internal + Fraction("1") / 10


# ── 프로세스 재시작 복구 — 디스크의 상태가 정본이다(§14.1) ────────────────────


@needs_ffmpeg
def test_fresh_process_recovers_and_resumes_durable_state(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    card = _card(tmp_path, "cardG", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    service.plan_segmentation(workspace, tile_seconds=Fraction(2))

    failing = _FailingExtractor(fail_on={2})
    original = extract_runner.DEFAULT_EXTRACTOR
    extract_runner.DEFAULT_EXTRACTOR = failing  # type: ignore[assignment]
    try:
        handle, record = _extract(service, workspace)
    finally:
        extract_runner.DEFAULT_EXTRACTOR = original  # type: ignore[assignment]
    assert record.status == "failed"

    # 프로세스 사망 흉내: 실행 중 표시로 되돌리고 새 서비스가 복구한다.
    RawWorkspace.open(workspace)  # 상태는 디스크에 있다
    from vlogsrt.project.raw.jobs import JobStore

    JobStore(RawWorkspace.open(workspace)).update(handle.job_id, status="running")
    restarted = raw.RawWorkspaceService()  # 새 프로세스
    assert restarted.recover_interrupted_jobs(workspace) == 1
    recovered = restarted.get_job(workspace, handle.job_id)
    assert recovered.status == "interrupted"

    retry = restarted.retry_segment_extraction(workspace, handle.job_id)
    record = _wait_terminal(restarted, workspace, retry.job_id)
    assert record.status == "succeeded", record.error
    summary = restarted.package_overview(workspace)
    assert summary is not None and summary.package.complete


# ── 취소 — 조각 사이 게이트, 끝난 결과는 보존(§14.1) ─────────────────────────


@needs_ffmpeg
def test_cancel_between_clips_preserves_published_and_resumes(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    card = _card(tmp_path, "cardH", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    service.plan_segmentation(workspace, tile_seconds=Fraction(2))

    waiting = threading.Event()
    release = threading.Event()
    original = extract_runner.DEFAULT_EXTRACTOR
    calls: list[Path] = []

    def cancelling_extractor(source, destination, *args, **kwargs):
        calls.append(destination)
        if len(calls) == 2:
            waiting.set()
            assert release.wait(timeout=30)
        return original(source, destination, *args, **kwargs)  # 붙잡은 진짜를 부른다

    extract_runner.DEFAULT_EXTRACTOR = cancelling_extractor  # type: ignore[assignment]
    try:
        handle = service.start_segment_extraction(workspace)
        assert waiting.wait(timeout=30)
        service.cancel_job(workspace, handle.job_id)
        release.set()
        record = _wait_terminal(service, workspace, handle.job_id)
        assert record.status == "cancelled"
        summary = service.package_overview(workspace)
        assert summary is not None
        assert summary.package.status == "interrupted"
        assert [clip.status for clip in summary.clips] == [
            "published",
            "failed",
        ]  # active ffmpeg cancelled

        # 취소된 작업은 재개 대상이 아니다 — 같은 지문의 새 시작이 package를 이어 쓴다.
        handle2, record2 = _extract(service, workspace)
        assert record2.status == "succeeded", record2.error
    finally:
        extract_runner.DEFAULT_EXTRACTOR = original  # type: ignore[assignment]
    resumed = service.package_overview(workspace)
    assert resumed is not None and resumed.package.complete
    assert resumed.package.package_dir == summary.package.package_dir  # 같은 package
    assert [clip.segment_order for clip in resumed.clips] == [1, 2, 3]
    assert len(calls) == 4  # 완료된 첫 조각 재사용; 취소된 둘째와 누락된 셋째만 추출


# ── 옛 package·계획 불변 — 새 추출은 새 package(§6.5) ─────────────────────────


@needs_ffmpeg
def test_old_package_and_plan_revisions_are_immutable(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    card = _card(tmp_path, "cardI", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    first_plan = service.plan_segmentation(workspace, tile_seconds=Fraction(2))
    handle, record = _extract(service, workspace)
    assert record.status == "succeeded", record.error
    first_summary = service.package_overview(workspace)
    assert first_summary is not None
    first_manifest_hash = _sha256(workspace / first_summary.manifest_relative_paths[0])
    first_clip_hash = _sha256(workspace / first_summary.clips[0].relative_path)

    # 같은 지문의 재시작은 새 package를 만들지 않는다 — 완성 안내로 끝난다.
    with pytest.raises(PackageError, match="이미 완성된 package"):
        service.start_segment_extraction(workspace, plan_revision_id=first_plan.revision_id)

    # 다른 계획(다른 타일)은 새 리비전·새 package — 옛 것은 그대로다.
    second_plan = service.plan_segmentation(workspace, tile_seconds=Fraction(3))
    assert second_plan.revision_id != first_plan.revision_id
    handle2, record2 = _extract(service, workspace, plan_revision_id=second_plan.revision_id)
    assert record2.status == "succeeded", record2.error
    second_summary = service.package_overview(workspace)
    assert second_summary is not None
    assert second_summary.package.package_dir == "clips/package_000002"

    assert _sha256(workspace / first_summary.manifest_relative_paths[0]) == first_manifest_hash
    assert _sha256(workspace / first_summary.clips[0].relative_path) == first_clip_hash
    restored_first = service.segment_plan_revision(workspace, first_plan.revision_id)
    assert restored_first.entries == first_plan.entries
    assert restored_first.fingerprint == first_plan.fingerprint
    assert [clip.segment_order for clip in first_summary.clips] == [1, 2, 3]
    assert [clip.segment_order for clip in second_summary.clips] == [1, 2]


# ── 등록 ID 재생·작업공간 이동 상대 경로(§4.3·§9.2) ──────────────────────────


@needs_ffmpeg
def test_registered_id_playback_and_workspace_movement(
    workspace: Path, tmp_path: Path, k25_audio: Path
) -> None:
    card = _card(tmp_path, "cardJ", {"제주일차.MOV": k25_audio})
    service = raw.RawWorkspaceService()
    _ingest_card(service, workspace, card)
    service.confirm_global_source_order(workspace)
    service.plan_segmentation(workspace, tile_seconds=Fraction(3))
    handle, record = _extract(service, workspace)
    assert record.status == "succeeded", record.error
    summary = service.package_overview(workspace)
    assert summary is not None

    for clip in summary.clips:
        assert clip.file_id is not None
        resolved = service.resolve_media(workspace, clip.file_id)
        assert resolved.kind == "artifact"
        assert resolved.path.is_file()
        assert resolved.path.stat().st_size > 0
        assert str(resolved.path).startswith(str(workspace / "clips"))
        assert resolved.content_type in {"video/mp4", "video/quicktime"}

    # 작업공간을 통째로 옮겨도 상대 경로로 다시 열린다(§9.2).
    moved = tmp_path / "moved-project"
    shutil.copytree(workspace, moved)
    reopened = raw.RawWorkspaceService()
    status = reopened.open_workspace(moved)
    assert status.workspace_root == str(moved.expanduser().resolve())
    moved_summary = reopened.package_overview(moved)
    assert moved_summary is not None and moved_summary.package.complete
    assert moved_summary.manifest_relative_paths == summary.manifest_relative_paths
    moved_clip = moved_summary.clips[0]
    assert moved_clip.file_id is not None
    resolved = reopened.resolve_media(moved, moved_clip.file_id)
    assert resolved.path.is_file()
    # 옮긴 작업공간의 원본도 SHA 재독출로 검증된다 — 등록 ID는 그대로(§14.2).
    original_ids = {
        clip.clip_id: clip.file_id for clip in summary.clips if clip.file_id is not None
    }
    for clip in moved_summary.clips:
        assert clip.file_id == original_ids[clip.clip_id]
    reopened.plan_segmentation(moved)  # 원본 해시 재검증까지 통과한다
