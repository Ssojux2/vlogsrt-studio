#!/usr/bin/env python3
"""Hydrogen 글자 귀 응답 녹화(W-114b, D187) — **사람만 실행한다. 유료다.**

    python scripts/record_hydrogen_text.py <조각.wav> --transcribe <전사응답.json> \\
        --out <녹화.json> --confirm-cost

1단계 조각 WAV 하나와 같은 조각의 Gemini 3.5 Transcribe 응답(`record_gemini_transcribe.py`가 남긴
파일)으로 줄 목록을 짓고(`word_cues.group` — 1단계와 같은 줄 끊기, 아이디는 차례 번호), 그 WAV ·
줄 · 초안을 Hydrogen 글자 귀의 문(`transcribe.request_gemini_generate`)으로 한 번 보낸다. 보낸 줄과
받은 응답을 그대로 JSON 파일 하나로 남기고, 줄 계획(받음·까닭·바뀐 줄 수)을 말한다 — 요청 모양과
품질을 사람이 확인하는 재료다. 키는 `.env`의 `GEMINI_API_KEY`를 쓰고 화면에 나오지 않는다.

`--confirm-cost` 없이는 상한만 말하고 보내지 않는다. 있는 파일은 덮지 않는다. 에이전트는 이
파일을 실행하지 않는다(CLAUDE.md — 유료 호출은 사람만).
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt import log  # noqa: E402
from vlogsrt.project.raw import gemini_cost, word_cues  # noqa: E402
from vlogsrt.project.raw import hydrogen_text_io as hio  # noqa: E402
from vlogsrt.project.raw.config import (  # noqa: E402
    GEMINI_ANALYSIS_PROFILES,
    HYDROGEN_TEXT,
    HydrogenTextSettings,
)
from vlogsrt.project.raw.gemini_transcribe_parse import parse_transcription  # noqa: E402

RECORDING_SCHEMA = "hydrogen-text-recording/v1"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hydrogen 글자 귀 응답 녹화(유료, 사람만)")
    parser.add_argument("wav", type=Path, help="1단계 조각 WAV(16 kHz 모노)")
    parser.add_argument(
        "--transcribe", type=Path, required=True, help="같은 조각의 Transcribe 응답 JSON"
    )
    parser.add_argument("--out", type=Path, required=True, help="녹화를 남길 JSON 경로(새 파일)")
    parser.add_argument(
        "--confirm-cost", action="store_true", help="상한을 보고 실제로 보낸다(유료)"
    )
    return parser


def wav_seconds(path: Path) -> Fraction:
    with wave.open(str(path), "rb") as audio:
        return Fraction(audio.getnframes(), audio.getframerate())


def chunk_lines(transcribe_reply: object, seconds: Fraction) -> tuple[hio.ChunkLine, ...]:
    """Transcribe 응답 → 1단계와 같은 줄(청크 국소 시각) — 아이디는 차례 번호다."""
    parsed = parse_transcription(transcribe_reply, chunk_duration=seconds)
    cues = word_cues.group(parsed.words).cues
    return tuple(
        hio.ChunkLine(str(number), cue.start, cue.end, cue.text) for number, cue in enumerate(cues)
    )


def quote(body: dict[str, object], settings: HydrogenTextSettings, today: date) -> tuple[str, bool]:
    """보내기 전 상한 — (보일 글, 매겼는가). 단가가 그날 유효하지 않으면 가격 미정이다."""
    profile = GEMINI_ANALYSIS_PROFILES[settings.model]
    try:
        bound = gemini_cost.payload_bound(body, profile, on=today)
    except ValueError as error:
        return f"가격 미정({error})", False
    tokens = f"입력 {bound.input_tokens} · 출력 {bound.output_tokens}토큰"
    return f"최대 약 ${bound.usd:.4f} ({tokens})", True


def _send(body: dict[str, object], settings: HydrogenTextSettings) -> dict[str, Any]:
    from vlogsrt import env
    from vlogsrt.transcribe import create_gemini_client, request_gemini_generate

    env.ensure_loaded()
    key = env.get(env.ANALYSIS_GEMINI_KEY_ENV).strip()
    if not key:
        raise SystemExit(f"{env.ANALYSIS_GEMINI_KEY_ENV}가 없다 — .env에 결제 연결 키를 넣으세요")
    profile = GEMINI_ANALYSIS_PROFILES[settings.model]
    client = create_gemini_client(
        api_key=key,
        base_url=profile.api_base_url,
        api_version=profile.api_version,
        timeout_seconds=profile.timeout_seconds,
    )
    return request_gemini_generate(client, model=settings.model, payload=body, online=True)


def recording(
    lines: tuple[hio.ChunkLine, ...], reply: dict[str, Any], settings: HydrogenTextSettings
) -> dict[str, Any]:
    return {
        "schema": RECORDING_SCHEMA,
        "model": settings.model,
        "prompt_revision": settings.prompt_revision,
        "plan_version": settings.plan_version,
        "lines": json.loads(hio.request_text(lines, settings))["lines"],
        "response": reply,
    }


def _report(lines: tuple[hio.ChunkLine, ...], reply: dict[str, Any]) -> None:
    plan = hio.read_reply(reply, lines)
    changed = sum(row.text != line.draft for row, line in zip(plan.lines, lines, strict=True))
    verdict = "받음" if plan.accepted else f"받지 않음({plan.reason}: {plan.detail})"
    log.out(f"줄 계획: {verdict} · 바뀐 줄 {changed}/{len(lines)} · 줄 밖 말 {len(plan.extra)}")


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.out.exists():
        log.error(f"{args.out}이 이미 있다 — 덮지 않는다")
        return 2
    settings = HYDROGEN_TEXT
    seconds = wav_seconds(args.wav)
    lines = chunk_lines(json.loads(args.transcribe.read_text(encoding="utf-8")), seconds)
    body = hio.payload(args.wav.read_bytes(), lines, settings)
    shown, priced = quote(body, settings, today or date.today())
    log.out(
        f"{args.wav.name}: {float(seconds):.1f}초 · 줄 {len(lines)} · {settings.model} · {shown}"
    )
    if not args.confirm_cost or not priced:
        log.out("보내지 않았다 — 상한을 확인했으면 --confirm-cost를 붙여 다시 실행하세요")
        return 2
    reply = _send(body, settings)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    document = recording(lines, reply, settings)
    args.out.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.out(f"녹화를 남겼다: {args.out}")
    _report(lines, reply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
