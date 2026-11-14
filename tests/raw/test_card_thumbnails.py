"""목록 썸네일(W-24 세션 I, D64) — 영상 카드는 그림을 가진다.

실측 18개 가운데 0024(33컷)만 썸네일이 하나도 없었다. 준비가 원본마다 첫 썸네일 앞에서 원본 전체
미리보기를 인코딩했고, 그 인코딩이 매번 한도·서버 멈춤에 끊겼다. 썸네일은 영상을 고를 때만
만들어서 다시 시도하는 길도 없었다. 카드의 그림은 대표 컷의 썸네일 하나뿐이라 첫 컷의 썸네일이
지워지면 다른 컷의 썸네일이 있어도 「낡음」이었다.

- 카드 그림(`cardThumb`)은 대표 컷의 그릴 수 있는 썸네일, 없으면 그 영상의 다른 컷 것이다.
  파일만 있고 등록이 없는(주소 없는) 썸네일은 그릴 수 없다.
- 준비는 썸네일을 먼저 만들고 원본 전체 미리보기는 그 뒤다 — 미리보기가 실패해도 썸네일은 있다.
- 라이브러리를 열면(정리 고리) 첫 컷 썸네일이 없는 영상만 그것 하나씩 만든다 — 원본 전체 미리보기
  없이, 실행마다 썸네일마다 한 번. 「라이브러리 닫기」가 취소하고 다시 열면 남은 것을 만든다.
- 레일은 준비됨인 썸네일의 주소만 부른다(낡음·실패의 옛 주소는 404다).

유료 호출 0회.
"""

from __future__ import annotations

import json
import subprocess
import threading
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_visual_review import _review_ready, _voiced_media  # noqa: E402
from tests.raw.test_visual_workbench_ui import (  # noqa: E402
    _NODE_HARNESS,
    _model_of,
    _new_server,
    _wait_terminal,
    needs_ffmpeg,
    needs_node,
)
from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt import preprocess  # noqa: E402
from vlogsrt.project.raw import visual_review  # noqa: E402
from vlogsrt.project.raw.models import VISUAL_PREPARE_JOB_KIND  # noqa: E402

SOURCES = 3


def _workbench(tmp_path: Path) -> tuple[workspace_service.RawWorkspaceUi, Path]:
    """원본 셋(3·4·5초 · 1초 타일 → 컷 열둘)의 검토 머리까지 — 썸네일은 아직 없다."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    card = tmp_path / "card"
    card.mkdir()
    for index in range(SOURCES):
        # 길이가 달라야 내용(해시)이 다르다 — 같은 바이트면 썸네일 키도 같다.
        _voiced_media(card / f"clip_{index:02d}.mp4", str(3 + index))
    _review_ready(server.raw, root, card, Fraction(1))
    return server, root


def _media_cards(server: workspace_service.RawWorkspaceUi, root: Path) -> list[dict[str, Any]]:
    return list(_model_of(server.visual_workbench_render(root)[1])["mediaCards"])


def _thumb_jobs(server: workspace_service.RawWorkspaceUi, root: Path) -> list[Any]:
    return [
        job
        for job in server.raw.list_jobs(root)
        if job.kind == VISUAL_PREPARE_JOB_KIND
        and job.input_fingerprint.startswith(visual_review.REPRESENTATIVE_THUMBS_PREFIX)
    ]


def _overview(server: workspace_service.RawWorkspaceUi, root: Path) -> Any:
    return server.raw.visual_review_query(root, include_all_sources=True)


def _wait_idle(server: workspace_service.RawWorkspaceUi, root: Path) -> None:
    for job in server.raw.all_active_jobs(root):
        _wait_terminal(server, root, job.job_id)


def _prepare_all(server: workspace_service.RawWorkspaceUi, root: Path) -> None:
    handle = server.raw.start_visual_page_preparation(root, page=1)
    record = _wait_terminal(server, root, handle.job_id)
    assert record.status == "succeeded", record.error


# ── 카드 그림 — 대표 컷 것, 없으면 그 영상의 다른 컷 것 ─────────────────────────


@needs_ffmpeg
def test_card_shows_another_cuts_thumb_when_the_first_cut_thumb_is_gone(tmp_path: Path) -> None:
    server, root = _workbench(tmp_path)
    _prepare_all(server, root)
    overview = _overview(server, root)
    first = overview.representatives[0]
    (root / first.thumb_relative_path).unlink()  # 첫 컷의 썸네일만 지운다(등록은 남는다)
    second = next(
        card
        for card in _overview(server, root).segments
        if card.source_id == first.source_id and card.segment_id != first.segment_id
    )

    cards = _media_cards(server, root)

    assert cards[0]["id"] == first.segment_id  # 카드가 고르는 컷은 그대로다(D64)
    assert cards[0]["thumb"]["state"] == "stale"
    assert cards[0]["cardThumb"] == f"/media/{second.thumb_registered_file_id}"
    for card in cards[1:]:  # 나머지 영상은 제 대표 컷의 썸네일이다
        assert card["cardThumb"] == card["thumb"]["src"] and card["thumb"]["state"] == "ready"


@needs_ffmpeg
def test_a_thumb_file_without_registration_is_not_drawn(tmp_path: Path) -> None:
    server, root = _workbench(tmp_path)
    _prepare_all(server, root)
    workspace = server.raw._workspace(root)
    overview = _overview(server, root)
    for card in overview.segments:  # 첫 영상의 컷 셋 — 파일은 두고 등록만 지운다
        if card.source_id == overview.representatives[0].source_id:
            assert workspace.discard_media_registration(card.thumb_relative_path)

    cards = _media_cards(server, root)

    assert cards[0]["thumb"] == {"state": "ready", "src": None}  # 파일은 있다 — 주소가 없다
    assert cards[0]["cardThumb"] is None  # 그릴 것이 없다 — 「준비됨」 글자를 보이지 않는다
    assert all(card["cardThumb"] for card in cards[1:])


# ── 준비 — 썸네일이 먼저, 원본 전체 미리보기는 그 뒤 ─────────────────────────────


@needs_ffmpeg
def test_a_failing_full_preview_still_leaves_every_thumbnail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root = _workbench(tmp_path)
    calls: list[str] = []
    real_frame = preprocess.extract_frame_index

    def frame(*args: Any, **kwargs: Any) -> Any:
        calls.append("thumb")
        return real_frame(*args, **kwargs)

    def preview(*args: Any, **kwargs: Any) -> Any:
        calls.append("preview")
        raise preprocess.ProcessTimeout(["ffmpeg"], 1.0, "원본 전체 미리보기")

    monkeypatch.setattr(preprocess, "extract_frame_index", frame)
    monkeypatch.setattr(preprocess, "extract_visual_review_preview", preview)

    handle = server.raw.start_visual_page_preparation(root, page=1)
    record = _wait_terminal(server, root, handle.job_id)

    assert record.status == "failed" and "원본 전체 미리보기" in (record.error or "")
    segments = _overview(server, root).segments
    assert len(segments) == 3 + 4 + 5
    assert all(card.thumb_state == "ready" for card in segments)
    assert calls == ["thumb"] * len(segments) + ["preview"] * SOURCES  # 썸네일이 다 먼저다
    assert (record.progress_current, record.progress_total) == (len(segments) + SOURCES,) * 2


# ── 라이브러리 열기 — 첫 컷 썸네일만, 실행마다 한 번 ───────────────────────────────


@needs_ffmpeg
def test_opening_makes_first_cut_thumbnails_for_every_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root = _workbench(tmp_path)
    encodes: list[str] = []
    monkeypatch.setattr(
        preprocess, "extract_visual_review_preview", lambda *a, **k: encodes.append("x")
    )
    assert all(card["cardThumb"] is None for card in _media_cards(server, root))
    signature = server.open_workspace_view(str(root))[1]

    server.capcut_refresh_view(signature)  # 열기 뒤 첫 갱신 — 정리 고리가 돈다
    (job,) = _thumb_jobs(server, root)
    record = _wait_terminal(server, root, job.job_id)

    assert record.status == "succeeded", record.error
    assert record.progress_total == SOURCES
    cards = _media_cards(server, root)
    assert len(cards) == SOURCES
    assert all(card["thumb"]["state"] == "ready" and card["cardThumb"] for card in cards)
    assert encodes == []  # 원본 전체 미리보기는 만들지 않는다
    others = [c for c in _overview(server, root).segments if c.segment_order > 0]
    assert all(card.thumb_state != "ready" for card in others)  # 첫 컷 것만 만든다
    server.capcut_refresh_view(signature)
    assert len(_thumb_jobs(server, root)) == 1  # 없는 것이 없으면 걸지 않는다


@needs_ffmpeg
def test_a_failing_thumbnail_is_tried_once_per_server_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, root = _workbench(tmp_path)
    attempts: list[int] = []

    def broken(*args: Any, **kwargs: Any) -> None:
        attempts.append(1)
        raise RuntimeError("프레임 추출 실패(시험)")

    monkeypatch.setattr(preprocess, "extract_frame_index", broken)
    handle = server.raw.start_representative_thumbnails(root)
    assert handle is not None
    record = _wait_terminal(server, root, handle.job_id)
    assert record.status == "failed" and "첫 컷 썸네일 실패 3건" in (record.error or "")

    for _ in range(3):  # 그리기마다 고리가 돌아도 다시 걸지 않는다
        assert server.raw.start_representative_thumbnails(root) is None
    signature = server.current_signature()
    server.capcut_refresh_view(signature)
    _wait_idle(server, root)
    assert len(attempts) == SOURCES and len(_thumb_jobs(server, root)) == 1
    assert all(card["thumb"]["state"] == "failed" for card in _media_cards(server, root))


@needs_ffmpeg
def test_closing_the_library_cancels_it_and_reopening_finishes_the_rest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    server, root = _workbench(tmp_path)
    server.build_blocks()
    entered, release = threading.Event(), threading.Event()
    real = visual_review._first_cut_thumb

    def held(*args: Any, **kwargs: Any) -> None:
        if not entered.is_set():
            entered.set()
            release.wait(timeout=20)
        real(*args, **kwargs)

    monkeypatch.setattr(visual_review, "_first_cut_thumb", held)
    signature = server.open_workspace_view(str(root))[1]
    server.capcut_refresh_view(signature)
    assert entered.wait(timeout=20)
    (job,) = _thumb_jobs(server, root)

    reply = server.close_library_view(signature)[0]
    release.set()
    record = _wait_terminal(server, root, job.job_id)

    assert reply == workspace_service.LIBRARY_CLOSED_TEXT  # 닫기를 막지 않는다
    assert record.status == "cancelled"
    signature = server.open_workspace_view(str(root))[1]
    server.capcut_refresh_view(signature)
    again = [job for job in _thumb_jobs(server, root) if job.job_id != record.job_id]
    # 끝내지 못한 썸네일(취소가 그 자리의 ffmpeg째 끊은 첫 영상 포함)을 모두 다시 건다.
    assert len(again) == 1 and again[0].progress_total == SOURCES
    _wait_idle(server, root)
    assert all(card["cardThumb"] for card in _media_cards(server, root))


# ── 실행본 — 카드는 서버가 고른 그림, 레일은 준비됨만 ─────────────────────────────

_THUMB_SCRIPT = r"""
const tState = () => windowScope.__vlVisualWorkbenchState;
tState().selectedId = null; tState().selectedPlan = null; tState().marker = null;
tState().lastMsg = ''; tState().filter = 'all';
const thumbModel = visualModel();
// 영상 A — 첫 컷의 썸네일은 낡음(옛 주소), 둘째는 실패(옛 주소), 셋째만 준비됨.
thumbModel.cards[0].thumb = { state: 'stale', src: '/media/old-a1' };
thumbModel.cards[1].thumb = { state: 'failed', src: '/media/old-a2' };
const videos = thumbModel.mediaCards;
videos[0].cardThumb = '/media/thumb-c';  // 서버가 고른 다른 컷의 그림
videos[1].thumb = { state: 'ready', src: null };  // 파일만 있고 주소가 없다
videos[1].cardThumb = null;
videos[2].cardThumb = null;  // 준비 중 — 그릴 것이 없다
thumbModel.mediaCards = videos;
const envThumb = visualEnv({}, thumbModel);
const railButtons = envThumb.rail.innerHTML.split('<button').filter((b) =>
  b.includes('data-vl-interval='));
const cardsHtml = envThumb.thumbs.innerHTML.split('<button').filter((b) =>
  b.includes('data-vl-card='));
checkpoints.push({ case: 'card-thumbs', rail: railButtons, cards: cardsHtml });
"""


def _run_thumb_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "thumbs.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _THUMB_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps(
            {"visual": ui_facade.VISUAL_BOOTSTRAP_JS, "caption": ui_facade.CAPTION_BOOTSTRAP_JS}
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "card-thumbs")


@needs_node
def test_cards_draw_the_server_choice_and_the_rail_skips_stale_addresses(
    tmp_path: Path,
) -> None:
    report = _run_thumb_harness(tmp_path)
    a, b, c = report["cards"]
    assert '<img src="/media/thumb-c"' in a  # 대표 컷은 낡음이어도 다른 컷의 그림
    # 화면 밖 카드의 그림은 스크롤해 올 때 받는다 — 첫 화면이 영상 수만큼 그림을 받지 않는다(D133).
    assert 'loading="lazy"' in a.split("<img", 1)[1].split(">", 1)[0]
    assert "<img" not in b and ">아직 없음</span>" in b  # 주소 없는 준비됨은 그리지 않는다
    assert "<img" not in c and ">준비 중</span>" in c
    rail = report["rail"]
    assert len(rail) == 3
    assert all("old-a" not in button for button in rail)  # 옛 주소를 부르지 않는다(404)
    assert all("background-image:url('/media/thumb-c')" in button for button in rail)


def test_the_thumbnail_hook_does_not_block_drawing_on_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """고리의 오류는 경고로 남고 그리기는 돈다(다른 정리 고리와 같다)."""
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None

    def broken(_root: Path) -> None:
        raise visual_review.VisualReviewError("시험 오류")

    monkeypatch.setattr(server.raw, "start_representative_thumbnails", broken)
    assert server._maybe_thumb_upkeep(root) is False


@needs_ffmpeg
def test_a_cancel_by_hand_sticks_until_the_library_is_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    """사람이 취소한 썸네일 작업을 다음 고리가 곧바로 다시 걸지 않는다 — 닫고 열면 다시 건다."""
    server, root = _workbench(tmp_path)
    server.build_blocks()
    entered, release = threading.Event(), threading.Event()
    real = visual_review._first_cut_thumb

    def held(*args: Any, **kwargs: Any) -> None:
        if not entered.is_set():
            entered.set()
            release.wait(timeout=20)
        real(*args, **kwargs)

    monkeypatch.setattr(visual_review, "_first_cut_thumb", held)
    signature = server.open_workspace_view(str(root))[1]
    server.capcut_refresh_view(signature)
    assert entered.wait(timeout=20)
    (job,) = _thumb_jobs(server, root)
    server.raw.cancel_job(root, job.job_id)
    release.set()
    assert _wait_terminal(server, root, job.job_id).status == "cancelled"

    server.capcut_refresh_view(signature)
    assert server.raw.start_representative_thumbnails(root) is None
    assert len(_thumb_jobs(server, root)) == 1  # 취소가 듣는다 — 다시 걸지 않았다

    server.close_library_view(signature)
    signature = server.open_workspace_view(str(root))[1]
    server.capcut_refresh_view(signature)
    _wait_idle(server, root)
    assert len(_thumb_jobs(server, root)) == 2
    assert all(card["cardThumb"] for card in _media_cards(server, root))
