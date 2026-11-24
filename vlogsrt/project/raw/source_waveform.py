"""원본 하나의 소리 파형(W-87b, D149) — 원본 범위 타임라인의 자막 레인이 그 위에 자막을 놓는다.

초안 편집(D117)의 파형과 같은 측정(`preprocess.amplitudes` — 빈마다 최댓값, 0–255)을
원본 전체에 한 번 하고 작업공간 `cache/timeline-waveform/<원본 sha>.<빈 ms>.json`에 둔다.
원본 시각은 원본 자막과 같은 0초부터다. **표시만 한다** — 자막·시각·지문·캐시 열쇠·승인
어디에도 들지 않는다(규칙 1·3: 시각을 만들지 않는다).
"""

from __future__ import annotations

import base64
import json
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import preprocess
from vlogsrt.config import Config
from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS, FinalEditSettings
from vlogsrt.project.raw.models import RawWorkspaceError

if TYPE_CHECKING:
    from vlogsrt.project.raw.workspace import RawWorkspace

CACHE_FOLDER = ("cache", "timeline-waveform")
SCHEMA = "vlogsrt-source-waveform/v1"
_PEAK_MAX = 255
_LOCK = threading.Lock()


def waveform(
    workspace: RawWorkspace, source_id: str, settings: FinalEditSettings = FINAL_EDIT_SETTINGS
) -> dict[str, Any]:
    """원본 소리의 진폭 — 처음 한 번 재고 원본 내용 sha로 캐시한다(무료 로컬)."""
    asset = workspace.source_asset(source_id)
    if not asset.content_sha256 or not asset.duration_seconds:
        raise RawWorkspaceError(
            f"원본 `{asset.source_name}`의 길이·내용을 몰라 파형을 재지 못했습니다"
        )
    folder = workspace.root.joinpath(*CACHE_FOLDER)
    path = folder / f"{asset.content_sha256}.{settings.waveform_bin_ms}.json"
    with _LOCK:
        cached = _read(path, settings)
        if cached is not None:
            return cached
        record = _measure(workspace.source_media_path(asset), asset.duration_seconds, settings)
        folder.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f".{os.getpid()}.part")
        temporary.write_text(json.dumps(record), encoding="utf-8")
        os.replace(temporary, path)
        return record


def _read(path: Path, settings: FinalEditSettings) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 망가진 캐시는 다시 잰다 — 다음 쓰기가 덮는다
    fresh = (
        isinstance(record, dict)
        and record.get("schema") == SCHEMA
        and record.get("bin_ms") == settings.waveform_bin_ms
    )
    return record if fresh else None


def _measure(media: Path, seconds: float, settings: FinalEditSettings) -> dict[str, Any]:
    probe = replace(
        Config(),
        cut_probe_rate=settings.waveform_rate,
        cut_quiet_window_ms=settings.waveform_bin_ms,
    )
    try:
        peaks = preprocess.amplitudes(media, 0.0, float(seconds), probe)
    except RuntimeError as error:
        raise RawWorkspaceError(f"원본 소리를 읽지 못했습니다 — {error}") from error
    packed = bytes(min(_PEAK_MAX, round(value * _PEAK_MAX)) for value in peaks)
    return {
        "schema": SCHEMA,
        "bin_ms": settings.waveform_bin_ms,
        "count": len(packed),
        "peaks_b64": base64.b64encode(packed).decode("ascii"),
    }
