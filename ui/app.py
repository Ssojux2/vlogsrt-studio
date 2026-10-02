"""gradio 화면 진입점 — 단일 자막 화면에서 프로젝트 중심 4단계 브라우저로 확장.

이 파일은 `vlogsrt/` 밖에 있다. 핵심 패키지의 의존성은 여전히 3개다(불변 규칙 7).
화면은 넷 — 1 프로젝트·가져오기 / 2 분석·조각 검토 / 3 편집용 폴더 / 4 최종 자막.

실행 계약(계획 §4.1·§4.4): loopback 고정, `share` 없음, `allowed_paths` 없음.
서버를 직접 띄우는 것은 이곳뿐이고, 서버 시작 때 한 번만 작업 복구가 일어난다.
기존 단일 영상 자막 화면은 `ui/service.py`·CLI(`vlogsrt run`)로 그대로 살아 있다.
"""

from __future__ import annotations

import argparse
import webbrowser

import uvicorn

from ui import service, workspace_service
from vlogsrt import log


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlogsrt-ui", description="vlogsrt 원본 브라우저 UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument(
        "--share", action="store_true", help="이전 버전 호환 인자 — 이 UI는 로컬 전용이다"
    )
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = parser.parse_args(argv)
    if args.share:
        log.error("이 UI는 로컬 전용이다 — 외부 공유(share)는 제공하지 않는다.")
        return 1

    # 이전 단일 화면 UI는 같은 포트의 옛 서버를 정리했지만, 이 서버는 작업의
    # 정본을 소유한다 — **다른 실행 중인 UI를 건드리지 않는다**(코디네이터 재결).
    # 포트가 차 있으면 옆으로 비켜 나가고 알림만 남긴다.
    try:
        port = service.pick_port(args.port)
    except service.UiError as exc:
        log.error(str(exc))
        return 1
    if port != args.port:
        log.warn(f"{args.port} 포트를 이미 쓰고 있습니다 — 다른 UI가 실행 중일 수 있습니다.")
        log.warn("그 서버가 진행 중 작업을 소유할 수 있으니 종료하지 마세요.")
        log.warn(f"이번에는 {port} 으로 띄웁니다. 아래 주소를 여세요.")

    server = workspace_service.RawWorkspaceUi(port=port)
    log.info(server.startup())
    app = server.build_fastapi()

    log.out(f"\nvlogsrt 원본 브라우저  →  http://127.0.0.1:{port}")
    log.out("멈추려면 Ctrl+C\n")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        log.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
