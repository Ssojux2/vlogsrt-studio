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
from ui.burn_cell import normalize_color
from ui.caption_grid_js import CAPTION_GRID_JS
from ui.caption_lane_js import CAPTION_LANE_JS
from ui.timeline_edit_js import TIMELINE_CORE_JS
from ui.timeline_player_js import PLAYER_APP_JS

PLAYER_BLOCK_ID = "vl-timeline-player"
PLAYER_ROOT_ID = "tl-root"
TAB_LABEL = "원본 범위 타임라인"
BURN_NONE = "none"
"""「번인 캡션: 없음」 — 예전 렌더 그대로(`timeline_share.BURN_NONE`과 같다, 시험이 대조한다)."""
BURN_ENGLISH = "ko-en"
"""「한국어 + 영어」 — 번역이 없으면 최종출력이 Gemini로 옮긴다(W-108, D182)."""
REFUSAL_LOGGED = {"render": "최종출력", "translate": "영어 만들기"}
"""거절을 서버 로그(활동 띠 · 「기록」)에도 남기는 요청 — 화면 상태 줄 하나로는 놓치기 쉽다."""
ANSWERED_ERRORS = (ValueError, TypeError, KeyError, OSError)
"""서버 함수가 500 대신 화면 글로 돌려주는 그 밖의 예외."""
JOB_ENDED = frozenset({"succeeded", "failed", "cancelled"})
"""끝난 작업 상태 — 렌더 작업을 묻는 `tl_job`이 끝을 알린다."""
PLAYER_JS = (
    TIMELINE_CORE_JS + CAPTION_GRID_JS + CAPTION_LANE_JS + keymap.keymap_js() + PLAYER_APP_JS
)
READY = "ready"


def _rational(row: dict[str, Any]) -> tuple[int, int]:
    return int(row["num"]), int(row["den"])


STYLE_KEYS = (
    "position", "font_px", "outline_px", "margin_px", "text_color", "x_permille", "y_permille",
)  # fmt: skip
"""자막 모양의 칸 — 뒤 둘은 화면에서 끌어 옮긴 자리(W-107, D181, 없으면 None)."""


def style_payload(style: Any) -> dict[str, Any]:
    """모양 → 브라우저가 받는 사전.
    (화면 계층은 프로젝트 계층을 import하지 않는다 — 칸만 읽는다.)"""
    return {key: getattr(style, key) for key in STYLE_KEYS}


def burn_model(style: Any, settings: Any) -> dict[str, Any]:
    """자막 겹(W-98, D173)이 번인 모양을 근사하는 재료 — 저장한 모양과 번인 설정의 비율들.
    화면에서 고치는 손(W-107, D181)이 쓰는 범위·기본값·자리 단위도 싣는다."""
    return {
        "style": style_payload(style),
        "referenceHeight": settings.reference_height,
        "englishScale": float(settings.english_scale),
        "sideMargin": float(settings.side_margin_ratio),
        "outlineColor": settings.outline_color,
        "positionScale": settings.position_scale,
        "ranges": {
            "font_px": list(settings.font_px_range),
            "outline_px": list(settings.outline_px_range),
            "margin_px": list(settings.margin_px_range),
        },
        "defaults": {
            "position": settings.default_position,
            "font_px": settings.default_font_px,
            "outline_px": settings.default_outline_px,
            "margin_px": settings.default_margin_px,
            "text_color": settings.default_text_color.upper(),
            "x_permille": None,
            "y_permille": None,
        },
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
        '<div id="tl-overlay" class="tl-overlay"></div>'
        + platform_keys.display(_style_tools_html())
        + "</div></div>"
        + platform_keys.display(
            '<div class="tl-transport">'
            '<button type="button" data-tl="prev-cut" title="이전 컷 (↑)">⏮</button>'
            '<button type="button" data-tl="toggle" id="tl-toggle"'
            ' title="재생 · 멈춤 (Space)">▶</button>'
            '<button type="button" data-tl="next-cut" title="다음 컷 (↓)">⏭</button>'
            '<span id="tl-timecode" class="tl-timecode">00:00:00:00</span>'
            # 저장 표지(디자인 점검 U8) — 폭 고정, 글은 실행본이 S.dirty · S.saving으로 단다.
            '<span id="tl-save" class="tl-save is-saved" hidden>✓ 저장됨</span>'
            '<span id="tl-source" class="tl-source"></span>'
            '<span class="tl-gap"></span>'
            # 도구(W-103, D176 — Final Cut 「선택」 A · 「블레이드」 B): 스토리라인 누름의 뜻.
            '<span class="tl-tools" role="group" aria-label="스토리라인 도구">'
            '<button type="button" data-tl="tool-select" id="tl-tool-select" aria-pressed="true"'
            ' title="선택 도구 — 클립을 고르고 끝을 끌어 길이를 고친다 (A)">↖ 선택</button>'
            '<button type="button" data-tl="tool-blade" id="tl-tool-blade" aria-pressed="false"'
            ' title="자르기 도구 — 스토리라인에서 누른 자리로 클립을 자른다 (B)">✂ 자르기</button>'
            "</span>"
            '<button type="button" data-tl="blade"'
            ' title="플레이헤드에서 클립 자르기 — Final Cut 「블레이드」 (⌘B)">'
            "플레이헤드 자르기</button>"
            '<button type="button" data-tl="delete-clip"'
            ' title="고른 클립 지우기 — 뒤가 당겨 온다 (⌫)">삭제</button>'
            '<button type="button" data-tl="join"'
            ' title="고른 클립을 맞닿은 다음 조각과 합치기">합치기</button>'
            '<button type="button" data-tl="undo" id="tl-undo"'
            ' title="실행 취소 (⌘Z)">실행 취소</button>'
            '<button type="button" data-tl="redo" id="tl-redo"'
            ' title="다시 실행 (⌘⇧Z)">다시 실행</button>'
            '<button type="button" data-tl="force-save" id="tl-force" hidden>'
            "내 편집으로 덮기</button>"
            '<button type="button" data-tl="prepare" id="tl-prepare" hidden>미리보기 준비</button>'
            # 보기(W-103, D176 — Final Cut 타임라인 오른쪽 위): 스키밍 · 스내핑 · 확대 막대.
            '<span class="tl-view" role="group" aria-label="타임라인 보기">'
            '<button type="button" data-tl="skimming" id="tl-skim" aria-pressed="true"'
            ' title="스키밍 — 타임라인에 올린 자리를 뷰어에 보인다 (S)">스키밍</button>'
            '<button type="button" data-tl="snapping" id="tl-snap" aria-pressed="true"'
            ' title="스내핑 — 끄는 끝이 컷 · 플레이헤드에 붙는다 (N)">스내핑</button>'
            # 가운데 고정 재생(D178) — 플레이헤드는 가운데, 타임라인이 흐른다.
            '<button type="button" data-tl="center-play" id="tl-center" aria-pressed="false"'
            ' title="가운데 고정 재생 — 재생하면 플레이헤드가 가운데에 서고 타임라인이 흐른다">'
            "가운데 고정</button>"
            '<input type="range" id="tl-zoom" min="0" max="1000" step="1"'
            ' aria-label="타임라인 확대" title="타임라인 확대 (⌘= · ⌘− · ⇧Z 맞춤)"></span>'
            # 편집 단추 묶음과 출력 묶음 사이 가름(디자인 점검 U9).
            '<span class="tl-divider" aria-hidden="true"></span>'
            # 「최종출력」(W-88 · D147 · 이름 D181) — Final Cut 공유 패널 「번인 캡션:」을 옮겼다.
            '<label class="tl-burn"'
            ' title="최종출력할 때 영상에 입힐 자막(Final Cut 「번인 캡션」)">'
            '영상에 넣을 자막 <select id="tl-burn" aria-label="영상에 넣을 자막">'
            '<option value="none">없음 — 렌더만</option>'
            '<option value="ko">한국어</option>'
            '<option value="ko-en"'
            ' title="영어 번역이 없으면 최종출력할 때 Gemini로 옮깁니다(유료)">'
            "한국어 + 영어</option></select></label>"
            '<button type="button" data-tl="render" id="tl-render" class="tl-primary"'
            ' title="저장한 타임라인을 원본에서 한 번 렌더하고, 고르면 자막을 입힌다(무료 로컬)">'
            "최종출력</button>"
            '<button type="button" data-tl="index" id="tl-index-toggle" aria-pressed="false"'
            ' title="타임라인 목록 — 클립 · 자막 목록, Final Cut 「인덱스」 (⇧⌘2)">목록</button>'
            '<span id="tl-status" class="tl-status" aria-live="polite"></span></div>'
            # 타임라인 인덱스(W-86c, D150) — Final Cut 「인덱스」: 클립 · 자막(「오류 보기」) 목록.
            '<div id="tl-index" class="tl-index" hidden><div class="tl-index-head">'
            '<button type="button" data-tl="index-clips" aria-pressed="true">클립</button>'
            '<button type="button" data-tl="index-cues" aria-pressed="false">자막</button>'
            '<label class="tl-index-errors"><input type="checkbox" id="tl-index-errors">'
            " 오류 보기</label></div>"
            '<div id="tl-index-list" class="tl-index-list" role="listbox"'
            ' aria-label="타임라인 인덱스 — 누르면 그 자리로 간다"></div></div>'
            # 전체 보기 띠(W-103, D176) — 타임라인 전체 · 아래 스토리라인이 보이는 자리(창).
            '<div class="tl-strip"><canvas id="tl-canvas" aria-label="타임라인 전체 보기 —'
            ' 누르거나 끌면 그 자리로 가고 아래 스토리라인이 따라간다"></canvas></div>'
            '<div class="tl-range"><div class="tl-range-head"><span id="tl-source-head"></span>'
            '<span class="tl-gap"></span>'
            '<button type="button" data-tl="trim-start"'
            ' title="클립 앞을 커서로 (⌥[)">앞 ⌥[</button>'
            '<button type="button" data-tl="trim-end" title="클립 뒤를 커서로 (⌥])">뒤 ⌥]</button>'
            "</div>"
            '<div class="tl-source-bar"><canvas id="tl-source-canvas"'
            ' aria-label="고른 클립의 원본 범위 — 손잡이를 끌어 앞뒤를 조정"></canvas></div></div>'
        )
        # 단축키 시트는 머리줄 「단축키」 판 하나다(W-102, D175).
        + f"{_cue_panel_html()}</div>"
    )


def _style_tools_html() -> str:
    """화면 자막 모양(W-107, D181) — 인스타그램·캡컷처럼 뷰어 위에서 자막을 끌어 옮기고
    모서리로 키운다.

    자막 겹은 매번 다시 그려지므로 손잡이는 그 형제다. 가운데 안내선 둘 · 덩어리 틀(모서리 넷) ·
    도구 막대 · 여는 단추. 모양은 전체 자막 하나다(큐마다가 아니다)."""
    grips = "".join(
        f'<span class="tl-style-grip" data-grip="{corner}" aria-hidden="true"></span>'
        for corner in ("nw", "ne", "sw", "se")
    )
    return (
        '<div class="tl-guide tl-guide-v" hidden></div>'
        '<div class="tl-guide tl-guide-h" hidden></div>'
        '<div id="tl-style-box" class="tl-style-box" hidden'
        ' title="끌어 옮기기 (⇧ 한 축만 · ⌘ 붙지 않게) — 모서리를 끌면 크기 (⇧ 가운데 기준)">'
        f"{grips}</div>"
        '<div id="tl-style-bar" class="tl-style-bar" hidden role="toolbar" aria-label="자막 모양">'
        '<span class="tl-style-name">전체 자막</span>'
        '<button type="button" data-style="font-down" title="글자 작게">A−</button>'
        '<output id="tl-style-font" title="글자 크기 (1080 기준 px)"></output>'
        '<button type="button" data-style="font-up" title="글자 크게">A+</button>'
        '<span class="tl-style-sep" aria-hidden="true"></span>'
        '<button type="button" data-style="outline-down" title="외곽선 얇게">◯−</button>'
        '<output id="tl-style-outline" title="외곽선 (px)"></output>'
        '<button type="button" data-style="outline-up" title="외곽선 두껍게">◯+</button>'
        '<label class="tl-style-color" title="글자색">'
        '<input type="color" id="tl-style-color" aria-label="글자색"></label>'
        '<span class="tl-style-sep" aria-hidden="true"></span>'
        '<button type="button" data-style="center-x" title="가로 가운데로">↔ 가운데</button>'
        '<button type="button" data-style="anchor"'
        ' title="위·아래 기본 자리로 (끌어 옮긴 자리를 버린다)">'
        "기본 자리</button>"
        '<button type="button" data-style="reset" title="모양 전체를 기본값으로">초기화</button>'
        '<button type="button" data-style="done" class="tl-primary"'
        ' title="닫기 (Esc)">완료</button>'
        "</div>"
        '<button type="button" id="tl-style-toggle" class="tl-style-toggle" aria-pressed="false"'
        ' title="자막 모양 — 화면에서 자막을 끌어 옮기고 크기·색을 바꾼다(전체 자막)">'
        "Aa 자막 모양</button>"
    )


def _cue_panel_html() -> str:
    """자막 칸(W-71i) — 보이는 자막 목록과 고른 자막의 편집 칸. 내용은 실행본이 그린다."""
    return platform_keys.display(
        '<div class="tl-cues"><div class="tl-cues-head"><strong>자막</strong>'
        '<span id="tl-cue-count" class="tl-cue-count"></span><span class="tl-gap"></span>'
        '<button type="button" data-tl="cue-insert"'
        ' title="플레이헤드에 빈 자막 넣기 (⌥C)">새 자막 ⌥C</button>'
        # 번역 모드(W-87d, D153) — Subtitle Edit 「번역 모드 전환」: 영어 열을 붙인다(읽기만).
        # 「영어 만들기」(W-99, D174) — 저장한 리비전의 자막을 Gemini로 옮긴다(유료 · 누름이 승인).
        '<button type="button" data-tl="translate" id="tl-translate"'
        ' title="저장한 자막을 영어로 옮긴다(Gemini · 유료 · 누름이 승인) —'
        ' 같은 글은 다시 사지 않고, 한국어를 고친 뒤 다시 누르면 지금 글을 옮긴다">'
        "영어 만들기</button>"
        '<button type="button" data-tl="translation" id="tl-translation" aria-pressed="false"'
        ' title="번역 모드 전환 — 렌더한 초안의 저장된 영어를 한국어 글로 찾아 보인다'
        '(사지 않는다)">번역 모드</button></div>'
        '<canvas id="tl-lane" class="tl-lane" tabindex="-1"'
        ' aria-label="스토리라인 · 파형 자막 레인 — 위쪽 클립 카드(이름 · 장면)를 눌러 고르고 끝을'
        " 끌어 길이를 고치고(자르기 도구면 누른 자리에서 자르고), 자막 블록을 끌어 시각을 고치고,"
        ' 빈 곳을 끌어 새 선택(Enter로 넣기)">'
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
/* 「타임라인으로 보내기」 요약(U1) — 한 줄 · 빠진 원본 목록만 줄을 더한다(안내 글은 title). */
.vl-timeline-summary { font-size: 12px; line-height: 18px; color: var(--vl-ink-muted); }
.vl-timeline-summary strong { color: var(--vl-ink); }
/* 빠진 것의 짧은 표(D179) — 이름은 마우스를 올리면. 한 줄을 넘지 않는다. */
.vl-timeline-summary { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.vl-timeline-note { display: inline-block; margin-left: 6px; padding: 0 6px; border-radius: 3px;
  background: var(--vl-warn-soft); color: var(--vl-warn); cursor: help; }
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
.tl-overlay.is-styled > div { paint-order: stroke fill; overflow-wrap: anywhere;
  pointer-events: auto; cursor: move; touch-action: none; }
/* 화면 자막 모양(W-107, D181) — 인스타그램·캡컷처럼 뷰어 위에서 자막을 끌고 모서리로 키운다. */
.tl-overlay.is-styled.is-point { justify-content: center; transform: translateY(-50%); }
.tl-style-box { position: absolute; z-index: 3; box-sizing: border-box; cursor: move;
  touch-action: none; border: 1px dashed var(--vl-on-video-overlay);
  outline: 1px solid var(--vl-video-well); }
.tl-style-box[hidden], .tl-style-bar[hidden], .tl-guide[hidden],
.tl-style-toggle[hidden] { display: none; }
.tl-style-grip { position: absolute; width: 10px; height: 10px; border-radius: 50%;
  background: var(--vl-on-video-overlay); border: 1px solid var(--vl-video-well); }
.tl-style-grip[data-grip="nw"] { left: -6px; top: -6px; cursor: nwse-resize; }
.tl-style-grip[data-grip="ne"] { right: -6px; top: -6px; cursor: nesw-resize; }
.tl-style-grip[data-grip="sw"] { left: -6px; bottom: -6px; cursor: nesw-resize; }
.tl-style-grip[data-grip="se"] { right: -6px; bottom: -6px; cursor: nwse-resize; }
.tl-guide { position: absolute; z-index: 2; pointer-events: none;
  background: var(--vl-snap-guide); }
.tl-guide-v { top: 0; bottom: 0; left: 50%; width: 1px; }
.tl-guide-h { left: 0; right: 0; top: 50%; height: 1px; }
.tl-style-bar { position: absolute; z-index: 4; top: 8px; left: 50%; transform: translateX(-50%);
  display: flex; flex-wrap: wrap; justify-content: center; align-items: center; gap: 4px;
  max-width: calc(100% - 16px); box-sizing: border-box; padding: 4px 6px; border-radius: 6px;
  font-size: 12px; background: var(--vl-video-overlay); color: var(--vl-on-video-overlay); }
.tl-style-bar.is-bottom { top: auto; bottom: 8px; }
.tl-style-bar button { font: inherit; font-size: 12px; height: 24px; padding: 0 6px;
  border-radius: 4px; cursor: pointer; border: 1px solid var(--vl-line-strong);
  background: transparent; color: inherit; }
.tl-style-bar button.tl-primary { background: var(--vl-accent); border-color: var(--vl-accent);
  color: var(--vl-on-accent); font-weight: 600; }
.tl-style-bar output { min-width: 2.2em; text-align: center; font-family: var(--vl-font-mono); }
.tl-style-name { font-weight: 600; margin-right: 2px; }
.tl-style-sep { align-self: stretch; width: 1px; background: var(--vl-line-strong); }
.tl-style-color { display: inline-flex; }
.tl-style-color input { width: 26px; height: 22px; padding: 0; border: 0; background: none;
  cursor: pointer; }
.tl-style-toggle { position: absolute; z-index: 4; top: 8px; right: 8px; height: 24px;
  padding: 0 8px; border-radius: 4px; font-size: 12px; cursor: pointer; opacity: 0.7;
  border: 1px solid var(--vl-line-strong); background: var(--vl-video-overlay);
  color: var(--vl-on-video-overlay); transition: opacity 120ms; }
.tl-frame:hover .tl-style-toggle, .tl-style-toggle:focus-visible { opacity: 1; }
.tl-frame.is-styling .tl-style-toggle { display: none; }
.tl-transport { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
.tl-transport button { font: inherit; font-size: 12px; padding: 2px 8px; border-radius: 4px;
  cursor: pointer; border: 1px solid var(--vl-line-strong); background: var(--vl-surface-raised);
  color: var(--vl-ink); height: 24px; box-sizing: border-box; }
.tl-transport button[hidden] { display: none; }
/* 도구 묶음(W-103, D176) — 붙은 두 단추, 켠 도구는 accent 채움(Final Cut 도구 메뉴). */
.tl-tools, .tl-view { display: inline-flex; align-items: center; }
.tl-view { gap: 4px; }
.tl-view button[aria-pressed="true"] { border-color: var(--vl-accent-ink);
  color: var(--vl-accent-ink); background: var(--vl-accent-soft); }
.tl-view input[type="range"] { width: 96px; accent-color: var(--vl-accent); }
.tl-tools button { border-radius: 0; }
.tl-tools button:first-child { border-radius: 4px 0 0 4px; }
.tl-tools button:last-child { border-radius: 0 4px 4px 0; border-left: 0; }
.tl-tools button[aria-pressed="true"] { background: var(--vl-accent-soft);
  border-color: var(--vl-accent-ink); color: var(--vl-accent-ink); font-weight: 600; }
.tl-tools button:last-child[aria-pressed="true"] { border-left: 1px solid var(--vl-accent-ink); }
.tl-timecode { font-family: var(--vl-font-mono); font-size: 14px; }
/* 저장 표지(디자인 점검 U8) — 폭 고정이라 글이 바뀌어도 옆 칸이 움직이지 않는다. */
.tl-save { display: inline-block; width: 7.5em; white-space: nowrap; font-size: 12px; }
.tl-save[hidden] { display: none; }
.tl-save.is-dirty { color: var(--vl-tier-yellow-ink); }
.tl-save.is-saving { color: var(--vl-ink-muted); }
.tl-save.is-saved { color: var(--vl-tier-green-ink); }
/* 편집 단추 묶음 | 출력 묶음(디자인 점검 U9) — 「최종출력」이 이 줄의 주 동작이다. */
.tl-divider { align-self: stretch; width: 1px; margin: 2px 2px; background: var(--vl-line-strong); }
.tl-transport button.tl-primary { background: var(--vl-accent); border-color: var(--vl-accent);
  color: var(--vl-on-accent); font-weight: 600; }
.tl-transport button.tl-primary:hover:not(:disabled) { background: var(--vl-accent-hover); }
.tl-source { color: var(--vl-ink-muted); }
.tl-gap { flex: 1 1 auto; }
.tl-status { color: var(--vl-ink-muted); flex: 1 0 100%; min-height: 1.4em; white-space: nowrap;
  overflow: hidden; text-overflow: ellipsis; }
.tl-status.is-error { color: var(--vl-danger); }
/* 전체 보기 띠(W-103, D176) — 읽는 곳은 아래 스토리라인이라 얇다(예전 56px 클립 띠). */
.tl-strip { position: relative; height: 22px; border: 1px solid var(--vl-line);
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
.tl-transport button:disabled { opacity: 1 !important; filter: none !important; cursor: default;
  color: var(--vl-ink-faint); border-color: var(--vl-line); background: transparent; }
.tl-strip canvas { position: absolute; inset: 0; width: 100%; height: 100%; cursor: pointer;
  touch-action: none; }
.tl-burn { display: inline-flex; gap: 4px; align-items: center; font-size: 12px;
  color: var(--vl-ink-muted); }
.tl-burn select { font: inherit; font-size: 12px; height: 24px; box-sizing: border-box;
  padding: 0 4px; }
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
.tl-cues button:disabled { opacity: 1 !important; filter: none !important; cursor: default;
  color: var(--vl-ink-faint); border-color: var(--vl-line); background: transparent; }
/* 키를 받을 창(W-89, D154) — 마지막으로 누른 창(⌫ 등 뜻이 창마다 다르다). 옅은 1px 고리는
   1.2:1로 안 보였다(디자인 점검 U6) — 2px accent-ink 고리와 머리의 「키 입력」 표지다.
   맞춤(is-fit)에서 `.tl-cues`는 `display: contents`라 그림자가 없으므로 상자가 있는
   자막 머리 · 레인 · 목록에 단다. */
.tl-root[data-tl-pane="captions"] .tl-cues-head,
.tl-root[data-tl-pane="captions"] .tl-lane,
.tl-root[data-tl-pane="captions"] .tl-cue-list,
.tl-root[data-tl-pane="timeline"] .tl-strip,
.tl-root[data-tl-pane="timeline"] .tl-lane { box-shadow: 0 0 0 2px var(--vl-accent-ink); }
.tl-root[data-tl-pane="captions"] .tl-cues-head { border-radius: 4px; }
.tl-root[data-tl-pane="captions"] .tl-cues-head::before,
.tl-root[data-tl-pane="timeline"] .tl-strip::before { content: "키 입력"; font-size: 11px;
  line-height: 14px; padding: 0 5px; border-radius: 3px; white-space: nowrap;
  color: var(--vl-accent-ink); background: var(--vl-accent-soft); }
.tl-root[data-tl-pane="timeline"] .tl-strip::before { position: absolute; right: 4px; bottom: 2px;
  z-index: 1; pointer-events: none; }
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
/* 스토리라인(이름 줄 26 + 필름 띠 40) · 눈금 · 파형 · 자막 — 필름 띠만큼 예전 124px에서 늘었다. */
.tl-lane { display: block; width: 100%; height: 164px; border-radius: 6px;
  touch-action: none; }
.tl-cues-body { display: flex; gap: 8px; align-items: stretch; }
.tl-cue-list { position: relative; flex: 1 1 60%; min-width: 0; max-height: 260px;
  overflow-y: auto;
  border: 1px solid var(--vl-line); border-radius: 6px; background: var(--vl-surface-sunken); }
/* 한 화면 맞춤(W-102, D175) — 실행본이 덱 남은 높이를 재 `is-fit`과 높이를 단다. 왼쪽은
   이동 막대 · 클립 띠 · 원본 띠 · 파형 레인(남은 높이), 오른쪽은 자막 머리 · 자막 목록 ·
   편집 칸이다. 자막 머리는 이동 막대 옆 오른쪽 열에 서서 왼쪽 열에 줄을 하나 덜 쓴다
   (디자인 점검 U1 — 14인치 1512×900에서 레인이 최소 높이를 못 받아 맞춤이 걸리지 않았다).
   덱은 스크롤하지 않고 자막 목록 하나만 스크롤한다. 인덱스를 열었거나 목록이 덱 밖
   (「자막」 작업공간)이면 달지 않는다. */
@media (min-width: 1051px) {
  .tl-root.is-fit { display: grid; gap: 6px 10px; margin-bottom: 0;
    grid-template-columns: minmax(0, 1fr) minmax(320px, 38%);
    grid-template-rows: auto auto auto minmax(0, 1fr);
    grid-template-areas: "transport head" "strip body" "range body" "lane body"; }
  .tl-root.is-fit > .tl-transport { grid-area: transport; }
  .tl-root.is-fit > .tl-strip { grid-area: strip; }
  .tl-root.is-fit > .tl-range { grid-area: range; }
  .tl-root.is-fit > .tl-cues { display: contents; }
  .tl-root.is-fit .tl-cues-head { grid-area: head; align-self: end; flex-wrap: wrap; }
  .tl-root.is-fit .tl-lane { grid-area: lane; height: 100%; min-height: 0; }
  .tl-root.is-fit .tl-cues-body { grid-area: body; flex-direction: column; min-height: 0; }
  .tl-root.is-fit .tl-cue-list { flex: 1 1 auto; max-height: none; min-height: 0; }
  .tl-root.is-fit .tl-cue-edit { flex: none; }
  /* 자막 칸이 재생기 밖이면(D178) 한 열 — 타임라인이 덱 폭 전체를 쓴다. */
  .tl-root.is-fit.is-solo { grid-template-columns: minmax(0, 1fr);
    grid-template-areas: "transport" "strip" "range" "lane"; }
}
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
/* 재생 중인 줄과 고른 줄을 가른다(디자인 점검 U6) — 재생 중은 hover 바탕 + ▶(줄 밖 흐름에
   두어 격자 칸을 밀지 않는다), 고른 줄은 accent-soft 바탕 + 3px accent 막대 + ink 글자.
   둘은 함께 설 수 있다. */
.tl-cue-row { position: relative; }
.tl-cue-row.is-current { background: var(--vl-surface-hover); }
.tl-cue-row.is-current::before { content: "▶"; position: absolute; left: 4px; top: 50%;
  transform: translateY(-50%); font-size: 9px; line-height: 1; color: var(--vl-accent-ink);
  pointer-events: none; }
.tl-cue-row.is-selected { background: var(--vl-accent-soft); color: var(--vl-ink);
  box-shadow: inset 3px 0 0 var(--vl-accent); }
.tl-cue-row.is-selected .tl-cue-time, .tl-cue-row.is-selected .tl-cue-n { color: var(--vl-ink); }
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

    def __init__(
        self,
        owner: Any,
        service_error: type[Exception],
        render_job_kind: str,
        translate_job_kind: str | None = None,
    ) -> None:
        self.owner = owner
        self.service_error = service_error
        self.render_job_kind = render_job_kind
        # 「영어 만들기」(W-99, D174)가 거는 번역 작업 — 재생기가 그 끝을 묻는다.
        self.translate_job_kind = translate_job_kind
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

        def tl_translate(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "translate")

        def tl_style(payload: dict[str, object]) -> dict[str, object]:
            return self.action(payload, "style")

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
            tl_translate,
            tl_style,
        ]

    def action(self, payload: dict[str, object], kind: str) -> dict[str, object]:
        """어떤 예외도 500이 아니라 값으로 돌아간다. 준비·저장·렌더는 서명·수명 토큰에 묶인다."""
        owner = self.owner

        def fail(error: object, *, stale: bool = False) -> dict[str, object]:
            message = owner._serve_error(error)
            if kind in REFUSAL_LOGGED:
                owner._serve_warn(f"{REFUSAL_LOGGED[kind]}을 걸지 못했다: {message}")
            return {"ok": False, "error": message, "stale": stale}

        try:
            root = owner._require_current_selection(owner._serve_str(payload, "signature") or None)
            if kind in ("prepare", "save", "render", "add", "translate", "style"):
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
            if kind == "translate":
                return self._translate(root, timeline_id, payload)
            if kind == "style":
                return self._style(root, payload)
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
        except (self.service_error, gr.Error, RuntimeError, *ANSWERED_ERRORS) as error:
            # gr.Error — 「영어 만들기」의 연결 없음(키 없음) 같은 화면 글도 값으로 돌려준다.
            # RuntimeError — 연결의 승인 오류(`AnalysisApprovalError`)도 화면 글이다: 예전에는
            # 서버 함수 밖으로 새어 단추가 아무 말 없이 멈췄다.
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
        """클립이 없는 원본을 넣는다(W-71g) — 서버가 지금 자막을 심어 새 리비전으로 쓴다.
        `at`(클립 차례 — 영상 목록에서 끌어 놓은 자리, W-111 · D185)이 있으면 그 자리다."""
        base = payload.get("baseRevision")
        document = payload.get("doc")
        source_id = self.owner._serve_str(payload, "sourceId")
        shape = isinstance(base, int) and not isinstance(base, bool) and isinstance(document, dict)
        if timeline_id is None or not shape or not source_id:
            raise ValueError("원본 넣기 요청의 모양이 틀렸습니다(타임라인 · 리비전 · 문서 · 원본).")
        at = payload.get("at")
        if at is not None and (not isinstance(at, int) or isinstance(at, bool)):
            raise ValueError("원본 넣기 자리가 정수가 아닙니다.")
        result = self.owner.raw.timeline_add_source(
            root, timeline_id, base, document, source_id, at=at
        )
        return {"ok": True, **result}

    def _job(self, root: Any, payload: dict[str, object]) -> dict[str, object]:
        """재생기가 건 렌더 작업의 상태(W-71h) — 렌더 작업만 답한다. 읽기만 한다."""
        job = self.owner.raw.get_job(root, self.owner._serve_str(payload, "jobId"))
        if job.kind not in (self.render_job_kind, self.translate_job_kind):
            raise ValueError("재생기가 건 작업(렌더 · 영어 만들기)이 아닙니다.")
        return {"ok": True, "status": job.status, "ended": job.status in JOB_ENDED}

    def _style(self, root: Any, payload: dict[str, object]) -> dict[str, object]:
        """화면에서 고친 자막 모양을 저장한다(W-107, D181) — 공유 탭 손잡이와 같은 검사·같은 자리.
        범위 밖이면 저장하지 않고 까닭을 돌려준다. 저장한 모양을 돌려준다."""
        given = payload.get("style")
        if not isinstance(given, dict):
            raise ValueError("자막 모양 요청의 모양이 틀렸습니다.")
        mapping = {key: given.get(key) for key in STYLE_KEYS}
        mapping["text_color"] = normalize_color(str(mapping["text_color"] or ""))
        saved = self.owner.raw.save_subtitle_burn_style(root, mapping)
        return {"ok": True, "style": style_payload(saved)}

    def _translate(
        self, root: Any, timeline_id: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        """저장한 리비전의 자막을 영어로 옮기는 작업을 건다(W-99, D174) — 누름이 승인이다(D110).

        리비전의 SRT를 타임라인 폴더에 쓰고 「영어 SRT로 변환」과 같은 작업에 넘긴다. 같은 글을 이미
        옮겼으면 저장된 번역을 쓰고(무료) 연결을 묻지 않는다."""
        revision = payload.get("revision")
        if timeline_id is None or not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("영어 만들기 요청의 모양이 틀렸습니다(타임라인 · 리비전).")
        raw = self.owner.raw
        srt = raw.timeline_korean_srt(root, timeline_id, revision)
        client = None
        if not raw.srt_translation_cached(root, srt):
            client = self.owner._live_transport_for_action().semantic_client(
                raw.translation_model()
            )
        handle = raw.start_srt_translation(root, srt, client)
        return {"ok": True, "jobId": handle.job_id}

    def _render(
        self, root: Any, timeline_id: str | None, payload: dict[str, object]
    ) -> dict[str, object]:
        """저장된 리비전 하나를 렌더하는 작업을 건다 — 작업이 그 리비전을 다시 확인한다(D130).

        「번인 캡션」을 고르면 같은 작업이 렌더 뒤 자막을 입힌다(「최종출력」, W-88 · D147).
        「없음」이면 예전 렌더 그대로다."""
        revision = payload.get("revision")
        if timeline_id is None or not isinstance(revision, int) or isinstance(revision, bool):
            raise ValueError("렌더 요청의 모양이 틀렸습니다(타임라인 · 리비전).")
        burn = self.owner._serve_str(payload, "burn") or BURN_NONE
        if burn == BURN_NONE:
            handle = self.owner.raw.start_timeline_render(root, timeline_id, revision)
            return {"ok": True, "jobId": handle.job_id}
        client = self._translator(root, timeline_id, revision) if burn == BURN_ENGLISH else None
        handle = self.owner.raw.start_timeline_share(root, timeline_id, revision, burn, client)
        return {"ok": True, "jobId": handle.job_id, "translating": client is not None}

    def _translator(self, root: Any, timeline_id: str, revision: int) -> object | None:
        """「한국어 + 영어」 최종출력(W-108, D182) — 그 리비전 글의 번역이 없으면 Gemini 연결
        (누름이 승인 · 유료). 이미 옮겼으면 None(무료). 키가 없으면 화면 글(`gr.Error`)이다."""
        raw = self.owner.raw
        if raw.srt_translation_cached(root, raw.timeline_korean_srt(root, timeline_id, revision)):
            return None
        return self.owner._live_transport_for_action().semantic_client(raw.translation_model())
