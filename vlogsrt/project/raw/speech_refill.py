"""말소리 빈칸 재전사(D59 19f) — 자막이 덮지 않은 말소리를 그 구간만 잘라 whisper에 다시 묻는다.

흐름: 서비스가 재료를 모아(`RefillPlan` — 구간은 순수 `speech_gaps`가 고른다) 범위(`scope_for`)를
짓고, 사람이 승인 단추를 누르면(그 범위의 digest에 묶인다) 작업 안에서 범위를 다시 재어 digest를
맞춘 뒤 `run`이 묻는다. 견적(분량·금액 문구)은 없다(D110). 받은 줄을 원본 자막에 넣는 일은
서비스의 `refill_*`만 한다(`source_transcript.insert_segments`).

지키는 선:

- **시각은 whisper 세그먼트 + 그 조각의 실제 표본 오프셋뿐이다**(규칙 1). 1단계와 같은 길
  (`transcribe_selected_audio` → `utterance_evidence`)로 틱을 잡고, 공통 시계로는 자막 시각
  확인(D55)과 같은 원점(`audio_start`)만큼 민다. 시각을 유도·배분·추정하지 않는다.
- **whisper 요청에는 힌트가 없다**(규칙 6) — 온도는 엔진 1차 값(0.6, D55)이고 문맥·용어집·언어는
  두 번째 귀(교차 확인이 켜져 있을 때)에게만 간다. 그 글자는 D37 게이트를 지난 줄만 바꾸고, 사이에
  낀 글자는 기록에만 남는다(규칙 1).
- **유료 요청은 승인이 묶은 조각만 나간다** — 원본·PCM 해시·조각·모델, 켜져 있으면 글자 요청
  신원(용어집). 누른 뒤 무엇이든 바뀌면 digest가 달라 0송신으로 닫힌다.
- **고른 구간은 통째로 묻는다**(규칙 4) — 이미 다시 물어 원본 자막에 넣은 조각 안의 구간만 다시
  사지 않는다(같은 소리를 두 번 사지 않는다).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any, ClassVar

from vlogsrt import log, oscompat
from vlogsrt.project.raw import source_text
from vlogsrt.project.raw import understanding as stage1
from vlogsrt.project.raw.analysis_approval import (
    AsrPhaseApproval,
    asr_settings_fingerprint,
)
from vlogsrt.project.raw.analysis_config import RawUnderstandingSettings
from vlogsrt.project.raw.analysis_workflow import ApprovedPhaseAsrProvider, CountingAsrGate
from vlogsrt.project.raw.caption_provider import timing_provider_for
from vlogsrt.project.raw.config import (
    SOURCE_TEXT_SETTINGS,
    RawWorkflowConfig,
)
from vlogsrt.project.raw.source_transcript import REFILL_FIELD
from vlogsrt.project.raw.speech_gaps import GapSpan, SampleRange, request_pieces
from vlogsrt.project.raw.understanding import SampleSpan, SelectedAudio, UtteranceEvidence
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE, AnalysisApprovalError

REFILL_SCHEMA = "speech-refill/v1"
REFILL_DIRNAME = "speech-refill"
"""작업공간 안 자리 — `cache/speech-refill/<원본>/`(조각 WAV·두 귀의 답 캐시·실행 기록)."""


# ─────────────────────────────────────────────────────────────
#  재료 — 서비스가 모은 값
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RefillAudio:
    """자막 시각 확인이 말소리를 잰 **그** PCM — 해시로 묶는다. `audio_start`는 표본 0의 공통 시계.

    원본 영상은 읽지 않는다 — 이 PCM은 확인이 원본과 표본 대조했거나 원본에서 새로 디코드한 것이다
    (D55). 그래서 `SelectedAudio.source_path` 자리에도 이 PCM을 둔다(전사 길은 그 칸을 읽지 않는다).
    """

    source_id: str
    source_sha256: str
    stream_index: int
    pcm_path: Path
    pcm_sha256: str
    channels: int
    sample_count: int
    origin: Fraction
    audio_start: Fraction

    def selected(self) -> SelectedAudio:
        return SelectedAudio(
            source_id=self.source_id,
            source_path=self.pcm_path,
            source_sha256=self.source_sha256,
            audio_stream_index=self.stream_index,
            pcm_path=self.pcm_path,
            pcm_sha256=self.pcm_sha256,
            sample_rate=stage1.PCM_SAMPLE_RATE,
            channels=self.channels,
            sample_count=self.sample_count,
            pcm_sample0_file_time=self.origin,
        )


@dataclass(frozen=True, slots=True)
class RefillPlan:
    """범위의 재료 — 지금 원본 자막 리비전·잰 PCM·고른 구간·두 귀의 설정(값)."""

    root: Path
    asset_id: str
    transcript_revision: str
    source_sha256: str
    pcm_sha256: str
    sample_count: int
    audio_start: Fraction
    spans: tuple[GapSpan, ...]
    workflow: RawWorkflowConfig
    cross_check: bool
    glossary: tuple[str, ...]
    chunk_bound: int
    refill_ids: frozenset[str] = frozenset()
    """지금 원본 자막에 줄이 남아 있는 빈칸 재전사(`refill_id`) — 넣은 뒤 장부를 쓰기 전에 멈췄어도
    그 실행이 다룬 조각을 다시 사지 않게 한다(검증 지적 3)."""
    failures_known: bool = True
    """1단계 기록이 있어 전사 실패 구간을 아는가 — 모르면 화면이 「실패 구간 안」을 말하지
    않는다."""

    @property
    def folder(self) -> Path:
        return self.root / "cache" / REFILL_DIRNAME / self.asset_id


@dataclass(frozen=True, slots=True)
class RefillScope:
    """빈칸 재전사 범위 — 다룰 조각과 모든 결속을 접은 digest(D110 — 분량·금액 문구는 없다).

    `pieces`는 이번에 다룰 조각(이미 다시 물어 넣은 조각 안의 것은 뺐다), `whisper_due`·`text_due`는
    그중 답이 캐시에 없어 새로 사는 조각이다. 승인은 이 값 그대로를 묶는다.
    """

    asset_id: str
    transcript_revision: str
    source_sha256: str
    pcm_sha256: str
    audio_start: Fraction
    spans: tuple[GapSpan, ...]
    pieces: tuple[SampleRange, ...]
    asked: tuple[SampleRange, ...]
    whisper_due: tuple[SampleRange, ...]
    text_due: tuple[SampleRange, ...]
    timing_model: str
    text_model: str | None
    text_identity: str | None
    digest: str
    failures_known: bool = True

    @property
    def refill_id(self) -> str:
        return self.digest[:16]

    @property
    def asking_speech_seconds(self) -> Fraction:
        """이번에 묻는 조각 안의 자막 없는 말소리(초) — 이미 물은 조각 밖."""
        return _speech_in(self.spans, self.pieces)

    def at(self, sample: int) -> Fraction:
        """PCM 표본 → 공통 시계(자막 시각 확인과 같은 원점) — 화면이 빈칸 자리로 갈 때 쓴다."""
        return Fraction(sample, stage1.PCM_SAMPLE_RATE) + self.audio_start

    @property
    def speech_seconds(self) -> Fraction:
        """고른 구간 안의 자막 없는 말소리(초)."""
        return Fraction(sum(span.speech for span in self.spans), stage1.PCM_SAMPLE_RATE)

    @property
    def failed_seconds(self) -> Fraction:
        """그중 1단계 전사가 실패한 청크 안의 말소리(초)."""
        return Fraction(sum(span.failed_speech for span in self.spans), stage1.PCM_SAMPLE_RATE)


# ─────────────────────────────────────────────────────────────
#  범위 — 다룰 조각, 새로 살 조각, digest
# ─────────────────────────────────────────────────────────────


def scope_for(plan: RefillPlan) -> RefillScope:
    """지금 재료의 범위 — 같은 재료면 같은 바이트다(파일은 읽기만 한다)."""
    all_pieces = request_pieces(plan.spans, plan.chunk_bound)
    asked_runs = _asked_runs(plan)
    pieces = tuple(p for p in all_pieces if not _contained(p, asked_runs))
    asked = tuple(p for p in all_pieces if _contained(p, asked_runs))
    slices = _slice_shas(plan)
    whisper_due = tuple(p for p in pieces if not _whisper_cached(plan, slices.get(p), p))
    identity = _identity(plan)
    text_due = (
        tuple(p for p in pieces if not _text_cached(plan, slices.get(p), p))
        if identity is not None
        else ()
    )
    base = RefillScope(
        asset_id=plan.asset_id,
        transcript_revision=plan.transcript_revision,
        source_sha256=plan.source_sha256,
        pcm_sha256=plan.pcm_sha256,
        audio_start=plan.audio_start,
        spans=plan.spans,
        pieces=pieces,
        asked=asked,
        whisper_due=whisper_due,
        text_due=text_due,
        timing_model=plan.workflow.models.timing_model,
        text_model=plan.workflow.models.text_model if identity is not None else None,
        text_identity=identity,
        digest="",
        failures_known=plan.failures_known,
    )
    return replace(base, digest=_digest(plan, base))


def _speech_in(spans: Sequence[GapSpan], pieces: Sequence[SampleRange]) -> Fraction:
    """조각들 안에 든 자막 없는 말소리(초) — 구간의 말소리 칸과 조각의 겹침만 센다."""
    samples = sum(
        max(0, min(end, b) - max(start, a))
        for span in spans
        for start, end in span.speech_runs
        for a, b in pieces
    )
    return Fraction(samples, stage1.PCM_SAMPLE_RATE)


def _identity(plan: RefillPlan) -> str | None:
    """두 번째 귀에 묻는 요청의 신원 — 교차 확인이 꺼져 있으면 None(글자 요청 없음)."""
    if not plan.cross_check:
        return None
    return source_text.text_identity(SOURCE_TEXT_SETTINGS, _glossary_sha(plan.glossary))


def _glossary_sha(terms: tuple[str, ...]) -> str:
    from vlogsrt.project.raw.glossary import glossary_sha

    return glossary_sha(terms)


def _digest(plan: RefillPlan, scope: RefillScope) -> str:
    """digest — 원본·자막 리비전·PCM·구간·조각·새로 살 조각·모델·설정 지문·글자 신원.

    금액·문구 칸은 없다(D110) — 같은 재료의 빈칸 재전사 아이디(`refill_id`)가 D110 이전과 다르다
    (이전 기록은 그대로 읽힌다 — 이미 넣은 조각은 `asked.json`의 표본 범위로 센다)."""
    body = {
        "schema": REFILL_SCHEMA,
        "asset": scope.asset_id,
        "source_sha256": scope.source_sha256,
        "transcript_revision": scope.transcript_revision,
        "pcm_sha256": scope.pcm_sha256,
        "audio_start": str(scope.audio_start),
        "sample_count": plan.sample_count,
        "spans": [[s.start, s.end, s.speech, s.failed_speech] for s in scope.spans],
        "pieces": [list(p) for p in scope.pieces],
        "asked": [list(p) for p in scope.asked],
        "whisper": [list(p) for p in scope.whisper_due],
        "text": [list(p) for p in scope.text_due],
        "timing_model": scope.timing_model,
        "settings": asr_settings_fingerprint(RawUnderstandingSettings(), plan.workflow),
        "text_model": scope.text_model,
        "text_identity": scope.text_identity,
    }
    encoded = json.dumps(body, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


# ─────────────────────────────────────────────────────────────
#  기록 — 잘라 둔 조각의 WAV 해시 · 이미 넣은 조각 · 실행 기록
# ─────────────────────────────────────────────────────────────


def _read(path: Path) -> Any:
    """기록 하나 — 없으면 None, 읽지 못하면 WARN하고 None(묻는 쪽, 상한으로 센다)."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warn(f"빈칸 재전사 기록을 읽지 못했다 — 없는 것으로 칩니다: {path.name} ({error})")
        return None


def _write(path: Path, body: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(body, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    oscompat.replace(temp, path)


def _slice_key(pcm_sha256: str, piece: SampleRange) -> str:
    return f"{pcm_sha256}:{piece[0]}:{piece[1]}"


def _slice_shas(plan: RefillPlan) -> dict[SampleRange, str]:
    """잘라 둔 조각의 WAV 해시(이 PCM의 것만) — 캐시 열쇠를 다시 잘지 않고 재는 재료."""
    body = _read(plan.folder / "slices.json")
    found: dict[SampleRange, str] = {}
    for key, sha in (body or {}).items():
        pcm, _, rest = str(key).partition(":")
        start, _, end = rest.partition(":")
        if pcm == plan.pcm_sha256 and start.isdigit() and end.isdigit():
            found[(int(start), int(end))] = str(sha)
    return found


def _asked_runs(plan: RefillPlan) -> tuple[SampleRange, ...]:
    """이 PCM에서 이미 다시 물어 원본 자막에 반영한 조각 — 합쳐진 구간열.

    장부(`asked.json`)에 더해, 지금 원본 자막에 줄이 남아 있는 실행의 표지(`claims.json` — 넣기
    **전에** 쓴다)도 센다: 넣은 뒤 장부를 쓰기 전에 멈춰도 같은 소리를 다시 사지 않는다(검증
    지적 3).
    """
    body = _read(plan.folder / "asked.json") or {}
    pieces = [(int(a), int(b)) for a, b in body.get(plan.pcm_sha256, [])]
    claims = _read(plan.folder / "claims.json") or {}
    for refill_id in sorted(plan.refill_ids):
        claim = claims.get(refill_id) or {}
        if claim.get("pcm") == plan.pcm_sha256:
            pieces += [(int(a), int(b)) for a, b in claim.get("pieces", [])]
    merged: list[list[int]] = []
    for start, end in sorted(pieces):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((a, b) for a, b in merged)


def _contained(piece: SampleRange, runs: Sequence[SampleRange]) -> bool:
    return any(start <= piece[0] and piece[1] <= end for start, end in runs)


def _whisper_cached(plan: RefillPlan, wav: str | None, piece: SampleRange) -> bool:
    """그 조각의 whisper 답이 캐시에 있는가 — 실행이 쓸 열쇠 그대로(실패는 캐시되지 않는다)."""
    if wav is None:
        return False
    key = stage1.asr_chunk_key(wav, WHISPER_ORIGIN, plan.workflow, SampleSpan(piece[0], piece[1]))
    body = _read(plan.folder / "whisper" / f"{key}.json")
    return isinstance(body, dict) and body.get("fingerprint") == key


def _text_cached(plan: RefillPlan, wav: str | None, piece: SampleRange) -> bool:
    if wav is None:
        return False
    key = source_text.text_chunk_key(
        wav,
        SOURCE_TEXT_SETTINGS,
        _glossary_sha(plan.glossary),
        SampleSpan(piece[0], piece[1]),
        ORIGIN_LIVE,
    )
    return source_text.cached_answer_exists(plan.folder / "text", key)


def mark_asked(plan: RefillPlan, pieces: Iterable[SampleRange]) -> None:
    """원본 자막에 반영한(넣었거나 넣지 않은 이유를 남긴) 조각 — 다음 범위가 다시 사지 않는다."""
    path = plan.folder / "asked.json"
    body = _read(path) or {}
    known = {tuple(p) for p in body.get(plan.pcm_sha256, [])}
    known.update(pieces)
    body[plan.pcm_sha256] = sorted([list(p) for p in known])
    _write(path, body)


def claim(plan: RefillPlan, scope: RefillScope, pieces: Iterable[SampleRange]) -> None:
    """넣기 **전에** 그 실행이 다룬 조각을 `refill_id`로 적는다.

    그 아이디의 줄이 원본 자막에 있으면 다음 범위가 그 조각을 이미 물은 것으로 센다 — 넣은 뒤 장부를
    쓰기 전에 멈춰도 다시 사지 않는다."""
    path = plan.folder / "claims.json"
    body = _read(path) or {}
    body[scope.refill_id] = {"pcm": plan.pcm_sha256, "pieces": [list(p) for p in pieces]}
    _write(path, body)


def write_record(plan: RefillPlan, scope: RefillScope, record: Mapping[str, object]) -> Path:
    """실행 기록 — 넣은 줄·넣지 않은 줄·실패·글자 경로까지(규칙 9 — 조용히 사라지는 것은 없다).

    실행마다 따로 남는다(`<refill_id>-NNN.json`) — 같은 범위를 다시 승인해도 앞 기록을 덮지 않는다.
    """
    folder = plan.folder / "runs"
    sequence = len(list(folder.glob(f"{scope.refill_id}-*.json"))) + 1
    path = folder / f"{scope.refill_id}-{sequence:03d}.json"
    _write(path, {"schema": REFILL_SCHEMA, "digest": scope.digest, **record})
    return path


def records(root: Path, asset_id: str) -> list[dict]:
    """이 원본의 실행 기록 전부 — 최근 것이 먼저다."""
    folder = root / "cache" / REFILL_DIRNAME / asset_id / "runs"
    found = [
        body for path in sorted(folder.glob("*.json")) if isinstance(body := _read(path), dict)
    ]
    return sorted(found, key=lambda body: str(body.get("created_at", "")), reverse=True)


def latest_record(root: Path, asset_id: str) -> dict | None:
    """가장 최근 실행 기록 — 화면의 「지난 빈칸 재전사」 한 줄의 재료."""
    found = records(root, asset_id)
    return found[0] if found else None


def skipped_rows(root: Path, asset_id: str) -> list[dict[str, object]]:
    """넣지 않은 줄 전부(겹침·범위 밖) — 최근 실행부터, 같은 줄은 한 번. 화면이 글자째 보인다.

    그 조각은 이미 물은 것으로 적혀 다시 묻지 않으므로 사람이 이 글을 보고 직접 넣을 수 있어야 한다
    (규칙 9).
    """
    seen: set[str] = set()
    rows: list[dict[str, object]] = []
    for body in records(root, asset_id):
        for why, items in (
            ("overlap", body.get("overlapped")),
            ("range", body.get("out_of_range")),
        ):
            for item in items or []:
                key = str(item.get("utterance_id"))
                if key not in seen:
                    seen.add(key)
                    rows.append({**item, "why": why})
    return rows


# ─────────────────────────────────────────────────────────────
#  묻기 — 승인이 묶은 조각만
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class ApprovedRefillTimingProvider:
    """빈칸 재전사의 whisper 전송 경계 — 승인이 묶은 PCM·조각만 1단계 승인 문(`inner`)으로 간다.

    `inner`(`ApprovedPhaseAsrProvider`)가 전송 순간마다 환경·online·원본·모델·설정 지문·수명주기·
    스트림·범위·예약을 다시 본다. 이 층은 그 앞에서 **어느 소리의 어느 조각인가**를 본다.
    """

    CACHE_ORIGIN: ClassVar[str] = "ApprovedRefillTimingProvider"
    """청크 캐시 열쇠의 제공자 칸(P-6, D160) — 클래스 이름이 아니라 이 고정 값이다. 지금 이름과
    같아 열쇠 바이트가 그대로다 — 클래스 이름을 바꿔도 이 값은 그대로 둔다."""

    inner: Any
    audio: SelectedAudio
    pcm_sha256: str
    pieces: frozenset[SampleRange]

    def timed_transcript(
        self,
        *,
        binding: Any,
        chunk: Any,
        settings: RawWorkflowConfig,
        on: date,
        stem: str = "",
        source: str = "",
    ) -> Any:
        if self.audio.pcm_sha256 != self.pcm_sha256:
            raise AnalysisApprovalError("승인이 본 PCM과 지금의 PCM이 다르다 — 보내지 않는다")
        if (int(binding.sample_start), int(binding.sample_end)) not in self.pieces:
            raise AnalysisApprovalError("승인한 범위에 없던 구간이다 — 승인 밖이라 보내지 않는다")
        return self.inner.timed_transcript(
            binding=binding, chunk=chunk, settings=settings, on=on, stem=stem, source=source
        )


WHISPER_ORIGIN = ApprovedRefillTimingProvider.CACHE_ORIGIN
"""whisper 캐시 열쇠의 제공자 칸 — 실행(`provider_origin`)과 범위 재기가 같은 값을 쓴다."""


@dataclass(frozen=True, slots=True)
class RefillRun:
    """한 번의 실행 — 넣을 줄(공통 시계), whisper 증거, 글자 경로 기록, 조각마다의 결과."""

    rows: list[dict]
    evidence: stage1.AsrEvidence
    text_record: dict[str, object] | None
    answered: tuple[SampleRange, ...]
    failed: tuple[dict[str, object], ...]


def run(
    plan: RefillPlan, scope: RefillScope, audio: RefillAudio, transport: Any, *, on: date
) -> RefillRun:
    """승인한 범위대로 묻는다 — 조각을 자르고(로컬), 승인을 만들고, whisper·두 번째 귀에 묻는다."""
    if audio.pcm_sha256 != scope.pcm_sha256 or audio.source_sha256 != scope.source_sha256:
        raise AnalysisApprovalError("승인이 본 원본·PCM과 지금이 다르다 — 보내지 않는다")
    selected = audio.selected()
    workdir = plan.folder / scope.refill_id
    shas = _cut_slices(plan, selected, scope.pieces, workdir)
    approval = _approval(plan, scope, audio, shas, on)
    spans = tuple(SampleSpan(a, b) for a, b in scope.pieces)
    evidence = stage1.transcribe_selected_audio(
        selected,
        spans,
        settings=plan.workflow,
        understanding=RawUnderstandingSettings(),
        provider=_timing_provider(plan, scope, selected, approval, transport),
        on=on,
        request_id=f"refill:{scope.refill_id}:{plan.asset_id}",
        workdir=workdir,
        cache_dir=plan.folder / "whisper",
    )
    utterances, record = _ears(plan, scope, selected, evidence, approval, transport, on, workdir)
    rows = _rows(utterances, audio.audio_start, scope.refill_id)
    answered = tuple((c.sample_start, c.sample_end) for c in evidence.chunks if c.succeeded)
    failed = tuple(
        {"start": c.sample_start, "end": c.sample_end, "failure": c.failure}
        for c in evidence.chunks
        if not c.succeeded
    )
    return RefillRun(rows, evidence, record, answered, failed)


def _cut_slices(
    plan: RefillPlan, audio: SelectedAudio, pieces: Sequence[SampleRange], workdir: Path
) -> dict[SampleRange, str]:
    """조각마다 WAV를 저장 바이트 그대로 자른다(로컬·무료) — 그 해시로 다음 범위가 캐시를 잰다."""
    workdir.mkdir(parents=True, exist_ok=True)
    shas: dict[SampleRange, str] = {}
    for position, piece in enumerate(pieces):
        path = stage1.chunk_wav_path(workdir, position)
        stage1.write_pcm_slice(audio, SampleSpan(*piece), path)
        shas[piece] = hashlib.sha256(path.read_bytes()).hexdigest()
    path = plan.folder / "slices.json"
    body = _read(path) or {}
    body.update({_slice_key(plan.pcm_sha256, piece): sha for piece, sha in shas.items()})
    _write(path, body)
    return shas


def _approval(
    plan: RefillPlan,
    scope: RefillScope,
    audio: RefillAudio,
    shas: Mapping[SampleRange, str],
    on: date,
) -> AsrPhaseApproval:
    """범위가 잰 값 그대로의 1단계 승인 — whisper 상한은 새로 살 조각 수, 글자 칸은 켜졌을 때만."""
    text: dict[str, Any] = {}
    if scope.text_identity is not None and scope.text_due:
        keys = tuple(_text_key(plan, shas[p], p) for p in scope.text_due)
        text = {
            "text_model": scope.text_model,
            "text_identity": scope.text_identity,
            "text_chunk_budget": len(keys),
            "text_chunk_keys": keys,
        }
    return AsrPhaseApproval(
        approved=True,
        source_sha256=audio.source_sha256,
        audio_stream_index=audio.stream_index,
        chunk_budget=max(1, len(scope.whisper_due)),
        settings_fingerprint=asr_settings_fingerprint(RawUnderstandingSettings(), plan.workflow),
        timing_model=scope.timing_model,
        approved_on=on.isoformat(),
        **text,
    )


def _text_key(plan: RefillPlan, wav: str, piece: SampleRange) -> str:
    return source_text.text_chunk_key(
        wav,
        SOURCE_TEXT_SETTINGS,
        _glossary_sha(plan.glossary),
        SampleSpan(*piece),
        ORIGIN_LIVE,
    )


def _transport_pair(transport: Any) -> tuple[Any, Any]:
    sender, cfg = getattr(transport, "sender", None), getattr(transport, "cfg", None)
    if sender is None or cfg is None:
        raise AnalysisApprovalError(
            "실제 호출에는 전송 경계(sender)와 엔진 설정(cfg)이 필요하다 — 사람이 공급한다"
        )
    return sender, cfg


def _timing_provider(
    plan: RefillPlan,
    scope: RefillScope,
    audio: SelectedAudio,
    approval: AsrPhaseApproval,
    transport: Any,
) -> ApprovedRefillTimingProvider:
    """whisper 경계 — 바깥에서 조각·PCM, 안쪽에서 1단계 승인 문·예약(상한 = 새로 살 조각 수)."""
    sender, cfg = _transport_pair(transport)
    inner = ApprovedPhaseAsrProvider(
        inner=timing_provider_for(
            plan.workflow,
            sender=sender,
            cfg=cfg,
            gemini_client=getattr(transport, "timing_client", None),
        ),
        workflow=plan.workflow,
        understanding=RawUnderstandingSettings(),
        audio=audio,
        approval=approval,
        gate=CountingAsrGate(limit=approval.chunk_budget),
    )
    return ApprovedRefillTimingProvider(
        inner=inner, audio=audio, pcm_sha256=scope.pcm_sha256, pieces=frozenset(scope.whisper_due)
    )


def _ears(
    plan: RefillPlan,
    scope: RefillScope,
    audio: SelectedAudio,
    evidence: stage1.AsrEvidence,
    approval: AsrPhaseApproval,
    transport: Any,
    on: date,
    workdir: Path,
) -> tuple[tuple[UtteranceEvidence, ...], dict[str, object] | None]:
    """시간 엔진이 들은 발화 — 아이디만 넣을 줄의 아이디로 바꾼다(시각·순서는 그대로).

    같은 WAV를 두 번째 귀(gpt)에 묻던 글자 경로(D59 19c)는 whisper와 함께 지웠다(D163)."""
    utterances = tuple(
        replace(item, utterance_id=f"refill_{scope.refill_id}_{ordinal:04d}")
        for ordinal, item in enumerate(stage1.utterance_evidence(audio, evidence))
    )
    return utterances, None  # 두 번째 귀(gpt)는 whisper와 함께 지웠다(D163)


def _rows(
    utterances: Sequence[UtteranceEvidence], audio_start: Fraction, refill_id: str
) -> list[dict]:
    """넣을 줄 — PCM 틱은 그대로, 시각만 공통 시계로 민다(확인과 같은 원점). 출처 칸을 단다."""
    shifted = [
        replace(item, start=item.start + audio_start, end=item.end + audio_start)
        for item in utterances
    ]
    rows = stage1.utterances_json(shifted)
    for row in rows:
        row[REFILL_FIELD] = refill_id
    return rows


__all__ = [
    "REFILL_DIRNAME",
    "REFILL_SCHEMA",
    "WHISPER_ORIGIN",
    "ApprovedRefillTimingProvider",
    "RefillAudio",
    "RefillPlan",
    "RefillScope",
    "RefillRun",
    "claim",
    "latest_record",
    "mark_asked",
    "records",
    "skipped_rows",
    "scope_for",
    "run",
    "write_record",
]
