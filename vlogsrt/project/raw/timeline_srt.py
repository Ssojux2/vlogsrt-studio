"""사람이 고른 SRT를 읽기만 한다 — Final Cut 타이틀의 재료(W-30f, D90).

SRT 시각은 타임라인 시각(0초 = 시퀀스 처음)이다. 여기서는 바이트를 한 번 읽어 그 sha256과 큐를
만들 뿐이고, 타이틀을 짜는 일은 순수 빌더(`finalcut_titles.plan_titles`)가 한다. 보내기 계층
(`finalcut_export`)은 원본을 다시 해시하지 않도록 hashlib을 모르므로 해시는 여기서 한다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vlogsrt import output
from vlogsrt.project.raw.finalcut_timeline import Cue
from vlogsrt.project.raw.models import RawWorkspaceError


class TimelineSrtError(RawWorkspaceError):
    """SRT를 읽지 못했다 — 파일 이름과 까닭을 말한다."""


@dataclass(frozen=True, slots=True)
class TimelineSrt:
    """SRT 하나 — 타임라인 시각의 큐와 리포트에 남길 사실(읽은 바이트의 sha256)."""

    path: Path
    sha256: str
    cues: tuple[Cue, ...]


def read(path: Path) -> TimelineSrt:
    """시각은 밀리초 정확 유리수, 글은 줄을 이은 그대로(BOM 허용, 엄격한 꼴).

    못 읽으면 파일 이름을 대고 거부한다 — 고쳐 읽지 않는다.
    """
    try:
        payload = path.read_bytes()
        cues = output.parse_srt(payload.decode("utf-8"))
    except (OSError, UnicodeDecodeError, RuntimeError) as error:
        raise TimelineSrtError(f"SRT `{path.name}`을 읽지 못했다 — {error}") from error
    return TimelineSrt(
        path=path,
        sha256=hashlib.sha256(payload).hexdigest(),
        cues=tuple(
            Cue(_milliseconds(cue.start), _milliseconds(cue.end), "\n".join(cue.lines))
            for cue in cues
        ),
    )


def _milliseconds(seconds: float) -> Fraction:
    """SRT 시각은 밀리초까지다 — 부동소수 초를 그 정확한 유리수로 되돌린다."""
    return Fraction(round(seconds * 1000), 1000)
