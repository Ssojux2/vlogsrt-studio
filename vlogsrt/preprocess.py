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
    entry = _selected_stream(completed.stdout, source, f"a:{stream_index}", "오디오", "audio")
    return AudioStreamInfo(
        stream_index=_stream_global_index(entry, source),
        start_time=_stream_fraction(entry, "start_time", source),
        sample_rate=_stream_positive_int(entry, "sample_rate", source),
        channels=_stream_positive_int(entry, "channels", source),
    )


@dataclass(frozen=True, slots=True)
class MediaSummary:
    """한 파일의 스트림 구성과 화면 기하 — 입력 합성이 선언해야 하는 것만 담는다.

    `probe_video_stream`이 증명하는 것은 시계뿐이다. 영상 파일에서 FCPXML
    `format`·`asset`을 적으려면 화면 크기와 색 좌표, 오디오 유무를 더 알아야 한다.
    그 값들을 **같은 ffprobe 한 번**에서 읽어 선언과 증거가 갈라지지 않게 한다.
    모르는 값은 빈 문자열·`None`으로 그대로 돌려준다 — 여기서 기본값을 지어내면
    그 추측이 타임라인 선언이 되어 버린다.
    """

    video_streams: int
    audio_streams: int
    width: int
    height: int
    color_primaries: str
    color_transfer: str
    color_matrix: str
    audio_channels: int | None
    audio_rate: int | None


def probe_media_summary(source: Path) -> MediaSummary:
    """스트림 구성·화면 크기·색 좌표·오디오 파라미터를 한 번에 읽는다.

    비디오 스트림이 없으면 죽는다 — 화면이 없는 입력은 컷 대상이 아니다. 오디오는
    없을 수 있고 그때 `audio_*`는 `None`이다. 기하와 색은 **첫 비디오 스트림**,
    오디오 파라미터는 **첫 오디오 스트림**의 것이다.
    """
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "stream=index,codec_type,width,height,"
            "color_primaries,color_transfer,color_space,channels,sample_rate",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    streams = _stream_entries(completed.stdout, source)
    videos = [entry for entry in streams if entry.get("codec_type") == "video"]
    audios = [entry for entry in streams if entry.get("codec_type") == "audio"]
    if not videos:
        raise RuntimeError(f"비디오 스트림이 없습니다: {source}")
    first_audio = audios[0] if audios else None
    return MediaSummary(
        video_streams=len(videos),
        audio_streams=len(audios),
        width=_stream_positive_int(videos[0], "width", source),
        height=_stream_positive_int(videos[0], "height", source),
        color_primaries=_stream_text(videos[0], "color_primaries"),
        color_transfer=_stream_text(videos[0], "color_transfer"),
        color_matrix=_stream_text(videos[0], "color_space"),
        audio_channels=(
            None if first_audio is None else _stream_positive_int(first_audio, "channels", source)
        ),
        audio_rate=(
            None
            if first_audio is None
            else _stream_positive_int(first_audio, "sample_rate", source)
        ),
    )


def _stream_entries(stdout: str, source: Path) -> list[dict[str, object]]:
    """ffprobe JSON의 streams 목록 — 항목이 아닌 것이 섞이면 죽는다."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 출력이 JSON이 아닙니다: {source} ({exc})") from exc
    streams = data.get("streams") if isinstance(data, dict) else None
    if not isinstance(streams, list):
        raise RuntimeError(f"ffprobe JSON의 streams가 목록이 아닙니다: {source} (값 {streams!r})")
    entries: list[dict[str, object]] = []
    for entry in streams:
        if not isinstance(entry, dict):
            raise RuntimeError(f"스트림 항목이 깨졌습니다: {source} (값 {entry!r})")
        entries.append(entry)
    return entries


def _stream_text(entry: dict[str, object], field: str) -> str:
    """선언 문자열을 그대로 — 없거나 `unknown`이면 빈 문자열이다(추측 금지)."""
    value = entry.get(field)
    if not isinstance(value, str) or value in ("", "unknown"):
        return ""
    return value


def _selected_stream(
    stdout: str, source: Path, selector: str, kind: str, codec: str
) -> dict[str, object]:
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
            f"{kind} 스트림 {selector}이 없습니다: {source} — 범위를 벗어난 선택이다"
        )
    if len(streams) > 1:
        raise RuntimeError(
            f"선택이 모호합니다: {selector}이 스트림 {len(streams)}개를 골랐습니다: {source}"
        )
    entry = streams[0]
    if not isinstance(entry, dict):
        raise RuntimeError(f"고른 스트림 항목이 깨졌습니다: {source} (값 {entry!r})")
    if entry.get("codec_type") != codec:
        raise RuntimeError(
            f"고른 스트림이 {kind}가 아닙니다: {source} (codec_type {entry.get('codec_type')!r})"
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


# ─────────────────────────────────────────────────────────────
#  비디오 프레임 — CFR 증명과 프레임 추출 (P5-04)
# ─────────────────────────────────────────────────────────────
#
# 여기는 ffprobe/ffmpeg 경계일 뿐이다. 증명된 시계로 무엇을 뽑을지 고르는 것은
# vlogsrt/project/vision/sampling.py의 몫이다.

_JPEG_QUALITY = 2
"""mjpeg 품질(2가 최선). 조정 대상이 아니라 해시 대상 바이트를 닫는 포맷 계약이다 —
같은 프레임은 같은 설정에서 같은 바이트를 내야 한다."""

_JPEG_PIXFMT = "yuvj444p"
"""JPEG의 풀레인지 4:4:4. 색차 서브샘플링이 없어 홀수 칸수도 그대로 담는다 —
4:2:0은 짝수 칸수를 요구해서 65x65 원본을 66x65로 키우는 길이었다. 결정성은
설정이 고정된 이상 변하지 않는다(같은 프레임 = 같은 바이트)."""


@dataclass(frozen=True, slots=True)
class VideoStreamInfo:
    """ffprobe가 **증명한** CFR 비디오 스트림의 시계.

    `presentation_origin`은 첫 디코드 프레임의 실측 PTS(정수 pts × time_base)다 —
    컨테이너 start_time 같은 작성값이 아니라 측정값이며 그대로 보존된다.
    `frame_duration`은 선언 r_frame_rate의 정확한 역수, `frame_count`는 프레임
    타임스탬프 증거로 센 수다. 이 셋은 모든 프레임이 원점+순번 격자 위에 있음을
    증명한 **뒤에야** 돌아온다 — 정수 PTS × time_base 외의 계산은 없다.
    """

    stream_index: int
    presentation_origin: Fraction
    frame_duration: Fraction
    frame_count: int


def _validate_video_ordinal(stream_index: int) -> None:
    """비디오 순번은 0 이상 정수다 — bool은 int인 척하지 못 한다."""
    if isinstance(stream_index, bool) or not isinstance(stream_index, int) or stream_index < 0:
        raise RuntimeError(f"비디오 순번은 0 이상 정수여야 합니다: {stream_index!r}")


def probe_video_stream(source: Path, stream_index: int = 0) -> VideoStreamInfo:
    """선언과 디코드 증거를 한 번의 ffprobe로 읽어 균일 CFR 그리드를 증명한다.

    명목 r_frame_rate·프레임 수가 같다고 CFR이 증명되지 않는다 — 프레임마다의
    정수 PTS를 time_base로 곱해 `첫 PTS + 순번 × 프레임 길이` 격자와 **정확
    유리수로** 대조한다. 선언 시계가 양자화되어 있으면(예: 30000/1001을
    time_base 1/1000에 담기) 틱 델타가 프레임 길이와 어긋나는데, 원점과 프레임
    시각이 독립적으로 양자화되므로 한 틱까지의 오차를 허용한다 — 단, 프레임
    길이가 틱에 정확히 떨어지는 시계에서는 양자화 오차가 아예 없으므로 허용도
    0이다(25fps를 1/1000에 담은 스트림의 한 틱 어긋남은 지터다). 반올림된
    pts_time 문자열이나 초 단위 입실론은 쓰지 않는다.
    격자가 증명되면 선언 메타데이터(avg_frame_rate·nb_frames·start_pts·duration_ts)
    가 디코드 증거와 어긋나지 않는지 마저 본다 — 시각 선언은 정수 start_pts·
    duration_ts(선언 time_base 단위)로만 읽는다. 없거나 모르는(`N/A`) 선언도
    증명이 아니다. 증명에 실패하면 조용한 기본값 대신 어긋난 좌표를 말하며 죽는다.
    """
    _validate_video_ordinal(stream_index)
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-select_streams", f"v:{stream_index}",
            "-show_entries", "stream=index,codec_type,r_frame_rate,avg_frame_rate,time_base,"
            "nb_frames,start_pts,duration_ts",
            "-show_entries", "frame=pts",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    entry = _selected_stream(completed.stdout, source, f"v:{stream_index}", "비디오", "video")
    rate = _stream_positive_fraction(entry, "r_frame_rate", source)
    average = _stream_positive_fraction(entry, "avg_frame_rate", source)
    time_base = _stream_positive_fraction(entry, "time_base", source)
    pts = _frame_pts(completed.stdout, source)
    frame_duration = Fraction(1) / rate
    _prove_uniform_grid(pts, time_base, frame_duration, source)
    _prove_declared_evidence(entry, average, pts, time_base, frame_duration, source)
    return VideoStreamInfo(
        stream_index=_stream_global_index(entry, source),
        presentation_origin=pts[0] * time_base,
        frame_duration=frame_duration,
        frame_count=len(pts),
    )


def probe_video_grid(source: Path, stream_index: int) -> VideoStreamInfo:
    """같은 격자를 **패킷 타임스탬프**로 증명한다 — 디코드하지 않는다.

    `probe_video_stream`은 프레임을 전부 디코드해 증거를 모은다. 그 값은 실측에서
    4K HEVC 1,940프레임에 40초가 넘었고, 75분 묶음이면 한 시간대가 된다 — 입력을
    합성하려고 영상을 통째로 디코드할 수는 없다. 비디오 패킷은 프레임 하나에
    하나씩 붙고 그 pts가 곧 제시 시각이라, 디먹싱만으로 같은 격자를 증명한다
    (실측: 40,730프레임이 1.4초).

    증명 절차와 허용 오차는 프레임 경로와 **같은 함수**를 쓴다. 다른 것은 증거의
    출처뿐이라 진단 문구도 그대로다. 디코더가 실제로 그 프레임을 내는지까지 봐야
    하는 곳(프레임 표본 추출)은 여전히 `probe_video_stream`을 쓴다 — 이 함수는
    그것을 대신하지 않는다.
    """
    _validate_video_ordinal(stream_index)
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-select_streams", f"v:{stream_index}",
            "-show_entries", "stream=index,codec_type,r_frame_rate,avg_frame_rate,time_base,"
            "nb_frames,start_pts,duration_ts",
            "-show_entries", "packet=pts",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    entry = _selected_stream(completed.stdout, source, f"v:{stream_index}", "비디오", "video")
    rate = _stream_positive_fraction(entry, "r_frame_rate", source)
    average = _stream_positive_fraction(entry, "avg_frame_rate", source)
    time_base = _stream_positive_fraction(entry, "time_base", source)
    pts = _packet_pts(completed.stdout, source)
    frame_duration = Fraction(1) / rate
    _prove_uniform_grid(pts, time_base, frame_duration, source)
    _prove_declared_evidence(entry, average, pts, time_base, frame_duration, source)
    return VideoStreamInfo(
        stream_index=_stream_global_index(entry, source),
        presentation_origin=pts[0] * time_base,
        frame_duration=frame_duration,
        frame_count=len(pts),
    )


def _packet_pts(stdout: str, source: Path) -> list[int]:
    """패킷 타임스탬프를 정수 pts로 — 제시 순서로 정렬한다.

    패킷은 디코드 순서로 나온다(B프레임). 격자는 제시 순서의 것이므로 정렬하고,
    중복은 엄격한 증가 검사가 잡는다.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 출력이 JSON이 아닙니다: {source} ({exc})") from exc
    packets = data.get("packets") if isinstance(data, dict) else None
    if not isinstance(packets, list):
        raise RuntimeError(f"ffprobe JSON의 packets가 목록이 아닙니다: {source} (값 {packets!r})")
    pts: list[int] = []
    for packet in packets:
        value = packet.get("pts") if isinstance(packet, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"패킷 pts를 정수로 읽지 못했습니다: {source} (값 {value!r})")
        pts.append(value)
    if not pts:
        raise RuntimeError(
            f"비디오 패킷 기록이 없습니다: {source} — CFR 그리드를 증명할 수 없습니다"
        )
    return sorted(pts)


def _stream_positive_fraction(entry: dict[str, object], field: str, source: Path) -> Fraction:
    """스트림의 시계 필드를 정확 분수로 — 0이하는 시계가 아니다."""
    value = _stream_fraction(entry, field, source)
    if value <= 0:
        raise RuntimeError(f"스트림 {field}가 양수가 아닙니다: {source} (값 {value})")
    return value


def _frame_pts(stdout: str, source: Path) -> list[int]:
    """프레임 타임스탬프 증거를 정수 pts로 읽는다 — 제시(presentation) 순서로 정렬.

    프레임 기록은 디코드 순서로 나올 수 있다(B프레임). 격자는 제시 순서의
    것이므로 정렬하고, 중복은 엄격한 증가 검사가 잡는다. 건너뛰기 디코딩은
    증거로 쓰지 않는다 — 이 호출은 스트림의 프레임 전부를 받는다.
    """
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 출력이 JSON이 아닙니다: {source} ({exc})") from exc
    frames = data.get("frames") if isinstance(data, dict) else None
    if not isinstance(frames, list):
        raise RuntimeError(f"ffprobe JSON의 frames가 목록이 아닙니다: {source} (값 {frames!r})")
    pts: list[int] = []
    for frame in frames:
        value = frame.get("pts") if isinstance(frame, dict) else None
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"프레임 pts를 정수로 읽지 못했습니다: {source} (값 {value!r})")
        pts.append(value)
    if not pts:
        raise RuntimeError(
            f"디코드 프레임 기록이 없습니다: {source} — CFR 그리드를 증명할 수 없습니다"
        )
    return sorted(pts)


def _tick_tolerance(time_base: Fraction, frame_duration: Fraction) -> Fraction:
    """선언 시계가 양자화되어 있을 때만 정확히 한 declared tick을 허용한다.

    프레임 길이가 틱에 정확히 떨어지면(예: 25fps = 40틱 @ 1/1000) 양자화 오차는
    아예 없고 한 틱의 어긋남도 진짜 비균일이다. 그 밖의 시계(예: 30000/1001을
    1/1000에 담기 = 프레임당 1001/30틱)에서는 측정값이 틱으로 반올림되므로
    원점과 각 프레임 시각이 독립적으로 양자화된다 — 한 틱이 그 전부다.
    """
    ticks_per_frame = frame_duration / time_base
    return Fraction(0) if ticks_per_frame.denominator == 1 else time_base


def _prove_uniform_grid(
    pts: list[int], time_base: Fraction, frame_duration: Fraction, source: Path
) -> None:
    """정렬된 정수 PTS 전부가 `원점 + 순번 × 프레임 길이` 격자 위에 있는지 본다.

    오차 허용은 `_tick_tolerance`가 정하는 대로다. PTS는 교정하지 않고
    측정값을 그대로 둔다.
    """
    tolerance = _tick_tolerance(time_base, frame_duration)
    for ordinal in range(1, len(pts)):
        if pts[ordinal] <= pts[ordinal - 1]:
            raise RuntimeError(
                f"프레임 pts가 엄격하게 증가하지 않습니다: {source} "
                f"(순번 {ordinal} pts {pts[ordinal]} — 중복)"
            )
    origin = pts[0]
    for ordinal, value in enumerate(pts):
        error = (value - origin) * time_base - ordinal * frame_duration
        if abs(error) > tolerance:
            raise RuntimeError(
                f"프레임 타임스탬프가 원점+순번 격자를 벗어났습니다: {source} "
                f"(순번 {ordinal}, pts {value}, 오차 {error}초 > 허용 {tolerance}초) — "
                "프레임을 잃거나 점프하거나 지터 섞인 그리드는 증명하지 못한다"
            )


def _prove_declared_evidence(
    entry: dict[str, object],
    average: Fraction,
    pts: list[int],
    time_base: Fraction,
    frame_duration: Fraction,
    source: Path,
) -> None:
    """선언 메타데이터가 디코드 증거와 어긋나지 않는지 마저 증명한다.

    격자 증명은 디코드된 PTS만 봤다 — 여기서는 컨테이너가 **선언한** 나머지
    (avg_frame_rate·nb_frames·start_pts·duration_ts)가 그 증거와 닿는지 본다.
    시각 선언은 정수 start_pts·duration_ts(선언 time_base 단위)로만 읽는다 —
    start_time·duration 소수 문자열은 틱에서 반올림된 그림자라 두 번 양자화된다.
    avg_frame_rate가 r과 **정확히** 같기를 요구하지 않는다: 길이가 프레임 길이의
    정수배가 아닌 스트림(예: 0.5초짜리 30000/1001)은 ffprobe가 avg를 개수/길이로
    계산하므로 r과 합법적으로 어긋난다. 대신 개수로 잰 avg 길이가 r 기준 개수
    길이와 같은 허용(양자화 시계의 한 declared tick) 안인지 본다 — 실제로 어긋나는
    avg는 여전히 걸린다. 선언 개수가 디코드 프레임 수와 다르면 컨테이너가 다른
    스트림을 설명하고 있는 것이고, start_pts·duration_ts가 실측 원점·범위와
    어긋나면 시계가 하나가 아니다. 없거나 `N/A`인 선언은 파싱에서 죽는다(조용한
    0이 아니다).
    """
    tolerance = _tick_tolerance(time_base, frame_duration)
    by_count = len(pts) * frame_duration
    by_last_pts = (pts[-1] - pts[0]) * time_base + frame_duration
    by_average = Fraction(len(pts)) / average
    if abs(by_average - by_count) > tolerance:
        raise RuntimeError(
            f"선언 avg_frame_rate가 r_frame_rate 기준 길이와 어긋납니다: {source} "
            f"(avg {average}, 개수 {len(pts)} → {by_average}초, r 기준 {by_count}초, "
            f"허용 {tolerance}초) — 평균률이 어긋나는 스트림은 균일 CFR이 아니다"
        )
    declared_count = _stream_positive_int(entry, "nb_frames", source)
    if declared_count != len(pts):
        raise RuntimeError(
            f"선언 프레임 수가 디코드 증거와 다릅니다: {source} "
            f"(선언 {declared_count}, 디코드 {len(pts)})"
        )
    start_pts = _stream_int(entry, "start_pts", source)
    if start_pts != pts[0]:
        raise RuntimeError(
            f"선언 start_pts가 첫 디코드 pts와 다릅니다: {source} "
            f"(선언 {start_pts}, 디코드 {pts[0]}) — 정수 비교이며 허용오차가 없다"
        )
    duration_ticks = _stream_positive_int(entry, "duration_ts", source)
    declared_duration = duration_ticks * time_base
    beyond_count = abs(declared_duration - by_count) > tolerance
    beyond_last_pts = abs(declared_duration - by_last_pts) > tolerance
    if beyond_count or beyond_last_pts:
        raise RuntimeError(
            f"선언 duration_ts가 프레임 증거와 어긋납니다: {source} "
            f"(선언 {duration_ticks}틱 = {declared_duration}초, "
            f"개수×프레임 길이 {by_count}초, 마지막 pts 기준 {by_last_pts}초, "
            f"허용 {tolerance}초)"
        )


def _require_index(value: int, label: str) -> None:
    """번호는 0 이상 정수다 — bool은 세는 수가 아니다."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"{label}는 0 이상 정수여야 합니다: {value!r}")


def _long_edge_scale(long_edge: int) -> str:
    """긴 변만 long_edge 안으로 줄이는 스케일 — 가로세로비를 지키고 절대 키우지 않는다.

    상자를 `min(입력, 한도)`로 양 변 모두 입력 이하로 묶고 거기에 맞추
    (`force_original_aspect_ratio=decrease`)므로: 한도 안의 입력은 그대로 통과하고,
    줄일 때도 짧은 변은 올림으로 커지지 않는다. 비율 오차는 정수 래스터 반올림이
    남기는 것뿐이다. 치수를 짝수로 강제하지 않는다 — 홀수 래스터는 4:4:4 픽셀
    형식이 그대로 담는다.
    """
    return (
        f"scale=w='min(iw,{long_edge})':h='min(ih,{long_edge})'"
        ":force_original_aspect_ratio=decrease"
    )


def extract_frame_index(
    source: Path,
    stream_index: int,
    frame_index: int,
    destination: Path,
    *,
    long_edge: int,
) -> Path:
    """정확히 한 프레임을 이미지로 뽑는다 — 전역 스트림 번호와 디코드 프레임 번호로.

    `-ss` 시각 탐색을 쓰지 않는다: 요청한 시각과 디코드된 프레임이 어긋나면
    이웃 프레임이 조용히 뽑힌다. `select=eq(n,번호)`는 디코드 출력(=제시 순서)
    의 프레임 번호를 직접 고른다. 인코딩 설정은 고정되어 같은 프레임이면 같은
    바이트를 낸다 — 해시 대상의 결정성이 계약이다(P5-04).
    """
    _require_index(stream_index, "스트림 번호")
    _require_index(frame_index, "프레임 번호")
    if isinstance(long_edge, bool) or not isinstance(long_edge, int) or long_edge <= 0:
        raise RuntimeError(f"long_edge는 양수 정수여야 합니다: {long_edge!r}")
    ffmpeg = require_ffmpeg()
    destination.parent.mkdir(parents=True, exist_ok=True)
    command = _frame_extraction_command(
        ffmpeg, source, stream_index, frame_index, destination, long_edge
    )
    _run(command)
    if not destination.is_file():
        raise RuntimeError(f"프레임 이미지가 남지 않았습니다: {destination}")
    log.debug(f"프레임 추출 완료: {destination} (스트림 {stream_index}, 프레임 {frame_index})")
    return destination


def _frame_extraction_command(
    ffmpeg: str,
    source: Path,
    stream_index: int,
    frame_index: int,
    destination: Path,
    long_edge: int,
) -> list[str]:
    """한 프레임을 뽑는 고정 argv — 셸도 조립도 없이 이 형태 그대로다."""
    # select 인자 안의 쉼표는 이스케이프(\,)하고 필터 사이 쉼표는 그대로 둔다
    frame_filter = f"select=eq(n\\,{frame_index})" + "," + _long_edge_scale(long_edge)
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-y",
        "-i", str(source),
        "-map", f"0:{stream_index}",
        "-vf", frame_filter,
        "-fps_mode", "passthrough",
        "-frames:v", "1",
        "-q:v", str(_JPEG_QUALITY),
        "-pix_fmt", _JPEG_PIXFMT,
        "-fflags", "+bitexact",
        "-flags:v", "+bitexact",
        str(destination),
    ]  # fmt: skip
