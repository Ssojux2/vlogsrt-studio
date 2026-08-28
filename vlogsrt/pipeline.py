"""파이프라인 조립 — 오디오 추출 → 조각 나누기 → 전사 → 정제 → SRT.

각 단계는 순수 함수 모듈에 있고, 여기서는 순서와 입출력만 다룬다.
시각은 전부 whisper 세그먼트에서 오고, 조각 오프셋을 더한 것이 전부다(D25).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from . import env, log, preprocess, refine, repair, transcribe, verify
from .config import Config
from .models import Chunk, ChunkMark, Cue, Repair, Segment, Trace
from .output import save_srt
from .transcribe import VERIFY_KIND, LiveTranscriber, Transcriber, TranscriptionJob
from .verify import Verification


@dataclass(frozen=True, slots=True)
class Prepared:
    stem: str
    duration: float
    chunks: tuple[Chunk, ...]
    source: str = ""
    audio: Path | None = None  # 되풀이 구간을 다시 자르려면 원본 오디오가 필요하다(D27)
    workdir: Path | None = None


@dataclass(frozen=True, slots=True)
class RunResult:
    trace: Trace
    cues: tuple[Cue, ...]
    outputs: dict[str, Path]
    verification: Verification | None = None
    repairs: tuple[Repair, ...] = ()


# ─────────────────────────────────────────────────────────────
#  전처리
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
    """오디오와 청크를 준비한다. 오프라인이고 메타 픽스처가 있으면 ffmpeg를 건너뛴다.

    `reuse_cache=False`면 지난 실행의 오디오를 절대 재사용하지 않고, 지문 폴더도
    만들지 않는다. 매번 새 영상만 보고 처리해야 하는 실행 경로가 쓴다.
    """
    stem = video.stem
    if offline:
        from_fixture = _prepare_from_fixture(video, stem)
        if from_fixture:
            return from_fixture

    if not video.is_file():
        raise RuntimeError(f"영상을 찾지 못했습니다: {video}")

    base = workdir or env.work_dir()
    cache = base / f"{stem}-{source_fingerprint(video)}" if reuse_cache else base
    cache.mkdir(parents=True, exist_ok=True)
    audio = cache / f"{stem}.mp3"

    if reuse_cache and audio.is_file():
        log.info(f"캐시 재사용: {cache}")
    else:
        preprocess.extract_audio(video, audio, cfg)
    duration = preprocess.probe_duration(audio)

    chunks = tuple(preprocess.make_chunks(audio, duration, cfg, cache))
    _announce(chunks, duration, cfg)
    return Prepared(
        stem=stem,
        duration=duration,
        chunks=chunks,
        source=str(video),
        audio=audio,
        workdir=cache,
    )


def _prepare_from_fixture(video: Path, stem: str) -> Prepared | None:
    """메타 픽스처만으로 준비한다. 오디오가 없으므로 되풀이 구간을 다시 자를 수 없다 —
    그때 재전사는 `<stem>.rescue.json`을 읽는다."""
    meta = transcribe.load_meta(env.fixture_dir(), stem)
    if meta.get("duration"):
        log.info(f"메타 픽스처 사용: {stem} ({float(meta['duration']):.1f}초)")
        return Prepared(stem=stem, duration=float(meta["duration"]), chunks=(), source=str(video))
    if not video.is_file():
        raise RuntimeError(
            f"영상도 메타 픽스처도 없습니다: {video}\n"
            f"사람이 `vlogsrt record {video}`를 실행해야 합니다."
        )
    return None


def _announce(chunks: tuple[Chunk, ...], duration: float, cfg: Config) -> None:
    """조각 길이를 반드시 찍는다. **설정이 안 먹은 것을 로그에서 바로 봐야 한다.**

    D26이 조각을 10분으로 낮췄는데도 38분 조각으로 돌아 되풀이가 재발한 적이 있다.
    설정 파일을 못 찾은 실행이었고, 로그만 봐서는 알 수 없었다(D27).
    """
    longest = max((chunk.duration for chunk in chunks), default=duration)
    log.info(
        f"조각 {len(chunks)}개 · 가장 긴 조각 {longest / 60:.1f}분 "
        f"(한도 {cfg.chunk_max_bytes / 1e6:.1f}MB / {cfg.chunk_max_seconds / 60:.0f}분)"
    )


# ─────────────────────────────────────────────────────────────
#  전사 → 큐
# ─────────────────────────────────────────────────────────────


def build_job(prepared: Prepared, kind: str = transcribe.FIXTURE_KIND) -> TranscriptionJob:
    return TranscriptionJob(
        stem=prepared.stem, chunks=prepared.chunks, source=prepared.source, kind=kind
    )


def fetch_draft(transcriber: Transcriber, job: TranscriptionJob, cfg: Config) -> list[Segment]:
    """1차 전사. **아직 골격이 아니다** — 되풀이 재전사가 끝나야 골격이 확정된다(D27)."""
    draft = transcribe.build_spine(transcriber.transcribe(job, cfg))
    if not draft:
        raise RuntimeError("전사 응답에서 세그먼트를 얻지 못했습니다.")
    return draft


def settle_spine(
    draft: list[Segment], prepared: Prepared, cfg: Config, transcriber: Transcriber
) -> repair.Settled:
    """되풀이 구간을 다시 물어 골격을 확정한다. **이 뒤로 골격은 불변이다**(규칙 2)."""
    source = repair.Source(
        stem=prepared.stem,
        duration=prepared.duration,
        audio=prepared.audio,
        workdir=prepared.workdir,
    )
    return repair.settle(draft, source, cfg, transcriber)


def spine_to_cues(spine: list[Segment]) -> list[Cue]:
    """골격 하나에 큐 하나. 시각도 텍스트도 세그먼트 그대로다(규칙 1)."""
    return [
        Cue(
            start=segment.start,
            end=segment.end,
            lines=(segment.text.strip(),),
            seg_ids=(segment.id,),
            index=position + 1,
        )
        for position, segment in enumerate(spine)
    ]


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
    workdir: Path | None = None,
    artifacts: bool = True,
    verify_with_second_model: bool = True,
    repair_repeats: bool = True,
) -> RunResult:
    """`artifacts=False`면 SRT 하나만 남기고 trace는 쓰지 않는다.

    `workdir`를 임시 폴더로 주면 오디오도 그 안에서 만들고 끝나면 함께 사라진다.
    실행 기록을 남기지 않는 경로가 이 둘을 같이 쓴다.
    """
    online = online and not force_offline
    offline = force_offline or not online and env.is_offline()
    prepared = prepare(video, cfg, offline=offline, workdir=workdir, reuse_cache=artifacts)
    transcriber = transcribe.get_transcriber(cfg, online=online, force_offline=offline)
    if not repair_repeats:
        cfg = replace(cfg, repair_enabled=False)

    settled = _make_spine(prepared, cfg, transcriber)
    cues = _make_cues(settled.spine, cfg, prepared.duration)
    trace = Trace(
        source=str(video),
        duration=prepared.duration,
        spine=settled.spine,
        cues=tuple(cues),
        config_snapshot=cfg.to_dict(),
        repairs=settled.repairs,
        chunks=tuple(ChunkMark.of(chunk) for chunk in prepared.chunks),
    )
    result = replace(
        _finish(trace, cues, outdir, prepared.stem, artifacts=artifacts),
        repairs=settled.repairs,
    )
    if not verify_with_second_model:
        return result
    # SRT는 이미 디스크에 있다. 검증은 여기서부터 무슨 일이 나도 그 파일을 못 건드린다(규칙 5).
    return replace(result, verification=report_verification(result, prepared, cfg, transcriber))


def _make_spine(prepared: Prepared, cfg: Config, transcriber: Transcriber) -> repair.Settled:
    """1차 전사 → 되풀이 재전사 → 골격 확정. 이 함수가 끝나면 골격은 불변이다(규칙 2)."""
    draft = fetch_draft(transcriber, build_job(prepared), cfg)
    log.info(f"1차 전사 {len(draft)}개 세그먼트")
    settled = settle_spine(draft, prepared, cfg, transcriber)
    log.info(f"골격 {len(settled.spine)}개 세그먼트")
    return settled


def _make_cues(spine: tuple[Segment, ...], cfg: Config, duration: float) -> list[Cue]:
    cues = refine.refine(spine_to_cues(list(spine)), cfg, duration)
    if not cues:
        raise RuntimeError("자막 큐가 하나도 남지 않았습니다. 오디오를 확인하세요.")
    log.info(f"큐 {len(cues)}개")
    return cues


def _finish(
    trace: Trace, cues: list[Cue], outdir: Path, stem: str, *, artifacts: bool = True
) -> RunResult:
    """`artifacts=False`면 SRT만 쓴다. trace는 만들지 않는다."""
    outdir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}
    if artifacts:
        outputs["trace"] = trace.save(outdir / "trace.json")
    outputs["srt"] = save_srt(cues, outdir / f"{stem}.srt")
    if artifacts:
        outputs["srt_alias"] = save_srt(cues, outdir / "output.srt")
    return RunResult(trace=trace, cues=tuple(cues), outputs=outputs)


def report_verification(
    result: RunResult, prepared: Prepared, cfg: Config, transcriber: Transcriber
) -> Verification | None:
    """두 번째 모델로 같은 오디오를 받아 대조하고 **표시만 한다**(규칙 5).

    실패해도 SRT를 막지 않는다 — 픽스처가 없거나 계정에 모델이 없거나 API가 죽어도
    자막은 이미 나와 있다. 그래서 여기서만은 예외를 삼키되, 조용히는 아니다.
    """
    try:
        responses = transcriber.transcribe(build_job(prepared, VERIFY_KIND), cfg)
        report = verify.verify(result.cues, result.trace.spine, responses, prepared.duration, cfg)
    except Exception as exc:  # noqa: BLE001 - 검증 실패가 자막을 막으면 안 된다
        log.warn(f"검증을 건너뛴다: {exc}")
        return None
    log.info(verify.summarize(report))
    for line in verify.details(report):
        log.warn(line)
    return report


# ─────────────────────────────────────────────────────────────
#  픽스처 녹화 / redo
# ─────────────────────────────────────────────────────────────


def record_fixtures(
    video: Path, cfg: Config, *, reuse_existing: bool = True, verify: bool = True
) -> dict[str, Path]:
    """실제 API를 호출해 픽스처를 녹화한다. **유료다.**

    호출자가 먼저 사람의 동의를 받아야 한다. 이 함수는 확인을 묻지 않는다.
    """
    prepared = prepare(video, cfg, offline=False)
    outputs: dict[str, Path] = {
        transcribe.FIXTURE_KIND: _record_one(
            prepared, prepared.chunks, cfg, transcribe.FIXTURE_KIND, reuse_existing
        )
    }
    if verify:
        outputs[VERIFY_KIND] = _record_one(
            prepared, prepared.chunks, cfg, VERIFY_KIND, reuse_existing
        )
    outputs.update(_record_repair(prepared, cfg, reuse_existing))

    outputs["meta"] = transcribe.save_meta(
        env.fixture_dir(), prepared.stem, cfg, prepared.duration, len(prepared.chunks)
    )
    return outputs


def _record_repair(prepared: Prepared, cfg: Config, reuse_existing: bool) -> dict[str, Path]:
    """되풀이가 있으면 재전사·심판 응답도 녹화한다. 없으면 한 푼도 안 쓴다.

    녹화된 1차 응답에서 되풀이를 찾아 **실행 때와 같은 자리**를 자른다. 그래야
    오프라인 재생이 같은 구간을 같은 오프셋으로 되짚는다.
    """
    if not cfg.repair_enabled:
        return {}
    draft = transcribe.build_spine(
        transcribe.FixtureTranscriber(fixture_dir=env.fixture_dir()).transcribe(
            build_job(prepared), cfg
        )
    )
    source = repair.Source(
        stem=prepared.stem,
        duration=prepared.duration,
        audio=prepared.audio,
        workdir=prepared.workdir,
    )
    spans = repair.plan_spans(draft, source, cfg)
    if not spans or not prepared.audio or not prepared.workdir:
        log.info("되풀이가 없어 재전사 픽스처는 녹화하지 않는다.")
        return {}
    chunks = tuple(preprocess.cut_spans(prepared.audio, spans, prepared.duration, prepared.workdir))
    hotter = replace(cfg, transcribe_temperature=cfg.repair_temperature)
    return {
        transcribe.RESCUE_KIND: _record_one(
            prepared, chunks, hotter, transcribe.RESCUE_KIND, reuse_existing
        ),
        transcribe.ARBITER_KIND: _record_one(
            prepared, chunks, cfg, transcribe.ARBITER_KIND, reuse_existing
        ),
    }


def _record_one(
    prepared: Prepared, chunks: tuple[Chunk, ...], cfg: Config, kind: str, reuse_existing: bool
) -> Path:
    """이미 받아둔 응답이 있으면 다시 결제하지 않는다."""
    fixture_dir = env.fixture_dir()
    path = fixture_dir / f"{prepared.stem}.{kind}.json"
    if reuse_existing and path.is_file():
        log.info(f"{kind} 응답이 이미 있어 다시 부르지 않는다: {path}")
        return path
    transcriber = LiveTranscriber(api_key=env.require_api_key(force=True), base_url=env.base_url())
    log.info(f"{kind} 호출 중…")
    job = TranscriptionJob(stem=prepared.stem, chunks=chunks, source=prepared.source, kind=kind)
    return transcribe.save_fixture(
        fixture_dir, prepared.stem, kind, transcriber.transcribe(job, cfg)
    )


def redo(trace_path: Path, cfg: Config, outdir: Path) -> RunResult:
    """API 없이 골격에서 다시 조판한다. 임계값을 바꿔가며 재측정하는 용도다."""
    trace = Trace.load(trace_path)
    if not trace.spine:
        raise RuntimeError(f"{trace_path}에 골격이 없습니다. `vlogsrt run`을 먼저 실행하세요.")
    stem = Path(trace.source).stem
    cues = refine.refine(spine_to_cues(list(trace.spine)), cfg, trace.duration)
    updated = replace(trace, cues=tuple(cues), config_snapshot=cfg.to_dict())
    # 재전사는 API가 필요하다. 골격에 이미 반영돼 있으니 기록만 그대로 들고 간다.
    return replace(_finish(updated, cues, outdir, stem), repairs=trace.repairs)
