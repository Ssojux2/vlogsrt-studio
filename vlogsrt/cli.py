"""커맨드라인 — `vlogsrt project …`(옛 Final Cut 파생 계층)과 `vlogsrt media …`(작업대 진단)만.

whisper로 자막을 만들던 옛 명령(`run`·`redo`·`eval`·`evalset`·`probe`·`record`·`config`·
`keywords`)은 whisper와 함께 지웠다(D163). 파싱만 하고 계산은 다른 모듈에 맡긴다.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__, env, log
from .project import commands


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vlogsrt", description="브이로그 자막 작업대 진단 도구")
    parser.add_argument("--version", action="version", version=f"vlogsrt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    _add_project_command(sub)
    # 원본 영상 브라우저 v2 — 조립만 여기서, 실행은 project.raw.commands가 맡는다.
    commands.add_media_parser(sub)
    return parser


def _add_project_command(sub: argparse._SubParsersAction) -> None:
    """파생 계층 명령 — cli.py가 보는 것은 `project.commands` 하나다(불변식 18).

    whisper로만 돌던 `analyze`·`caption`·`caption-run`·`run`은 whisper와 함께 지웠다(D163).
    """
    project = sub.add_parser(
        "project", help="Final Cut 프로젝트에서 러프컷 초안 자막을 만든다 (파생 계층)"
    )
    project_commands = project.add_subparsers(
        dest="project_command",
        required=True,
        metavar=("{receive,inspect,review,roughcut,sync-check}"),
    )
    sync = project_commands.add_parser(
        "sync-check",
        help="SRT 자막 시각을 원본 말소리와 대조해 표시만 한다 (무료 로컬 VAD · SRT는 안 바뀐다)",
    )
    sync.add_argument("--media", type=Path, required=True, help="원본 영상")
    sync.add_argument("--srt", type=Path, required=True, help="확인할 SRT (읽기만 한다)")
    sync.add_argument("--audio-ordinal", type=int, default=0, help="오디오 트랙 순번(기본 0)")
    sync.add_argument("--json", action="store_true", help="보고서 JSON을 그대로 인쇄")
    sync.add_argument(
        "--no-write", action="store_true", help="곁파일 <srt>.sync.json도 쓰지 않는다"
    )
    receive = project_commands.add_parser(
        "receive", help="FCPXML 또는 원본 영상 묶음을 작업공간에 수신"
    )
    receive.add_argument("input", type=Path, nargs="?", default=None)
    receive.add_argument(
        "--from-clips",
        type=Path,
        default=None,
        help="원본 영상이 든 디렉터리 — 파일명 순서로 이어 붙인 타임라인을 합성한다",
    )
    receive.add_argument("--workspace", type=Path, required=True)

    inspect = project_commands.add_parser(
        "inspect", help="네트워크 없이 준비를 확인하고 승인이 묶을 범위를 인쇄"
    )
    inspect.add_argument("workspace", type=Path)
    inspect.add_argument("--project-config", type=Path, default=None)
    inspect.add_argument("--media-map", type=Path, default=None)

    review = project_commands.add_parser("review", help="사람이 승인한 후보로 계획 고정")
    review.add_argument("workspace", type=Path)
    review.add_argument("--approve", action="append", default=[])

    roughcut = project_commands.add_parser("roughcut", help="승인 계획을 새 FCPXML로 렌더")
    roughcut.add_argument("workspace", type=Path)
    roughcut.add_argument("-o", "--out", type=Path, required=True)


def cmd_project(args: argparse.Namespace) -> int:
    """파생 계층 — 계산·자격증명·예외 변환은 전부 project.commands의 몫이다."""
    return commands.project_command(args)


def cmd_media(args: argparse.Namespace) -> int:
    """원본 영상 브라우저 v2의 진단 CLI — 조립만 하고 실행은 raw.commands가 맡는다."""
    return commands.media_command(args)


_COMMANDS = {
    "project": cmd_project,
    "media": cmd_media,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env.ensure_loaded()
    try:
        return _COMMANDS[args.command](args)
    except RuntimeError as exc:
        log.error(str(exc))
        return 1
    except KeyboardInterrupt:
        log.error("중단됨")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
