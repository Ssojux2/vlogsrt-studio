"""재생 작업공간 도우미 — Gemini 시간 엔진·whisper 모양 재생 선언을 쓰는 시험이 함께 쓴다.

옛 글자 경로 시험(`test_source_text_flow`·`test_gemini_text_flow`)은 whisper와 함께 지웠다(D163) —
그 파일들의 재생 도우미만 여기로 옮겼다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.config import GEMINI_TRANSCRIBE_PROFILE, OPENAI_LUNA_MODEL

WHISPER_TEXT = "합성 영상입니다."


def _gemini_payload() -> dict[str, object]:
    """Gemini 3.5 Transcribe 응답 하나 — 시각 단어 셋."""
    words = [("합성", "0.6s", "0.9s"), ("영상이네요", "1.0s", "1.4s"), ("음", "2.0s", "2.3s")]
    text = "합성 영상이네요. 음"
    return {
        "status": "completed",
        "model": GEMINI_TRANSCRIBE_PROFILE.model,
        "output_text": text,
        "steps": [
            {
                "type": "model_output",
                "content": [
                    {
                        "type": "text",
                        "text": text,
                        "annotations": [
                            {"type": "word_info", "text": w, "start_offset": s, "end_offset": e}
                            for w, s, e in words
                        ],
                    }
                ],
            }
        ],
    }


def _write_gemini(root: Path, source_sha: str) -> None:
    body = {"sources": {source_sha: {"0": _gemini_payload()}}}
    (root / "analysis" / "replay" / "word-text-fixtures.json").write_text(
        json.dumps(body, ensure_ascii=False), encoding="utf-8"
    )


def _phase_documents(root: Path, asset_id: str, name: str = "asr_phase.json") -> list[dict]:
    found = []
    for path in sorted((root / "analysis").rglob(name)):
        body = json.loads(path.read_text(encoding="utf-8"))
        if body.get("asset_id") == asset_id:
            found.append({**body, "_folder": str(path.parent)})
    return found


def _replay_workspace(tmp_path: Path) -> tuple[raw.RawWorkspaceService, Path, str, str]:
    service = raw.RawWorkspaceService()
    root = tmp_path / "ws"
    service.create_workspace(root, "글자 재생")
    workflow = service.settings(root)
    models = replace(workflow.models, luna_model=OPENAI_LUNA_MODEL)
    service.update_settings(root, replace(workflow, models=models))
    media = tmp_path / "talk.mov"
    _make_media(media, audio=True, seconds="3")
    handle = service.import_selected_video(root, media)
    assert _wait_terminal(service, root, handle.job_id).status == "succeeded"
    service.ensure_source_order(root)
    (asset,) = service.list_source_assets(root)
    return service, root, asset.asset_id, asset.content_sha256 or ""


def _write_replay(root: Path, source_sha: str) -> None:
    """whisper 모양 발화 선언과 Luna 창 판정 — 재생 분석의 재료."""
    folder = root / "analysis" / "replay"
    folder.mkdir(parents=True, exist_ok=True)
    asr = {"sources": {source_sha: [{"start": 0.5, "end": 1.5, "text": WHISPER_TEXT}]}}
    luna = {
        "windows": {
            "window_000000": {
                "window_id": "window_000000",
                "speech_summary": "합성 설명",
                "visual_summary": "합성 풍경",
                "scene_tags": ["scenery"],
                "evidence_ids": ["window_000000"],
                "suggested_boundary_ids": [],
                "editing_notes": [],
                "needs_review": False,
                "uncertainties": [],
            }
        }
    }
    for name, body in (("asr", asr), ("luna", luna)):
        (folder / f"{name}-fixtures.json").write_text(
            json.dumps(body, ensure_ascii=False), encoding="utf-8"
        )
