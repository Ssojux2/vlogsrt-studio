"""작업대는 보이는 대로 한다 — 화면과 동작 일치(W-24 세션 D, D64).

감사에서 찾은 어긋남마다 하나씩 못 박는다(유료 호출 0회).

- 목록은 영상 카드뿐이라 쪽 넘김이 없다 — 옛 「다음 쪽」은 그리지 않는 컷 목록을 넘겼다.
- 영상 카드는 모든 영상을 그리고, 카드의 대표 컷은 쪽과 무관하다(18개 작업공간 시험은
  `test_render_cost.py`가 같은 합성 작업공간으로 한다).
- 「사용할 컷」·「제외된 컷」은 영상마다 그 영상의 컷 전부로 판정한다.
- 필터는 탭의 것이다 — 틱·명령 줄 단추의 다시 그리기가 「전체」로 되돌리지 않는다.
- 「미리보기 다시 준비」는 고른 영상을 준비하고, 만들 것이 없으면 그렇게 말한다.
- 「선택 분석 취소」는 도는 분석만 멈춘다 — 끝난 분석을 「취소됨」으로 덮지 않는다. 「전체 분석
  취소」는 멈춘 것이 없으면 그렇게 말한다.
- 버려진 답의 「보내는 중」 각주는 남지 않는다.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_visual_workbench_ui import (  # noqa: E402
    _NODE_HARNESS,
    _model_of,
    _new_server,
    _prepare_review,
    _wait_terminal,
    needs_ffmpeg,
    needs_node,
)
from ui import ui_facade  # noqa: E402

_TRUTH_SCRIPT = r"""
const tState = () => windowScope.__vlVisualWorkbenchState;
function freshState() {
  tState().selectedId = null; tState().selectedPlan = null; tState().marker = null;
  tState().lastMsg = ''; tState().filter = 'all';
}
const act = (name) => ({ getAttribute: (k) => (k === 'data-vl-act' ? name : null) });
// 서버 모양 — 컷 목록(`cards`)은 지금 쪽(24컷)뿐이고 영상 카드는 영상마다 하나(대표 컷)다.
// 영상 B·C의 컷은 이 쪽에 없다(옛 화면은 「아직 없음」 대역 카드를 냈다).
function pagedModel() {
  const m = visualModel();
  m.page = 1; m.pageCount = 3; m.total = 60;
  m.cards = m.cards.filter((c) => c.sourceId === 'srcA');
  m.mediaCards = [
    { id: 'segA1', sourceId: 'srcA', sourceName: '01-landscape.mp4', start: '0', end: '3',
      dur: '3', sel: 'included', thumb: { state: 'ready', src: '/media/thumb-a' }, media: true },
    { id: 'segB1', sourceId: 'srcB', sourceName: '02-portrait-silent.mp4', start: '0',
      end: '7/4', dur: '7/4', sel: 'included', thumb: { state: 'ready', src: '/media/thumb-b' },
      media: true },
    { id: 'segC1', sourceId: 'srcC', sourceName: '03-pending.mp4', start: '0', end: '2',
      dur: '2', sel: 'included', thumb: { state: 'preparing', src: null }, media: true },
  ];
  // 영상 A — 컷 전부 사용. 영상 B — 대표 컷은 사용이지만 다른 컷이 제외다. 영상 C — 컷 전부 제외.
  m.sources.srcA.rail.intervals[2].sel = 'included';
  m.sources.srcB.rail.intervals[1].sel = 'excluded';
  m.sources.srcC.rail.intervals[0].sel = 'excluded';
  return m;
}
const shown = (html) => ['segA1', 'segB1', 'segC1']
  .filter((id) => html.includes('data-vl-card="' + id + '"'));
const pressed = (html) => (html.match(/data-vl-act="filter-([a-z]+)" aria-pressed="true"/) ||
  [])[1];

freshState();
const envPaged = visualEnv({ vl_prepare: () => ({ __hold: true }) }, pagedModel());
const pagedThumbs = envPaged.thumbs.innerHTML;
envPaged.click(act('prepare'));
await new Promise((resolve) => setTimeout(resolve, 0));
const prepareCall = envPaged.serverCalls[0];
checkpoints.push({ case: 'truth-cards',
  noPager: !pagedThumbs.includes('vl-pager') && !pagedThumbs.includes('쪽') &&
    !pagedThumbs.includes('page-next') && !pagedThumbs.includes('page-prev'),
  head: pagedThumbs.includes('<b>영상 3개</b> · 촬영 시각 순'),
  allVideos: shown(pagedThumbs),
  readyThumbB: pagedThumbs.includes('src="/media/thumb-b"'),
  noMissing: !pagedThumbs.includes('아직 없음'),
  prepare: prepareCall && { name: prepareCall.name, sourceId: prepareCall.payload.sourceId } });

// 필터 — 영상마다 그 영상의 컷 전부. 맞는 컷이 하나라도 있으면 보인다.
function filtered(filter) {
  freshState();
  const m = pagedModel();
  m.filter = filter;
  return visualVisible(m);
}
function visualVisible(m) {
  const env = visualEnv({}, m);
  return shown(env.thumbs.innerHTML);
}
const byFilter = { included: filtered('included'), excluded: filtered('excluded') };

// 필터는 탭의 것이다 — 필터를 실어 온 답(「제외된 컷」)이 그린 뒤 틱의 다시 그리기(필터 칸이
// 빈 모델)가 와도 그대로 남는다. 필터를 실어 온 다른 답(「전체」)만 바꾼다.
freshState();
const tabModel = pagedModel();
tabModel.filter = 'excluded';
const envTab = visualEnv({}, tabModel);
const beforeTick = { cards: shown(envTab.thumbs.innerHTML),
  pressed: pressed(envTab.thumbs.innerHTML) };
const tickModel = pagedModel();
tickModel.epoch = 'epoch-tick';
tickModel.filter = null;
envTab.root.setAttribute('data-vl-model', JSON.stringify(tickModel));
envTab.rewired();
const afterTick = { cards: shown(envTab.thumbs.innerHTML),
  pressed: pressed(envTab.thumbs.innerHTML), state: tState().filter };
const allModel = pagedModel();
allModel.epoch = 'epoch-all';
allModel.filter = 'all';
envTab.root.setAttribute('data-vl-model', JSON.stringify(allModel));
envTab.rewired();
const afterAll = { cards: shown(envTab.thumbs.innerHTML),
  pressed: pressed(envTab.thumbs.innerHTML) };
checkpoints.push({ case: 'truth-filter', byFilter, beforeTick, afterTick, afterAll });

// 버려진 답 — 같은 화면 생명의 틱이 그 사이 다시 그렸다(epoch만 바뀜). 다시 그린 화면은
// 「보내는 중」을 되살려 그리므로, 답이 버려지면 그 각주를 갈아 쓴다. 뒤에 나간 요청의 각주는
// 건드리지 않는다.
freshState();
const envDrop = visualEnv({ vl_refresh: () => ({ __hold: true }) }, pagedModel());
envDrop.click(act('refresh'));
await new Promise((resolve) => setTimeout(resolve, 0));
const redraw = pagedModel();
redraw.epoch = 'epoch-redrawn';
redraw.filter = null;
envDrop.root.setAttribute('data-vl-model', JSON.stringify(redraw));
envDrop.rewired();
const footBefore = envDrop.registry['vl-footnote'].textContent;
envDrop.held[0].deferred.resolve({ ok: true, echoEpoch: 'epoch-1', html: '<div>', message: '' });
await new Promise((resolve) => setTimeout(resolve, 0));
const footAfter = envDrop.registry['vl-footnote'].textContent;
envDrop.click(act('refresh'));
await new Promise((resolve) => setTimeout(resolve, 0));
envDrop.click(act('filter-included'));
await new Promise((resolve) => setTimeout(resolve, 0));
const redraw2 = pagedModel();
redraw2.epoch = 'epoch-redrawn-2';
redraw2.filter = null;
envDrop.root.setAttribute('data-vl-model', JSON.stringify(redraw2));
envDrop.rewired();
const footLater = envDrop.registry['vl-footnote'].textContent;
envDrop.held[1].deferred.resolve({ ok: true, echoEpoch: 'epoch-redrawn', html: '<div>',
  message: '' });
await new Promise((resolve) => setTimeout(resolve, 0));
checkpoints.push({ case: 'truth-dropped', footBefore, footAfter, footLater,
  footKeptForLater: envDrop.registry['vl-footnote'].textContent,
  props: envDrop.propsCalls.length });

// 첫 쪽 밖 영상 — 쪽 목록(`cards`)에는 영상 A의 컷뿐이고, 고른 영상 B의 컷은 서버가 따로 싣는다
// (`viewCards`). 레일 구간의 썸네일과 컷 찾기가 쪽과 무관하게 그 컷을 본다.
// 서버가 고른 컷(`selectedCard`)은 다른 컷이라 그 길로는 찾지 못한다 — `viewCards`만 안다.
function viewModel(selectedIndex) {
  const m = pagedModel();
  const b = visualModel().cards.filter((c) => c.sourceId === 'srcB');
  b[1].thumb = { state: 'ready', src: '/media/thumb-b2' };
  m.viewCards = b;
  m.selectedId = b[selectedIndex].id; m.selectedSourceId = 'srcB';
  m.selectedCard = b[selectedIndex];
  return m;
}
freshState();
const envRail = visualEnv({}, viewModel(0));
const viewRail = envRail.rail.innerHTML.split('<button')
  .filter((b) => b.includes('data-vl-interval'));
freshState();
const envView = visualEnv({ vl_select_source: () => ({ __hold: true }) }, viewModel(1));
envView.click({ getAttribute: (k) => (k === 'data-vl-card' ? 'segB1' : null) });
checkpoints.push({ case: 'truth-view', viewRail,
  head: (envView.panel.innerHTML.match(/<p class="vl-kv vl-panel-head">.*?<\/p>/) || [''])[0],
  selected: tState().selectedId });
"""


def _run(tmp_path: Path, case: str) -> dict[str, Any]:
    harness = tmp_path / "truth.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _TRUTH_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
        ),
        encoding="utf-8",
    )
    payload = tmp_path / "payload.json"
    payload.write_text(
        json.dumps({"visual": ui_facade.VISUAL_BOOTSTRAP_JS}),
        encoding="utf-8",
    )
    completed = subprocess.run(
        ["node", str(harness), str(payload)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2500:]
    return next(row for row in json.loads(completed.stdout) if row.get("case") == case)


@needs_node
def test_the_list_has_no_pager_and_draws_every_video_card(tmp_path: Path) -> None:
    report = _run(tmp_path, "truth-cards")
    assert report["noPager"] is True  # 서버에 쪽이 셋이어도 넘길 것이 없다
    assert report["head"] is True
    assert report["allVideos"] == ["segA1", "segB1", "segC1"]  # 이 쪽에 컷이 없는 영상까지
    assert report["readyThumbB"] is True and report["noMissing"] is True


@needs_node
def test_prepare_asks_for_the_selected_video(tmp_path: Path) -> None:
    report = _run(tmp_path, "truth-cards")
    assert report["prepare"] == {"name": "vl_prepare", "sourceId": "srcA"}


@needs_node
def test_use_and_exclude_filters_judge_each_video_by_all_its_cuts(tmp_path: Path) -> None:
    by_filter = _run(tmp_path, "truth-filter")["byFilter"]
    # B는 대표 컷이 사용이어도 제외 컷이 있어 「제외된 컷」에 보이고, C는 사용 컷이 없다.
    assert by_filter["included"] == ["segA1", "segB1"]
    assert by_filter["excluded"] == ["segB1", "segC1"]


@needs_node
def test_the_tab_filter_survives_a_redraw_without_a_filter(tmp_path: Path) -> None:
    report = _run(tmp_path, "truth-filter")
    assert report["beforeTick"] == {"cards": ["segB1", "segC1"], "pressed": "excluded"}
    assert report["afterTick"] == {
        "cards": ["segB1", "segC1"],
        "pressed": "excluded",
        "state": "excluded",
    }
    assert report["afterAll"] == {"cards": ["segA1", "segB1", "segC1"], "pressed": "all"}


@needs_node
def test_a_dropped_reply_does_not_leave_the_sending_footnote(tmp_path: Path) -> None:
    report = _run(tmp_path, "truth-dropped")
    assert report["footBefore"] == "서버에 보내는 중입니다…"  # 다시 그린 화면이 되살렸다
    assert "보내는 중" not in report["footAfter"]
    assert "반영하지 않았습니다" in report["footAfter"]
    # 뒤에 나간 요청(필터)이 각주를 맡았으면 앞 요청의 버려진 답은 그 각주를 건드리지 않는다.
    assert report["footLater"] == report["footKeptForLater"] == "서버에 보내는 중입니다…"
    assert report["props"] == 0  # 버려진 답은 화면을 바꾸지 않는다


@needs_node
def test_rail_thumbnails_come_from_the_selected_video_beyond_the_page(tmp_path: Path) -> None:
    rail = _run(tmp_path, "truth-view")["viewRail"]
    assert len(rail) == 2 and 'data-vl-interval="segB2"' in rail[1]
    assert "background-image:url('/media/thumb-b2')" in rail[1]  # 쪽 밖 컷의 제 썸네일
    assert "background-image:url('/media/thumb-b')" in rail[0]


@needs_node
def test_find_cut_resolves_a_cut_of_a_video_beyond_the_page(tmp_path: Path) -> None:
    report = _run(tmp_path, "truth-view")
    # 영상 B 카드(대표 컷 segB1)를 누르면 영상 전체(0~1.75초)가 아니라 그 컷의 범위다.
    assert report["selected"] == "segB1"
    assert "컷 #1 · 1.500~2.000초 (0.500초)" in report["head"], report["head"]


# ── 서버 — 「선택 분석 취소」·「미리보기 다시 준비」 ─────────────────────────────


def _selected_task(server: Any, root: Path, stage: str) -> dict[str, Any]:
    state = {"root": root, "token": "task-token", "stage": stage, "asset_ids": ()}
    server._quick = state
    server._quick_tasks[state["token"]] = state
    return state


def test_cancel_on_a_finished_analysis_changes_nothing(tmp_path: Path) -> None:
    server = _new_server(tmp_path)
    root = server.workspace_root()
    task = _selected_task(server, root, "추천 구간 준비 완료")
    generation = server._capcut_render_generation
    reply = server.capcut_cancel_view(server.current_signature())
    assert "취소할 선택 분석 작업이 없습니다" in reply
    assert task == {
        "root": root,
        "token": "task-token",
        "stage": "추천 구간 준비 완료",
        "asset_ids": (),
    }
    assert server._quick is task and server._capcut_render_generation == generation


def test_cancel_all_with_nothing_running_says_so(tmp_path: Path) -> None:
    server = _new_server(tmp_path)
    task = _selected_task(server, server.workspace_root(), "추천 구간 준비 완료")
    reply = server.capcut_cancel_all_view(server.current_signature())
    assert reply == "취소할 분석이 없습니다. 끝난 결과는 그대로입니다."
    assert task["stage"] == "추천 구간 준비 완료" and task["token"] == "task-token"


def test_cancel_on_a_running_analysis_still_cancels(tmp_path: Path) -> None:
    server = _new_server(tmp_path)
    root = server.workspace_root()
    task = _selected_task(server, root, "분석 대기")
    reply = server.capcut_cancel_view(server.current_signature())
    assert reply == "취소를 요청했습니다. 완료된 결과는 보존됩니다."
    assert task["stage"] == "취소됨" and task["token"] != "task-token"


@needs_ffmpeg
def test_preview_reprepare_targets_the_selected_video_and_says_when_nothing_is_left(
    tmp_path: Path,
) -> None:
    server = _new_server(tmp_path)
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    model = _model_of(server.visual_workbench_render(root)[1])
    fns = {fn.__name__: fn for fn in server._visual_server_functions()}

    def prepare(shown: dict[str, Any], **extra: Any) -> dict[str, Any]:
        payload = {
            "epoch": shown["epoch"],
            "token": shown["token"],
            "signature": shown["signature"],
            "page": 1,
            "selectedId": shown["selectedId"],
            **extra,
        }
        return fns["vl_prepare"](payload)

    unchosen = prepare(model)
    assert unchosen["ok"] and unchosen["message"] == "미리보기를 준비할 영상을 먼저 고르세요."
    assert not [job for job in server.raw.list_jobs(root) if job.kind == "visual_prepare"]

    started = prepare(model, sourceId=model["selectedSourceId"])
    assert started["ok"] and "이 영상의 미리보기 준비 작업" in started["message"]
    jobs = [job for job in server.raw.list_jobs(root) if job.kind == "visual_prepare"]
    assert len(jobs) == 1
    assert _wait_terminal(server, root, jobs[0].job_id).status == "succeeded"
    ready = _model_of(server.visual_workbench_render(root)[1])
    assert all(card["thumb"]["state"] == "ready" for card in ready["cards"])

    again = prepare(ready, sourceId=ready["selectedSourceId"])
    assert again["ok"] and "새로 만들 것이 없습니다" in again["message"]
    assert "시작했습니다" not in again["message"]
    assert [j.job_id for j in server.raw.list_jobs(root) if j.kind == "visual_prepare"] == [
        jobs[0].job_id
    ]  # 작업을 걸지 않았다
