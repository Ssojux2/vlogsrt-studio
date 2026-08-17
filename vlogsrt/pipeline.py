"""파이프라인 조립 — M1 전처리 → M2 전사 → M3 매핑 → M4 정제 → M5 출력.

각 단계는 순수 함수 모듈에 있고, 여기서는 순서와 입출력만 다룬다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from pathlib import Path

from . import env, gapfill, log, loops, mapping, preprocess, refine, transcribe
from .config import Config
from .models import Chunk, Cue, Issue, Segment, Trace, Word
from .output import save_srt, to_review_cues
from .report import build_report
from .transcribe import LiveTranscriber, Transcriber, TranscriptionJob
from .validate import ERR, count_by_level, validate

STAGES = ("spine", "canonical", "map", "refine", "output")
Span = tuple[float, float]


@dataclass(frozen=True, slots=True)
class Prepared:
    stem: str
    duration: float
    silences: tuple[Span, ...]
    chunks: tuple[Chunk, ...]
    source: str = ""


@dataclass(frozen=True, slots=True)
class RunResult:
    trace: Trace
    cues: tuple[Cue, ...]
    issues: tuple[Issue, ...]
    outputs: dict[str, Path]


# ─────────────────────────────────────────────────────────────
#  M1 — 전처리
# ─────────────────────────────────────────────────────────────


def source_fingerprint(path: Path) -> str:
    """파일 전체를 읽지 않고 지문을 만든다. 장편 영상에서 해시에 몇 분을 쓸 이유가 없다."""
    stat = path.stat()
    digest = hashlib.sha256()
    digest.update(f"{path.name}:{stat.st_size}:{int(stat.st_mtime)}".encode())
    with path.open("rb") as handle:
        digest.update(handle.read(1_000_000))
    return digest.hexdigest()[:16]


def prepare(
    video: Path,
    cfg: Config,
    *,
    offline: bool,
    workdir: Path | None = None,
    reuse_cache: bool = True,
) -> Prepared:
    """오디오·무음·청크를 준비한다. 오프라인이고 메타 픽스처가 있으면 ffmpeg를 건너뛴다.

    `reuse_cache=False`면 지난 실행의 오디오를 절대 재사용하지 않고, 지문 폴더도
    만들지 않는다. 매번 새 영상만 보고 처리해야 하는 실행 경로가 쓴다.
    """
    stem = video.stem
    if offline:
        meta = transcribe.load_meta(env.fixture_dir(), stem)
        if meta.get("duration"):
            silences = tuple((float(a), float(b)) for a, b in meta.get("silences", []))
            log.info(f"메타 픽스처 사용: {stem} ({float(meta['duration']):.1f}초)")
            return Prepared(
                stem=stem,
                duration=float(meta["duration"]),
                silences=silences,
                chunks=(),
                source=str(video),
            )
        if not video.is_file():
            raise RuntimeError(
                f"영상도 메타 픽스처도 없습니다: {video}\n"
                f"사람이 `vlogsrt record {video}`를 실행해야 합니다."
            )

    if not video.is_file():
        raise RuntimeError(f"영상을 찾지 못했습니다: {video}")

    base = workdir or env.work_dir()
    cache = base / f"{stem}-{source_fingerprint(video)}" if reuse_cache else base
    cache.mkdir(parents=True, exist_ok=True)
    audio = cache / f"{stem}.mp3"
    silence_cache = cache / "silences.json"

    if reuse_cache and audio.is_file() and silence_cache.is_file():
        log.info(f"캐시 재사용: {cache}")
        duration = preprocess.probe_duration(audio)
        silences = tuple(
            (float(a), float(b)) for a, b in json.loads(silence_cache.read_text(encoding="utf-8"))
        )
    else:
        preprocess.extract_audio(video, audio, cfg)
        duration = preprocess.probe_duration(audio)
        silences = tuple(preprocess.detect_silence(audio, cfg, duration))
        if reuse_cache:
            silence_cache.write_text(
                json.dumps([list(span) for span in silences]), encoding="utf-8"
            )

    chunks = tuple(preprocess.make_chunks(audio, cfg, cache, list(silences)))
    return Prepared(
        stem=stem, duration=duration, silences=silences, chunks=chunks, source=str(video)
    )


# ─────────────────────────────────────────────────────────────
#  M2 — 전사
# ─────────────────────────────────────────────────────────────


def build_job(prepared: Prepared) -> TranscriptionJob:
    return TranscriptionJob(stem=prepared.stem, chunks=prepared.chunks, source=prepared.source)


def fetch_spine(
    transcriber: Transcriber, job: TranscriptionJob, cfg: Config, silences: tuple[Span, ...]
) -> tuple[list[Segment], list[Word]]:
    responses = transcriber.whisper(job, cfg)
    spine, words = transcribe.build_spine(responses)
    if not spine:
        raise RuntimeError("whisper 응답에서 세그먼트를 얻지 못했습니다.")
    return transcribe.apply_verdicts(spine, silences, cfg), words


# ─────────────────────────────────────────────────────────────
#  전체 실행
# ─────────────────────────────────────────────────────────────


def run(
    video: Path,
    cfg: Config,
    outdir: Path,
    *,
    online: bool = False,
    force_offline: bool = False,
    stop_after: str = "output",
    fast: bool = False,
    workdir: Path | None = None,
    artifacts: bool = True,
) -> RunResult:
    """`artifacts=False`면 SRT 하나만 남기고 trace·리포트·검토본은 쓰지 않는다.

    `workdir`를 임시 폴더로 주면 오디오도 그 안에서 만들고 끝나면 함께 사라진다.
    실행 기록을 남기지 않는 경로가 이 둘을 같이 쓴다.
    """
    if stop_after not in STAGES:
        raise RuntimeError(
            f"--stop-after 값이 올바르지 않습니다: {stop_after} (가능: {', '.join(STAGES)})"
        )
    effective = replace(cfg, word_timestamps=not fast)
    online = online and not force_offline
    offline = force_offline or not online and env.is_offline()
    prepared = prepare(video, effective, offline=offline, workdir=workdir, reuse_cache=artifacts)
    transcriber = transcribe.get_transcriber(effective, online=online, force_offline=offline)
    job = build_job(prepared)

    spine, words = fetch_spine(transcriber, job, effective, prepared.silences)
    if fast:
        words = []
    log.info(f"골격 {len(spine)}개 세그먼트, 단어 {len(words)}개")
    warn_about_loops(spine, effective, prepared.duration)

    trace = Trace(
        source=str(video),
        duration=prepared.duration,
        spine=tuple(spine),
        words=tuple(words),
        silences=prepared.silences,
        config_snapshot=effective.to_dict(),
    )
    if stop_after == "spine":
        return _finish(
            trace, [], [], outdir, prepared.stem, effective, write_srt=False, artifacts=artifacts
        )

    canonical = transcribe.canonical_text(transcriber.gpt(job, effective))
    if not canonical.strip():
        raise RuntimeError("gpt-transcribe 응답이 비었습니다. 픽스처를 확인하세요.")
    trace = replace(trace, canonical=canonical)
    if stop_after == "canonical":
        return _finish(
            trace, [], [], outdir, prepared.stem, effective, write_srt=False, artifacts=artifacts
        )

    draft = _map_stage(spine, canonical, words, prepared.silences, effective, trace)
    trace = replace(trace, cues=tuple(draft.cues), warnings=tuple(draft.warnings))
    if stop_after == "map":
        return _finish(trace, draft.cues, [], outdir, prepared.stem, effective, artifacts=artifacts)

    cues = refine.refine(draft.cues, words, effective, prepared.duration)
    cues = mapping.score_cues(cues, spine, effective)
    issues = validate(cues, spine, effective)
    _report_issues(issues)
    if stop_after == "refine":
        return _finish(trace, cues, issues, outdir, prepared.stem, effective, artifacts=artifacts)

    return _finish(
        trace,
        cues,
        issues,
        outdir,
        prepared.stem,
        effective,
        write_report=True,
        artifacts=artifacts,
    )


@dataclass(frozen=True, slots=True)
class _MapStage:
    cues: list[Cue]
    warnings: list[str]


def _map_stage(
    spine: list[Segment],
    canonical: str,
    words: list[Word],
    silences: tuple[Span, ...],
    cfg: Config,
    trace: Trace,
) -> _MapStage:
    """골격 큐에 정본 텍스트를 얹는다. 시각은 이 단계에서 절대 변하지 않는다."""
    del trace
    base = mapping.spine_to_cues(spine)
    if not base:
        raise RuntimeError("판정을 통과한 세그먼트가 없습니다. 임계값이나 오디오를 확인하세요.")
    mapped, cuts_a, global_sim = mapping.apply_canonical(base, spine, canonical, words, cfg)
    warnings: list[str] = []
    if global_sim < cfg.global_sim_floor:
        # F13 폴백. 두 전사가 서로 다른 말을 하고 있다. gpt 텍스트를 믿을 수 없다.
        message = (
            f"전역 유사도 {global_sim:.2f} < {cfg.global_sim_floor}. "
            "정본 매핑을 포기하고 whisper 골격 텍스트로 출력한다."
        )
        log.warn(message)
        warnings.append(message)
        return _MapStage(cues=base, warnings=warnings)
    del cuts_a  # 공백 채우기는 절단점이 아니라 시간축을 본다. 그쪽이 환청에 안 흔들린다.
    spread, filled = gapfill.fill_gaps(mapped, spine, cfg, silences)
    warnings.extend(gapfill.report(filled))
    return _MapStage(cues=spread, warnings=warnings)


def warn_about_loops(spine: list[Segment], cfg: Config, duration: float) -> list[str]:
    """whisper가 같은 문장을 되풀이했는지 알린다.

    골격이 곧 타이밍이라, 여기가 무너지면 어떤 후처리도 자막을 살리지 못한다.
    조용히 진행하면 사람은 망가진 SRT를 받고 나서야 안다(D13). 판정은 바꾸지
    않고 보고만 한다 — 되풀이가 잦다면 `prompt`부터 의심할 것.
    """
    found = loops.find_loops(spine, cfg)
    if not found:
        return []
    seconds = loops.looped_seconds(found)
    share = seconds / duration * 100 if duration > 0 else 0.0
    notices = [
        f"whisper가 같은 문장을 되풀이한 구간 {len(found)}개 · "
        f"{seconds:.0f}초 (영상의 {share:.1f}%). 그 구간 자막은 믿을 수 없다."
    ]
    for loop in sorted(found, key=lambda item: -item.duration)[:3]:
        notices.append(f"  {loop.start:.0f}~{loop.end:.0f}초 · {loop.count}회 · {loop.text[:40]!r}")
    if any(loop.start < duration * 0.05 for loop in found):
        notices.append("  영상 시작부터 반복한다면 whisper 힌트(prompt)를 의심하라.")
    for notice in notices:
        log.warn(notice)
    return notices


def _report_issues(issues: list[Issue]) -> None:
    counts = count_by_level(issues)
    for issue in issues:
        if issue.level == ERR:
            log.error(f"[{issue.code}] 큐 {issue.cue_id}: {issue.message}")
    # 수준 이름을 그대로 찍지 않는다. DoD가 `grep -c ERR`로 오류 유무를 세기 때문이다.
    log.info(f"검사 결과 오류 {counts['ERR']} / 경고 {counts['WARN']} / 정보 {counts['INFO']}")


def _finish(
    trace: Trace,
    cues: list[Cue],
    issues: list[Issue],
    outdir: Path,
    stem: str,
    cfg: Config,
    *,
    write_srt: bool = True,
    write_report: bool = False,
    artifacts: bool = True,
) -> RunResult:
    """`artifacts=False`면 SRT만 쓴다. trace·검토본·리포트는 만들지 않는다."""
    outdir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    if artifacts:
        outputs["trace"] = trace.save(outdir / "trace.json")
    if write_srt and cues:
        outputs["srt"] = save_srt(cues, outdir / f"{stem}.srt")
        if artifacts:
            outputs["srt_alias"] = save_srt(cues, outdir / "output.srt")
    if write_report and cues and artifacts:
        review = to_review_cues(cues)
        outputs["review"] = save_srt(review, outdir / f"{stem}.review.srt")
        outputs["review_alias"] = save_srt(review, outdir / "output.review.srt")
        body = build_report(trace.source, cues, list(trace.spine), issues, trace.duration, cfg)
        report_path = outdir / f"{stem}.report.md"
        report_path.write_text(body, encoding="utf-8")
        (outdir / "output.report.md").write_text(body, encoding="utf-8")
        outputs["report"] = report_path
    return RunResult(trace=trace, cues=tuple(cues), issues=tuple(issues), outputs=outputs)


# ─────────────────────────────────────────────────────────────
#  B0 베이스라인 / redo
# ─────────────────────────────────────────────────────────────


def baseline(
    video: Path, cfg: Config, outdir: Path, *, online: bool = False, force_offline: bool = False
) -> RunResult:
    """whisper 세그먼트를 그대로 SRT로 뽑는다. 대시보드 첫 행(B0)이다."""
    online = online and not force_offline
    offline = force_offline or not online and env.is_offline()
    prepared = prepare(video, cfg, offline=offline)
    transcriber = transcribe.get_transcriber(cfg, online=online, force_offline=offline)
    job = build_job(prepared)
    spine, words = fetch_spine(transcriber, job, cfg, prepared.silences)
    cues = refine.renumber(mapping.spine_to_cues(spine))
    trace = Trace(
        source=str(video),
        duration=prepared.duration,
        spine=tuple(spine),
        cues=tuple(cues),
        words=tuple(words),
        silences=prepared.silences,
        config_snapshot=cfg.to_dict(),
    )
    return _finish(trace, cues, [], outdir, prepared.stem, cfg)


def record_fixtures(video: Path, cfg: Config, *, reuse_existing: bool = True) -> dict[str, Path]:
    """실제 API를 호출해 픽스처를 녹화한다. **유료다.**

    호출자가 먼저 사람의 동의를 받아야 한다. 이 함수는 확인을 묻지 않는다.

    이미 받아둔 응답은 다시 부르지 않는다. 한쪽 모델만 실패했을 때 재시도하면서
    성공한 쪽까지 다시 결제하는 일을 막는다 — 77분 영상이면 한 모델에 약 $0.46이다.
    """
    prepared = prepare(video, cfg, offline=False)
    fixture_dir = env.fixture_dir()
    existing = {kind: fixture_dir / f"{prepared.stem}.{kind}.json" for kind in ("whisper", "gpt")}
    pending = [kind for kind, path in existing.items() if not (reuse_existing and path.is_file())]
    for kind, path in existing.items():
        if kind not in pending:
            log.info(f"{kind} 응답이 이미 있어 다시 부르지 않는다: {path}")

    outputs: dict[str, Path] = {
        kind: path for kind, path in existing.items() if kind not in pending
    }
    if pending:
        transcriber = LiveTranscriber(
            api_key=env.require_api_key(force=True), base_url=env.base_url()
        )
        job = build_job(prepared)
        calls = {"whisper": transcriber.whisper, "gpt": transcriber.gpt}
        for kind in pending:
            log.info(f"{kind} 호출 중…")
            outputs[kind] = transcribe.save_fixture(
                fixture_dir, prepared.stem, kind, calls[kind](job, cfg)
            )

    outputs["meta"] = transcribe.save_meta(
        fixture_dir,
        prepared.stem,
        cfg,
        prepared.duration,
        prepared.silences,
        len(prepared.chunks),
    )
    return outputs


def redo(trace_path: Path, cfg: Config, outdir: Path) -> RunResult:
    """API 없이 M4~M5만 다시 돈다. 임계값을 바꿔가며 재측정하는 용도다."""
    trace = Trace.load(trace_path)
    if not trace.cues:
        raise RuntimeError(f"{trace_path}에 큐 초안이 없습니다. `vlogsrt run`을 먼저 실행하세요.")
    stem = Path(trace.source).stem
    cues = refine.refine(list(trace.cues), list(trace.words), cfg, trace.duration)
    cues = mapping.score_cues(cues, list(trace.spine), cfg)
    issues = validate(cues, list(trace.spine), cfg)
    _report_issues(issues)
    updated = replace(trace, config_snapshot=cfg.to_dict())
    return _finish(updated, cues, issues, outdir, stem, cfg, write_report=True)
