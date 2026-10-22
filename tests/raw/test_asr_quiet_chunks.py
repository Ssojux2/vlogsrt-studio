"""W-25d — 1단계 청크를 조용한 지점에서 자른다(D74).

유료 호출은 없다. 시험이 못 박는 것:

- 계획: 청크 수는 고정 계획과 같고(견적·승인 예산 그대로), 청크마다 상한 이하이며 `[0, 표본 수]`를
  빈틈없이 덮는다(규칙 4). 목표(균등 분할) ±찾는 폭 안에서 가장 조용한 칸의 가운데를 자르고, 고르게
  시끄러우면 목표 가까이를 자른다. 상한이 막으면 폭을 줄인다. 같은 PCM에는 같은 자리다.
- 고르기: 이 원본의 고정 계획 청크가 캐시에 하나라도 있으면 고정 계획 그대로(이미 산 전사를 다시
  사지 않는다). 없으면(처음 듣는 원본 · 다시 듣기로 차례가 오른 원본) 조용한 계획이다.
- 기록: 조용한 계획이면 1단계 기록에 계획 판이 실리고, 고정 계획이면 칸이 없다(옛 바이트 그대로).
- 교차 확인 견적: 다시 듣기를 기다리는 원본의 고정 계획 기록은 글자 캐시를 빼는 데 쓰지 않는다.
- 설정: 찾는 폭·칸은 원본 엔진의 `Config.cut_*` 그대로다.
"""

from __future__ import annotations

import hashlib
import json
from array import array
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from vlogsrt.config import Config
from vlogsrt.project.raw import analysis_workflow, cross_check
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.analysis_config import (
    ASR_CHUNK_CUT,
    AsrChunkCutSettings,
    RawUnderstandingSettings,
)
from vlogsrt.project.raw.caption_provider import parse_timing_response
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.understanding import SampleSpan, SelectedAudio

RATE = stage1.PCM_SAMPLE_RATE
SETTINGS = RawUnderstandingSettings()
BOUND = int(SETTINGS.asr_chunk_max_seconds) * RATE
BEFORE_SHUTDOWN = date(2026, 10, 2)


def audio_with(
    tmp_path: Path, seconds: float, quiet: tuple[tuple[float, float], ...] = ()
) -> SelectedAudio:
    """크기 1000의 고른 소리 위에 `quiet` 구간만 0인 합성 PCM."""
    count = int(seconds * RATE)
    samples = array("h", [1000, -1000]) * (count // 2) + array("h", [1000] * (count % 2))
    for start, end in quiet:
        for index in range(int(start * RATE), int(end * RATE)):
            samples[index] = 0
    pcm = tmp_path / "analysis.pcm"
    pcm.write_bytes(samples.tobytes())
    return SelectedAudio(
        source_id="source_test",
        source_path=tmp_path / "original.mp4",
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=pcm,
        pcm_sha256=hashlib.sha256(pcm.read_bytes()).hexdigest(),
        sample_rate=RATE,
        channels=1,
        sample_count=count,
        pcm_sample0_file_time=Fraction(0),
    )


def cuts_of(spans: tuple[SampleSpan, ...]) -> list[int]:
    return [span.start for span in spans[1:]]


def assert_covering(spans: tuple[SampleSpan, ...], audio: SelectedAudio) -> None:
    assert spans[0].start == 0 and spans[-1].end == audio.sample_count
    assert all(left.end == right.start for left, right in zip(spans, spans[1:], strict=False))
    assert all(0 < span.end - span.start <= BOUND for span in spans)


# ── 설정 ─────────────────────────────────────────────────────────────────────


def test_the_search_reach_and_hop_are_the_engine_cut_settings() -> None:
    cfg = Config()
    assert ASR_CHUNK_CUT.search_seconds == Fraction(str(cfg.cut_search_seconds))
    assert ASR_CHUNK_CUT.hop_ms == cfg.cut_quiet_window_ms
    assert AsrChunkCutSettings() == ASR_CHUNK_CUT


# ── 계획 ─────────────────────────────────────────────────────────────────────


def test_short_audio_is_one_chunk_like_the_fixed_plan(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 150.0)
    assert stage1.plan_quiet_chunks(audio, SETTINGS) == stage1.plan_audio_chunks(audio, SETTINGS)


def test_cuts_land_in_the_quiet_gaps_near_the_even_targets(tmp_path: Path) -> None:
    # 400초 → 3청크, 목표 133.33초·266.67초. 조용한 틈은 목표 ±5초 안에 있다.
    audio = audio_with(tmp_path, 400.0, quiet=((130.0, 130.4), (269.5, 270.0)))
    spans = stage1.plan_quiet_chunks(audio, SETTINGS)
    assert len(spans) == len(stage1.plan_audio_chunks(audio, SETTINGS)) == 3
    assert_covering(spans, audio)
    first, second = cuts_of(spans)
    assert 130.0 * RATE <= first < 130.4 * RATE
    assert 269.5 * RATE <= second < 270.0 * RATE


def test_evenly_loud_audio_cuts_next_to_the_target(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0)
    spans = stage1.plan_quiet_chunks(audio, SETTINGS)
    assert_covering(spans, audio)
    hop = ASR_CHUNK_CUT.hop_ms * RATE // 1000
    targets = [audio.sample_count * index // 3 for index in (1, 2)]
    assert all(
        abs(cut - target) <= hop for cut, target in zip(cuts_of(spans), targets, strict=True)
    )


def test_a_gap_outside_the_reach_is_not_used(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((120.0, 121.0),))  # 목표 133.33초에서 12초 앞
    first = cuts_of(stage1.plan_quiet_chunks(audio, SETTINGS))[0]
    assert abs(first - audio.sample_count // 3) <= ASR_CHUNK_CUT.hop_ms * RATE // 1000


def test_the_chunk_bound_narrows_the_search(tmp_path: Path) -> None:
    """정확히 상한 두 배면 자리는 상한 하나뿐이다 — 조용한 틈이 3초 앞이어도 상한을 넘지 않는다."""
    audio = audio_with(tmp_path, 360.0, quiet=((177.0, 177.5),))
    spans = stage1.plan_quiet_chunks(audio, SETTINGS)
    assert cuts_of(spans) == [BOUND]
    assert_covering(spans, audio)


def test_the_plan_is_deterministic(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((131.0, 131.2),))
    assert stage1.plan_quiet_chunks(audio, SETTINGS) == stage1.plan_quiet_chunks(audio, SETTINGS)


# ── 고르기 — 이미 산 전사는 고정 계획 그대로 ───────────────────────────────────


class Replies:
    """청크마다 빈 세그먼트를 돌려주는 시간 제공자 가짜."""

    def __init__(self) -> None:
        self.seen: list[tuple[int, int]] = []

    def timed_transcript(self, *, binding: Any, chunk: Any, settings: Any, **_: Any) -> Any:
        self.seen.append((binding.sample_start, binding.sample_end))
        return parse_timing_response(
            {"segments": [{"start": 0.0, "end": 1.0, "text": "말"}]},
            binding=binding,
            model=settings.models.timing_model,
            provider=settings.caption.timing_provider,
            chunk_duration=chunk.duration,
            chunk_offset=chunk.offset,
        )


def _heard(tmp_path: Path, audio: SelectedAudio, spans: Any, cache: Path, epoch: int = 0) -> Any:
    return stage1.transcribe_selected_audio(
        audio,
        spans,
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=Replies(),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        cache_dir=cache,
        asr_epoch=epoch,
    )


def _plan(tmp_path: Path, audio: SelectedAudio, cache: Path, epoch: int = 0) -> Any:
    return stage1.plan_heard_chunks(
        audio,
        SETTINGS,
        settings=RawWorkflowConfig(),
        provider=Replies(),
        workdir=tmp_path,
        cache_dir=cache,
        asr_epoch=epoch,
    )


def test_a_source_never_heard_gets_the_quiet_plan(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((131.0, 131.2),))
    plan = _plan(tmp_path, audio, tmp_path / "cache")
    assert plan.schema == stage1.CHUNK_PLAN_QUIET
    assert plan.spans == stage1.plan_quiet_chunks(audio, SETTINGS)


def test_a_source_already_heard_keeps_the_fixed_plan_and_buys_nothing(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((131.0, 131.2),))
    cache = tmp_path / "cache"
    fixed = stage1.plan_audio_chunks(audio, SETTINGS)
    _heard(tmp_path, audio, fixed, cache)
    plan = _plan(tmp_path, audio, cache)
    assert plan.schema == stage1.CHUNK_PLAN_FIXED and plan.spans == fixed


def test_a_partly_heard_source_keeps_the_fixed_plan(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0)
    cache = tmp_path / "cache"
    fixed = stage1.plan_audio_chunks(audio, SETTINGS)
    _heard(tmp_path, audio, fixed[:1], cache)
    assert _plan(tmp_path, audio, cache).schema == stage1.CHUNK_PLAN_FIXED


def test_relistening_moves_to_the_quiet_plan(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((131.0, 131.2),))
    cache = tmp_path / "cache"
    _heard(tmp_path, audio, stage1.plan_audio_chunks(audio, SETTINGS), cache)
    assert _plan(tmp_path, audio, cache, epoch=1).schema == stage1.CHUNK_PLAN_QUIET


def test_a_single_chunk_source_is_always_the_fixed_plan(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 100.0)
    assert _plan(tmp_path, audio, tmp_path / "cache").schema == stage1.CHUNK_PLAN_FIXED


def test_the_quiet_plan_hears_the_quiet_spans_and_the_cache_replays_them(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((131.0, 131.2),))
    cache = tmp_path / "cache"
    plan = _plan(tmp_path, audio, cache)
    _heard(tmp_path, audio, plan.spans, cache)
    again = _plan(tmp_path, audio, cache)
    assert (
        again == plan
    )  # 조용한 계획으로 산 청크는 고정 계획 캐시가 아니다 — 같은 계획을 다시 고른다


# ── 기록 ─────────────────────────────────────────────────────────────────────


def test_the_record_names_the_quiet_plan_and_rebuilds(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0, quiet=((131.0, 131.2),))
    evidence = stage1.transcribe_selected_audio(
        audio,
        stage1.plan_quiet_chunks(audio, SETTINGS),
        settings=RawWorkflowConfig(),
        understanding=SETTINGS,
        provider=Replies(),
        on=BEFORE_SHUTDOWN,
        request_id="run-1",
        workdir=tmp_path,
        chunk_plan=stage1.CHUNK_PLAN_QUIET,
    )
    document = {"asr": stage1.asr_evidence_json(evidence)}
    assert document["asr"]["chunk_plan"] == "asr-chunks/v2"
    rebuilt = analysis_workflow._asr_from_artifact(document)
    assert stage1.asr_evidence_json(rebuilt) == document["asr"]


def test_the_fixed_plan_record_has_no_plan_field(tmp_path: Path) -> None:
    audio = audio_with(tmp_path, 400.0)
    evidence = _heard(tmp_path, audio, stage1.plan_audio_chunks(audio, SETTINGS), tmp_path / "c")
    assert "chunk_plan" not in stage1.asr_evidence_json(evidence)


# ── 교차 확인 견적 ───────────────────────────────────────────────────────────


def _record(root: Path, chunk_plan: str | None) -> None:
    folder = root / "analysis" / "anal_1" / "000001_src"
    folder.mkdir(parents=True, exist_ok=True)
    asr: dict[str, Any] = {
        "sample_rate": RATE,
        "chunks": [
            {"chunk_index": 0, "sample_start": 0, "sample_end": BOUND, "wav_sha256": "1" * 64},
            {
                "chunk_index": 1,
                "sample_start": BOUND,
                "sample_end": BOUND + RATE,
                "wav_sha256": "2" * 64,
            },
        ],
    }
    if chunk_plan is not None:
        asr["chunk_plan"] = chunk_plan
    body = {"source_sha256": "f" * 64, "created_at": "2026-10-22T00:00:00+00:00", "asr": asr}
    (folder / "asr_outcome.json").write_text(json.dumps(body), encoding="utf-8")


@pytest.mark.parametrize(
    ("chunk_plan", "replanned", "usable"),
    [
        (None, False, True),  # 같은 차례 — 고정 계획 캐시가 있어 고정 계획 그대로다
        (None, True, False),  # 다시 듣기 대기 — 조용한 계획으로 다시 자르므로 옛 청크는 쓸 수 없다
        ("asr-chunks/v2", True, True),  # 조용한 계획은 같은 PCM에서 같은 자리다
    ],
)
def test_the_text_quote_skips_fixed_records_of_a_source_waiting_to_relisten(
    tmp_path: Path, chunk_plan: str | None, replanned: bool, usable: bool
) -> None:
    _record(tmp_path, chunk_plan)
    found = cross_check._recorded_chunks(tmp_path, "src", "f" * 64, replanned=replanned)
    assert (found is not None) == usable
