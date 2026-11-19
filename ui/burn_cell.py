"""「자막 입히기」 칸(W-65, D116) — 영상 하나에 한국어(+ 영어) SRT를 Pretendard로 번인한다.

결과 카드 아래 「영어 SRT로 변환」 칸 다음에 선다. 사람이 고르는 것은 자리·글자 크기·외곽선·글자색
(+ 가장자리 여백)뿐이고 애니메이션·효과는 없다 — 영화 자막처럼 한글 줄 아래에 영어 줄(4/5 크기)이다.
세 경로 칸은 비우면 마지막 초안(`draft.mp4` · 가장 새 SRT · 그 옆의 `<이름>.en.srt`)이다 — 그 초안에
최종 편집을 저장했으면 그 컷과 편집본 SRT(`edit/final.srt`)이고, 영어는 지금 편집본 글을 옮긴
파일만이다(D118).
「미리보기」는 첫 한글 큐의 프레임 한 장을 내보낼 영상과 같은 필터로 그린다.
「자막 입혀 내보내기」는 무료 로컬 작업이고 끝날 때까지 기다려 결과 줄에 쓴 자리를 말한다.
화면 틀(`RawWorkspaceUi`)과는
`owner`의 작은 면(선택 확인 · 경로 고르기 · 활동 칸 · 서비스)으로만, 서비스와는
`RawWorkspaceService`의 자막 입히기 문으로만 닿는다. `vlogsrt.project`는 import하지 않는다 —
그 예외는 `ui/workspace_service.py` 하나라 서비스의 오류 종류도 만들 때 받는다.
"""

from __future__ import annotations

import base64
import html
import re
import time
from pathlib import Path
from typing import Any

import gradio as gr

from ui import chooser as ui_chooser

BURN_ID = "vl-burn"
BURN_JOB_LABEL = "자막 입히기"
TITLE_TEXT = (
    "**자막 입히기** — 영상에 한국어·영어 자막을 Pretendard로 새겨 `<이름>.burned.mp4`를"
    " 만듭니다(원본은 그대로)."
)
VIDEO_LABEL = "영상"
VIDEO_INFO = (
    "비워 두면 마지막 초안의 draft.mp4입니다 — 최종 편집을 저장했으면 그 컷대로 잘라 이어 입힙니다."
)
KOREAN_LABEL = "한국어 SRT"
KOREAN_INFO = (
    "비워 두면 마지막 초안의 SRT입니다 — 최종 편집을 저장했으면 편집본 SRT(`edit/final.srt`)입니다."
)
ENGLISH_LABEL = "영어 SRT"
ENGLISH_INFO = (
    "비워 두면 한국어 SRT 옆의 `<이름>.en.srt`(없으면 한글만)입니다 — 최종 편집이면 지금 편집본"
    " 글을 옮긴 것만 씁니다."
)
BROWSE_LABEL = "고르기"
POSITION_LABEL = "위치"
POSITION_CHOICES = [("아래", "bottom"), ("위", "top")]
FONT_LABEL = "글자 크기 (1080p 기준 px)"
OUTLINE_LABEL = "외곽선 (px)"
MARGIN_LABEL = "가장자리 여백 (px)"
COLOR_LABEL = "글자색"
PREVIEW_LABEL = "미리보기"
EXPORT_LABEL = "자막 입혀 내보내기"
PREVIEW_TEXT = "미리보기를 그렸습니다 — 내보낼 영상과 같은 글꼴·필터입니다."
EDIT_TEXT = "최종 편집(리비전 {revision} · 클립 {clips}개{hidden})의 컷과 함께 입힙니다."
LEFT_OUT_TEXT = (
    "영어 SRT {names}는 지금 편집본 글을 옮긴 것이 아니라 뺐습니다 — 「영어 SRT로 변환」으로 다시"
    " 옮기거나(글이 같으면 저장된 번역이라 무료) 영어 칸에서 직접 고르세요."
)
DONE_TEXT = "자막을 입힌 영상을 냈습니다 — `{path}` · 자막 {events}개{extra}."
JOINED_TEXT = "같은 영상에 자막을 입히던 작업에 붙었고 그 작업이 끝났습니다 — `{name}` 옆을 보세요."
CANCELLED_TEXT = "자막 입히기를 취소했습니다 — 쓴 파일은 없습니다."
POLL_SECONDS = 0.5
"""내보내기 단추가 작업 끝을 다시 보는 간격 — 단추는 작업이 끝날 때까지 기다려 결과를 말한다."""
JOB_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled"})
WAKE_JS = (
    "(...values) => { if (window.__vlServer) { window.__vlServer.wake(true); } return values; }"
)
"""내보내기의 앞 단계 — 활동 띠를 바쁜 폴링으로 깨우고 입력을 그대로 넘긴다(D101)."""
STYLE_KEYS = ("position", "font_px", "outline_px", "margin_px", "text_color")
OVERLAY_STYLE_JS = """
(signature, position, font, outline, margin, color) => {
  const player = window.__vlTimelinePlayer;
  if (player && player.setBurnStyle) {
    player.setBurnStyle({ position: position, font_px: Number(font), outline_px: Number(outline),
      margin_px: Number(margin), text_color: color });
  }
  return [signature, position, font, outline, margin, color];
}
"""
"""손잡이를 바꾸면 저장하기 전에 재생기 자막 겹을 다시 그린다(W-98, D173) — 값은 그대로 넘긴다."""
_SHORT_HEX = re.compile(r"#([0-9A-Fa-f])([0-9A-Fa-f])([0-9A-Fa-f])")


def normalize_color(value: str | None) -> str:
    """색 고르기의 값 → `#RRGGBB`. 세 자리 16진 줄임꼴도 받는다(Gradio는 16진 글을 넘긴다)."""
    text = (value or "").strip()
    if short := _SHORT_HEX.fullmatch(text):
        return "#" + "".join(part * 2 for part in short.groups()).upper()
    return text.upper()


def _edit_text(edit: dict[str, Any] | None) -> str:
    """최종 편집으로 입힐 때 덧붙이는 줄 — 리비전·클립 수·가린 자막, 뺀 영어 SRT(D118)."""
    if not edit:
        return ""
    hidden = f" · 컷에 가려진 자막 {edit['hidden']}개" if edit.get("hidden") else ""
    text = " " + EDIT_TEXT.format(revision=edit["revision"], clips=edit["clips"], hidden=hidden)
    left_out = edit.get("english_left_out") or []
    if left_out:
        text += " " + LEFT_OUT_TEXT.format(names=", ".join(f"`{name}`" for name in left_out))
    return text


def _done_text(facts: dict[str, Any]) -> str:
    notes = []
    if not facts.get("english"):
        notes.append("한글만")
    if facts.get("empty"):
        notes.append(f"빈 큐 {facts['empty']}개 건너뜀")
    if facts.get("fallback"):
        notes.append("영상 옆에 쓸 수 없어 작업공간에 씀")
    if facts.get("hdr"):
        notes.append("HDR 원본을 SDR로")
    extra = f" ({' · '.join(notes)})" if notes else ""
    done = DONE_TEXT.format(path=facts["path"], events=facts["events"], extra=extra)
    return done + _edit_text(facts.get("edit"))


def _given(text: Any) -> Path | None:
    value = str(text or "").strip()
    return Path(value).expanduser() if value else None


class BurnCell:
    """칸 하나 — `build`는 화면 틀이 무대를 지을 때, `wire`는 단추를 이을 때 부른다."""

    def __init__(self, owner: Any, service_error: type[Exception]) -> None:
        self.owner = owner
        self.service_error = service_error

    # ── 짓기 ──────────────────────────────────────────────────────────

    def build(self) -> None:
        settings = self.owner.raw.subtitle_burn_settings()
        style = self.owner.raw.subtitle_burn_style()
        with gr.Group(elem_id=BURN_ID):
            gr.Markdown(TITLE_TEXT, elem_id=f"{BURN_ID}-title")
            self.video, self.video_browse = self._path_row(VIDEO_LABEL, VIDEO_INFO, "video")
            self.korean, self.korean_browse = self._path_row(KOREAN_LABEL, KOREAN_INFO, "korean")
            self.english, self.english_browse = self._path_row(
                ENGLISH_LABEL, ENGLISH_INFO, "english"
            )
            self.position = gr.Radio(
                POSITION_CHOICES,
                value=style.position,
                label=POSITION_LABEL,
                elem_id=f"{BURN_ID}-position",
            )
            self.font = self._slider(FONT_LABEL, settings.font_px_range, style.font_px, "font")
            self.outline = self._slider(
                OUTLINE_LABEL, settings.outline_px_range, style.outline_px, "outline"
            )
            self.margin = self._slider(
                MARGIN_LABEL, settings.margin_px_range, style.margin_px, "margin"
            )
            self.color = gr.ColorPicker(
                style.text_color, label=COLOR_LABEL, elem_id=f"{BURN_ID}-color"
            )
            with gr.Row():
                self.preview_button = gr.Button(
                    PREVIEW_LABEL, size="sm", elem_id=f"{BURN_ID}-preview-run"
                )
                self.export_button = gr.Button(EXPORT_LABEL, size="sm", elem_id=f"{BURN_ID}-run")
            self.preview_html = gr.HTML("", elem_id=f"{BURN_ID}-preview")
            self.result = gr.Markdown("", elem_id=f"{BURN_ID}-result")

    def _path_row(self, label: str, info: str, name: str) -> tuple[Any, Any]:
        box = gr.Textbox(value="", label=label, info=info, elem_id=f"{BURN_ID}-{name}")
        browse = gr.Button(
            f"{label} {BROWSE_LABEL}",
            size="sm",
            visible=self.owner._chooser_available,
            elem_id=f"{BURN_ID}-{name}-browse",
        )
        return box, browse

    def _slider(self, label: str, bounds: tuple[int, int], value: int, name: str) -> Any:
        low, high = bounds
        return gr.Slider(low, high, value=value, step=1, label=label, elem_id=f"{BURN_ID}-{name}")

    def style_inputs(self) -> list[Any]:
        return [self.position, self.font, self.outline, self.margin, self.color]

    def path_inputs(self) -> list[Any]:
        return [self.video, self.korean, self.english]

    # ── 잇기 ──────────────────────────────────────────────────────────

    def wire(self, status: Any) -> None:
        """고르기는 칸만 채우고, 미리보기는 그림을, 내보내기는 끝날 때까지 기다려 결과 줄을 쓴다."""
        for button, box, kind in (
            (self.video_browse, self.video, ui_chooser.VIDEO_KIND),
            (self.korean_browse, self.korean, ui_chooser.SRT_KIND),
            (self.english_browse, self.english, ui_chooser.SRT_KIND),
        ):
            button.click(
                lambda kind=kind: self.browse_view(kind),
                None,
                box,
                api_name=False,
                concurrency_limit=1,
                concurrency_id=ui_chooser.CHOOSER_CONCURRENCY_ID,
            )
        inputs = [self.owner._signature_state, *self.path_inputs(), *self.style_inputs()]
        previewed = self.preview_button.click(
            self.preview_view, inputs, [self.preview_html, status], concurrency_limit=1
        )
        self.owner._report_activity(previewed, status)
        exported = self.export_button.click(
            self.export_view, inputs, [self.result, status], concurrency_limit=1, js=WAKE_JS
        )
        self.owner._report_activity(exported, status)
        # 손잡이를 사람이 바꾸면 바로 저장하고 재생기 자막 겹에도 보인다(W-98, D173) — 공유의
        # 번인이 화면에 보인 모양을 쓴다. `input`은 사람의 손만 — 되살리기(`restore_view`)는 아니다.
        for knob in self.style_inputs():
            knob.input(
                self.save_view,
                [self.owner._signature_state, *self.style_inputs()],
                None,
                js=OVERLAY_STYLE_JS,
                show_progress="hidden",
                api_name=False,
            )

    def wire_restore(self, event: Any) -> None:
        """페이지를 열면 그 작업공간에 저장한 모양으로 손잡이를 맞춘다."""
        event.then(
            self.restore_view,
            self.owner._signature_state,
            self.style_inputs(),
            show_progress="hidden",
        )

    # ── 처리기 ────────────────────────────────────────────────────────

    def browse_view(self, kind: str) -> str:
        chosen = self.owner._pick_path(kind)
        if chosen is None:
            raise gr.Error("취소 — 파일을 고르지 않았습니다.")
        return str(chosen)

    def restore_view(self, signature: str | None) -> tuple[Any, ...]:
        root = self.owner._snapshot_root()
        if root is None:
            return tuple(gr.update() for _ in STYLE_KEYS)
        style = self.owner.raw.subtitle_burn_style(root)
        return (style.position, style.font_px, style.outline_px, style.margin_px, style.text_color)

    def save_view(self, signature: str | None, *values: Any) -> None:
        root = self.owner._snapshot_root()
        if root is None or not signature or signature != self.owner._signature_for(root):
            return
        try:
            self.owner.raw.save_subtitle_burn_style(root, self._mapping(values))
        except self.service_error as error:
            gr.Warning(f"자막 모양을 저장하지 못했습니다 — {error}")

    def preview_view(self, signature: str | None, *values: Any) -> tuple[str, str]:
        closed = self.owner._library_first_reply()
        if closed is not None:
            return "", closed
        root = self.owner._require_current_selection(signature)
        raw = self.owner.raw
        try:
            style = self._style(values[3:])
            job = raw.prepare_subtitle_burn(root, *(_given(text) for text in values[:3]))
            picture = raw.subtitle_burn_preview(job, style)
        except self.service_error as error:
            raise gr.Error(str(error)) from error
        encoded = base64.b64encode(picture).decode("ascii")
        alt = html.escape(f"{job.video.name} 자막 미리보기")
        image = (
            f'<img src="data:image/jpeg;base64,{encoded}" alt="{alt}"'
            ' style="width:100%;height:auto;display:block;border-radius:6px">'
        )
        edit = job.edit.facts() if job.edit is not None else None
        return image, PREVIEW_TEXT + _edit_text(edit)

    def export_view(self, signature: str | None, *values: Any) -> tuple[str, str]:
        closed = self.owner._library_first_reply()
        if closed is not None:
            return "", closed
        root = self.owner._require_current_selection(signature)
        raw = self.owner.raw
        try:
            style = self._style(values[3:])
            job = raw.prepare_subtitle_burn(root, *(_given(text) for text in values[:3]))
            text = self._run(root, job, style)
        except self.service_error as error:
            raise gr.Error(str(error)) from error
        return text, text

    def _run(self, root: Path, job: Any, style: Any) -> str:
        facts: list[dict[str, Any]] = []
        raw = self.owner.raw
        handle = raw.start_subtitle_burn(root, job, style, facts.append)
        record = raw.get_job(root, handle.job_id)
        while record.status not in JOB_TERMINAL_STATUSES:
            time.sleep(POLL_SECONDS)
            record = raw.get_job(root, handle.job_id)
        if record.status == "cancelled":
            return CANCELLED_TEXT
        if record.status != "succeeded":
            raise self.service_error(f"자막을 입히지 못했습니다 — {record.error}")
        return _done_text(facts[0]) if facts else JOINED_TEXT.format(name=job.video.name)

    def _mapping(self, values: tuple[Any, ...]) -> dict[str, Any]:
        """손잡이 값 → 모양 사전. 색은 `#RRGGBB`로 맞춘다."""
        mapping = dict(zip(STYLE_KEYS, values, strict=True))
        mapping["text_color"] = normalize_color(mapping["text_color"])
        return mapping

    def _style(self, values: tuple[Any, ...]) -> Any:
        """손잡이 값 → 검사한 모양."""
        return self.owner.raw.subtitle_burn_style(values=self._mapping(values))
