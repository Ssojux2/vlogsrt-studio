"""V1 영상 우선 검토 백엔드의 계약 — 읽기 질의·미리보기 준비·리비전 묶음 편집.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §4.3·§10·§12·§14.2. 합성 미디어
(ffmpeg lavfi — 발화·무성·소리 두 트랙·세로 회전·0이 아닌 원점)로 온전한 검토
리비전을 만들어, (1) 질의가 미디어를 만들지 않고(ffmpeg 금지 몽키패치 증명)
(2) 준비가 한 페이지의 없는 항목만 순서대로 채우며 다시 만들지 않고
(3) 침묵·소리 여러 트랙 선택·세로 회전·0이 아닌 원점의 측정 대응을 지키며
(4) 편집이 stale·길이 0·안 닿은 이웃을 거부하며 (5) 원본·package가 하나도
바뀌지 않고 (6) 정리(purge) 뒤 같은 등록 ID로 회복하는지를 증명한다.
유료 호출은 0회다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any, cast

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw import visual_review
from vlogsrt.project.raw.models import MediaNotFoundError, PlannedInterval
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade
from vlogsrt.project.raw.segment_review import (
    SELECTION_EXCLUDED,
    SourceClock,
    StaleRevisionError,
    make_source_clock,
)
from vlogsrt.project.raw.visual_review import (
    VISUAL_PAGE_SIZE,
    VisualReviewError,
    middle_frame_index,
    snapped_frame_index,
)
from vlogsrt.project.raw.workspace import RawWorkspace

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAIT_SECONDS = 300.0
ONE_SECOND_TILE = Fraction(1)


def _encode(args: list[str]) -> None:
    completed = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, f"합성 미디어 생성 실패: {completed.stderr[-400:]}"


def _voiced_media(path: Path, seconds: str) -> None:
    """소리 한 트랙 있는 MP4 — 발화 원본의 자리."""
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=black:s=64x32:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000",
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
    """오디오 트랙이 아예 없는 MP4 — 침묵은 무발화와 다른 합법 상태다."""
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


def _dual_audio_media(path: Path, seconds: str) -> None:
    """소리 두 트랙 — 미리보기는 고른 하나만 담는다(브라우저는 트랙을 못 고른다)."""
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            f"color=black:s=64x32:r=10:d={seconds}",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:r=8000",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "2:a",
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


def _portrait_rotated_media(path: Path) -> None:
    """세로(32x64)에 90° 회전 선언 — 재생 화면의 방향·비율이 보존되어야 한다."""
    plain = path.with_name(path.stem + "-plain.mp4")
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "color=black:s=32x64:r=10:d=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(plain),
        ]
    )
    _encode(["-display_rotation", "90", "-i", str(plain), "-c:v", "copy", str(path)])
    plain.unlink()


def _offset_media(path: Path) -> None:
    """0이 아닌 제시 원점(2.5초) — 미리보기의 시각 대응은 잰 값이어야 한다."""
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x32:r=10:d=2",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            "-output_ts_offset",
            "2.5",
            str(path),
        ]
    )


def _dual_tone_media(path: Path) -> None:
    """소리 두 트랙 — 440Hz@8k 첫 트랙, 880Hz@16k+250ms 지연 둘째 트랙.

    공개 선택이 어느 트랙을 담았는지 샘플레이트(8000/16000)로 증명한다 — 주파수는
    ffprobe이 안 보지만 샘플레이트는 그대로 남는다. 둘째 트랙의 시작 지연은
    트랙마다 시각 구조가 다르다는 재료다(A2 하위 기본 요소와 같은 값).
    """
    _encode(
        [
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x32:r=10:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:r=16000",
            "-filter_complex",
            "[2:a]adelay=250|250[second]",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "[second]",
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


def _leading_audio_media(path: Path) -> None:
    """선행 소리 원본 — 소리가 영상보다 0.5초 먼저 시작한다(G3 실측의 재현).

    `-itsoffset`으로 입력별 제시 시각을 옮긴다: 영상 +2초, 소리 +1.5초. 원본 시계의
    원점(영상 첫 PTS)은 2초이고 소리는 그보다 앞선다. 미리보기에서 영상이 브라우저
    시간표(이른 소리에서 시작)보다 늦게 나타나는 것이 이 검사의 재료다.
    """
    _encode(
        [
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x32:r=10:d=3",
            "-itsoffset",
            "1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000",
            "-map",
            "0:v",
            "-map",
            "1:a",
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


def _dual_leading_media(path: Path) -> None:
    """선행 소리 두 트랙 — 소리0가 제일 이르고(1.5초) 영상(2초)·소리1(2.25초)이 뒤따른다.

    G3 조합 재현의 재료다: 어떤 소리를 골라도(트랙 0/1/침묵) 미리보기의 출력
    영상 첫 PTS는 같다 — ffmpeg이 입력의 선언 시작(1.372초)만큼 전체를 당기는
    컨테이너 사실이지 소리 선택이 아니다. 소리 원점 뺄셈은 늦은 트랙·침묵에서
    0을 거짓으로 내놓았다(A3 실측: 세 선택 모두 6673/12800, 디코드 mse 0).
    프레임마다 화면이 다른 testsrc2라 화소 대응 오라클도 쓸 수 있다.
    """
    _encode(
        [
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=128x64:rate=25:duration=3",
            "-itsoffset",
            "1.5",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000:duration=3.5",
            "-filter_complex",
            "[2:a]adelay=750|750[second]",
            "-itsoffset",
            "2.25",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=880:r=16000:duration=3",
            "-map",
            "0:v",
            "-map",
            "1:a",
            "-map",
            "[second]",
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


def _shifted_media(path: Path) -> None:
    """모든 스트림이 2초부터 시작하는 발화 원본 — 선언 시작이 0이 아닌(E1) 재료.

    시각 편집 → 재계획 → 재추출이 이 원본 위에서 끝까지 흐르는지가 회귀의
    골자다(정착 기준선에서는 지원 문에서 막혔다).
    """
    _encode(
        [
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "color=black:s=64x32:r=25:d=3",
            "-itsoffset",
            "2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:r=8000:duration=3",
            "-map",
            "0:v",
            "-map",
            "1:a",
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


def _frame_gray(path: Path, frame_index: int) -> bytes:
    """정확히 한 프레임을 회색조 원시 화소로 디코드 — 화소 대응 오라클의 밑천.

    `-ss` 시각 탐색을 쓰지 않는다(`extract_frame_index`와 같은 이유): 디코드
    출력의 프레임 번호로 고른다. 스트림 복사 미리보기는 원본 비트스트림 그대로라
    같은 번호의 화소가 바이트까지 같아야 한다.
    """
    completed = subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-nostdin",
            "-i",
            str(path),
            "-vf",
            f"select=eq(n\\,{frame_index})",
            "-frames:v",
            "1",
            "-pix_fmt",
            "gray",
            "-f",
            "rawvideo",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr[-300:]
    return completed.stdout


def _wait_terminal(service: raw_service.RawWorkspaceService, root: Path, job_id: str):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    return service.get_job(root, job_id)


def _ingest_card(service: raw_service.RawWorkspaceService, root: Path, card: Path) -> None:
    proposal = service.scan_sources(root, card)
    confirmation = service.confirm_ingest(root, proposal)
    handle = service.start_ingest_copy(root, confirmation)
    record = _wait_terminal(service, root, handle.job_id)
    assert record.status == "succeeded", record.error
    service.confirm_global_source_order(root)


def _asset_id_by_name(service: raw_service.RawWorkspaceService, root: Path, name: str) -> str:
    for asset in service.list_source_assets(root):
        if asset.source_name == name:
            return asset.asset_id
    raise AssertionError(f"원본이 없다: {name}")


def _review_ready(
    service: raw_service.RawWorkspaceService, root: Path, card: Path, tile: Fraction
) -> str:
    """카드 가져오기 → 전체 순서 → 균일 타일 계획 → 검토 초기화까지 끝낸 작업공간."""
    _ingest_card(service, root, card)
    plan = service.plan_segmentation(root, tile_seconds=tile)
    ReviewWorkspaceFacade(root).initialize_review(plan.revision_id)
    return plan.revision_id


def _prepared_page(
    service: raw_service.RawWorkspaceService,
    root: Path,
    base_plan_id: str,
    page: int,
    audio_selections: dict[str, int | None] | None = None,
    expected_revision_id: str | None = None,
):
    handle = service.start_visual_page_preparation(
        root,
        base_plan_id=base_plan_id,
        page=page,
        audio_selections=audio_selections,
        expected_revision_id=expected_revision_id,
    )
    return handle, _wait_terminal(service, root, handle.job_id)


def _tree_hashes(root: Path, relative: str) -> dict[str, str]:
    """디렉터리 아래 전체 파일의 해시 — 불변 증명의 밑천(상대 경로 → sha256)."""
    base = root / relative
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def _forbidden(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("질의는 ffmpeg·ffprobe를 부르지 않는다 — 생성 0 계약 위반")


@pytest.fixture()
def service() -> raw_service.RawWorkspaceService:
    return raw_service.RawWorkspaceService()


@pytest.fixture()
def workspace_root(service: raw_service.RawWorkspaceService, tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    service.create_workspace(root, "영상검토")
    return root


# ── 순수 계산 — 스냅은 앞장 동률, 유리수만 ────────────────────────────────────


def _clock(origin: int = 0, frame_ticks: int = 3003, time_base_den: int = 90000) -> SourceClock:
    return make_source_clock(
        source_id="src",
        source_sha256="0" * 64,
        time_base_num=1,
        time_base_den=time_base_den,
        origin_pts=origin,
        end_pts=origin + 10 * frame_ticks,
        frame_duration=Fraction(frame_ticks, time_base_den),
    )


def test_snapped_frame_tie_chooses_earlier() -> None:
    """두 프레임의 정확히 한가운데는 앞장이다 — float 없이 유리수로만 계산한다."""
    clock = _clock()
    half = clock.origin_pts + clock.frame_ticks + clock.frame_ticks // 2  # 1.5 프레임 자리
    assert snapped_frame_index(clock, half) == 1  # 동률 → 앞장
    assert snapped_frame_index(clock, clock.origin_pts + clock.frame_ticks) == 1
    assert snapped_frame_index(clock, clock.origin_pts) == 0


def test_snapped_frame_subtick_fraction_below_tie_above() -> None:
    """홀수 틱 격자(NTSC 계열)의 동률은 **반 틱 PTS**에 온다 — 서브틱 유리수로 정확히.

    틱이 홀수면 두 프레임의 한가운데는 정수 PTS가 아니다(3.5 프레임 자리가
    x.5 틱). float로 바꾸는 순간 그 값이 흔들린다 — Fraction 입력은 그대로 정확하고,
    아래·동률·위가 각각 앞장·앞장·뒷장으로 갈린다.
    """
    clock = _clock(frame_ticks=1001, time_base_den=30000)
    tie = clock.origin_pts + Fraction(7 * 1001, 2)  # 정확히 3.5 프레임 — 반 틱 PTS
    assert tie.denominator != 1  # 서브틱 점이다(정수가 아니다)
    assert snapped_frame_index(clock, tie) == 3  # 동률 → 앞장
    assert snapped_frame_index(clock, tie - 1) == 3  # 살짝 아래 → 앞장
    assert snapped_frame_index(clock, tie + 1) == 4  # 살짝 위 → 뒷장
    assert snapped_frame_index(clock, tie - Fraction(1, 1000)) == 3
    assert snapped_frame_index(clock, tie + Fraction(1, 1000)) == 4


def test_middle_frame_of_odd_frame_count_is_earlier_of_two_centers() -> None:
    """프레임 5개 조각의 가운데는 가운데 두 후보 중 앞쪽이다(동률 앞장)."""
    clock = _clock()
    start = clock.origin_pts
    end = start + 5 * clock.frame_ticks  # 프레임 0..4
    assert middle_frame_index(clock, start, end) == 2  # 가운데 2.5 프레임 → 2


def test_middle_frame_rejects_empty_range() -> None:
    clock = _clock()
    with pytest.raises(VisualReviewError, match="비었거나 뒤집"):
        middle_frame_index(clock, 3003, 3003)


# ── 질의 — 읽기만 한다(생성·제공자 호출 0) ────────────────────────────────────


@needs_ffmpeg
def test_query_never_generates_media_nor_calls_ffmpeg(
    service: raw_service.RawWorkspaceService,
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """질의는 ffmpeg·ffprobe를 전혀 부르지 않고 파일도 하나도 만들지 않는다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    overview = service.visual_review_query(workspace_root, base_plan_id=base)

    assert overview.base_plan_id == base
    assert overview.total_segments == 3
    assert overview.page_count == 1
    assert overview.revision_id.endswith("/000000")
    assert overview.stale_sources == ()
    assert overview.selected is not None
    assert overview.selected.segment_id == overview.segments[0].segment_id

    # ffmpeg/ffprobe가 전부 금지인 상태에서 질의가 살아남는다 — 생성 0의 증명.
    monkeypatch.setattr(preprocess, "require_ffmpeg", _forbidden)
    monkeypatch.setattr(preprocess, "probe_raw_media", _forbidden)
    monkeypatch.setattr(preprocess, "ffmpeg_provenance", _forbidden)
    frozen = service.visual_review_query(workspace_root, base_plan_id=base, page=1)
    assert frozen.total_segments == 3
    assert all(card.thumb_state == "missing" for card in frozen.segments)
    assert all(preview.state == "missing" for preview in frozen.sources)
    previews = workspace_root / "previews"
    assert not previews.exists() or not any(previews.rglob("*"))
    assert service.list_media(workspace_root, kind="preview") == ()  # 미리보기 등록 0건

    # 리비전 묶음·명시적 소리 선택이 붙어도 여전히 생성 0이다 — 기록 읽기뿐.
    source_id = frozen.sources[0].source_id
    chosen = service.visual_review_query(
        workspace_root,
        base_plan_id=base,
        page=1,
        expected_revision_id=frozen.revision_id,
        audio_selections={source_id: 0},
    )
    assert chosen.sources[0].state == "missing"  # 이 선택은 아직 만든 적이 없다
    assert chosen.sources[0].audio_ordinal == 0  # 선택은 값으로 보인다
    assert chosen.sources[0].audio_tracks == ()  # 첫 준비 전 — 트랙 목록은 모른다(조사 없음)


# ── 준비 — 한 페이지의 없는 항목만, 다시 만들지 않는다 ────────────────────────


@needs_ffmpeg
def test_prepare_fills_only_requested_page_and_is_idempotent(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """26조각(페이지 2개) — 1페이지 준비는 24썸네일만, 2페이지는 나머지 2개만 만든다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "긴발화.mp4", "26")  # 1초 타일 → 26조각
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)

    thumbs = workspace_root / "previews" / "visual" / "thumbs"
    handle, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    assert handle.reattached is False
    assert len(sorted(thumbs.glob("*.jpg"))) == VISUAL_PAGE_SIZE  # 2페이지는 아직 없다

    before = {path.name: path.stat().st_mtime_ns for path in sorted(thumbs.glob("*.jpg"))}
    _, again = _prepared_page(service, workspace_root, base, page=1)
    assert again.status == "succeeded", again.error
    assert {  # 이미 있는 항목은 다시 만들지 않는다 — 같은 파일, 같은 시각
        path.name: path.stat().st_mtime_ns for path in sorted(thumbs.glob("*.jpg"))
    } == before

    _, record_page2 = _prepared_page(service, workspace_root, base, page=2)
    assert record_page2.status == "succeeded", record_page2.error
    assert len(sorted(thumbs.glob("*.jpg"))) == 26

    overview = service.visual_review_query(workspace_root, base_plan_id=base, page=2)
    assert overview.page == 2
    assert len(overview.segments) == 2
    assert all(card.thumb_state == "ready" for card in overview.segments)
    assert all(card.thumb_registered_file_id for card in overview.segments)
    source = overview.sources[0]
    assert source.state == "ready"
    assert source.registered_file_id is not None
    assert source.audio_ordinal == 0  # 소리 있는 원본 — 첫 트랙을 골랐다
    assert Fraction(source.duration_seconds) > Fraction(25)


# ── 소리 선택 — 침묵·여러 트랙은 서로 다른 합법 선택이다 ───────────────────────


@needs_ffmpeg
def test_silent_and_multi_audio_sources_get_selected_audio_only(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """침묵 원본은 영상만, 소리 두 트랙 원본은 고른 하나만 미리보기에 들어간다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "2")
    _silent_media(card / "무성.mp4", "2")
    _dual_audio_media(card / "두트랙.mp4", "2")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error

    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    by_source = {preview.source_id: preview for preview in overview.sources}
    assert {preview.state for preview in by_source.values()} == {"ready"}

    audio_counts: dict[str, int] = {}
    for preview in by_source.values():
        assert preview.relative_path is not None
        probe = preprocess.probe_raw_media(workspace_root / preview.relative_path)
        audio_counts[preview.source_id] = len(probe.audio)
    silent_id = _asset_id_by_name(service, workspace_root, "무성.mp4")
    dual_id = _asset_id_by_name(service, workspace_root, "두트랙.mp4")
    voiced_id = _asset_id_by_name(service, workspace_root, "발화.mp4")
    assert audio_counts[voiced_id] == 1
    assert audio_counts[silent_id] == 0  # 침묵 — 트랙 없음이 그대로 간다
    assert audio_counts[dual_id] == 1  # 두 트랙 중 고른 하나만
    assert by_source[silent_id].audio_ordinal is None
    assert by_source[dual_id].audio_ordinal == 0
    assert by_source[dual_id].audio_stream_index == 1  # 첫 오디오의 전역 번호


# ── 화면 보존 — 세로·회전·0이 아닌 원점 ──────────────────────────────────────


@needs_ffmpeg
def test_portrait_rotation_and_aspect_preserved(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """세로 32x64 + 90° 회전 원본 — 미리보기도 같은 기하·회전 선언을 가진다."""
    card = tmp_path / "card"
    card.mkdir()
    _portrait_rotated_media(card / "세로회전.mov")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    preview = overview.sources[0]
    assert preview.state == "ready"
    assert (preview.width, preview.height) == (32, 64)  # 저장 기하 보존
    assert preview.rotation_degrees == 90  # 회전 선언 보존 — 비율·방향이 같다


@needs_ffmpeg
def test_nonzero_origin_returns_measured_browser_mapping(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """원점 2.5초·영상 홀로 원본 — 브라우저 원점 이동은 0, 재생 위치는 원본 국소 시각 그대로.

    영상이 파일의 가장 이른(유일한) 스트림이므로 브라우저 시간표의 원점 이동은
    0이다 — 원점 2.5초는 원본 시계가 가진 값이고 미리보기 매핑을 밀지 않는다.
    미리보기의 프레임 수·프레임 길이가 원본 시계와 같으면 상대 시간이 보존된
    증거다(G3와 짝: 선행 소리가 있을 때만 이동이 양수가 된다).
    """
    card = tmp_path / "card"
    card.mkdir()
    _offset_media(card / "옮겨진.mp4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    preview = overview.sources[0]
    assert preview.state == "ready"
    assert preview.audio_origin_seconds is None  # 소리 트랙이 없다
    assert preview.browser_origin_seconds is not None
    assert Fraction(preview.browser_origin_seconds) == 0  # 영상 홀로 — 원점 이동 없음
    for card_item in overview.segments:
        assert card_item.preview_seek_seconds is not None
        assert Fraction(card_item.preview_seek_seconds) == Fraction(card_item.snapped_seconds)
    assert overview.selected is not None
    clock = overview.selected.clock
    assert clock.origin_pts > 0  # 원본의 제시 원점은 실제로 0이 아니다(2.5초)
    preview_clock = preprocess.probe_extraction_clock(
        workspace_root / (preview.relative_path or "")
    )
    source_clock = preprocess.probe_extraction_clock(
        workspace_root / "originals" / "import_000001" / "옮겨진.mp4"
    )
    assert preview_clock.frame_count == source_clock.frame_count  # 프레임 수 보존
    assert preview_clock.frame_duration == source_clock.frame_duration  # 격자 보존


@needs_ffmpeg
def test_zero_origin_source_maps_seek_directly(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """원점 0 원본 — 잰 원점이 0이므로 재생 위치는 조각 시각 그대로다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "보통.mp4", "2")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    assert Fraction(overview.sources[0].browser_origin_seconds or "1") == 0
    for card_item in overview.segments:
        assert card_item.preview_seek_seconds == card_item.snapped_seconds


# ── 페이지 계약 — 선택 조각이 필요한 값을 전부 실는다 ─────────────────────────


@needs_ffmpeg
def test_selected_segment_carries_clock_intervals_and_registered_ids(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """선택 조각은 시계·간격·가운데 프레임·오른쪽 이웃·등록 ID를 한 번에 실는다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    selected = overview.selected
    assert selected is not None
    clock = selected.clock
    assert clock.time_base == Fraction(clock.time_base_num, clock.time_base_den)
    assert clock.origin_pts == selected.start_pts  # 첫 조각은 원점에서 시작
    assert clock.end_pts >= selected.end_pts
    assert clock.frame_ticks > 0
    assert selected.right_neighbor is not None
    assert selected.right_neighbor.start_pts == selected.end_pts  # 같은 원본의 맞닿은 이웃
    assert selected.right_neighbor.source_id == selected.source_id
    assert selected.thumb_registered_file_id is not None
    assert selected.preview_registered_file_id is not None
    assert selected.snapped_pts == (
        clock.origin_pts + selected.snapped_frame_index * clock.frame_ticks
    )
    last = overview.segments[-1]
    assert last.right_neighbor is None  # 원본의 마지막 조각 — 오른쪽 이웃은 없다
    with pytest.raises(VisualReviewError, match="선택 조각"):
        service.visual_review_query(workspace_root, base_plan_id=base, selected_segment_id="없는놈")
    with pytest.raises(VisualReviewError, match="페이지는 1 이상"):
        service.visual_review_query(workspace_root, base_plan_id=base, page=0)


# ── 편집 — 리비전 묶음·길이 0·안 닿은 이웃 ────────────────────────────────────


def _grid_point(base: int, offset: int, tick: int) -> int:
    """격자 위의 점 — 앵커에서 offset 프레임만큼 간 자리."""
    return base + offset * tick


@needs_ffmpeg
def test_edits_reject_stale_zero_length_and_noncontiguous_neighbors(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """stale 리비전·길이 0 분할·경계 밖 이동·건너뛴 이웃 짝은 전부 거부다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    head_id = overview.revision_id
    first, second, third, fourth = (
        overview.segments[0],
        overview.segments[1],
        overview.segments[2],
        overview.segments[3],
    )
    clock = overview.selected.clock if overview.selected else None
    assert clock is not None
    tick = clock.frame_ticks
    origin = clock.origin_pts

    with pytest.raises(StaleRevisionError, match="다르다"):
        service.visual_split_segment(
            workspace_root,
            base_plan_id=base,
            expected_revision_id="phantom/000000",
            segment_id=first.segment_id,
            split_pts=_grid_point(origin, 1, tick),
        )
    with pytest.raises(VisualReviewError, match="길이 0"):
        service.visual_split_segment(  # 조각 시작점에서 분할 → 길이 0 자식
            workspace_root,
            base_plan_id=base,
            expected_revision_id=head_id,
            segment_id=first.segment_id,
            split_pts=first.start_pts,
        )
    with pytest.raises(VisualReviewError, match="프레임 격자 밖"):
        service.visual_split_segment(
            workspace_root,
            base_plan_id=base,
            expected_revision_id=head_id,
            segment_id=first.segment_id,
            split_pts=first.start_pts + tick // 2,
        )
    with pytest.raises(VisualReviewError, match="맞닿은 조각에만"):
        service.visual_adjust_boundary(  # first와 third — 가운데 second를 건너뛴다
            workspace_root,
            base_plan_id=base,
            expected_revision_id=head_id,
            left_segment_id=first.segment_id,
            right_segment_id=third.segment_id,
            boundary_pts=_grid_point(origin, 11, tick),
        )
    with pytest.raises(VisualReviewError, match="길이 0"):
        service.visual_adjust_boundary(  # 왼쪽을 비우는 이동
            workspace_root,
            base_plan_id=base,
            expected_revision_id=head_id,
            left_segment_id=first.segment_id,
            right_segment_id=second.segment_id,
            boundary_pts=first.start_pts,
        )

    split_at = _grid_point(first.start_pts, 5, tick)  # 첫 조각(1초·10프레임)의 가운데
    edited = service.visual_split_segment(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=head_id,
        segment_id=first.segment_id,
        split_pts=split_at,
    )
    assert edited.revision_id != head_id
    with pytest.raises(StaleRevisionError):  # 옛 리비전으로 다시 편집 — stale
        service.visual_set_selection(
            workspace_root,
            base_plan_id=base,
            expected_revision_id=head_id,
            segment_id=first.segment_id,
            selection=SELECTION_EXCLUDED,
        )
    with pytest.raises(VisualReviewError, match="맞닿은 조각에만"):
        service.visual_merge_segments(  # second와 fourth — 가운데 third를 건너뛴다
            workspace_root,
            base_plan_id=base,
            expected_revision_id=edited.revision_id,
            first_segment_id=second.segment_id,
            second_segment_id=fourth.segment_id,
        )
    reselected = service.visual_set_selection(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=edited.revision_id,
        segment_id=second.segment_id,
        selection=SELECTION_EXCLUDED,
    )
    after = service.visual_review_query(workspace_root, base_plan_id=base)
    assert after.revision_id == reselected.revision_id
    assert after.total_segments == 5  # 4 + 분할 1 — 제외는 조각을 지우지 않는다(§6.4)
    excluded = [card for card in after.segments if card.selection == SELECTION_EXCLUDED]
    assert len(excluded) == 1 and excluded[0].segment_id == second.segment_id


# ── 편집 제안 — 저장보다 앞서고 아무 것도 쓰지 않는다 ─────────────────────────


@needs_ffmpeg
def test_snap_proposal_is_read_only_and_precedes_save(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """제안은 스냅·판정만 말한다 — 리비전·파일·작업이 전혀 바뀌지 않는다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    head_id = overview.revision_id
    first, second = overview.segments[0], overview.segments[1]
    clock = overview.selected.clock if overview.selected else None
    assert clock is not None
    tick = clock.frame_ticks

    jobs_before = service.list_jobs(workspace_root)
    wanted = _grid_point(first.start_pts, 5, tick) + tick // 2  # 두 프레임의 한가운데
    proposal = service.visual_snap_proposal(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=head_id,
        segment_id=first.segment_id,
        desired_pts=wanted,
    )
    assert proposal.valid is True
    assert proposal.head_revision_id == head_id
    assert proposal.snap_pts == _grid_point(first.start_pts, 5, tick)  # 동률 앞장
    assert not (workspace_root / "previews").exists() or not any(
        (workspace_root / "previews").rglob("*")
    )  # 제안은 미디어를 만들지 않는다
    assert all(job.kind != "visual_prepare" for job in jobs_before)

    edge = service.visual_snap_proposal(  # 조각 시작점 — 길이 0 판정이 미리 나온다
        workspace_root,
        base_plan_id=base,
        expected_revision_id=head_id,
        segment_id=first.segment_id,
        desired_pts=first.start_pts,
    )
    assert edge.valid is False
    assert edge.reason is not None and "길이 0" in edge.reason

    bad_boundary = service.visual_boundary_proposal(  # 건너뛴 이웃 — 저장 전에 거절 사유
        workspace_root,
        base_plan_id=base,
        expected_revision_id=head_id,
        left_segment_id=first.segment_id,
        right_segment_id=overview.segments[2].segment_id,
        desired_pts=_grid_point(first.start_pts, 11, tick),
    )
    assert bad_boundary.valid is False
    assert bad_boundary.reason is not None and "맞닿지 않" in bad_boundary.reason

    good_boundary = service.visual_boundary_proposal(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=head_id,
        left_segment_id=first.segment_id,
        right_segment_id=second.segment_id,
        desired_pts=_grid_point(first.start_pts, 11, tick),
    )
    assert good_boundary.valid is True
    assert good_boundary.snap_pts == _grid_point(first.start_pts, 11, tick)

    # 제안 뒤 헤드는 그대로 — 저장은 제안이 준 값을 받아서만 일어난다.
    after = service.visual_review_query(workspace_root, base_plan_id=base)
    assert after.revision_id == head_id
    moved = service.visual_adjust_boundary(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=good_boundary.head_revision_id,
        left_segment_id=first.segment_id,
        right_segment_id=second.segment_id,
        boundary_pts=good_boundary.snap_pts,
    )
    assert moved.revision_id != head_id


# ── 공개 소리 선택 — 서수마다 별개 캐시, 바꿔치기 없음 ─────────────────────────


@needs_ffmpeg
def test_audio_selection_switches_registered_preview_per_choice(
    service: raw_service.RawWorkspaceService,
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """두 트랙 원본 — 서수 선택마다 별개 미리보기, 0→1→0→1은 제각각 올바른 등록을 돌려준다.

    명시적 선택 질의는 그 서수의 기록만 본다 — 방금 다른 서수를 만들었다 해도
    최근 것을 바꿔치기하지 않는다. 다시 만드는 선택은 이미 있는 파일을 재사용한다
    (같은 등록 ID·바이트·mtime), 선택 재료는 작업 지문에 묶여 별개 작업이 되고,
    전부 갖춰진 선택의 재준비는 ffmpeg을 한 번도 돌리지 않는다.
    """
    card = tmp_path / "card"
    card.mkdir()
    _dual_tone_media(card / "두트랙.mp4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    source_id = service.visual_review_query(workspace_root, base_plan_id=base).sources[0].source_id

    _, first = _prepared_page(service, workspace_root, base, page=1)  # 기본 — 첫 트랙
    assert first.status == "succeeded", first.error
    default = service.visual_review_query(workspace_root, base_plan_id=base)
    assert default.sources[0].audio_ordinal == 0
    id0 = default.sources[0].registered_file_id
    assert id0 is not None
    path0 = service.resolve_media(workspace_root, id0).path
    probe0 = preprocess.probe_raw_media(path0)
    assert len(probe0.audio) == 1 and probe0.audio[0].sample_rate == 8000  # 440Hz 트랙
    inventory = default.sources[0].audio_tracks  # 준비가 조사해 남긴 목록
    assert [(track.ordinal, track.sample_rate) for track in inventory] == [
        (0, 8000),
        (1, 16000),
    ]
    assert [track.stream_index for track in inventory] == [1, 2]

    _, second = _prepared_page(
        service, workspace_root, base, page=1, audio_selections={source_id: 1}
    )
    assert second.status == "succeeded", second.error
    chosen1 = service.visual_review_query(
        workspace_root, base_plan_id=base, audio_selections={source_id: 1}
    )
    assert chosen1.sources[0].audio_ordinal == 1
    id1 = chosen1.sources[0].registered_file_id
    assert id1 is not None and id1 != id0
    path1 = service.resolve_media(workspace_root, id1).path
    probe1 = preprocess.probe_raw_media(path1)
    assert len(probe1.audio) == 1 and probe1.audio[0].sample_rate == 16000  # 880Hz 트랙
    assert all(card.preview_registered_file_id == id1 for card in chosen1.segments)

    # 최근 기록 함정 — 명시적 0은 방금 만든 1이 아니라 0의 짝을 돌려준다
    chosen0 = service.visual_review_query(
        workspace_root, base_plan_id=base, audio_selections={source_id: 0}
    )
    assert chosen0.sources[0].registered_file_id == id0
    assert all(card.preview_registered_file_id == id0 for card in chosen0.segments)
    latest = service.visual_review_query(workspace_root, base_plan_id=base)  # 생략 = 최근
    assert latest.sources[0].registered_file_id == id1

    # 아직 안 만든 선택은 missing — 다른 선택의 흔적과 무관하다
    unseen = service.visual_review_query(
        workspace_root, base_plan_id=base, audio_selections={source_id: None}
    )
    assert unseen.sources[0].state == "missing"
    assert unseen.sources[0].audio_ordinal is None

    # 0→1→0→1 — 다시 만드는 선택은 재사용이다(같은 ID·바이트·mtime, 파일은 2개)
    _, again1 = _prepared_page(
        service, workspace_root, base, page=1, audio_selections={source_id: 1}
    )
    assert again1.status == "succeeded", again1.error
    assert service.resolve_media(workspace_root, id1).path.stat().st_mtime_ns == (
        path1.stat().st_mtime_ns
    )
    assert service.resolve_media(workspace_root, id1).path.stat().st_size == (path1.stat().st_size)
    mp4s = sorted((workspace_root / "previews" / "visual").glob("*.mp4"))
    assert len(mp4s) == 2  # 서수마다 하나 — 그 밖의 중복 없음
    jobs = [
        job for job in service.list_jobs(workspace_root, limit=20) if job.kind == "visual_prepare"
    ]
    assert len({job.input_fingerprint for job in jobs}) == 2  # 선택이 지문에 묶였다

    # 전부 갖춰진 선택의 재준비 — 추출이 금지여도 성공(없는 것만 만든다)
    monkeypatch.setattr(preprocess, "extract_visual_review_preview", _forbidden)
    monkeypatch.setattr(preprocess, "extract_frame_index", _forbidden)
    _, reused = _prepared_page(
        service, workspace_root, base, page=1, audio_selections={source_id: 1}
    )
    assert reused.status == "succeeded", reused.error
    assert (
        service.visual_review_query(
            workspace_root, base_plan_id=base, audio_selections={source_id: 1}
        )
        .sources[0]
        .registered_file_id
        == id1
    )


@needs_ffmpeg
def test_explicit_silent_choice_and_invalid_ordinals(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """명시적 침묵은 트랙이 있어도 영상만 — 범위 밖·잘못된 서수는 조용한 환원 없이 거부다."""
    card = tmp_path / "card"
    card.mkdir()
    _dual_tone_media(card / "두트랙.mp4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    source_id = service.visual_review_query(workspace_root, base_plan_id=base).sources[0].source_id

    _, silent = _prepared_page(
        service, workspace_root, base, page=1, audio_selections={source_id: None}
    )
    assert silent.status == "succeeded", silent.error
    quiet = service.visual_review_query(
        workspace_root, base_plan_id=base, audio_selections={source_id: None}
    )
    assert quiet.sources[0].state == "ready"
    assert quiet.sources[0].audio_ordinal is None  # 명시적 침묵 — 트랙이 있는 원본이다
    assert quiet.sources[0].registered_file_id is not None
    probe = preprocess.probe_raw_media(
        service.resolve_media(workspace_root, quiet.sources[0].registered_file_id or "").path
    )
    assert len(probe.audio) == 0  # 영상만 담겼다

    _, beyond = _prepared_page(
        service, workspace_root, base, page=1, audio_selections={source_id: 2}
    )
    assert beyond.status == "failed"  # 트랙은 2개(0..1) — 조사 뒤 실패로 닫는다
    assert beyond.error is not None and "소리 서수" in beyond.error

    for bad in (-1, True, 1.5):  # 형이 틀린 선택은 제출 순간에 거부 — 작업도 안 생긴다
        with pytest.raises(VisualReviewError, match="소리 서수"):
            service.start_visual_page_preparation(
                workspace_root,
                base_plan_id=base,
                page=1,
                audio_selections={source_id: cast(Any, bad)},
            )


# ── 리비전 묶음 — 살아남은 조각의 옛 사건도 새 토큰을 못 받는다 ────────────────


@needs_ffmpeg
def test_proposals_query_and_preparation_reject_stale_revision(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """머리가 앞서 갔으면 살아남은 조각의 제안·질의·준비도 stale다(저장 토큰 발급 없음)."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    held = overview.revision_id
    first, second, third = overview.segments[0], overview.segments[1], overview.segments[2]
    clock = overview.selected.clock if overview.selected else None
    assert clock is not None
    tick = clock.frame_ticks

    # 다른 조각을 분할해 머리를 앞서 보낸다 — first·second는 그대로 살아남는다
    edited = service.visual_split_segment(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=held,
        segment_id=third.segment_id,
        split_pts=_grid_point(third.start_pts, 5, tick),
    )
    assert edited.revision_id != held

    desired = _grid_point(first.start_pts, 5, tick)
    with pytest.raises(StaleRevisionError, match="다르다"):
        service.visual_snap_proposal(
            workspace_root,
            base_plan_id=base,
            expected_revision_id=held,
            segment_id=first.segment_id,
            desired_pts=desired,
        )
    with pytest.raises(StaleRevisionError, match="다르다"):
        service.visual_boundary_proposal(
            workspace_root,
            base_plan_id=base,
            expected_revision_id=held,
            left_segment_id=first.segment_id,
            right_segment_id=second.segment_id,
            desired_pts=desired,
        )
    with pytest.raises(StaleRevisionError, match="다르다"):
        service.visual_review_query(workspace_root, base_plan_id=base, expected_revision_id=held)
    with pytest.raises(StaleRevisionError, match="다르다"):
        service.start_visual_page_preparation(
            workspace_root, base_plan_id=base, page=1, expected_revision_id=held
        )
    assert all(
        job.kind != "visual_prepare" for job in service.list_jobs(workspace_root)
    )  # 제출 전 거부 — 작업이 생기지 않는다

    # 새로 고침(묶음 생략)은 최신을 준다 — 거기서 온 토큰으로만 저장이 열린다
    fresh = service.visual_review_query(workspace_root, base_plan_id=base)
    assert fresh.revision_id == edited.revision_id
    proposal = service.visual_snap_proposal(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=fresh.revision_id,
        segment_id=first.segment_id,
        desired_pts=desired,
    )
    assert proposal.valid is True
    assert proposal.head_revision_id == fresh.revision_id
    saved = service.visual_split_segment(
        workspace_root,
        base_plan_id=base,
        expected_revision_id=proposal.head_revision_id,
        segment_id=first.segment_id,
        split_pts=proposal.snap_pts,
    )
    assert saved.revision_id != fresh.revision_id

    # 예약된 작업도 실행 순간에 다시 본다 — stale면 새 기하를 몰래 채우지 않는다
    workspace = RawWorkspace.open(workspace_root)
    facade = ReviewWorkspaceFacade(workspace_root)

    def stale_body(context: object) -> None:
        visual_review.run_page_preparation(
            context,  # type: ignore[arg-type]
            workspace,
            facade,
            base_plan_id=base,
            page=1,
            expected_revision_id=held,
        )

    handle = service.submit_job(
        workspace_root, "visual_prepare", "stale-page-check", stale_body, total=1
    )
    stale = _wait_terminal(service, workspace_root, handle.job_id)
    assert stale.status == "failed"
    assert stale.error is not None and "다르다" in stale.error


# ── 정확 유리수 입력 — 서브틱·동률·거부 ───────────────────────────────────────


@needs_ffmpeg
def test_proposal_accepts_exact_fraction_input_and_rejects_inexact(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """제안의 원하는 점은 서브틱 유리수까지 정확히 — float·무한·NaN·bool은 거부다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    selected = overview.selected
    assert selected is not None
    clock = selected.clock
    tick = clock.frame_ticks
    origin = clock.origin_pts
    segment_id = selected.segment_id

    def propose(value: Fraction | int) -> visual_review.VisualSnapProposal:
        return service.visual_snap_proposal(
            workspace_root,
            base_plan_id=base,
            expected_revision_id=overview.revision_id,
            segment_id=segment_id,
            desired_pts=value,
        )

    tie = origin + Fraction(5 * tick, 2)  # 정확히 2.5 프레임 자리(틱 홀수면 반 틱 PTS)
    below = tie - Fraction(1, 1000)
    above = tie + Fraction(1, 1000)
    assert propose(tie).snap_pts == origin + 2 * tick  # 동률 → 앞장
    assert propose(below).snap_pts == origin + 2 * tick  # 살짝 아래 → 앞장
    assert propose(above).snap_pts == origin + 3 * tick  # 살짝 위 → 뒷장
    assert propose(origin + 3 * tick).snap_pts == origin + 3 * tick  # 정수 입력도 그대로
    exact = propose(tie)
    assert exact.valid is True
    assert exact.desired_pts == tie  # 서브틱 값이 그대로 보존된다
    assert exact.desired_seconds == str(Fraction(5 * tick, 2) * clock.time_base)
    assert exact.snap_seconds == str(2 * tick * clock.time_base)

    for bad in (1.5, float("inf"), float("nan"), True, "1501.5"):
        with pytest.raises(VisualReviewError, match="정확한 정수"):
            propose(cast(Any, bad))  # type: ignore[arg-type]


# ── 선행 소리(G3) — 브라우저 시각은 잰 원점을 **더한다** ───────────────────────


@needs_ffmpeg
def test_leading_audio_preview_maps_browser_time_by_addition(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """소리가 영상보다 먼저 시작하면 브라우저 시간표가 소리에서 시작한다 — 가산 매핑.

    ROOT 실측 재현: 선행 소리 원본의 미리보기는 영상 원점이 0이 아닌 채로 나오고,
    원본 국소 시각 s는 브라우저 시각 s + (영상 원점 − 소리 원점)에 나타난다. 뺄셈
    공식은 첫 영상 프레임보다 앞인 값을 내서 틀렸다. 영상·소리의 상대 시각은
    그대로 보존된다(remux가 선언 시각을 하나도 안 바꾼 증거).
    """
    card = tmp_path / "card"
    card.mkdir()
    _leading_audio_media(card / "선행소리.mp4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    preview = overview.sources[0]
    assert preview.state == "ready"
    assert preview.browser_origin_seconds is not None
    offset = Fraction(preview.browser_origin_seconds)
    assert offset > 0  # 선행 소리 — 영상이 브라우저 시간표보다 늦게 시작한다

    preview_path = workspace_root / (preview.relative_path or "")
    out_probe = preprocess.probe_raw_media(preview_path)
    out_clock = preprocess.probe_extraction_clock(preview_path, probe=out_probe)
    assert len(out_probe.audio) == 1
    audio_origin = out_probe.audio[0].start_time
    assert audio_origin < out_clock.presentation_origin  # 소리가 먼저다
    assert preview.audio_origin_seconds is not None
    assert Fraction(preview.audio_origin_seconds) == audio_origin  # 기록 = 잰 값
    # 이 매핑의 법칙 — 브라우저 원점은 **출력 영상 첫 PTS 그 자체**다(소리 원점
    # 뺄셈이 우연히 같은 값을 내는 이 원본에서도 법칙을 직접 잰다, G3).
    assert offset == out_clock.presentation_origin

    for card_item in overview.segments:  # 가산 매핑 — 뺄셈이면 음수가 나오는 자리들
        assert card_item.preview_seek_seconds is not None
        assert Fraction(card_item.preview_seek_seconds) == (
            Fraction(card_item.snapped_seconds) + offset
        )

    source_path = workspace_root / "originals" / "import_000001" / "선행소리.mp4"
    source_probe = preprocess.probe_raw_media(source_path)
    source_clock = preprocess.probe_extraction_clock(source_path, probe=source_probe)
    lead_out = out_clock.presentation_origin - audio_origin
    lead_source = source_clock.presentation_origin - source_probe.audio[0].start_time
    # 원본의 영상−소리 상대 시각이 그대로 흐른다 — muxer가 시간표를 옮겨 최소
    # 스트림을 0에 두지만(이 파일에선 소리), 그 이동은 두 스트림이 같이 받으므로
    # 차이만 남는다. 1ms 안은 타임베이스 반올림이다(ROOT 실측 0.521328↔명목 0.5와
    # 같은 크기의 오차).
    assert abs(lead_out - lead_source) < Fraction(1, 1000)


# ── G3 조합 — 세 소리 선택 모두 브라우저 원점 = 출력 영상 첫 PTS ──────────────


@needs_ffmpeg
def test_browser_origin_is_output_video_first_pts_for_every_audio_choice(
    service: raw_service.RawWorkspaceService,
    workspace_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """세 소리 선택(0/1/침묵) 모두 브라우저 원점 = 그 미리보기의 출력 영상 첫 PTS.

    프레임 대응이 오라클이다 — 스트림 복사 미리보기의 프레임 k 화소가 원본 프레임
    k와 바이트까지 같고(PTS→프레임), 원점+국소시각이 곧 재생 위치다. 늦은 트랙·
    침묵에서 0을 내놓던 소리 원점 뺄셈은 폐기(A3 실측). F1 기록의 틀린 0도
    증거(video_origin_seconds)에서 ffprobe·쓰기 없이 회복한다 — 매핑 값만으로
    파일·등록 ID를 다시 만들지 않는다.
    """
    card = tmp_path / "card"
    card.mkdir()
    _dual_leading_media(card / "선행두트랙.mp4")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    source_id = service.visual_review_query(workspace_root, base_plan_id=base).sources[0].source_id
    source_path = workspace_root / "originals" / "import_000001" / "선행두트랙.mp4"

    origins: dict[int | None, Fraction] = {}
    for choice in (0, 1, None):
        _, record = _prepared_page(
            service, workspace_root, base, page=1, audio_selections={source_id: choice}
        )
        assert record.status == "succeeded", record.error
        overview = service.visual_review_query(
            workspace_root, base_plan_id=base, audio_selections={source_id: choice}
        )
        preview = overview.sources[0]
        assert preview.state == "ready"
        assert preview.audio_ordinal == choice
        assert preview.browser_origin_seconds is not None
        origin = Fraction(preview.browser_origin_seconds)
        assert origin > 0  # 소리0가 영상보다 앞서므로 출력 영상은 0이 아닌 자리에서
        preview_path = workspace_root / (preview.relative_path or "")
        measured = preprocess.probe_extraction_clock(preview_path)
        assert origin == measured.presentation_origin  # 공개 값 = 출력 영상 첫 PTS
        origins[choice] = origin
        for frame in (0, 12, 40):  # 화소 대응 — 같은 비트스트림, 같은 화소
            assert _frame_gray(preview_path, frame) == _frame_gray(source_path, frame)
        for card_item in overview.segments:  # 재생 위치 = 국소 시각 + 출력 영상 원점
            assert card_item.preview_seek_seconds is not None
            assert Fraction(card_item.preview_seek_seconds) == (
                Fraction(card_item.snapped_seconds) + origin
            )
    assert origins[0] == origins[1] == origins[None]  # 이동은 소리 선택과 무관하다

    # F1 기록 회복 — 늦은 트랙 선택의 기록에 틀린 0이 적혀 있어도 증거에서 잇는다
    stale_choice = service.visual_review_query(
        workspace_root, base_plan_id=base, audio_selections={source_id: 1}
    )
    registered_id = stale_choice.sources[0].registered_file_id
    record_file = workspace_root / Path(stale_choice.sources[0].relative_path or "").with_suffix(
        ".json"
    )
    payload = json.loads(record_file.read_text(encoding="utf-8"))
    assert Fraction(str(payload["browser_origin_seconds"])) == origins[1]  # 새 공식 기록
    payload["browser_origin_seconds"] = "0"  # F1 뺄셈 공식이 남겼을 값(늦은 트랙 → 0)
    record_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    monkeypatch.setattr(preprocess, "require_ffmpeg", _forbidden)
    monkeypatch.setattr(preprocess, "probe_raw_media", _forbidden)
    monkeypatch.setattr(preprocess, "ffmpeg_provenance", _forbidden)
    monkeypatch.setattr(preprocess, "probe_extraction_clock", _forbidden)
    files_before = sorted(
        path for path in (workspace_root / "previews").rglob("*") if path.is_file()
    )
    recovered = service.visual_review_query(
        workspace_root, base_plan_id=base, audio_selections={source_id: 1}
    )
    assert recovered.sources[0].state == "ready"
    assert recovered.sources[0].browser_origin_seconds is not None
    assert Fraction(recovered.sources[0].browser_origin_seconds) == origins[1]  # 회복
    assert recovered.sources[0].registered_file_id == registered_id  # ID 재발급 없음
    assert (
        sorted(path for path in (workspace_root / "previews").rglob("*") if path.is_file())
        == files_before
    )  # 파일도 하나도 새로 안 만든다


# ── E1 계보 — 옮겨진 원본의 시각 편집 → 재계획 → 새 package ───────────────────


@needs_ffmpeg
def test_visual_edits_replan_to_a_new_package_lineage(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """선언 시작 2초 원본에서 분할·경계 이동 → 재계획 → 새 package가 완성된다.

    정착 기준선에서는 이 경로가 지원 문(선언 창 좌표 착오)에서 막혔다. 이제 기본
    계획의 package와 편집 뒤 재계획의 package가 각각 완성되고, 옛 package는
    바이트 그대로다(새 리비전은 새 package에서 뽑는다, §6.5). 조각 프레임 수가
    편집 경계를 그대로 산다 — 절대 탐색이 프레임을 옮기지 않는 증거.
    """
    card = tmp_path / "card"
    card.mkdir()
    _shifted_media(card / "옮겨진발화.mov")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)

    first_handle = service.start_segment_extraction(workspace_root, base)
    first_done = _wait_terminal(service, workspace_root, first_handle.job_id)
    assert first_done.status == "succeeded", first_done.error
    first_package = service.package_overview(workspace_root)
    assert first_package is not None
    assert len(first_package.clips) == 3
    first_dir = first_package.package.package_dir
    first_hashes = dict(_tree_hashes(workspace_root, first_dir))

    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    head = overview.revision_id
    clock = overview.selected.clock if overview.selected else None
    assert clock is not None
    assert clock.origin_pts > 0  # 원본 시계 원점이 0이 아니다(2초) — E1 재료
    first = overview.segments[0]
    split = service.visual_split_segment(  # 첫 조각(1초)을 프레임 13 자리에서 둘로
        workspace_root,
        base_plan_id=base,
        expected_revision_id=head,
        segment_id=first.segment_id,
        split_pts=first.start_pts + 13 * clock.frame_ticks,
    )
    split_view = service.visual_review_query(workspace_root, base_plan_id=base)
    child_b, second = split_view.segments[1], split_view.segments[2]
    moved = service.visual_adjust_boundary(  # child_b|second 경계를 0.76초로
        workspace_root,
        base_plan_id=base,
        expected_revision_id=split.revision_id,
        left_segment_id=child_b.segment_id,
        right_segment_id=second.segment_id,
        boundary_pts=child_b.start_pts + 6 * clock.frame_ticks,
    )
    edited = service.visual_review_query(workspace_root, base_plan_id=base)
    assert edited.revision_id == moved.revision_id
    assert len(edited.segments) == 4

    time_base = Fraction(clock.time_base_num, clock.time_base_den)
    intervals = [
        PlannedInterval(
            start=(seg.start_pts - clock.origin_pts) * time_base,
            end=(seg.end_pts - clock.origin_pts) * time_base,
        )
        for seg in edited.segments
    ]
    replan = service.plan_segmentation(
        workspace_root, intervals={edited.segments[0].source_id: intervals}
    )
    assert replan.revision_id != base
    assert len(replan.entries) == 4  # 분할이 계획에도 산다

    second_handle = service.start_segment_extraction(workspace_root, replan.revision_id)
    second_done = _wait_terminal(service, workspace_root, second_handle.job_id)
    assert second_done.status == "succeeded", second_done.error
    new_package = service.package_overview(workspace_root)
    assert new_package is not None
    assert new_package.package.package_dir != first_dir  # 새 리비전은 새 package
    assert len(new_package.clips) == 4
    assert _tree_hashes(workspace_root, first_dir) == first_hashes  # 옛 package 불변
    for clip, want in zip(new_package.clips, (13, 6, 31, 25), strict=True):
        clip_clock = preprocess.probe_extraction_clock(workspace_root / clip.relative_path)
        assert clip_clock.frame_count == want  # 13+6+31+25 = 75프레임, 구멍 없음


# ── 침묵 상태 — 오디오 없음은 실패가 아니다 ───────────────────────────────────


@needs_ffmpeg
def test_silent_source_preview_is_ready_not_failed(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """오디오 트랙 없는 원본의 미리보기는 ready다 — 침묵은 지원 밖이 아니다."""
    card = tmp_path / "card"
    card.mkdir()
    _silent_media(card / "무성.mp4", "2")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    preview = service.visual_review_query(workspace_root, base_plan_id=base).sources[0]
    assert preview.state == "ready"
    assert preview.error is None


# ── 침범 금지 — 원본·package는 하나도 바뀌지 않는다 ───────────────────────────


@needs_ffmpeg
def test_prepare_never_mutates_originals_or_package(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """준비 뒤 원본의 해시 나무가 바이트 그대로다 — 미리보기는 previews/ 아래만."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    originals = dict(_tree_hashes(workspace_root, "originals"))
    clips = (
        dict(_tree_hashes(workspace_root, "clips")) if (workspace_root / "clips").is_dir() else {}
    )
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    assert _tree_hashes(workspace_root, "originals") == originals
    after_clips = (
        dict(_tree_hashes(workspace_root, "clips")) if (workspace_root / "clips").is_dir() else {}
    )
    assert after_clips == clips  # package는 하나도 바뀌지 않는다

    preview_files = sorted(
        path.relative_to(workspace_root).as_posix()
        for path in (workspace_root / "previews").rglob("*")
        if path.is_file()
    )
    assert preview_files, "미리보기 파일이 있어야 한다"
    assert all(path.startswith("previews/visual/") for path in preview_files)
    stray = workspace_root / "previews" / "visual" / "notes.txt"
    stray.write_text("x", encoding="utf-8")
    with pytest.raises(Exception, match="미디어 확장자가 아니다"):
        service.register_media(workspace_root, stray, "preview")
    stray.unlink()
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    resolved = service.resolve_media(
        workspace_root, overview.selected.thumb_registered_file_id or ""
    )
    assert resolved.kind == "preview"
    assert resolved.content_type == "image/jpeg"
    assert resolved.path.suffix == ".jpg"


# ── 정리(purge)와 회복 — 같은 등록 ID로 되살아난다 ─────────────────────────────


@needs_ffmpeg
def test_cache_purge_recovery_reuses_registration_ids(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """정리 뒤 상태는 stale — 다시 준비하면 같은 file_id로 회복한다."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    _, record = _prepared_page(service, workspace_root, base, page=1)
    assert record.status == "succeeded", record.error
    before = service.visual_review_query(workspace_root, base_plan_id=base)
    thumb_ids = [card.thumb_registered_file_id for card in before.segments]
    preview_id = before.sources[0].registered_file_id
    assert all(thumb_ids) and preview_id

    facade = ReviewWorkspaceFacade(workspace_root)
    purge = facade.managed_cleanup()
    assert purge.removed_files > 0

    purged = service.visual_review_query(workspace_root, base_plan_id=base)
    assert purged.sources[0].state == "stale"  # 등록 행은 남았는데 파일이 없다
    assert all(card.thumb_state == "stale" for card in purged.segments)
    with pytest.raises(MediaNotFoundError):
        service.resolve_media(workspace_root, thumb_ids[0] or "none")

    _, recovered = _prepared_page(service, workspace_root, base, page=1)
    assert recovered.status == "succeeded", recovered.error
    after = service.visual_review_query(workspace_root, base_plan_id=base)
    assert after.sources[0].state == "ready"
    assert after.sources[0].registered_file_id == preview_id  # 같은 ID로 회복
    assert [card.thumb_registered_file_id for card in after.segments] == thumb_ids
    resolved = service.resolve_media(workspace_root, after.sources[0].registered_file_id or "")
    assert resolved.path.is_file() and resolved.path.suffix == ".mp4"


# ── 동시성 — 같은 페이지를 동시에 준비해도 캐시는 안전하다 ─────────────────────


def _concurrent_body(workspace: RawWorkspace, facade: ReviewWorkspaceFacade, base: str):
    """두 작업이 같은 페이지 본문을 동시에 돌리는 묶음 — 경합 재현용."""

    def body(context: object) -> None:
        visual_review.run_page_preparation(
            context,
            workspace,
            facade,
            base_plan_id=base,
            page=1,  # type: ignore[arg-type]
        )

    return body


@needs_ffmpeg
def test_concurrent_preparation_keeps_cache_consistent(
    service: raw_service.RawWorkspaceService, workspace_root: Path, tmp_path: Path
) -> None:
    """두 작업이 같은 페이지를 동시에 채워도 파일은 하나·온전하다(link 게시)."""
    card = tmp_path / "card"
    card.mkdir()
    _voiced_media(card / "발화.mp4", "3")
    base = _review_ready(service, workspace_root, card, ONE_SECOND_TILE)
    facade = ReviewWorkspaceFacade(workspace_root)
    workspace = RawWorkspace.open(workspace_root)

    handles = [
        service.submit_job(
            workspace_root,
            "visual_prepare",
            f"concurrent-{index}",
            _concurrent_body(workspace, facade, base),
            total=3,
        )
        for index in range(2)
    ]
    records = [_wait_terminal(service, workspace_root, handle.job_id) for handle in handles]
    assert all(record.status == "succeeded" for record in records), [r.error for r in records]

    thumbs = sorted((workspace_root / "previews" / "visual" / "thumbs").glob("*.jpg"))
    assert len(thumbs) == 3  # 같은 키의 파일은 하나 — 진 쪽 임시 파일은 사라졌다
    for thumb in thumbs:
        assert thumb.stat().st_size > 0
    overview = service.visual_review_query(workspace_root, base_plan_id=base)
    assert all(card.thumb_state == "ready" for card in overview.segments)
    preview_files = sorted((workspace_root / "previews" / "visual").glob("*.mp4"))
    assert len(preview_files) == 1  # 원본 전체 미리보기도 하나
    probe = preprocess.probe_raw_media(preview_files[0])
    assert probe.video.codec_name == "h264"
