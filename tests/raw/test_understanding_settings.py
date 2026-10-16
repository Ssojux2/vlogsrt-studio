"""A4 이해 설정 — 계획 §15.1의 초기 기준이 그대로 얼어 있는지 본다.

창 15초·문맥 2초·창당 8장·긴변 768은 문서의 초기 기준값이다. ASR 청크 상한은
엔진의 동결 상한(180초·24MB)을 넘지 못한다 — 이 설정을 올리는 것은 D26·D28이
닫은 길을 다시 여는 시도다.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from vlogsrt.project.raw.analysis_config import (
    MAX_ASR_CHUNK_BYTES,
    MAX_ASR_CHUNK_SECONDS,
    UNDERSTANDING_SCHEMA_VERSION,
    RawUnderstandingSettings,
    understanding_settings_problems,
    validate_understanding_settings,
)


def test_defaults_pin_the_plan_initial_baseline() -> None:
    """기본값은 계획 §15.1 그대로다 — 창 15초·문맥 2초·8장·긴변 768."""
    settings = RawUnderstandingSettings()
    assert settings.window_seconds == 15.0
    assert settings.context_seconds == 2.0
    assert settings.max_images_per_window == 8
    assert settings.image_long_edge == 768
    assert settings.schema_version == UNDERSTANDING_SCHEMA_VERSION
    assert settings.prompt_revision
    validate_understanding_settings(settings)


@pytest.mark.parametrize(
    "settings",
    [
        replace(RawUnderstandingSettings(), window_seconds=0.0),
        replace(RawUnderstandingSettings(), window_seconds=-15.0),
        replace(RawUnderstandingSettings(), window_seconds=float("nan")),
        replace(RawUnderstandingSettings(), window_seconds=float("inf")),
        replace(RawUnderstandingSettings(), window_seconds=True),
        replace(RawUnderstandingSettings(), window_seconds="15"),  # type: ignore[arg-type]
        replace(RawUnderstandingSettings(), context_seconds=-2.0),
        replace(RawUnderstandingSettings(), context_seconds=15.5),  # 창보다 길다
        replace(RawUnderstandingSettings(), max_images_per_window=0),
        replace(RawUnderstandingSettings(), max_images_per_window=True),
        replace(RawUnderstandingSettings(), image_long_edge=0),
        replace(RawUnderstandingSettings(), image_long_edge=768.0),  # type: ignore[arg-type]
        replace(RawUnderstandingSettings(), prompt_revision="  "),
        replace(RawUnderstandingSettings(), schema_version="raw-understanding/v2"),
    ],
)
def test_broken_settings_are_rejected_whole(settings: RawUnderstandingSettings) -> None:
    """값역 밖 설정은 전부 한 문제 목록으로 나온다 — 조용히 고쳐지지 않는다."""
    problems = understanding_settings_problems(settings)
    assert problems
    with pytest.raises(ValueError, match="계약을 어긴다"):
        validate_understanding_settings(settings)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("asr_chunk_max_seconds", 180.1),
        ("asr_chunk_max_seconds", 600.0),
        ("asr_chunk_max_bytes", MAX_ASR_CHUNK_BYTES + 1),
    ],
)
def test_asr_chunk_ceiling_is_frozen(field: str, value: object) -> None:
    """청크 상한은 동결 상한 이하만 가능 — 올리는 설정은 만들어지지 않는다."""
    settings = replace(RawUnderstandingSettings(), **{field: value})
    assert understanding_settings_problems(settings)


def test_lowering_chunk_bounds_is_allowed() -> None:
    """낮추는 것은 회차별 조정이다 — 허용한다."""
    settings = replace(
        RawUnderstandingSettings(), asr_chunk_max_seconds=60.0, asr_chunk_max_bytes=8_000_000
    )
    validate_understanding_settings(settings)
    assert MAX_ASR_CHUNK_SECONDS == 180.0 and MAX_ASR_CHUNK_BYTES == 24_000_000
