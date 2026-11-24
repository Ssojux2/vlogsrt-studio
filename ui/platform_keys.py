"""운영체제마다 단축키 표기를 고른다(W-84, D144) — 순수 함수, 표준 라이브러리만.

화면과 서버는 같은 컴퓨터다(로컬 작업대) — 그래서 서버의 `sys.platform`이 브라우저를 연 운영체제다.
키 표는 macOS 글자(⌘ ⌥ ⇧ ⌫)로 적고, macOS가 아니면 「Mod = Ctrl」로 읽는다
(Final Cut과 Subtitle Edit `GetCommandOrWin`과 같은 규칙). macOS 바이트는 그대로다.

브라우저 판정(`metaKey` 대 `ctrlKey`)은 재생기 키 표(`ui/keymap.py`의 `__vlKeys.match`)가
이미 한다 — 여기는 사람이 읽는 글자만 바꾼다.
"""

from __future__ import annotations

import sys

MAC_PLATFORM = "darwin"

# macOS 글자 → 다른 운영체제 글자. 수식키는 뒤에 `+`를 붙여 「Ctrl+Shift+Z」 꼴이 된다.
_MODIFIERS: tuple[tuple[str, str], ...] = (
    ("⌘", "Ctrl+"),
    ("⌃", "Ctrl+"),
    ("⌥", "Alt+"),
    ("⇧", "Shift+"),
)
_KEYS: tuple[tuple[str, str], ...] = (
    ("⌫", "Backspace"),
    ("⌦", "Delete"),
    ("−", "-"),
)


def is_mac(platform: str | None = None) -> bool:
    """`platform`을 주지 않으면 부를 때의 `sys.platform`이다(정의할 때 굳히지 않는다)."""
    return (sys.platform if platform is None else platform) == MAC_PLATFORM


def display(text: str, platform: str | None = None) -> str:
    """사람이 읽는 단축키 글자 — macOS면 그대로, 아니면 ⌘→Ctrl+ · ⌥→Alt+ · ⇧→Shift+ · ⌫→Backspace.

    키 칸 하나(「⌘⇧Z」)든 문장 안에 섞인 글자(「되돌리기 (⌘Z)」)든 같은 규칙이다."""
    if is_mac(platform):
        return text
    for mac, other in _KEYS:
        text = text.replace(mac, other)
    for mac, other in _MODIFIERS:
        text = text.replace(mac, other)
    return text
