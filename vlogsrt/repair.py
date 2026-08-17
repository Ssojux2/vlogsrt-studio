"""되풀이 재전사 — **되풀이 구간만 다시 물어본다**(D27).

`verify`와 헷갈리면 안 된다. 검증은 표시만 하고 출력을 못 건드린다(규칙 5).
이 계층은 **출력을 바꾸는 유일한 판정 계층**이며, 그래서 자리도 다르다:
골격이 조립되기 **전**, 전사 단계 안에서 끝난다. 조립이 끝난 골격은 그 뒤로 불변이다(규칙 2).

시각은 여전히 whisper 세그먼트에서만 온다(규칙 1). 재전사도 whisper 호출이고,
오프셋도 ffmpeg가 CSV로 알려준 실제값이다. **바뀌는 것은 어느 응답이 그 구간을
덮느냐 하나뿐이다.**

구간 하나가 겪는 일:

    되풀이 검출 → 앞뒤 여유를 두고 다시 자른다
      → 심판 모델(gpt-transcribe)에게 "여기 말이 있나" 묻는다
      → whisper에 다른 온도로 다시 묻는다
          · 멀쩡한 글자가 왔다      → 그 구간을 갈아끼운다      (replaced)
          · 또 되풀이인데 심판은 정상이라 한다 → 온도를 올려 한 번 더
          · 끝내 회복 못 했다        → 첫 장만 남기고 비운다     (collapsed)

**마지막 줄이 규칙 9를 되풀이에 한해 연 지점이다.** 1668장이 서느니 한 장이 서고
나머지가 비는 편이 낫다는 판단이며, 그 판단은 되풀이 구간 안에서만 유효하다.
재전사를 다 시도한 뒤에만 여기 온다.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from . import log, preprocess, repetition, transcribe
from .config import Config
from .models import (
    REPAIR_COLLAPSED,
    REPAIR_REPLACED,
    Chunk,
    Repair,
    Repeat,
    Segment,
    Span,
)
from .transcribe import ARBITER_KIND, RESCUE_KIND, ChunkResponse, Transcriber, TranscriptionJob


@dataclass(frozen=True, slots=True)
class Source:
    """재전사가 오디오를 다시 자르려면 필요한 것. `audio`가 없으면 픽스처만 쓴다."""

    stem: str
    duration: float
    audio: Path | None = None
    workdir: Path | None = None


@dataclass(frozen=True, slots=True)
class Settled:
    """조립이 끝난 골격. **여기서부터 불변이다**(규칙 2)."""

    spine: tuple[Segment, ...]
    repairs: tuple[Repair, ...]


@dataclass(frozen=True, slots=True)
class _Plan:
    """되풀이 하나와, 그것을 다시 물으려고 잘라낼 구간.

    `owned`는 이 계획이 걷어낼 세그먼트다. **구간은 그 세그먼트들의 시작~끝이지,
    그 반대가 아니다.** 구간을 먼저 정하고 걸치는 세그먼트를 지우면, 재전사가 덮지
    않는 꼬리가 조용히 사라진다(규칙 9).
    """

    repeat: Repeat
    span: Span
    owned: tuple[int, ...]


def settle(
    draft: Sequence[Segment], source: Source, cfg: Config, transcriber: Transcriber
) -> Settled:
    """1차 골격에서 되풀이를 걷어내고 최종 골격을 조립한다."""
    plans = _plan(draft, source, cfg)
    if not plans:
        return Settled(spine=tuple(draft), repairs=())

    log.info(f"되풀이 {len(plans)}구간 {sum(p.repeat.duration for p in plans):.0f}초 — 다시 묻는다")
    chunks = _cut(source, [plan.span for plan in plans], cfg)
    rescued, attempts, arbiter = _rescue(plans, chunks, source, cfg, transcriber)
    return _assemble(draft, plans, rescued, attempts, arbiter)


# ─────────────────────────────────────────────────────────────
#  계획 — 어디를 다시 물을 것인가
# ─────────────────────────────────────────────────────────────


def plan_spans(draft: Sequence[Segment], source: Source, cfg: Config) -> list[Span]:
    """다시 물어볼 구간만. 픽스처 녹화가 같은 자리를 잘라내려고 쓴다."""
    return [plan.span for plan in _plan(draft, source, cfg)]


def _plan(draft: Sequence[Segment], source: Source, cfg: Config) -> list[_Plan]:
    # 시도 횟수가 0이면 아무것도 묻지 않고 전부 접게 된다. 그건 재전사가 아니라 삭제다.
    if not cfg.repair_enabled or cfg.repair_max_attempts < 1:
        return []
    repeats = [
        repeat
        for repeat in repetition.find_repeats(draft, cfg)
        if repeat.duration >= cfg.repair_min_seconds
    ]
    owner = _assign(draft, repeats, cfg)
    plans: list[_Plan] = []
    for index, repeat in enumerate(repeats):
        owned = [segment for segment in draft if owner.get(segment.id) == index]
        if not owned:
            continue  # 이웃 계획이 이 되풀이까지 데려갔다. 같은 자리를 두 번 물을 이유가 없다
        plans.append(
            _Plan(
                repeat=repeat,
                span=(
                    min(segment.start for segment in owned),
                    max(segment.end for segment in owned),
                ),
                owned=tuple(segment.id for segment in owned),
            )
        )
    return _disjoint(sorted(plans, key=lambda plan: plan.span))


def _disjoint(plans: Sequence[_Plan]) -> list[_Plan]:
    """구간이 겹치는 계획은 버린다. **못 고치는 것이 잘못 고치는 것보다 낫다.**

    세그먼트는 계획 하나에만 속하지만, whisper가 시각이 겹치는 세그먼트를 내놓으면
    구간까지 겹칠 수 있다. 그러면 조각 수가 구간 수와 어긋나 재전사가 통째로 취소된다.
    겹친 것 하나만 버리면 나머지 되풀이는 정상적으로 고쳐진다. 버린 구간은 손대지
    않은 채로 남아 검증 로그에 뜬다.
    """
    kept: list[_Plan] = []
    for plan in plans:
        if kept and plan.span[0] < kept[-1].span[1]:
            log.warn(
                f"{plan.repeat.start:.0f}~{plan.repeat.end:.0f}초 되풀이는 앞 구간과 겹쳐 "
                f"재전사하지 않는다. 자막은 1차 전사 그대로 남는다."
            )
            continue
        kept.append(plan)
    return kept


def _assign(draft: Sequence[Segment], repeats: Sequence[Repeat], cfg: Config) -> dict[int, int]:
    """세그먼트마다 주인 계획을 정한다. **한 세그먼트는 한 계획에만 속한다.**

    되풀이 자신의 세그먼트를 먼저 배정하고, 그다음 여유에 걸치는 것을 배정한다.
    순서가 반대면 앞 계획의 여유가 뒤 계획의 되풀이를 삼킨다.

    여유에 **닿기만 해도** 세그먼트를 통째로 데려간다. 반만 데려가면 재전사가 덮지
    않는 꼬리가 남아 조용히 사라진다(규칙 9).
    """
    owner: dict[int, int] = {}
    for index, repeat in enumerate(repeats):
        for seg_id in repeat.seg_ids:
            owner.setdefault(seg_id, index)
    for index, repeat in enumerate(repeats):
        low = repeat.start - cfg.repair_margin
        high = repeat.end + cfg.repair_margin
        for segment in draft:
            if segment.end > low and segment.start < high:
                owner.setdefault(segment.id, index)
    return owner


def _cut(source: Source, spans: Sequence[Span], cfg: Config) -> list[Chunk]:
    """오디오가 있으면 그 구간만 다시 자른다. 픽스처만 있는 실행은 자를 것이 없다.

    조각 수가 구간 수와 다르면 **아무것도 하지 않는다.** 계획과 조각은 순서로 짝지어지므로,
    하나만 어긋나도 다른 구간의 오디오를 그 자리에 갈아끼우게 된다 — 조용한 손실이다.
    """
    if not source.audio or not source.workdir:
        return []
    try:
        chunks = preprocess.cut_spans(source.audio, spans, source.duration, source.workdir)
    except RuntimeError as exc:
        # 자르지 못해도 자막은 이미 만들 수 있다. 다만 되풀이는 못 고친다.
        log.warn(f"재전사할 조각을 자르지 못했다: {exc}")
        return []
    if len(chunks) != len(spans):
        log.warn(
            f"재전사 조각이 {len(chunks)}개인데 구간은 {len(spans)}개다. "
            f"짝을 믿을 수 없어 재전사를 건너뛴다."
        )
        return []
    return chunks


# ─────────────────────────────────────────────────────────────
#  묻기 — 심판 한 번, whisper는 회복할 때까지
# ─────────────────────────────────────────────────────────────


def _ask_arbiter(
    chunks: Sequence[Chunk],
    wanted: Sequence[int],
    source: Source,
    cfg: Config,
    transcriber: Transcriber,
) -> dict[int, bool]:
    """두 번째 모델에게 "이 구간에 되살릴 말이 있나" 묻는다.

    **이 모델은 시각을 만들지 않는다**(글자만 준다). 그래서 자막을 직접 고치지 못하고,
    대신 whisper에 **한 번 더 돈을 쓸 값어치가 있는지**를 정한다. 심판이 멀쩡한 말을
    들었다면 그 구간에는 되살릴 발화가 있다는 뜻이다.

    **재전사가 실패한 구간만 묻는다.** whisper가 첫 시도에 회복했으면 심판의 답이
    바꿀 것이 없다 — 멀쩡한 영상에서 심판 값이 0원인 이유다.
    """
    try:
        responses = transcriber.transcribe(_job(source, _subset(chunks, wanted), ARBITER_KIND), cfg)
    except Exception as exc:  # noqa: BLE001 - 심판이 죽어도 자막은 이미 나와 있다
        log.warn(f"심판 모델을 부르지 못했다: {exc}. 더 시도하지 않는다.")
        return {}
    return {
        index: not repetition.looks_repetitive(transcribe.parse_text(response.payload), cfg)
        for index, response in _by_span(responses, chunks)
        if index in set(wanted)
    }


def _rescue(
    plans: Sequence[_Plan],
    chunks: Sequence[Chunk],
    source: Source,
    cfg: Config,
    transcriber: Transcriber,
) -> tuple[dict[int, list[Segment]], dict[int, int], dict[int, bool]]:
    """회복할 때까지 온도를 올려가며 다시 묻는다. 심판이 허락하는 만큼만."""
    rescued: dict[int, list[Segment]] = {}
    attempts: dict[int, int] = dict.fromkeys(range(len(plans)), 0)
    arbiter: dict[int, bool] = {}
    pending = list(range(len(plans)))

    for attempt in range(cfg.repair_max_attempts):
        hotter = replace(
            cfg,
            transcribe_temperature=cfg.repair_temperature + attempt * cfg.repair_temperature_step,
        )
        responses = _ask_whisper(chunks, pending, source, hotter, transcriber)
        failed: list[int] = []
        for index in pending:
            attempts[index] += 1
            segments = _segments(responses.get(index))
            if segments and repetition.repetitive_share(segments, cfg) <= cfg.loop_repetitive_share:
                rescued[index] = segments
            else:
                failed.append(index)
        if not failed:
            break
        # 실패한 구간만, 그리고 한 번만 심판에게 묻는다. 접힐 구간의 판정 근거도 여기서 남는다.
        unasked = [index for index in failed if index not in arbiter]
        if unasked:
            arbiter.update(_ask_arbiter(chunks, unasked, source, cfg, transcriber))
        pending = [index for index in failed if arbiter.get(index, False)]
        if not pending:
            break
    return rescued, attempts, arbiter


def _subset(chunks: Sequence[Chunk], wanted: Sequence[int]) -> list[Chunk]:
    keep = set(wanted)
    return [chunk for index, chunk in enumerate(chunks) if index in keep]


def _ask_whisper(
    chunks: Sequence[Chunk],
    pending: Sequence[int],
    source: Source,
    cfg: Config,
    transcriber: Transcriber,
) -> dict[int, ChunkResponse]:
    try:
        responses = transcriber.transcribe(_job(source, _subset(chunks, pending), RESCUE_KIND), cfg)
    except Exception as exc:  # noqa: BLE001 - 실패는 접기로 이어진다. 실행을 죽이지는 않는다
        log.warn(f"재전사 호출이 실패했다: {exc}")
        return {}
    return dict(_by_span(responses, chunks))


def _job(source: Source, chunks: Sequence[Chunk], kind: str) -> TranscriptionJob:
    return TranscriptionJob(stem=source.stem, chunks=tuple(chunks), kind=kind)


def _by_span(
    responses: Sequence[ChunkResponse], chunks: Sequence[Chunk]
) -> list[tuple[int, ChunkResponse]]:
    """응답을 계획에 되짚는다. **오프셋이 그 조각 안에 있는 것만** 붙인다.

    위치로 짝지으면 안 된다 — 픽스처는 요청한 부분집합과 상관없이 녹화된 전부를
    돌려주고, ffmpeg가 실제로 자른 자리는 요청값과 프레임 경계만큼 다르다.

    **가장 가까운 조각이라고 무조건 붙이지도 않는다.** 낡은 픽스처가 30분 떨어진
    오프셋을 들고 오면, 그걸 억지로 붙이는 순간 남의 구간 자막이 이 자리에 선다.
    짝을 못 찾은 응답은 버린다 — 그 계획은 재전사 실패로 처리된다.
    """
    if not chunks:
        return list(enumerate(responses))
    matched: list[tuple[int, ChunkResponse]] = []
    for response in responses:
        index = min(range(len(chunks)), key=lambda i: abs(chunks[i].offset - response.offset))
        chunk = chunks[index]
        if abs(chunk.offset - response.offset) > chunk.duration:
            log.warn(
                f"재전사 응답의 오프셋 {response.offset:.1f}초가 어느 구간과도 맞지 않는다. "
                f"버린다 — 픽스처가 지금 설정과 다른 자리에서 녹화됐을 수 있다."
            )
            continue
        matched.append((index, response))
    return matched


def _segments(response: ChunkResponse | None) -> list[Segment]:
    if response is None:
        return []
    try:
        return transcribe.parse_segments(response.payload, response.offset)
    except RuntimeError as exc:
        log.warn(f"재전사 응답에서 세그먼트를 읽지 못했다: {exc}")
        return []


# ─────────────────────────────────────────────────────────────
#  조립 — 여기서 골격이 확정된다
# ─────────────────────────────────────────────────────────────


def _assemble(
    draft: Sequence[Segment],
    plans: Sequence[_Plan],
    rescued: dict[int, list[Segment]],
    attempts: dict[int, int],
    arbiter: dict[int, bool],
) -> Settled:
    """되풀이 구간을 대체본으로 갈고 나머지는 그대로 둔다. 시간 순으로 다시 번호를 매긴다."""
    replaced_ids: set[int] = set()
    incoming: list[Segment] = []
    records: list[Repair] = []

    for index, plan in enumerate(plans):
        replacement = rescued.get(index)
        if replacement is not None:
            # 대체본은 계획이 가진 세그먼트의 시작~끝을 그대로 덮는다. 걷어낼 것도 그만큼이다.
            taken = set(plan.owned)
            outcome = REPAIR_REPLACED
        else:
            # 접기는 되풀이 자신만 건드린다. 여유에 딸려온 멀쩡한 세그먼트는 그대로 둔다.
            taken = set(plan.repeat.seg_ids)
            replacement = repetition.collapse([s for s in draft if s.id in taken])
            outcome = REPAIR_COLLAPSED
        replaced_ids.update(taken)
        incoming.extend(replacement)
        records.append(
            Repair(
                repeat=plan.repeat,
                outcome=outcome,
                arbiter_clean=arbiter.get(index, False),
                attempts=attempts.get(index, 0),
                before=len(taken),
                after=len(replacement),
                arbiter_answered=index in arbiter,
                span=plan.span,
            )
        )

    kept = [segment for segment in draft if segment.id not in replaced_ids]
    ordered = sorted([*kept, *incoming], key=lambda segment: (segment.start, segment.end))
    spine = tuple(replace(segment, id=number) for number, segment in enumerate(ordered))
    _report(records)
    return Settled(spine=spine, repairs=tuple(records))


def _report(records: Sequence[Repair]) -> None:
    """장수를 함께 찍는다. **10장이 1장이 됐다면 그것도 사람이 봐야 할 일이다** —
    모델이 그 구간을 그렇게 들었다는 뜻이고, 자막이 그만큼 비게 된다."""
    for record in records:
        span = f"{record.repeat.start:.0f}~{record.repeat.end:.0f}초"
        if record.outcome == REPAIR_REPLACED:
            asked = f"{record.span[0]:.0f}~{record.span[1]:.0f}초"
            log.info(
                f"  {span} 되풀이 → {asked}를 다시 물어 {record.before}장이 {record.after}장이 됐다"
            )
            continue
        log.warn(
            f"  {span} 되풀이 {record.before}장을 회복하지 못했다 "
            f"({record.attempts}회 시도, {record.verdict}). "
            f"첫 장만 남기고 그 구간은 비운다."
        )


def summarize(repairs: Sequence[Repair]) -> str:
    replaced = [r for r in repairs if r.outcome == REPAIR_REPLACED]
    collapsed = [r for r in repairs if r.outcome == REPAIR_COLLAPSED]
    seconds = sum(r.repeat.duration for r in repairs)
    return (
        f"되풀이 재전사: {len(repairs)}구간 {seconds:.0f}초"
        f" · 회복 {len(replaced)} · 접음 {len(collapsed)}"
    )
