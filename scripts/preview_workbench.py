"""오프라인 미리보기 — 합성 원본 라이브러리로 작업대를 띄운다(W-84, D144). 무료 · 키 없음.

    python scripts/preview_workbench.py                 # 만들고(처음만) 7871에 띄운다
    python scripts/preview_workbench.py --build-only    # 만들기만(다시 부르면 캐시를 쓴다)
    python scripts/preview_workbench.py --rebuild       # 지우고 다시 만든다

라이브러리는 UI 통합 인수 정리 부록 B(`docs/plans/2026-11-16-ui-integration-handoff.md`)와
같다 — 원본 셋(4K 가로 · HD 가로 · 세로 24fps, 각 30초, 자막 4줄)을 「폴더 불러오기」로
들이고 재생 픽스처로 분석한 뒤 원본 범위 타임라인 하나를 만든다.

자리는 운영체제 임시 폴더의 `vlogsrt-preview/`(`--dir`로 바꾼다)이고 상태 파일도 거기
따로 둔다 — 사람의 `.vlogsrt-ui-state.json`을 건드리지 않는다. 저장소 안
(`.claude/worktrees/…`·`.vlogsrt-cache/`)은 숨김 경로라 폴더 불러오기가 원본을
거부한다(링크 풀기의 숨김·보호 칸 거부).

`ui.app.main`은 부르지 않는다: 저장소마다 하나인 UI 잠금(`UiInstance`)이 같은 저장소의
실제(온라인) 앱에 붙어 버리기 때문이다. 대신 `RawWorkspaceUi`를 127.0.0.1에 직접 띄우고
`VLOGSRT_OFFLINE=1`을 강제한다. 시험 도우미(`tests.raw.test_render_export`)에 기대므로
그 시험이 바뀌면 함께 손본다.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
PREVIEW_DIR = Path(tempfile.gettempdir()) / "vlogsrt-preview"
STATE_NAME = "ui-state.json"
READY_NAME = "ready.json"
SHOOT_NAME = "shoot"
DEFAULT_PORT = 7871
HOST = "127.0.0.1"
SECONDS = 30
CUE_COUNT = 4
CUE_SPACING_SECONDS = 7
CUE_LENGTH_SECONDS = 1.8
EXTRA_WINDOWS = 40
# (이름, 크기, 레이트, 촬영 시각, 첫 자막 시작 초) — 촬영 시각 순은 C → B → A다.
SOURCES: tuple[tuple[str, str, int, str, float], ...] = (
    ("A_uhd.mp4", "3840x2160", 30, "2026-09-01T10:05:00Z", 0.2),
    ("B_hd.mp4", "1920x1080", 30, "2026-09-01T10:00:00Z", 0.4),
    ("C_tall.mp4", "1080x1920", 24, "2026-09-01T09:55:00Z", 0.3),
)


def preview_paths(base: Path = PREVIEW_DIR) -> dict[str, Path]:
    return {
        "base": base,
        "shoot": base / SHOOT_NAME,
        "state": base / STATE_NAME,
        "ready": base / READY_NAME,
    }


def offline_env(env: dict[str, str]) -> dict[str, str]:
    """유료 호출 문을 닫는다 — 바깥 값이 무엇이든 1이다."""
    return {**env, "VLOGSRT_OFFLINE": "1"}


def _speech(offset: float) -> list[dict[str, Any]]:
    return [
        {
            "start": CUE_SPACING_SECONDS * k + offset,
            "end": CUE_SPACING_SECONDS * k + offset + CUE_LENGTH_SECONDS,
            "text": f"{k + 1}번째 말입니다",
        }
        for k in range(CUE_COUNT)
    ]


def build(base: Path = PREVIEW_DIR, *, rebuild: bool = False) -> Path:
    """합성 라이브러리 작업공간 루트 — 이미 만들었으면 그대로 돌려준다."""
    paths = preview_paths(base)
    if rebuild and base.exists():
        shutil.rmtree(base)
    if paths["ready"].is_file():
        return Path(json.loads(paths["ready"].read_text(encoding="utf-8"))["root"])
    if base.exists():
        shutil.rmtree(base)  # 중간에 멈춘 만들기 — 반쯤 든 라이브러리를 다시 쓰지 않는다
    paths["shoot"].mkdir(parents=True)
    root = _seed(paths["shoot"])
    _write_state(paths["state"], root)
    paths["ready"].write_text(json.dumps({"root": str(root)}, ensure_ascii=False), "utf-8")
    return root


def _seed(folder: Path) -> Path:
    sys.path.insert(0, str(ROOT))
    import tests.raw.test_render_export as tre
    import tests.raw.test_review_workspace as trw
    from vlogsrt.project.raw import service as raw_service
    from vlogsrt.project.raw import timeline_edit_io, timing_engine
    from vlogsrt.project.raw.config import TIMING_ENGINE_WHISPER

    tre.UTTERANCES.clear()
    tre.UTTERANCES.update({name: _speech(offset) for name, _, _, _, offset in SOURCES})
    for name, size, rate, created, _ in SOURCES:
        tre._encode(folder / name, size, rate, SECONDS, created=created)
    originals = {name: tre._identity(folder / name) for name in tre.UTTERANCES}
    # 재생 픽스처는 whisper 응답 모양이다 — 새로 듣는 엔진을 whisper로 둔다(bench_workbench와 같다).
    timing_engine.DEFAULT_NEW_HEARING_ENGINE = TIMING_ENGINE_WHISPER
    raw_service.DEFAULT_NEW_HEARING_ENGINE = TIMING_ENGINE_WHISPER
    real = tre._write_replay_fixtures

    def more_windows(root: Path, sources: dict[str, Any]) -> None:
        real(root, sources)
        luna = root / "analysis/replay/luna-fixtures.json"
        data = json.loads(luna.read_text(encoding="utf-8"))
        for n in range(1, EXTRA_WINDOWS):
            key = f"window_{n:06d}"
            body = trw._luna_body(key)
            body.update(
                suitability="usable", suitability_reason="선명하다", scene_tags=["dialogue"]
            )
            data["windows"][key] = body
        luna.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    tre._write_replay_fixtures = more_windows
    try:
        trip = tre._trip(folder, originals)
    finally:
        tre._write_replay_fixtures = real
    timeline_edit_io.create_timeline(trip.service, trip.root)
    return Path(trip.root)


def _write_state(state: Path, root: Path) -> None:
    """작업대가 처음 열 작업공간을 상태 파일에 남긴다 — 화면의 「열기」와 같은 길이다."""
    from ui.workspace_service import RawWorkspaceUi

    RawWorkspaceUi(state_path=state).open_workspace_view(str(root))


def serve(port: int, base: Path = PREVIEW_DIR) -> int:
    import uvicorn

    from ui import app as ui_app
    from ui import workspace_service

    server = workspace_service.RawWorkspaceUi(port=port, state_path=preview_paths(base)["state"])
    with ui_app._server_activity():
        server.startup()
        uvicorn.run(server.build_fastapi(), host=HOST, port=port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--build-only", action="store_true", help="만들기만 하고 띄우지 않는다")
    parser.add_argument("--rebuild", action="store_true", help="지우고 다시 만든다")
    parser.add_argument(
        "--dir", type=Path, default=PREVIEW_DIR, help="라이브러리 자리(숨김 경로 밖)"
    )
    args = parser.parse_args(argv)
    os.environ.update(offline_env(dict(os.environ)))
    sys.path.insert(0, str(ROOT))
    base = args.dir.expanduser().resolve()
    root = build(base, rebuild=args.rebuild)
    sys.stdout.write(f"미리보기 작업공간: {root}\n")
    sys.stdout.flush()
    if args.build_only:
        return 0
    sys.stdout.write(f"작업대(오프라인) → http://{HOST}:{args.port}\n")
    sys.stdout.flush()
    return serve(args.port, base)


if __name__ == "__main__":
    raise SystemExit(main())
