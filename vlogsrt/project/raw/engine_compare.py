"""시간 엔진 비교(W-31f, D92) — 다시 들은 원본의 새 엔진 자막과 그 앞 리비전을 견준다.

값만 받는 순수 계층이다(파일·DB·프로세스·서비스를 모른다). 읽는 일은
`scripts/engine_compare_report.py`가 한다. 자막을 바꾸지 않고 기본 엔진을 바꾸지 않는다 — 숫자와
판정만 낸다.

- **리비전 고르기** `pick_pair`: 원본 자막 리비전은 엔진을 적지 않는다. 그래서 고정 표가 새
  엔진이라고 한 원본에서, 사람이 고치지 않은(`edited` 거짓) 가장 늦은 리비전을 새 엔진의 전사로,
  바로 앞 차례의 리비전을 견줄 기준(다시 듣기 전에 사람이 보던 자막)으로 본다. 앞 리비전이 없으면
  기준이 없다.
- **시각 정답**: 정답은 whisper 줄에 묶여 찍혔다(D67) — 새 엔진의 줄과는 아이디로 짝지을 수 없다.
  그래서 두 리비전 모두 **가장 가까운 경계**로 잰다(시작 정답 ↔ 가장 가까운 줄 시작, 끝 정답 ↔ 가장
  가까운 줄 끝). 같은 규칙이라 공정하지만 줄이 많을수록 경계도 많다는 점은 줄 수와 함께 읽는다. 기준
  리비전은 예전 보고서와 같은 줄 짝 지표(`timing_truth.pairs`)도 함께 낸다.
- **환각류 「확인 필요」**: 시각 재료 없이 도는 글자 규칙 다섯(같은 말 런 · 작은 어휘 런 · 끝 인사말
  · 다른 문자 · 부호뿐인 줄)이 짚은 줄 수(D59 19e·D77).
- **판정** `verdict`: `EngineCompareSettings`의 선을 새 엔진이 넘는가 — 재료가 없는 기준은 None이다.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any

from vlogsrt.project.raw import subtitle_suspects, timing_truth
from vlogsrt.project.raw.config import ENGINE_COMPARE_SETTINGS, EngineCompareSettings

HALLUCINATION_CODES = (
    subtitle_suspects.REPEAT,
    subtitle_suspects.SMALL_VOCABULARY,
    subtitle_suspects.BOILERPLATE,
    subtitle_suspects.OTHER_SCRIPT,
    subtitle_suspects.SYMBOLS_ONLY,
)
"""환각류 표시 — 시각·말소리 재료 없이 글자만으로 도는 규칙(지어낸 말의 모양)."""

START_HIT = "start_hit"
END_HIT = "end_hit"
START_MAE = "start_mae"
HALLUCINATION = "hallucination"
RECALL = "recall"
CRITERIA = (START_HIT, END_HIT, START_MAE, HALLUCINATION, RECALL)


@dataclass(frozen=True, slots=True)
class RevisionPair:
    """원본 하나의 (기준 리비전, 새 엔진 리비전) — 기준이 없으면 None."""

    baseline: Mapping[str, Any] | None
    engine: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class LineStats:
    lines: int
    chars: int
    seconds: Fraction
    chars_per_second: Fraction | None


def pick_pair(revisions: Sequence[Mapping[str, Any]]) -> RevisionPair | None:
    """리비전들(차례 무관) → 새 엔진 전사와 바로 앞 리비전. 고치지 않은 리비전이 없으면 None."""
    ordered = sorted(revisions, key=lambda row: int(row["sequence"]))
    seeded = [row for row in ordered if not row.get("edited", False)]
    if not seeded:
        return None
    engine = seeded[-1]
    before = [row for row in ordered if int(row["sequence"]) == int(engine["sequence"]) - 1]
    return RevisionPair(before[0] if before else None, engine)


def nearest_pairs(
    marks: Iterable[timing_truth.TruthMark], lines: Sequence[timing_truth.TruthLine]
) -> tuple[timing_truth.TruthPair, ...]:
    """정답마다 가장 가까운 같은 쪽 경계(같으면 앞) — 줄 아이디를 보지 않는다(엔진 무관)."""
    if not lines:
        return ()
    found: list[timing_truth.TruthPair] = []
    for mark in marks:
        start = mark.edge == timing_truth.EDGE_START
        line = min(
            lines,
            key=lambda row: (
                abs((row.start if start else row.end) - mark.seconds),
                row.start if start else row.end,
            ),
        )
        found.append(
            timing_truth.TruthPair(
                mark.asset_id,
                line.cue_id,
                mark.stratum,
                mark.edge,
                mark.seconds,
                line.start if start else line.end,
            )
        )
    return tuple(found)


def hallucination_lines(transcript: Mapping[str, Any]) -> int:
    """환각류 표시가 하나라도 붙은 줄 수 — 글자 규칙만(시각·말소리 재료 없이)."""
    cues = [(str(row["utterance_id"]), str(row["text"])) for row in transcript["utterances"]]
    found = subtitle_suspects.suspects(cues)
    return sum(
        any(reason.code in HALLUCINATION_CODES for reason in reasons) for reasons in found.values()
    )


def line_stats(transcript: Mapping[str, Any]) -> LineStats:
    """줄 수 · 글자 수(공백 뺌) · 줄이 덮은 초 합 · 초당 글자."""
    lines = timing_truth.lines_of(transcript)
    chars = sum(len("".join(line.text.split())) for line in lines)
    seconds = sum((max(line.end - line.start, Fraction(0)) for line in lines), Fraction(0))
    return LineStats(len(lines), chars, seconds, None if not seconds else Fraction(chars) / seconds)


def verdict(
    baseline: Mapping[str, Any],
    engine: Mapping[str, Any],
    settings: EngineCompareSettings = ENGINE_COMPARE_SETTINGS,
) -> dict[str, bool | None]:
    """새 엔진이 기준마다 선을 넘는가 — 입력은 `start_hit`·`end_hit`·`start_mae`·`hallucination`·
    `recall` 칸(정확 유리수 또는 정수, 없으면 None). 한쪽이라도 없으면 그 기준은 None이다."""

    def both(name: str) -> tuple[Any, Any] | None:
        left, right = baseline.get(name), engine.get(name)
        return None if left is None or right is None else (left, right)

    checks: dict[str, bool | None] = {}
    pair = both(START_HIT)
    checks[START_HIT] = None if pair is None else pair[1] >= pair[0]
    pair = both(END_HIT)
    checks[END_HIT] = None if pair is None else pair[1] >= pair[0] - settings.end_hit_slack
    pair = both(START_MAE)
    checks[START_MAE] = None if pair is None else pair[1] <= pair[0] + settings.start_mae_slack
    pair = both(HALLUCINATION)
    checks[HALLUCINATION] = (
        None
        if pair is None
        else Fraction(pair[1]) <= Fraction(pair[0]) * settings.hallucination_share
    )
    pair = both(RECALL)
    checks[RECALL] = None if pair is None else pair[1] >= pair[0] - settings.recall_slack
    return checks


__all__ = [
    "CRITERIA",
    "HALLUCINATION_CODES",
    "LineStats",
    "RevisionPair",
    "hallucination_lines",
    "line_stats",
    "nearest_pairs",
    "pick_pair",
    "verdict",
]
