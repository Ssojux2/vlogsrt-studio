"""오디오 추출·무음 검출·청크 분할.

ffmpeg는 `subprocess`로 직접 부른다(불변 규칙 7). 순수 계산과 외부 호출을 갈라놓아,
ffmpeg가 없는 환경에서도 분할 로직은 그대로 테스트된다.
"""

from __future__ import annotations

import math
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import log
from .config import Config
from .models import Chunk

_SILENCE_START_RE = re.compile(r"silence_start:\s*(-?[\d.]+)")
_SILENCE_END_RE = re.compile(r"silence_end:\s*(-?[\d.]+)")

Span = tuple[float, float]


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """분할 계획. `forced`는 적당한 무음을 못 찾아 목표 지점을 밀어붙였다는 뜻이다."""

    start: float
    end: float
    index: int
    forced: bool = False

    @property
    def duration(self) -> float:
        return self.end - self.start


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


def audio_filter(cfg: Config) -> str:
    """브이로그는 구간마다 볼륨 편차가 크다. loudnorm 없이는 무음 검출이 흔들린다."""
    return f"loudnorm=I={cfg.loudnorm_i:g}:TP={cfg.loudnorm_tp:g}:LRA={cfg.loudnorm_lra:g}"


def extract_audio(src: Path, dst: Path, cfg: Config) -> Path:
    ffmpeg = require_ffmpeg()
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", str(src),
            "-vn",
            "-af", audio_filter(cfg),
            "-ac", str(cfg.channels),
            "-ar", str(cfg.sample_rate),
            "-c:a", "libmp3lame",
            "-b:a", cfg.audio_bitrate,
            str(dst),
        ]
    )  # fmt: skip
    log.debug(f"오디오 추출 완료: {dst} ({dst.stat().st_size / 1_000_000:.1f}MB)")
    return dst


def parse_silence_output(stderr: str, duration: float) -> list[Span]:
    """`silencedetect` 결과는 stdout이 아니라 stderr에 나온다."""
    spans: list[Span] = []
    pending: float | None = None
    for line in stderr.splitlines():
        start_match = _SILENCE_START_RE.search(line)
        if start_match:
            pending = max(0.0, float(start_match.group(1)))
            continue
        end_match = _SILENCE_END_RE.search(line)
        if end_match and pending is not None:
            end = float(end_match.group(1))
            if end > pending:
                spans.append((pending, end))
            pending = None
    if pending is not None and duration > pending:
        spans.append((pending, duration))
    return spans


def detect_silence(path: Path, cfg: Config, duration: float | None = None) -> list[Span]:
    ffmpeg = require_ffmpeg()
    total = probe_duration(path) if duration is None else duration
    command = [
        ffmpeg, "-hide_banner", "-nostdin",
        "-i", str(path),
        "-af", f"silencedetect=noise={cfg.silence_threshold_db:g}dB:d={cfg.silence_min_duration:g}",
        "-f", "null", "-",
    ]  # fmt: skip
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"silencedetect 실패 (코드 {completed.returncode})\n{tail}")
    return parse_silence_output(completed.stderr, total)


def plan_chunks(
    duration: float, size_bytes: int, silences: list[Span], cfg: Config
) -> list[ChunkPlan]:
    """크기 제한을 넘으면 무음 한가운데를 골라 자른다. 없으면 목표 지점을 강제한다."""
    if duration <= 0:
        raise RuntimeError(f"오디오 길이가 0 이하입니다: {duration}")
    if size_bytes <= cfg.chunk_max_bytes:
        return [ChunkPlan(start=0.0, end=duration, index=0)]

    count = math.ceil(size_bytes / cfg.chunk_max_bytes)
    target_length = duration / count
    window = target_length * cfg.chunk_search_ratio
    centers = sorted((start + end) / 2 for start, end in silences)

    boundaries: list[float] = []
    forced_flags: list[bool] = []
    previous = 0.0
    for step in range(1, count):
        target = target_length * step
        floor = max(previous + cfg.chunk_min_duration, target - window)
        ceiling = min(duration - cfg.chunk_min_duration, target + window)
        picked = _nearest_center(centers, target, floor, ceiling)
        forced = picked is None
        cut = target if picked is None else picked
        cut = min(max(cut, previous + cfg.chunk_min_duration), duration)
        boundaries.append(cut)
        forced_flags.append(forced)
        previous = cut

    plans: list[ChunkPlan] = []
    start = 0.0
    for index, cut in enumerate(boundaries):
        plans.append(ChunkPlan(start=start, end=cut, index=index, forced=forced_flags[index]))
        start = cut
    plans.append(ChunkPlan(start=start, end=duration, index=len(boundaries)))
    return plans


def _nearest_center(
    centers: list[float], target: float, floor: float, ceiling: float
) -> float | None:
    if floor > ceiling:
        return None
    inside = [c for c in centers if floor <= c <= ceiling]
    if not inside:
        return None
    return min(inside, key=lambda c: abs(c - target))


def make_chunks(
    audio: Path, cfg: Config, workdir: Path, silences: list[Span] | None = None
) -> list[Chunk]:
    duration = probe_duration(audio)
    spans = silences if silences is not None else detect_silence(audio, cfg, duration)
    plans = plan_chunks(duration, audio.stat().st_size, spans, cfg)
    if len(plans) == 1:
        return [Chunk(path=audio, offset=0.0, duration=duration, index=0)]

    ffmpeg = require_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    chunks: list[Chunk] = []
    for plan in plans:
        if plan.forced:
            log.warn(f"청크 {plan.index}: {plan.start:.2f}초에 적당한 무음이 없어 강제로 잘랐다.")
        else:
            log.debug(f"청크 {plan.index} 경계: {plan.start:.2f}초")
        destination = workdir / f"{audio.stem}.chunk{plan.index:02d}.mp3"
        _run(
            [
                ffmpeg, "-hide_banner", "-nostdin", "-y",
                "-i", str(audio),
                # -ss를 -i 뒤에 두는 이유: 정확한 탐색이 필요하다. offset이 틀리면
                # 재기준화가 통째로 어긋난다.
                "-ss", f"{plan.start:.3f}",
                "-to", f"{plan.end:.3f}",
                "-ac", str(cfg.channels),
                "-ar", str(cfg.sample_rate),
                "-c:a", "libmp3lame",
                "-b:a", cfg.audio_bitrate,
                str(destination),
            ]
        )  # fmt: skip
        chunks.append(
            Chunk(
                path=destination,
                offset=plan.start,
                duration=plan.duration,
                index=plan.index,
            )
        )
    return chunks
