"""파형 자막 레인의 배선(W-87b, D149) — 서버 함수 · 파형 캐시 · 재생기 연결."""

from __future__ import annotations

import base64
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

from tests.raw.test_timeline_player_ui import _server  # noqa: E402
from ui.timeline_player import PLAYER_JS  # noqa: E402
from ui.timeline_player_js import PLAYER_APP_JS  # noqa: E402
from vlogsrt.project.raw import source_waveform  # noqa: E402
from vlogsrt.project.raw.models import RawWorkspaceError  # noqa: E402


def test_wave_server_function_returns_the_source_waveform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    server, _root, signature = _server(tmp_path)
    player = server._timeline_player
    asked: list[str] = []

    def wave(_root: Path, source_id: str) -> dict[str, Any]:
        asked.append(source_id)
        return {"bin_ms": 20, "peaks_b64": "AA=="}

    monkeypatch.setattr(server.raw, "timeline_waveform", wave)
    got = player.action({"signature": signature, "sourceId": "s1"}, "wave")
    assert got == {"ok": True, "sourceId": "s1", "wave": {"bin_ms": 20, "peaks_b64": "AA=="}}
    assert asked == ["s1"]
    assert "tl_wave" in [f.__name__ for f in player.server_functions()]


def _workspace(tmp_path: Path, sha: str | None, seconds: float | None) -> Any:
    asset = SimpleNamespace(content_sha256=sha, duration_seconds=seconds, source_name="a.mp4")
    return SimpleNamespace(
        root=tmp_path,
        source_asset=lambda _id: asset,
        source_media_path=lambda _asset: tmp_path / "a.mp4",
    )


def test_source_waveform_is_measured_once_and_cached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[float] = []

    def amplitudes(path: Path, start: float, length: float, cfg: Any) -> list[float]:
        calls.append(length)
        return [0.0, 0.5, 1.0]

    monkeypatch.setattr(source_waveform.preprocess, "amplitudes", amplitudes)
    workspace = _workspace(tmp_path, "abc", 2.5)
    first = source_waveform.waveform(workspace, "s1")
    second = source_waveform.waveform(workspace, "s1")
    assert first == second and calls == [2.5]
    assert base64.b64decode(first["peaks_b64"]) == bytes([0, 128, 255])
    assert (tmp_path / "cache" / "timeline-waveform" / "abc.20.json").is_file()


def test_source_waveform_refuses_an_unknown_source(tmp_path: Path) -> None:
    with pytest.raises(RawWorkspaceError, match="파형"):
        source_waveform.waveform(_workspace(tmp_path, None, None), "s1")


def test_the_player_draws_the_lane_and_edits_only_through_document_operations() -> None:
    assert "__vlCaptionLane" in PLAYER_JS and 'id="tl-lane"' not in PLAYER_APP_JS
    js = PLAYER_APP_JS
    for needle in (
        'lane.addEventListener("pointerdown", onLaneDown);',
        'lane.addEventListener("wheel", onLaneWheel, { passive: false });',
        "TL.cueSetTimes(S.doc, id,",
        "TL.cueShift(S.doc, id,",
        'TL.cueInsert(S.doc, a[0], a[1], b[1], "")',
        'if (ev.code === "Enter") { insertSelection(); }',
        "LN.snap(ticks, laneSnaps(), S.settings.snapPx * view.tpp)",
    ):
        assert needle in js, needle
    # ⇧이면 스냅하지 않는다(Subtitle Edit) — 눌린 수식키를 확인한다.
    assert "var snapped = ev.shiftKey ? ticks :" in js
