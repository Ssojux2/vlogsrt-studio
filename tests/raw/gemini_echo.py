"""시험용 Gemini 3.5 Transcribe 시간 엔진 — whisper 모양 세그먼트를 단어 응답으로 돌려준다(D163).

whisper가 은퇴해 실제 1단계는 Gemini 문으로만 듣는다. 예전 시험은 whisper 응답 모양의 가짜
전송(`segments`)으로 실제 호출 경로를 쟀다 — 이 가짜는 같은 세그먼트를 Gemini `word_info`로 옮겨
같은 경로를 Gemini로 잰다. 세그먼트 하나가 낱말 몇 개가 되고, 낱말은 모두 그 세그먼트의 시각을
가진다(시각을 나누지 않는다 — 단어 묶기는 그 시각을 그대로 쓴다). 보낸 전사 요청은 `calls`에 쌓인다.

Hydrogen 글자 귀(D187)의 generateContent 요청은 `text_calls`에 따로 쌓이고, 받은 줄마다 초안 글자를
그대로 돌려준다 — 전사 수를 세는 시험이 그대로 전사 수를 센다.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt.gemini_messages import GeminiClient
from vlogsrt.project.raw.config import HYDROGEN_TEXT


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


def hydrogen_echo(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Hydrogen 글자 귀 요청 → 받은 줄마다 초안 글자를 그대로 들은 응답(generateContent) 한 벌."""
    rows = json.loads(payload["contents"][0]["parts"][2]["text"])["lines"]
    items = [{"id": row["id"], "ko": row["draft"], "speech": True} for row in rows]
    text = json.dumps({"items": items, "extra": []}, ensure_ascii=False)
    return {
        "candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}],
        "usageMetadata": {"promptTokenCount": 100, "candidatesTokenCount": 10},
        "modelVersion": f"{HYDROGEN_TEXT.model}-001",
    }


class GeminiEcho:
    """가짜 opener — 요청을 세고 정해진 세그먼트를 Gemini 응답으로 돌려준다."""

    def __init__(self, segments: Sequence[Mapping[str, object]] | None = None) -> None:
        self.segments: list[Mapping[str, object]] = list(segments or ())
        self.calls: list[dict[str, Any]] = []
        self.text_calls: list[dict[str, Any]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        payload = json.loads(request.data)
        if ":generateContent" in request.full_url:
            self.text_calls.append(payload)
            return _Reply(json.dumps(hydrogen_echo(payload), ensure_ascii=False).encode("utf-8"))
        self.calls.append(payload)
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


def write_utterance_replay(
    directory: Path, sources: Mapping[str, Sequence[Mapping[str, object]]]
) -> None:
    """원본 sha → 발화 선언(원본 오디오 시계 초)을 Gemini 재생 선언으로 쓴다.

    선언 파일은 `word-text-fixtures.json`이고 whisper 모양 재생 선언(`asr-fixtures.json`)을
    대신한다(D163). 시험 원본은 Gemini 청크 한도보다 짧아 청크 하나(0번)이므로 원본 시계가 곧
    청크 시계다 — 청크가 더 있으면 그 청크는 선언이 없어 보이는
    실패다(지어내지 않는다). 이미 있는 선언 파일의 다른 원본은 그대로 둔다.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "word-text-fixtures.json"
    body: dict[str, Any] = {"sources": {}}
    if path.is_file():
        body = json.loads(path.read_text(encoding="utf-8"))
        body.setdefault("sources", {})
    for sha, utterances in sources.items():
        body["sources"][sha] = {"0": interaction_body(utterances)}
    path.write_text(json.dumps(body, ensure_ascii=False), encoding="utf-8")
