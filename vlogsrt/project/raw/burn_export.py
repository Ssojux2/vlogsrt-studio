"""자막 입히기(번인, W-65 D116) — 영상 하나 + 한국어 SRT(+ 영어 SRT) → `<이름>.burned.mp4`.

읽기·짝짓기·영상 조사는 작업을 걸기 **전에** 한다 — 깨진 SRT나 짝이 안 맞는 영어 SRT는 작업이
되지 않는다. 조판은 순수 `burn_in`, ffmpeg는 `preprocess`(규칙 7)이고 여기는 그 사이의 파일만
다룬다:
임시 작업 폴더에 ASS와 꾸러미 글꼴을 놓고, 결과는 영상 옆에 새 이름으로만 쓴다(덮지 않는다). 영상
옆에 쓸 수 없으면 작업공간 `export/burned/<시각>/`이다. 무료 로컬 일이고 입력 영상·SRT·원본 자막·
지문·캐시 열쇠에는 아무것도 쓰지 않는다. 사람이 고른 모양은 작업공간 표지 `burn_style`에 둔다.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from importlib import resources
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import log, preprocess
from vlogsrt.project.raw import burn_in, media_refs, render_export, srt_english, timeline_srt
from vlogsrt.project.raw.burn_config import BURN_SETTINGS, BurnSettings
from vlogsrt.project.raw.config import RENDER_SETTINGS
from vlogsrt.project.raw.models import JobHandle, RawWorkspaceError

if TYPE_CHECKING:
    from vlogsrt.project.raw.jobs import JobContext
    from vlogsrt.project.raw.service import RawWorkspaceService

BURN_STYLE_KEY = "burn_style"
"""작업공간 표지 — 마지막으로 고른 모양(JSON). 지문·승인·견적 밖이다."""
REPORT_SCHEMA = "vlogsrt-subtitle-burn/v1"
SUBTITLE_NAME = "burn.ass"
FONTS_NAME = "fonts"
STAMP_FORMAT = "%Y%m%d-%H%M%S"
NO_DRAFT_TEXT = "영상과 한국어 SRT를 고르세요 — 아직 낸 초안이 없습니다."
SCRATCH_PREFIX = "vlogsrt-burn-"
_FONT_PACKAGE = "vlogsrt.project.raw"
_FONT_FOLDER = ("data", "fonts")
_COPY_AUDIO_CODECS = frozenset({"aac", "mp3", "alac", "ac3", "eac3"})
"""mp4에 그대로 실을 수 있는 소리 — 그 밖(PCM 등)은 AAC로 한 번 인코딩한다."""
_HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_QUARTER_TURNS = frozenset({90, 270, -90, -270})
_OUTPUT_MODE = 0o644


@dataclass(frozen=True, slots=True)
class BurnJob:
    """입힐 영상 하나 — 읽은 SRT, 짝지은 사건, 화면 크기·프레임 길이·길이, 소리 처리."""

    video: Path
    korean: timeline_srt.TimelineSrt
    english: timeline_srt.TimelineSrt | None
    pairing: burn_in.BurnPairing
    width: int
    height: int
    frame_duration: Fraction
    duration: Fraction
    audio: preprocess.BurnAudio
    hdr: bool


# ── 준비(무료, 작업 전) ───────────────────────────────────────────────


def prepare(video: Path, korean_srt: Path, english_srt: Path | None) -> BurnJob:
    """SRT를 읽어 짝짓고 영상을 조사한다 — 못 하면 이름을 대고 거부한다."""
    if not video.is_file():
        raise RawWorkspaceError(f"영상 파일을 찾지 못했습니다: {video}")
    korean = timeline_srt.read(korean_srt)
    english = timeline_srt.read(english_srt) if english_srt is not None else None
    try:
        pairing = burn_in.pair_cues(korean.cues, english.cues if english else None)
    except burn_in.BurnInError as error:
        raise RawWorkspaceError(str(error)) from error
    if not pairing.events:
        raise RawWorkspaceError(f"SRT `{korean_srt.name}`에 입힐 자막 글이 없습니다")
    try:
        probe = preprocess.probe_raw_media(video)
    except RuntimeError as error:
        raise RawWorkspaceError(f"영상 `{video.name}`을 읽지 못했습니다 — {error}") from error
    return _job(video, korean, english, pairing, probe)


def _job(
    video: Path,
    korean: timeline_srt.TimelineSrt,
    english: timeline_srt.TimelineSrt | None,
    pairing: burn_in.BurnPairing,
    probe: preprocess.RawMediaProbe,
) -> BurnJob:
    stream = probe.video
    width, height = stream.width, stream.height
    if stream.rotation_degrees in _QUARTER_TURNS:
        width, height = height, width  # 디코더가 돌려 세운 화면이 자막의 좌표다
    rate = stream.avg_frame_rate if stream.avg_frame_rate > 0 else stream.r_frame_rate
    if rate <= 0 or width < 1 or height < 1:
        raise RawWorkspaceError(f"영상 `{video.name}`의 크기·프레임레이트를 알 수 없습니다")
    audio: preprocess.BurnAudio = "none"
    if probe.audio:
        audio = "copy" if probe.audio[0].codec_name in _COPY_AUDIO_CODECS else "encode"
    hdr = stream.color_transfer in _HDR_TRANSFERS
    return BurnJob(
        video, korean, english, pairing, width, height, 1 / rate, probe.duration, audio, hdr
    )


def default_inputs(
    service: RawWorkspaceService, root: Path
) -> tuple[Path, Path, Path | None] | None:
    """칸을 비웠을 때의 재료 — 마지막 초안의 영상·가장 새 SRT·그 옆의 영어 SRT(있으면)."""
    latest = service.latest_render_export(root)
    if latest is None:
        return None
    korean = Path(str(latest["srt"]))
    return Path(str(latest["video"])), korean, english_beside(korean)


def resolve_inputs(
    service: RawWorkspaceService,
    root: Path,
    video: Path | None,
    korean: Path | None,
    english: Path | None,
) -> tuple[Path, Path, Path | None]:
    """빈 칸(None)을 채운 (영상, 한국어 SRT, 영어 SRT 또는 None) — 영상·한국어는 마지막 초안,
    영어는 한국어 SRT 옆의 D114 이름(없으면 한글만). 고른 SRT가 `.srt` 파일이 아니면 거부한다."""
    if video is None or korean is None:
        defaults = default_inputs(service, root)
        if defaults is None:
            raise RawWorkspaceError(NO_DRAFT_TEXT)
        video = video or defaults[0]
        korean = korean or defaults[1]
    _require_srt(korean)
    if english is None:
        return video, korean, english_beside(korean)
    _require_srt(english)
    return video, korean, english


def _require_srt(path: Path) -> None:
    if path.suffix.lower() != ".srt" or not path.is_file():
        raise RawWorkspaceError(f"SRT 파일(.srt)을 고르세요 — {path.name}은 SRT 파일이 아닙니다.")


def english_beside(korean: Path) -> Path | None:
    """한국어 SRT 옆의 영어 SRT(D114 이름) — `<이름>.en.srt`, 없으면 가장 새
    `<이름>.en.<시각>.srt`."""
    plain = korean.parent / srt_english.english_name(korean)
    if plain.is_file():
        return plain
    stamped = sorted(korean.parent.glob(f"{plain.stem}.*{plain.suffix}"))
    return stamped[-1] if stamped else None


# ── 모양(작업공간 표지) ────────────────────────────────────────────────


def saved_style(
    service: RawWorkspaceService, root: Path, settings: BurnSettings = BURN_SETTINGS
) -> burn_in.BurnStyle:
    """마지막으로 고른 모양 — 없거나 못 읽으면 기본 모양(못 읽으면 경고한다)."""
    stored = service._workspace(root).meta_value(BURN_STYLE_KEY)
    if not stored:
        return burn_in.default_style(settings)
    try:
        return burn_in.style_from_mapping(json.loads(stored), settings)
    except (ValueError, AttributeError) as error:
        log.warn(f"저장된 자막 모양을 읽지 못해 기본 모양을 씁니다 — {error}")
        return burn_in.default_style(settings)


def save_style(service: RawWorkspaceService, root: Path, style: burn_in.BurnStyle) -> None:
    payload = json.dumps(burn_in.style_to_mapping(style), sort_keys=True, ensure_ascii=False)
    service._workspace(root).set_meta_value(BURN_STYLE_KEY, payload)


# ── 작업 폴더 · ffmpeg ─────────────────────────────────────────────────


def font_file(settings: BurnSettings = BURN_SETTINGS) -> Path:
    """꾸러미에 실은 글꼴 파일 — 없으면 거부한다(다른 글꼴로 바꿔 그리지 않게)."""
    path = Path(str(resources.files(_FONT_PACKAGE).joinpath(*_FONT_FOLDER, settings.font_file)))
    if not path.is_file():
        raise RawWorkspaceError(f"자막 글꼴 `{settings.font_file}`이 꾸러미에 없습니다: {path}")
    return path


def executable(settings: BurnSettings = BURN_SETTINGS) -> str:
    """libass가 든 ffmpeg — 없으면 까는 명령을 대고 거부한다."""
    try:
        return preprocess.subtitle_ffmpeg(settings.ffmpeg_candidates)
    except RuntimeError as error:
        raise RawWorkspaceError(str(error)) from error


def _stage(
    folder: Path, program: str, video: Path, document: str, settings: BurnSettings
) -> preprocess.BurnRequest:
    """작업 폴더에 ASS와 글꼴을 놓는다 — 필터 그래프는 이 폴더 안의 상대 이름만 싣는다."""
    (folder / SUBTITLE_NAME).write_text(document, encoding="utf-8")
    fonts = folder / FONTS_NAME
    fonts.mkdir()
    (fonts / settings.font_file).write_bytes(font_file(settings).read_bytes())
    return preprocess.BurnRequest(program, video, folder, SUBTITLE_NAME, FONTS_NAME)


def preview(
    job: BurnJob, style: burn_in.BurnStyle, settings: BurnSettings = BURN_SETTINGS
) -> bytes:
    """첫 한글 큐 가운데 프레임 하나에 그 큐를 얹은 JPEG — 내보낼 영상과 같은 필터·글꼴이다."""
    event = burn_in.preview_event(job.pairing)
    if event is None:
        raise RawWorkspaceError("미리 볼 자막 글이 없습니다")
    document = burn_in.preview_document(event, style, job.width, job.height, settings)
    at = min((event.start + event.end) / 2, max(Fraction(0), job.duration - job.frame_duration))
    program = executable(settings)
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        request = _stage(Path(scratch), program, job.video, document, settings)
        try:
            return preprocess.burn_preview_frame(
                request,
                at,
                long_edge=settings.preview_long_edge,
                quality=settings.preview_jpeg_quality,
                timeout=settings.preview_timeout_seconds,
            )
        except RuntimeError as error:
            raise RawWorkspaceError(f"미리보기를 만들지 못했습니다 — {error}") from error


# ── 내보내기 ──────────────────────────────────────────────────────────


def _output(
    service: RawWorkspaceService, root: Path, job: BurnJob, settings: BurnSettings, stamp: str
) -> tuple[Path, bool]:
    """(쓸 자리, 작업공간으로 물러났는가) — 같은 이름이 있으면 시각을 붙인 새 이름이다."""
    name = f"{job.video.stem}{settings.output_infix}"
    folder, fallback = job.video.parent, False
    if not os.access(folder, os.W_OK):
        log.warn(f"영상 옆에 쓸 수 없어 작업공간에 씁니다: {folder}")
        folder = service._workspace(root).root.joinpath(*settings.fallback_folder, stamp)
        folder.mkdir(parents=True, exist_ok=True)
        fallback = True
    target = folder / f"{name}.mp4"
    if target.exists() or target.with_suffix(".json").exists():
        target = folder / f"{name}.{stamp}.mp4"
    return target, fallback


def _encode(
    request: preprocess.BurnRequest, output: Path, job: BurnJob, codec: str
) -> preprocess.BurnedVideo:
    encoding = render_export._encoding(RENDER_SETTINGS, codec)
    target = preprocess.RenderTarget(job.width, job.height, job.frame_duration)
    with media_refs.local_media_slot():
        return preprocess.burn_subtitles(
            request, output, target, encoding, duration=job.duration, audio=job.audio
        )


def _encode_with_fallback(
    request: preprocess.BurnRequest, output: Path, job: BurnJob
) -> tuple[preprocess.BurnedVideo, str]:
    """하드웨어 인코더가 그 영상에서 실패하면 libx264로 한 번 더(미리보기와 같은 물러남, W-45)."""
    codecs = RENDER_SETTINGS.video_codecs
    codec = preprocess.pick_video_encoder(codecs, request.executable)
    try:
        return _encode(request, output, job, codec), codec
    except preprocess.ProcessTimeout:
        raise
    except RuntimeError as error:
        software = codecs[-1]
        if codec == software:
            raise
        log.warn(f"{codec}로 자막을 입히지 못해 {software}로 다시 만듭니다 — {error}")
        return _encode(request, output, job, software), software


def burn(
    service: RawWorkspaceService,
    root: Path,
    job: BurnJob,
    style: burn_in.BurnStyle,
    settings: BurnSettings = BURN_SETTINGS,
) -> dict[str, Any]:
    """자막을 입힌 영상과 보고서를 새로 쓴다 — 결과 사실(쓴 자리·센 수)을 돌려준다."""
    if job.hdr:
        log.warn(f"{job.video.name}은 HDR이다 — 톤 매핑 없이 8비트 SDR로 입힌다(초안과 같다)")
    program = executable(settings)
    document = burn_in.ass_document(job.pairing, style, job.width, job.height, settings)
    stamp = datetime.now(UTC).strftime(STAMP_FORMAT)
    output, fallback = _output(service, root, job, settings, stamp)
    log.info(f"자막 입히는 중 · {job.video.name} · 큐 {len(job.pairing.events)}개 → {output.name}")
    with tempfile.TemporaryDirectory(prefix=SCRATCH_PREFIX) as scratch:
        request = _stage(Path(scratch), program, job.video, document, settings)
        try:
            burned, codec = _encode_with_fallback(request, output, job)
        except RuntimeError as error:
            raise RawWorkspaceError(f"자막을 입히지 못했습니다 — {error}") from error
    os.chmod(burned.path, _OUTPUT_MODE)  # 스테이징 자리는 0600이다 — 사람이 쓸 결과는 보통 권한
    report = _report(job, style, settings, burned, codec, program)
    report_path = output.with_suffix(".json")
    render_export._write_new(report_path, json.dumps(report, ensure_ascii=False, indent=2).encode())
    return _facts(job, burned, codec, report_path, fallback)


def _srt_facts(read: timeline_srt.TimelineSrt | None) -> dict[str, Any] | None:
    if read is None:
        return None
    return {"path": str(read.path), "sha256": read.sha256, "cues": len(read.cues)}


def _report(
    job: BurnJob,
    style: burn_in.BurnStyle,
    settings: BurnSettings,
    burned: preprocess.BurnedVideo,
    codec: str,
    program: str,
) -> dict[str, Any]:
    return {
        "schema": REPORT_SCHEMA,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "video": str(job.video),
        "output": str(burned.path),
        "korean_srt": _srt_facts(job.korean),
        "english_srt": _srt_facts(job.english),
        "style": burn_in.style_to_mapping(style),
        "font": {"file": settings.font_file, "name": settings.font_name},
        "events": len(job.pairing.events),
        "empty_cues": job.pairing.empty,
        "size": {"width": burned.width, "height": burned.height},
        "duration_seconds": float(burned.duration),
        "encoder": codec,
        "audio": job.audio,
        "hdr_source": job.hdr,
        "ffmpeg": {"path": program, "version": preprocess.executable_version(program)},
    }


def _facts(
    job: BurnJob,
    burned: preprocess.BurnedVideo,
    codec: str,
    report: Path,
    fallback: bool,
) -> dict[str, Any]:
    return {
        "path": str(burned.path),
        "report": str(report),
        "events": len(job.pairing.events),
        "empty": job.pairing.empty,
        "english": job.english is not None,
        "encoder": codec,
        "fallback": fallback,
        "hdr": job.hdr,
    }


def start(
    service: RawWorkspaceService,
    root: Path,
    job: BurnJob,
    style: burn_in.BurnStyle,
    done: Callable[[dict[str, Any]], None] | None = None,
) -> JobHandle:
    """번인 작업을 건다(무료 로컬) — libass ffmpeg가 없으면 작업 전에 거부한다. 모양을 저장한다."""
    from vlogsrt.project.raw.service import SUBTITLE_BURN_JOB_KIND

    executable()
    save_style(service, root, style)

    def body(context: JobContext) -> None:
        facts = burn(service, root, job, style)
        if done is not None:
            done(facts)

    fingerprint = f"{SUBTITLE_BURN_JOB_KIND}:{job.video}"
    return service.submit_job(root, SUBTITLE_BURN_JOB_KIND, fingerprint, body)
