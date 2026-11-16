"""타임라인 자막의 영어 열(W-87d, D153) — 저장된 번역을 한국어 글로 찾는다(읽기만, 무료).

번역 저장(D114)은 SRT 통째의 열쇠(`translation.source_key`)이고 줄의 한국어를 싣지 않는다. 그래서
이 타임라인을 렌더한 초안(`export/<시각>/render_report.json`의 `timeline.id`)마다 그 `draft.srt`를
다시 읽어 저장된 번역과 줄 아이디로 맞춰 **한국어 글(공백을 한 칸으로) → 영어** 짝을 만든다. 새
초안이 먼저다. 글을 고친 줄은 짝이 없다 — 화면이 「번역 없음」으로 보인다.

부르지 않고(사지 않는다 — 번역은 결과 카드의 「영어 SRT로 변환」뿐이다, D114), 쓰지 않는다. 자막·
시각·문서·지문·캐시 열쇠 어디에도 들지 않는다.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from vlogsrt.project.raw import render_export, srt_english
from vlogsrt.project.raw.models import RawWorkspaceError

if TYPE_CHECKING:
    from vlogsrt.project.raw.service import RawWorkspaceService


def english_by_text(service: RawWorkspaceService, root: Path, timeline_id: str) -> dict[str, str]:
    """이 타임라인을 렌더한 초안들의 저장된 번역 — {한국어 한 줄: 영어}, 새 초안의 것이 이긴다."""
    pairs: dict[str, str] = {}
    workspace_root = service._workspace(root).root
    for folder in render_export.report_folders(workspace_root):
        if _timeline_of(folder) != timeline_id:
            continue
        for korean, english in _pairs(service, root, folder / render_export.SRT_NAME).items():
            pairs.setdefault(korean, english)
    return pairs


def _timeline_of(folder: Path) -> str | None:
    try:
        report = json.loads((folder / render_export.REPORT_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 못 읽는 보고서는 이 타임라인의 것으로 치지 않는다
    timeline = report.get(render_export.TIMELINE_REPORT_KEY) if isinstance(report, dict) else None
    return str(timeline.get("id")) if isinstance(timeline, dict) else None


def _pairs(service: RawWorkspaceService, root: Path, srt: Path) -> dict[str, str]:
    if not srt.is_file():
        return {}
    try:
        job = srt_english.prepare(service, root, srt)
    except RawWorkspaceError:
        return {}  # 옮길 글이 없거나 못 읽는 SRT — 짝이 없을 뿐이다
    document = srt_english.cached(service, root, job)
    if document is None:
        return {}
    english = document.get("english") or {}
    return {
        line.text: str(english[line.utterance_id])
        for line in job.lines
        if english.get(line.utterance_id)
    }
