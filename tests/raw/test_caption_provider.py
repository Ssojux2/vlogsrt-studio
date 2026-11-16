"""최종 자막 역할별 어댑터(A7-1) — 문구와 시간이 타입으로 갈라져 있는지 본다.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.4·§13.1. 네트워크 없이
가짜 전송 경계(요청 기록·준비된 응답)만으로 돈다 — 실제 호출은 살지 않는다.
"""

from __future__ import annotations

import hashlib
from dataclasses import fields
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.models import Chunk
from vlogsrt.project.raw.caption_provider import (
    AudioChunkBinding,
    TextTranscript,
    audio_chunk_binding,
    parse_timing_response,
)

BEFORE_SHUTDOWN = date(2026, 10, 2)
SHUTDOWN_DAY = date(2027, 2, 26)
TEXT_PAYLOAD: dict[str, Any] = {"text": "망원동 카페", "language": "ko"}
TIMING_PAYLOAD: dict[str, Any] = {
    "segments": [
        {"text": " 망원동", "start": 0.0, "end": 1.5},
        {"text": " 카페", "start": 2.0, "end": 3.5},
    ]
}


def chunk(tmp_path: Path) -> Chunk:
    audio = tmp_path / "final.mp3"
    audio.write_bytes(b"synthetic-audio-bytes")
    return Chunk(audio, 120.0, 12.0, 3)


def binding_of(target: Chunk) -> AudioChunkBinding:
    return audio_chunk_binding(
        target,
        sample_start=480000,
        sample_end=528000,
        sample_rate=4000,
        request_id="run-1",
    )


# ─────────────────────────────────────────────────────────────
#  청크 신원 — 두 경로가 같은 오디오를 들었다는 증명
# ─────────────────────────────────────────────────────────────


def test_binding_hashes_the_actual_chunk_bytes(tmp_path: Path) -> None:
    """해시는 파일 내용에서 온다 — 청크가 바뀌면 신원도 바뀐다."""
    target = chunk(tmp_path)
    binding = binding_of(target)
    assert binding.audio_sha256 == hashlib.sha256(b"synthetic-audio-bytes").hexdigest()
    (tmp_path / "final.mp3").write_bytes(b"different-bytes")
    assert (
        audio_chunk_binding(
            target, sample_start=0, sample_end=10, sample_rate=1, request_id="run-2"
        ).audio_sha256
        != binding.audio_sha256
    )


@pytest.mark.parametrize(
    ("sample_start", "sample_end", "sample_rate", "request_id"),
    [
        (10, 10, 4000, "r"),
        (-1, 10, 4000, "r"),
        (0, 10, 0, "r"),
        (0, 10, 4000, "  "),
        # 좌표는 정확한 정수만 — bool·float은 애노테이션을 넘어 새어 들어온다(루트 리뷰)
        (True, 10, 4000, "r"),
        (0, 10.5, 4000, "r"),
        (0, 10, False, "r"),
    ],
)
def test_binding_rejects_broken_sample_ranges(
    tmp_path: Path,
    sample_start: int,
    sample_end: int,
    sample_rate: int,
    request_id: str,
) -> None:
    """샘플 범위는 [start, end) 반개구간의 정수 좌표다(§13.3) — 깨진 신원은 못 만든다."""
    from vlogsrt.project.raw.caption_provider import InvalidChunkBinding

    with pytest.raises(InvalidChunkBinding):
        audio_chunk_binding(
            chunk(tmp_path),
            sample_start=sample_start,  # type: ignore[arg-type]
            sample_end=sample_end,  # type: ignore[arg-type]
            sample_rate=sample_rate,  # type: ignore[arg-type]
            request_id=request_id,
        )


# ─────────────────────────────────────────────────────────────
#  결과형 — 시각 칸의 유무가 타입으로 갈라진다
# ─────────────────────────────────────────────────────────────


def test_text_transcript_has_no_time_fields() -> None:
    """문구 형에는 시각 칸이 하나도 없다 — 칸을 추가하려는 순간 계약 위반이다."""
    assert {field.name for field in fields(TextTranscript)} == {
        "binding",
        "model",
        "text",
        "detected_language",
        "usage",
        "response",
        "languages",
    }


def test_timed_transcript_carries_provider_coordinates(tmp_path: Path) -> None:
    """세그먼트 시각은 제공자 좌표 그대로 — 오프셋은 곱하지 않고 함께 실린다(§11.6)."""
    binding = binding_of(chunk(tmp_path))
    timed = parse_timing_response(
        TIMING_PAYLOAD,
        binding=binding,
        model="m",
        provider="p",
        chunk_duration=12.0,
        chunk_offset=120.0,
    )
    assert [(segment.start, segment.end) for segment in timed.segments] == [
        (0.0, 1.5),
        (2.0, 3.5),
    ]
    assert timed.chunk_offset == 120.0
    assert timed.time_unit == "seconds"
    assert TIMING_PAYLOAD["segments"][0]["start"] == 0.0  # 원응답은 그대로다


# ─────────────────────────────────────────────────────────────
#  응답 해석 — 꼴이 어긋나면 지어내지 않고 실패한다
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    [
        [],
        "segments",
        {},
        {"segments": "x"},
        {"segments": [{}]},
        {"segments": [{"text": "a"}]},
        {"segments": [{"text": "a", "start": 0.0}]},
        {"segments": [{"text": "a", "start": 0.0, "end": float("nan")}]},
        {"segments": [{"text": "a", "start": True, "end": 1.0}]},
        # §11.11 — 음수·뒤집힘·소유 범위 밖은 고쳐 쓰지 않고 실패다(루트 리뷰). 길이 0과
        # 관용(2초) 안의 넘침은 D55부터 실패가 아니다 — tests/raw/test_timing_fixes.py
        {"segments": [{"text": "음수", "start": -0.5, "end": 1.0}]},
        {"segments": [{"text": "뒤집힘", "start": 4.0, "end": 3.0}]},
        {"segments": [{"text": "범위 밖", "start": 0.0, "end": 14.5}]},
        {"text": "시각 없는 덩어리"},  # json 문구 응답을 시간 응답으로 끼우는 경우
    ],
)
def test_parse_timing_response_rejects_malformed(payload: object, tmp_path: Path) -> None:
    """세그먼트·시각·글자가 빠지거나 소유 범위를 어기면 0으로 메우지 않는다."""
    from vlogsrt.project.raw.caption_provider import MalformedCaptionResponse

    with pytest.raises(MalformedCaptionResponse):
        parse_timing_response(
            payload,
            binding=binding_of(chunk(tmp_path)),
            model="m",
            provider="p",
            chunk_duration=12.0,
        )


@pytest.mark.parametrize(
    ("chunk_offset", "chunk_duration"),
    [(-0.1, 12.0), (float("inf"), 12.0), (0.0, 0.0), (0.0, float("nan"))],
)
def test_parse_timing_rejects_broken_chunk_coordinates(
    tmp_path: Path, chunk_offset: float, chunk_duration: float
) -> None:
    """청크 좌표 자체가 깨진 해석은 시작하지 않는다(루트 리뷰)."""
    from vlogsrt.project.raw.caption_provider import MalformedCaptionResponse

    with pytest.raises(MalformedCaptionResponse):
        parse_timing_response(
            TIMING_PAYLOAD,
            binding=binding_of(chunk(tmp_path)),
            model="m",
            provider="p",
            chunk_duration=chunk_duration,
            chunk_offset=chunk_offset,
        )


def test_parse_timing_response_allows_silence(tmp_path: Path) -> None:
    """빈 segments는 **미확인 빈 골격**이다 — 무발화의 증명으로 바꾸지 않는다(§11.11)."""
    timed = parse_timing_response(
        {"segments": []},
        binding=binding_of(chunk(tmp_path)),
        model="m",
        provider="p",
        chunk_duration=12.0,
    )
    assert timed.segments == ()
    assert timed.unconfirmed_empty is True
    assert timed.binding.audio_sha256  # 신원은 그대로 묶여 있다


# ─────────────────────────────────────────────────────────────
#  요청 빌더 — 지원 조합만 만들 수 있다
# ─────────────────────────────────────────────────────────────


# ─────────────────────────────────────────────────────────────
#  공급자 — 수명주기 검사가 전송보다 먼저다
# ─────────────────────────────────────────────────────────────


def test_offline_history_needs_no_lifecycle_gate(tmp_path: Path) -> None:
    """과거 응답의 재해석은 온라인 문을 지나지 않는다 — 종료일 뒤에도 열린다(§11.10)."""
    binding = binding_of(chunk(tmp_path))
    timed = parse_timing_response(
        TIMING_PAYLOAD,
        binding=binding,
        model="m",
        provider="p",
        chunk_duration=12.0,
        chunk_offset=0.0,
    )
    assert len(timed.segments) == 2  # 수명주기 가드는 온라인 문에만 있다 — 이 길엔 없다
