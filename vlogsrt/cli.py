"""커맨드라인. 파싱과 출력만 하고, 계산은 전부 다른 모듈에 맡긴다."""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__, env, evaluate, keywords, log, pipeline, preprocess, spans, transcribe
from .config import Config, format_config, load_config
from .evaluate import DASHBOARD_HEADER, build_dashboard_row
from .output import parse_srt

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

    again = sub.add_parser("redo", help="API 없이 정제·출력만 재실행")
    again.add_argument("trace", type=Path)
    _add_config_options(again)
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

    probe = sub.add_parser("probe", help="무음·발화 구간·묶음을 본다 (API 없음)")
    probe.add_argument("video", type=Path)
    _add_config_options(probe)

    record = sub.add_parser("record", help="실제 API를 호출해 픽스처를 녹화한다 (유료)")
    record.add_argument("video", type=Path)
    record.add_argument("--yes", action="store_true", help="비용 확인을 건너뛴다")
    _add_config_options(record)

    settings = sub.add_parser("config", help="설정 확인")
    settings.add_argument("action", choices=["show"])
    _add_config_options(settings)

    words = sub.add_parser("keywords", help="사전 확인")
    words.add_argument("action", choices=["show"])
    words.add_argument("--file", type=Path, default=None)
    _add_config_options(words)
    return parser


def _add_config_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--preset", default=None, help="vlog | shorts | lecture")
    parser.add_argument("--max-chars", type=int, default=None, dest="max_chars_per_line")
    parser.add_argument("--max-lines", type=int, default=None, dest="max_lines")
    parser.add_argument("--max-cps", type=float, default=None, dest="max_cps")
    parser.add_argument("--language", default=None)
    parser.add_argument("--keywords-file", type=Path, default=None)
    parser.add_argument("--keywords", default=None, help="쉼표로 구분")


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
    entries = keywords.load_keywords(
        episode_file=getattr(args, "keywords_file", None),
        inline=keywords.split_inline(getattr(args, "keywords", None)),
    )
    return replace(cfg, keywords=tuple(entries)) if entries else cfg


def _resolve_online(args: argparse.Namespace) -> bool:
    if getattr(args, "online", False) and getattr(args, "offline", False):
        raise RuntimeError("--offline과 --online을 함께 쓸 수 없습니다.")
    return bool(getattr(args, "online", False))


def _confirm_cost(duration: float, cfg: Config, assume_yes: bool) -> None:
    cost = transcribe.estimate_cost(duration, cfg)
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


def cmd_run(args: argparse.Namespace) -> int:
    cfg = config_from_args(args)
    online = _resolve_online(args)
    if args.dry_run:
        prepared = pipeline.prepare(args.video, cfg, offline=args.offline or not online)
        cost = transcribe.estimate_cost(prepared.duration, cfg)
        batches = max(1, len(prepared.chunks))
        log.out(f"길이 {prepared.duration:.1f}초 · 묶음 {batches}개 · 예상 ${cost:.3f}")
        return 0
    if online:
        # 오디오를 뽑기 전에 원본 길이만 보고 확인한다. ffprobe는 즉시 끝난다.
        _confirm_cost(preprocess.probe_duration(args.video), cfg, args.yes)
    result = pipeline.run(args.video, cfg, args.out, online=online, force_offline=args.offline)
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
    """API를 부르지 않고 무음 검출까지만 돌려본다. 타이밍이 잡히는지 미리 본다."""
    cfg = config_from_args(args)
    duration = preprocess.probe_duration(args.video)
    workdir = env.work_dir() / "probe"
    audio = preprocess.extract_audio(args.video, workdir / f"{args.video.stem}.mp3", cfg)
    size = audio.stat().st_size
    silences = preprocess.detect_silence(audio, cfg, duration)
    speech = spans.speech_spans(silences, duration, cfg)
    batches = spans.group_spans(speech, size, duration, cfg)
    talk = sum(end - start for start, end in speech)
    log.out(f"길이       {duration:.2f}초")
    log.out(f"오디오     {audio} ({size / 1_000_000:.2f}MB)")
    log.out(f"무음       {len(silences)}구간")
    log.out(f"발화       {len(speech)}구간 · {talk:.1f}초 ({talk / duration * 100:.0f}%)")
    log.out(f"묶음       {len(batches)}개 = 요청 {len(batches)}회")
    pipeline.warn_about_coarse_spans(batches)
    return 0


def cmd_record(args: argparse.Namespace) -> int:
    """사람만 실행한다. 실제 API를 부르고 요금이 발생한다."""
    cfg = config_from_args(args)
    _confirm_cost(preprocess.probe_duration(args.video), cfg, args.yes)
    for path in pipeline.record_fixtures(args.video, cfg).values():
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


_COMMANDS = {
    "run": cmd_run,
    "redo": cmd_redo,
    "eval": cmd_eval,
    "evalset": cmd_evalset,
    "probe": cmd_probe,
    "record": cmd_record,
    "config": cmd_config,
    "keywords": cmd_keywords,
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
