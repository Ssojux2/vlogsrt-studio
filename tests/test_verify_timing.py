"""`make verify`의 자를 못 박는다.

이 도구는 "자막이 소리와 안 맞는다"는 신고를 받는 유일한 게이트인데, 조각 파일 이름이
어긋난 채로 오래 돌았고 그 상태가 **통과로 보였다**(D32). 그래서 여기서 검사하는 것은
계산만이 아니라 **건너뛴 것이 통과로 보이지 않는가**까지다.

오디오를 읽지 않는 순수 함수만 다룬다. ffmpeg도 API도 부르지 않는다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from vlogsrt.models import ChunkMark, Cue, Segment, Trace
from vlogsrt.preprocess import chunk_glob, chunk_template

ROOT = Path(__file__).resolve().parent.parent
API_FIXTURES = ROOT / "tests" / "fixtures" / "api"


def _load_script() -> ModuleType:
    """`scripts/`는 패키지가 아니라 경로로 불러온다."""
    path = ROOT / "scripts" / "verify_timing.py"
    spec = importlib.util.spec_from_file_location("verify_timing", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    # `@dataclass`가 `sys.modules[cls.__module__]`를 들여다본다. 먼저 등록해야 한다.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


vt = _load_script()


def seg(seg_id: int, start: float, end: float, no_speech: float = 0.0) -> Segment:
    return Segment(id=seg_id, start=start, end=end, text="말", no_speech_prob=no_speech)


def chained(start: float, count: int, step: float, seg_id: int = 0) -> list[Segment]:
    """시각이 붙은 세그먼트 사슬 — `다음.start == 앞.end`."""
    return [seg(seg_id + i, start + i * step, start + (i + 1) * step) for i in range(count)]


def spaced(start: float, count: int, step: float, gap: float, seg_id: int = 0) -> list[Segment]:
    """사이에 쉼이 있는 세그먼트 — 붙지 않았다."""
    return [
        seg(seg_id + i, start + i * (step + gap), start + i * (step + gap) + step)
        for i in range(count)
    ]


# ── 조각 파일 이름: 쓰는 쪽과 찾는 쪽이 같아야 한다 ──────────


def test_the_glob_finds_what_the_template_writes() -> None:
    """D32의 사고 그 자체. 예전에는 `chunk%04d`로 쓰고 `batch*`로 찾았다."""
    written = chunk_template("demo") % 3
    assert written == "demo.chunk0003.mp3"
    assert Path(written).match(chunk_glob("demo"))


def test_verify_timing_uses_the_shared_glob() -> None:
    body = (ROOT / "scripts" / "verify_timing.py").read_text(encoding="utf-8")
    assert "chunk_glob(stem)" in body
    assert "batch" not in body, "조각 이름을 여기서 따로 짓지 마라. preprocess가 정한다"


# ── 건너뛴 검사는 통과가 아니다 ──────────────────────────────


def test_a_skipped_check_is_labelled_skip_not_ok() -> None:
    check = vt.skip("청크 정렬", "조각 mp3가 없다")
    assert check.skipped
    assert check.label == "SKIP"


def test_missing_chunks_skip_instead_of_passing_silently() -> None:
    check = vt.check_chunks(Path("audio.mp3"), [], [], 100.0)
    assert check.skipped, "파일을 못 찾은 것이 OK로 보이면 안 된다"


def test_mismatched_chunk_count_skips_rather_than_pairing_wrong_audio() -> None:
    """짝이 어긋나면 남의 구간과 대조하게 된다. 그건 답이 아니라 소음이다."""
    check = vt.check_chunks(Path("audio.mp3"), [Path("a.mp3"), Path("b.mp3")], [0.0], 100.0)
    assert check.skipped


def test_missing_srt_skips(tmp_path: Path) -> None:
    assert vt.check_srt_coverage(tmp_path / "output.srt", 100.0).skipped


# ── 조각 경계 되짚기 ─────────────────────────────────────────


def test_marks_from_offsets_tile_the_whole_audio() -> None:
    """규칙 4 — 조각은 빈틈없이 [0, duration]을 덮는다. 되짚기도 그래야 한다."""
    marks = vt.marks_from_offsets([0.0, 180.0, 360.0], 500.0)
    assert [mark.offset for mark in marks] == [0.0, 180.0, 360.0]
    assert [mark.duration for mark in marks] == [180.0, 180.0, 140.0]
    assert marks[-1].end == 500.0


def test_marks_from_offsets_survives_unsorted_input() -> None:
    marks = vt.marks_from_offsets([180.0, 0.0], 360.0)
    assert [mark.offset for mark in marks] == [0.0, 180.0]


def test_marks_from_offsets_gives_up_quietly_without_duration() -> None:
    assert vt.marks_from_offsets([0.0, 10.0], 0.0) == []


# ── 시각 붙음 프로파일 ───────────────────────────────────────


def test_glue_profile_finds_the_tail_and_leaves_the_head_alone() -> None:
    """조각 앞은 재고 뒤는 붙인 상태 — 실측 픽스처 조각 1의 모양이다."""
    mark = ChunkMark(index=0, offset=0.0, duration=100.0)
    spine = spaced(0.0, 20, step=2.0, gap=0.5) + chained(60.0, 20, step=2.0, seg_id=100)
    bands = vt.glue_profile(spine, [mark])

    assert bands[0].rate == 0.0
    assert bands[-1].rate > 0.9


def test_glue_is_not_counted_across_a_chunk_boundary() -> None:
    """경계에서 이으면 경계 자체가 붙음으로 잡혀 우리가 찾는 신호를 가린다."""
    marks = [
        ChunkMark(index=0, offset=0.0, duration=10.0),
        ChunkMark(index=1, offset=10.0, duration=10.0),
    ]
    # 앞 조각의 마지막이 10.0에서 끝나고 뒤 조각의 처음이 10.0에서 시작한다.
    spine = [seg(0, 5.0, 10.0), seg(1, 10.0, 15.0)]
    assert sum(band.glued for band in vt.glue_profile(spine, marks)) == 0


def test_glue_rise_flags_the_degrading_chunk_only() -> None:
    marks = [
        ChunkMark(index=0, offset=0.0, duration=100.0),
        ChunkMark(index=1, offset=100.0, duration=100.0),
    ]
    degrading = spaced(0.0, 15, step=2.0, gap=0.5) + chained(70.0, 15, step=2.0, seg_id=100)
    healthy = spaced(100.0, 30, step=2.0, gap=0.6, seg_id=200)
    rises = {rise.index: rise for rise in vt.glue_rises(degrading + healthy, marks)}

    assert rises[0].rise > vt.GLUE_RISE_TOL
    assert rises[1].rise <= vt.GLUE_RISE_TOL


def test_a_chunk_with_too_few_segments_is_not_judged() -> None:
    """세그먼트가 몇 개뿐이면 비율이 우연에 흔들린다. 짐작을 판정으로 쓰지 않는다."""
    mark = ChunkMark(index=0, offset=0.0, duration=100.0)
    assert vt.glue_rises(chained(0.0, 4, step=2.0), [mark]) == []


def test_check_glue_skips_without_boundaries() -> None:
    assert vt.check_glue(list(chained(0.0, 20, 2.0)), []).skipped


def test_check_glue_fails_on_a_degrading_chunk() -> None:
    mark = ChunkMark(index=0, offset=0.0, duration=100.0)
    spine = spaced(0.0, 15, step=2.0, gap=0.5) + chained(70.0, 15, step=2.0, seg_id=100)
    check = vt.check_glue(spine, [mark])
    assert not check.passed and not check.skipped


def test_check_glue_passes_a_healthy_chunk() -> None:
    mark = ChunkMark(index=0, offset=0.0, duration=100.0)
    check = vt.check_glue(list(spaced(0.0, 30, step=2.0, gap=0.6)), [mark])
    assert check.passed and not check.skipped


# ── 실측 픽스처: 조각 1의 꼬리가 실제로 무너져 있다 ──────────


@pytest.mark.skipif(
    not (API_FIXTURES / "fr_day_1_2_fhd.whisper.json").is_file(),
    reason="녹화 픽스처가 없는 체크아웃",
)
def test_the_recorded_fixture_still_shows_the_tail_collapse() -> None:
    """D32의 근거를 표본으로 못 박는다.

    조각이 38분이던 시절의 응답이다. 골격 기준으로 조각 1은 머리 3.6% → 꼬리 48.6%로
    오르고(상승 0.449), 조각 0은 18.5% → 38.5%(상승 0.200)에 머문다. 검출기가 앞의 것을
    못 짚거나 뒤의 것을 짚으면 자가 무뎌진 것이다.
    """
    from vlogsrt.transcribe import build_spine, load_fixture_responses

    responses = load_fixture_responses(API_FIXTURES / "fr_day_1_2_fhd.whisper.json")
    meta = json.loads((API_FIXTURES / "fr_day_1_2_fhd.meta.json").read_text(encoding="utf-8"))
    spine = build_spine(responses)
    marks = vt.marks_from_offsets([r.offset for r in responses], float(meta["duration"]))

    rises = {rise.index: rise for rise in vt.glue_rises(spine, marks)}
    assert rises[1].head < 0.10
    assert rises[1].tail > 0.40
    assert rises[1].rise > vt.GLUE_RISE_TOL
    # 조각 0은 같은 회차인데도 상승이 임계값 아래다. 임계값이 길이 검사가 아니라는 뜻이다.
    assert rises[0].rise < vt.GLUE_RISE_TOL

    # 두 조각을 합친 프로파일도 조각 뒤로 갈수록 단조롭게 오른다.
    bands = vt.glue_profile(spine, marks)
    assert bands[0].rate < 0.10
    assert bands[-1].rate > 0.40


# ── 조판 지연: 조판 계층이 시각을 뒤로 미는가 (D33) ──────────


def lagged_trace(shift: float, count: int = 20) -> Trace:
    """세그먼트 사슬과, 그것을 `shift`만큼 옮겨 만든 큐."""
    spine = spaced(0.0, count, step=1.0, gap=1.0)
    cues = tuple(
        Cue(
            start=segment.start + shift,
            end=segment.end + shift,
            lines=(segment.text,),
            seg_ids=(segment.id,),
            index=position + 1,
        )
        for position, segment in enumerate(spine)
    )
    return Trace(source="x.mp4", duration=count * 2.0, spine=tuple(spine), cues=cues)


def test_check_cue_lag_fails_on_a_late_typeset() -> None:
    """실측 최빈값이 그대로 재현되면 실패해야 한다 — 이게 D33의 결함이다."""
    check = vt.check_cue_lag(lagged_trace(+0.14))
    assert not check.passed and not check.skipped


def test_check_cue_lag_passes_a_healthy_typeset() -> None:
    """lead_in만 도는 정상 조판은 음수라 통과한다."""
    check = vt.check_cue_lag(lagged_trace(-0.10))
    assert check.passed and not check.skipped


def test_check_cue_lag_tolerates_a_few_late_cues() -> None:
    """whisper 원본이 겹친 구간의 충돌 해소까지 실패로 세면 자가 너무 뾰족하다."""
    trace = lagged_trace(-0.10, count=20)
    late = replace(trace.cues[0], start=trace.cues[0].start + 0.24)
    check = vt.check_cue_lag(replace(trace, cues=(late, *trace.cues[1:])))
    assert check.passed and not check.skipped


def test_check_cue_lag_counts_only_the_first_cue_of_a_segment() -> None:
    """분할된 뒤쪽 조각은 늦는 게 맞다. 같이 세면 결함이 정상에 묻힌다."""
    trace = lagged_trace(-0.10, count=20)
    tail = replace(trace.cues[0], start=trace.cues[0].start + 5.0, index=99)
    check = vt.check_cue_lag(replace(trace, cues=(*trace.cues, tail)))
    assert check.passed, "같은 seg_id를 두 번 세면 안 된다"


def test_check_cue_lag_skips_without_cues() -> None:
    """건너뛴 것은 통과가 아니다(D32)."""
    assert vt.check_cue_lag(None).skipped
    spine = tuple(spaced(0.0, 5, step=1.0, gap=1.0))
    assert vt.check_cue_lag(Trace(source="x.mp4", duration=10.0, spine=spine)).skipped


@pytest.mark.skipif(
    not (API_FIXTURES / "fr_day_1_2_fhd.whisper.json").is_file(),
    reason="녹화 픽스처가 없는 체크아웃",
)
def test_the_recorded_fixture_is_typeset_without_lag() -> None:
    """D33의 근거를 표본으로 못 박는다.

    같은 응답을 `refine`에 그대로 태운다. 고치기 전에는 큐 1291개 중 355개(27.5%)가
    늦었고 **그중 223개가 정확히 `+0.14초`** 였다 — `lead_out`이 없는 무음을 만든 지문이다.
    고친 뒤에는 14개(1.1%)뿐이고 `+0.14` 무리가 사라진다.

    남는 세 장은 다른 기전이다. whisper가 0.02초짜리 세그먼트를 냈고, `min_gap`을
    양쪽에서 떼낼 수 없어 `resolve_overlaps`가 뒤 큐를 민다. 앞 큐의 시작을 더 당겨야
    풀리는데 그건 규칙 3의 `lead_in` 한도를 넘는다. 그래서 고치지 않는다.
    """
    from vlogsrt.config import Config
    from vlogsrt.pipeline import spine_to_cues
    from vlogsrt.refine import adjust_timing, merge_pass, polish_pass, refine, split_pass
    from vlogsrt.transcribe import build_spine, load_fixture_responses

    responses = load_fixture_responses(API_FIXTURES / "fr_day_1_2_fhd.whisper.json")
    meta = json.loads((API_FIXTURES / "fr_day_1_2_fhd.meta.json").read_text(encoding="utf-8"))
    spine = build_spine(responses)
    cfg = Config()
    duration = float(meta["duration"])
    cues = refine(spine_to_cues(spine), cfg, duration)

    check = vt.check_cue_lag(
        Trace(
            source="fr_day_1_2_fhd.mp4",
            duration=duration,
            spine=tuple(spine),
            cues=tuple(cues),
        )
    )
    assert check.passed and not check.skipped, check.detail

    # 가장 날카로운 자: **`adjust_timing`은 실측에서 단 한 장도 늦추지 않는다.**
    before = merge_pass(split_pass(polish_pass(spine_to_cues(spine), cfg), cfg), cfg)
    after = adjust_timing(before, cfg, duration)
    assert all(new.start <= old.start + 1e-9 for new, old in zip(after, before, strict=True)), (
        "lead_out이 다시 없는 무음을 만들고 있다"
    )

    starts = {segment.id: segment.start for segment in spine}
    seen: set[int] = set()
    lags = []
    for cue in cues:
        head = cue.seg_ids[0]
        if head in seen:
            continue
        seen.add(head)
        lags.append(cue.start - starts[head])
    # 고치기 전의 지문: `+0.14`가 큐의 17%(223장)였다. 무리가 남으면 안 된다.
    assert len([lag for lag in lags if abs(lag - 0.14) < 5e-3]) <= 5
    assert sum(1 for lag in lags if lag > vt.LAG_LATE_TOL_S) / len(lags) < 0.02


# ── 지연 탐색: 음의 지연도 보인다 ────────────────────────────


def test_best_lag_can_see_a_negative_lag() -> None:
    """예전 자는 `max(0, lag)` 때문에 **빠른 쪽** 어긋남을 못 봤다."""
    probe = [0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0]
    pad = 3
    reference = [0.0] * (pad - 2) + probe + [0.0] * (pad + 2)
    lag, corr = vt.best_lag(probe, reference, pad)

    assert corr > 0.99
    assert lag == pytest.approx(-2 * vt.HOP_MS / 1000)


def test_best_lag_does_not_reward_a_shorter_comparison() -> None:
    """참조가 오른쪽에서 잘려도 지연 0을 찾아야 한다.

    지연마다 비교 길이가 다르면 데이터가 적게 겹치는 지연이 더 높은 점수를 받는다.
    점수를 오디오가 아니라 길이가 정하게 된다(D32).
    """
    probe = [0.0, 0.1, 0.9, 0.2, 0.0, 0.7, 0.3, 0.0, 0.0, 0.5, 0.8, 0.1]
    pad = 4
    lag, corr = vt.best_lag(probe, [0.0] * pad + probe, pad)  # 오른쪽 여유가 없다

    assert lag == 0.0
    assert corr == pytest.approx(1.0)


def test_probe_window_gives_up_where_it_cannot_search_both_ways() -> None:
    """오디오 끝에 걸린 창은 없는 소리와 비교하게 된다. 짐작 대신 건너뛴다."""
    assert vt._probe_window(Path("a.mp3"), 100.0, Path("c.mp3"), 99.0, 0.0, 30.0) is None


# ── D55: 말소리 대조 — 빨강만 실패, 측정 불가는 SKIP(통과가 아니다) ──────────


def test_speech_sync_skips_without_a_video_or_srt(tmp_path: Path) -> None:
    srt = tmp_path / "output.srt"
    assert vt.check_speech_sync(None, srt).label == "SKIP"
    video = tmp_path / "video.mov"
    video.write_bytes(b"x")
    assert vt.check_speech_sync(video, srt).label == "SKIP"


@pytest.mark.parametrize(
    ("verdict", "label"),
    [("red", "FAIL"), ("amber", "OK  "), ("green", "OK  "), ("skip", "SKIP")],
)
def test_speech_sync_maps_the_product_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, label: str
) -> None:
    from vlogsrt.project.raw import sync_report

    video = tmp_path / "video.mov"
    video.write_bytes(b"x")
    srt = tmp_path / "output.srt"
    srt.write_text("1\n00:00:01,000 --> 00:00:02,000\n안녕\n", encoding="utf-8")
    before = srt.read_bytes()

    def measured(*_: object, **__: object) -> dict[str, object]:
        return {"verdict": verdict, "reasons": [{"text": "이유"}]}

    monkeypatch.setattr(sync_report, "measure_srt", measured)
    check = vt.check_speech_sync(video, srt)
    assert check.label == label
    assert srt.read_bytes() == before  # SRT는 읽기만 한다
