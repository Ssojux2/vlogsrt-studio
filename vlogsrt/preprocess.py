"""오디오 추출과 조각 나누기 — **유일한 ffmpeg 경계**(불변 규칙 7).

ffmpeg는 `subprocess`로 직접 부른다(규칙 10). 자를 자리를 고르는 계산은 전부
`cutpoints.py`에 있고 여기서는 외부 프로세스만 다룬다 — ffmpeg가 없는 환경에서도
분할 로직은 그대로 테스트된다.
"""

from __future__ import annotations

import array
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from . import cutpoints, log
from .config import Config
from .models import Chunk


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


# ─────────────────────────────────────────────────────────────
#  자를 자리 — 진폭을 재서 가장 조용한 곳을 고른다
# ─────────────────────────────────────────────────────────────


def amplitudes(path: Path, start: float, length: float, cfg: Config) -> list[float]:
    """구간별 최대 진폭. ffmpeg는 디코딩만 하고 계산은 여기서 한다.

    자를 자리를 고르는 데만 쓰므로 낮은 주파수로 충분하다. **경계 근처만 읽는다** —
    파일 전체를 디코딩할 이유가 없다.
    """
    ffmpeg = require_ffmpeg()
    command = [ffmpeg, "-v", "error", "-nostdin"]
    if start > 0:
        command += ["-ss", f"{start:.6f}"]
    command += [
        "-i", str(path),
        "-t", f"{max(0.0, length):.6f}",
        "-f", "s16le", "-ac", "1", "-ar", str(cfg.cut_probe_rate), "-",
    ]  # fmt: skip
    completed = subprocess.run(command, capture_output=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"진폭을 읽지 못했습니다: {path} (코드 {completed.returncode})")

    raw = completed.stdout
    samples = array.array("h")
    samples.frombytes(raw[: len(raw) - len(raw) % samples.itemsize])
    hop = max(1, cfg.cut_probe_rate * cfg.cut_quiet_window_ms // 1000)
    return [
        max(abs(value) for value in samples[index : index + hop]) / 32768
        for index in range(0, len(samples) - hop + 1, hop)
    ]


def find_quiet_point(audio: Path, target: float, duration: float, cfg: Config) -> float:
    """목표 경계 앞뒤 `cut_search_seconds` 안에서 가장 조용한 지점을 찾는다."""
    window_start = max(0.0, target - cfg.cut_search_seconds)
    window_end = min(duration, target + cfg.cut_search_seconds)
    levels = amplitudes(audio, window_start, window_end - window_start, cfg)
    hop_seconds = cfg.cut_quiet_window_ms / 1000
    return cutpoints.quietest(levels, hop_seconds, window_start, target)


def make_chunks(audio: Path, duration: float, cfg: Config, workdir: Path) -> list[Chunk]:
    """모델 입력 한도로 조각 수를 정하고, 경계를 가장 조용한 곳으로 옮겨 자른다.

    조각은 언제나 `[0, duration]`을 빈틈없이 덮는다 — 여기서 구간을 고르지 않는다(규칙 4).
    """
    cuts = cutpoints.plan_cuts(
        duration,
        audio.stat().st_size,
        cfg,
        lambda target: find_quiet_point(audio, target, duration, cfg),
    )
    if not cuts:
        return [Chunk(path=audio, offset=0.0, duration=duration, index=0)]
    log.debug(f"자를 시각: {', '.join(f'{cut:.2f}초' for cut in cuts)}")
    return split_at(audio, cuts, workdir)


def split_at(audio: Path, cuts: Sequence[float], workdir: Path) -> list[Chunk]:
    """ffmpeg 세그먼트 분할 한 번. **오프셋은 ffmpeg가 알려준 실제값을 쓴다.**

    `-ss`로 조각마다 탐색하면 매번 처음부터 디코딩한다. 게다가 요청한 시각과 실제로
    잘린 시각이 mp3 프레임 경계(~26ms)만큼 다른데, 요청값을 오프셋으로 쓰면 그 차이가
    자막 전체에 실린다(D13이 그 사고였다). `-c copy`라 재인코딩도 없다.
    """
    ffmpeg = require_ffmpeg()
    workdir.mkdir(parents=True, exist_ok=True)
    listing = workdir / f"{audio.stem}.segments.csv"
    _run(
        [
            ffmpeg, "-hide_banner", "-nostdin", "-y",
            "-i", str(audio),
            "-f", "segment",
            "-segment_times", ",".join(f"{cut:.6f}" for cut in cuts),
            "-segment_list", str(listing),
            "-segment_list_type", "csv",
            "-c", "copy",
            str(workdir / f"{audio.stem}.chunk%04d.mp3"),
        ]
    )  # fmt: skip
    rows = cutpoints.parse_segment_list(listing.read_text(encoding="utf-8"))
    if not rows:
        raise RuntimeError(f"ffmpeg가 조각 목록을 남기지 않았습니다: {listing}")
    return [
        Chunk(path=workdir / name, offset=start, duration=end - start, index=index)
        for index, (name, start, end) in enumerate(rows)
    ]
