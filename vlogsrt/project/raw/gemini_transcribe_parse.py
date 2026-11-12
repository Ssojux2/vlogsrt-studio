"""Gemini 3.5 Transcribe 응답 해석(W-31b, D92) — 엄격하고 순수하다(입출력·전송 없음).

응답(https://ai.google.dev/gemini-api/docs/transcribe): 전체 글과, 단어마다
`steps[].content[].annotations[]`의 `{"type": "word_info", "text", "start_offset", "end_offset"}`
(`"0.100s"` 꼴). 전체 글은 SDK 꼴이면 `output_text`(SDK의 편의 칸)다. `urllib`가 받는 REST 응답에는
그 칸이 없어 `steps[].content[]`의 글 칸(`type: "text"`의 `text`)을 차례로 띄어 잇는다
(W-52b2 — 실제 첫 영상의 모든 조각이 이 칸 때문에 실패했다). 여기서는 그 단어를 **정확 유리수
시각**과 함께 꺼내고, 띄어쓰기·문장부호를 전체 글에 맞춰 앞 단어 뒤에 붙인다 — 큐 글자는
단어만으로 다시 지을 수 있다(`word_cues`).

지키는 선(규칙 1·4·9, D92):

- 시각은 단어의 `start_offset`·`end_offset`뿐이다. 꼴이 어긋나거나 없거나 뒤집힌 시각은 **고치지
  않고** 그 단어를 시각 없는 단어로 남긴다(글자는 버리지 않는다, 센다).
- 단어가 덮지 못한 한글·영문·숫자는 **자리 없는 글자**다 — 시각이 없어 내보내지 않고 수만 센다
  (D37의 자리 없는 gpt 글자와 같은 생각).
- 청크 끝을 관용(D55, 2초) 안에서 넘은 끝만 청크 끝으로 닫는다. 그 밖은 청크 실패다.
- 글자는 있는데 시각 단어가 하나도 없으면 실패다(시각 없는 글자를 내보내지 않는다). 글자도 단어도
  없으면 빈 결과다 — 「말이 아니다」라고 판단하지 않는다(규칙 4, 미확인 빈 결과).
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction

from vlogsrt.project.raw.config import TIMING_RESPONSE_TOLERANCE

COMPLETED = "completed"
WORD_INFO = "word_info"
TEXT = "text"
"""REST 응답에서 전체 글을 싣는 content 항목의 종류(W-52b2)."""
USAGE = "usage"
METADATA_KEYS = frozenset(
    {"id", "created", "updated", "model", "object", "service_tier", "status", USAGE}
)
"""글을 싣지 않는 최상위 칸 — 실제 무음 조각(2026-11-12)의 REST 응답은 이 칸만 있었다. 이 밖의 칸이
하나라도 있으면 모양이 바뀐 것일 수 있어 빈 결과로 받지 않는다."""
ALIGNED = "aligned"
FALLBACK = "fallback"
"""단어 글자를 `output_text`에서 차례로 찾지 못했다 — 단어를 띄어 이어 붙인다(센다)."""
OFFSET = re.compile(r"(\d+(?:\.\d{1,9})?)s")
"""protobuf Duration의 JSON 꼴(`"1.250s"`) — 그 밖의 꼴은 시각으로 읽지 않는다."""


class GeminiTranscribeResponseError(ValueError):
    """응답을 받아 줄 수 없다 — 청크 하나의 보이는 실패다(조용히 비우지 않는다)."""


@dataclass(frozen=True, slots=True)
class ParsedWord:
    """단어 하나 — `display`는 글자 + 뒤(첫 단어는 앞도)에 붙은 띄어쓰기·부호.

    시각은 청크 국소 초이고, 없으면(꼴이 어긋난 응답) 둘 다 None이다. `dropped_after`는 이 단어 뒤
    (첫 단어는 앞도) 사이 글에서 싣지 못한 자리 없는 글자 수다 — 「확인 필요」 표시의 재료일 뿐이다.
    """

    text: str
    display: str
    start: Fraction | None
    end: Fraction | None
    dropped_after: int = 0

    @property
    def timed(self) -> bool:
        return self.start is not None and self.end is not None


@dataclass(frozen=True, slots=True)
class ParsedTranscript:
    words: tuple[ParsedWord, ...]
    output_text: str
    alignment: str
    unplaced_chars: int = 0
    untimed_words: int = 0
    clamped_words: int = 0


def parse_transcription(
    payload: object,
    *,
    chunk_duration: Fraction | float,
    tolerance: Fraction | float = TIMING_RESPONSE_TOLERANCE.chunk_end_overrun_seconds,
) -> ParsedTranscript:
    """응답 → 단어(정확 시각·붙은 부호)와 센 수. 받아 줄 수 없는 응답은 이름을 대고 실패한다."""
    if not isinstance(payload, Mapping):
        raise GeminiTranscribeResponseError(
            f"전사 응답이 JSON 객체가 아니다: {type(payload).__name__}"
        )
    if payload.get("status") != COMPLETED:
        raise GeminiTranscribeResponseError(
            f"전사가 끝나지 않았다: status={payload.get('status')!r}"
        )
    output_text = unicodedata.normalize("NFC", _transcript_text(payload))
    duration, limit = _fraction(chunk_duration), _fraction(tolerance)
    raw = _word_records(payload.get("steps"))
    timed = [_timed(record, duration, limit) for record in raw]
    if output_text.strip() and not any(word.timed for word, _ in timed):
        raise GeminiTranscribeResponseError("글자는 있는데 시각이 있는 단어가 하나도 없다")
    words, alignment, unplaced = _aligned([word for word, _ in timed], output_text)
    return ParsedTranscript(
        words=words,
        output_text=output_text,
        alignment=alignment,
        unplaced_chars=unplaced,
        untimed_words=sum(not word.timed for word in words),
        clamped_words=sum(clamped for _, clamped in timed),
    )


def _transcript_text(payload: Mapping[str, object]) -> str:
    """전체 글 — SDK 꼴 `output_text`가 있으면 그것, 없으면 REST의 content 글 칸을 띄어 이은 것.

    둘 다 없으면 실패다 — 빈 글로 지어내지 않는다. 하나만 예외다: 끝난(`completed`) 응답이 `usage`를
    싣고 글 없는 칸(`METADATA_KEYS`)만 있으면 모델이 듣고 적을 것이 없었던 조각(무음)이다 —
    빈 결과로 받고 소비 계층이 미확인 빈 골격으로 다룬다(「말이 아니다」라는 판정이 아니다,
    규칙 4)."""
    text = payload.get("output_text")
    if text is not None:
        if not isinstance(text, str):
            raise GeminiTranscribeResponseError("전사 응답의 output_text가 문자열이 아니다")
        return text
    steps = payload.get("steps")
    if steps is None and USAGE in payload and set(payload) <= METADATA_KEYS:
        return ""
    if steps is None:
        # 받은 칸 이름만 적는다(글은 싣지 않는다) — 모양이 또 다르면 상태 줄만으로 짚는다.
        keys = ", ".join(sorted(str(key) for key in payload))
        raise GeminiTranscribeResponseError(
            f"전사 응답에 output_text도 steps도 없다 (받은 칸: {keys})"
        )
    pieces: list[str] = []
    for step in _list(steps, "steps"):
        for raw in _list(_mapping(step, "steps[]").get("content", []), "content"):
            item = _mapping(raw, "content[]")
            if item.get("type") != TEXT:
                continue
            piece = item.get("text")
            if not isinstance(piece, str):
                raise GeminiTranscribeResponseError("전사 응답의 글 칸 text가 문자열이 아니다")
            pieces.append(piece)
    return " ".join(piece for piece in pieces if piece.strip())


def _fraction(value: Fraction | float) -> Fraction:
    return value if isinstance(value, Fraction) else Fraction(repr(float(value)))


def _word_records(steps: object) -> list[Mapping[str, object]]:
    """`steps[].content[].annotations[]` 가운데 `word_info`만 — 모양이 어긋나면 실패한다."""
    if steps is None:
        return []
    records: list[Mapping[str, object]] = []
    for step in _list(steps, "steps"):
        for item in _list(_mapping(step, "steps[]").get("content", []), "content"):
            for note in _list(_mapping(item, "content[]").get("annotations", []), "annotations"):
                if isinstance(note, Mapping) and note.get("type") == WORD_INFO:
                    records.append(note)
    return records


def _list(value: object, where: str) -> Sequence[object]:
    if not isinstance(value, list):
        raise GeminiTranscribeResponseError(f"전사 응답의 {where}가 배열이 아니다")
    return value


def _mapping(value: object, where: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise GeminiTranscribeResponseError(f"전사 응답의 {where}가 JSON 객체가 아니다")
    return value


def _timed(
    record: Mapping[str, object], duration: Fraction, tolerance: Fraction
) -> tuple[ParsedWord, bool]:
    """(단어, 청크 끝으로 닫았는가) — 시각 꼴이 어긋나면 시각 없는 단어다(글자는 남는다)."""
    text = record.get("text")
    if not isinstance(text, str) or not text.strip():
        raise GeminiTranscribeResponseError(f"word_info에 글자가 없다: {record!r}")
    word = unicodedata.normalize("NFC", text.strip())
    start, end = _offset(record.get("start_offset")), _offset(record.get("end_offset"))
    if start is None or end is None or start > end:
        return ParsedWord(word, word, None, None), False
    over = end - duration
    if over <= 0:
        return ParsedWord(word, word, start, end), False
    if over > tolerance:
        raise GeminiTranscribeResponseError(
            f"단어 {word!r}의 끝 {float(end)}초가 청크 길이 {float(duration)}초를"
            " 관용 밖으로 넘는다"
        )
    return ParsedWord(word, word, min(start, duration), duration), True


def _offset(value: object) -> Fraction | None:
    if not isinstance(value, str):
        return None
    match = OFFSET.fullmatch(value)
    return Fraction(match.group(1)) if match else None


def _aligned(words: list[ParsedWord], text: str) -> tuple[tuple[ParsedWord, ...], str, int]:
    """단어를 `output_text`에서 차례로 찾아 사이의 띄어쓰기·부호를 앞 단어에 붙인다.

    사이의 글자 가운데 한글·영문·숫자는 붙이지 않고 센다(자리 없는 글자). 한 단어라도 못 찾으면
    단어를 띄어 이어 붙이고 `FALLBACK`이라 적는다 — 글자는 하나도 버리지 않는다.
    """
    starts: list[int] = []
    cursor = 0
    for word in words:
        found = text.find(word.text, cursor)
        if found < 0:
            return tuple(_with(w, f"{w.text} ") for w in words), FALLBACK, 0
        starts.append(found)
        cursor = found + len(word.text)
    unplaced = 0
    shaped: list[ParsedWord] = []
    for index, word in enumerate(words):
        head = "" if index else _separators(text[: starts[0]])
        tail_end = starts[index + 1] if index + 1 < len(words) else len(text)
        gap = text[starts[index] + len(word.text) : tail_end]
        dropped = _letters(gap) + (_letters(text[: starts[0]]) if not index else 0)
        unplaced += dropped
        shaped.append(_with(word, head + word.text + _separators(gap), dropped))
    return tuple(shaped), ALIGNED, unplaced


def _with(word: ParsedWord, display: str, dropped: int = 0) -> ParsedWord:
    return ParsedWord(word.text, display, word.start, word.end, dropped)


def _is_letter(char: str) -> bool:
    return unicodedata.category(char)[0] in ("L", "N")


def _letters(gap: str) -> int:
    return sum(_is_letter(char) for char in gap)


def _separators(gap: str) -> str:
    """사이 글의 앞쪽 띄어쓰기·부호 — 자리 없는 글자가 나오면 거기서 멈춘다(그 뒤는 세기만)."""
    kept = []
    for char in gap:
        if _is_letter(char):
            break
        kept.append(char)
    return "".join(kept)
