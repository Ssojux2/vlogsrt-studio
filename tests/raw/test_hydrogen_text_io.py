"""W-114b(D187) — Hydrogen 글자 귀의 요청·해석: 같은 청크 WAV + 줄 목록 + 초안 → 줄마다 글자.

시험이 못 박는 것:

- 요청 본문은 안내문 · 인라인 WAV · 줄 목록(`id`·청크 국소 `start`/`end`·`draft`) 셋이고,
  응답 schema가 JSON을 강제한다. 줄 시각은 설정 자릿수로 반올림한 값이고 같은 입력이면
  바이트가 같다(요청 신원).
- 응답 schema에도 안내문에도 시각 칸이 없다 — Hydrogen은 시간 엔진이 아니다(규칙 1).
- 인라인 한도(20 MB)를 넘는 본문은 보내기 전에 닫는다.
- 상한은 실제 본문으로 잰다 — WAV 머리 길이의 올림 초 × 초당 32토큰 + 글자 바이트 + schema +
  부대 여유.
- 본문은 Gemini 문(`transcribe.request_gemini_generate`)을 그대로 지난다(맨몸·관찰자 둘 다).
- 응답 읽기는 늘 계획을 낸다 — 막힘·출력 한도·STOP 아님·JSON 아님은 청크 전체 초안 + 까닭이다.

유료 호출은 없다 — 전송 자리에 가짜 opener가 앉는다.
"""

from __future__ import annotations

import base64
import io
import json
import math
import struct
import wave
from datetime import date
from fractions import Fraction
from typing import Any

import pytest

from tests.raw.test_gemini_timing_provider import _client, _Reply
from vlogsrt.project.raw import gemini_cost, hydrogen_text
from vlogsrt.project.raw import hydrogen_text_io as hio
from vlogsrt.project.raw.config import (
    GEMINI_HYDROGEN_ANALYSIS_PROFILE,
    HYDROGEN_TEXT,
    HydrogenTextSettings,
)

F = Fraction
ON = date(2026, 11, 21)
LINES = (
    hio.ChunkLine("u0", F(0), F(21, 10), "안녕 하세여"),
    hio.ChunkLine("u1", F(1, 3), F(5), "오늘은 한강에"),
)


HALF_FIVE = F(5, 2)


def _wav(seconds: Fraction = HALF_FIVE, rate: int = 16_000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(rate)
        audio.writeframes(struct.pack("<h", 0) * int(seconds * rate))
    return buffer.getvalue()


def _parts(body: dict[str, Any]) -> list[dict[str, Any]]:
    (turn,) = body["contents"]
    assert turn["role"] == "user"
    parts: list[dict[str, Any]] = turn["parts"]
    return parts


def test_the_payload_is_instruction_inline_wav_and_the_line_list() -> None:
    wav = _wav()
    body = hio.payload(wav, LINES, HYDROGEN_TEXT)
    instruction, audio, rows = _parts(body)
    assert instruction == {"text": hio.INSTRUCTION}
    assert audio == {
        "inlineData": {"mimeType": "audio/wav", "data": base64.b64encode(wav).decode()}
    }
    assert json.loads(rows["text"]) == {
        "lines": [
            {"id": "u0", "start": 0.0, "end": 2.1, "draft": "안녕 하세여"},
            {"id": "u1", "start": 0.333, "end": 5.0, "draft": "오늘은 한강에"},
        ]
    }
    assert body["generationConfig"] == {
        "maxOutputTokens": HYDROGEN_TEXT.max_output_tokens,
        "responseMimeType": "application/json",
        "responseJsonSchema": hio.RESPONSE_SCHEMA,
        "thinkingConfig": {"thinkingLevel": HYDROGEN_TEXT.thinking_level},
    }


def test_the_line_text_is_byte_stable_and_uses_the_configured_decimals() -> None:
    assert hio.request_text(LINES, HYDROGEN_TEXT) == (
        '{"lines":[{"id":"u0","start":0.0,"end":2.1,"draft":"안녕 하세여"},'
        '{"id":"u1","start":0.333,"end":5.0,"draft":"오늘은 한강에"}]}'
    )
    one = HydrogenTextSettings(time_decimals=1)
    assert json.loads(hio.request_text(LINES, one))["lines"][1]["start"] == 0.3
    assert hio.payload(_wav(), LINES, HYDROGEN_TEXT) == hio.payload(_wav(), LINES, HYDROGEN_TEXT)


def _keys(node: object) -> set[str]:
    if isinstance(node, dict):
        return set(node) | {key for value in node.values() for key in _keys(value)}
    if isinstance(node, list):
        return {key for value in node for key in _keys(value)}
    return set()


def test_the_reply_schema_has_no_time_field() -> None:
    keys = _keys(hio.RESPONSE_SCHEMA)
    assert {"items", "extra", "id", "ko", "speech", "after_id"} <= keys
    assert not keys & {"start", "end", "time", "timestamp", "offset"}


def test_an_oversize_body_is_refused_before_sending(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gemini_cost, "INLINE_REQUEST_LIMIT_BYTES", 1000)
    with pytest.raises(ValueError, match="인라인"):
        hio.payload(_wav(), LINES, HYDROGEN_TEXT)


def test_the_bound_counts_audio_seconds_text_and_schema() -> None:
    body = hio.payload(_wav(F(5, 2)), LINES, HYDROGEN_TEXT)
    bound = gemini_cost.payload_bound(body, GEMINI_HYDROGEN_ANALYSIS_PROFILE, on=ON)
    profile = GEMINI_HYDROGEN_ANALYSIS_PROFILE
    text = sum(len(part["text"].encode()) for part in _parts(body) if "text" in part)
    schema_bytes = len(
        json.dumps(hio.RESPONSE_SCHEMA, ensure_ascii=False, separators=(",", ":")).encode()
    )
    audio = math.ceil(F(5, 2)) * profile.audio_tokens_per_second
    assert bound.input_tokens == text + schema_bytes + profile.quote_overhead_tokens + audio
    assert bound.output_tokens == HYDROGEN_TEXT.max_output_tokens


def test_unreadable_wav_cannot_be_bounded() -> None:
    body = hio.payload(_wav(), LINES, HYDROGEN_TEXT)
    _parts(body)[1]["inlineData"]["data"] = base64.b64encode(b"RIFF-not-a-wav").decode()
    with pytest.raises(ValueError, match="잴 수 없는"):
        gemini_cost.payload_bound(body, GEMINI_HYDROGEN_ANALYSIS_PROFILE, on=ON)


def _response(body: object, finish: str = "STOP") -> dict[str, Any]:
    text = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False)
    return {
        "candidates": [
            {
                "content": {"parts": [{"text": "생각", "thought": True}, {"text": text}]},
                "finishReason": finish,
            }
        ],
        "usageMetadata": {"promptTokenCount": 1300, "candidatesTokenCount": 90},
        "modelVersion": f"{GEMINI_HYDROGEN_ANALYSIS_PROFILE.model}-001",
        "responseId": "resp_synthetic",
    }


GOOD = {
    "items": [
        {"id": "u0", "ko": "안녕하세요.", "speech": True},
        {"id": "u1", "ko": "오늘은 한강에 왔어요", "speech": True},
    ],
    "extra": [{"after_id": "u1", "ko": "어 잠깐만"}],
}


def test_a_good_reply_reads_to_hydrogen_lines() -> None:
    plan = hio.read_reply(_response(GOOD), LINES)
    assert plan.accepted
    assert [(row.line_id, row.text, row.source) for row in plan.lines] == [
        ("u0", "안녕하세요.", hydrogen_text.SOURCE_HYDROGEN),
        ("u1", "오늘은 한강에 왔어요", hydrogen_text.SOURCE_HYDROGEN),
    ]
    assert plan.extra == (hydrogen_text.ExtraSpeech("u1", "어 잠깐만"),)


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ({"promptFeedback": {"blockReason": "OTHER"}}, hydrogen_text.REASON_UNREADABLE),
        (_response(GOOD, finish="MAX_TOKENS"), hydrogen_text.REASON_UNREADABLE),
        (_response(GOOD, finish="SAFETY"), hydrogen_text.REASON_UNREADABLE),
        (_response("{not json"), hydrogen_text.REASON_UNREADABLE),
        (_response({"items": GOOD["items"][:1]}), hydrogen_text.REASON_MISSING_ID),
        ("not a mapping", hydrogen_text.REASON_UNREADABLE),
    ],
)
def test_an_unreadable_or_mismatched_reply_keeps_the_draft(response: object, reason: str) -> None:
    plan = hio.read_reply(response, LINES)
    assert not plan.accepted and plan.reason == reason and plan.detail
    assert [(row.line_id, row.text) for row in plan.lines] == [
        (line.line_id, line.draft) for line in LINES
    ]


class _Opener:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        self.sent.append((request.full_url, json.loads(request.data)))
        return _Reply(json.dumps(_response(GOOD), ensure_ascii=False).encode("utf-8"))


class _Ledger:
    def __init__(self) -> None:
        self.trace: list[str] = []

    def authorize(self, meta: Any) -> str:
        self.trace.append("authorize")
        return "ticket"

    def on_response(self, ticket: object, meta: Any, response: Any) -> None:
        self.trace.append("response")

    def on_error(self, ticket: object, meta: Any, error: Exception) -> None:
        self.trace.append("error")


@pytest.mark.parametrize("observed", [False, True])
def test_the_payload_passes_the_gemini_door_and_the_receipt_prices_hydrogen(
    observed: bool,
) -> None:
    from types import SimpleNamespace

    from vlogsrt.transcribe import request_gemini_generate

    opener, ledger = _Opener(), _Ledger()
    body = hio.payload(_wav(), LINES, HYDROGEN_TEXT)
    gates: dict[str, Any] = {"observer": ledger} if observed else {}
    response = request_gemini_generate(
        _client(opener), model=HYDROGEN_TEXT.model, payload=body, online=True, **gates
    )
    ((url, sent),) = opener.sent
    assert url.endswith(f"/models/{HYDROGEN_TEXT.model}:generateContent")
    assert sent == body  # 인라인 오디오 부분이 바이트 그대로 지난다
    assert ledger.trace == (["authorize", "response"] if observed else [])
    assert hio.read_reply(response, LINES).accepted
    meta = SimpleNamespace(model=HYDROGEN_TEXT.model)
    usage = gemini_cost.measure_gemini_usage(meta, response, on=ON)
    assert usage.unpriced_reason is None and usage.cost_usd


def test_the_door_stays_closed_without_online() -> None:
    from vlogsrt.transcribe import request_gemini_generate

    opener = _Opener()
    body = hio.payload(_wav(), LINES, HYDROGEN_TEXT)
    with pytest.raises(RuntimeError, match="online"):
        request_gemini_generate(_client(opener), model=HYDROGEN_TEXT.model, payload=body)
    assert opener.sent == []


def test_settings_reject_unknown_models_and_bad_values() -> None:
    with pytest.raises(ValueError, match="모델"):
        HydrogenTextSettings(model="gemini-0-nothing")
    with pytest.raises(ValueError, match="thinking"):
        HydrogenTextSettings(thinking_level="NONE")
    with pytest.raises(ValueError, match="max_output_tokens"):
        HydrogenTextSettings(max_output_tokens=0)
    with pytest.raises(ValueError, match="time_decimals"):
        HydrogenTextSettings(time_decimals=-1)
    with pytest.raises(ValueError, match="판"):
        HydrogenTextSettings(plan_version="hydrogen-text-plan/v0")
    assert HYDROGEN_TEXT.model == GEMINI_HYDROGEN_ANALYSIS_PROFILE.model
