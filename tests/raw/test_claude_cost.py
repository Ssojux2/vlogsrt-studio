"""W-14 / D54 — Claude 의미 분석의 보수적 비용 상한과 그 상한을 지키는 실행 문맥.

상한은 공식 단가 스냅샷(2026-10-09)의 산술일 뿐 청구서가 아니다. 토큰 수는 UTF-8 바이트
이하라는 가정 위에 서 있고(첫 사람 실행의 영수증이 확인한다), 모양을 모르는 페이로드는
상한을 잴 수 없으니 닫힌다. 문맥은 모든 창의 상한이 승인 상한 이하일 때만 만들어진다 —
아니면 전송 0회.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_claude_payload import (
    CLAUDE,
    LUNA,
    claude_client,
    claude_run,
    claude_workflow,
    sof_jpeg,
    visual_responder,
)
from tests.raw.test_understanding_luna import SETTINGS, env_online, make_evidence
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.claude_cost import (
    image_token_bound,
    jpeg_dimensions,
    payload_bound,
    per_image_bound,
    window_ceiling_usd,
)
from vlogsrt.project.raw.config import claude_profile
from vlogsrt.project.raw.understanding import FrameSamplingResult
from vlogsrt.project.raw.understanding_luna import (
    PRICE_OFFICIAL,
    PRICE_UNKNOWN,
    PRICE_USER_SUPPLIED,
    AnalysisApprovalError,
    AnalysisBudget,
    LunaCostEstimate,
    build_semantic_payload,
    estimate_luna_cost,
    luna_output_schema_bytes,
    semantic_analysis_budget,
    validated_luna_responder,
)

PROFILE = claude_profile(CLAUDE)

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg가 없는 환경")


# ─────────────────────────────────────────────────────────────
#  산술 — 창당 상한과 페이로드 하나의 상한
# ─────────────────────────────────────────────────────────────


def test_the_image_bound_is_the_28_pixel_patch_square_capped_by_the_tier() -> None:
    assert per_image_bound(PROFILE, 768) == 784  # ⌈768/28⌉² — 우리 프레임은 줄지 않는다
    assert per_image_bound(PROFILE, 28) == 1
    assert per_image_bound(PROFILE, 29) == 4
    assert per_image_bound(PROFILE, 4000) == 4784  # 고해상도 등급의 이미지당 상한


def test_the_default_window_ceiling_is_the_documented_decimal() -> None:
    """(12000 + 801 + 2000 + 8×784)×$4 + 8192×$20, 100만 토큰당 — 창당 $0.248132."""
    assert luna_output_schema_bytes() == 801
    ceiling = window_ceiling_usd(PROFILE, SETTINGS, schema_bytes=801)
    assert ceiling == Decimal("0.248132")
    budget = semantic_analysis_budget(CLAUDE, SETTINGS, max_luna_requests=40, max_asr_chunks=4)
    assert budget.luna_price_status == PRICE_OFFICIAL
    assert budget.request_ceiling_usd == Decimal("0.248132")
    assert (budget.max_luna_requests, budget.max_asr_chunks) == (40, 4)


def _text_bytes(payload: dict[str, Any]) -> int:
    content = payload["messages"][0]["content"]
    return sum(len(block["text"].encode("utf-8")) for block in content if block["type"] == "text")


def _with_frames(tmp_path: Path, width: int, height: int) -> dict[str, Any]:
    """`make_evidence`의 두 프레임을 SOF 헤더가 있는 JPEG로 바꾼 Claude 페이로드."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    evidence = make_evidence(tmp_path)
    for frame in evidence.frames:
        frame.image_path.write_bytes(sof_jpeg(width, height))
    return build_semantic_payload(evidence, SETTINGS, CLAUDE)


def test_a_payload_bound_counts_text_bytes_schema_overhead_and_real_image_sizes(
    tmp_path: Path,
) -> None:
    payload = _with_frames(tmp_path, 768, 432)
    images = sum(1 for block in payload["messages"][0]["content"] if block["type"] == "image")
    assert images == 2
    bound = payload_bound(payload, PROFILE)
    expected_input = _text_bytes(payload) + 801 + 2000 + 2 * 448  # ⌈768/28⌉×⌈432/28⌉ = 28×16
    assert (bound.input_tokens, bound.output_tokens) == (expected_input, 8192)
    assert bound.usd == (Decimal(expected_input) * 4 + Decimal(8192) * 20) / Decimal(1_000_000)
    assert bound.usd <= window_ceiling_usd(PROFILE, SETTINGS, schema_bytes=801)


def test_image_tokens_come_from_the_jpeg_dimensions_not_the_configured_edge() -> None:
    """설정의 긴 변을 믿지 않는다 — 실제 프레임이 크면 그만큼 비싸다(⌈w/28⌉×⌈h/28⌉, 상한 4784)."""
    assert jpeg_dimensions(sof_jpeg(1920, 1080)) == (1920, 1080)
    assert image_token_bound(PROFILE, sof_jpeg(1920, 1080)) == 2691  # 69 × 39
    assert image_token_bound(PROFILE, sof_jpeg(768, 432)) == 448
    assert image_token_bound(PROFILE, sof_jpeg(4000, 3000)) == 4784  # 고해상도 등급의 상한


def _segment(marker: int, body: bytes) -> bytes:
    """JPEG 세그먼트 하나 — 표지 2바이트, 길이(자신 포함) 2바이트, 본문."""
    return bytes([0xFF, marker]) + (len(body) + 2).to_bytes(2, "big") + body


def _frame_header(width: int, height: int) -> bytes:
    """SOFn·DHP 본문 — 정밀도·세로·가로·성분 하나."""
    return b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big") + b"\x01\x01\x11\x00"


MAIN = sof_jpeg(768, 432)
THUMBNAIL = sof_jpeg(160, 120)


@pytest.mark.parametrize(
    "body",
    [
        MAIN.replace(b"\xff\xc0", b"\xff\xc2", 1),  # SOF2 점진(progressive) — 한 바이트 패치
        MAIN.replace(b"\xff\xc0", b"\xff\xc1", 1),  # SOF1 확장 순차
        # EXIF APP1 안에 썸네일 JPEG(제 SOF0 160×120)가 통째로 있다 — 주 프레임을 읽어야 한다
        MAIN[:2]
        + _segment(0xE1, b"Exif\x00\x00" + b"MM\x00\x2a\x00\x00\x00\x08" + THUMBNAIL)
        + MAIN[2:],
        MAIN[:2] + b"\xff" * 5 + MAIN[2:],  # 표지 앞의 채움 바이트
        MAIN[:2] + b"\xff\xe0\x00\x02" * 3 + MAIN[2:],  # 빈 APP0 세그먼트들
        MAIN[:2] + b"\xff\xd0" + MAIN[2:],  # 홀로 서는 표지(RST0)
    ],
)
def test_the_main_frame_size_is_read_through_every_legal_prefix(body: bytes) -> None:
    assert jpeg_dimensions(body) == (768, 432)
    assert image_token_bound(PROFILE, body) == 448


@pytest.mark.parametrize(
    "body",
    [
        b"\xff\xd8fakejpeg",  # SOF가 없다
        b"not a jpeg at all",
        b"",
        sof_jpeg(1920, 1080)[:-14],  # SOF가 크기 칸 안에서 잘렸다
        sof_jpeg(1920, 1080)[:-6],  # 크기 칸은 있지만 SOF 세그먼트가 끝나지 않았다
        sof_jpeg(0, 1080),  # 폭 0 — 크기를 모른다
        sof_jpeg(1920, 0),  # 높이 0(DNL) — 크기를 모른다
        MAIN[:2] + _segment(0xDA, b"\x00" * 10) + MAIN[2:],  # SOF보다 먼저 SOS가 온다
        MAIN[:2] + b"\xff\xd9" + MAIN[2:],  # SOF보다 먼저 EOI가 온다
        MAIN[:2] + b"\xff\xe0\x00\x00" + MAIN[2:],  # 세그먼트 길이 0
        MAIN[:2] + b"\xff\xe0\x00\x01" + MAIN[2:],  # 세그먼트 길이 1
        MAIN[:2] + b"\xff\xe1\xff\xff" + b"Exif\x00\x00" + MAIN[2:30],  # 길이가 파일 밖이다
        # 계층(hierarchical) JPEG — DHP가 그림 전체 크기를, SOF5~7·13~15가 차분 프레임을 말한다
        b"\xff\xd8"
        + _segment(0xDE, _frame_header(4000, 3000))
        + _segment(0xC5, _frame_header(100, 100))
        + b"\xff\xd9",
        MAIN.replace(b"\xff\xc0", b"\xff\xc5", 1),
        MAIN.replace(b"\xff\xc0", b"\xff\xc7", 1),
        MAIN.replace(b"\xff\xc0", b"\xff\xcd", 1),
        MAIN.replace(b"\xff\xc0", b"\xff\xcf", 1),
    ],
)
def test_an_unreadable_image_is_bounded_at_the_tier_cap(body: bytes) -> None:
    """크기를 못 읽으면 보수적으로 — 이미지당 상한(4784)으로 센다."""
    assert jpeg_dimensions(body) is None
    assert image_token_bound(PROFILE, body) == 4784


def test_a_payload_with_unreadable_or_large_frames_is_bounded_conservatively(
    tmp_path: Path,
) -> None:
    fake = build_semantic_payload(make_evidence(tmp_path), SETTINGS, CLAUDE)
    assert payload_bound(fake, PROFILE).input_tokens == _text_bytes(fake) + 801 + 2000 + 2 * 4784
    large = _with_frames(tmp_path / "large", 1920, 1080)
    assert payload_bound(large, PROFILE).input_tokens == _text_bytes(large) + 801 + 2000 + 2 * 2691
    broken = _with_block(
        fake,
        {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": "!!not-base64!!"},
        },
    )
    assert payload_bound(broken, PROFILE).input_tokens == _text_bytes(broken) + 801 + 2000 + (
        3 * 4784
    )


@needs_ffmpeg
def test_an_encoder_jpeg_is_read_at_its_real_size(tmp_path: Path) -> None:
    """실제 인코더가 쓴 JPEG(APP 세그먼트 뒤의 SOF)도 같은 크기로 읽힌다."""
    target = tmp_path / "frame.jpg"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1920x1080:rate=1:duration=1",
            "-frames:v",
            "1",
            str(target),
        ],
        check=True,
    )
    body = target.read_bytes()
    assert jpeg_dimensions(body) == (1920, 1080)
    assert image_token_bound(PROFILE, body) == 2691


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: {**p, "temperature": 0},
        lambda p: {**p, "inference_geo": "us"},  # US 전용은 1.1배 — 표준 상한으로 못 잰다
        lambda p: {**p, "service_tier": "auto"},  # 싣지 않는 칸 — 모르는 칸은 잴 수 없다
        lambda p: {**p, "service_tier": "standard_only"},
        lambda p: {key: value for key, value in p.items() if key != "inference_geo"},
        lambda p: {**p, "thinking": {"type": "adaptive"}},
        lambda p: {key: value for key, value in p.items() if key != "max_tokens"},
        lambda p: {**p, "max_tokens": 0},
        lambda p: {**p, "messages": []},
        lambda p: {**p, "messages": [{**p["messages"][0], "role": "assistant"}]},
        lambda p: _with_block(p, {"type": "document", "source": {}}),
        lambda p: _with_block(p, {"type": "text", "text": 3}),
        lambda p: _with_block(
            p, {"type": "image", "source": {"type": "url", "url": "https://x.example/a.jpg"}}
        ),
        lambda p: _with_block(
            p,
            {
                "type": "image",
                "source": {"type": "base64", "media_type": "image/png", "data": "AA=="},
            },
        ),
        lambda p: {**p, "output_config": {"effort": "medium"}},
    ],
)
def test_an_unknown_payload_shape_cannot_be_bounded(tmp_path: Path, mutate: Any) -> None:
    payload = build_semantic_payload(make_evidence(tmp_path), SETTINGS, CLAUDE)
    payload_bound(payload, PROFILE)  # 원본은 잴 수 있다 — 바뀐 칸 하나가 닫는다
    with pytest.raises(ValueError):
        payload_bound(mutate(payload), PROFILE)


def _with_block(payload: dict[str, Any], block: dict[str, Any]) -> dict[str, Any]:
    message = payload["messages"][0]
    return {**payload, "messages": [{**message, "content": [*message["content"], block]}]}


# ─────────────────────────────────────────────────────────────
#  예산·추정 — 공식 단가는 Claude만, Luna는 그대로
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        {"luna_price_status": PRICE_OFFICIAL},
        {"luna_price_status": PRICE_OFFICIAL, "request_ceiling_usd": Decimal("0")},
        {"luna_price_status": PRICE_OFFICIAL, "request_ceiling_usd": Decimal("-1")},
        {"luna_price_status": PRICE_OFFICIAL, "request_ceiling_usd": Decimal("NaN")},
        {"luna_price_status": PRICE_OFFICIAL, "request_ceiling_usd": 0.25},
        {"request_ceiling_usd": Decimal("0.25")},
        {
            "luna_price_status": PRICE_USER_SUPPLIED,
            "luna_user_ceiling_per_request": Decimal("1"),
            "request_ceiling_usd": Decimal("0.25"),
        },
    ],
)
def test_an_official_budget_needs_a_finite_positive_ceiling(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        AnalysisBudget(max_luna_requests=1, max_asr_chunks=1, **kwargs)


def test_claude_estimates_are_official_and_luna_estimates_are_unchanged() -> None:
    official = semantic_analysis_budget(CLAUDE, SETTINGS, max_luna_requests=3, max_asr_chunks=1)
    estimate = estimate_luna_cost(3, official)
    assert estimate.price_status == PRICE_OFFICIAL
    assert estimate.ceiling_total == Decimal("0.248132") * 3
    assert estimate.user_ceiling_total is None
    assert "상한" in estimate.note

    unknown = semantic_analysis_budget(LUNA, SETTINGS, max_luna_requests=3, max_asr_chunks=1)
    assert unknown == AnalysisBudget(max_luna_requests=3, max_asr_chunks=1)
    assert estimate_luna_cost(3, unknown) == LunaCostEstimate(
        request_count=3,
        price_status=PRICE_UNKNOWN,
        user_ceiling_total=None,
        note="Luna 단가 미확인 — 금액 추정을 만들지 않는다(0원 계산 금지)",
    )
    assert estimate_luna_cost(3, unknown).ceiling_total is None


# ─────────────────────────────────────────────────────────────
#  실행 문맥 — 공식 예산과 승인 상한 아래의 페이로드만, 아니면 0송신
# ─────────────────────────────────────────────────────────────


class _NoSend:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls += 1
        raise AssertionError("문맥이 서지 않았는데 전송이 일어났다")


def _audio_responder(tmp_path: Path, budget: AnalysisBudget) -> Any:
    audio, plan, utterances, approval = claude_run(tmp_path)
    return validated_luna_responder(
        client=claude_client(),
        settings=claude_workflow(),
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        utterances=utterances,
        frames=FrameSamplingResult((), ()),
        approval=replace(approval, budget=replace(budget, max_luna_requests=len(plan.windows))),
    )


@pytest.mark.parametrize(
    ("budget", "fragment"),
    [
        (AnalysisBudget(max_luna_requests=1, max_asr_chunks=9), "공식 단가"),
        (
            AnalysisBudget(
                max_luna_requests=1,
                max_asr_chunks=9,
                luna_price_status=PRICE_OFFICIAL,
                request_ceiling_usd=Decimal("0.0001"),
            ),
            "상한",
        ),
    ],
)
def test_the_claude_context_refuses_unofficial_or_over_ceiling_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, budget: AnalysisBudget, fragment: str
) -> None:
    env_online(monkeypatch)
    sender = _NoSend()
    monkeypatch.setattr(luna, "request_claude_messages", sender)
    with pytest.raises(AnalysisApprovalError, match=fragment):
        _audio_responder(tmp_path, budget)
    assert sender.calls == 0


def test_the_luna_context_needs_no_official_price(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Luna 승인은 여전히 단가 미확인 예산으로 선다 — 기존 흐름은 그대로다."""
    from tests.raw.test_understanding_luna import matched_approval, online_run, online_workflow

    env_online(monkeypatch)
    audio, plan, utterances, fingerprint = online_run(tmp_path)
    responder = validated_luna_responder(
        client=object(),
        settings=online_workflow(),
        understanding=SETTINGS,
        audio=audio,
        plan=plan,
        utterances=utterances,
        frames=FrameSamplingResult((), ()),
        approval=matched_approval(audio, plan, fingerprint),
    )
    assert responder.approval.budget.luna_price_status == PRICE_UNKNOWN


def test_a_payload_that_cannot_be_bounded_closes_the_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """상한을 잴 수 없는 창이 하나라도 있으면 문맥이 서지 않는다 — 건너뛰고 보내는 길은 없다."""
    env_online(monkeypatch)
    sender = _NoSend()
    monkeypatch.setattr(luna, "request_claude_messages", sender)

    def unmeasurable(*args: Any, **kwargs: Any) -> Any:
        raise ValueError("synthetic unmeasurable payload")

    monkeypatch.setattr(luna, "payload_bound", unmeasurable)
    official = semantic_analysis_budget(CLAUDE, SETTINGS, max_luna_requests=1, max_asr_chunks=9)
    with pytest.raises(AnalysisApprovalError, match="상한을 잴 수 없다"):
        _audio_responder(tmp_path, official)
    assert sender.calls == 0


def test_the_visual_claude_context_refuses_an_over_ceiling_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_online(monkeypatch)
    sender = _NoSend()
    monkeypatch.setattr(luna, "request_claude_messages", sender)
    with pytest.raises(AnalysisApprovalError, match="상한"):
        visual_responder(tmp_path, CLAUDE, claude_client(), ceiling=Decimal("0.0001"))
    assert sender.calls == 0
    ceiling = window_ceiling_usd(PROFILE, SETTINGS, schema_bytes=luna_output_schema_bytes())
    built = visual_responder(tmp_path, CLAUDE, claude_client(), ceiling=ceiling)
    assert built.model == CLAUDE  # 실제 크기(768×432) 프레임이면 승인 상한 아래에서 선다
