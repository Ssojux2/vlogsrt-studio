"""D61 — 「기록」 판의 서버 쪽: 지금 도는 작업 · 서버 로그 (읽기 전용 경로와 화면).

경로 `/vl-activity`는 다른 경로와 같은 Host/Origin 가드를 지나고, 쓰기·작업 제출·유료 호출이
없다. 화면은 바쁘거나 판이 열린 동안 2초마다, 한가하면 15초마다 읽고(D101), 서버 글은 전부
`textContent`로 넣는다.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)
pytest.importorskip(
    "fastapi",
    reason="웹 UI extra(fastapi)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from fastapi.testclient import TestClient  # noqa: E402

from ui import ui_facade, workspace_service  # noqa: E402
from ui.activity_log import ActivityLog  # noqa: E402
from vlogsrt import preprocess  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402
from vlogsrt.project.raw.jobs import JobContext  # noqa: E402
from vlogsrt.project.raw.models import SourceAssetDraft  # noqa: E402

PORT = 7993
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")
SOURCE_NAME = "DJI_20260901_0016_D.mov"


@pytest.fixture
def ui(tmp_path: Path, local_socket: None) -> workspace_service.RawWorkspaceUi:
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    status = server.raw.create_workspace(tmp_path / "ws", "기록")
    server._remember(Path(status.workspace_root))
    server.activity_log = ActivityLog()
    return server


def _client(server: workspace_service.RawWorkspaceUi) -> TestClient:
    return TestClient(server.build_fastapi(mount_blocks=False), base_url="http://127.0.0.1")


def _add_source(server: workspace_service.RawWorkspaceUi, tmp_path: Path) -> Path:
    library = tmp_path / "Library.fcpbundle"
    draft = SourceAssetDraft(
        source_root=str(library),
        source_relative_path=f"Original Media/{SOURCE_NAME}",
        source_name=SOURCE_NAME,
        size_bytes=10,
        source_mtime_ns=1,
        source_inode=1,
        capture_time=None,
        duration_seconds=545.0,
        width=None,
        height=None,
        ordinal=1,
        basis_note="시험",
    )
    root = Path(server.workspace_root())
    server.raw._workspace(root).create_ingest_batch((draft,), "시험")
    return library / "Original Media" / SOURCE_NAME


def _activity(**fields: object) -> preprocess.ProcessActivity:
    values: dict[str, object] = {
        "process_id": 1,
        "step": "분석 프록시 만들기",
        "tool": "ffmpeg",
        "input_path": None,
        "output_path": None,
        "started_at": time.time(),
        "timeout_seconds": None,
        "progress_seconds": None,
        "whole_input": True,
    }
    values.update(fields)
    return preprocess.ProcessActivity(**values)  # type: ignore[arg-type]


def _workspace_bytes(root: Path) -> dict[str, str]:
    """DB 내용의 바이트 — DB 파일과 WAL 쓰기 기록(`-wal`).

    `-shm`은 WAL 색인(공유 메모리)이라 **읽기도** 읽기 표지를 적는다(D63) — 내용이 아니다.
    쓰기는 반드시 `-wal`(또는 체크포인트된 DB)에 남으므로 그 둘로 「읽기만 한다」를 잰다.
    """
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.glob("project.sqlite3*"))
        if not path.name.endswith("-shm")
    }


# ── 경로 — 모양 ──────────────────────────────────────────────────────────────


def test_processes_name_the_source_and_show_elapsed_limit_and_percent(
    ui: workspace_service.RawWorkspaceUi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _add_source(ui, tmp_path)
    now = time.time()
    monkeypatch.setattr(
        workspace_service.preprocess,
        "active_processes",
        lambda: (
            _activity(
                input_path=str(original),
                started_at=now - 1510.4,
                timeout_seconds=3271.0,
                progress_seconds=338.2,
            ),
            _activity(
                process_id=2,
                step="썸네일 추출",
                input_path="/elsewhere/clip.mov",
                started_at=now - 5,
                progress_seconds=2.0,
            ),
            _activity(
                process_id=3,
                step="클립 추출",
                input_path=str(original),
                started_at=now - 65,
                progress_seconds=30.0,
                whole_input=False,
            ),
        ),
    )
    body = _client(ui).get("/vl-activity").json()
    first, second, third = body["processes"]
    assert first["name"] == SOURCE_NAME
    assert first["percent"] == 62
    assert first["text"] == f"분석 프록시 만들기 · {SOURCE_NAME} · 25:10 경과 / 한도 54:31 · 62%"
    assert second["name"] == "clip.mov"  # 모르는 경로는 파일 이름
    assert second["percent"] is None  # 길이를 모르면 %가 없다
    assert second["text"] == "썸네일 추출 · clip.mov · 0:05 경과 / 한도 없음"
    assert third["percent"] is None  # 범위 디코드는 원본 길이로 재지 않는다
    assert third["text"] == f"클립 추출 · {SOURCE_NAME} · 1:05 경과 / 한도 없음"
    # 띠의 머리줄은 첫 프로세스 줄이다 — 도는 것이 있으면 바쁘다(D101).
    assert body["busy"] is True
    assert body["headline"] == first["text"]


def test_running_and_queued_jobs_show_progress_and_freshness(
    ui: workspace_service.RawWorkspaceUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_service.preprocess, "active_processes", lambda: ())
    root = Path(ui.workspace_root())
    started, release = threading.Event(), threading.Event()

    def body(context: JobContext) -> None:
        context.set_total(18)
        context.set_progress(12)
        started.set()
        release.wait(20)

    handle = ui.raw.submit_job(root, raw_service.ANALYSIS_JOB_KIND, "시험", body)
    try:
        assert started.wait(10)
        payload = _client(ui).get("/vl-activity").json()
    finally:
        release.set()
    deadline = time.monotonic() + 10
    while ui.raw.get_job(root, handle.job_id).status == "running" and time.monotonic() < deadline:
        time.sleep(0.02)
    (job,) = payload["jobs"]
    assert job["label"] == "영상 분석 1단계(전사·장면)"
    assert (job["status"], job["progress_current"], job["progress_total"]) == ("running", 12, 18)
    assert job["text"] == "영상 분석 1단계(전사·장면) · 진행 중 · 12/18 · 방금 갱신"
    assert job["percent"] == 66  # 진행 막대 — 내림
    assert payload["processes"] == []
    assert (payload["busy"], payload["headline"]) == (
        True,
        job["text"],
    )  # 프로세스가 없으면 작업 줄
    finished = _client(ui).get("/vl-activity").json()
    assert finished["jobs"] == []  # 끝난 작업은 「지금 도는 작업」이 아니다
    assert (finished["busy"], finished["headline"]) == (False, "")


def test_log_lines_arrive_after_the_given_sequence(
    ui: workspace_service.RawWorkspaceUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workspace_service.preprocess, "active_processes", lambda: ())
    ui.activity_log.record("stderr", "› 분석 프록시 준비: a.mov (12.0초)")
    ui.activity_log.record("stderr", "WARN 분석 프록시 — 원본으로 물러납니다")
    ui.activity_log.record("stderr", "ERR <img src=x onerror=alert(1)>")
    client = _client(ui)
    first = client.get("/vl-activity", params={"after": 0}).json()["log"]
    assert [line["level"] for line in first["lines"]] == ["info", "warn", "error"]
    # 글 그대로 보낸다 — 화면이 textContent로 넣는다.
    assert first["lines"][2]["text"] == "ERR <img src=x onerror=alert(1)>"
    assert first["latest"] == 3
    assert all(len(line["time"]) == 8 for line in first["lines"])  # HH:MM:SS
    later = client.get("/vl-activity", params={"after": 3}).json()["log"]
    assert later["lines"] == [] and later["latest"] == 3
    # 같은 서버 프로세스는 같은 boot — 다시 띄운 서버(새 버퍼)는 다른 boot라 화면이 처음부터 받는다.
    assert later["boot"] == first["boot"] == ui.activity_log.boot_id
    ui.activity_log = ActivityLog()
    ui.activity_log.record("stderr", "› 다시 띄운 서버의 첫 줄")
    restarted = client.get("/vl-activity", params={"after": 3}).json()["log"]
    assert restarted["boot"] != first["boot"]
    assert restarted["lines"] == []  # 번호만으로는 다시 뜬 것을 모른다 — 그래서 boot를 본다


def test_no_workspace_still_lists_processes_and_log(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    server = workspace_service.RawWorkspaceUi(port=PORT, state_path=tmp_path / "state.json")
    server.activity_log = ActivityLog()
    monkeypatch.setattr(
        workspace_service.preprocess,
        "active_processes",
        lambda: (_activity(input_path="/x/proxy.mp4", tool="ffprobe", step="ffprobe"),),
    )
    body = _client(server).get("/vl-activity").json()
    assert [row["name"] for row in body["processes"]] == ["proxy.mp4"]
    assert body["jobs"] == []


# ── 경로 — 읽기만 · 가드 ────────────────────────────────────────────────────


def test_the_route_reads_only(
    ui: workspace_service.RawWorkspaceUi, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = _add_source(ui, tmp_path)
    root = Path(ui.workspace_root())
    done = ui.raw.submit_job(root, "source_preview", "끝", lambda context: None)
    deadline = time.monotonic() + 10
    while ui.raw.get_job(root, done.job_id).status != "succeeded" and time.monotonic() < deadline:
        time.sleep(0.02)
    monkeypatch.setattr(
        workspace_service.preprocess,
        "active_processes",
        lambda: (_activity(input_path=str(original), progress_seconds=10.0),),
    )
    submitted: list[object] = []
    monkeypatch.setattr(ui.raw, "submit_job", lambda *a, **k: submitted.append(a))
    before = _workspace_bytes(root)
    files_before = sorted(path.relative_to(root) for path in root.rglob("*"))
    client = _client(ui)
    for after in (0, 1, 99):
        assert client.get("/vl-activity", params={"after": after}).status_code == 200
    assert _workspace_bytes(root) == before
    assert sorted(path.relative_to(root) for path in root.rglob("*")) == files_before
    assert submitted == []


def test_the_route_is_behind_the_same_host_guard(ui: workspace_service.RawWorkspaceUi) -> None:
    client = _client(ui)
    assert client.get("/vl-activity", headers={"host": "evil.test"}).status_code == 403
    assert client.post("/vl-activity").status_code in (403, 405)  # 쓰는 길이 없다
    response = client.get("/vl-activity")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"


# ── 화면 — 판의 두 칸 · 줄 글 ───────────────────────────────────────────────


def test_the_activity_panel_gains_two_server_sections_above_the_client_log() -> None:
    markup = ui_facade.activity_html()
    procs = markup.index('id="vl-server-procs"')
    server_log = markup.index('id="vl-server-log"')
    client_log = markup.index('class="vl-activity-empty"')
    assert markup.index("지금 도는 작업") < procs < markup.index("서버 로그") < server_log
    assert server_log < client_log  # 지금 화면 기록은 그 아래 그대로
    assert markup.index('id="vl-activity-usage"') < procs  # 실사용 줄은 맨 위 고정 그대로
    assert '<p id="vl-activity-usage" class="vl-activity-usage" hidden></p>' in markup
    assert 'aria-live="off"' in markup  # 2초마다 바뀌는 칸이 화면 낭독기를 시끄럽게 하지 않는다


def test_the_progress_panel_sits_in_the_strip_before_the_full_log() -> None:
    """도는 일은 「기록」을 누르지 않아도 보인다 — 띠 안의 배지와 명령 줄 아래 펼침 판(D101)."""
    markup = ui_facade.activity_html()
    count = markup.index('id="vl-activity-count"')
    panel = markup.index('id="vl-progress"')
    assert count < markup.index('id="vl-activity-log-open"') < panel
    assert panel < markup.index('id="vl-activity-log"')  # 판은 「기록」 판과 따로다
    for node in ("vl-progress-rows", "vl-progress-log", "vl-progress-pin", "vl-progress-close"):
        assert markup.index(f'id="{node}"') > panel
    head = markup[panel : markup.index(">", panel)]
    assert 'role="region"' in head and 'aria-label="작업 진행"' in head and "hidden" in head
    assert 'id="vl-progress-pin" class="vl-btn vl-btn-ghost vl-btn-sm" aria-pressed="false"' in (
        markup
    )
    css = ui_facade.WORKBENCH_CSS
    rule = css[css.index(".vl-progress {") :]
    rule = rule[: rule.index("}")]
    for want in ("position: absolute;", "top: calc(100% + 6px);", "right: 0;", "z-index: 60;"):
        assert want in rule, want  # 명령 줄 아래에 겹친다 — 무대를 밀지 않는다
    assert ".vl-progress[hidden]" in css and ".vl-activity-count[hidden]" in css
    bar = css[css.index(".vl-progress-bar {") :]
    assert "background: var(--vl-accent-ink);" in bar[: bar.index("}")]  # 본점 채움은 셋뿐


def test_rows_read_like_the_examples() -> None:
    assert ui_facade.format_clock(1510.4) == "25:10"
    assert ui_facade.format_clock(3725) == "1:02:05"
    assert ui_facade.format_clock(0) == "0:00"
    assert (
        ui_facade.process_row_text("분석 프록시 만들기", "a.mov", 1510.4, 3271.0, 62)
        == "분석 프록시 만들기 · a.mov · 25:10 경과 / 한도 54:31 · 62%"
    )
    assert ui_facade.process_row_text("썸네일 추출", "b.mov", 5, None, None) == (
        "썸네일 추출 · b.mov · 0:05 경과 / 한도 없음"
    )
    assert ui_facade.progress_percent(338.2, 545.0) == 62
    assert ui_facade.progress_percent(600.0, 545.0) == 100
    assert ui_facade.progress_percent(None, 545.0) is None
    assert ui_facade.progress_percent(10.0, None) is None
    assert ui_facade.progress_percent(10.0, 0.0) is None
    assert ui_facade.job_row_text("의미 분석", "진행 중", 3, 7, 4) == (
        "의미 분석 · 진행 중 · 3/7 · 4분 전 갱신"
    )
    assert ui_facade.job_row_text("자동 정리", "대기", 0, 0, 0) == "자동 정리 · 대기 · 방금 갱신"
    assert ui_facade.job_row_text("자동 정리", "대기", 0, 0, None) == "자동 정리 · 대기"
    assert ui_facade.job_percent(12, 18) == 66
    assert ui_facade.job_percent(3, 0) is None  # 전체를 모르면 막대가 없다
    assert ui_facade.job_percent(20, 18) == 100


def test_view_settings_feed_the_shell_and_the_server_page() -> None:
    """폴링 주기·판 줄 수는 설정 한 곳(D101) — 실행본에 숫자 리터럴로 따로 박지 않는다."""
    view = ui_facade.ACTIVITY_VIEW
    script = ui_facade.SERVER_ACTIVITY_JS
    assert "__VL_ACTIVITY_VIEW__" not in script
    assert json.dumps(dataclasses.asdict(view)) in script
    for literal in ("2000", "15000", "500"):
        assert literal not in script.replace(json.dumps(dataclasses.asdict(view)), "")
    assert view.server_log_lines == workspace_service.ACTIVITY_LOG_PAGE
    assert view.poll_busy_ms < view.poll_idle_ms  # 바쁠 때가 더 자주 묻는다


def test_server_log_colours_come_from_the_design_tokens() -> None:
    css = ui_facade.WORKBENCH_CSS
    box = css[css.index(".vl-server-log {") :]
    assert "font-family: var(--vl-font-mono);" in box[: box.index("}")]
    warn = css[css.index('.vl-server-line[data-vl-level="warn"]') :]
    assert "color: var(--vl-warn);" in warn[: warn.index("}")]
    error = css[css.index('.vl-server-line[data-vl-level="error"]') :]
    assert "color: var(--vl-danger);" in error[: error.index("}")]


def test_the_shell_never_writes_server_text_as_html() -> None:
    script = ui_facade.SHELL_JS
    assert "innerHTML" not in script and "insertAdjacentHTML" not in script
    assert ui_facade.SERVER_ACTIVITY_JS.strip() in script
    assert f'"{workspace_service.ACTIVITY_ROUTE}"' in script
    assert ui_facade.ACTIVITY_ROUTE == workspace_service.ACTIVITY_ROUTE
    # 이어 거는 setTimeout 하나 — 주기는 바쁨에 따라 바뀐다(D101).
    assert "setInterval" not in ui_facade.SERVER_ACTIVITY_JS


_POLL_HARNESS = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[2], 'utf8');
const responses = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
function el(id, tag) {
  const node = { id, tagName: tag || 'DIV', hidden: false, title: '', attrs: {}, children: [],
    classes: new Set(), scrollTop: 0, scrollHeight: 0, clientHeight: 100, htmlWrites: 0,
    style: {}, className: '',
    classList: { contains(c) { return node.classes.has(c); } },
    setAttribute(k, v) { this.attrs[k] = String(v); },
    getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; },
    appendChild(c) { this.children.push(c); this.scrollHeight += 15; return c; },
    removeChild(c) { this.children.splice(this.children.indexOf(c), 1); return c; },
    get firstChild() { return this.children[0] || null; },
    get childElementCount() { return this.children.length; },
    querySelector() { return null; }, querySelectorAll() { return []; },
    closest() { return null; }, focus() {} };
  let text = '';
  Object.defineProperty(node, 'textContent', {
    get() { return text || node.children.map((c) => c.textContent).join(' '); },
    set(v) { text = String(v); node.children = []; } });
  Object.defineProperty(node, 'innerHTML', { set() { node.htmlWrites += 1; } });
  return node;
}
const reg = {};
['vl-activity', 'vl-activity-msg', 'vl-activity-key', 'vl-activity-log', 'quick-status',
 'project-tools', 'settings-drawer', 'vl-keysheet', 'vl-usage-src', 'vl-activity-usage',
 'vl-server-procs', 'vl-server-log', 'vl-server-note', 'vl-activity-count', 'vl-progress',
 'vl-progress-rows', 'vl-progress-log', 'vl-progress-pin', 'vl-progress-close'
].forEach((id) => { reg[id] = el(id); });
['vl-activity-log', 'vl-keysheet', 'vl-progress', 'vl-activity-count'].forEach((id) => {
  reg[id].hidden = true; });
reg['vl-server-log'].clientHeight = 20;  // 줄 하나 15px — 둘째 줄부터 넘친다
const fetches = [];
const timers = [];
let failNext = false;
const window = {
  setInterval() {},
  setTimeout(fn, ms) { timers.push({ fn, ms, live: true }); return timers.length; },
  clearTimeout(id) { if (timers[id - 1]) { timers[id - 1].live = false; } },
  fetch(url) {
    fetches.push(url);
    if (failNext) { failNext = false; return Promise.reject(new Error('연결 끊김')); }
    const body = responses[Math.min(fetches.length - 1, responses.length - 1)];
    return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
  },
};
const listeners = {};
const document = {
  body: { appendChild(child) { reg[child.id] = child; } },
  getElementById: (id) => reg[id] || null,
  querySelector: () => null,
  querySelectorAll: () => [],
  createElement: (tag) => el('', tag),
  addEventListener: (type, fn) => { (listeners[type] = listeners[type] || []).push(fn); },
};
class MutationObserver { constructor(fn) { this.fn = fn; } observe() {} disconnect() {} }
global.requestAnimationFrame = (fn) => fn();
function target(selector) {
  const node = { id: selector.slice(1), closest: (sel) => (sel === selector ? node : null) };
  return node;
}
const click = (selector) =>
  (listeners.click || []).forEach((fn) => fn({ target: target(selector) }));
const settle = async () => { for (let i = 0; i < 4; i += 1) {
  await new Promise((resolve) => setImmediate(resolve)); } };
const S = () => window.__vlServer;
const live = () => timers.filter((t) => t.live);
const pollWait = () => { const t = live().find((x) => x.fn === S().poll); return t ? t.ms : null; };
async function tick() {
  const t = live().find((x) => x.fn === S().poll);
  t.live = false; t.fn(); await settle();
}
const lines = () => reg['vl-server-log'].children.map((row) => ({
  level: row.attrs['data-vl-level'], text: row.children.map((c) => c.textContent) }));
const rows = () => reg['vl-server-procs'].children.map((row) => ({
  kind: row.attrs['data-vl-kind'] || null, text: row.textContent }));
const panel = () => ({
  hidden: reg['vl-progress'].hidden,
  badge: reg['vl-activity-count'].hidden ? null : reg['vl-activity-count'].textContent,
  strip: reg['vl-activity-msg'].textContent,
  bars: reg['vl-progress-rows'].children.map((row) => row.children.length > 1
    ? row.children[1].children[0].style.width : null),
  tail: reg['vl-progress-log'].children.map((row) => row.children[1].textContent),
  wait: pollWait() });
(async () => {
  const run = new Function('window', 'document', 'MutationObserver', 'element', source);
  run(window, document, MutationObserver, null);
  const out = { load: { fetches: fetches.length, wait: pollWait() } };
  await tick();  // 열자마자 한 번 — 한가하다
  out.idle = { ...panel(), fetches: fetches.slice() };
  click('#vl-activity-log-open');
  out.openWait = pollWait();  // 판을 열면 곧바로 묻는다
  await tick();
  out.opened = { url: fetches[fetches.length - 1], rows: rows(), lines: lines(),
    wait: pollWait(), note: reg['vl-server-note'].textContent, panel: panel() };
  const box = reg['vl-server-log'];
  box.scrollTop = box.scrollHeight - box.clientHeight;
  await tick();
  out.second = { url: fetches[fetches.length - 1], lines: lines().length,
    stuck: box.scrollTop === box.scrollHeight };
  box.scrollTop = 0;
  await tick();
  out.third = { scrollTop: box.scrollTop, lines: lines().length };
  failNext = true;
  await tick();
  out.failed = { note: reg['vl-server-note'].textContent, wait: pollWait() };
  await tick();
  out.rebooted = { note: reg['vl-server-note'].textContent, lines: lines().length,
    wait: pollWait() };
  await tick();
  out.refetched = { url: fetches[fetches.length - 1], lines: lines(), wait: pollWait() };
  click('#vl-activity-log-open');  // 「기록」을 닫는다 — 바쁘면 「작업 진행」 판이 그 자리를 잇는다
  out.closed = { logHidden: reg['vl-activity-log'].hidden, ...panel() };
  await tick();
  out.busy = panel();
  click('#vl-progress-close');
  await tick();
  out.dismissed = panel();
  await tick(); await tick();  // 빈 응답 둘 — 한가해진다
  out.quiet = panel();
  window.__vlActivity.reply('**폴더**를 불러옵니다');
  out.woke = pollWait();
  await tick();
  out.newRound = panel();
  click('#vl-progress-pin');
  await tick(); await tick();
  out.pinned = { ...panel(), pressed: reg['vl-progress-pin'].attrs['aria-pressed'] };
  click('#vl-progress-pin');
  out.unpinned = panel();
  S().wake(true);
  await tick(); await tick(); await tick();
  out.lingering = { ...panel(), collapse: live().filter((t) => t.fn !== S().poll)
    .map((t) => t.ms) };
  live().filter((t) => t.fn !== S().poll).forEach((t) => { t.live = false; t.fn(); });
  out.collapsed = panel();
  out.htmlWrites = Object.values(reg).reduce((n, node) => n + node.htmlWrites, 0);
  process.stdout.write(JSON.stringify(out));
})();
"""

PROCESS_TEXT = "분석 프록시 만들기 · a.mov · 25:10 경과 / 한도 54:31 · 62%"
JOB_TEXT = "영상 분석 1단계(전사·장면) · 진행 중 · 12/18 · 3분 전 갱신"


def _payload(
    latest: int, lines: list[tuple[int, str, str]], boot: str = "A", *, busy: bool = True
) -> dict:
    return {
        "busy": busy,
        "headline": PROCESS_TEXT if busy else "",
        "processes": [{"text": PROCESS_TEXT, "percent": 62}] if busy else [],
        "jobs": [{"text": JOB_TEXT, "percent": 66}] if busy else [],
        "log": {
            "boot": boot,
            "latest": latest,
            "lines": [
                {"seq": seq, "time": "12:00:00", "level": level, "text": text}
                for seq, level, text in lines
            ],
        },
    }


def _run_harness(tmp_path: Path, responses: list[dict]) -> dict:
    harness = tmp_path / "poll.js"
    harness.write_text(_POLL_HARNESS, encoding="utf-8")
    script = tmp_path / "shell.js"
    script.write_text(ui_facade.SHELL_JS, encoding="utf-8")
    data = tmp_path / "responses.json"
    data.write_text(json.dumps(responses, ensure_ascii=False), encoding="utf-8")
    completed = subprocess.run(
        ["node", str(harness), str(script), str(data)], capture_output=True, text=True, timeout=20
    )
    assert completed.returncode == 0, completed.stderr[-2000:]
    return json.loads(completed.stdout)


@needs_node
def test_one_poller_shows_running_work_without_opening_the_log(tmp_path: Path) -> None:
    """한 고리가 바쁠 때 짧게·한가할 때 길게 묻고, 도는 일은 띠와 「작업 진행」 판에 저절로 보인다.

    「기록」 판은 예전처럼 다 보인다(D61). 판을 닫아도 바쁘면 「작업 진행」 판이 잇고(D101),
    닫기를 누른 회차에는 다시 펼치지 않으며, 고정하면 한가해도 남고, 아니면 지연 뒤 접힌다.
    서버 글은 언제나 textContent다.
    """
    view = ui_facade.ACTIVITY_VIEW
    route = workspace_service.ACTIVITY_ROUTE
    burst = [(seq, "info", f"› 원본 확인 {seq}/9") for seq in range(3, 10)]
    responses = [
        _payload(1, [(1, "info", "› 지난 줄")], busy=False),
        _payload(3, [(2, "info", "› 준비"), (3, "warn", "<b>WARN</b> 잔재")]),
        _payload(4, [(4, "error", "ERR 실패")]),
        _payload(5, [(5, "info", "› 하나 더")]),
        _payload(5, []),  # 이 요청은 끊긴다 — 쓰이지 않는다
        # 다시 뜬 서버: 옛 번호 뒤의 줄만 온 응답 — 화면은 버리고 처음부터 다시 묻는다.
        _payload(9, [(9, "info", "› 다시 뜬 서버의 아홉째 줄")], boot="B"),
        _payload(2, [(1, "info", "› 다시 시작"), (2, "info", "› 둘째")], boot="B"),
        _payload(9, burst, boot="B"),
        _payload(9, [], boot="B"),  # 닫은 회차 — 여전히 바쁘다
        _payload(9, [], boot="B", busy=False),
        _payload(9, [], boot="B", busy=False),
        _payload(9, [], boot="B"),  # 단추 응답이 연 새 회차
        _payload(9, [], boot="B", busy=False),
        _payload(9, [], boot="B", busy=False),
        _payload(9, [], boot="B"),
        _payload(9, [], boot="B", busy=False),
        _payload(9, [], boot="B", busy=False),
    ]
    out = _run_harness(tmp_path, responses)
    assert out["load"] == {"fetches": 0, "wait": 0}  # 열자마자 묻지만 동기로 부르지 않는다
    idle = out["idle"]
    assert idle["fetches"] == [f"{route}?after=0"]
    assert (idle["hidden"], idle["badge"], idle["wait"]) == (True, None, view.poll_idle_ms)
    assert idle["tail"] == ["› 지난 줄"]  # 지난 기록은 판에 남지만 판을 펼치지 않는다
    assert out["openWait"] == 0
    opened = out["opened"]
    assert opened["url"] == f"{route}?after=1"
    assert opened["rows"] == [
        {"kind": "process", "text": PROCESS_TEXT},
        {"kind": "job", "text": JOB_TEXT},
    ]
    assert opened["lines"] == [
        {"level": "info", "text": ["12:00:00", "› 지난 줄"]},
        {"level": "info", "text": ["12:00:00", "› 준비"]},
        {"level": "warn", "text": ["12:00:00", "<b>WARN</b> 잔재"]},  # 글자 그대로
    ]
    assert opened["wait"] == view.poll_busy_ms
    assert (
        opened["panel"]["hidden"] is True
    )  # 「기록」 판이 열린 동안은 같은 내용을 겹쳐 펴지 않는다
    assert opened["panel"]["badge"] == "2개 작업" and opened["panel"]["strip"] == PROCESS_TEXT
    assert out["second"] == {"url": f"{route}?after=3", "lines": 4, "stuck": True}
    assert out["third"] == {"scrollTop": 0, "lines": 5}
    assert "연결 끊김" in out["failed"]["note"] and out["failed"]["wait"] == view.poll_busy_ms
    assert out["rebooted"] == {"note": "", "lines": 0, "wait": 0}  # 비우고 곧바로 다시 묻는다
    assert out["refetched"] == {
        "url": f"{route}?after=0",
        "lines": [
            {"level": "info", "text": ["12:00:00", "› 다시 시작"]},
            {"level": "info", "text": ["12:00:00", "› 둘째"]},
        ],
        "wait": view.poll_busy_ms,
    }
    closed = out["closed"]
    assert closed["logHidden"] is True and closed["hidden"] is False
    busy = out["busy"]
    assert busy["bars"] == ["62%", "66%"]  # 막대 폭은 숫자로만
    assert busy["tail"] == [f"› 원본 확인 {seq}/9" for seq in range(4, 10)]  # 최근 6줄
    assert len(busy["tail"]) == view.panel_log_lines
    assert out["dismissed"]["hidden"] is True  # 닫은 회차에는 다시 펴지 않는다
    quiet = out["quiet"]
    assert (quiet["hidden"], quiet["badge"], quiet["wait"]) == (True, None, view.poll_idle_ms)
    assert quiet["strip"] == ""  # 머리줄이 걷혔다 — 되살릴 화면 기록이 없다
    assert out["woke"] == 0  # 단추 응답은 곧바로 묻는다
    new_round = out["newRound"]
    assert new_round["hidden"] is False and new_round["wait"] == view.poll_busy_ms
    assert new_round["strip"] == "폴더를 불러옵니다"  # 방금 온 단추 응답은 머리줄이 덮지 않는다
    pinned = out["pinned"]
    assert pinned["hidden"] is False and pinned["pressed"] == "true"
    assert pinned["wait"] == view.poll_busy_ms  # 고정한 판은 한가해도 짧게 묻는다
    assert out["unpinned"]["hidden"] is True
    lingering = out["lingering"]
    assert lingering["hidden"] is False and lingering["collapse"] == [view.panel_collapse_delay_ms]
    assert out["collapsed"]["hidden"] is True
    assert out["htmlWrites"] == 0


def test_an_old_queued_job_is_found_by_status_not_by_a_recent_window(
    ui: workspace_service.RawWorkspaceUi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """대기 줄은 오래 기다린 작업일수록 앞에 있다 — 최근 N개 창으로 찾으면 그 작업을 놓친다."""
    monkeypatch.setattr(workspace_service.preprocess, "active_processes", lambda: ())
    root = Path(ui.workspace_root())
    store = ui.raw._jobs(root)._store
    old, _ = store.insert_deduplicated("caption_run", "오래 기다림", 0)
    store._update(old.job_id, {"created_at": "2020-01-01T00:00:00+00:00"})  # 확실히 가장 오래됐다
    for number in range(210):
        record, _ = store.insert_deduplicated("source_preview", f"끝남-{number}", 0)
        store.update(record.job_id, status="succeeded")
    before = _workspace_bytes(root)
    jobs = _client(ui).get("/vl-activity").json()["jobs"]
    assert [(job["kind"], job["status"]) for job in jobs] == [("caption_run", "queued")]
    assert jobs[0]["text"] == "최종 자막 만들기 · 대기 · 방금 갱신"
    assert _workspace_bytes(root) == before  # 상태로 찾는 질의도 읽기만 한다
