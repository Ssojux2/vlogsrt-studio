"""한국어 SRT → 영어 SRT(W-63, D114) — 읽기(무료) · 번역(유료, 사람이 누른 뒤) · 쓰기.

사람이 건넨 한국어 SRT 하나를 큐마다 영어로 옮겨 같은 번호·같은 시각의 영어 SRT를 쓴다. 시각은
입력이 준 밀리초 값 그대로 다시 적을 뿐 만들거나 옮기지 않는다(규칙 1·3) — 겹침·순서도 그대로다.
번역 문서는 `analysis/translation/<열쇠>.json`(D105와 같은 자리·꼴)이고 열쇠는 큐 글에만
묶이므로 같은 글의 SRT는 다시 사지 않는다. 부르기는 `transcribe.request_gemini_generate`
문 하나다(규칙 7).
입력 SRT·한국어 원본 자막·지문·캐시 열쇠에는 아무것도 쓰지 않는다.

서비스만 부른다(`RawWorkspaceService.start_srt_translation`).
"""

from __future__ import annotations

import json
import os
import types
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import log
from vlogsrt.project.raw import timeline_srt, translation
from vlogsrt.project.raw import translation_io as tio
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT
from vlogsrt.project.raw.config import TRANSLATION, TranslationSettings
from vlogsrt.project.raw.finalcut_timeline import Cue
from vlogsrt.project.raw.models import AnalysisError, RawWorkspaceError
from vlogsrt.project.timebase import format_srt_time
from vlogsrt.task_runtime import check_cancel

if TYPE_CHECKING:
    from vlogsrt.project.raw.service import RawWorkspaceService

ENGLISH_SUFFIX = ".en"
KOREAN_SUFFIX = ".ko"
FALLBACK_FOLDER = ("export", "english")
"""입력 옆에 쓸 수 없을 때의 자리 — 작업공간 `export/english/<시각>/`."""
STAMP_FORMAT = "%Y%m%d-%H%M%S"


@dataclass(frozen=True, slots=True)
class SrtJob:
    """옮길 SRT 하나 — 읽은 큐(시각·글), 보낼 줄, 번역 열쇠."""

    path: Path
    cues: tuple[Cue, ...]
    lines: tuple[translation.TranslationLine, ...]
    key: str


def prepare(
    service: RawWorkspaceService, root: Path, srt: Path, settings: TranslationSettings = TRANSLATION
) -> SrtJob:
    """SRT를 읽어 옮길 줄과 열쇠를 짓는다(무료). 못 읽거나 옮길 글이 없으면 이름을 대고 거부."""
    try:
        read = timeline_srt.read(srt)
    except timeline_srt.TimelineSrtError as error:
        hint = " UTF-8로 저장해 다시 고르세요." if isinstance(error.__cause__, UnicodeError) else ""
        raise RawWorkspaceError(f"{error}{hint}") from error
    lines = translation.cue_lines([cue.text for cue in read.cues])
    if not lines:
        raise RawWorkspaceError(f"SRT `{srt.name}`에 옮길 자막 글이 없습니다")
    glossary_sha = service.glossary(root).sha
    key = translation.source_key(translation.SRT_FILE_NAMESPACE, lines, glossary_sha, settings)
    return SrtJob(srt, read.cues, lines, key)


def _folder(service: RawWorkspaceService, root: Path) -> Path:
    return service._workspace(root).root


def cached(service: RawWorkspaceService, root: Path, job: SrtJob) -> dict | None:
    """저장된 번역(같은 열쇠) — 있으면 다시 사지 않는다."""
    path = tio.result_path(_folder(service, root), ANALYSIS_ROOT, job.key)
    return tio.load(path, tio.RESULT_SCHEMA, job.key)


def require_online(service: RawWorkspaceService, root: Path) -> None:
    """보내기 전 문 — 오프라인 환경·작업공간 network_mode가 online이 아니면 닫는다."""
    from vlogsrt.project.raw.understanding_luna import online_env_blocked

    blocked = online_env_blocked()
    if blocked is not None:
        raise AnalysisError(blocked)
    if service.settings(root).network_mode != "online":
        raise AnalysisError("작업공간 network_mode가 online이 아니다 — 영어 자막을 옮길 수 없다")


def _ask(
    client: object,
    batch: translation.TranslationBatch,
    terms: Sequence[str],
    s: TranslationSettings,
) -> tuple[dict[str, str], dict[str, Any]]:
    """한 묶음을 보내고 해석한다 — (줄 → 영어, 잰 사용량)."""
    from vlogsrt.project.raw import gemini_cost
    from vlogsrt.project.raw.understanding_luna import gemini_text
    from vlogsrt.transcribe import request_gemini_generate

    body = tio.payload(batch, terms, s)
    response = request_gemini_generate(client, model=s.model, payload=body, online=True)
    text, reason = gemini_text(response)
    if text is None:
        raise translation.TranslationError(reason)
    english = translation.parse(json.loads(text), batch)
    meta = types.SimpleNamespace(model=s.model)
    usage = gemini_cost.measure_gemini_usage(meta, response, on=date.today())
    return english, asdict(usage)


LINE_ID = "line"
"""한 줄 번역(W-112)에서 옮길 줄의 아이디 — 맥락 줄(`c1`…)과 겹치지 않는다."""


def translate_line(
    service: RawWorkspaceService,
    root: Path,
    korean: str,
    context: Sequence[str],
    client: object,
    settings: TranslationSettings = TRANSLATION,
) -> str:
    """자막 한 줄을 영어로 옮긴다(유료 — 번역 모드 「이 줄 영어 번역」 누름이 승인, W-112 · D186).

    앞 줄들(`context`, 많아야 `context_lines`줄)은 맥락으로만 싣고 옮기지 않는다. 요청 · 해석 ·
    용어집은 「영어 만들기」와 같다. 저장하지 않는다 — 화면이 고친 영어(D166)로 문서에 넣는다."""
    require_online(service, root)
    text = " ".join(korean.split())
    if not text:
        raise RawWorkspaceError("옮길 한국어가 비었습니다")
    before = [" ".join(line.split()) for line in context if line.strip()]
    before = before[max(0, len(before) - settings.context_lines) :]
    lines = tuple(translation.TranslationLine(f"c{n}", line) for n, line in enumerate(before, 1))
    batch = translation.TranslationBatch(0, lines, (translation.TranslationLine(LINE_ID, text),))
    english, _usage = _ask(client, batch, service.glossary(root).terms, settings)
    log.info(f"자막 한 줄을 영어로 옮겼다 · {settings.model}")
    return translation.wrap_english(english[LINE_ID], settings.english_line_chars)[0]


def translate(
    service: RawWorkspaceService,
    root: Path,
    job: SrtJob,
    client: object,
    settings: TranslationSettings = TRANSLATION,
) -> dict[str, Any]:
    """묶음마다 옮겨 한 문서로 쓴다(유료) — 실패는 까닭을 `.failed.json`에 남기고 다시 올린다."""
    folder = _folder(service, root)
    terms = service.glossary(root).terms
    english: dict[str, str] = {}
    usage: list[dict[str, Any]] = []
    log.info(f"영어 자막 옮기는 중 · {job.path.name} · {len(job.lines)}줄")
    try:
        for batch in translation.build_batches(job.lines, settings):
            check_cancel()
            found, measured = _ask(client, batch, terms, settings)
            english.update(found)
            usage.append(measured)
    except (translation.TranslationError, ValueError, RuntimeError, OSError) as error:
        failure = tio.failure_document(job.key, job.path.name, settings.model, str(error))
        tio.write(tio.failure_path(folder, ANALYSIS_ROOT, job.key), failure)
        raise
    document = tio.result_document(job.key, job.path.name, settings.model, english, usage)
    tio.write(tio.result_path(folder, ANALYSIS_ROOT, job.key), document)
    return document


def srt_body(cues: Sequence[Cue], texts: Sequence[str]) -> str:
    """같은 번호·같은 시각 줄에 글만 바꾼 SRT — 시각은 읽은 밀리초 값을 그대로 적는다."""
    blocks = []
    for number, (cue, text) in enumerate(zip(cues, texts, strict=True), start=1):
        timeline = f"{format_srt_time(cue.start)} --> {format_srt_time(cue.end)}"
        blocks.append(f"{number}\n{timeline}\n{text}\n")
    return "\n".join(blocks)


def english_name(srt: Path) -> str:
    """`draft.srt` → `draft.en.srt`, `여행.ko.srt` → `여행.en.srt`."""
    stem = srt.stem.removesuffix(KOREAN_SUFFIX) if srt.stem.endswith(KOREAN_SUFFIX) else srt.stem
    return f"{stem}{ENGLISH_SUFFIX}{srt.suffix}"


def _write_new(path: Path, payload: bytes) -> None:
    """새 파일로만 쓴다 — 이미 있으면 `FileExistsError`(덮어쓰지 않는다)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "wb") as handle:
        handle.write(payload)


def _place(folder: Path, name: str, payload: bytes, stamp: str) -> tuple[Path, bool]:
    """(쓴 자리, 같은 글이 이미 있었는가) — 같은 이름에 다른 글이 있으면 시각을 붙인 새 이름."""
    target = folder / name
    if target.is_file() and target.read_bytes() == payload:
        return target, True
    if target.exists():
        target = folder / f"{Path(name).stem}.{stamp}{Path(name).suffix}"
    _write_new(target, payload)
    return target, False


def english_payload(
    job: SrtJob, document: dict[str, Any], settings: TranslationSettings = TRANSLATION
) -> tuple[bytes, dict[str, Any]]:
    """저장된 번역 → 영어 SRT 바이트와 센 수. 같은 입력이면 같은 바이트다(쓴 파일과 대조한다)."""
    english = {str(key): str(value) for key, value in document["english"].items()}
    return english_texts_payload(job, english, settings)


def english_texts_payload(
    job: SrtJob, english: dict[str, str], settings: TranslationSettings = TRANSLATION
) -> tuple[bytes, dict[str, Any]]:
    """{큐 번호: 영어} → 영어 SRT 바이트와 센 수 — 옮긴 글이 없는 큐가 있으면 거부한다(규칙 9)."""
    texts, counts = translation.english_cue_texts(
        [cue.text for cue in job.cues], english, settings.english_line_chars
    )
    return srt_body(job.cues, texts).encode("utf-8"), counts


def english_candidates(korean: Path) -> list[Path]:
    """한국어 SRT 옆의 영어 SRT(D114 이름) — 시각을 붙인 이름이 새것부터, 맨 뒤가 처음 이름."""
    plain = korean.parent / english_name(korean)
    stamped = sorted(korean.parent.glob(f"{plain.stem}.*{plain.suffix}"), reverse=True)
    return [*stamped, *([plain] if plain.is_file() else [])]


def matching_english(
    service: RawWorkspaceService, root: Path, korean: Path
) -> tuple[Path | None, tuple[Path, ...]]:
    """(지금 한국어 글을 옮긴 영어 SRT, 그렇지 않은 옆의 영어 SRT들) — 무료, 부르지 않는다.

    지금 글의 저장된 번역으로 다시 조판한 바이트와 같은 파일만 지금 글의 영어다. 글을 고친 뒤의
    옛 영어 SRT나 손으로 고친 영어 SRT는 맞지 않는다(그런 파일은 사람이 직접 고르면 쓴다, D118)."""
    candidates = english_candidates(korean)
    if not candidates:
        return None, ()
    job = prepare(service, root, korean)
    document = cached(service, root, job)
    if document is not None:
        expected, _ = english_payload(job, document)
        for candidate in candidates:
            if candidate.read_bytes() == expected:
                return candidate, ()
    return None, tuple(candidates)


def write_english(
    service: RawWorkspaceService,
    root: Path,
    job: SrtJob,
    document: dict[str, Any],
    settings: TranslationSettings = TRANSLATION,
) -> dict[str, Any]:
    """조판해 입력 옆에 쓴다(못 쓰면 작업공간 `export/english/<시각>/`) — 결과 사실을 돌려준다."""
    payload, counts = english_payload(job, document, settings)
    return place_english(service, root, job, payload, counts, settings)


def place_english(
    service: RawWorkspaceService,
    root: Path,
    job: SrtJob,
    payload: bytes,
    counts: dict[str, Any],
    settings: TranslationSettings = TRANSLATION,
) -> dict[str, Any]:
    """영어 SRT 바이트를 입력 옆 새 이름에(같은 글이면 그 파일) — 못 쓰면 작업공간에."""
    stamp = datetime.now(UTC).strftime(STAMP_FORMAT)
    name = english_name(job.path)
    try:
        path, existed = _place(job.path.parent, name, payload, stamp)
    except OSError as error:
        log.warn(f"입력 옆에 쓰지 못해 작업공간에 씁니다: {job.path.parent} — {error}")
        fallback = _folder(service, root).joinpath(*FALLBACK_FOLDER, stamp)
        fallback.mkdir(parents=True, exist_ok=True)
        path, existed = _place(fallback, name, payload, stamp)
    return {"path": str(path), "already_there": existed, "model": settings.model, **counts}


def convert(
    service: RawWorkspaceService, root: Path, job: SrtJob, client: object | None
) -> dict[str, Any]:
    """저장된 번역이 있으면 그것으로(무료), 없으면 옮겨서(유료) 영어 SRT를 쓴다."""
    document = cached(service, root, job)
    reused = document is not None
    if document is None:
        if client is None:
            raise RawWorkspaceError("영어 자막을 옮길 Gemini 연결이 없습니다")
        document = translate(service, root, job, client)
    return {**write_english(service, root, job, document), "reused": reused}
