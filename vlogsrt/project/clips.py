"""원본 영상 목록을 FCPXML 1.14 문서로 **합성한다**.

파생 계층의 입구는 `parse_project` 하나였고, 그래서 입력은 Final Cut이 내보낸
XML뿐이었다. 실물 export는 29.97 소스를 30p 타임라인에 얹은 것 하나로 거부됐고
(애셋 format·클립 format 오버라이드·`conform-rate`), 그대로 진행하면 가장 긴
클립 끝에서 1.36초가 밀렸다.

여기서는 **타임라인을 우리가 저작한다**. 영상 파일을 파일명 순서로 한 줄에 이어
붙인 FCPXML을 만들고, 그것을 **기존 파서에 그대로 먹인다**. 문서를 손으로
조립하지 않는 이유는 셋이다.

- `export.py`·`review.py`가 입력 `raw_xml`을 출력 템플릿으로 재사용한다. 합성
  XML이 있으면 출력 경로가 한 줄도 바뀌지 않는다.
- `fcpxml.py`의 거부 규율(격자·순서·겹침·소스 범위)이 **우리가 만든 것에도**
  그대로 걸린다. 합성물이라고 믿지 않는다.
- 사용자가 받을 산출물이 애초에 FCPXML이다. 입구와 출구가 같은 형식을 쓴다.

합성기는 **추측하지 않는다**. 프레임 레이트·화면 크기·색 좌표·오디오 파라미터가
클립마다 다르면 맞추지 않고 거부하며, 어느 파일의 어떤 값이 어긋났는지 말한다.
리샘플링은 1.36초 사고의 원인이었다.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from .. import preprocess
from .media import sha256_file
from .models import UnsupportedProject
from .timebase import require_supported_frame_duration

SUPPORTED_VERSION = "1.14"
"""합성 문서의 FCPXML 버전 — 파서가 받는 유일한 버전과 같다."""

CLIP_SUFFIXES: frozenset[str] = frozenset({".mov", ".mp4", ".m4v", ".mkv"})
"""입력으로 모으는 확장자. 대소문자는 무시한다."""

DEFAULT_EVENT_NAME = "vlogsrt"

_FORMAT_ID = "r0"
"""합성 문서의 유일한 format id — 클립이 균일하므로 하나면 족하다."""

# FCPXML `format@name`의 프레임 레이트 꼬리표. Final Cut이 쓰는 철자 그대로다 —
# 29.97은 `2997`, 23.976은 `2398`이다.
_FRAME_RATE_TAGS: dict[Fraction, str] = {
    Fraction(1001, 24000): "2398",
    Fraction(1, 24): "24",
    Fraction(1, 25): "25",
    Fraction(1001, 30000): "2997",
    Fraction(1, 30): "30",
    Fraction(1, 50): "50",
    Fraction(1001, 60000): "5994",
    Fraction(1, 60): "60",
}

# (primaries, transfer, matrix) → FCPXML `colorSpace` 철자. ffprobe가 부르는 이름과
# Final Cut이 적는 이름이 달라서 표로 잇는다. 표에 없으면 **거부한다** — 색 좌표를
# 기본값으로 밀어 넣으면 그 추측이 타임라인 선언이 된다.
_COLOR_SPACES: dict[tuple[str, str, str], str] = {
    ("bt709", "bt709", "bt709"): "1-1-1 (Rec. 709)",
    ("smpte170m", "bt709", "smpte170m"): "6-1-6 (Rec. 601 (NTSC))",
    ("bt470bg", "bt709", "bt470bg"): "5-1-6 (Rec. 601 (PAL))",
    ("bt2020", "arib-std-b67", "bt2020nc"): "9-18-9 (Rec. 2020 HLG)",
    ("bt2020", "smpte2084", "bt2020nc"): "9-16-9 (Rec. 2020 PQ)",
}

_AUDIO_LAYOUTS: dict[int, str] = {1: "mono", 2: "stereo"}
_AUDIO_RATE_TAGS: dict[int, str] = {44100: "44.1k", 48000: "48k"}


@dataclass(frozen=True, slots=True)
class ClipProbe:
    """한 영상 파일에서 **실측한** 값 — 합성 문서가 적을 선언의 재료.

    `frame_count`와 `frame_duration`은 `preprocess.probe_video_stream`이 전 프레임
    pts로 CFR을 증명한 뒤에 돌려준 값이다. 시각은 초가 아니라 **프레임 수**로
    들고 다닌다 — 부동소수가 끼어들 자리를 두지 않는다.
    """

    path: Path
    frame_duration: Fraction
    frame_count: int
    width: int
    height: int
    color_space: str
    has_audio: bool
    audio_channels: int | None
    audio_rate: int | None
    content_sha256: str

    @property
    def duration(self) -> Fraction:
        """소스 전체 길이 — 프레임 수 × 프레임 길이."""
        return self.frame_count * self.frame_duration


def collect_clip_paths(directory: Path) -> tuple[Path, ...]:
    """디렉터리에서 영상 파일을 **파일명 오름차순**으로 모은다.

    순서가 곧 타임라인 순서다. 숨김 파일과 확장자가 다른 것은 건너뛴다 —
    하나도 못 찾으면 어디를 뒤졌는지 말하며 거부한다.
    """
    if not directory.is_dir():
        raise UnsupportedProject(str(directory), "입력 경로가 디렉터리가 아니다")
    found = sorted(
        (entry for entry in directory.iterdir() if _is_clip(entry)),
        key=lambda entry: entry.name,
    )
    if not found:
        suffixes = ", ".join(sorted(CLIP_SUFFIXES))
        raise UnsupportedProject(str(directory), f"영상 파일이 없다 — 찾는 확장자: {suffixes}")
    return tuple(found)


def _is_clip(entry: Path) -> bool:
    return (
        entry.is_file() and not entry.name.startswith(".") and entry.suffix.lower() in CLIP_SUFFIXES
    )


def probe_clips(paths: Sequence[Path]) -> tuple[ClipProbe, ...]:
    """주어진 순서 그대로 실측한다 — 재정렬하지 않는다.

    ffmpeg 경계는 `preprocess` 하나다(규칙 7). 여기서는 그 결과를 합쳐 한 클립의
    선언을 만들 뿐이다.
    """
    if not paths:
        raise UnsupportedProject("(입력 없음)", "영상 파일이 하나도 주어지지 않았다")
    return tuple(_probe_one(path) for path in paths)


def _probe_one(path: Path) -> ClipProbe:
    where = str(path)
    if not path.is_file():
        raise UnsupportedProject(where, "입력 파일이 없다")
    summary = preprocess.probe_media_summary(path)
    if summary.video_streams != 1:
        raise UnsupportedProject(
            where, f"비디오 스트림이 {summary.video_streams}개다 — 정확히 하나여야 한다"
        )
    if summary.audio_streams > 1:
        raise UnsupportedProject(
            where, f"오디오 스트림이 {summary.audio_streams}개다 — 0개나 1개여야 한다"
        )
    stream = preprocess.probe_video_grid(path, 0)
    try:
        frame_duration = require_supported_frame_duration(stream.frame_duration)
    except ValueError as error:
        raise UnsupportedProject(where, str(error)) from error
    return ClipProbe(
        path=path,
        frame_duration=frame_duration,
        frame_count=stream.frame_count,
        width=summary.width,
        height=summary.height,
        color_space=_color_space(summary, where),
        has_audio=summary.audio_streams == 1,
        audio_channels=summary.audio_channels,
        audio_rate=summary.audio_rate,
        content_sha256=sha256_file(path),
    )


def _color_space(summary: preprocess.MediaSummary, where: str) -> str:
    key = (summary.color_primaries, summary.color_transfer, summary.color_matrix)
    space = _COLOR_SPACES.get(key)
    if space is None:
        raise UnsupportedProject(
            where,
            f"모르는 색 좌표다: primaries={key[0]!r} transfer={key[1]!r} matrix={key[2]!r}"
            " — 기본값으로 밀어 넣지 않고 거부한다",
        )
    return space


def synthesize_fcpxml(clips: Sequence[ClipProbe], *, project_name: str) -> bytes:
    """클립 실측값을 FCPXML 1.14 바이트로. 같은 입력이면 **같은 바이트**가 나온다.

    순수 함수다 — 파일을 읽지도 ffmpeg를 부르지도 않는다. 시각은 전부 프레임
    수에서 계산하고, 표기는 프레임 길이의 분모를 유지한다(1168프레임 @ 1001/30000
    → `1169168/30000s`). Final Cut 자신이 적는 모양이다.
    """
    if not clips:
        raise UnsupportedProject("(입력 없음)", "합성할 클립이 하나도 없다")
    _require_uniform(clips)
    first = clips[0]
    total_frames = sum(clip.frame_count for clip in clips)
    lines: list[str] = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!DOCTYPE fcpxml>",
        "",
        f'<fcpxml version="{SUPPORTED_VERSION}">',
        "    <resources>",
        f"        {_format_element(first)}",
    ]
    for index, clip in enumerate(clips):
        lines.extend(_asset_lines(clip, _asset_id(index)))
    lines.append("    </resources>")
    lines.append("    <library>")
    lines.append(f'        <event name="{_attr(DEFAULT_EVENT_NAME)}">')
    lines.append(f'            <project name="{_attr(project_name)}" uid="{_project_uid(clips)}">')
    lines.append(f"                {_sequence_open(first, total_frames)}")
    lines.append("                    <spine>")
    offset_frames = 0
    for index, clip in enumerate(clips):
        lines.append(f"                        {_clip_element(clip, index, offset_frames)}")
        offset_frames += clip.frame_count
    lines.append("                    </spine>")
    lines.append("                </sequence>")
    lines.append("            </project>")
    lines.append("        </event>")
    lines.append("    </library>")
    lines.append("</fcpxml>")
    return ("\n".join(lines) + "\n").encode("utf-8")


def _require_uniform(clips: Sequence[ClipProbe]) -> None:
    """섞인 입력은 거부한다 — 어느 파일의 어떤 값이 다른지 이름으로 지목한다."""
    first = clips[0]
    for clip in clips[1:]:
        _require_same(clip, "프레임 길이", first.frame_duration, clip.frame_duration, first)
        _require_same(clip, "가로 크기", first.width, clip.width, first)
        _require_same(clip, "세로 크기", first.height, clip.height, first)
        _require_same(clip, "색 좌표", first.color_space, clip.color_space, first)
        _require_same(clip, "오디오 유무", first.has_audio, clip.has_audio, first)
        _require_same(clip, "오디오 채널 수", first.audio_channels, clip.audio_channels, first)
        _require_same(clip, "오디오 샘플레이트", first.audio_rate, clip.audio_rate, first)
    if first.has_audio:
        _require_audio_declarations(first)


def _require_same(
    clip: ClipProbe, label: str, expected: object, actual: object, first: ClipProbe
) -> None:
    if expected == actual:
        return
    raise UnsupportedProject(
        str(clip.path),
        f"{label}가 첫 클립과 다르다: {actual} ≠ {expected} ({first.path.name})"
        " — 맞추지 않고 거부한다",
    )


def _require_audio_declarations(clip: ClipProbe) -> None:
    if clip.audio_channels not in _AUDIO_LAYOUTS:
        supported = ", ".join(str(value) for value in sorted(_AUDIO_LAYOUTS))
        raise UnsupportedProject(
            str(clip.path),
            f"지원하지 않는 오디오 채널 수다: {clip.audio_channels} — 지원: {supported}",
        )
    if clip.audio_rate not in _AUDIO_RATE_TAGS:
        supported = ", ".join(str(value) for value in sorted(_AUDIO_RATE_TAGS))
        raise UnsupportedProject(
            str(clip.path),
            f"지원하지 않는 오디오 샘플레이트다: {clip.audio_rate} — 지원: {supported}",
        )


def _format_element(clip: ClipProbe) -> str:
    tag = _FRAME_RATE_TAGS[clip.frame_duration]
    name = f"FFVideoFormat{clip.width}x{clip.height}p{tag}"
    return (
        f'<format id="{_FORMAT_ID}" name="{name}"'
        f' frameDuration="{_time(1, clip.frame_duration)}"'
        f' width="{clip.width}" height="{clip.height}"'
        f' colorSpace="{_attr(clip.color_space)}"/>'
    )


def _asset_lines(clip: ClipProbe, asset_id: str) -> list[str]:
    uid = _asset_uid(clip)
    audio = ""
    if clip.has_audio:
        audio = (
            ' hasAudio="1" audioSources="1"'
            f' audioChannels="{clip.audio_channels}" audioRate="{clip.audio_rate}"'
        )
    else:
        audio = ' hasAudio="0"'
    return [
        f'        <asset id="{asset_id}" name="{_attr(clip.path.stem)}" uid="{uid}"'
        f' start="0s" duration="{_time(clip.frame_count, clip.frame_duration)}"'
        f' hasVideo="1" format="{_FORMAT_ID}" videoSources="1"{audio}>',
        f'            <media-rep kind="original-media" sig="{uid}"'
        f' src="{_attr(clip.path.resolve().as_uri())}"/>',
        "        </asset>",
    ]


def _sequence_open(clip: ClipProbe, total_frames: int) -> str:
    audio = ""
    if clip.has_audio:
        layout = _AUDIO_LAYOUTS[_require_int(clip.audio_channels)]
        rate = _AUDIO_RATE_TAGS[_require_int(clip.audio_rate)]
        audio = f' audioLayout="{layout}" audioRate="{rate}"'
    return (
        f'<sequence format="{_FORMAT_ID}"'
        f' duration="{_time(total_frames, clip.frame_duration)}"'
        f' tcStart="0s" tcFormat="NDF"{audio}>'
    )


def _clip_element(clip: ClipProbe, index: int, offset_frames: int) -> str:
    role = ' audioRole="dialogue"' if clip.has_audio else ""
    return (
        f'<asset-clip ref="{_asset_id(index)}"'
        f' offset="{_time(offset_frames, clip.frame_duration)}"'
        f' name="{_attr(clip.path.stem)}" start="0s"'
        f' duration="{_time(clip.frame_count, clip.frame_duration)}"'
        f' format="{_FORMAT_ID}" tcFormat="NDF"{role}/>'
    )


def _asset_id(index: int) -> str:
    """리소스 id — 순번으로만 정한다. 입력이 같으면 id도 같다."""
    return f"r{index + 1}"


def _time(frames: int, frame_duration: Fraction) -> str:
    """프레임 수를 FCPXML 시간 문자열로 — 프레임 길이의 분모를 그대로 쓴다."""
    if frames == 0:
        return "0s"
    numerator = frames * frame_duration.numerator
    denominator = frame_duration.denominator
    if denominator == 1:
        return f"{numerator}s"
    return f"{numerator}/{denominator}s"


def _asset_uid(clip: ClipProbe) -> str:
    """내용 해시에서 나온 32자 대문자 — 경로가 바뀌어도 같은 소스로 읽힌다."""
    return clip.content_sha256[:32].upper()


def _project_uid(clips: Sequence[ClipProbe]) -> str:
    """클립 내용 해시들에서 유도한 UUID 모양 문자열 — 같은 묶음이면 같은 값."""
    digest = hashlib.sha256()
    for clip in clips:
        digest.update(clip.content_sha256.encode("ascii"))
        digest.update(b"\0")
    value = digest.hexdigest()[:32].upper()
    return f"{value[:8]}-{value[8:12]}-{value[12:16]}-{value[16:20]}-{value[20:32]}"


def _require_int(value: int | None) -> int:
    if value is None:
        raise UnsupportedProject("(합성)", "오디오가 있다고 선언해 놓고 파라미터가 비었다")
    return value


def _attr(value: str) -> str:
    """속성 값 이스케이프 — 따옴표까지 막는다(파일명에 들어갈 수 있다)."""
    return (
        value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )
