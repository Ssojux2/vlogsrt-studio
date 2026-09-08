"""오디오 추출과 조각 나누기 — **유일한 ffmpeg 경계**(불변 규칙 7).

ffmpeg는 `subprocess`로 직접 부른다(규칙 10). 자를 자리를 고르는 계산은 전부
`cutpoints.py`에 있고 여기서는 외부 프로세스만 다룬다 — ffmpeg가 없는 환경에서도
분할 로직은 그대로 테스트된다.
"""

from __future__ import annotations

import array
import json
import re
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path

from . import cutpoints, log
from .config import Config
from .models import Chunk, Span

REPAIR_DIR = "repair"  # 재전사 조각은 따로 둔다. 1차 조각을 덮어쓰면 안 된다

# 조각 파일 이름은 **여기 한 줄에서만** 나온다. 검증 도구가 다른 이름(`batch*`)을 찾고
# 있었고, 그 바람에 청크 정렬 검사가 "파일이 없다 → 단일 청크 → 통과"로 조용히
# 넘어갔다(D32). 쓰는 쪽과 찾는 쪽이 같은 함수를 부르면 다시는 어긋나지 않는다.
_CHUNK_NAME = "{stem}.chunk{number}.mp3"


def chunk_template(stem: str) -> str:
    """ffmpeg `-f segment`의 출력 패턴. 번호는 ffmpeg가 채운다."""
    return _CHUNK_NAME.format(stem=stem, number="%04d")


def chunk_glob(stem: str) -> str:
    """이미 잘려 있는 조각을 찾는 glob. `chunk_template`과 같은 자리에서 온다."""
    return _CHUNK_NAME.format(stem=stem, number="*")


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
#  원본 PCM — 잰 시계의 원점 (P2-01)
# ─────────────────────────────────────────────────────────────

_S16LE_SAMPLE_WIDTH = 2  # s16le 한 샘플의 바이트 폭. 포맷의 사실이지 임계값이 아니다
_PCM_SAMPLE_RATE = 16000  # 이 단계의 계약 주파수. 원본은 무엇이든 여기로 리샘플된다


@dataclass(frozen=True, slots=True)
class AudioStreamInfo:
    """ffprobe가 본 오디오 스트림. `stream_index`는 ffprobe가 알려준 **전역 번호**다.

    `probe_audio_stream`에는 오디오 순번(0부터)을 넣고, 이 값으로 다시
    `-map 0:<번호>`가 정해진다 — 순번과 전역 번호는 영상이 앞서면 어긋난다.
    `start_time`은 컨테이너가 적어둔 값일 뿐 **디코드 원점의 증거가 아니다** —
    실측에서 AAC/MKV는 이 값이 −0.264초인데 디코드 원점은 −0.2초다.
    """

    stream_index: int
    start_time: Fraction
    sample_rate: int
    channels: int


@dataclass(frozen=True, slots=True)
class PcmExtraction:
    """16 kHz s16le PCM과 그 시계의 실측 원점.

    `pcm_sample0_file_time`이 PCM 0번 프레임의 파일 시각이다 — 프라이밍이 남든
    (AAC/MOV), 잘리든(AAC/MKV), 리샘플되든 겪은 뒤의 값이지 보정 상수가 아니다.
    """

    path: Path
    stream: AudioStreamInfo
    sample_count: int
    sample_width: int
    pcm_sample0_file_time: Fraction


def _validate_ordinal(stream_index: int) -> None:
    """오디오 순번은 0 이상 정수다 — bool은 int인 척하지 못 한다."""
    if isinstance(stream_index, bool) or not isinstance(stream_index, int) or stream_index < 0:
        raise RuntimeError(f"오디오 순번은 0 이상 정수여야 합니다: {stream_index!r}")


def probe_audio_stream(source: Path, stream_index: int) -> AudioStreamInfo:
    """ffprobe JSON에서 오디오 스트림의 시계를 **정확한 분수로** 읽는다.

    시각을 부동소수로 읽으면 1.936이 흔들린다 — `Fraction(str(value))`로 파싱한다.
    없는 필드·깨진 값은 어떤 필드인지 말하며 죽는다. 돌려주는 `stream_index`는
    ffprobe가 알려준 전역 번호다 — 순번이 아니다.
    """
    _validate_ordinal(stream_index)
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-select_streams", f"a:{stream_index}",
            "-show_entries", "stream=index,codec_type,start_time,sample_rate,channels",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    entry = _selected_stream(completed.stdout, source, stream_index)
    return AudioStreamInfo(
        stream_index=_stream_global_index(entry, source),
        start_time=_stream_fraction(entry, "start_time", source),
        sample_rate=_stream_positive_int(entry, "sample_rate", source),
        channels=_stream_positive_int(entry, "channels", source),
    )


def _selected_stream(stdout: str, source: Path, ordinal: int) -> dict[str, object]:
    """고르기가 실패하는 네 방식은 전부 다른 말로 죽는다 — 원인을 헷갈리지 않게."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 출력이 JSON이 아니다: {source} ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"ffprobe JSON에 streams 목록이 없습니다: {source}")
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError(f"ffprobe JSON의 streams가 목록이 아닙니다: {source} (값 {streams!r})")
    if not streams:
        raise RuntimeError(
            f"오디오 스트림 a:{ordinal}이 없습니다: {source} — 범위를 벗어난 선택이다"
        )
    if len(streams) > 1:
        raise RuntimeError(
            f"선택이 모호합니다: a:{ordinal}이 스트림 {len(streams)}개를 골랐습니다: {source}"
        )
    entry = streams[0]
    if not isinstance(entry, dict):
        raise RuntimeError(f"고른 스트림 항목이 깨졌습니다: {source} (값 {entry!r})")
    if entry.get("codec_type") != "audio":
        raise RuntimeError(
            f"고른 스트림이 오디오가 아닙니다: {source} (codec_type {entry.get('codec_type')!r})"
        )
    return entry


def _stream_global_index(entry: dict[str, object], source: Path) -> int:
    """전역 스트림 번호 — ffprobe가 JSON 숫자로만 준다. 0 이상이어야 매핑된다."""
    value = entry.get("index")
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"스트림 index를 읽지 못했습니다: {source} (값 {value!r})")
    return value


def _stream_fraction(entry: dict[str, object], field: str, source: Path) -> Fraction:
    value = entry.get(field)
    try:
        return Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"스트림 {field}를 읽지 못했습니다: {source} (값 {value!r})") from exc


def _stream_int(entry: dict[str, object], field: str, source: Path) -> int:
    value = entry.get(field)
    try:
        return int(str(value))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"스트림 {field}를 읽지 못했습니다: {source} (값 {value!r})") from exc


def _stream_positive_int(entry: dict[str, object], field: str, source: Path) -> int:
    value = _stream_int(entry, field, source)
    if value <= 0:
        raise RuntimeError(f"스트림 {field}가 양수가 아닙니다: {source} (값 {value})")
    return value


# 변환(aresample·aformat·asettb)이 끝난 뒤 ashowinfo가 남기는 프레임 기록.
# pts는 asettb가 정한 1/rate 단위의 정수라 곧 샘플 번호다. 토큰 경계를 못 박는다 —
# `pts:1.5`·`pts:1/2`를 `1`로 슬쩍 읽으면 원점이 소리 없이 틀어진다.
_ASHOWINFO_HEADER = re.compile(r"\[Parsed_ashowinfo_\d+ [^\]]*\]")
_ASHOWINFO_PTS = re.compile(r"(?<!\S)pts:(-?\d+)(?!\S)")
_ASHOWINFO_CHANNELS = re.compile(r"(?<!\S)channels:(\d+)(?!\S)")
_ASHOWINFO_RATE = re.compile(r"(?<!\S)rate:(\d+)(?!\S)")
_ASHOWINFO_SAMPLES = re.compile(r"(?<!\S)nb_samples:(\d+)(?!\S)")


@dataclass(frozen=True, slots=True)
class _ConvertedFrame:
    pts: int
    channels: int
    rate: int
    samples: int


def extract_pcm_s16le(
    source: Path, destination: Path, *, stream_index: int, sample_rate: int = 16000
) -> PcmExtraction:
    """원본 오디오를 s16le PCM으로 **잰 시계 그대로** 뽑는다(P2-01).

    `-copyts`로 컨테이너 시계를 유지하고, 변환 후 첫 프레임의 정수 pts를 PCM 0번
    프레임의 파일 시각으로 쓴다. 프라이밍이 남든 잘리든 그 실측값 그대로다 —
    `-start_at_zero`도, 시각을 다시 쓰는 `asetpts`도, 보정 상수도 없다.
    주파수는 이 단계의 계약인 16 kHz 하나다 — 원본이 44.1/48 kHz면 **리샘플되어**
    들어온다. 매핑은 ffprobe가 알려준 전역 번호로 정확히 한 스트림만 짚는다.
    """
    if (
        isinstance(sample_rate, bool)
        or not isinstance(sample_rate, int)
        or sample_rate != _PCM_SAMPLE_RATE
    ):
        raise RuntimeError(f"이 단계의 PCM 주파수는 {_PCM_SAMPLE_RATE}Hz다: {sample_rate!r}")
    stream = probe_audio_stream(source, stream_index)
    ffmpeg = require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    filters = f"aresample={sample_rate},aformat=sample_fmts=s16,asettb=1/{sample_rate},ashowinfo"
    completed = _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-nostdin", "-y",
            "-copyts",
            "-i", str(source),
            "-map", f"0:{stream.stream_index}",
            "-vn",
            "-af", filters,
            "-c:a", "pcm_s16le",
            "-f", "s16le",
            str(destination),
        ]
    )  # fmt: skip
    frames = _converted_frames(completed.stderr, source)
    origin = _sample0_file_time(frames, sample_rate, stream.channels, source)
    sample_count = _pcm_frame_count(destination, stream.channels)
    if sample_count != sum(frame.samples for frame in frames):
        raise RuntimeError(
            f"ashowinfo 기록이 파일 전체를 설명하지 못합니다: {destination} "
            f"(기록 {sum(frame.samples for frame in frames)}프레임, 파일 {sample_count}프레임)"
        )
    log.debug(f"PCM 추출 완료: {destination} ({sample_count}프레임, 원점 {origin}초)")
    return PcmExtraction(
        path=destination,
        stream=stream,
        sample_count=sample_count,
        sample_width=_S16LE_SAMPLE_WIDTH,
        pcm_sample0_file_time=origin,
    )


def _converted_frames(stderr: str, source: Path) -> list[_ConvertedFrame]:
    frames: list[_ConvertedFrame] = []
    for line in stderr.splitlines():
        if not _ASHOWINFO_HEADER.search(line):
            continue
        frame = _ConvertedFrame(
            pts=_frame_field(_ASHOWINFO_PTS, "pts", line, source),
            channels=_frame_field(_ASHOWINFO_CHANNELS, "channels", line, source),
            rate=_frame_field(_ASHOWINFO_RATE, "rate", line, source),
            samples=_frame_field(_ASHOWINFO_SAMPLES, "nb_samples", line, source),
        )
        # pts는 음수여도 된다(원점이 0보다 앞선다). 나머지는 양수여야 시계다.
        if frame.channels <= 0 or frame.rate <= 0 or frame.samples <= 0:
            raise RuntimeError(
                f"ashowinfo 값이 양수가 아닙니다: {source} "
                f"(channels {frame.channels}, rate {frame.rate}, nb_samples {frame.samples})"
            )
        frames.append(frame)
    if not frames:
        raise RuntimeError(
            f"변환된 프레임 기록(ashowinfo)이 없습니다: {source} — 원점을 잴 수 없다"
        )
    return frames


def _frame_field(pattern: re.Pattern[str], field: str, line: str, source: Path) -> int:
    match = pattern.search(line)
    if match is None:
        raise RuntimeError(f"ashowinfo 줄에서 {field}를 읽지 못했습니다: {source} ({line.strip()})")
    return int(match.group(1))


def _sample0_file_time(
    frames: Sequence[_ConvertedFrame], sample_rate: int, channels: int, source: Path
) -> Fraction:
    """변환 후 첫 프레임의 pts가 PCM 0번 프레임의 파일 시각이다.

    프레임 시계에 구멍이 있으면 파일 전체를 하나의 원점으로 믿지 않는다.
    """
    for frame in frames:
        if frame.channels != channels:
            raise RuntimeError(
                f"변환 뒤 채널이 프로브와 다릅니다: {source} "
                f"(프로브 {channels}, 출력 {frame.channels})"
            )
        if frame.rate != sample_rate:
            raise RuntimeError(
                f"변환 뒤 주파수가 요청과 다릅니다: {source} "
                f"(요청 {sample_rate}, 출력 {frame.rate})"
            )
    for index in range(1, len(frames)):
        earlier, later = frames[index - 1], frames[index]
        if later.pts != earlier.pts + earlier.samples:
            raise RuntimeError(
                f"프레임 시계가 불연속입니다: {source} (pts {earlier.pts} 뒤에 {later.pts})"
            )
    return Fraction(frames[0].pts, sample_rate)


def _pcm_frame_count(destination: Path, channels: int) -> int:
    total = destination.stat().st_size
    frame_bytes = _S16LE_SAMPLE_WIDTH * channels
    if total % frame_bytes:
        raise RuntimeError(
            f"PCM이 채널 프레임을 반쯤 남겼습니다: {destination} ({total} 바이트, "
            f"프레임 {frame_bytes} 바이트)"
        )
    return total // frame_bytes


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
            str(workdir / chunk_template(audio.stem)),
        ]
    )  # fmt: skip
    rows = cutpoints.parse_segment_list(listing.read_text(encoding="utf-8"))
    if not rows:
        raise RuntimeError(f"ffmpeg가 조각 목록을 남기지 않았습니다: {listing}")
    return [
        Chunk(path=workdir / name, offset=start, duration=end - start, index=index)
        for index, (name, start, end) in enumerate(rows)
    ]


def cut_spans(audio: Path, spans: Sequence[Span], duration: float, workdir: Path) -> list[Chunk]:
    """지목된 구간만 다시 잘라낸다. 되풀이를 다시 물어보기 위한 조각이다(D27).

    **1차 분할과 같은 장치를 쓴다** — 구간 경계를 전부 자를 자리로 넘겨 `split_at`을
    한 번 돌리고, 원하는 구간에 해당하는 조각만 고른다. `-ss`로 따로 탐색하지 않는
    이유는 D13과 같다: 요청한 시각과 실제로 잘린 시각이 mp3 프레임 경계만큼 다르고,
    그 차이를 오프셋으로 쓰면 자막이 통째로 밀린다. 여기서도 오프셋은 ffmpeg가
    CSV로 알려준 실제값이다.

    **버리는 구간이 아니다.** 여기서 고르지 않은 조각은 1차 전사 결과가 그대로 남는다.
    """
    if not spans:
        return []
    cuts = sorted({edge for span in spans for edge in span if 0.0 < edge < duration})
    if not cuts:
        # 구간이 오디오 전체다. 자를 자리가 없으면 원본이 곧 그 조각이다(`make_chunks`와 같다).
        # 빈 `-segment_times`를 넘기면 ffmpeg가 "Empty time specification"으로 죽는다.
        return [Chunk(path=audio, offset=0.0, duration=duration, index=0)]
    pieces = split_at(audio, cuts, workdir / REPAIR_DIR)
    wanted = [
        piece
        for piece in pieces
        if any(low <= piece.offset + piece.duration / 2 < high for low, high in spans)
    ]
    return [replace(piece, index=index) for index, piece in enumerate(wanted)]
