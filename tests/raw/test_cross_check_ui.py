"""D59 19e — 교차 확인 화면: 「다른 귀」 줄과 「가져오기」 · 불일치 표지 · 「확인 필요」 배지 ·
용어집 칸.

유료 호출은 없다. 포트는 7993(시험용)만 쓴다 — 실제 앱 상태는 건드리지 않는다. 시험이 못 박는 것:

- 작업대 모델의 원본 자막 줄이 「다른 귀」 증거(`ear`)와 「확인 필요」 이유(`suspects`)를 싣고,
  줄은 하나도 빠지거나 바뀌지 않는다(규칙 4·9).
- 「가져오기」는 문구 칸만 채우고 서버를 부르지 않는다. 사람이 「저장」을 누르면 기존 편집 길을
  지나 자막이 「고친 자막」이 된다.
- 타임라인 칩과 목록의 표지·말풍선, 편집기의 배지가 그려진다.
- 용어집 칸은 설정 서랍에 있고, 저장은 쓸 수 없는 글자를 정중히 거절하며, 저장하면 보이는 견적
  문구가 새 용어집으로 다시 묶인다(19d 결속).
- 화면 문구에 「VAD」·금액·모델 이름이 없다(D57).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
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

from tests.raw.test_cross_check import (  # noqa: E402
    EarsSender,
    _cache_live_answer,
    _library,
    _settled,
    _turn,
)
from tests.raw.test_relisten import approve, stage  # noqa: E402
from tests.raw.test_selection_analysis import wait_until  # noqa: E402
from tests.raw.test_visual_workbench_ui import (  # noqa: E402
    _NODE_HARNESS,
    _model_of,
    _prepare_review,
)
from ui import ui_facade, workspace_service  # noqa: E402
from ui.workspace_service import (  # noqa: E402
    GLOSSARY_BUSY_TEXT,
    GLOSSARY_ID,
    GLOSSARY_INFO,
    GLOSSARY_LABEL,
    GLOSSARY_SAVE_ID,
    GLOSSARY_STATUS_ID,
    RawWorkspaceUi,
)
from vlogsrt.project.raw import (  # noqa: E402
    glossary,
    source_transcript,
    subtitle_suspects,
    sync_report,
    text_ear,  # noqa: E402
)
from vlogsrt.project.raw import service as raw  # noqa: E402
from vlogsrt.project.raw.config import SourceTextSettings  # noqa: E402

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")


@pytest.fixture(autouse=True)
def _gpt_ear_for_new_hearings(monkeypatch: pytest.MonkeyPatch) -> None:
    """이 파일은 gpt 다른 귀(D59 19d)의 화면을 시험한다 — 용어집은 그 귀에만 간다. W-35b부터 처음
    듣는 whisper 영상의 글자 귀는 Gemini다(D95, 용어집 없음). 여기서는 처음 듣는 영상도 gpt 귀로
    듣게 해 그 화면을 그대로 잰다."""
    monkeypatch.setattr(text_ear, "DEFAULT_NEW_HEARING_EAR", text_ear.EAR_GPT)


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node가 없는 환경")

TRANSCRIBE = raw.SELECTION_MODE_TRANSCRIBE
POLITE_END = re.compile(r"(니다|세요)[.)]*$")
ALT = "치즈 하나 둘 셋"
MODEL = SourceTextSettings().model


def _rows() -> list[dict[str, Any]]:
    """실데이터 모양의 원본 자막 — 메아리 셋 · 다른 귀 후보 · 고친 줄 · 다른 문자 · 끝 인사말 ·
    말소리 측정상 없음."""

    def row(index: int, text: str, **evidence: str) -> dict[str, Any]:
        start = Fraction(index, 2)
        return {
            "utterance_id": f"u{index}",
            "text": text,
            "start": source_transcript.encoded(start),
            "end": source_transcript.encoded(start + Fraction(2, 5)),
            **evidence,
        }

    return [
        row(
            0, "치즈", whisper_text="치즈", text_source="whisper", alt_text=ALT, text_origin="live"
        ),
        row(1, "치즈"),
        row(2, "치즈"),
        row(3, "왕방 왔어요", whisper_text="왕반 왔어요", text_source=MODEL, text_origin="live"),
        row(4, " 물놀이 усhod"),
        row(5, " 영상 봐주셔서 감사합니다"),
        row(6, "여기 봐"),
    ]


@pytest.fixture
def evidence_bench(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    """검토가 선 작업대 + 증거가 실린 원본 자막 일곱 줄 + 지금 리비전의 확인 보고서(값)."""
    server = workspace_service.RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    assert server.create_workspace_view("교차 확인 화면", str(tmp_path / "ws"))[1]
    root = server.workspace_root()
    assert root is not None
    _prepare_review(server, root, tmp_path)
    asset = server.raw.list_source_assets(root)[0]
    source_transcript.seed(
        server.raw._workspace(root), asset.asset_id, _rows(), Fraction(4), asset.content_sha256
    )
    current = {
        "level": "amber",
        "label": "자막 시각 확인",
        "tone": "amber",
        "stale": False,
        "running": False,
        "reasons": [],
        "checks": [],
        "cues": [
            {"id": "u6", "start": "3", "end": "17/5", "text": "여기 봐", "flags": ["no_speech"]}
        ],
    }
    monkeypatch.setattr(server.raw, "sync_check_view", lambda _root, _sid: current)
    return server, root, asset.asset_id


@needs_ffmpeg
def test_the_model_carries_ears_and_reasons_and_keeps_every_cue(
    evidence_bench: tuple[Any, Path, str],
) -> None:
    server, root, source_id = evidence_bench
    _, html = server.visual_workbench_render(root, source_id=source_id)
    cues = _model_of(html)["sources"][source_id]["subtitles"]
    assert [cue["text"] for cue in cues] == [row["text"] for row in _rows()]  # 하나도 빠지지 않는다
    by_id = {cue["id"]: cue for cue in cues}
    assert by_id["u0"]["ear"] == {"mark": "alt", "other": ALT, "first": ""}
    assert by_id["u3"]["ear"] == {
        "mark": "replaced",
        "other": "왕방 왔어요",
        "first": "왕반 왔어요",
        "firstEngine": "whisper-1",  # 「처음 들은 글」을 들은 엔진(W-52b1)
    }
    repeat = {"code": subtitle_suspects.REPEAT, "text": "같은 말이 3번 이어집니다"}
    assert [by_id[i]["suspects"] for i in ("u0", "u1", "u2")] == [[repeat]] * 3
    assert by_id["u4"]["suspects"][0]["code"] == subtitle_suspects.OTHER_SCRIPT
    assert by_id["u5"]["suspects"][0]["code"] == subtitle_suspects.BOILERPLATE
    assert by_id["u6"]["suspects"] == [
        {"code": subtitle_suspects.NO_SPEECH, "text": "말소리를 못 찾음 (측정 한계일 수 있음)"}
    ]
    assert "ear" not in by_id["u1"] and "suspects" not in by_id["u3"]
    stored = source_transcript.load(server.raw._workspace(root), source_id)
    assert stored is not None and stored["edited"] is False and stored["sequence"] == 0


@needs_ffmpeg
def test_saving_a_taken_line_goes_through_the_edit_path_and_marks_it_edited(
    evidence_bench: tuple[Any, Path, str],
) -> None:
    """「가져오기」 뒤 사람이 「저장」을 누른 모양 — 기존 편집 길(`vl_source_caption`)이 고친
    자막을 만든다."""
    server, root, source_id = evidence_bench
    _, html = server.visual_workbench_render(root, source_id=source_id)
    model = _model_of(html)
    source = model["sources"][source_id]
    save = {fn.__name__: fn for fn in server._visual_server_functions()}["vl_source_caption"]
    reply = save(
        {
            "epoch": model["epoch"],
            "signature": model["signature"],
            "token": model["token"],
            "sourceId": source_id,
            "transcriptRevision": source["transcriptRevision"],
            "action": "edit",
            "utteranceId": "u0",
            "text": source["subtitles"][0]["ear"]["other"],
            "start": "",
            "end": "",
        }
    )
    assert reply["ok"] is True, reply
    stored = source_transcript.load(server.raw._workspace(root), source_id)
    assert stored is not None and stored["edited"] is True and stored["sequence"] == 1
    first = stored["utterances"][0]
    assert (first["text"], first["text_source"], first["whisper_text"]) == (ALT, "manual", "치즈")
    after = _model_of(reply["html"])["sources"][source_id]["subtitles"]
    assert len(after) == len(_rows()) and "ear" not in after[0]  # 이제 두 귀가 같은 글이다


# ── 클라이언트 — node에서 실제 실행본으로 그리고 누른다 ──────────────────────────────────

_EAR_SCRIPT = r"""
const earModel = visualModel();
earModel.epoch = 'ear-1'; earModel.token = 'ear-token';
const repeat = { code: 'repeat', text: '같은 말이 3번 이어집니다' };
earModel.sources.srcA.transcriptRevision = 'rev-a';
earModel.sources.srcA.subtitles = [
  { id: 'u0', start: '0', end: '2/5', text: '치즈',
    ear: { mark: 'alt', other: '치즈 <하나>', first: '' }, suspects: [repeat] },
  { id: 'u1', start: '1/2', end: '9/10', text: '치즈', suspects: [repeat] },
  { id: 'u2', start: '1', end: '7/5', text: '치즈', suspects: [repeat] },
  { id: 'u3', start: '3/2', end: '19/10', text: '왕방 왔어요',
    ear: { mark: 'replaced', other: '왕방 왔어요', first: '왕반 왔어요',
      firstEngine: 'whisper-1' } },
  { id: 'u4', start: '2', end: '12/5', text: '여기 봐',
    suspects: [{ code: 'no_speech', text: '말소리를 못 찾음 (측정 한계일 수 있음)' }] },
];
const saved = [];
const envEar = visualEnv({ vl_source_caption: (payload) => { saved.push(payload);
  return { ok: true, echoEpoch: 'ear-1', message: 'x' }; } }, earModel);
const rail = envEar.rail.innerHTML;
const pick = (id) => envEar.click(el({ attrs: { 'data-vl-act': 'source-pick',
  'data-vl-cue-id': id } }));
pick('u0');
const altPanel = envEar.panel.innerHTML;
const box = el({ id: 'vl-source-text', value: '치즈' });
envEar.registry['vl-source-text'] = box;
envEar.registry['vl-source-start'] = el({ id: 'vl-source-start', value: '0.000' });
envEar.registry['vl-source-end'] = el({ id: 'vl-source-end', value: '0.400' });
envEar.registry['vl-source-right'] = el({ id: 'vl-source-right', value: '' });
envEar.click(el({ attrs: { 'data-vl-act': 'ear-take', 'data-vl-take': 'other' } }));
const afterTake = { value: box.value, calls: envEar.serverCalls.length,
  foot: envEar.registry['vl-footnote'].textContent };
envEar.rewired();  // 같은 리비전으로 다시 그려도 가져온 글자가 문구 칸에 남는다
const redrawn = envEar.panel.innerHTML;
envEar.click(el({ attrs: { 'data-vl-act': 'source-edit' } }));
await new Promise((resolve) => setTimeout(resolve, 0));
const footAfterSave = envEar.registry['vl-footnote'].textContent;
pick('u3');
const footKept = envEar.registry['vl-footnote'].textContent;  // 가져오기 각주가 아니면 그대로
const replacedPanel = envEar.panel.innerHTML;
envEar.click(el({ attrs: { 'data-vl-act': 'ear-take', 'data-vl-take': 'first' } }));
const firstTaken = box.value;
const footTaken = envEar.registry['vl-footnote'].textContent;
pick('u0');  // 다른 줄을 골랐다 돌아오면 가져온 글자는 버려져 있다
const footDropped = envEar.registry['vl-footnote'].textContent;
const backPanel = envEar.panel.innerHTML;
pick('u4');
const silentPanel = envEar.panel.innerHTML;
checkpoints.push({ case: 'ears', rail, altPanel, afterTake, redrawn, replacedPanel, firstTaken,
  backPanel, silentPanel, footAfterSave, footKept, footTaken, footDropped,
  saved: saved.map((p) => ({ text: p.text, action: p.action, utteranceId: p.utteranceId })) });
"""


def _run_ear_harness(tmp_path: Path) -> dict[str, Any]:
    harness = tmp_path / "ears.js"
    harness.write_text(
        _NODE_HARNESS.replace(
            "process.stdout.write(JSON.stringify(checkpoints));",
            _EAR_SCRIPT + "\nprocess.stdout.write(JSON.stringify(checkpoints));",
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
    return next(row for row in json.loads(completed.stdout) if row.get("case") == "ears")


def _plain(markup: str) -> str:
    return re.sub(r"<[^>]+>", "", markup)


@needs_node
def test_the_editor_shows_the_other_ear_and_take_only_fills_the_text_box(tmp_path: Path) -> None:
    report = _run_ear_harness(tmp_path)
    panel = report["altPanel"]
    editor = panel[panel.index('<fieldset class="vl-source-caption-editor">') :]
    assert "다른 귀: 치즈 &lt;하나&gt;" in editor  # 글자는 escape되어 읽기 전용 줄로
    assert "처음 들은 글" not in editor[: editor.index("<textarea")]  # 후보 줄의 처음 글은 지금 글
    assert 'data-vl-act="ear-take" data-vl-take="other"' in editor
    assert "「가져오기」는 문구 칸만 채웁니다 — 「저장」을 눌러야 바뀝니다." in editor
    # 이유와 「표시만」 안내는 따로 줄이다 — 붙어 읽히지 않는다(검증 지적 5).
    reasons = (
        '<span class="vl-badge" data-vl-suspect="1">확인 필요</span> 같은 말이 3번 이어집니다</p>'
    )
    assert (
        reasons + '<p class="vl-kv"><small>표시만 합니다 — 자막은 그대로입니다.</small></p>'
        in editor
    )
    # 가져오기 — 문구 칸만 채우고 서버를 부르지 않는다(자동 저장 없음).
    assert report["afterTake"] == {
        "value": "치즈 <하나>",
        "calls": 0,
        "foot": "문구 칸에 넣었습니다 — 「저장」을 눌러야 원본 자막이 바뀝니다.",
    }
    assert ">치즈 &lt;하나&gt;</textarea>" in report["redrawn"]
    # 「저장」은 사람이 누른다 — 그때 기존 편집 길로 가져온 글자가 간다.
    assert report["saved"] == [{"text": "치즈 <하나>", "action": "edit", "utteranceId": "u0"}]
    replaced = report["replacedPanel"]
    assert "다른 귀: 왕방 왔어요" in replaced
    assert "처음 들은 글 (whisper-1): 왕반 왔어요" in replaced  # 들은 엔진 이름(W-52b1)
    assert 'data-vl-take="first"' in replaced
    assert "두 귀가 맞춰 본 줄이라 다른 귀의 글자로 바꿨습니다." in replaced
    assert report["firstTaken"] == "왕반 왔어요"
    assert ">치즈</textarea>" in report["backPanel"]  # 다른 줄을 고르면 가져온 글자는 버린다
    # 가져온 글자를 버리면 그 각주도 걷는다 — 다른 각주(서버 답)는 건드리지 않는다(검증 지적 4).
    take_note = "문구 칸에 넣었습니다 — 「저장」을 눌러야 원본 자막이 바뀝니다."
    assert report["footTaken"] == take_note and report["footDropped"] == ""
    assert report["footKept"] == report["footAfterSave"] != take_note
    silent = report["silentPanel"]
    assert "확인 필요</span> 말소리를 못 찾음 (측정 한계일 수 있음)</p>" in silent
    assert "표시만 합니다 — 자막은 그대로입니다." in silent


def test_a_long_candidate_scrolls_inside_a_capped_line() -> None:
    """480자 후보도 줄 높이가 묶여 문구 칸·「저장」을 밀어내지 않는다 — 넘치면 줄 안에서
    스크롤한다(검증 지적 3)."""
    rules = dict(re.findall(r"(\.vl-ear-text) \{([^}]*)\}", ui_facade.WORKBENCH_CSS))
    body = rules[".vl-ear-text"]
    assert "max-height:" in body and "overflow-y: auto" in body
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    assert 'class="vl-ear-text" tabindex="0"' in script  # 키보드로도 스크롤한다


@needs_node
def test_the_rail_and_the_cue_list_mark_disagreements_without_moving_anything(
    tmp_path: Path,
) -> None:
    report = _run_ear_harness(tmp_path)
    chips = [c for c in report["rail"].split("<button") if "vl-source-caption" in c]
    assert len(chips) == 5  # 줄 수 그대로 — 아무것도 숨기지 않는다
    assert 'data-vl-ear="alt" data-vl-suspect="1" style="left:' in chips[0]
    assert "다른 귀는 다르게 들었습니다 — 고르지 않은 후보가 있습니다" in chips[0]
    assert "확인 필요 — 같은 말이 3번 이어집니다" in chips[0]
    assert 'data-vl-ear="replaced" style="left:' in chips[3]
    assert "다른 귀가 고친 줄입니다 — 처음 들은 글과 다릅니다" in chips[3]
    assert "data-vl-ear" not in chips[1] and "data-vl-ear" not in chips[4]
    listed = report["altPanel"]
    summary = "<summary>원본 자막 목록 · 확인 필요 4개 · 다른 귀 2개</summary>"
    assert summary in listed
    assert '<span class="vl-badge" data-vl-ear="alt" title=' in listed
    assert ">다른 귀 후보</span>" in listed and ">다른 귀로 고침</span>" in listed
    assert listed.count('data-vl-act="source-pick"') == 5
    css = ui_facade.WORKBENCH_CSS
    assert '.vl-source-caption[data-vl-ear="alt"]::after' in css
    assert '.vl-source-caption[data-vl-ear="replaced"] { box-shadow: inset 0 -2px 0' in css
    # 표지는 가상 원소·안쪽 그림자뿐 — 칩 자신의 자리·크기 칸을 건드리지 않는다(B단계 좌표).
    rules = dict(re.findall(r"(\.vl-source-caption\[data-vl-ear[^{]*)\{([^}]*)\}", css))
    alt = rules['.vl-source-caption[data-vl-ear="alt"]::after ']
    assert "position: absolute" in alt and "border-top: 7px solid var(--vl-warn)" in alt
    replaced = rules['.vl-source-caption[data-vl-ear="replaced"] ']
    assert replaced.strip() == "box-shadow: inset 0 -2px 0 var(--vl-accent-ink);"


# ── 용어집 칸 — 서랍 · 배선 · 저장 · 거절 · 견적 다시 묶기 ─────────────────────────────


def _ancestors(block: Any) -> list[Any]:
    found, node = [], getattr(block, "parent", None)
    while node is not None:
        found.append(node)
        node = getattr(node, "parent", None)
    return found


def test_blocks_put_the_glossary_in_the_settings_drawer_and_wire_save_and_refresh(
    tmp_path: Path, local_socket: None
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("용어집", str(tmp_path / "ws"))
    app = ui.build_blocks()
    by_id = {getattr(b, "elem_id", None): b for b in app.blocks.values()}
    box, save, status = by_id[GLOSSARY_ID], by_id[GLOSSARY_SAVE_ID], by_id[GLOSSARY_STATUS_ID]
    assert isinstance(box, gr.Textbox) and box.label == GLOSSARY_LABEL and box.info == GLOSSARY_INFO
    assert isinstance(save, gr.Button) and save.value == "용어집 저장"
    assert by_id["settings-drawer"] in _ancestors(box)
    loaded = ui._glossary_loaded_state
    assert isinstance(loaded, gr.State)  # 이 탭이 불러온 용어집(sha·글) — 탭마다 따로다
    deps = app.config["dependencies"]
    (clicked,) = [d for d in deps if d["targets"] == [(save._id, "click")]]
    assert clicked["inputs"] == [ui._signature_state._id, box._id, loaded._id]
    assert clicked["outputs"] == [status._id, box._id, ui._capcut_progress_md._id, loaded._id]
    assert not [d for d in deps if (box._id, "change") in d["targets"]]  # 저장은 단추뿐이다
    # 쓰지 않은 칸만 맞추는 `glossary_tick`은 1초 상태 틱 하나 안에서 돈다(D63 세션 C).
    (tick,) = [f for f in app.fns.values() if getattr(f.fn, "__name__", "") == "status_tick"]
    inputs, outputs = [b._id for b in tick.inputs], [b._id for b in tick.outputs]
    assert inputs[0] == ui._signature_state._id and inputs[3:5] == [box._id, loaded._id]
    assert outputs[2:5] == [box._id, status._id, loaded._id]
    refreshers = [
        f for f in app.fns.values() if getattr(f.fn, "__name__", "") == "glossary_controls"
    ]
    # 작업공간을 열고·만들고·바꾸고·닫는 사건마다(영상 분석 모델 칸과 같다 — 닫기는 D64 세션 F)
    assert len(refreshers) == 11  # 폴더 불러오기 셋(W-46a — 단추 · Enter · 찾아보기 뒤)
    assert all(
        [block._id for block in f.outputs] == [box._id, status._id, loaded._id] for f in refreshers
    )


def test_saving_the_glossary_refuses_bad_letters_politely_and_keeps_what_was_typed(
    tmp_path: Path, local_socket: None
) -> None:
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("용어집", str(tmp_path / "ws"))
    root = ui.workspace_root()
    assert root is not None
    signature = ui.current_signature()
    loaded = ui.glossary_loaded_value()
    status, box, summary, kept = ui.glossary_save_view(signature, "왕방\n<b>백숙</b>\n", loaded)
    assert "용어집을 저장하지 않았습니다" in status and "그 줄을 고쳐 주세요" in status
    assert "\\<" in status  # 글 그대로 그려진다(마크다운이 꾸미지 않는다)
    assert box == gr.skip() and summary == gr.skip()  # 사람이 쓴 칸은 그대로 둔다
    assert kept == gr.skip() and ui.raw.glossary(root).terms == ()
    status, box, _, loaded = ui.glossary_save_view(signature, "왕방\n# 메모\n백숙\n왕방\n", loaded)
    assert status.startswith("용어집을 저장했습니다 — 용어 2개")
    assert box.value == "왕방\n백숙" and ui.raw.glossary(root).terms == ("왕방", "백숙")
    assert loaded == [ui.raw.glossary(root).sha, "왕방\n백숙"]  # 저장한 것이 이 탭의 기준이 된다
    status, box, summary, _ = ui.glossary_save_view(signature, "왕방\n백숙", loaded)
    assert "지금 용어집에는 용어가 2개 있습니다" in status and summary == gr.skip()
    controls_box, controls_status, controls_loaded = ui.glossary_controls()
    assert controls_box.value == "왕방\n백숙" and "용어가 2개" in controls_status
    assert controls_loaded == loaded
    reopened = RawWorkspaceUi(port=7993, state_path=tmp_path / "state2.json")
    reopened.open_workspace_view(str(root))
    assert reopened.glossary_value() == "왕방\n백숙"  # 앱을 다시 열어도 그 작업공간의 용어집
    with pytest.raises(gr.Error):
        ui.glossary_save_view("다른 화면", "왕방", loaded)
    ui._analysis_busy = lambda _root: True  # type: ignore[method-assign]
    status, box, summary, _ = ui.glossary_save_view(signature, "닭도리탕", loaded)
    assert GLOSSARY_BUSY_TEXT.split(" — ")[0] in status.replace("\\", "")
    assert ui.raw.glossary(root).terms == ("왕방", "백숙")


def test_a_glossary_saved_in_another_tab_is_never_silently_overwritten(
    tmp_path: Path, local_socket: None
) -> None:
    """탭마다 불러온 용어집에 저장을 묶는다(검증 지적 6) — 그사이 다른 탭이 저장했으면 한 번
    거절하고
    지금 용어집을 보이며, 쓴 글은 그대로 둔다. 확인한 뒤 다시 누르면 쓴 글로 저장한다. 1초 틱은 칸이
    불러온 그대로일 때(사람이 쓰지 않았을 때)만 칸을 새 용어집으로 맞춘다."""
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("용어집", str(tmp_path / "ws"))
    root = ui.workspace_root()
    assert root is not None
    signature = ui.current_signature()
    tab_a = ui.glossary_loaded_value()
    tab_b = ui.glossary_loaded_value()
    tab_c = ui.glossary_loaded_value()
    assert tab_a == [ui.raw.glossary(root).sha, ""]
    _, _, _, tab_b = ui.glossary_save_view(signature, "왕방", tab_b)  # 탭 B가 먼저 저장했다
    status, box, summary, tab_a = ui.glossary_save_view(signature, "백숙", tab_a)
    assert status.replace("\\", "").startswith(
        "다른 탭에서 용어집이 바뀌었습니다 — 지금 용어집: 왕방."
    )
    assert "쓴 글은 그대로 두었습니다" in status and POLITE_END.search(status.replace("\\", ""))
    assert box == gr.skip() and summary == gr.skip()  # 쓴 「백숙」은 칸에 그대로 남는다
    assert ui.raw.glossary(root).terms == ("왕방",) and tab_a == tab_b  # 덮어쓰지 않았다
    status, box, _, tab_a = ui.glossary_save_view(signature, "백숙", tab_a)  # 확인한 뒤 다시
    assert status.startswith("용어집을 저장했습니다") and ui.raw.glossary(root).terms == ("백숙",)
    # 탭 C는 칸을 고치지 않았다 — 틱이 새 용어집으로 맞춘다. 탭 B는 칸에 쓰는 중이다 — 그대로 둔다.
    box, status, tab_c = ui.glossary_tick(signature, "", tab_c)
    assert box.value == "백숙" and tab_c == tab_a and "용어가 1개" in status
    assert ui.glossary_tick(signature, "백숙", tab_c) == (gr.skip(), gr.skip(), gr.skip())
    typing = ui.glossary_tick(signature, "왕방\n닭도리탕", tab_b)
    assert typing == (gr.skip(), gr.skip(), gr.skip())
    assert ui.glossary_tick("다른 화면", "", tab_b) == (gr.skip(), gr.skip(), gr.skip())


def test_an_unreadable_glossary_is_warned_not_swallowed(
    tmp_path: Path, local_socket: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """저장된 용어집이 깨졌으면 칸은 비우지만 WARN을 남긴다(검증 지적 7) — 1초 틱이 같은 경고를
    되풀이하지는 않는다."""
    ui = RawWorkspaceUi(port=7993, state_path=tmp_path / "state.json")
    ui.create_workspace_view("용어집", str(tmp_path / "ws"))
    root = ui.workspace_root()
    assert root is not None
    warnings: list[str] = []
    monkeypatch.setattr(workspace_service.log, "warn", warnings.append)

    def broken(_root: Path) -> Any:
        raise raw.RawWorkspaceError(
            "저장된 용어집의 모양이 어긋났습니다 — 용어집을 다시 저장해 주세요"
        )

    monkeypatch.setattr(ui.raw, "glossary", broken)
    assert ui.glossary_value() == "" and ui._glossary_sha(root) == ""
    loaded = ["s", ""]
    for _ in range(3):
        assert ui.glossary_tick(ui.current_signature(), "", loaded) == (
            gr.skip(),
            gr.skip(),
            gr.skip(),
        )
    _, status, _ = ui.glossary_controls()
    assert "모양이 어긋났습니다" in status.replace("\\", "")
    assert len(warnings) == 1 and "용어집을 읽지 못했습니다" in warnings[0]


@needs_ffmpeg
def test_saving_the_glossary_makes_the_next_approval_ask_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """두 영상의 글자 답이 빈 용어집으로 캐시에 있다 — 승인이 잴 범위에 물을 글자가 없다. 「왕방」을
    저장하면 같은 청크가 새 용어집으로는 답이 없으므로 다음 승인이 그 청크를 다시 묻는다(저장은
    호출 0회)."""
    ui, ears, client, root, ids = _library(tmp_path, monkeypatch, on=False, sender=EarsSender())
    approve(ui, TRANSCRIBE)
    wait_until(lambda: all(stage(ui, root, i) == raw.TRANSCRIBED_STAGE for i in ids.values()))
    _settled(ui, root)
    for asset_id in ids.values():
        _cache_live_answer(ui, root, asset_id)
    _turn(ui, root, True)
    before = ui.raw.selection_quote(root, ui._library_ids(root))
    assert not any(e.text_chunk_count for e in before.estimates)
    sent = (len(ears.whisper), len(ears.text), len(client.calls))
    status, box, shown, _ = ui.glossary_save_view(
        ui.current_signature(), "왕방\n", ui.glossary_loaded_value()
    )
    assert status.replace("\\", "").startswith("용어집을 저장했습니다 — 용어 1개.")
    assert box.value == "왕방"
    assert shown == ui._quick_status()  # 상태 줄을 바로 다시 그린다
    _settled(ui, root)
    after = ui.raw.selection_quote(root, ui._library_ids(root))
    assert after.digest != before.digest
    assert [e.text_chunk_count for e in after.estimates] == [1, 1]
    assert (
        len(ears.whisper),
        len(ears.text),
        len(client.calls),
    ) == sent  # 저장은 아무것도 사지 않는다
    workspace = ui.raw._workspace(root)
    assert glossary.current(workspace).terms == ("왕방",)


# ── 문구(D57) — 「VAD」·금액·모델 이름 없음, 존댓말 ────────────────────────────────────


def test_the_screen_never_says_vad_in_the_fixed_wording_and_keeps_error_tails_verbatim() -> None:
    """화면 말은 코드가 쓴 고정 머리만 바꾼다 — ` — ` 뒤의 오류·경로는 바이트 그대로
    (검증 지적 1)."""
    tail = "onnx SileroVADIterator: EVADE at /Volumes/VADUZ/x.mov"
    document = {
        "verdict": "skip",
        "reasons": [
            {"text": f"{sync_report.VAD_FAILED_HEAD} — {tail}"},
            {"text": f"{sync_report.VAD_MISSING_HEAD} — No module named 'onnxruntime'"},
            {"text": "RuntimeError: VADUZ 볼륨이 없다"},  # 모르는 글은 손대지 않는다
        ],
        "checks": [{"name": "전체 어긋남", "status": "skip", "detail": "잴 수 없음 — VAD 2"}],
        "cues": [{"id": "u6", "text": "여기 봐 VAD", "flags": ["no_speech"]}],
    }
    view = sync_report.view_of(document, running=False)
    assert view is not None
    assert view["reasons"] == [
        f"말소리 측정이 실패해 잴 수 없음 — {tail}",
        "말소리 측정 런타임이 없어 잴 수 없음 (선택 extra `project`) — No module named"
        " 'onnxruntime'",
        "RuntimeError: VADUZ 볼륨이 없다",
    ]
    assert view["checks"] == document["checks"]  # 고정 머리가 아니면 그대로
    assert view["cues"][0]["text"] == "여기 봐 VAD"  # 자막 글자는 화면 말이 아니다 — 그대로
    shutdown = f"SyncShutdown: {sync_report.VAD_SHUTDOWN_HEAD} — {tail}"
    failed = sync_report.failure_view(shutdown, running=False)
    assert failed["reasons"] == [f"SyncShutdown: 앱이 끝나는 중이라 말소리 측정을 멈췄다 — {tail}"]
    for head in (
        sync_report.VAD_FAILED_HEAD,
        sync_report.VAD_MISSING_HEAD,
        sync_report.VAD_SHUTDOWN_HEAD,
    ):
        assert "VAD" in head and "VAD" not in sync_report.screen_text(head)
    for source in (ui_facade.VISUAL_BOOTSTRAP_JS, ui_facade.WORKBENCH_CSS):
        assert "VAD" not in source


def test_the_new_copy_is_polite_and_names_no_model_or_price() -> None:
    copy = (
        GLOSSARY_LABEL,
        GLOSSARY_INFO,
        GLOSSARY_BUSY_TEXT,
        workspace_service._glossary_now(glossary.Glossary()),
        workspace_service._glossary_now(glossary.Glossary(sequence=0, terms=("왕방",))),
        workspace_service._glossary_saved(glossary.Glossary(sequence=0, terms=("왕방",))),
        workspace_service._glossary_saved(glossary.Glossary(sequence=1)),
        workspace_service._glossary_moved(glossary.Glossary()),
        workspace_service._glossary_moved(
            glossary.Glossary(sequence=2, terms=tuple(f"용어{i}" for i in range(7)))
        ),
    )
    for text in copy:
        assert "$" not in text and "VAD" not in text, text
        assert not re.search(r"GPT|whisper|Transcribe|Claude|Sonnet|Luna", text), text
    for sentence in copy[1:]:
        assert POLITE_END.search(sentence), sentence
    assert "지금 용어집: 비어 있음." in copy[7]
    assert "지금 용어집: 용어0 · 용어1 · 용어2 · 용어3 · 용어4 외 2개." in copy[8]
    # 용어집은 두 범위의 견적 모두에 실린다 — 「전사만」 견적만 말하지 않는다(검증 지적 2).
    for text in (GLOSSARY_INFO, copy[5], copy[6]):
        assert "다음 견적" in text and "「전사만」 견적" not in text, text
    script = ui_facade.VISUAL_BOOTSTRAP_JS
    for phrase in (
        "다른 귀는 다르게 들었습니다 — 고르지 않은 후보가 있습니다",
        "다른 귀가 고친 줄입니다 — 처음 들은 글과 다릅니다",
        "두 귀가 맞지 않아 글자를 바꾸지 않았습니다.",
        "두 귀가 맞춰 본 줄이라 다른 귀의 글자로 바꿨습니다.",
        "문구 칸에 넣었습니다 — 「저장」을 눌러야 원본 자막이 바뀝니다.",
    ):
        assert phrase in script, phrase
    start = script.index("function earNote(")
    added = script[start : script.index("function captionEditor(")]
    assert "$" not in added and not re.search(r"GPT|whisper|Transcribe", added)
