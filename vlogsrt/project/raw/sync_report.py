"""자막 시각 확인(D55)의 작업공간 쪽 — 재료를 모아 순수 계산에 넘기고 보고서만 쓴다.

재료: 지금 원본 자막 리비전(공통 시계), 1단계 기록(저장 PCM 신원·원점·실패 청크), 원본.
저장 PCM은 해시가 기록과 같고 원본과 창 몇 개가 맞을 때만 쓴다 — 아니면 원본을 한 번
새로 디코드한다(원점까지 잰다). 말소리 칸은 무료 로컬 Silero VAD다(두 번째 whisper 없음).

쓰는 것은 `cache/sync-check/` 아래(보고서·VAD 칸·표본 대조·대체 PCM)와 SRT 옆 곁파일
`<srt>.sync.json`뿐이다. 전사·자막·SRT·분석 지문은 읽기만 하고 절대 쓰지 않는다(규칙 4·5의
대응). 판정은 표시 전용 — 등급·자동 정리·선택·출력·분할 어디에도 들어가지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import log, preprocess
from vlogsrt.project import sync_check
from vlogsrt.project.config import (
    SYNC_CHECK_SETTINGS,
    ProjectConfig,
    SyncCheckSettings,
    sync_check_settings_sha256,
    validate_sync_check_settings,
)
from vlogsrt.project.pcm import PcmReference
from vlogsrt.project.sync_check import Check, Cue, Reason
from vlogsrt.project.vad.inference import infer_vad
from vlogsrt.project.vad.runtime import VadUnavailable, bundled_artifact, create_cpu_session

SYNC_CACHE = Path("cache") / "sync-check"
"""작업공간 안 유일한 쓰기 자리 — 어느 지문에도 들어가지 않는다."""

SIDECAR_SUFFIX = ".sync.json"
VAD_SCHEMA = "sync-vad/v1"
PCM_RATE = 16000
PCM_WIDTH = 2
_HASH_BLOCK = 1 << 20
_AUDIO_ABSENT = "audio_absent"  # 1단계 기록의 오디오 없음 표지(understanding.SPEECH_AUDIO_ABSENT)

BADGES: dict[str, tuple[str, str]] = {
    sync_check.LEVEL_RED: ("자막 시각 어긋남", "red"),
    sync_check.LEVEL_AMBER: ("자막 시각 확인", "amber"),
    sync_check.LEVEL_GREEN: ("자막 시각 OK", "green"),
    sync_check.LEVEL_SKIP: ("측정 불가", "grey"),
}
"""판정 → (화면 글, 표지 색). 측정 불가는 회색이다 — 결코 초록이 아니다(D32)."""


def badge(level: str) -> tuple[str, str]:
    return BADGES.get(level, BADGES[sync_check.LEVEL_SKIP])


class SyncUnavailable(Exception):
    """잴 재료를 못 만들었다 — 판정은 측정 불가이고 이유가 화면에 나간다."""

    def __init__(self, reason: Reason, checks: Sequence[Check] = ()) -> None:
        super().__init__(reason.text)
        self.reason = reason
        self.checks = tuple(checks)


@dataclass(frozen=True, slots=True)
class FailedChunk:
    """1단계에서 실패한 전사 청크 — PCM 시계의 범위와 실패 문구(자막이 없는 구멍)."""

    start: Fraction
    end: Fraction
    reason: str


@dataclass(frozen=True, slots=True)
class Stage1Audio:
    """1단계 기록이 잰 오디오 신원 — 저장 PCM은 기록 폴더의 `analysis.pcm`이다."""

    folder: Path
    created_at: str
    absent: bool
    pcm_sha256: str = ""
    stream_index: int = 0
    channels: int = 0
    sample_count: int = 0
    origin: Fraction = Fraction(0)
    presentation_origin: Fraction = Fraction(0)
    failed: tuple[FailedChunk, ...] = ()
    clamped: int = 0  # 1단계가 청크 끝에 맞춘 세그먼트 수(D55)

    @property
    def pcm_path(self) -> Path:
        return self.folder / "analysis.pcm"


@dataclass(frozen=True, slots=True)
class SyncSource:
    """원본 하나의 잴 재료 — 큐는 원본 자막의 공통 시계 그대로다."""

    source_id: str
    source_name: str
    source_sha256: str
    original: Path | None
    original_note: str
    transcript_revision: str
    cues: tuple[Cue, ...]
    stage1: Stage1Audio | None


@dataclass(frozen=True, slots=True)
class PcmChoice:
    """잴 PCM — 저장본(검증됨)이거나 새로 디코드한 것. 원점과 검사 줄을 함께 싣는다."""

    path: Path
    pcm_sha256: str
    stream_index: int
    channels: int
    sample_count: int
    origin: Fraction
    presentation_origin: Fraction
    basis: str
    checks: tuple[Check, ...]

    @property
    def audio_start(self) -> Fraction:
        """PCM 표본 0의 공통 시계 위치(1단계 `common_timeline`과 같은 산술)."""
        return self.origin - self.presentation_origin


# ─────────────────────────────────────────────────────────────
#  재료 — 1단계 기록·원본 자막 (읽기만)
# ─────────────────────────────────────────────────────────────


def _rational(value: object) -> Fraction:
    if isinstance(value, Mapping):
        return Fraction(int(value["num"]), int(value["den"]))
    return Fraction(str(value))


def cues_of(transcript: Mapping[str, Any]) -> tuple[Cue, ...]:
    """원본 자막 리비전의 큐 — 공통 시계의 정확 유리수 그대로."""
    return tuple(
        Cue(
            str(item["utterance_id"]),
            _rational(item["start"]),
            _rational(item["end"]),
            item["text"],
        )
        for item in transcript.get("utterances", [])
    )


STAGE1_DOCUMENTS = ("asr_phase.json", "analysis_transcript.json")
"""1단계 오디오 신원을 담은 문서 — 두 단계 실행은 앞의 것, 한 번에 끝난 실행은 뒤의 것."""


def stage1_audio(workspace_root: Path, asset_id: str, source_sha256: str) -> Stage1Audio | None:
    """이 원본의 가장 최근 1단계 기록 — 원본 해시가 같은 것만. 없으면 None. 읽기만 한다."""
    found: list[Stage1Audio] = []
    for folder in sorted(workspace_root.glob(f"analysis/*/*_{asset_id}")):
        try:
            parsed = _stage1_in(folder, source_sha256)
        except (OSError, ValueError, KeyError, TypeError) as error:
            log.warn(f"자막 시각 확인 — 1단계 기록을 읽지 못해 건너뛴다: {folder} ({error})")
            continue
        if parsed is not None:
            found.append(parsed)
    return max(found, key=lambda item: item.created_at, default=None)


def _stage1_in(folder: Path, source_sha256: str) -> Stage1Audio | None:
    record = _read_json(folder / "analysis_record.json") or {}
    for name in STAGE1_DOCUMENTS:
        path = folder / name
        if path.is_file():
            body = json.loads(path.read_text(encoding="utf-8"))
            created = str(body.get("created_at") or record.get("created_at") or "")
            return _stage1_of(body, folder, source_sha256, created, record)
    return None


def _stage1_of(
    body: Mapping[str, Any],
    folder: Path,
    source_sha256: str,
    created: str,
    record: Mapping[str, Any],
) -> Stage1Audio | None:
    audio = body.get("audio")
    if not isinstance(audio, Mapping) or audio.get("status") == _AUDIO_ABSENT:
        absent_owner = body.get("source_sha256", source_sha256)
        if absent_owner != source_sha256:
            return None
        return Stage1Audio(folder=folder, created_at=created, absent=True)
    if body.get("source_sha256", audio.get("source_sha256")) != source_sha256:
        return None
    rate = int(audio["sample_rate"])
    return Stage1Audio(
        folder=folder,
        created_at=created,
        absent=False,
        pcm_sha256=str(audio["pcm_sha256"]),
        stream_index=int(audio["audio_stream_index"]),
        channels=int(audio["channels"]),
        sample_count=int(audio["sample_count"]),
        origin=_rational(audio["pcm_sample0_file_time"]),
        presentation_origin=_presentation_of(body, record),
        failed=_failed_chunks(body, rate),
        clamped=sum(
            len(chunk.get("clamped_segments", []))
            for chunk in (body.get("asr") or {}).get("chunks", [])
        ),
    )


def _presentation_of(body: Mapping[str, Any], record: Mapping[str, Any]) -> Fraction:
    """영상 제시 원점 — 1단계 기록의 격자, 없으면 분석 기록의 시간축, 둘 다 없으면 0."""
    video = body.get("video")
    if isinstance(video, Mapping) and "presentation_origin" in video:
        return _rational(video["presentation_origin"])
    timeline = record.get("timeline") or {}
    origin = (timeline.get("video") or {}).get("presentation_origin")
    return Fraction(0) if origin is None else _rational(origin)


def _failed_chunks(body: Mapping[str, Any], rate: int) -> tuple[FailedChunk, ...]:
    chunks = (body.get("asr") or {}).get("chunks", [])
    return tuple(
        FailedChunk(
            Fraction(int(chunk["sample_start"]), rate),
            Fraction(int(chunk["sample_end"]), rate),
            str(chunk["failure"]),
        )
        for chunk in chunks
        if chunk.get("failure")
    )


# ─────────────────────────────────────────────────────────────
#  PCM — 저장본을 검증하거나 원본을 새로 디코드한다
# ─────────────────────────────────────────────────────────────


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(_HASH_BLOCK), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def choose_pcm(source: SyncSource, cache_root: Path, settings: SyncCheckSettings) -> PcmChoice:
    """저장 PCM이 기록·원본과 맞으면 그것, 아니면 원본을 새로 디코드한다."""
    stage1 = source.stage1
    if stage1 is not None and stage1.absent:
        raise SyncUnavailable(Reason("audio_absent", sync_check.LEVEL_SKIP, "오디오 트랙이 없음"))
    notes: tuple[Check, ...] = ()
    if stage1 is not None:
        stored, notes = _stored_choice(source, stage1, cache_root, settings)
        if stored is not None:
            return stored
    else:
        notes = (Check("저장 PCM", sync_check.STATUS_WARN, "1단계 기록이 없어 원본을 새로 디코드"),)
    if source.original is None:
        text = f"원본을 읽지 못해 PCM을 확인할 수 없음 — {source.original_note}"
        raise SyncUnavailable(Reason("pcm_unverifiable", sync_check.LEVEL_SKIP, text), notes)
    return _decoded_choice(source, cache_root, settings, notes)


def _stored_choice(
    source: SyncSource, stage1: Stage1Audio, cache_root: Path, settings: SyncCheckSettings
) -> tuple[PcmChoice | None, tuple[Check, ...]]:
    name = "저장 PCM"
    if not stage1.pcm_path.is_file():
        return None, (Check(name, sync_check.STATUS_WARN, "1단계 PCM이 없어 새로 디코드"),)
    if sha256_file(stage1.pcm_path) != stage1.pcm_sha256:
        return None, (
            Check(name, sync_check.STATUS_WARN, "1단계 PCM 해시가 기록과 달라 새로 디코드"),
        )
    choice = PcmChoice(
        path=stage1.pcm_path,
        pcm_sha256=stage1.pcm_sha256,
        stream_index=stage1.stream_index,
        channels=stage1.channels,
        sample_count=stage1.sample_count,
        origin=stage1.origin,
        presentation_origin=stage1.presentation_origin,
        basis="stored",
        checks=(),
    )
    if source.original is None:
        text = f"원본을 읽지 못해 저장 PCM을 원본과 대조할 수 없음 — {source.original_note}"
        raise SyncUnavailable(Reason("pcm_unverifiable", sync_check.LEVEL_SKIP, text))
    spot = spot_check(choice, source, cache_root, settings)
    if spot.status != sync_check.STATUS_PASS:
        warned = Check(spot.name, sync_check.STATUS_WARN, spot.detail + " — 새로 디코드")
        return None, (warned,)
    return replace(choice, checks=(spot, _origin_check(choice, None, settings))), ()


def _decoded_choice(
    source: SyncSource, cache_root: Path, settings: SyncCheckSettings, notes: tuple[Check, ...]
) -> PcmChoice:
    assert source.original is not None
    decoded = decoded_pcm(source, cache_root)
    checks = (*notes, _origin_check(decoded, source.stage1, settings))
    return replace(decoded, checks=checks)


def decoded_pcm(source: SyncSource, cache_root: Path) -> PcmChoice:
    """원본을 한 번 새로 디코드해 둔다(원점까지 잰다) — 원본 해시·스트림이 같으면 다시 쓴다."""
    assert source.original is not None
    folder = cache_root / "pcm" / source.source_sha256[:24]
    meta_path, pcm_path = folder / "audio.json", folder / "audio.pcm"
    ordinal, stream_index = _audio_ordinal(source)
    meta = _read_json(meta_path)
    if (
        meta is not None
        and meta.get("source_sha256") == source.source_sha256
        and meta.get("stream_index") == stream_index
        and pcm_path.is_file()
        and sha256_file(pcm_path) == meta.get("pcm_sha256")
    ):
        return _choice_from_meta(meta, pcm_path)
    folder.mkdir(parents=True, exist_ok=True)
    partial = folder / f".audio-{uuid.uuid4().hex}.pcm"  # 같은 프로세스의 두 작업도 겹치지 않게
    try:
        extraction = preprocess.extract_pcm_s16le(source.original, partial, stream_index=ordinal)
        os.replace(partial, pcm_path)
    finally:
        partial.unlink(missing_ok=True)
    meta = {
        "source_sha256": source.source_sha256,
        "stream_index": extraction.stream.stream_index,
        "channels": extraction.stream.channels,
        "sample_count": extraction.sample_count,
        "origin": sync_check.fraction_text(extraction.pcm_sample0_file_time),
        "presentation_origin": sync_check.fraction_text(_presentation_origin(source)),
        "pcm_sha256": sha256_file(pcm_path),
    }
    _atomic_write(meta_path, json.dumps(meta, sort_keys=True))
    return _choice_from_meta(meta, pcm_path)


def _choice_from_meta(meta: Mapping[str, Any], pcm_path: Path) -> PcmChoice:
    return PcmChoice(
        path=pcm_path,
        pcm_sha256=str(meta["pcm_sha256"]),
        stream_index=int(meta["stream_index"]),
        channels=int(meta["channels"]),
        sample_count=int(meta["sample_count"]),
        origin=Fraction(str(meta["origin"])),
        presentation_origin=Fraction(str(meta["presentation_origin"])),
        basis="decoded",
        checks=(),
    )


def _audio_ordinal(source: SyncSource) -> tuple[int, int]:
    """(오디오 순번, 전역 번호) — 1단계가 고른 트랙이 있으면 그 트랙, 없으면 첫 오디오."""
    assert source.original is not None
    wanted = source.stage1.stream_index if source.stage1 is not None else None
    count = preprocess.probe_audio_stream_count(source.original)
    for ordinal in range(count):
        index = preprocess.probe_audio_stream(source.original, ordinal).stream_index
        if wanted is None or index == wanted:
            return ordinal, index
    raise SyncUnavailable(
        Reason(
            "audio_missing", sync_check.LEVEL_SKIP, "1단계가 고른 오디오 트랙을 원본에서 못 찾음"
        )
    )


def _presentation_origin(source: SyncSource) -> Fraction:
    if source.stage1 is not None:
        return source.stage1.presentation_origin
    assert source.original is not None
    try:
        return preprocess.probe_extraction_clock(source.original).presentation_origin
    except RuntimeError as error:
        log.warn(f"자막 시각 확인 — 영상 원점을 못 재 0으로 둔다: {source.source_name} ({error})")
        return Fraction(0)


def _origin_check(
    choice: PcmChoice, recorded: Stage1Audio | None, settings: SyncCheckSettings
) -> Check:
    """원점 — 새로 잰 값이 1단계 기록과 다르면 경고(자막 전체가 그만큼 밀린다)."""
    name = "오디오 원점"
    detail = (
        f"표본 0 = 파일 {float(choice.origin):.3f}초"
        f" · 영상 원점 {float(choice.presentation_origin):.3f}초"
    )
    if recorded is None or recorded.absent or choice.basis == "stored":
        return Check(name, sync_check.STATUS_INFO, detail + " (대조할 다른 기록이 없음)")
    gap = abs(choice.origin - recorded.origin)
    if gap > settings.origin_negligible_seconds:
        return Check(
            name, sync_check.STATUS_WARN, f"{detail} — 1단계 기록과 {float(gap):.3f}초 다름"
        )
    return Check(name, sync_check.STATUS_PASS, detail)


# ─────────────────────────────────────────────────────────────
#  표본 대조 — 저장 PCM의 창 몇 개를 원본 디코드와 견준다(D32 값)
# ─────────────────────────────────────────────────────────────


def spot_windows(sample_count: int, settings: SyncCheckSettings) -> tuple[Fraction, ...]:
    """창 시작(PCM 초) — 길이를 고르게 나눈 자리, 짧은 원본은 창 하나."""
    duration = Fraction(sample_count, PCM_RATE)
    width = min(settings.spot_window_seconds, duration)
    starts: list[Fraction] = []
    for index in range(1, settings.spot_windows + 1):
        center = duration * index / (settings.spot_windows + 1)
        start = min(max(Fraction(0), center - width / 2), duration - width)
        start = Fraction(int(start * PCM_RATE), PCM_RATE)
        if start not in starts:
            starts.append(start)
    return tuple(starts)


def spot_check(
    choice: PcmChoice, source: SyncSource, cache_root: Path, settings: SyncCheckSettings
) -> Check:
    """창마다 (지연, 상관) — 모두 허용 지연 안·상관 하한 위여야 통과. 결과는 캐시한다."""
    settings8 = sync_check_settings_sha256(settings)[:8]
    path = (
        cache_root
        / "spot"
        / f"{choice.pcm_sha256[:24]}-{source.source_sha256[:24]}-{settings8}.json"
    )
    cached = _read_json(path)
    if cached is not None and cached.get("pcm_sha256") == choice.pcm_sha256:
        return Check(str(cached["name"]), str(cached["status"]), str(cached["detail"]))
    rows = [
        _spot_window(choice, source, start, settings)
        for start in spot_windows(choice.sample_count, settings)
    ]
    hop = settings.spot_envelope_seconds
    passed = all(
        abs(lag) * hop <= settings.spot_max_lag_seconds and score >= settings.spot_min_correlation
        for _, lag, score in rows
    )
    detail = " · ".join(
        f"{float(start):.0f}초 지연 {float(lag * hop):+.2f}초 상관 {score:.3f}"
        for start, lag, score in rows
    )
    check = Check(
        "저장 PCM ↔ 원본 표본", sync_check.STATUS_PASS if passed else sync_check.STATUS_FAIL, detail
    )
    body = {
        "pcm_sha256": choice.pcm_sha256,
        "name": check.name,
        "status": check.status,
        "detail": check.detail,
    }
    _atomic_write(path, json.dumps(body, ensure_ascii=False, sort_keys=True))
    return check


def _spot_window(
    choice: PcmChoice, source: SyncSource, start: Fraction, settings: SyncCheckSettings
) -> tuple[Fraction, int, float]:
    assert source.original is not None
    width = min(settings.spot_window_seconds, Fraction(choice.sample_count, PCM_RATE) - start)
    frames = int(width * PCM_RATE)
    frame_bytes = PCM_WIDTH * choice.channels
    with choice.path.open("rb") as handle:
        handle.seek(int(start * PCM_RATE) * frame_bytes)
        stored = handle.read(frames * frame_bytes)
    fresh = preprocess.pcm_window_s16le(
        source.original,
        stream_index=choice.stream_index,
        start=choice.origin + start,
        seconds=width,
        channels=choice.channels,
    )
    hop_samples = int(settings.spot_envelope_seconds * PCM_RATE)
    reference = sync_check.pcm_envelope(stored, choice.channels, hop_samples)
    probe = sync_check.pcm_envelope(fresh, choice.channels, hop_samples)
    max_lag = int(settings.spot_search_seconds / settings.spot_envelope_seconds)
    lag, score = sync_check.envelope_alignment(reference, probe, max_lag)
    return start, lag, score


# ─────────────────────────────────────────────────────────────
#  말소리 칸 — 무료 로컬 Silero, PCM 해시로 캐시
# ─────────────────────────────────────────────────────────────


def vad_key(project: ProjectConfig) -> str:
    body = {
        "schema": VAD_SCHEMA,
        "model": project.vad_model_sha256,
        "positive": project.vad_positive,
        "window": project.vad_window_samples,
        "context": project.vad_context_samples,
        "rate": project.vad_sample_rate,
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True).encode("utf-8")).hexdigest()


def default_session_factory(project: ProjectConfig) -> Callable[[], object]:
    """세션을 한 번만 만든다 — 런타임이 없으면 `VadUnavailable`이 측정 불가가 된다."""
    made: list[object] = []

    def factory() -> object:
        if not made:
            made.append(create_cpu_session(bundled_artifact(project)))
        return made[0]

    return factory


def speech_track(
    choice: PcmChoice,
    source_id: str,
    cache_root: Path,
    project: ProjectConfig,
    session_factory: Callable[[], object],
) -> sync_check.SpeechTrack:
    """채널 합집합 말소리 칸 — 같은 PCM·같은 VAD면 캐시를 다시 쓴다(디코드·추론 0)."""
    key = vad_key(project)
    path = cache_root / "vad" / f"{choice.pcm_sha256[:24]}-{key[:8]}.json"
    window = project.vad_window_samples
    frame_count = -(-choice.sample_count // window)
    cached = _read_json(path)
    if (
        cached is not None
        and cached.get("pcm_sha256") == choice.pcm_sha256
        and cached.get("vad") == key
    ):
        runs = tuple((int(start), int(end)) for start, end in cached["runs"])
    else:
        runs = _infer_runs(choice, source_id, project, session_factory, frame_count)
        body = {
            "pcm_sha256": choice.pcm_sha256,
            "vad": key,
            "frame_count": frame_count,
            "runs": runs,
        }
        _atomic_write(path, json.dumps(body, sort_keys=True))
    return sync_check.SpeechTrack(
        Fraction(window, project.vad_sample_rate),
        frame_count,
        Fraction(choice.sample_count, project.vad_sample_rate),
        runs,
    )


def _infer_runs(
    choice: PcmChoice,
    source_id: str,
    project: ProjectConfig,
    session_factory: Callable[[], object],
    frame_count: int,
) -> tuple[tuple[int, int], ...]:
    try:
        session = session_factory()
    except VadUnavailable as error:
        text = f"VAD 런타임이 없어 잴 수 없음 (선택 extra `project`) — {error}"
        raise SyncUnavailable(Reason("vad_unavailable", sync_check.LEVEL_SKIP, text)) from error
    reference = PcmReference(
        source_id=source_id,
        original_stem=source_id,
        original_sha256="",
        path=choice.path,
        pcm_sha256=choice.pcm_sha256,
        analysis_key=f"sync-check:{choice.pcm_sha256}",
        pcm_sample0_file_time=choice.origin,
        sample_rate=project.vad_sample_rate,
        channels=choice.channels,
        sample_count=choice.sample_count,
    )
    result = infer_vad(reference, source_id, project, session)
    if result.status != "success":
        text = f"VAD가 실패해 잴 수 없음 — {result.error}"
        raise SyncUnavailable(Reason("vad_failed", sync_check.LEVEL_SKIP, text))
    flags = [False] * frame_count
    for window in result.windows:
        if window.probability >= project.vad_positive:
            flags[window.start_sample // project.vad_window_samples] = True
    return sync_check.speech_runs(flags)


# ─────────────────────────────────────────────────────────────
#  측정 → 보고서
# ─────────────────────────────────────────────────────────────


def measure_source(
    source: SyncSource,
    *,
    cache_root: Path,
    settings: SyncCheckSettings = SYNC_CHECK_SETTINGS,
    project: ProjectConfig | None = None,
    session_factory: Callable[[], object] | None = None,
) -> dict[str, Any]:
    """원본 하나를 잰 보고서 문서 — 잴 수 없으면 이유가 실린 측정 불가 문서다."""
    validate_sync_check_settings(settings)
    project = project or ProjectConfig()
    factory = session_factory or default_session_factory(project)
    chunk_reasons, chunk_checks = _chunk_findings(source)
    try:
        choice = choose_pcm(source, cache_root, settings)
        speech = speech_track(choice, source.source_id, cache_root, project, factory)
    except SyncUnavailable as missing:
        verdict = sync_check.skipped_verdict(
            (missing.reason, *chunk_reasons), (*missing.checks, *chunk_checks)
        )
        return report_document(source, settings, verdict, None, None, None)
    shift = choice.audio_start
    cues = tuple(replace(cue, start=cue.start - shift, end=cue.end - shift) for cue in source.cues)
    metrics = sync_check.measure(cues, speech, settings)
    verdict = sync_check.judge(metrics, settings, extra_checks=(*choice.checks, *chunk_checks))
    verdict = replace(verdict, reasons=(*verdict.reasons, *chunk_reasons))
    return report_document(source, settings, verdict, metrics, choice, speech)


def _chunk_findings(source: SyncSource) -> tuple[tuple[Reason, ...], tuple[Check, ...]]:
    """1단계 전사 실패 청크 — 자막이 없는 구멍이다(규칙 9). 판정을 바꾸지 않고 알린다."""
    if source.stage1 is None or source.stage1.absent:
        detail = "1단계 기록이 없어 실패 구간을 모름" if source.stage1 is None else "오디오 없음"
        return (), (Check("전사 실패 구간", sync_check.STATUS_SKIP, detail),)
    clamped = _clamped_findings(source.stage1)
    failed = source.stage1.failed
    if not failed:
        return clamped[0], (Check("전사 실패 구간", sync_check.STATUS_PASS, "없음"), *clamped[1])
    shift = source.stage1.origin - source.stage1.presentation_origin
    spans = ", ".join(
        f"{float(item.start + shift):.0f}–{float(item.end + shift):.0f}초" for item in failed
    )
    text = f"전사 청크 {len(failed)}개가 실패해 그 구간({spans})에 자막이 없음"
    reason = Reason("failed_chunks", "info", text)
    return (reason, *clamped[0]), (
        Check("전사 실패 구간", sync_check.STATUS_WARN, text),
        *clamped[1],
    )


def _clamped_findings(stage1: Stage1Audio) -> tuple[tuple[Reason, ...], tuple[Check, ...]]:
    """1단계가 청크 끝을 살짝 넘은 세그먼트를 끝에 맞췄다면 그 수를 알린다(D55) — 판정은 그대로."""
    if not stage1.clamped:
        return (), ()
    text = f"{stage1.clamped}줄을 조각 끝에 맞춤 — whisper 끝 시각이 조각 길이를 살짝 넘었다"
    return (Reason("clamped", "info", text),), (
        Check("조각 끝 넘침", sync_check.STATUS_INFO, text),
    )


def report_document(
    source: SyncSource,
    settings: SyncCheckSettings,
    verdict: sync_check.Verdict,
    metrics: sync_check.SyncMetrics | None,
    choice: PcmChoice | None,
    speech: sync_check.SpeechTrack | None,
) -> dict[str, Any]:
    stage1 = source.stage1
    shift = choice.audio_start if choice is not None else Fraction(0)
    flagged = [] if metrics is None else _common_flags(metrics.flagged, shift)
    return {
        "schema": sync_check.REPORT_SCHEMA,
        "settings_version": settings.version,
        "settings_sha256": sync_check_settings_sha256(settings),
        "source": {
            "source_id": source.source_id,
            "source_name": source.source_name,
            "source_sha256": source.source_sha256,
            "transcript_revision": source.transcript_revision,
        },
        "stage1_pcm_sha256": _stage1_key(stage1),
        "pcm": None if choice is None else _pcm_document(choice),
        "speech_seconds": None
        if speech is None
        else sync_check.fraction_text(speech.speech_seconds),
        "metrics": None if metrics is None else sync_check.metrics_document(metrics),
        "cues": flagged,
        "failed_chunks": _failed_document(stage1),
        **sync_check.verdict_document(verdict),
    }


def _stage1_key(stage1: Stage1Audio | None) -> str | None:
    if stage1 is None:
        return None
    return "absent" if stage1.absent else stage1.pcm_sha256


def _pcm_document(choice: PcmChoice) -> dict[str, Any]:
    return {
        "basis": choice.basis,
        "pcm_sha256": choice.pcm_sha256,
        "channels": choice.channels,
        "sample_count": choice.sample_count,
        "origin": sync_check.fraction_text(choice.origin),
        "presentation_origin": sync_check.fraction_text(choice.presentation_origin),
        "audio_start": sync_check.fraction_text(choice.audio_start),
    }


def _common_flags(flagged: Iterable[sync_check.CueFlag], shift: Fraction) -> list[dict[str, Any]]:
    """표지 큐를 원본 자막의 공통 시계로 되돌려 싣는다 — 화면이 그 자리로 간다."""
    restored = [
        replace(item, cue=replace(item.cue, start=item.cue.start + shift, end=item.cue.end + shift))
        for item in flagged
    ]
    return sync_check.flagged_document(restored)


def _failed_document(stage1: Stage1Audio | None) -> list[dict[str, str]]:
    if stage1 is None or stage1.absent:
        return []
    shift = stage1.origin - stage1.presentation_origin
    return [
        {
            "start": sync_check.fraction_text(item.start + shift),
            "end": sync_check.fraction_text(item.end + shift),
            "reason": item.reason,
        }
        for item in stage1.failed
    ]


def error_document(
    source: SyncSource, settings: SyncCheckSettings, error: BaseException
) -> dict[str, Any]:
    """측정 중 오류 — 측정 불가 보고서다. 자막은 건드리지 않는다."""
    reason = Reason(
        "error", sync_check.LEVEL_SKIP, f"측정 중 오류 — {type(error).__name__}: {error}"
    )
    return report_document(
        source, settings, sync_check.skipped_verdict((reason,)), None, None, None
    )


# ─────────────────────────────────────────────────────────────
#  보고서 파일
# ─────────────────────────────────────────────────────────────


def report_path(
    cache_root: Path, source_id: str, transcript_revision: str, settings: SyncCheckSettings
) -> Path:
    settings8 = sync_check_settings_sha256(settings)[:8]
    return cache_root / "reports" / source_id / f"{transcript_revision}-{settings8}.json"


def write_report(
    cache_root: Path, document: Mapping[str, Any], settings: SyncCheckSettings
) -> Path:
    source = document["source"]
    path = report_path(cache_root, source["source_id"], source["transcript_revision"], settings)
    _atomic_write(path, sync_check.report_json(document))
    return path


def load_report(path: Path) -> dict[str, Any] | None:
    body = _read_json(path)
    return body if body is not None and body.get("schema") == sync_check.REPORT_SCHEMA else None


def latest_report(cache_root: Path, source_id: str) -> dict[str, Any] | None:
    """가장 최근에 쓴 보고서 — 지금 리비전의 보고서가 아직 없을 때 '낡음'으로 보여준다."""
    folder = cache_root / "reports" / source_id
    candidates = sorted(folder.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
    for path in candidates:
        body = load_report(path)
        if body is not None:
            return body
    return None


def is_current(
    document: Mapping[str, Any] | None, source: SyncSource, settings: SyncCheckSettings
) -> bool:
    """보고서가 지금 재료(자막 리비전·1단계 PCM·기준)와 같은가 — 다르면 다시 잰다."""
    return (
        document is not None
        and document["source"]["transcript_revision"] == source.transcript_revision
        and document.get("stage1_pcm_sha256") == _stage1_key(source.stage1)
        and document.get("settings_sha256") == sync_check_settings_sha256(settings)
    )


def state_key(source: SyncSource, settings: SyncCheckSettings) -> str:
    """할 일 열쇠 — 같은 열쇠는 같은 측정이다(앱 실행마다 한 번 거는 기준)."""
    return "|".join(
        (
            source.transcript_revision,
            _stage1_key(source.stage1) or "-",
            sync_check_settings_sha256(settings)[:8],
        )
    )


# ─────────────────────────────────────────────────────────────
#  SRT 모드 — `vlogsrt project sync-check`
# ─────────────────────────────────────────────────────────────

_SRT_TIME = re.compile(r"(\d+):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d+):(\d{2}):(\d{2})[,.](\d{3})")


def srt_cues(text: str) -> tuple[Cue, ...]:
    """SRT 큐 — 시각은 밀리초 정확 유리수. 고치지 않고 읽기만 한다."""
    cues: list[Cue] = []
    for block in re.split(r"\r?\n\s*\r?\n", text.strip()):
        lines = [line for line in block.splitlines() if line.strip()]
        for position, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match is None:
                continue
            numbers = [int(value) for value in match.groups()]
            start = Fraction(
                numbers[0] * 3600000 + numbers[1] * 60000 + numbers[2] * 1000 + numbers[3], 1000
            )
            end = Fraction(
                numbers[4] * 3600000 + numbers[5] * 60000 + numbers[6] * 1000 + numbers[7], 1000
            )
            body = "\n".join(lines[position + 1 :])
            cues.append(Cue(f"cue_{len(cues) + 1:05d}", start, end, body))
            break
    return tuple(cues)


def sidecar_path(srt: Path) -> Path:
    return srt.with_name(srt.name + SIDECAR_SUFFIX)


def write_sidecar(srt: Path, document: Mapping[str, Any]) -> Path:
    """SRT 옆 곁파일 하나 — SRT 자체는 한 바이트도 바꾸지 않는다."""
    path = sidecar_path(srt)
    _atomic_write(path, sync_check.report_json(document))
    return path


def summary_lines(document: Mapping[str, Any]) -> list[str]:
    """사람이 읽는 요약 — 판정 표지·이유·검사 표. CLI가 그대로 인쇄한다."""
    label, _tone = badge(str(document.get("verdict", sync_check.LEVEL_SKIP)))
    source = document.get("source", {})
    lines = [f"{source.get('source_name', '?')}: {label} ({document.get('verdict')})"]
    lines += [f"  · {item['text']}" for item in document.get("reasons", [])]
    lines += [
        f"    [{str(item['status']).upper():4}] {item['name']} — {item['detail']}"
        for item in document.get("checks", [])
    ]
    return lines


def measure_srt(
    media: Path,
    srt: Path,
    *,
    scratch: Path,
    audio_ordinal: int = 0,
    settings: SyncCheckSettings = SYNC_CHECK_SETTINGS,
    project: ProjectConfig | None = None,
    session_factory: Callable[[], object] | None = None,
) -> dict[str, Any]:
    """SRT 파일 하나를 원본 영상의 말소리와 대조한다 — SRT는 읽기만 하고 `scratch`에만 쓴다.

    SRT 시각은 영상 재생 시계(영상 원점 = 0)로 읽는다. 오디오 원점이 0이 아니면 그만큼을
    빼서 PCM 시계로 옮기고, 잰 어긋남이 그 원점과 같으면 'SRT가 오디오 표본 0 기준으로
    쓰였다'(원점을 빼먹었다)는 노랑 이유를 단다.
    """
    validate_sync_check_settings(settings)
    project = project or ProjectConfig()
    factory = session_factory or default_session_factory(project)
    source = _srt_source(media, srt)
    choice = _srt_pcm(media, scratch, audio_ordinal, settings)
    try:
        speech = speech_track(choice, source.source_id, scratch, project, factory)
    except SyncUnavailable as missing:
        verdict = sync_check.skipped_verdict((missing.reason,), choice.checks)
        return report_document(source, settings, verdict, None, choice, None)
    shift = choice.audio_start
    cues = tuple(replace(cue, start=cue.start - shift, end=cue.end - shift) for cue in source.cues)
    metrics = sync_check.measure(cues, speech, settings)
    verdict = sync_check.judge(
        metrics,
        settings,
        extra_checks=choice.checks,
        extra_reasons=origin_reasons(metrics, shift, settings),
    )
    return report_document(source, settings, verdict, metrics, choice, speech)


def _srt_source(media: Path, srt: Path) -> SyncSource:
    """SRT 하나의 재료 — 리비전은 SRT 바이트의 해시다(SRT는 읽기만 한다)."""
    body = srt.read_bytes()
    return SyncSource(
        source_id=media.stem,
        source_name=media.name,
        source_sha256="",
        original=media,
        original_note="",
        transcript_revision=hashlib.sha256(body).hexdigest(),
        cues=srt_cues(body.decode("utf-8-sig")),
        stage1=None,
    )


def _srt_pcm(
    media: Path, scratch: Path, audio_ordinal: int, settings: SyncCheckSettings
) -> PcmChoice:
    pcm_path = scratch / "audio.pcm"
    extraction = preprocess.extract_pcm_s16le(media, pcm_path, stream_index=audio_ordinal)
    try:
        presentation = preprocess.probe_extraction_clock(media).presentation_origin
    except RuntimeError as error:
        log.warn(f"자막 시각 확인 — 영상 원점을 못 재 0으로 둔다: {media.name} ({error})")
        presentation = Fraction(0)
    choice = PcmChoice(
        path=pcm_path,
        pcm_sha256=sha256_file(pcm_path),
        stream_index=extraction.stream.stream_index,
        channels=extraction.stream.channels,
        sample_count=extraction.sample_count,
        origin=extraction.pcm_sample0_file_time,
        presentation_origin=presentation,
        basis="decoded",
        checks=(),
    )
    return replace(choice, checks=(_origin_check(choice, None, settings),))


def origin_reasons(
    metrics: sync_check.SyncMetrics, audio_start: Fraction, settings: SyncCheckSettings
) -> tuple[Reason, ...]:
    """잰 어긋남이 오디오 원점과 같으면 — SRT가 오디오 표본 0 기준으로 쓰인 모양이다."""
    offset = metrics.offset
    if abs(audio_start) <= settings.origin_negligible_seconds:
        return ()
    if not offset.counted or offset.seconds is None:
        return ()
    if abs(offset.seconds - audio_start) > settings.origin_match_seconds:
        return ()
    text = (
        f"어긋남({float(offset.seconds):+.2f}초)이 오디오 원점({float(audio_start):+.3f}초)과 같음"
        " — SRT가 오디오 표본 0 기준으로 쓰였을 수 있음"
    )
    return (Reason("origin", sync_check.LEVEL_AMBER, text),)


# ─────────────────────────────────────────────────────────────
#  작업공간 — 모으기·할 일·작업 본문
# ─────────────────────────────────────────────────────────────


def gather_source(workspace: Any, asset: Any) -> SyncSource | None:
    """원본 하나의 재료 — 원본 자막이 없으면 잴 것도 없다(None)."""
    from vlogsrt.project.raw.models import RawWorkspaceError
    from vlogsrt.project.raw.source_transcript import load

    transcript = load(workspace, asset.asset_id)
    if transcript is None:
        return None
    original: Path | None = None
    note = ""
    try:
        original = workspace.source_media_path(asset)
        if original is not None and not original.is_file():
            original, note = None, f"원본 파일이 없음: {asset.source_name}"
    except (RawWorkspaceError, OSError) as error:
        original, note = None, str(error)
    sha = asset.content_sha256 or ""
    return SyncSource(
        source_id=asset.asset_id,
        source_name=asset.source_name,
        source_sha256=sha,
        original=original,
        original_note=note,
        transcript_revision=str(transcript["revision"]),
        cues=cues_of(transcript),
        stage1=stage1_audio(workspace.root, asset.asset_id, sha),
    )


def pending_sources(
    workspace: Any,
    settings: SyncCheckSettings = SYNC_CHECK_SETTINGS,
    source_ids: Sequence[str] | None = None,
    *,
    force: bool = False,
) -> dict[str, str]:
    """잴 원본 → 할 일 열쇠. 보고서가 없거나 재료가 바뀐 것만(`force`면 고른 것 전부)."""
    cache_root = workspace.root / SYNC_CACHE
    pending: dict[str, str] = {}
    for asset in workspace.list_source_assets():
        if source_ids is not None and asset.asset_id not in source_ids:
            continue
        source = gather_source(workspace, asset)
        if source is None:
            continue
        current = load_report(
            report_path(cache_root, source.source_id, source.transcript_revision, settings)
        )
        if force or not is_current(current, source, settings):
            pending[source.source_id] = state_key(source, settings)
    return pending


def run_checks(
    context: Any,
    workspace: Any,
    source_ids: Sequence[str],
    *,
    done: int = 0,
    settings: SyncCheckSettings = SYNC_CHECK_SETTINGS,
    project: ProjectConfig | None = None,
    session_factory: Callable[[], object] | None = None,
) -> None:
    """작업 본문 — 원본마다 재고 보고서만 쓴다. 한 원본의 실패는 측정 불가 보고서로 남는다.

    `done`은 같은 작업이 앞서 잰 수다 — 합쳐진 묶음의 진행은 거기서 이어진다.
    """
    project = project or ProjectConfig()
    factory = session_factory or default_session_factory(project)
    cache_root = workspace.root / SYNC_CACHE
    context.set_total(done + len(source_ids))
    for position, source_id in enumerate(source_ids, start=done):
        context.check_cancel()
        source = gather_source(workspace, workspace.source_asset(source_id))
        if source is not None:
            write_report(
                cache_root, _measured(source, cache_root, settings, project, factory), settings
            )
        context.set_progress(position + 1)


def _measured(
    source: SyncSource,
    cache_root: Path,
    settings: SyncCheckSettings,
    project: ProjectConfig,
    factory: Callable[[], object],
) -> dict[str, Any]:
    try:
        return measure_source(
            source,
            cache_root=cache_root,
            settings=settings,
            project=project,
            session_factory=factory,
        )
    except (OSError, RuntimeError, ValueError) as error:
        log.warn(f"자막 시각 확인 실패 — {source.source_name}: {error}")
        return error_document(source, settings, error)


def measure_workspace(
    workspace: Any, source_ids: Sequence[str] | None = None, *, write: bool = True
) -> list[dict[str, Any]]:
    """작업 없이 그 자리에서 잰다(CLI). `write=False`면 작업공간에 아무것도 쓰지 않는다 —
    VAD 칸·디코드·표본 대조는 끝나면 지워지는 임시 폴더에 둔다."""
    sources = [
        source
        for asset in workspace.list_source_assets()
        if source_ids is None or asset.asset_id in source_ids
        for source in (gather_source(workspace, asset),)
        if source is not None
    ]
    project = ProjectConfig()
    factory = default_session_factory(project)
    if write:
        cache_root = workspace.root / SYNC_CACHE
        documents = [
            _measured(s, cache_root, SYNC_CHECK_SETTINGS, project, factory) for s in sources
        ]
        for document in documents:
            write_report(cache_root, document, SYNC_CHECK_SETTINGS)
        return documents
    with tempfile.TemporaryDirectory(prefix="vlogsrt-sync-") as scratch:
        return [_measured(s, Path(scratch), SYNC_CHECK_SETTINGS, project, factory) for s in sources]


def current_report(
    workspace: Any, source_id: str, settings: SyncCheckSettings = SYNC_CHECK_SETTINGS
) -> dict[str, Any] | None:
    """지금 원본 자막 리비전의 보고서 — 없으면 가장 최근 것을 `stale`로. 읽기만 한다."""
    from vlogsrt.project.raw.source_transcript import load

    transcript = load(workspace, source_id)
    if transcript is None:
        return None
    cache_root = workspace.root / SYNC_CACHE
    exact = load_report(report_path(cache_root, source_id, str(transcript["revision"]), settings))
    if exact is not None:
        return {**exact, "stale": False}
    older = latest_report(cache_root, source_id)
    return None if older is None else {**older, "stale": True}


SYNC_FAILED_LABEL = "자막 시각 확인을 걸지 못함 (로그 참고)"
"""확인 작업을 걸지조차 못한 원본의 표지 — 측정 불가와 같은 회색이다(초록이 아니다)."""


def failure_view(reason: str, *, running: bool) -> dict[str, Any]:
    """'걸지 못함' 화면 값 — 이유를 싣고 「다시 재기」가 다시 건다."""
    return {
        "level": "unavailable",
        "label": SYNC_FAILED_LABEL,
        "tone": "grey",
        "stale": False,
        "running": running,
        "reasons": [reason],
        "checks": [],
        "cues": [],
    }


def view_of(document: Mapping[str, Any] | None, *, running: bool) -> dict[str, Any] | None:
    """화면용 요약 — 표지 글·색·이유·검사·표지 큐. 측정 불가는 회색이다(초록이 아니다)."""
    if document is None:
        if not running:
            return None
        return {"level": "pending", "label": "자막 시각 재는 중(무료)", "tone": "grey"}
    level = str(document.get("verdict", sync_check.LEVEL_SKIP))
    label, tone = badge(level)
    return {
        "level": level,
        "label": label,
        "tone": tone,
        "stale": bool(document.get("stale")),
        "running": running,
        "reasons": [str(item.get("text", "")) for item in document.get("reasons", [])],
        "checks": list(document.get("checks", [])),
        "cues": list(document.get("cues", [])),
    }
