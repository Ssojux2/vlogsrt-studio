"""최종 영상 입력(A7-2) — FCPXML 없는 `FinalVideoInput`의 정본.

`docs/plans/2026-10-02-raw-video-browser-v2.md` §11.1·§11.6. 외부 편집기에서
나온 **완성 영상 파일 하나**가 최종 자막의 유일한 입력이다 — FCPXML·최종
타임라인 구조·원본 조각 사용 목록은 요구하지 않는다.

이 모듈이 세우는 계약:

- **신원은 해시다.** `final_video_revision = SHA-256(최종 영상 바이트)`. 같은
  파일명을 덮어쓴 새 영상은 해시가 다르므로 새 리비전이다(§11.1).
- **검증된 사본이 있어야 한다.** 사본은 바이트를 그대로 옮기고 해시로 검증한다.
  원본은 절대 손대지 않고, 등록 뒤 원본이 바뀌면 실패로 끝난다 — 낡은 신원으로
  오디오를 뽑는 길은 없다.
- **오디오는 잰 값이다.** 실제 선택 오디오 스트림을 `preprocess.extract_pcm_s16le`로
  16kHz s16le PCM으로 뽑아 원점(`pcm_sample0_file_time`)·샘플 수를 **실측**한다.
  컨테이너의 start_time 선언은 디코드 원점의 증거가 아니다.
- **시각 환산은 한 번이다.** `final_cue_time = audio_to_video_origin +
  actual_chunk_start_sample / extracted_sample_rate + provider_segment_time`(§11.6).
  유리수로 계산하고 SRT/VTT 직렬화에서만 밀리초로 반올림한다.

되풀이 재전사가 구간을 다시 자르는 커터도 여기 있다 — 저장 PCM의 샘플 좌표로
잘라 `repair.Source.cutter`에 얹는 모양으로 돌려준다.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from vlogsrt import preprocess
from vlogsrt.models import Chunk
from vlogsrt.project.raw.caption_config import FinalCaptionSettings, validate_final_caption_settings
from vlogsrt.project.raw.understanding import (
    PCM_SAMPLE_WIDTH,
    SampleSpan,
    SelectedAudio,
    select_audio,
    write_pcm_slice,
)

FINAL_VIDEO_MANIFEST_NAME = "final-video.json"
"""검증 사본 옆에 남는 신원 기록의 파일명(§11.1 — 리비전의 열쇠와 함께)."""

_VERIFIED_COPY_DIR = "final-video"


class FinalVideoError(RuntimeError):
    """최종 영상 등록이 계약을 어긴다 — 모든 갈래의 부모."""


class FinalVideoNoAudio(FinalVideoError):
    """최종 영상에 오디오 스트림이 아예 없다(§11.11 — `no_audio`는 별개 상태다).

    무발화(`confirmed_no_speech`)가 아니다. 오디오가 없는 영상은 전사 입력 자체가
    없으므로 실패가 아니라 **명시적으로 다른 상태**로 올라간다 — 빈 SRT 성공으로
    바꾸는 길은 없다.
    """


class AmbiguousAudioSelection(FinalVideoError):
    """오디오 트랙이 여럿인데 사용자가 어느 트랙/믹스인지 확정하지 않았다(§11.1).

    몰래 첫 트랙을 고르는 길은 없다 — 원본 편집 미디어의 트랙 구성은 바꾸지
    않고, 대신 선택을 요구한다.
    """


class FinalVideoCopyFailed(FinalVideoError):
    """검증 사본 만들기에 실패했다 — 반쪽 사본을 신원으로 삼는 길은 없다."""


class FinalVideoChanged(FinalVideoError):
    """등록 도중 원본이 바뀌었다 — 낡은 해시로는 어느 파일도 믿을 수 없다."""


class ChunkPlanningError(FinalVideoError):
    """청크 계획이 커버리지 계약을 어긴다(§11.6 — 전체 범위를 빈틈없이)."""


def _sha256_file(path: Path) -> str:
    """파일 내용의 SHA-256 — 스트리밍으로 읽어 큰 최종본도 자리를 다 쓰지 않게."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class FinalVideoInput:
    """최종 자막 실행의 불변 입력 — 신원·검증 사본·잰 오디오·영상 길이.

    `final_video_revision`은 원본 바이트의 SHA-256이고 사본 해시와 같아야 한다.
    `audio`는 선택된 실제 스트림의 잰 PCM 신원(`SelectedAudio` — 원점·샘플 수가
    실측값이다). `video_duration`은 최종 영상 컨테이너의 길이로, 큐가 최종 영상
    범위를 벗어났는지 보는 유일한 기준이다. 시각 환산은 `to_final_time` 하나뿐이다.
    """

    source_id: str
    final_video_revision: str
    original_path: Path
    verified_copy_path: Path
    verified_copy_sha256: str
    manifest_path: Path
    audio: SelectedAudio
    video_duration: float
    audio_stream_count: int

    @property
    def extent(self) -> Fraction:
        """선택 오디오의 정규화 시간축 길이 — `샘플 수 / 주파수`의 정확 유리수."""
        return self.audio.extent

    def to_final_time(self, normalized: Fraction | float) -> Fraction:
        """정규화 시각 → 최종 영상 시각. 원점 차이는 **한 번만** 반영한다(§11.6).

        `audio.pcm_sample0_file_time`이 오디오의 실측 디코드 원점이다 — 오디오가
        영상보다 늦게 시작하면 그 차이가 여기 들어가고, 다시 더하는 곳은 없다.
        """
        return self.audio.file_time(Fraction(normalized))

    def chunk_time(self, span: SampleSpan) -> Fraction:
        """청크 시작 샘플의 정규화 시각 — 청크 오프셋의 정확한 원천(§11.6)."""
        return Fraction(span.start, self.audio.sample_rate)


def register_final_video(
    source: Path, workdir: Path, *, source_id: str, audio_ordinal: int | None = None
) -> FinalVideoInput:
    """최종 영상을 등록한다 — 신원·검증 사본·선택 오디오·신원 기록을 한 번에.

    오디오 스트림이 하나뿐이면 `audio_ordinal`을 생략할 수 있다. 여럿이면
    생략할 수 없다(§11.1 — 사용자가 트랙/믹스를 확정한다). 오디오가 없으면
    `FinalVideoNoAudio`로 올라가며, 그것은 무발화와 다른 상태다(§11.11).
    """
    if not source.is_file():
        raise FinalVideoError(f"최종 영상 파일이 없다: {source}")
    if not source_id.strip():
        raise FinalVideoError("source_id가 비어 있다")
    revision = _sha256_file(source)
    stream_count = preprocess.probe_audio_stream_count(source)
    ordinal = _resolve_audio_ordinal(source, stream_count, audio_ordinal)
    copy_path = _verified_copy_into(source, workdir, revision)
    audio_workdir = workdir / "caption-audio"
    audio_workdir.mkdir(parents=True, exist_ok=True)
    audio = select_audio(copy_path, audio_workdir, source_id=source_id, audio_ordinal=ordinal)
    input_ = FinalVideoInput(
        source_id=source_id,
        final_video_revision=revision,
        original_path=source,
        verified_copy_path=copy_path,
        verified_copy_sha256=revision,
        manifest_path=copy_path.parent / FINAL_VIDEO_MANIFEST_NAME,
        audio=audio,
        video_duration=preprocess.probe_duration(copy_path),
        audio_stream_count=stream_count,
    )
    _write_manifest(input_.manifest_path, input_, ordinal)
    return input_


def _resolve_audio_ordinal(source: Path, stream_count: int, ordinal: int | None) -> int:
    """어느 오디오 스트림을 볼지 정한다 — 묵시적 선택은 단일 트랙에서만 허용된다."""
    if stream_count < 1:
        raise FinalVideoNoAudio(
            f"최종 영상에 오디오 스트림이 없다: {source} — no_audio 상태로 다룬다(§11.11)"
        )
    if ordinal is None:
        if stream_count != 1:
            raise AmbiguousAudioSelection(
                f"오디오 트랙이 {stream_count}개다 — 어느 트랙/믹스인지"
                " audio_ordinal로 확정해야 한다"
            )
        return 0
    if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 0:
        raise FinalVideoError(f"오디오 순번은 0 이상 정수여야 한다: {ordinal!r}")
    if ordinal >= stream_count:
        raise FinalVideoError(f"오디오 순번 {ordinal}이(가) 스트림 수 {stream_count}를 벗어난다")
    return ordinal


def _verified_copy_into(source: Path, workdir: Path, revision: str) -> Path:
    """검증 사본을 만들고 그 자리를 돌려준다 — 등록 뒤 원본 불변을 다시 본다."""
    copy_dir = workdir / _VERIFIED_COPY_DIR
    copy_dir.mkdir(parents=True, exist_ok=True)
    copy_path = copy_dir / f"{revision}{source.suffix or '.bin'}"
    _verified_copy(source, copy_path, revision)
    if _sha256_file(source) != revision:
        raise FinalVideoChanged(
            f"등록 도중 원본이 바뀌었다: {source} — 낡은 해시로는 등록을 마칠 수 없다"
        )
    return copy_path


def _verified_copy(source: Path, destination: Path, revision: str) -> None:
    """바이트를 그대로 옮기고 해시로 검증한다 — 원본은 읽기만 한다.

    같은 해시의 사본이 이미 있으면 다시 복사하지 않는다(§11.1 — 관리 중인 불변
    사본의 중복 복사 금지). 사본 해시가 어긋나면 그 사본은 폐기하고 실패한다.
    """
    if destination.is_file():
        if _sha256_file(destination) == revision:
            return
        raise FinalVideoCopyFailed(
            f"검증 사본이 이미 있으나 해시가 다르다: {destination} — 예전 실행의 잔해다"
        )
    temp = destination.with_name(destination.name + ".partial")
    temp.unlink(missing_ok=True)
    digest = hashlib.sha256()
    try:
        with source.open("rb") as reader, temp.open("wb") as writer:
            for block in iter(lambda: reader.read(1024 * 1024), b""):
                digest.update(block)
                writer.write(block)
        if digest.hexdigest() != revision:
            raise FinalVideoCopyFailed(
                f"검증 사본의 해시가 원본과 다르다: {destination} — 반쪽 사본은 신원이 못 된다"
            )
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def _write_manifest(manifest_path: Path, input_: FinalVideoInput, audio_ordinal: int) -> None:
    """신원 기록을 원자적으로 남긴다 — 리비전·사본·선택 오디오의 열쇠."""
    payload = {
        "schema": "final-video/v1",
        "source_id": input_.source_id,
        "final_video_revision": input_.final_video_revision,
        "verified_copy_sha256": input_.verified_copy_sha256,
        "audio_streams": input_.audio_stream_count,
        "audio_ordinal": audio_ordinal,
        "audio_stream_index": input_.audio.audio_stream_index,
        "pcm_sha256": input_.audio.pcm_sha256,
        "sample_rate": input_.audio.sample_rate,
        "channels": input_.audio.channels,
        "sample_count": input_.audio.sample_count,
        "pcm_sample0_file_time": str(input_.audio.pcm_sample0_file_time),
        "video_duration": input_.video_duration,
    }
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    temp = manifest_path.with_name(manifest_path.name + ".partial")
    try:
        temp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        os.replace(temp, manifest_path)
    finally:
        temp.unlink(missing_ok=True)


def require_unchanged(input_: FinalVideoInput) -> None:
    """실행 직전에 사본이 여전히 그 리비전인지 다시 본다.

    묶은 뒤 파일이 바뀌었으면 그 신원은 **낡은 신원**이다 — 낡은 신원의 응답을
    새 오디오에 붙이면 다른 파일을 들었다고 기록하게 된다.
    """
    if not input_.verified_copy_path.is_file():
        raise FinalVideoChanged(f"검증 사본이 없다: {input_.verified_copy_path}")
    if _sha256_file(input_.verified_copy_path) != input_.final_video_revision:
        raise FinalVideoChanged(
            f"검증 사본이 등록 때와 다르다: {input_.verified_copy_path} — 리비전이 바뀌었다"
        )


# ─────────────────────────────────────────────────────────────
#  청크 계획 — 선택 오디오 전체를 빈틈없이
# ─────────────────────────────────────────────────────────────


def plan_caption_chunks(
    input_: FinalVideoInput, settings: FinalCaptionSettings
) -> tuple[SampleSpan, ...]:
    """선택 오디오 전체를 빈틈없이 덮는 청크 계획(§11.6).

    상한은 시간(≤180초)과 바이트(API 안전 여유) 중 작은 쪽이고 마지막 청크는
    남은 만큼 짧다 — 늘리거나 붙이지 않는다. VAD를 이유로 구간을 빼지 않는다.
    문구·시간 두 경로가 **같은 이 계획**을 쓴다(§11.3 — 같은 오디오의 증명).
    """
    validate_final_caption_settings(settings)
    audio = input_.audio
    frame_bytes = PCM_SAMPLE_WIDTH * audio.channels
    seconds_bound = int(settings.chunk_max_seconds * audio.sample_rate)
    bytes_bound = settings.chunk_max_bytes // frame_bytes
    bound = min(seconds_bound, bytes_bound)
    if bound < 1:
        raise ChunkPlanningError(
            f"청크 상한이 한 프레임보다 작다 — 시간 {settings.chunk_max_seconds}초,"
            f" 바이트 {settings.chunk_max_bytes}, 프레임 {frame_bytes}바이트"
        )
    spans = [
        SampleSpan(start, min(start + bound, audio.sample_count))
        for start in range(0, audio.sample_count, bound)
    ]
    _require_coverage(spans, audio.sample_count)
    return tuple(spans)


def _require_coverage(spans: Sequence[SampleSpan], sample_count: int) -> None:
    """커버리지 불변 — 순서·빈틈·겹침 없음·전체 덮음(꼬리 포함)."""
    if not spans:
        raise ChunkPlanningError("청크 계획이 비었다 — 오디오가 없다")
    cursor = 0
    for position, span in enumerate(spans):
        if span.start != cursor or span.end <= span.start:
            raise ChunkPlanningError(
                f"청크 {position}이 커버리지를 어긴다: [{span.start}, {span.end}),"
                f" 기대 시작 {cursor}"
            )
        cursor = span.end
    if cursor != sample_count:
        raise ChunkPlanningError(
            f"청크 계획이 오디오 전체를 덮지 않는다: {cursor}/{sample_count} 샘플"
        )


def write_caption_chunk(input_: FinalVideoInput, span: SampleSpan, destination: Path) -> Path:
    """계획의 청크 하나를 WAV로 — 데이터 바이트는 저장 PCM 그대로다."""
    return write_pcm_slice(input_.audio, span, destination)


def caption_chunk(
    input_: FinalVideoInput, span: SampleSpan, position: int, wav_path: Path
) -> Chunk:
    """WAV를 엔진 `Chunk`로 — 오프셋은 청크 시작 샘플의 정확한 유리수(§11.6).

    `offset`이 곧 `actual_chunk_start_sample / extracted_sample_rate`다. 오프셋을
    가정한 청크 길이나 요청 순서로 만들지 않는다.
    """
    rate = input_.audio.sample_rate
    return Chunk(
        path=wav_path,
        offset=float(Fraction(span.start, rate)),
        duration=float(Fraction(span.end - span.start, rate)),
        index=position,
    )


# ─────────────────────────────────────────────────────────────
#  재전사 커터 — 되풀이 구간을 저장 PCM에서 다시 자른다
# ─────────────────────────────────────────────────────────────


def pcm_span_cutter(
    input_: FinalVideoInput, workdir: Path
) -> Callable[[Sequence[tuple[float, float]]], list[Chunk]]:
    """`repair.Source.cutter`에 얹을 커터 — 정규화 초 구간을 PCM 슬라이스로.

    되풀이 재전사가 다시 묻는 구간은 **저장 PCM의 같은 바이트**에서 자른다.
    구간은 세그먼트 경계(초)로 오는데, 좌표계가 정규화 시간축이므로
    `샘플 = 초 × 주파수`로 정확히 되돌린다. 조각 수가 구간 수와 다르면
    `repair`가 짝을 믿을 수 없어 그대로 멈춘다 — 그 검사는 되풀이 계층의 것이다.
    """
    rate = input_.audio.sample_rate
    count = input_.audio.sample_count

    def cut(spans: Sequence[tuple[float, float]]) -> list[Chunk]:
        workdir.mkdir(parents=True, exist_ok=True)
        chunks: list[Chunk] = []
        for position, (start, end) in enumerate(spans):
            first = max(0, min(count, round(start * rate)))
            last = max(first + 1 if count > first else first, min(count, round(end * rate)))
            if last <= first:
                raise ChunkPlanningError(f"재전사 구간이 오디오 밖이다: [{start}, {end})초")
            span = SampleSpan(first, last)
            wav = workdir / f"repair-{position:04d}.wav"
            write_pcm_slice(input_.audio, span, wav)
            chunks.append(caption_chunk(input_, span, position, wav))
        return chunks

    return cut
