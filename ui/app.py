"""gradio 화면 진입점 — 단일 자막 화면에서 프로젝트 중심 4단계 브라우저로 확장.

이 파일은 `vlogsrt/` 밖에 있다. 핵심 패키지의 의존성은 여전히 3개다(불변 규칙 7).
화면은 넷 — 1 프로젝트·가져오기 / 2 분석·조각 검토 / 3 편집용 폴더 / 4 최종 자막.

실행 계약(계획 §4.1·§4.4): loopback 고정, `share` 없음, `allowed_paths` 없음.
서버를 직접 띄우는 것은 이곳뿐이고, 서버 시작 때 한 번만 작업 복구가 일어난다.
기존 단일 영상 자막 화면은 `ui/service.py`·CLI(`vlogsrt run`)로 그대로 살아 있다.
"""

from __future__ import annotations

import argparse
import os
import webbrowser
from pathlib import Path

import uvicorn

from ui import service, workspace_service
from ui.instance import UiInstance
from vlogsrt import log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlogsrt-ui", description="vlogsrt 원본 브라우저 UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--share", action="store_true", help="이전 버전 호환 인자 — 이 UI는 로컬 전용이다"
    )
    network = parser.add_mutually_exclusive_group()
    network.add_argument(
        "--online",
        dest="online",
        action="store_true",
        help="온라인 분석 허용(기본값, 영상별 승인은 별도)",
    )
    network.add_argument(
        "--offline",
        dest="online",
        action="store_false",
        help="이 앱 실행의 유료 API 호출 차단",
    )
    parser.set_defaults(online=True)
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = parser.parse_args(argv)
    if args.share:
        log.error("이 UI는 로컬 전용이다 — 외부 공유(share)는 제공하지 않는다.")
        return 1
    # The interactive app defaults to paid-enabled mode; each selected scope still
    # needs approval. Set before dotenv loading so stale file defaults cannot win.
    os.environ["VLOGSRT_OFFLINE"] = "0" if args.online else "1"

    with UiInstance(Path(__file__).resolve().parent.parent, args.port) as instance:
        url = f"http://127.0.0.1:{instance.port}"
        if not instance.owned:
            if not instance.wait_until_listening():
                log.error(
                    "기존 서버가 시작 또는 종료 중이며 아직 연결할 수 없습니다. "
                    "잠시 뒤 다시 실행하세요. 다른 포트에는 실행하지 않았습니다."
                )
                return 1
            log.out(f"이미 실행 중인 vlogsrt를 엽니다: {url}")
            if not args.no_browser:
                webbrowser.open(url)
            return 0
        try:
            # A foreign/old process on the requested port must not silently create
            # another UI on a different port. No process is killed by the launcher.
            port = service.pick_port(args.port, attempts=1)
        except service.UiError:
            log.error(
                f"{args.port} 포트가 사용 중입니다. 기존 서버를 확인하세요. "
                "새 서버를 다른 포트에 자동 실행하지 않았습니다."
            )
            return 1
        server = workspace_service.RawWorkspaceUi(port=port)
        log.info(server.startup())
        app = server.build_fastapi()
        log.out(f"\nvlogsrt 원본 브라우저  →  {url}")
        log.out("멈추려면 Ctrl+C\n")
        if not args.no_browser:
            webbrowser.open(url)
        try:
            uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
        except KeyboardInterrupt:
            log.info("종료")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
