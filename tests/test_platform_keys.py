"""운영체제별 단축키 표기(W-84, D144) — macOS는 바이트 그대로, 다른 운영체제는 Mod = Ctrl."""

from __future__ import annotations

import pytest

from ui import final_edit_ui, platform_keys, timeline_player


@pytest.mark.parametrize(
    ("keys", "other"),
    [
        ("⌘B", "Ctrl+B"),
        ("⌘⇧Z", "Ctrl+Shift+Z"),
        ("⌥[", "Alt+["),
        ("⇧⌫", "Shift+Backspace"),
        ("⌘−", "Ctrl+-"),
        ("Space", "Space"),
        ("되돌리기 (⌘Z)", "되돌리기 (Ctrl+Z)"),
    ],
)
def test_non_mac_reads_mod_as_ctrl(keys: str, other: str) -> None:
    assert platform_keys.display(keys, "win32") == other
    assert platform_keys.display(keys, "linux") == other


@pytest.mark.parametrize("keys", ["⌘B", "⌘⇧Z", "⌥[", "⇧⌫", "Space"])
def test_mac_keeps_the_glyphs(keys: str) -> None:
    assert platform_keys.display(keys, "darwin") == keys


def test_keysheet_follows_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    """키 표(시트·브라우저 표)가 이 운영체제의 글자를 쓴다 — macOS는 예전 바이트 그대로."""
    monkeypatch.setattr(platform_keys.sys, "platform", "darwin")
    mac_rows = {row["action"]: row["keys"] for row in final_edit_ui.keymap_payload()}
    assert mac_rows["blade"] == "⌘B"
    assert mac_rows["redo"] in {"⌘⇧Z", "Ctrl+Y"}
    monkeypatch.setattr(platform_keys.sys, "platform", "win32")
    win_rows = [row for row in final_edit_ui.keymap_payload() if row["action"] == "blade"]
    assert [row["keys"] for row in win_rows] == ["Ctrl+B"]
    assert all("⌘" not in str(row["keys"]) for row in final_edit_ui.keymap_payload())


def test_player_buttons_follow_the_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    model = {"frame": {"width": 1920, "height": 1080}}
    monkeypatch.setattr(platform_keys.sys, "platform", "darwin")
    mac = timeline_player.timeline_player_html(model)
    assert "(⌘B)" in mac and "⌥C" in mac
    monkeypatch.setattr(platform_keys.sys, "platform", "win32")
    win = timeline_player.timeline_player_html(model)
    assert "(Ctrl+B)" in win and "Alt+C" in win
    assert "⌘" not in win and "⌥" not in win
