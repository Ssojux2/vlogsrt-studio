"""빠진 슬롯·속도 전환 원본도 명목 격자에 맞춰 받는다(W-27c, D70) — 시계·정책·지문.

실데이터(새 라이브러리 아이폰 34개): W-27b(D69) 뒤에도 9개가 지원 밖이었다 — 빈 슬롯 둘
(IMG_8871·8877), r_frame_rate=120 선언 셋(8850·8854·8865), 24↔30 섞임 하나(8858), 30fps 재료를
ffprobe가 30000/1001로 읽은 셋(8861·8862·8870). 명목 프레임 길이를 패킷 간격에서 고르고(간격
길이 몫이 가장 큰 간격, Final Cut 레이트만) 가운데 붙잡기(슬롯 s는 `pts < 원점 + s·T + T/2`인
마지막 패킷)로 슬롯에 놓는다. 그 T에서 항등이면 snap(새 추출 길 없음), 아니면 map이다. map만
규칙 판(`grid-conform/v1`)을 지문·열쇠에 싣는다 — 균일·snap 원본은 바이트까지 W-27b 그대로다.
유료 호출 0회 — 가짜 ffprobe 출력만 쓴다(진짜 ffmpeg는 `test_grid_map_media.py`).
"""

from __future__ import annotations

from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_grid_conform import (
    SNAP_ONLY,
    T,
    _clock,
    _img_8863,
    _one_off,
    _packets,
    _stream_json,
)
from tests.raw.test_native_media_probe import _matrix_clock, _matrix_probe
from vlogsrt import preprocess
from vlogsrt.preprocess import GRID_CONFORM_TAG, GridConformPolicy
from vlogsrt.project.fcp_tables import FRAME_RATE_TAGS
from vlogsrt.project.raw import finalcut_export, media_refs, visual_review
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw import understanding_luna as luna
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_workflow import source_input_fingerprint
from vlogsrt.project.raw.config import RawModelRoles, RawWorkflowConfig
from vlogsrt.project.raw.service import _MeasuredSource, _plan_fingerprint_inputs


def _held(slots: tuple[int, ...]) -> list[int]:
    return [s for s in range(1, len(slots)) if slots[s] == slots[s - 1]]


def _hidden(slots: tuple[int, ...], packets: int) -> int:
    return packets - len(set(slots))


# ── 실데이터 모양 — 간격 순서는 실측 런을 그대로 옮겼다 ────────────────────────


def img_8877() -> list[int]:
    """IMG_8877: r=30 · 간격 19×2 · 20×443 · 21×3 · 25×2(순번 240·410) — 451패킷."""
    deltas = [20] * 240 + [25] + [20] * 21 + [21, 19, 21, 19, 21] + [20] * 143 + [25] + [20] * 39
    return _packets(deltas)


def img_8858() -> list[int]:
    """IMG_8858: r=24 · 25틱(24fps) 사이에 20틱(30fps) 두 토막 · 끝에 26/24 흔들림 — 401패킷."""
    deltas = (
        [25] * 150 + [20] * 42 + [25] * 114 + [20] * 32 + [25] * 54 + [26, 24, 26, 24, 26]
    ) + [25] * 3
    return _packets(deltas)


def img_8865() -> list[int]:
    """IMG_8865: r=120인데 20틱(30fps) — 25틱 둘(+5 · +5)이 쌓여 한 슬롯이 빈다. 904패킷."""
    deltas = (
        ([20] * 91 + [25] + [20] * 380 + [21, 19] + [20] * 3 + [21] + [20] * 2 + [19, 21])
        + [20] * 78
        + [25]
        + [20] * 342
    )
    return _packets(deltas)


def img_8861() -> list[int]:
    """IMG_8861: 20틱 138개 · 21틱 2개(순번 45·136) — ffprobe가 r_frame_rate를 30000/1001로
    읽는다."""
    return _packets([20] * 45 + [21] + [20] * 90 + [21] + [20] * 3)


def test_img_8877_holds_the_one_slot_its_late_jump_leaves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """+5 · +6 지터 위에 순번 410의 25틱이 +11틱을 만든다 — 그 뒤 패킷 k는 슬롯 k+1이다."""
    pts = img_8877()
    assert (len(pts), pts[-1]) == (451, 9011)  # 실측 패킷 수·마지막 pts

    clock = _clock(monkeypatch, tmp_path, _stream_json(pts))

    assert (clock.uniform_grid, clock.conform_kind) == (True, "map")
    assert clock.frame_duration == Fraction(1, 30)
    assert clock.frame_count == 452 == len(clock.slot_packets)  # 패킷 451개가 슬롯 452칸
    assert clock.exact_end == Fraction(452, 30)  # 명목 끝
    assert _held(clock.slot_packets) == [411]  # 슬롯 411은 패킷 410을 붙잡는다
    assert clock.slot_packets[410:413] == (410, 410, 411)
    assert clock.slot_packets[-1] == 450
    assert clock.max_deviation_ticks == 9  # 20(k+1) − (20k + 11)
    assert GRID_CONFORM_TAG in clock.grid_note and "빈 슬롯 1칸(허용 4칸)" in clock.grid_note
    assert "snap이 아닌 까닭: 슬롯 411이 비었다" in clock.grid_note  # 왜 snap이 아닌지 남는다


def test_img_8865_declares_120fps_but_is_a_30fps_map_with_one_held_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pts = img_8865()
    assert (len(pts), pts[-1]) == (904, 18071)
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="120/1"))
    assert (clock.conform_kind, clock.frame_duration) == ("map", Fraction(1, 30))
    assert clock.frame_count == 905 and len(_held(clock.slot_packets)) == 1
    assert "선언 r_frame_rate 120는 기록뿐" in clock.grid_note


def test_img_8858_lays_401_packets_on_386_slots_at_24fps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """25틱(24fps)이 간격 길이의 대부분이다 — 30fps 토막은 한 슬롯 창에 두 패킷이 들어 뒤가
    보인다."""
    pts = img_8858()
    assert (len(pts), pts[-1]) == (401, 9631)

    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="24/1"))

    assert (clock.conform_kind, clock.frame_duration, clock.frame_count) == (
        "map",
        Fraction(1, 24),
        386,
    )
    assert _held(clock.slot_packets) == []  # 빈 슬롯은 없다
    assert _hidden(clock.slot_packets, len(pts)) == 15  # 겹쳐 가려진 30fps 패킷
    assert clock.exact_end == Fraction(386, 24)


def test_img_8850_declares_120fps_and_becomes_a_24fps_map(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """간격 20×52(1040틱)·25×136(3400틱) — 명목은 25틱(24fps), r_frame_rate 120은 기록뿐이다."""
    pts = _packets([20] * 52 + [25] * 136)
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="120/1"))
    assert (clock.conform_kind, clock.frame_duration, clock.frame_count) == (
        "map",
        Fraction(1, 24),
        179,
    )
    assert _hidden(clock.slot_packets, len(pts)) == 10 and _held(clock.slot_packets) == []
    assert "snap이 아닌 까닭: 명목 25틱에서도 snap이 아니다" in clock.grid_note


def test_img_8861_is_a_snap_on_the_30fps_its_packets_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """r_frame_rate 30000/1001은 1/600의 정수 틱이 아니다 — 간격에서 다시 고른 20틱에서 항등이다.

    snap이라 슬롯 대응·맞춤 표지가 없다(snap 우선 — 새 추출 길이 없다, D70).
    """
    pts = img_8861()
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts, rate="30000/1001"))
    assert (clock.uniform_grid, clock.conform_kind) == (True, "snap")
    assert (clock.frame_duration, clock.frame_count) == (Fraction(1, 30), 141)
    assert (clock.max_deviation_ticks, clock.slot_packets) == (2, ())
    assert clock.exact_end == Fraction(141, 30)
    assert "다시 골랐다" in clock.grid_note and "30000/1001" in clock.grid_note
    assert GRID_CONFORM_TAG not in clock.grid_note


# ── 규칙 — 가운데 붙잡기·동점·거부 ─────────────────────────────────────────────


def test_a_packet_on_the_midpoint_belongs_to_the_later_slot() -> None:
    """슬롯 s는 `pts < 원점 + s·T + T/2`인 마지막 패킷 — 반 프레임 동점은 뒤 슬롯이다."""
    assert (preprocess._nearest_slot(9, T), preprocess._nearest_slot(10, T)) == (0, 1)
    assert preprocess._nearest_slot(29, T) == 1 and preprocess._nearest_slot(30, T) == 2
    # 70 = 3·20 + 10은 슬롯 4 — 슬롯 3은 40을 붙잡고, 70·80이 겹친 슬롯 4는 뒤(80)를 보인다.
    assert preprocess._slot_packets([0, 20, 40, 70, 80], T) == (0, 1, 2, 2, 4)


@pytest.mark.parametrize(("offset", "held", "hidden"), [(10, [300], 1), (-10, [], 0)])
def test_half_a_frame_is_a_map_not_a_snap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, offset: int, held: list[int], hidden: int
) -> None:
    """+10틱은 동점이라 뒤 슬롯(301)으로 가 슬롯 300이 빈다. −10틱은 항등이지만 반 프레임이다."""
    clock = _clock(monkeypatch, tmp_path, _stream_json(_one_off(offset)))
    assert (clock.uniform_grid, clock.conform_kind, clock.frame_count) == (True, "map", 600)
    assert _held(clock.slot_packets) == held
    assert _hidden(clock.slot_packets, 600) == hidden
    if offset < 0:
        assert clock.slot_packets == tuple(range(600))  # 항등 대응이어도 snap 조건(미만) 밖


def test_the_nominal_frame_is_the_interval_covering_the_most_time() -> None:
    """간격마다 `개수 × 간격` — 동점은 짧은 간격이다."""
    assert preprocess._nominal_ticks(_packets([20] * 30 + [30] * 20)) == 20  # 600 대 600
    assert preprocess._nominal_ticks(_packets([20] * 29 + [30] * 20)) == 30  # 580 대 600
    assert preprocess._nominal_ticks(_packets([25] * 3 + [20] * 4)) == 20  # 75 대 80


def test_a_tie_toward_the_shorter_interval_can_be_refused_for_its_held_slots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """동점(600 대 600)이 20틱을 고르면 30틱 토막은 두 슬롯마다 하나가 빈다 — 몫 상한을 넘는다."""
    pts = _packets([20] * 30 + [30] * 20)
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts))
    assert clock.uniform_grid is False
    assert "빈 슬롯 10칸" in clock.grid_note and "허용 1칸" in clock.grid_note


def test_an_unsupported_majority_rate_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """30틱(20fps)이 명목이면 Final Cut 타임라인에 놓을 격자가 없다 — 지원 밖 그대로다."""
    clock = _clock(monkeypatch, tmp_path, _stream_json(_packets([30] * 100)))
    assert clock.uniform_grid is False
    assert "격자 다시 맞춤 불가" in clock.grid_note
    assert "Final Cut 프레임 레이트가 아니다" in clock.grid_note
    assert (clock.conform_kind, clock.slot_packets, clock.frame_count) == ("uniform", (), 101)


def test_held_slots_beyond_the_share_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """빈 슬롯 허용은 `max(1, ⌊슬롯 수 / 100⌋)` — 202칸에 둘은 받고 203칸에 셋은 거부한다."""
    two = _packets([20] * 60 + [40] + [20] * 60 + [40] + [20] * 77)  # 200패킷
    clock = _clock(monkeypatch, tmp_path, _stream_json(two))
    assert (clock.conform_kind, clock.frame_count, len(_held(clock.slot_packets))) == (
        "map",
        202,
        2,
    )
    three = _packets([20] * 60 + [40] + [20] * 60 + [40] + [20] * 40 + [40] + [20] * 36)
    clock = _clock(monkeypatch, tmp_path, _stream_json(three))
    assert clock.uniform_grid is False
    assert "빈 슬롯 3칸" in clock.grid_note and "허용 2칸" in clock.grid_note


def test_non_monotonic_packets_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pts = [T * n for n in range(100)]
    pts[50] = pts[49]  # 같은 pts 두 패킷
    clock = _clock(monkeypatch, tmp_path, _stream_json(pts))
    assert clock.uniform_grid is False
    assert "격자 다시 맞춤 불가" in clock.grid_note and "늦지 않다" in clock.grid_note


def test_a_policy_without_rate_remap_keeps_the_declared_rate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """다시 고르기를 허락하지 않으면 선언과 같은 명목만 map으로 받는다(8877은 받고 8850은 거부)."""
    durations = frozenset(FRAME_RATE_TAGS)
    gaps_only = GridConformPolicy(max_dropped_slots=1, nominal_frame_durations=durations)
    accepted = _clock(monkeypatch, tmp_path, _stream_json(img_8877()), policy=gaps_only)
    assert accepted.conform_kind == "map"
    stdout = _stream_json(_packets([20] * 52 + [25] * 136), rate="120/1")
    refused = _clock(monkeypatch, tmp_path, stdout, policy=gaps_only)
    assert refused.uniform_grid is False
    assert "속도 다시 고르기를 허용하지 않는다" in refused.grid_note


# ── W-27b 회귀 — 받던 모양은 시계 바이트까지 그대로 ────────────────────────────────


def _w27b_accepted() -> list[tuple[str, str, str]]:
    shapes = {
        "8844": _packets([20] * 100 + [21] + [20] * 300 + [19] + [20] * 300 + [21] + [20] * 144),
        "6": _one_off(6),
        "+9": _one_off(9),
        "-9": _one_off(-9),
    }
    cases = [(name, _stream_json(pts), "snap") for name, pts in shapes.items()]
    uniform = [256 + 512 * n for n in range(100)]
    cases.append(("uniform", _stream_json(uniform, rate="25/1", tb="1/12800"), "uniform"))
    # 양자화된 30000/1001 @ 1/1000은 D107부터 받지 않는다(정수 틱이 아닌 균일은 검토 시계가 세울 수
    # 없다) — `tests/raw/test_quantized_grid_library.py`가 그 지원 밖 판정을 잠근다.
    return cases


@pytest.mark.parametrize(("name", "stdout", "kind"), _w27b_accepted())
def test_w27b_shapes_keep_their_clock_byte_for_byte(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, stdout: str, kind: str
) -> None:
    """D70의 정책으로 재도 snap만 아는 판과 시계가 칸마다 같다(기록 글까지) — 대응은 비어 있다."""
    opened = _clock(monkeypatch, tmp_path, stdout)
    narrow = _clock(monkeypatch, tmp_path, stdout, policy=SNAP_ONLY)
    assert opened == narrow, name
    assert (opened.conform_kind, opened.slot_packets) == (kind, ())


def test_img_8863_keeps_its_w27b_clock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    opened = _img_8863(monkeypatch, tmp_path)
    monkeypatch.setattr(preprocess, "RAW_GRID_CONFORM", SNAP_ONLY)
    assert opened == _img_8863(monkeypatch, tmp_path)


# ── 정책 — 설정에서 온다 ────────────────────────────────────────────────────────


def test_the_raw_policy_opens_map_with_a_bounded_share_of_held_slots() -> None:
    policy = preprocess.RAW_GRID_CONFORM
    assert policy == GridConformPolicy(
        max_dropped_slots=1,
        half_frame_bound=Fraction(1, 2),
        allow_rate_remap=True,
        max_dropped_share=Fraction(1, 100),
        nominal_frame_durations=policy.nominal_frame_durations,
    )
    assert policy.nominal_frame_durations == frozenset(FRAME_RATE_TAGS)  # Final Cut 레이트 표
    assert policy.maps is True
    allowances = [policy.dropped_allowance(n) for n in (50, 100, 199, 200, 452, 905)]
    assert allowances == [1, 1, 1, 2, 4, 9]
    with pytest.raises(ValueError):
        GridConformPolicy(max_dropped_share=Fraction(1))
    with pytest.raises(ValueError):
        GridConformPolicy(nominal_frame_durations=frozenset({Fraction(0)}))


# ── 지문·열쇠 — map만 규칙 판, 균일·snap은 W-27b 바이트 그대로 ──────────────────────

SETTINGS = RawUnderstandingSettings()
WORKFLOW = RawWorkflowConfig(models=replace(RawModelRoles(), luna_model="claude-sonnet-5-5"))
"""지문 고정값은 W-27b 판의 기본 설정(그때 기본 분석 모델 Sonnet 5.5, D58)으로 잰 것이다 — 기본이
Flash로 바뀐 뒤(D87)에도 같은 입력을 쓰려고 모델을 못 박는다."""
GRID = stage1.VideoGrid(0, Fraction(0), Fraction(1, 30), 848)
MAPPED = replace(GRID, slot_packets=tuple(range(847)) + (846,))
# 아래 값은 W-27b 판(이 변경 앞의 트리)에서 같은 입력으로 잰 것이다 — 균일·snap은 바이트 그대로다.
INPUT_W27B = "9b65928f52ba5db8b6108ee84284fe5f21f651d7c69e41b76b28b0844a102f09"
VISUAL_W27B = "4f0e042ffcaa9c054c008c13b93fc4488825f61791674ad4d69690d514c40d44"
AUDIO_W27B = "899cdc6c5f37aa1f49d5f5c894af9d0c5e18deab91bad566a4abcab52a1a9647"
THUMB_W27B = "524abe376454538a4a98a408"


def _input(video: stage1.VideoGrid) -> str:
    return source_input_fingerprint(
        source_sha256="a" * 64, video=video, audio=None, settings=SETTINGS, workflow=WORKFLOW
    )


def _visual(video: stage1.VideoGrid) -> str:
    timeline = stage1.common_timeline(source_id="s", video=video, audio=None)
    plan = stage1.plan_common_windows(timeline, (), SETTINGS)
    return luna.visual_analysis_fingerprint(
        source_sha256="a" * 64,
        video=video,
        plan=plan,
        settings=SETTINGS,
        luna_model=WORKFLOW.models.luna_model,
    )


def _audio_fp(video: stage1.VideoGrid) -> str:
    audio = stage1.SelectedAudio(
        source_id="s",
        source_path=Path("originals/x.mov"),
        source_sha256="a" * 64,
        audio_stream_index=1,
        pcm_path=Path("analysis.pcm"),
        pcm_sha256="b" * 64,
        sample_rate=16000,
        channels=2,
        sample_count=96000,
        pcm_sample0_file_time=Fraction(1, 5),
    )
    timeline = stage1.common_timeline(source_id="s", video=video, audio=audio)
    plan = stage1.plan_common_windows(timeline, (), SETTINGS)
    return luna.analysis_fingerprint(
        audio=audio,
        plan=plan,
        settings=SETTINGS,
        asr_model=WORKFLOW.models.timing_model,
        luna_model=WORKFLOW.models.luna_model,
    )


def test_analysis_fingerprints_carry_the_tag_only_for_a_map_grid() -> None:
    assert (_input(GRID), _visual(GRID), _audio_fp(GRID)) == (INPUT_W27B, VISUAL_W27B, AUDIO_W27B)
    assert _input(MAPPED) != INPUT_W27B
    assert _visual(MAPPED) != VISUAL_W27B
    assert _audio_fp(MAPPED) != AUDIO_W27B


def test_the_thumbnail_key_carries_the_tag_only_for_a_map_source() -> None:
    assert visual_review.thumbnail_media_key("a" * 64, 7) == THUMB_W27B
    assert visual_review.thumbnail_media_key("a" * 64, 7, mapped=False) == THUMB_W27B
    assert visual_review.thumbnail_media_key("a" * 64, 7, mapped=True) != THUMB_W27B


def test_the_frame_source_carries_the_tag_only_for_a_map_source() -> None:
    assert media_refs._conform_suffix(False) == ""
    assert media_refs._conform_suffix(True) == f"+{GRID_CONFORM_TAG}"


def _plan_inputs(clock: preprocess.RawVideoClock) -> dict[str, object]:
    asset: Any = SimpleNamespace(content_sha256="c" * 64)
    spans: Any = [SimpleNamespace(core_start_pts=0, core_end_pts=clock.frame_count * T)]
    return _plan_fingerprint_inputs(asset, _MeasuredSource(clock, clock.frame_count * T), spans)


def test_the_plan_fingerprint_inputs_carry_the_tag_only_for_a_map_source() -> None:
    tb = Fraction(1, 600)
    common: dict[str, object] = {
        "time_base": tb,
        "last_pts": 59 * T,
        "frame_duration": Fraction(1, 30),
        "frame_count": 60,
        "exact_end": Fraction(2),
    }
    expected = {
        "content_sha256": "c" * 64,
        "time_base": "1/600",
        "origin_pts": 0,
        "frame_count": 60,
        "frame_duration": "1/30",
        "boundaries": [0, 60 * T],
    }
    snapped = _matrix_clock(**common, max_deviation_ticks=6, conform_kind="snap")
    assert _plan_inputs(_matrix_clock(**common)) == expected  # W-27b 판의 칸 그대로
    assert _plan_inputs(snapped) == expected
    mapped = _matrix_clock(**common, conform_kind="map", slot_packets=tuple(range(60)))
    assert _plan_inputs(mapped) == {**expected, "grid_conform": GRID_CONFORM_TAG}


# ── 추출 argv·지원 매트릭스 ─────────────────────────────────────────────────────


def _map_clock() -> preprocess.RawVideoClock:
    """60슬롯 — 슬롯 20이 빈다(패킷 19를 붙잡는다), 패킷 59개."""
    slots = tuple(range(20)) + tuple(range(19, 59))
    return _matrix_clock(
        time_base=Fraction(1, 600),
        last_pts=59 * T,
        frame_duration=Fraction(1, 30),
        frame_count=60,
        exact_end=Fraction(2),
        max_deviation_ticks=0,
        conform_kind="map",
        slot_packets=slots,
    )


def _plan(clock: preprocess.RawVideoClock, first: int, after: int, **kwargs: Any) -> Any:
    span = preprocess.clock_frame_range(clock, first, after)
    return preprocess._plan_raw_media(clock, span, preprocess.RawClipHandles(), (), **kwargs)


def test_a_map_extraction_reads_video_and_audio_from_two_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """영상은 정확 탐색 없이 `fps`가 슬롯을 고르고, 소리는 첫 슬롯 시각에서 자른다(시프트 0)."""
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    clock = _map_clock()
    probe = _matrix_probe(time_base=Fraction(1, 600), r_frame_rate=Fraction(30))
    plan = _plan(clock, 21, 40)  # 첫 슬롯 21 = 패킷 20, 처음 보인 슬롯 21 → 한 프레임 앞(슬롯 20)
    assert (plan.seek, plan.common_shift_ticks, plan.snap_ticks_per_frame) == (
        Fraction(21, 30),
        0,
        0,
    )
    assert plan.slot_window.seek == Fraction(20, 30) and plan.encoder_time_base is None
    held = _plan(clock, 20, 40)  # 슬롯 20은 패킷 19(슬롯 19에서 처음 보임) → 슬롯 18에서 찾는다
    assert held.slot_window.seek == Fraction(18, 30)
    argv = preprocess._raw_extract_command(
        Path("/src/clip.mov"), Path("/stage/out.part"), probe, preprocess.RAW_SOURCE_MATCHED, plan
    )
    assert argv[argv.index("-noaccurate_seek") + 1 : argv.index("-noaccurate_seek") + 3] == [
        "-ss",
        "0.666666",
    ]
    assert argv[argv.index("-vf") + 1] == (
        "setpts=PTS+400,fps=fps=30/1:round=near,trim=start_pts=21:end_pts=40,setpts=PTS-21"
    )
    assert "-copyts" not in argv and "-af" not in argv
    assert any("격자 맞춤(map, D70)" in warning for warning in plan.warnings)


def test_a_remapped_snap_sets_the_encoder_clock_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """r_frame_rate 30000/1001인 30fps snap — `-enc_time_base 1/30` 하나만 더한다(D70)."""
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda tool="ffmpeg": tool)
    tb = Fraction(1, 600)
    clock = _matrix_clock(
        time_base=tb,
        last_pts=59 * T,
        frame_duration=Fraction(1, 30),
        frame_count=60,
        exact_end=Fraction(2),
        max_deviation_ticks=2,
        conform_kind="snap",
    )
    probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30000, 1001))
    remapped = _plan(clock, 15, 45, declared_rate=probe.video.r_frame_rate)
    assert remapped.encoder_time_base == Fraction(1, 30)
    argv = preprocess._raw_extract_command(
        Path("/src/clip.mov"), Path("/out"), probe, preprocess.RAW_SOURCE_MATCHED, remapped
    )
    same = _plan(clock, 15, 45, declared_rate=Fraction(30))
    assert same.encoder_time_base is None
    plain = preprocess._raw_extract_command(
        Path("/src/clip.mov"), Path("/out"), probe, preprocess.RAW_SOURCE_MATCHED, same
    )
    at = argv.index("-enc_time_base")
    assert argv[at : at + 2] == ["-enc_time_base", "1/30"]
    assert argv[:at] + argv[at + 2 :] == plain


def test_the_declared_window_asks_a_map_source_to_start_its_last_packet_inside() -> None:
    """map의 마지막 패킷은 명목보다 짧게 보일 수 있다 — 선언 창 안에서 **시작**하는지만 본다."""
    tb = Fraction(1, 600)
    probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30), duration_ts=1800)
    probe = replace(probe, duration=Fraction(3))
    inside = replace(_map_clock(), last_pts=1799)
    preprocess._assert_raw_supported(probe, inside)
    with pytest.raises(preprocess.RawMediaUnsupported, match="선언 창"):
        preprocess._assert_raw_supported(probe, replace(inside, last_pts=1800))


# ── Final Cut — 머리 정보·라이브러리 선언은 map에서만 한 프레임 미만 ─────────────────


def test_a_map_header_may_differ_by_less_than_one_frame() -> None:
    """실측 IMG_8877: 머리 정보 9030틱 = 451.5프레임 대 명목 452 — 반 프레임 차이를 받는다."""
    asset: Any = SimpleNamespace(source_name="IMG_8877.mov")
    tolerance = finalcut_export._grid_tolerance(_map_clock())
    assert tolerance == Fraction(19, 20)
    tb = Fraction(1, 600)
    for ticks, ok in ((9030, True), (9040 - 19, True), (9040 + 19, True), (9040 - 20, False)):
        probe = _matrix_probe(time_base=tb, r_frame_rate=Fraction(30), duration_ts=ticks)
        args = (asset, 0, 452, Fraction(1, 30), probe, tolerance)
        if ok:
            finalcut_export._require_plain_timeline(*args)
        else:
            with pytest.raises(finalcut_export.FinalCutExportError, match="편집 목록"):
                finalcut_export._require_plain_timeline(*args)
    snapped = replace(_map_clock(), conform_kind="snap", slot_packets=(), max_deviation_ticks=6)
    assert finalcut_export._grid_tolerance(snapped) == Fraction(6, 20)  # W-27b 그대로
