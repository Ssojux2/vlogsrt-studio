"""소스 전사 어댑터 — 명시적 네임스페이스와 유일 소스 (P3-02).

소스 하나는 저장 PCM 참조(P2) 위에서 **레거시 엔진 그대로** 돈다: 조각 계획 →
1차 전사(P3-01 결과형) → 되풀이 재전사(주입된 PCM 커터) → 골격 → 큐 → 정제 →
소스 로컬 SRT. 어댑터가 새로 만드는 것은 조각의 좌표(정확한 샘플)와 실패의
상태뿐이다 — 시각도 글자도 whisper 응답에서 온다(규칙 1).

여기서 증명하는 것:

- 저장 PCM `[0, sample_count)`을 한 번도 빠뜨리거나 겹치지 않고 덮는 조각 계획,
  경계는 조용한 창(quiet_sample)에서 골라 exact 샘플로 잘린다.
- 재사용 신원은 analysis_key(내용)다 — 같은 내용의 반복 인스턴스는 한 번만 전사하고,
  unavailable 리소스는 한 푼도 쓰지 않고 그대로 흘러간다.
- 호출·응답 계약 위반(누락·중복·요청 밖·깨진 payload)은 조용한 성공이 아니라
  **실제 참조 신원을 가진 error 기록**이고, 완성물(SRT)은 나오지 않는다.
- 심판·검증 응답은 글자만 요구한다 — 명시적 문자열 text(빈 문자열 포함) 또는
  parse_text가 읽는 목록 형태. 시각은 요구하지도 지어내지도 않는다.

가짜 전사기·손으로 만든 참조만 쓴다. 네트워크도 ffmpeg도 없다.
"""

from __future__ import annotations

import array
import hashlib
import json
import wave
from collections.abc import Callable
from dataclasses import FrozenInstanceError, dataclass, field
from dataclasses import replace as data_replace
from fractions import Fraction
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import output, pipeline, refine, repair, transcribe
from vlogsrt.config import Config
from vlogsrt.models import (
    REPAIR_COLLAPSED,
    REPAIR_KEPT,
    REPAIR_REPLACED,
    Segment,
)
from vlogsrt.project import speech
from vlogsrt.project.media import ResolvedMedia
from vlogsrt.project.pcm import PcmReference, SampleRange
from vlogsrt.project.speech import (
    SourceSegment,
    SourceTrace,
    UnavailableSourceAudio,
    plan_source_ranges,
    transcribe_source,
    transcribe_unique_sources,
)
from vlogsrt.transcribe import (
    ARBITER_KIND,
    FIXTURE_KIND,
    RESCUE_KIND,
    ChunkCoverage,
    ChunkResponse,
    FixtureTranscriber,
    TranscriptionJob,
)

RATE = 16000
LOOP = "꼬불꼬불 꼬불하고 여기 안으로 들어갔다가"
CLEAN = "성당 앞을 지나서 골목으로 들어가면 작은 광장이 나옵니다"


# ─────────────────────────────────────────────────────────────
#  재료 — 패턴 PCM·손 참조·손 미디어·픽스처
# ─────────────────────────────────────────────────────────────


def _frame_value(frame: int, channel: int = 0) -> int:
    """프레임마다 다른 값 — 슬라이스가 남의 프레임을 실어 오면 바로 드러난다."""
    return ((frame * 3 + channel * 977) % 28001) - 14000


def _pattern_pcm(frames: int, channels: int = 1) -> bytes:
    samples = array.array("h")
    for frame in range(frames):
        for channel in range(channels):
            samples.append(_frame_value(frame, channel))
    return samples.tobytes()


def _level_pcm(frames: int, level: int) -> bytes:
    return array.array("h", [level] * frames).tobytes()


def _quiet_hole_pcm(frames: int, quiet_lo: int, quiet_hi: int) -> bytes:
    """온통 시끄러운 PCM에 무음 창 하나 — 자를 자리가 어디로 옮겨가는지 잰다."""
    values = [1000] * frames
    values[quiet_lo:quiet_hi] = [0] * (quiet_hi - quiet_lo)
    return array.array("h", values).tobytes()


def _reference(
    path: Path,
    pcm_bytes: bytes,
    *,
    source_id: str = "acid-1",
    stem: str = "demo",
    original_sha256: str = "a" * 64,
    origin: Fraction = Fraction(0),
    analysis_key: str = "key-1",
    channels: int = 1,
) -> PcmReference:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pcm_bytes)
    return PcmReference(
        source_id=source_id,
        original_stem=stem,
        original_sha256=original_sha256,
        path=path,
        pcm_sha256=hashlib.sha256(pcm_bytes).hexdigest(),
        analysis_key=analysis_key,
        pcm_sample0_file_time=origin,
        sample_rate=RATE,
        channels=channels,
        sample_count=len(pcm_bytes) // (2 * channels),
    )


def _media(
    *,
    resource_scoped_id: str = "doc-1/r-1",
    source_id: str = "acid-1",
    original_sha256: str = "a" * 64,
    has_audio: bool = True,
) -> ResolvedMedia:
    original = Path("/media/original.mov")
    return ResolvedMedia(
        resource_scoped_id=resource_scoped_id,
        asset_content_id=source_id,
        original=original,
        image_source=original,
        original_sha256=original_sha256,
        has_audio=has_audio,
        used_proxy=False,
    )


def _unavailable(
    *,
    resource_scoped_id: str = "doc-1/r-2",
    source_id: str = "acid-1",
    original_sha256: str = "a" * 64,
    status: str = "no_audio",
    reason_code: str = "declared_no_audio",
    reason: str = "리소스 hasAudio=0",
    variant: str = "unavailable_source_audio",
) -> UnavailableSourceAudio:
    return UnavailableSourceAudio(
        variant=variant,  # type: ignore[arg-type]
        resource_scoped_id=resource_scoped_id,
        source_id=source_id,
        original_sha256=original_sha256,
        status=status,  # type: ignore[arg-type]
        reason_code=reason_code,  # type: ignore[arg-type]
        reason=reason,
    )


def _seg(start: float, end: float, text: str) -> dict[str, Any]:
    return {"start": start, "end": end, "text": text}


def _record(index: int, offset: float, response: dict[str, Any]) -> dict[str, Any]:
    return {"index": index, "offset": offset, "response": response}


def _write_fixture(namespace: Path, stem: str, kind: str, records: list[dict[str, Any]]) -> Path:
    namespace.mkdir(parents=True, exist_ok=True)
    path = namespace / f"{stem}.{kind}.json"
    path.write_text(json.dumps({"chunks": records}, ensure_ascii=False), encoding="utf-8")
    return path


def _wav_frames(path: Path) -> tuple[tuple[int, int, int, int], bytes]:
    with wave.open(str(path), "rb") as handle:
        params = (
            handle.getnchannels(),
            handle.getsampwidth(),
            handle.getframerate(),
            handle.getnframes(),
        )
        raw = handle.readframes(handle.getnframes())
    return params, raw


@dataclass
class _Counting:
    """FixtureTranscriber를 싸서 호출 기록만 남긴다. 응답은 그대로 흘린다."""

    inner: FixtureTranscriber
    calls: list[tuple[str, tuple[int, ...], Path | None]] = field(default_factory=list)

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls.append((job.kind, tuple(chunk.index for chunk in job.chunks), job.namespace))
        return self.inner.transcribe(job, cfg)

    def kinds(self) -> list[str]:
        return [kind for kind, _chunks, _ns in self.calls]


@dataclass
class _Scripted:
    """종류별로 정해둔 응답을 만들어 주는 가짜 전사기 — 계약 위반 주입용."""

    calls: list[tuple[str, tuple[int, ...]]] = field(default_factory=list)
    whisper: Callable[[TranscriptionJob], list[ChunkResponse]] = field(default=lambda job: [])
    rescue: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None
    arbiter: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls.append((job.kind, tuple(chunk.index for chunk in job.chunks)))
        handlers: dict[str, Callable[[TranscriptionJob], list[ChunkResponse]] | None] = {
            FIXTURE_KIND: self.whisper,
            RESCUE_KIND: self.rescue,
            ARBITER_KIND: self.arbiter,
        }
        handler = handlers.get(job.kind)
        if handler is None:
            raise RuntimeError(f"예약하지 않은 {job.kind} 호출이다")
        return list(handler(job))


def _kinds(scripted: _Scripted) -> list[str]:
    return [kind for kind, _chunks in scripted.calls]


def _clean_setup(
    tmp_path: Path,
    *,
    source_id: str = "acid-1",
    analysis_key: str = "key-1",
    stem: str = "demo",
    frames: int = 320_000,
    origin: Fraction = Fraction(0),
    response: dict[str, Any] | None = None,
) -> tuple[ResolvedMedia, PcmReference, _Counting, Path, Path]:
    """되풀이 없는 20초 소스 — 초안 3장, 재전사 0회."""
    pcm = _pattern_pcm(frames)
    reference = _reference(
        tmp_path / "ref.pcm",
        pcm,
        source_id=source_id,
        stem=stem,
        analysis_key=analysis_key,
        origin=origin,
    )
    media = _media(source_id=source_id)
    artifacts = tmp_path / "artifacts"
    namespace = artifacts / source_id
    segments = {
        "segments": [
            _seg(0.5, 2.0, "안녕하세요"),
            _seg(3.0, 5.5, "오늘은 망원동에 왔습니다"),
            _seg(8.0, 10.0, "브이로그입니다"),
        ]
    }
    payload = segments if response is None else response
    _write_fixture(namespace, stem, FIXTURE_KIND, [_record(0, 0.0, payload)])
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    return media, reference, transcriber, artifacts, namespace


def _repeat_setup(
    tmp_path: Path,
    *,
    count: int,
    rescue_payload: dict[str, Any],
    arbiter_payload: dict[str, Any] | None,
    analysis_key: str = "key-1",
    extra_rescue_records: list[dict[str, Any]] | None = None,
) -> tuple[ResolvedMedia, PcmReference, _Counting, Path, Path, bytes]:
    """되풀이 하나(10초부터 1초씩)를 가진 70초 소스."""
    frames = 70 * RATE
    pcm = _pattern_pcm(frames)
    reference = _reference(tmp_path / "ref.pcm", pcm, analysis_key=analysis_key)
    media = _media()
    artifacts = tmp_path / "artifacts"
    namespace = artifacts / "acid-1"
    segments = [
        _seg(0.0, 2.0, "첫 문장입니다"),
        *(_seg(10.0 + index, 11.0 + index, LOOP) for index in range(count)),
        _seg(55.0, 57.0, "마지막 문장입니다"),
    ]
    _write_fixture(namespace, "demo", FIXTURE_KIND, [_record(0, 0.0, {"segments": segments})])
    rescue_records = [_record(0, 10.0, rescue_payload)]
    if extra_rescue_records:
        rescue_records.extend(extra_rescue_records)
    _write_fixture(namespace, "demo", RESCUE_KIND, rescue_records)
    if arbiter_payload is not None:
        _write_fixture(namespace, "demo", ARBITER_KIND, [_record(0, 10.0, arbiter_payload)])
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    scratch = namespace / "work" / analysis_key
    return media, reference, transcriber, artifacts, scratch, pcm


def _loop_rescue() -> dict[str, Any]:
    """회복에 실패하는 재전사 응답 — 여전히 되풀이다."""
    return {"segments": [_seg(index * 2.0, index * 2.0 + 1.8, LOOP) for index in range(12)]}


def _clean_rescue() -> dict[str, Any]:
    return {"segments": [_seg(0.0, 1.8, CLEAN)]}


# ─────────────────────────────────────────────────────────────
#  조각 계획 — 전체를 한 번씩만, 경계는 조용한 창에서
# ─────────────────────────────────────────────────────────────


def test_plan_covers_the_whole_reference_exactly_once(tmp_path: Path) -> None:
    """합이 정확히 sample_count이고 순서대로 겹치지 않는다(규칙 4)."""
    frames = 320_000
    reference = _reference(tmp_path / "ref.pcm", _pattern_pcm(frames))
    single = plan_source_ranges(reference, Config())
    assert single == (SampleRange(0, frames),)

    many = plan_source_ranges(reference, data_replace(Config(), chunk_max_seconds=5.0))
    assert len(many) == 4  # ceil(20초 / 5초)
    assert many[0].start == 0 and many[-1].end == frames
    assert all(later.start >= earlier.end for earlier, later in pairwise(many))
    assert sum(span.end - span.start for span in many) == frames


def test_plan_respects_the_byte_limit_too(tmp_path: Path) -> None:
    """바이트 한도가 이길 때도 조각 수는 그 한도를 따른다(D26·D27)."""
    frames = 64_000  # s16le 모노 — 128,000바이트
    reference = _reference(tmp_path / "ref.pcm", _pattern_pcm(frames))
    cfg = data_replace(Config(), chunk_max_bytes=32_000)
    ranges = plan_source_ranges(reference, cfg)
    assert len(ranges) == 4  # ceil(128,000 / 32,000)
    assert sum(span.end - span.start for span in ranges) == frames


def test_plan_moves_the_boundary_to_the_quietest_window(tmp_path: Path) -> None:
    """목표 경계 근처에서 가장 조용한 창으로 옮겨 자른다 — 없으면 목표 그대로."""
    frames = 64_000  # 4초
    # 1.5~1.6초(24000~25600프레임)에만 무음이 있다. 목표는 2.0초(32000).
    pcm = _quiet_hole_pcm(frames, 24_000, 25_600)
    reference = _reference(tmp_path / "ref.pcm", pcm)
    ranges = plan_source_ranges(reference, data_replace(Config(), chunk_max_seconds=2.0))
    # 무음 창 안에서 목표에 가장 가까운 시작: 25600 - 320(창 폭) = 25280
    assert ranges == (SampleRange(0, 25_280), SampleRange(25_280, frames))

    # 조용한 곳이 없으면(온통 같은 진폭) 목표 지점에서 잘린다.
    flat = _reference(tmp_path / "flat.pcm", _level_pcm(frames, 1000))
    cuts = plan_source_ranges(flat, data_replace(Config(), chunk_max_seconds=2.0))
    assert cuts == (SampleRange(0, 32_000), SampleRange(32_000, frames))


def test_plan_rejects_a_reference_with_no_frames(tmp_path: Path) -> None:
    reference = _reference(tmp_path / "empty.pcm", b"")
    with pytest.raises(ValueError, match="프레임|없"):
        plan_source_ranges(reference, Config())


# ─────────────────────────────────────────────────────────────
#  소스 하나의 전사 — 엔진 전체가 명시적 네임스페이스 안에서 돈다
# ─────────────────────────────────────────────────────────────


def test_a_clean_source_runs_the_engine_and_publishes_the_source_srt(
    tmp_path: Path,
) -> None:
    media, reference, transcriber, artifacts, namespace = _clean_setup(tmp_path)
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)

    assert isinstance(trace, SourceTrace)
    assert trace.variant == "source_trace"
    assert trace.status == "success"
    assert trace.source_id == media.asset_content_id == "acid-1"
    assert trace.original_stem == "demo"
    assert trace.analysis_key == reference.analysis_key
    assert trace.pcm_analysis_key == reference.analysis_key
    assert trace.pcm_sample0_file_time == Fraction(0)
    assert trace.error is None
    assert trace.model == Config().transcribe_model
    assert len(trace.config_sha256) == 64

    assert len(trace.draft) == 3
    first = trace.draft[0]
    assert isinstance(first, SourceSegment)
    assert first.audio_start == Fraction(1, 2)
    assert first.audio_end == Fraction(2)
    assert first.file_start == Fraction(1, 2)
    assert first.file_end == Fraction(2)
    assert first.text == "안녕하세요"
    ids = [record.segment_id for record in trace.draft]
    assert len(set(ids)) == 3 and all(len(value) == 64 for value in ids)

    assert len(trace.settled_spine) == 3
    assert trace.repairs == ()
    assert len(trace.cues) == 3
    assert trace.coverage == (ChunkCoverage(index=0, offset=0.0, duration=20.0, segment_count=3),)

    srt = namespace / "demo.SOURCE.srt"
    assert trace.source_srt_relative_path == "acid-1/demo.SOURCE.srt"
    assert srt.is_file()
    assert trace.source_srt_sha256 == hashlib.sha256(srt.read_bytes()).hexdigest()
    assert srt.read_text(encoding="utf-8") == output.write_srt(list(trace.cues))

    # 되풀이가 없으면 재전사는 한 푼도 쓰지 않는다. 모든 잡은 명시적 네임스페이스를 골랐다.
    assert transcriber.kinds() == [FIXTURE_KIND]
    assert all(ns == namespace for _kind, _chunks, ns in transcriber.calls)

    # 아무것도 artifact_dir 밖에 쓰지 않는다.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["artifacts", "ref.pcm"]


def test_a_nonzero_origin_is_added_exactly_once_for_file_times(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _namespace = _clean_setup(
        tmp_path, origin=Fraction(-1, 5)
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    first = trace.draft[0]
    assert trace.pcm_sample0_file_time == Fraction(-1, 5)
    assert first.audio_start == Fraction(1, 2)
    assert first.file_start == Fraction(1, 2) - Fraction(1, 5) == Fraction(3, 10)
    assert first.file_end == Fraction(2) - Fraction(1, 5) == Fraction(9, 5)


def test_initial_wavs_are_real_pcm_slices_with_snapped_metadata(
    tmp_path: Path,
) -> None:
    """조각은 진짜 P2 바이트 슬라이스고, 좌표는 실제 잘린 프레임에서 나온다."""
    frames = 64_000
    pcm = _quiet_hole_pcm(frames, 24_000, 25_600)
    reference = _reference(tmp_path / "ref.pcm", pcm)
    media = _media()
    artifacts = tmp_path / "artifacts"
    namespace = artifacts / "acid-1"
    payload = {"segments": [_seg(0.5, 2.0, "첫마디입니다"), _seg(3.0, 4.5, "둘째 마디입니다")]}
    _write_fixture(
        namespace,
        "demo",
        FIXTURE_KIND,
        [
            _record(0, 0.0, payload),
            _record(1, float(Fraction(25_280, RATE)), payload),
        ],
    )
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    cfg = data_replace(Config(), chunk_max_seconds=2.0)

    trace = transcribe_source(media, reference, cfg, transcriber, artifacts)

    assert trace.status == "success"
    # 덮개의 좌표는 잘린(snapped) 프레임 그 자체다 — 요청한 초가 아니다.
    assert [cover.offset for cover in trace.coverage] == [0.0, float(Fraction(25_280, RATE))]
    assert [cover.duration for cover in trace.coverage] == [
        float(Fraction(25_280, RATE)),
        float(Fraction(frames - 25_280, RATE)),
    ]
    scratch = namespace / "work" / "key-1"
    head, head_raw = _wav_frames(scratch / "initial-0000.wav")
    tail, tail_raw = _wav_frames(scratch / "initial-0001.wav")
    assert head == (1, 2, RATE, 25_280)
    assert tail == (1, 2, RATE, frames - 25_280)
    assert head_raw == pcm[: 25_280 * 2]
    assert tail_raw == pcm[25_280 * 2 :]


@pytest.mark.parametrize(
    "response",
    [
        pytest.param({"segments": []}, id="literal-empty"),
        pytest.param({"segments": [_seg(0.0, 1.0, "   ")]}, id="filtered-empty"),
    ],
)
def test_a_successful_empty_analysis_publishes_the_empty_srt(
    tmp_path: Path, response: dict[str, Any]
) -> None:
    """빈 성공은 결정적인 빈 SRT와 그 해시를 낸다 — 조용한 실패가 아니다."""
    media, reference, transcriber, artifacts, namespace = _clean_setup(tmp_path, response=response)
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "empty"
    assert trace.draft == () and trace.settled_spine == () and trace.cues == ()
    assert trace.repairs == ()
    srt = namespace / "demo.SOURCE.srt"
    assert srt.read_bytes() == b""
    assert trace.source_srt_relative_path == "acid-1/demo.SOURCE.srt"
    assert trace.source_srt_sha256 == hashlib.sha256(b"").hexdigest()
    assert transcriber.kinds() == [FIXTURE_KIND], "빈 초안에는 재전사가 없다"


def test_a_malformed_initial_payload_is_an_error_with_real_provenance(
    tmp_path: Path,
) -> None:
    media, reference, transcriber, artifacts, namespace = _clean_setup(tmp_path, response={})
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "error"
    assert trace.error is not None and "segments" in trace.error
    # 실제 참조의 신원은 그대로 남는다 — unavailable로 강등되지 않는다.
    assert trace.source_id == reference.source_id
    assert trace.original_stem == reference.original_stem
    assert trace.analysis_key == reference.analysis_key
    assert trace.pcm_analysis_key == reference.analysis_key
    assert trace.pcm_sample0_file_time == reference.pcm_sample0_file_time
    assert trace.source_srt_relative_path is None and trace.source_srt_sha256 is None
    assert not (namespace / "demo.SOURCE.srt").exists()
    assert trace.draft == () and trace.coverage == ()


def test_finite_zero_and_reversed_segments_are_retained(tmp_path: Path) -> None:
    """길이 0·뒤집힌 유한 세그먼트는 어댑터가 걸러지 않는다(규칙 9)."""
    response = {
        "segments": [
            _seg(1.0, 1.0, "점"),
            _seg(5.0, 4.5, "뒤집힘"),
            _seg(8.0, 10.0, "정상입니다"),
        ]
    }
    media, reference, transcriber, artifacts, _namespace = _clean_setup(tmp_path, response=response)
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "success"
    assert [record.text for record in trace.draft] == ["점", "뒤집힘", "정상입니다"]
    # 시각 접기는 레거시(parse_segments)의 몫이다 — 어댑터는 값을 그대로 옮긴다.
    assert trace.draft[0].audio_start == trace.draft[0].audio_end == Fraction(1)
    assert trace.draft[1].audio_start == Fraction(5)
    assert trace.draft[1].audio_end == Fraction(5)
    assert len({record.segment_id for record in trace.draft}) == 3


def test_the_engine_runs_in_order_with_one_polish_and_the_pcm_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """build_spine → settle → spine_to_cues → refine. polish은 refine 안에서 딱 한 번."""
    order: list[str] = []
    seen: dict[str, float] = {}
    polish_count = {"n": 0}

    real_build = transcribe.build_spine
    real_settle = repair.settle
    real_cues = pipeline.spine_to_cues
    real_refine = refine.refine
    real_polish = refine.polish_pass

    def build_spine_spy(responses: list) -> list[Segment]:
        order.append("build_spine")
        return real_build(responses)

    def settle_spy(draft, source, cfg, inner):
        order.append("settle")
        seen["source_duration"] = source.duration
        return real_settle(draft, source, cfg, inner)

    def cues_spy(spine):
        order.append("spine_to_cues")
        return real_cues(spine)

    def refine_spy(cues, cfg, duration=0.0):
        order.append("refine")
        seen["refine_duration"] = duration
        return real_refine(cues, cfg, duration)

    def polish_spy(cues, cfg):
        polish_count["n"] += 1
        return real_polish(cues, cfg)

    monkeypatch.setattr(transcribe, "build_spine", build_spine_spy)
    monkeypatch.setattr(repair, "settle", settle_spy)
    monkeypatch.setattr(pipeline, "spine_to_cues", cues_spy)
    monkeypatch.setattr(refine, "refine", refine_spy)
    monkeypatch.setattr(refine, "polish_pass", polish_spy)

    media, reference, transcriber, artifacts, _namespace = _clean_setup(tmp_path)
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)

    assert trace.status == "success"
    assert order == ["build_spine", "settle", "spine_to_cues", "refine"]
    assert polish_count["n"] == 1, "refine가 이미 polish을 부른다 — 두 번 부르면 안 된다"
    expected = float(Fraction(reference.sample_count, reference.sample_rate))
    assert seen["source_duration"] == expected == 20.0
    assert seen["refine_duration"] == expected


def test_ids_and_artifacts_are_deterministic_across_runs(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _namespace = _clean_setup(tmp_path)
    first = transcribe_source(media, reference, Config(), transcriber, artifacts)

    other = tmp_path / "other-artifacts"
    _write_fixture(
        other / "acid-1",
        "demo",
        FIXTURE_KIND,
        [
            _record(
                0,
                0.0,
                {
                    "segments": [
                        _seg(0.5, 2.0, "안녕하세요"),
                        _seg(3.0, 5.5, "오늘은 망원동에 왔습니다"),
                        _seg(8.0, 10.0, "브이로그입니다"),
                    ]
                },
            )
        ],
    )
    second_transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    second = transcribe_source(media, reference, Config(), second_transcriber, other)
    assert first == second
    assert first.draft == second.draft
    assert (artifacts / "acid-1" / "demo.SOURCE.srt").read_bytes() == (
        other / "acid-1" / "demo.SOURCE.srt"
    ).read_bytes()


def test_two_configs_leave_different_trace_fingerprints(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _namespace = _clean_setup(tmp_path)
    first = transcribe_source(media, reference, Config(), transcriber, artifacts)
    second_transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    second = transcribe_source(
        media,
        reference,
        data_replace(Config(), max_chars_per_line=20),
        second_transcriber,
        artifacts,
    )
    assert first.config_sha256 != second.config_sha256


# ─────────────────────────────────────────────────────────────
#  재전사 — 남은 PCM 커터, 유지·접기, 심판
# ─────────────────────────────────────────────────────────────


def test_a_recovered_repeat_is_replaced_using_retained_pcm(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, scratch, pcm = _repeat_setup(
        tmp_path, count=8, rescue_payload=_clean_rescue(), arbiter_payload=None
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)

    assert trace.status == "success"
    assert [record.outcome for record in trace.repairs] == [REPAIR_REPLACED]
    assert [record.text for record in trace.settled_spine] == [
        "첫 문장입니다",
        CLEAN,
        "마지막 문장입니다",
    ]
    # 회복하면 심판은 한 푼도 안 쓴다.
    assert transcriber.kinds() == [FIXTURE_KIND, RESCUE_KIND]

    # 중첩 재전사 WAV는 같은 저장 PCM의 실제 바이트 슬라이스다.
    repair_wav = scratch / "repair-0000.wav"
    params, raw = _wav_frames(repair_wav)
    assert params == (1, 2, RATE, 8 * RATE)  # 구간 10~18초
    assert raw == pcm[10 * RATE * 2 : 18 * RATE * 2]
    # 1차 조각은 재전사가 끝나도 그대로 있다.
    assert (scratch / "initial-0000.wav").is_file()


def test_a_short_unrecovered_repeat_is_kept(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _scratch, _pcm = _repeat_setup(
        tmp_path,
        count=8,
        rescue_payload=_loop_rescue(),
        arbiter_payload={"text": LOOP * 12},
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "success"
    assert [record.outcome for record in trace.repairs] == [REPAIR_KEPT]
    assert [record.text for record in trace.settled_spine] == [
        record.text for record in trace.draft
    ]
    assert transcriber.kinds() == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND]


def test_a_long_unrecovered_repeat_is_collapsed(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _scratch, _pcm = _repeat_setup(
        tmp_path,
        count=40,
        rescue_payload=_loop_rescue(),
        arbiter_payload={"text": LOOP * 12},
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "success"
    assert [record.outcome for record in trace.repairs] == [REPAIR_COLLAPSED]
    assert [record.text for record in trace.settled_spine] == [
        "첫 문장입니다",
        LOOP,
        "마지막 문장입니다",
    ]


def test_a_positive_arbiter_answer_buys_one_hotter_retry(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _scratch, _pcm = _repeat_setup(
        tmp_path, count=8, rescue_payload=_loop_rescue(), arbiter_payload={"text": CLEAN}
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert transcriber.kinds() == [
        FIXTURE_KIND,
        RESCUE_KIND,
        ARBITER_KIND,
        RESCUE_KIND,
    ]
    assert trace.repairs[0].arbiter_clean and trace.repairs[0].arbiter_answered
    assert trace.status == "success"


@pytest.mark.parametrize("text", ["", "   "], ids=["empty", "blank"])
def test_an_empty_arbiter_answer_is_valid_no_speech_not_an_error(tmp_path: Path, text: str) -> None:
    """빈 심판 답은 말이 없다는 증거다 — 계약 위반이 아니라 유지·접기 정책으로 흐른다."""
    media, reference, transcriber, artifacts, _scratch, _pcm = _repeat_setup(
        tmp_path, count=8, rescue_payload=_loop_rescue(), arbiter_payload={"text": text}
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert transcriber.kinds() == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND]
    assert trace.status == "success"
    assert trace.error is None
    assert trace.repairs[0].arbiter_answered and not trace.repairs[0].arbiter_clean


def test_the_arbiter_list_of_text_fallback_shape_is_valid(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _scratch, _pcm = _repeat_setup(
        tmp_path,
        count=8,
        rescue_payload=_loop_rescue(),
        arbiter_payload={"segments": [{"text": CLEAN}, {"text": "광장이 나옵니다"}]},
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "success"
    assert trace.error is None
    assert trace.repairs[0].arbiter_clean


def test_extra_namespace_records_outside_the_retry_subset_are_filtered(
    tmp_path: Path,
) -> None:
    """네임스페이스 픽스처의 부분집합 선택은 P3-01을 따른다 — 남는 기록은 결정적으로 걸러진다."""
    media, reference, transcriber, artifacts, _scratch, _pcm = _repeat_setup(
        tmp_path,
        count=8,
        rescue_payload=_clean_rescue(),
        arbiter_payload=None,
        extra_rescue_records=[_record(1, 42.0, _clean_rescue())],
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "success"
    assert [record.outcome for record in trace.repairs] == [REPAIR_REPLACED]


# ─────────────────────────────────────────────────────────────
#  운영 실패 포획 — 조용한 성공이 아니라 error 기록
# ─────────────────────────────────────────────────────────────


def _echo_whisper(job: TranscriptionJob) -> list[ChunkResponse]:
    segments = [
        _seg(0.0, 2.0, "첫 문장입니다"),
        *(_seg(10.0 + index, 11.0 + index, LOOP) for index in range(8)),
        _seg(55.0, 57.0, "마지막 문장입니다"),
    ]
    return [
        ChunkResponse(index=chunk.index, offset=chunk.offset, payload={"segments": segments})
        for chunk in job.chunks
    ]


def _scripted_repeat(
    tmp_path: Path, **kwargs: Any
) -> tuple[ResolvedMedia, PcmReference, _Scripted, Path, Path]:
    pcm = _pattern_pcm(70 * RATE)
    reference = _reference(tmp_path / "ref.pcm", pcm)
    scripted = _Scripted(whisper=_echo_whisper, **kwargs)
    artifacts = tmp_path / "artifacts"
    return _media(), reference, scripted, artifacts, artifacts / "acid-1"


def test_a_dead_rescue_call_is_captured_not_cached_as_success(
    tmp_path: Path,
) -> None:
    def dead_rescue(job: TranscriptionJob) -> list[ChunkResponse]:
        raise RuntimeError("API가 죽었다")

    media, reference, scripted, artifacts, namespace = _scripted_repeat(
        tmp_path, rescue=dead_rescue
    )
    trace = transcribe_source(media, reference, Config(), scripted, artifacts)
    assert trace.status == "error"
    assert trace.error is not None and "API가 죽었다" in trace.error
    # 이미 만들어진 초안·덮개 증거는 남는다 — 완성물은 나오지 않는다.
    assert len(trace.draft) == 10
    assert len(trace.coverage) == 1
    assert not (namespace / "demo.SOURCE.srt").exists()
    assert trace.source_srt_relative_path is None


def test_a_dead_arbiter_call_is_captured(tmp_path: Path) -> None:
    def dead_arbiter(job: TranscriptionJob) -> list[ChunkResponse]:
        raise RuntimeError("심판 모델이 죽었다")

    media, reference, scripted, artifacts, _namespace = _scripted_repeat(
        tmp_path, rescue=lambda job: _rescue_responses(job, _loop_rescue()), arbiter=dead_arbiter
    )
    trace = transcribe_source(media, reference, Config(), scripted, artifacts)
    assert trace.status == "error"
    assert trace.error is not None and "심판" in trace.error
    assert len(trace.draft) == 10


def _rescue_responses(job: TranscriptionJob, payload: dict[str, Any]) -> list[ChunkResponse]:
    return [
        ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
        for chunk in job.chunks
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({}, id="empty-object"),
        pytest.param({"text": 3}, id="non-string-text"),
        pytest.param({"text": None}, id="null-text"),
        pytest.param({"text": ["말"]}, id="list-text"),
        pytest.param({"segments": [{"text": 3}]}, id="segment-non-string-text"),
        pytest.param({"segments": [{"start": 1}]}, id="segment-missing-text"),
        pytest.param({"segments": "말"}, id="segments-not-a-list"),
        pytest.param(["문자열", "목록"], id="top-level-list"),
    ],
)
def test_a_malformed_arbiter_payload_is_a_captured_operational_error(
    tmp_path: Path, payload: Any
) -> None:
    media, reference, scripted, artifacts, namespace = _scripted_repeat(
        tmp_path,
        rescue=lambda job: _rescue_responses(job, _loop_rescue()),
        arbiter=lambda job: [ChunkResponse(index=0, offset=job.chunks[0].offset, payload=payload)],
    )
    trace = transcribe_source(media, reference, Config(), scripted, artifacts)
    assert trace.status == "error"
    assert trace.error is not None and "심판" in trace.error
    assert not (namespace / "demo.SOURCE.srt").exists()
    assert len(trace.draft) == 10


def test_a_failing_pcm_cutter_is_an_error_not_a_silent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """주입된 커터의 실패는 접히지 않고 올라온다 — 프로젝트 경로에서 성공은 영구 캐시다."""
    real_slice = speech.write_wav_slice

    def failing_slice(reference: PcmReference, samples: SampleRange, destination: Path) -> Path:
        if destination.name.startswith("repair"):
            raise RuntimeError("슬라이스가 죽었다")
        return real_slice(reference, samples, destination)

    monkeypatch.setattr(speech, "write_wav_slice", failing_slice)
    media, reference, transcriber, artifacts, namespace, _pcm = _repeat_setup(
        tmp_path, count=8, rescue_payload=_clean_rescue(), arbiter_payload=None
    )
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    assert trace.status == "error"
    assert trace.error is not None and "슬라이스가 죽었다" in trace.error
    assert len(trace.draft) == 10
    assert not (namespace / "demo.SOURCE.srt").exists()


def test_a_duplicate_rescue_response_is_rejected(tmp_path: Path) -> None:
    def duplicated(job: TranscriptionJob) -> list[ChunkResponse]:
        once = _rescue_responses(job, _loop_rescue())
        return [*once, *once]

    media, reference, scripted, artifacts, _namespace = _scripted_repeat(
        tmp_path, rescue=duplicated, arbiter=None
    )
    trace = transcribe_source(media, reference, Config(), scripted, artifacts)
    assert trace.status == "error"
    assert trace.error is not None and "둘 이상" in trace.error


def test_a_dead_arbiter_handler_is_still_the_only_extra_call(tmp_path: Path) -> None:
    """재전사 실패 뒤 심판 핸들러가 없으면 그 호출 실패까지 포획된다."""
    media, reference, scripted, artifacts, _namespace = _scripted_repeat(
        tmp_path, rescue=lambda job: _rescue_responses(job, _loop_rescue())
    )
    trace = transcribe_source(media, reference, Config(), scripted, artifacts)
    assert trace.status == "error"
    assert _kinds(scripted) == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND]


# ─────────────────────────────────────────────────────────────
#  1차 호출의 응답 덮개 — 누락·중복·요청 밖·오프셋 어긋남
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("responses", "match"),
    [
        pytest.param([], "응답이 돌아오지 않은", id="missing"),
        pytest.param("double", "둘 이상", id="duplicate"),
        pytest.param("extra", "요청하지 않은", id="unmatched"),
        pytest.param("wrong-offset", "오프셋", id="offset-mismatch"),
    ],
)
def test_broken_initial_coverage_is_rejected_before_any_spine(
    tmp_path: Path, responses: str, match: str
) -> None:
    pcm = _pattern_pcm(320_000)
    reference = _reference(tmp_path / "ref.pcm", pcm)
    good = [ChunkResponse(index=0, offset=0.0, payload={"segments": [_seg(0.5, 2.0, "안녕")]})]

    def handler(job: TranscriptionJob) -> list[ChunkResponse]:
        if responses == "double":
            return [*good, *good]
        if responses == "extra":
            return [
                *good,
                ChunkResponse(index=7, offset=99.0, payload={"segments": [_seg(0.0, 1.0, "남")]}),
            ]
        if responses == "wrong-offset":
            return [
                ChunkResponse(index=0, offset=99.0, payload={"segments": [_seg(0.5, 2.0, "안녕")]})
            ]
        return []

    scripted = _Scripted(whisper=handler)
    trace = transcribe_source(_media(), reference, Config(), scripted, tmp_path / "artifacts")
    assert trace.status == "error"
    assert trace.error is not None and match in trace.error
    assert trace.draft == () and trace.coverage == ()
    assert not (tmp_path / "artifacts" / "acid-1" / "demo.SOURCE.srt").exists()


def test_a_non_object_initial_payload_is_rejected(tmp_path: Path) -> None:
    scripted = _Scripted(whisper=lambda job: [ChunkResponse(index=0, offset=0.0, payload=["목록"])])
    reference = _reference(tmp_path / "ref.pcm", _pattern_pcm(320_000))
    trace = transcribe_source(_media(), reference, Config(), scripted, tmp_path / "artifacts")
    assert trace.status == "error"
    assert trace.error is not None and "JSON 객체" in trace.error


# ─────────────────────────────────────────────────────────────
#  유일 소스 목록 — analysis_key로 중복 제거, unavailable 무통과
# ─────────────────────────────────────────────────────────────


def test_repeated_sources_share_one_analysis_by_content(tmp_path: Path) -> None:
    pcm = _pattern_pcm(320_000)
    reference = _reference(tmp_path / "a.pcm", pcm, analysis_key="key-1")
    twin = _reference(tmp_path / "b.pcm", pcm, analysis_key="key-1")
    media = _media(resource_scoped_id="doc-1/r-1")
    twin_media = _media(resource_scoped_id="doc-1/r-9")
    artifacts = tmp_path / "artifacts"
    _write_fixture(
        artifacts / "acid-1",
        "demo",
        FIXTURE_KIND,
        [_record(0, 0.0, {"segments": [_seg(0.5, 2.0, "안녕하세요")]})],
    )
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))

    analyses = transcribe_unique_sources(
        [(media, reference), (twin_media, twin)], Config(), transcriber, artifacts
    )

    assert len(analyses) == 2
    assert isinstance(analyses[0], SourceTrace) and analyses[0] is analyses[1]
    assert transcriber.kinds() == [FIXTURE_KIND], "같은 내용은 한 번만 전사한다"


def test_same_stem_different_content_are_two_analyses(tmp_path: Path) -> None:
    pcm = _pattern_pcm(320_000)
    other = _pattern_pcm(319_999)
    reference = _reference(tmp_path / "a.pcm", pcm, analysis_key="key-1", original_sha256="a" * 64)
    twin = _reference(
        tmp_path / "b.pcm",
        other,
        source_id="acid-2",
        analysis_key="key-2",
        original_sha256="b" * 64,
    )
    artifacts = tmp_path / "artifacts"
    for source_id in ("acid-1", "acid-2"):
        _write_fixture(
            artifacts / source_id,
            "demo",
            FIXTURE_KIND,
            [_record(0, 0.0, {"segments": [_seg(0.5, 2.0, "안녕하세요")]})],
        )
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))

    analyses = transcribe_unique_sources(
        [
            (_media(source_id="acid-1", original_sha256="a" * 64), reference),
            (
                _media(
                    source_id="acid-2", original_sha256="b" * 64, resource_scoped_id="doc-1/r-2"
                ),
                twin,
            ),
        ],
        Config(),
        transcriber,
        artifacts,
    )

    assert analyses[0] is not analyses[1]
    assert analyses[0].analysis_key != analyses[1].analysis_key
    assert transcriber.kinds() == [FIXTURE_KIND, FIXTURE_KIND]


def test_an_unavailable_resource_passes_through_with_zero_calls(
    tmp_path: Path,
) -> None:
    pcm = _pattern_pcm(320_000)
    reference = _reference(tmp_path / "ref.pcm", pcm)
    missing = _unavailable()
    artifacts = tmp_path / "artifacts"
    _write_fixture(
        artifacts / "acid-1",
        "demo",
        FIXTURE_KIND,
        [_record(0, 0.0, {"segments": [_seg(0.5, 2.0, "안녕하세요")]})],
    )
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))

    analyses = transcribe_unique_sources(
        [(_media(), reference), (_media(resource_scoped_id="doc-1/r-2"), missing)],
        Config(),
        transcriber,
        artifacts,
    )

    assert isinstance(analyses[0], SourceTrace)
    assert analyses[1] is missing
    assert transcriber.kinds() == [FIXTURE_KIND]

    repeated = transcribe_unique_sources(
        [
            (_media(resource_scoped_id="doc-1/r-2"), missing),
            (_media(resource_scoped_id="doc-1/r-2"), missing),
        ],
        Config(),
        transcriber,
        artifacts,
    )
    assert repeated == (missing, missing)
    assert transcriber.kinds() == [FIXTURE_KIND], "unavailable은 한 푼도 안 쓴다"


def test_same_content_with_different_audio_dispositions_stay_distinct(
    tmp_path: Path,
) -> None:
    """같은 내용이라도 has_audio 삼태는 리소스마다 정확히 온다(P1-02)."""
    pcm = _pattern_pcm(320_000)
    reference = _reference(tmp_path / "ref.pcm", pcm)
    silent_media = _media(resource_scoped_id="doc-1/r-2", source_id="acid-1", has_audio=False)
    silent = _unavailable()
    artifacts = tmp_path / "artifacts"
    _write_fixture(
        artifacts / "acid-1",
        "demo",
        FIXTURE_KIND,
        [_record(0, 0.0, {"segments": [_seg(0.5, 2.0, "안녕하세요")]})],
    )
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))

    analyses = transcribe_unique_sources(
        [(_media(), reference), (silent_media, silent)],
        Config(),
        transcriber,
        artifacts,
    )

    assert isinstance(analyses[0], SourceTrace) and analyses[0].status == "success"
    assert analyses[1] is silent
    assert transcriber.kinds() == [FIXTURE_KIND]


# ─────────────────────────────────────────────────────────────
#  호출 앞 신원 검증 — 경로·순서·이름으로 짝을 묶지 않는다
# ─────────────────────────────────────────────────────────────


def test_an_identity_mismatch_rejects_the_batch_before_any_call(
    tmp_path: Path,
) -> None:
    pcm = _pattern_pcm(320_000)
    wrong_id = _reference(tmp_path / "b.pcm", pcm, source_id="acid-other")
    wrong_hash = _reference(tmp_path / "c.pcm", pcm, original_sha256="c" * 64)
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    artifacts = tmp_path / "artifacts"

    for second in (wrong_id, wrong_hash):
        with pytest.raises(ValueError, match="신원|해시"):
            transcribe_unique_sources([(_media(), second)], Config(), transcriber, artifacts)
    assert transcriber.calls == []

    with pytest.raises(ValueError, match="신원|해시"):
        transcribe_source(_media(), wrong_id, Config(), transcriber, artifacts)
    assert transcriber.calls == []


def test_an_unavailable_identity_mismatch_rejects_before_any_call(
    tmp_path: Path,
) -> None:
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    artifacts = tmp_path / "artifacts"
    mismatches = [
        _unavailable(resource_scoped_id="doc-1/r-9"),
        _unavailable(source_id="acid-other"),
        _unavailable(original_sha256="9" * 64),
    ]
    for missing in mismatches:
        with pytest.raises(ValueError, match="신원|해시"):
            transcribe_unique_sources(
                [(_media(resource_scoped_id="doc-1/r-2"), missing)],
                Config(),
                transcriber,
                artifacts,
            )
    assert transcriber.calls == []


@pytest.mark.parametrize(
    "item",
    [
        pytest.param(("only-media",), id="one-tuple"),
        pytest.param(("media", "reference", "extra"), id="three-tuple"),
        pytest.param("junk", id="not-a-tuple"),
        pytest.param((_media(), 42), id="unknown-second"),
    ],
)
def test_schema_broken_input_rejects_before_any_call(tmp_path: Path, item: Any) -> None:
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    with pytest.raises(ValueError, match="쌍|원소|미디어"):
        transcribe_unique_sources([item], Config(), transcriber, tmp_path / "artifacts")
    assert transcriber.calls == []


def test_a_reference_without_frames_rejects_before_any_call(tmp_path: Path) -> None:
    reference = _reference(tmp_path / "ref.pcm", b"")
    transcriber = _Counting(FixtureTranscriber(fixture_dir=tmp_path))
    with pytest.raises(ValueError, match="sample_count|프레임"):
        transcribe_source(_media(), reference, Config(), transcriber, tmp_path / "artifacts")
    assert transcriber.calls == []


# ─────────────────────────────────────────────────────────────
#  기록 불변 — 상태·변별자는 만드는 순간에 검사된다
# ─────────────────────────────────────────────────────────────


def test_trace_records_are_immutable(tmp_path: Path) -> None:
    media, reference, transcriber, artifacts, _namespace = _clean_setup(tmp_path)
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    with pytest.raises(FrozenInstanceError):
        trace.status = "error"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        trace.draft[0].text = "바꿈"  # type: ignore[misc]
    with pytest.raises(AttributeError):
        trace.draft.append(
            SourceSegment(
                "x", Fraction(0), Fraction(1), Fraction(0), Fraction(1), "x", 0.0, 0.0, 0.0
            )
        )  # type: ignore[attr-defined]
    missing = _unavailable()
    with pytest.raises(FrozenInstanceError):
        missing.reason = "바꿈"  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    [
        pytest.param({"variant": "unavailable_source_audio"}, id="wrong-variant"),
        pytest.param({"status": "ok"}, id="unknown-status"),
        pytest.param({"error": "말도 안 되는 조합"}, id="success-with-error"),
        pytest.param({"source_srt_relative_path": None}, id="success-without-srt"),
        pytest.param({"source_srt_sha256": None}, id="success-without-hash"),
    ],
)
def test_source_trace_rejects_inconsistent_records(tmp_path: Path, changes: dict[str, Any]) -> None:
    media, reference, transcriber, artifacts, _namespace = _clean_setup(tmp_path)
    trace = transcribe_source(media, reference, Config(), transcriber, artifacts)
    with pytest.raises(ValueError):
        data_replace(trace, **changes)

    error_trace = data_replace(
        trace,
        status="error",
        error="실패",
        source_srt_relative_path=None,
        source_srt_sha256=None,
    )
    assert error_trace.status == "error"
    with pytest.raises(ValueError):
        data_replace(error_trace, error=None)
    with pytest.raises(ValueError):
        data_replace(error_trace, source_srt_relative_path="acid-1/demo.SOURCE.srt")

    empty_trace = data_replace(
        trace,
        status="empty",
        error=None,
        draft=(),
        settled_spine=(),
        cues=(),
        source_srt_sha256=hashlib.sha256(b"").hexdigest(),
    )
    with pytest.raises(ValueError):
        data_replace(empty_trace, draft=trace.draft)


def test_empty_status_requires_no_draft_or_cues_on_construction() -> None:
    """빈 성공인데 초안이 남아 있으면 그 기록 자체가 무효다."""
    good = SourceTrace(
        variant="source_trace",
        source_id="acid-1",
        original_stem="demo",
        analysis_key="key-1",
        pcm_analysis_key="key-1",
        pcm_sample0_file_time=Fraction(0),
        status="empty",
        draft=(),
        settled_spine=(),
        repairs=(),
        cues=(),
        coverage=(),
        model="model",
        config_sha256="0" * 64,
        source_srt_relative_path="acid-1/demo.SOURCE.srt",
        source_srt_sha256=hashlib.sha256(b"").hexdigest(),
        error=None,
    )
    assert good.status == "empty"
    with pytest.raises(ValueError):
        data_replace(
            good,
            settled_spine=(
                SourceSegment(
                    "s", Fraction(0), Fraction(1), Fraction(0), Fraction(1), "x", 0.0, 0.0, 0.0
                ),
            ),
        )


@pytest.mark.parametrize(
    ("status", "reason_code", "ok"),
    [
        ("no_audio", "declared_no_audio", True),
        ("error", "stream_unavailable", True),
        ("error", "pcm_unavailable", True),
        ("no_audio", "stream_unavailable", False),
        ("no_audio", "pcm_unavailable", False),
        ("error", "declared_no_audio", False),
    ],
)
def test_unavailable_status_and_reason_must_agree(status: str, reason_code: str, ok: bool) -> None:
    if ok:
        record = _unavailable(status=status, reason_code=reason_code)
        assert record.status == status
    else:
        with pytest.raises(ValueError, match="status|reason"):
            _unavailable(status=status, reason_code=reason_code)


def test_unavailable_rejects_a_wrong_discriminator_and_blank_strings() -> None:
    with pytest.raises(ValueError, match="변별자"):
        _unavailable(variant="source_trace")
    with pytest.raises(ValueError, match="reason"):
        _unavailable(reason="")
    with pytest.raises(ValueError, match="source_id"):
        _unavailable(source_id="")


def test_source_trace_rejects_blank_identity_strings() -> None:
    with pytest.raises(ValueError, match="analysis_key"):
        SourceTrace(
            variant="source_trace",
            source_id="acid-1",
            original_stem="demo",
            analysis_key="",
            pcm_analysis_key="key-1",
            pcm_sample0_file_time=Fraction(0),
            status="error",
            draft=(),
            settled_spine=(),
            repairs=(),
            cues=(),
            coverage=(),
            model="model",
            config_sha256="0" * 64,
            source_srt_relative_path=None,
            source_srt_sha256=None,
            error="실패",
        )
