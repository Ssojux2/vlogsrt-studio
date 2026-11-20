"""「공유」(W-88, D147) — 원본 범위 타임라인의 저장한 리비전을 렌더하고, 고르면 한/영 자막을 입힌다.

Final Cut Pro의 공유 패널(「번인 캡션:」 — 없음 / 언어)을 옮겼다(정적 분석,
`docs/plans/2026-11-16-fcp-window.md`). 작업 하나가 두 단계를 차례로 한다 — 둘 다 이미 있는 길이다:

1. 렌더: `timeline_render.export_timeline` → `render_export.export_plan`(D130 — 같은
   `export/<시각>/`, 보고서 `timeline`).
2. 번인(고르면): `burn_export.prepare` → `burn_export.burn` — 자르지 않은 길(D116)이다. 렌더한
   `draft.mp4`가 곧 편집본이므로 초안 편집(D117)의 컷이 끼지 않는다. 모양은 작업공간에 저장한
   모양(D116 손잡이), 이름은 덮지 않는 `<이름>.burned.mp4`.

D118에 맞먹는 검사: 렌더한 `draft.srt`의 바이트가 그 리비전 문서의 `timeline_edit.srt_text`와
같아야 입힌다 — 다르면 다른 글을 입히게 되므로 거부한다.

**영어는 공유 안에서 사지 않는다**(D114 — 번역은 사람이 따로 누르는 유료 단추): 지금 한국어 글을
옮긴 영어 SRT가 옆에 있으면 쓰고, 없어도 같은 글의 번역이 작업공간에 저장돼 있으면 무료로 그 옆에
써서 쓴다. 둘 다 없으면 한국어만 입히고 까닭을 남긴다.

**사람이 고친 영어가 먼저다**(D166): 문서의 고친 영어 중 지금 한국어와 같은 글에 묶인 것(낡지 않은
것)은 그 큐의 저장된 번역을 덮는다. 번역이 없어도 고친 영어가 보이는 큐를 다 덮으면 그것만으로 쓴다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import log
from vlogsrt.project.raw import (
    burn_export,
    burn_in,
    srt_english,
    timeline_edit,
    timeline_edit_io,
    timeline_render,
    translation,
)
from vlogsrt.project.raw.models import AnalysisError, RawWorkspaceError

if TYPE_CHECKING:
    from vlogsrt.project.raw.jobs import JobContext
    from vlogsrt.project.raw.service import RawWorkspaceService

BURN_NONE = "none"
BURN_KOREAN = "ko"
BURN_KOREAN_ENGLISH = "ko-en"
BURN_MODES = (BURN_NONE, BURN_KOREAN, BURN_KOREAN_ENGLISH)
BURN_MODE_NAMES = {
    BURN_NONE: "자막 없음",
    BURN_KOREAN: "한국어",
    BURN_KOREAN_ENGLISH: "한국어 + 영어",
}
"""「최종출력」(W-107, D181)의 서버 줄이 말하는 번인 캡션 이름 — 화면 고르기와 같은 말."""

ENGLISH_OFF = "off"
ENGLISH_USED = "used"
ENGLISH_WRITTEN = "written"
ENGLISH_MISSING = "missing"
ENGLISH_EDITED = "edited"
ENGLISH_TRANSLATED = "translated"
ENGLISH_FAILED = "failed"
MISSING_ENGLISH_TEXT = (
    "이 글의 영어 번역이 아직 없어 한국어만 입혔습니다 — 「영어 만들기」를 누르거나"
    " 「한국어 + 영어」로 다시 최종출력하면 옮겨서 입힙니다."
)


def require_mode(mode: str) -> str:
    if mode not in BURN_MODES:
        raise RawWorkspaceError(
            f"번인 캡션을 알 수 없습니다: {mode} (있는 것: {', '.join(BURN_MODES)})"
        )
    return mode


def share(
    service: RawWorkspaceService,
    root: Path,
    timeline_id: str,
    revision: int,
    mode: str,
    style: burn_in.BurnStyle,
    context: JobContext | None = None,
    *,
    translator: object | None = None,
) -> dict[str, Any]:
    """렌더하고(언제나) 고르면 입힌다 — 결과 사실(폴더 · 영상 · SRT · 번인 · 영어)을 돌려준다.

    `translator`(Gemini 클라이언트, W-108 · D182)가 있으면 「한국어 + 영어」에서 번역이 없을 때
    그 자리에서 옮긴다(유료 — 「최종출력」 누름이 승인). 없으면 예전처럼 사지 않는다(D114)."""
    require_mode(mode)
    # 활동 띠·「기록」의 서버 로그가 가장 새 줄을 보인다(D101) — 최종출력이 도는 동안 그 줄이다.
    log.info(f"최종출력 중 · 리비전 {revision} · 영상에 넣을 자막: {BURN_MODE_NAMES[mode]}")
    exported = timeline_render.export_timeline(service, root, timeline_id, revision, context)
    facts: dict[str, Any] = {
        "folder": str(exported.folder),
        "video": str(exported.video),
        "srt": str(exported.srt),
        "burn": mode,
        "english": ENGLISH_OFF,
        "burned": None,
    }
    if mode == BURN_NONE:
        log.info(f"최종출력을 마쳤다: {exported.video}")
        return facts
    document = _require_rendered_text(root, timeline_id, revision, exported.srt)
    english, facts["english"] = _english(service, root, exported.srt, mode, document, translator)
    job = burn_export.prepare(exported.video, exported.srt, english)
    facts["burned"] = burn_export.burn(service, root, job, style)
    log.info(f"최종출력을 마쳤다: {facts['burned']['path']}")
    return facts


def _require_rendered_text(
    root: Path, timeline_id: str, revision: int, srt: Path
) -> timeline_edit.TimelineDocument:
    """렌더한 SRT가 그 리비전 문서의 글과 바이트가 같은가(D118의 sha 검사와 같은 뜻) — 그 문서."""
    _name, current, document = timeline_edit_io.read_document(root, timeline_id)
    if current != revision:
        raise RawWorkspaceError(
            f"렌더하는 사이 타임라인이 리비전 {current}으로 바뀌었다 — 렌더한 {revision}의 영상에"
            " 다른 글을 입히지 않는다. 다시 공유하세요"
        )
    expected = hashlib.sha256(timeline_edit.srt_text(document).encode("utf-8")).hexdigest()
    rendered = hashlib.sha256(srt.read_bytes()).hexdigest()
    if expected != rendered:
        raise RawWorkspaceError(
            f"렌더한 `{srt.name}`이 리비전 {revision}의 자막과 다르다 — 입히지 않았다(D118)"
        )
    return document


def _english(
    service: RawWorkspaceService,
    root: Path,
    korean: Path,
    mode: str,
    document: timeline_edit.TimelineDocument,
    translator: object | None = None,
) -> tuple[Path | None, str]:
    """입힐 영어 SRT와 그 출처. 고친 영어가 있으면 그것이 먼저다(D166). 저장된 번역이 없으면
    `translator`가 있을 때만 옮긴다(W-108, D182) — 없으면 사지 않는다(D114)."""
    if mode != BURN_KOREAN_ENGLISH:
        return None, ENGLISH_OFF
    edits = edited_by_number(document)
    if edits:
        return _english_with_edits(service, root, korean, edits, translator)
    matched, _others = srt_english.matching_english(service, root, korean)
    if matched is not None:
        return matched, ENGLISH_USED
    job = srt_english.prepare(service, root, korean)
    saved = srt_english.cached(service, root, job)
    source = ENGLISH_WRITTEN
    if saved is None and translator is not None:
        saved = _translate(service, root, job, translator)
        if saved is None:
            return None, ENGLISH_FAILED
        source = ENGLISH_TRANSLATED
    if saved is None:
        log.warn(MISSING_ENGLISH_TEXT)
        return None, ENGLISH_MISSING
    written = srt_english.write_english(service, root, job, saved)
    return Path(str(written["path"])), source


def _translate(
    service: RawWorkspaceService, root: Path, job: Any, translator: object
) -> dict[str, Any] | None:
    """Gemini로 옮긴다(유료). 실패하면 렌더한 영상을 버리지 않고 까닭을 경고로 남긴다(규칙 9)."""
    failures = (translation.TranslationError, AnalysisError, ValueError, RuntimeError, OSError)
    try:
        return srt_english.translate(service, root, job, translator)
    except failures as error:
        log.warn(f"영어 번역에 실패해 한국어만 입혔습니다 — {error}")
        return None


def edited_by_number(document: timeline_edit.TimelineDocument) -> dict[str, str]:
    """렌더한 SRT의 큐 번호(1부터) → 낡지 않은 고친 영어. 가려진 자막의 것은 들지 않는다."""
    if not document.english:
        return {}
    cues = {cue.cue_id: cue for cue in document.cues}
    fresh: dict[str, str] = {}
    for cue_id, korean, english in document.english:
        if cues[cue_id].text == korean:
            fresh[cue_id] = english
    rendered = timeline_edit.render_cues(document)[0]
    return {
        str(number): fresh[cue.utterance_id]
        for number, cue in enumerate(rendered, start=1)
        if cue.utterance_id in fresh
    }


def _english_with_edits(
    service: RawWorkspaceService,
    root: Path,
    korean: Path,
    edits: dict[str, str],
    translator: object | None = None,
) -> tuple[Path | None, str]:
    """저장된 번역(있으면)에 고친 영어를 덮어 쓴다. 빈 큐가 남으면 `translator`가 있을 때만 옮겨
    채우고(W-108, D182), 없으면 한국어만(사지 않는다)."""
    job = srt_english.prepare(service, root, korean)
    saved = srt_english.cached(service, root, job)
    english = {str(k): str(v) for k, v in (saved or {}).get("english", {}).items()}
    english.update(edits)
    try:
        payload, counts = srt_english.english_texts_payload(job, english)
    except translation.TranslationError:
        bought = _translate(service, root, job, translator) if translator is not None else None
        if bought is None:
            if translator is None:
                log.warn(MISSING_ENGLISH_TEXT)
            return None, ENGLISH_FAILED if translator is not None else ENGLISH_MISSING
        english = {str(k): str(v) for k, v in bought.get("english", {}).items()}
        english.update(edits)
        payload, counts = srt_english.english_texts_payload(job, english)
    written = srt_english.place_english(service, root, job, payload, counts)
    return Path(str(written["path"])), ENGLISH_EDITED
