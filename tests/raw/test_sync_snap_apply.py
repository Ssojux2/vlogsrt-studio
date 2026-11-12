"""D59 19h — 시각 맞춤 적용·되돌리기: 원본 자막 쓰기(`apply_changes`·`restore`)와 서비스의 문.

유료 호출은 없다 — 빈칸 재전사를 함께 도는 시험만 요청을 세는 가짜 전송이 받는다(환경 문은 그
시험 안에서만 가짜로 연다). 시험이 못 박는 것:

- 적용은 리비전 하나다 — **모든** 줄의 새 시각을 함께(일부만이면 거절), 바뀌는 칸은 `start`·`end`
  뿐이고 글자·아이디·순서·수·증거 칸은 바이트 그대로, `edited=True`, 출처 기록, 편집 검사 통과.
  「옮김」만 적용한 꼴은 편집 검사가 거절한다(0011 꼴) — 전부를 함께 쓰는 길 하나뿐이다.
- 적용은 화면이 본 제안 sha에 묶인다 — 자막 편집·다시 잰 측정·기준 변경·다른 탭이면 아무것도 쓰지
  않는다.
- 되돌리기는 적용 전 줄과 `edited`를 바이트 그대로 되살린다 — 지문·다시 듣기 자격이 적용 전과 같다.
  그 뒤에 고치거나 넣은 자막이 있으면 거절한다. 두 번 눌러도 하나만 된다.
- 순서: 다시 듣기 대기·빈칸 재전사 중에는 적용하지 않는다. 적용한 자막은 다시 듣지 않고(되돌리는
  길을 말한다), 빈칸 재전사는 적용 뒤에도 된다(그러면 되돌리기는 거절된다).
- 시계: 제안(PCM 시계) → 원본 자막(공통 시계)은 `audio_start`만큼 정확 유리수로 옮긴다.
- 길이 0 줄이 있어도 적용된다(그 줄은 그대로 남는다, D55 버그 3).
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from tests._sync_fakes import BURSTS, energy_factory, make_media, numpy_or_fake
from tests.raw.test_speech_refill import RefillSender, context, transport
from tests.raw.test_speech_refill_edges import Blocking
from vlogsrt.project import config as project_config
from vlogsrt.project import sync_check, sync_snap
from vlogsrt.project.raw import asr_epoch, source_transcript, speech_refill, sync_report
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.models import RawWorkspaceError

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
EARLY = Fraction(3, 2)  # 원본 자막이 말보다 1.5초 이르다 — 확인이 세는 전체 어긋남
QUIET = (Fraction(88, 10), Fraction(93, 10))  # 두 덩어리 사이 조용한 곳의 줄 — 맞추면 뒤집혀 막힌다
ZERO = Fraction(1, 5)  # 길이 0 줄(D55 버그 3 꼴)
enc = source_transcript.encoded


# ── 재료 ─────────────────────────────────────────────────────────────────────


def _wait(service: raw.RawWorkspaceService, root: Path, job_id: str) -> Any:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        record = service.get_job(root, job_id)
        if record.status in TERMINAL:
            return record
        time.sleep(0.02)
    raise AssertionError(f"작업이 끝나지 않았다: {job_id}")


def wait_sync(service: raw.RawWorkspaceService, root: Path) -> None:
    latest = service.latest_job(root, raw.SYNC_CHECK_JOB_KIND)
    assert latest is not None
    assert _wait(service, root, latest.job_id).status == "succeeded"


def snap_rows(origin: Fraction = Fraction(0)) -> list[dict[str, Any]]:
    """덩어리 1–13에 1.5초 이른 줄 · 맨 앞 길이 0 줄 · 조용한 곳의 줄 하나(공통 시계)."""
    rows = [{"utterance_id": "z0", "text": "음", "start": enc(ZERO), "end": enc(ZERO)}]
    for index, (start, end) in enumerate(BURSTS[1:], start=1):
        rows.append(
            {
                "utterance_id": f"c{index:02d}",
                "text": f"말 {index}",
                "start": enc(Fraction(start) + origin - EARLY),
                "end": enc(Fraction(end) + origin - EARLY),
                "start_sample": index * 1000,
                "end_sample": index * 1000 + 500,
                "evidence_id": f"req:{index}",
            }
        )
        if index == 3:
            rows.append(
                {
                    "utterance_id": "q",
                    "text": "조용",
                    "start": enc(QUIET[0] + origin),
                    "end": enc(QUIET[1] + origin),
                }
            )
    return rows


def snap_workspace(tmp_path: Path, audio_delay: str = "0") -> tuple[Any, Path, str]:
    """톤 덩어리 원본 하나에 이른 원본 자막을 심고 자막 시각 확인(가짜 에너지 VAD)까지 끝낸
    작업공간."""
    card = tmp_path / "card"
    make_media(card / "tone.mov", audio_delay=audio_delay)
    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "시각 맞춤")
    proposal = service.scan_sources(root, card)
    copied = service.start_ingest_copy(root, service.confirm_ingest(root, proposal))
    assert _wait(service, root, copied.job_id).status == "succeeded"
    service.confirm_global_source_order(root)
    asset = service.list_source_assets(root)[0]
    rows = snap_rows(Fraction(audio_delay))
    workspace = service._workspace(root)
    source_transcript.seed(workspace, asset.asset_id, rows, Fraction(40), asset.content_sha256)
    assert service.start_sync_checks(root, (asset.asset_id,)) is not None
    wait_sync(service, root)
    return service, root, asset.asset_id


@pytest.fixture
def snapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Path, str]:
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    return snap_workspace(tmp_path)


def _plain(tmp_path: Path) -> tuple[Any, str, dict[str, Any]]:
    """미디어 없는 작업공간 하나에 심은 원본 자막 — 쓰기 함수만 볼 때."""
    service = raw.RawWorkspaceService()
    root = tmp_path / "plain"
    service.create_workspace(root, "쓰기")
    workspace = service._workspace(root)
    rows = [
        {
            "utterance_id": "a",
            "text": "첫째",
            "start": enc(Fraction(1)),
            "end": enc(Fraction(2)),
            "chunk_index": 0,
            "position": 0,
            "start_sample": 16000,
            "end_sample": 32000,
            "evidence_id": "req:0",
            "whisper_text": "첫째",
            "text_source": "whisper",
            "retext_reason": "",
            "alt_text": "",
        },
        {
            "utterance_id": "z",
            "text": "길이 영",
            "start": enc(Fraction(3)),
            "end": enc(Fraction(3)),
        },
        {"utterance_id": "b", "text": "둘째", "start": enc(Fraction(5)), "end": enc(Fraction(6))},
    ]
    document = source_transcript.seed(workspace, "asset_x", rows, Fraction(10), "sha")
    return workspace, "asset_x", document


def _times(
    document: dict[str, Any], **moves: tuple[str, str]
) -> list[tuple[str, Fraction, Fraction]]:
    """모든 줄의 (아이디, 시작, 끝) — `moves`에 적은 줄만 새 시각."""
    found = []
    for row in document["utterances"]:
        start, end = (
            source_transcript.rational(row["start"]),
            source_transcript.rational(row["end"]),
        )
        if row["utterance_id"] in moves:
            start, end = (Fraction(value) for value in moves[row["utterance_id"]])
        found.append((row["utterance_id"], start, end))
    return found


def _revisions(workspace: Any, asset_id: str) -> int:
    with workspace.connect() as db:
        return int(
            db.execute(
                "SELECT COUNT(*) FROM source_transcripts WHERE asset_id=?", (asset_id,)
            ).fetchone()[0]
        )


def _without_times(row: dict[str, Any]) -> str:
    return json.dumps(
        {k: v for k, v in row.items() if k not in ("start", "end")}, ensure_ascii=False
    )


def _apply(workspace: Any, asset_id: str, document: dict[str, Any], times: list[Any]) -> dict:
    return source_transcript.apply_changes(
        workspace,
        asset_id,
        document["revision"],
        times,
        plan_sha256="f" * 64,
        settings_version="sync-snap/v1",
    )


# ── 쓰기 — 리비전 하나, 시각만, 전부 아니면 전무 ──────────────────────────────


def test_apply_writes_one_revision_with_only_the_times_changed(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    times = _times(seeded, a=("9/10", "21/10"), b=("49/10", "63/10"))
    written = _apply(workspace, asset_id, seeded, times)
    stored = source_transcript.load(workspace, asset_id)
    assert stored == written and _revisions(workspace, asset_id) == 2
    assert stored["sequence"] == 1 and stored["edited"] is True
    assert stored["revision"] != seeded["revision"]
    before, after = seeded["utterances"], stored["utterances"]
    assert [r["utterance_id"] for r in after] == [r["utterance_id"] for r in before]
    for old, new in zip(before, after, strict=True):
        assert _without_times(old) == _without_times(new)  # 글자·아이디·증거 칸(틱 포함) 그대로
    assert json.dumps(after[1], ensure_ascii=False) == json.dumps(before[1], ensure_ascii=False)
    assert after[0]["start"] == enc(Fraction(9, 10)) and after[0]["end"] == enc(Fraction(21, 10))
    assert stored["dirty_ranges"] == [
        {"start": enc(Fraction(9, 10)), "end": enc(Fraction(21, 10))},
        {"start": enc(Fraction(49, 10)), "end": enc(Fraction(63, 10))},
    ]
    assert stored["snap"] == {
        "schema": source_transcript.SNAP_SCHEMA,
        "revision": stored["revision"],
        "from_revision": seeded["revision"],
        "from_edited": False,
        "plan_sha256": "f" * 64,
        "settings_version": "sync-snap/v1",
        "changed": 2,
    }
    assert source_transcript.snap_applied(stored) == stored["snap"]
    source_transcript._validate_rows(after, Fraction(10), {"a", "b"})  # 편집 검사를 지난다


@pytest.mark.parametrize("shape", ["missing", "reordered", "extra", "renamed"])
def test_apply_takes_every_row_or_none(tmp_path: Path, shape: str) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    times = _times(seeded, a=("9/10", "2"))
    if shape == "missing":
        times = times[:1] + times[2:]  # 「옮김」 줄만 — 일부만 주는 꼴
    elif shape == "reordered":
        times = [times[2], times[1], times[0]]
    elif shape == "extra":
        times = [*times, ("new", Fraction(8), Fraction(9))]
    else:
        times = [("x", times[0][1], times[0][2]), *times[1:]]
    with pytest.raises(RawWorkspaceError) as refused:
        _apply(workspace, asset_id, seeded, times)
    assert str(refused.value) == source_transcript.SNAP_ROWS_MISMATCH
    assert _revisions(workspace, asset_id) == 1


def test_apply_needs_exact_fractions_and_a_change(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    floats = [(row_id, float(start), float(end)) for row_id, start, end in _times(seeded)]
    with pytest.raises(TypeError):
        _apply(workspace, asset_id, seeded, floats)
    with pytest.raises(RawWorkspaceError) as same:
        _apply(workspace, asset_id, seeded, _times(seeded))
    assert str(same.value) == source_transcript.SNAP_NO_CHANGE
    assert _revisions(workspace, asset_id) == 1


def test_apply_refuses_a_revision_that_moved_on(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    edited = source_transcript.edit(
        workspace, asset_id, seeded["revision"], action="edit", utterance_id="b", text="고친 말"
    )
    with pytest.raises(RawWorkspaceError) as refused:
        _apply(workspace, asset_id, seeded, _times(seeded, a=("9/10", "2")))
    assert str(refused.value) == source_transcript.SNAP_REVISION_CHANGED
    assert source_transcript.load(workspace, asset_id) == edited


def test_applying_only_the_moved_rows_is_refused_by_the_edit_check(tmp_path: Path) -> None:
    """0011 꼴 — 밀린 이웃만 옮기고 막힌 줄을 두면 겹친다. 모든 줄을 함께 옮기면 지난다."""
    service = raw.RawWorkspaceService()
    root = tmp_path / "shift"
    service.create_workspace(root, "밀기")
    workspace = service._workspace(root)
    rows = [
        {"utterance_id": "m", "text": "옮김", "start": enc(Fraction(1)), "end": enc(Fraction(2))},
        {"utterance_id": "k", "text": "막힘", "start": enc(Fraction(3)), "end": enc(Fraction(4))},
    ]
    seeded = source_transcript.seed(workspace, "asset_s", rows, Fraction(10), "sha")
    partial = _times(seeded, m=("5/2", "7/2"))  # 「옮김」만 +1.5초 — 막힌 줄과 겹친다
    with pytest.raises(RawWorkspaceError) as refused:
        _apply(workspace, "asset_s", seeded, partial)
    assert "2번 자막 시각이 앞 자막과 겹칩니다" in str(refused.value)
    assert _revisions(workspace, "asset_s") == 1
    whole = _times(seeded, m=("5/2", "7/2"), k=("9/2", "11/2"))  # 막힌 줄도 밀기만큼 함께
    assert _apply(workspace, "asset_s", seeded, whole)["sequence"] == 1


def test_two_applies_on_one_revision_write_once(tmp_path: Path) -> None:
    """두 탭이 같은 순간 적용해도 하나만 쓴다 — 리비전 대조는 쓰기 잠금 안에서 한다."""
    workspace, asset_id, seeded = _plain(tmp_path)
    times = _times(seeded, a=("9/10", "2"))
    gate = threading.Barrier(2)
    outcomes: list[object] = []

    def press() -> None:
        gate.wait()
        try:
            outcomes.append(_apply(workspace, asset_id, seeded, times)["revision"])
        except RawWorkspaceError as error:
            outcomes.append(error)

    threads = [threading.Thread(target=press) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(item, str) for item in outcomes) == 1
    (lost,) = [item for item in outcomes if isinstance(item, RawWorkspaceError)]
    assert (
        str(lost) == source_transcript.SNAP_REVISION_CHANGED
    )  # 「아무것도 바꾸지 않았습니다」로 끝난다
    assert _revisions(workspace, asset_id) == 2


def test_zero_length_rows_stay_and_the_rest_moves(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    stored = _apply(workspace, asset_id, seeded, _times(seeded, a=("1/2", "5/2")))
    zero = stored["utterances"][1]
    assert zero == seeded["utterances"][1] and zero["start"] == zero["end"]


# ── 되돌리기 — 바이트 그대로, 그 뒤 편집은 지우지 않는다 ─────────────────────────


def test_restore_brings_back_rows_edited_and_fingerprint(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    before_fp = source_transcript.fingerprint(workspace, asset_id)
    before_bind = [
        source_transcript.binding_fingerprint(workspace, asset_id, on) for on in (True, False)
    ]
    applied = _apply(workspace, asset_id, seeded, _times(seeded, a=("9/10", "2")))
    assert source_transcript.fingerprint(workspace, asset_id) != before_fp
    restored = source_transcript.restore(workspace, asset_id, applied["revision"])
    assert json.dumps(restored["utterances"], ensure_ascii=False) == json.dumps(
        seeded["utterances"], ensure_ascii=False
    )
    assert restored["edited"] is False and source_transcript.human_edited(restored) is False
    assert source_transcript.fingerprint(workspace, asset_id) == before_fp
    after_bind = [
        source_transcript.binding_fingerprint(workspace, asset_id, on) for on in (True, False)
    ]
    assert after_bind == before_bind
    assert "snap" not in restored and source_transcript.snap_applied(restored) is None
    assert restored["snap_undo"] == {
        "schema": source_transcript.SNAP_UNDO_SCHEMA,
        "revision": restored["revision"],
        "undone_revision": applied["revision"],
        "restored_revision": seeded["revision"],
        "plan_sha256": "f" * 64,
    }
    assert restored["dirty_ranges"] == applied["dirty_ranges"] * 2
    # 같은 발화로 다시 심어도 새 리비전이 생기지 않는다(고치지 않은 자막과 같은 문서).
    again = source_transcript.seed(workspace, asset_id, seeded["utterances"], Fraction(10), "sha")
    assert again["revision"] == restored["revision"]


def test_restore_of_an_edited_transcript_keeps_it_edited(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    edited = source_transcript.edit(
        workspace, asset_id, seeded["revision"], action="edit", utterance_id="b", text="고친 말"
    )
    applied = _apply(workspace, asset_id, edited, _times(edited, a=("9/10", "2")))
    assert applied["snap"]["from_edited"] is True
    restored = source_transcript.restore(workspace, asset_id, applied["revision"])
    assert restored["utterances"] == edited["utterances"] and restored["edited"] is True


def _legacy(tmp_path: Path, shape: str) -> tuple[Any, str, dict[str, Any]]:
    """지금 리비전의 `edited` 칸 모양 — 있음(거짓·참) · 없음(차례 0) · 없음(차례 > 0, 옛 문서)."""
    workspace, asset_id, seeded = _plain(tmp_path)
    current = seeded
    if shape in ("present_true", "missing_seq_gt_0"):
        current = source_transcript.edit(
            workspace, asset_id, seeded["revision"], action="edit", utterance_id="b", text="고침"
        )
    if shape.startswith("missing"):
        old = {key: value for key, value in current.items() if key != "edited"}
        with workspace.connect() as db:
            db.execute(
                "UPDATE source_transcripts SET payload=? WHERE revision=?",
                (json.dumps(old, ensure_ascii=False), current["revision"]),
            )
            db.commit()
        current = old
    return workspace, asset_id, current


@pytest.mark.parametrize(
    "shape", ["present_false", "present_true", "missing_seq_0", "missing_seq_gt_0"]
)
def test_restore_keeps_human_edited_and_seed_exactly_as_before(tmp_path: Path, shape: str) -> None:
    """되돌린 리비전에서 `human_edited`와 심기(`seed`)가 적용 전과 똑같이 굴러간다 — 옛 문서도.

    칸이 없고 차례 > 0인 옛 문서는 칸을 빼 둔다(되돌린 차례도 > 0이라 고친 자막으로 읽히고, 심기는
    칸이 없는 문서로 본다). 칸이 없고 차례 0이면 `edited=False`를 적는다(19h 검증 뒤 고침)."""
    twin_ws, twin_id, twin = _legacy(tmp_path / "twin", shape)  # 적용하지 않은 쌍둥이
    workspace, asset_id, before = _legacy(tmp_path / "snap", shape)
    applied = _apply(workspace, asset_id, before, _times(before, a=("9/10", "2")))
    restored = source_transcript.restore(workspace, asset_id, applied["revision"])
    assert ("edited" in restored) == (shape != "missing_seq_gt_0")
    if shape == "missing_seq_0":
        assert restored["edited"] is False
    elif "edited" in before:
        assert restored["edited"] is before["edited"]
    assert source_transcript.human_edited(restored) is source_transcript.human_edited(before)
    assert source_transcript.fingerprint(workspace, asset_id) == source_transcript.fingerprint(
        twin_ws, twin_id
    )
    rows = [dict(row, text=row["text"] + "!") for row in before["utterances"]]
    reseeded_twin = source_transcript.seed(twin_ws, twin_id, rows, Fraction(10), "sha")
    reseeded = source_transcript.seed(workspace, asset_id, rows, Fraction(10), "sha")
    replaced_twin = reseeded_twin["revision"] != twin["revision"]
    replaced = reseeded["revision"] != restored["revision"]
    assert replaced == replaced_twin  # 심기가 덮는지(또는 두는지)가 적용 전과 같다


def test_restore_after_a_later_edit_or_a_second_press_is_refused(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    applied = _apply(workspace, asset_id, seeded, _times(seeded, a=("9/10", "2")))
    later = source_transcript.edit(
        workspace, asset_id, applied["revision"], action="edit", utterance_id="b", text="뒤에 고침"
    )
    assert "snap" not in later  # 출처 기록은 적용한 리비전에만 있다
    with pytest.raises(RawWorkspaceError) as stale:
        source_transcript.restore(workspace, asset_id, applied["revision"])
    assert str(stale.value) == source_transcript.UNDO_CHANGED
    with pytest.raises(RawWorkspaceError) as nothing:
        source_transcript.restore(workspace, asset_id, later["revision"])
    assert str(nothing.value) == source_transcript.UNDO_NOTHING
    assert source_transcript.load(workspace, asset_id) == later  # 나중 편집이 그대로다
    # 두 번 누름 — 하나만 되돌린다.
    workspace2, asset2, seeded2 = _plain(tmp_path / "two")
    applied2 = _apply(workspace2, asset2, seeded2, _times(seeded2, a=("9/10", "2")))
    source_transcript.restore(workspace2, asset2, applied2["revision"])
    with pytest.raises(RawWorkspaceError):
        source_transcript.restore(workspace2, asset2, applied2["revision"])
    assert _revisions(workspace2, asset2) == 3


def test_snap_records_never_ride_along_with_edits_and_inserts(tmp_path: Path) -> None:
    workspace, asset_id, seeded = _plain(tmp_path)
    applied = _apply(workspace, asset_id, seeded, _times(seeded, a=("9/10", "2")))
    row = {"utterance_id": "n", "text": "새 줄", "start": enc(Fraction(7)), "end": enc(Fraction(8))}
    inserted = source_transcript.insert_segments(workspace, asset_id, applied["revision"], [row])
    assert "snap" not in inserted.document and inserted.inserted == ("n",)
    with pytest.raises(RawWorkspaceError):
        source_transcript.restore(workspace, asset_id, applied["revision"])


# ── 서비스 — 제안 sha · 순서 · 시계 ─────────────────────────────────────────────


def _preview(service: raw.RawWorkspaceService, root: Path, asset_id: str) -> raw.SnapPreview:
    preview = service.apply_snap_preview(root, asset_id)
    assert isinstance(preview, raw.SnapPreview), preview
    return preview


def test_the_preview_counts_the_shift_and_never_calls_a_moving_blocked_row_unchanged(
    snapped: tuple[Any, Path, str],
) -> None:
    service, root, asset_id = snapped
    preview = _preview(service, root, asset_id)
    assert preview.shift > Fraction(7, 5) and preview.shift < Fraction(8, 5)  # 1.5초 이른 자막
    counts = dict(preview.counts)
    assert counts == {"moved": 13, "unchanged": 0, "blocked": 2, "no_speech": 0}
    assert preview.total == 15 and preview.changes == 14 and preview.shift_only == 1
    assert preview.shifted == 14  # 길이 0 줄은 밀지 않는다
    assert (preview.level, preview.predicted) == ("red", "green")
    plan, _ = service._apply_snap_plan(root, asset_id, strict=True)
    quiet = next(item for item in plan.proposals if item.cue_id == "q")
    assert quiet.status == "blocked" and quiet.moves and quiet.shift_only
    zero = next(item for item in plan.proposals if item.cue_id == "z0")
    assert zero.status == "blocked" and not zero.moves
    # 먼저 들을 줄 — 끝이 크게 움직인 것부터(같으면 시작, 그다음 순서), 바뀌는 줄만.
    assert len(preview.listen) == raw.SNAP_LISTEN_FIRST
    ends = [abs(cue.new_end - cue.old_end) for cue in preview.listen]
    assert ends == sorted(ends, reverse=True)
    moving = sorted((abs(p.end_move) for p in plan.proposals if p.moves), reverse=True)[
        : raw.SNAP_LISTEN_FIRST
    ]
    assert ends == moving
    assert service._workspace(root) and _revisions(service._workspace(root), asset_id) == 1


def test_apply_writes_every_proposal_and_binds_to_the_sha(snapped: tuple[Any, Path, str]) -> None:
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    before = source_transcript.load(workspace, asset_id)
    preview = _preview(service, root, asset_id)
    plan, _ = service._apply_snap_plan(root, asset_id, strict=True)
    document = service.apply_snap(root, asset_id, preview.sha256)
    assert document["snap"]["plan_sha256"] == preview.sha256 == plan.sha256
    assert document["snap"]["changed"] == preview.changes == 14
    rows = document["utterances"]
    assert len(rows) == len(before["utterances"]) == len(plan.proposals)
    for row, old, item in zip(rows, before["utterances"], plan.proposals, strict=True):
        assert source_transcript.rational(row["start"]) == plan.on_common(item.new_start)
        assert source_transcript.rational(row["end"]) == plan.on_common(item.new_end)
        assert _without_times(row) == _without_times(old)
    assert rows[0] == before["utterances"][0]  # 길이 0 줄은 바이트 그대로 남는다
    source_transcript._validate_rows(rows, Fraction(40), {r["utterance_id"] for r in rows[1:]})
    wait_sync(service, root)  # 적용이 시각 확인을 다시 건다
    report = sync_report.current_report(workspace, asset_id)
    assert report is not None and report["source"]["transcript_revision"] == document["revision"]
    assert report["verdict"] == "green"


def _refused(service: Any, root: Path, asset_id: str, sha: str) -> str:
    with pytest.raises(RawWorkspaceError) as refused:
        service.apply_snap(root, asset_id, sha)
    return str(refused.value)


def test_a_stale_sha_writes_nothing(
    snapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    seen = _preview(service, root, asset_id).sha256
    # 다른 탭 — 화면이 본 적 없는 sha.
    assert _refused(service, root, asset_id, "0" * 64) == raw.SNAP_CHANGED
    # 기준이 바뀌었다(찾는 폭).
    wider = replace(project_config.SYNC_SNAP_SETTINGS, reach_seconds=Fraction(2))
    with monkeypatch.context() as patch:
        patch.setattr(project_config, "SYNC_SNAP_SETTINGS", wider)
        assert _refused(service, root, asset_id, seen) == raw.SNAP_CHANGED
    # 측정이 다시 재어져 말소리 칸이 달라졌다.
    report = sync_report.current_report(workspace, asset_id)
    assert report is not None
    vad = next((root / "cache" / "sync-check" / "vad").glob("*.json"))
    cached = json.loads(vad.read_text(encoding="utf-8"))
    original = vad.read_text(encoding="utf-8")
    cached["runs"] = [[start, end + 1] for start, end in cached["runs"]]
    vad.write_text(json.dumps(cached), encoding="utf-8")
    assert _refused(service, root, asset_id, seen) == raw.SNAP_CHANGED
    vad.write_text(original, encoding="utf-8")
    assert _preview(service, root, asset_id).sha256 == seen
    # 다른 탭이 자막을 고쳤다 — 새 리비전을 다시 재기 전이든 뒤든 그 sha로는 쓰지 않는다.
    current = source_transcript.load(workspace, asset_id)
    service.edit_source_transcript(
        root, asset_id, current["revision"], action="edit", utterance_id="c05", text="고친 말"
    )
    waiting = _refused(service, root, asset_id, seen)
    assert waiting in (f"{raw.SNAP_WAITING} {raw.SNAP_NOTHING_CHANGED}", raw.SNAP_CHANGED)
    wait_sync(service, root)
    assert _refused(service, root, asset_id, seen) == raw.SNAP_CHANGED
    assert _revisions(workspace, asset_id) == 2  # 심기 + 사람 편집뿐
    assert "snap" not in source_transcript.load(workspace, asset_id)


def test_apply_waits_for_relisten_and_refill_and_relisten_waits_for_undo(
    snapped: tuple[Any, Path, str],
) -> None:
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    sha = _preview(service, root, asset_id).sha256
    asr_epoch.bump(workspace, asset_id)
    assert service.apply_snap_blocker(root, asset_id) == raw.SNAP_RELISTEN_PENDING
    refused = _refused(service, root, asset_id, sha)
    assert refused == f"{raw.SNAP_RELISTEN_PENDING} {raw.SNAP_NOTHING_CHANGED}"
    asr_epoch.withdraw(workspace, asset_id)
    release = threading.Event()
    entered = threading.Event()

    def hold(_context: Any) -> None:
        entered.set()
        assert release.wait(30)

    running = service.submit_job(root, raw.REFILL_JOB_KIND, f"{asset_id}:{'d' * 64}", hold)
    assert entered.wait(30)
    assert _refused(service, root, asset_id, sha) == (
        f"{raw.SNAP_REFILL_RUNNING} {raw.SNAP_NOTHING_CHANGED}"
    )
    release.set()
    assert _wait(service, root, running.job_id).status == "succeeded"
    applied = service.apply_snap(root, asset_id, sha)
    # 적용한 자막은 다시 듣지 않는다 — 되돌리는 길을 말한다(유료 호출 0).
    with pytest.raises(RawWorkspaceError) as relisten:
        service.relisten_source(root, asset_id)
    assert str(relisten.value) == raw.RELISTEN_SNAPPED_REFUSAL
    assert not asr_epoch.state(workspace, asset_id).pending  # 차례를 올리지 않았다
    service.revert_snap(root, asset_id, applied["revision"])
    assert service.relisten_source(root, asset_id).pending  # 되돌리면 다시 들을 수 있다


def test_undo_returns_fingerprint_and_relisten_eligibility(snapped: tuple[Any, Path, str]) -> None:
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    before = source_transcript.load(workspace, asset_id)
    prints = (
        source_transcript.fingerprint(workspace, asset_id),
        source_transcript.binding_fingerprint(workspace, asset_id, True),
        source_transcript.binding_fingerprint(workspace, asset_id, False),
    )
    applied = service.apply_snap(root, asset_id, _preview(service, root, asset_id).sha256)
    assert service.revert_snap_state(root, asset_id) == applied["snap"]
    restored = service.revert_snap(root, asset_id, applied["revision"])
    assert json.dumps(restored["utterances"], ensure_ascii=False) == json.dumps(
        before["utterances"], ensure_ascii=False
    )
    assert restored["edited"] is before["edited"] is False
    assert (
        source_transcript.fingerprint(workspace, asset_id),
        source_transcript.binding_fingerprint(workspace, asset_id, True),
        source_transcript.binding_fingerprint(workspace, asset_id, False),
    ) == prints
    assert service.transcript_human_edited(restored) is False
    assert service.revert_snap_state(root, asset_id) is None
    with pytest.raises(RawWorkspaceError) as twice:  # 두 번 누름·다른 탭
        service.revert_snap(root, asset_id, applied["revision"])
    assert str(twice.value) == source_transcript.UNDO_CHANGED
    assert _revisions(workspace, asset_id) == 3
    wait_sync(service, root)
    assert _preview(service, root, asset_id).sha256 != applied["snap"]["plan_sha256"]  # 새 리비전


def test_refill_after_apply_is_allowed_and_then_undo_is_refused(
    snapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """적용 뒤의 빈칸 재전사는 된다 — 돈이 헛되지 않고 아무것도 덮지 않는다. 그 대신 그 뒤로는
    되돌리기가 넣은 줄을 지우지 않게 거절된다."""
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    applied = service.apply_snap(root, asset_id, _preview(service, root, asset_id).sha256)
    wait_sync(service, root)
    quote = service.refill_scope(root, asset_id)
    assert isinstance(quote, speech_refill.RefillScope), quote
    assert quote.transcript_revision == applied["revision"]
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    service.set_cross_check(root, False)
    quote = service.refill_scope(root, asset_id)
    sender = RefillSender()
    record = service.refill_source(root, asset_id, quote, transport(sender), context())
    assert record["inserted"], record
    after_refill = source_transcript.load(workspace, asset_id)
    with pytest.raises(RawWorkspaceError) as refused:
        service.revert_snap(root, asset_id, applied["revision"])
    assert str(refused.value) == source_transcript.UNDO_CHANGED
    assert source_transcript.load(workspace, asset_id) == after_refill  # 넣은 줄이 그대로다


def test_refill_waits_for_the_new_report_and_a_stale_refill_quote_never_lands(
    snapped: tuple[Any, Path, str],
) -> None:
    """적용은 자막 리비전을 바꾼다 — 그 전 견적의 digest로는 빈칸 재전사가 묻지 않는다(0송신)."""
    service, root, asset_id = snapped
    old_quote = service.refill_scope(root, asset_id)
    assert isinstance(old_quote, speech_refill.RefillScope)
    service.apply_snap(root, asset_id, _preview(service, root, asset_id).sha256)
    sender = RefillSender()
    with pytest.raises(RawWorkspaceError) as refused:
        service.refill_source(root, asset_id, old_quote, transport(sender), context())
    assert str(refused.value) in (raw.REFILL_CHANGED, raw.REFILL_WAITING)
    assert sender.whisper == [] and sender.text == []


def test_the_clock_round_trip_is_exact_when_audio_starts_late(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """제안(PCM 시계) → 원본 자막(공통 시계)은 확인이 잰 `audio_start`만큼 — 정확 유리수,
    float 없음."""
    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    service, root, asset_id = snap_workspace(tmp_path, audio_delay="0.5")
    workspace = service._workspace(root)
    measured = sync_report.measured_speech(workspace, asset_id)
    assert measured is not None and measured.audio_start != 0
    before = source_transcript.load(workspace, asset_id)
    for cue, row in zip(measured.cues, before["utterances"], strict=True):
        assert cue.start == source_transcript.rational(row["start"]) - measured.audio_start
    plan, _ = service._apply_snap_plan(root, asset_id, strict=True)
    assert plan.source.audio_start == measured.audio_start
    document = service.apply_snap(root, asset_id, plan.sha256)
    for row, item in zip(document["utterances"], plan.proposals, strict=True):
        start, end = (
            source_transcript.rational(row["start"]),
            source_transcript.rational(row["end"]),
        )
        assert isinstance(start, Fraction) and isinstance(end, Fraction)
        assert start == item.new_start + measured.audio_start
        assert end == item.new_end + measured.audio_start
        assert row["start"] == enc(item.new_start + measured.audio_start)  # 저장도 분자/분모 그대로


def test_without_usable_speech_cells_the_reason_names_the_recheck(
    snapped: tuple[Any, Path, str],
) -> None:
    """지금 보고서는 있는데 그 말소리 칸이 없으면 기다리라고 하지 않는다 — 저절로 다시 재지 않으니
    화면의 「다시 재기(무료)」를 말한다. 적용도 같은 까닭으로 거절하고 쓰지 않는다."""
    service, root, asset_id = snapped
    sha = _preview(service, root, asset_id).sha256
    for cells in (root / "cache" / "sync-check" / "vad").glob("*.json"):
        cells.unlink()
    assert service.apply_snap_preview(root, asset_id) == raw.SNAP_SPEECH_MISSING
    refused = _refused(service, root, asset_id, sha)
    assert refused == f"{raw.SNAP_SPEECH_MISSING} {raw.SNAP_NOTHING_CHANGED}"
    assert _revisions(service._workspace(root), asset_id) == 1
    assert service.start_sync_checks(root, (asset_id,), force=True) is not None
    wait_sync(service, root)  # 다시 재면 칸이 돌아와 같은 제안이다
    assert _preview(service, root, asset_id).sha256 == sha


def test_after_an_edit_the_next_proposal_is_near_idempotent(
    snapped: tuple[Any, Path, str],
) -> None:
    """적용한 리비전에는 새 제안이 없다(한 걸음). 그 뒤 사람이 고쳐(글자만) 새 리비전이 되면 다시 잰
    제안이 서는데, 전체 어긋남을 다시 밀지 않고 거의 그대로 둔다 — 되풀이 적용이 바깥으로 번지는
    길(D59 19h)은 이렇게 사람 편집·빈칸 넣기를 사이에 둘 때뿐이다."""
    service, root, asset_id = snapped
    first = _preview(service, root, asset_id)
    applied = service.apply_snap(root, asset_id, first.sha256)
    wait_sync(service, root)
    assert service.apply_snap_preview(root, asset_id) == raw.SNAP_APPLIED_ONCE
    service.edit_source_transcript(
        root, asset_id, applied["revision"], action="edit", utterance_id="c05", text="말 5!"
    )
    wait_sync(service, root)
    again = _preview(service, root, asset_id)
    assert again.shift == 0 and again.shifted == 0
    assert again.changes <= 2, again  # 맞춘 경계·lead 그대로 — 남는 것은 끝 lead 둘레뿐
    assert again.level == again.predicted == "green"
    plan, measured = service._apply_snap_plan(root, asset_id, strict=True)
    biggest = max((max(abs(p.start_move), abs(p.end_move)) for p in plan.proposals), default=0)
    assert biggest <= project_config.SYNC_SNAP_SETTINGS.lead_out
    offset = sync_check.measure(measured.cues, measured.speech, project_config.SYNC_CHECK_SETTINGS)
    assert not offset.offset.counted


# ── 19h 검증 뒤 고침 — 한 걸음 규칙 · 쓰기 트랜잭션 안의 다시 검사 · 분석 중 ─────────────────


def test_a_snap_applied_revision_is_never_snapped_again(tmp_path: Path) -> None:
    """한 걸음 규칙 — 적용한 리비전에는 다시 적용하지 않는다(쓰기 계층에서도)."""
    workspace, asset_id, seeded = _plain(tmp_path)
    applied = _apply(workspace, asset_id, seeded, _times(seeded, a=("9/10", "2")))
    with pytest.raises(RawWorkspaceError) as refused:
        _apply(workspace, asset_id, applied, _times(applied, a=("1", "2")))
    assert str(refused.value) == source_transcript.SNAP_ALREADY_APPLIED
    assert (
        f"{raw.SNAP_APPLIED_ONCE} {raw.SNAP_NOTHING_CHANGED}"
        == source_transcript.SNAP_ALREADY_APPLIED
    )
    assert _revisions(workspace, asset_id) == 2
    # 되돌린 뒤나 사람이 고친 뒤에는 다시 맞출 수 있다(한 걸음씩).
    edited = source_transcript.edit(
        workspace, asset_id, applied["revision"], action="edit", utterance_id="b", text="고침"
    )
    assert _apply(workspace, asset_id, edited, _times(edited, a=("1", "2")))["sequence"] == 3


def test_the_service_refuses_a_second_apply_and_offers_only_the_undo(
    snapped: tuple[Any, Path, str],
) -> None:
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    applied = service.apply_snap(root, asset_id, _preview(service, root, asset_id).sha256)
    wait_sync(service, root)  # 다시 잰 뒤에도 적용한 리비전 위에는 새 제안을 세우지 않는다
    assert service.apply_snap_preview(root, asset_id) == raw.SNAP_APPLIED_ONCE
    assert _refused(service, root, asset_id, applied["snap"]["plan_sha256"]) == (
        source_transcript.SNAP_ALREADY_APPLIED
    )
    assert _refused(service, root, asset_id, "0" * 64) == source_transcript.SNAP_ALREADY_APPLIED
    assert _revisions(workspace, asset_id) == 2  # 심기 + 적용뿐


def test_apply_undo_apply_undo_returns_to_the_original_bytes(
    snapped: tuple[Any, Path, str],
) -> None:
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    original = source_transcript.load(workspace, asset_id)
    prints = source_transcript.fingerprint(workspace, asset_id)
    for _ in range(2):
        wait_sync(service, root)
        applied = service.apply_snap(root, asset_id, _preview(service, root, asset_id).sha256)
        restored = service.revert_snap(root, asset_id, applied["revision"])
        assert json.dumps(restored["utterances"], ensure_ascii=False) == json.dumps(
            original["utterances"], ensure_ascii=False
        )
        assert restored["edited"] is original["edited"]
        assert source_transcript.fingerprint(workspace, asset_id) == prints
    assert _revisions(workspace, asset_id) == 5


def _pause(monkeypatch: pytest.MonkeyPatch, target: Any, name: str) -> tuple[Any, Any]:
    """`target.name`을 부르는 스레드 「paused」를 그 안에서 멈춰 세운다(다른 스레드는 그대로)."""
    entered, release = threading.Event(), threading.Event()
    real = getattr(target, name)

    def held(*args: Any, **kwargs: Any) -> Any:
        value = real(*args, **kwargs)
        if threading.current_thread().name == "paused":
            entered.set()
            assert release.wait(30)
        return value

    monkeypatch.setattr(target, name, held)
    return entered, release


def _in_thread(work: Any) -> tuple[threading.Thread, list[Any]]:
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(work())
        except RawWorkspaceError as error:
            outcome.append(error)

    thread = threading.Thread(target=run, name="paused")
    thread.start()
    return thread, outcome


def test_a_relisten_pressed_while_apply_waits_wins_and_apply_writes_nothing(
    snapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """적용이 막는 사정을 본 뒤 쓰기 전에 다시 듣기가 차례를 올리면, 적용은 쓰기 트랜잭션 안에서
    그것을 보고 거절한다 — 고친 자막에 유료 다시 듣기가 남지 않는다(19h 검증 뒤 고침 3)."""
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    sha = _preview(service, root, asset_id).sha256
    entered, release = _pause(monkeypatch, sync_snap, "propose")
    thread, outcome = _in_thread(lambda: service.apply_snap(root, asset_id, sha))
    assert entered.wait(30)
    assert service.relisten_source(root, asset_id).pending  # 아직 고친 자막이 아니다
    release.set()
    thread.join(30)
    (refused,) = outcome
    assert str(refused) == f"{raw.SNAP_RELISTEN_PENDING} {raw.SNAP_NOTHING_CHANGED}"
    assert _revisions(workspace, asset_id) == 1
    assert asr_epoch.state(workspace, asset_id).pending  # 사람이 고른 다시 듣기는 그대로


def test_an_apply_landing_while_relisten_checks_its_audio_refuses_the_relisten(
    snapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """반대 순서 — 다시 듣기가 검사(ffprobe)하는 사이 적용이 쓰이면, 차례 올리기가 같은 쓰기
    트랜잭션 안에서 고친 자막을 보고 거절한다(되돌리는 길을 말한다)."""
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    sha = _preview(service, root, asset_id).sha256
    entered, release = _pause(monkeypatch, raw, "_source_has_audio")
    thread, outcome = _in_thread(lambda: service.relisten_source(root, asset_id))
    assert entered.wait(30)
    service.apply_snap(root, asset_id, sha)
    release.set()
    thread.join(30)
    (refused,) = outcome
    assert str(refused) == raw.RELISTEN_SNAPPED_REFUSAL
    assert not asr_epoch.state(workspace, asset_id).pending


def test_a_refill_started_while_apply_waits_keeps_its_answers_and_apply_writes_nothing(
    snapped: tuple[Any, Path, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """적용이 쓰기 전에 빈칸 재전사 작업이 등록되면(같은 DB의 쓰기 트랜잭션) 적용이 거절하고, 빈칸
    재전사는 받은 줄을 그대로 넣는다 — 유료 답이 헛되지 않는다(19h 검증 뒤 고침 3)."""
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    service.set_cross_check(root, False)
    quote = service.refill_scope(root, asset_id)
    assert isinstance(quote, speech_refill.RefillScope), quote
    sha = _preview(service, root, asset_id).sha256
    entered, release = _pause(monkeypatch, sync_snap, "propose")
    thread, outcome = _in_thread(lambda: service.apply_snap(root, asset_id, sha))
    assert entered.wait(30)
    sender = Blocking()
    handle = service.refill_start(root, asset_id, quote, transport(sender))
    assert sender.entered.wait(30)  # 빈칸 재전사가 유료 요청 중이다
    release.set()
    thread.join(30)
    (refused,) = outcome
    assert str(refused) == f"{raw.SNAP_REFILL_RUNNING} {raw.SNAP_NOTHING_CHANGED}"
    sender.release.set()
    assert _wait(service, root, handle.job_id).status == "succeeded"
    document = source_transcript.load(workspace, asset_id)
    assert document["sequence"] == 1 and "snap" not in document  # 넣은 줄만 — 적용은 없다
    record = service.refill_last(root, asset_id)
    assert record is not None and not record.get("refused") and record["inserted"]
    assert len(sender.whisper) == len(quote.pieces)  # 산 답은 모두 넣기까지 갔다(헛된 호출 없음)


def _analysis_in_progress(service: Any, root: Path, asset_id: str) -> threading.Event:
    """그 원본을 지금 분석(`analyzing`)하는 활성 분석 작업 하나 — 놓아줄 때까지 멈춰 있다."""
    from vlogsrt.project.raw.analysis_store import AnalysisStore
    from vlogsrt.project.raw.models import (
        ANALYSIS_JOB_KIND,
        AnalysisRevision,
        AnalysisSourceResult,
    )

    store = AnalysisStore(service._workspace(root))
    order = service.latest_global_source_order(root)
    now = "2026-10-15T00:00:00+00:00"
    revision = AnalysisRevision(
        "anal_snapprobe", order.revision_id, "user_selected", "full", "{}", "{}", "fp-snap",
        "analyzing", 1, 0, None, now, now,
    )  # fmt: skip
    store.insert_revision(revision)
    name = service.list_source_assets(root)[0].source_name
    store.upsert_source_result(
        AnalysisSourceResult(
            "anal_snapprobe", asset_id, 0, name, "-", "analyzing", None, None, None, None, 0, 0,
            None, None, now, now,
        )
    )  # fmt: skip
    release = threading.Event()
    service.submit_job(root, ANALYSIS_JOB_KIND, "fp-snap", lambda _context: release.wait(30))
    return release


def test_apply_and_undo_wait_while_that_source_is_being_analyzed(
    snapped: tuple[Any, Path, str],
) -> None:
    """서비스가 스스로 막는다 — 그 원본을 지금 분석하는 작업이 있으면 적용·되돌리기를 받지 않는다
    (작업 등록부·분석 표가 같은 DB라 쓰기 트랜잭션 안에서도 본다, 19h 검증 뒤 잔손질)."""
    service, root, asset_id = snapped
    workspace = service._workspace(root)
    sha = _preview(service, root, asset_id).sha256
    applied = service.apply_snap(root, asset_id, sha)
    release = _analysis_in_progress(service, root, asset_id)
    assert service.apply_snap_blocker(root, asset_id) == raw.SNAP_ANALYSIS_RUNNING
    with pytest.raises(RawWorkspaceError) as refused:
        service.revert_snap(root, asset_id, applied["revision"])
    assert str(refused.value) == f"{raw.SNAP_ANALYSIS_RUNNING} {raw.SNAP_NOTHING_CHANGED}"
    assert _revisions(workspace, asset_id) == 2
    release.set()
    deadline = time.monotonic() + 30
    while service.apply_snap_blocker(root, asset_id) is not None and time.monotonic() < deadline:
        time.sleep(0.05)
    assert service.revert_snap(root, asset_id, applied["revision"])["sequence"] == 2
