"""배분 — 묶음이 돌려준 글자를 발화 구간에 나눠 담는다.

**시각을 만들지 않는다**(불변 규칙 3). 구간은 이미 `spans.py`가 정했고, 여기서는
어느 구간에 어느 어절이 들어가느냐만 정한다.

한국어는 음절 문자라 **글자 수 ∝ 발화 시간**이 대체로 성립한다(D24). 어절을
쪼개지 않으므로 단어가 두 자막에 걸치지 않는다.

순수 함수다. 네트워크도 ffmpeg도 쓰지 않는다.
"""

from __future__ import annotations

from collections.abc import Sequence

from .models import Segment, Span


def allocate(text: str, spans: Sequence[Span], seg_id_base: int) -> list[Segment]:
    """글자를 구간 길이에 비례해 배분한다. **어절은 하나도 버리지 않는다**(규칙 9).

    구간마다 독립적으로 반올림하면 오차가 뒤로 쌓이므로, 누적 길이 비율로 누적
    목표 글자 수를 잡는다. 마지막 구간은 남은 어절을 전부 받는다.
    """
    words = text.split()
    if not words or not spans:
        return []
    total = sum(max(0.0, end - start) for start, end in spans)
    if total <= 0:  # 길이 0짜리 구간뿐이면 나눌 기준이 없다. 첫 구간에 몰아준다.
        return [Segment(id=seg_id_base, start=spans[0][0], end=spans[0][1], text=" ".join(words))]

    letters = sum(len(word) for word in words)
    segments: list[Segment] = []
    cursor = 0
    taken = 0
    elapsed = 0.0
    for position, (start, end) in enumerate(spans):
        elapsed += max(0.0, end - start)
        target = letters * elapsed / total
        last = position == len(spans) - 1
        picked: list[str] = []
        while cursor < len(words) and (taken < target or last):
            picked.append(words[cursor])
            taken += len(words[cursor])
            cursor += 1
        if picked:
            segments.append(
                Segment(
                    id=seg_id_base + len(segments),
                    start=start,
                    end=end,
                    text=" ".join(picked),
                )
            )
    return segments
