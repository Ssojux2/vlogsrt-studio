"""gradio 화면. 조립만 하고 계산은 `ui/service.py`가 한다.

이 파일은 `vlogsrt/` 밖에 있다. 핵심 패키지의 의존성은 여전히 3개다(불변 규칙 7).
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

PRESETS = ["vlog", "shorts", "lecture"]
REVIEW_HEADERS = ["#", "시각", "신뢰도", "whisper가 들은 것", "gpt-transcribe (최종)", "표시"]
WARNING_HEADERS = ["수준", "코드", "큐", "내용"]
DICT_HEADERS = ["정본", "whisper가 들은 것", "횟수"]

CSS = """
.paid-zone { border: 1px solid var(--color-accent); border-radius: 8px; padding: 12px; }
footer { display: none !important; }
"""


def _preview(outcome: service.Outcome) -> Any:
    """자막을 얹은 영상 미리보기.

    `gr.update(subtitles=...)`는 경로 문자열을 그대로 보내서 프런트가 못 읽는다.
    컴포넌트 인스턴스를 돌려주면 생성자가 파일을 FileData로 감싸준다.
    """
    video = outcome.video_for_preview
    if not video:
        return gr.Video(value=None, label="원본 영상이 없어 미리보기를 건너뜁니다", height=360)
    return gr.Video(value=video, subtitles=str(outcome.srt_path), label="자막 미리보기", height=360)


def _spread(outcome: service.Outcome) -> tuple[Any, ...]:
    return (
        outcome.summary,
        _preview(outcome),
        outcome.srt_text,
        outcome.review_text,
        outcome.report_markdown,
        outcome.review_rows,
        outcome.warning_rows,
        outcome.dictionary_rows,
        str(outcome.trace_path),
        gr.update(interactive=True),
    )


def build_app(root_dir: Path) -> gr.Blocks:
    with gr.Blocks(title="vlogsrt") as app:  # theme/css는 gradio 6부터 launch()로 옮겨졌다
        gr.Markdown("# vlogsrt — 브이로그 자막 만들기")
        gr.Markdown(service.mode_banner())
        trace_state = gr.Textbox(visible=False)

        # ── ① 영상 고르기 ────────────────────────────────────
        gr.Markdown("## ① 영상 고르기")
        with gr.Row():
            path_box = gr.Textbox(
                label="영상 경로",
                placeholder="/Users/me/Movies/vlog01.mp4 (드래그해 붙여넣어도 됩니다)",
                scale=5,
                autofocus=True,
            )
            inspect_button = gr.Button("확인", variant="secondary", scale=1)

        with gr.Row():
            recorded_box = gr.Dropdown(
                label="이미 녹화한 회차 (무료로 다시 돌릴 수 있습니다)",
                choices=service.recorded_stems(),
                interactive=True,
                scale=4,
            )
            refresh_button = gr.Button("목록 새로고침", scale=1)

        with gr.Accordion("파일 찾아보기", open=False):
            explorer = gr.FileExplorer(
                glob="**/*.{mp4,mov,m4v,mkv,webm,mp3,m4a,wav}",
                root_dir=str(root_dir),
                file_count="single",
                label=f"기준 폴더: {root_dir}",
                height=280,
            )

        info_panel = gr.Markdown("경로를 넣고 **확인**을 누르세요.")

        # ── ② 만들기 ─────────────────────────────────────────
        gr.Markdown("## ② 자막 만들기")
        with gr.Row():
            preset_box = gr.Dropdown(PRESETS, value="vlog", label="프리셋", scale=1)
            chars_slider = gr.Slider(8, 28, value=16, step=1, label="한 줄 최대 글자", scale=2)
            cps_slider = gr.Slider(6, 20, value=12, step=0.5, label="최대 CPS (읽기 속도)", scale=2)
            lines_slider = gr.Slider(1, 3, value=2, step=1, label="최대 줄 수", scale=1)
        with gr.Row():
            keywords_box = gr.Textbox(
                label="고유명사 사전 (줄바꿈 또는 쉼표)",
                placeholder="블루펭귄\n망원동",
                lines=2,
                scale=3,
            )
            outdir_box = gr.Textbox(label="저장 폴더", value="out", scale=1)

        make_button = gr.Button("자막 만들기 (무료)", variant="primary")

        with (
            gr.Accordion("아직 녹화하지 않은 영상이라면 — 유료", open=False),
            gr.Column(elem_classes="paid-zone"),
        ):
            gr.Markdown(
                "실제 OpenAI API를 부릅니다. **회차당 한 번만** 하면 되고, "
                "그 뒤로는 위의 무료 버튼으로 몇 번이든 다시 만들 수 있습니다."
            )
            agree_box = gr.Checkbox(label="비용이 발생하는 것에 동의합니다", value=False)
            record_button = gr.Button("녹화하고 만들기 (유료)", variant="stop")

        # ── ③ 결과 ───────────────────────────────────────────
        gr.Markdown("## ③ 결과")
        summary_panel = gr.Markdown("아직 만든 자막이 없습니다.")
        with gr.Row():
            video_player = gr.Video(label="자막 미리보기", height=360, scale=3)
            with gr.Column(scale=2):
                retune_button = gr.Button("조판만 다시 (무료·즉시)", interactive=False)
                gr.Markdown("위 슬라이더를 바꾼 뒤 누르세요. API를 부르지 않아 곧바로 끝납니다.")
                download_files = gr.File(label="내려받기", file_count="multiple", height=140)

        with gr.Tabs():
            with gr.Tab("우선 확인"):
                review_table = gr.Dataframe(
                    headers=REVIEW_HEADERS, wrap=True, interactive=False, max_height=420
                )
            with gr.Tab("자막 (SRT)"):
                srt_view = gr.Code(label="output.srt", lines=20)
            with gr.Tab("검토용 (갈린 글자만 [?])"):
                review_view = gr.Code(label="output.review.srt", lines=20)
            with gr.Tab("리포트"):
                report_view = gr.Markdown()
            with gr.Tab("경고"):
                warning_table = gr.Dataframe(
                    headers=WARNING_HEADERS, wrap=True, interactive=False, max_height=420
                )
            with gr.Tab("사전 후보"):
                dict_table = gr.Dataframe(
                    headers=DICT_HEADERS, wrap=True, interactive=False, max_height=420
                )

        outputs = [
            summary_panel,
            video_player,
            srt_view,
            review_view,
            report_view,
            review_table,
            warning_table,
            dict_table,
            trace_state,
            retune_button,
        ]

        # ── 동작 연결 ─────────────────────────────────────────
        def on_inspect(path_text: str, preset: str, chars: int, lines: int, cps: float) -> str:
            cfg = service.build_config(preset, chars, lines, cps, "")
            try:
                return service.describe(service.inspect_source(path_text, cfg))
            except UiError as exc:
                raise gr.Error(str(exc)) from exc

        def on_make(
            path_text: str,
            preset: str,
            chars: int,
            lines: int,
            cps: float,
            keywords: str,
            outdir: str,
            progress: gr.Progress = gr.Progress(),  # noqa: B008 - gradio 관례
        ) -> tuple[Any, ...]:
            progress(0.2, desc="골격과 정본을 읽는 중")
            cfg = service.build_config(preset, chars, lines, cps, keywords)
            try:
                outcome = service.generate(path_text, cfg, outdir)
            except UiError as exc:
                raise gr.Error(str(exc)) from exc
            except RuntimeError as exc:
                raise gr.Error(f"실행 실패\n{exc}") from exc
            progress(1.0, desc="완료")
            return _spread(outcome)

        def on_record(
            path_text: str,
            preset: str,
            chars: int,
            lines: int,
            cps: float,
            keywords: str,
            outdir: str,
            agreed: bool,
            progress: gr.Progress = gr.Progress(),  # noqa: B008 - gradio 관례
        ) -> tuple[Any, ...]:
            cfg = service.build_config(preset, chars, lines, cps, keywords)
            progress(0.1, desc="오디오 추출 중")
            try:
                progress(0.3, desc="API 호출 중 — 영상 길이에 따라 몇 분 걸릴 수 있습니다")
                outcome = service.record_and_generate(path_text, cfg, outdir, agreed)
            except UiError as exc:
                raise gr.Error(str(exc)) from exc
            except RuntimeError as exc:
                raise gr.Error(f"녹화 실패\n{exc}") from exc
            progress(1.0, desc="완료")
            return _spread(outcome)

        def on_retune(
            trace_path: str, preset: str, chars: int, lines: int, cps: float, outdir: str
        ) -> tuple[Any, ...]:
            cfg = service.build_config(preset, chars, lines, cps, "")
            try:
                return _spread(service.retune(trace_path, cfg, outdir))
            except UiError as exc:
                raise gr.Error(str(exc)) from exc

        def on_download(trace_path: str) -> list[str]:
            """회차 이름 파일만 올린다. `output.*`는 같은 내용의 별칭이라 뺀다."""
            if not trace_path:
                return []
            folder = Path(trace_path).parent
            wanted = {".srt", ".md"}
            return [
                str(path)
                for path in sorted(folder.glob("*"))
                if path.suffix in wanted and not path.name.startswith("output.")
            ]

        knobs = [preset_box, chars_slider, lines_slider, cps_slider]
        make_inputs = [path_box, *knobs, keywords_box, outdir_box]

        inspect_button.click(on_inspect, [path_box, *knobs], info_panel)
        path_box.submit(on_inspect, [path_box, *knobs], info_panel)
        make_button.click(on_make, make_inputs, outputs).then(
            on_download, trace_state, download_files
        )
        record_button.click(on_record, [*make_inputs, agree_box], outputs).then(
            on_download, trace_state, download_files
        )
        retune_button.click(on_retune, [trace_state, *knobs, outdir_box], outputs).then(
            on_download, trace_state, download_files
        )

        refresh_button.click(
            lambda: gr.update(choices=service.recorded_stems()), None, recorded_box
        )
        recorded_box.change(lambda stem: stem or "", recorded_box, path_box).then(
            on_inspect, [path_box, *knobs], info_panel
        )
        explorer.change(lambda picked: picked or "", explorer, path_box)

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="vlogsrt-ui", description="vlogsrt 웹 UI")
    parser.add_argument("--root", type=Path, default=_default_root(), help="파일 탐색 기준 폴더")
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
        build_app(args.root).launch(
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


def _default_root() -> Path:
    movies = Path.home() / "Movies"
    return movies if movies.is_dir() else Path.home()


if __name__ == "__main__":
    raise SystemExit(main())
