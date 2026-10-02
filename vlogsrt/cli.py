"""커맨드라인. 파싱과 출력만 하고, 계산은 전부 다른 모듈에 맡긴다."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import (
    __version__,
    cutpoints,
    env,
    evaluate,
    keywords,
    log,
    pipeline,
    preprocess,
    repair,
    transcribe,
)
from .config import Config, format_config, load_config
from .evaluate import DASHBOARD_HEADER, build_dashboard_row
from .output import parse_srt
from .project import commands

DASHBOARD_BEGIN = "<!-- vlogsrt:dashboard -->"
DASHBOARD_END = "<!-- /vlogsrt:dashboard -->"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="vlogsrt", description="브이로그 자막 SRT 생성기")
    parser.add_argument("--version", action="version", version=f"vlogsrt {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="전체 파이프라인 실행")
    run.add_argument("video", type=Path)
    _add_config_options(run)
    _add_mode_options(run)
    run.add_argument("-o", "--out", type=Path, default=Path("out"))
    run.add_argument("--dry-run", action="store_true", help="비용 추정만 하고 끝낸다")
    run.add_argument(
        "--no-verify",
        action="store_true",
        help=(
            "교차 확인을 끈다. 표시만 하므로 자막은 안 바뀐다. "
            "글자 교정이 켜져 있으면 두 번째 모델은 그대로 부른다 — 비용은 줄지 않는다."
        ),
    )
    run.add_argument(
        "--no-repair",
        action="store_true",
        help="되풀이 재전사를 끈다. 기본은 켜져 있다 — 되풀이 구간만 다시 물어 갈아끼운다.",
    )
    run.add_argument(
        "--no-retext",
        action="store_true",
        help="글자 교정을 끈다. 기본은 켜져 있다 — 시각은 whisper, 글자는 두 번째 모델(D37).",
    )
    _add_polish_option(run)

    again = sub.add_parser("redo", help="API 없이 정제·출력만 재실행")
    again.add_argument("trace", type=Path)
    _add_config_options(again)
    _add_polish_option(again)
    again.add_argument("-o", "--out", type=Path, default=None)

    scored = sub.add_parser("eval", help="SRT 두 개를 비교한다")
    scored.add_argument("hyp", type=Path)
    scored.add_argument("ref", type=Path)
    scored.add_argument("--json", action="store_true")
    _add_config_options(scored)

    dashboard = sub.add_parser("evalset", help="골든셋 전체를 재고 DASHBOARD.md를 갱신한다")
    dashboard.add_argument("--golden", type=Path, default=Path("golden"))
    dashboard.add_argument("--hyp-dir", type=Path, default=Path("eval"))
    dashboard.add_argument("--out", type=Path, default=Path("docs/DASHBOARD.md"))
    _add_config_options(dashboard)

    probe = sub.add_parser("probe", help="길이와 조각 수를 본다 (API 없음)")
    probe.add_argument("video", type=Path)
    _add_config_options(probe)

    record = sub.add_parser("record", help="실제 API를 호출해 픽스처를 녹화한다 (유료)")
    record.add_argument("video", type=Path)
    record.add_argument("--yes", action="store_true", help="비용 확인을 건너뛴다")
    record.add_argument(
        "--no-verify",
        action="store_true",
        help="검증용 응답은 녹화하지 않는다. 글자 교정이 켜져 있으면 그래도 녹화한다.",
    )
    _add_config_options(record)

    settings = sub.add_parser("config", help="설정 확인")
    settings.add_argument("action", choices=["show"])
    _add_config_options(settings)

    words = sub.add_parser("keywords", help="사전 확인")
    words.add_argument("action", choices=["show"])
    words.add_argument("--file", type=Path, default=None)
    _add_config_options(words)
    _add_project_command(sub)
    # 원본 영상 브라우저 v2 — 조립만 여기서, 실행은 project.raw.commands가 맡는다.
    commands.add_media_parser(sub)
    return parser


def _add_project_command(sub: argparse._SubParsersAction) -> None:
    """파생 계층 명령 — cli.py가 보는 것은 `project.commands` 하나다(불변식 18).

    `run`은 임시다: 최종 표면은 `inspect | cut | approve | caption`이고 사람 게이트
    우회인 `run`을 두지 않는다. 비용 길이 안전장치는 없다(추정은 `inspect`의 몫).
    """
    project = sub.add_parser(
        "project", help="Final Cut 프로젝트에서 러프컷 초안 자막을 만든다 (파생 계층)"
    )
    project_commands = project.add_subparsers(
        dest="project_command",
        required=True,
        metavar="{receive,inspect,analyze,review,roughcut,caption,caption-estimate,caption-run}",
    )
    run = project_commands.add_parser(
        "run",
        help=argparse.SUPPRESS,
    )
    run.add_argument("input", type=Path, help="Final Cut 프로젝트(.fcpxml)")
    run.add_argument(
        "-o",
        "--out",
        type=Path,
        required=True,
        help="산출 뿌리 — artifacts/와 package/가 형제로 생긴다",
    )
    run.add_argument(
        "--media-map", type=Path, default=None, help="미디어 맵 JSON (resource_id 경로 지정)"
    )
    run.add_argument(
        "--project-config",
        type=Path,
        default=None,
        help="vlogsrt.project.toml 경로 — 없으면 cwd의 기본 경로",
    )
    run.add_argument(
        "--dtd", type=Path, default=None, help="DTD 경로 — 주지 않으면 대조하지 않는다"
    )
    run.add_argument(
        "--vision-fixtures",
        type=Path,
        default=None,
        help="비전 판정 재생 — <block_id>.vision.json 이 담긴 디렉터리. 유료 호출 0회",
    )
    mode = run.add_mutually_exclusive_group()
    mode.add_argument("--offline", action="store_true", help="픽스처만 사용 (기본값)")
    mode.add_argument(
        "--online", action="store_true", help="실제 API 호출. 유료. 길이 안전장치 없음"
    )
    # 진단 호환 명령은 파싱되지만 도움말 선택지에서는 숨긴다.
    project_commands._choices_actions = [
        action for action in project_commands._choices_actions if action.dest != "run"
    ]

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

    inspect = project_commands.add_parser("inspect", help="네트워크 없이 분석 비용 상한 산출")
    inspect.add_argument("workspace", type=Path)
    inspect.add_argument("--project-config", type=Path, default=None)
    inspect.add_argument("--media-map", type=Path, default=None)

    analyze = project_commands.add_parser("analyze", help="승인된 온라인 또는 픽스처 분석")
    analyze.add_argument("workspace", type=Path)
    analyze.add_argument("--candidates", type=Path, default=None, help=argparse.SUPPRESS)
    analyze.add_argument("--authorization", type=Path, default=None)
    analyze.add_argument("--vision-fixtures", type=Path, default=None)
    analyze.add_argument("--project-config", type=Path, default=None)
    analyze.add_argument("--media-map", type=Path, default=None)
    analyze_mode = analyze.add_mutually_exclusive_group()
    analyze_mode.add_argument("--offline", action="store_true", help="픽스처만 사용")
    analyze_mode.add_argument(
        "--online", action="store_true", help="승인 JSONL에 묶인 실제 API 호출"
    )

    review = project_commands.add_parser("review", help="사람이 승인한 후보로 계획 고정")
    review.add_argument("workspace", type=Path)
    review.add_argument("--approve", action="append", default=[])

    roughcut = project_commands.add_parser("roughcut", help="승인 계획을 새 FCPXML로 렌더")
    roughcut.add_argument("workspace", type=Path)
    roughcut.add_argument("-o", "--out", type=Path, required=True)

    caption = project_commands.add_parser("caption", help="최종 공유 오디오로 한국어 SRT 생성")
    caption.add_argument("workspace", type=Path)
    caption.add_argument("render_audio", type=Path)
    caption.add_argument("final_fcpxml", type=Path)
    caption.add_argument("-o", "--out", type=Path, required=True)
    caption.add_argument("--yes", action="store_true", help="표시된 온라인 예상 비용 확인")
    caption.add_argument(
        "--caption-authorization",
        type=Path,
        default=None,
        help="CaptionAuthorization JSONL — 온라인 자막은 승인이 필요하다",
    )
    caption_mode = caption.add_mutually_exclusive_group()
    caption_mode.add_argument("--offline", action="store_true", help="픽스처만 사용 (기본값)")
    caption_mode.add_argument("--online", action="store_true", help="실제 API 호출. 유료")

    # 공유 받기로 시작한 자막 job의 두 문(S4A) — 러프컷 workflow가 없는 job에서
    # 추정과 실행이 각각 승인 문을 지난다. 위 `caption`(통합 흐름)과 자리가 다르다.
    caption_estimate = project_commands.add_parser(
        "caption-estimate",
        help="스테이징된 최종 내보내기의 자막 비용 고정 (전사 없음, 키 없음)",
    )
    caption_estimate.add_argument(
        "job_dir",
        type=Path,
        help="final.wav·final.fcpxml·caption-origin.json이 스테이징된 job 폴더",
    )

    caption_run = project_commands.add_parser(
        "caption-run", help="승인된 자막 실행 — 최종 오디오로 SRT 생성"
    )
    caption_run.add_argument(
        "job_dir", type=Path, help="추정이 고정된 자막 job 폴더 (승인 문서 포함)"
    )
    caption_run.add_argument(
        "-o", "--out", type=Path, required=True, help="SRT·검토 보고서가 나가는 폴더"
    )
    caption_run.add_argument(
        "--project-config", type=Path, default=None, help="vlogsrt.project.toml 경로"
    )
    caption_run_mode = caption_run.add_mutually_exclusive_group()
    caption_run_mode.add_argument(
        "--offline", action="store_true", help="픽스처만 사용 — 유료 호출 0회"
    )
    caption_run_mode.add_argument(
        "--online", action="store_true", help="승인 문서에 묶인 실제 API 호출. 유료"
    )


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", default=None, help="vlog | shorts | lecture")
    parser.add_argument("--max-chars", type=int, default=None, dest="max_chars_per_line")
    parser.add_argument("--max-lines", type=int, default=None, dest="max_lines")
    parser.add_argument("--max-cps", type=float, default=None, dest="max_cps")
    parser.add_argument("--language", default=None)
    parser.add_argument("--keywords-file", type=Path, default=None)
    parser.add_argument("--keywords", default=None, help="쉼표로 구분")


def _add_polish_option(parser: argparse.ArgumentParser) -> None:
    """자막을 만드는 두 경로(`run`·`redo`)에만 붙인다. 나머지 명령은 조판을 하지 않는다."""
    parser.add_argument(
        "--no-polish",
        action="store_true",
        help="조판 정리를 끈다. 기본은 켜져 있다 — 온점을 지우고 부호를 다듬는다.",
    )


def _add_mode_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--offline", action="store_true", help="픽스처만 사용 (기본값)")
    parser.add_argument("--online", action="store_true", help="실제 API 호출. 유료.")
    parser.add_argument("--yes", action="store_true", help="비용 확인을 건너뛴다")


def config_from_args(args: argparse.Namespace) -> Config:
    overrides: dict[str, Any] = {
        key: getattr(args, key, None)
        for key in ("max_chars_per_line", "max_lines", "max_cps", "language")
    }
    cfg = load_config(preset=getattr(args, "preset", None), overrides=overrides)
    if getattr(args, "no_polish", False):
        cfg = replace(cfg, polish_enabled=False)
    entries = keywords.load_keywords(
        episode_file=getattr(args, "keywords_file", None),
        inline=keywords.split_inline(getattr(args, "keywords", None)),
    )
    return replace(cfg, keywords=tuple(entries)) if entries else cfg


def _resolve_online(args: argparse.Namespace) -> bool:
    if getattr(args, "online", False) and getattr(args, "offline", False):
        raise RuntimeError("--offline과 --online을 함께 쓸 수 없습니다.")
    return bool(getattr(args, "online", False))


def _confirm_cost(duration: float, cfg: Config, assume_yes: bool, models: int = 1) -> None:
    cost = transcribe.estimate_cost(duration, cfg) * models
    guard = env.cost_guard_seconds()
    log.info(f"오디오 {duration:.0f}초, 예상 비용 약 ${cost:.3f}")
    if assume_yes or duration <= guard:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            f"{duration:.0f}초는 안전장치({guard:.0f}초)를 넘습니다. 계속하려면 --yes를 붙이세요."
        )
    answer = input(f"{duration / 60:.1f}분 분량입니다. 계속할까요? [y/N] ").strip().lower()
    if answer not in {"y", "yes"}:
        raise RuntimeError("사용자가 중단했습니다.")


# ─────────────────────────────────────────────────────────────
#  커맨드
# ─────────────────────────────────────────────────────────────


def _models_paid(args: argparse.Namespace, cfg: Config) -> int:
    """두 번째 모델은 검증과 글자 교정이 **함께** 쓴다. 둘 다 꺼야 한 번만 낸다(D37)."""
    return 1 if args.no_verify and not cfg.retext_enabled else 2


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    if args.no_retext:
        cfg = replace(cfg, retext_enabled=False)
    online = _resolve_online(args)
    if args.dry_run:
        prepared = pipeline.prepare(args.video, cfg, offline=args.offline or not online)
        cost = transcribe.estimate_cost(prepared.duration, cfg) * _models_paid(args, cfg)
        pieces = max(1, len(prepared.chunks))
        log.out(
            f"길이 {prepared.duration:.1f}초 · 조각 {pieces}개"
            f"(각 {prepared.duration / pieces / 60:.1f}분) · 예상 ${cost:.3f}"
        )
        if not args.no_repair:
            # 되풀이가 얼마나 나올지는 전사해봐야 안다. 0원일 수도, 26%일 수도 있다(D27).
            log.out("되풀이 재전사가 켜져 있다 — 되풀이 구간 길이만큼 비용이 더 붙는다.")
        return 0
    if online:
        # 오디오를 뽑기 전에 원본 길이만 보고 확인한다. ffprobe는 즉시 끝난다.
        _confirm_cost(preprocess.probe_duration(args.video), cfg, args.yes, _models_paid(args, cfg))
    result = pipeline.run(
        args.video,
        cfg,
        args.out,
        online=online,
        force_offline=args.offline,
        verify_with_second_model=not args.no_verify,
        repair_repeats=not args.no_repair,
    )
    if result.repairs:
        log.out(repair.summarize(result.repairs))
    for name, path in result.outputs.items():
        if not name.endswith("_alias"):
            log.out(f"{name}: {path}")
    return 0


def cmd_redo(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    outdir = args.out or args.trace.parent
    result = pipeline.redo(args.trace, cfg, outdir)
    log.out(f"srt: {result.outputs['srt']}")
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    metrics = evaluate.evaluate_cues(
        parse_srt(args.hyp.read_text(encoding="utf-8")),
        parse_srt(args.ref.read_text(encoding="utf-8")),
        cfg,
    )
    if args.json:
        log.out(json.dumps(metrics.to_dict(), ensure_ascii=False, indent=2))
    else:
        log.out(evaluate.format_metrics(metrics))
    return 0


def cmd_evalset(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    rows: list[str] = []
    for variant in sorted(p for p in args.hyp_dir.glob("*") if p.is_dir()):
        for reference in sorted(args.golden.glob("*.srt")):
            candidate = variant / f"{reference.stem}.srt"
            if not candidate.is_file():
                continue
            metrics = evaluate.evaluate_cues(
                parse_srt(candidate.read_text(encoding="utf-8")),
                parse_srt(reference.read_text(encoding="utf-8")),
                cfg,
            )
            rows.append(build_dashboard_row(reference.stem, variant.name, metrics.to_dict()))
    if not rows:
        log.warn(f"{args.hyp_dir} 아래에서 비교할 SRT를 찾지 못했다.")
    _write_dashboard(args.out, rows)
    log.out(f"대시보드 갱신: {args.out} ({len(rows)}행)")
    return 0


def _write_dashboard(path: Path, rows: list[str]) -> None:
    table = "\n".join([DASHBOARD_BEGIN, DASHBOARD_HEADER, *rows, DASHBOARD_END])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file():
        body = path.read_text(encoding="utf-8")
        if DASHBOARD_BEGIN in body and DASHBOARD_END in body:
            head = body.split(DASHBOARD_BEGIN)[0]
            tail = body.split(DASHBOARD_END)[1]
            path.write_text(f"{head}{table}{tail}", encoding="utf-8")
            return
    path.write_text(f"# 측정 대시보드\n\n{table}\n", encoding="utf-8")


def cmd_probe(args: argparse.Namespace) -> int:
    """API를 부르지 않고 길이와 조각 수만 본다."""
    cfg = config_from_args(args)
    duration = preprocess.probe_duration(args.video)
    workdir = env.work_dir() / "probe"
    audio = preprocess.extract_audio(args.video, workdir / f"{args.video.stem}.mp3", cfg)
    size = audio.stat().st_size
    pieces = cutpoints.chunk_count(size, duration, cfg)
    log.out(f"길이       {duration:.2f}초")
    log.out(f"오디오     {audio} ({size / 1_000_000:.2f}MB)")
    log.out(
        f"조각       {pieces}개 · 각 {duration / pieces / 60:.1f}분 "
        f"(한도 {cfg.chunk_max_bytes / 1e6:.1f}MB / {cfg.chunk_max_seconds / 60:.0f}분)"
    )
    log.out(f"예상 비용   ${transcribe.estimate_cost(duration, cfg):.3f}")
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """사람만 실행한다. 실제 API를 부르고 요금이 발생한다."""
    cfg = config_from_args(args)
    _confirm_cost(preprocess.probe_duration(args.video), cfg, args.yes, _models_paid(args, cfg))
    # 글자 교정이 켜져 있으면 오프라인 재생에도 두 번째 모델 응답이 필요하다(D37).
    wanted = not args.no_verify or cfg.retext_enabled
    for path in pipeline.record_fixtures(args.video, cfg, verify=wanted).values():
        log.out(str(path))
    return 0


def cmd_config(args: argparse.Namespace) -> int:
    log.out(format_config(config_from_args(args)))
    return 0


def cmd_keywords(args: argparse.Namespace) -> int:
    entries = keywords.load_keywords(
        episode_file=args.file, inline=keywords.split_inline(args.keywords)
    )
    for entry in entries:
        log.out(entry)
    log.info(f"{len(entries)}개")
    return 0


def cmd_project(args: argparse.Namespace) -> int:
    """파생 계층 — 계산·자격증명·예외 변환은 전부 project.commands의 몫이다."""
    return commands.project_command(args)


def cmd_media(args: argparse.Namespace) -> int:
    """원본 영상 브라우저 v2의 진단 CLI — 조립만 하고 실행은 raw.commands가 맡는다."""
    return commands.media_command(args)


_COMMANDS = {
    "run": cmd_run,
    "redo": cmd_redo,
    "eval": cmd_eval,
    "evalset": cmd_evalset,
    "probe": cmd_probe,
    "record": cmd_record,
    "config": cmd_config,
    "keywords": cmd_keywords,
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
