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
from ui.service import UiError
from vlogsrt import log

CSS = """
#exec-log textarea { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 13px; }
footer { display: none !important; }
"""


def build_app() -> gr.Blocks:
    with gr.Blocks(title="vlogsrt") as app:  # theme/css는 gradio 6부터 launch()로 옮겨졌다
        gr.Markdown(f"### vlogsrt  ·  {service.status_line()}")

        with gr.Row():
            path_box = gr.Textbox(
                label="영상 경로",
                info="찾아보기로 고르거나 경로를 직접 붙여넣으세요. 업로드하지 않습니다.",
                placeholder="/Users/me/Movies/vlog01.mp4",
                scale=6,
                autofocus=True,
            )
            browse_button = gr.Button("찾아보기", scale=1)

        with gr.Row():
            prompt_box = gr.Textbox(
                label="whisper 힌트 (선택)",
                info=(
                    "지시문이 아니라 **영상에 나올 법한 단어**를 적으세요. "
                    "whisper는 이 값을 직전 자막으로 보고 이어 씁니다 — "
                    '"구어체로 써줘" 같은 문장을 넣으면 그 문장을 되풀이합니다.'
                ),
                placeholder="망원동, 블루펭귄, 아페로, 코나",
                lines=1,
                scale=7,
            )
            run_button = gr.Button("자막 만들기", variant="primary", scale=1)

        log_box = gr.Textbox(
            label="실행 로그", lines=10, max_lines=20, interactive=False, elem_id="exec-log"
        )
        srt_box = gr.Code(label="SRT", lines=24)
        files_box = gr.File(label="저장된 파일", file_count="multiple", height=110)

        # ── 동작 ──────────────────────────────────────────────
        def on_run(path_text: str, prompt_text: str) -> Any:
            """제너레이터로 돌려 진행 상황을 실시간으로 흘린다.

            한 번에 반환하면 긴 영상에서 몇 분 동안 화면이 비어 멈춘 것처럼 보인다.
            """
            for report in service.stream_subtitles(path_text, prompt_text):
                yield report.log, report.srt_text, list(report.files)

        def on_browse() -> Any:
            """OS 파일 대화상자를 열어 실제 경로를 받아온다. 취소하면 그대로 둔다."""
            try:
                chosen = service.pick_file_dialog()
            except UiError as exc:
                raise gr.Error(str(exc)) from exc
            return chosen or gr.skip()

        inputs = [path_box, prompt_box]
        outputs = [log_box, srt_box, files_box]
        browse_button.click(on_browse, None, path_box)
        run_button.click(on_run, inputs, outputs)
        path_box.submit(on_run, inputs, outputs)
        prompt_box.submit(on_run, inputs, outputs)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlogsrt-ui", description="vlogsrt 웹 UI")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--share", action="store_true", help="외부 공유 링크 생성")
    parser.add_argument("--no-browser", action="store_true", help="브라우저를 열지 않는다")
    args = parser.parse_args(argv)

    stopped = service.stop_stale_ui(args.port)
    if stopped:
        log.warn(f"이전에 띄운 UI를 정리했습니다 (pid {', '.join(map(str, stopped))}).")
        log.warn("브라우저는 한 번 강력 새로고침 하세요 — Cmd+Shift+R")

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
