"""W-30d — 새 작업공간은 고르지 않아도 Gemini 3.8 Flash로 판정한다: 작업대 전체 흐름(D87).

가짜 전송만 바뀐다 — 전사(whisper 가짜)·창 계획·프레임·**창 클립(실제 ffmpeg,
무료·로컬)**·승인·응답자· 영수증은 진짜 길이다. 유료 호출 0회. 시험이 못 박는 것: 요청마다 클립(그림
+ 소리) + 프레임 + 안내문이 가고, 영수증이 Flash 공식 단가로 값을 매기며, 견적 상한이 Flash 창당
상한 × 창 수다.
"""

from __future__ import annotations

import base64
import json
import shutil
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_claude_analysis_flow import DONE, judgment, make_server, settle
from vlogsrt import preprocess
from vlogsrt.gemini_messages import GeminiClient
from vlogsrt.project.raw import gemini_cost
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.config import GEMINI_ANALYSIS_PROFILES, gemini_profile
from vlogsrt.project.raw.understanding_luna import gemini_output_schema_bytes

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")

needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")

FLASH = "gemini-3.8-flash"
USAGE = {"promptTokenCount": 8000, "candidatesTokenCount": 300, "thoughtsTokenCount": 700}
"""가짜 응답의 사용량 — Flash 공식 단가로 (8000×$0.75 + 1000×$3.75)/100만 = $0.00975."""


class _Reply:
    def __init__(self, body: dict[str, Any]) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}
        self._raw = json.dumps(body, ensure_ascii=False).encode("utf-8")

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> _Reply:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class GeminiOpener:
    """generateContent 전송 자리의 가짜 — 실제 요청 객체를 받아 (URL, 본문)을 기록한다(소켓
    없음)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: Any, timeout: float) -> _Reply:
        body = json.loads(request.data)
        self.calls.append((request.full_url, body))
        text = body["contents"][0]["parts"][-1]["text"]
        model = request.full_url.rsplit("/models/", 1)[1].split(":", 1)[0]
        reply = {
            "candidates": [
                {
                    "content": {
                        "role": "model",
                        "parts": [
                            {"text": "합성 생각", "thought": True},
                            {"text": json.dumps(judgment(text), ensure_ascii=False)},
                        ],
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": dict(USAGE),
            "modelVersion": model,
            "responseId": f"resp_flow_{len(self.calls)}",
        }
        return _Reply(reply)


def gemini_clients(opener: GeminiOpener) -> dict[str, GeminiClient]:
    return {
        model: GeminiClient(
            api_key="AIza-synthetic-flow",
            base_url=profile.api_base_url,
            api_version=profile.api_version,
            timeout_seconds=profile.timeout_seconds,
            opener=opener,
        )
        for model, profile in GEMINI_ANALYSIS_PROFILES.items()
    }


@needs_ffmpeg
def test_a_new_workspace_analyzes_with_flash_clips_and_frames(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    opener = GeminiOpener()
    server, sender, luna = make_server(
        tmp_path,
        monkeypatch,
        source,
        opener=None,
        model=None,
        gemini_clients=gemini_clients(opener),
    )
    root = server.workspace_root()
    assert server.raw.settings(root).models.luna_model == FLASH
    server.capcut_choose_view(server.current_signature())
    state = settle(server)
    assert state["stage"] == DONE, state
    assert (len(sender.calls), len(luna.calls), len(opener.calls)) == (1, 0, 3)
    sounds = []
    for url, body in opener.calls:
        assert url == (
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.8-flash:generateContent"
        )
        assert set(body) == {"contents", "generationConfig"}
        parts = body["contents"][0]["parts"]
        video = parts[1]
        assert video["inlineData"]["mimeType"] == "video/mp4"
        clip = tmp_path / f"clip-{len(url)}-{parts[0]['text'][:3]}.mp4"
        clip.write_bytes(base64.b64decode(video["inlineData"]["data"]))
        probe = preprocess.probe_raw_media(clip)
        sounds.append(len(probe.audio))
        assert max(probe.video.width, probe.video.height) <= 640
        assert "clip(그림과 소리" in parts[-1]["text"]
        assert body["generationConfig"]["thinkingConfig"] == {"thinkingLevel": "MEDIUM"}
        bound = gemini_cost.payload_bound(body, gemini_profile(FLASH), on=date.today())
        assert bound.usd > 0
    assert sounds[0] == 1 and all(count <= 1 for count in sounds)  # 합성 원본의 소리는 4초뿐이다
    revision = AnalysisStore(server.raw._workspace(root)).revision(state["revision"])
    assert revision is not None and json.loads(revision.models_json)["luna"] == FLASH
    with sqlite3.connect(root / "project.sqlite3") as db:
        db.row_factory = sqlite3.Row
        rows = list(db.execute("SELECT * FROM analysis_requests WHERE model=?", (FLASH,)))
    assert len(rows) == 3 and all(row["unpriced_reason"] is None for row in rows)
    assert all(Decimal(row["cost_usd"]) == Decimal("0.00975") for row in rows)
    (quoted,) = state["quote"].estimates
    per_window = gemini_cost.window_ceiling_usd(
        gemini_profile(FLASH),
        RawUnderstandingSettings(),
        schema_bytes=gemini_output_schema_bytes(),
        on=date.today(),
    )
    assert per_window is not None
    assert Decimal(quoted.ceiling_usd) == per_window * quoted.window_count
