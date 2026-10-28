"""W-17 B단계(D57) — 작업대 배치: 머리줄 · 명령 줄 활동 칸 · 서랍 · 무대 격자 · 카드 · 패널.

선언(CSS·HTML·배선)과 실행본(node)을 함께 잰다. 처리기·입출력·승인 결합은 그대로이고,
옮긴 것은 자리와 단추 응답이 보이는 곳뿐이다 — 시험이 그 둘을 나눠 본다. 유료 호출 0회.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from collections.abc import Iterator
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

from ui import design_tokens, ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    CONFIRM_ALL_SOURCE_LABEL,
    FINALCUT_SEND_LABEL,
    LIBRARY_APPROVE_LABEL,
    OFFLINE_MODE_NOTE,
    ONLINE_MODE_NOTE,
    RawWorkspaceUi,
)

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

ACTION_LABELS = (
    "영상 선택",
    "자동 정리",
    "선택 분석 취소",
    "전체 분석 취소",
    FINALCUT_SEND_LABEL,
    CONFIRM_ALL_SOURCE_LABEL,  # 대기열 단추가 누르는 숨은 원천(옛 「자동 제외 확인」, D57 C)
    "라이브러리 닫기",  # 머리줄 단추가 누르는 숨은 원천(D64 세션 F)
    "자동 제외 전체 복원",
    "실행 취소",
    "결과 확인",
    "원본 미리보기 준비",
    "실패 구간 재시도",
    "폴더 열기",
    "선택한 오리지널 연결",
)


def _server(tmp_path: Path) -> RawWorkspaceUi:
    server = RawWorkspaceUi(state_path=tmp_path / "ui-state.json")
    server.create_workspace_view("배치", str(tmp_path / "workspace"))
    return server


def _ancestors(block: Any) -> Iterator[Any]:
    node = getattr(block, "parent", None)
    while node is not None:
        yield node
        node = getattr(node, "parent", None)


def _media_block(css: str, query: str) -> str:
    start = css.index(f"@media ({query})")
    return css[start : css.index("\n}\n", start)]


# ── 머리줄 — 흐름 단계는 보고만 한다 ─────────────────────────────────────────


def test_flow_steps_report_live_counts_and_never_gate_the_send() -> None:
    """네 단계의 수와 상태 — 「검토」의 제외 제안 수는 정보다(보내기를 막지 않는다, D56)."""
    empty = ui_facade.flow_steps(ui_facade.FlowCounts())
    assert [s.label for s in empty] == ["폴더 불러오기", "분석 승인", "검토", "내보내기"]
    assert [s.state for s in empty] == ["cur", "todo", "todo", "todo"]
    assert all(s.sub == "" for s in empty)
    analysing = ui_facade.flow_steps(ui_facade.FlowCounts(imported=18, analysed=6))
    assert [s.state for s in analysing] == ["done", "cur", "todo", "todo"]
    assert (analysing[0].sub, analysing[1].sub) == ("18개", "6/18")
    review = ui_facade.flow_steps(ui_facade.FlowCounts(imported=18, analysed=18, proposals=7))
    assert [s.state for s in review] == ["done", "done", "cur", "todo"]
    assert review[2].sub == "미확인 비추천 7" and review[2].alert is True
    sent = ui_facade.flow_steps(
        ui_facade.FlowCounts(imported=18, analysed=18, proposals=7, sent=True)
    )
    assert [s.state for s in sent] == ["done", "done", "done", "done"]
    assert sent[3].sub == "보냄" and sent[2].alert is True  # 미검토가 남아도 보냈다(키워드로 간다)
    html = ui_facade.flow_steps_html(review)
    assert html.count('class="vl-step ') == 4 and html.count('aria-current="step"') == 1
    assert '<span class="sub r">미확인 비추천 7</span>' in html
    assert "disabled" not in html  # 단계는 보고만 한다 — 누르는 곳도 막는 곳도 아니다


def test_top_bar_names_project_steps_mode_shortcuts_and_settings() -> None:
    flow = ui_facade.flow_steps_html(ui_facade.flow_steps(ui_facade.FlowCounts(imported=3)))
    online = ui_facade.topbar_html(
        online=True, mode_note=ONLINE_MODE_NOTE, project="<제주>", flow=flow
    )
    assert '<span class="vl-wordmark">vlogsrt</span>' in online
    assert "&lt;제주&gt;" in online and "<제주>" not in online  # 이름은 이스케이프된다
    assert 'data-vl-drawer="project-tools"' in online  # 프로젝트 이름이 왼쪽 서랍을 연다
    assert 'data-vl-drawer="settings-drawer"' in online and ">설정</span>" in online
    assert 'class="vl-mode online"' in online and ONLINE_MODE_NOTE in online
    assert ">단축키</span>" in online and "data-vl-keys-open" in online
    assert flow in online
    keys = online[online.index('id="vl-keysheet"') :]
    # 재생 다섯 + 시작·끝 표시 셋(I·O · 1·2·3 · Esc — W-46b, D100 §7b).
    assert keys.count("<dt>") == len(ui_facade.SHORTCUT_SHEET) == 8
    assert "입력 칸에 글을 쓰는 중에는 단축키가 동작하지 않습니다." in keys
    offline = ui_facade.topbar_html(online=False, mode_note=OFFLINE_MODE_NOTE, project="", flow="")
    assert 'class="vl-mode offline"' in offline and OFFLINE_MODE_NOTE in offline
    assert ui_facade.PROJECT_EMPTY_LABEL in offline
    css = ui_facade.WORKBENCH_CSS
    assert (
        "border: 1px solid var(--vl-warn); border-radius: 4px; background: var(--vl-warn-soft);"
        in (css)
    )  # 온라인은 경고 모양(돈이 나갈 수 있다)
    assert "height: var(--vl-topbar-h);" in css


def test_shortcut_hint_matches_the_client_seek_constant() -> None:
    """단축키 안내의 ±초는 실행본이 실제로 옮기는 초와 같다."""
    seconds = ui_facade.SHORTCUT_SEEK_SECONDS
    assert f"var VL_SEEK_SECONDS = {seconds};" in ui_facade.VISUAL_BOOTSTRAP_JS
    line = ui_facade.statusline_html()
    assert f"±{seconds}초" in line and 'id="vl-footnote"' in line
    assert line.index('id="vl-footnote"') < line.index('class="vl-keys"')  # 각주 밖의 형제


def test_workbench_value_carries_the_flow_for_the_top_bar() -> None:
    counts = ui_facade.FlowCounts(imported=2, analysed=1, proposals=1)
    full = ui_facade.visual_workbench_html({"empty": None}, project="A&B", flow=counts)
    assert 'data-vl-project="A&amp;B"' in full and "data-vl-flow=" in full
    flow = re.search(r'data-vl-flow="([^"]*)"', full)
    assert flow is not None and "분석 승인" in flow.group(1) and "&lt;span" in flow.group(1)
    placeholder = ui_facade.visual_workbench_html({"empty": "준비 중"}, project="A", flow=counts)
    assert "data-vl-empty" in placeholder and "data-vl-flow=" in placeholder
    assert "data-vl-flow" not in ui_facade.visual_workbench_html({"empty": "x"})
    js = ui_facade.VISUAL_BOOTSTRAP_JS
    assert 'd.getElementById("vl-flow")' in js and 'd.getElementById("vl-project-name")' in js
    assert "name.textContent = project" in js  # 이름은 textContent로만


# ── 실제 Blocks — 머리줄 · 명령 줄 · 무대 · 서랍 ──────────────────────────────


def _built(tmp_path: Path) -> tuple[RawWorkspaceUi, Any, dict[Any, Any], dict[Any, Any]]:
    server = _server(tmp_path)
    app = server.build_blocks()
    by_id = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    buttons = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}
    return server, app, by_id, buttons


def test_blocks_place_top_bar_command_bar_and_stage(tmp_path: Path, local_socket: None) -> None:
    _server_, app, by_id, buttons = _built(tmp_path)
    header = by_id[workspace_service.APP_HEADER_ID]
    assert isinstance(header, gr.HTML) and header.js_on_load == ui_facade.SHELL_JS
    bar = by_id["vl-command-bar"]
    assert isinstance(bar, gr.Row)
    for label in (
        workspace_service.FOLDER_IMPORT_LABEL,
        LIBRARY_APPROVE_LABEL,
        "선택 분석 취소",
        "전체 분석 취소",
        workspace_service.RENDER_EXPORT_LABEL,
        CONFIRM_ALL_SOURCE_LABEL,  # 숨은 원천 — 화면의 단추는 제외 제안 대기열 안이다(D57 C)
    ):
        assert buttons[label].parent is bar, label
    in_bar = [b for b in app.blocks.values() if isinstance(b, gr.Button) and b.parent is bar]
    # 주 단추는 단계마다 하나 — 분석 승인(견적에 묶인 돈의 문)과 초안 내보내기(W-46a).
    assert [b.value for b in in_bar if b.variant == "primary"] == [
        LIBRARY_APPROVE_LABEL,
        workspace_service.RENDER_EXPORT_LABEL,
    ]
    assert FINALCUT_SEND_LABEL not in [b.value for b in in_bar]  # 서랍의 「고급 · Final Cut」
    # 취소 단추는 늘 보이고 늘 누를 수 있다 — 도는 작업을 못 멈추는 일이 없다.
    for label in ("선택 분석 취소", "전체 분석 취소"):
        assert buttons[label].visible is True and buttons[label].interactive is not False
    assert by_id["vl-activity-block"].parent is bar and by_id["quick-status"].parent is bar
    # 견적 칸은 무대 격자 안의 한 열 — 견적 문구(승인 클릭이 함께 보낸다, D54)와 그 아래 전문.
    quote = by_id["vl-quote"]
    assert isinstance(quote, gr.Column) and quote.parent is by_id["visual-stage"]
    assert by_id["library-analysis-scope"].parent is quote
    assert by_id["vl-quote-note"].parent is quote
    for child in bar.parent.children:
        assert not isinstance(child, gr.Accordion), child  # 무대 위·아래로 열리는 것이 없다


def test_blocks_move_project_and_settings_tools_into_two_drawers(
    tmp_path: Path, local_socket: None
) -> None:
    _server_, app, by_id, buttons = _built(tmp_path)
    left, right = by_id["project-tools"], by_id["settings-drawer"]
    assert isinstance(left, gr.Sidebar) and left.position == "left" and left.open is False
    assert isinstance(right, gr.Sidebar) and right.position == "right" and right.open is False
    for label in (
        "프로젝트 열기",
        "새 프로젝트",
        "Final Cut 라이브러리에서 가져오기",
        "경로로 열기",
    ):
        assert left in _ancestors(buttons[label]), label
    for label in ("자동 제외 전체 복원", "실행 취소", "결과 확인", "폴더 열기"):
        assert right in _ancestors(buttons[label]), label
    assert right in _ancestors(by_id["quick-api-panel"])
    assert right in _ancestors(by_id["analysis-model"])
    for label in ("고급 편집 도구", "고급 · 클립 패키지", "복원 · 전달 · 고급 조작"):
        accordion = next(
            b for b in app.blocks.values() if isinstance(b, gr.Accordion) and b.label == label
        )
        assert right in _ancestors(accordion), label


def test_every_action_reply_reaches_the_activity_area(tmp_path: Path, local_socket: None) -> None:
    """단추 응답(옛 숨은 상태 칸 포함)은 사건이 끝날 때 활동 칸으로 간다 — 처리기는 그대로다."""
    server = _server(tmp_path)
    app = server.build_blocks()
    deps = app.config["dependencies"]
    replies = {
        dep["trigger_after"]: dep for dep in deps if dep.get("js") == ui_facade.ACTIVITY_REPLY_JS
    }
    buttons = {b.value: b for b in app.blocks.values() if isinstance(b, gr.Button)}
    status_id = server._capcut_status._id
    for label in ACTION_LABELS:
        (click,) = [d for d in deps if d["targets"] == [(buttons[label]._id, "click")]]
        assert click["outputs"][0] == status_id, label  # 응답 칸 그대로(배선 무변경)
        reply = replies[click["id"]]
        assert reply["trigger_only_on_success"] is True and reply["backend_fn"] is False, label
        assert reply["inputs"] == [status_id], label
    (approve,) = [
        d for d in deps if d["targets"] == [(buttons[LIBRARY_APPROVE_LABEL]._id, "click")]
    ]
    assert replies[approve["id"]]["inputs"] == [server._capcut_progress_md._id]
    # 승인 클릭은 앞 단계(보이는 견적만 보낸다)를 거치고, 입력은 그대로 견적 문구다(D54).
    assert approve["js"] == ui_facade.APPROVE_GUARD_JS
    assert server._library_summary_md._id in approve["inputs"]
    notes = [
        d
        for d in deps
        if d.get("trigger_after") == approve["id"] and d["outputs"] == [server._quote_note_md._id]
    ]
    assert len(notes) == 1 and notes[0]["trigger_only_on_success"] is True  # 답을 전문 칸에 바로
    cells = [server._quote_note_md._id, server._usage_md._id]
    # 1초 상태 틱 하나가 두 칸을 잇달아 쓴다(D63 세션 C).
    writers = [
        d for d in deps if any(d["outputs"][i : i + 2] == cells for i in range(len(d["outputs"])))
    ]
    (cell_tick,) = [d for d in writers if d["targets"][0][1] == "tick"]
    # 「라이브러리 닫기」도 두 칸을 직접 쓴다 — 닫은 탭의 틱은 칸을 건너뛴다(D64 세션 F).
    close_click = [(buttons["라이브러리 닫기"]._id, "click")]
    assert [d["targets"] for d in writers if d is not cell_tick] == [close_click]
    assert cell_tick["targets"][0][1] == "tick"  # 1초 타이머 — 전문 칸 + 기록 판의 실사용 원천
    # 2초 타이머는 같은 숨은 칸을 다시 쓴다 — 그 쓰기는 활동 칸으로 가지 않는다.
    ticks = [d for d in deps if d["targets"] and d["targets"][0][1] == "tick"]
    assert ticks and not any(d["id"] in replies for d in ticks)


# ── CSS — 고정 높이 칸 · 겹치는 서랍 · 세 분기점 ──────────────────────────────


def test_activity_area_is_one_fixed_line() -> None:
    css = ui_facade.WORKBENCH_CSS
    assert "height: var(--vl-commandbar-h);" in css
    activity = css[css.index(".vl-activity {") :]
    assert "height: var(--vl-control-h);" in activity[: activity.index("}")]
    message = css[css.index(".vl-activity .msg {") :]
    message = message[: message.index("}")]
    for rule in ("white-space: nowrap;", "overflow: hidden;", "text-overflow: ellipsis;"):
        assert rule in message, rule
    assert "#vl-command-bar > #quick-status { display: none; }" in css  # 원천일 뿐 보이지 않는다
    log = css[css.index(".vl-keysheet-pop, .vl-activity-log {") :]
    assert "position: absolute;" in log[: log.index("}")]  # 떠 있는 판 — 무대를 밀지 않는다
    assert "var(--vl-shadow-pop)" in log[: log.index("}")]
    activity_html = ui_facade.activity_html()
    assert 'id="vl-activity-msg"' in activity_html and 'role="status"' in activity_html
    assert 'id="vl-activity-log"' in activity_html and "hidden" in activity_html
    # 실사용 줄의 자리 — 기록 판 맨 위 고정 줄, 여러 줄로 다 보인다(원천 칸은 숨는다).
    assert '<p id="vl-activity-usage" class="vl-activity-usage" hidden></p>' in activity_html
    assert "#vl-command-bar > #vl-usage-src { display: none; }" in css
    usage = css[css.index(".vl-activity-usage {") :]
    usage = usage[: usage.index("}")]
    assert "white-space: normal;" in usage and "overflow-wrap: anywhere;" in usage
    assert "ellipsis" not in usage


def test_drawers_overlay_the_stage_without_pushing_it() -> None:
    css = ui_facade.WORKBENCH_CSS
    assert ".sidebar-parent, .sidebar-parent:has(.sidebar.open) {" in css
    assert "padding-left: 0 !important; padding-right: 0 !important;" in css
    assert (
        "#project-tools > .toggle-button, #settings-drawer > .toggle-button { display: none; }"
        in (css)
    )
    assert "transition: none !important;" in css
    head = ui_facade.drawer_head_html("설정", "settings-drawer")
    assert 'data-vl-drawer-close="settings-drawer"' in head and ">닫기</button>" in head


def test_grid_areas_at_three_breakpoints() -> None:
    css = ui_facade.WORKBENCH_CSS
    wide = css[css.index("#visual-stage {") :]
    wide = wide[: wide.index("}")]
    assert "grid-template-columns: var(--vl-list-col) minmax(0, 1fr) var(--vl-panel-col);" in wide
    # 오른쪽 열: 견적 · 추천 구간 · 「고급 도구」로 펼치는 패널(W-46b, D100 §7b — 네 영역).
    assert (
        '"thumbs player quote" "thumbs player ranges" "thumbs player panel" "thumbs timeline'
        ' timeline"\n    "foot foot foot"' in wide
    )
    assert "--vl-list-col: minmax(240px, 280px);" in wide
    assert "--vl-panel-col: minmax(280px, 320px);" in wide
    two = _media_block(css, "max-width: 1050px")
    # 견적 칸은 명령 줄 바로 아래(승인 단추 곁), 타임라인은 재생기 바로 아래(사용자 결정 6).
    assert (
        '"quote quote" "thumbs player" "thumbs timeline" "thumbs ranges"\n      "thumbs panel"'
        ' "foot foot"' in two
    )
    assert "#visual-stage .vl-thumbs { max-height: none; contain: size; }" in two
    one = _media_block(css, "max-width: 860px")
    # 견적 다음 영상·타임라인·추천 구간(W-46b)
    assert '"quote" "player" "timeline" "ranges" "thumbs" "panel" "foot"' in one
    assert "#vl-quote { grid-area: quote;" in css and "#library-analysis-scope {\n" in css
    assert "#visual-stage .vl-cards {" in one and "display: flex; overflow: auto hidden;" in one
    assert css.index("@media (max-width: 1050px)") < css.index("@media (max-width: 860px)")
    # 승인할 견적이 없으면 견적 문구는 숨고, 전문도 없으면 넓은 화면의 패널이 그 자리까지 쓴다.
    assert (
        "#review-tab:has(#library-approve:disabled) #library-analysis-scope { display: none; }"
        in (css)
    )
    # 초안 결과 카드(W-46a)가 있으면 그 칸도 덮지 않는다 — 올라오는 것은 추천 구간 칸이다(W-46b).
    assert "#visual-stage:not(:has(#vl-quote-note)):not(:has(.vl-export)) #vl-ranges {" in css


@pytest.mark.parametrize("side", [0, 1], ids=["light", "dark"])
def test_new_shell_text_pairs_hold_contrast(side: int) -> None:
    """B단계가 새로 놓은 글자·땅 짝 — 카드 위 수·배지, 연결 방식 표지, 끝난 단계 표."""
    from tests.raw.test_workbench_theme import _contrast

    tokens = {name: values[side] for name, values in design_tokens.COLOR_TOKENS.items()}
    for ink, grounds in (
        ("ink-muted", ("surface-raised", "surface-hover", "accent-soft")),  # 0인 수
        ("accent-ink", ("surface-raised", "surface-hover", "accent-soft")),  # 돌고 있음 배지
        ("ink", ("warn-soft", "surface-raised")),  # 연결 방식 · 활동 기록
        ("tier-red-ink", ("bg", "surface-raised")),  # 「검토」의 제외 제안 수 · 결정 상자
    ):
        for ground in grounds:
            assert _contrast(tokens[ink], tokens[ground]) >= 4.5, (ink, ground)
    assert _contrast(tokens["bg"], tokens["success"]) >= 3.0  # 끝난 단계의 확인 표(표지)
    assert ".vl-cnt.z { color: var(--vl-ink-muted); }" in ui_facade.WORKBENCH_CSS


# ── 카드 · 패널 — 파이썬 쪽 값 ────────────────────────────────────────────────


def test_a_red_span_is_open_only_while_its_covering_cut_is_unreviewed() -> None:
    """빨강 구간의 `open` — 그 가운데를 덮는 컷에 미검토 표지가 남았을 때만 열려 있다(D57 C).

    컷 경계와 맞지 않는지는 `loose`가 따로 말한다 — 경계와 맞지 않아도 덮는 컷이 미검토면 열려 있다.
    """
    source: dict[str, Any] = {
        "rail": {
            "duration": "9",
            "frameSec": "1/25",
            "intervals": [
                {"id": "a", "start": "0", "end": "3"},
                {"id": "b", "start": "3", "end": "6"},
                {"id": "c", "start": "6", "end": "9"},
            ],
        },
        "suggestions": [
            {"start": "0", "end": "3", "tier": "red"},  # 덮는 컷 a — 미검토
            {"start": "3", "end": "6", "tier": "red"},  # 덮는 컷 b — 미검토 아님
            {"start": "61/10", "end": "77/10", "tier": "red"},  # 컷 c 안 · 경계와 맞지 않음
            {"start": "77/10", "end": "9", "tier": "green"},
        ],
    }
    RawWorkspaceUi._mark_open_reds(source, {"a"})
    opened = [row.get("open") for row in source["suggestions"]]
    # 경계와 맞지 않는 빨강은 그 가운데를 덮는 컷 c를 따른다 — c가 미검토가 아니니 열려 있지
    # 않다. 옛 규칙('경계와 맞지 않으면 늘 열림')은 화면 어디서도 닫을 수 없었다(D57 C).
    assert opened == [True, False, False, None]
    assert [row.get("loose") for row in source["suggestions"]] == [False, False, True, None]
    RawWorkspaceUi._mark_open_reds(source, {"a", "c"})  # 덮는 컷 c가 미검토면 열려 있다
    assert [row.get("open") for row in source["suggestions"]] == [True, False, True, None]


def test_card_badge_is_one_analysis_state_only_when_it_needs_attention() -> None:
    badge = RawWorkspaceUi._analysis_badge
    assert badge({}, "분석 대기") is None  # 작업 없는 영상 — 띠가 '분석 전'을 말한다
    assert badge({"stage": "추천 구간 준비 완료"}, "추천 구간 준비 완료") is None
    assert badge({"stage": "분석 대기"}, "분석 대기") == {"label": "분석 대기", "tone": "run"}
    assert badge({"stage": "승인 대기"}, "승인 대기") == {"label": "승인 대기", "tone": ""}
    assert badge({"stage": "분석 확인 필요", "error": "x"}, "분석 확인 필요")["tone"] == "err"
    stage = workspace_service.MODEL_CHANGED_STAGE
    assert badge({"stage": "추천 구간 준비 완료"}, stage) == {"label": stage, "tone": "warn"}


def test_flow_counts_read_the_workspace_and_the_last_send(
    tmp_path: Path, local_socket: None
) -> None:
    server = _server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    assert server._flow_counts(root) == ui_facade.FlowCounts()
    signature = str(server._signature_for(root))
    server._finalcut_sent[signature] = {"error": "실패"}
    assert server._flow_counts(root).sent is False  # 실패는 보낸 것이 아니다
    server._finalcut_sent[signature] = {"path": str(tmp_path / "x.fcpxml")}
    assert server._flow_counts(root, analysed=2, proposals=4) == ui_facade.FlowCounts(
        imported=0, analysed=2, proposals=4, sent=True
    )
    top = server._topbar_value()
    assert "배치" in top and 'id="vl-flow"' in top


# ── 실행본(node) — 활동 칸·서랍 ───────────────────────────────────────────────

_SHELL_HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
function el(id, tag) {
  const node = { id, tagName: tag || 'DIV', hidden: false, textContent: '', title: '', attrs: {},
    children: [], classes: new Set(), clicks: 0, focused: 0,
    classList: { contains(c) { return node.classes.has(c); } },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.children.push(c); }, focus() { this.focused += 1; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; } };
  return node;
}
const reg = {};
['vl-activity', 'vl-activity-msg', 'vl-activity-key', 'vl-activity-log', 'quick-status',
 'project-tools', 'settings-drawer', 'vl-keysheet', 'vl-usage-src', 'vl-activity-usage',
].forEach((id) => { reg[id] = el(id); });
reg['vl-activity-usage'].hidden = true;
let usageParagraphs = [];
reg['vl-usage-src'].querySelectorAll = () => usageParagraphs;
reg['vl-activity-key'].hidden = true;
reg['vl-keysheet'].hidden = true;
const list = el('list', 'OL');
const empty = el('empty');
reg['vl-activity-log'].hidden = true;
reg['vl-activity-log'].querySelector = (s) => (
  s === 'ol' ? list : s === '.vl-activity-empty' ? empty : null);
function drawer(id) {
  const toggle = el(id + '-toggle', 'BUTTON');
  toggle.click = () => {
    toggle.clicks += 1;
    const d = reg[id];
    if (d.classes.has('open')) { d.classes.delete('open'); } else { d.classes.add('open'); }
  };
  const close = el(id + '-close', 'BUTTON');
  reg[id].querySelector = (s) => (s === ':scope > .toggle-button' ? toggle
    : s === '[data-vl-drawer-close]' ? close : null);
  return { toggle, close };
}
const left = drawer('project-tools');
const right = drawer('settings-drawer');
let statusParagraphs = [{ textContent: '영상을 선택하면 미리보기가 이어집니다.' }];
reg['quick-status'].querySelectorAll = () => statusParagraphs;
const listeners = {};
const observers = [];
const window = { setInterval() {}, };
const document = {
  body: { appendChild(child) { reg[child.id] = child; } },
  getElementById: (id) => reg[id] || null,
  querySelector: (s) => null,
  querySelectorAll: () => [],
  createElement: (tag) => el('', tag),
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
};
class MutationObserver {
  constructor(fn) { this.fn = fn; observers.push(this); }
  observe() {}
  disconnect() {}
}
global.requestAnimationFrame = (fn) => fn();
// 선택자 목록을 실제로 맞춰 보는 closest — 태그와 [속성="값"]만 읽으면 충분하다.
function matches(node, sel) {
  return sel.split(',').some((part) => {
    const s = part.trim();
    const tag = (s.match(/^[a-z]+/) || [null])[0];
    if (tag && node.tagName.toLowerCase() !== tag) { return false; }
    const want = [...s.matchAll(/\[([\w-]+)(?:="([^"]*)")?\]/g)];
    if (!tag && !want.length) { return false; }
    return want.every(([, k, v]) => k in node.attrs && (v === undefined || node.attrs[k] === v));
  });
}
function real(node, attrs) {
  Object.assign(node.attrs, attrs || {});
  node.closest = (sel) => (matches(node, sel) ? node : null);
  return node;
}
const press = (target) => (listeners.keydown || []).forEach((fn) => fn(
  { key: 'Escape', isComposing: false, target }));
const run = new Function('window', 'document', 'MutationObserver', 'element', source);
run(window, document, MutationObserver, null);
const A = window.__vlActivity;
const out = {};
out.firstStatus = reg['vl-activity-msg'].textContent;
// 1초 다시 그리기 — 같은 글이면 기록이 늘지 않는다.
observers[0].fn([]); observers[0].fn([]);
out.entriesAfterRedraw = A.entries.length;
out.usageAtLoad = reg['vl-activity-usage'].hidden;
// 유료 실행 뒤의 실사용 줄 — 기록 판 맨 위 고정 줄에 그대로(기록 항목은 늘지 않는다).
const usage = '실사용 $0.4123 · 전사 812초 · Luna 입력 182,330/출력 9,812 토큰 ' +
  '(불확실 0건·가격 미정 0건)';
usageParagraphs = [{ textContent: usage }];
A.readUsage();
out.usage = { hidden: reg['vl-activity-usage'].hidden, text: reg['vl-activity-usage'].textContent,
  entries: A.entries.length };
const markdown = '**취소를 요청했습니다.**\n\n완료된 결과는 `보존`됩니다. window.__xss ' +
  '[문서](https://x.test/a_(b))';
A.reply(markdown);
out.reply = { text: reg['vl-activity-msg'].textContent, title: reg['vl-activity-msg'].title,
  kind: reg['vl-activity'].attrs['data-vl-kind'], entries: A.entries.length };
A.reply(markdown);
out.sameReplyLogged = A.entries.length;
// 응답과 같은 글의 상태 줄 — 두 번 적지 않는다.
statusParagraphs = [{ textContent: reg['vl-activity-msg'].textContent }];
observers[0].fn([]);
out.statusAfterSameReply = A.entries.length;
// 사건의 출력이 상태 줄을 먼저 갈아 쓰고 응답이 뒤따라와도 한 번만 적는다.
statusParagraphs = [{ textContent: '거절 · 견적 $1.49' }];
observers[0].fn([]);
A.reply('**거절** · 견적 $1.49');
out.replyAfterSameStatus = { entries: A.entries.length, kind: A.entries[0].kind };
statusParagraphs = [{ textContent: '연결 설정 필요 · 아직 분석하지 않았습니다' }];
observers[0].fn([]);
out.keyButton = reg['vl-activity-key'].hidden;
for (let i = 0; i < 30; i += 1) { A.reply('응답 ' + i); }
out.capped = A.entries.length;
out.scrimBefore = reg['vl-scrim'] ? reg['vl-scrim'].hidden : 'missing';
window.__vlDrawer('project-tools', true);
out.leftOpen = { open: reg['project-tools'].classes.has('open'), clicks: left.toggle.clicks,
  focused: left.close.focused, scrim: reg['vl-scrim'].hidden };
// 막을 누르면 서랍만 닫힌다(그 뒤의 승인 단추에는 닿지 않는다).
(listeners.click || []).forEach((fn) => fn({ target: reg['vl-scrim'] }));
out.scrimClick = { left: reg['project-tools'].classes.has('open'), scrim: reg['vl-scrim'].hidden };
window.__vlDrawer('project-tools', true);
window.__vlDrawer('settings-drawer', true);
out.rightOpen = { right: reg['settings-drawer'].classes.has('open'),
  left: reg['project-tools'].classes.has('open') };
window.__vlDrawer('settings-drawer', true);
out.idempotent = right.toggle.clicks;
// Esc는 입력 칸·펼친 고르기 칸의 몫 — 서랍을 닫지 않는다(진짜 선택자로 맞춰 본다).
press(real(el('field', 'INPUT')));
press(real(el('combo', 'DIV'), { role: 'combobox', 'aria-expanded': 'true' }));
press(real(el('options', 'UL'), { role: 'listbox' }));
out.escInField = reg['settings-drawer'].classes.has('open');
// 단축키 판을 연 단추 위의 Esc — 그 단추도 펼침 표시를 달지만 판을 닫는다.
const opener = real(el('keys', 'BUTTON'), { 'data-vl-keys-open': '' });
document.querySelectorAll = (sel) => (sel === '[data-vl-keys-open]' ? [opener] : []);
(listeners.click || []).forEach((fn) => fn({ target: opener }));
out.sheetOpened = { open: !reg['vl-keysheet'].hidden, expanded: opener.attrs['aria-expanded'] };
press(opener);
out.sheetEscaped = { open: !reg['vl-keysheet'].hidden, expanded: opener.attrs['aria-expanded'],
  drawer: reg['settings-drawer'].classes.has('open') };
press({ closest: () => null });
out.escaped = { open: reg['settings-drawer'].classes.has('open'), scrim: reg['vl-scrim'].hidden };
A.openKey(false);
out.keyNotice = { open: reg['settings-drawer'].classes.has('open'),
  scrim: reg['vl-scrim'].hidden };
process.stdout.write(JSON.stringify(out));
"""


@needs_node
def test_shell_script_runs_the_activity_area_the_drawers_and_the_scrim(tmp_path: Path) -> None:
    harness = tmp_path / "shell.js"
    harness.write_text(_SHELL_HARNESS, encoding="utf-8")
    script = tmp_path / "shell_src.js"
    script.write_text(ui_facade.SHELL_JS, encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(script)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    out = json.loads(completed.stdout)
    assert out["firstStatus"] == "영상을 선택하면 미리보기가 이어집니다."
    assert out["entriesAfterRedraw"] == 1  # 같은 글의 다시 그리기는 기록하지 않는다
    assert out["usageAtLoad"] is True  # 유료 실행 전에는 고정 줄이 없다
    usage = (
        "실사용 $0.4123 · 전사 812초 · Luna 입력 182,330/출력 9,812 토큰 (불확실 0건·가격 미정 0건)"
    )
    assert out["usage"] == {"hidden": False, "text": usage, "entries": 1}
    # 걷는 것은 굵게 표지와 링크 주소뿐 — 역따옴표·밑줄 두 개(식별자)는 그대로다.
    text = "취소를 요청했습니다. · 완료된 결과는 `보존`됩니다. window.__xss 문서"
    assert out["reply"] == {"text": text, "title": text, "kind": "reply", "entries": 2}
    assert out["sameReplyLogged"] == 3  # 누른 만큼 남는다
    assert out["statusAfterSameReply"] == 3  # 응답과 같은 상태 줄은 두 번 적지 않는다
    assert out["replyAfterSameStatus"] == {"entries": 4, "kind": "reply"}  # 반대 순서도 한 번
    assert out["keyButton"] is False  # 키가 없다는 알림이면 「API 연결 열기」가 보인다
    assert out["capped"] == 20  # 기록은 최근 20개
    assert out["scrimBefore"] is True  # 막은 처음부터 있고 서랍이 닫혀 있으면 숨는다
    assert out["leftOpen"] == {"open": True, "clicks": 1, "focused": 1, "scrim": False}
    assert out["scrimClick"] == {"left": False, "scrim": True}  # 막을 누르면 서랍만 닫힌다
    assert out["rightOpen"] == {"right": True, "left": False}  # 서랍은 하나씩
    assert out["idempotent"] == 1  # 열린 서랍을 다시 열라고 해도 닫히지 않는다
    assert out["escInField"] is True  # 입력 칸·펼친 고르기 칸의 Esc는 서랍을 닫지 않는다
    assert out["sheetOpened"] == {"open": True, "expanded": "true"}
    # 판을 연 단추 위의 Esc는 판만 닫는다(서랍은 그대로 — 판부터 닫는다).
    assert out["sheetEscaped"] == {"open": False, "expanded": "false", "drawer": True}
    assert out["escaped"] == {"open": False, "scrim": True}  # 그 밖의 Esc는 서랍을 닫는다
    assert out["keyNotice"] == {"open": True, "scrim": False}  # 키 알림은 설정 서랍을 연다


_GUARD_HARNESS = r"""
const fs = require('fs');
const guard = new Function('return ' + fs.readFileSync(process.argv[2], 'utf8'))();
const quotes = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
// 그라디오 마크다운 칸의 실제 꼴: 감싼 칸 안에 상태 표시(1초 틱마다 '0.1s' 같은 글이 잠깐
// 들어온다)와 그려진 마크다운 노드가 형제로 있다.
function scenario(opts) {
  const calls = { scrolled: 0, toggled: [] };
  const child = {};
  const text = opts.text === undefined ? '\n  ' + quotes.raw.replace(' · ', ' ·\n ') + ' \n'
    : opts.text;
  const md = { contains: (node) => node === md || node === child,
    getBoundingClientRect: () => opts.box, textContent: text,
    clientHeight: opts.box.height, scrollHeight: opts.scrollHeight || opts.box.height };
  const tracker = { textContent: opts.tracker || '' };
  const quote = { contains: (node) => [quote, md, child, tracker].includes(node),
    getBoundingClientRect: () => opts.box, textContent: tracker.textContent + ' ' + text,
    clientHeight: opts.box.height, scrollHeight: opts.box.height,
    querySelector: (sel) => (sel === '[data-testid="markdown"]' && !opts.noMarkdown ? md : null),
    scrollIntoView: () => { calls.scrolled += 1; } };
  global.window = { innerWidth: 1280, innerHeight: 800,
    getComputedStyle: () => ({ display: opts.display || 'block', visibility: 'visible' }),
    __vlDrawer: (id, open) => { if (open === false) { calls.toggled.push(id); } } };
  global.document = {
    getElementById: (id) => (id === 'library-analysis-scope' ? quote : null),
    querySelector: (sel) => (opts.drawer && sel.includes('.open') ? {} : null),
    querySelectorAll: () => [],
    elementFromPoint: () => (opts.covered ? { id: 'vl-scrim' } : child),
  };
  const sent = guard('sig', 'key', opts.shown === undefined ? quotes.shown : opts.shown);
  return { quote: sent[2], scrolled: calls.scrolled, closed: calls.toggled.length };
}
const box = { left: 955, top: 107, right: 1253, bottom: 175, width: 298, height: 68 };
const below = { left: 16, top: 900, right: 984, bottom: 980, width: 968, height: 80 };
process.stdout.write(JSON.stringify({
  visible: scenario({ box }),
  // 1초 틱의 상태 표시가 '0.1s'를 든 순간 — 그려진 견적은 그대로다.
  tracker: scenario({ box, tracker: '0.1s' }),
  drawer: scenario({ box, drawer: true }),
  offscreen: scenario({ box: below }),
  covered: scenario({ box, covered: true }),
  hidden: scenario({ box, display: 'none' }),
  // 칸 안에서 잘린 견적(넘친 줄이 가려짐) · 보낼 문구와 다른 글(아직 안 바뀐 화면).
  clipped: scenario({ box, scrollHeight: box.height + 24 }),
  mismatch: scenario({ box, text: quotes.raw.replace('$1.49', '$0.99') }),
  // 그려진 마크다운 노드를 못 찾으면 닫힌 쪽으로 — 같은 거절 길.
  noMarkdown: scenario({ box, noMarkdown: true }),
  // 마크다운이 움직일 글자(~ _ * [ ] ( ) ` $)도 escape된 값은 글 그대로 그려진다.
  special: scenario({ box, shown: quotes.specialShown, text: quotes.specialRaw }),
}));
"""


@needs_node
def test_approve_pre_step_sends_the_quote_only_when_a_person_can_see_it(tmp_path: Path) -> None:
    """견적 칸이 서랍·막에 덮였거나 화면 밖·숨김·잘림·다른 글이면 빈 견적 → 서버가 거절(D54).

    읽는 것은 그려진 마크다운 노드뿐이다 — 감싼 칸의 상태 표시(1초 틱의 '0.1s')는 보지 않는다.
    보내는 값은 화면 칸의 값(escape된 마크다운)을 되푼 견적 원문이다(서버 대조는 그대로).
    """
    raw = "분석 대상 3개 · Claude 비용 상한 $1.49"
    special = "약 2~3분 · _x_ · *y* · [t](u) · `c` · $1.49 + $2.50 (보수적)"
    harness = tmp_path / "guard.js"
    harness.write_text(_GUARD_HARNESS, encoding="utf-8")
    script = tmp_path / "guard_src.js"
    script.write_text(ui_facade.APPROVE_GUARD_JS, encoding="utf-8")
    quotes = tmp_path / "quotes.json"
    quotes.write_text(
        json.dumps(
            {
                "raw": raw,
                "shown": ui_facade.literal_markdown(raw),
                "specialRaw": special,
                "specialShown": ui_facade.literal_markdown(special),
            }
        ),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(script), str(quotes)],
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    out = json.loads(completed.stdout)
    assert out["visible"] == {"quote": raw, "scrolled": 0, "closed": 0}
    assert out["tracker"] == {"quote": raw, "scrolled": 0, "closed": 0}  # 틱 글자에 속지 않는다
    # 서랍이 열려 있으면 서랍을 닫고 견적을 보이게 한 뒤 빈 견적을 보낸다.
    assert out["drawer"] == {"quote": "", "scrolled": 1, "closed": 2}
    for case in ("offscreen", "covered", "hidden", "clipped", "mismatch", "noMarkdown"):
        assert out[case]["quote"] == "" and out[case]["scrolled"] == 1, case
    assert out["special"] == {"quote": special, "scrolled": 0, "closed": 0}


def test_the_quote_cell_draws_the_quote_text_exactly(tmp_path: Path, local_socket: None) -> None:
    """견적 칸은 견적 원문을 글 그대로 그린다 — 마크다운이 움직일 글자는 모두 escape한다.

    CommonMark는 백슬래시 뒤 ASCII 문장부호를 글자 그대로 그린다(2~3분이 23분이 되지 않는다).
    LaTeX 구분자도 끈다. 서버 대조(`shown_summary == summary`)는 원문 그대로다.
    """
    punctuation = "".join(chr(c) for c in range(0x21, 0x7F) if not chr(c).isalnum())
    escaped = ui_facade.literal_markdown(punctuation)
    assert escaped == "".join("\\" + ch for ch in punctuation)  # 하나도 빠짐없이
    plain = "분석 대상 3개 · 전사 최대 3회 1234"
    assert ui_facade.literal_markdown(plain) == plain  # 한글·숫자·가운뎃점·빈칸은 그대로
    server = _server(tmp_path)
    server.build_blocks()
    summary = server.library_analysis_summary()
    assert server.library_analysis_summary_markdown() == ui_facade.literal_markdown(summary)
    cell = server._library_summary_md
    assert cell.value == ui_facade.literal_markdown(summary) and cell.latex_delimiters == []


def test_a_blank_shown_quote_is_refused_with_the_quote_in_full(
    tmp_path: Path, local_socket: None
) -> None:
    """서버 결합은 그대로 — 빈 견적 문구는 지금 견적과 달라 「새 견적을 확인하세요」로 거절된다.

    거절 답(견적·상한 포함)은 견적 칸 전문에 말줄임 없이 다 실린다(활동 칸은 한 줄일 뿐이다).
    """
    server = _server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    signature = server.current_signature()
    summary = (
        "분석 대상 18개 · 저장 결과 2개 재사용 · 전사 최대 23회 + 영상 분석(Claude Opus 5.5) "
        "최대 195회 · Claude 비용 상한 $48.39 (보수적). 승인하면 두 단계가 이어집니다. "
        "최대 3개를 동시에 분석합니다."
    )
    server._library_quotes[root] = SimpleNamespace(
        asset_ids=("a",), digest="d", workflow="w", estimates=()
    )
    server._library_ids = lambda _root: ("a",)  # type: ignore[method-assign]
    server.library_analysis_summary = lambda: summary  # type: ignore[method-assign]
    usage = "실사용 $0.4123 · 전사 812초 (불확실 0건·가격 미정 0건)"
    server._usage_line = lambda: usage  # type: ignore[method-assign]  # 앞선 유료 실행의 흔적
    status, _panel, _key = server.library_analysis_view(signature, "", "")
    assert "새 견적을 확인하세요" in status and summary in status  # 견적·상한을 직접 싣는다
    assert server._approval_jobs.get(root) is None  # 아무것도 시작하지 않았다
    note = server.quote_note_markdown()
    # 전문 칸은 거절 알림 자신의 글 — 견적·상한은 다 싣고, 실사용 줄은 싣지 않는다.
    assert status == f"{note}\n\n{usage}"
    assert note.count("$48.39 (보수적)") == 1 and len(note) > 200 and "실사용" not in note
    view = server.quote_note_view(signature)
    assert view.visible is True and view.value == note
    css = ui_facade.WORKBENCH_CSS
    rule = css[css.index("#library-analysis-scope, #vl-quote-note {") :]
    rule = rule[: rule.index("}")]
    assert "white-space: normal;" in rule and "ellipsis" not in rule  # 여러 줄, 말줄임 없음


def test_a_usage_only_status_puts_nothing_above_the_player(
    tmp_path: Path, local_socket: None
) -> None:
    """유료 실행 뒤 상태 줄에는 언제나 「실사용 $…」가 붙는다 — 그것만으로는 전문 칸이 뜨지 않는다.

    전문 칸은 1050px 이하에서 재생기 위에 선다(결정 6: 영상·타임라인 먼저). 실사용 줄은 대신
    `#vl-usage-src`를 거쳐 기록 판의 고정 줄에 말줄임 없이 다 보인다.
    """
    server = _server(tmp_path)
    signature = server.current_signature()
    assert server.quote_note_markdown() == ""
    assert server.quote_note_view(signature).visible is False
    usage = "실사용 $0.4123 · 전사 812초 · 승인 상한 $1.49 (불확실 0건·가격 미정 0건)"
    server._usage_line = lambda: usage  # type: ignore[method-assign]
    assert usage in server._quick_status()  # 상태 줄(활동 칸)에는 그대로 붙는다
    assert server.quote_note_markdown() == ""  # 재생기 위에는 오지 않는다
    note, source = server.quote_cells_view(signature)
    assert note.visible is False and source == usage  # 기록 판의 원천은 실사용 줄 그대로
    assert server.quote_note_view("다른 서명") == gr.skip()  # 다른 화면의 틱은 건너뛴다
    assert server.quote_cells_view("다른 서명") == (gr.skip(), gr.skip())


def test_the_quote_note_clears_when_the_next_approve_passes_or_the_quote_changes(
    tmp_path: Path, local_socket: None
) -> None:
    """규칙: 전문 칸은 살아 있는 승인 알림 가운데 견적·금액을 실은 것만 싣는다.

    다음 승인 클릭이 견적 대조를 지나 새 알림(키 필요·접수…)으로 바뀌거나, 견적이 바뀌어
    알림이 물러나면 칸이 걷힌다.
    """
    server = _server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    # 키 없음 길로 고정한다 — 다른 시험 모듈이 수집 때 가짜 키를 환경에 남기면(전체 실행) 서버가
    # 그 키를 찍어 다음 클릭이 접수까지 간다.
    server._startup_credentials = None
    server._live_transport = None
    signature = server.current_signature()
    summary = (
        "분석 대상 1개 · 전사 최대 1회 + 영상 분석(Claude Opus 5.5) 최대 2회 · "
        "Claude 비용 상한 $0.50 (보수적)."
    )
    server._library_quotes[root] = SimpleNamespace(
        asset_ids=("a",), digest="d", workflow="w", estimates=()
    )
    server._library_ids = lambda _root: ("a",)  # type: ignore[method-assign]
    server.library_analysis_summary = lambda: summary  # type: ignore[method-assign]
    server.library_analysis_view(signature, "", "")  # 빈 문구 → 거절
    assert summary in server.quote_note_markdown()
    # 견적을 다시 본 다음 클릭은 대조를 지난다 — 오프라인·키 없음이라 키 알림에서 멈춘다.
    status, _panel, _key = server.library_analysis_view(signature, "", summary)
    assert "연결 설정 필요" in status and server._approval_jobs.get(root) is None
    assert server.quote_note_markdown() == ""
    # 다시 거절된 뒤 견적이 바뀌면(모델·범위) 알림이 물러나고 칸도 걷힌다.
    server.library_analysis_view(signature, "", "")
    assert summary in server.quote_note_markdown()
    server._library_quotes[root] = SimpleNamespace(
        asset_ids=("a",), digest="d2", workflow="w", estimates=()
    )
    assert server.quote_note_markdown() == ""
    # 승인 길의 다른 답이라도 금액을 실었으면 다 싣는다(예: 시작 실패가 상한을 말할 때).
    failed = "승인한 분석을 시작하지 못했습니다: 비용 상한 $0.50을 넘는 요청"
    server._set_approval_notice(root, workspace_service.NOTICE_FAILED, failed)
    assert server.quote_note_markdown() == failed
    server._set_approval_notice(root, workspace_service.NOTICE_ACCEPTED)
    assert server.quote_note_markdown() == ""  # 접수 알림은 금액이 없다


# ── 실행본(node) — 카드 요약·결정 상자·머리줄 옮기기(작업대 하네스 재사용) ──────────

_PANEL_SCRIPT = r"""
// 카드·컷 어느 길로 와도 같은 결정 상자다 — 빨강 막대를 누르지 않고 카드로 고른다.
if (windowScope.__vlVisualWorkbenchState) { windowScope.__vlVisualWorkbenchState.marker = null; }
const envCard = visualEnv({
  vl_apply: () => ({ ok: true, echoEpoch: 'epoch-1', message: '저장했다' }),
}, tierModel('all'));
envCard.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segA3' : null) });
const cardPanel = envCard.panel.innerHTML;
envCard.click({ getAttribute: (k) => (k === 'data-vl-act' ? 'red-confirm' : null) });
await new Promise((resolve) => setTimeout(resolve, 0));
const cardConfirm = envCard.serverCalls[0];
envCard.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segA1' : null) });
const plainPanel = envCard.panel.innerHTML;
// 머리줄 옮기기 — 작업대 값이 실은 흐름 조각·프로젝트 이름을 커밋 때 머리줄에 넣는다.
const envFlow = visualEnv({}, tierModel('all'));
envFlow.registry['vl-flow'] = el({ id: 'vl-flow' });
envFlow.registry['vl-project-name'] = el({ id: 'vl-project-name' });
envFlow.root.attrs['data-vl-flow'] = '<span class="vl-step cur">검토</span>';
envFlow.root.attrs['data-vl-project'] = '제주 <3일>';
flushObservers(envFlow.block);
checkpoints.push({ case: 'panel-b', cardPanel, plainPanel,
  cardConfirm: cardConfirm && { selection: cardConfirm.payload.selection,
    segmentId: cardConfirm.payload.segmentId },
  thumbs: envCard.thumbs.innerHTML,
  flow: envFlow.registry['vl-flow'].innerHTML,
  project: envFlow.registry['vl-project-name'].textContent });
"""


def _run_panel_harness(tmp_path: Path) -> dict[str, Any]:
    from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, _TIER_SCRIPT

    harness = tmp_path / "panel.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _TIER_SCRIPT + _PANEL_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "panel-b")


@needs_node
def test_card_path_opens_the_same_decision_box_in_a_fixed_panel_order(tmp_path: Path) -> None:
    report = _run_panel_harness(tmp_path)
    panel = report["cardPanel"]
    # 미검토 제외 제안 컷을 카드로 고르면 막대를 누른 것과 같은 상자 — 판정 · 규칙 · 두 단추.
    assert '<section class="vl-decision"' in panel and " · 미검토</span>" in panel
    assert "자막 · 자막 없음(긴 공백): 발화 없음 · 자막 없는 공백 5초 이상" in panel
    assert 'data-vl-act="red-confirm">제외 유지(확인)</button>' in panel
    assert 'data-vl-act="red-revert">사용으로 되돌리기</button>' in panel
    assert report["cardConfirm"] == {"selection": "excluded", "segmentId": "segA3"}
    # 순서는 고정 — 머리 · 결정 · 판정 줄 · 알림 · 편집 · 접힌 상세(메모는 근거와 따로).
    order = [
        panel.index("vl-panel-head"),
        panel.index('class="vl-decision"'),
        panel.index("상태 제외"),
        panel.index('data-vl-act="split"'),
        panel.index('data-vl-keep="evidence"'),
        panel.index('data-vl-keep="memo"'),
    ]
    assert order == sorted(order), order
    evidence = panel[panel.index('data-vl-keep="evidence"') : panel.index('data-vl-keep="memo"')]
    assert "vl-note-input" not in evidence  # 사람의 메모는 기계 근거와 다른 칸이다
    assert '<details class="vl-details" data-vl-keep="memo"><summary>메모</summary>' in panel
    assert '<select id="vl-track" class="vl-input">' in panel  # 소리 트랙은 전체 폭(접힌 칸 안)
    # 다른 컷은 결정 상자 없이 사용·제외 단추 하나.
    plain = report["plainPanel"]
    assert "vl-decision" not in plain and 'data-vl-act="toggle-sel"' in plain


@needs_node
def test_cards_summarise_the_whole_video_and_the_flow_reaches_the_top_bar(
    tmp_path: Path,
) -> None:
    report = _run_panel_harness(tmp_path)
    cards = report["thumbs"].split('<button type="button" class="vl-thumb')[1:]
    assert cards and all(card.count('class="vl-badge') <= 1 for card in cards)  # 배지는 하나뿐
    assert all('<span class="vl-mbar" aria-hidden="true">' in card for card in cards)
    assert report["flow"] == '<span class="vl-step cur">검토</span>'
    assert report["project"] == "제주 <3일>"  # textContent로만 넣는다(표지로 해석되지 않는다)


_DRAWER_KEY_SCRIPT = r"""
// 서랍을 열면 「닫기」에 초점이 간다 — 거기서 누른 스페이스가 막 뒤의 영상을 틀지 않는다.
const inOpenDrawer = { closest(sel) { return sel === '.sidebar.open' ? this : null; } };
vis.offsetParent = {}; vis.paused = true;
vis.play = function () { this.played += 1; this.paused = false; return Promise.resolve(); };
const playedBefore = vis.played;
const drawerSpace = fire('keydown', 'Space', { target: inOpenDrawer });
const ignored = vis.played === playedBefore && drawerSpace.prevented === 0;
// 서랍이 열린 채 초점이 막 뒤(예: 승인 단추)에 남아 있어도 재생 단축키가 아니다.
keyEl._doc.querySelector = (sel) => (sel === '.sidebar.open' ? {} : null);
const behind = fire('keydown', 'Space', { target: { closest() { return null; } } });
const behindIgnored = vis.played === playedBefore && behind.prevented === 0;
keyEl._doc.querySelector = () => null;
const outside = fire('keydown', 'Space', { target: { closest() { return null; } } });
checkpoints.push({ case: 'drawer-keys', ignored, behindIgnored,
  outsidePlays: vis.played === playedBefore + 1 && outside.prevented === 1 });
"""


@needs_node
def test_space_inside_an_open_drawer_does_not_play_the_video(tmp_path: Path) -> None:
    from tests.raw.test_visual_workbench_ui import _NODE_HARNESS, _SHORTCUT_SCRIPT

    harness = tmp_path / "drawer_keys.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _SHORTCUT_SCRIPT + _DRAWER_KEY_SCRIPT + "\nprocess.stdout.write("
            "JSON.stringify(checkpoints));",
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
    report = next(row for row in json.loads(completed.stdout) if row.get("case") == "drawer-keys")
    assert report == {
        "case": "drawer-keys",
        "ignored": True,
        "behindIgnored": True,
        "outsidePlays": True,
    }
