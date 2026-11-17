"""4화면 브라우저 — 실백엔드(W2·W3) 합성 미디어 흐름(F1).

가짜 파사드 시대는 끝났다. 이 묶음은 병합된 실제 백엔드 위에서 화면 처리기
전체를 돈다:

- 검토(화면 2): 실제 계획·시계·분석 증거에서 초기화하고, 메모·선택·경계·나누기·
  합치기가 **화면이 본 헤드 리비전에 묶여** 적용된다. 격자 밖 초는 거부된다.
- 재계획·재추출·측정 VTT: 편집 덮개가 새 계획 리비전으로 환원되고 새 package에
  뽑히며, 검토 VTT가 내용 해시 이름의 등록 파일로만 제공된다(출처 표시 동반).
- 확정 내보내기(화면 3): 제출 때의 **화면이 본 리비전·선택**에 묶인 작업 본문,
  캐시 정리 뒤에도 확정 바이트가 그대로다(재검증이 증명한다).
- 확정 내보내기는 백엔드에서도 제출 때의 리비전 아이디에 묶인다(경합 0바이트 거부).
- 자막(화면 4)·F2·F3 시험은 숨은 「자막」 탭과 함께 걷었다(D162). 자막 작업공간의
  등록·실행·검토는 `tests/raw/test_caption_workspace.py`·`test_caption_review.py`가 잰다
  (선택 오디오 미리보기 `caption_preview`는 그 탭 시험만 쟀다 — 지금은 시험이 없다).
- DB v5→v6 승격과 저장소 스키마(출처 `origin` 열 포함)의 존재를 직접 확인한다.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import time
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw.gemini_echo import write_utterance_replay
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.models import PlannedInterval  # noqa: E402
from vlogsrt.project.raw.workspace import DB_NAME  # noqa: E402

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
WAIT_SECONDS = 300.0

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)


# ── 합성 미디어·준비 도구 ─────────────────────────────────────────────────────


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


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path, relative: str) -> dict[str, str]:
    base = root / relative
    return {
        path.relative_to(root).as_posix(): _sha256(path)
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="session")
def spoken_media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    card = tmp_path_factory.mktemp("ui_card")
    card.joinpath("DCIM").mkdir()
    _av_media(card / "DCIM" / "발화.mov", "4")
    _av_media(card / "DCIM" / "무성.mov", "3")
    return card


def _wait_terminal(server: workspace_service.RawWorkspaceUi, root: Path, job_id: str):
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        record = server.raw.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    return server.raw.get_job(root, job_id)


def _prepare(
    server: workspace_service.RawWorkspaceUi,
    root: Path,
    card: Path,
    *,
    with_analysis: bool,
) -> str:
    """카드 가져오기 → 전체 순서 → 구간 계획 → (재생 분석) → 추출까지 끝낸다.

    돌려주는 값은 첫 계획 리비전 ID다(검토 초기화·재추출 판정의 기준).
    """
    proposal = server.raw.scan_sources(root, card)
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    record = _wait_terminal(server, root, handle.job_id)
    assert record.status == "succeeded", record.error
    server.raw.confirm_global_source_order(root)
    assets = {asset.source_name: asset.asset_id for asset in server.raw.list_source_assets(root)}
    intervals = {
        assets["발화.mov"]: [
            PlannedInterval(Fraction(0), Fraction(2)),
            PlannedInterval(Fraction(2), Fraction(1000)),
        ],
        assets["무성.mov"]: [PlannedInterval(Fraction(0), Fraction(1000))],
    }
    plan = server.raw.plan_segmentation(root, intervals=intervals)
    if with_analysis:
        shas = {
            asset.asset_id: (asset.content_sha256 or "")
            for asset in server.raw.list_source_assets(root)
        }
        fixtures = root / "analysis" / "replay"
        fixtures.mkdir(parents=True, exist_ok=True)
        write_utterance_replay(
            fixtures,
            {
                shas[assets["발화.mov"]]: [
                    {"start": 0.8, "end": 2.0, "text": "도착해서 일정 설명"},
                    {"start": 2.7, "end": 3.4, "text": "다음 장소 안내"},
                ]
            },
        )
        analysis = server.raw.start_analysis(root)
        analysis_record = _wait_terminal(server, root, analysis.job_id)
        assert analysis_record.status == "succeeded", analysis_record.error
    extract = server.raw.start_segment_extraction(root, plan_revision_id=plan.revision_id)
    extract_record = _wait_terminal(server, root, extract.job_id)
    assert extract_record.status == "succeeded", extract_record.error
    return plan.revision_id


def _new_server(tmp_path: Path, **kwargs: Any) -> workspace_service.RawWorkspaceUi:
    server = workspace_service.RawWorkspaceUi(
        port=7860, state_path=tmp_path / "state.json", **kwargs
    )
    view = server.create_workspace_view("검토확인", str(tmp_path / "ws"))
    signature = view[1]
    assert signature
    return server


def _open(server: workspace_service.RawWorkspaceUi, root: Path) -> str:
    view = server.open_workspace_view(str(root))
    signature = view[1]
    assert signature
    return signature


# ── 순수 도우미 — 계약: 파생 계층 import 없음 ────────────────────────────────


def test_ui_facade_has_no_derived_imports() -> None:
    """순수 도우미 — 모듈이 vlogsrt·fastapi·gradio를 알지 않는다."""
    import ui.ui_facade as module

    sources = set(vars(module))
    assert not any(name.startswith(("vlogsrt", "gradio", "fastapi")) for name in sources)


def test_media_player_helper() -> None:
    """재생기 HTML — 값은 이스케이프되고 자막 트랙·출처 표시는 선택이다."""
    player = ui_facade.media_player_html(
        "/media/abc", track_path="/media/vtt1", caption="출처 — 원본 `a.mov`"
    )
    assert '<source src="/media/abc">' in player
    assert 'src="/media/vtt1"' in player and "<track" in player
    assert "출처 — 원본" in player
    plain = ui_facade.media_player_html("/media/abc")
    assert "<track" not in plain and "출처" not in plain


def test_review_rows_markdown() -> None:
    """검토 표 — 빈 값은 정직한 안내."""
    assert "검토 리비전이 없다" in ui_facade.review_rows_markdown([])
    rows_md = ui_facade.review_rows_markdown(
        [{"segment_id": "s1", "source_order": 1, "start_seconds": "0"}]
    )
    assert "`s1`" in rows_md


# ── 백엔드 없는 빌드 — 정직한 오류(주입으로 시뮬레이션) ───────────────────────


def test_screens_close_honestly_without_backend(tmp_path: Path, monkeypatch) -> None:
    """검토 백엔드가 없으면 화면은 안내 오류로 닫힌다(빈 성공이 아니다)."""
    server = _new_server(tmp_path)
    signature = _open(server, tmp_path / "ws")
    root = server.workspace_root()
    assert root is not None

    def unavailable(*_args: object, **_kwargs: object) -> object:
        raise raw_service.WorkspaceModuleUnavailableError(
            "검토 작업공간 백엔드(review_workspace)가 이 빌드에 없다"
        )

    monkeypatch.setattr(server.raw, "review_workspace_facade", unavailable)
    for call in (
        lambda: server.review_initialize_view(signature),
        lambda: server.review_filter_view(signature, "", ""),
        lambda: server.review_note_view(signature, "s1", "메모"),
        lambda: server.review_selection_view(signature, "s1", "제외"),
        lambda: server.review_boundary_view(signature, "s1", "2"),
        lambda: server.review_split_view(signature, "s1", "2"),
        lambda: server.review_merge_view(signature, "s1", "s2"),
        lambda: server.review_reextract_view(signature),
        lambda: server.review_plan_view(signature),
        lambda: server.review_export_view(signature, "", True),
        lambda: server.review_cleanup_view(signature, True),
    ):
        with pytest.raises(gr.Error, match="백엔드"):
            call()
    # 새로고침은 그리기 경로다 — 오류 대신 정직한 안내 문구로 패널을 그린다.
    refreshed = server.review_refresh_view(signature)
    assert "백엔드 없음" in refreshed


# ── 화면 2 — 실백엔드 검토 흐름 ───────────────────────────────────────────────


@needs_ffmpeg
def test_review_screen_real_flow(tmp_path: Path, spoken_media: Path) -> None:
    """검토 초기화→메타데이터 편집→경계·나누기·합치기→재추출 판정 — 실백엔드로."""
    server = _new_server(tmp_path)
    signature = _open(server, tmp_path / "ws")
    root = server.workspace_root()
    assert root is not None
    plan_id = _prepare(server, root, spoken_media, with_analysis=True)

    panel = server.review_initialize_view(signature)
    assert plan_id in panel and "검토" in panel
    facade = server.raw.review_workspace_facade(root)
    loaded = facade.load_head(plan_id)
    revision = loaded.revision
    assert revision.base_plan_id == plan_id
    by_name = {asset.source_name: asset.asset_id for asset in server.raw.list_source_assets(root)}
    speech = next(seg for seg in revision.segments if seg.source_id == by_name["발화.mov"])
    silent = next(seg for seg in revision.segments if seg.source_id == by_name["무성.mov"])
    # 시간순 표 — 파일 원점 기준 초 환산(발화 0~2초 조각이 보인다).
    clock = revision.clock_for(speech.source_id)
    assert clock is not None
    panel = server.review_refresh_view(signature)
    assert speech.segment_id in panel and "2" in panel
    assert server._review_revision_id == revision.revision_id
    assert server._review_base_plan_id == plan_id

    # 메타데이터 편집 — 재추출 대상은 늘 0개다(계획 범위 그대로).
    noted = server.review_note_view(signature, speech.segment_id, "도착 장면")
    assert "메모 저장" in noted and "편집을 적용했다" in noted and "도착 장면" in noted
    selected = server.review_selection_view(signature, speech.segment_id, "제외")
    assert "선택 표시" in selected and "편집을 적용했다" in selected and "excluded" in selected
    first_reextract = server.review_reextract_view(signature)
    assert "0개" in first_reextract and "0개가 정상" in first_reextract

    # 격자 밖 초 — 프레임 간격과 함께 정직하게 거부된다.
    with pytest.raises(gr.Error, match="프레임 격자"):
        server.review_split_view(signature, speech.segment_id, "1.55")
    with pytest.raises(gr.Error, match="프레임 격자"):
        server.review_boundary_view(signature, speech.segment_id, "1.55")

    # 경계 조정 — 발화의 첫 조각과 둘째 조각의 공유 경계를 1.5초로 옮긴다.
    frame_seconds = Fraction(clock.frame_ticks) * clock.time_base
    boundary_seconds = 15 * frame_seconds  # 10fps → 1.5초, 격자 위
    assert any(
        seg.source_id == speech.source_id and seg.start_pts == speech.end_pts
        for seg in revision.segments
    )  # 오른쪽 이웃이 있다(경계 조정의 짝)
    boundary = server.review_boundary_view(
        signature, speech.segment_id, ui_facade.format_seconds(boundary_seconds)
    )
    assert "경계 조정" in boundary and "편집을 적용했다" in boundary

    # 나누기 — 무성 조각을 1.5초에서 둘로, 그리고 다시 합친다(같은 원본 맞닿음).
    silent_clock = revision.clock_for(silent.source_id)
    assert silent_clock is not None
    split_at = 15 * Fraction(silent_clock.frame_ticks) * silent_clock.time_base
    split = server.review_split_view(
        signature, silent.segment_id, ui_facade.format_seconds(split_at)
    )
    assert "나누기" in split and "편집을 적용했다" in split
    spans = server._review_spans
    silent_children = sorted(
        (seg for seg, (asset, _s, _e) in spans.items() if asset == silent.source_id),
        key=lambda seg: spans[seg][1],
    )
    assert len(silent_children) == 2
    merged = server.review_merge_view(signature, silent_children[0], silent_children[1])
    assert "이웃 합치기" in merged and "편집을 적용했다" in merged

    # 재추출 판정 — 계획과 달라진 경계(발화 2조각)만 나온다. 백엔드 판정과 같다.
    reextract = server.review_reextract_view(signature)
    assert "2개" in reextract
    head = facade.load_head(plan_id).revision
    speech_ids = {seg.segment_id for seg in head.segments if seg.source_id == speech.source_id}
    backend_ids = facade.reextract_ids(revision, head)
    assert speech_ids == backend_ids  # 순수 도메인 판정과 화면 판정이 일치한다
    # 편집이 없는 조각(무성 — 나누고 다시 합쳐 원래 범위)은 다시 뽑을 대상이 아니다.
    silent_id = next(seg.segment_id for seg in head.segments if seg.source_id == silent.source_id)
    assert silent_id not in backend_ids


@needs_ffmpeg
def test_review_plan_reextract_vtt_real_flow(tmp_path: Path, spoken_media: Path) -> None:
    """검토 계획 반영→새 package 추출→측정 VTT 등록 제공(호출 0회)."""
    server = _new_server(tmp_path)
    signature = _open(server, tmp_path / "ws")
    root = server.workspace_root()
    assert root is not None
    plan_id = _prepare(server, root, spoken_media, with_analysis=True)
    server.review_initialize_view(signature)
    facade = server.raw.review_workspace_facade(root)
    head = facade.load_head(plan_id).revision
    speech_asset = next(
        asset for asset in server.raw.list_source_assets(root) if asset.source_name == "발화.mov"
    )
    speech = next(seg for seg in head.segments if seg.source_id == speech_asset.asset_id)
    clock = head.clock_for(speech.source_id)
    assert clock is not None
    frame_seconds = Fraction(clock.frame_ticks) * clock.time_base

    # 경계 편집 → 새 계획 리비전 → 새 package 추출(옛 package는 불변).
    server.review_boundary_view(
        signature, speech.segment_id, ui_facade.format_seconds(15 * frame_seconds)
    )
    old_package = server._latest_package_dir(root)
    assert old_package is not None
    old_hashes = _tree_hashes(root, old_package)
    planned = server.review_plan_view(signature)
    assert "새 계획 리비전" in planned
    assert server._review_base_plan_id != plan_id
    extract = server.raw.start_segment_extraction(root)
    record = _wait_terminal(server, root, extract.job_id)
    assert record.status == "succeeded", record.error
    new_package = server._latest_package_dir(root)
    assert new_package is not None and new_package != old_package
    assert _tree_hashes(root, old_package) == old_hashes

    # 측정 VTT — 등록 파일은 내용 해시 이름, 재생기에는 출처가 함께 나온다.
    clips = server.clip_choices(root)
    assert clips
    speech_clips = [
        value
        for label, value in clips
        if server._segment_id_for_clip(root, value) in server._review_spans
    ]
    assert speech_clips
    file_id = speech_clips[0]
    previews_before = len(server.raw.list_media(root, kind="preview"))
    player = server.review_vtt_toggle_view(signature, file_id, True)
    assert "<track" in player and 'kind="subtitles"' in player
    assert "출처 — 원본" in player and "추출" in player
    track_id = player.split('src="/media/')[2].split('"')[0]
    vtt_media = server.raw.resolve_media(root, track_id)
    vtt_relative = vtt_media.path.relative_to(root).as_posix()
    assert vtt_relative.startswith("previews/review-vtt/")
    assert vtt_media.path.read_text(encoding="utf-8").startswith("WEBVTT")
    # 토글은 조회다 — 다시 켜도 등록이 늘지 않고(멱등) 호출도 재생성도 없다.
    again = server.review_vtt_toggle_view(signature, file_id, True)
    assert again.split('src="/media/')[2].split('"')[0] == track_id
    assert len(server.raw.list_media(root, kind="preview")) == previews_before + 1
    off = server.review_vtt_toggle_view(signature, file_id, False)
    assert "<track" not in off


# ── 화면 3 — 확정 내보내기: 표시 묶음·불변·Finder·정리 ───────────────────────


@needs_ffmpeg
def test_export_binds_displayed_revision_and_survives_purge(
    tmp_path: Path, spoken_media: Path
) -> None:
    """내보내기 작업은 제출 때의 검토 리비전·선택에 묶이고 정리 뒤에도 바이트 그대로다."""
    opened: list[Path] = []
    server = workspace_service.RawWorkspaceUi(
        port=7860, state_path=tmp_path / "state.json", finder_opener=opened.append
    )
    view = server.create_workspace_view("검토확인", str(tmp_path / "ws"))
    signature = view[1]
    root = server.workspace_root()
    assert root is not None
    _prepare(server, root, spoken_media, with_analysis=False)
    server.review_initialize_view(signature)
    facade = server.raw.review_workspace_facade(root)
    head = facade.load_head().revision
    first_segment = sorted(server._review_spans, key=lambda seg: server._review_spans[seg][1])[0]

    # 화면이 본 리비전 뒤에 다른 클라이언트가 편집하면 내보내기는 거부된다.
    server.review_refresh_view(signature)
    displayed = server._review_revision_id
    assert displayed is not None
    facade.apply_edit(
        head.base_plan_id, raw_service.review_note_edit(displayed, first_segment, "다른 클라이언트")
    )
    with pytest.raises(gr.Error, match="바뀌었다"):
        server.review_export_view(signature, "", True)

    # 새로 고친 뒤 제출 — 선택 검증(없는 조각)과 확인란 게이트부터.
    server.review_refresh_view(signature)
    with pytest.raises(gr.Error, match="확인이 필요하다"):
        server.review_export_view(signature, "", False)
    with pytest.raises(gr.Error, match="없는 조각"):
        server.review_export_view(signature, "segment_없는것", True)
    export_md, _package_md = server.review_export_view(signature, first_segment, True)
    assert "제출" in export_md and server._review_revision_id in export_md
    job_id = export_md.split("`")[1]
    record = _wait_terminal(server, root, job_id)
    assert record.status == "succeeded", record.error

    # 본문이 잡은 자리 — 서버 소유 confirmed_reviewexport 이름공간.
    destination_rel = server._last_export_dir
    assert destination_rel is not None and destination_rel.startswith("clips/confirmed_")
    destination = root / destination_rel
    assert destination.is_dir() and (destination / "media").is_dir()
    frozen = _tree_hashes(root, destination_rel)

    copied = server.review_export_copy_view(signature)
    assert copied == server._last_export_path
    note = server.review_export_open_view(signature)
    assert "Finder에서 열었다" in note and opened[-1] == destination.resolve()
    server._last_export_path = str(tmp_path / "outside")
    with pytest.raises(gr.Error, match="작업공간 안이 아니다"):
        server.review_export_open_view(signature)
    server._last_export_path = destination_rel
    with pytest.raises(gr.Error, match="절대 경로"):
        server.review_export_open_view(signature)
    server._last_export_path = copied

    # 전체 내보내기 — 선택본과 다른 자리에 따로 산다.
    full_md, _ = server.review_export_view(signature, "", True)
    full_job = full_md.split("`")[1]
    full_record = _wait_terminal(server, root, full_job)
    assert full_record.status == "succeeded", full_record.error
    assert server._last_export_dir != destination_rel

    # 캐시 정리 — 확정본은 지워지지 않고 재검증이 그대로임을 증명한다.
    server._last_export_dir = destination_rel
    server._last_export_path = copied
    with pytest.raises(gr.Error, match="정리 확인이 필요하다"):
        server.review_cleanup_view(signature, False)
    cleanup = server.review_cleanup_view(signature, True)
    assert "지운 파일" in cleanup and "재검증 **통과**" in cleanup
    assert _tree_hashes(root, destination_rel) == frozen


@needs_ffmpeg
def test_export_confirmed_binds_expected_revision_backend(
    tmp_path: Path, spoken_media: Path
) -> None:
    """백엔드 경합 — 제출 때의 리비전 아이디가 아니면 확정 복사는 0바이트로 닫힌다."""
    server = _new_server(tmp_path)
    signature = _open(server, tmp_path / "ws")
    root = server.workspace_root()
    assert root is not None
    _prepare(server, root, spoken_media, with_analysis=False)
    server.review_initialize_view(signature)
    server.review_refresh_view(signature)
    facade = server.raw.review_workspace_facade(root)
    loaded = facade.load_head()
    revision = loaded.revision
    package_dir = server._latest_package_dir(root)
    assert package_dir is not None

    # 제출 때 본 아이디 그대로면 성공 — 그 리비전으로 복사·기록까지 끝난다.
    outcome = facade.export_confirmed(package_dir, expected_review_revision_id=revision.revision_id)
    facade.verify_export(outcome.destination_dir)
    assert (root / outcome.destination_dir).is_dir()

    # 경합: 머리가 옮긴 뒤 낡은 아이디로 내보내면 복사 전에 거부된다.
    target = revision.segments[0].segment_id
    edited = facade.apply_edit(
        revision.base_plan_id,
        raw_service.review_note_edit(revision.revision_id, target, "경합 편집"),
    )
    exports_before = sorted(
        path.name for path in (root / "clips").glob("confirmed_reviewexport_*") if path.is_dir()
    )
    with pytest.raises(raw_service.StaleRevisionError, match="다르다"):
        facade.export_confirmed(package_dir, expected_review_revision_id=revision.revision_id)
    exports_after = sorted(
        path.name for path in (root / "clips").glob("confirmed_reviewexport_*") if path.is_dir()
    )
    assert exports_after == exports_before  # 실패한 시도는 아무 폴더도 남기지 않는다

    # 새 머리 아이디로는 성공하고, 기록 행도 그 아이디를 못 박는다.
    outcome2 = facade.export_confirmed(package_dir, expected_review_revision_id=edited.revision_id)
    with sqlite3.connect(root / "project.sqlite3") as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT destination_dir, review_revision_id FROM confirmed_export_revisions"
            " ORDER BY rowid"
        ).fetchall()
    by_dir = {str(row["destination_dir"]): str(row["review_revision_id"]) for row in rows}
    assert by_dir[outcome.destination_dir] == revision.revision_id
    assert by_dir[outcome2.destination_dir] == edited.revision_id


# ── DB v6 — 저장소 스키마·v5 승격·출처 열 ───────────────────────────────────


def test_db_v6_schema_and_v5_upgrade(tmp_path: Path) -> None:
    """중앙 마이그레이션이 W3 표를 만들고 v5 작업공간을 v6으로 올린다(W2 표는 D163에 지웠다)."""
    service = raw_service.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "스키마확인")
    expected_tables = {
        "review_workspace_revisions",
        "review_workspace_heads",
        "review_workspace_remappings",
        "confirmed_export_revisions",
    }
    with sqlite3.connect(root / DB_NAME) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        assert expected_tables <= tables
        assert "caption_responses" not in tables  # W2 자막 저장소는 지웠다(D163)
        connection.execute(
            "UPDATE meta SET value = 'raw-workspace-db/5' WHERE key = 'db_schema_version'"
        )

    # 승격은 **새 서버(재시작)** 경로에서 일어난다 — 돌아가는 서비스의 캐시가
    # 버전을 바꾸는 것이 아니라, 다음 열기가 덧붙이기 마이그레이션을 돌린다.
    restarted = raw_service.RawWorkspaceService()
    status = restarted.open_workspace(root)  # v5 → v6 승격
    assert status.db_schema_version == "raw-workspace-db/6"
    with sqlite3.connect(root / DB_NAME) as connection:
        version = connection.execute(
            "SELECT value FROM meta WHERE key = 'db_schema_version'"
        ).fetchone()[0]
        assert version == "raw-workspace-db/6"
        tables_after = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
    assert expected_tables <= tables_after  # 승격이 표를 지우거나 비우지 않는다


# ── CLI 진단 — 로컬만, 계약 위반은 정직하게 ─────────────────────────────────


def test_cli_review_closes_honestly(tmp_path: Path) -> None:
    """`media review` — 계획이 없으면 오류 안내로 끝난다(`media caption`은 D163에 지웠다)."""
    import argparse as argparse_module

    from vlogsrt.project.raw import commands as raw_commands

    root = tmp_path / "ws"
    raw_service.RawWorkspaceService().create_workspace(root, "제주")
    review_ns = argparse_module.Namespace(workspace=str(root), init=True)
    assert raw_commands.cmd_review(review_ns) == 1
