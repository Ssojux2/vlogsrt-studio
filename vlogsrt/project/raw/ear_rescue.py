"""「다른 귀 기준 재전사」의 판정(W-29b, D78) — 값만 받아 값만 돌려준다.

1단계에서 whisper와 다른 귀(GPT-Transcribe)가 같은 청크 WAV를 듣는다. 다른 귀 글자가 있는데 whisper
청크가 D37 조각 게이트(`retext.plan`의 조각 유사도)를 못 지나면 whisper가 지어냈을 수 있다 — 실측
「시청해주셔서 감사합니다!」 한 줄이 24초를 덮었다. 그런 청크만 whisper에 다시 묻고(**시각은 언제나
whisper 답**, 규칙 1), 다른 귀와 맞는 첫 답을 채택한다. 채택된 답 위에서 D37 게이트가 다른 귀 글자를
얹는다 — 다른 귀 글자를 whisper 시각에 지어 붙이지 않는다.

다른 귀가 아무것도 듣지 못한 청크는 묻지 않는다 — 「이 구간은 말이 아니다」는 판정하지 않는다(규칙
4). 조각 게이트를 다시 쓰므로 임계값은 엔진 `retext_*`·`repair_*` 그대로다(규칙 8).
"""

from __future__ import annotations

from collections.abc import Sequence

from vlogsrt import retext
from vlogsrt.config import Config
from vlogsrt.hangul import strip_for_compare
from vlogsrt.models import Segment

Transcript = Sequence[tuple[float, float, str]]
"""청크 국소 초의 (시작, 끝, 글자) — `AsrChunkOutcome.transcript`와 같은 모양."""


TEMPERATURE_DIGITS = 6
"""온도 사다리의 소수 자리 — 0.8 + 0.2가 1.0000000000000002가 되어 캐시 열쇠가 흔들리지 않게."""


def temperatures(cfg: Config) -> tuple[float, ...]:
    """다시 묻는 온도 차례 — 엔진 되풀이 재전사(D28)와 같은 사다리(0.8 → 1.0). 견적과 실행이 모두
    엔진 기본 설정(`Config()`)으로 잰다(D78)."""
    return tuple(
        round(cfg.repair_temperature + step * cfg.repair_temperature_step, TEMPERATURE_DIGITS)
        for step in range(cfg.repair_max_attempts)
    )


def agreement(
    transcript: Transcript, duration: float, other: str, cfg: Config
) -> tuple[bool, float]:
    """(조각 게이트를 지나는가, 조각 유사도) — 1단계 글자 경로가 쓰는 `retext.plan` 그대로."""
    spine = [
        Segment(id=position, start=start, end=end, text=text)
        for position, (start, end, text) in enumerate(transcript)
    ]
    record = retext.plan(
        spine, [retext.ChunkText(index=0, offset=0.0, text=other)], duration, (), cfg
    )
    if not record.chunks:
        return False, 0.0
    (chunk,) = record.chunks
    return bool(spine) and chunk.accepted, chunk.similarity


def needs_rescue(transcript: Transcript, duration: float, other: str, cfg: Config) -> bool:
    """다른 귀가 말을 들었는데 whisper가 아무것도 못 들었거나 조각 게이트를 못 지났다."""
    if not strip_for_compare(other):
        return False
    trusted, _similarity = agreement(transcript, duration, other, cfg)
    return not trusted
