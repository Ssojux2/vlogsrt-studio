"""파이프라인 조립 — 오디오 추출 → 분할 → 전사 → 정제 → SRT.

각 단계는 순수 함수 모듈에 있고, 여기서는 순서와 입출력만 다룬다.
자막을 다시 재는 단계는 없다(D21).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, replace
from pathlib import Path

from . import env, log, preprocess, refine, transcribe
from .config import Config
from .models import Chunk, Cue, Segment, Trace
from .output import save_srt
from .transcribe import LiveTranscriber, Transcriber, TranscriptionJob


@dataclass(frozen=True, slots=True)
class Prepared:
    stem: str
    duration: float
    chunks: tuple[Chunk, ...]
    source: str = ""


@dataclass(frozen=True, slots=True)
class RunResult:
    trace: Trace
    cues: tuple[Cue, ...]
    outputs: dict[str, Path]


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
        meta = transcribe.load_meta(env.fixture_dir(), stem)
        if meta.get("duration"):
            log.info(f"메타 픽스처 사용: {stem} ({float(meta['duration']):.1f}초)")
            return Prepared(
                stem=stem, duration=float(meta["duration"]), chunks=(), source=str(video)
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

    if reuse_cache and audio.is_file():
        log.info(f"캐시 재사용: {cache}")
    else:
        preprocess.extract_audio(video, audio, cfg)
    duration = preprocess.probe_duration(audio)

    chunks = tuple(preprocess.make_chunks(audio, cfg, cache))
    return Prepared(stem=stem, duration=duration, chunks=chunks, source=str(video))


# ─────────────────────────────────────────────────────────────
#  전사 → 큐
# ─────────────────────────────────────────────────────────────


def build_job(prepared: Prepared) -> TranscriptionJob:
    return TranscriptionJob(stem=prepared.stem, chunks=prepared.chunks, source=prepared.source)


def fetch_spine(transcriber: Transcriber, job: TranscriptionJob, cfg: Config) -> list[Segment]:
    """골격 생성의 끝. 이 뒤로 골격은 불변이다(규칙 2)."""
    spine = transcribe.build_spine(transcriber.transcribe(job, cfg))
    if not spine:
        raise RuntimeError("전사 응답에서 세그먼트를 얻지 못했습니다.")
    return spine


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
) -> RunResult:
    """`artifacts=False`면 SRT 하나만 남기고 trace는 쓰지 않는다.

    `workdir`를 임시 폴더로 주면 오디오도 그 안에서 만들고 끝나면 함께 사라진다.
    실행 기록을 남기지 않는 경로가 이 둘을 같이 쓴다.
    """
    online = online and not force_offline
    offline = force_offline or not online and env.is_offline()
    prepared = prepare(video, cfg, offline=offline, workdir=workdir, reuse_cache=artifacts)
    transcriber = transcribe.get_transcriber(cfg, online=online, force_offline=offline)

    spine = fetch_spine(transcriber, build_job(prepared), cfg)
    log.info(f"골격 {len(spine)}개 세그먼트")

    cues = refine.refine(spine_to_cues(spine), cfg, prepared.duration)
    if not cues:
        raise RuntimeError("자막 큐가 하나도 남지 않았습니다. 오디오를 확인하세요.")
    log.info(f"큐 {len(cues)}개")

    trace = Trace(
        source=str(video),
        duration=prepared.duration,
        spine=tuple(spine),
        cues=tuple(cues),
        config_snapshot=cfg.to_dict(),
    )
    return _finish(trace, cues, outdir, prepared.stem, artifacts=artifacts)


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


# ─────────────────────────────────────────────────────────────
#  픽스처 녹화 / redo
# ─────────────────────────────────────────────────────────────


def record_fixtures(video: Path, cfg: Config, *, reuse_existing: bool = True) -> dict[str, Path]:
    """실제 API를 호출해 픽스처를 녹화한다. **유료다.**

    호출자가 먼저 사람의 동의를 받아야 한다. 이 함수는 확인을 묻지 않는다.
    """
    prepared = prepare(video, cfg, offline=False)
    fixture_dir = env.fixture_dir()
    response_path = fixture_dir / f"{prepared.stem}.{transcribe.FIXTURE_KIND}.json"

    outputs: dict[str, Path] = {}
    if reuse_existing and response_path.is_file():
        log.info(f"전사 응답이 이미 있어 다시 부르지 않는다: {response_path}")
        outputs[transcribe.FIXTURE_KIND] = response_path
    else:
        transcriber = LiveTranscriber(
            api_key=env.require_api_key(force=True), base_url=env.base_url()
        )
        log.info("전사 호출 중…")
        outputs[transcribe.FIXTURE_KIND] = transcribe.save_fixture(
            fixture_dir,
            prepared.stem,
            transcribe.FIXTURE_KIND,
            transcriber.transcribe(build_job(prepared), cfg),
        )

    outputs["meta"] = transcribe.save_meta(
        fixture_dir, prepared.stem, cfg, prepared.duration, len(prepared.chunks)
    )
    return outputs


def redo(trace_path: Path, cfg: Config, outdir: Path) -> RunResult:
    """API 없이 골격에서 다시 조판한다. 임계값을 바꿔가며 재측정하는 용도다."""
    trace = Trace.load(trace_path)
    if not trace.spine:
        raise RuntimeError(f"{trace_path}에 골격이 없습니다. `vlogsrt run`을 먼저 실행하세요.")
    stem = Path(trace.source).stem
    cues = refine.refine(spine_to_cues(list(trace.spine)), cfg, trace.duration)
    updated = replace(trace, cues=tuple(cues), config_snapshot=cfg.to_dict())
    return _finish(updated, cues, outdir, stem)
