"""오프라인 미리보기 실행기(`scripts/preview_workbench.py`, W-84) — 격리 · 오프라인.

실제 라이브러리 만들기(ffmpeg 원본 셋 · 폴더 불러오기 · 재생 분석)는 1분 넘게 걸려
여기서 돌리지 않는다 — W-84에서 두 번 돌려(두 번째는 캐시) 확인했다.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "preview_workbench.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("preview_workbench", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


preview = _load()


def test_offline_is_forced_whatever_the_environment_says() -> None:
    assert preview.offline_env({"VLOGSRT_OFFLINE": "0", "X": "1"}) == {
        "VLOGSRT_OFFLINE": "1",
        "X": "1",
    }


def test_paths_stay_inside_the_preview_folder(tmp_path: Path) -> None:
    paths = preview.preview_paths(tmp_path)
    assert all(path == tmp_path or tmp_path in path.parents for path in paths.values())
    assert paths["state"].name != ".vlogsrt-ui-state.json"


def test_default_folder_is_outside_the_repository_and_not_hidden() -> None:
    """저장소 안(워크트리 `.claude/…`)은 숨김 경로라 폴더 불러오기가 원본을 거부한다."""
    base = preview.PREVIEW_DIR
    assert ROOT not in base.parents
    assert not any(part.startswith(".") for part in base.parts[-1:])


def test_never_takes_the_repository_ui_lock_and_binds_loopback() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "UiInstance(" not in text
    assert 'HOST = "127.0.0.1"' in text
    assert "--online" not in text


def test_a_finished_build_is_reused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Path] = []
    monkeypatch.setattr(preview, "_seed", lambda folder: calls.append(folder) or tmp_path / "ws")
    monkeypatch.setattr(preview, "_write_state", lambda state, root: None)
    first = preview.build(tmp_path / "p")
    second = preview.build(tmp_path / "p")
    assert first == second == tmp_path / "ws"
    assert len(calls) == 1
    ready = json.loads((tmp_path / "p" / preview.READY_NAME).read_text(encoding="utf-8"))
    assert ready == {"root": str(tmp_path / "ws")}


def test_a_half_finished_build_starts_over(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    base = tmp_path / "p"
    (base / "shoot").mkdir(parents=True)
    (base / "shoot" / "partial.mp4").write_bytes(b"x")
    monkeypatch.setattr(preview, "_seed", lambda folder: tmp_path / "ws")
    monkeypatch.setattr(preview, "_write_state", lambda state, root: None)
    preview.build(base)
    assert not (base / "shoot" / "partial.mp4").exists()
