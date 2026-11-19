"""원본 범위 타임라인 재생기(W-71c) — 렌더하지 않고 원본마다 미리보기를 틀어 컷을 건넌다.

「타임라인으로 보내기」(W-71b, D126)가 만든 타임라인을 「최종 편집」 탭 머리에서 튼다. 클립이 있는
원본마다 원본 전체 미리보기(`/media/<등록 ID>` — 서버가 준 ID만)를 쓰고, 원본 국소 s초는 미리보기
`원점 + s`초다(`video_origin_seconds`). 영상은 둘이다 — 보이는 활성 영상과, 다음 컷 뒤 클립
시작에 미리 맞춘 대기 영상. 컷에서 둘을 바꿔 틀어(`TL.playStep`) 다른 원본으로 가는 시크를 기다리지
않는다. 미리보기가 없는 원본은 「준비 안 됨」으로 보이고 거기서 멈춘다(조용히 건너뛰지 않는다) —
사람이 누르면 그 원본들만 무료 로컬 준비를 건다.

화면 틀(`RawWorkspaceUi`)과는 `owner`의 작은 면(선택 확인 · 수명 토큰 · 서비스)으로만 닿는다.
`vlogsrt.project`는 import하지 않는다 — 서비스의 오류 종류는 만들 때 받는다.
"""

from __future__ import annotations

import html
import json
import uuid
from collections.abc import Callable
from typing import Any

import gradio as gr

from ui import keymap, platform_keys
from ui.caption_grid_js import CAPTION_GRID_JS
from ui.caption_lane_js import CAPTION_LANE_JS
from ui.timeline_edit_js import TIMELINE_CORE_JS
from ui.timeline_player_js import PLAYER_APP_JS

PLAYER_BLOCK_ID = "vl-timeline-player"
PLAYER_ROOT_ID = "tl-root"
TAB_LABEL = "원본 범위 타임라인"
BURN_NONE = "none"
"""「번인 캡션: 없음」 — 예전 렌더 그대로(`timeline_share.BURN_NONE`과 같다, 시험이 대조한다)."""
JOB_ENDED = frozenset({"succeeded", "failed", "cancelled"})
"""끝난 작업 상태 — 렌더 작업을 묻는 `tl_job`이 끝을 알린다."""
PLAYER_JS = (
    TIMELINE_CORE_JS + CAPTION_GRID_JS + CAPTION_LANE_JS + keymap.keymap_js() + PLAYER_APP_JS
)
READY = "ready"


def _rational(row: dict[str, Any]) -> tuple[int, int]:
    return int(row["num"]), int(row["den"])


def burn_model(style: Any, settings: Any) -> dict[str, Any]:
    """자막 겹(W-98, D173)이 번인 모양을 근사하는 재료 — 저장한 모양과 번인 설정의 비율들.
    (화면 계층은 프로젝트 계층을 import하지 않는다 — 받은 값의 칸만 읽는다.)"""
    keys = ("position", "font_px", "outline_px", "margin_px", "text_color")
    return {
        "style": {key: getattr(style, key) for key in keys},
        "referenceHeight": settings.reference_height,
        "englishScale": float(settings.english_scale),
        "sideMargin": float(settings.side_margin_ratio),
        "outlineColor": settings.outline_color,
    }


def client_model(
    signature: str,
    token: str,
    opened: dict[str, Any],
    media: dict[str, Any],
    tiers: dict[str, Any],
    burn: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """브라우저가 받는 재료 — 문서는 브라우저 핵심(`__vlTL`)의 모양 그대로다."""
    facts = opened["facts"]
    num, den = _rational(facts["sequence"]["frame_duration"])
    sources = []
    names: dict[str, str] = {}
    for row in facts["sources"]:
        source_num, source_den = _rational(row["frame_duration"])
        source_id = str(row["source_id"])
        sources.append(
            {"id": source_id, "num": source_num, "den": source_den, "frames": row["frame_count"]}
        )
        names[source_id] = str(row.get("name", source_id))
    payload = opened["doc"]
    return {
        "epoch": uuid.uuid4().hex,
        "signature": signature,
        "token": token,
        "timelineId": opened["timelineId"],
        "revision": opened["revision"],
        "frame": {
            "width": int(facts["sequence"]["width"]),
            "height": int(facts["sequence"]["height"]),
        },
        "doc": {
            "facts": {"sequence": {"num": num, "den": den}, "sources": sources},
            "clips": payload["clips"],
            "cues": payload["cues"],
            "nextCue": payload["nextCue"],
            "seeded": payload["seeded"],
            # 고친 영어(D166)는 있을 때만 — 없는 문서의 화면 모델은 예전 바이트 그대로다.
            **({"english": payload["english"]} if "english" in payload else {}),
        },
        "names": names,
        "media": media["sources"],
        "tiers": tiers,
        "settings": opened["settings"],
        "burn": burn,
    }


def _model_holder(model: dict[str, Any] | None) -> str:
    """모델은 숨은 칸의 글자로 싣는다(최종 편집과 같은 꼴 — `<`·`&`는 JSON 안에서 이스케이프)."""
    payload = json.dumps(model or {"epoch": uuid.uuid4().hex, "empty": True}, ensure_ascii=False)
    text = payload.replace("<", "\\u003c").replace("&", "\\u0026")
    return f'<div class="tl-model" data-tl-model hidden>{text}</div>'


def timeline_player_html(model: dict[str, Any] | None) -> str:
    """재생기 뼈대 — 화면 틀(시퀀스 비율) · 영상 둘 · 자막 겹 · 이동 막대 · 클립 띠. 없으면 빈 칸"""
    # (빈 칸도 모델 칸은 싣는다 — 실행본이 「타임라인 없음」을 읽는다).
    if model is None:
        return f'<div id="{PLAYER_ROOT_ID}" class="tl-root tl-empty">{_model_holder(None)}</div>'
    frame = model["frame"]
    ratio = f"{int(frame['width'])} / {int(frame['height'])}"
    return (
        f'<div id="{PLAYER_ROOT_ID}" class="tl-root" tabindex="0" data-vl-own-keys'
        f' aria-label="{html.escape(TAB_LABEL)} 재생기">{_model_holder(model)}'
        f'<div class="tl-stage"><div class="tl-frame" style="aspect-ratio: {ratio}">'
        '<video id="tl-deck-a" class="tl-deck" preload="auto" playsinline></video>'
        '<video id="tl-deck-b" class="tl-deck is-standby" preload="auto" playsinline></video>'
        '<div id="tl-missing" class="tl-missing" hidden></div>'
        '<div id="tl-overlay" class="tl-overlay"></div></div></div>'
        + platform_keys.display(
            '<div class="tl-transport">'
            '<button type="button" data-tl="prev-cut" title="이전 컷 (↑)">⏮</button>'
            '<button type="button" data-tl="toggle" id="tl-toggle"'
            ' title="재생 · 멈춤 (Space)">▶</button>'
            '<button type="button" data-tl="next-cut" title="다음 컷 (↓)">⏭</button>'
            '<span id="tl-timecode" class="tl-timecode">00:00:00:00</span>'
            '<span id="tl-source" class="tl-source"></span>'
            '<span class="tl-gap"></span>'
            '<button type="button" data-tl="blade"'
            ' title="플레이헤드에서 클립 자르기 (⌘B)">블레이드</button>'
            '<button type="button" data-tl="delete-clip"'
            ' title="고른 클립 지우기 — 뒤가 당겨 온다 (⌫)">삭제</button>'
            '<button type="button" data-tl="join"'
            ' title="고른 클립을 맞닿은 다음 조각과 합치기">합치기</button>'
            '<button type="button" data-tl="undo" id="tl-undo"'
            ' title="실행 취소 (⌘Z)">실행 취소</button>'
            '<button type="button" data-tl="redo" id="tl-redo"'
            ' title="실행 복귀 (⌘⇧Z)">실행 복귀</button>'
            '<button type="button" data-tl="force-save" id="tl-force" hidden>'
            "내 편집으로 덮기</button>"
            '<button type="button" data-tl="prepare" id="tl-prepare" hidden>미리보기 준비</button>'
            # 「공유」(W-88, D147) — Final Cut 공유 패널의 「번인 캡션:」(없음 · 언어)을 옮겼다.
            '<label class="tl-burn" title="공유할 때 영상에 입힐 자막(Final Cut 「번인 캡션」)">'
            '번인 캡션 <select id="tl-burn" aria-label="번인 캡션">'
            '<option value="none">없음 — 렌더만</option>'
            '<option value="ko">한국어</option>'
            '<option value="ko-en">한국어 + 영어</option></select></label>'
            '<button type="button" data-tl="render" id="tl-render"'
            ' title="저장한 타임라인을 원본에서 한 번 렌더하고, 고르면 자막을 입힌다(무료 로컬)">'
            "공유</button>"
            '<button type="button" data-tl="index" id="tl-index-toggle" aria-pressed="false"'
            ' title="타임라인 인덱스 — 클립 · 자막 목록 (⇧⌘2)">인덱스</button>'
            '<span id="tl-status" class="tl-status" aria-live="polite"></span></div>'
            # 타임라인 인덱스(W-86c, D150) — Final Cut 「인덱스」: 클립 · 자막(「오류 보기」) 목록.
            '<div id="tl-index" class="tl-index" hidden><div class="tl-index-head">'
            '<button type="button" data-tl="index-clips" aria-pressed="true">클립</button>'
            '<button type="button" data-tl="index-cues" aria-pressed="false">자막</button>'
            '<label class="tl-index-errors"><input type="checkbox" id="tl-index-errors">'
            " 오류 보기</label></div>"
            '<div id="tl-index-list" class="tl-index-list" role="listbox"'
            ' aria-label="타임라인 인덱스 — 누르면 그 자리로 간다"></div></div>'
            '<div class="tl-strip"><canvas id="tl-canvas"></canvas></div>'
            '<div class="tl-range"><div class="tl-range-head"><span id="tl-source-head"></span>'
            '<span class="tl-gap"></span>'
            '<select id="tl-add-pick" class="tl-add-pick" aria-label="클립이 없는 원본" hidden>'
            "</select>"
            '<button type="button" data-tl="add-source" id="tl-add" hidden'
            ' title="고른 원본 전체를 촬영 차례 자리에 클립으로 넣는다">원본 넣기</button>'
            '<button type="button" data-tl="trim-start"'
            ' title="클립 앞을 커서로 (⌥[)">앞 ⌥[</button>'
            '<button type="button" data-tl="trim-end" title="클립 뒤를 커서로 (⌥])">뒤 ⌥]</button>'
            "</div>"
            '<div class="tl-source-bar"><canvas id="tl-source-canvas"'
            ' aria-label="고른 클립의 원본 범위 — 손잡이를 끌어 앞뒤를 조정"></canvas></div></div>'
        )
        + f"{_cue_panel_html()}{keymap.keysheet_html()}</div>"
    )


def _cue_panel_html() -> str:
    """자막 칸(W-71i) — 보이는 자막 목록과 고른 자막의 편집 칸. 내용은 실행본이 그린다."""
    return platform_keys.display(
        '<div class="tl-cues"><div class="tl-cues-head"><strong>자막</strong>'
        '<span id="tl-cue-count" class="tl-cue-count"></span><span class="tl-gap"></span>'
        '<button type="button" data-tl="cue-insert"'
        ' title="플레이헤드에 빈 자막 넣기 (⌥C)">새 자막 ⌥C</button>'
        # 번역 모드(W-87d, D153) — Subtitle Edit 「번역 모드 전환」: 영어 열을 붙인다(읽기만).
        '<button type="button" data-tl="translation" id="tl-translation" aria-pressed="false"'
        ' title="번역 모드 전환 — 렌더한 초안의 저장된 영어를 한국어 글로 찾아 보인다'
        '(사지 않는다)">번역 모드</button></div>'
        '<canvas id="tl-lane" class="tl-lane" tabindex="-1"'
        ' aria-label="파형 자막 레인 — 위 클립 띠의 끝을 끌어 클립 길이를 고치고,'
        ' 자막 블록을 끌어 시각을 고치고, 빈 곳을 끌어 새 선택(Enter로 넣기)">'
        "</canvas>"
        '<div class="tl-cues-body"><div id="tl-cue-list" class="tl-cue-list" role="listbox"'
        ' aria-label="보이는 자막 — 누르면 고르고 그 시작으로 간다"></div>'
        '<div id="tl-cue-edit" class="tl-cue-edit"></div></div></div>'
    )


def timeline_error_html(text: str) -> str:
    """재생기를 열지 못한 까닭 — 빈 칸으로 숨기지 않는다(글은 이스케이프한다)."""
    return (
        f'<div id="{PLAYER_ROOT_ID}" class="tl-root tl-error">{_model_holder(None)}'
        f"<p>{html.escape(text)}</p></div>"
    )


TIMELINE_PLAYER_CSS = """
#vl-timeline-player { padding: 0; }
.tl-root { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; outline: none;
  font-family: var(--vl-font-sans); color: var(--vl-ink); }
.tl-root:focus-visible { box-shadow: 0 0 0 2px var(--vl-accent); border-radius: 6px; }
.tl-empty { display: none; }
.tl-error p { margin: 0; padding: 6px 10px; border: 1px solid var(--vl-warn); border-radius: 6px;
  background: var(--vl-warn-soft); font-size: 13px; }
.tl-stage { display: flex; justify-content: center; background: var(--vl-video-well);
  border-radius: 6px; padding: 4px; }
.tl-frame { position: relative; height: min(48vh, 520px); max-width: 100%;
  background: var(--vl-video-well); overflow: hidden; }
.tl-deck { position: absolute; inset: 0; width: 100%; height: 100%; object-fit: contain;
  background: var(--vl-video-well); }
.tl-deck.is-standby { opacity: 0; pointer-events: none; }
.tl-missing { position: absolute; inset: 0; display: flex; align-items: center;
  justify-content: center; text-align: center; padding: 16px; font-size: 14px;
  color: var(--vl-on-video-overlay); background: var(--vl-video-well); }
.tl-missing[hidden] { display: none; }
.tl-overlay { position: absolute; left: 6%; right: 6%; bottom: 7%; text-align: center;
  pointer-events: none; white-space: pre-line; font-size: clamp(14px, 2.2vw, 28px);
  font-weight: 600; color: var(--vl-on-video-overlay);
  text-shadow: 0 0 3px var(--vl-video-well), 0 0 3px var(--vl-video-well),
    0 0 6px var(--vl-video-well); }
/* 번인 모양을 따르는 자막 겹(W-98, D173) — 크기·색·외곽선·자리는 실행본이 저장한 모양으로 단다. */
.tl-overlay.is-styled { text-shadow: none; line-height: 1.2; display: flex;
  flex-direction: column; align-items: center; justify-content: flex-end; gap: 0.1em;
  font-family: "Pretendard", var(--vl-font-sans); }
.tl-overlay.is-styled.is-top { justify-content: flex-start; }
.tl-overlay.is-styled > div { paint-order: stroke fill; overflow-wrap: anywhere; }
.tl-transport { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
.tl-transport button { font: inherit; font-size: 12px; padding: 2px 8px; border-radius: 4px;
  cursor: pointer; border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  color: var(--vl-ink); }
.tl-transport button[hidden] { display: none; }
.tl-timecode { font-family: var(--vl-font-mono); font-size: 14px; }
.tl-source { color: var(--vl-ink-muted); }
.tl-gap { flex: 1 1 auto; }
.tl-status { color: var(--vl-ink-muted); flex: 1 0 100%; min-height: 1.4em; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.tl-status.is-error { color: var(--vl-danger); }
.tl-strip { position: relative; height: 56px; border: 1px solid var(--vl-line);
  border-radius: 6px; background: var(--vl-surface-sunken); overflow: hidden; }
.tl-range { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.tl-range-head { display: flex; gap: 6px; align-items: center; color: var(--vl-ink-muted); }
.tl-range-head select { font: inherit; font-size: 12px; max-width: 220px; }
.tl-range-head button { font: inherit; font-size: 12px; padding: 1px 8px; border-radius: 4px;
  cursor: pointer; border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  color: var(--vl-ink); }
.tl-source-bar { position: relative; height: 36px; border: 1px solid var(--vl-line);
  border-radius: 6px; background: var(--vl-surface-sunken); overflow: hidden; }
.tl-source-bar canvas { position: absolute; inset: 0; width: 100%; height: 100%;
  touch-action: none; }
.tl-transport button:disabled { opacity: 0.5; cursor: default; }
.tl-strip canvas { position: absolute; inset: 0; width: 100%; height: 100%; cursor: pointer;
  touch-action: none; }
.tl-burn { display: inline-flex; gap: 4px; align-items: center; font-size: 12px;
  color: var(--vl-ink-muted); }
.tl-burn select { font: inherit; font-size: 12px; }
/* 오른쪽 누름 메뉴(Subtitle Edit) — 문서 위에 뜬다(재생기 밖, 누른 자리). */
.tl-menu { position: fixed; z-index: 1000; display: flex; flex-direction: column; min-width: 180px;
  padding: 4px 0; background: var(--vl-surface-raised); border: 1px solid var(--vl-line-strong);
  border-radius: 6px; box-shadow: var(--vl-shadow-pop); }
.tl-menu button { text-align: left; padding: 6px 12px; border: 0; background: transparent;
  color: var(--vl-ink); font: inherit; font-size: 13px; cursor: pointer; }
.tl-menu button:hover, .tl-menu button:focus-visible { background: var(--vl-accent-soft); }
.tl-cues { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.tl-cues-head { display: flex; gap: 6px; align-items: center; }
.tl-cue-count { color: var(--vl-ink-muted); }
.tl-cues button { font: inherit; font-size: 12px; padding: 1px 8px; border-radius: 4px;
  cursor: pointer; border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  color: var(--vl-ink); }
.tl-cues button:disabled { opacity: 0.5; cursor: default; }
/* 키를 받을 창(W-89, D154) — 마지막으로 누른 창을 옅게 표시한다(⌫ 등 뜻이 창마다 다르다). */
.tl-root[data-tl-pane="captions"] .tl-cues,
.tl-root[data-tl-pane="timeline"] .tl-strip { box-shadow: 0 0 0 1px var(--vl-accent-soft); }
.tl-keys { font-size: 12px; color: var(--vl-ink-muted); }
.tl-keys h4 { margin: 6px 0 2px; font-size: 12px; color: var(--vl-ink); }
.tl-keys dl { display: grid; grid-template-columns: max-content 1fr; gap: 2px 10px; margin: 0; }
.tl-keys dt { white-space: nowrap; }
.tl-keys dd { margin: 0; }
.tl-index { display: flex; flex-direction: column; gap: 4px; font-size: 12px; }
.tl-index[hidden] { display: none; }
.tl-index-head { display: flex; gap: 6px; align-items: center; }
.tl-index-head button[aria-pressed="true"] { background: var(--vl-accent-soft); }
.tl-index-errors { color: var(--vl-ink-muted); }
.tl-index-list { max-height: 160px; overflow-y: auto; border: 1px solid var(--vl-line);
  border-radius: 6px; background: var(--vl-surface-sunken); }
.tl-index-row { display: grid; grid-template-columns: 2.5em 6.5em minmax(0, 1fr) auto;
  column-gap: 8px; padding: 3px 8px; cursor: pointer; border-bottom: 1px solid var(--vl-line); }
.tl-index-row.is-selected { box-shadow: inset 3px 0 0 var(--vl-accent); }
.tl-index-row > span:nth-child(3) { min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
/* 인덱스는 덱의 왼쪽 열(W-86d, D151) — Final Cut처럼 타임라인 옆에 선다(넓은 화면만). 제 내용
   높이로 덱을 늘리지 않고(`contain: size`) 옆 칸들 높이만큼 서서 목록이 그 안에서 스크롤한다. */
@media (min-width: 1051px) {
  .tl-root:has(> #tl-index:not([hidden])) { display: grid; row-gap: 6px; column-gap: 8px;
    grid-template-columns: minmax(0, var(--vl-index-w)) minmax(0, 1fr);
    grid-template-areas: "stage stage" "transport transport" "index strip" "index range"
      "index cues"; }
  .tl-root:has(> #tl-index:not([hidden])) > .tl-stage { grid-area: stage; }
  .tl-root:has(> #tl-index:not([hidden])) > .tl-transport { grid-area: transport; }
  .tl-root:has(> #tl-index:not([hidden])) > .tl-strip { grid-area: strip; }
  .tl-root:has(> #tl-index:not([hidden])) > .tl-range { grid-area: range; }
  .tl-root:has(> #tl-index:not([hidden])) > .tl-cues { grid-area: cues; }
  .tl-root > #tl-index { grid-area: index; contain: size; min-height: 0; }
  .tl-root > #tl-index .tl-index-list { flex: 1 1 auto; min-height: 0; max-height: none; }
}
.tl-lane { display: block; width: 100%; height: 124px; border-radius: 6px;
  touch-action: none; }
.tl-cues-body { display: flex; gap: 8px; align-items: stretch; }
.tl-cue-list { position: relative; flex: 1 1 60%; min-width: 0; max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--vl-line); border-radius: 6px; background: var(--vl-surface-sunken); }
/* Subtitle Edit식 표(W-87, D146) — # · 시작 · 끝 · 길이 · 텍스트 · 간격 · 문자/초.
   머리는 목록 위에 붙는다. */
.tl-cue-row, .tl-cue-grid-head { display: grid; column-gap: 8px; padding: 3px 8px;
  grid-template-columns: 2.5em 6.5em 6.5em 3.5em minmax(0, 1fr) 3.5em 3.5em; }
/* 번역 모드(W-87d) — 텍스트 옆에 영어 열. 짝이 없는 줄은 흐리게 「번역 없음」. */
.tl-cue-list.is-translation .tl-cue-row, .tl-cue-list.is-translation .tl-cue-grid-head {
  grid-template-columns: 2.5em 6.5em 6.5em 3.5em minmax(0, 1fr) minmax(0, 1fr) 3.5em 3.5em; }
.tl-cue-en { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.tl-cue-en.is-edited { color: var(--vl-accent-ink); }
.tl-cue-en-edit { display: flex; flex-direction: column; gap: 2px; }
.tl-cue-en-edit[hidden] { display: none; }
.tl-cue-en.is-missing, .tl-cue-en-note.is-missing { color: var(--vl-ink-muted);
  font-style: italic; }
#tl-translation[aria-pressed="true"] { background: var(--vl-accent-soft); }
.tl-cue-grid-head { position: sticky; top: 0; z-index: 1; background: var(--vl-surface-raised);
  color: var(--vl-ink-muted); border-bottom: 1px solid var(--vl-line-strong); }
.tl-cue-row { cursor: pointer; border-bottom: 1px solid var(--vl-line); }
.tl-cue-row .tl-cue-n, .tl-cue-num { font-variant-numeric: tabular-nums; color: var(--vl-ink-muted);
  text-align: right; }
.tl-cues .is-bad { background: var(--vl-danger-soft); color: var(--vl-danger); }
.tl-cue-row:last-child { border-bottom: none; }
.tl-cue-row.is-current { background: var(--vl-accent-soft); }
.tl-cue-row.is-selected { box-shadow: inset 3px 0 0 var(--vl-accent); }
.tl-cue-row .tl-cue-time { font-family: var(--vl-font-mono); color: var(--vl-ink-muted);
  flex: none; }
.tl-cue-row .tl-cue-words { min-width: 0; overflow: hidden; text-overflow: ellipsis;
  white-space: nowrap; }
.tl-cue-row .tl-cue-warn { color: var(--vl-warn); flex: none; }
.tl-cue-empty { padding: 6px 8px; color: var(--vl-ink-muted); }
.tl-cue-edit { flex: 1 1 40%; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
.tl-cue-edit textarea { font: inherit; font-size: 13px; min-height: 52px; resize: vertical;
  padding: 4px 6px; border: 1px solid var(--vl-line-strong); border-radius: 4px;
  background: var(--vl-surface-raised); color: var(--vl-ink); }
.tl-cue-edit .tl-cue-buttons { display: flex; flex-wrap: wrap; gap: 4px; }
.tl-cue-edit .tl-cue-muted { color: var(--vl-ink-muted); }
.tl-cue-edit .tl-cue-warn { color: var(--vl-warn); min-height: 1.4em; }
.tl-cue-times { display: flex; flex-wrap: wrap; gap: 6px 10px; align-items: center; }
.tl-cue-times .tl-tc { width: 9em; font-family: var(--vl-font-mono); font-size: 12px;
  padding: 1px 4px; border: 1px solid var(--vl-line-strong); border-radius: 4px;
  background: var(--vl-surface-raised); color: var(--vl-ink); }
.tl-cue-times .tl-cue-num { text-align: left; padding: 0 4px; border-radius: 4px; }
@media (max-width: 720px) { .tl-cues-body { flex-direction: column; } }
"""


class TimelinePlayer:
    """칸 하나 — 「최종 편집」 탭 머리에서 타임라인 요약 아래에 선다."""

    def __init__(self, owner: Any, service_error: type[Exception], render_job_kind: str) -> None:
        self.owner = owner
        self.service_error = service_error
        self.render_job_kind = render_job_kind
        self.block: Any = None

    def build(self) -> None:
        self.block = gr.HTML(
            timeline_player_html(None),
            elem_id=PLAYER_BLOCK_ID,
            js_on_load=PLAYER_JS,
            server_functions=self.server_functions(),
            apply_default_css=False,
        )

    def wire(self, refresh_events: tuple[Any, ...], sent: Any) -> None:
        """작업공간이 바뀌면, 그리고 「타임라인으로 보내기」가 성공하면 다시 그린다."""
        signature = self.owner._signature_state
        for event in refresh_events:
            event.then(self.view, signature, self.block, show_progress="hidden")
        sent.success(self.view, signature, self.block, show_progress="hidden")

    def view(self, signature: str | None) -> str:
        """지금 작업공간의 가장 새 타임라인 — 없거나 못 읽으면 빈 칸(요약 칸이 까닭을 말한다)."""
        root = self.owner._snapshot_root()
        if root is None or not signature or signature != self.owner._signature_for(root):
            return timeline_player_html(None)
        if not self.owner.raw.has_timeline(root):
            return timeline_player_html(None)
        try:
            opened = self.owner.raw.timeline_open(root)
            media = self.owner.raw.timeline_media(root, opened["timelineId"])
        except self.service_error as error:
            return timeline_error_html(f"타임라인 재생기를 열지 못했습니다 — {error}")
        token = self.owner._visual_lifetime_token
        # 등급 띠는 고른 클립의 원본만 그때 묻는다(`tl_tiers`) — 원본 104개면 그리기마다 0.9초였다.
        raw = self.owner.raw
        burn = burn_model(raw.subtitle_burn_style(root), raw.subtitle_burn_settings())
        return timeline_player_html(client_model(signature, token, opened, media, {}, burn))

    # ── 서버 함수(클라이언트 `server.tl_*`) ──────────────────────────

    def server_functions(self) -> list[Callable[[dict[str, object]], dict[str, object]]]:
        def tl_media(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "media")

        def tl_prepare(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "prepare")

        def tl_save(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "save")

        def tl_render(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "render")

        def tl_tiers(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "tiers")

        def tl_add_source(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "add")

        def tl_job(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "job")

        def tl_wave(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "wave")

        def tl_english(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "english")

        return [
            tl_media,
            tl_prepare,
            tl_save,
            tl_render,
            tl_tiers,
            tl_add_source,
            tl_job,
            tl_wave,
            tl_english,
        ]

    def action(self, payload: dict[str, object], kind: str) -> dict[str, object]:
        """어떤 예외도 500이 아니라 값으로 돌아간다. 준비·저장·렌더는 서명·수명 토큰에 묶인다."""
        owner = self.owner

        def fail(error: object, *, stale: bool = False) -> dict[str, object]:
            return {"ok": False, "error": owner._serve_error(error), "stale": stale}

        try:
            root = owner._require_current_selection(owner._serve_str(payload, "signature") or None)
            if kind in ("prepare", "save", "render", "add"):
                owner._require_workbench_token(payload, owner._visual_lifetime_token, TAB_LABEL)
        except gr.Error as error:
            return fail(error, stale=True)
        timeline_id = owner._serve_str(payload, "timelineId") or None
        try:
            if kind == "save":
                return self._save(root, timeline_id, payload)
            if kind == "render":
                return self._render(root, timeline_id, payload)
            if kind == "add":
                return self._add(root, timeline_id, payload)
            if kind == "job":
                return self._job(root, payload)
            if kind == "wave":
                source_id = owner._serve_str(payload, "sourceId")
                wave = owner.raw.timeline_waveform(root, source_id)
                return {"ok": True, "sourceId": source_id, "wave": wave}
            if kind == "english":
                pairs = owner.raw.timeline_english(root, timeline_id or "")
                return {"ok": True, "timelineId": timeline_id, "pairs": pairs}
            if kind == "tiers":
                source_id = owner._serve_str(payload, "sourceId")
                tiers = owner.raw.timeline_tiers(root, [source_id])
                return {"ok": True, "sourceId": source_id, "tiers": tiers[source_id]}
            media = owner.raw.timeline_media(root, timeline_id)
            if kind == "media":
                return {"ok": True, "media": media["sources"]}
            missing = tuple(
                source_id
                for source_id, row in media["sources"].items()
                if row["state"] not in (READY, "preparing")
            )
            if missing:
                owner.raw.start_source_preview(root, asset_ids=missing)
            return {"ok": True, "started": list(missing)}
        except (self.service_error, ValueError, TypeError, KeyError, OSError) as error:
            return fail(error)

    def _save(
        self, root: Any, timeline_id: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        """문서 전체를 저장한다 — 서버가 파일의 사실로 다시 검사하고, 리비전이 다르면 충돌이다."""
        base = payload.get("baseRevision")
        document = payload.get("doc")
        if timeline_id is None or not isinstance(base, int) or not isinstance(document, dict):
            raise ValueError("저장 요청의 모양이 틀렸습니다(타임라인 · 리비전 · 문서).")
        result = self.owner.raw.timeline_save(
            root, timeline_id, base, document, force=payload.get("force") is True
        )
        return {"ok": True, **result}

    def _add(
        self, root: Any, timeline_id: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        """클립이 없는 원본을 넣는다(W-71g) — 서버가 지금 자막을 심어 새 리비전으로 쓴다."""
        base = payload.get("baseRevision")
        document = payload.get("doc")
        source_id = self.owner._serve_str(payload, "sourceId")
        shape = isinstance(base, int) and not isinstance(base, bool) and isinstance(document, dict)
        if timeline_id is None or not shape or not source_id:
            raise ValueError("원본 넣기 요청의 모양이 틀렸습니다(타임라인 · 리비전 · 문서 · 원본).")
        result = self.owner.raw.timeline_add_source(root, timeline_id, base, document, source_id)
        return {"ok": True, **result}

    def _job(self, root: Any, payload: dict[str, object]) -> dict[str, object]:
        """재생기가 건 렌더 작업의 상태(W-71h) — 렌더 작업만 답한다. 읽기만 한다."""
        job = self.owner.raw.get_job(root, self.owner._serve_str(payload, "jobId"))
        if job.kind != self.render_job_kind:
            raise ValueError("렌더 작업이 아닙니다.")
        return {"ok": True, "status": job.status, "ended": job.status in JOB_ENDED}

    def _render(
        self, root: Any, timeline_id: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        """저장된 리비전 하나를 렌더하는 작업을 건다 — 작업이 그 리비전을 다시 확인한다(D130).

        「번인 캡션」을 고르면 같은 작업이 렌더 뒤 자막을 입힌다(「공유」, W-88 · D147).
        「없음」이면 예전 렌더 그대로다."""
        revision = payload.get("revision")
        if timeline_id is None or not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("렌더 요청의 모양이 틀렸습니다(타임라인 · 리비전).")
        burn = self.owner._serve_str(payload, "burn") or BURN_NONE
        if burn == BURN_NONE:
            handle = self.owner.raw.start_timeline_render(root, timeline_id, revision)
        else:
            handle = self.owner.raw.start_timeline_share(root, timeline_id, revision, burn)
        return {"ok": True, "jobId": handle.job_id}
