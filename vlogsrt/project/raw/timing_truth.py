"""시각 정답(W-25b, D67) — 사람이 들어 찍은 원본 자막 줄의 참 시작·끝. 자막과 **따로** 둔다.

자막 시각을 고치는 다음 세션들(W-25c·d·e)은 말소리 측정(VAD)과 견줄 수밖에 없었다 — 그 측정이
맞춤 제안의 재료이기도 해서 자기 참조다. 사람이 플레이어로 들어 찍은 시각이 바깥 기준이 된다.

정답은 작업공간 DB의 `timing_truth` 표에만 산다. 찍어도 줄은 한 칸도 움직이지 않고, 원본 자막
리비전을 쓰지 않으며, 어느 지문·캐시 열쇠·승인·견적·등급에도 들어가지 않는다(불변식이 그 계층들의
import를 막는다). 읽는 곳은 화면의 표시와 `scripts/timing_truth_report.py`뿐이다.

**낡음**: 정답은 그것을 찍은 줄(아이디 + 그때의 글자)에 묶인다. 지금 리비전에 그 아이디가 없거나
글자가 다르면 낡은 정답이다 — 다시 쓰지 않고 보고서·화면이 「다시 찍기」로 알린다. 아이디만 보지
않는 까닭: 1단계 줄 아이디는 차례 번호(`utterance_000005`)라 다시 듣기 뒤 같은 아이디가 다른 말을
가리킨다. 글자를 고치거나 나누거나 합친 줄도 낡는다(경계가 바뀌었을 수 있다). 시각 맞춤 적용은
아이디·글자가 그대로라 정답이 그대로 남는다 — 바로 그 전후를 견주려고 찍는 정답이다.

표는 처음 찍을 때 만들어지고, 읽기는 표를 만들지 않는다(읽기만 하는 작업공간을 건드리지 않는다).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from typing import Any

from vlogsrt.project.config import TIMING_TRUTH_SETTINGS, TimingTruthSettings
from vlogsrt.project.raw import read_scope
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.source_transcript import rational

TABLE = "timing_truth"
EDGE_START = "start"
EDGE_END = "end"
EDGES = (EDGE_START, EDGE_END)

STRATUM_CHUNK_FIRST = "chunk_first"
"""1단계 청크마다 그 청크 안에서 가장 먼저 시작하는 줄 — 「조각 첫 줄 0.0」 버릇의 자리."""
STRATUM_NO_SPEECH = "no_speech"
"""지금 리비전의 자막 시각 확인이 「말소리를 못 찾음」으로 짚은 줄."""
STRATUM_WHOLE_SECOND = "whole_second"
"""시작이나 끝이 PCM 시계의 정수 초인 줄 — 온도 0 시절 whisper의 버릇(D55 버그 1)."""
STRATUM_ORDINARY = "ordinary"
STRATA = (STRATUM_CHUNK_FIRST, STRATUM_NO_SPEECH, STRATUM_WHOLE_SECOND, STRATUM_ORDINARY)
"""층 — 한 줄이 여럿에 들면 앞의 것이 이긴다(층마다 한 번만 센다)."""

STALE_LINE_MISSING = "line_missing"
STALE_TEXT_CHANGED = "text_changed"


@dataclass(frozen=True, slots=True)
class TruthLine:
    """원본 자막 줄 하나 — 시각은 공통 시계의 정확 유리수다."""

    cue_id: str
    text: str
    start: Fraction
    end: Fraction


@dataclass(frozen=True, slots=True)
class TruthMark:
    """찍은 정답 하나 — 줄 아이디·그때의 글자·리비전에 묶인다."""

    asset_id: str
    utterance_id: str
    edge: str
    seconds: Fraction
    transcript_revision: str
    line_text: str
    stratum: str
    marked_at: str


@dataclass(frozen=True, slots=True)
class MarkState:
    """정답 하나와 지금 리비전에서의 처지 — `stale`이 None이면 지금 줄에 그대로 맞는다."""

    mark: TruthMark
    stale: str | None


@dataclass(frozen=True, slots=True)
class TruthPair:
    """정답과 견줄 시각 하나(자막 또는 제안) — 같은 줄·같은 경계."""

    asset_id: str
    cue_id: str
    stratum: str
    edge: str
    truth: Fraction
    measured: Fraction

    @property
    def error(self) -> Fraction:
        """`+`면 견준 시각이 정답보다 늦다."""
        return self.measured - self.truth


@dataclass(frozen=True, slots=True)
class TruthMetrics:
    """지표 — 값이 없으면 None(그 경계의 정답이 하나도 없다)."""

    starts: int
    ends: int
    start_mae: Fraction | None
    end_mae: Fraction | None
    start_hit: Fraction | None
    end_hit: Fraction | None
    end_cut: Fraction | None
    """끝이 말보다 먼저 끝나는 몫 — 자막이 말을 자른다."""


# ── 순수 계산 ───────────────────────────────────────────────────────────────


def lines_of(transcript: Mapping[str, Any]) -> tuple[TruthLine, ...]:
    """원본 자막 리비전의 줄 — 순서 그대로."""
    return tuple(
        TruthLine(
            str(row["utterance_id"]), str(row["text"]), rational(row["start"]), rational(row["end"])
        )
        for row in transcript.get("utterances", [])
    )


def stratify(
    lines: Sequence[TruthLine],
    *,
    chunk_starts: Iterable[Fraction],
    audio_start: Fraction,
    no_speech: frozenset[str],
) -> dict[str, str]:
    """줄마다 층 하나(`STRATA` 앞이 이긴다). `chunk_starts`는 공통 시계, `audio_start`는 PCM 표본
    0의 공통 시계 위치다 — 정수 초는 PCM 시계(whisper가 본 시계)에서 잰다."""
    firsts = _chunk_firsts(lines, sorted(set(chunk_starts)))
    strata: dict[str, str] = {}
    for line in lines:
        if line.cue_id in firsts:
            strata[line.cue_id] = STRATUM_CHUNK_FIRST
        elif line.cue_id in no_speech:
            strata[line.cue_id] = STRATUM_NO_SPEECH
        elif _whole(line.start - audio_start) or _whole(line.end - audio_start):
            strata[line.cue_id] = STRATUM_WHOLE_SECOND
        else:
            strata[line.cue_id] = STRATUM_ORDINARY
    return strata


def _chunk_firsts(lines: Sequence[TruthLine], starts: Sequence[Fraction]) -> set[str]:
    """청크 `[c_i, c_i+1)`마다 그 안에서 가장 먼저 시작하는 줄(같으면 앞 줄)."""
    firsts: set[str] = set()
    for index, low in enumerate(starts):
        high = starts[index + 1] if index + 1 < len(starts) else None
        inside = [
            (line.start, order, line.cue_id)
            for order, line in enumerate(lines)
            if low <= line.start and (high is None or line.start < high)
        ]
        if inside:
            firsts.add(min(inside)[2])
    return firsts


def _whole(value: Fraction) -> bool:
    return value.denominator == 1


def suggest(
    lines: Sequence[TruthLine],
    strata: Mapping[str, str],
    settings: TimingTruthSettings = TIMING_TRUTH_SETTINGS,
) -> dict[str, str]:
    """들어 볼 줄 — 층마다 설정한 수만큼, 그 층의 줄 가운데 고르게 퍼뜨려 고른다(결정적).

    모자란 층을 다른 층으로 채우지 않는다 — 층별 값이 섞이지 않게."""
    wanted = {
        STRATUM_CHUNK_FIRST: settings.chunk_first_lines,
        STRATUM_NO_SPEECH: settings.no_speech_lines,
        STRATUM_WHOLE_SECOND: settings.whole_second_lines,
        STRATUM_ORDINARY: settings.ordinary_lines,
    }
    picked: set[str] = set()
    for stratum in STRATA:
        members = [line.cue_id for line in lines if strata.get(line.cue_id) == stratum]
        picked.update(_spread(members, wanted[stratum]))
    return {line.cue_id: strata[line.cue_id] for line in lines if line.cue_id in picked}


def _spread(members: Sequence[str], count: int) -> list[str]:
    """`count`개를 고르게 — 칸을 `count`등분한 가운데 자리."""
    if count >= len(members):
        return list(members)
    return [members[(2 * k + 1) * len(members) // (2 * count)] for k in range(count)]


def judge(marks: Iterable[TruthMark], lines: Sequence[TruthLine]) -> tuple[MarkState, ...]:
    """정답마다 지금 리비전에서의 처지 — 아이디가 없거나 글자가 다르면 낡았다."""
    by_id = {line.cue_id: line for line in lines}
    states: list[MarkState] = []
    for mark in marks:
        line = by_id.get(mark.utterance_id)
        if line is None:
            states.append(MarkState(mark, STALE_LINE_MISSING))
        elif line.text != mark.line_text:
            states.append(MarkState(mark, STALE_TEXT_CHANGED))
        else:
            states.append(MarkState(mark, None))
    return tuple(states)


def pairs(states: Iterable[MarkState], lines: Sequence[TruthLine]) -> tuple[TruthPair, ...]:
    """낡지 않은 정답과 `lines`의 같은 줄·같은 경계 — `lines`는 자막이거나 제안의 시각이다."""
    by_id = {line.cue_id: line for line in lines}
    found: list[TruthPair] = []
    for state in states:
        line = by_id.get(state.mark.utterance_id)
        if state.stale is not None or line is None:
            continue
        measured = line.start if state.mark.edge == EDGE_START else line.end
        found.append(
            TruthPair(
                state.mark.asset_id,
                line.cue_id,
                state.mark.stratum,
                state.mark.edge,
                state.mark.seconds,
                measured,
            )
        )
    return tuple(found)


def metrics(
    found: Iterable[TruthPair], settings: TimingTruthSettings = TIMING_TRUTH_SETTINGS
) -> TruthMetrics:
    """시작·끝 MAE · ±적중 폭 적중률 · 끝 자름 비율(정확 유리수)."""
    rows = list(found)
    starts = [pair.error for pair in rows if pair.edge == EDGE_START]
    ends = [pair.error for pair in rows if pair.edge == EDGE_END]
    tolerance = settings.hit_seconds
    return TruthMetrics(
        starts=len(starts),
        ends=len(ends),
        start_mae=_mean(abs(error) for error in starts),
        end_mae=_mean(abs(error) for error in ends),
        start_hit=_share(abs(error) <= tolerance for error in starts),
        end_hit=_share(abs(error) <= tolerance for error in ends),
        end_cut=_share(error < 0 for error in ends),
    )


def _mean(values: Iterable[Fraction]) -> Fraction | None:
    items = list(values)
    return None if not items else sum(items, Fraction(0)) / len(items)


def _share(flags: Iterable[bool]) -> Fraction | None:
    items = list(flags)
    return None if not items else Fraction(sum(items), len(items))


# ── 표 ──────────────────────────────────────────────────────────────────────


def mark(
    workspace: Any,
    asset_id: str,
    line: TruthLine,
    edge: str,
    seconds: Fraction,
    *,
    transcript_revision: str,
    stratum: str,
) -> TruthMark:
    """정답 하나를 찍는다 — 같은 줄·같은 경계의 옛 정답(낡은 것 포함)을 바꿔 쓴다. 줄은 그대로다."""
    _require_mark(edge, seconds, stratum)
    entry = TruthMark(
        asset_id=asset_id,
        utterance_id=line.cue_id,
        edge=edge,
        seconds=seconds,
        transcript_revision=transcript_revision,
        line_text=line.text,
        stratum=stratum,
        marked_at=datetime.now(UTC).isoformat(),
    )
    with workspace.connect() as db:
        _create(db)
        _upsert(db, entry)
        db.commit()
    return entry


def _upsert(db: Any, entry: TruthMark) -> None:
    db.execute(
        f"INSERT INTO {TABLE}(asset_id, utterance_id, edge, seconds, transcript_revision, "
        "line_text, stratum, marked_at) VALUES(?,?,?,?,?,?,?,?) "
        "ON CONFLICT(asset_id, utterance_id, edge) DO UPDATE SET seconds=excluded.seconds, "
        "transcript_revision=excluded.transcript_revision, line_text=excluded.line_text, "
        "stratum=excluded.stratum, marked_at=excluded.marked_at",
        (
            entry.asset_id,
            entry.utterance_id,
            entry.edge,
            str(entry.seconds),
            entry.transcript_revision,
            entry.line_text,
            entry.stratum,
            entry.marked_at,
        ),
    )


def clear(workspace: Any, asset_id: str, utterance_id: str, edge: str) -> bool:
    """정답 하나를 지운다 — 지웠으면 True. 표가 없으면 만들지 않는다."""
    _require_edge(edge)
    with workspace.connect() as db:
        if not _table_exists(db):
            return False
        removed = db.execute(
            f"DELETE FROM {TABLE} WHERE asset_id=? AND utterance_id=? AND edge=?",
            (asset_id, utterance_id, edge),
        ).rowcount
        db.commit()
    return removed > 0


def marks(workspace: Any, asset_id: str) -> tuple[TruthMark, ...]:
    """원본 하나의 정답 — 읽기 범위 안에서는 표를 한 번에 읽는다(D63). 표를 만들지 않는다."""
    everything = read_scope.memo(
        ("timing_truth.marks", str(workspace.root)), lambda: _all(workspace)
    )
    return tuple(entry for entry in everything if entry.asset_id == asset_id)


def _all(workspace: Any) -> tuple[TruthMark, ...]:
    # 그리기 범위 안에서는 이미 열린 연결을 함께 쓴다 — 정답 표가 DB 열기를 늘리지 않는다(D67).
    with read_scope.shared(("workspace.db", str(workspace.root)), workspace.connect) as db:
        return marks_on(db)


def marks_on(db: Any) -> tuple[TruthMark, ...]:
    """연결 하나 위의 모든 정답 — 읽기 전용 연결(보고서의 `mode=ro`)에서도 된다."""
    if not _table_exists(db):
        return ()
    rows = db.execute(
        f"SELECT asset_id, utterance_id, edge, seconds, transcript_revision, line_text, stratum, "
        f"marked_at FROM {TABLE} ORDER BY asset_id, utterance_id, edge"
    ).fetchall()
    return tuple(
        TruthMark(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            Fraction(str(row[3])),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
        )
        for row in rows
    )


def _require_mark(edge: str, seconds: Fraction, stratum: str) -> None:
    _require_edge(edge)
    if not isinstance(seconds, Fraction) or seconds < 0:
        raise RawWorkspaceError(f"정답 시각은 0 이상의 정확한 초여야 합니다: {seconds!r}")
    if stratum not in STRATA:
        raise RawWorkspaceError(f"모르는 층입니다: {stratum!r}")


def _require_edge(edge: str) -> None:
    if edge not in EDGES:
        raise RawWorkspaceError(f"정답 경계는 start 또는 end입니다: {edge!r}")


def _table_exists(db: Any) -> bool:
    found = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)
    ).fetchone()
    return found is not None


def _create(db: Any) -> None:
    db.execute(
        f"CREATE TABLE IF NOT EXISTS {TABLE} (asset_id TEXT NOT NULL, utterance_id TEXT NOT NULL, "
        "edge TEXT NOT NULL CHECK(edge IN ('start','end')), seconds TEXT NOT NULL, "
        "transcript_revision TEXT NOT NULL, line_text TEXT NOT NULL, stratum TEXT NOT NULL, "
        "marked_at TEXT NOT NULL, PRIMARY KEY(asset_id, utterance_id, edge))"
    )


__all__ = [
    "EDGES",
    "EDGE_END",
    "EDGE_START",
    "STALE_LINE_MISSING",
    "STALE_TEXT_CHANGED",
    "STRATA",
    "STRATUM_CHUNK_FIRST",
    "STRATUM_NO_SPEECH",
    "STRATUM_ORDINARY",
    "STRATUM_WHOLE_SECOND",
    "TABLE",
    "MarkState",
    "TruthLine",
    "TruthMark",
    "TruthMetrics",
    "TruthPair",
    "stratify",
    "clear",
    "judge",
    "lines_of",
    "mark",
    "marks",
    "marks_on",
    "metrics",
    "pairs",
    "suggest",
]
