"""gradio 화면. 조립만 하고 계산은 `ui/service.py`가 한다.

이 파일은 `vlogsrt/` 밖에 있다. 핵심 패키지의 의존성은 여전히 3개다(불변 규칙 7).
화면은 셋뿐이다: 영상 고르기 · 실행 로그 · SRT.
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
from vlogsrt import log

CSS = """
#exec-log textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
footer { display: none !important; }
"""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="vlogsrt") as app:  # theme/css는 gradio 6부터 launch()로 옮겨졌다
        gr.Markdown(f"### vlogsrt  ·  {service.status_line()}")

        # OS 기본 파일 선택창. 브라우저가 경로를 넘겨주지 않으므로 파일 자체를 받는다.
        picker = gr.File(
            label="영상 고르기",
            file_count="single",
            file_types=list(service.VIDEO_SUFFIXES),
            type="filepath",
            height=120,
        )
        with gr.Row():
            path_box = gr.Textbox(
                label="영상 경로",
                info="위에서 고르면 자동으로 채워집니다. 경로를 직접 붙여넣어도 됩니다.",
                placeholder="/Users/me/Movies/vlog01.mp4",
                scale=6,
                autofocus=True,
            )
            run_button = gr.Button("자막 만들기", variant="primary", scale=1)

        log_box = gr.Textbox(
            label="실행 로그", lines=10, max_lines=20, interactive=False, elem_id="exec-log"
        )
        srt_box = gr.Code(label="SRT", lines=24)
        files_box = gr.File(label="저장된 파일", file_count="multiple", height=110)

        # ── 동작 ──────────────────────────────────────────────
        def on_run(path_text: str) -> tuple[str, str, list[str]]:
            report = service.make_subtitles(path_text)
            return report.log, report.srt_text, list(report.files)

        def on_pick(picked: str | None) -> Any:
            """고른 파일의 서버 경로를 경로 칸에 넣는다.

            선택을 지웠을 때(None) 경로 칸까지 비우지는 않는다. 직접 붙여넣은
            경로가 사라지면 곤란하다.
            """
            return picked or gr.skip()

        picker.change(on_pick, picker, path_box)
        run_button.click(on_run, path_box, [log_box, srt_box, files_box])
        path_box.submit(on_run, path_box, [log_box, srt_box, files_box])

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlogsrt-ui", description="vlogsrt 웹 UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="외부 공유 링크 생성")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = parser.parse_args(argv)

    try:
        port = service.pick_port(args.port)
    except service.UiError as exc:
        log.error(str(exc))
        return 1
    for notice in service.busy_port_notice(args.port, port) if port != args.port else []:
        log.warn(notice)

    # gradio의 시작 배너는 버퍼에 갇혀 안 보일 때가 있다. 주소는 우리가 직접 찍는다.
    log.out(f"\nvlogsrt UI  →  http://127.0.0.1:{port}")
    log.out("멈추려면 Ctrl+C\n")
    try:
        build_app().launch(
            server_port=port,
            share=args.share,
            inbrowser=not args.no_browser,
            theme=gr.themes.Soft(),
            css=CSS,
            allowed_paths=[str(Path.home()), "/tmp"],
            max_file_size=None,
        )
    except KeyboardInterrupt:
        log.info("종료")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
