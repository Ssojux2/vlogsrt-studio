"""오디오 추출·무음 검출·묶음 조각 만들기 — **ffmpeg 경계**(불변 규칙 5).

ffmpeg는 `subprocess`로 직접 부른다(규칙 7). 계산은 전부 `spans.py`에 있고
여기서는 외부 프로세스만 다룬다 — ffmpeg가 없는 환경에서도 로직은 그대로 테스트된다.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import env, log
from .config import Config
from .models import Chunk, Span
from .spans import Batch, parse_silence_output


def require_ffmpeg(tool: str = "ffmpeg") -> str:
    path = shutil.which(tool)
    if not path:
        raise RuntimeError(f"{tool} 를 찾지 못했습니다. `brew install ffmpeg` 후 다시 실행하세요.")
    return path


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = subprocess.run(command, capture_output=True, text=True, check=False)
    except OSError as exc:
        raise RuntimeError(f"명령 실행 실패: {command[0]} ({exc})") from exc
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"{command[0]} 실패 (코드 {completed.returncode})\n{tail}")
    return completed


def probe_duration(path: Path) -> float:
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ]
    )  # fmt: skip
    raw = completed.stdout.strip()
    try:
        return float(raw)
    except ValueError as exc:
        raise RuntimeError(f"길이를 읽지 못했습니다: {path} (ffprobe 출력 {raw!r})") from exc


def extract_audio(src: Path, dst: Path, cfg: Config) -> Path:
    """오디오를 손대지 않고 뽑는다 — 필터 없음(D20).

    음질 필터(highpass/eq/loudnorm)는 제거했다. 타임라인 오차는 0.000초로
    결백했지만(D13), 전처리 최소화가 우선이다.
    """
    ffmpeg = require_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", str(src),
            "-vn",
            "-ac", str(cfg.channels),
            "-ar", str(cfg.sample_rate),
            "-c:a", "libmp3lame",
            "-b:a", cfg.audio_bitrate,
            str(dst),
        ]
    )  # fmt: skip
    log.debug(f"오디오 추출 완료: {dst} ({dst.stat().st_size / 1_000_000:.1f}MB)")
    return dst


def detect_silence(path: Path, cfg: Config, duration: float | None = None) -> list[Span]:
    """무음을 찾는다. 여기서 나온 값이 발화 구간이 되고, 발화 구간이 자막 시각이 된다.

    실패해도 예외를 삼키지 않는다 — 무음을 못 찾으면 타이밍의 출처가 없다.
    """
    ffmpeg = require_ffmpeg()
    total = probe_duration(path) if duration is None else duration
    threshold = f"noise={cfg.silence_threshold_db:g}dB:d={cfg.silence_min_duration:g}"
    command = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-i", str(path),
        "-af", f"silencedetect={threshold}",
        "-f", "null", "-",
    ]  # fmt: skip
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"silencedetect 실패 (코드 {completed.returncode})\n{tail}")
    return parse_silence_output(completed.stderr, total)


def cut_batches(audio: Path, batches: Sequence[Batch], workdir: Path, cfg: Config) -> list[Chunk]:
    """묶음마다 오디오 조각을 만든다. 자르는 자리는 이미 무음 안이다(`spans.group_spans`)."""
    if not batches:
        return []
    ffmpeg = require_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)

    def cut(batch: Batch) -> Chunk:
        destination = workdir / f"{audio.stem}.batch{batch.index:04d}.mp3"
        _run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-i", str(audio),
                # -ss를 -i 뒤에 두는 이유: 정확한 탐색이 필요하다. offset이 틀리면
                # 자막 전체가 밀린다(D13).
                "-ss", f"{batch.start:.3f}",
                "-to", f"{batch.end:.3f}",
                "-ac", str(cfg.channels),
                "-ar", str(cfg.sample_rate),
                "-c:a", "libmp3lame",
                "-b:a", cfg.audio_bitrate,
                str(destination),
            ]
        )  # fmt: skip
        return Chunk(
            path=destination,
            offset=batch.start,
            duration=batch.duration,
            index=batch.index,
            spans=batch.spans,
        )

    with ThreadPoolExecutor(max_workers=env.max_workers()) as pool:
        chunks = list(pool.map(cut, batches))
    warn_about_short_cuts(chunks, batches, cfg)
    return chunks


def warn_about_short_cuts(
    chunks: Sequence[Chunk], batches: Sequence[Batch], cfg: Config
) -> list[str]:
    """조각이 계획보다 짧으면 그만큼의 소리가 전사에 오르지 못한다.

    멈추지는 않는다(규칙 9) — 자막에 구멍이 나도 SRT는 나오되, 어디에 났는지는
    로그가 말한다. 자막 품질을 판정하는 게 아니라 **입력이 다 들어갔는지**만 센다.
    """
    complaints: list[str] = []
    for chunk, batch in zip(chunks, batches, strict=True):
        actual = probe_duration(chunk.path)
        if actual < batch.duration - cfg.chunk_duration_tolerance:
            complaints.append(
                f"조각 {batch.index}이 계획보다 짧다: {actual:.2f}초 < {batch.duration:.2f}초 "
                f"({batch.start:.1f}~{batch.end:.1f}초 구간의 소리가 자막에서 빠진다)"
            )
    for complaint in complaints:
        log.warn(complaint)
    return complaints
