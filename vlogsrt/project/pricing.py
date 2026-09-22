"""승인에 묶이는 공식 가격의 정본. 2026-09-21 공개 요금표의 정규 스냅샷.

모델 리터럴은 계약상 `vlogsrt/config.py`·`vlogsrt/project/config.py`에만 살 수
있어서 엔진 모델 이름은 `Config()` 기본값에서 읽는다(불변 규칙 §7). 가격 값은
공식 문서가 정본이다 — 이 모듈은 그 값의 정규 스냅샷과 `snapshot_revision`를
제공할 뿐, 사용자 예산 가정(`ProjectConfig`의 `*_cost_per_minute`)과는 무관하다.
"""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.project.workflow_protocol import PricingSnapshot

PRICING_EFFECTIVE_DATE = "2026-09-21"
PRICING_DOCUMENTATION_URLS = (
    "https://developers.openai.com/api/docs/models/gpt-5.6-luna",
    "https://developers.openai.com/api/docs/pricing",
    "https://developers.openai.com/api/docs/models/whisper-1",
    "https://developers.openai.com/api/docs/guides/images-vision",
)
LUNA_MODEL = "gpt-5.6-luna"
LUNA_INPUT_PER_MILLION_TOKENS = "0.20"
LUNA_OUTPUT_PER_MILLION_TOKENS = "1.20"


def official_pricing_snapshot(luna_model: str = LUNA_MODEL) -> PricingSnapshot:
    """2026-09-21 Standard/global 공개 가격의 정규 스냅샷(USD).

    whisper-1 $0.006/분, gpt-transcribe $0.0045/분, Luna 입력 $0.20·출력
    $1.20/MTok. `snapshot_revision`가 같으면 같은 가격에 대한 같은 승인이다.
    """
    engine = Config()
    return PricingSnapshot.create(
        effective_date=PRICING_EFFECTIVE_DATE,
        currency="USD",
        processing_region="global",
        service_tier="standard",
        luna_model=luna_model,
        luna_input_per_million_tokens=LUNA_INPUT_PER_MILLION_TOKENS,
        luna_output_per_million_tokens=LUNA_OUTPUT_PER_MILLION_TOKENS,
        whisper_model=engine.transcribe_model,
        whisper_per_minute="0.006",
        gpt_transcribe_model=engine.verify_model,
        gpt_transcribe_per_minute="0.0045",
        documentation_urls=PRICING_DOCUMENTATION_URLS,
    )
