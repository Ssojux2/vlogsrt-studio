"""gradio 화면. 조립만 하고 계산은 `ui/service.py`가 한다.

이 파일은 `vlogsrt/` 밖에 있다. 핵심 패키지의 의존성은 여전히 3개다(불변 규칙 7).
화면은 셋뿐이다: 경로 입력 · 실행 로그 · SRT.
"""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

try:
    import gradio as gr
except ModuleNotFoundError as exc:  # pragma: no cover - 설치 안내 전용 경로
    raise SystemExit(
        'gradio가 설치되어 있지 않습니다. 웹 UI는 선택적 extra입니다.\n  pip install -e ".[ui]"'
    ) from exc

from ui import service
from ui.service import UiError
from vlogsrt import log

MEDIA_GLOB = "**/*.{mp4,MP4,mov,MOV,m4v,mkv,webm,mp3,m4a,wav}"

CSS = """
#exec-log textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
footer { display: none !important; }
"""


def _explorer(root: Path) -> Any:
    """기준 폴더가 바뀌면 인스턴스를 새로 돌려줘 다시 그리게 한다."""
    return gr.FileExplorer(
        glob=MEDIA_GLOB,
        root_dir=str(root),
        file_count="single",
        label=str(root),
        height=260,
    )


def build_app(root_dir: Path) -> gr.Blocks:
    with gr.Blocks(title="vlogsrt") as app:  # theme/css는 gradio 6부터 launch()로 옮겨졌다
        gr.Markdown(f"### vlogsrt  ·  {service.status_line()}")

        with gr.Row():
            path_box = gr.Textbox(
                label="영상 경로",
                placeholder="/Users/me/Movies/vlog01.mp4",
                scale=6,
                autofocus=True,
            )
            browse_button = gr.Button("찾아보기", scale=1)
            run_button = gr.Button("자막 만들기", variant="primary", scale=1)

        # 브라우저 앱이라 OS 파일 대화상자는 못 연다. 서버 쪽 폴더를 훑는 패널로 대신한다.
        browse_open = gr.State(False)
        with gr.Column(visible=False) as browse_panel:
            with gr.Row():
                folder_box = gr.Textbox(label="폴더", value=str(root_dir), scale=5)
                open_button = gr.Button("열기", scale=1)
            with gr.Row():
                shortcuts = [
                    (gr.Button(label, size="sm"), folder)
                    for label, folder in service.browse_shortcuts()
                ]
            explorer = _explorer(root_dir)

        log_box = gr.Textbox(
            label="실행 로그", lines=10, max_lines=20, interactive=False, elem_id="exec-log"
        )
        srt_box = gr.Code(label="SRT", lines=24)
        files_box = gr.File(label="저장된 파일", file_count="multiple", height=110)

        # ── 동작 ──────────────────────────────────────────────
        def on_run(path_text: str) -> tuple[str, str, list[str]]:
            report = service.make_subtitles(path_text)
            return report.log, report.srt_text, list(report.files)

        def on_open(folder_text: str) -> tuple[Any, str]:
            try:
                root = service.resolve_folder(folder_text)
            except UiError as exc:
                raise gr.Error(str(exc)) from exc
            return _explorer(root), str(root)

        def on_pick(picked: str | list[str] | None) -> tuple[Any, Any, Any]:
            """고른 파일을 경로 칸에 넣고 패널을 닫는다.

            빈 선택이면 그대로 둔다. 페이지가 처음 뜰 때 탐색기가 빈 change를
            쏘는데, 그걸 받아버리면 열자마자 경로 칸이 지워진다.
            """
            path = (picked[0] if isinstance(picked, list) else picked) or ""
            if not path:
                return gr.skip(), gr.skip(), gr.skip()
            return path, gr.update(visible=False), False

        def on_browse(is_open: bool) -> tuple[Any, bool]:
            return gr.update(visible=not is_open), not is_open

        browse_button.click(on_browse, browse_open, [browse_panel, browse_open])
        run_button.click(on_run, path_box, [log_box, srt_box, files_box])
        path_box.submit(on_run, path_box, [log_box, srt_box, files_box])
        open_button.click(on_open, folder_box, [explorer, folder_box])
        folder_box.submit(on_open, folder_box, [explorer, folder_box])
        for button, folder in shortcuts:
            button.click(lambda folder=str(folder): folder, None, folder_box).then(
                on_open, folder_box, [explorer, folder_box]
            )
        explorer.change(on_pick, explorer, [path_box, browse_panel, browse_open])

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlogsrt-ui", description="vlogsrt 웹 UI")
    parser.add_argument("--root", type=Path, default=None, help="찾아보기 기준 폴더")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="외부 공유 링크 생성")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = parser.parse_args(argv)

    try:
        port = service.pick_port(args.port)
    except UiError as exc:
        log.error(str(exc))
        return 1
    if port != args.port:
        log.warn(f"{args.port} 포트를 이미 쓰고 있어 {port} 으로 띄웁니다.")

    # gradio의 시작 배너는 버퍼에 갇혀 안 보일 때가 있다. 주소는 우리가 직접 찍는다.
    log.out(f"\nvlogsrt UI  →  http://127.0.0.1:{port}")
    log.out("멈추려면 Ctrl+C\n")
    try:
        build_app(args.root or service.default_browse_root()).launch(
            server_port=port,
            share=args.share,
            inbrowser=not args.no_browser,
            theme=gr.themes.Soft(),
            css=CSS,
            allowed_paths=[str(Path.home()), "/tmp"],
        )
    except KeyboardInterrupt:
        log.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
