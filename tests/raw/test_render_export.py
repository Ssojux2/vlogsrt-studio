"""추천만 이어붙인 초안 영상 + SRT + 보고서(W-44, D100 §5b) — 렌더러와 내보내기.

합성 lavfi 원본만 쓴다(유료 호출 0회 — 분석은 재생 픽스처). 렌더러 쪽은 작은 화면으로 프레임 수·
필러박스·24→30 변환·소리 없는 원본의 침묵·인코더 물러나기·검증 실패를 보고, 내보내기 쪽은 폴더
하나(4K@30 2초 · 1080p@30 9초 · 세로 1080×1920@24 2초, 촬영 시각은 이름 순과 거꾸로)를 불러와 분석·
정리한 뒤 실제로 낸다: 1920×1080@30 H.264 + AAC 48 kHz 2채널 · 프레임 수 = 계획 · 길이 1/60초 안 ·
SRT = 계획의 투영 · 확인하지 않은 빨강도 내고 보고서에 남김 · map 원본 제외 · 두 번째 내보내기는 새
폴더 · 원본 바이트·mtime 그대로 · 작업공간 `export/` 밖에 쓰지 않음 · 취소하면 폴더를 남기지 않음.
"""

from __future__ import annotations

import array
import dataclasses
import hashlib
import json
import shutil
import subprocess
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_review_workspace import _wait_terminal, _write_replay_fixtures
from vlogsrt import preprocess
from vlogsrt.project.raw import render_export, render_plan, timeline_srt
from vlogsrt.project.raw.config import RENDER_SETTINGS
from vlogsrt.project.raw.jobs import JobCancelledError
from vlogsrt.project.raw.service import ANALYSIS_PROXY_JOB_KIND, RawWorkspaceService
from vlogsrt.project.timebase import milliseconds_half_up
from vlogsrt.task_runtime import cancellation_scope

pytestmark = [
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe가 없는 환경",
    ),
]

F = Fraction
MOMENT = datetime(2026, 10, 28, 1, 2, 3, tzinfo=UTC)
SMALL = preprocess.RenderTarget(320, 180, F(1, 30))
_ENCODER_WORKS = preprocess.encoder_works
"""시험이 가짜로 바꾸기 전의 진짜(캐시 있는) 확인 함수."""


def _encode(
    path: Path, size: str, rate: int, seconds: int, *, audio: bool = True, created: str = ""
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration={seconds}",
    ]  # fmt: skip
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={seconds}"]
    command += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-threads", "2"]
    command += ["-c:a", "aac", "-ac", "1"] if audio else []
    command += ["-metadata", f"creation_time={created}"] if created else []
    subprocess.run([*command, str(path)], check=True, capture_output=True)
    return path


def _encoding(codec: str = "libx264") -> preprocess.RenderEncoding:
    return render_export._encoding(RENDER_SETTINGS, codec)


def _streams(path: Path) -> dict[str, Any]:
    completed = subprocess.run(
        [
            "ffprobe", "-v", "error", "-count_frames",
            "-show_entries", "stream=codec_type,codec_name,width,height,r_frame_rate,"
            "nb_read_frames,sample_rate,channels:format=duration",
            "-of", "json", str(path),
        ],
        check=True, capture_output=True, text=True,
    )  # fmt: skip
    payload = json.loads(completed.stdout)
    by_type = {stream["codec_type"]: stream for stream in payload["streams"]}
    return {**by_type, "duration": F(payload["format"]["duration"])}


def _gray_frame(path: Path, at: Fraction, size: tuple[int, int]) -> bytes:
    """`at`초의 한 장을 작은 회색조로 — 필러박스 칸을 재는 재료."""
    completed = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", f"{float(at):.6f}", "-i", str(path),
            "-frames:v", "1", "-vf", f"scale={size[0]}:{size[1]}",
            "-f", "rawvideo", "-pix_fmt", "gray", "-",
        ],
        check=True, capture_output=True,
    )  # fmt: skip
    return completed.stdout


def _pcm(path: Path) -> bytes:
    """첫 소리 트랙을 48 kHz 모노 s16 그대로."""
    completed = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "48000",
         "-f", "s16le", "-"],
        check=True, capture_output=True,
    )  # fmt: skip
    return completed.stdout


def _assert_pillarboxed(frame: bytes, width: int, height: int, border: int) -> None:
    rows = [frame[row * width : (row + 1) * width] for row in range(height)]
    left = max(max(row[:border]) for row in rows)
    right = max(max(row[width - border :]) for row in rows)
    middle = max(max(row[width // 2 - border : width // 2 + border]) for row in rows)
    assert left <= 24 and right <= 24  # 채움색 검정(Y 16) — 인코딩 오차만큼
    assert middle > 100  # 그림이 가운데에 있다


# ── 렌더러(preprocess) ────────────────────────────────────────────────────


def test_the_encoder_listing_is_parsed_from_the_table() -> None:
    listing = (
        "Encoders:\n V..... = Video\n ------\n"
        " V....D libx264              libx264 H.264 / AVC\n"
        " V....D h264_videotoolbox    VideoToolbox H.264 Encoder\n"
        " A....D aac                  AAC (Advanced Audio Coding)\n"
    )
    assert preprocess._parse_encoders(listing) == frozenset({"libx264", "h264_videotoolbox", "aac"})
    assert "libx264" in preprocess.available_encoders()
    assert preprocess.available_encoders() is preprocess.available_encoders()  # 한 번만 묻는다


def test_a_listed_hardware_encoder_that_fails_its_probe_falls_back_to_libx264(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []
    real = preprocess.encoder_works

    def recorded(name: str) -> bool:
        probed.append(name)
        return bool(real(name))

    monkeypatch.setattr(
        preprocess, "available_encoders", lambda: frozenset({"h264_fakehw", "libx264"})
    )
    monkeypatch.setattr(preprocess, "encoder_works", recorded)
    picked = preprocess.pick_video_encoder(("h264_unlisted", "h264_fakehw", "libx264"))
    assert picked == "libx264"
    assert probed == ["h264_fakehw", "libx264"]  # 목록에 없는 후보는 띄우지도 않는다
    assert real("h264_fakehw") is False  # 목록에 있어도 실제로 돌지 않으면 건너뛴다
    with pytest.raises(RuntimeError, match="H.264 인코더가 없습니다"):
        preprocess.pick_video_encoder(("h264_unlisted",))


@pytest.fixture(scope="module")
def small_media(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    folder = tmp_path_factory.mktemp("render_media")
    return {
        "wide": _encode(folder / "wide.mp4", "160x90", 30, 2),
        "tall_silent": _encode(folder / "tall.mp4", "90x160", 24, 2, audio=False),
    }


def test_a_silent_24fps_portrait_part_is_pillarboxed_converted_and_filled_with_silence(
    small_media: dict[str, Path], tmp_path: Path
) -> None:
    part = preprocess.RenderPartSpec(index=0, start_frame=0, end_frame=48, output_frames=60)
    path = preprocess.render_part(small_media["tall_silent"], part, SMALL, _encoding(), tmp_path)
    assert path == tmp_path / "part00000.mov"
    streams = _streams(path)
    video, audio = streams["video"], streams["audio"]
    assert (video["width"], video["height"], video["r_frame_rate"]) == (320, 180, "30/1")
    assert int(video["nb_read_frames"]) == 60  # 48장(24fps 2초)을 30fps 60장으로
    assert (audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) == (
        "pcm_s16le", 48000, 2,
    )  # fmt: skip
    pcm = _pcm(path)
    assert len(pcm) == 2 * 48000 * 2  # 정확히 2초의 침묵 — 이은 뒤 소리가 밀리지 않는다
    assert pcm == bytes(len(pcm))
    _assert_pillarboxed(_gray_frame(path, F(1), (64, 36)), 64, 36, border=12)


def test_parts_concatenate_by_copy_and_the_sound_stays_on_its_own_part(
    small_media: dict[str, Path], tmp_path: Path
) -> None:
    encoding = _encoding()
    silent = preprocess.render_part(
        small_media["tall_silent"], preprocess.RenderPartSpec(0, 0, 24, 30), SMALL, encoding,
        tmp_path,
    )  # fmt: skip
    loud = preprocess.render_part(
        small_media["wide"], preprocess.RenderPartSpec(1, 15, 45, 30), SMALL, encoding, tmp_path
    )
    draft = preprocess.concat_parts(
        [silent, loud], tmp_path / "draft.mp4", SMALL, encoding, expected_frames=60
    )
    assert (draft.frame_count, draft.frame_duration) == (60, F(1, 30))
    assert abs(draft.duration - 2) <= F(1, 60)
    streams = _streams(draft.path)
    assert streams["audio"]["codec_name"] == "aac" and int(streams["audio"]["sample_rate"]) == 48000
    samples = _pcm(draft.path)
    loud_from = next(
        index // 2
        for index in range(0, len(samples), 2)
        if abs(int.from_bytes(samples[index : index + 2], "little", signed=True)) > 1000
    )
    assert abs(loud_from - 48000) <= 1600  # 소리는 둘째 조각(1초)에서 시작한다 — 한 프레임 안
    assert sorted(p.name for p in tmp_path.iterdir()) == ["draft.mp4", "part00000.mov",
                                                           "part00001.mov"]  # fmt: skip


def test_a_wrong_frame_count_is_refused_and_nothing_is_published(
    small_media: dict[str, Path], tmp_path: Path
) -> None:
    encoding = _encoding()
    part = preprocess.render_part(
        small_media["wide"], preprocess.RenderPartSpec(0, 0, 30, 30), SMALL, encoding, tmp_path
    )
    with pytest.raises(preprocess.RenderVerificationError, match="31"):
        preprocess.concat_parts([part], tmp_path / "draft.mp4", SMALL, encoding, expected_frames=31)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["part00000.mov"]
    with pytest.raises(preprocess.RawStagingError, match="덮어쓰지"):
        preprocess.concat_parts([part], part, SMALL, encoding, expected_frames=30)


def _part(
    index: int, start: int, end: int, output: int, *, fps_convert: bool = False
) -> render_plan.RenderPart:
    fd = F(1, 30)
    return render_plan.RenderPart(
        index=index, source_id=f"s{index}", start_frame=start, end_frame=end,
        start=start * fd, end=end * fd, output_start_frame=0, output_frames=output,
        output_start=F(0), output_end=output * fd, reason="green",
        scale=False, pad=False, fps_convert=fps_convert,
    )  # fmt: skip


def test_long_one_to_one_parts_split_into_even_pieces_and_the_rest_stay_whole() -> None:
    parts = [
        _part(0, 100, 400, 300),  # 10초 · 1:1 → 4초 이하 셋(100장씩)
        _part(1, 0, 48, 60, fps_convert=True),  # 레이트 변환 — 통째로
        _part(2, 10, 131, 120),  # D71 누적 맞춤이 한 장 줄였다 — 통째로
        _part(3, 0, 90, 90),  # 3초 — 한도 안
    ]
    pieces = render_export.render_pieces(parts, F(1, 30), F(4))
    assert [(p.part_index, p.spec) for p in pieces] == [
        (0, preprocess.RenderPartSpec(0, 100, 200, 100)),
        (0, preprocess.RenderPartSpec(1, 200, 300, 100)),
        (0, preprocess.RenderPartSpec(2, 300, 400, 100)),
        (1, preprocess.RenderPartSpec(3, 0, 48, 60)),
        (2, preprocess.RenderPartSpec(4, 10, 131, 120)),
        (3, preprocess.RenderPartSpec(5, 0, 90, 90)),
    ]
    uneven = render_export.render_pieces([_part(0, 0, 301, 301)], F(1, 30), F(4))
    assert [p.spec.output_frames for p in uneven] == [100, 100, 101]  # 고르게 · 합은 그대로
    whole = render_export.render_pieces(parts, F(1, 30), F(0))  # 0이면 예전 그대로
    assert [p.spec for p in whole] == [
        preprocess.RenderPartSpec(i, p.start_frame, p.end_frame, p.output_frames)
        for i, p in enumerate(parts)
    ]


def test_a_part_rendered_in_pieces_joins_without_losing_frames_or_samples(
    small_media: dict[str, Path], tmp_path: Path
) -> None:
    encoding = _encoding()
    whole_dir, split_dir = tmp_path / "whole", tmp_path / "split"
    whole_dir.mkdir(), split_dir.mkdir()
    source = small_media["wide"]
    whole = preprocess.render_part(
        source, preprocess.RenderPartSpec(0, 0, 60, 60), SMALL, encoding, whole_dir
    )
    halves = [
        preprocess.render_part(
            source,
            preprocess.RenderPartSpec(k, 30 * k, 30 * k + 30, 30),
            SMALL,
            encoding,
            split_dir,
        )  # fmt: skip
        for k in range(2)
    ]
    one = array.array("h", _pcm(whole))
    two = array.array("h", _pcm(halves[0]) + _pcm(halves[1]))
    assert len(one) == len(two) == 2 * 48000  # 이음매에서 샘플이 빠지거나 겹치지 않는다
    assert max(abs(a - b) for a, b in zip(one, two, strict=True)) <= 16  # -66 dBFS 아래
    joined = preprocess.concat_parts(
        halves, split_dir / "draft.mp4", SMALL, encoding, expected_frames=60
    )
    assert (joined.frame_count, joined.duration) == (60, 2)


def test_a_map_clock_is_never_rendered(small_media: dict[str, Path], tmp_path: Path) -> None:
    source = small_media["wide"]
    probe = preprocess.probe_raw_media(source)
    clock = dataclasses.replace(
        preprocess.probe_extraction_clock(source, probe=probe), conform_kind="map"
    )
    with pytest.raises(preprocess.RawMediaUnsupported, match="map"):
        preprocess.render_part(
            source, preprocess.RenderPartSpec(0, 0, 30, 30), SMALL, _encoding(), tmp_path,
            probe=probe, clock=clock,
        )  # fmt: skip
    assert list(tmp_path.iterdir()) == []


# ── 내보내기(작업공간) ─────────────────────────────────────────────────────

UTTERANCES = {
    "A_uhd.mp4": [{"start": 0.1, "end": 1.9, "text": "안녕하세요. 여기는 제주입니다."}],
    "B_hd.mp4": [
        {"start": 0.2, "end": 1.4, "text": "바다가 보여요."},
        {"start": 1.5, "end": 2.6, "text": "정말 예뻐요..."},
        {"start": 8.0, "end": 8.9, "text": "다음으로 가요."},
    ],
    "C_tall.mp4": [{"start": 0.1, "end": 1.9, "text": "세로로 찍었어요."}],
}
"""B의 2.6→8.0초가 5초 넘는 자막 공백이라 빨강(제외 제안 · 미검토)이 된다."""


@dataclass(frozen=True)
class Folder:
    service: RawWorkspaceService
    folder: Path
    root: Path
    originals: dict[str, tuple[str, int]]


def _identity(path: Path) -> tuple[str, int]:
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mtime_ns


def _tree(folder: Path) -> dict[str, tuple[int, int]]:
    """폴더의 파일 전부(크기·mtime) — 작업공간 `export/`와 DB 파일만 뺀다."""
    rows: dict[str, tuple[int, int]] = {}
    for path in folder.rglob("*"):
        relative = path.relative_to(folder).as_posix()
        if not path.is_file() or relative.startswith("_vlogsrt/export/"):
            continue
        if ".sqlite3" in path.name:
            continue
        facts = path.stat()
        rows[relative] = (facts.st_size, facts.st_mtime_ns)
    return rows


@pytest.fixture(scope="module")
def trip(tmp_path_factory: pytest.TempPathFactory) -> Folder:
    """불러오기 → 재생 분석 → 추천 최소 길이 1초(2초 원본도 추천) → 자동 정리."""
    folder = tmp_path_factory.mktemp("render_trip") / "제주 여행"
    _encode(folder / "A_uhd.mp4", "3840x2160", 30, 2, created="2026-09-01T10:05:00Z")
    _encode(folder / "B_hd.mp4", "1920x1080", 30, 9, created="2026-09-01T10:00:00Z")
    _encode(folder / "C_tall.mp4", "1080x1920", 24, 2, created="2026-09-01T09:55:00Z")
    originals = {name: _identity(folder / name) for name in UTTERANCES}
    return _trip(folder, originals)


def _trip(folder: Path, originals: dict[str, tuple[str, int]]) -> Folder:
    service = RawWorkspaceService()
    status, handle = service.start_folder_import(folder)
    root = Path(status.workspace_root)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    for job in service.recent_jobs(root, [ANALYSIS_PROXY_JOB_KIND], None):
        assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    assets = service.list_source_assets(root)
    _write_replay_fixtures(root, {a.content_sha256: UTTERANCES[a.source_name] for a in assets})
    luna = root / "analysis/replay/luna-fixtures.json"
    fixtures = json.loads(luna.read_text(encoding="utf-8"))
    fixtures["windows"]["window_000000"].update(
        suitability="usable", suitability_reason="선명하다", scene_tags=["dialogue"]
    )
    luna.write_text(json.dumps(fixtures, ensure_ascii=False), encoding="utf-8")
    analysis = service.start_analysis(root, mode="replay")
    assert _wait_terminal(service, root, analysis.job_id).status == "succeeded"
    service.set_tier_settings(root, {"recommended_min_seconds": 1.0})
    cleanup = service.start_cleanup(root)
    assert _wait_terminal(service, root, cleanup.job_id).status == "succeeded"
    return Folder(service, folder, root, originals)


@dataclass(frozen=True)
class Exported:
    result: render_export.ExportResult
    report: dict[str, Any]
    before: dict[str, tuple[int, int]]
    after: dict[str, tuple[int, int]]


@pytest.fixture(scope="module")
def exported(trip: Folder) -> Exported:
    before = _tree(trip.folder)
    result = render_export.export_draft(trip.service, trip.root, now=lambda: MOMENT)
    report = json.loads(result.report.read_text(encoding="utf-8"))
    return Exported(result, report, before, _tree(trip.folder))


def _part_of(plan: render_plan.RenderPlan, name: str, report: dict[str, Any]) -> Any:
    ids = {row["name"]: row["source_id"] for row in report["sources"]}
    return [part for part in plan.parts if part.source_id == ids[name]]


def test_the_draft_is_1080p30_h264_with_aac_stereo_and_exact_frames(exported: Exported) -> None:
    result, plan = exported.result, exported.result.plan
    assert result.folder == result.video.parent
    assert result.folder.parent.name == "export" and result.folder.name == "20261028T010203Z"
    assert sorted(p.name for p in result.folder.iterdir()) == [
        "draft.mp4", "draft.srt", "render_report.json",
    ]  # fmt: skip
    streams = _streams(result.video)
    video, audio = streams["video"], streams["audio"]
    assert (video["codec_name"], video["width"], video["height"]) == ("h264", 1920, 1080)
    assert video["r_frame_rate"] == "30/1"
    assert (audio["codec_name"], int(audio["sample_rate"]), audio["channels"]) == ("aac", 48000, 2)
    assert int(video["nb_read_frames"]) == plan.total_frames
    assert plan.total_frames == sum(part.output_frames for part in plan.parts)
    assert abs(streams["duration"] - plan.total_seconds) <= F(1, 60)
    assert exported.report["verified"]["frames"] == plan.total_frames


def test_capture_order_portrait_pillarbox_and_24fps_conversion(exported: Exported) -> None:
    plan, report = exported.result.plan, exported.report
    assert [row["name"] for row in report["sources"]] == ["C_tall.mp4", "B_hd.mp4", "A_uhd.mp4"]
    assert plan.sequence == render_plan.SequenceFormat("landscape", 1920, 1080, F(1, 30))
    (tall,) = _part_of(plan, "C_tall.mp4", report)
    assert (tall.pad, tall.fps_convert) == (True, True)
    assert (tall.end_frame - tall.start_frame, tall.output_frames) == (48, 60)
    (uhd,) = _part_of(plan, "A_uhd.mp4", report)
    assert (uhd.scale, uhd.pad, uhd.fps_convert) == (True, False, False)
    middle = (tall.output_start + tall.output_end) / 2
    _assert_pillarboxed(_gray_frame(exported.result.video, middle, (192, 108)), 192, 108, 40)


def test_the_srt_is_the_plans_projection_with_polished_text(exported: Exported) -> None:
    plan, srt = exported.result.plan, exported.result.srt
    raw = srt.read_bytes()
    assert raw == render_plan.srt_text(plan).encode("utf-8")
    assert not raw.startswith(b"\xef\xbb\xbf") and b"\r\n" not in raw
    parsed = timeline_srt.read(srt)
    counts = plan.cue_counts
    assert len(parsed.cues) == counts.kept + counts.split == len(plan.cues) > 0
    for line, cue in zip(parsed.cues, plan.cues, strict=True):
        assert milliseconds_half_up(line.start) == milliseconds_half_up(cue.start)
        assert milliseconds_half_up(line.end) == milliseconds_half_up(cue.end)
        assert line.text == cue.text and line.text
        assert 0 <= cue.start <= cue.end <= plan.total_seconds
    texts = [cue.text for cue in plan.cues]
    assert "정말 예뻐요…" in texts and not any(text.endswith(".") for text in texts)


def test_unreviewed_red_is_left_out_without_blocking_and_reported(exported: Exported) -> None:
    report = exported.report
    assert report["unreviewed_red_dropped"] > 0
    red = [row for row in report["dropped"] if row["reason"] == render_plan.DROP_RED_UNREVIEWED]
    assert red and all(row["review_state"] == render_plan.REVIEW_AUTO_EXCLUDED for row in red)
    assert len(_part_of(exported.result.plan, "B_hd.mp4", report)) == 2  # 빨강 앞뒤 두 조각
    assert report["encoder"] == exported.result.encoder
    assert report["ffmpeg_version"].startswith("ffmpeg version")
    assert set(report["timings"]) == {
        "gather_seconds", "render_seconds", "concat_seconds", "total_seconds",
    }  # fmt: skip
    assert report["outputs"]["folder"] == str(exported.result.folder)


def test_originals_are_untouched_and_only_export_is_written(
    trip: Folder, exported: Exported
) -> None:
    assert {name: _identity(trip.folder / name) for name in UTTERANCES} == trip.originals
    assert exported.after == exported.before  # 원본 폴더·작업공간 모두 export/ 밖은 그대로다
    media = trip.service.resolve_media(trip.root, exported.result.media_file_id)
    assert media.path == exported.result.video.resolve()


def test_a_second_export_is_a_new_folder_and_latest_reads_it(
    trip: Folder, exported: Exported
) -> None:
    first = {p.name: _identity(p) for p in exported.result.folder.iterdir()}
    second = render_export.export_draft(trip.service, trip.root, now=lambda: MOMENT)
    assert second.folder.name == "20261028T010203Z_2"
    assert {p.name: _identity(p) for p in exported.result.folder.iterdir()} == first
    latest = trip.service.latest_render_export(trip.root)
    assert latest is not None and latest["folder"] == str(second.folder)
    assert latest["media_file_id"] == second.media_file_id
    assert latest["unreviewed_red_dropped"] == exported.report["unreviewed_red_dropped"]
    assert latest["parts"] == len(second.plan.parts)


def test_an_export_in_short_pieces_keeps_the_plan_frames_and_srt(
    trip: Folder, exported: Exported
) -> None:
    short = dataclasses.replace(RENDER_SETTINGS, piece_max_seconds=F(1))
    later = datetime(2026, 10, 28, 4, 0, 0, tzinfo=UTC)
    result = render_export.export_draft(trip.service, trip.root, now=lambda: later, settings=short)
    report = json.loads(result.report.read_text(encoding="utf-8"))
    assert report["parts"] == exported.report["parts"]  # 계획은 그대로 — 나뉜 것은 렌더 토막뿐
    assert result.srt.read_bytes() == exported.result.srt.read_bytes()
    assert report["parallel"]["pieces"] > len(result.plan.parts)
    assert report["parallel"]["piece_max_seconds"] == {"num": 1, "den": 1}
    assert exported.report["parallel"]["pieces"] == len(exported.result.plan.parts)
    assert int(_streams(result.video)["video"]["nb_read_frames"]) == result.plan.total_frames
    assert not list(result.folder.glob("part*"))


def test_srt_again_writes_a_new_srt_beside_the_latest_draft_and_touches_nothing_else(
    trip: Folder, exported: Exported
) -> None:
    """「SRT만 다시 내보내기」(D115) — 보고서 조각 + 지금 자막, 새 이름으로만, 영상은 그대로."""
    latest = trip.service.latest_render_export(trip.root)
    assert latest is not None and not latest["srt_again"]
    folder = Path(latest["folder"])
    before = {p.name: _identity(p) for p in folder.iterdir()}
    later = datetime(2026, 10, 28, 5, 0, 0, tzinfo=UTC)
    first = render_export.export_srt_again(trip.service, trip.root, now=lambda: later)
    second = render_export.export_srt_again(trip.service, trip.root, now=lambda: later)
    assert first.folder == folder
    assert (first.srt.name, second.srt.name) == (
        "draft-20261028T050000Z.srt", "draft-20261028T050000Z_2.srt",
    )  # fmt: skip
    assert (
        first.srt.read_bytes() == (folder / "draft.srt").read_bytes()
    )  # 자막이 같으면 같은 바이트
    assert first.cues == len(timeline_srt.read(first.srt).cues) > 0
    assert {p.name: _identity(p) for p in folder.iterdir() if p.name in before} == before
    again = trip.service.latest_render_export(trip.root)
    assert again is not None and again["srt"] == str(second.srt) and again["srt_again"]
    assert render_export.newest_srt(exported.result.folder) == exported.result.srt


def _cancel_once_a_part_exists(root: Path) -> Callable[[], None]:
    def check() -> None:
        if any((root / "export").glob("*/_staging/part*.mov")):
            raise JobCancelledError("render")

    return check


def test_cancelling_mid_render_leaves_no_folder_or_staging(trip: Folder) -> None:
    before = sorted(p.name for p in (trip.root / "export").iterdir())
    later = datetime(2026, 10, 28, 5, 0, 0, tzinfo=UTC)
    with (
        cancellation_scope(_cancel_once_a_part_exists(trip.root)),
        pytest.raises(JobCancelledError),
    ):
        render_export.export_draft(trip.service, trip.root, now=lambda: later)
    assert sorted(p.name for p in (trip.root / "export").iterdir()) == before
    assert not list((trip.root / "export").rglob("_staging"))


def test_a_map_source_is_left_out_whole_and_reported(
    trip: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = preprocess.probe_extraction_clock

    def mapped(path: Path, **kwargs: Any) -> preprocess.RawVideoClock:
        clock = real(path, **kwargs)
        return (
            dataclasses.replace(clock, conform_kind="map") if path.name == "C_tall.mp4" else clock
        )

    monkeypatch.setattr(preprocess, "probe_extraction_clock", mapped)
    later = datetime(2026, 10, 28, 6, 0, 0, tzinfo=UTC)
    result = render_export.export_draft(trip.service, trip.root, now=lambda: later)
    report = json.loads(result.report.read_text(encoding="utf-8"))
    assert [row["name"] for row in report["unsupported"]] == ["C_tall.mp4"]
    tall = [row for row in report["dropped"] if row["reason"] == render_plan.DROP_UNSUPPORTED_GRID]
    assert len(tall) == 1 and tall[0]["start"] == {"num": 0, "den": 1}
    assert _part_of(result.plan, "C_tall.mp4", report) == []
    assert int(_streams(result.video)["video"]["nb_read_frames"]) == result.plan.total_frames


def test_a_source_the_renderer_refuses_is_left_out_and_named_not_blocking(
    trip: Folder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D113: 렌더 판정이 거부한 원본 하나가 초안 전체를 막지 않는다 — 빼고 보고서·요약에
    이름과 까닭을 남긴다."""
    real = preprocess.render_part_failures

    def refused(
        probe: preprocess.RawMediaProbe, clock: preprocess.RawVideoClock
    ) -> tuple[str, ...]:
        if probe.source.name == "C_tall.mp4":
            return ("패킷 증거가 선언 창을 넘는다",)
        return real(probe, clock)

    monkeypatch.setattr(preprocess, "render_part_failures", refused)
    later = datetime(2026, 10, 28, 6, 30, 0, tzinfo=UTC)
    result = render_export.export_draft(trip.service, trip.root, now=lambda: later)
    report = json.loads(result.report.read_text(encoding="utf-8"))
    assert [(row["name"], row["reason"]) for row in report["unsupported"]] == [
        ("C_tall.mp4", render_plan.DROP_UNRENDERABLE)
    ]
    assert "패킷 증거가 선언 창을 넘는다" in report["unsupported"][0]["detail"]
    assert _part_of(result.plan, "C_tall.mp4", report) == []
    assert int(_streams(result.video)["video"]["nb_read_frames"]) == result.plan.total_frames
    summary = trip.service.latest_render_export(trip.root)
    assert summary is not None and summary["folder"] == str(result.folder)
    assert summary["unsupported"] == []
    assert summary["unrenderable"] == ["C_tall.mp4 — 렌더할 수 없다 — 패킷 증거가 선언 창을 넘는다"]


def test_nothing_to_keep_is_refused_before_any_folder(trip: Folder) -> None:
    before = sorted(p.name for p in (trip.root / "export").iterdir())
    strict = dataclasses.replace(RENDER_SETTINGS, min_part_seconds=F(100))
    with pytest.raises(render_export.RenderExportError, match="남길 추천 구간이 없어"):
        render_export.export_draft(trip.service, trip.root, settings=strict)
    assert sorted(p.name for p in (trip.root / "export").iterdir()) == before


def test_the_service_job_renders_and_the_activity_label_names_it(trip: Folder) -> None:
    from ui import workspace_service

    handle = trip.service.start_render_export(trip.root)
    record = _wait_terminal(trip.service, trip.root, handle.job_id)
    assert record.status == "succeeded", record.error
    assert record.kind == "render_export"
    latest = trip.service.latest_render_export(trip.root)
    assert latest is not None and Path(latest["video"]).is_file()
    assert workspace_service.ACTIVITY_JOB_LABELS["render_export"] == "추천만 이어붙이기"
    assert "render_export" in workspace_service.CLOSE_CANCELS_JOB_KINDS


@pytest.fixture(autouse=True)
def _fresh_encoder_probes() -> Iterator[None]:
    """인코더 확인 캐시를 시험마다 비운다 — 가짜 목록으로 바꾼 시험이 다음 시험에 새지 않게."""
    _ENCODER_WORKS.cache_clear()
    yield
    _ENCODER_WORKS.cache_clear()
