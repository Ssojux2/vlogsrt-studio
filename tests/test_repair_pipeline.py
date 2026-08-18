"""되풀이 재전사가 파이프라인 안에서 실제로 되풀이를 걷어내는가 — 끝에서 끝까지.

되풀이가 든 합성 픽스처(`loopy`)를 쓴다. 네트워크도 ffmpeg도 없다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vlogsrt import pipeline
from vlogsrt.config import Config
from vlogsrt.models import REPAIR_COLLAPSED, REPAIR_REPLACED, WHY_LETTER
from vlogsrt.output import parse_srt

SYNTHETIC = Path(__file__).parent / "fixtures" / "synthetic"
VIDEO = Path("golden/loopy.mp4")
LOOP = "꼬불꼬불 꼬불하고 여기 안으로 들어갔다가"


@pytest.fixture(autouse=True)
def synthetic_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(SYNTHETIC))


def _srt_texts(path: Path) -> list[str]:
    return [cue.text for cue in parse_srt(path.read_text(encoding="utf-8"))]


# ─────────────────────────────────────────────────────────────
#  되풀이가 사라진다 — 이 태스크의 목적
# ─────────────────────────────────────────────────────────────


def test_the_loop_is_gone_from_the_srt(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert not any(LOOP in text for text in _srt_texts(result.outputs["srt"]))


def test_without_repair_the_loop_survives(tmp_path: Path) -> None:
    """대조군. 끄면 지금까지와 똑같이 되풀이가 그대로 나온다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True, repair_repeats=False)
    looped = [text for text in _srt_texts(result.outputs["srt"]) if LOOP in text]
    assert len(looped) >= 10


def test_the_rescued_words_take_its_place(tmp_path: Path) -> None:
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(_srt_texts(result.outputs["srt"]))
    assert "작은 광장이 나옵니다" in body
    assert "돌바닥이" in body


def test_speech_outside_the_loop_is_untouched(tmp_path: Path) -> None:
    """규칙 9 — 되풀이 밖은 한 글자도 잃지 않는다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(_srt_texts(result.outputs["srt"]))
    for said in ("안녕하세요", "노트르담", "생각보다 사람이", "구독 부탁드려요"):
        assert said in body


def test_replacement_times_stay_on_the_original_timeline(tmp_path: Path) -> None:
    """규칙 1 — 재전사 시각도 `세그먼트 시각 + 조각 오프셋`이다.

    픽스처의 조각 오프셋이 8.0초, 첫 세그먼트가 조각 좌표 2.0초이므로 원본 10.0초다.
    """
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    rescued = [seg for seg in result.trace.spine if "골목이" in seg.text]
    assert len(rescued) == 1
    assert rescued[0].start == pytest.approx(10.0)


def test_the_repair_is_recorded_in_the_trace(tmp_path: Path) -> None:
    """사람이 나중에 무엇이 갈렸는지 따질 수 있어야 한다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert [record.outcome for record in result.repairs] == [REPAIR_REPLACED]
    saved = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert saved["repairs"][0]["repeat"]["count"] == 10
    assert saved["repairs"][0]["before"] == 10


def test_the_spine_stays_ordered_and_traceable(tmp_path: Path) -> None:
    """규칙 2 — 모든 큐가 골격의 세그먼트로 되짚힌다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    known = {segment.id for segment in result.trace.spine}
    for cue in result.cues:
        assert cue.seg_ids
        assert set(cue.seg_ids) <= known
    starts = [segment.start for segment in result.trace.spine]
    assert starts == sorted(starts)


# ─────────────────────────────────────────────────────────────
#  회복 못 했을 때 — 첫 장만 남는다
# ─────────────────────────────────────────────────────────────


def test_a_missing_rescue_fixture_collapses_the_span(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """재전사할 응답이 없으면 회복 실패다. 1668장 대신 한 장만 남는다."""
    lonely = tmp_path / "no-rescue"
    lonely.mkdir()
    for name in ("loopy.whisper.json", "loopy.meta.json", "loopy.gpt.json"):
        (lonely / name).write_bytes((SYNTHETIC / name).read_bytes())
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(lonely))

    result = pipeline.run(VIDEO, Config(), tmp_path / "out", force_offline=True)
    assert [record.outcome for record in result.repairs] == [REPAIR_COLLAPSED]
    assert len([text for text in _srt_texts(result.outputs["srt"]) if LOOP in text]) == 1


def test_a_collapsed_span_still_leaves_the_rest_of_the_video(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """접는 것은 되풀이 구간뿐이다. 앞뒤 발화까지 데려가지 않는다."""
    lonely = tmp_path / "no-rescue"
    lonely.mkdir()
    for name in ("loopy.whisper.json", "loopy.meta.json", "loopy.gpt.json"):
        (lonely / name).write_bytes((SYNTHETIC / name).read_bytes())
    monkeypatch.setenv("VLOGSRT_FIXTURE_DIR", str(lonely))

    result = pipeline.run(VIDEO, Config(), tmp_path / "out", force_offline=True)
    body = " ".join(_srt_texts(result.outputs["srt"]))
    for said in ("안녕하세요", "노트르담", "생각보다 사람이", "구독 부탁드려요"):
        assert said in body


# ─────────────────────────────────────────────────────────────
#  검증과의 경계 — 재전사가 출력을 바꾸는 유일한 계층이다
# ─────────────────────────────────────────────────────────────


def test_verification_still_cannot_change_the_srt(tmp_path: Path) -> None:
    """규칙 5 — 되풀이가 있는 영상에서도 검증 on/off SRT 바이트가 같다."""
    plain = pipeline.run(
        VIDEO, Config(), tmp_path / "plain", force_offline=True, verify_with_second_model=False
    )
    checked = pipeline.run(VIDEO, Config(), tmp_path / "checked", force_offline=True)
    assert plain.outputs["srt"].read_bytes() == checked.outputs["srt"].read_bytes()


def test_the_leftover_loop_is_still_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """접힌 구간은 이미 되풀이가 아니다. 검증이 조용해진 것이 정상이다."""
    result = pipeline.run(VIDEO, Config(), tmp_path, force_offline=True)
    assert result.verification is not None
    assert result.verification.loops == ()


# ─────────────────────────────────────────────────────────────
#  긴 되풀이 — 조각마다 갈린다 (D28)
# ─────────────────────────────────────────────────────────────

LONG_VIDEO = Path("golden/loopylong.mp4")


def test_a_long_repeat_is_asked_in_pieces_not_in_one_lump(tmp_path: Path) -> None:
    """**D28 사고의 회귀 테스트.**

    300초짜리 되풀이를 한 조각으로 다시 물으면, 이미 whisper를 무너뜨린 오디오를
    무너뜨린 그 길이 그대로 다시 주는 셈이다.
    """
    cfg = Config()
    result = pipeline.run(LONG_VIDEO, cfg, tmp_path, force_offline=True)
    assert len(result.repairs) == 3, [record.span for record in result.repairs]
    for record in result.repairs:
        asked = record.span[1] - record.span[0]
        assert asked <= cfg.repair_chunk_seconds, f"{record.span}가 한도를 넘었다"


def test_one_piece_recovers_while_the_others_collapse(tmp_path: Path) -> None:
    """부분 회복. 예전에는 통째로 갈거나 통째로 접는 것뿐이었다."""
    result = pipeline.run(LONG_VIDEO, Config(), tmp_path, force_offline=True)
    assert [record.outcome for record in result.repairs] == [
        REPAIR_REPLACED,
        REPAIR_COLLAPSED,
        REPAIR_COLLAPSED,
    ]
    texts = _srt_texts(result.outputs["srt"])
    assert any("볕이 잘 들어옵니다" in text for text in texts), "되살아난 조각이 자막에 없다"
    assert len([text for text in texts if LOOP in text]) == 2, "접힌 조각마다 첫 장 하나"


def test_the_pieces_cover_the_repeat_without_a_gap(tmp_path: Path) -> None:
    """규칙 4 — 쪼개도 되풀이 구간 전체가 덮인다. 사이가 비면 그만큼 조용히 사라진다."""
    result = pipeline.run(LONG_VIDEO, Config(), tmp_path, force_offline=True)
    spans = sorted(record.span for record in result.repairs)
    for earlier, later in zip(spans, spans[1:], strict=False):
        assert earlier[1] == later[0], f"계획 사이가 비었다: {spans}"


def test_speech_around_the_long_repeat_survives(tmp_path: Path) -> None:
    """규칙 9 — 되풀이 밖은 한 글자도 잃지 않는다."""
    result = pipeline.run(LONG_VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(_srt_texts(result.outputs["srt"]))
    for said in ("안녕하세요", "노트르담", "카메라가 흔들려도", "사람들이 앉아 있어요"):
        assert said in body


# ─────────────────────────────────────────────────────────────
#  낱자 되풀이 — 세그먼트마다 글자 하나뿐인 구간 (D29)
# ─────────────────────────────────────────────────────────────

LETTER_VIDEO = Path("golden/loopyletter.mp4")


def test_a_letter_run_no_longer_walks_into_the_srt(tmp_path: Path) -> None:
    """**D29 사고의 회귀 테스트.**

    실측: 84~172초 44장이 전부 한 글자였는데 검출이 아무것도 못 짚어 재전사가 한 번도
    돌지 않았고, 44장이 경고 하나 없이 SRT로 나갔다.
    """
    result = pipeline.run(LETTER_VIDEO, Config(), tmp_path, force_offline=True)
    assert [record.outcome for record in result.repairs] == [REPAIR_COLLAPSED]
    letters = [text for text in _srt_texts(result.outputs["srt"]) if len(text.strip()) == 1]
    assert len(letters) == 1, f"접힌 구간에 첫 장 하나만 남아야 한다: {letters}"


def test_the_letter_run_is_reported_not_swallowed(tmp_path: Path) -> None:
    """규칙 9 — 접힌 구간은 반드시 기록에 남는다."""
    result = pipeline.run(LETTER_VIDEO, Config(), tmp_path, force_offline=True)
    record = result.repairs[0]
    assert record.repeat.reason == WHY_LETTER
    assert record.repeat.count == 44
    saved = json.loads((tmp_path / "trace.json").read_text(encoding="utf-8"))
    assert saved["repairs"][0]["repeat"]["reason"] == WHY_LETTER


def test_the_arbiter_is_only_asked_once_when_it_hears_nothing(tmp_path: Path) -> None:
    """심판이 빈 글자를 주면 더 뜨거운 whisper를 사지 않는다(D29)."""
    result = pipeline.run(LETTER_VIDEO, Config(), tmp_path, force_offline=True)
    record = result.repairs[0]
    assert record.arbiter_answered
    assert not record.arbiter_clean
    assert record.attempts == 1


def test_speech_around_the_letter_run_survives(tmp_path: Path) -> None:
    """규칙 9 — 되풀이 밖은 한 글자도 잃지 않는다."""
    result = pipeline.run(LETTER_VIDEO, Config(), tmp_path, force_offline=True)
    body = " ".join(_srt_texts(result.outputs["srt"]))
    for said in ("안녕하세요", "노트르담", "세컨드", "예쁜 차가 없어요", "구독 부탁드려요"):
        assert said in body
