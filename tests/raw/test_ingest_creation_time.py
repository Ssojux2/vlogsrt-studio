"""A2 남은 몫 — 조사가 촬영 시각을 **유효할 때만** 묶는다(계획 §6.2·코디네이터 재결).

진짜 ffprobe 조사자(`ffprobe_media_inspector`)를 합성 미디어로 부린다:

- 시간대 있는 상식 bound 안의 creation_time → `valid`, 값이 묶이고 정렬 근거가 된다.
- 시간대 없음·말도 안 되는 날짜 → `invalid` — 값을 버린 채 '있는데 못 믿는다'로 남는다.
- 선언 자체가 없음 → `uninspected`.
- 파일 수정 시각(mtime)은 어떤 경우에도 촬영 시각으로 대체되지 않는다.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import ingest

APPLE_DATE = "com.apple.quicktime.creationdate"

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


def _encode(path: Path, creation_time: str | None, apple_date: str | None = None) -> Path:
    args = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=1",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
    ]  # fmt: skip
    if apple_date is not None:
        # 애플 키는 mdta 키 목록에만 들어간다 — FCP·아이폰이 쓰는 자리와 같다.
        args += ["-movflags", "use_metadata_tags", "-metadata", f"{APPLE_DATE}={apple_date}"]
    if creation_time is not None:
        args += ["-metadata", f"creation_time={creation_time}"]
    args.append(str(path))
    completed = subprocess.run(args, capture_output=True, text=True, check=False)
    assert completed.returncode == 0, completed.stderr[-300:]
    return path


@pytest.fixture
def media(tmp_path: Path) -> Path:
    directory = tmp_path / "media"
    directory.mkdir()
    return directory


@needs_ffmpeg
def test_valid_creation_time_is_bound_and_used_for_order(media: Path) -> None:
    first = _encode(media / "a.MOV", "2026-09-01T10:00:00Z")
    inspection = ingest.ffprobe_media_inspector(first)
    assert inspection.capture_time_status == "valid"
    assert inspection.capture_time is not None
    assert inspection.capture_time.startswith("2026-09-01T10:00:00")
    assert inspection.width == 320 and inspection.height == 180
    assert inspection.audio_streams == 0
    assert inspection.duration_seconds is not None and inspection.duration_seconds > 0


@needs_ffmpeg
def test_out_of_bounds_creation_time_is_invalid(media: Path) -> None:
    """상식 bound를 벗어난 시각은 invalid — 값은 버리고 상태로 남는다.

    참고: ffmpeg은 시간대 없는 creation_time을 만나 로컬 시간대로 정규화해
    저장하므로, '시간대 없는 값'은 컨테이너까지 살아남지 못한다 — 그 판정은
    preprocess의 `_parse_creation_time` 시험이 직접 담당한다.
    """
    future = _encode(media / "b.MOV", "2037-01-01T00:00:00Z")
    inspection = ingest.ffprobe_media_inspector(future)
    assert inspection.capture_time_status == "invalid"
    assert inspection.capture_time is None


@needs_ffmpeg
def test_absurd_creation_time_is_invalid(media: Path) -> None:
    absurd = _encode(media / "c.MOV", "1971-01-01T00:00:00Z")
    inspection = ingest.ffprobe_media_inspector(absurd)
    assert inspection.capture_time_status == "invalid"
    assert inspection.capture_time is None


@needs_ffmpeg
def test_missing_creation_time_is_uninspected(media: Path) -> None:
    bare = _encode(media / "d.MOV", None)
    inspection = ingest.ffprobe_media_inspector(bare)
    assert inspection.capture_time_status == "uninspected"
    assert inspection.capture_time is None


@needs_ffmpeg
def test_mtime_is_never_used_as_capture_time(media: Path, tmp_path: Path) -> None:
    """mtime을 옛날로 물려도 촬영 시각은 미조사 그대로 — 대체 금지(§6.2)."""
    stale = _encode(media / "e.MOV", None)
    old = stale.stat().st_mtime - 90 * 24 * 3600
    os.utime(stale, (old, old))
    inspection = ingest.ffprobe_media_inspector(stale)
    assert inspection.capture_time is None
    assert inspection.capture_time_status == "uninspected"

    card = tmp_path / "card"
    card.mkdir()
    (tmp_path / "ws").mkdir()
    shutil.copyfile(stale, card / "e.MOV")
    proposal = ingest.scan_sources(card, tmp_path / "ws")
    assert proposal.order[0].inspection.capture_time is None


@needs_ffmpeg
def test_order_follows_valid_capture_times(tmp_path: Path, media: Path) -> None:
    """유효한 촬영 시각은 정렬 근거가 된다 — 파일 번호와 어긋나도 시각이 이긴다(§6.2)."""
    card = tmp_path / "card"
    card.mkdir()
    (tmp_path / "ws").mkdir()
    _encode(card / "DJI_0002.MOV", "2026-09-02T09:00:00+09:00")
    _encode(card / "DJI_0001.MOV", "2026-09-01T09:00:00+09:00")
    proposal = ingest.scan_sources(card, tmp_path / "ws")
    ordered = [candidate.source_name for candidate in proposal.order]
    assert ordered == ["DJI_0001.MOV", "DJI_0002.MOV"]
    notes = [candidate.inspection.capture_time_status for candidate in proposal.order]
    assert notes == ["valid", "valid"]


@needs_ffmpeg
def test_apple_creation_date_is_read_from_a_real_container(media: Path) -> None:
    """실제 MOV의 애플 촬영 날짜(`+0900`)가 조사·되채움 양쪽에서 읽힌다(D53).

    ffmpeg은 `use_metadata_tags`일 때 `creation_time`을 mvhd와 키 목록에 겹쳐 적어
    `A;B`로 읽힌다(못 믿는 값) — 애플 키가 앞서므로 그 겹침은 촬영 시각을 가리지 못한다.
    둘 다 유효할 때의 우선순위는 `test_native_media_probe`가 직접 잰다.
    """
    shot = _encode(
        media / "IMG_0001.MOV", "2026-08-14T10:38:34Z", apple_date="2026-08-11T12:50:46+0900"
    )
    inspection = ingest.ffprobe_media_inspector(shot)
    assert inspection.capture_time_status == "valid"
    assert inspection.capture_time == "2026-08-11T03:50:46+00:00"
    verdict = preprocess.probe_creation_time(shot)
    assert verdict.valid and verdict.utc is not None
    assert verdict.utc.isoformat() == "2026-08-11T03:50:46+00:00"


@needs_ffmpeg
def test_container_creation_time_still_works_without_apple_date(media: Path) -> None:
    """애플 키가 없는 카메라는 예전 그대로 컨테이너 `creation_time`이 근거다."""
    plain = _encode(media / "DJI_0001.MOV", "2026-09-01T10:00:00Z")
    verdict = preprocess.probe_creation_time(plain)
    assert verdict.valid and verdict.utc is not None
    assert verdict.utc.isoformat() == "2026-09-01T10:00:00+00:00"
    assert preprocess.probe_creation_time(_encode(media / "bare.MOV", None)).valid is False


@needs_ffmpeg
def test_scan_order_follows_apple_shot_dates(tmp_path: Path) -> None:
    """파일 번호·컨테이너 시각이 거꾸로여도 애플 촬영 날짜가 순서를 정한다(D53)."""
    card = tmp_path / "card"
    card.mkdir()
    (tmp_path / "ws").mkdir()
    _encode(card / "A_0001.MOV", None, apple_date="2026-08-11T15:00:00+0900")
    _encode(card / "A_0002.MOV", None, apple_date="2026-08-11T09:00:00+0900")
    proposal = ingest.scan_sources(card, tmp_path / "ws")
    assert [candidate.source_name for candidate in proposal.order] == ["A_0002.MOV", "A_0001.MOV"]
