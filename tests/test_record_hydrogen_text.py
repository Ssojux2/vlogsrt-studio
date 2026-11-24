"""사람 전용 Hydrogen 글자 귀 녹화 스크립트(W-114b) — 확인 없이는 보내지 않는다.

시험은 `_send`에 닿지 않는다 — 닿으면 실패하는 가짜를 앉힌다. 유료 호출은 없다.
"""

from __future__ import annotations

import importlib.util
import json
import struct
import wave
from datetime import date
from fractions import Fraction
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from vlogsrt.project.raw.config import HYDROGEN_TEXT

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "record_hydrogen_text.py"
TRANSCRIBE = Path(__file__).resolve().parent / "fixtures" / "synthetic" / "gemini"
ON = date(2026, 11, 21)


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_hydrogen_text", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wav(path: Path, seconds: int = 30) -> Path:
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(struct.pack("<h", 0) * 16_000 * seconds)
    return path


def _argv(tmp_path: Path, *extra: str) -> list[str]:
    wav = _wav(tmp_path / "c.wav")
    transcribe = str(TRANSCRIBE / "transcribe_korean.json")
    return [str(wav), "--transcribe", transcribe, "--out", str(tmp_path / "rec.json"), *extra]


def test_without_confirmation_it_only_quotes_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    monkeypatch.setattr(script, "_send", lambda *_: pytest.fail("확인 없이 보냈다"))
    assert script.main(_argv(tmp_path), today=ON) == 2
    assert not (tmp_path / "rec.json").exists()


def test_an_existing_output_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    monkeypatch.setattr(script, "_send", lambda *_: pytest.fail("덮으려고 보냈다"))
    (tmp_path / "rec.json").write_text("{}", encoding="utf-8")
    assert script.main(_argv(tmp_path, "--confirm-cost"), today=ON) == 2
    assert (tmp_path / "rec.json").read_text(encoding="utf-8") == "{}"


def test_an_expired_price_never_sends_even_with_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    monkeypatch.setattr(script, "_send", lambda *_: pytest.fail("가격 미정인데 보냈다"))
    assert script.main(_argv(tmp_path, "--confirm-cost"), today=date(2027, 6, 1)) == 2


def test_lines_come_from_the_transcribe_reply_with_order_ids() -> None:
    script = _script()
    reply = json.loads((TRANSCRIBE / "transcribe_korean.json").read_text(encoding="utf-8"))
    lines = script.chunk_lines(reply, Fraction(30))
    assert [line.line_id for line in lines] == [str(n) for n in range(len(lines))]
    assert lines and all(line.start < line.end and line.draft for line in lines)
    text, priced = script.quote(
        script.hio.payload(b"RIFF", lines, HYDROGEN_TEXT), HYDROGEN_TEXT, ON
    )
    assert not priced and text.startswith("가격 미정")  # 읽지 못한 WAV는 잴 수 없다


def test_a_confirmed_run_writes_the_sent_lines_and_the_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    sent: list[dict[str, Any]] = []

    def fake_send(body: dict[str, Any], settings: Any) -> dict[str, Any]:
        sent.append(body)
        rows = json.loads(body["contents"][0]["parts"][2]["text"])["lines"]
        items = [{"id": row["id"], "ko": row["draft"], "speech": True} for row in rows]
        text = json.dumps({"items": items, "extra": []}, ensure_ascii=False)
        return {"candidates": [{"content": {"parts": [{"text": text}]}, "finishReason": "STOP"}]}

    monkeypatch.setattr(script, "_send", fake_send)
    assert script.main(_argv(tmp_path, "--confirm-cost"), today=ON) == 0
    document = json.loads((tmp_path / "rec.json").read_text(encoding="utf-8"))
    assert document["schema"] == script.RECORDING_SCHEMA
    assert document["model"] == HYDROGEN_TEXT.model
    assert [row["id"] for row in document["lines"]] == [
        str(n) for n in range(len(document["lines"]))
    ]
    assert len(sent) == 1 and document["response"]["candidates"]
