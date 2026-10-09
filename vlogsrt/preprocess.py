"""오디오 추출과 조각 나누기 — **유일한 ffmpeg 경계**(불변 규칙 7).

ffmpeg는 `subprocess`로 직접 부른다(규칙 10). 자를 자리를 고르는 계산은 전부
`cutpoints.py`에 있고 여기서는 외부 프로세스만 다룬다 — ffmpeg가 없는 환경에서도
분할 로직은 그대로 테스트된다.
"""

from __future__ import annotations

import array
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from fractions import Fraction
from pathlib import Path
from typing import Any

from . import cutpoints, log
from .config import Config
from .models import Chunk, Span
from .task_runtime import cancellation_active, check_cancel

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


def run_process(
    command: list[str], *, on_poll: Callable[[], None] | None = None, **kwargs: Any
) -> subprocess.CompletedProcess:
    if not cancellation_active() and on_poll is None:
        return subprocess.run(command, **kwargs)
    check_cancel()
    timeout = kwargs.pop("timeout", 1800.0)
    kwargs.pop("check", None)
    if kwargs.pop("capture_output", False):
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process = subprocess.Popen(command, **kwargs)
    deadline = time.monotonic() + timeout
    try:
        while True:
            check_cancel()
            if time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=0.15)
                if on_poll is not None:
                    on_poll()
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                if on_poll is not None:
                    on_poll()
                continue
    except BaseException:
        process.terminate()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
        raise


def _run(command: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        completed = run_process(command, capture_output=True, text=True, check=False)
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


# 스트림 확인 ffprobe의 마감 — 동작 임계값이 아니라 멈춘 자식을 무한히 기다리지
# 않는 상한이다(S2A-2 위상 검사). 조각 분할·추출에는 붙지 않는다: 그 경로들은
# 이미 실패 전파와 재시도 계약을 가진다.
_PROBE_TIMEOUT_SECONDS = 30.0


def _run_probe(command: list[str]) -> str:
    """마감이 정해진 ffprobe 한 번 — 위상 확인 전용의 `_run` 변형.

    실패의 네 가지 모양(실행 불가·비0 종료·마감 초과·깨진 출력)이 전부
    RuntimeError로 나간다. 이 경로의 호출자는 실패를 **절대 0으로 해석해서는
    안 된다** — '스트림 0개·무음'은 지원되는 상태지 실패의 위장이 아니다.
    """
    try:
        completed = run_process(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"스트림 확인이 마감({_PROBE_TIMEOUT_SECONDS:.0f}초)을 넘겼다:"
            f" {command[0]} {command[-1]}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"명령 실행 실패: {command[0]} ({exc})") from exc
    if completed.returncode != 0:
        tail = "\n".join(completed.stderr.strip().splitlines()[-5:])
        raise RuntimeError(f"{command[0]} 실패 (코드 {completed.returncode})\n{tail}")
    return completed.stdout


_KNOWN_CODEC_TYPES = frozenset({"video", "audio", "subtitle", "data", "attachment"})
"""ffprobe `codec_type`의 닫힌 값 집합 — 이 밖의 값은 증거가 깨졌다는 뜻이다.

위상 검사는 스트림 수로 유료 호출을 가르므로, 항목 하나의 분류를 모르는 채
'오디오 아님'으로 세면 깨진 증거가 0스트림(무음)으로 위장된다. 그래서 이
호출의 판독은 닫힌 집합 밖을 받지 않는다(다른 소비자의 `_stream_entries`
계약은 그대로다).
"""


def _stream_codec_type(entry: dict[str, object], source: Path) -> str:
    """한 스트림의 분류 — 없거나·틀린형이거나·모르는 값이면 죽는다(실패 닫힘)."""
    value = entry.get("codec_type")
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"스트림 항목에 codec_type이 없습니다: {source} (값 {value!r})")
    if value not in _KNOWN_CODEC_TYPES:
        raise RuntimeError(f"스트림 항목의 codec_type을 모릅니다: {source} (값 {value!r})")
    return value


def probe_audio_stream_count(source: Path) -> int:
    """ffprobe가 본 오디오 **스트림** 수 — 영상 유무와 무관하다(S2A-2).

    `probe_media_summary`는 컷 합성을 위해 영상 기하를 요구하지만, 위상 검사는
    오디오만 있는 원본도 받아야 한다. 이 호출은 `codec_type`만 센다: 스테레오·
    멀티채널 채널은 한 스트림이고, 실패는 결코 0으로 번역되지 않는다.
    """
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run_probe(
        [
            ffprobe, "-v", "error",
            "-show_entries", "stream=codec_type",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    streams = _stream_entries(completed, source)
    return sum(1 for entry in streams if _stream_codec_type(entry, source) == "audio")


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
_ASHOWINFO_INDEX = re.compile(r"(?<!\S)n:(\d+)(?!\S)")
# 번호 값이 망가진 줄(`n:1.5`)은 조각이 아니라 거절할 기록이다 — 있는지만 따로 본다.
_ASHOWINFO_INDEX_KEY = re.compile(r"(?<!\S)n:")
_ASHOWINFO_PTS = re.compile(r"(?<!\S)pts:(-?\d+)(?!\S)")
_ASHOWINFO_CHANNELS = re.compile(r"(?<!\S)channels:(\d+)(?!\S)")
_ASHOWINFO_RATE = re.compile(r"(?<!\S)rate:(\d+)(?!\S)")
_ASHOWINFO_SAMPLES = re.compile(r"(?<!\S)nb_samples:(\d+)(?!\S)")


@dataclass(frozen=True, slots=True)
class _ConvertedFrame:
    index: int
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
            ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin", "-y",
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
        # ashowinfo는 한 프레임을 av_log 여러 번에 나눠 찍는다. 그 사이에 다른 스레드의 줄이
        # 끼면 꼬리가 새 접두어를 달고 다시 찍히는데, 그 조각엔 `n:`이 없다. 조각을 건너뛰어도
        # 기록이 실제로 빠졌다면 아래 번호 연속성 검사가 드러낸다.
        if not _ASHOWINFO_HEADER.search(line) or not _ASHOWINFO_INDEX_KEY.search(line):
            continue
        frame = _converted_frame(line, source)
        if frame.index != len(frames):
            raise RuntimeError(
                f"ashowinfo 프레임 번호가 이어지지 않습니다: {source} "
                f"(기대 n:{len(frames)}, 기록 n:{frame.index})"
            )
        frames.append(frame)
    if not frames:
        raise RuntimeError(
            f"변환된 프레임 기록(ashowinfo)이 없습니다: {source} — 원점을 잴 수 없다"
        )
    return frames


def _converted_frame(line: str, source: Path) -> _ConvertedFrame:
    frame = _ConvertedFrame(
        index=_frame_field(_ASHOWINFO_INDEX, "n", line, source),
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
    return frame


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
    completed = run_process(command, capture_output=True, check=False)
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


_SELECT_CHAIN_MAX_TERMS = 50
"""`select` 식 한 사슬(`a+b+…`)에 두는 항의 상한 — ffmpeg 식 파서의 재귀 깊이 한도다.

libavutil/eval.c는 깊이 100(`Parser.stack_index`)이 바닥나면 AVERROR(ENOMEM)
("Cannot allocate memory")로 필터 초기화에서 죽는다. ffmpeg 9.0.1 실측: 평평한 `+` 사슬
100항 통과·101항 실패, 64×64 lavfi 입력에서도 같다 — 디코드 메모리가 아니다. 괄호 묶음의
깊이는 바깥 사슬 위치 + 안쪽 사슬 길이이므로 반씩 쪼개 묶으면 잎 길이 + log2(묶음 수)다.
50이면 2만 항(약 10시간 분량)까지 실측 통과한다. 외부 도구의 상수라 `_RAW_US_PER_SECOND`처럼
여기 산다 — `RawUnderstandingSettings`에 넣으면 입력 지문이 바뀌어 저장된 분석이 다시 청구된다.
"""


def _select_frames_expression(indices: Sequence[int]) -> str:
    """정렬된 프레임 번호를 고르는 select 식 — 균형 잡힌 괄호 나무로 파서 깊이를 묶는다.

    항의 집합과 평가 결과는 평평한 사슬과 같다(디코드 번호 `n`만 본다). 50항 이하는
    평평한 그대로라 기존 호출의 식 문자열도 바뀌지 않는다.
    """
    terms = [f"eq(n\\,{index})" for index in indices]

    def build(chunk: list[str]) -> str:
        if len(chunk) <= _SELECT_CHAIN_MAX_TERMS:
            return "+".join(chunk)
        half = len(chunk) // 2
        return f"({build(chunk[:half])})+({build(chunk[half:])})"

    return build(terms)


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


def extract_frame_indices(
    source: Path,
    stream_index: int,
    frame_indices: Sequence[int],
    directory: Path,
    *,
    long_edge: int,
    on_frame: Callable[[int, Path], None] | None = None,
) -> dict[int, Path]:
    """Decode once in presentation order and publish only completed JPEGs.

    Source decode indices remain exact; no approximate timestamp seeking is used.
    image2 atomic_writing prevents progress polling from publishing partial files.
    Completed frames survive cancellation, while the private staging area is removed.
    select 식은 균형 잡힌 괄호 나무다 — 평평한 `+` 사슬은 101항에서 ffmpeg 식 파서가
    깊이 한도로 죽는다(`_SELECT_CHAIN_MAX_TERMS`).
    """
    _require_index(stream_index, "스트림 번호")
    for index in frame_indices:
        _require_index(index, "프레임 번호")
    if isinstance(long_edge, bool) or not isinstance(long_edge, int) or long_edge <= 0:
        raise RuntimeError("long_edge는 양수 정수여야 합니다")
    indices = sorted(set(frame_indices))
    if not indices:
        return {}
    directory.mkdir(parents=True, exist_ok=True)
    published: dict[int, Path] = {}
    with tempfile.TemporaryDirectory(prefix=".frame-batch-", dir=directory) as temporary:
        staging = Path(temporary)

        def publish_ready() -> None:
            check_cancel()
            while len(published) < len(indices):
                ordinal = len(published)
                staged = staging / f"{ordinal:08d}.jpg"
                if not staged.is_file():
                    break
                index = indices[ordinal]
                target = directory / f"frame_{index:08d}.jpg"
                os.replace(staged, target)
                published[index] = target
                if on_frame is not None:
                    on_frame(index, target)
                check_cancel()

        select = _select_frames_expression(indices)
        command = [
            require_ffmpeg(),
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(source),
            "-map",
            f"0:{stream_index}",
            "-vf",
            "select=" + select + "," + _long_edge_scale(long_edge),
            "-fps_mode",
            "passthrough",
            "-frames:v",
            str(len(indices)),
            "-q:v",
            str(_JPEG_QUALITY),
            "-pix_fmt",
            _JPEG_PIXFMT,
            "-fflags",
            "+bitexact",
            "-flags:v",
            "+bitexact",
            "-f",
            "image2",
            "-start_number",
            "0",
            "-atomic_writing",
            "1",
            str(staging / "%08d.jpg"),
        ]
        completed = run_process(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=1800.0,
            on_poll=publish_ready,
        )
        if completed.returncode:
            raise RuntimeError("프레임 묶음 추출 실패: " + completed.stderr[-500:])
        publish_ready()
        if len(published) != len(indices):
            raise RuntimeError(f"프레임 묶음이 완전하지 않습니다: {len(published)}/{len(indices)}")
    return published


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


# ─────────────────────────────────────────────────────────────
#  원본 미디어 경계 — A3 조사·프레임 정확 추출 (2026-10-02 v2 계획 §8)
# ─────────────────────────────────────────────────────────────
#
# 위 계층(가져오기·A3 러너)은 이 절의 데이터 타입만 알면 된다. 의존은 한 방향이다 —
# 이 모듈은 vlogsrt.project를 모르며 ffprobe/ffmpeg 호출도 여기(규칙 7)에만 있다.
# 시각은 정수 PTS × time_base, 또는 정확한 `Fraction`으로만 다룬다(§8.3).

_RAW_US_PER_SECOND = 1_000_000
"""ffmpeg `-ss`/`-t` 파서의 최소 눈금(AV_TIME_BASE). 초→문자열 반올림의 근거다."""

# 촬영 시각 태그를 원본 순서의 근거로 인정하는 상식 bound. 1990년 이전은 소비자
# 디지털 카메라가 없고(1904-01-01은 mac epoch 오류의 전형), 살짝 미래는 카메라
# 시계 오차로 본다 — 그보다 미래면 시계가 틀렸다는 뜻이라 순서 근거가 못 된다.
_CREATION_EARLIEST = datetime(1990, 1, 1, tzinfo=UTC)
_CREATION_FUTURE_SLACK = timedelta(hours=48)


class RawMediaUnsupported(RuntimeError):
    """지원 매트릭스 밖의 원본 — 이유를 말하고 죽는다. 정밀도를 몰래 낮추지 않는다."""


class RawClipVerificationError(RuntimeError):
    """추출물 검증 실패 — 이 파일은 게시할 수 없다(스테이징에 머문다)."""


class RawStagingError(RuntimeError):
    """스테이징 배치의 거부 — 원본이나 이웃 출력을 덮어칠 수 있는 자리는 쓰지 않는다.

    A3P-F2: ffmpeg을 시작하기 전(별칭·이미 있는 경로)과 게시 직전(경합)에 일어난다.
    무엇 하나 덮어쓰지 않은 채로 죽는다 — 원본 불변은 이 경계의 절대 조건이다.
    """


@dataclass(frozen=True, slots=True)
class CreationTime:
    """컨테이너가 적은 촬영 시각과 그 판정.

    `valid`는 '순서 정렬의 근거로 쓸 수 있는가'다 — ISO-8601로 파싱되고 시간대가
    있으며 상식 bound 안에 있어야 한다. 못 미치면 `utc`는 None이고, raw 값은
    기록으로 그대로 남긴다(없는 값을 지어내지 않는다).
    """

    raw: str
    valid: bool
    utc: datetime | None


_CREATION_TAGS = ("com.apple.quicktime.creationdate", "creation_time")
"""촬영 시각 태그 — 앞의 것이 이긴다(D53).

FCP는 가져온 원본을 다시 감싸며 컨테이너 `creation_time`을 **가져온 시각**으로 덮는다.
촬영 시각은 애플 키 `com.apple.quicktime.creationdate`에만 남는다(실측: DJI 파일명의 촬영
시각과 일치) — 그래서 애플 키를 먼저 읽고, 없거나 못 믿을 때만 `creation_time`을 쓴다.
"""


def _parse_creation_time(raw: str) -> CreationTime:
    if not raw:
        return CreationTime(raw="", valid=False, utc=None)
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return CreationTime(raw=raw, valid=False, utc=None)
    if stamp.tzinfo is None:
        # 시간대 없는 시각은 같은 카드 안에서도 비교할 수 없다 — 순서 근거가 아니다.
        return CreationTime(raw=raw, valid=False, utc=None)
    utc = stamp.astimezone(UTC)
    if utc < _CREATION_EARLIEST or utc > datetime.now(UTC) + _CREATION_FUTURE_SLACK:
        return CreationTime(raw=raw, valid=False, utc=None)
    return CreationTime(raw=raw, valid=True, utc=utc)


def _creation_from_tags(fmt: dict[str, object]) -> CreationTime:
    """태그 순서대로 처음 유효한 촬영 시각 — 하나도 없으면 처음 보인 원값을 무효로 남긴다."""
    first_raw = ""
    for key in _CREATION_TAGS:
        verdict = _parse_creation_time(_format_tag(fmt, key))
        if verdict.valid:
            return verdict
        first_raw = first_raw or verdict.raw
    return CreationTime(raw=first_raw, valid=False, utc=None)


def probe_creation_time(source: Path) -> CreationTime:
    """원본 하나의 촬영 시각만 읽는다 — 컨테이너 태그뿐이라 디코딩이 없다(D53 되채움)."""
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format_tags=" + ",".join(_CREATION_TAGS),
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    fmt = _raw_probe_json(completed.stdout, source).get("format")
    return _creation_from_tags(fmt if isinstance(fmt, dict) else {})


@dataclass(frozen=True, slots=True)
class RawAudioTrack:
    """ffprobe가 본 오디오 스트림 하나. `stream_index`는 전역 번호다.

    `start_time`·`start_pts`는 컨테이너 **선언**일 뿐 디코드 원점의 증거가 아니다
    (AAC 프라이밍은 이 값을 음수로 만든다). 실측 패킷 증거는 `AudioPacketExtent`.
    """

    stream_index: int
    codec_name: str
    channels: int
    sample_rate: int
    time_base: Fraction
    start_time: Fraction
    start_pts: int | None


@dataclass(frozen=True, slots=True)
class RawVideoMedia:
    """첫 비디오 스트림의 선언 — 코덱·화면·색·회전·시계.

    모르는 색 좌표는 빈 문자열 그대로다. `width`/`height`는 **저장된 기하**다 —
    회전(90/270°)이 있으면 재생 화면은 뒤바뀐다. 시계의 실측(격자 증명)은
    `probe_extraction_clock`의 몫이다.
    """

    stream_index: int
    codec_name: str
    profile: str
    pix_fmt: str
    bit_depth: int
    width: int
    height: int
    color_primaries: str
    color_transfer: str
    color_matrix: str
    color_range: str
    rotation_degrees: int
    time_base: Fraction
    r_frame_rate: Fraction
    avg_frame_rate: Fraction
    start_pts: int
    duration_ts: int | None


@dataclass(frozen=True, slots=True)
class RawMediaProbe:
    """가져오기가 필요로 하는 원본 조사 결과 전부 — ffprobe 한 번에서 나온다."""

    source: Path
    container: str
    duration: Fraction
    format_start: Fraction
    """format start_time 선언 — 컨테이너 시각표의 시작(절대 좌표 재료).

    선언 창은 `format_start + duration`까지다(절대 끝). 시작이 0이 아닌
    컨테이너에서 길이(구간)와 절대 끝을 바꿔 비교하면 정상 파일을 거짓으로
    막는다(A3 실측). 없는 선언("N/A")은 0 — 창이 좁아질 뿐 열리지는 않는다.
    """

    creation_time: CreationTime
    video: RawVideoMedia
    audio: tuple[RawAudioTrack, ...]
    other_tracks: tuple[tuple[int, str], ...]
    """첫 비디오·오디오가 아닌 스트림의 (전역 번호, codec_type). 추가 비디오
    스트림도 여기에 남는다 — 지원 매트릭스가 이걸 본다."""
    ffprobe_version: str


def probe_raw_media(source: Path, *, video_ordinal: int = 0) -> RawMediaProbe:
    """원본 하나를 ffprobe 한 번으로 조사한다 — 가져오기용 빠른 경로.

    비디오 스트림이 없거나 선언이 깨져 있으면 죽는다(확장자만 믿지 않는다, §5.2).
    값을 지어내지 않는다: 모르는 색 좌표는 빈 문자열, 없는 촬영 시각은
    `CreationTime.valid=False`다.
    """
    _validate_video_ordinal(video_ordinal)
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-show_entries",
            "stream=index,codec_type,codec_name,profile,pix_fmt,width,height,"
            "color_primaries,color_transfer,color_space,color_range,"
            "r_frame_rate,avg_frame_rate,time_base,start_time,start_pts,duration_ts,"
            "sample_rate,channels:stream_side_data=rotation"
            ":format=format_name,duration,start_time:format_tags=" + ",".join(_CREATION_TAGS),
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    return _parse_raw_probe(completed.stdout, source, video_ordinal)


def _raw_probe_json(stdout: str, source: Path) -> dict[str, object]:
    """ffprobe JSON 최상위 객체 — streams와 format을 함께 읽는 경로의 공통 파서."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"ffprobe 출력이 JSON이 아닙니다: {source} ({exc})") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"ffprobe JSON이 객체가 아닙니다: {source} (값 {data!r})")
    return data


def _parse_raw_probe(stdout: str, source: Path, video_ordinal: int) -> RawMediaProbe:
    data = _raw_probe_json(stdout, source)
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError(f"ffprobe JSON의 streams가 목록이 아닙니다: {source} (값 {streams!r})")
    typed = [
        (entry, _stream_codec_type(entry, source)) for entry in _stream_entries(stdout, source)
    ]
    videos = [entry for entry, kind in typed if kind == "video"]
    audios = [entry for entry, kind in typed if kind == "audio"]
    if video_ordinal >= len(videos):
        raise RuntimeError(
            f"비디오 스트림이 없거나 순번을 벗어났습니다: {source} (v:{video_ordinal})"
        )
    chosen = videos[video_ordinal]
    others = tuple(
        (_stream_global_index(entry, source), kind)
        for entry, kind in typed
        if entry is not chosen and kind != "audio"
    )
    fmt = data.get("format")
    if not isinstance(fmt, dict):
        raise RuntimeError(f"ffprobe JSON에 format이 없습니다: {source}")
    return RawMediaProbe(
        source=source,
        container=_format_text(fmt, "format_name", source),
        duration=_format_fraction(fmt, "duration", source),
        format_start=_format_start_seconds(fmt, source),
        creation_time=_creation_from_tags(fmt),
        video=_raw_video_media(chosen, source),
        audio=tuple(_raw_audio_track(entry, source) for entry in audios),
        other_tracks=others,
        ffprobe_version=_tool_version("ffprobe"),
    )


def _format_text(fmt: dict[str, object], field: str, source: Path) -> str:
    value = fmt.get(field)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"format {field}를 읽지 못했습니다: {source} (값 {value!r})")
    return value


def _format_fraction(fmt: dict[str, object], field: str, source: Path) -> Fraction:
    value = fmt.get(field)
    try:
        parsed = Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"format {field}를 읽지 못했습니다: {source} (값 {value!r})") from exc
    if parsed <= 0:
        raise RuntimeError(f"format {field}가 양수가 아닙니다: {source} (값 {value!r})")
    return parsed


def _format_start_seconds(fmt: dict[str, object], source: Path) -> Fraction:
    """format start_time 선언 — 없는 선언은 0(fail-closed), 부서진 선언은 죽인다.

    ffprobe는 값이 없는 컨테이너에 "N/A"를 적는다. 0으로 두면 지원 경계의
    선언 창이 좁아질 뿐이다(길이와 절대 끝이 우연히 같아지는 0 시작 파일의
    기존 행동). 숫자로 못 읽는 값은 부서진 선언이다 — 0으로 돌려주면 없는
    창을 지어내므로 조사 단계에서 죽인다.
    """
    value = fmt.get("start_time")
    if value is None or (isinstance(value, str) and value.strip().lower() in ("", "n/a")):
        return Fraction(0)
    try:
        return Fraction(str(value))
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        raise RuntimeError(f"format start_time를 읽지 못했습니다: {source} (값 {value!r})") from exc


def _format_tag(fmt: dict[str, object], key: str) -> str:
    tags = fmt.get("tags")
    if not isinstance(tags, dict):
        return ""
    value = tags.get(key)
    return value if isinstance(value, str) else ""


_PIXFMT_DEPTH = re.compile(r"(\d+)(?:le|be)?$")
"""픽셀 형식 이름 끝의 비트 폭 — `yuv420p10le`→10, `yuv420p`→8(접미 없음)."""


def _pix_fmt_bit_depth(pix_fmt: str) -> int:
    """이름에서 채널당 비트 폭을 읽는다.

    패킹 rgb24/rgb48의 끝수는 채널을 곱한 **픽셀** 폭이고(24=8×3), 평면 gbrp10의
    끝수는 채널당 폭이다 — 두 가족을 갈라 읽는다. 못 읽으면 8이다(미지원 형식은
    어차피 지원 매트릭스에서 막힌다).
    """
    if pix_fmt.startswith(("rgb", "bgr", "gbr")):
        body = pix_fmt[:-2] if pix_fmt.endswith(("le", "be")) else pix_fmt
        match = re.search(r"(\d+)$", body)
        if match:
            width = int(match.group(1))
            if re.search(r"p\d+$", body):
                return width
            if width >= 24 and width % 3 == 0:
                return width // 3
        return 8
    match = _PIXFMT_DEPTH.search(pix_fmt)
    return int(match.group(1)) if match else 8


def _stream_rotation(entry: dict[str, object], source: Path) -> int:
    """display matrix 회전 — side_data가 있으면 그 값, 없으면 0이다.

    회전 키가 있는데 숫자가 아니면 죽는다: 0으로 돌려주면 '회전 없음'이라는
    거짓 선언이 된다.
    """
    side = entry.get("side_data_list")
    if side is None:
        return 0
    if not isinstance(side, list):
        raise RuntimeError(f"side_data_list가 목록이 아닙니다: {source} (값 {side!r})")
    for item in side:
        if not isinstance(item, dict) or "rotation" not in item:
            continue
        value = item["rotation"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"회전 값을 읽지 못했습니다: {source} (값 {value!r})")
        return int(value)
    return 0


def _raw_video_media(entry: dict[str, object], source: Path) -> RawVideoMedia:
    pix_fmt = _stream_text(entry, "pix_fmt")
    if not pix_fmt:
        raise RuntimeError(f"비디오 pix_fmt를 읽지 못했습니다: {source}")
    return RawVideoMedia(
        stream_index=_stream_global_index(entry, source),
        codec_name=_require_stream_text(entry, "codec_name", source),
        profile=_stream_text(entry, "profile"),
        pix_fmt=pix_fmt,
        bit_depth=_pix_fmt_bit_depth(pix_fmt),
        width=_stream_positive_int(entry, "width", source),
        height=_stream_positive_int(entry, "height", source),
        color_primaries=_stream_text(entry, "color_primaries"),
        color_transfer=_stream_text(entry, "color_transfer"),
        color_matrix=_stream_text(entry, "color_space"),
        color_range=_stream_text(entry, "color_range"),
        rotation_degrees=_stream_rotation(entry, source),
        time_base=_stream_positive_fraction(entry, "time_base", source),
        r_frame_rate=_stream_positive_fraction(entry, "r_frame_rate", source),
        avg_frame_rate=_stream_positive_fraction(entry, "avg_frame_rate", source),
        start_pts=_stream_int(entry, "start_pts", source),
        duration_ts=_optional_stream_int(entry, "duration_ts", source),
    )


def _require_stream_text(entry: dict[str, object], field: str, source: Path) -> str:
    value = _stream_text(entry, field)
    if not value:
        raise RuntimeError(f"스트림 {field}를 읽지 못했습니다: {source}")
    return value


def _raw_audio_track(entry: dict[str, object], source: Path) -> RawAudioTrack:
    return RawAudioTrack(
        stream_index=_stream_global_index(entry, source),
        codec_name=_require_stream_text(entry, "codec_name", source),
        channels=_stream_positive_int(entry, "channels", source),
        sample_rate=_stream_positive_int(entry, "sample_rate", source),
        time_base=_stream_positive_fraction(entry, "time_base", source),
        start_time=_stream_fraction(entry, "start_time", source),
        start_pts=_optional_stream_pts(entry, "start_pts", source),
    )


def _optional_stream_int(entry: dict[str, object], field: str, source: Path) -> int | None:
    """선언 정수는 없을 수 있다(mkv/webm의 duration_ts 등) — 없음은 None, 깨진 값은 죽는다."""
    value = entry.get(field)
    if value is None or value == "N/A":
        return None
    parsed = _stream_int(entry, field, source)
    if parsed <= 0:
        raise RuntimeError(f"스트림 {field}가 양수가 아닙니다: {source} (값 {value!r})")
    return parsed


def _optional_stream_pts(entry: dict[str, object], field: str, source: Path) -> int | None:
    """선언 pts는 없을 수 있다 — 없음은 None, 깨진 값은 죽는다."""
    value = entry.get(field)
    if value is None or value == "N/A":
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise RuntimeError(f"스트림 {field}를 읽지 못했습니다: {source} (값 {value!r})")
    return value


def _tool_version(tool: str) -> str:
    completed = _run([tool, "-version"])
    first = completed.stdout.strip().splitlines()
    if not first:
        raise RuntimeError(f"{tool} -version 출력이 비었습니다")
    return first[0]


# ─────────────────────────────────────────────────────────────
#  추출 시계 — 정수 PTS 증거와 프레임 경계 (§8.3)
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RawVideoClock:
    """비디오 스트림의 **측정된** 시계 — 패킷(디먹스) 증거다.

    `presentation_origin`은 첫 패킷 정수 pts × time_base, `exact_end`는 마지막
    프레임 제시의 끝이다. `uniform_grid`는 프레임 격자 증명(CFR)의 결과고,
    증명에 실패하면 `grid_note`가 위반 내용을 그대로 싣는다 — VFR을 죽이지 않고
    '측정된 사실'로 돌려준다(추출은 지원 매트릭스에서 VFR을 막는다).
    이 증거는 타임스탬프 격자만 증명한다. **소스의 디코드 품질은 증명하지
    않는다** — 디코드 검증은 추출물에 대해 `extract_raw_clip`이 한다.
    """

    stream_index: int
    time_base: Fraction
    first_pts: int
    last_pts: int
    presentation_origin: Fraction
    frame_duration: Fraction
    frame_count: int
    exact_end: Fraction
    uniform_grid: bool
    grid_note: str


def probe_extraction_clock(
    source: Path, *, video_ordinal: int = 0, probe: RawMediaProbe | None = None
) -> RawVideoClock:
    """비디오 패킷 타임스탬프로 격자를 잰다 — 디코드하지 않는다.

    격자 증명 자체는 `probe_video_grid`와 같은 함수를 쓴다. 다른 점은 실패의
    취급뿐이다: 캡션 엔진의 증명은 CFR이 증명될 때까지 살아있는 계약이지만,
    원본 경계는 VFR도 '측정된 사실'로 돌려줘야 가져오기가 판단을 할 수 있다.
    """
    _validate_video_ordinal(video_ordinal)
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-select_streams", f"v:{video_ordinal}",
            "-show_entries", "stream=index,codec_type,r_frame_rate,avg_frame_rate,time_base,"
            "nb_frames,start_pts,duration_ts",
            "-show_entries", "packet=pts",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    entry = _selected_stream(completed.stdout, source, f"v:{video_ordinal}", "비디오", "video")
    rate = _stream_positive_fraction(entry, "r_frame_rate", source)
    time_base = _stream_positive_fraction(entry, "time_base", source)
    pts = _packet_pts(completed.stdout, source)
    clock = _measured_clock(entry, rate, time_base, pts, source)
    if probe is not None and clock.stream_index != probe.video.stream_index:
        raise RuntimeError(
            f"시계의 스트림이 프로브와 다릅니다: {source} "
            f"(시계 {clock.stream_index}, 프로브 {probe.video.stream_index}) — 순번이 어긋났다"
        )
    return clock


def _measured_clock(
    entry: dict[str, object],
    rate: Fraction,
    time_base: Fraction,
    pts: list[int],
    source: Path,
) -> RawVideoClock:
    frame_duration = Fraction(1) / rate
    notes: list[str] = []
    uniform = True
    try:
        _prove_uniform_grid(pts, time_base, frame_duration, source)
    except RuntimeError as exc:
        uniform = False
        notes.append(str(exc))
    try:
        average = _stream_positive_fraction(entry, "avg_frame_rate", source)
        _prove_declared_evidence(entry, average, pts, time_base, frame_duration, source)
    except RuntimeError as exc:
        # 격자가 균일해도 선언이 어긋나면 증거가 갈라진다 — VFR 판정은 바꾸지 않고
        # 기록으로만 남긴다(선언 부재를 VFR로 오인해 정상 원본을 막지 않는다).
        notes.append(f"선언 불일치: {exc}")
    origin = pts[0] * time_base
    exact_end = (
        origin + len(pts) * frame_duration
        if uniform
        else _vfr_exact_end(pts, time_base, frame_duration)
    )
    return RawVideoClock(
        stream_index=_stream_global_index(entry, source),
        time_base=time_base,
        first_pts=pts[0],
        last_pts=pts[-1],
        presentation_origin=origin,
        frame_duration=frame_duration,
        frame_count=len(pts),
        exact_end=exact_end,
        uniform_grid=uniform,
        grid_note="\n".join(notes),
    )


def _vfr_exact_end(pts: list[int], time_base: Fraction, frame_duration: Fraction) -> Fraction:
    """균일하지 않은 시계의 끝 — 마지막 델타로 잰 추정치임을 밝혀둔다.

    VFR은 추출이 막혀 있으므로 이 값은 기록용이다. 프레임이 하나뿐이면 델타를
    잴 수 없어 선언 프레임 길이로 추정한다.
    """
    if len(pts) >= 2:
        last_delta = (pts[-1] - pts[-2]) * time_base
    else:
        last_delta = frame_duration
    return pts[-1] * time_base + last_delta


@dataclass(frozen=True, slots=True)
class RawPtsRange:
    """원본 좌표의 반개 구간 — `[start_pts, end_pts)` (틱).

    틱은 time_base와 함께 **기록**이다. ffmpeg에 넘기는 요청은 초(Fraction)에서
    µs 문자열로 정확히 변환되고, 실제 결과는 추출물 검증이 잰다.
    """

    time_base: Fraction
    start_pts: int
    end_pts: int

    @property
    def start_seconds(self) -> Fraction:
        return self.start_pts * self.time_base

    @property
    def end_seconds(self) -> Fraction:
        return self.end_pts * self.time_base

    @property
    def duration_seconds(self) -> Fraction:
        return (self.end_pts - self.start_pts) * self.time_base


def _as_fraction_seconds(value: object, label: str) -> Fraction:
    """시각 인자는 정확한 유리수만 받는다 — float는 근사라 족쇄가 된다(§8.3)."""
    if isinstance(value, bool) or not isinstance(value, (Fraction, int)):
        raise RuntimeError(f"{label}는 Fraction 또는 정수여야 합니다: {value!r}")
    return Fraction(value)


def _snap_ticks(seconds: Fraction, time_base: Fraction, *, ceil: bool) -> int:
    ticks = seconds / time_base
    return math.ceil(ticks) if ceil else math.floor(ticks)


def _validate_frame_window(first: int, after_last: int, frame_count: int) -> None:
    for label, value in (("첫 프레임", first), ("끝 프레임(제외)", after_last)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"{label}은 정수여야 합니다: {value!r}")
    if first < 0 or after_last <= first or after_last > frame_count:
        raise RuntimeError(
            f"프레임 창이 잘못되었습니다: [{first}, {after_last}) / {frame_count}프레임"
        )


def clock_frame_range(clock: RawVideoClock, first_frame: int, after_last_frame: int) -> RawPtsRange:
    """프레임 번호 반개 구간을 원본 PTS 범위로 — 격자 위의 정확한 유리수에서 간다."""
    _validate_frame_window(first_frame, after_last_frame, clock.frame_count)
    start = clock.presentation_origin + first_frame * clock.frame_duration
    end = clock.presentation_origin + after_last_frame * clock.frame_duration
    return RawPtsRange(
        time_base=clock.time_base,
        start_pts=_snap_ticks(start, clock.time_base, ceil=False),
        end_pts=_snap_ticks(end, clock.time_base, ceil=True),
    )


def clock_range_covering(clock: RawVideoClock, start: Fraction, end: Fraction) -> RawPtsRange:
    """초 범위를 **바깥으로** 덮는 프레임 경계를 고른다.

    안쪽으로 스냅하면 경계 프레임이 잘리는 방향으로 내려간다 — 원본 보존(§6.1)은
    바깥으로만 간다. 요청이 원본 프레임과 하나도 안 겹치면 죽는다(빈 조각 금지).
    """
    start_s = _as_fraction_seconds(start, "범위 시작")
    end_s = _as_fraction_seconds(end, "범위 끝")
    if end_s <= start_s:
        raise RuntimeError(f"범위가 비거나 뒤집혔습니다: [{start_s}, {end_s})")
    first = max(0, math.floor((start_s - clock.presentation_origin) / clock.frame_duration))
    after = min(
        clock.frame_count,
        math.ceil((end_s - clock.presentation_origin) / clock.frame_duration),
    )
    if first >= after:
        raise RuntimeError(
            f"요청 범위가 원본 프레임과 닿지 않습니다: [{start_s}, {end_s}) — "
            f"원본 [{clock.presentation_origin}, {clock.exact_end})"
        )
    return clock_frame_range(clock, first, after)


@dataclass(frozen=True, slots=True)
class AudioPacketExtent:
    """오디오 스트림의 패킷 실측 — 원점·끝·패킷 간격(틱).

    `packet_ticks`는 잰 최소 양수 간격이다(패킷 한 개의 길이). 오디오 정렬
    허용치의 근거가 되는 값이라 하드코딩된 밀리초를 쓰지 않는다.
    """

    stream_index: int
    time_base: Fraction
    first_pts: int | None
    last_pts: int | None
    packet_count: int
    packet_ticks: int


def probe_audio_extents(source: Path) -> tuple[AudioPacketExtent, ...]:
    """오디오 패킷 전체를 한 번 디먹스해 실측 원점·끝을 잰다.

    원본마다 한 번만 부르고 결과를 `extract_raw_clip`에 넘겨 재사용한다. 선언
    `start_time`과 달리 이 값은 패킷 증거다 — AAC 프라이밍의 음수 원점도 그대로
    나온다.
    """
    ffprobe = require_ffmpeg("ffprobe")
    completed = _run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "stream=index,codec_type,time_base:packet=stream_index,pts",
            "-of", "json",
            str(source),
        ]
    )  # fmt: skip
    data = _raw_probe_json(completed.stdout, source)
    streams = data.get("streams")
    if not isinstance(streams, list):
        raise RuntimeError(f"ffprobe JSON의 streams가 목록이 아닙니다: {source} (값 {streams!r})")
    audio_bases: dict[int, Fraction] = {}
    for entry in streams:
        if isinstance(entry, dict) and entry.get("codec_type") == "audio":
            index = _stream_global_index(entry, source)
            audio_bases[index] = _stream_positive_fraction(entry, "time_base", source)
    pts_by_stream = _audio_packet_pts(data.get("packets"), source)
    return tuple(
        _audio_extent(index, time_base, pts_by_stream.get(index, []))
        for index, time_base in sorted(audio_bases.items())
    )


def _audio_packet_pts(packets: object, source: Path) -> dict[int, list[int]]:
    if packets is None:
        return {}
    if not isinstance(packets, list):
        raise RuntimeError(f"ffprobe JSON의 packets가 목록이 아닙니다: {source} (값 {packets!r})")
    grouped: dict[int, list[int]] = {}
    for packet in packets:
        if not isinstance(packet, dict):
            raise RuntimeError(f"패킷 항목이 깨졌습니다: {source} (값 {packet!r})")
        index = packet.get("stream_index")
        value = packet.get("pts")
        if isinstance(index, bool) or not isinstance(index, int):
            raise RuntimeError(f"패킷 stream_index를 읽지 못했습니다: {source} (값 {index!r})")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"오디오 패킷 pts를 정수로 읽지 못했습니다: {source} (값 {value!r})")
        grouped.setdefault(index, []).append(value)
    return grouped


def _audio_extent(stream_index: int, time_base: Fraction, pts: Sequence[int]) -> AudioPacketExtent:
    if not pts:
        return AudioPacketExtent(
            stream_index=stream_index,
            time_base=time_base,
            first_pts=None,
            last_pts=None,
            packet_count=0,
            packet_ticks=0,
        )
    ordered = sorted(pts)
    gaps = [b - a for a, b in zip(ordered, ordered[1:], strict=False) if b - a > 0]
    return AudioPacketExtent(
        stream_index=stream_index,
        time_base=time_base,
        first_pts=ordered[0],
        last_pts=ordered[-1],
        packet_count=len(ordered),
        packet_ticks=min(gaps) if gaps else 0,
    )


# ─────────────────────────────────────────────────────────────
#  편집용 추출 — 프레임 정확 재인코딩 (§8)
# ─────────────────────────────────────────────────────────────

# 프로파일 설정은 얼어 있다(코디네이터 재결). 바꿀 때는 계약 문서와 시험 한도를
# 함께 고친다. crf 17/18 — 각 인코더 문서의 '시각적 무손실' 권장 등급(x265는
# 같은 등급이 +1이다). preset medium — 품질/속도의 고정 기준점.
_RAW_X264_ARGS = ("-preset", "medium", "-crf", "17")
_RAW_X265_ARGS = ("-preset", "medium", "-crf", "18", "-tag:v", "hvc1")
# prores_ks profile 3 = ProRes 422 HQ(4:2:2 10bit) — 편집 마스터용 상위 등급.
_RAW_PRORES_ARGS = ("-profile:v", "3")

_RAW_SOURCE_MATCHED_ENCODERS: Mapping[str, tuple[str, tuple[str, ...]]] = {
    "h264": ("libx264", _RAW_X264_ARGS),
    "hevc": ("libx265", _RAW_X265_ARGS),
}

_RAW_SUPPORTED_CODECS = frozenset(_RAW_SOURCE_MATCHED_ENCODERS)
_RAW_SUPPORTED_PIXFMTS = frozenset({"yuv420p", "yuv420p10le"})
# 선언된 HDR 전달 특성은 MVP 밖이다 — 실기 기기 검증 전에는 8bit/SDR로 낮추는
# 대신 이유를 말하고 막는다(§8.2, 코디네이터 재결).
_RAW_BLOCKED_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})


@dataclass(frozen=True, slots=True)
class RawTranscodeProfile:
    """추출 프로파일 — 이름·오디오 정책·컨테이너·픽셀 형식이 얼어 있는 값이다.

    `pix_fmt`이 None이면 원본 픽셀 형식을 그대로 쓴다(source_matched). 인코더
    선택은 프로파일 이름과 원본 코덱이 함께 정한다(`_raw_encoder`).
    """

    name: str
    audio_codec: str
    container: str
    pix_fmt: str | None
    encoder_args: tuple[str, ...]


RAW_SOURCE_MATCHED = RawTranscodeProfile(
    name="source_matched",
    audio_codec="copy",
    container="mp4",
    pix_fmt=None,
    encoder_args=(),
)
"""기본 프로파일 — 원본 크기·프레임 타이밍·비트 깊이·색을 지키는 고품질 재인코딩.

재압축이 일어난다는 사실이 계약의 일부다(§8.1)."""

RAW_EDIT_PRORES = RawTranscodeProfile(
    name="edit_prores",
    audio_codec="pcm_s16le",
    container="mov",
    pix_fmt="yuv422p10le",
    encoder_args=_RAW_PRORES_ARGS,
)
"""선택 프로파일 — MOV + ProRes 422 HQ + PCM. 4:2:0 → 4:2:2 색차 업샘플링은
프로파일의 본질이므로 결과에 경고로 기록한다."""


@dataclass(frozen=True, slots=True)
class RawClipHandles:
    """앞뒤 편집 여유분(초). 기본 0 — 본 구간끼리는 겹침도 틈도 없다(§6.5)."""

    before: Fraction = Fraction(0)
    after: Fraction = Fraction(0)


def _assert_raw_supported(probe: RawMediaProbe, clock: RawVideoClock) -> None:
    """지원 매트릭스 — 밖이면 이유를 말하고 죽인다. 정밀도를 낮추지 않는다."""
    video = probe.video
    failures: list[str] = []
    if video.codec_name not in _RAW_SUPPORTED_CODECS:
        failures.append(f"코덱 {video.codec_name!r} (지원: h264/hevc)")
    if video.pix_fmt not in _RAW_SUPPORTED_PIXFMTS:
        failures.append(
            f"픽셀 형식 {video.pix_fmt!r} bit depth {video.bit_depth} (지원: 4:2:0 8/10bit)"
        )
    if video.color_transfer in _RAW_BLOCKED_TRANSFERS:
        failures.append(f"HDR 전달 특성 {video.color_transfer!r} — 실기 검증 전 지원 밖")
    if any(kind == "video" for _, kind in probe.other_tracks):
        failures.append("비디오 스트림이 둘 이상이다")
    declared_end = probe.format_start + probe.duration
    if clock.exact_end > declared_end + probe.video.time_base:
        # 실측: ffmpeg의 입력 -ss(절대 좌표)는 선언 창(start_time+duration)까지만
        # 읽는다(1.25초 이동 pts를 만든 파일에서 90프레임 중 33프레임만 들어옴).
        # 선언 창보다 먼 패킷이 있으면 그 구간이 조용히 사라지므로 추출 전에 막는다.
        # 창은 절대 좌표로 재었다 — 시작 2초·길이 4.8초·패킷 끝 6.8초의 정상 파일을
        # 길이(구간)와 절대 끝을 바꿔 비교해 거짓으로 막아선 안 된다(A3 실측).
        failures.append(
            f"패킷 증거({clock.exact_end}초)가 선언 창("
            f"{probe.format_start}~{declared_end}초)을 넘는다 — 입력 탐색 창이 그 구간을 잃는다"
        )
    if not clock.uniform_grid:
        failures.append(f"VFR(균일 격자 아님): {clock.grid_note}")
    if failures:
        raise RawMediaUnsupported(
            f"지원하지 않는 원본입니다: {probe.source} — " + "; ".join(failures)
        )


@dataclass(frozen=True, slots=True)
class _RawMediaPlan:
    """추출 한 번의 계획 — 매핑·지역 시각·출력 길이가 여기서 정해진다."""

    media_range: RawPtsRange
    media_first_frame: int
    media_after_last_frame: int
    core_first_frame: int
    core_after_last_frame: int
    core_local_start: Fraction
    core_local_end: Fraction
    out_duration: Fraction
    final_clip: bool
    mapped_audio: tuple[AudioPacketExtent, ...]
    unmapped: tuple[tuple[int, str], ...]
    warnings: tuple[str, ...]


def _validate_raw_range(source_range: RawPtsRange) -> None:
    if source_range.time_base <= 0:
        raise RuntimeError(f"time_base가 양수가 아닙니다: {source_range.time_base}")
    for label, value in (("start_pts", source_range.start_pts), ("end_pts", source_range.end_pts)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError(f"{label}는 정수여야 합니다: {value!r}")
    if source_range.end_pts <= source_range.start_pts:
        raise RuntimeError(
            f"범위가 비거나 뒤집혔습니다: [{source_range.start_pts}, {source_range.end_pts})"
        )


def _plan_raw_media(
    clock: RawVideoClock,
    source_range: RawPtsRange,
    handles: RawClipHandles,
    audio_extents: Sequence[AudioPacketExtent],
) -> _RawMediaPlan:
    core_start = source_range.start_seconds
    core_end = source_range.end_seconds
    origin = clock.presentation_origin
    before = _as_fraction_seconds(handles.before, "앞 핸들")
    after = _as_fraction_seconds(handles.after, "뒤 핸들")
    if before < 0 or after < 0:
        raise RuntimeError(f"핸들은 0 이상이어야 합니다: 앞 {before}, 뒤 {after}")
    core_first = math.floor((core_start - origin) / clock.frame_duration)
    core_after = math.ceil((core_end - origin) / clock.frame_duration)
    if core_first < 0 or core_after > clock.frame_count:
        raise RuntimeError(
            f"core 범위가 원본을 벗어났습니다: [{core_start}, {core_end})초 — "
            f"원본 [{origin}, {clock.exact_end})초. 몰래 줄이지 않는다"
        )
    media_first = max(0, math.floor((core_start - before - origin) / clock.frame_duration))
    media_after = min(
        clock.frame_count,
        math.ceil((core_end + after - origin) / clock.frame_duration),
    )
    if media_first >= media_after:
        raise RuntimeError("핸들을 포함한 매체 범위가 비었습니다")
    media_range = clock_frame_range(clock, media_first, media_after)
    media_start = media_range.start_seconds
    media_end = media_range.end_seconds
    # 오디오 꼬리 보존은 원본의 마지막 영상 프레임을 담은 클립에서만 한다(A3P-F2).
    # 내부 클립이 남은 원본 오디오를 통째로 싣으면 인접 클립끼리 같은 소리를 반복해
    # 순서 결합의 구간 계약이 깨진다(실측: 1초 클립에 7초 오디오).
    final_clip = media_after >= clock.frame_count
    mapping = _map_raw_audio(audio_extents, media_start, media_end, preserve_tail=final_clip)
    out_duration = max(media_end - media_start, mapping.audio_tail_local)
    warnings = list(mapping.warnings)
    if out_duration > media_end - media_start:
        warnings.append(
            f"영상 끝 뒤 오디오 꼬리 {out_duration - (media_end - media_start)}초를 보존한다"
        )
    elif not final_clip:
        warnings.append(
            "내부 클립이다 — 오디오는 매체 끝에서 잘린다(경계에 걸친 마지막 패킷 하나는 "
            "코덱 패킷 반올림으로 통째로 남는다)"
        )
    return _RawMediaPlan(
        media_range=media_range,
        media_first_frame=media_first,
        media_after_last_frame=media_after,
        core_first_frame=core_first,
        core_after_last_frame=core_after,
        core_local_start=core_start - media_start,
        core_local_end=core_end - media_start,
        out_duration=out_duration,
        final_clip=final_clip,
        mapped_audio=mapping.mapped,
        unmapped=mapping.unmapped,
        warnings=tuple(warnings),
    )


@dataclass(frozen=True, slots=True)
class _RawAudioMapping:
    mapped: tuple[AudioPacketExtent, ...]
    unmapped: tuple[tuple[int, str], ...]
    warnings: tuple[str, ...]
    audio_tail_local: Fraction


def _map_raw_audio(
    audio_extents: Sequence[AudioPacketExtent],
    media_start: Fraction,
    media_end: Fraction,
    *,
    preserve_tail: bool,
) -> _RawAudioMapping:
    """구간 안에 패킷이 있는 오디오 트랙만 매핑한다.

    패킷이 하나도 없는 트랙은 빈 스트림이 되어 컨테이너에서 사라진다 — 조용히
    버리는 대신 unmapped로 남긴다. 영상 끝을 넘는 **원본 꼬리**는 마지막 클립
    (`preserve_tail`)에서만 보존한다(코디네이터 재결) — 내부 클립은 매체 끝
    경계에서 잘린다. ffmpeg `-t`는 패킷 눈금으로 자르므로 경계에 걸친 마지막
    패킷 하나는 통째로 남는다(코덱 패킷 반올림, 검증이 한 패킷+한 틱만 허용).
    """
    mapped: list[AudioPacketExtent] = []
    unmapped: list[tuple[int, str]] = []
    warnings: list[str] = []
    tail = Fraction(0)
    for extent in audio_extents:
        if extent.first_pts is None or extent.last_pts is None:
            unmapped.append((extent.stream_index, "패킷이 없는 오디오 스트림"))
            continue
        first = extent.first_pts * extent.time_base
        last = extent.last_pts * extent.time_base
        if first >= media_end or last <= media_start:
            unmapped.append((extent.stream_index, "이 구간에 오디오 패킷이 없다"))
            continue
        mapped.append(extent)
        if preserve_tail:
            packet = extent.packet_ticks * extent.time_base
            tail = max(tail, last + packet - media_start)
    if unmapped:
        listed = ", ".join(f"스트림 {index}: {reason}" for index, reason in unmapped)
        warnings.append(f"매핑하지 않은 오디오: {listed}")
    return _RawAudioMapping(
        mapped=tuple(mapped),
        unmapped=tuple(unmapped),
        warnings=tuple(warnings),
        audio_tail_local=tail,
    )


def _format_us(seconds: Fraction, *, ceil: bool) -> str:
    """정확한 유리수 초를 ffmpeg `-ss`/`-t` 의 µs 문자열로.

    `-ss`는 바닥(목표보다 늦게 자르지 않는다), `-t`는 천장(끝 프레임을 잃지
    않는다). 실제 결과는 추출물 검증이 잰다 — 이 문자열은 요청일 뿐이다.
    """
    us = seconds * _RAW_US_PER_SECOND
    ticks = math.ceil(us) if ceil else math.floor(us)
    sign = "-" if ticks < 0 else ""
    ticks = abs(ticks)
    return f"{sign}{ticks // _RAW_US_PER_SECOND}.{ticks % _RAW_US_PER_SECOND:06d}"


def _raw_encoder(profile: RawTranscodeProfile, codec_name: str) -> tuple[str, tuple[str, ...]]:
    if profile.name == "source_matched":
        try:
            return _RAW_SOURCE_MATCHED_ENCODERS[codec_name]
        except KeyError:
            raise RawMediaUnsupported(
                f"source_matched 프로파일은 {codec_name!r}를 재인코딩하지 않는다 "
                f"(지원: {', '.join(sorted(_RAW_SUPPORTED_CODECS))})"
            ) from None
    if profile.name == "edit_prores":
        return "prores_ks", profile.encoder_args
    raise RuntimeError(f"알 수 없는 프로파일입니다: {profile.name!r}")


def _raw_color_args(video: RawVideoMedia) -> list[str]:
    """선언된 색 좌표만 넘긴다 — 모르는 값은 지어내지 않고 그대로 둔다."""
    args: list[str] = []
    if video.color_primaries:
        args += ["-color_primaries", video.color_primaries]
    if video.color_transfer:
        args += ["-color_trc", video.color_transfer]
    if video.color_matrix:
        args += ["-colorspace", video.color_matrix]
    if video.color_range:
        args += ["-color_range", video.color_range]
    return args


def _assert_safe_staging(source: Path, staging_output: Path) -> None:
    """스테이징 경로가 원본을 가리키는지를 ffmpeg보다 먼저 잡는다(A3P-F2).

    같은 경로·심볼릭 링크 별칭(상위 디렉터리 심볼릭 링크 포함 — resolve로 푼다)·
    하드링크 별칭(samefile)은 무조건 거부한다. 이미 있는 출력물도 `-y`로 덮어쓰는
    대신 거부한다 — 게시는 os.link(있으면 실패)로 원자적으로 하므로 ffmpeg이
    호출자 경로를 직접 쓸 일이 아예 없다. 원본이 없는 호출은 여기서 보지 않고
    조사(ffprobe)가 크게 죽게 둔다.
    """
    if not os.path.exists(source):
        return
    if staging_output.resolve() == source.resolve():
        raise RawStagingError(
            f"스테이징 경로가 원본을 가리킨다(같은 파일: {staging_output} → "
            f"{source}) — 원본은 절대 덮어쓰지 않는다"
        )
    if os.path.lexists(staging_output):
        if os.path.exists(staging_output) and os.path.samefile(source, staging_output):
            raise RawStagingError(
                f"스테이징 경로가 원본의 별칭이다(하드링크: {staging_output}) — "
                "원본은 절대 덮어쓰지 않는다"
            )
        raise RawStagingError(
            f"스테이징 경로에 이미 무언가 있다(덮어쓰지 않는다): {staging_output}"
        )


def _reserve_staging_temp(staging_output: Path) -> Path:
    """호출자 경로 옆에 O_EXCL로만 만드는 전용 임시 파일 — 자리를 원자적으로 맡는다.

    ffmpeg 출력과 검증은 전부 이 임시 파일에서 일어난다. `-y`가 못 박는 대상은
    이 파일뿐인데, 이름을 우리가 O_EXCL로 exclusively 만들었으므로 이웃이나 원본을
    덮어칠 경로가 될 수 없다(원본 이름과 전혀 다른 접두·pid·무작위 토큰).
    """
    for _ in range(64):
        candidate = staging_output.parent / (
            f".{staging_output.name}.{os.getpid()}.{secrets.token_hex(4)}.part"
        )
        try:
            descriptor = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return candidate
    raise RawStagingError(f"스테이징 임시 자리를 못 잡는다: {staging_output.parent}")


def _publish_staged_clip(temp: Path, staging_output: Path) -> None:
    """검증을 통과한 임시 파일을 호출자 경로로 원자 게시 — 이미 있으면 절대 덮어치지 않는다.

    link(2)는 마지막 이름 요소를 따라가지 않으므로, 게시 순간 경로가 심볼릭
    링크로 바뀌어 있어도 EEXIST로 막힌다(경합에서도 원본을 못 건드린다).
    """
    try:
        os.link(temp, staging_output)
    except FileExistsError:
        temp.unlink(missing_ok=True)
        raise RawStagingError(
            f"게시 경로가 이미 있다(경합 — 아무것도 덮어쓰지 않았다): {staging_output}"
        ) from None
    except OSError as exc:
        temp.unlink(missing_ok=True)
        raise RawStagingError(
            f"게시에 실패했다(아무것도 덮어쓰지 않았다): {staging_output} ({exc})"
        ) from exc
    temp.unlink(missing_ok=True)


def _raw_extract_command(
    source: Path,
    staging_output: Path,
    probe: RawMediaProbe,
    profile: RawTranscodeProfile,
    plan: _RawMediaPlan,
) -> list[str]:
    encoder, encoder_args = _raw_encoder(profile, probe.video.codec_name)
    # -ss를 time_base 한 틱 일찍 잡는다: 양자화된 시계(예: 1/1000)에서 격자 시각과
    # 실제 pts가 한 틱 어길 수 있는데, 바닥 µs만 쓰면 첫 프레임이 잘릴 수 있다.
    # 한 틱 일찍는 이전 프레임을 되살리지 못한다(프레임 간격 ≫ 틱). 음수 -ss는
    # ffmpeg가 '지연'으로 읽으므로 0에서 자른다.
    seek = max(Fraction(0), plan.media_range.start_seconds - probe.video.time_base)
    # -y는 호출자 경로가 아니라 우리가 O_EXCL로 exclusively 맡은 임시 파일에만
    # 붙는다(A3P-F2). 임시 파일은 검증을 통과한 뒤 os.link로 원자 게시된다 —
    # 있으면 EEXIST라 경합에서도 아무것도 덮어쓰지 않는다.
    command = [
        require_ffmpeg(), "-hide_banner", "-nostdin", "-y",
        # -ss의 값은 절대 좌표(원본 시계의 제시 PTS)다. ffmpeg 입력 -ss의 기본
        # 해석은 선언 시작(format start_time)을 0으로 두고 더하는 것이라, 시작이
        # 0이 아닌 컨테이너에서는 그만큼 늦은 자리를 찾는다(실측: 시작 2초 원본의
        # 절대 2.399초 점이 4.399초로 읽혀 100프레임 자리에 60프레임). -seek_timestamp 1
        # 이 -ss를 파일의 절대 타임스탬프 축에 묶는다(A3 실측 재현).
        "-seek_timestamp", "1",
        "-ss", _format_us(seek, ceil=False),
        "-t", _format_us(plan.out_duration, ceil=True),
        "-i", str(source),
        "-map", f"0:{probe.video.stream_index}",
    ]  # fmt: skip
    for extent in plan.mapped_audio:
        command += ["-map", f"0:{extent.stream_index}"]
    pix_fmt = profile.pix_fmt if profile.pix_fmt is not None else probe.video.pix_fmt
    frames = plan.media_after_last_frame - plan.media_first_frame
    # 영상 프레임 창은 select로 못 박는다(§8.3 CFR). -frames:v을 쓰면 ffmpeg가 그
    # 스트림이 끝나는 순간 출력을 마무리해 영상 뒤 오디오 꼬리가 잘리는 것을
    # 실측했다(50프레임 + 꼬리 4.5초 → 오디오 2.0초에서 끊김). select는 필터라
    # 입력 읽기가 -t까지 계속된다. avoid_negative_ts도 건드리지 않는다 — make_zero가
    # 스트림마다 다른 보정을 만들어 공통 시프트(§8.3)를 깨뜨렸다(출력 elst 1초).
    command += [
        "-c:v", encoder, *encoder_args,
        "-pix_fmt", pix_fmt,
        "-vf", f"select=lt(n\\,{frames})",
        "-fps_mode", "passthrough",
        "-c:a", profile.audio_codec,
    ]  # fmt: skip
    # 색 좌표는 출력 경로 **앞**에 붙는다 — ffmpeg는 출력 URL 뒤의 인자를 무시한다.
    return command + _raw_color_args(probe.video) + ["-f", profile.container, str(staging_output)]


@dataclass(frozen=True, slots=True)
class RawClipExtraction:
    """검증을 통과한 스테이징 추출물 — 절대·지역 시각이 갈라져 기록된다.

    `source_range`·`media_range`·`core_first_frame`은 **원본 절대 좌표**다.
    `core_local_start`·`core_local_end`·`media_local_end`·`audio_local_starts`는
    **클립 지역 시각**이다(매체 시작 = 0, 공통 기준 변환 적용). 게시는 호출자의
    몫이다 — 이 값은 스테이징 파일의 기록일 뿐이다.
    """

    path: Path
    profile_name: str
    source_range: RawPtsRange
    media_range: RawPtsRange
    core_first_frame: int
    core_after_last_frame: int
    core_local_start: Fraction
    core_local_end: Fraction
    media_local_end: Fraction
    frame_count: int
    frame_duration: Fraction
    video: RawVideoMedia
    audio: tuple[RawAudioTrack, ...]
    audio_local_starts: tuple[tuple[int, Fraction | None], ...]
    rotation_applied: bool
    unmapped_tracks: tuple[tuple[int, str], ...]
    warnings: tuple[str, ...]
    ffmpeg_version: str


def extract_raw_clip(
    source: Path,
    staging_output: Path,
    source_range: RawPtsRange,
    profile: RawTranscodeProfile,
    *,
    handles: RawClipHandles | None = None,
    probe: RawMediaProbe | None = None,
    clock: RawVideoClock | None = None,
    audio_extents: tuple[AudioPacketExtent, ...] | None = None,
) -> RawClipExtraction:
    """원본 구간을 프레임 정확히 재인코딩해 **스테이징에만** 쓴다(§8.4).

    매체 시각은 공통 기준 변환으로 0부터 시작한다 — 영상·오디오를 따로 0으로
    만들지 않아 상대 싱크가 산다(§8.3). 강제 30fps·8bit 강등·LUT·정규화·속도
    변경은 없다. 검증(디먹스·격자·프레임 수·기하·색·오디오·디코드)을 통과해야
    값을 돌려주고, 실패하면 `RawClipVerificationError`로 죽는다 — 0바이트·깨진
    파일은 절대 성공으로 보이지 않는다.

    스테이징 안전(A3P-F2): 원본을 가리키는 경로(같은 경로·심볼릭 링크·상위
    심볼릭 링크·하드링크 별칭)와 이미 있는 출력은 ffmpeg보다 먼저 `RawStagingError`로
    거부한다. ffmpeg은 O_EXCL로 맡은 임시 파일에만 쓰고, 호출자 경로에는 검증
    통과 뒤 link(2) 한 번으로만 생긴다(있으면 실패 — 경합도 덮어쓰지 않는다).
    """
    _validate_raw_range(source_range)
    _assert_safe_staging(source, staging_output)
    media_probe = probe if probe is not None else probe_raw_media(source)
    video_clock = clock if clock is not None else probe_extraction_clock(source, probe=media_probe)
    extents = audio_extents if audio_extents is not None else probe_audio_extents(source)
    clip_handles = handles if handles is not None else RawClipHandles()
    _assert_raw_supported(media_probe, video_clock)
    plan = _plan_raw_media(video_clock, source_range, clip_handles, extents)
    staging_output.parent.mkdir(parents=True, exist_ok=True)
    staged = _reserve_staging_temp(staging_output)
    try:
        _run(_raw_extract_command(source, staged, media_probe, profile, plan))
        out_probe, out_clock = _verify_raw_output(staged, media_probe, video_clock, profile, plan)
    except BaseException:
        # 실패 잔여물은 임시 이름으로만 존재하다 사라진다 — 호출자 경로에는
        # 절대 얼굴을 내밀지 않는다(반쯤 된 파일이 '있는 출력'처럼 보이면 안 된다).
        staged.unlink(missing_ok=True)
        raise
    _publish_staged_clip(staged, staging_output)
    rotation_applied = media_probe.video.rotation_degrees != 0
    warnings = plan.warnings
    if rotation_applied:
        warnings = warnings + (
            f"회전 {media_probe.video.rotation_degrees}°를 물리적으로 적용했다"
            f"(저장 기하 {media_probe.video.width}x{media_probe.video.height})",
        )
    if profile.name == "edit_prores" and media_probe.video.pix_fmt.startswith("yuv420"):
        warnings = warnings + (
            "색차 4:2:0 → 4:2:2 업샘플링(edit_prores 프로파일의 성질, 되돌릴 수 없다)",
        )
    log.debug(
        f"원본 구간 추출 완료: {staging_output} "
        f"({plan.media_after_last_frame - plan.media_first_frame}프레임, "
        f"core 지역 [{plan.core_local_start}, {plan.core_local_end})초)"
    )
    return _raw_clip_result(
        staging_output,
        profile,
        source_range,
        plan,
        out_probe,
        out_clock,
        rotation_applied,
        warnings,
    )


def _raw_clip_result(
    staging_output: Path,
    profile: RawTranscodeProfile,
    source_range: RawPtsRange,
    plan: _RawMediaPlan,
    out_probe: RawMediaProbe,
    out_clock: RawVideoClock,
    rotation_applied: bool,
    warnings: tuple[str, ...],
) -> RawClipExtraction:
    return RawClipExtraction(
        path=staging_output,
        profile_name=profile.name,
        source_range=source_range,
        media_range=plan.media_range,
        core_first_frame=plan.core_first_frame,
        core_after_last_frame=plan.core_after_last_frame,
        core_local_start=plan.core_local_start,
        core_local_end=plan.core_local_end,
        media_local_end=out_probe.duration,
        frame_count=out_clock.frame_count,
        frame_duration=out_clock.frame_duration,
        video=out_probe.video,
        audio=out_probe.audio,
        audio_local_starts=tuple(
            (track.stream_index, track.start_time) for track in out_probe.audio
        ),
        rotation_applied=rotation_applied,
        unmapped_tracks=plan.unmapped,
        warnings=warnings,
        ffmpeg_version=_tool_version("ffmpeg"),
    )


def _verify_raw_output(
    staging_output: Path,
    source_probe: RawMediaProbe,
    source_clock: RawVideoClock,
    profile: RawTranscodeProfile,
    plan: _RawMediaPlan,
) -> tuple[RawMediaProbe, RawVideoClock]:
    """추출물을 실측으로 검증한다 — 통과 못 하면 이 파일은 게시할 수 없다.

    구조 검사(§8.4)다: 디먹스·격자·프레임 수·기하·픽셀 형식·색·오디오 트랙·공통
    시프트 보존·전체 디코드. 경계 발화·싱크·색의 실기 검사는 별도이며 여기서
    통과했다는 뜻이 아니다.
    """
    if not staging_output.is_file() or staging_output.stat().st_size == 0:
        raise RawClipVerificationError(f"출력이 비었거나 없습니다: {staging_output}")
    out_probe = probe_raw_media(staging_output)
    out_clock = probe_extraction_clock(staging_output, probe=out_probe)
    failures: list[str] = []
    failures += _verify_raw_video(out_probe, out_clock, source_probe, source_clock, profile, plan)
    failures += _verify_raw_audio(out_probe, probe_audio_extents(staging_output), plan)
    failures += _verify_raw_duration(out_probe, plan)
    if failures:
        raise RawClipVerificationError(
            f"추출물 검증 실패 (게시 불가): {staging_output}\n" + "\n".join(failures)
        )
    _verify_raw_decodes(staging_output)
    return out_probe, out_clock


def _verify_raw_video(
    out_probe: RawMediaProbe,
    out_clock: RawVideoClock,
    source_probe: RawMediaProbe,
    source_clock: RawVideoClock,
    profile: RawTranscodeProfile,
    plan: _RawMediaPlan,
) -> list[str]:
    video = out_probe.video
    source = source_probe.video
    failures: list[str] = []
    expected_frames = plan.media_after_last_frame - plan.media_first_frame
    if out_clock.frame_count != expected_frames:
        failures.append(
            f"프레임 수가 다릅니다: 기대 {expected_frames}, 출력 {out_clock.frame_count}"
        )
    if out_clock.frame_duration != source_clock.frame_duration:
        failures.append(
            f"프레임 타이밍이 바뀌었습니다: 원본 {source_clock.frame_duration}, "
            f"출력 {out_clock.frame_duration} — 무단 fps 변환은 없다"
        )
    if not out_clock.uniform_grid:
        failures.append(f"출력 격자가 균일하지 않습니다: {out_clock.grid_note}")
    if abs(out_clock.presentation_origin) > out_clock.time_base:
        failures.append(
            f"출력 원점이 0이 아닙니다: {out_clock.presentation_origin}초 "
            f"(허용 {out_clock.time_base}초)"
        )
    expected_width, expected_height = _expected_geometry(source)
    if (video.width, video.height) != (expected_width, expected_height):
        failures.append(
            f"해상도가 다릅니다: 기대 {expected_width}x{expected_height}, "
            f"출력 {video.width}x{video.height}"
        )
    # source_matched는 원본 픽셀 형식이 곧 기대값이다 — 프로파일에 끼워넣은 값을
    # 따라가면 10bit 강등이 검증을 통과한다(실측: 변형 프로파일로 재현).
    if profile.name == "source_matched":
        expected_pix_fmt = source.pix_fmt
    else:
        expected_pix_fmt = profile.pix_fmt if profile.pix_fmt is not None else source.pix_fmt
    if video.pix_fmt != expected_pix_fmt:
        failures.append(
            f"픽셀 형식이 다릅니다: 기대 {expected_pix_fmt}, 출력 {video.pix_fmt} "
            f"(bit depth {source.bit_depth} → {video.bit_depth}) — 무단 비트 강등은 없다"
        )
    failures += _verify_raw_colors(video, source)
    if video.rotation_degrees != 0:
        failures.append(f"출력에 회전 메타데이터가 남았습니다: {video.rotation_degrees}°")
    return failures


def _expected_geometry(source: RawVideoMedia) -> tuple[int, int]:
    """기대 출력 기하 — 회전 90/270°는 autorotate가 물리로 적용해 뒤바뀐다."""
    if source.rotation_degrees % 180 != 0:
        return source.height, source.width
    return source.width, source.height


def _verify_raw_colors(video: RawVideoMedia, source: RawVideoMedia) -> list[str]:
    failures: list[str] = []
    for label, declared, actual in (
        ("color_primaries", source.color_primaries, video.color_primaries),
        ("color_transfer", source.color_transfer, video.color_transfer),
        ("color_matrix", source.color_matrix, video.color_matrix),
        ("color_range", source.color_range, video.color_range),
    ):
        if declared and declared != actual:
            failures.append(
                f"선언된 색 좌표가 사라졌습니다: {label} 원본 {declared!r}, 출력 {actual!r}"
            )
    return failures


def _verify_raw_audio(
    out_probe: RawMediaProbe,
    out_extents: tuple[AudioPacketExtent, ...],
    plan: _RawMediaPlan,
) -> list[str]:
    failures: list[str] = []
    if len(out_probe.audio) != len(plan.mapped_audio):
        failures.append(
            f"오디오 트랙 수가 다릅니다: 기대 {len(plan.mapped_audio)}, 출력 {len(out_probe.audio)}"
        )
        return failures
    if len(out_extents) != len(out_probe.audio):
        failures.append(
            f"출력 오디오 패킷 증거 수가 다릅니다: 트랙 {len(out_probe.audio)}, "
            f"증거 {len(out_extents)}"
        )
        return failures
    for extent, track, out_extent in zip(
        plan.mapped_audio, out_probe.audio, out_extents, strict=True
    ):
        source = extent.first_pts * extent.time_base if extent.first_pts is not None else None
        if source is None:
            failures.append(f"오디오 스트림 {extent.stream_index}: 매핑했는데 패킷 증거가 없다")
            continue
        expected = max(Fraction(0), source - plan.media_range.start_seconds)
        tolerance = extent.packet_ticks * extent.time_base + extent.time_base
        measured = track.start_time
        if abs(measured - expected) > tolerance:
            failures.append(
                f"오디오 스트림 {track.stream_index}의 시작이 어긋납니다: "
                f"기대(지역) {expected}초, 출력 {measured}초, 허용 {tolerance}초 "
                "— 공통 기준 변환이 깨졌다"
            )
        if out_extent.last_pts is None or out_extent.packet_ticks == 0:
            failures.append(f"오디오 스트림 {track.stream_index}: 출력에 패킷 증거가 없다")
            continue
        # 구간 계약(A3P-F2 둘째): 매핑된 오디오는 계획된 출력 길이를 코덱 패킷
        # 반올림(경계에 걸친 한 패킷)과 한 틱까지만 넘을 수 있다. 내부 클립이
        # 원본의 남은 오디오를 통째로 싣는 것(실측: 1초 클립에 7초)을 여기서 잡는다.
        out_packet = out_extent.packet_ticks * out_extent.time_base
        source_packet = extent.packet_ticks * extent.time_base
        audio_end = out_extent.last_pts * out_extent.time_base + out_packet
        bound = plan.out_duration + max(out_packet, source_packet) + extent.time_base
        if audio_end > bound:
            failures.append(
                f"오디오 스트림 {track.stream_index}가 매체 끝을 넘는다: "
                f"끝 {audio_end}초, 계획 길이 {plan.out_duration}초, "
                f"허용(패킷 반올림) {max(out_packet, source_packet) + extent.time_base}초 "
                "— 내부 클립은 경계에서 잘린다"
            )
    return failures


def _verify_raw_duration(out_probe: RawMediaProbe, plan: _RawMediaPlan) -> list[str]:
    failures: list[str] = []
    # 컨테이너 길이 선언은 스트림 time_base 눈금으로 반올림된다 — 오디오 패킷 하나와
    # 출력 영상 틱 하나 중 큰 쪽이 '선언 반올림'의 전부다.
    allowed_short = out_probe.video.time_base
    for extent in plan.mapped_audio:
        allowed_short = max(allowed_short, extent.packet_ticks * extent.time_base)
    if out_probe.duration + allowed_short < plan.out_duration:
        failures.append(
            f"출력 길이가 모자랍니다: 기대 {plan.out_duration}초, 출력 {out_probe.duration}초 "
            f"(선언 반올림 {allowed_short}초 허용) — 꼬리가 잘렸다"
        )
    return failures


def _verify_raw_decodes(path: Path) -> None:
    """출력 전체를 디코드해 본다 — 패킷 선언만으로 디코드 품질을 주장하지 않는다.

    `-xerror`는 첫 오류에서 0이 아닌 종료 코드를 내므로, 0이 아닌 종료·오류
    줄 둘 다 실패다. 실패한 파일은 게시할 수 없다.
    """
    ffmpeg = require_ffmpeg()
    completed = run_process(
        [ffmpeg, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0 or completed.stderr.strip():
        tail = "\n".join(completed.stderr.strip().splitlines()[-5:])
        raise RawClipVerificationError(
            f"출력 디코드 검증 실패: {path} (코드 {completed.returncode})\n{tail}"
        )


def ffmpeg_provenance() -> str:
    """ffmpeg/ffprobe 버전과 필요 인코더의 실측 가용성 — 보고·진단용."""
    ffmpeg = require_ffmpeg()
    encoders = _run([ffmpeg, "-hide_banner", "-encoders"]).stdout
    required = ("libx264", "libx265", "prores_ks", "aac")
    available = [name for name in required if re.search(rf"\s{name}\s", encoders)]
    missing = [name for name in required if name not in available]
    lines = [
        _tool_version("ffmpeg"),
        _tool_version("ffprobe"),
        f"encoders: {', '.join(available) or '없음'}",
    ]
    if missing:
        lines.append(f"MISSING encoders: {', '.join(missing)} — 해당 프로파일 사용 불가")
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────
#  자막 검토 미리보기 — 선택 오디오 한 트랙의 브라우저 재생(F2)
# ─────────────────────────────────────────────────────────────
#
# 브라우저(Chromium)는 컨테이너의 오디오 트랙을 고르지 못한다(audioTracks=null).
# 그래서 자막 검토 화면이 **선택한 트랙만** 담은 MP4 미리보기를 만들어 듣는다 —
# 원본(검증 사본)은 절대 손대지 않는다. ffmpeg 호출은 여기(규칙 7)에만 산다.

_CAPTION_PREVIEW_VIDEO_COPY_PIXFMTS = frozenset({"yuv420p", "yuvj420p"})
"""코덱 복사가 허용되는 픽셀 형식 — 8bit 4:2:0만 브라우저가 안정적으로 재생한다."""

# 미리보기 인코딩 설정은 얼어 있다(코디네이터 재결 — 위 추출 프로파일과 같은 규율).
# 미리보기는 납품물이 아니라 검토 보조라 crf 20을 쓴다(납품 추출 프로파일의 17과
# 다르다). 음성은 AAC 192k — 검토 청취용 상한이지 원본 오디오 정책이 아니다.
_CAPTION_PREVIEW_X264_ARGS = ("-preset", "medium", "-crf", "20")
_CAPTION_PREVIEW_AUDIO_BITRATE = "192k"
_CAPTION_PREVIEW_ORIGIN_PROBE_SECONDS = "0.2"
"""원점 측정에 디코드하는 앞머리 길이 — 첫 프레임의 pts만 필요하다."""


class CaptionPreviewUnsupported(RuntimeError):
    """자막 미리보기를 만들 수 없는 입력 — 이유를 말하고 죽는다(빈 성공 금지)."""


@dataclass(frozen=True, slots=True)
class CaptionPreviewExtraction:
    """선택 오디오 미리보기의 실측 — 원점은 자막 시계와 같은 잣대로 잰 값이다.

    `audio_origin`은 미리보기 파일의 선택 오디오를 16 kHz s16le 파이프라인으로
    디코드했을 때 첫 프레임의 파일 시각(`extract_pcm_s16le`과 같은 측정)이다.
    정규화 시각 `t`(등록 원점 상대)의 재생 위치는 `audio_origin + t`고, 최종
    영상 시계의 큐 시각에서는 등록 원점과의 **차이만** 보정하면 된다 — 같은
    측정 방식이라 보정 항은 정확하다.
    """

    path: Path
    remuxed: bool
    video_codec: str
    audio_codec: str
    audio_stream_count: int
    audio_origin: Fraction
    duration: Fraction
    source_video_codec: str
    source_audio_codec: str
    ffmpeg_version: str
    warnings: tuple[str, ...]


def _caption_preview_track(probe: RawMediaProbe, audio_stream_index: int) -> RawAudioTrack:
    """전역 스트림 번호로 오디오 트랙을 짚는다 — 없는 번호는 죽는다."""
    for track in probe.audio:
        if track.stream_index == audio_stream_index:
            return track
    raise CaptionPreviewUnsupported(
        f"오디오 스트림 {audio_stream_index}이(가) 이 파일에 없다: {probe.source}"
        f" (오디오 트랙 {len(probe.audio)}개)"
    )


def _caption_preview_codec_args(
    probe: RawMediaProbe, track: RawAudioTrack
) -> tuple[list[str], tuple[str, ...], bool]:
    """브라우저 재생 가능한 MP4를 위한 인코딩 인자 — 코덱 복사가 가능하면 복사한다.

    복사 판정은 **실제 브라우저가 받아들이는 조합**만 허용한다: 영상 h264 + 8bit
    4:2:0, 소리 aac. 하나라도 어긋나면 그 다리만 재인코딩한다(ProRes·10bit·PCM은
    여기서 H264/AAC로 내려간다 — 원본은 그대로다).
    """
    video = probe.video
    video_copy = video.codec_name == "h264" and video.pix_fmt in _CAPTION_PREVIEW_VIDEO_COPY_PIXFMTS
    audio_copy = track.codec_name == "aac"
    warnings: list[str] = []
    args: list[str] = []
    if video_copy:
        args += ["-c:v", "copy"]
    else:
        args += ["-c:v", "libx264", *_CAPTION_PREVIEW_X264_ARGS, "-pix_fmt", "yuv420p"]
        warnings.append(
            f"영상 {video.codec_name}/{video.pix_fmt}을(를) H264 8bit로 변환했다 (원본은 그대로다)"
        )
    if audio_copy:
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", _CAPTION_PREVIEW_AUDIO_BITRATE]
        warnings.append(f"소리 {track.codec_name}을(를) AAC로 변환했다 (원본은 그대로다)")
    return args, tuple(warnings), video_copy and audio_copy


def _caption_preview_origin(preview: Path, channels: int) -> Fraction:
    """미리보기 선택 오디오의 디코드 원점 — 등록 원점과 같은 16 kHz 파이프라인.

    `extract_pcm_s16le`과 같은 필터열로 앞머리만 디코드해 ashowinfo의 첫 프레임
    pts를 원점으로 쓴다. 싱크는 null이다 — pts 기록에 출력 바이트는 필요 없다.
    """
    ffmpeg = require_ffmpeg()
    rate = _PCM_SAMPLE_RATE
    filters = f"aresample={rate},aformat=sample_fmts=s16,asettb=1/{rate},ashowinfo"
    completed = _run(
        [
            ffmpeg, "-hide_banner", "-loglevel", "info", "-nostats", "-nostdin", "-y",
            "-copyts",
            "-i", str(preview),
            "-map", "0:a:0",
            "-vn",
            "-t", _CAPTION_PREVIEW_ORIGIN_PROBE_SECONDS,
            "-af", filters,
            "-f", "null", "-",
        ]
    )  # fmt: skip
    frames = _converted_frames(completed.stderr, preview)
    return _sample0_file_time(frames, rate, channels, preview)


def extract_caption_preview(
    source: Path, staging_output: Path, *, audio_stream_index: int
) -> CaptionPreviewExtraction:
    """선택 오디오 한 트랙만 담은 브라우저 재생 MP4를 **스테이징에** 만든다(F2).

    매핑은 영상 첫 스트림과 지정한 오디오 스트림 하나뿐이다 — 다른 오디오 트랙은
    미리보기에 못 들어가므로 검토자는 자막이 묶인 **그 트랙**을 듣는다. `-ss`를
    쓰지 않아 영상·소리의 상대 싱크가 그대로 흐른다. 스테이징 안전장치(원본 별칭
    거부·O_EXCL 임시 파일·link 게시)는 `extract_raw_clip`과 같은 것을 쓴다.
    """
    _validate_ordinal(audio_stream_index)
    _assert_safe_staging(source, staging_output)
    probe = probe_raw_media(source)
    track = _caption_preview_track(probe, audio_stream_index)
    codec_args, warnings, remuxed = _caption_preview_codec_args(probe, track)
    staging_output.parent.mkdir(parents=True, exist_ok=True)
    staged = _reserve_staging_temp(staging_output)
    try:
        _run(
            [
                require_ffmpeg(), "-hide_banner", "-nostdin", "-y",
                "-i", str(source),
                "-map", f"0:{probe.video.stream_index}",
                "-map", f"0:{track.stream_index}",
                "-dn", "-sn",
                *codec_args,
                "-movflags", "+faststart",
                "-f", "mp4",
                str(staged),
            ]
        )  # fmt: skip
        origin = _caption_preview_origin(staged, track.channels)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    out_probe = probe_raw_media(staged)
    if len(out_probe.audio) != 1:
        staged.unlink(missing_ok=True)
        raise CaptionPreviewUnsupported(
            f"미리보기에 오디오 트랙이 {len(out_probe.audio)}개다 — 선택 트랙 하나만 담겨야 한다"
        )
    _publish_staged_clip(staged, staging_output)
    return CaptionPreviewExtraction(
        path=staging_output,
        remuxed=remuxed,
        video_codec=out_probe.video.codec_name,
        audio_codec=out_probe.audio[0].codec_name,
        audio_stream_count=len(out_probe.audio),
        audio_origin=origin,
        duration=out_probe.duration,
        source_video_codec=probe.video.codec_name,
        source_audio_codec=track.codec_name,
        ffmpeg_version=_tool_version("ffmpeg"),
        warnings=warnings,
    )


# ─────────────────────────────────────────────────────────────
#  영상 우선 검토 미리보기 — 원본 전체의 브라우저 재생 MP4 (V1)
# ─────────────────────────────────────────────────────────────
#
# 자막 미리보기(F2)와 같은 인코딩 규율이지만 입력이 다르다: 검토 화면은 **원본
# 전체**를 탐색 가능하게 띄운다(§4.3 — 조각 미리보기가 아니라 원본 직접 제공의
# 제한된 경로). `-ss`/`-t`를 쓰지 않으므로 원본 시각과 미리보기 시각의 대응은
# 원점 이동 하나뿐이다. 침묵(오디오 트랙 없음)도, 여러 오디오 트랙 중 **선택한
# 하나**만 담은 것도 다 지원한다 — 어느 쪽이든 영상·소리 이외의 것은 못 들어간다.


class VisualPreviewUnsupported(RuntimeError):
    """영상 검토 미리보기를 만들 수 없는 입력 — 이유를 말하고 죽는다(빈 성공 금지)."""


@dataclass(frozen=True, slots=True)
class VisualPreviewExtraction:
    """원본 전체 미리보기의 실측 — 화면 기하·회전·시각 원점까지 남긴다.

    `rotation_degrees`는 **출력** 파일의 회전 선언이다. 코덱 복사는 원본의 display
    matrix를 그대로 옮기고, 재인코딩은 ffmpeg이 회전을 물리적으로 구운다 — 어느
    쪽이든 재생 화면의 가로세로비·방향은 원본과 같다(§8.2). `video_origin`은
    **출력 영상의 잰 제시 원점**(첫 패킷 PTS × time_base)이다 — `-ss`를 쓰지
    않아도 ffmpeg이 입력의 선언 시작(format start_time)만큼 전체를 당기므로(-copyts
    아님) 이 값이 원본 국소 시각에서 브라우저 시각으로 가는 이동이고, 원점이 0이라고
    가정하지 않고 잰 그대로 돌려준다(코디네이터 계약). `audio_origin`은 출력
    오디오의 제시 원점 선언(트랙이 없으면 None) — **증거** 값이다. 브라우저 매핑은
    소리 원점 뺄셈이 아니라 `video_origin` 하나로 정한다(늦은 트랙·침묵 선택에서
    뺄셈은 0을 거짓으로 내놓았다, A3 실측). 이 계층은 잰 값만 실고, 더하고 빼는
    것은 검토 계층의 계약이다.
    """

    path: Path
    remuxed: bool
    video_codec: str
    audio_codec: str | None
    audio_stream_count: int
    duration: Fraction
    video_origin: Fraction
    audio_origin: Fraction | None
    width: int
    height: int
    rotation_degrees: int
    source_video_codec: str
    source_audio_codec: str | None
    ffmpeg_version: str
    warnings: tuple[str, ...]


def _visual_preview_args(
    probe: RawMediaProbe, track: RawAudioTrack | None
) -> tuple[list[str], tuple[str, ...], bool]:
    """브라우저 재생 인코딩 인자 — 자막 미리보기와 같은 판정, 침묵은 영상만 지나간다."""
    video = probe.video
    video_copy = video.codec_name == "h264" and video.pix_fmt in _CAPTION_PREVIEW_VIDEO_COPY_PIXFMTS
    warnings: list[str] = []
    args: list[str] = []
    if video_copy:
        args += ["-c:v", "copy"]
    else:
        args += ["-c:v", "libx264", *_CAPTION_PREVIEW_X264_ARGS, "-pix_fmt", "yuv420p"]
        warnings.append(
            f"영상 {video.codec_name}/{video.pix_fmt}을(를) H264 8bit로 변환했다 (원본은 그대로다)"
        )
    if track is None:
        return args, tuple(warnings), video_copy
    if track.codec_name == "aac":
        args += ["-c:a", "copy"]
    else:
        args += ["-c:a", "aac", "-b:a", _CAPTION_PREVIEW_AUDIO_BITRATE]
        warnings.append(f"소리 {track.codec_name}을(를) AAC로 변환했다 (원본은 그대로다)")
    return args, tuple(warnings), video_copy and track.codec_name == "aac"


def extract_visual_review_preview(
    source: Path, staging_output: Path, *, audio_stream_index: int | None
) -> VisualPreviewExtraction:
    """원본 전체를 브라우저 재생 MP4로 **스테이징에** 만든다 — 침묵·선택 트랙 한 개.

    매핑은 영상 첫 스트림과 (고르면) 지정한 오디오 스트림 하나뿐이다. `-ss`를 쓰지
    않아 원본 시각이 그대로 흐른다. 크기·회전을 바꾸는 필터는 하나도 없다 —
    재인코딩할 때 ffmpeg의 기본 autorotate가 방향을 지킨다. 스테이징 안전장치는
    `extract_raw_clip`·`extract_caption_preview`과 같은 것을 쓴다.
    """
    _assert_safe_staging(source, staging_output)
    probe = probe_raw_media(source)
    track = (
        None if audio_stream_index is None else _caption_preview_track(probe, audio_stream_index)
    )
    codec_args, warnings, remuxed = _visual_preview_args(probe, track)
    staging_output.parent.mkdir(parents=True, exist_ok=True)
    staged = _reserve_staging_temp(staging_output)
    try:
        command = [
            require_ffmpeg(), "-hide_banner", "-nostdin", "-y",
            "-i", str(source),
            "-map", f"0:{probe.video.stream_index}",
        ]  # fmt: skip
        if track is not None:
            command += ["-map", f"0:{track.stream_index}"]
        command += ["-dn", "-sn", *codec_args, "-movflags", "+faststart", "-f", "mp4", str(staged)]
        _run(command)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    out_probe = probe_raw_media(staged)
    expected_audio = 0 if track is None else 1
    if len(out_probe.audio) != expected_audio:
        staged.unlink(missing_ok=True)
        raise VisualPreviewUnsupported(
            f"미리보기에 오디오 트랙이 {len(out_probe.audio)}개다 — "
            f"선택({None if track is None else track.stream_index})과 같아야 한다"
        )
    out_clock = probe_extraction_clock(staged, probe=out_probe)
    _publish_staged_clip(staged, staging_output)
    return VisualPreviewExtraction(
        path=staging_output,
        remuxed=remuxed,
        video_codec=out_probe.video.codec_name,
        audio_codec=out_probe.audio[0].codec_name if out_probe.audio else None,
        audio_stream_count=len(out_probe.audio),
        duration=out_probe.duration,
        video_origin=out_clock.presentation_origin,
        audio_origin=out_probe.audio[0].start_time if out_probe.audio else None,
        width=out_probe.video.width,
        height=out_probe.video.height,
        rotation_degrees=out_probe.video.rotation_degrees,
        source_video_codec=probe.video.codec_name,
        source_audio_codec=None if track is None else track.codec_name,
        ffmpeg_version=_tool_version("ffmpeg"),
        warnings=warnings,
    )


@dataclass(frozen=True, slots=True)
class AnalysisProxyBuild:
    """분석 프록시 한 번의 실측 — 격자는 만든 파일에서 다시 쟀다(원본과의 비교는 호출자 몫)."""

    path: Path
    width: int
    height: int
    frame_count: int
    frame_duration: Fraction
    presentation_origin: Fraction


def even_scaled_edge(edge: int, scale: float) -> int:
    """한 변에 배율을 곱해 짝수로 **내린다** — H.264 4:2:0이 홀수 변을 못 담는다.

    ffmpeg 필터(`trunc(변*배율/2)*2`)와 같은 식이다. 호출자가 만들어질 크기를 미리 알아
    파일을 열기 전에 기대와 대조할 수 있다.
    """
    return max(2, int(edge * scale / 2) * 2)


def _analysis_proxy_command(
    source: Path,
    staged: Path,
    stream_index: int,
    *,
    scale: float,
    video_codec: str,
    preset: str,
    crf: int,
    gop: int,
    pix_fmt: str,
) -> list[str]:
    """영상만, 배율만 줄이고 시각은 그대로 — 입력 `-copyts`·패스스루·B프레임 없음이 격자를 지킨다.

    B프레임이 있으면 mp4 먹서가 첫 PTS를 당겨 원점이 어긋난다. `-copyts`가 입력 시각을
    출력으로 그대로 넘기고, 패스스루는 프레임을 버리거나 복제하지 않으며, 인코더 시간
    단위를 입력(`demux`)에 맞추지 않으면 원점이 프레임 길이의 배수로 반올림된다.
    """
    edge = "max(2,trunc({side}*{scale}/2)*2)"
    shrink = "scale=w='{w}':h='{h}'".format(
        w=edge.format(side="iw", scale=repr(scale)), h=edge.format(side="ih", scale=repr(scale))
    )
    return [
        require_ffmpeg(), "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-copyts", "-i", str(source),
        "-map", f"0:{stream_index}",
        "-an", "-sn", "-dn",
        "-vf", shrink,
        "-fps_mode", "passthrough", "-enc_time_base", "demux",
        "-c:v", video_codec, "-preset", preset, "-crf", str(crf),
        "-g", str(gop), "-bf", "0", "-pix_fmt", pix_fmt,
        "-movflags", "+faststart", "-f", "mp4", str(staged),
    ]  # fmt: skip


def build_analysis_proxy(
    source: Path,
    staging_output: Path,
    *,
    scale: float,
    video_codec: str,
    preset: str,
    crf: int,
    gop: int,
    pix_fmt: str,
) -> AnalysisProxyBuild:
    """원본을 한 번 읽어 저해상도 분석 프록시를 **스테이징에** 만들고 원자 게시한다.

    프레임 한 장마다 4K 스트림을 앞에서부터 디코드하는 비용을 이 한 번으로 바꾼다. 실측값만
    돌려준다 — 원본과의 대조는 호출자(`media_refs`)가 짝 검증으로 한다. 취소는 `_run`이 듣는다.
    """
    _assert_safe_staging(source, staging_output)
    probe = probe_raw_media(source)
    staging_output.parent.mkdir(parents=True, exist_ok=True)
    staged = _reserve_staging_temp(staging_output)
    try:
        _run(
            _analysis_proxy_command(
                source,
                staged,
                probe.video.stream_index,
                scale=scale,
                video_codec=video_codec,
                preset=preset,
                crf=crf,
                gop=gop,
                pix_fmt=pix_fmt,
            )
        )
        measured = _measure_proxy(staged)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise
    _publish_staged_clip(staged, staging_output)
    return replace(measured, path=staging_output)


def _measure_proxy(staged: Path) -> AnalysisProxyBuild:
    """스테이징 파일에서 크기·격자를 다시 잰다 — 게시 경로는 호출자가 바꿔 끼운다."""
    out_probe = probe_raw_media(staged)
    clock = probe_extraction_clock(staged, probe=out_probe)
    return AnalysisProxyBuild(
        path=staged,
        width=out_probe.video.width,
        height=out_probe.video.height,
        frame_count=clock.frame_count,
        frame_duration=clock.frame_duration,
        presentation_origin=clock.presentation_origin,
    )


# 32×32로 줄인 뒤 루마(Y) 평면만 그대로 꺼낸다 — 한 장 1024바이트.
_CORRESPONDENCE_VIDEO_FILTER = "scale=32:32,format=yuv420p,extractplanes=y"


def correspondence_samples(
    path: Path, seconds: Sequence[Fraction], *, audio: bool = False
) -> tuple[bytes, ...]:
    """Small decoded samples for explicit original/proxy correspondence checks.

    영상 표본은 32×32 루마 평면이다. 회색(`format=gray`)은 swscale이 전범위로 환산하고
    그 양이 축소 경로(4K→32와 960→32)마다 달라 원본·프록시가 다른 눈금으로 재졌다(실측
    기울기 1.36 vs 1.17, 같은 프레임의 오차 바닥 5–11). 루마 평면은 환산 없이 그대로
    복사된다(실측 0.2–1.3).
    """
    samples = []
    ffmpeg = require_ffmpeg()
    for second in seconds:
        command = [ffmpeg, "-v", "error", "-ss", str(float(second)), "-i", str(path)]
        if audio:
            command += [
                "-map",
                "0:a:0",
                "-t",
                "0.25",
                "-ac",
                "1",
                "-ar",
                "8000",
                "-f",
                "s16le",
                "-",
            ]
        else:
            command += [
                "-map",
                "0:v:0",
                "-frames:v",
                "1",
                "-vf",
                _CORRESPONDENCE_VIDEO_FILTER,
                "-f",
                "rawvideo",
                "-",
            ]
        result = run_process(command, capture_output=True, check=False, timeout=30)
        if result.returncode or not result.stdout:
            raise RuntimeError("대응 표본을 추출하지 못했습니다")
        samples.append(result.stdout)
    return tuple(samples)


def verified_scene_times(path: Path) -> tuple[Fraction, ...]:
    """Decoded adjacent-frame cuts; low resolution is sufficient for cut evidence."""
    result = _run(
        [
            require_ffmpeg(),
            "-hide_banner",
            "-loglevel",
            "info",
            "-nostats",
            "-i",
            str(path),
            "-an",
            "-vf",
            "scale=160:-2,select=gt(scene\\,0.35),showinfo",
            "-fps_mode",
            "vfr",
            "-f",
            "null",
            "-",
        ]
    )
    return tuple(Fraction(value) for value in re.findall(r"pts_time:([0-9.]+)", result.stderr))
