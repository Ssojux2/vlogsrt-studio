"""세그먼트 간 반복 환청 검출.

이 테스트들은 실측에서 나온 실패를 고정한다. 77분 브이로그에서 whisper-1이
같은 문장을 103개 세그먼트에 걸쳐 570초 동안 반복했고, 압축률 필터는 그 구조를
보지 못했다. 아래 `test_compression_ratio_alone_misses_a_split_loop`가 그 상황이다.
"""

from __future__ import annotations

from vlogsrt.config import Config
from vlogsrt.loops import demote, find_loops, looped_seconds, undetected
from vlogsrt.models import VERDICT_COMPRESSION_FAIL, VERDICT_OK, VERDICT_SILENT

from .conftest import make_segment


def repeated(text: str, count: int, *, step: float = 2.0, **kwargs: object) -> list:
    return [
        make_segment(i, i * step, i * step + step, text, **kwargs)  # type: ignore[arg-type]
        for i in range(count)
    ]


def test_finds_a_run_of_identical_segments() -> None:
    spine = repeated("여기가 보통은", 5)
    loops = find_loops(spine, Config())
    assert len(loops) == 1
    assert loops[0].count == 5
    assert loops[0].start == 0.0 and loops[0].end == 10.0
    assert loops[0].seg_ids == (0, 1, 2, 3, 4)


def test_normal_speech_is_not_a_loop() -> None:
    spine = [
        make_segment(0, 0.0, 2.0, "오늘은 망원동에 왔어"),
        make_segment(1, 2.0, 4.0, "빵집부터 가자"),
        make_segment(2, 4.0, 6.0, "여기 진짜 맛있어"),
    ]
    assert find_loops(spine, Config()) == []


def test_short_repeats_are_not_loops() -> None:
    """맞장구가 두어 번 겹치는 것은 환청이 아니다."""
    assert find_loops(repeated("응", 4), Config()) == []  # loop_min_chars
    assert find_loops(repeated("그러니까", 2), Config()) == []  # loop_min_repeats
    assert find_loops(repeated("그러니까", 3, step=0.5), Config()) == []  # loop_min_seconds


def test_whitespace_and_nfc_differences_still_count_as_repeats() -> None:
    spine = [
        make_segment(0, 0.0, 2.0, "여기가  보통은"),
        make_segment(1, 2.0, 4.0, " 여기가 보통은 "),
        make_segment(2, 4.0, 6.0, "여기가 보통은"),
    ]
    assert len(find_loops(spine, Config())) == 1


def test_two_separate_loops_are_reported_separately() -> None:
    spine = [
        *repeated("여기가 보통은", 3),
        make_segment(90, 6.0, 8.0, "이제 다른 얘기"),
        *[make_segment(100 + i, 8.0 + i * 2, 10.0 + i * 2, "너무 어려워.") for i in range(4)],
    ]
    loops = find_loops(spine, Config())
    assert [loop.count for loop in loops] == [3, 4]
    assert looped_seconds(loops) == 14.0


def test_compression_ratio_alone_misses_a_split_loop() -> None:
    """실측 재현 — 반복이 세그먼트마다 쪼개지면 압축률은 정상값을 돌려준다.

    whisper가 한 세그먼트 안에서 반복하면 compression_ratio가 튀어 걸린다. 그러나
    세그먼트를 나눠 반복하면 각 세그먼트는 짧고 정상이라 압축률이 1 근처다.
    """
    from vlogsrt.transcribe import judge_verdict

    cfg = Config()
    spine = repeated("여기가 보통은", 6, compression_ratio=0.97)
    assert all(judge_verdict(segment, [], cfg) == "ok" for segment in spine)

    loops = find_loops(spine, cfg)
    assert looped_seconds(loops) == 12.0
    assert len(undetected(loops, spine)) == 6  # 압축률 필터를 전부 통과했다


def test_undetected_is_empty_when_the_filter_caught_everything() -> None:
    spine = repeated("여기가 보통은", 5, verdict="compression_fail")
    loops = find_loops(spine, Config())
    assert undetected(loops, spine) == []


# ─────────────────────────────────────────────────────────────
#  강등 — 되풀이 구간은 큐가 되면 안 된다 (D18)
# ─────────────────────────────────────────────────────────────


def test_demote_excludes_loop_segments_from_the_skeleton() -> None:
    """실측(D18): '오 오 오' 구간이 ok로 통과해 큐가 되자 정렬이 무너져
    빈 큐만 남았고, 그 빈 큐들이 시간축을 차지해 3분 이전이 통째로 사라졌다."""
    cfg = Config()
    spine = repeated("여기가 보통은", 6, compression_ratio=0.97)
    judged = demote(spine, cfg)
    assert all(s.verdict == VERDICT_COMPRESSION_FAIL for s in judged)
    assert undetected(find_loops(judged, cfg), judged) == []


def test_demote_keeps_id_and_timing_and_leaves_the_input_alone() -> None:
    spine = repeated("여기가 보통은", 6)
    judged = demote(spine, Config())
    assert [(s.id, s.start, s.end, s.text) for s in judged] == [
        (s.id, s.start, s.end, s.text) for s in spine
    ]
    assert all(s.verdict == VERDICT_OK for s in spine)  # 원본은 그대로다


def test_demote_leaves_normal_speech_alone() -> None:
    spine = [
        make_segment(0, 0.0, 2.0, "오늘은 망원동에 왔어"),
        make_segment(1, 2.0, 4.0, "빵집부터 가자"),
    ]
    assert demote(spine, Config()) == spine


def test_demote_respects_the_loop_thresholds() -> None:
    """맞장구 두어 번은 환청이 아니다. find_loops와 같은 기준을 쓴다."""
    short_run = repeated("그러니까", 2)
    assert demote(short_run, Config()) == short_run


def test_demote_does_not_touch_silent_members() -> None:
    """silent은 "발화가 없었다"는 판정이다. 강등해 닻으로 만들면
    발화가 없던 자리에 자막을 만들게 된다."""
    spine = repeated("음..", 5, verdict=VERDICT_SILENT)
    judged = demote(spine, Config())
    assert all(s.verdict == VERDICT_SILENT for s in judged)


def test_fetch_spine_returns_a_demoted_skeleton() -> None:
    """강등은 골격 생성 단계에서 끝난다. 그 뒤로 골격은 불변이다(규칙 1)."""
    from vlogsrt.pipeline import fetch_spine
    from vlogsrt.transcribe import ChunkResponse

    class FakeTranscriber:
        def whisper(self, job: object, cfg: object) -> list[ChunkResponse]:
            segments = [
                {
                    "id": i,
                    "start": i * 2.0,
                    "end": i * 2.0 + 2.0,
                    "text": "여기가 보통은",
                    "avg_logprob": -0.3,
                    "compression_ratio": 0.97,
                    "no_speech_prob": 0.1,
                }
                for i in range(6)
            ]
            return [ChunkResponse(index=0, offset=0.0, payload={"segments": segments, "words": []})]

    spine, _ = fetch_spine(FakeTranscriber(), job=None, cfg=Config(), silences=())  # type: ignore[arg-type]
    assert all(s.verdict == VERDICT_COMPRESSION_FAIL for s in spine)


# ─────────────────────────────────────────────────────────────
#  파이프라인이 사람에게 알린다
# ─────────────────────────────────────────────────────────────


def test_pipeline_warns_when_whisper_loops() -> None:
    """조용히 진행하면 사람은 망가진 SRT를 받고 나서야 안다."""
    from vlogsrt.pipeline import warn_about_loops

    spine = repeated("여기가 보통은", 6)
    notices = warn_about_loops(spine, Config(), duration=100.0)
    assert notices
    assert "6개" not in notices[0]  # 구간 수는 1개다. 반복 횟수와 헷갈리면 안 된다
    assert "1개" in notices[0] and "12초" in notices[0]
    assert "12.0%" in notices[0]
    assert any("여기가 보통은" in line for line in notices)


def test_pipeline_points_at_the_prompt_when_the_loop_starts_early() -> None:
    """영상 시작부터 반복하면 whisper 힌트가 원인일 가능성이 높다(D13)."""
    from vlogsrt.pipeline import warn_about_loops

    early = warn_about_loops(repeated("구어체 한국어", 5), Config(), duration=4598.0)
    assert any("prompt" in line for line in early)

    late = [make_segment(i, 3000.0 + i * 2, 3002.0 + i * 2, "여기가 보통은") for i in range(5)]
    assert not any("prompt" in line for line in warn_about_loops(late, Config(), 4598.0))


def test_clean_spine_produces_no_warning() -> None:
    from vlogsrt.pipeline import warn_about_loops

    spine = [make_segment(i, i * 2.0, i * 2.0 + 2.0, f"문장 {i}") for i in range(10)]
    assert warn_about_loops(spine, Config(), duration=20.0) == []
