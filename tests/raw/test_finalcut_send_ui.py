"""「Final Cut으로 보내기」 — 한 단추가 전체 타임라인을 쓰고 Final Cut에 건넨다(W-16 C, D56).

옛 두 단계(확정 패키지를 뽑아 XML을 쓰는 단추 + 따로 여는 단추)를 대신한다.
**시험은 실제 Final Cut을 절대 열지 않는다** — 열면 사용자의 진짜 라이브러리로 가져오기가
일어난다. 그래서 언제나 가짜 opener를 넣고, `tests/conftest.py`가 실제 열기 자리를 막는다.
"""

from __future__ import annotations

import dataclasses
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_finalcut_export import _plain_project
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import preprocess

pytestmark = [
    pytest.mark.skipif(
        shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
        reason="ffmpeg/ffprobe가 없는 환경",
    ),
]
gr = pytest.importorskip("gradio", reason="웹 UI extra(gradio)가 없다", exc_type=ImportError)


def _server(tmp_path: Path, opened: list[Path]) -> tuple[Any, str, Path]:
    from ui.workspace_service import RawWorkspaceUi

    _service, root = _plain_project(tmp_path)
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json", finalcut_opener=opened.append)
    signature = server.open_workspace_view(str(root))[1]
    return server, signature, root


def _send_job(server: Any, root: Path) -> Any:
    from ui.workspace_service import FINALCUT_SEND_JOB_KIND

    job = next(j for j in server.raw.list_jobs(root) if j.kind == FINALCUT_SEND_JOB_KIND)
    return _wait_terminal(server.raw, root, job.job_id)


def test_send_writes_the_timeline_and_hands_the_fcpxml_to_the_opener(tmp_path: Path):
    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    message = server.finalcut_send_view(signature)
    assert "Final Cut" in message
    finished = _send_job(server, root)
    assert finished.status == "succeeded", finished.error
    (path,) = opened
    folder = (root / "review" / "finalcut").resolve()
    assert path.resolve().parent == folder and path.suffix == ".fcpxml" and path.is_file()
    assert path.with_suffix(".json").is_file()
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보냈습니다" in status
    assert "원본 1개" in status and "넣지 않은 원본 0개" in status and path.name in status
    # 「폴더 열기」는 보낸 폴더를 가리킨다(가짜 Finder).
    assert server._last_export_path == str(path.parent)
    finder: list[Path] = []
    server._finder_opener = finder.append
    server.review_export_open_view(signature)
    assert finder == [folder]


def test_refusal_names_the_file_and_explains_vfr_in_plain_korean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    real = preprocess.probe_extraction_clock

    def vfr(path: Path, **kwargs: Any) -> Any:
        return dataclasses.replace(real(path, **kwargs), uniform_grid=False, grid_note="간격")

    with monkeypatch.context() as patch:
        patch.setattr(preprocess, "probe_extraction_clock", vfr)
        server.finalcut_send_view(signature)
        failed = _send_job(server, root)
    assert failed.status == "failed" and opened == []
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보내지 못했습니다" in status and "plain.mov" in status
    assert "가변 프레임(VFR)" in status and "아이폰" in status
    # 실패 뒤 「실패 구간 재시도」는 같은 보내기를 다시 건다 — 이번에는 통과한다.
    server.capcut_retry_view(signature)
    jobs = [j for j in server.raw.list_jobs(root) if j.kind == "finalcut_send"]
    assert len(jobs) == 2
    assert _wait_terminal(server.raw, root, jobs[0].job_id).status == "succeeded"
    assert len(opened) == 1


def test_a_caption_timing_lookup_failure_still_sends_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """`자막 시각 확인 필요` 표지는 표시 전용이다 — 못 만들어도 보내기는 막지 않고, 키워드 없이
    보냈다는 것과 그 이유를 결과에 보인다(조용히 빠지지 않는다)."""

    def broken(*_: Any, **__: Any) -> Any:
        raise ValueError("보고서가 깨졌다")

    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    monkeypatch.setattr(server.raw, "caption_timing_flags", broken)
    server.finalcut_send_view(signature)
    finished = _send_job(server, root)
    assert finished.status == "succeeded", finished.error
    assert len(opened) == 1
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보냈습니다" in status
    assert "자막 시각 확인 필요" in status and "ValueError: 보고서가 깨졌다" in status


def test_a_failed_open_is_reported_with_the_written_file(tmp_path: Path):
    """쓰기는 끝났는데 Final Cut이 열리지 않았다 — 성공처럼 보이지 않고, 쓴 파일 이름을 댄다."""
    from vlogsrt.project.raw.service import RawWorkspaceError

    def broken_opener(path: Path) -> None:
        raise RawWorkspaceError("open 실패(코드 1)")

    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    server._finalcut_opener = broken_opener
    server.finalcut_send_view(signature)
    assert _send_job(server, root).status == "failed"
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보내지 못했습니다" in status and "열지 못했습니다" in status
    assert ".fcpxml" in status and "보냈습니다 ·" not in status


def test_the_real_opener_is_one_open_call_and_tests_can_never_reach_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import ui.workspace_service as workspace_ui

    target = tmp_path / "x.fcpxml"
    # 가드가 없으면(`--noconftest`) 아래 호출이 진짜 열기가 된다 — 부르기 전에 멈춘다.
    if getattr(workspace_ui.open_in_final_cut, "__wrapped__", None) is None:
        pytest.fail("conftest의 Final Cut 가드가 없다 — 실제 열기를 부르지 않고 멈춘다")
    # conftest가 실제 열기 자리 셋을 모두 막아 두었다 — 메서드, 모듈 함수, 그리고 "Final Cut Pro"를
    # 실은 모든 subprocess 호출(실행 전에 멈춘다).
    with pytest.raises(AssertionError, match="가짜"):
        workspace_ui.RawWorkspaceUi._system_finalcut_open(target)
    with pytest.raises(AssertionError, match="가짜"):
        workspace_ui.open_in_final_cut(target)
    with pytest.raises(AssertionError, match="Final Cut"):
        subprocess.run(["/usr/bin/true", "Final Cut Pro", str(target)], check=False)
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    from vlogsrt.project.raw.service import RawWorkspaceError

    if sys.platform != "darwin":
        # Final Cut Pro는 macOS에만 있다 — 다른 운영체제에서는 열기를 부르지 않고
        # 까닭을 댄다(W-84a).
        with pytest.raises(RawWorkspaceError, match="macOS에만"):
            workspace_ui.open_in_final_cut.__wrapped__(target)
        assert calls == []
        return
    workspace_ui.open_in_final_cut.__wrapped__(target)  # 막기 전 원래 함수 — argv만 본다
    assert calls == [["/usr/bin/open", "-a", "Final Cut Pro", str(target)]]


def test_the_real_opener_explains_that_final_cut_is_macos_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Windows에는 `/usr/bin/open`도 Final Cut도 없다 — FileNotFoundError 대신 사유(D100 §1b)."""
    import sys

    import ui.workspace_service as workspace_ui
    from vlogsrt.project.raw import service as raw_service

    monkeypatch.setattr(subprocess, "run", lambda *_a, **_k: pytest.fail("띄우지 않는다"))
    monkeypatch.setattr(sys, "platform", "win32")
    target = tmp_path / "x.fcpxml"
    with pytest.raises(raw_service.RawWorkspaceError, match="macOS에만"):
        workspace_ui.open_in_final_cut.__wrapped__(target)


def _failed_job(root: Path, kind: str) -> str:
    from vlogsrt.project.raw.jobs import JobStore
    from vlogsrt.project.raw.workspace import RawWorkspace

    store = JobStore(RawWorkspace.open(root))
    record, _ = store.insert_deduplicated(kind, f"old-{kind}", 0)
    store.update(record.job_id, status="failed", error="옛 실패")
    return record.job_id


def test_retry_of_an_old_selected_cuts_job_points_at_the_send_button_and_never_sends(
    tmp_path: Path,
):
    """옛 「선택한 컷」 출력 작업의 재시도가 전체 타임라인을 몰래 보내고 Final Cut을 열면
    안 된다."""
    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    _failed_job(root, "capcut_export")
    message = server.capcut_retry_view(signature)
    assert "Final Cut으로 보내기" in message
    assert not [j for j in server.raw.list_jobs(root) if j.kind == "finalcut_send"]
    assert opened == []


@pytest.mark.parametrize(
    ("where", "error"),
    [("export", OSError("디스크가 가득 찼다")), ("opener", ValueError("알 수 없는 열기 실패"))],
)
def test_any_failure_is_shown_as_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str, error: Exception
):
    """어떤 종류의 예외든 실패한 보내기는 「보냈습니다」로 보이지 않는다 — 사유를 그대로 보인다."""

    def broken(*_: Any, **__: Any) -> Any:
        raise error

    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    if where == "export":
        monkeypatch.setattr(server.raw, "export_finalcut_timeline", broken)
    else:
        server._finalcut_opener = broken
    server.finalcut_send_view(signature)
    assert _send_job(server, root).status == "failed"
    status = str(server.capcut_status_view(signature))
    assert "Final Cut으로 보내지 못했습니다" in status and str(error) in status
    assert "보냈습니다 ·" not in status


def test_send_button_replaces_the_old_two_step_labels(tmp_path: Path, local_socket: None):
    from ui.workspace_service import FINALCUT_SEND_LABEL, RawWorkspaceUi

    app = RawWorkspaceUi(state_path=tmp_path / "ui-state.json").build_blocks()
    labels = [str(b.value) for b in app.blocks.values() if isinstance(b, gr.Button)]
    assert FINALCUT_SEND_LABEL == "Final Cut으로 보내기" and FINALCUT_SEND_LABEL in labels
    # 옛 이름은 조각을 이어 쓴다 — 저장소 grep 인수(옛 이름 0건)가 이 시험에 걸리지 않게.
    assert "선택한 컷 " + "Final Cut으로 출력" not in labels
    assert "Final Cut에서 " + "열기" not in labels
    assert not hasattr(RawWorkspaceUi, "capcut_" + "handoff_view")
    assert not hasattr(RawWorkspaceUi, "capcut_" + "open_view")


# ─── D59 — 보낸 결과가 자막이 비어 있는 곳을 이름으로 말한다 ─────────────────────


def _honest_report() -> dict[str, Any]:
    """실측 09:17:44 보내기의 꼴 — 0008 큐 0개, 0018 실패 청크, 0011은 확인을 기다린다."""
    return {
        "sources": [
            {"asset_id": "a8", "name": "DJI_0008_D", "captions": False, "caption_gaps": []},
            {"asset_id": "a11", "name": "DJI_0011_D", "captions": True, "caption_gaps": []},
            {
                "asset_id": "a18",
                "name": "DJI_0018_D",
                "captions": True,
                "caption_gaps": [{"start": "1260/1", "end": "4077073/3000", "reason": "x"}],
            },
            {"asset_id": "a19", "name": "DJI_0019_D", "captions": True, "caption_gaps": None},
        ],
        "sync_check": {
            "rule": "current_red_only",
            "running": True,
            "missing": [
                {"source_id": "a8", "name": "DJI_0008_D.mov"},
                {"source_id": "a11", "name": "DJI_0011_D.mov"},
            ],
            "stale": [{"source_id": "a19", "name": "DJI_0019_D.mov"}],
            "flagged": [],
        },
    }


def test_send_facts_pick_empty_sources_failed_spans_and_unchecked_timing() -> None:
    from ui.workspace_service import _send_caption_facts

    facts = _send_caption_facts(_honest_report())
    assert facts == {
        "no_captions": ["DJI_0008_D"],
        "caption_gaps": [{"name": "DJI_0018_D", "spans": "1260–1359초"}],
        "unknown_gaps": ["DJI_0019_D"],  # 자막은 있는데 전사 기록이 없다 — '없음'이 아니라 '모름'
        "unchecked": ["DJI_0011_D", "DJI_0019_D"],  # 자막 없는 0008은 확인을 기다리지 않는다
        "sync_running": True,
    }


def test_send_text_says_it_politely_and_without_prices() -> None:
    from ui.workspace_service import _finalcut_sent_text, _send_caption_facts

    sent = {
        "path": "/x/vlogsrt_20261011T001744Z.fcpxml",
        "counts": {"sources": 4, "spine_clips": 9, "disabled_clips": 0, "unreviewed_clips": 0},
        "not_exported": 0,
        **_send_caption_facts(_honest_report()),
    }
    text = _finalcut_sent_text(sent)
    assert "자막이 없는 영상 1개는 자막 없이 보냈습니다: DJI_0008_D." in text
    assert "전사가 실패해 자막이 빠진 구간이 있는 영상 1개: DJI_0018_D(1260–1359초)" in text
    assert "전사 기록이 없어 빠진 구간을 알 수 없는 영상 1개: DJI_0019_D." in text
    assert (
        "시각 확인이 끝나지 않은 영상 2개(DJI_0011_D, DJI_0019_D)에는 「자막 시각 확인 필요」"
        " 키워드가 붙지 않았습니다 — 확인이 끝난 뒤 다시 보내면 빨강으로 나온 영상에만 붙습니다."
        " 지금 확인하는 중입니다."
    ) in text
    assert "$" not in text and "달러" not in text
    quiet = _finalcut_sent_text(
        {**sent, "no_captions": [], "caption_gaps": [], "unknown_gaps": [], "unchecked": []}
    )
    assert "자막이 없는" not in quiet and "시각 확인이 끝나지" not in quiet
    assert "알 수 없는" not in quiet
    old = {k: v for k, v in sent.items() if k in ("path", "counts", "not_exported")}
    assert _finalcut_sent_text(old).startswith("**Final Cut으로 보냈습니다**")  # 예전 기억도 읽힌다


def test_send_text_names_the_clips_conformed_to_the_project_rate() -> None:
    """프레임레이트가 섞이면 많은 쪽 레이트 하나로 보낸다(D71) — 맞춰 재생되는 영상은 이름으로."""
    from ui.workspace_service import _finalcut_sent_text

    counts = {"sources": 3, "spine_clips": 5, "disabled_clips": 0, "unreviewed_clips": 0}
    sent = {
        "path": "/x/vlogsrt_20261018T001744Z.fcpxml",
        "counts": {**counts, "frame_rate": "29.97", "conformed_sources": ["IMG_8852"]},
        "not_exported": 0,
    }
    assert (
        "프레임레이트가 다른 영상 1개(IMG_8852)는 프로젝트 29.97fps에 맞춰 재생됩니다"
        " — 길이·자막은 그대로입니다."
    ) in _finalcut_sent_text(sent)
    single = {**sent, "counts": {**counts, "frame_rate": "30", "conformed_sources": []}}
    assert "프레임레이트" not in _finalcut_sent_text(single)
    old = {**sent, "counts": counts}  # W-27d 앞의 기록에는 칸이 없다
    assert "프레임레이트" not in _finalcut_sent_text(old)
    assert "자막 조각" not in _finalcut_sent_text(sent)  # 손댄 조각이 없으면 말하지 않는다


def test_send_text_says_how_many_caption_pieces_were_left_out_or_clamped() -> None:
    """맞춰 재생되는 클립 끝에서 빼거나 자른 자막 조각도 말없이 두지 않는다(D71, 규칙 9의 정신)."""
    from ui.workspace_service import _finalcut_sent_text

    counts = {
        "sources": 2, "spine_clips": 3, "disabled_clips": 0, "unreviewed_clips": 0,
        "frame_rate": "24", "conformed_sources": ["h60"],
        "conformed_hidden_captions": 1, "conformed_clamped_captions": 2,
    }  # fmt: skip
    text = _finalcut_sent_text({"path": "/x/v.fcpxml", "counts": counts, "not_exported": 0})
    assert (
        "클립 경계에서 한 프레임보다 짧게 남은 자막 조각 1개는 그 클립에서 보이지 않는 자리라"
        " 싣지 않았습니다."
    ) in text
    assert "자막 조각 2개는 클립 끝에서 한 프레임 안쪽으로 잘렸습니다." in text


def test_sending_a_source_without_subtitles_names_it_in_the_result(tmp_path: Path):
    opened: list[Path] = []
    server, signature, root = _server(tmp_path, opened)
    server.finalcut_send_view(signature)
    assert _send_job(server, root).status == "succeeded"
    status = str(server.capcut_status_view(signature))
    assert "자막이 없는 영상 1개는 자막 없이 보냈습니다: plain." in status
    assert "시각 확인이 끝나지 않은" not in status  # 잴 자막이 없으면 기다리는 것도 아니다
