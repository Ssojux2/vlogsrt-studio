"""영어 자막 번역(W-63, D114 — D105에서 왔다)의 순수 계층.

묶음 · 요청 글 · 응답 해석 · 열쇠 · 줄 나눔.

사람이 건넨 한국어 SRT의 큐 글을 큐마다 영어 한 줄로 옮긴다. **시각은 다루지 않는다** — 영어
SRT는 입력 SRT의 큐 시각을 그대로 쓴다(규칙 1·3). 줄을 합치거나 나누거나
빼지 않는다: 응답은 묶음의 아이디와 정확히 같아야 하고, 아니면 거부한다(규칙 9 — 조용히 빠지는 줄이
없다). 부르기·저장은 서비스와 `translation_io`가 한다.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from vlogsrt.project.raw.config import TranslationSettings

KEY_SCHEMA = "raw-translation-key/v1"
SRT_FILE_NAMESPACE = "srt-file/v1"
"""SRT 파일 번역 열쇠의 원본 자리(D114) — 파일 바이트가 아니라 큐 글에만 묶인다(시각만 고친
SRT는 다시 사지 않는다)."""

INSTRUCTION = "\n".join(
    (
        "You translate Korean subtitles of a travel vlog into natural, concise English subtitles.",
        "- Translate every item in `lines` into exactly one English subtitle with the same `id`.",
        "- Never merge, split, skip or reorder items. Keep fillers and exclamations short.",
        "- `context` items are the lines just before: read them for meaning, never translate.",
        "- Keep names of people and places as spoken; use the `glossary` spellings when given.",
        "- Output plain subtitle text only: no quotes around lines, no notes, no Korean.",
    )
)

RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"id": {"type": "string"}, "en": {"type": "string"}},
                "required": ["id", "en"],
            },
        }
    },
    "required": ["items"],
}


class TranslationError(ValueError):
    """응답이 그 묶음의 줄과 맞지 않는다 — 그 원본의 영어 자막은 쓰지 않는다."""


@dataclass(frozen=True, slots=True)
class TranslationLine:
    """옮길 줄 하나 — 원본 자막 줄 아이디와 내보내는 한국어."""

    utterance_id: str
    text: str


@dataclass(frozen=True, slots=True)
class TranslationBatch:
    """한 요청 — 옮길 줄과 그 앞의 맥락 줄(옮기지 않는다)."""

    index: int
    context: tuple[TranslationLine, ...]
    lines: tuple[TranslationLine, ...]


def build_batches(
    lines: Sequence[TranslationLine], settings: TranslationSettings
) -> tuple[TranslationBatch, ...]:
    """줄을 `batch_lines`씩 — 모든 줄이 한 번씩 들어가고, 둘째부터 직전 줄이 맥락으로 붙는다."""
    batches = []
    for index, start in enumerate(range(0, len(lines), settings.batch_lines)):
        context = tuple(lines[max(0, start - settings.context_lines) : start])
        chunk = tuple(lines[start : start + settings.batch_lines])
        batches.append(TranslationBatch(index, context, chunk))
    return tuple(batches)


def _rows(lines: Sequence[TranslationLine]) -> list[dict[str, str]]:
    return [{"id": line.utterance_id, "ko": line.text} for line in lines]


def batch_text(batch: TranslationBatch, glossary_terms: Sequence[str]) -> str:
    """요청에 싣는 글 — 용어집 · 맥락 · 옮길 줄의 JSON."""
    body = {
        "glossary": list(glossary_terms),
        "context": _rows(batch.context),
        "lines": _rows(batch.lines),
    }
    return json.dumps(body, ensure_ascii=False)


def _items(body: object) -> list[object]:
    items = body.get("items") if isinstance(body, Mapping) else None
    if not isinstance(items, list):
        raise TranslationError("응답에 items 목록이 없다")
    return items


def parse(body: object, batch: TranslationBatch) -> dict[str, str]:
    """응답 → 아이디별 영어 — 묶음의 아이디와 정확히 같고 모두 비지 않아야 한다."""
    wanted = [line.utterance_id for line in batch.lines]
    found: dict[str, str] = {}
    for item in _items(body):
        if not isinstance(item, Mapping):
            raise TranslationError("응답 줄의 모양이 다르다")
        key, text = item.get("id"), item.get("en")
        if not isinstance(key, str) or not isinstance(text, str):
            raise TranslationError("응답 줄의 모양이 다르다 (id·en은 글자)")
        if key not in wanted:
            raise TranslationError(f"모르는 줄 아이디가 왔다: {key}")
        if key in found:
            raise TranslationError(f"같은 줄이 두 번 왔다: {key}")
        cleaned = " ".join(text.split())
        if not cleaned:
            raise TranslationError(f"영어가 비었다: {key}")
        found[key] = cleaned
    missing = [key for key in wanted if key not in found]
    if missing:
        raise TranslationError(f"영어가 빠졌다: {len(missing)}줄 (첫 줄 {missing[0]})")
    return {key: found[key] for key in wanted}


def source_key(
    source_sha256: str,
    lines: Sequence[TranslationLine],
    glossary_sha: str,
    settings: TranslationSettings,
) -> str:
    """원본 하나의 번역 열쇠 — 판·모델·묶음·안내문·용어집·원본·줄(아이디와 글자)."""
    document = [
        KEY_SCHEMA,
        settings.prompt_revision,
        settings.model,
        settings.batch_lines,
        settings.context_lines,
        INSTRUCTION,
        glossary_sha,
        source_sha256,
        [[line.utterance_id, line.text] for line in lines],
    ]
    body = json.dumps(document, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def wrap_english(text: str, line_chars: int) -> tuple[str, bool]:
    """영어 한 줄을 `line_chars`마다 낱말 사이로 나눈다 — (나눈 글, 두 줄을 넘는가).

    긴 낱말은 자르지 않고, 두 줄을 넘어도 글자를 버리지 않는다(센다)."""
    lines = textwrap.wrap(
        " ".join(text.split()),
        width=line_chars,
        break_long_words=False,
        break_on_hyphens=False,
    )
    return "\n".join(lines), len(lines) > 2


def cue_lines(texts: Sequence[str]) -> tuple[TranslationLine, ...]:
    """SRT 큐 글 → 옮길 줄. 아이디는 **파일 전체에서의 순번**(1부터)이고, 여러 줄 큐는 한 줄로
    잇고, 빈 큐는 보내지 않는다 — 빈 큐를 빼고 다시 매기지 않으므로 영어가 제 큐로 돌아간다."""
    lines = []
    for number, text in enumerate(texts, start=1):
        joined = " ".join(text.split())
        if joined:
            lines.append(TranslationLine(str(number), joined))
    return tuple(lines)


def english_cue_texts(
    texts: Sequence[str], english: Mapping[str, str], line_chars: int
) -> tuple[tuple[str, ...], dict[str, int]]:
    """큐마다 나눈 영어(빈 큐는 빈 글 그대로)와 센 수 — 옮긴 글이 없는 큐가 있으면 거부한다
    (한국어로 메우거나 조용히 비우지 않는다, 규칙 9)."""
    rows: list[str] = []
    empty = over = missing = 0
    for number, text in enumerate(texts, start=1):
        if not text.strip():
            rows.append("")
            empty += 1
            continue
        found = english.get(str(number))
        if found is None:
            missing += 1
            continue
        wrapped, too_long = wrap_english(found, line_chars)
        rows.append(wrapped)
        over += too_long
    if missing:
        raise TranslationError(f"옮긴 글이 없는 큐가 {missing}장 있다")
    counts = {"cues": len(rows), "empty_cues": empty, "over_two_lines": over}
    return tuple(rows), counts
