"""「타임라인으로 보내기」(W-71b, D124) — 렌더하지 않고 추천 구간을 원본 범위 타임라인으로 만든다.

명령 줄의 「추천만 이어붙여 내보내기」 옆에 선다(대신하지 않는다 — 렌더·번인은 아직 초안
길이다, W-71e까지). 누르면 무료 로컬 작업(원본 조사만, ffmpeg 렌더 없음)을 걸고 끝날 때까지
기다려, 성공하면 「최종 편집」 탭으로 넘기고 그 탭 머리에 타임라인 요약(클립·자막 수, 바뀐 원본
자막, 지원 밖·심지 않은 원본)을 보인다. 재생·범위 조정 화면은 다음 단계(W-71c·d)다.

화면 틀(`RawWorkspaceUi`)과는 `owner`의 작은 면(선택 확인 · 라이브러리 닫힘 답 · 서비스)으로만
닿는다.
`vlogsrt.project`는 import하지 않는다 — 그 예외는 `ui/workspace_service.py` 하나라 서비스의 오류
종류·작업 종류·탭 열쇠도 만들 때 받는다.
"""

from __future__ import annotations

import html
import time
from fractions import Fraction
from typing import Any

import gradio as gr

SEND_LABEL = "타임라인으로 보내기"
SEND_ID = "vl-timeline-send"
SUMMARY_ID = "vl-timeline-summary"
JOB_LABEL = SEND_LABEL
POLL_SECONDS = 0.2
"""단추가 작업 끝을 다시 보는 간격 — 「영어 SRT로 변환」과 같다."""
JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
STARTED_TEXT = "타임라인을 만들었습니다 — 클립 {clips}개 · 자막 {cues}줄(렌더하지 않았습니다)."
CANCELLED_TEXT = "타임라인 만들기를 취소했습니다 — 쓴 파일은 없습니다."
EMPTY_TEXT = ""
NEXT_STEP_TEXT = "원본 범위 재생·클립 앞뒤 조정 화면은 다음 단계에서 이 탭에 붙습니다."


def _seconds(model: dict[str, Any]) -> float:
    duration = model["facts"]["sequence"]["frame_duration"]
    frames = int(model["summary"]["edited_frames"])
    return float(frames * Fraction(int(duration["num"]), int(duration["den"])))


def _names(rows: list[dict[str, Any]]) -> str:
    return ", ".join(html.escape(str(row.get("name", row.get("source_id", "")))) for row in rows)


def _notes(model: dict[str, Any]) -> list[str]:
    """빠진 것·바뀐 것 — 규칙 9: 조용히 사라지는 원본이 없게 이름을 댄다."""
    seed = model.get("seed", {})
    notes: list[str] = []
    changed = model.get("changedSources", [])
    if changed:
        notes.append(
            f"원본 자막이 그 뒤 바뀌었습니다: {_names(changed)}"
            " — 타임라인은 심을 때의 자막 그대로입니다."
        )
    for row in seed.get("unsupported", []):
        why = row.get("detail") or row.get("reason", "")
        notes.append(
            f"지원 밖 원본이라 넣지 않았습니다: {html.escape(str(row.get('name', '')))}"
            f" — {html.escape(str(why))}"
        )
    unseeded = seed.get("unseeded", [])
    if unseeded:
        notes.append(f"추천 구간이 없어 넣지 않은 원본: {_names(unseeded)}")
    if seed.get("empty"):
        notes.append(f"조판 정리 뒤 빈 자막 {int(seed['empty'])}줄은 심지 않았습니다.")
    if seed.get("clamped"):
        notes.append(
            f"원본 밖으로 나간 자막 시각 {int(seed['clamped'])}줄을 원본 안으로 넣었습니다."
        )
    return notes


def timeline_summary_html(model: dict[str, Any] | None) -> str:
    """「최종 편집」 탭 머리의 타임라인 요약 — 모든 글을 이스케이프한다. 없으면 빈 칸."""
    if model is None:
        return EMPTY_TEXT
    summary = model["summary"]
    head = (
        f"<strong>원본 범위 타임라인 · {html.escape(str(model['timelineId']))}</strong>"
        f" · 리비전 {int(model['revision'])} · 클립 {int(summary['clips'])}개"
        f" · 자막 {int(summary['visible_cues'])}/{int(summary['cues'])}줄 보임"
        f" · {_seconds(model):.1f}초"
    )
    items = "".join(f"<li>{note}</li>" for note in [*_notes(model), html.escape(NEXT_STEP_TEXT)])
    return f'<div class="vl-timeline-summary">{head}<ul>{items}</ul></div>'


class TimelineCell:
    """단추 하나와 「최종 편집」 탭 머리의 요약 칸 하나."""

    def __init__(self, owner: Any, service_error: type[Exception], job_kind: str, tab_key: str):
        self.owner = owner
        self.service_error = service_error
        self.job_kind = job_kind
        self.tab_key = tab_key
        self.button: Any = None
        self.summary: Any = None

    def build_button(self) -> None:
        self.button = gr.Button(SEND_LABEL, elem_id=SEND_ID)

    def build_summary(self) -> None:
        self.summary = gr.HTML(EMPTY_TEXT, elem_id=SUMMARY_ID, apply_default_css=False)

    def wire(self, tabs: Any, status: Any, refresh_events: tuple[Any, ...]) -> Any:
        """누름 → 만들기 → (탭, 요약, 활동 칸). 작업공간이 바뀌면 요약만 다시 그린다."""
        signature = self.owner._signature_state
        sent = self.button.click(
            self.send_view, signature, [tabs, self.summary, status], concurrency_limit=1
        )
        for event in refresh_events:
            event.then(self.view, signature, self.summary, show_progress="hidden")
        return sent

    def send_view(self, signature: str | None) -> tuple[Any, Any, str]:
        """「타임라인으로 보내기」 — 작업이 끝날 때까지 기다려, 성공하면 「최종 편집」 탭으로."""
        closed = self.owner._library_first_reply()
        if closed is not None:
            return gr.skip(), gr.skip(), closed
        root = self.owner._require_current_selection(signature)
        try:
            handle = self.owner.raw.start_timeline_export(root)
            record = self._wait(root, handle.job_id)
            if record.status == "cancelled":
                return gr.skip(), gr.skip(), CANCELLED_TEXT
            if record.status != "succeeded":
                raise self.service_error(f"타임라인을 만들지 못했습니다 — {record.error}")
            model = self.owner.raw.timeline_open(root)
        except self.service_error as error:
            raise gr.Error(str(error)) from error
        text = STARTED_TEXT.format(
            clips=model["summary"]["clips"], cues=model["summary"]["visible_cues"]
        )
        return gr.Tabs(selected=self.tab_key), timeline_summary_html(model), text

    def _wait(self, root: Any, job_id: str) -> Any:
        record = self.owner.raw.get_job(root, job_id)
        while record.status not in JOB_TERMINAL:
            time.sleep(POLL_SECONDS)
            record = self.owner.raw.get_job(root, job_id)
        return record

    def view(self, signature: str | None) -> str:
        """지금 작업공간의 가장 새 타임라인 요약 — 없거나 못 읽으면 빈 칸(못 읽으면 까닭)."""
        root = self.owner._snapshot_root()
        if root is None or not signature or signature != self.owner._signature_for(root):
            return EMPTY_TEXT
        if not self.owner.raw.has_timeline(root):
            return EMPTY_TEXT
        try:
            return timeline_summary_html(self.owner.raw.timeline_open(root))
        except self.service_error as error:
            reason = html.escape(str(error))
            return f'<div class="vl-timeline-summary">타임라인을 열지 못했습니다 — {reason}</div>'
