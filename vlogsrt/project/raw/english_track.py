"""초안의 영어 자막(W-53, D105) — 옮길 원본 고르기(무료) · 번역(유료, 내보내기 누름 뒤) · 재료.

견적(분량·금액 문구)은 없다(D110) — 「영어 자막도 만들기」를 켜고 내보내기를 누르는 것이 승인이다.

원본마다 **지금 원본 자막 전체**를 옮긴다(초안에 실리는 대로 다듬은 한국어). 검토(추천·제외)가
바뀌어도 번역은 그대로 쓰이고, 자막을 고치면 열쇠가 바뀌어 그 원본만 다시 옮긴다. 이미 옮긴 원본은
다시 사지 않는다. 부르기는 `transcribe.request_gemini_generate` 문 하나다(규칙 7). 한국어 자막·시각·
지문·캐시 열쇠에는 아무것도 쓰지 않는다 — 영어 SRT는 초안 계획의 큐 시각을 그대로 쓴다(규칙 1·3).

서비스만 부른다(`RawWorkspaceService.start_render_export`).
"""

from __future__ import annotations

import json
import types
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import log
from vlogsrt.project.raw import render_plan, translation
from vlogsrt.project.raw import translation_io as tio
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT
from vlogsrt.project.raw.config import TRANSLATION, TranslationSettings
from vlogsrt.project.raw.models import SourceAsset
from vlogsrt.project.raw.render_export import EnglishTrack
from vlogsrt.task_runtime import check_cancel

if TYPE_CHECKING:
    from vlogsrt.project.raw.service import RawWorkspaceService


@dataclass(frozen=True, slots=True)
class EnglishSource:
    """옮길 원본 하나 — 줄과 그 번역 열쇠."""

    asset: SourceAsset
    lines: tuple[translation.TranslationLine, ...]
    key: str


def sources(
    service: RawWorkspaceService, root: Path, settings: TranslationSettings = TRANSLATION
) -> list[EnglishSource]:
    """촬영 순서의 원본마다 내보낼 한국어 줄(다듬은 글, 빈 줄은 뺀다 — 초안 투영과 같은 규칙)."""
    glossary_sha = service.glossary(root).sha
    found = []
    for asset in service.global_order_proposal(root):
        document = service.source_transcript(root, asset.asset_id) or {}
        lines = tuple(
            translation.TranslationLine(str(row["utterance_id"]), text)
            for row in document.get("utterances", [])
            if (text := render_plan.caption_text(str(row["text"]))).strip()
        )
        if lines:
            key = translation.source_key(asset.content_sha256 or "", lines, glossary_sha, settings)
            found.append(EnglishSource(asset, lines, key))
    return found


def _folder(service: RawWorkspaceService, root: Path) -> Path:
    return service._workspace(root).root


def _cached(service: RawWorkspaceService, root: Path, item: EnglishSource) -> dict | None:
    path = tio.result_path(_folder(service, root), ANALYSIS_ROOT, item.key)
    return tio.load(path, tio.RESULT_SCHEMA, item.key)


def missing_keys(
    service: RawWorkspaceService, root: Path, settings: TranslationSettings = TRANSLATION
) -> tuple[str, ...]:
    """아직 옮기지 않은 원본의 번역 열쇠(무료, 부르지 않는다) — 내보내기를 누른 순간 이것만 산다."""
    found = sources(service, root, settings)
    return tuple(item.key for item in found if _cached(service, root, item) is None)


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


def _translate_one(
    service: RawWorkspaceService,
    root: Path,
    item: EnglishSource,
    client: object,
    settings: TranslationSettings,
) -> None:
    """원본 하나를 묶음마다 옮겨 한 문서로 쓴다 — 실패는 까닭을 남기고 이름을 대고 올린다."""
    folder = _folder(service, root)
    terms = service.glossary(root).terms
    english: dict[str, str] = {}
    usage: list[dict[str, Any]] = []
    try:
        for batch in translation.build_batches(item.lines, settings):
            check_cancel()
            found, measured = _ask(client, batch, terms, settings)
            english.update(found)
            usage.append(measured)
    except (translation.TranslationError, ValueError, RuntimeError, OSError) as error:
        failure = tio.failure_document(item.key, item.asset.asset_id, settings.model, str(error))
        tio.write(tio.failure_path(folder, ANALYSIS_ROOT, item.key), failure)
        raise
    document = tio.result_document(item.key, item.asset.asset_id, settings.model, english, usage)
    tio.write(tio.result_path(folder, ANALYSIS_ROOT, item.key), document)


def translate_missing(
    service: RawWorkspaceService,
    root: Path,
    client: object,
    approved: Sequence[str],
    settings: TranslationSettings = TRANSLATION,
) -> list[dict[str, str]]:
    """승인한 열쇠 가운데 아직 옮기지 않은 원본을 옮긴다(유료) — 한 원본이 실패해도 나머지는
    옮긴다. 승인 뒤 자막이 바뀐 원본(열쇠가 다르다)은 사지 않는다(내보내기가 번역 없음으로 센다)."""
    failed: list[dict[str, str]] = []
    for item in sources(service, root, settings):
        if item.key not in approved or _cached(service, root, item) is not None:
            continue
        log.info(f"영어 자막 옮기는 중 · {item.asset.source_name}")
        try:
            _translate_one(service, root, item, client, settings)
        except (translation.TranslationError, ValueError, RuntimeError, OSError) as error:
            log.warn(f"영어 자막을 옮기지 못했습니다: {item.asset.source_name} — {error}")
            failed.append({"name": item.asset.source_name, "reason": str(error)})
    return failed


def track(
    service: RawWorkspaceService,
    root: Path,
    failed: Sequence[Mapping[str, str]] = (),
    settings: TranslationSettings = TRANSLATION,
) -> EnglishTrack:
    """내보내기에 넘길 재료 — 번역이 있는 원본의 나눈 영어와 번역이 없는 원본의 이름·까닭.

    번역이 없는 원본이 있어도 여기서 막지 않는다 — 초안에 실린 큐가 그 원본의 것일 때만 내보내기가
    영어 SRT를 쓰지 않는다(초안에서 빠진 원본은 영어를 막지 않는다)."""
    texts: dict[tuple[str, str], str] = {}
    over = 0
    lacking = [dict(row) for row in failed]
    named = {row["name"] for row in lacking}
    for item in sources(service, root, settings):
        document = _cached(service, root, item)
        if document is None:
            if item.asset.source_name not in named:
                lacking.append({"name": item.asset.source_name, "reason": "번역이 없습니다"})
            continue
        for line in item.lines:
            wrapped, too_long = translation.wrap_english(
                str(document["english"][line.utterance_id]), settings.english_line_chars
            )
            texts[(item.asset.asset_id, line.utterance_id)] = wrapped
            over += too_long
    report = {"model": settings.model, "over_two_lines": over, "untranslated": lacking}
    return EnglishTrack(texts, report)


def require_online(service: RawWorkspaceService, root: Path) -> None:
    """보내기 전 문 — 오프라인 환경·작업공간 network_mode가 online이 아니면 닫는다."""
    from vlogsrt.project.raw.models import AnalysisError
    from vlogsrt.project.raw.understanding_luna import online_env_blocked

    blocked = online_env_blocked()
    if blocked is not None:
        raise AnalysisError(blocked)
    if service.settings(root).network_mode != "online":
        raise AnalysisError("작업공간 network_mode가 online이 아니다 — 영어 자막을 옮길 수 없다")
