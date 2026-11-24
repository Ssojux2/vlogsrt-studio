"""gradio 화면 진입점 — 단일 자막 화면에서 프로젝트 중심 4단계 브라우저로 확장.

이 파일은 `vlogsrt/` 밖에 있다. 핵심 패키지의 의존성은 여전히 3개다(불변 규칙 7).
화면은 넷 — 1 프로젝트·가져오기 / 2 분석·조각 검토 / 3 편집용 폴더 / 4 최종 자막.

실행 계약(계획 §4.1·§4.4): loopback 고정, `share` 없음, `allowed_paths` 없음.
서버를 직접 띄우는 것은 이곳뿐이고, 서버 시작 때 한 번만 작업 복구가 일어난다.
옛 단일 영상 자막 화면과 CLI(`vlogsrt run`)는 whisper와 함께 지웠다(D163).
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import threading
import webbrowser
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from ui import activity_log, chooser, service, workspace_service
from ui.instance import UiInstance
from vlogsrt import log, preprocess, scratch_dirs

OPEN_FILE_SOFT_LIMIT = 4096
"""Finder·터미널에서 띄우면 macOS 기본 soft 한도 256을 물려받는다 — 작업 여럿이
ffmpeg·DB·소켓을 함께 쥐면 모자란다."""


def _raise_open_file_limit() -> None:
    if sys.platform == "win32":
        return  # Windows에는 rlimit가 없다 — CRT 서술자 상한(8192)은 여기서 바꾸지 않는다(D100 §1)
    import resource  # POSIX에만 있는 모듈 — 맨 위에서 import하면 Windows에서 앱이 뜨지 않는다

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    target = (
        OPEN_FILE_SOFT_LIMIT if hard == resource.RLIM_INFINITY else min(OPEN_FILE_SOFT_LIMIT, hard)
    )
    if soft == resource.RLIM_INFINITY or soft >= target:
        return
    try:
        resource.setrlimit(resource.RLIMIT_NOFILE, (target, hard))
    except (ValueError, OSError) as error:
        log.warn(f"열린 파일 한도를 {soft}에서 올리지 못했다: {error}")
        return
    log.info(f"열린 파일 한도 {soft} → {target}")


@contextmanager
def _server_activity() -> Iterator[None]:
    """서버가 도는 동안만 — 터미널 줄을 「기록」 판에도 남기고 ffmpeg 진행 파일을 켠다(D61).

    CLI와 시험은 이 길을 지나지 않으므로 argv가 그대로다. 진행 파일 폴더는 끝날 때 치운다.
    폴더 이름에 pid를 싣는다 — 서버가 죽어 남으면 다음 시작의 청소가 지운다(D63 H).
    """
    with (
        # 며칠 켜 둔 서버에서 `$TMPDIR` 청소가 먼저 지웠어도 끝내기가 죽지 않는다.
        tempfile.TemporaryDirectory(
            prefix=scratch_dirs.pid_prefix(scratch_dirs.PROGRESS_SCRATCH),
            ignore_cleanup_errors=True,
        ) as progress,
        activity_log.captured(),
    ):
        preprocess.enable_process_progress(Path(progress))
        try:
            yield
        finally:
            preprocess.disable_process_progress()


def _parser() -> argparse.ArgumentParser:
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
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
        # 서버를 띄우는 자리에서만 — 죽은 프로세스가 남긴 pid 임시 폴더를 치운다(D63 H).
        scratch_dirs.sweep_dead_scratch()
        _start_picker_warmup()
        with _server_activity():
            return _serve(port, url, open_browser=not args.no_browser)


def _start_picker_warmup() -> None:
    """첫 「찾아보기」가 컴파일을 기다리지 않게 선택 창 앱을 뒤에서 미리 만든다(D106).

    누르기와 겹쳐도 `chooser._adopt_compiled`의 원자적 이름 바꾸기가 풀므로 잠금이 없다.
    """
    threading.Thread(target=chooser.warm_picker_app, daemon=True, name="picker-warm").start()


def _serve(port: int, url: str, *, open_browser: bool) -> int:
    _raise_open_file_limit()
    server = workspace_service.RawWorkspaceUi(port=port)
    log.info(server.startup())
    app = server.build_fastapi()
    log.out(f"\nvlogsrt 원본 브라우저  →  {url}")
    log.out("멈추려면 Ctrl+C\n")
    if open_browser:
        webbrowser.open(url)
    try:
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    except KeyboardInterrupt:
        log.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
