"""작업공간 모드 — 가로(롱폼) · 세로(숏폼), 라이브러리마다 하나(W-37a, D97, 순수).

자동이면 불러온 영상의 **개수** 다수다 — 길이로 재면 긴 가로 B롤 하나가 숏폼 라이브러리를 뒤집는다.
방향은 `finalcut_timeline.orientation_of`(너비 ≥ 높이면 가로 — 정사각형도 가로, D66)이고, 크기를
모르는 원본은 세지 않는다. 같으면 설정의 편이다. 사람이 고른 가로·세로가 자동보다 이긴다.

모드는 **표지와 흐름 게이트**일 뿐이다: 원본을 빼지 않고(규칙 9), Final Cut 문서의 방향(켠 클립
길이 투표, D66)과 바이트를 바꾸지 않는다 — 다르면 보고서가 메모만 남긴다. 이 모듈은 DB·파일·서비스를
모른다.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from vlogsrt.project.raw.config import (
    MODE_AUTO,
    MODE_CHOICES,
    MODE_LANDSCAPE,
    MODE_PORTRAIT,
    WORKSPACE_MODE,
    WorkspaceModeSettings,
)
from vlogsrt.project.raw.finalcut_timeline import orientation_of

BASIS_AUTO = "auto"
BASIS_MANUAL = "manual"
MODE_LABELS = {MODE_LANDSCAPE: "가로 모드(롱폼)", MODE_PORTRAIT: "세로 모드(숏폼)"}


@dataclass(frozen=True, slots=True)
class ModeDecision:
    """정한 모드와 그 재료 — 자동이 고를 모드(`auto_mode`)는 사람이 골랐어도 보인다."""

    mode: str
    basis: str
    auto_mode: str
    landscape: int
    portrait: int
    unknown: int

    @property
    def label(self) -> str:
        return MODE_LABELS[self.mode]


def decide_mode(
    sizes: Iterable[tuple[int | None, int | None]],
    choice: str = MODE_AUTO,
    settings: WorkspaceModeSettings = WORKSPACE_MODE,
) -> ModeDecision:
    """원본 (너비, 높이)들과 사람이 고른 값 → 모드. 모르는 고른 값은 거부한다."""
    if choice not in MODE_CHOICES:
        raise ValueError(f"모드는 {', '.join(MODE_CHOICES)} 가운데 하나다: {choice!r}")
    landscape = portrait = unknown = 0
    for width, height in sizes:
        if width is None or height is None:
            unknown += 1
        elif orientation_of(width, height) == MODE_LANDSCAPE:
            landscape += 1
        else:
            portrait += 1
    if landscape == portrait:
        auto = settings.tie
    else:
        auto = MODE_LANDSCAPE if landscape > portrait else MODE_PORTRAIT
    manual = choice != MODE_AUTO
    return ModeDecision(
        mode=choice if manual else auto,
        basis=BASIS_MANUAL if manual else BASIS_AUTO,
        auto_mode=auto,
        landscape=landscape,
        portrait=portrait,
        unknown=unknown,
    )


__all__ = ["BASIS_AUTO", "BASIS_MANUAL", "MODE_LABELS", "ModeDecision", "decide_mode"]
