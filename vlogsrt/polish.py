"""조판 정리 — 영화 자막 관행으로 문장부호를 다듬는다(D30).

whisper는 받아쓰기 모델이라 문어체 부호를 그대로 실어 보낸다. 영화·방송 자막은
반대다: 문장 끝에 마침표를 찍지 않고, 말줄임표는 `…` 한 글자이며, 같은 부호를 겹쳐
쓰지 않는다.

**지울 수 있는 것은 문장부호뿐이다. 글자는 한 자도 못 지운다.** 그것이 이 계층의
유일한 안전선이고, `strip_for_compare` 등식으로 테스트가 지킨다. 시각도 못 건드리고
(규칙 3), 정리 결과가 비면 원문을 그대로 되돌린다(규칙 9). 골격이 아니라 큐를 손대므로
`trace.json`의 `spine[]`에는 언제나 부호가 살아 있는 원문이 남는다(규칙 2).

순수 함수 모듈이다 — I/O도 API도 없다.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from functools import lru_cache

from .config import Config
from .models import Cue

__all__ = ["POLISHED", "polish_pass", "polish_text"]

POLISHED = "polished"

# 말줄임표로 읽히는 것은 온점 2개 이상뿐이다. 하나는 그냥 마침표다.
_ELLIPSIS_RUN = re.compile(r"\.{2,}")
# 이 부호들 앞에는 공백을 새로 넣지 않는다. 닫는 짝이라 앞말에 붙어 있어야 한다.
_CLOSERS = ")]}\"'”’》」』"


@lru_cache(maxsize=32)
def _mark_class(marks: tuple[str, ...]) -> str:
    """문자 클래스 본문. 부호가 config에서 오므로 매번 이스케이프해야 한다."""
    return "".join(re.escape(mark) for mark in marks)


def _collapse(text: str, cfg: Config) -> str:
    """같은 부호가 연속되면 하나로. `?!`는 겹쳐 쓴 것이 아니라 뜻이 다른 둘이라 남긴다."""
    if not cfg.polish_collapse_marks:
        return text
    marks = _mark_class(cfg.polish_collapse_marks)
    return re.sub(rf"([{marks}])\1+", r"\1", text)


def _drop(text: str, cfg: Config) -> str:
    """온점을 지운다. **숫자 사이는 예외다** — 지우면 1.5초가 15초가 된다."""
    for mark in cfg.polish_drop_marks:
        escaped = re.escape(mark)
        text = re.sub(rf"(?<!\d){escaped}|{escaped}(?!\d)", "", text)
    return text


def _tighten(text: str, cfg: Config) -> str:
    """부호 앞 공백을 없애고, 뒤에 글자가 붙어 있으면 공백 하나를 넣는다."""
    if not cfg.polish_tight_marks:
        return text
    marks = _mark_class(cfg.polish_tight_marks)
    tightened = re.sub(rf"\s+(?=[{marks}])", "", text)
    return re.sub(rf"(?<=[{marks}])(?=[^\s{marks}{re.escape(_CLOSERS)}])", " ", tightened)


def _strip_trailing(text: str, cfg: Config) -> str:
    """큐 끝의 쉼표는 지운다. 문장 중간 쉼표는 뜻이 있으니 그대로 둔다."""
    stripped = text.rstrip()
    while stripped and stripped[-1] in cfg.polish_trailing_drop:
        stripped = stripped[:-1].rstrip()
    return stripped


def polish_text(text: str, cfg: Config) -> str:
    """**순서가 곧 규칙이다** — 말줄임표를 먼저 세우지 않으면 온점 삭제가 통째로 먹는다."""
    body = _ELLIPSIS_RUN.sub(cfg.polish_ellipsis, text)
    body = _collapse(body, cfg)
    body = _drop(body, cfg)
    body = _tighten(body, cfg)
    return " ".join(_strip_trailing(body, cfg).split())


def polish_pass(cues: Sequence[Cue], cfg: Config) -> list[Cue]:
    """큐마다 글자만 다듬는다. 시각도 `seg_ids`도 그대로 통과한다."""
    if not cfg.polish_enabled:
        return list(cues)
    polished: list[Cue] = []
    for cue in cues:
        body = polish_text(cue.text, cfg)
        if not body or body == cue.text:
            # 부호밖에 없던 큐는 원문을 그대로 둔다 — 정리는 큐를 비우지 않는다(규칙 9).
            polished.append(cue)
            continue
        polished.append(cue.with_text(body).with_flag(POLISHED))
    return polished
