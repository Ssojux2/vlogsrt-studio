"""시험용 Gemini 3.5 Transcribe 시간 엔진 — whisper 모양 세그먼트를 단어 응답으로 돌려준다(D163).

whisper가 은퇴해 실제 1단계는 Gemini 문으로만 듣는다. 예전 시험은 whisper 응답 모양의 가짜
전송(`segments`)으로 실제 호출 경로를 쟀다 — 이 가짜는 같은 세그먼트를 Gemini `word_info`로 옮겨
같은 경로를 Gemini로 잰다. 세그먼트 하나가 낱말 몇 개가 되고, 낱말은 모두 그 세그먼트의 시각을
가진다(시각을 나누지 않는다 — 단어 묶기는 그 시각을 그대로 쓴다). 보낸 요청은 `calls`에 쌓인다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from fractions import Fraction
from typing import Any

from vlogsrt.gemini_messages import GeminiClient


class _Reply:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def _seconds(value: object) -> str:
    """`1.5` → `"1.500s"` — 공급자의 Duration 글자 모양."""
    exact = Fraction(str(value))
    return f"{float(exact):.3f}s"


def interaction_body(segments: Sequence[Mapping[str, object]]) -> dict[str, Any]:
    """세그먼트 → Gemini 전사 응답(Interactions) 한 벌."""
    words: list[dict[str, str]] = []
    texts: list[str] = []
    for segment in segments:
        text = str(segment.get("text", "")).strip()
        if not text:
            continue
        texts.append(text)
        for token in text.split():
            words.append(
                {
                    "type": "word_info",
                    "text": token,
                    "start_offset": _seconds(segment["start"]),
                    "end_offset": _seconds(segment["end"]),
                }
            )
    output = " ".join(texts)
    return {
        "id": "int_synthetic_echo",
        "status": "completed",
        "model": "gemini-3.5-transcribe",
        "output_text": output,
        "steps": [
            {
                "type": "model_output",
                "content": [{"type": "text", "text": output, "annotations": words}],
            }
        ],
        "usage": {"total_input_tokens": 100, "total_output_tokens": 10},
    }


class GeminiEcho:
    """가짜 opener — 요청을 세고 정해진 세그먼트를 Gemini 응답으로 돌려준다."""

    def __init__(self, segments: Sequence[Mapping[str, object]] | None = None) -> None:
        self.segments: list[Mapping[str, object]] = list(segments or ())
        self.calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        self.calls.append(json.loads(request.data))
        body = json.dumps(interaction_body(self.segments), ensure_ascii=False)
        return _Reply(body.encode("utf-8"))

    def client(self) -> GeminiClient:
        return GeminiClient(
            api_key="AIza-synthetic",
            base_url="https://generativelanguage.googleapis.com",
            api_version="v1beta",
            timeout_seconds=30.0,
            opener=self,
        )
