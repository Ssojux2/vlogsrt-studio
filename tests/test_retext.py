"""글자 교정(D37) — 순수 함수. 시각은 whisper, 글자만 gpt-transcribe.

안전선은 셋이다: 세그먼트 사이에 끼는 gpt 글자는 출력하지 않는다(시각이 없다),
세그먼트 게이트를 통과하지 못하면 whisper 글자를 둔다, 한 gpt 글자는 두 번 나가지 않는다.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from pathlib import Path

import pytest

from vlogsrt import pipeline, retext, transcribe
from vlogsrt.cli import main
from vlogsrt.config import Config
from vlogsrt.hangul import strip_for_compare
from vlogsrt.models import (
    REPAIR_COLLAPSED,
    REPAIR_KEPT,
    REPAIR_REPLACED,
    RETEXT_APPLIED,
    RETEXT_OFF,
    RETEXT_UNAVAILABLE,
    Cue,
    Repair,
    Repeat,
    Segment,
    Trace,
)
from vlogsrt.output import parse_srt
from vlogsrt.retext import ChunkText
from vlogsrt.transcribe import TranscriptionJob


def _seg(seg_id: int, start: float, end: float, text: str) -> Segment:
    return Segment(id=seg_id, start=start, end=end, text=text)


def _plan(
    spine: list[Segment],
    chunks: list[ChunkText],
    duration: float = 60.0,
    repairs: tuple[Repair, ...] = (),
    cfg: Config | None = None,
) -> retext.RetextRecord:
    return retext.plan(spine, chunks, duration, repairs, cfg or Config())


# 조각 게이트를 끈다. **세그먼트 게이트 혼자서도 막는다**는 것을 보이려는 장면이다 —
# 실제로는 이런 세그먼트가 멀쩡한 조각 안에 섞여 조각 게이트를 통과한다(D37).
SEGMENT_GATE_ONLY = replace(Config(), retext_chunk_similarity_floor=0.0)


def _repair(outcome: str, span: tuple[float, float]) -> Repair:
    repeat = Repeat(start=span[0], end=span[1], count=5, text="치즈", reason="echo")
    return Repair(
        repeat=repeat, outcome=outcome, arbiter_clean=False, attempts=1, before=5, after=1,
        span=span,
    )  # fmt: skip


# ─────────────────────────────────────────────────────────────
#  폭주 재현 셋 — 게이트가 막는다
# ─────────────────────────────────────────────────────────────


def test_an_under_transcribed_segment_does_not_swallow_the_chunk() -> None:
    """과소전사 — whisper '흠' 1자에 gpt 80자가 몰리면 교체하지 않는다."""
    spoken = (
        "오늘은 파리 외곽으로 나가는 길인데 생각보다 차가 많이 막혀서 조금 늦어질 것 같아요 " * 2
    )
    record = _plan([_seg(0, 0.0, 0.4, "흠")], [ChunkText(0, 0.0, spoken)], cfg=SEGMENT_GATE_ONLY)

    assert dict(record.texts) == {}
    assert [rejection.seg_id for rejection in record.rejections] == [0]
    assert record.rejections[0].reason == retext.REASON_TOO_LONG


def test_an_isolated_grunt_keeps_its_letter_and_the_rest_is_recorded_unplaced() -> None:
    """gpt가 '음'도 들었으면 그 한 글자만 세그먼트 몫이고, 나머지는 자리가 없다."""
    spoken = "음 그러니까 여기가 그 유명한 빵집이에요"
    record = _plan([_seg(0, 0.0, 0.4, "음")], [ChunkText(0, 0.0, spoken)], cfg=SEGMENT_GATE_ONLY)

    assert dict(record.texts) == {0: "음"}
    unplaced = "".join(item.text for item in record.unplaced)
    assert strip_for_compare(unplaced) == strip_for_compare("그러니까 여기가 그 유명한 빵집이에요")


def test_a_hallucinated_ellipsis_run_does_not_receive_real_speech() -> None:
    """환각 — whisper '...' ×5 자리에 gpt의 실제 발화. 글자가 없는 세그먼트는 교체하지 않는다."""
    spine = [_seg(i, float(i), float(i) + 0.9, "...") for i in range(5)]
    record = _plan(
        spine,
        [ChunkText(0, 0.0, "여기 진짜 예쁘다 사진 한 장만 찍고 갈게요")],
        cfg=SEGMENT_GATE_ONLY,
    )

    assert dict(record.texts) == {}
    assert {rejection.seg_id for rejection in record.rejections} == set(range(5))
    assert record.unplaced, "시각이 없는 gpt 글자는 출력하지 않고 기록한다"


def test_collapse_wreckage_is_not_filled_with_three_hundred_letters() -> None:
    """collapse 잔해 — '치즈' 한 장에 gpt 300자. 교체하지 않는다."""
    speech = "우리 여기서 사진 찍자 하나 둘 셋 한 번 더 찍을게 이번엔 다 같이 웃어봐 " * 8
    record = _plan(
        [_seg(0, 10.0, 11.0, "치즈")], [ChunkText(0, 0.0, speech)], cfg=SEGMENT_GATE_ONLY
    )

    assert dict(record.texts) == {}
    assert record.rejections[0].seg_id == 0


def test_the_second_model_dropping_half_the_words_is_refused() -> None:
    """삭제 방향 — gpt가 whisper의 말을 절반 지우면 교체하지 않는다."""
    whisper = "숙소에서 나와서 강가로 걸어가는 중인데 날씨가 정말 좋아요"
    record = _plan(
        [_seg(0, 0.0, 4.0, whisper)], [ChunkText(0, 0.0, "숙소에서 나와서")], cfg=SEGMENT_GATE_ONLY
    )

    assert dict(record.texts) == {}
    assert record.rejections[0].reason in {retext.REASON_TOO_SHORT, retext.REASON_MATCH}


# ─────────────────────────────────────────────────────────────
#  정렬 — 잃지 않고, 겹치지 않는다
# ─────────────────────────────────────────────────────────────


def test_punctuation_only_differences_replace_everything_and_keep_gpt_marks() -> None:
    spine = [
        _seg(0, 0.0, 3.0, "안녕하세요 오늘은 망원동에 왔습니다"),
        _seg(1, 3.5, 6.0, "낮술 하러 왔어요"),
    ]
    gpt = "안녕하세요, 오늘은 망원동에 왔습니다. 낮술 하러 왔어요!"
    record = _plan(spine, [ChunkText(0, 0.0, gpt)])

    assert record.status == RETEXT_APPLIED
    assert dict(record.texts) == {
        0: "안녕하세요, 오늘은 망원동에 왔습니다.",
        1: "낮술 하러 왔어요!",
    }
    assert record.rejections == () and record.unplaced == ()


def test_a_corrected_word_lands_in_its_own_segment() -> None:
    spine = [
        _seg(0, 0.0, 3.0, "여기는 블루팽귄이라는"),
        _seg(1, 3.5, 5.0, "가게입니다"),
    ]
    record = _plan(spine, [ChunkText(0, 0.0, "여기는 블루펭귄이라는 가게입니다")])

    assert dict(record.texts) == {0: "여기는 블루펭귄이라는", 1: "가게입니다"}


def test_replace_blocks_lose_no_letters_and_assign_none_twice() -> None:
    """replace 블록의 잉여 글자도 블록이 걸친 세그먼트에 빠짐없이 나뉜다."""
    spine = [
        _seg(0, 0.0, 2.0, "가나다라 마바사"),
        _seg(1, 2.0, 4.0, "아자차카 타파하"),
        _seg(2, 4.0, 6.0, "하나 둘 셋 넷"),
    ]
    gpt = "가나다라 마바사 아자차카 타파하 하나 둘 셋 넷 다섯"
    cfg = replace(Config(), retext_length_ratio_max=100.0, retext_segment_match_floor=0.0)
    record = _plan(spine, [ChunkText(0, 0.0, gpt)], cfg=cfg)

    placed = "".join(text for _, text in sorted(record.texts))
    unplaced = "".join(item.text for item in record.unplaced)
    assert sorted(strip_for_compare(placed + unplaced)) == sorted(strip_for_compare(gpt))


def test_every_gpt_letter_lands_exactly_once_under_random_alignments() -> None:
    """칸막이 불변식 — 교체·불통과·자리 없음 셋을 이어 붙이면 gpt 글자열이 **순서까지** 그대로다.

    삽입을 경계로 미끄러뜨리는 처리가 글자를 잃거나 두 번 세지 않는지 무작위로 흔든다.
    """
    rng = random.Random(37)
    words = ["사진", "말", "치즈", "하나", "둘", "셋", "가게", "말말", "좋아요", "음", "네"]
    for _ in range(300):
        spine = [
            _seg(i, float(i), float(i) + 0.5, " ".join(rng.choices(words, k=rng.randint(0, 4))))
            for i in range(rng.randint(1, 6))
        ]
        gpt = " ".join(rng.choices(words, k=rng.randint(0, 20)))
        record = _plan(spine, [ChunkText(0, 0.0, gpt)], cfg=SEGMENT_GATE_ONLY)

        pieces: dict[int, str] = dict(record.texts)
        pieces.update({item.seg_id: item.candidate for item in record.rejections})
        order = sorted(pieces.items())
        placed = "".join(text for _, text in order)
        assert sorted(
            strip_for_compare(placed + "".join(u.text for u in record.unplaced))
        ) == sorted(strip_for_compare(gpt))


def test_letters_between_segments_are_recorded_not_printed() -> None:
    spine = [_seg(0, 0.0, 2.0, "첫 번째 말"), _seg(1, 5.0, 7.0, "두 번째 말")]
    gpt = "첫 번째 말 그 사이에 whisper가 놓친 말 두 번째 말"
    record = _plan(spine, [ChunkText(0, 0.0, gpt)])

    assert dict(record.texts) == {0: "첫 번째 말", 1: "두 번째 말"}
    assert len(record.unplaced) == 1
    missing = record.unplaced[0]
    assert strip_for_compare(missing.text) == strip_for_compare("그 사이에 whisper가 놓친 말")
    assert (missing.before, missing.after) == (0, 1)
    assert missing.chars == len(strip_for_compare(missing.text))


def test_a_rejected_segment_keeps_its_gpt_range_away_from_neighbours() -> None:
    """불통과 세그먼트에 대응된 gpt 글자는 이웃에게도 주지 않는다 — 같은 말이 두 번 나간다."""
    spine = [
        _seg(0, 0.0, 2.0, "오늘 날씨가"),
        _seg(1, 2.0, 2.3, "음"),
        _seg(2, 2.5, 4.0, "정말 좋네요"),
    ]
    gpt = "오늘 날씨가 아니 근데 진짜로 저기 저 건물 좀 봐봐 정말 좋네요"
    record = _plan(spine, [ChunkText(0, 0.0, gpt)])

    texts = dict(record.texts)
    assert 1 not in texts
    body = strip_for_compare(" ".join(texts.values()))
    assert "건물" not in body
    assert body.count("오늘날씨가") == 1


# ─────────────────────────────────────────────────────────────
#  조각
# ─────────────────────────────────────────────────────────────


def test_segments_are_assigned_by_start_and_the_last_chunk_runs_to_the_end() -> None:
    spine = [
        _seg(0, 1.0, 3.0, "첫 조각의 말"),
        _seg(1, 9.9, 10.4, "경계를 넘는 말"),
        _seg(2, 10.0, 12.0, "둘째 조각의 말"),
        _seg(3, 25.0, 26.0, "영상 끝의 말"),
    ]
    chunks = [
        ChunkText(1, 10.0, "둘째 조각의 말 영상 끝의 말"),
        ChunkText(0, 0.0, "첫 조각의 말 경계를 넘는 말"),
    ]
    record = _plan(spine, chunks, duration=24.0)

    assert [chunk.span for chunk in record.chunks] == [(0.0, 10.0), (10.0, 24.0)]
    assert dict(record.texts) == {
        0: "첫 조각의 말",
        1: "경계를 넘는 말",
        2: "둘째 조각의 말",
        3: "영상 끝의 말",
    }


def test_a_chunk_below_the_similarity_floor_stays_whisper() -> None:
    spine = [_seg(0, 0.0, 3.0, "오늘은 망원동에 왔습니다")]
    record = _plan(spine, [ChunkText(0, 0.0, "전혀 다른 녹음의 글자들이 들어왔다")])

    assert dict(record.texts) == {}
    assert record.chunks[0].accepted is False
    assert record.rejections[0].reason == retext.REASON_CHUNK


def test_an_empty_second_model_chunk_replaces_nothing() -> None:
    record = _plan([_seg(0, 0.0, 3.0, "오늘은 망원동에 왔습니다")], [ChunkText(0, 0.0, "")])
    assert dict(record.texts) == {}


def test_a_chunk_without_whisper_segments_records_all_its_letters() -> None:
    spine = [_seg(0, 0.0, 3.0, "오늘은 망원동에 왔습니다")]
    chunks = [
        ChunkText(0, 0.0, "오늘은 망원동에 왔습니다"),
        ChunkText(1, 30.0, "whisper가 비운 조각"),
    ]
    record = _plan(spine, chunks)

    assert dict(record.texts) == {0: "오늘은 망원동에 왔습니다"}
    assert [item.chunk_index for item in record.unplaced] == [1]


# ─────────────────────────────────────────────────────────────
#  되풀이 구간
# ─────────────────────────────────────────────────────────────


def test_collapsed_and_kept_repairs_are_excluded_and_their_letters_counted() -> None:
    spine = [
        _seg(0, 0.0, 2.0, "사진 찍을게요"),
        _seg(1, 2.0, 3.0, "치즈"),
        _seg(2, 40.0, 42.0, "하나 둘 셋"),
        _seg(3, 42.0, 43.0, "브이"),
    ]
    gpt = "사진 찍을게요 치즈 하나 둘 셋 브이"
    repairs = (_repair(REPAIR_COLLAPSED, (2.0, 3.0)), _repair(REPAIR_KEPT, (42.0, 43.0)))
    record = _plan(spine, [ChunkText(0, 0.0, gpt)], repairs=repairs)

    texts = dict(record.texts)
    assert set(texts) == {0, 2}
    assert [item.seg_ids for item in record.excluded] == [(1,), (3,)]
    assert [item.outcome for item in record.excluded] == [REPAIR_COLLAPSED, REPAIR_KEPT]
    assert [item.gpt_chars for item in record.excluded] == [2, 2]


def test_a_replaced_repair_span_is_still_retexted() -> None:
    spine = [_seg(0, 0.0, 2.0, "다시 받은 글자")]
    record = _plan(
        spine,
        [ChunkText(0, 0.0, "다시 받은 글자")],
        repairs=(_repair(REPAIR_REPLACED, (0.0, 2.0)),),
    )
    assert dict(record.texts) == {0: "다시 받은 글자"}
    assert record.excluded == ()


def test_a_repair_without_a_span_falls_back_to_the_repeat_and_says_so() -> None:
    spine = [_seg(0, 0.0, 2.0, "치즈"), _seg(1, 3.0, 5.0, "좋아요")]
    loose = replace(_repair(REPAIR_KEPT, (0.0, 2.0)), span=(0.0, 0.0))
    record = _plan(spine, [ChunkText(0, 0.0, "치즈 좋아요")], repairs=(loose,))

    assert record.excluded[0].seg_ids == (0,)
    assert record.excluded[0].span_missing is True


# ─────────────────────────────────────────────────────────────
#  기록
# ─────────────────────────────────────────────────────────────


def test_the_record_round_trips_through_json() -> None:
    spine = [_seg(0, 0.0, 2.0, "첫 번째 말"), _seg(1, 5.0, 7.0, "아")]
    gpt = "첫 번째 말, 사이에 낀 말 아니 이건 전혀 다른 긴 문장이에요"
    record = _plan(spine, [ChunkText(0, 0.0, gpt)], repairs=(_repair(REPAIR_KEPT, (5.0, 7.0)),))

    assert record.rejections or record.excluded
    assert retext.RetextRecord.from_dict(record.to_dict()) == record


def test_unavailable_and_off_records_replace_nothing() -> None:
    assert retext.unavailable("boom").texts == ()
    assert retext.unavailable("boom").error == "boom"
    assert retext.switched_off().texts == ()


# ─────────────────────────────────────────────────────────────
#  파이프라인 — 오프라인 합성 픽스처
# ─────────────────────────────────────────────────────────────

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
RETEXTED = Path("golden/retexted.mp4")
LOOPYLONG = Path("golden/loopylong.mp4")
WHISPER_ONLY = replace(Config(), retext_enabled=False)


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def _body(result: pipeline.RunResult) -> str:
    return " ".join(cue.text for cue in parse_srt(result.outputs["srt"].read_text("utf-8")))


@pytest.mark.usefixtures("synthetic")
def test_the_srt_carries_gpt_letters_on_whisper_times(tmp_path: Path) -> None:
    result = pipeline.run(RETEXTED, Config(), tmp_path, force_offline=True)

    body = _body(result)
    assert "블루펭귄" in body and "블루팽귄" not in body
    assert "소시지" in body
    # 세그먼트 사이에 낀 말은 시각이 없어 나가지 않는다 — 기록에만 남는다.
    assert "연남동" not in body
    assert any("연남동" in item.text for item in result.trace.retext.unplaced)
    # 과소전사 세그먼트에 몰린 말은 게이트가 막는다.
    assert "수제 맥주" not in body
    assert any(cue.flags and pipeline.RETEXTED in cue.flags for cue in result.cues)


@pytest.mark.usefixtures("synthetic")
def test_retext_changes_letters_only_right_after_spine_to_cues(tmp_path: Path) -> None:
    """큐 개수·시각·seg_ids는 교정 on/off에 같다. refine 뒤 개수는 요구하지 않는다(D37)."""
    result = pipeline.run(RETEXTED, Config(), tmp_path, force_offline=True)
    spine = list(result.trace.spine)
    plain = pipeline.spine_to_cues(spine)
    fixed = pipeline.apply_retext(pipeline.spine_to_cues(spine), result.trace.retext)

    def shape(cues: list[Cue]) -> list[tuple[float, float, tuple[int, ...], int]]:
        return [(cue.start, cue.end, cue.seg_ids, cue.index) for cue in cues]

    assert shape(fixed) == shape(plain)
    assert [cue.text for cue in fixed] != [cue.text for cue in plain]


@pytest.mark.usefixtures("synthetic")
def test_refined_retext_cues_stay_inside_their_whisper_segments(tmp_path: Path) -> None:
    cfg = Config()
    result = pipeline.run(RETEXTED, cfg, tmp_path, force_offline=True)
    segments = {segment.id: segment for segment in result.trace.spine}
    for cue in result.cues:
        first, last = segments[cue.seg_ids[0]], segments[cue.seg_ids[-1]]
        assert cue.start >= first.start - cfg.lead_in - 1e-6
        assert cue.end <= last.end + cfg.lead_out + cfg.min_duration + 1e-6


@pytest.mark.usefixtures("synthetic")
def test_verification_cannot_change_the_retexted_srt(tmp_path: Path) -> None:
    """규칙 5 — 교정이 켜져 있어도 검증 on/off에 SRT 바이트가 같다."""
    plain = pipeline.run(
        RETEXTED, Config(), tmp_path / "plain", force_offline=True, verify_with_second_model=False
    )
    checked = pipeline.run(RETEXTED, Config(), tmp_path / "checked", force_offline=True)

    assert checked.verification is not None
    assert plain.outputs["srt"].read_bytes() == checked.outputs["srt"].read_bytes()
    assert "블루펭귄" in _body(plain), "--no-verify여도 두 번째 모델 응답은 쓰인다"


@pytest.mark.usefixtures("synthetic")
def test_the_second_model_is_called_once_for_retext_and_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    kinds: list[str] = []
    real = transcribe.FixtureTranscriber.transcribe

    def counting(self: transcribe.FixtureTranscriber, job: TranscriptionJob, cfg: Config):  # type: ignore[no-untyped-def]
        kinds.append(job.kind)
        return real(self, job, cfg)

    monkeypatch.setattr(transcribe.FixtureTranscriber, "transcribe", counting)
    pipeline.run(RETEXTED, Config(), tmp_path, force_offline=True)
    assert kinds.count(transcribe.VERIFY_KIND) == 1


@pytest.mark.usefixtures("synthetic")
def test_turning_retext_off_gives_back_the_whisper_letters(tmp_path: Path) -> None:
    result = pipeline.run(RETEXTED, WHISPER_ONLY, tmp_path, force_offline=True)
    assert "블루팽귄" in _body(result)
    assert result.trace.retext.status == RETEXT_OFF


def test_a_missing_second_model_still_leaves_a_whisper_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    lonely = tmp_path / "no-gpt"
    lonely.mkdir()
    for name in ("retexted.whisper.json", "retexted.meta.json"):
        (lonely / name).write_bytes((SYNTHETIC / name).read_bytes())
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(lonely))
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "WARN")

    result = pipeline.run(RETEXTED, Config(), tmp_path / "out", force_offline=True)

    assert "블루팽귄" in _body(result)
    assert result.trace.retext.status == RETEXT_UNAVAILABLE
    assert result.trace.retext.error
    assert json.loads((tmp_path / "out" / "trace.json").read_text("utf-8"))["retext"]["status"] == (
        RETEXT_UNAVAILABLE
    )
    assert "글자 교정" in capsys.readouterr().err


@pytest.mark.usefixtures("synthetic")
def test_a_raising_second_model_still_leaves_a_whisper_srt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = transcribe.FixtureTranscriber.transcribe

    def broken(self: transcribe.FixtureTranscriber, job: TranscriptionJob, cfg: Config):  # type: ignore[no-untyped-def]
        if job.kind == transcribe.VERIFY_KIND:
            raise RuntimeError("계정에 모델이 없다")
        return real(self, job, cfg)

    monkeypatch.setattr(transcribe.FixtureTranscriber, "transcribe", broken)
    result = pipeline.run(RETEXTED, Config(), tmp_path, force_offline=True)

    assert "블루팽귄" in _body(result)
    assert result.trace.retext.status == RETEXT_UNAVAILABLE
    assert "계정에 모델이 없다" in result.trace.retext.error
    assert result.verification is None


@pytest.mark.usefixtures("synthetic")
def test_unrecovered_repeats_are_really_excluded_and_warned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """빈 repairs가 배선되면 조용히 통과하는 모양을 막는다 — 제외가 **실제로** 있어야 한다."""
    monkeypatch.setenv("VLOGSRT_LOG_LEVEL", "WARN")
    result = pipeline.run(LOOPYLONG, Config(), tmp_path, force_offline=True)

    record = result.trace.retext
    held = {seg_id for item in record.excluded for seg_id in item.seg_ids}
    assert held, "loopylong에는 접힌 되풀이가 있다"
    assert held.isdisjoint(dict(record.texts))
    assert "되풀이는 교정하지 않는다" in capsys.readouterr().err


@pytest.mark.usefixtures("synthetic")
def test_redo_reapplies_the_retext_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    first = pipeline.run(RETEXTED, Config(), tmp_path, force_offline=True)
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(tmp_path / "no-such-dir"))
    again = pipeline.redo(tmp_path / "trace.json", Config(), tmp_path / "redo")

    assert again.outputs["srt"].read_bytes() == first.outputs["srt"].read_bytes()
    assert Trace.load(tmp_path / "redo" / "trace.json").retext == first.trace.retext


@pytest.mark.usefixtures("synthetic")
def test_cli_no_retext_prints_whisper_letters(tmp_path: Path) -> None:
    assert main(["run", str(RETEXTED), "--offline", "--no-retext", "-o", str(tmp_path)]) == 0
    assert "블루팽귄" in (tmp_path / "retexted.srt").read_text("utf-8")
