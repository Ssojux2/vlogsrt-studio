"""최종 편집 모델(W-66, D117) — 초안 영상 위 클립과 초안 시각 자막, 그리고 편집 타임라인 투영.

클립 연산은 자막을 건드리지 않는다. 지운 구간의 자막은 투영에서 가려지고(편집본 SRT에서 빠짐)
문서에는 남으며, 클립을 다시 늘리면 돌아온다. 계산은 정수 틱이라 편집 밀리초가 그대로 왕복한다.
"""

from __future__ import annotations

import json
from fractions import Fraction as F
from pathlib import Path

import pytest

from vlogsrt.project.raw import final_edit as fe
from vlogsrt.project.raw.final_edit_config import FINAL_EDIT_SETTINGS, FinalEditSettings

FD30 = F(1, 30)
FD2997 = F(1001, 30000)
CASES = Path(__file__).resolve().parent.parent / "fixtures" / "synthetic" / "final_edit_cases.json"


def _draft(frames: int = 300, duration: F = FD30) -> fe.DraftFacts:
    return fe.DraftFacts(frame_duration=duration, total_frames=frames)


def _doc(
    clips: list[tuple[int, int]],
    cues: list[tuple[str, int, int, str]],
    frames: int = 300,
    duration: F = FD30,
) -> fe.EditDocument:
    return fe.EditDocument(
        draft=_draft(frames, duration),
        clips=tuple(fe.Clip(*clip) for clip in clips),
        cues=tuple(
            sorted((fe.EditCue(*cue) for cue in cues), key=lambda c: (c.start_ms, c.end_ms))
        ),
        next_cue=1,
    )


def _shown(document: fe.EditDocument) -> list[tuple[str, F, F, bool]]:
    return [(cue.cue_id, cue.start, cue.end, cue.trimmed) for cue in fe.project(document).cues]


# ─── 심기 · 전선 모양 · 검사 ─────────────────────────────────────────────


def test_seed_keeps_whole_draft_and_numbers_cues() -> None:
    document = fe.seed(_draft(), [(F(2), F(3), "둘"), (F(1), F(3, 2), "하나")])
    assert document.clips == (fe.Clip(0, 300),)
    assert [(cue.cue_id, cue.start_ms, cue.end_ms) for cue in document.cues] == [
        ("c2", 1000, 1500),
        ("c1", 2000, 3000),
    ]
    assert fe.srt_text(fe.project(document)) == (
        "1\n00:00:01,000 --> 00:00:01,500\n하나\n\n2\n00:00:02,000 --> 00:00:03,000\n둘\n"
    )


def test_seed_refuses_sub_millisecond_times() -> None:
    with pytest.raises(fe.FinalEditError, match="밀리초"):
        fe.seed(_draft(), [(F(1, 3), F(1), "x")])


def test_payload_round_trip() -> None:
    document = _doc([(0, 100), (150, 300)], [("c1", 0, 1000, "가\n나"), ("n3", 5000, 6000, "")])
    payload = fe.to_payload(document)
    again = fe.from_payload(json.loads(json.dumps(payload)), document.draft)
    assert again == document


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"clips": [[0, 400]], "cues": [], "nextCue": 1}, "차례대로"),
        ({"clips": [[100, 50]], "cues": [], "nextCue": 1}, "차례대로"),
        ({"clips": [[0, 100], [90, 200]], "cues": [], "nextCue": 1}, "차례대로"),
        ({"clips": [[0, True]], "cues": [], "nextCue": 1}, "정수"),
        ({"clips": [[0, 1.5]], "cues": [], "nextCue": 1}, "정수"),
        ({"clips": [], "cues": [["x1", 0, 1, "a"]], "nextCue": 1}, "아이디"),
        ({"clips": [], "cues": [["c1", 0, 1, "a"], ["c1", 2, 3, "b"]], "nextCue": 1}, "아이디"),
        ({"clips": [], "cues": [["c1", 5, 1, "a"]], "nextCue": 1}, "밖"),
        ({"clips": [], "cues": [["c1", 0, 10001, "a"]], "nextCue": 1}, "밖"),
        ({"clips": [], "cues": [["c1", 0, 1, "a\n\nb"]], "nextCue": 1}, "빈 줄"),
        ({"clips": [], "cues": [["c1", 0, 1, "a\r"]], "nextCue": 1}, "빈 줄"),
        ({"clips": [], "cues": [["c2", 5, 6, "a"], ["c1", 0, 1, "b"]], "nextCue": 1}, "차례"),
        ({"clips": [], "cues": [], "nextCue": 0}, "번호"),
        ({"clips": "x", "cues": [], "nextCue": 1}, "목록"),
    ],
)
def test_from_payload_refuses_broken_documents(payload: dict, message: str) -> None:
    with pytest.raises(fe.FinalEditError, match=message):
        fe.from_payload(payload, _draft())


def test_from_payload_enforces_text_and_count_limits() -> None:
    tight = FinalEditSettings(max_text_chars=3, max_cues=1)
    with pytest.raises(fe.FinalEditError, match="3자"):
        fe.from_payload(
            {"clips": [], "cues": [["c1", 0, 1, "abcd"]], "nextCue": 1}, _draft(), tight
        )
    two = [["c1", 0, 1, "a"], ["c2", 2, 3, "b"]]
    with pytest.raises(fe.FinalEditError, match="1개"):
        fe.from_payload({"clips": [], "cues": two, "nextCue": 1}, _draft(), tight)


# ─── 클립 연산 ───────────────────────────────────────────────────────────


def test_blade_splits_inside_clip_and_refuses_on_edit_point() -> None:
    document = fe.blade(_doc([(0, 300)], []), 120)
    assert document.clips == (fe.Clip(0, 120), fe.Clip(120, 300))
    with pytest.raises(fe.FinalEditError, match="편집 점"):
        fe.blade(document, 120)
    with pytest.raises(fe.FinalEditError, match="편집 점"):
        fe.blade(document, 0)


def test_ripple_delete_across_clips_shortens_and_splits() -> None:
    document = _doc([(0, 100), (150, 250), (260, 300)], [])
    # 편집 프레임: [0,100) [100,200) [200,240). [50, 120) 지우기.
    after = fe.ripple_delete(document, 50, 120)
    assert after.clips == (fe.Clip(0, 50), fe.Clip(170, 250), fe.Clip(260, 300))
    middle = fe.ripple_delete(_doc([(0, 300)], []), 100, 200)
    assert middle.clips == (fe.Clip(0, 100), fe.Clip(200, 300))
    everything = fe.ripple_delete(document, 0, 240)
    assert everything.clips == ()
    with pytest.raises(fe.FinalEditError, match="범위"):
        fe.ripple_delete(document, 10, 10)


def test_trim_bounds_and_extend_restores() -> None:
    document = _doc([(0, 100), (150, 300)], [])
    assert fe.set_clip_end(document, 0, 150).clips[0] == fe.Clip(0, 150)
    with pytest.raises(fe.FinalEditError):
        fe.set_clip_end(document, 0, 151)
    assert fe.set_clip_start(document, 1, 100).clips[1] == fe.Clip(100, 300)
    with pytest.raises(fe.FinalEditError):
        fe.set_clip_start(document, 1, 99)
    with pytest.raises(fe.FinalEditError):
        fe.set_clip_start(document, 1, 300)  # 최소 1프레임은 남는다
    assert fe.set_clip_end(document, 1, 151).clips[1] == fe.Clip(150, 151)


def test_join_only_contiguous_clips_and_delete_clips() -> None:
    bladed = fe.blade(_doc([(0, 300)], []), 100)
    assert fe.join_clips(bladed, 0).clips == (fe.Clip(0, 300),)
    with pytest.raises(fe.FinalEditError, match="합칠"):
        fe.join_clips(_doc([(0, 100), (150, 300)], []), 0)
    assert fe.delete_clips(bladed, [0]).clips == (fe.Clip(100, 300),)
    with pytest.raises(fe.FinalEditError):
        fe.delete_clips(bladed, [2])


# ─── 투영: 컷과 자막 ─────────────────────────────────────────────────────


CUES = [
    ("c1", 1000, 2000, "앞"),  # 지운 구간 앞
    ("c2", 3000, 3500, "안"),  # 지운 구간 [3000, 4000)ms 안
    ("c3", 3500, 4500, "끝 걸침"),  # 뒤쪽이 남는다
    ("c4", 2500, 3200, "앞 걸침"),  # 앞쪽이 남는다
    ("c5", 6000, 7000, "뒤"),
]


def test_cut_hides_inside_cue_trims_partial_and_ripples_later() -> None:
    document = fe.ripple_delete(_doc([(0, 300)], CUES), 90, 120)  # 초안 3초–4초를 지운다
    projection = fe.project(document)
    assert [cue.cue_id for cue in projection.hidden] == ["c2"]
    assert _shown(document) == [
        ("c1", F(1), F(2), False),
        ("c4", F(5, 2), F(3), True),
        ("c3", F(3), F(7, 2), True),
        ("c5", F(5), F(6), False),
    ]
    assert len(document.cues) == len(CUES)  # 문서에서는 지우지 않는다


def test_cut_inside_cue_shrinks_it_by_removed_length() -> None:
    document = fe.ripple_delete(_doc([(0, 300)], [("c1", 1000, 5000, "긴 말")]), 60, 90)
    assert _shown(document) == [("c1", F(1), F(4), True)]


def test_extending_clip_restores_hidden_cue() -> None:
    cut = fe.ripple_delete(_doc([(0, 300)], CUES), 90, 120)
    restored = fe.set_clip_end(cut, 0, 120)
    assert fe.project(restored).hidden == ()
    assert _shown(restored)[2] == ("c2", F(3), F(7, 2), False)


def test_zero_length_cue_visible_inside_clip_only() -> None:
    document = _doc([(0, 30), (60, 90)], [("c1", 500, 500, "a"), ("c2", 1500, 1500, "b")])
    assert [cue.cue_id for cue in fe.project(document).cues] == ["c1"]
    assert [cue.cue_id for cue in fe.project(document).hidden] == ["c2"]


def test_srt_text_matches_draft_byte_format() -> None:
    document = fe.ripple_delete(_doc([(0, 300)], CUES), 90, 120)
    assert fe.srt_text(fe.project(document)).splitlines()[:3] == [
        "1",
        "00:00:01,000 --> 00:00:02,000",
        "앞",
    ]


@pytest.mark.parametrize("residue", range(30))
def test_edited_ms_round_trips_at_2997(residue: int) -> None:
    # 29.97fps에서 컷 하나 뒤의 편집 시각 — 프레임 경계가 밀리초에 떨어지지 않아도 친 값이 돌아온다.
    document = _doc([(0, 100), (100 + residue, 3000)], [], frames=3000, duration=FD2997)
    rate = fe.tick_rate(document.draft)
    unit = fe.ms_ticks(document.draft)
    for typed in (3500, 3501, 3502, 40000, 80123):
        draft_ms = fe.draft_ms_at(document, typed * unit, fe.SIDE_START)
        ticks = fe.edited_ticks(document, draft_ms)
        assert fe.format_srt_time(F(ticks, rate)) == fe.format_srt_time(F(typed, 1000))


def test_cut_point_sides() -> None:
    document = _doc([(0, 30), (60, 90)], [])
    boundary = 30 * fe.frame_ticks(document.draft)
    assert fe.draft_ms_at(document, boundary, fe.SIDE_START) == 2000
    assert fe.draft_ms_at(document, boundary, fe.SIDE_END) == 1000
    assert fe.edited_ticks(document, 1500) == boundary  # 지운 구간 → 컷 점
    assert fe.draft_frame_at(document, 30) == 60
    assert fe.draft_frame_at(document, 60) == 90
    assert fe.edited_frame_of(document, 45) is None
    assert fe.edited_frame_of(document, 61) == 31


def test_keep_and_removed_ranges() -> None:
    document = _doc([(30, 60), (90, 120)], [])
    assert fe.keep_ranges(document) == ((30, 60, F(1), F(2)), (90, 120, F(3), F(4)))
    assert fe.removed_ranges(document) == ((0, 30), (60, 90), (120, 300))


# ─── 자막 연산 ───────────────────────────────────────────────────────────


def test_cue_split_merge_insert_delete() -> None:
    document = _doc([(0, 300)], [("c1", 1000, 3000, "하나 둘 셋 넷")])
    left, right = fe.split_text("하나 둘 셋 넷")
    split = fe.cue_split(document, "c1", 2000, left, right)
    assert [(c.cue_id, c.start_ms, c.end_ms, c.text) for c in split.cues] == [
        ("c1", 1000, 2000, "하나 둘"),
        ("n1", 2000, 3000, "셋 넷"),
    ]
    assert split.next_cue == 2
    merged = fe.cue_merge_next(split, "c1")
    assert [(c.cue_id, c.start_ms, c.end_ms, c.text) for c in merged.cues] == [
        ("c1", 1000, 3000, "하나 둘 셋 넷")
    ]
    inserted = fe.cue_insert(merged, 5000, 7000, "")
    assert inserted.cues[-1] == fe.EditCue("n2", 5000, 7000, "")
    assert fe.cue_delete(inserted, "n2").cues == merged.cues
    with pytest.raises(fe.FinalEditError):
        fe.cue_split(document, "c1", 1000, "a", "b")


def test_merge_skips_hidden_cue() -> None:
    document = fe.ripple_delete(_doc([(0, 300)], CUES), 90, 120)
    merged = fe.cue_merge_next(document, "c4")  # 다음 보이는 자막은 c3(c2는 가려졌다)
    assert "c3" not in [cue.cue_id for cue in merged.cues]
    assert "c2" in [cue.cue_id for cue in merged.cues]


def test_split_text_rules() -> None:
    assert fe.split_text("가\n나") == ("가", "나")
    assert fe.split_text("가나다") == ("가나다", "")
    assert fe.split_text("ab cd ef") == ("ab cd", "ef")  # 가운데(4)에서 같은 거리면 앞 공백


def test_shift_clamps_and_set_times_checks() -> None:
    document = _doc([(0, 300)], [("c1", 500, 1500, "a")])
    left = fe.cue_shift(document, "c1", -900).cues[0]
    right = fe.cue_shift(document, "c1", 99999).cues[0]
    assert (left.start_ms, left.end_ms) == (0, 1000)
    assert (right.start_ms, right.end_ms) == (9000, 10000)
    with pytest.raises(fe.FinalEditError):
        fe.cue_set_times(document, "c1", 2000, 1000)
    resorted = fe.cue_set_times(_doc([(0, 300)], CUES), "c5", 0, 100)
    assert resorted.cues[0].cue_id == "c5"


def test_cues_delete_inside_keeps_video() -> None:
    document = _doc([(0, 300)], CUES)
    after = fe.cues_delete_inside(document, 2500, 4500)
    assert [cue.cue_id for cue in after.cues] == ["c1", "c5"]
    assert after.clips == document.clips
    with pytest.raises(fe.FinalEditError, match="없다"):
        fe.cues_delete_inside(document, 0, 100)


def test_set_text_refuses_blank_line() -> None:
    with pytest.raises(fe.FinalEditError):
        fe.cue_set_text(_doc([(0, 300)], [("c1", 0, 1000, "a")]), "c1", "a\n\nb")


# ─── 경고 ────────────────────────────────────────────────────────────────


def test_warnings_follow_refine_limits() -> None:
    cues = [
        ("c1", 0, 500, "짧"),  # 짧음
        ("c2", 500, 8000, "길게"),  # 김 · 다음과 틈 좁음
        ("c3", 8050, 9000, "열세글자가넘는빠른자막입니다요"),  # 빠름 · 다음과 겹침
        ("c4", 8900, 9900, ""),  # 빈 글
    ]
    found = {(w.cue_id, w.kind) for w in fe.warnings(_doc([(0, 300)], cues))}
    assert found == {
        ("c1", "short"),
        ("c2", "long"),
        ("c2", "gap"),
        ("c3", "fast"),
        ("c3", "overlap"),
        ("c4", "empty"),
    }


# ─── 공용 사례(브라우저 JS와 같은 값) ────────────────────────────────────


def _run_case(case: dict) -> dict:
    draft = fe.DraftFacts(
        F(case["draft"]["num"], case["draft"]["den"]), case["draft"]["totalFrames"]
    )
    document = fe.from_payload(case["doc"], draft)
    try:
        if case["op"] == "project":
            projection = fe.project(document)
            rate = fe.tick_rate(draft)
            return {
                "cues": [
                    [c.cue_id, int(c.start * rate), int(c.end * rate), c.trimmed]
                    for c in projection.cues
                ],
                "hidden": [cue.cue_id for cue in projection.hidden],
                "srt": fe.srt_text(projection),
            }
        if case["op"] == "warnings":
            return {"warnings": [[w.cue_id, w.kind] for w in fe.warnings(document)]}
        if case["op"] in ("draft_ms_at", "edited_ticks"):
            return {"value": getattr(fe, case["op"])(document, *case["args"])}
        return {"doc": fe.to_payload(getattr(fe, case["op"])(document, *case["args"]))}
    except fe.FinalEditError:
        return {"error": True}


def test_shared_cases_match_python() -> None:
    cases = json.loads(CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 30
    for case in cases:
        assert _run_case(case) == case["expect"], case["name"]


def test_settings_are_the_module_default() -> None:
    assert FinalEditSettings() == FINAL_EDIT_SETTINGS
