"""오디오 추출과 청크 분할.

ffmpeg는 `subprocess`로 직접 부른다(불변 규칙 7). 순수 계산과 외부 호출을 갈라놓아,
ffmpeg가 없는 환경에서도 분할 로직은 그대로 테스트된다.
"""

from __future__ import annotations

import math
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import log
from .config import Config
from .models import Chunk


@dataclass(frozen=True, slots=True)
class ChunkPlan:
    """분할 계획. 순수 계산이라 ffmpeg 없이 테스트된다."""

    start: float
    end: float
    index: int

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


def chunk_count(duration: float, size_bytes: int, cfg: Config) -> int:
    """몇 조각으로 나눌지. 업로드 크기 한도와 길이 한도 **둘 다** 지켜야 한다."""
    by_size = math.ceil(size_bytes / cfg.chunk_max_bytes)
    by_time = math.ceil(duration / cfg.chunk_max_seconds)
    return max(1, by_size, by_time)


def plan_chunks(duration: float, size_bytes: int, cfg: Config) -> list[ChunkPlan]:
    """API 한도를 넘으면 균등하게 자른다.

    무음을 찾아 그 한가운데를 고르던 시절이 있었다(D21에서 제거). 실측에서는
    경계 대부분이 결국 강제 분할이었고, 그때마다 경고가 로그를 덮었다. 이제는
    조각 수를 줄여(한도 1500초) 경계 자체를 적게 만든다.
    """
    if duration <= 0:
        raise RuntimeError(f"오디오 길이가 0 이하입니다: {duration}")

    count = chunk_count(duration, size_bytes, cfg)
    step = duration / count
    return [
        ChunkPlan(start=step * index, end=min(step * (index + 1), duration), index=index)
        for index in range(count)
    ]


def make_chunks(audio: Path, cfg: Config, workdir: Path) -> list[Chunk]:
    duration = probe_duration(audio)
    plans = plan_chunks(duration, audio.stat().st_size, cfg)
    if len(plans) == 1:
        return [Chunk(path=audio, offset=0.0, duration=duration, index=0)]

    ffmpeg = require_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    chunks: list[Chunk] = []
    for plan in plans:
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
    verify_coverage(chunks, plans, cfg)
    return chunks


def verify_coverage(chunks: list[Chunk], plans: list[ChunkPlan], cfg: Config) -> None:
    """만들어진 조각이 계획한 구간을 실제로 덮는지 확인한다.

    조각이 계획보다 짧으면 그 길이만큼의 소리가 전사에 아예 오르지 못한다. 자막에는
    구멍으로 남고, 프로그램은 아무 말도 하지 않는다 — 그러지 않으려고 여기서 멈춘다.
    자막 품질을 판정하는 게 아니라 **입력이 다 들어갔는지**만 센다(D23, 규칙 4 무관).
    """
    for chunk, plan in zip(chunks, plans, strict=True):
        actual = probe_duration(chunk.path)
        if actual < plan.duration - cfg.chunk_duration_tolerance:
            raise RuntimeError(
                f"청크 {plan.index}가 계획보다 짧습니다: {actual:.2f}초 < {plan.duration:.2f}초\n"
                f"  {plan.start:.1f}~{plan.end:.1f}초 구간의 소리가 자막에서 빠집니다.\n"
                f"  `make clean` 후 다시 실행하세요. 계속 나면 원본 오디오를 확인하세요."
            )
