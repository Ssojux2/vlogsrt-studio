"""Hydrogen 글자 귀(W-114b, D187)의 요청 본문·응답 읽기 — 부르지 않는다.

Transcribe가 들은 **같은 청크 WAV**(인라인 오디오)와 그 청크의 줄 목록(`id` · 청크
국소 `start`/`end` · Transcribe 초안 `draft`)으로 Gemini generateContent 본문을 짓고,
응답 schema로 JSON을 강제한다. 줄 시각은 Transcribe가 준 값을 **닻으로 싣기만** 한다 —
응답 schema에는 시각 칸이 없고, 응답 글에 시각이 적혀 있어도 읽지 않는다(Hydrogen은
시간 엔진이 아니다, 규칙 1). Transcribe에는 여전히 아무 힌트도 가지 않는다(규칙 6) —
이 본문은 Hydrogen에만 간다.

부르기(유료)는 하지 않는다 — 승인 뒤 호출자가 `transcribe.request_gemini_generate`로만
한다(규칙 7, W-114c). 응답 읽기(`read_reply`)는 늘 계획을 낸다: 막힘·출력 한도·STOP
아님·빈 글·JSON 아님은 청크 전체 초안 + 까닭이다(`hydrogen_text.draft_only`). 문이
던지는 것(승인·취소·전송 실패)은 여기 오지 않는다 — 호출자에게 그대로 올라간다.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.raw import gemini_cost, hydrogen_text
from vlogsrt.project.raw.config import HydrogenTextSettings

JSON_MIME_TYPE = "application/json"

INSTRUCTION = "\n".join(
    (
        "You transcribe the Korean speech in the attached audio, one chunk of a travel vlog,"
        " line by line.",
        "- `lines` lists this chunk's subtitle lines: `id`, `start` and `end` in seconds from the"
        " start of the audio, and `draft`, an automatic transcript that may be wrong.",
        "- Return exactly one item per line with the same `id`. `ko` is the Korean heard between"
        " its `start` and `end`, verbatim: keep casual speech, slang, fillers and repetitions as"
        " spoken; do not correct grammar, translate or summarize.",
        "- Never merge, split, skip, reorder or add lines. Use `draft` only to find where each"
        " line begins and ends; trust the audio over the draft.",
        "- If nothing is spoken between `start` and `end`, set `speech` to false and `ko` to an"
        " empty string; otherwise `speech` is true.",
        "- Speech heard outside every line goes to `extra`, with `after_id` set to the id of the"
        " line just before it (leave `after_id` out before the first line).",
        "- Never write times or notes in `ko`.",
    )
)

RESPONSE_SCHEMA: dict[str, object] = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "ko": {"type": "string"},
                    "speech": {"type": "boolean"},
                },
                "required": ["id", "ko", "speech"],
            },
        },
        "extra": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"after_id": {"type": "string"}, "ko": {"type": "string"}},
                "required": ["ko"],
            },
        },
    },
    "required": ["items", "extra"],
}


@dataclass(frozen=True, slots=True)
class ChunkLine:
    """요청에 싣는 줄 하나 — Transcribe 줄 아이디 · 청크 국소 시각(닻) · 초안 글자."""

    line_id: str
    start: Fraction
    end: Fraction
    draft: str


def draft_lines(lines: Sequence[ChunkLine]) -> tuple[hydrogen_text.DraftLine, ...]:
    """계획에 넘길 줄 — 시각을 떼고 아이디와 초안 글자만."""
    return tuple(hydrogen_text.DraftLine(line.line_id, line.draft) for line in lines)


def _seconds(value: Fraction, decimals: int) -> float:
    """청크 국소 초를 설정 자릿수로 반올림한 값 — 같은 유리수면 같은 JSON 숫자다."""
    scale = 10**decimals
    return round(value * scale) / scale


def request_text(lines: Sequence[ChunkLine], settings: HydrogenTextSettings) -> str:
    """요청에 싣는 줄 목록의 JSON — 압축 표기라 같은 입력이면 바이트가 같다(요청 신원)."""
    rows = [
        {
            "id": line.line_id,
            "start": _seconds(line.start, settings.time_decimals),
            "end": _seconds(line.end, settings.time_decimals),
            "draft": line.draft,
        }
        for line in lines
    ]
    return json.dumps({"lines": rows}, ensure_ascii=False, separators=(",", ":"))


def payload(
    wav: bytes, lines: Sequence[ChunkLine], settings: HydrogenTextSettings
) -> dict[str, object]:
    """generateContent 본문 — 안내문 · 인라인 WAV · 줄 목록, JSON schema 강제. 인라인 한도를
    넘으면 보내기 전에 닫는다(ValueError)."""
    audio = {"mimeType": settings.audio_mime_type, "data": base64.b64encode(wav).decode("ascii")}
    body: dict[str, object] = {
        "contents": [
            {
                "role": "user",
                "parts": [
                    {"text": INSTRUCTION},
                    {"inlineData": audio},
                    {"text": request_text(lines, settings)},
                ],
            }
        ],
        "generationConfig": {
            "maxOutputTokens": settings.max_output_tokens,
            "responseMimeType": JSON_MIME_TYPE,
            "responseJsonSchema": RESPONSE_SCHEMA,
            "thinkingConfig": {"thinkingLevel": settings.thinking_level},
        },
    }
    size = gemini_cost.request_bytes(body)
    if size > gemini_cost.INLINE_REQUEST_LIMIT_BYTES:
        raise ValueError(
            f"Hydrogen 글자 요청이 인라인 한도를 넘는다: {size}바이트"
            f" > {gemini_cost.INLINE_REQUEST_LIMIT_BYTES}"
        )
    return body


def read_reply(response: object, lines: Sequence[ChunkLine]) -> hydrogen_text.LineTextPlan:
    """generateContent 응답 → 줄 글자 계획. 읽지 못하면 청크 전체 초안 + 까닭(던지지 않는다)."""
    from vlogsrt.project.raw.understanding_luna import gemini_text

    drafts = draft_lines(lines)
    if not isinstance(response, Mapping):
        return hydrogen_text.draft_only(
            drafts, hydrogen_text.REASON_UNREADABLE, "Gemini 응답이 JSON 객체가 아니다"
        )
    text, reason = gemini_text(response)
    if text is None:
        return hydrogen_text.draft_only(drafts, hydrogen_text.REASON_UNREADABLE, reason)
    try:
        body = json.loads(text)
    except json.JSONDecodeError as error:
        detail = f"Hydrogen 응답 본문이 JSON이 아니다: {error.msg}"
        return hydrogen_text.draft_only(drafts, hydrogen_text.REASON_UNREADABLE, detail)
    return hydrogen_text.plan(drafts, body)


__all__ = [
    "INSTRUCTION",
    "RESPONSE_SCHEMA",
    "ChunkLine",
    "draft_lines",
    "payload",
    "read_reply",
    "request_text",
]
