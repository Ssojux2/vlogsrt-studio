"""W-17 C단계(D57) — 제외 제안 대기열 · 필터 「제외 제안」 · 알림 · 문구.

대기열은 그릴 때마다 검토 머리(미검토 표지)와 등급 증거(빨강 구간)에서 다시 센다 — 어디에도
보관하지 않는다(D51·D52와 같은 자리). 일괄 확인은 옛 「자동 제외 확인」과 **같은 처리기 · 같은
서비스 호출**이다 — 자리와 이름만 바뀌었다(W-13 사람 손 규칙 · 실행 취소 · 「전체 복원」 그대로,
D53). 미검토 제안은 「Final Cut으로 보내기」를 막지 않는다(D56). 유료 호출 0회.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from tests.raw import test_creation_order as co  # noqa: E402
from tests.raw.test_review_notes import MACHINE as MACHINE_NOTE  # noqa: E402
from tests.raw.test_review_workspace import _wait_terminal  # noqa: E402
from tests.raw.test_tier_review_gate import tier_project  # noqa: E402
from tests.raw.test_visual_workbench_ui import _model_of  # noqa: E402
from ui import ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    CONFIRM_ALL_SOURCE_LABEL,
    FINALCUT_SEND_JOB_KIND,
    FINALCUT_SEND_LABEL,
    QUICK_APPROVED_TEXT,
    RawWorkspaceUi,
)
from vlogsrt.project.raw import finalcut_timeline  # noqa: E402
from vlogsrt.project.raw.review_workspace import ReviewWorkspaceFacade  # noqa: E402
from vlogsrt.project.raw.segment_review import (  # noqa: E402
    AUTO_EXCLUDED_UNREVIEWED_TAG,
    chronological,
    is_unreviewed_auto_exclusion,
    unreviewed_auto_exclusions,
)

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")
HANGUL = re.compile(r"^[가-힣 ]+$")


# ── 대기열 — 순수 계산(촬영 시각 순, 영상 안에서는 시각 순) ─────────────────────────
#
# M(남음) = 미검토 표지가 남은 컷 — 일괄 확인이 확인하는 것 · 머리줄 수 · 보내기가
# `제외 제안(미검토)` 키워드를 다는 것과 같은 집합이다. N = M + 이미 결정한 제안(자동 제외
# 근거 메모가 남은 컷).
# 컷 경계와 맞지 않는 빨강은 대기열 줄이 아니다 — 그 컷의 결정을 따르고 따로 닫을 길이 없다.

# 근거 메모는 자동 정리가 쓰는 모양 그대로여야 N에 든다(D57 C 고침 2) — 머리말만으로는 안 된다.
AUTO = MACHINE_NOTE


def _rail(*cuts: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "duration": cuts[-1][2],
        "frameSec": "1/25",
        "intervals": [{"id": i, "start": a, "end": b} for i, a, b in cuts],
    }


def _red(start: str, end: str) -> dict[str, Any]:
    return {"start": start, "end": end, "tier": "red"}


def _seg(segment_id: str, source_id: str, notes: str = "") -> SimpleNamespace:
    return SimpleNamespace(segment_id=segment_id, source_id=source_id, notes=notes)


def _queue_sources() -> dict[str, dict[str, Any]]:
    """두 영상 — 사전 순서와 다르게 촬영 시각 순(B가 먼저)으로 넣는다."""
    return {
        "srcB": {
            "sourceName": "B.mov",
            "rail": _rail(("b1", "0", "3"), ("b2", "3", "6"), ("b3", "6", "9")),
            "suggestions": [
                _red("0", "3"),  # 미검토 컷 b1
                _red("3", "6"),  # 사람이 결정한 컷 b2(근거 메모는 남는다)
                _red("61/10", "77/10"),  # 컷 경계와 맞지 않음(사용 중인 b3 안)
                {"start": "77/10", "end": "9", "tier": "green"},
            ],
        },
        "srcA": {
            "sourceName": "A.mov",
            "rail": _rail(("a1", "0", "2"), ("a2", "2", "4")),
            "suggestions": [
                _red("99/50", "201/50")
            ],  # a2를 프레임 안쪽으로 붙인 빨강 · a1은 빨강 없음
        },
    }


def _queue_ordered() -> list[SimpleNamespace]:
    return [
        _seg("b1", "srcB", AUTO),
        _seg("b2", "srcB", AUTO),
        _seg("b3", "srcB", "사람 메모"),
        _seg("a1", "srcA", AUTO),
        _seg("a2", "srcA", "수동 선택 · " + AUTO),
    ]


def test_queue_counts_and_order_follow_shot_time_then_time() -> None:
    sources = _queue_sources()
    pending = {"b1", "a1", "a2"}
    for source in sources.values():
        RawWorkspaceUi._mark_open_reds(source, pending)
    queue = RawWorkspaceUi._proposal_queue(sources, _queue_ordered(), pending)
    assert queue["order"] == ["srcB", "srcA"]  # 사전 순서가 아니라 넣은(촬영 시각) 순서
    # N=4(결정한 b2 포함) · M=K=3(미검토 컷) · 경계와 맞지 않는 빨강 1개는 줄이 아니다.
    assert (queue["total"], queue["open"], queue["confirmable"], queue["loose"]) == (4, 3, 3, 1)
    # 경계와 맞지 않는 빨강이 있는 영상 — 안내 줄이 이름을 댄다(검증 MINOR 2).
    assert queue["looseSources"] == [{"sourceId": "srcB", "sourceName": "B.mov", "count": 1}]
    # 그 수는 한 번만 센다 — 각주(`redUnmaterialized`)도 대기열의 수를 그대로 쓴다(검증 NIT).
    fields = RawWorkspaceUi._proposal_fields(sources, _queue_ordered(), pending)
    assert fields["redUnmaterialized"] == fields["queue"]["loose"] == 1
    assert fields["redPending"] == 3
    assert [(it["sourceId"], it["segmentId"], it["start"], it["end"]) for it in queue["items"]] == [
        ("srcB", "b1", "0", "3"),
        ("srcA", "a1", "0", "2"),  # 빨강 없이 남은 미검토 자동 제외도 줄이다(컷 범위)
        ("srcA", "a2", "2", "4"),
    ]
    first, bare, inner = queue["items"]
    assert (first["redStart"], first["redEnd"]) == ("0", "3") and first["sourceName"] == "B.mov"
    assert "redStart" not in bare
    assert (inner["redStart"], inner["redEnd"]) == ("99/50", "201/50")


def test_an_empty_library_has_an_empty_queue() -> None:
    assert RawWorkspaceUi._proposal_queue({}, [], set()) == {
        "total": 0,
        "open": 0,
        "confirmable": 0,
        "loose": 0,
        "looseSources": [],
        "order": [],
        "items": [],
    }
    decided = {"srcA": {"sourceName": "A", "rail": _rail(("a1", "0", "2")), "suggestions": []}}
    decided["srcA"]["suggestions"] = [_red("0", "2")]
    RawWorkspaceUi._mark_open_reds(decided["srcA"], set())
    queue = RawWorkspaceUi._proposal_queue(decided, [_seg("a1", "srcA", AUTO)], set())
    assert (queue["total"], queue["open"], queue["confirmable"], queue["items"]) == (1, 0, 0, [])


def test_red_spans_follow_their_cut_and_loose_reds_never_stay_open() -> None:
    """빨강의 `open`은 그 가운데를 덮는 컷이 미검토일 때뿐이다 — 경계와 맞지 않는 빨강도 그 컷을
    따른다. 옛 규칙(경계와 맞지 않으면 늘 열림)은 어떤 화면 동작으로도 닫히지 않았다(D57 C).
    """
    source = _queue_sources()["srcB"]
    RawWorkspaceUi._mark_open_reds(source, {"b1", "b3"})
    rows = source["suggestions"]
    assert [row.get("open") for row in rows] == [True, False, True, None]
    assert [row.get("loose") for row in rows] == [False, False, True, None]
    RawWorkspaceUi._mark_open_reds(source, {"b1"})  # b3을 결정했다(사용·제외 어느 쪽이든)
    assert [row.get("open") for row in rows] == [True, False, False, None]


@needs_ffmpeg
def test_n_reads_the_note_auto_cleanup_actually_writes(tmp_path: Path) -> None:
    """N이 세는 근거는 자동 정리가 실제로 쓴 메모다 — 머리말이 아니라 근거 모양(D57 C 고침 2)."""
    from vlogsrt.project.raw import review_notes

    _server, _root, _signature, facade = _opened_tier(tmp_path)
    first, proposal, last = _cuts(facade)
    line = review_notes.auto_note_line(proposal.notes)
    assert line is not None and line.machine == proposal.notes and line.trailing == ""
    assert line.provenance["segment_id"] == proposal.segment_id
    assert not review_notes.has_auto_note(first.notes) and not review_notes.has_auto_note(
        last.notes
    )


# ── 실제 작업대 값 — 읽을 때마다 다시 센다 ─────────────────────────────────────────


def _with_tiers(server: RawWorkspaceUi) -> None:
    """작업대가 등급 증거를 읽게 — 선택 분석 기록 대신 지금 분석 리비전을 영상마다 건넨다."""
    original = server._task_views

    def views(root: Path) -> dict[str, dict[str, Any]]:
        summary = server.raw.analysis_overview(root)
        if summary is None:
            return original(root)
        revision = summary.revision.revision_id
        stage = workspace_service.FINISHED_QUICK_STAGES[0]
        return {
            asset.asset_id: {"revision": revision, "stage": stage, "token": "t"}
            for asset in server.raw.list_source_assets(root)
        }

    server._task_views = views  # type: ignore[method-assign]


def _counts(server: RawWorkspaceUi, root: Path) -> tuple[int, int, int, int, int, int]:
    """(N, M, K, 머리줄 수, 작업대 미검토 수, 경계와 맞지 않는 빨강) — 한 번 그린 값에서."""
    model = _model_of(server.visual_workbench_render(root)[1])
    queue = model["queue"]
    header = server._flow_counts(root).proposals
    assert queue["loose"] == model["redUnmaterialized"]  # 대기열 안내와 각주가 같은 수
    per_source = [it["sourceId"] for it in queue["items"]]
    assert sum(per_source.count(sid) for sid in set(per_source)) == queue["open"]  # Σ 카드 ✕
    return (
        queue["total"],
        queue["open"],
        queue["confirmable"],
        header,
        model["redPending"],
        queue["loose"],
    )


def _opened_tier(tmp_path: Path) -> tuple[RawWorkspaceUi, Path, str, ReviewWorkspaceFacade]:
    _, root = tier_project(tmp_path, suitability="usable")
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    signature = server.open_workspace_view(str(root))[1]
    _with_tiers(server)
    return server, root, signature, ReviewWorkspaceFacade(root)


def _cuts(facade: ReviewWorkspaceFacade) -> list[Any]:
    return list(chronological(facade.load_head().revision))


@needs_ffmpeg
def test_the_workbench_value_carries_the_queue_and_recounts_after_a_decision(
    tmp_path: Path,
) -> None:
    server, root, signature, facade = _opened_tier(tmp_path)
    pending = unreviewed_auto_exclusions(facade.load_head().revision)
    assert len(pending) == 1
    model = _model_of(server.visual_workbench_render(root)[1])
    (item,) = model["queue"]["items"]
    assert item["segmentId"] == pending[0].segment_id
    assert (item["start"], item["end"]) == ("83/25", "267/25")  # 컷 범위
    assert (item["redStart"], item["redEnd"]) == ("33/10", "107/10")  # 등급 구간
    # M == K == 머리줄 == 작업대 미검토 수 — 확인 · 실행 취소 · 전체 복원 내내.
    assert _counts(server, root) == (1, 1, 1, 1, 1, 0)
    server.capcut_confirm_auto_view(signature)
    assert _counts(server, root) == (1, 0, 0, 0, 0, 0)  # N은 결정해도 줄지 않는다
    server.capcut_edit_view(signature, "undo")
    assert _counts(server, root) == (1, 1, 1, 1, 1, 0)
    server.capcut_edit_view(signature, "restore")
    assert _counts(server, root) == (1, 0, 0, 0, 0, 0)


@needs_ffmpeg
def test_merge_then_use_then_exclude_never_leaves_an_item_that_cannot_close(
    tmp_path: Path,
) -> None:
    """검증 시나리오 — 미검토 컷을 앞 컷과 합치면 빨강이 컷 경계와 맞지 않게 된다. 옛 대기열은 그
    빨강을 영원히 '남음'으로 셌다(사용으로 되돌려도 제외로 확인해도 닫히지 않았다)."""
    server, root, _signature, facade = _opened_tier(tmp_path)
    first, proposal, _last = _cuts(facade)
    base = facade.load_head().revision.base_plan_id
    facade.merge_segments(base, first.segment_id, proposal.segment_id)
    merged = _cuts(facade)[0]
    assert merged.selection == "excluded"  # 합치면 미검토 제외가 이긴다(옛 규칙 그대로)
    assert _counts(server, root) == (1, 1, 1, 1, 1, 1)  # 합친 컷이 줄 · 빨강은 경계 밖
    facade.set_selection(base, merged.segment_id, "included")  # 「사용으로 되돌리기」
    assert _counts(server, root) == (1, 0, 0, 0, 0, 1)
    assert _model_of(server.visual_workbench_render(root)[1])["queue"]["items"] == []
    facade.set_selection(base, merged.segment_id, "excluded")  # 막대로 연 「제외 유지(확인)」
    assert _counts(server, root) == (1, 0, 0, 0, 0, 1)


@needs_ffmpeg
def test_a_loose_red_inside_an_included_or_excluded_cut_is_not_an_item(tmp_path: Path) -> None:
    server, root, _signature, facade = _opened_tier(tmp_path)
    first, proposal, _last = _cuts(facade)
    base = facade.load_head().revision.base_plan_id
    facade.set_selection(base, proposal.segment_id, "included")  # 사용으로 결정
    facade.merge_segments(base, first.segment_id, proposal.segment_id)  # 사용 컷 안의 빨강
    assert _cuts(facade)[0].selection == "included"
    assert _counts(server, root) == (1, 0, 0, 0, 0, 1)
    model = _model_of(server.visual_workbench_render(root)[1])
    reds = [r for s in model["sources"].values() for r in s.get("suggestions", [])]
    reds = [r for r in reds if r["tier"] == "red"]
    assert [(r["open"], r["loose"]) for r in reds] == [(False, True)]  # 컷(사용)을 따른다
    facade.set_selection(base, _cuts(facade)[0].segment_id, "excluded")  # 제외 컷 안의 빨강
    assert _counts(server, root) == (1, 0, 0, 0, 0, 1)


@needs_ffmpeg
def test_n_stays_when_one_of_two_split_proposals_is_decided(tmp_path: Path) -> None:
    """검증 시나리오 — 빨강 없는 미검토 컷을 결정하자 「3개 중 3개」가 「2개 중 2개」로 줄었다."""
    server, root, _signature, facade = _opened_tier(tmp_path)
    head = facade.load_head().revision
    proposal = _cuts(facade)[1]
    clock = head.clock_for(proposal.source_id)
    middle = (proposal.start_pts + proposal.end_pts) // 2
    split = next(
        p for d in range(5000) for p in (middle + d, middle - d) if clock.is_frame_boundary(p)
    )
    facade.split_segment(head.base_plan_id, proposal.segment_id, split)
    halves = [c for c in _cuts(facade) if c.selection == "excluded"]
    assert len(halves) == 2
    assert _counts(server, root) == (2, 2, 2, 2, 2, 0)
    facade.set_selection(head.base_plan_id, halves[0].segment_id, "excluded")  # 앞 반쪽 확인
    assert _counts(server, root) == (2, 1, 1, 1, 1, 0)


# ── 메모 칸 — 사람 메모만 보이고 고친다. 기계 근거는 글자 그대로 남는다(D57 C 고침 2) ──────


def _card(model: dict[str, Any], segment_id: str) -> dict[str, Any]:
    cards = [*model["cards"], model.get("selectedCard") or {}]
    return next(card for card in cards if card.get("id") == segment_id)


def _notes(facade: ReviewWorkspaceFacade, segment_id: str) -> str:
    return next(s.notes for s in _cuts(facade) if s.segment_id == segment_id)


def _save_memo(server: RawWorkspaceUi, root: Path, segment_id: str, memo: str) -> dict[str, Any]:
    """작업대의 「메모 저장」 — 화면이 본 리비전에 묶인 `vl_apply` 메모 편집(서버 함수)."""
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    model = _model_of(server.visual_workbench_render(root)[1])
    reply = fns["vl_apply"](
        {
            "epoch": model["epoch"],
            "token": model["token"],
            "signature": model["signature"],
            "op": "note",
            "revisionId": model["revisionId"],
            "basePlanId": model["basePlanId"],
            "segmentId": segment_id,
            "note": memo,
            "page": 1,
            "sourceId": _card(model, segment_id)["sourceId"],
            "selectedId": segment_id,
            "playheadSec": "0",
        }
    )
    assert reply["ok"], reply
    return _model_of(reply["html"])


@needs_ffmpeg
def test_the_memo_box_carries_only_the_human_memo(tmp_path: Path) -> None:
    """검증 MINOR 1 — 메모 칸이 `자동 제외: {json}` 날 근거로 미리 채워져 있었다."""
    server, root, _signature, facade = _opened_tier(tmp_path)
    first, proposal, _last = _cuts(facade)
    assert proposal.notes.startswith("자동 제외: {") and first.notes.startswith("확인 필요: ")
    model = _model_of(server.visual_workbench_render(root)[1])
    for card in model["cards"]:
        assert "자동 제외:" not in card["note"] and "확인 필요:" not in card["note"], card
        assert "{" not in card["note"], card
    (auto,) = _card(model, proposal.segment_id)["machine"]
    assert auto["kind"] == "auto" and auto["reason"] == "tier_silence"
    assert (auto["start"], auto["end"]) == ("33/10", "107/10")  # 근거 구간(초)
    (review,) = _card(model, first.segment_id)["machine"]
    assert review == {"kind": "review", "text": first.notes.removeprefix("확인 필요: ")}


@needs_ffmpeg
def test_saving_or_clearing_a_memo_keeps_the_machine_note_and_n(tmp_path: Path) -> None:
    """검증 MINOR 1 — 결정한 제안의 메모 칸을 비우자 근거가 지워져 「2개 중 0개」가 「1개 중
    0개」로 줄었다. 이제 칸은 사람 메모뿐이고, 저장은 근거 줄을 한 글자도 바꾸지 않는다."""
    server, root, signature, facade = _opened_tier(tmp_path)
    proposal = _cuts(facade)[1]
    machine = proposal.notes
    server.capcut_confirm_auto_view(signature)
    assert _counts(server, root) == (1, 0, 0, 0, 0, 0)
    saved = _save_memo(server, root, proposal.segment_id, "원거리 인물 확인함")
    assert _card(saved, proposal.segment_id)["note"] == "원거리 인물 확인함"
    assert _notes(facade, proposal.segment_id) == machine + "\n원거리 인물 확인함"
    assert _counts(server, root) == (1, 0, 0, 0, 0, 0)
    cleared = _save_memo(server, root, proposal.segment_id, "")  # 메모 칸을 비운다
    assert _card(cleared, proposal.segment_id)["note"] == ""
    assert _notes(facade, proposal.segment_id) == machine  # 근거는 글자 그대로
    assert _counts(server, root) == (1, 0, 0, 0, 0, 0)  # N 그대로
    _save_memo(server, root, proposal.segment_id, "")  # 빈 칸을 또 저장해도 그대로
    assert _notes(facade, proposal.segment_id) == machine


@needs_ffmpeg
def test_a_typed_auto_prefix_never_counts_as_a_proposal(tmp_path: Path) -> None:
    """검증 MINOR 1 — 사람이 뺀 컷의 메모에 「자동 제외: 」를 적자 N이 늘었다(부분 문자열 대조)."""
    server, root, _signature, facade = _opened_tier(tmp_path)
    first = _cuts(facade)[0]
    facade.set_selection(facade.load_head().revision.base_plan_id, first.segment_id, "excluded")
    assert _counts(server, root)[0] == 1
    typed = "자동 제외: 이건 내가 뺐다"
    model = _save_memo(server, root, first.segment_id, typed)
    assert _card(model, first.segment_id)["note"] == typed  # 사람 글이라 칸에 그대로 보인다
    assert _notes(facade, first.segment_id) == first.notes + "\n" + typed  # 확인 필요 줄은 그대로
    assert _counts(server, root)[0] == 1  # N 그대로(옛: 2)
    assert _model_of(server.visual_workbench_render(root)[1])["queue"]["total"] == 1


@needs_ffmpeg
def test_an_old_memo_mixed_into_the_machine_note_round_trips(tmp_path: Path) -> None:
    """옛 작업대는 메모 칸을 근거로 미리 채웠다 — 사람은 그 뒤에 말을 덧붙였다(같은 줄)."""
    server, root, _signature, facade = _opened_tier(tmp_path)
    proposal = _cuts(facade)[1]
    base = facade.load_head().revision.base_plan_id
    mixed = proposal.notes + " 원거리 인물 확인함"
    facade.set_notes(base, proposal.segment_id, mixed)
    card = _card(_model_of(server.visual_workbench_render(root)[1]), proposal.segment_id)
    assert card["note"] == "원거리 인물 확인함" and card["machine"][0]["kind"] == "auto"
    assert _counts(server, root) == (1, 1, 1, 1, 1, 0)
    _save_memo(server, root, proposal.segment_id, card["note"])  # 고치지 않고 저장
    assert _notes(facade, proposal.segment_id) == mixed  # 한 글자도 바뀌지 않는다
    edited = _save_memo(server, root, proposal.segment_id, "인물 없음")
    assert _card(edited, proposal.segment_id)["note"] == "인물 없음"
    assert _notes(facade, proposal.segment_id) == proposal.notes + "\n인물 없음"
    assert _counts(server, root) == (1, 1, 1, 1, 1, 0)


@needs_ffmpeg
def test_a_saved_memo_rides_split_and_replan_with_the_machine_note(tmp_path: Path) -> None:
    """메모를 쓰는 다른 길 — 나누기는 메모를 복사하고 계획 반영(입양)은 글자 그대로 옮긴다."""
    server, root, signature, facade = _opened_tier(tmp_path)
    proposal = _cuts(facade)[1]
    server.capcut_confirm_auto_view(signature)
    _save_memo(server, root, proposal.segment_id, "확인함")
    kept = proposal.notes + "\n확인함"
    head = facade.load_head().revision
    clock = head.clock_for(proposal.source_id)
    middle = (proposal.start_pts + proposal.end_pts) // 2
    split = next(
        p for d in range(5000) for p in (middle + d, middle - d) if clock.is_frame_boundary(p)
    )
    facade.split_segment(head.base_plan_id, proposal.segment_id, split)
    assert _counts(server, root)[:2] == (2, 0)  # 두 반쪽 모두 결정한 제안이다
    snapshot, mapping = facade.plan_from_review(facade.load_head().revision.base_plan_id)
    facade.adopt_plan_review(snapshot, mapping)
    halves = [c for c in _cuts(facade) if c.selection == "excluded"]
    assert [c.notes for c in halves] == [kept, kept]
    model = _model_of(server.visual_workbench_render(root)[1])
    assert [_card(model, c.segment_id)["note"] for c in halves] == ["확인함", "확인함"]
    assert _counts(server, root)[:2] == (2, 0)


@needs_ffmpeg
def test_a_merged_two_memo_cut_round_trips_through_an_unchanged_save(tmp_path: Path) -> None:
    """검증 고침 4 — 메모 칸이 한 줄 입력이라 합친 컷의 두 메모가 「왼쪽 메모오른쪽 메모」로 보였고,
    고치지 않고 저장해도 그 붙은 한 줄로 바뀌었다. 이제 여러 줄 칸이 줄을 그대로 싣는다."""
    server, root, _signature, facade = _opened_tier(tmp_path)
    first, proposal, _last = _cuts(facade)
    _save_memo(server, root, first.segment_id, "왼쪽 메모")
    _save_memo(server, root, proposal.segment_id, "오른쪽 메모")
    base = facade.load_head().revision.base_plan_id
    facade.merge_segments(base, first.segment_id, proposal.segment_id)
    merged = _notes(facade, first.segment_id)
    assert merged == first.notes + "\n왼쪽 메모\n" + proposal.notes + "\n오른쪽 메모"
    card = _card(_model_of(server.visual_workbench_render(root)[1]), first.segment_id)
    assert card["note"] == "왼쪽 메모\n오른쪽 메모"
    _save_memo(server, root, first.segment_id, card["note"])  # 고치지 않고 저장
    assert _notes(facade, first.segment_id) == merged  # 한 글자도 바뀌지 않는다
    typed = _save_memo(server, root, first.segment_id, "첫 줄\n둘째 줄")  # 칸에서 친 줄바꿈
    assert _card(typed, first.segment_id)["note"] == "첫 줄\n둘째 줄"
    assert _notes(facade, first.segment_id) == (
        first.notes + "\n" + proposal.notes + "\n첫 줄\n둘째 줄"
    )


@needs_ffmpeg
def test_restore_after_a_memo_save_restores_the_decided_proposal(tmp_path: Path) -> None:
    """메모는 결정이 아니다(D57 C 고침 4) — 저장이 근거 줄을 지키므로 「자동 제외 전체 복원」은
    메모를 쓴 결정한 제안도 사용으로 되돌린다. 옛 작업대는 미리 채운 근거를 사람 글로 덮어 그
    컷을 복원에서 빠뜨렸다(보내기에서 `enabled="0"` 「제외 확정」으로 남았다)."""
    server, root, signature, facade = _opened_tier(tmp_path)
    proposal = _cuts(facade)[1]
    server.capcut_confirm_auto_view(signature)
    _save_memo(server, root, proposal.segment_id, "원거리 인물 확인함")
    server.capcut_edit_view(signature, "restore")
    restored = next(c for c in _cuts(facade) if c.segment_id == proposal.segment_id)
    assert restored.selection == "included"
    assert restored.notes == proposal.notes + "\n원거리 인물 확인함"  # 메모는 남는다
    assert _counts(server, root)[:2] == (1, 0)


@needs_ffmpeg
def test_the_review_panel_memo_keeps_the_machine_note_too(
    tmp_path: Path, local_socket: None
) -> None:
    """옛 검토 패널(서랍 안)의 메모 칸도 같은 길 — 사람 메모만 갈아 끼운다."""
    server, root, signature, facade = _opened_tier(tmp_path)
    proposal = _cuts(facade)[1]
    server.review_refresh_view(signature)
    server.review_note_view(signature, proposal.segment_id, "현장 메모\n둘째 줄")
    assert _notes(facade, proposal.segment_id) == proposal.notes + "\n현장 메모\n둘째 줄"
    assert _counts(server, root)[0] == 1
    # 그 칸도 여러 줄이다 — 한 줄 칸에서는 줄바꿈을 칠 수 없었다.
    app = server.build_blocks()
    (box,) = [b for b in app.blocks.values() if isinstance(b, gr.Textbox) and b.label == "메모"]
    assert box.lines >= 2


# ── 필터 「제외 제안」 — 미검토 제외 제안만 ───────────────────────────────────────


@needs_ffmpeg
def test_the_proposal_filter_returns_only_unreviewed_exclusions(tmp_path: Path) -> None:
    """옛 「검토 대기」는 '검토 필요' 권고·전사 실패 조각도 실었다 — 이제 미검토 제안만이다."""
    service, root = tier_project(tmp_path, suitability="usable")
    facade = ReviewWorkspaceFacade(root)
    head = facade.load_head().revision
    other = next(s for s in chronological(head) if not is_unreviewed_auto_exclusion(s))
    facade.set_override(head.base_plan_id, other.segment_id, "review_required")
    shown = service.visual_review_query(root, selection_filter="review")
    head = facade.load_head().revision
    pending = {s.segment_id for s in unreviewed_auto_exclusions(head)}
    assert pending and {card.segment_id for card in shown.segments} == pending
    assert other.segment_id not in {card.segment_id for card in shown.segments}


# ── 일괄 확인 — 옛 「자동 제외 확인」과 같은 처리기·같은 호출 ──────────────────────


def _built(tmp_path: Path) -> tuple[RawWorkspaceUi, Any]:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("대기열", str(tmp_path / "workspace"))
    return server, server.build_blocks()


def _click_dep(app: Any, button: Any) -> dict[str, Any]:
    (dep,) = [d for d in app.config["dependencies"] if d["targets"] == [(button._id, "click")]]
    return dep


def test_bulk_confirm_moves_into_the_queue_with_the_same_handler(
    tmp_path: Path, local_socket: None
) -> None:
    server, app = _built(tmp_path)
    buttons = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}
    assert "자동 제외 확인" not in buttons  # 명령 줄의 옛 단추는 없다 — 대기열이 그 자리다
    source = buttons[CONFIRM_ALL_SOURCE_LABEL]
    assert source.elem_id == ui_facade.CONFIRM_ALL_SOURCE_ID
    assert source.visible == "hidden"  # 화면에는 대기열의 단추가 보이고, 이것은 그 원천이다
    dep = _click_dep(app, source)
    assert app.fns[dep["id"]].fn == server.capcut_confirm_auto_view  # 같은 처리기
    assert dep["inputs"] == [server._signature_state._id]
    assert dep["outputs"] == [
        server._capcut_status._id,
        server._visual_player_block._id,
        server._visual_workbench_block._id,
    ]
    tails = [
        d
        for d in app.config["dependencies"]
        if d.get("trigger_after") == dep["id"] and d.get("js") == ui_facade.ACTIVITY_REPLY_JS
    ]
    assert len(tails) == 1  # 응답은 그대로 활동 칸으로 간다
    # 명령 줄의 숨은 원천이다 — 서랍·아코디언 안이 아니다(보내기 단추는 W-46a에 서랍으로 갔다).
    assert source.parent is buttons[workspace_service.RENDER_EXPORT_LABEL].parent
    node = source.parent
    while node is not None:
        assert not isinstance(node, (gr.Accordion, gr.Sidebar))
        node = getattr(node, "parent", None)
    js = ui_facade.VISUAL_BOOTSTRAP_JS
    assert f'doc.getElementById("{ui_facade.CONFIRM_ALL_SOURCE_ID}")' in js


def test_proposals_never_disable_the_send_button(tmp_path: Path, local_socket: None) -> None:
    """D56 — 미검토 제안은 정보일 뿐이다. 보내기 단추를 끄는 배선이 없다."""
    server, app = _built(tmp_path)
    send = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}[
        FINALCUT_SEND_LABEL
    ]
    assert send.visible is True and send.interactive is not False
    assert not [d for d in app.config["dependencies"] if send._id in d["outputs"]]
    js = ui_facade.VISUAL_BOOTSTRAP_JS
    queue_js = js[js.index("function queueBlock(") : js.index("function nextQueueItem(")]
    assert "disabled" not in queue_js.replace('" disabled"', "")  # 「다음」만 끈다
    keyword = finalcut_timeline.KEYWORD_UNREVIEWED  # 대기열이 말하는 키워드 = 실제로 다는 키워드
    assert '개는 <span class=\\"vl-kw\\">' + keyword + "</span> 키워드로 갑니다 — 빨강은 " in js
    assert "꺼진 채 갑니다(Final Cut에서 V로 켭니다)" in js  # W-29d, D80


@needs_ffmpeg
def test_bulk_confirm_keeps_the_w13_rules_undo_and_restore(
    tmp_path: Path, local_socket: None
) -> None:
    """같은 호출이라 결과도 같다 — 바뀐 영상만 사람 손(D53), 실행 취소·「전체 복원」 그대로."""
    service, root = co.reverse_imported_project(tmp_path, human=False)
    two = co._ids(service, root)["DJI_0002.mov"]
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    signature = server.open_workspace_view(str(root))[1]
    # 열기가 건 정리(촬영 시각 순 맞춤이 머리를 새 계획으로 옮긴다, D53)가 끝난 뒤에 잰다 — 그
    # 작업과 겨루면 누가 먼저냐에 따라 조각 아이디가 갈린다(W-45가 정리를 빠르게 해 드러났다).
    assert co._wait_upkeep(server.raw, root).status == "succeeded"
    facade = ReviewWorkspaceFacade(root)
    before = facade.load_head().revision
    pending = {s.segment_id for s in unreviewed_auto_exclusions(before)}
    assert [s.source_id for s in unreviewed_auto_exclusions(before)] == [two]
    app = server.build_blocks()
    source = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}[
        CONFIRM_ALL_SOURCE_LABEL
    ]
    handler = app.fns[_click_dep(app, source)["id"]].fn
    message, _shell, _workbench = handler(signature)
    confirmed = facade.load_head().revision
    assert confirmed.edit is not None and confirmed.edit.notes == "자동 제외 확인"
    assert facade.human_source_ids(confirmed) == frozenset({two})
    assert unreviewed_auto_exclusions(confirmed) == ()
    for old, new in zip(before.segments, confirmed.segments, strict=True):
        assert new.selection == old.selection  # 제외는 그대로 — 표지만 걷힌다
        assert AUTO_EXCLUDED_UNREVIEWED_TAG not in new.visual_tags
    assert "제외 제안 1개를 확인했습니다" in message
    server.capcut_edit_view(signature, "undo")
    undone = facade.load_head().revision
    assert {s.segment_id for s in unreviewed_auto_exclusions(undone)} == pending
    assert facade.human_source_ids(undone) == frozenset()  # 되돌린 편집은 손이 아니다
    handler(signature)
    server.capcut_edit_view(signature, "restore")
    restored = facade.load_head().revision
    assert restored.edit is not None and restored.edit.notes == "자동 제외 전체 복원"
    assert all(s.selection == "included" for s in restored.segments if s.segment_id in pending)
    assert facade.human_source_ids(restored) == frozenset({two})
    assert service.list_jobs(root)  # 작업 기록은 그대로 읽힌다(유료 작업 없음)
    assert not [j for j in service.list_jobs(root) if j.kind == FINALCUT_SEND_JOB_KIND]


# ── 알림(toast) — 완료는 알림, 막힘은 주의, 실패는 오류. 머리 글자는 한글이다 ──────────


def test_toast_titles_are_korean_and_ride_on_every_toast(monkeypatch: pytest.MonkeyPatch) -> None:
    assert set(ui_facade.TOAST_TITLES) == {"error", "warning", "info", "success"}
    assert all(HANGUL.match(title) for title in ui_facade.TOAST_TITLES.values())
    calls: list[tuple[str, str, str]] = []
    monkeypatch.setattr(gr, "Info", lambda m, **k: calls.append(("info", m, k["title"])))
    monkeypatch.setattr(gr, "Warning", lambda m, **k: calls.append(("warning", m, k["title"])))
    workspace_service._toast("info", "끝났습니다.")
    workspace_service._toast("warning", "막혔습니다.")
    assert calls == [
        ("info", "끝났습니다.", ui_facade.TOAST_TITLES["info"]),
        ("warning", "막혔습니다.", ui_facade.TOAST_TITLES["warning"]),
    ]
    error = workspace_service._error("실패했습니다.")
    assert isinstance(error, gr.Error) and error.title == ui_facade.TOAST_TITLES["error"]
    # 그라디오 6.29는 알림 머리를 종류에서 지어 영어로 박는다 — 머리줄 실행본이 한글로 바꾼다.
    shell = ui_facade.SHELL_JS
    assert "W.__vlToastTitles = function" in shell and ".toast-title" in shell
    for title in ui_facade.TOAST_TITLES.values():
        assert title in shell
    # 알림은 오른쪽 아래 — 오른쪽 위(그라디오 기본)면 넓은 화면의 견적 칸을 덮어 승인 앞 단계가
    # 알림이 사라질 때까지 승인을 거절했다(탐침 실측).
    css = workspace_service.BASE_CSS
    toast_wrap = css[css.index("body .toast-wrap {") :]
    assert "top: auto !important;" in toast_wrap[: toast_wrap.index("}")]
    assert "bottom: calc(var(--vl-statusline-h) + 16px) !important;" in toast_wrap


def _record_toasts(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(workspace_service, "_toast", lambda level, msg: seen.append((level, msg)))
    return seen


@needs_ffmpeg
def test_command_replies_raise_the_matching_toast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _record_toasts(monkeypatch)
    _, root = tier_project(tmp_path, suitability="usable")
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    signature = server.open_workspace_view(str(root))[1]
    server.capcut_confirm_auto_view(signature)
    assert seen[-1] == ("info", "제외 제안 1개를 확인했습니다. 제외는 그대로 유지됩니다.")
    server.capcut_confirm_auto_view(signature)
    assert seen[-1] == ("warning", "확인할 제외 제안이 없습니다.")
    server.capcut_edit_view(signature, "undo")
    assert seen[-1] == ("info", "마지막 편집을 되돌렸습니다.")
    server.capcut_edit_view(signature, "restore")
    assert seen[-1][0] == "info" and seen[-1][1].startswith("자동 제외를 모두 복원했습니다")
    reply = server.capcut_cancel_view(signature)
    assert seen[-1] == ("warning", reply) and "취소할 선택 분석 작업이 없습니다" in reply


def test_a_finished_send_is_announced_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_socket: None
) -> None:
    seen = _record_toasts(monkeypatch)
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("보내기 알림", str(tmp_path / "workspace"))
    root, signature = server.workspace_root(), server.current_signature()
    assert root is not None and signature is not None
    server.finalcut_toast_view(signature)
    assert seen == []  # 보낸 적이 없으면 아무 말도 없다
    handle = server.raw.submit_job(root, FINALCUT_SEND_JOB_KIND, "a", lambda context: None)
    assert _wait_terminal(server.raw, root, handle.job_id).status == "succeeded"
    counts = {"sources": 2, "spine_clips": 5, "disabled_clips": 1, "unreviewed_clips": 1}
    server._finalcut_sent[signature] = {
        "path": str(tmp_path / "vlogsrt_x.fcpxml"),
        "counts": counts,
        "not_exported": 0,
    }
    server.finalcut_toast_view(signature)
    server.finalcut_toast_view(signature)
    assert seen == [("info", "Final Cut으로 보냈습니다. 원본 2개 · 클립 5개.")]
    handle = server.raw.submit_job(root, FINALCUT_SEND_JOB_KIND, "b", lambda context: None)
    assert _wait_terminal(server.raw, root, handle.job_id).status == "succeeded"
    # 서비스의 실제 거부 문구 — 작업 ID·날 열거값(ingest_copy)을 싣는다. 알림은 쉬운 말로 바꾼다.
    refused = (
        "영상 가져오기 작업이 아직 진행 중으로 기록돼 있다: job_000012(ingest_copy,"
        " 2026-10-10T01:02:03Z 시작) — 끝난 뒤 다시 보내세요."
    )
    server._finalcut_sent[signature] = {"error": refused}
    with pytest.raises(gr.Error) as raised:
        server.finalcut_toast_view(signature)
    assert raised.value.title == ui_facade.TOAST_TITLES["error"]
    message = str(raised.value.message)
    assert message == (
        "Final Cut으로 보내지 못했습니다. 영상 가져오기가 아직 진행 중입니다. 끝난 뒤 다시"
        " 보내세요. 전체 내용은 「기록」에 있습니다."
    )
    server.finalcut_toast_view(signature)  # 한 번만 — 다시 던지지 않는다


def test_the_sent_summary_names_timeline_clips(tmp_path: Path) -> None:
    counts = {"sources": 2, "spine_clips": 5, "disabled_clips": 1, "unreviewed_clips": 1}
    text = workspace_service._finalcut_sent_text(
        {"path": str(tmp_path / "x.fcpxml"), "counts": counts, "not_exported": 0}
    )
    assert "타임라인 클립 5개" in text and "스파인" not in text


def test_the_sent_summary_describes_the_sent_file_not_a_live_count(tmp_path: Path) -> None:
    """검증 고침 4 잔손질 — 「… 미검토 제외 제안 2개 …」가 활동 칸에 남아 0개 남은 대기열 곁에서
    지금의 수처럼 읽혔다. 보낸 파일 하나의 사실로 말한다."""
    counts = {"sources": 3, "spine_clips": 6, "disabled_clips": 0, "unreviewed_clips": 2}
    sent = {"path": str(tmp_path / "vlogsrt_x.fcpxml"), "counts": counts, "not_exported": 0}
    text = workspace_service._finalcut_sent_text(sent)
    keyword = finalcut_timeline.KEYWORD_UNREVIEWED
    assert "보낸 파일 `vlogsrt_x.fcpxml`" in text
    assert f"보낸 파일에는 미검토 제외 제안 2개를 「{keyword}」 키워드로 달았습니다." in text
    assert " · 미검토 제외 제안 2개 · " not in text
    counts["unreviewed_clips"] = 0
    assert "미검토" not in workspace_service._finalcut_sent_text(sent)


def test_the_legend_names_the_hollow_loose_style() -> None:
    """검증 고침 4 잔손질 — 등급 범례에 속 빈 빨강(경계와 맞지 않는 제안)을 더한다(안내 줄의 말)."""
    from tests.raw.test_workbench_theme import _rules

    shell = ui_facade.visual_player_shell_html(None, "")
    legend = shell[shell.index('class="vl-legend"') : shell.index("</span></span>") + 14]
    rules = dict(_rules(ui_facade.WORKBENCH_CSS))
    assert '<span class="vl-tier-key loose"' in legend
    assert "경계 어긋남</span>" in legend  # 1280에서 한 줄에 들도록 짧은 말 — 전문은 title
    assert " · " not in legend and "column-gap: 8px" in rules[".vl-legend"]
    assert 'title="컷 경계와 맞지 않는 제외 제안 — 남은 수에 들지 않습니다"' in legend
    assert "var(--vl-tier-red-ink)" in rules[".vl-tier-key.loose"]
    assert "dashed" in rules[".vl-key-hollow"]
    # 그라디오의 `.prose *` 글자색이 덮지 않도록 부모(빨강 잉크)의 색을 물려받는다(검증 고침 4 NIT).
    assert "color: inherit" in rules[".vl-key-hollow"]


def test_the_memo_field_is_an_editable_field_for_global_keys() -> None:
    """여러 줄 메모 칸 안에서는 재생 단축키(Space/K·J/L·,/.)와 Esc가 듣지 않는다(다른 칸과 같다)."""
    source = Path(ui_facade.__file__).read_text(encoding="utf-8")
    editable = source[source.index("var VL_EDITABLE = ") :].split(";", 1)[0]
    owners = source[source.index("var ESC_OWNERS = ") :].split(";", 1)[0]
    assert "textarea" in editable and "textarea" in owners


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        ("이 작업공간에서 다른 Final Cut 보내기가 진행 중이다 — 끝난 뒤 다시 보내세요", "다른"),
        ("가변 프레임(VFR) 영상이 있다: a.mov", "가변 프레임(VFR)"),
        ("검토 머리의 원본이 낡았다 — job_000003", "원본 영상이 바뀌었습니다"),
        ("x.fcpxml은 썼지만 Final Cut을 열지 못했습니다 — OSError", "열지 못했습니다"),
        ("알 수 없는 까닭 snake_case_enum job_000009", ""),
    ],
)
def test_send_failure_toasts_speak_plain_korean(error: str, reason: str) -> None:
    text = workspace_service._send_failure_text(error)
    assert text.startswith("Final Cut으로 보내지 못했습니다. ") and reason in text
    assert text.endswith("전체 내용은 「기록」에 있습니다.")
    assert "job_" not in text and "_" not in text and "다 " not in text


@needs_ffmpeg
def test_the_next_focus_follows_the_shown_plan_and_reports_a_stale_cut(tmp_path: Path) -> None:
    server, root, signature, facade = _opened_tier(tmp_path)
    head = facade.load_head().revision
    proposal = unreviewed_auto_exclusions(head)[0]
    payload = {
        "signature": signature,
        "token": server._visual_lifetime_token,
        "epoch": "e",
        "page": 1,
        "basePlanId": head.base_plan_id,
        "sourceId": proposal.source_id,
    }
    found = server.visual_workbench_action({**payload, "focusId": proposal.segment_id}, "refresh")
    assert found["ok"] is True and found["focusStale"] is False
    assert _model_of(found["html"])["selectedId"] == proposal.segment_id
    gone = server.visual_workbench_action({**payload, "focusId": "seg_gone"}, "refresh")
    # 그사이 나뉘거나 합쳐진 컷 — 다른 컷으로 몰래 다시 그리지 않고(값 없음) 알린다.
    assert gone["ok"] is True and gone["focusStale"] is True and "html" not in gone
    assert gone["message"].endswith("「다음」을 다시 누르세요.")


def test_toasts_do_not_touch_the_warning_filters() -> None:
    """`warnings.catch_warnings()`는 스레드 안전하지 않다 — 처리기는 일꾼 스레드에서 돈다."""
    import inspect

    assert "catch_warnings" not in inspect.getsource(workspace_service._toast)


def test_the_left_drawer_speaks_politely(tmp_path: Path, local_socket: None) -> None:
    _server, app = _built(tmp_path)
    boxes = {b.label: b for b in app.blocks.values() if isinstance(b, gr.Textbox)}
    path_box = boxes["작업공간 경로(표시)"]
    assert path_box.info == "선택기로 고른 자리를 보여 줍니다. 직접 적을 수는 없습니다."
    assert (
        path_box.placeholder.startswith("「프로젝트 열기」") and "아래" not in path_box.placeholder
    )


# ── 문구 — 모델 이름은 돈·모델 고르기 자리에만, 화면에 없는 조작을 가리키지 않는다 ─────


def test_the_started_notice_names_no_model() -> None:
    for name in ("Luna", "Claude", "gpt"):
        assert name not in QUICK_APPROVED_TEXT
    assert QUICK_APPROVED_TEXT.endswith("니다.")


@needs_ffmpeg
def test_approving_under_claude_never_says_luna(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.raw.test_browser_runtime import _make_media
    from tests.raw.test_claude_analysis_flow import CLAUDE, ClaudeOpener, choose_model, make_server
    from tests.raw.test_selection_analysis import wait_until

    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="8")
    server, _sender, _luna = make_server(
        tmp_path, monkeypatch, source, opener=ClaudeOpener(), auto_approve=False
    )
    choose_model(server, CLAUDE)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    reply = server.quick_approve_view(server.current_signature())
    assert QUICK_APPROVED_TEXT in reply and "Luna" not in reply


def test_main_surface_copy_points_only_at_controls_on_screen() -> None:
    js = ui_facade.VISUAL_BOOTSTRAP_JS
    for retired in ("「자동 정리」", "Luna 분석 대기", "검토 대기", '페이지 " +', "조각이 없다"):
        assert retired not in js, retired
    shell = ui_facade.activity_html()
    assert "API 연결" not in shell and ">키 입력<" not in shell  # 키는 `.env`에서만(D102)
    assert workspace_service.ONLINE_MODE_NOTE == "온라인 · 분석 승인 뒤 유료"
    assert workspace_service.OFFLINE_MODE_NOTE == "오프라인 · 유료 분석 꺼짐"


# ── 실행본(node) — 대기열 · 「다음」 · 일괄 확인 · 필터 · 목록 머리 · 사용|제외 · 컷 이름표 ───


_QUEUE_SCRIPT = r"""
// ── 제외 제안 대기열(D57 C) ─────────────────────────────────────────────
function queueModel() {
  const m = tierModel('all');
  m.redUnmaterialized = 0;
  m.sources.srcA.suggestions[2].open = true;
  m.sources.srcB.suggestions = [
    { start: '7/5', end: '2', tier: 'red', hold: false, rec: 'normal', rule: 'silent-not-scenery',
      reason: 'rB', redReason: 'tier_silence', audio: silent, open: true,
      video: { status: 'usable', label: '사용 가능', reason: '세로 화면', evidence: [] } },
  ];
  m.sources.srcB.rail.intervals[0].sel = 'excluded';
  // 메모 칸은 사람 메모뿐이다 — 기계 근거는 서버가 따로 실어 「분석 근거」에 쉬운 말로 보인다.
  Object.assign(m.cards[3], { sel: 'excluded', tier: 'red', autoPending: true,
    tags: ['자동 제외 · 미검토'], note: '현장 메모\n둘째 줄',
    machine: [{ kind: 'auto', reason: 'tier_silence', start: '7/5', end: '2' },
      { kind: 'review', text: 'window_000000:검토 필요' }] });
  // 줄 = 미검토 컷(컷 범위) + 그 컷의 빨강 구간. segB1의 빨강은 컷보다 앞에서 시작한다.
  m.queue = { total: 3, open: 2, confirmable: 2, loose: 0, order: ['srcA', 'srcB', 'srcC'],
    items: [
      { sourceId: 'srcA', sourceName: '01-landscape.mp4', segmentId: 'segA3',
        start: '2', end: '3', redStart: '21/10', redEnd: '29/10' },
      { sourceId: 'srcB', sourceName: '02-portrait-silent.mp4', segmentId: 'segB1',
        start: '3/2', end: '2', redStart: '7/5', redEnd: '2' },
    ] };
  return m;
}
function withMedia(m) {
  m.mediaCards = ['srcA', 'srcB', 'srcC'].map((sid) => Object.assign({},
    m.cards.find((c) => c.sourceId === sid),
    { media: true, start: '0', end: m.sources[sid].rail.duration,
      dur: m.sources[sid].rail.duration }));
  return m;
}
const act = (name) => ({ getAttribute: (k) => (k === 'data-vl-act' ? name : null) });
const refreshReply = () => ({ ok: true, echoEpoch: 'epoch-1', html: '<div>', message: '' });
const qState = () => windowScope.__vlVisualWorkbenchState;
qState().selectedId = null; qState().selectedPlan = null; qState().marker = null;
qState().lastMsg = ''; qState().filter = 'all';
const envQ = visualEnv({ vl_refresh: refreshReply }, queueModel());
const queuePanel = envQ.thumbs.innerHTML;
// 1) 같은 영상·이 쪽의 컷 — 서버 없이 그 컷과 빨강 구간을 고른다(결정 상자가 열린다).
envQ.click(act('queue-next'));
const local = { calls: envQ.serverCalls.length, panel: envQ.panel.innerHTML,
  marker: qState().marker && qState().marker.segmentId, selected: qState().selectedId,
  time: envQ.vid.state.currentTime, rail: envQ.rail.innerHTML,
  refocus: qState().queueRefocus };
// 2) 다른 영상 — 서버가 그 컷을 고른 값을 다시 그린다(초점 요청).
envQ.click(act('queue-next'));
await new Promise((resolve) => setTimeout(resolve, 0));
const focusCall = envQ.serverCalls[0];
const focused = queueModel();
focused.epoch = 'epoch-2'; focused.selectedId = 'segB1'; focused.selectedSourceId = 'srcB';
focused.selectedCard = Object.assign({}, focused.cards[3]);
envQ.root.attrs['data-vl-model'] = JSON.stringify(focused);
envQ.rewired();
const remote = { panel: envQ.panel.innerHTML, src: envQ.vid.video.src,
  marker: qState().marker && qState().marker.segmentId,
  time: envQ.vid.state.currentTime };
// 2초 틱이 다른 영상의 첫 컷을 고른 값을 그려도(같은 리비전) 초점 컷이 남는다.
const ticked = queueModel();
ticked.epoch = 'epoch-3'; ticked.selectedId = 'segA1'; ticked.selectedSourceId = 'srcA';
ticked.cards = ticked.cards.filter((c) => c.id !== 'segB1');
envQ.root.attrs['data-vl-model'] = JSON.stringify(ticked);
envQ.rewired();
const afterTick = { selected: qState().selectedId, decision: envQ.panel.innerHTML.includes(
  'class="vl-decision"') };
// 3) 끝에서 다시 처음으로 — 첫 제안(다른 영상)으로 돌아간다.
envQ.click(act('queue-next'));
await new Promise((resolve) => setTimeout(resolve, 0));
const wrapCall = envQ.serverCalls[1];
// 4) 일괄 확인 — 숨은 그라디오 단추(옛 처리기)를 누른다. 서버 함수는 부르지 않는다.
const sourceButton = el({ id: 'vl-queue-confirm-src' });
sourceButton.clicks = 0; sourceButton.click = () => { sourceButton.clicks += 1; };
envQ.registry['vl-queue-confirm-src'] = sourceButton;
const callsBefore = envQ.serverCalls.length;
envQ.click(act('queue-confirm'));
const bulk = { clicks: sourceButton.clicks, calls: envQ.serverCalls.length - callsBefore };
// 5) 빈 대기열 — 「다음」이 꺼지고 눌러도 아무것도 하지 않는다.
const emptyModel = queueModel();
emptyModel.queue = { total: 3, open: 0, confirmable: 0, loose: 0,
  order: ['srcA', 'srcB', 'srcC'], items: [] };
qState().selectedId = null; qState().marker = null; qState().lastMsg = '';
const envQEmpty = visualEnv({ vl_refresh: refreshReply }, emptyModel);
envQEmpty.click(act('queue-next'));
const empty = { thumbs: envQEmpty.thumbs.innerHTML, calls: envQEmpty.serverCalls.length,
  footnote: envQEmpty.registry['vl-footnote'].textContent };
const noneModel = queueModel();
noneModel.queue = { total: 0, open: 0, confirmable: 0, loose: 0, order: [], items: [] };
const envNone = visualEnv({}, noneModel);
// 경계와 맞지 않는 빨강만 있는 라이브러리(실데이터 사본의 모양) — 남은 것은 없고 안내 한 줄.
const looseOnly = queueModel();
looseOnly.redUnmaterialized = 6;
looseOnly.queue = { total: 0, open: 0, confirmable: 0, loose: 6, order: [], items: [],
  looseSources: ['a.mov', 'b.mov', 'c.mov', 'd.mov', 'e.mov'].map((name, index) => ({
    sourceId: 'x' + index, sourceName: name, count: index ? 1 : 2 })) };
const envLooseOnly = visualEnv({}, looseOnly);
// 경계와 맞지 않는 빨강은 결정한 제안(반쯤 흐린 빗금)과 다르게 — 속 빈 빨강으로 그린다.
qState().selectedId = null; qState().marker = null;
const styled = queueModel();
styled.sources.srcA.suggestions[2].open = false;  // 결정한 제안
styled.sources.srcA.suggestions.push({ start: '6/5', end: '9/5', tier: 'red', hold: false,
  rec: 'normal', rule: 'silent-not-scenery', reason: 'rL', redReason: 'tier_silence',
  audio: silent, open: false, loose: true,
  video: { status: 'usable', label: '사용 가능', reason: '바다', evidence: [] } });
styled.redUnmaterialized = 1;
styled.queue = Object.assign({}, styled.queue, { loose: 1, looseSources: [
  { sourceId: 'srcA', sourceName: '01-landscape.mp4', count: 1 }] });
const envStyled = visualEnv({}, styled);
const styles = { thumbs: envStyled.thumbs.innerHTML,
  bars: envStyled.rail.innerHTML.split('<button').filter((b) => b.includes('vl-recommendation')) };
// 6) 필터 「제외 제안」 — 컷 카드는 미검토 제안만, 영상 카드는 열린 제안이 있는 영상만.
qState().selectedId = null; qState().marker = null;
const envFilter = visualEnv({}, Object.assign(queueModel(), { filter: 'review' }));
const media = withMedia(queueModel());
media.filter = 'review';
media.queue.items = media.queue.items.filter((it) => it.sourceId === 'srcA');
const envMedia = visualEnv({}, media);
media.filter = 'all';
const envMediaAll = visualEnv({}, media);
// 7) 사용 | 제외 — 같은 처리기(toggle-sel)로 뒤집는다. 눌린 쪽은 아무것도 하지 않는다.
qState().selectedId = null; qState().marker = null;
const envSeg = visualEnv({ vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: 'x' }) },
  queueModel());
const segPanel = envSeg.panel.innerHTML;
envSeg.click({ getAttribute: () => null });
envSeg.click(act('toggle-sel'));
await new Promise((resolve) => setTimeout(resolve, 0));
const segRefocus = qState().queueRefocus;
// 8) 영상 카드가 있는 화면 — 「다음」이 고른 컷이 그 영상의 첫 컷이고 이 쪽 목록 밖일 때, 같은
//    리비전의 2초 틱 뒤에도 패널은 영상 전체가 아니라 그 컷이다(검증 MAJOR 2).
qState().selectedId = null; qState().marker = null; qState().focusCard = null;
const envMF = visualEnv({ vl_refresh: refreshReply }, withMedia(queueModel()));
envMF.click(act('queue-next'));  // segA3 — 같은 영상, 이 쪽 목록
envMF.click(act('queue-next'));  // segB1 — 다른 영상, 서버 초점
await new Promise((resolve) => setTimeout(resolve, 0));
const mfFocused = withMedia(queueModel());
mfFocused.epoch = 'epoch-2'; mfFocused.selectedId = 'segB1'; mfFocused.selectedSourceId = 'srcB';
mfFocused.selectedCard = Object.assign({}, mfFocused.cards[3]);
mfFocused.cards = mfFocused.cards.filter((c) => c.id !== 'segB1');  // 이 쪽 목록 밖
envMF.root.attrs['data-vl-model'] = JSON.stringify(mfFocused);
envMF.rewired();
const mfFocusPanel = envMF.panel.innerHTML;
const mfTick = withMedia(queueModel());
mfTick.epoch = 'epoch-3'; mfTick.selectedId = 'segA1'; mfTick.selectedSourceId = 'srcA';
mfTick.selectedCard = Object.assign({}, mfTick.cards[0]);
mfTick.cards = mfTick.cards.filter((c) => c.id !== 'segB1');
envMF.root.attrs['data-vl-model'] = JSON.stringify(mfTick);
envMF.rewired();
const mediaFocus = { focusPanel: mfFocusPanel, tickPanel: envMF.panel.innerHTML,
  selected: qState().selectedId, time: envMF.vid.state.currentTime };
// 9) 경계와 맞지 않는 빨강의 결정 상자 — 단추가 컷 전체를 정한다고 말한다(새 동작 없음).
qState().selectedId = null; qState().marker = null; qState().focusCard = null;
const lm = queueModel();
lm.sources.srcA.suggestions.push({ start: '6/5', end: '9/5', tier: 'red', hold: false,
  rec: 'normal', rule: 'silent-not-scenery', reason: 'rL', redReason: 'tier_silence',
  audio: silent, open: false, loose: true,
  video: { status: 'usable', label: '사용 가능', reason: '바다', evidence: [] } });
const envLoose = visualEnv({ vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: 'x' }) },
  lm);
envLoose.rail._listeners.click.slice().forEach((fn) => fn({ target: {
  getAttribute: (k) => (k === 'data-vl-recommendation' ? '3' : null) } }));
const loosePanel = envLoose.panel.innerHTML;
envLoose.click(act('red-confirm'));
await new Promise((resolve) => setTimeout(resolve, 0));
const looseCall = envLoose.serverCalls[0];
// 10) 그사이 바뀐 초점 — 다른 컷으로 몰래 건너뛰지 않고 활동 칸에 알린다.
qState().selectedId = null; qState().marker = null; qState().lastMsg = '';
const replies = [];
windowScope.__vlActivity = { reply(text) { replies.push(String(text)); } };
const envQStale = visualEnv({ vl_refresh: () => ({ ok: true, echoEpoch: 'epoch-1',
  message: '고른 제외 제안이 그사이 바뀌었습니다.', focusStale: true }) }, queueModel());
envQStale.click(act('queue-next'));
envQStale.click(act('queue-next'));
await new Promise((resolve) => setTimeout(resolve, 0));
const stale = { armed: qState().queueFocus || null, replies,
  rendered: envQStale.propsCalls.length, footnote: envQStale.registry['vl-footnote'].textContent };
delete windowScope.__vlActivity;
checkpoints.push({ case: 'queue', queuePanel, local, remote, afterTick,
  focus: focusCall && { name: focusCall.name, focusId: focusCall.payload.focusId,
    sourceId: focusCall.payload.sourceId, filter: focusCall.payload.filter,
    basePlanId: focusCall.payload.basePlanId },
  wrap: wrapCall && { focusId: wrapCall.payload.focusId, sourceId: wrapCall.payload.sourceId },
  bulk, empty, noneThumbs: envNone.thumbs.innerHTML, looseOnly: envLooseOnly.thumbs.innerHTML,
  filterThumbs: envFilter.thumbs.innerHTML, mediaThumbs: envMedia.thumbs.innerHTML,
  mediaAll: envMediaAll.thumbs.innerHTML, segPanel,
  segCall: envSeg.serverCalls[0] && { name: envSeg.serverCalls[0].name,
    selection: envSeg.serverCalls[0].payload.selection,
    segmentId: envSeg.serverCalls[0].payload.segmentId },
  segCalls: envSeg.serverCalls.length, segRefocus, mediaFocus, loosePanel, styles,
  looseCall: looseCall && { selection: looseCall.payload.selection,
    segmentId: looseCall.payload.segmentId }, stale });
"""


def _run_queue_harness(tmp_path: Path, extra: str = "", case: str = "queue") -> dict[str, Any]:
    from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, _TIER_SCRIPT

    harness = tmp_path / "queue.js"
    script = _TIER_SCRIPT + _QUEUE_SCRIPT + extra
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            script + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    return next(row for row in json.loads(completed.stdout) if row.get("case") == case)


@needs_node
def test_the_queue_panel_counts_and_offers_next_and_bulk(tmp_path: Path) -> None:
    report = _run_queue_harness(tmp_path)
    thumbs = report["queuePanel"]
    assert '<section class="vl-queue" aria-label="제외 제안 대기열">' in thumbs
    assert "제외 제안 3개 중 2개 남음" in re.sub(r"<[^>]+>", "", thumbs)
    assert 'data-vl-act="queue-next">다음</button>' in thumbs
    assert 'data-vl-act="queue-confirm">남은 2개 모두 제외 유지</button>' in thumbs
    keyword = '<span class="vl-kw">제외 제안(미검토)</span>'
    note = (
        f"미검토 제외 제안 2개는 {keyword} 키워드로 갑니다 — 빨강은 꺼진 채 갑니다"
        "(Final Cut에서 V로 켭니다)."
    )
    assert note in thumbs  # 정보일 뿐 — 보내기를 막지 않는다(D56)
    # 목록 열 맨 위 — 결정 상자는 패널 맨 위를 그대로 지킨다(1280×800에서 스크롤 없이).
    assert thumbs.index('class="vl-queue"') < thumbs.index("vl-list-head")
    assert "vl-queue" not in report["local"]["panel"]


@needs_node
def test_next_walks_the_open_proposals_and_wraps(tmp_path: Path) -> None:
    report = _run_queue_harness(tmp_path)
    local = report["local"]
    # 같은 영상의 이 쪽 컷 — 서버 없이 컷과 빨강 구간을 함께 고르고 결정 상자를 연다.
    assert local["calls"] == 0 and local["selected"] == "segA3" and local["marker"] == "segA3"
    assert '<section class="vl-decision"' in local["panel"]
    assert abs(local["time"] - 2.1) < 1e-9  # 빨강 구간의 시작으로
    assert "vl-rec-sel" in local["rail"]  # 고른 빨강 막대가 표시된다
    assert local["refocus"] is True  # 다시 그려도 「다음」에 초점을 되돌린다(연달아 Enter)
    # 다른 영상 — 서버가 그 컷을 고른다(필터는 그대로, 미검토 컷은 어느 필터에서도 보인다).
    assert report["focus"] == {
        "name": "vl_refresh",
        "focusId": "segB1",
        "sourceId": "srcB",
        "filter": "all",
        "basePlanId": "plan-1",  # 화면이 본 계획 — 재계획 뒤에도 같은 컷을 찾는 재료
    }
    remote = report["remote"]
    assert remote["src"] == "/media/preview-b" and remote["marker"] == "segB1"
    assert '<section class="vl-decision"' in remote["panel"]
    assert "02-portrait-silent.mp4" in remote["panel"]
    assert abs(remote["time"] - (1.5 + 0.521)) < 1e-9  # 원점을 더한 구간 시작
    assert report["afterTick"] == {"selected": "segB1", "decision": True}
    # 마지막 제안 다음은 처음 — 첫 영상의 제안으로 돌아간다.
    assert report["wrap"] == {"focusId": "segA3", "sourceId": "srcA"}


@needs_node
def test_bulk_confirm_presses_the_old_handler_and_the_empty_queue_is_inert(
    tmp_path: Path,
) -> None:
    report = _run_queue_harness(tmp_path)
    assert report["bulk"] == {"clicks": 1, "calls": 0}
    empty = report["empty"]
    assert "제외 제안 3개 중 0개 남음" in re.sub(r"<[^>]+>", "", empty["thumbs"])
    assert 'data-vl-act="queue-next" disabled>다음</button>' in empty["thumbs"]
    assert "queue-confirm" not in empty["thumbs"] and "꺼진 채" not in empty["thumbs"]
    assert empty["calls"] == 0 and empty["footnote"] == "남은 제외 제안이 없습니다."
    assert "vl-queue" not in report["noneThumbs"]  # 제안이 없으면 대기열도 없다
    # 경계와 맞지 않는 빨강만 있으면 — 남은 것은 없다고 말하고 「다음」도 일괄도 없다. 안내 한 줄.
    loose = re.sub(r"<[^>]+>", "", report["looseOnly"])
    assert "남은 제외 제안이 없습니다." in loose
    assert "queue-next" not in report["looseOnly"] and "queue-confirm" not in report["looseOnly"]
    # 그 빨강이 있는 영상을 셋까지 이름으로 대고 나머지는 「외 N개」(검증 MINOR 2).
    assert (
        "컷 경계와 맞지 않는 제외 제안 6개가 a.mov, b.mov, c.mov 외 2개 영상에 있습니다." in loose
    )
    assert "남은 수에 들지 않고, 그 구간을 덮는 컷의 결정을 따릅니다." in loose
    assert "「현재 위치에서 나누기」" in loose


@needs_node
def test_the_proposal_filter_shows_only_unreviewed_proposals(tmp_path: Path) -> None:
    report = _run_queue_harness(tmp_path)
    # 목록은 언제나 영상 카드다(D64 — 컷 카드로 물러나는 길을 지웠다). 대기열에 열린 제안(segA3·
    # segB1)이 남은 영상 A·B만 보이고, 보류·검토 필요뿐인 영상 C는 제안이 아니다.
    cards = report["filterThumbs"]
    assert ">제외 제안</button>" in cards and "검토 대기" not in cards
    assert 'data-vl-act="filter-review" aria-pressed="true"' in cards
    assert 'data-vl-card="segA1"' in cards and 'data-vl-card="segB1"' in cards
    assert 'data-vl-card="segC1"' not in cards
    videos = report["mediaThumbs"]
    assert 'data-vl-card="segA1"' in videos  # 열린 제안이 있는 영상(대표 카드)
    assert 'data-vl-card="segB1"' not in videos and 'data-vl-card="segC1"' not in videos
    assert "영상 1개" in videos
    everything = report["mediaAll"]
    assert "<b>영상 3개</b> · 촬영 시각 순" in everything and "페이지 1/1" not in everything
    # 카드의 ✕ 수는 대기열 줄(미검토 컷) 수다 — 열린 빨강 구간 수가 아니다(Σ ✕ = M).
    cards = everything.split('<button type="button" class="vl-thumb')[1:]
    assert ['<span class="vl-cnt r">✕ 1</span>' in c for c in cards] == [True, False, False]


@needs_node
def test_the_use_exclude_control_keeps_the_toggle_handler(tmp_path: Path) -> None:
    report = _run_queue_harness(tmp_path)
    panel = report["segPanel"]
    assert '<div class="vl-seg" role="group" aria-label="이 컷 사용 여부">' in panel
    assert '<button type="button" class="vl-btn" aria-pressed="true">사용</button>' in panel
    assert 'data-vl-act="toggle-sel" aria-pressed="false">제외</button>' in panel
    assert report["segCalls"] == 1  # 눌린 쪽(사용)은 아무것도 하지 않는다
    assert report["segRefocus"] is False  # 다른 것을 누르면 「다음」 초점을 되돌리지 않는다
    assert report["segCall"] == {"name": "vl_apply", "selection": "excluded", "segmentId": "segA1"}


@needs_node
def test_rail_cuts_carry_number_and_range_labels(tmp_path: Path) -> None:
    rail = _run_queue_harness(tmp_path)["local"]["rail"]
    for label in ("<b>#1</b> 0.0–1.0", "<b>#2</b> 1.0–2.0", "<b>#3</b> 2.0–3.0"):
        assert label in rail, label
    assert "<span>01-landscape.mp4</span>" not in rail  # 파일 이름을 컷마다 되풀이하지 않는다
    assert 'aria-label="컷 #1 · 0.0초~1.0초 — 클릭·Enter로 그 시각으로 이동"' in rail


@needs_node
def test_a_focused_first_cut_never_turns_into_its_video_card(tmp_path: Path) -> None:
    """영상 카드는 첫 컷의 ID를 빌려 쓴다 — 「다음」이 고른 컷이 이 쪽 목록 밖이면 2초 틱 뒤
    그 ID가 영상 카드(영상 전체 범위)로 풀려, 컷 #1의 결정 상자 위에 영상 전체 범위가 섰다."""
    focus = _run_queue_harness(tmp_path)["mediaFocus"]
    for panel in (focus["focusPanel"], focus["tickPanel"]):
        assert "02-portrait-silent.mp4</strong> · 컷 #1 · 1.500~2.000초 (0.500초)" in panel
        assert "0.000~1.750초" not in panel  # 영상 전체 범위가 아니다
        assert '<section class="vl-decision"' in panel
    assert focus["selected"] == "segB1"
    assert abs(focus["time"] - (1.5 + 0.521)) < 1e-9  # 빨강(1.4초)보다 늦은 컷 시작으로 옮긴다


@needs_node
def test_a_loose_red_decision_box_says_it_decides_the_whole_cut(tmp_path: Path) -> None:
    report = _run_queue_harness(tmp_path)
    panel = report["loosePanel"]
    box = panel[panel.index('<section class="vl-decision"') : panel.index("</section>")]
    text = re.sub(r"<[^>]+>", "", box)
    assert "이 제외 제안(1.200~1.800초)은 컷 경계와 맞지 않습니다." in text
    assert "제안 구간이 아니라 컷 #2 전체(1.000~2.000초)를 정합니다." in text
    assert "「현재 위치에서 나누기」" in text
    # 새 동작은 없다 — 같은 단추가 그 컷 전체를 겨냥한다(옛 동작 그대로).
    assert report["looseCall"] == {"selection": "excluded", "segmentId": "segA2"}


@needs_node
def test_the_rule_id_lives_in_the_details_not_in_the_decision_box(tmp_path: Path) -> None:
    panel = _run_queue_harness(tmp_path)["local"]["panel"]
    box = panel[panel.index('<section class="vl-decision"') : panel.index("</section>")]
    assert "silent-not-scenery" not in box
    evidence = panel[panel.index('data-vl-keep="evidence"') :]
    assert "판정 규칙 <code>silent-not-scenery</code>" in evidence
    assert "발화 발화" not in panel


@needs_node
def test_the_memo_box_shows_the_human_memo_and_evidence_reads_plain_korean(
    tmp_path: Path,
) -> None:
    """검증 MINOR 1 — 메모 칸은 사람 메모뿐, 기계 근거는 접힌 「분석 근거」에 쉬운 말로(D52)."""
    panel = _run_queue_harness(tmp_path)["remote"]["panel"]
    memo = panel[panel.index('data-vl-keep="memo"') :]
    # 여러 줄 칸 — 사람의 줄을 그대로 싣는다(검증 고침 4: 한 줄 칸은 줄을 붙였다).
    assert '<textarea id="vl-note-input"' in memo and 'aria-label="메모"' in memo
    assert ">현장 메모\n둘째 줄</textarea>" in memo
    assert 'type="text" id="vl-note-input"' not in panel
    evidence = panel[panel.index('data-vl-keep="evidence"') : panel.index('data-vl-keep="memo"')]
    text = re.sub(r"<[^>]+>", "", evidence)
    assert "자동 제외 근거 · 자막 없음 · AI가 풍경으로 보지 않음 · 1.4~2.0초" in text
    assert "확인 필요 · window_000000:검토 필요" in text
    assert "자동 판정 이유 · 자막 없음 · AI가 풍경으로 보지 않음" in panel  # 상태 줄 그대로
    for raw in ("자동 제외: ", "확인 필요: ", "&quot;reason", '"reason"', "tier_silence"):
        assert raw not in panel, raw


@needs_node
def test_loose_reds_are_hollow_not_like_decided_proposals(tmp_path: Path) -> None:
    """검증 MINOR 2 — 경계와 맞지 않는 빨강이 결정한 제안과 똑같이(반쯤 흐린 빗금)그려졌다."""
    styles = _run_queue_harness(tmp_path)["styles"]
    assert '<i class="r done"' in styles["thumbs"] and '<i class="r loose"' in styles["thumbs"]
    classes = [re.findall(r'class="([^"]+)"', bar)[0] for bar in styles["bars"]]
    reds = [(cls, bar) for cls, bar in zip(classes, styles["bars"], strict=True) if " red" in cls]
    (decided, _), (loose_cls, loose) = reds[-2:]
    assert "loose" not in decided and loose_cls == "vl-recommendation normal red loose"
    assert "컷 경계와 맞지 않음 · 남은 수에 들지 않음" in loose  # 접근 이름·풍선 도움말
    text = re.sub(r"<[^>]+>", "", styles["thumbs"])
    assert "컷 경계와 맞지 않는 제외 제안 1개가 01-landscape.mp4에 있습니다." in text
    assert (
        "✕ 1" in text and "✕ 2" not in text
    )  # 카드 ✕는 M 그대로 — 경계와 맞지 않는 빨강은 안 센다


def test_a_long_video_name_wraps_inside_the_queue() -> None:
    """안내 줄이 영상 이름을 댄다 — 띄어쓰기 없는 긴 파일 이름이 목록 열 밖으로 잘리지 않는다."""
    from tests.raw.test_workbench_theme import _rules

    rules = dict(_rules(ui_facade.WORKBENCH_CSS))
    assert "grid-template-columns: minmax(0, 1fr)" in rules[".vl-queue"]
    assert "overflow-wrap: anywhere" in rules[".vl-queue-note"]


def test_loose_red_marks_are_hollow_red_with_contrast() -> None:
    from tests.raw.test_workbench_theme import _contrast, _rules
    from ui import design_tokens

    rules = dict(_rules(ui_facade.WORKBENCH_CSS))
    mini, rail = rules[".vl-mbar .r.loose"], rules[".vl-recommendation.red.loose"]
    for body in (mini, rail):
        assert "background: transparent" in body and "var(--vl-tier-red-ink)" in body
    assert "dashed" in rail and "opacity" not in mini  # 결정한 제안(`r done`)은 흐린 빗금
    assert "opacity" in rules[".vl-mbar .r.done"]
    for light_or_dark in (0, 1):
        tokens = {name: pair[light_or_dark] for name, pair in design_tokens.COLOR_TOKENS.items()}
        # 속 빈 표지의 선·글자는 띠·렠 바탕(sunken) 위 — 비글자 3:1, 글자(✕)는 4.5:1.
        assert _contrast(tokens["tier-red-ink"], tokens["surface-sunken"]) >= 4.5


# ── 상태 줄 각주 — 수는 그릴 때마다의 값(대기열과 같은 값)만 말한다(D57 C 고침 3) ──────────

_FOOTNOTE_SCRIPT = r"""
// 각주가 말하는 수는 대기열과 같은 그리기 값이어야 한다 — 옛 각주는 마지막 서버 문구(`lastMsg`)를
// 다음 문구가 올 때까지 다시 그려, 일괄 확인 뒤에도 「미검토 제외 제안 N개」가 남았다.
const LIVE = __LIVE__;
const SELECT_REPLY = __SELECT_REPLY__;
function liveModel(live, epoch) {
  const m = queueModel();
  m.epoch = epoch; m.autoAnalyzeSelection = true;
  m.queue = live.queue; m.redPending = live.redPending;
  m.redUnmaterialized = live.redUnmaterialized;
  return m;
}
const readFoot = (env) => ({ footnote: env.registry['vl-footnote'].textContent,
  queue: env.thumbs.innerHTML.replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim() });
qState().selectedId = null; qState().marker = null; qState().lastMsg = '';
qState().focusCard = null; qState().requestedSource = null;
const envFoot = visualEnv({ vl_select_source: () => Object.assign({}, SELECT_REPLY,
  { echoEpoch: 'epoch-f0', html: null }) }, liveModel(LIVE.before, 'epoch-f0'));
// 1) 영상 카드를 눌러 분석 선택 — 서버 답(실제 문구)이 각주에 선다.
envFoot.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segB1' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const footSteps = [Object.assign({ step: 'selected' }, readFoot(envFoot))];
// 2) 일괄 확인 · 3) 실행 취소 — 각 리비전을 같은 작업대에 커밋한다(그라디오의 다시 그리기).
[['confirmed', 'epoch-f1'], ['undone', 'epoch-f2']].forEach(([step, epoch]) => {
  envFoot.root.attrs['data-vl-model'] = JSON.stringify(liveModel(LIVE[step], epoch));
  envFoot.rewired();
  footSteps.push(Object.assign({ step }, readFoot(envFoot)));
});
checkpoints.push({ case: 'footnote', footSteps });
"""

_FOOTNOTE_COUNT_SCRIPT = r"""
// 경계와 맞지 않는 빨강의 각주 — 그 수가 바뀌면(나누기·합치기) 다음 그리기에서 바로 따라간다.
const footText = (env) => env.registry['vl-footnote'].textContent;
qState().selectedId = null; qState().marker = null; qState().lastMsg = '';
const looseLive = (count, epoch) => {
  const m = queueModel(); m.epoch = epoch; m.redUnmaterialized = count;
  m.queue = Object.assign({}, m.queue, { loose: count, looseSources: count ? [
    { sourceId: 'srcA', sourceName: '01-landscape.mp4', count }] : [] });
  return m;
};
const envLooseFoot = visualEnv({}, looseLive(2, 'epoch-l0'));
const looseSteps = [footText(envLooseFoot)];
[[1, 'epoch-l1'], [0, 'epoch-l2']].forEach(([count, epoch]) => {
  envLooseFoot.root.attrs['data-vl-model'] = JSON.stringify(looseLive(count, epoch));
  envLooseFoot.rewired();
  looseSteps.push(footText(envLooseFoot));
});
// 「남은 제외 제안이 없습니다」도 그때의 수다 — 다음 그리기에서 남은 제안이 생기면 남지 않는다.
qState().selectedId = null; qState().marker = null; qState().lastMsg = '';
const none = queueModel();
none.queue = { total: 1, open: 0, confirmable: 0, loose: 0, looseSources: [], order: [],
  items: [] };
none.redPending = 0; none.redUnmaterialized = 0;
const envNoneFoot = visualEnv({}, none);
envNoneFoot.click(act('queue-next'));
const noneSaid = footText(envNoneFoot);
const reopened = queueModel(); reopened.epoch = 'epoch-n1'; reopened.redUnmaterialized = 0;
envNoneFoot.root.attrs['data-vl-model'] = JSON.stringify(reopened);
envNoneFoot.rewired();
checkpoints.push({ case: 'footcount', looseSteps, noneSaid, noneAfter: footText(envNoneFoot) });
"""


def _stated(text: str, pattern: str) -> list[int]:
    return [int(n) for n in re.findall(pattern, text)]


def _live(model: dict[str, Any]) -> dict[str, Any]:
    return {key: model[key] for key in ("queue", "redPending", "redUnmaterialized")}


@needs_ffmpeg
@needs_node
def test_the_footnote_and_queue_agree_after_confirm_and_undo(tmp_path: Path) -> None:
    """검증 뒤 고침 3 — 영상 카드를 누른 답이 「미검토 제외 제안 1개 …」를 각주에 남겼고, 일괄 확인
    뒤에도 그 문구가 다시 그려져 대기열(0개 남음)과 어긋났다. 실제 서버의 답과 리비전으로 잰다."""
    server, root, signature, facade = _opened_tier(tmp_path)
    proposal = _cuts(facade)[1]
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}
    before = _model_of(server.visual_workbench_render(root)[1])
    reply = fns["vl_select_source"](
        {
            "epoch": before["epoch"],
            "token": before["token"],
            "signature": before["signature"],
            "sourceId": proposal.source_id,
            "selectedId": proposal.segment_id,
        }
    )
    assert reply["ok"], reply
    confirmed = _model_of(server.capcut_confirm_auto_view(signature)[2])
    undone = _model_of(server.capcut_edit_view(signature, "undo")[2])
    assert [m["queue"]["open"] for m in (before, confirmed, undone)] == [1, 0, 1]
    live = {"before": _live(before), "confirmed": _live(confirmed), "undone": _live(undone)}
    select = json.dumps({"ok": True, "message": reply["message"]}, ensure_ascii=False)
    script = _FOOTNOTE_SCRIPT.replace("__LIVE__", json.dumps(live, ensure_ascii=False))
    report = _run_queue_harness(tmp_path, script.replace("__SELECT_REPLY__", select), "footnote")
    for row, model in zip(report["footSteps"], (before, confirmed, undone), strict=True):
        pending, loose = model["queue"]["open"], model["queue"]["loose"]
        assert all(n == pending for n in _stated(row["footnote"], r"미검토 제외 제안 (\d+)개")), row
        assert all(n == pending for n in _stated(row["queue"], r"중 (\d+)개 남음")), row
        assert all(n == loose for n in _stated(row["footnote"], r"제외 제안 (\d+)개가 컷")), row


@needs_node
def test_count_lines_in_the_footnote_follow_every_render(tmp_path: Path) -> None:
    """경계와 맞지 않는 빨강 수·「남은 제외 제안이 없습니다」는 `lastMsg`로 남지 않는다(고침 3)."""
    report = _run_queue_harness(tmp_path, _FOOTNOTE_COUNT_SCRIPT, "footcount")
    first, second, third = report["looseSteps"]
    assert first.startswith("제외 제안 2개가 컷 경계와 맞지 않아")
    assert second.startswith("제외 제안 1개가 컷 경계와 맞지 않아")  # 옛: 「2개」가 남았다
    assert "컷 경계" not in third  # 0개면 그 줄이 없다
    assert report["noneSaid"] == "남은 제외 제안이 없습니다."
    assert report["noneAfter"] != "남은 제외 제안이 없습니다."  # 다음 그리기에 남지 않는다


@needs_node
def test_a_stale_focus_is_announced_instead_of_jumping(tmp_path: Path) -> None:
    stale = _run_queue_harness(tmp_path)["stale"]
    assert stale["armed"] is None and stale["rendered"] == 0  # 다른 컷으로 다시 그리지 않는다
    assert stale["replies"] == ["고른 제외 제안이 그사이 바뀌었습니다."]  # 활동 칸
    assert stale["footnote"] == "고른 제외 제안이 그사이 바뀌었습니다."


_INERT_SCRIPT = r"""
// 서랍이 열린 동안 서랍 밖은 inert — 키보드로도 닿지 않는다. 닫히는 모든 길에서 걷힌다.
function tnode(id, tag) {
  const n = el(id, tag || 'DIV');
  n.parentElement = null; n.inert = false;
  n.removeAttribute = function (k) { delete this.attrs[k]; };
  return n;
}
const body = tnode('', 'BODY');
const app = tnode('app');
const header = tnode('app-header', 'HEADER');
const tabs = tnode('tabs');
const toastWrap = tnode('toasts');
toastWrap.classes.add('toast-wrap');
const scrimEl = reg['vl-scrim'];
const leftEl = reg['project-tools'];
const rightEl = reg['settings-drawer'];
[leftEl, rightEl, scrimEl].forEach((n) => {
  n.inert = false; n.removeAttribute = function (k) { delete this.attrs[k]; };
});
app.children = [header, tabs, leftEl, rightEl];
body.children = [app, scrimEl, toastWrap];
app.children.forEach((n) => { n.parentElement = app; });
body.children.forEach((n) => { n.parentElement = body; });
app.parentElement = body;
document.body = body;
const nodes = [app, header, tabs, leftEl, rightEl, scrimEl, toastWrap];
document.querySelectorAll = (sel) => (sel === '[data-vl-inert]'
  ? nodes.filter((n) => 'data-vl-inert' in n.attrs) : []);
const inertIds = () => nodes.filter((n) => n.inert).map((n) => n.id || n.tagName);
const syncs = observers.filter((o) => o.fn === window.__vlSyncDrawers);
const res = {};
window.__vlDrawer('settings-drawer', false); window.__vlDrawer('project-tools', false);
res.closed = inertIds();
window.__vlDrawer('settings-drawer', true);
res.rightOpen = inertIds();
press({ closest: () => null });  // Esc
res.afterEsc = inertIds();
(listeners.click || []).forEach((fn) => fn({ target: real(el('opener', 'BUTTON'),
  { 'data-vl-drawer': 'project-tools' }) }));
res.leftOpen = inertIds();
(listeners.click || []).forEach((fn) => fn({ target: scrimEl }));  // 막
res.afterScrim = inertIds();
right.toggle.click(); syncs.forEach((o) => o.fn([]));  // 그라디오 자신의 여닫이(class 변화)
res.gradioOpen = inertIds();
right.toggle.click(); syncs.forEach((o) => o.fn([]));
res.gradioClosed = inertIds();
window.__vlDrawer('settings-drawer', true);
(listeners.click || []).forEach((fn) => fn({ target: real(el('closer', 'BUTTON'),
  { 'data-vl-drawer-close': 'settings-drawer' }) }));  // 서랍 머리 「닫기」
res.afterClose = inertIds();
res.marks = nodes.filter((n) => 'data-vl-inert' in n.attrs).length;
process.stdout.write(JSON.stringify(res));
"""


@needs_node
def test_open_drawers_make_everything_else_inert_until_any_close(tmp_path: Path) -> None:
    from tests.raw.test_workbench_layout import _SHELL_HARNESS

    harness = tmp_path / "inert.js"
    harness.write_text(
        _SHELL_HARNESS.replace("process.stdout.write(JSON.stringify(out));", _INERT_SCRIPT),
        encoding="utf-8",
    )
    script = tmp_path / "shell_src.js"
    script.write_text(ui_facade.SHELL_JS, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(script)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    res = json.loads(completed.stdout)
    # 열린 서랍의 바깥 형제(머리줄 · 탭 · 다른 서랍)만 — 막과 알림 칸은 그대로 누를 수 있다.
    assert res["rightOpen"] == ["app-header", "tabs", "project-tools"]
    assert res["leftOpen"] == ["app-header", "tabs", "settings-drawer"]
    assert res["gradioOpen"] == ["app-header", "tabs", "project-tools"]
    for closed in ("closed", "afterEsc", "afterScrim", "gradioClosed", "afterClose"):
        assert res[closed] == [], closed  # Esc · 막 · 그라디오 여닫이 · 「닫기」 모두 걷힌다
    assert res["marks"] == 0


_TOAST_SCRIPT = r"""
const kinds = ['error', 'warning', 'info', 'success'];
const toasts = kinds.map((kind) => {
  const text = { nodeType: 3, nodeValue: kind.charAt(0).toUpperCase() + kind.slice(1) + ' ' };
  return { kind, text, classList: { contains: (c) => c === 'toast-title' || c === kind },
    firstChild: text };
});
const closes = [el('c1', 'BUTTON')];
closes[0].attrs['aria-label'] = 'Close';
const scope = { querySelectorAll: (sel) => (sel === '.toast-title' ? toasts
  : sel === '[data-testid="toast-close"]' ? closes : []) };
window.__vlToastTitles(scope);
window.__vlToastTitles(scope);  // 두 번 불러도 같다
out.toasts = toasts.map((t) => t.text.nodeValue);
out.close = closes[0].attrs['aria-label'];
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_toast_headers_are_relabelled_in_korean(tmp_path: Path) -> None:
    from tests.raw.test_workbench_layout import _SHELL_HARNESS

    harness = tmp_path / "toast.js"
    harness.write_text(
        _SHELL_HARNESS.replace("process.stdout.write(JSON.stringify(out));", _TOAST_SCRIPT),
        encoding="utf-8",
    )
    script = tmp_path / "shell_src.js"
    script.write_text(ui_facade.SHELL_JS, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(script)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    out = json.loads(completed.stdout)
    titles = ui_facade.TOAST_TITLES
    assert out["toasts"] == [titles[k] + " " for k in ("error", "warning", "info", "success")]
    assert out["close"] == "닫기"
