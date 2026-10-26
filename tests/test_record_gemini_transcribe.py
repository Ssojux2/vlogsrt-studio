"""사람 전용 Gemini 3.5 Transcribe 녹화 스크립트(W-31 H1) — 확인 없이는 보내지 않는다."""

from __future__ import annotations

import importlib.util
import struct
import wave
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "record_gemini_transcribe.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("record_gemini_transcribe", SCRIPT)
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


def test_without_confirmation_it_only_quotes_and_sends_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    monkeypatch.setattr(script, "_send", lambda *_: pytest.fail("확인 없이 보냈다"))
    out = tmp_path / "reply.json"
    assert script.main([str(_wav(tmp_path / "c.wav")), "--out", str(out)]) == 2
    assert not out.exists()


def test_an_existing_output_is_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    monkeypatch.setattr(script, "_send", lambda *_: pytest.fail("덮으려고 보냈다"))
    out = tmp_path / "reply.json"
    out.write_text("{}", encoding="utf-8")
    argv = [str(_wav(tmp_path / "c.wav")), "--out", str(out), "--confirm-cost"]
    assert script.main(argv) == 2 and out.read_text(encoding="utf-8") == "{}"


def test_the_quote_is_the_per_minute_price_and_closes_after_the_recheck_date() -> None:
    script = _script()
    assert script.estimate_usd(60.0, date(2026, 10, 26)) == Decimal("0.005")
    assert script.estimate_usd(60.0, date(2027, 6, 1)) is None


def test_with_confirmation_the_reply_is_written_as_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = _script()
    sent: list[tuple[Path, float]] = []

    def fake_send(path: Path, seconds: float) -> dict[str, object]:
        sent.append((path, seconds))
        return {"status": "completed", "output_text": "안녕"}

    monkeypatch.setattr(script, "_send", fake_send)
    wav = _wav(tmp_path / "c.wav", seconds=2)
    out = tmp_path / "fixtures" / "reply.json"
    argv = [str(wav), "--out", str(out), "--confirm-cost"]
    assert script.main(argv, today=date(2026, 10, 26)) == 0
    assert sent == [(wav, 2.0)]
    assert '"output_text": "안녕"' in out.read_text(encoding="utf-8")
