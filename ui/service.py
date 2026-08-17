"""UI가 부르는 순수 로직. **gradio를 import하지 않는다.**

화면 조립은 `ui/app.py`가 하고, 여기서는 경로 해석·비용 계산·파이프라인 호출만 한다.
그래야 gradio 없이도 테스트가 돈다.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass, replace
from pathlib import Path

from vlogsrt import env, pipeline, preprocess, transcribe
from vlogsrt.config import Config, load_config
from vlogsrt.models import Cue, Issue
from vlogsrt.output import format_timecode
from vlogsrt.report import dictionary_candidates
from vlogsrt.validate import ERR, WARN

VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".m4a", ".mp3", ".wav")
_QUOTES = "\"'“”‘’"


class UiError(RuntimeError):
    """사람에게 그대로 보여줄 오류. 화면단에서 배너로 바꾼다."""


# ─────────────────────────────────────────────────────────────
#  경로 해석
# ─────────────────────────────────────────────────────────────


def resolve_path(raw: str) -> Path:
    """사람이 붙여넣는 온갖 형태를 받아준다.

    터미널에서 드래그하면 따옴표와 이스케이프가 붙고, Finder에서 복사하면
    앞뒤 공백이 붙는다. 그걸 매번 손으로 지우게 하면 UI를 만든 의미가 없다.
    """
    text = (raw or "").strip().strip(_QUOTES).strip()
    if not text:
        raise UiError("영상 경로를 입력하세요.")
    if text.startswith("file://"):
        from urllib.parse import unquote, urlparse

        text = unquote(urlparse(text).path)
    text = text.replace("\\ ", " ").replace("\\~", "~")
    return Path(text).expanduser()


def recorded_stems() -> list[str]:
    """이미 녹화된 회차. 이 목록에 있으면 무료로 몇 번이든 돌릴 수 있다."""
    fixture_dir = env.fixture_dir()
    if not fixture_dir.is_dir():
        return []
    return sorted(
        path.name.removesuffix(".whisper.json") for path in fixture_dir.glob("*.whisper.json")
    )


def has_fixture(stem: str) -> bool:
    return (env.fixture_dir() / f"{stem}.whisper.json").is_file()


# ─────────────────────────────────────────────────────────────
#  ① 확인
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Inspection:
    path: Path
    stem: str
    exists: bool
    recorded: bool
    duration: float
    estimated_cost: float
    note: str

    @property
    def ready_free(self) -> bool:
        return self.recorded

    @property
    def ready_paid(self) -> bool:
        return self.exists


def inspect_source(raw_path: str, cfg: Config) -> Inspection:
    """돈 쓰기 전에 보는 화면. ffprobe만 부르고 오디오는 추출하지 않는다."""
    path = resolve_path(raw_path)
    stem = path.stem
    recorded = has_fixture(stem)
    exists = path.is_file()

    if not exists and not recorded:
        raise UiError(
            f"파일이 없습니다: {path}\n"
            "경로를 확인하거나, 이미 녹화한 회차라면 아래 목록에서 고르세요."
        )
    if exists and path.suffix.lower() not in VIDEO_SUFFIXES:
        raise UiError(f"영상/오디오 파일이 아닌 것 같습니다: {path.suffix or '확장자 없음'}")

    duration, note = _duration_of(path, stem, exists, recorded)
    return Inspection(
        path=path,
        stem=stem,
        exists=exists,
        recorded=recorded,
        duration=duration,
        estimated_cost=transcribe.estimate_cost(duration, cfg),
        note=note,
    )


def _duration_of(path: Path, stem: str, exists: bool, recorded: bool) -> tuple[float, str]:
    if recorded:
        meta = transcribe.load_meta(env.fixture_dir(), stem)
        if meta.get("duration"):
            return float(meta["duration"]), "녹화된 회차 — 영상 파일 없이도 돌아갑니다."
    if not exists:
        return 0.0, "녹화 기록은 있으나 길이를 알 수 없습니다."
    try:
        return preprocess.probe_duration(path), ""
    except RuntimeError as exc:
        raise UiError(f"길이를 읽지 못했습니다.\n{exc}") from exc


def describe(inspection: Inspection) -> str:
    minutes, seconds = divmod(int(inspection.duration), 60)
    lines = [
        f"### {inspection.path.name}",
        "",
        f"- 경로 · `{inspection.path}`",
        f"- 길이 · {minutes}분 {seconds}초",
    ]
    if inspection.recorded:
        lines.append("- 녹화 · **완료** — 자막 만들기는 무료입니다")
    else:
        lines.append(f"- 녹화 · **아직 안 함** — 첫 실행에 약 **${inspection.estimated_cost:.3f}**")
    if inspection.note:
        lines.extend(["", inspection.note])
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  설정
# ─────────────────────────────────────────────────────────────


def build_config(
    preset: str,
    max_chars: int,
    max_lines: int,
    max_cps: float,
    keywords_text: str,
) -> Config:
    cfg = load_config(
        preset=preset or None,
        overrides={
            "max_chars_per_line": int(max_chars),
            "max_lines": int(max_lines),
            "max_cps": float(max_cps),
        },
    )
    entries = parse_keywords_box(keywords_text)
    return replace(cfg, keywords=tuple(entries)) if entries else cfg


def parse_keywords_box(text: str) -> list[str]:
    """줄바꿈이든 쉼표든 받는다. 사람에게 형식을 외우게 하지 않는다."""
    raw = (text or "").replace(",", "\n")
    seen = [line.split("#", 1)[0].strip() for line in raw.splitlines()]
    return list(dict.fromkeys(entry for entry in seen if entry))


# ─────────────────────────────────────────────────────────────
#  ②③ 실행
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class Outcome:
    stem: str
    video: str
    srt_path: Path
    review_path: Path
    report_path: Path
    trace_path: Path
    srt_text: str
    review_text: str
    report_markdown: str
    summary: str
    review_rows: list[list[str]]
    warning_rows: list[list[str]]
    dictionary_rows: list[list[str]]

    @property
    def video_for_preview(self) -> str | None:
        return self.video if Path(self.video).is_file() else None


def generate(raw_path: str, cfg: Config, outdir_text: str, fast: bool = False) -> Outcome:
    """무료 경로. 녹화된 픽스처로만 돈다."""
    # 녹화 여부를 먼저 본다. 안 된 파일이면 ffprobe로 시간을 쓸 이유도,
    # 깨진 파일 오류로 사람을 헷갈리게 할 이유도 없다.
    path = resolve_path(raw_path)
    if not has_fixture(path.stem):
        raise UiError(
            f"'{path.stem}' 은 아직 녹화되지 않았습니다.\n"
            "아래 '녹화하고 만들기'를 쓰세요. 실제 API를 부르고 요금이 발생합니다."
        )
    inspection = inspect_source(raw_path, cfg)
    outdir = _outdir(outdir_text, inspection.stem)
    result = pipeline.run(inspection.path, cfg, outdir, force_offline=True, fast=fast)
    return _to_outcome(result, inspection.path)


def record_and_generate(raw_path: str, cfg: Config, outdir_text: str, agreed: bool) -> Outcome:
    """유료 경로. 동의 없이는 한 발짝도 나가지 않는다."""
    inspection = inspect_source(raw_path, cfg)
    if not agreed:
        raise UiError("유료 호출입니다. 비용 동의에 체크해야 진행합니다.")
    if not inspection.exists:
        raise UiError(f"녹화하려면 영상 파일이 있어야 합니다: {inspection.path}")
    guard = env.cost_guard_seconds()
    if inspection.duration > guard:
        raise UiError(
            f"{inspection.duration / 60:.0f}분은 안전장치({guard / 60:.0f}분)를 넘습니다.\n"
            "짧게 잘라서 먼저 시험하거나, 터미널에서 `vlogsrt record <video> --yes`를 쓰세요."
        )
    pipeline.record_fixtures(inspection.path, cfg)
    outdir = _outdir(outdir_text, inspection.stem)
    result = pipeline.run(inspection.path, cfg, outdir, force_offline=True)
    return _to_outcome(result, inspection.path)


def retune(trace_path_text: str, cfg: Config, outdir_text: str) -> Outcome:
    """조판만 다시. API를 부르지 않아 0.1초면 끝난다."""
    trace_path = Path(trace_path_text or "")
    if not trace_path.is_file():
        raise UiError("먼저 자막을 한 번 만들어야 조판을 다시 할 수 있습니다.")
    stem = trace_path.parent.name or "out"
    result = pipeline.redo(trace_path, cfg, _outdir(outdir_text, stem, keep=True))
    return _to_outcome(result, Path(result.trace.source))


def _outdir(outdir_text: str, stem: str, keep: bool = False) -> Path:
    base = Path((outdir_text or "out").strip()).expanduser()
    return base if keep and base.name == stem else base / stem


def _to_outcome(result: pipeline.RunResult, video: Path) -> Outcome:
    cues = list(result.cues)
    outputs = result.outputs
    srt_path = outputs["srt"]
    review_path = outputs.get("review", srt_path)
    report_path = outputs.get("report", srt_path)
    return Outcome(
        stem=srt_path.stem,
        video=str(video),
        srt_path=srt_path,
        review_path=review_path,
        report_path=report_path,
        trace_path=outputs["trace"],
        srt_text=srt_path.read_text(encoding="utf-8"),
        review_text=review_path.read_text(encoding="utf-8"),
        report_markdown=report_path.read_text(encoding="utf-8") if report_path.is_file() else "",
        summary=summarize(result),
        review_rows=priority_rows(cues, result.trace.config_snapshot),
        warning_rows=warning_rows(list(result.issues)),
        dictionary_rows=dictionary_rows(cues, _snapshot_config(result)),
    )


def _snapshot_config(result: pipeline.RunResult) -> Config:
    """실행에 실제로 쓰인 설정을 되살린다. 화면 슬라이더와 어긋나지 않게."""
    snapshot = result.trace.config_snapshot
    minimum = snapshot.get("dict_min_occurrences")
    return Config() if minimum is None else replace(Config(), dict_min_occurrences=int(minimum))


def summarize(result: pipeline.RunResult) -> str:
    cues = list(result.cues)
    errors = sum(1 for issue in result.issues if issue.level == ERR)
    warnings = sum(1 for issue in result.issues if issue.level == WARN)
    threshold = float(result.trace.config_snapshot.get("conf_threshold", 0.75))
    check_count = sum(1 for cue in cues if cue.conf < threshold or cue.flags)
    seconds_each = float(result.trace.config_snapshot.get("review_seconds_per_flag", 20.0))
    excluded = sum(1 for segment in result.trace.spine if segment.verdict != "ok")
    orphans = sum(1 for cue in cues if cue.origin == "orphan")
    parts = [
        f"**큐 {len(cues)}개** · 오류 {errors} · 경고 {warnings}",
        f"확인할 큐 **{check_count}개** (예상 검토 {check_count * seconds_each / 60:.1f}분)",
        f"환청 판정으로 뺀 세그먼트 {excluded}개 · 되살린 고아 큐 {orphans}개",
    ]
    if result.trace.warnings:
        parts.append("⚠️ " + " / ".join(result.trace.warnings))
    return "  \n".join(parts)


def priority_rows(cues: list[Cue], snapshot: dict[str, object]) -> list[list[str]]:
    """신뢰도 낮은 순. whisper와 gpt를 나란히 놓아야 판단이 빠르다."""
    threshold = float(snapshot.get("conf_threshold", 0.75))  # type: ignore[arg-type]
    flagged = sorted(
        (cue for cue in cues if cue.conf < threshold or cue.flags),
        key=lambda cue: (cue.conf, cue.start),
    )
    return [
        [
            str(cue.index),
            format_timecode(cue.start),
            f"{cue.conf:.2f}",
            cue.whisper_text or "—",
            cue.text,
            ", ".join(cue.flags) or "—",
        ]
        for cue in flagged
    ]


def warning_rows(issues: list[Issue]) -> list[list[str]]:
    return [
        [issue.level, issue.code, str(issue.cue_id), issue.message]
        for issue in issues
        if issue.level in (ERR, WARN)
    ]


def dictionary_rows(cues: list[Cue], cfg: Config) -> list[list[str]]:
    """다음 회차 사전 후보. 2회 이상 반복된 불일치만 올라온다."""
    return [
        [candidate.canonical, candidate.heard, f"{candidate.count}회"]
        for candidate in dictionary_candidates(cues, cfg)
    ]


def pick_port(preferred: int, attempts: int = 20) -> int:
    """이미 쓰는 포트면 옆으로 비켜간다.

    앞서 띄운 UI가 남아 있는 일이 흔한데, 그때마다 프로세스를 죽이라고 하는 것보다
    다음 포트로 뜨고 알려주는 편이 낫다.
    """
    for offset in range(attempts):
        candidate = preferred + offset
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", candidate))
            except OSError:
                continue
        return candidate
    raise UiError(
        f"{preferred}부터 {preferred + attempts - 1}까지 빈 포트가 없습니다. "
        "--port 로 다른 번호를 지정하세요."
    )


def mode_banner() -> str:
    if env.is_offline():
        return "🟢 **오프라인 모드** — 녹화된 회차는 무료로 몇 번이든 돌릴 수 있습니다."
    return "🟠 **온라인 모드** (`VLOGSRT_OFFLINE=0`) — 녹화 버튼이 실제 요금을 발생시킵니다."
