#!/usr/bin/env python3
"""Gemini 3.5 Transcribe 응답 녹화(W-31 H1, D92) — **사람만 실행한다. 유료다.**

    python scripts/record_gemini_transcribe.py <조각.wav> --out <응답.json> --confirm-cost

1단계 조각 WAV 하나(작업공간 `analysis/<rev>/<src>/asr-chunk-*.wav`)를 시간 엔진의 문
(`transcribe.request_gemini_transcription`)으로 한 번 보내고, 받은 응답을 그대로 JSON 파일로 남긴다
— 해석기의 계약 시험 재료다. 오디오는 인라인 base64 · `store: false` · verbatim · 단어 시각 ·
언어 잠금 `ko-KR`(등록표 `GEMINI_TRANSCRIBE_PROFILE`). 키는 `.env`의 `GEMINI_API_KEY`를 쓰고
화면에 나오지 않는다.

`--confirm-cost` 없이는 견적만 말하고 보내지 않는다. 있는 파일은 덮지 않는다. 에이전트는 이 파일을
실행하지 않는다(CLAUDE.md — 유료 호출은 사람만).
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt import log  # noqa: E402
from vlogsrt.models import Chunk  # noqa: E402
from vlogsrt.project.raw.config import (  # noqa: E402
    GEMINI_TRANSCRIBE_PROFILE,
    gemini_transcribe_language_codes,
    gemini_transcribe_price_per_minute,
)

CONTRACT_LANGUAGES = ("ko",)
"""1단계 계약의 예상 언어 — `RawCaptionContract.languages`의 기본과 같다."""


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Gemini 3.5 Transcribe 응답 녹화(유료, 사람만)")
    parser.add_argument("wav", type=Path, help="1단계 조각 WAV(16 kHz 모노)")
    parser.add_argument("--out", type=Path, required=True, help="응답을 남길 JSON 경로(새 파일)")
    parser.add_argument(
        "--confirm-cost", action="store_true", help="견적을 보고 실제로 보낸다(유료)"
    )
    return parser


def wav_seconds(path: Path) -> float:
    with wave.open(str(path), "rb") as audio:
        return audio.getnframes() / audio.getframerate()


def estimate_usd(seconds: float, today: date) -> Decimal | None:
    """분당 단가 × 길이 — 재확인 기한이 지나면 None(가격 미정)."""
    per_minute = gemini_transcribe_price_per_minute(GEMINI_TRANSCRIBE_PROFILE, today=today)
    if per_minute is None:
        return None
    return per_minute * Decimal(str(seconds)) / Decimal(60)


def _send(path: Path, seconds: float) -> dict[str, Any]:
    from vlogsrt import env
    from vlogsrt.transcribe import (
        GeminiTranscriptionRequest,
        create_gemini_client,
        request_gemini_transcription,
    )

    env.ensure_loaded()
    key = env.get(env.ANALYSIS_GEMINI_KEY_ENV).strip()
    if not key:
        raise SystemExit(f"{env.ANALYSIS_GEMINI_KEY_ENV}가 없다 — .env에 결제 연결 키를 넣으세요")
    profile = GEMINI_TRANSCRIBE_PROFILE
    client = create_gemini_client(
        api_key=key,
        base_url=profile.api_base_url,
        api_version=profile.api_version,
        timeout_seconds=profile.timeout_seconds,
    )
    request = GeminiTranscriptionRequest(
        model=profile.model,
        chunk=Chunk(path=path, offset=0.0, duration=seconds, index=0),
        language_codes=gemini_transcribe_language_codes(CONTRACT_LANGUAGES),
        max_audio_seconds=profile.max_audio_seconds_with_word_times,
        max_request_bytes=profile.inline_request_max_bytes,
        mode=profile.mode,
        granularities=profile.granularities,
    )
    return request_gemini_transcription(client, request=request, online=True, stem=path.stem)


def main(argv: list[str] | None = None, *, today: date | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.out.exists():
        log.error(f"{args.out}이 이미 있다 — 덮지 않는다")
        return 2
    seconds = wav_seconds(args.wav)
    cost = estimate_usd(seconds, today or date.today())
    shown = "가격 미정(재확인 기한 지남)" if cost is None else f"최대 약 ${cost:.4f}"
    log.out(f"{args.wav.name}: {seconds:.1f}초 · {GEMINI_TRANSCRIBE_PROFILE.label} · {shown}")
    if not args.confirm_cost or cost is None:
        log.out("보내지 않았다 — 견적을 확인했으면 --confirm-cost를 붙여 다시 실행하세요")
        return 2
    reply = _send(args.wav, seconds)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(reply, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    log.out(f"응답을 남겼다: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
