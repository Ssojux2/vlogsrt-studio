"""되풀이 검출 — **자리를 짚을 뿐 아무것도 바꾸지 않는다.**

순수 함수만 둔다. 여기서 나온 구간을 무엇에 쓸지는 부르는 쪽이 정한다:
`verify`는 표시만 하고(규칙 5), `repair`는 그 구간만 다시 물어본다(D27).

신호가 둘이다.

- **메아리(echo)**: 연속 세그먼트가 글자 그대로 같다. 세그먼트 하나만 보면 정상이라
  whisper의 환청 필터를 그대로 통과한다.
- **압축률(compression)**: whisper가 세그먼트마다 실어 보내는 `compression_ratio`.
  우리가 계산한 값이 아니라 응답에 있던 값이다. 레퍼런스 구현이 되풀이를 판정할 때
  쓰는 바로 그 지표이고, 임계값 2.4도 거기서 왔다.

두 번째 신호가 중요한 이유: OpenAI 전사 API의 자동 온도 상승은 **로그 확률**을 보고
발동하는데, 되풀이에 빠진 whisper는 확신에 차 있다. 실측에서 `compression_ratio > 2.4`인
세그먼트 248개 중 `avg_logprob < -1.0`인 것은 **0개**였고 `temperature`는 전부 0.0이었다.
호스팅 폴백이 원리적으로 반응할 수 없는 병리라, 우리가 직접 잡아야 한다(D27).
"""

from __future__ import annotations

import zlib
from collections.abc import Callable, Sequence

from .config import Config
from .hangul import to_nfc
from .models import WHY_COMPRESSION, WHY_ECHO, Repeat, Segment


def find_repeats(spine: Sequence[Segment], cfg: Config) -> list[Repeat]:
    """되풀이 구간. 두 신호가 짚은 곳을 합쳐 시간 순으로 돌려준다."""
    found = [*_echo_runs(spine, cfg), *_compression_runs(spine, cfg)]
    return _merge(sorted(found, key=lambda repeat: (repeat.start, repeat.end)))


def text_compression_ratio(text: str) -> float:
    """whisper의 `compression_ratio`와 같은 자. 되풀이일수록 잘 압축된다.

    **짧은 창에만 써라.** 압축률은 글자가 길어지기만 해도 올라간다 — 창 없이 통째로
    재면 길이 검사가 된다(`looks_repetitive` 참조).
    """
    raw = to_nfc(text).encode("utf-8")
    if not raw:
        return 0.0
    return len(raw) / len(zlib.compress(raw))


def looks_repetitive(text: str, cfg: Config) -> bool:
    """글자만 있을 때의 판정. **whisper처럼 창을 나눠 잰다.**

    통째로 재면 안 된다. 압축률은 입력이 길수록 올라가서, 긴 정상 발화가 되풀이로
    잡힌다 — 실측: whisper 스스로 정상이라 표시한 세그먼트 529개를 이어붙이면 2.45로
    임계값(2.4)을 넘는다. 그러면 27분짜리 구간을 **완벽하게 되살려도** 접히게 된다.
    whisper의 레퍼런스 구현이 임계값을 재는 곳도 30초 디코딩 창 하나다.

    창 60어절에서 실측 분포가 갈렸다(정상 최대 2.01 / 되풀이 최소 3.01, 임계값 2.4).
    """
    windows = _windows(to_nfc(text).split(), cfg)
    if not windows:
        return False
    looped = sum(1 for ratio in windows if ratio > cfg.loop_compression_ratio)
    return looped / len(windows) > cfg.loop_repetitive_share


def repetitive_seconds(segments: Sequence[Segment], cfg: Config) -> float:
    """되풀이가 덮는 **시간**. 회복본에 얼마가 남았는지를 재는 자다.

    비율(`repetitive_share`)만으로는 못 잡는다. 비율은 구간이 길수록 느슨해져서 490초
    구간이 245초를 되풀이해도 합격선(0.5) 안이었다(D28). 남은 초는 구간 길이와 무관하다.
    """
    if not segments:
        return 0.0
    return sum(repeat.duration for repeat in find_repeats(segments, cfg))


def repetitive_share(segments: Sequence[Segment], cfg: Config) -> float:
    """세그먼트가 있을 때의 판정 — 되풀이가 덮는 **시간의 비율**.

    재전사 응답은 `verbose_json`이라 세그먼트 경계와 whisper 자신의 지표를 함께 준다.
    글자로 어림잡는 것보다 이쪽이 정확하고, 일부만 되풀이인 결과를 통째로 버리지 않는다.
    """
    if not segments:
        return 0.0
    span = max(segment.end for segment in segments) - min(segment.start for segment in segments)
    if span <= 0:
        return 0.0
    return min(1.0, repetitive_seconds(segments, cfg) / span)


def _windows(words: Sequence[str], cfg: Config) -> list[float]:
    """반 창씩 겹치며 훑는다. 되풀이가 창 경계에 걸쳐도 놓치지 않는다."""
    size = max(1, cfg.loop_ratio_window_words)
    if len(words) <= size:
        return [text_compression_ratio(" ".join(words))] if words else []
    step = max(1, size // 2)
    return [
        text_compression_ratio(" ".join(words[at : at + size]))
        for at in range(0, len(words) - size + 1, step)
    ]


def collapse(segments: Sequence[Segment]) -> list[Segment]:
    """첫 장만 남긴다. **회복 못 한 구간의 마지막 처리다**(D27).

    1668장이 서느니 한 장이 서고 나머지는 비는 편이 낫다는 판단이며, 규칙 9를
    되풀이에 한해 연 지점이다. 여기 오기 전에 재전사를 다 시도한 뒤여야 한다.
    """
    return list(segments[:1])


def _key(segment: Segment) -> str:
    """비교용 표준형. 공백과 유니코드 정규화 차이로 되풀이를 놓치지 않게 한다."""
    return " ".join(to_nfc(segment.text).split())


def _runs(
    spine: Sequence[Segment], same: Callable[[Segment, Segment], bool]
) -> list[list[Segment]]:
    """`same(앞, 뒤)`가 참인 동안 이어지는 묶음. 검출 두 개가 이 뼈대를 같이 쓴다."""
    grouped: list[list[Segment]] = []
    for segment in spine:
        if grouped and same(grouped[-1][-1], segment):
            grouped[-1].append(segment)
        else:
            grouped.append([segment])
    return grouped


def _echo_runs(spine: Sequence[Segment], cfg: Config) -> list[Repeat]:
    def same_words(earlier: Segment, later: Segment) -> bool:
        return bool(_key(later)) and _key(earlier) == _key(later)

    runs = _runs(spine, same_words)
    return [
        _to_repeat(run, WHY_ECHO, _key(run[0]))
        for run in runs
        if _qualifies(run, cfg) and len(_key(run[0])) >= cfg.loop_min_chars
    ]


def _compression_runs(spine: Sequence[Segment], cfg: Config) -> list[Repeat]:
    def both_flagged(earlier: Segment, later: Segment) -> bool:
        ceiling = cfg.loop_compression_ratio
        return earlier.compression_ratio > ceiling and later.compression_ratio > ceiling

    runs = _runs(spine, both_flagged)
    return [
        _to_repeat(run, WHY_COMPRESSION, _key(run[0]))
        for run in runs
        if run[0].compression_ratio > cfg.loop_compression_ratio and _qualifies(run, cfg)
    ]


def _qualifies(run: Sequence[Segment], cfg: Config) -> bool:
    if len(run) < cfg.loop_min_repeats:
        return False
    return run[-1].end - run[0].start >= cfg.loop_min_seconds


def _to_repeat(run: Sequence[Segment], reason: str, text: str) -> Repeat:
    return Repeat(
        start=run[0].start,
        end=run[-1].end,
        count=len(run),
        text=text,
        reason=reason,
        seg_ids=tuple(segment.id for segment in run),
    )


def _merge(repeats: Sequence[Repeat]) -> list[Repeat]:
    """겹치는 구간은 하나로. 같은 자리를 두 번 재전사할 이유가 없다."""
    merged: list[Repeat] = []
    for repeat in repeats:
        if merged and repeat.start <= merged[-1].end:
            merged[-1] = _absorb(merged[-1], repeat)
        else:
            merged.append(repeat)
    return merged


def _absorb(kept: Repeat, other: Repeat) -> Repeat:
    seg_ids = tuple(dict.fromkeys((*kept.seg_ids, *other.seg_ids)))
    return Repeat(
        start=min(kept.start, other.start),
        end=max(kept.end, other.end),
        count=len(seg_ids),
        text=kept.text,
        reason=kept.reason if kept.reason == other.reason else WHY_ECHO,
        seg_ids=seg_ids,
    )
