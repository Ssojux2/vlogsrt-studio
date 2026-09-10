"""남긴 경계 오디오만 다시 전사하는 실행기 (P4-03 계약).

계획("Boundary transcripts" ＋ P3 어댑터)의 정본:

- **입력은 저장 PCM의 남긴 프레임 그대로다.** 요청의 `sample_range`만큼 바이트를
  옮기고 그 밖의 프레임은 한 개도 안 들어온다 — 제거된 오디오의 덧붙임·문맥이
  없다. 재전사 조각도 같은 저장 PCM에서 남긴 범위 안으로 묶여 잘린다.
- **시계는 세 층이다.** 엔진과 정제는 경계 국소 시계(길이 = 남긴 프레임/주파수),
   큐는 국소 시각에 `Fraction(시작 프레임, 주파수)`를 **정확히 한 번** 더한
   디코드 오디오 국소 시계, 파일 시계의 원점은 파일/XML 배치 때 한 번 더한다.
   반올림은 SRT 직렬화까지 없다.
- **엔진은 P3 소스 분석과 같은 순서다.** 1차 전사 → 골격 → 재전사 → 불변 골격 →
   큐 → 정제(polish 포함, 딱 한 번). 되풀이 재전사는 남은 기본값 그대로 산다.
- **실패는 조용한 성공이 아니다.** 주입 커터·호출·응답 계약 위반은 타입 있는
   경계 error 기록이고 완성물은 나오지 않는다 — 원본 소스 큐는 그대로 남는다.
- **경계는 되풀이하지 않는다.** `cut_truncated`는 종결 메타데이터다 — 자기
   끝점 때문에 다시 경계 요청을 만들지 않지만, 되풀이 재전사와 정제는 평범하게
   받는다.

가짜 전사기·손 참조·패턴 PCM만 쓴다. 네트워크도 ffmpeg도 없다.
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
from pathlib import Path
from typing import Any

import pytest

from vlogsrt import pipeline, refine, repair, transcribe
from vlogsrt.config import Config
from vlogsrt.models import (
    REPAIR_COLLAPSED,
    REPAIR_KEPT,
    REPAIR_REPLACED,
    Cue,
    Segment,
)
from vlogsrt.project import boundary
from vlogsrt.project.boundary import (
    BoundaryRequest,
    BoundaryTranscript,
    transcribe_boundaries,
)
from vlogsrt.project.pcm import PcmReference, SampleRange
from vlogsrt.project.speech import SourceSegment, SourceTrace
from vlogsrt.transcribe import (
    ARBITER_KIND,
    FIXTURE_KIND,
    RESCUE_KIND,
    ChunkResponse,
    FixtureTranscriber,
    TranscriptionJob,
)

RATE = 16000
LOOP = "꼬불꼬불 꼬불하고 여기 안으로 들어갔다가"
CLEAN = "성당 앞을 지나서 골목으로 들어가면 작은 광장이 나옵니다"


# ─────────────────────────────────────────────────────────────
#  재료 — 패턴 PCM·손 참조·손 트레이스·손 요청·가짜 전사기
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
    analysis_key: str = "pcm-key-1",
    channels: int = 1,
    sample_rate: int = RATE,
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
        sample_rate=sample_rate,
        channels=channels,
        sample_count=len(pcm_bytes) // (2 * channels),
    )


def _trace(
    *,
    source_id: str = "acid-1",
    analysis_key: str = "source-key-1",
    pcm_analysis_key: str = "pcm-key-1",
    origin: Fraction = Fraction(0),
) -> SourceTrace:
    """손 트레이스 — 경계 실행기가 보는 신원(분석 키·PCM 키·원점)만 실었다."""
    return SourceTrace(
        variant="source_trace",
        source_id=source_id,
        original_stem="demo",
        analysis_key=analysis_key,
        pcm_analysis_key=pcm_analysis_key,
        pcm_sample0_file_time=origin,
        status="success",
        draft=(),
        settled_spine=(
            SourceSegment(
                segment_id=f"{source_id}-seg-0",
                audio_start=Fraction(1, 2),
                audio_end=Fraction(2),
                file_start=Fraction(1, 2) + origin,
                file_end=Fraction(2) + origin,
                text="원본 골격",
                compression_ratio=1.0,
                avg_logprob=-0.1,
                no_speech_prob=0.0,
            ),
        ),
        repairs=(),
        cues=(Cue(start=0.5, end=2.0, lines=("원본 자막",), seg_ids=(0,), index=1),),
        coverage=(),
        model="fixture-model",
        config_sha256="c" * 64,
        source_srt_relative_path=f"work/{source_id}/demo.SOURCE.srt",
        source_srt_sha256="d" * 64,
        error=None,
    )


def _request(
    *,
    request_id: str = "req-1",
    source_id: str = "acid-1",
    pcm_analysis_key: str = "pcm-key-1",
    start: int = 160_000,
    end: int = 288_000,
    origin: Fraction = Fraction(0),
) -> BoundaryRequest:
    """손 요청 — 시작·끝 프레임과 원점만 맞춘 P4-02 산출물의 모양이다."""
    return BoundaryRequest(
        request_id=request_id,
        slice_id="slice-1",
        instance_ids=("inst-1",),
        source_id=source_id,
        pcm_analysis_key=pcm_analysis_key,
        source_start=Fraction(start, RATE) + origin,
        source_end=Fraction(end, RATE) + origin,
        timeline_start=Fraction(0),  # 이 실행기가 보는 재료가 아니다(P4-04b)
        sample_range=SampleRange(start, end),
        source_segment_ids=("acid-1-seg-0",),
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
class _Scripted:
    """종류별로 정해둔 응답을 만들어 주는 가짜 전사기 — 잡 그대로를 기록한다."""

    calls: list[TranscriptionJob] = field(default_factory=list)
    whisper: Callable[[TranscriptionJob], list[ChunkResponse]] = field(default=lambda job: [])
    rescue: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None
    arbiter: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None

    def transcribe(self, job: TranscriptionJob, cfg: Config) -> list[ChunkResponse]:
        self.calls.append(job)
        handlers: dict[str, Callable[[TranscriptionJob], list[ChunkResponse]] | None] = {
            FIXTURE_KIND: self.whisper,
            RESCUE_KIND: self.rescue,
            ARBITER_KIND: self.arbiter,
        }
        handler = handlers.get(job.kind)
        if handler is None:
            raise RuntimeError(f"예약하지 않은 {job.kind} 호출이다")
        return list(handler(job))

    def kinds(self) -> list[str]:
        return [job.kind for job in self.calls]


def _echo(job: TranscriptionJob, payload: dict[str, Any]) -> list[ChunkResponse]:
    return [
        ChunkResponse(index=chunk.index, offset=chunk.offset, payload=payload)
        for chunk in job.chunks
    ]


def _clean_whisper(job: TranscriptionJob) -> list[ChunkResponse]:
    payload = {
        "segments": [
            _seg(0.5, 2.0, "안녕하세요"),
            _seg(3.0, 5.5, "오늘은 망원동에 왔습니다"),
            _seg(6.0, 7.5, "브이로그입니다"),
        ]
    }
    return _echo(job, payload)


def _clean_rescue() -> dict[str, Any]:
    return {"segments": [_seg(0.0, 1.8, CLEAN)]}


def _loop_rescue() -> dict[str, Any]:
    """회복에 실패하는 재전사 응답 — 여전히 되풀이다."""
    return {"segments": [_seg(index * 2.0, index * 2.0 + 1.8, LOOP) for index in range(12)]}


def _run(
    tmp_path: Path,
    reference: PcmReference,
    trace: SourceTrace,
    request: BoundaryRequest,
    cfg: Config,
    scripted: _Scripted,
) -> tuple[BoundaryTranscript, Path]:
    """요청 하나를 돌리고 (기록, 완성물 뿌리)를 돌려준다."""
    artifacts = tmp_path / "artifacts"
    (transcript,) = transcribe_boundaries([request], [reference], [trace], cfg, scripted, artifacts)
    return transcript, artifacts


def _world(
    tmp_path: Path,
    *,
    origin: Fraction = Fraction(0),
    stem: str = "demo",
    rng: tuple[int, int] = (160_000, 288_000),
    frames: int = 320_000,
    pcm: bytes | None = None,
    cfg: Config | None = None,
    whisper: Callable[[TranscriptionJob], list[ChunkResponse]] | None = None,
) -> tuple[PcmReference, SourceTrace, BoundaryRequest, _Scripted, Config, bytes]:
    """되풀이 없는 8초 경계 — 초안 3장, 재전사 0회가 기본이다."""
    stored = pcm if pcm is not None else _pattern_pcm(frames)
    reference = _reference(tmp_path / "ref.pcm", stored, stem=stem, origin=origin)
    trace = _trace(origin=origin)
    request = _request(start=rng[0], end=rng[1], origin=origin)
    scripted = _Scripted(whisper=whisper or _clean_whisper)
    return reference, trace, request, scripted, cfg or Config(), stored


def _repeat_world(
    tmp_path: Path,
    *,
    count: int,
    rescue_payload: dict[str, Any],
    arbiter_payload: dict[str, Any] | None,
    margin: float = 0.0,
    hotter_clean: bool = False,
) -> tuple[PcmReference, SourceTrace, BoundaryRequest, _Scripted, Config, bytes]:
    """경계 안에 되풀이 하나를 가진 세계 — 재전사가 실제로 돈다.

    되풀이 세그먼트는 국소 [2+i, 3+i)에 산다(margin=0이면 재전사 조각이 국소
    [2, 5)로 정확히 잘린다). `hotter_clean`이면 두 번째 재전사가 회복에 성공한다.
    """
    stored = _pattern_pcm(320_000)
    reference = _reference(tmp_path / "ref.pcm", stored)
    trace = _trace()
    request = _request()
    scripted = _Scripted(
        whisper=lambda job: _echo(
            job,
            {
                "segments": [
                    _seg(0.0, 1.0, "첫 마디입니다"),
                    *(_seg(2.0 + index, 3.0 + index, LOOP) for index in range(count)),
                    _seg(6.0, 7.0, "끝 마디입니다"),
                ]
            },
        ),
        rescue=_stateful_rescue(rescue_payload, hotter_clean),
        arbiter=_fixed_arbiter(arbiter_payload),
    )
    cfg = data_replace(Config(), repair_margin=margin)
    return reference, trace, request, scripted, cfg, stored


def _fixed_arbiter(
    payload: dict[str, Any] | None,
) -> Callable[[TranscriptionJob], list[ChunkResponse]] | None:
    if payload is None:
        return None
    return lambda job: [ChunkResponse(index=0, offset=job.chunks[0].offset, payload=payload)]


def _stateful_rescue(
    first: dict[str, Any], second_clean: bool
) -> Callable[[TranscriptionJob], list[ChunkResponse]]:
    """첫 재전사는 `first`를, 심판이 허락한 두 번째는 회복본을 준다."""
    attempts = {"n": 0}

    def rescue(job: TranscriptionJob) -> list[ChunkResponse]:
        attempts["n"] += 1
        payload = first if attempts["n"] == 1 or not second_clean else _clean_rescue()
        return _echo(job, payload)

    return rescue


def _collapse_world(
    tmp_path: Path, *, rescue_payload: dict[str, Any], arbiter_payload: dict[str, Any]
) -> tuple[PcmReference, SourceTrace, BoundaryRequest, _Scripted, Config, bytes]:
    """40초 경계 안의 긴 되풀이(38초) — 회복 실패 시 접히는 세계."""
    frames = 50 * RATE
    stored = _pattern_pcm(frames)
    reference = _reference(tmp_path / "ref.pcm", stored)
    trace = _trace()
    request = _request(start=80_000, end=720_000)  # 국소 40초
    scripted = _Scripted(
        whisper=lambda job: _echo(
            job,
            {
                "segments": [
                    _seg(0.0, 1.0, "첫 마디입니다"),
                    *(_seg(1.0 + index, 2.0 + index, LOOP) for index in range(38)),
                    _seg(39.0, 40.0, "끝 마디입니다"),
                ]
            },
        ),
        rescue=lambda job: _echo(job, rescue_payload),
        arbiter=_fixed_arbiter(arbiter_payload),
    )
    return reference, trace, request, scripted, Config(), stored


# ─────────────────────────────────────────────────────────────
#  입력 바이트 — 저장 PCM의 남긴 프레임 그대로, 밖은 한 프레임도 없다
# ─────────────────────────────────────────────────────────────


def test_success_transcribes_exactly_the_retained_samples(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, pcm = _world(tmp_path)
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    namespace = artifacts / "acid-1" / "boundary" / transcript.cache_key
    scratch = namespace / "work"
    params, raw = _wav_frames(scratch / "initial-0000.wav")
    assert params == (1, 2, RATE, 128_000)  # [160000, 288000) — 국소 8초
    assert raw == pcm[160_000 * 2 : 288_000 * 2]
    # 경계 바로 밖 프레임은 한 개도 들어오지 않는다 — 패턴 PCM이 프레임마다 다르므로
    # 바이트 동일성이 곧 배제의 증명이다.
    assert raw[:2] == pcm[160_000 * 2 : 160_000 * 2 + 2]
    assert raw[-2:] == pcm[288_000 * 2 - 2 : 288_000 * 2]
    assert len(raw) == 128_000 * 2
    # 스크래치는 명시적 네임스페이스의 휘발성 work/ 자식이고 완성물과 갈라진다.
    assert scratch.is_dir()
    assert (namespace / "demo.BOUNDARY.json").is_file()
    # 완성물은 완성물 뿌리 안에만 산다.
    assert sorted(path.name for path in tmp_path.iterdir()) == ["artifacts", "ref.pcm"]
    assert scripted.kinds() == [FIXTURE_KIND], "되풀이가 없으면 재전사는 한 푼도 안 쓴다"


def test_multi_chunk_plan_snaps_offsets_and_durations_to_actual_frames(
    tmp_path: Path,
) -> None:
    """4.5초 경계를 2초 한도로 3조각 — 경계는 무음 창(190000)에서, 좌표는 실제
    잘린 프레임에서 나온다. 국소 오프셋은 남긴 범위의 시작을 뺀 값이다."""
    pcm = _quiet_hole_pcm(320_000, 190_000, 191_600)
    reference, trace, request, scripted, cfg, _pcm = _world(
        tmp_path,
        rng=(160_000, 232_000),
        pcm=pcm,
        cfg=data_replace(Config(), chunk_max_seconds=2.0),
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    (job,) = scripted.calls
    chunks = sorted(job.chunks, key=lambda chunk: chunk.index)
    assert [(chunk.index, chunk.offset, chunk.duration) for chunk in chunks] == [
        (0, 0.0, float(Fraction(30_000, RATE))),
        (1, float(Fraction(30_000, RATE)), float(Fraction(18_000, RATE))),
        (2, float(Fraction(48_000, RATE)), float(Fraction(24_000, RATE))),
    ]
    scratch = artifacts / "acid-1" / "boundary" / transcript.cache_key / "work"
    head, head_raw = _wav_frames(scratch / "initial-0000.wav")
    middle, middle_raw = _wav_frames(scratch / "initial-0001.wav")
    tail, tail_raw = _wav_frames(scratch / "initial-0002.wav")
    assert head[:3] == middle[:3] == tail[:3] == (1, 2, RATE)
    assert head[3] == 30_000 and middle[3] == 18_000 and tail[3] == 24_000
    assert head_raw == pcm[320_000:380_000]
    assert middle_raw == pcm[380_000:416_000]
    assert tail_raw == pcm[416_000:464_000]


def test_namespace_is_asset_boundary_cache_key_with_the_reference_stem(
    tmp_path: Path,
) -> None:
    """1차·재전사·심판 잡이 모두 같은 명시적 네임스페이스와 참조의 stem을 쓴다."""
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path,
        count=3,
        rescue_payload=_loop_rescue(),
        arbiter_payload={"text": CLEAN},
        hotter_clean=True,
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    assert scripted.kinds() == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND, RESCUE_KIND]
    namespace = artifacts / "acid-1" / "boundary" / transcript.cache_key
    assert {job.namespace for job in scripted.calls} == {namespace}
    assert {job.stem for job in scripted.calls} == {"demo"}, "잡 stem은 참조의 original_stem이다"
    assert transcript.original_stem == reference.original_stem == "demo"


# ─────────────────────────────────────────────────────────────
#  연결 — 신원으로만 묶고 순서를 믿지 않는다
# ─────────────────────────────────────────────────────────────


def test_shuffled_request_reference_trace_orders_give_identical_transcripts(
    tmp_path: Path,
) -> None:
    """요청·참조·트레이스 순서를 제각각 섞어도 같은 기록이 같은 요청에 간다."""
    reference_a = _reference(tmp_path / "a.pcm", _pattern_pcm(320_000))
    reference_c = _reference(
        tmp_path / "c.pcm",
        _pattern_pcm(319_999),
        source_id="acid-3",
        original_sha256="b" * 64,
        analysis_key="pcm-key-3",
    )
    trace_a = _trace()
    trace_c = _trace(source_id="acid-3", analysis_key="source-key-3", pcm_analysis_key="pcm-key-3")
    requests = [
        _request(request_id="req-a", start=160_000, end=200_000),
        _request(
            request_id="req-c",
            start=160_000,
            end=288_000,
            source_id="acid-3",
            pcm_analysis_key="pcm-key-3",
        ),
        _request(request_id="req-b", start=200_000, end=288_000),
    ]

    artifacts = tmp_path / "artifacts"
    one = transcribe_boundaries(
        requests,
        [reference_a, reference_c],
        [trace_a, trace_c],
        Config(),
        _Scripted(whisper=_clean_whisper),
        artifacts,
    )
    shuffled = transcribe_boundaries(
        [requests[2], requests[0], requests[1]],
        [reference_c, reference_a],
        [trace_c, trace_a],
        Config(),
        _Scripted(whisper=_clean_whisper),
        artifacts / "shuffled",
    )

    assert [item.request_id for item in one] == ["req-a", "req-c", "req-b"], "요청 순서 그대로"
    by_id = {item.request_id: item for item in shuffled}
    assert set(by_id) == {"req-a", "req-c", "req-b"}
    for transcript in one:
        assert transcript == by_id[transcript.request_id]
    assert one[0].cache_key != one[1].cache_key, "다른 소스는 다른 키다"
    assert one[0].cache_key != one[2].cache_key, "다른 범위는 다른 키다"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda refs, traces: (refs, []), id="missing-trace"),
        pytest.param(lambda refs, traces: ([], traces), id="missing-reference"),
        pytest.param(
            lambda refs, traces: (
                [data_replace(refs[0], analysis_key="pcm-다른-분석")],
                traces,
            ),
            id="mismatched-pcm-key",
        ),
        pytest.param(lambda refs, traces: ([*refs, *refs], traces), id="duplicate-references"),
        pytest.param(lambda refs, traces: (refs, [*traces, *traces]), id="duplicate-traces"),
        pytest.param(
            lambda refs, traces: (
                refs,
                [data_replace(traces[0], pcm_sample0_file_time=Fraction(-1, 20))],
            ),
            id="origin-disagreement",
        ),
    ],
)
def test_broken_linkage_rejects_the_batch_before_any_call(
    tmp_path: Path, mutate: Callable[[list[PcmReference], list[SourceTrace]], tuple]
) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    references, traces = mutate([reference], [trace])
    with pytest.raises(ValueError, match="트레이스|참조|키|원점|둘 이상"):
        transcribe_boundaries([request], references, traces, cfg, scripted, tmp_path / "artifacts")
    assert scripted.calls == [], "깨진 연결은 호출 앞에서 죽는다"


@pytest.mark.parametrize(
    "request_over",
    [
        pytest.param(
            lambda request: data_replace(request, sample_range=SampleRange(160_000, 999_999)),
            id="range-beyond-stored",
        ),
        pytest.param(
            lambda request: data_replace(request, sample_range=SampleRange(288_000, 160_000)),
            id="reversed-range",
        ),
    ],
)
def test_broken_requests_reject_before_any_call(
    tmp_path: Path, request_over: Callable[[BoundaryRequest], BoundaryRequest]
) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    broken = request_over(request)
    with pytest.raises(ValueError, match="범위"):
        transcribe_boundaries([broken], [reference], [trace], cfg, scripted, tmp_path / "artifacts")
    assert scripted.calls == []


def test_duplicate_request_ids_reject_before_any_call(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    with pytest.raises(ValueError, match="중복"):
        transcribe_boundaries(
            [request, request], [reference], [trace], cfg, scripted, tmp_path / "artifacts"
        )
    assert scripted.calls == []


# ─────────────────────────────────────────────────────────────
#  캐시 신원 — 내용·정책·범위·설정이 키다. 경로와 stem은 아니다
# ─────────────────────────────────────────────────────────────


def test_cache_key_changes_for_every_identity_component(tmp_path: Path) -> None:
    """명세된 신원 구성요소마다 키가 달라진다 — 각각 다른 완성물 자리에 산다."""
    base_reference, trace, base_request, _scripted, cfg, _pcm = _world(tmp_path / "base")
    base, _artifacts = _run(
        tmp_path / "base",
        base_reference,
        trace,
        base_request,
        cfg,
        _Scripted(whisper=_clean_whisper),
    )

    changed = bytearray(_pattern_pcm(320_000))
    changed[0] ^= 1
    other_pcm_trace = data_replace(trace, pcm_analysis_key="pcm-other")
    other_pcm_request = data_replace(base_request, pcm_analysis_key="pcm-other")
    variants: list[tuple[str, PcmReference, SourceTrace, BoundaryRequest, Config]] = [
        (
            "original",
            data_replace(base_reference, original_sha256="b" * 64),
            trace,
            base_request,
            cfg,
        ),
        ("pcm", _reference(tmp_path / "v-pcm.pcm", bytes(changed)), trace, base_request, cfg),
        (
            "analysis",
            data_replace(base_reference, analysis_key="pcm-other"),
            other_pcm_trace,
            other_pcm_request,
            cfg,
        ),
        (
            "channels",
            _reference(tmp_path / "v-stereo.pcm", _pattern_pcm(320_000, channels=2), channels=2),
            trace,
            base_request,
            cfg,
        ),
        (
            "rate",
            _reference(tmp_path / "v-rate.pcm", _pattern_pcm(320_000), sample_rate=12_000),
            trace,
            base_request,
            cfg,
        ),
        (
            "range",
            base_reference,
            trace,
            data_replace(base_request, sample_range=SampleRange(160_000, 280_000)),
            cfg,
        ),
        (
            "trace",
            base_reference,
            data_replace(trace, analysis_key="source-key-2"),
            base_request,
            cfg,
        ),
        (
            "config",
            base_reference,
            trace,
            base_request,
            data_replace(Config(), max_chars_per_line=20),
        ),
    ]

    keys = {base.cache_key}
    for name, reference, item_trace, request, item_cfg in variants:
        transcript, artifacts = _run(
            tmp_path / name,
            reference,
            item_trace,
            request,
            item_cfg,
            _Scripted(whisper=_clean_whisper),
        )
        assert transcript.cache_key not in keys, f"{name} 구성요소가 키를 바꾸지 않았다"
        keys.add(transcript.cache_key)
        assert (artifacts / "acid-1" / "boundary" / transcript.cache_key).is_dir()
    assert len(keys) == len(variants) + 1


def test_cache_key_ignores_path_and_stem(tmp_path: Path) -> None:
    """같은 내용은 경로·stem이 달라도 같은 키를 얻는다 — stem은 이름 재료일 뿐이다."""
    pcm = _pattern_pcm(320_000)
    first_reference = _reference(tmp_path / "one.pcm", pcm, stem="demo")
    second_reference = _reference(tmp_path / "elsewhere" / "two.pcm", pcm, stem="other")
    trace = _trace()
    request = _request()
    artifacts = tmp_path / "artifacts"

    first = transcribe_boundaries(
        [request],
        [first_reference],
        [trace],
        Config(),
        _Scripted(whisper=_clean_whisper),
        artifacts,
    )[0]
    second = transcribe_boundaries(
        [request],
        [second_reference],
        [trace],
        Config(),
        _Scripted(whisper=_clean_whisper),
        artifacts,
    )[0]

    assert first.cache_key == second.cache_key
    namespace = artifacts / "acid-1" / "boundary" / first.cache_key
    assert (namespace / "demo.BOUNDARY.json").is_file()
    assert (namespace / "other.BOUNDARY.json").is_file(), "stem은 완성물 이름에만 쓰인다"


# ─────────────────────────────────────────────────────────────
#  엔진 — P3 소스 분석과 같은 순서, polish은 정확히 한 번
# ─────────────────────────────────────────────────────────────


def test_engine_runs_in_order_with_one_polish_and_the_exact_local_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1차 → 골격 → 재전사 → 불변 골격 → 큐 → 정제. 길이는 남긴 프레임 그대로다."""
    order: list[str] = []
    seen: dict[str, float | str] = {}
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
        seen["source_stem"] = source.stem
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

    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    assert order == ["build_spine", "settle", "spine_to_cues", "refine"]
    assert polish_count["n"] == 1, "refine가 이미 polish을 부른다 — 두 번 부르면 안 된다"
    assert (
        seen["source_duration"] == seen["refine_duration"] == float(Fraction(128_000, RATE)) == 8.0
    )
    assert seen["source_stem"] == "demo"


def test_recovered_repeat_is_replaced_from_the_retained_pcm(tmp_path: Path) -> None:
    """경계 안 되풀이도 재전사가 고친다 — 조각은 남긴 PCM의 실제 바이트 슬라이스다."""
    reference, trace, request, scripted, cfg, pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_clean_rescue(), arbiter_payload=None
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    assert [record.outcome for record in transcript.repairs] == [REPAIR_REPLACED]
    assert [record.text for record in transcript.settled_spine] == [
        "첫 마디입니다",
        CLEAN,
        "끝 마디입니다",
    ]
    assert scripted.kinds() == [FIXTURE_KIND, RESCUE_KIND]
    scratch = artifacts / "acid-1" / "boundary" / transcript.cache_key / "work"
    params, raw = _wav_frames(scratch / "repair-0000.wav")
    assert params == (1, 2, RATE, 48_000)  # 국소 [2, 5) — margin=0
    assert raw == pcm[192_000 * 2 : 240_000 * 2]
    assert (scratch / "initial-0000.wav").is_file(), "1차 조각은 그대로 남는다"


def test_nested_repair_cut_is_clamped_to_the_retained_boundary(tmp_path: Path) -> None:
    """되풀이 구간이 경계 밖으로 넘치면(응답의 마지막 세그먼트가 국소 9.0초까지
    간다) 남긴 범위로 묶인다 — 조각은 국소 [2, 8) 실측 프레임이지 요청한 7초
    원본이 아니다."""
    stored = _pattern_pcm(320_000)
    reference = _reference(tmp_path / "ref.pcm", stored)
    trace = _trace()
    request = _request()
    scripted = _Scripted(
        whisper=lambda job: _echo(
            job,
            {
                "segments": [
                    _seg(2.0, 3.0, LOOP),
                    _seg(3.0, 4.0, LOOP),
                    _seg(4.0, 9.0, LOOP),  # 국소 길이 8.0초를 넘는다 — 남긴 끝에서 묶인다
                ]
            },
        ),
        rescue=lambda job: _echo(job, _clean_rescue()),
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, Config(), scripted)

    assert transcript.status == "success"
    assert [record.outcome for record in transcript.repairs] == [REPAIR_REPLACED]
    scratch = artifacts / "acid-1" / "boundary" / transcript.cache_key / "work"
    params, raw = _wav_frames(scratch / "repair-0000.wav")
    assert params == (1, 2, RATE, 96_000)  # 국소 [2, 8) — 실측 프레임 96,000개
    assert raw == stored[192_000 * 2 : 288_000 * 2]
    (rescue_job,) = [job for job in scripted.calls if job.kind == RESCUE_KIND]
    (chunk,) = rescue_job.chunks
    assert chunk.offset == 2.0
    assert chunk.duration == float(Fraction(96_000, RATE)) == 6.0, "묶인 실측 길이다"


def test_short_unrecovered_repeat_is_kept(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_loop_rescue(), arbiter_payload={"text": LOOP * 12}
    )
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)
    assert transcript.status == "success"
    assert [record.outcome for record in transcript.repairs] == [REPAIR_KEPT]
    assert [record.text for record in transcript.settled_spine] == [
        record.text for record in transcript.draft
    ]
    assert scripted.kinds() == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND]


def test_long_unrecovered_repeat_is_collapsed(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _collapse_world(
        tmp_path, rescue_payload=_loop_rescue(), arbiter_payload={"text": LOOP * 12}
    )
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)
    assert transcript.status == "success"
    assert [record.outcome for record in transcript.repairs] == [REPAIR_COLLAPSED]
    assert [record.text for record in transcript.settled_spine] == [
        "첫 마디입니다",
        LOOP,
        "끝 마디입니다",
    ]


def test_a_positive_arbiter_answer_buys_one_hotter_retry(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path,
        count=3,
        rescue_payload=_loop_rescue(),
        arbiter_payload={"text": CLEAN},
        hotter_clean=True,
    )
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)
    assert scripted.kinds() == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND, RESCUE_KIND]
    assert transcript.repairs[0].arbiter_clean and transcript.repairs[0].arbiter_answered
    assert transcript.status == "success"


@pytest.mark.parametrize("text", ["", "   "], ids=["empty", "blank"])
def test_an_empty_arbiter_answer_is_valid_no_speech_not_an_error(tmp_path: Path, text: str) -> None:
    """빈 심판 답은 말이 없다는 증거다 — 계약 위반이 아니라 유지·접기 정책으로 흐른다."""
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_loop_rescue(), arbiter_payload={"text": text}
    )
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)
    assert scripted.kinds() == [FIXTURE_KIND, RESCUE_KIND, ARBITER_KIND]
    assert transcript.status == "success"
    assert transcript.error is None
    assert transcript.repairs[0].arbiter_answered and not transcript.repairs[0].arbiter_clean


def test_the_arbiter_list_of_text_fallback_shape_is_valid(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path,
        count=3,
        rescue_payload=_loop_rescue(),
        arbiter_payload={"segments": [{"text": CLEAN}, {"text": "광장이 나옵니다"}]},
    )
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)
    assert transcript.status == "success"
    assert transcript.error is None
    assert transcript.repairs[0].arbiter_clean


def test_reference_stem_fixture_namespace_reuse_and_retry_subset(tmp_path: Path) -> None:
    """네임스페이스 픽스처는 참조 stem으로 읽히고, 요청한 조각만 결정적으로 걸린다."""
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_clean_rescue(), arbiter_payload=None
    )
    artifacts = tmp_path / "artifacts"
    first = transcribe_boundaries([request], [reference], [trace], cfg, scripted, artifacts)[0]

    namespace = artifacts / "acid-1" / "boundary" / first.cache_key
    whisper_payload = {
        "segments": [
            _seg(0.0, 1.0, "첫 마디입니다"),
            *(_seg(2.0 + index, 3.0 + index, LOOP) for index in range(3)),
            _seg(6.0, 7.0, "끝 마디입니다"),
        ]
    }
    # 남는 기록(요청하지 않은 조각 번호)은 P3-01 선택이 결정적으로 걸러낸다.
    _write_fixture(
        namespace,
        "demo",
        FIXTURE_KIND,
        [_record(0, 0.0, whisper_payload), _record(1, 42.0, whisper_payload)],
    )
    _write_fixture(
        namespace,
        "demo",
        RESCUE_KIND,
        [_record(0, 2.0, _clean_rescue()), _record(1, 9.9, _loop_rescue())],
    )

    replay = transcribe_boundaries(
        [request],
        [reference],
        [trace],
        cfg,
        FixtureTranscriber(fixture_dir=tmp_path),
        artifacts,
    )[0]
    again = transcribe_boundaries(
        [request],
        [reference],
        [trace],
        cfg,
        FixtureTranscriber(fixture_dir=tmp_path),
        artifacts,
    )[0]

    assert replay == first == again, "같은 키의 같은 바이트는 재출판이 아니라 재사용이다"
    assert len(list(namespace.glob("*.BOUNDARY.json"))) == 1
    assert [job.kind for job in scripted.calls] == [FIXTURE_KIND, RESCUE_KIND]


# ─────────────────────────────────────────────────────────────
#  시계 — 국소 → 디코드 국소, 원점은 파일 배치 때 한 번
# ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", [Fraction(0), Fraction(8, 125), Fraction(-1, 20)])
def test_local_cues_shift_to_the_decoded_clock_without_the_origin(
    tmp_path: Path, origin: Fraction
) -> None:
    """큐는 국소 시각에 `Fraction(시작 프레임, 주파수)`만 한 번 더한다 — 원점은 아직
    아니다. 원점을 두 번 더하면 이 등식이 깨진다. lead는 0으로 둬서 정제가 시각을
    옮기지 않게 한다 — 시험하는 것은 환산 그 자체다."""
    cfg = data_replace(Config(), lead_in=0.0, lead_out=0.0)
    reference, trace, request, scripted, _cfg, _pcm = _world(tmp_path, origin=origin, cfg=cfg)
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    base = Fraction(160_000, RATE)  # 남긴 범위의 디코드 국소 시작 = 10초
    assert transcript.cues[0].start == base + Fraction(1, 2) == Fraction(21, 2)
    assert transcript.cues[0].end == base + Fraction(2) == 12
    assert transcript.cues[0].source_id == "acid-1"
    for cue in transcript.cues:
        assert base <= cue.start < cue.end <= base + Fraction(128_000, RATE)
        assert cue.segment_ids, "큐는 골격 역추적 가능해야 한다"


@pytest.mark.parametrize("origin", [Fraction(8, 125), Fraction(-1, 20)])
def test_segment_records_add_the_origin_exactly_once_for_file_times(
    tmp_path: Path, origin: Fraction
) -> None:
    """세그먼트 기록의 두 시계 — audio_*는 디코드 국소, file_*는 원점을 정확히 한 번."""
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path, origin=origin)
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    first = transcript.draft[0]
    assert isinstance(first, SourceSegment)
    assert first.audio_start == Fraction(21, 2)
    assert first.audio_end == Fraction(12)
    assert first.file_start == Fraction(21, 2) + origin
    assert first.file_end == Fraction(12) + origin
    assert transcript.settled_spine[0].file_start == Fraction(21, 2) + origin
    ids = [record.segment_id for record in transcript.draft]
    assert len(set(ids)) == 3 and all(len(value) == 64 for value in ids)


# ─────────────────────────────────────────────────────────────
#  상태 — 빈 성공은 명시적, 실패는 타입 있는 기록
# ─────────────────────────────────────────────────────────────


def test_an_explicitly_empty_response_is_a_typed_empty_transcript(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(
        tmp_path, whisper=lambda job: _echo(job, {"segments": []})
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "empty"
    assert transcript.draft == () and transcript.settled_spine == () and transcript.cues == ()
    assert transcript.repairs == () and transcript.error is None
    assert scripted.kinds() == [FIXTURE_KIND], "빈 초안에는 재전사가 없다"
    namespace = artifacts / "acid-1" / "boundary" / transcript.cache_key
    assert (namespace / "demo.BOUNDARY.json").is_file(), "빈 성공도 완결된 기록이다"


def test_a_whitespace_only_success_is_an_error_not_empty(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(
        tmp_path, whisper=lambda job: _echo(job, {"segments": [_seg(0.0, 1.0, "   ")]})
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "초안" in transcript.error
    assert transcript.draft == ()
    assert list((artifacts / "acid-1").rglob("*.BOUNDARY.json")) == []


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
def test_a_malformed_arbiter_payload_is_a_typed_boundary_error(
    tmp_path: Path, payload: Any
) -> None:
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_loop_rescue(), arbiter_payload=payload
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "심판" in transcript.error
    assert len(transcript.draft) == 5, "이미 만든 초안 증거는 그대로 남는다"
    assert list((artifacts / "acid-1").rglob("*.BOUNDARY.json")) == []


def test_a_dead_rescue_call_is_a_typed_error_with_partial_evidence(tmp_path: Path) -> None:
    def dead_rescue(job: TranscriptionJob) -> list[ChunkResponse]:
        raise RuntimeError("API가 죽었다")

    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload={}, arbiter_payload=None
    )
    scripted.rescue = dead_rescue
    before = trace.cues
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "API가 죽었다" in transcript.error
    assert len(transcript.draft) == 5
    assert list((artifacts / "acid-1").rglob("*.BOUNDARY.json")) == []
    assert trace.cues == before, "실패가 원본 소스 큐를 지우지 않는다"


def test_a_dead_arbiter_call_is_a_typed_error(tmp_path: Path) -> None:
    def dead_arbiter(job: TranscriptionJob) -> list[ChunkResponse]:
        raise RuntimeError("심판 모델이 죽었다")

    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_loop_rescue(), arbiter_payload=None
    )
    scripted.arbiter = dead_arbiter
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "심판" in transcript.error
    assert len(transcript.draft) == 5


def test_a_duplicate_rescue_response_is_rejected(tmp_path: Path) -> None:
    def duplicated(job: TranscriptionJob) -> list[ChunkResponse]:
        once = _echo(job, _loop_rescue())
        return [*once, *once]

    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload={}, arbiter_payload=None
    )
    scripted.rescue = duplicated
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "둘 이상" in transcript.error


@pytest.mark.parametrize(
    ("responses", "match"),
    [
        pytest.param("missing", "응답이 돌아오지 않은", id="missing"),
        pytest.param("double", "둘 이상", id="duplicate"),
        pytest.param("extra", "요청하지 않은", id="unmatched"),
        pytest.param("wrong-offset", "오프셋", id="offset-mismatch"),
        pytest.param("non-object", "JSON 객체", id="non-object-payload"),
    ],
)
def test_broken_initial_coverage_is_rejected_before_any_spine(
    tmp_path: Path, responses: str, match: str
) -> None:
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
        if responses == "non-object":
            return [ChunkResponse(index=0, offset=0.0, payload=["목록"])]
        return []

    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path, whisper=handler)
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and match in transcript.error
    assert transcript.draft == () and transcript.settled_spine == () and transcript.cues == ()
    assert list((artifacts / "acid-1").rglob("*.BOUNDARY.json")) == []


def test_a_failing_pcm_cutter_is_a_typed_error_not_a_silent_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """주입된 커터의 실패는 접히지 않고 올라온다 — 경계 경로의 성공은 영구 캐시다."""
    real_slice = boundary.write_wav_slice

    def failing_slice(reference: PcmReference, samples: SampleRange, destination: Path) -> Path:
        if destination.name.startswith("repair"):
            raise RuntimeError("슬라이스가 죽었다")
        return real_slice(reference, samples, destination)

    monkeypatch.setattr(boundary, "write_wav_slice", failing_slice)
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_clean_rescue(), arbiter_payload=None
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "슬라이스가 죽었다" in transcript.error
    assert len(transcript.draft) == 5
    assert list((artifacts / "acid-1").rglob("*.BOUNDARY.json")) == []


def test_a_cutter_count_mismatch_is_a_typed_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(boundary, "_repair_chunks", lambda *args: [])
    reference, trace, request, scripted, cfg, _pcm = _repeat_world(
        tmp_path, count=3, rescue_payload=_clean_rescue(), arbiter_payload=None
    )
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "error"
    assert transcript.error is not None and "커터" in transcript.error
    assert list((artifacts / "acid-1").rglob("*.BOUNDARY.json")) == []


def test_a_conflicting_completed_artifact_is_preserved_and_reported(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    transcript, artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)
    manifest = artifacts / "acid-1" / "boundary" / transcript.cache_key / "demo.BOUNDARY.json"
    foreign = "이미 있던 다른 완성물\n".encode()
    manifest.write_bytes(foreign)

    again, _artifacts = _run(
        tmp_path, reference, trace, request, cfg, _Scripted(whisper=_clean_whisper)
    )

    assert again.status == "error"
    assert again.error is not None and "보존" in again.error
    assert manifest.read_bytes() == foreign, "있는 완성물은 못 덮어쓴다"
    assert len(again.draft) == 3, "이미 만든 초안 증거는 그대로 남는다"


# ─────────────────────────────────────────────────────────────
#  종결 — cut_truncated는 재귀를 막는 메타데이터다
# ─────────────────────────────────────────────────────────────


def test_cut_truncated_is_terminal_and_still_repairs_and_refines(tmp_path: Path) -> None:
    """끝점에서 잘린 세그먼트 — 자기 끝점으로 다시 경계 요청을 만들지 않고, 되풀이
    재전사와 정제는 평범하게 받는다."""
    reference, trace, request, scripted, cfg, _pcm = _world(
        tmp_path,
        whisper=lambda job: _echo(
            job,
            {
                "segments": [
                    _seg(0.5, 2.0, "안녕하세요"),
                    _seg(3.0, 8.0, "끝까지 이어지는 마디입니다"),  # 국소 끝 8.0에서 잘린다
                ]
            },
        ),
    )
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.status == "success"
    assert transcript.cut_truncated is True, "종결 메타데이터는 기본 참이다"
    assert scripted.kinds() == [FIXTURE_KIND], "자기 끝점의 재귀 경계 요청은 없다"
    base = Fraction(160_000, RATE)
    assert transcript.cues[-1].end == base + 8 == Fraction(18), "정제는 국소 길이 그대로다"
    assert len(transcript.settled_spine) == 2


def test_records_are_frozen_and_consistent(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    with pytest.raises(FrozenInstanceError):
        transcript.status = "error"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        transcript.cues[0].text = "바꿈"  # type: ignore[misc]
    with pytest.raises(ValueError):
        data_replace(transcript, error="말도 안 되는 조합")  # 성공에 error는 못 온다
    with pytest.raises(ValueError):
        data_replace(transcript, status="empty")  # 큐가 남은 채 빈 성공은 못 온다
    error_like = data_replace(transcript, status="error", error="실패")
    with pytest.raises(ValueError):
        data_replace(error_like, error=None)  # error 상태에서 error 문장이 사라진다
    with pytest.raises(ValueError):
        data_replace(transcript, status="ok")  # 모르는 상태다


def test_transcript_carries_request_provenance(tmp_path: Path) -> None:
    reference, trace, request, scripted, cfg, _pcm = _world(tmp_path)
    transcript, _artifacts = _run(tmp_path, reference, trace, request, cfg, scripted)

    assert transcript.request_id == request.request_id
    assert transcript.source_id == request.source_id == reference.source_id
    assert transcript.pcm_analysis_key == request.pcm_analysis_key == reference.analysis_key
    assert transcript.sample_range == request.sample_range
    assert transcript.source_start == request.source_start == Fraction(10)
    assert transcript.source_end == request.source_end == Fraction(18)
    assert len(transcript.boundary_id) == 64 and len(transcript.cache_key) == 64
