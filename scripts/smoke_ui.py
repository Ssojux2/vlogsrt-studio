"""작업대가 이 운영체제에서 뜨는가 — CI의 첫 단계(W-84, D144). 오프라인 · 127.0.0.1.

    python scripts/smoke_ui.py

`ui.app`·`ui.workspace_service`를 import하고(import만으로 죽는 운영체제 분기를
잡는다), 빈 상태 파일로 `RawWorkspaceUi`를 127.0.0.1의 빈 포트에 띄워 첫 화면(`/`)과
활동 경로(`/vl-activity`)가 200인지 본다. `ui.app.main`은 부르지 않는다 — 저장소마다
하나인 UI 잠금(`UiInstance`)이 같은 저장소의 실제 앱에 붙기 때문이다. 유료 호출 문은
`VLOGSRT_OFFLINE=1`이 닫는다.
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
START_TIMEOUT_SECONDS = 120.0
REQUEST_TIMEOUT_SECONDS = 60.0
POLL_SECONDS = 0.1
PATHS = ("/", "/vl-activity")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((HOST, 0))
        return int(probe.getsockname()[1])


def _get(url: str) -> int:
    with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # noqa: S310
        response.read()
        return int(response.status)


def main() -> int:
    os.environ["VLOGSRT_OFFLINE"] = "1"
    sys.path.insert(0, str(ROOT))
    import uvicorn

    import ui.app  # noqa: F401 — 진입점 import만으로 죽는 분기(resource·fcntl 등)를 잡는다
    from ui import workspace_service

    with tempfile.TemporaryDirectory(prefix="vlogsrt-smoke-") as scratch:
        port = _free_port()
        server_ui = workspace_service.RawWorkspaceUi(
            port=port, state_path=Path(scratch) / "ui-state.json"
        )
        app = server_ui.build_fastapi()
        server = uvicorn.Server(uvicorn.Config(app, host=HOST, port=port, log_level="warning"))
        thread = threading.Thread(target=server.run, daemon=True, name="smoke-ui")
        thread.start()
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                sys.stderr.write("작업대 서버가 뜨지 않았습니다.\n")
                return 1
            time.sleep(POLL_SECONDS)
        try:
            for path in PATHS:
                status = _get(f"http://{HOST}:{port}{path}")
                sys.stdout.write(f"{path} → {status}\n")
                if status != 200:
                    return 1
        finally:
            server.should_exit = True
            thread.join(timeout=START_TIMEOUT_SECONDS)
    sys.stdout.write(f"작업대가 {sys.platform}에서 떴습니다.\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
