"""「글자 다시 받기(Hydrogen)」(W-114d, D187) — 이미 들은 Gemini 영상의 원본 자막 글자를 Hydrogen이
그 영상의 1단계 청크 WAV로 다시 듣는다. 시각·줄은 지금 리비전 그대로다.

재료는 지금 원본 자막 리비전의 줄과, 그 영상의 가장 최근 1단계 기록(`asr_outcome.json` 들)의 청크
(범위·WAV sha)와 같은 폴더의 청크 WAV다. 줄은 1단계 청크 번호·틱(`chunk_index`·`start_sample`·
`end_sample`)으로 청크에 놓는다 — 그 칸이 없거나 청크 밖인 줄(빈칸 재전사로 넣은 줄 등)과 사람이
준 글자의 줄은 보내지 않고 글자 그대로다(`replace_texts`도 사람 글자를 덮지 않는다).

범위(`RetextScope`)의 digest가 지금 리비전·요청 신원·청크 열쇠를 묶는다 — 화면 단추가 그 digest를
싣고 서버가 누른 순간 다시 재어 대조한다(D110 — 누름이 승인, 견적 없음). 물을 청크(캐시에 답이
없는 것)만 보내고 그 수가 요청 상한이다. 받은 답은 1단계 글자 귀와 같은 캐시에 남는다(열쇠가 같다 —
같은 WAV·같은 줄·같은 초안이면 다시 사지 않는다). 쓰기는 서비스의 정착 한 자리가 한다.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import log
from vlogsrt.project.raw import hydrogen_text, hydrogen_text_pass, request_log
from vlogsrt.project.raw import hydrogen_text_io as hio
from vlogsrt.project.raw.config import HydrogenTextSettings
from vlogsrt.project.raw.source_transcript import HUMAN_TEXT_SOURCES
from vlogsrt.project.raw.understanding import chunk_wav_path
from vlogsrt.project.raw.understanding_luna import ORIGIN_LIVE
from vlogsrt.task_runtime import check_cancel, provider_error

DIGEST_SCHEMA = "raw-hydrogen-retext-scope/v1"
STAGE1_RECORDS = ("asr_outcome.json", "asr_phase.json")
"""청크 WAV·범위를 실은 1단계 기록 — 청크 WAV가 같은 폴더에 있다."""

NO_TRANSCRIPT = "원본 자막이 없습니다 — 먼저 이 영상을 들어 주세요."
NO_STAGE1 = (
    "이 영상의 1단계 청크 소리를 찾지 못했습니다 — 「다시 듣기」로 다시 들으면 Hydrogen 글자를"
    " 함께 받습니다."
)
NO_LINES = "청크에 놓을 수 있는 자막 줄이 없습니다 — 아무것도 보내지 않습니다."


@dataclass(frozen=True, slots=True)
class RetextChunk:
    """보낼 청크 하나 — 1단계 청크 범위·WAV와 그 청크에 놓인 줄(청크 국소 시각 · 지금 글자)."""

    chunk_index: int
    sample_start: int
    sample_end: int
    wav_sha256: str
    wav_path: Path
    lines: tuple[hio.ChunkLine, ...]
    key: str
    cached: bool


@dataclass(frozen=True, slots=True)
class RetextScope:
    """지금 범위 — 리비전·청크·digest. `pieces`는 물을(캐시에 답이 없는) 청크 수다."""

    asset_id: str
    revision: str
    chunks: tuple[RetextChunk, ...]
    digest: str
    pieces: int
    left_out: int
    """청크에 놓지 못해 보내지 않는 줄 수 — 글자 그대로다."""


def scope_for(
    workspace_root: Path,
    asset_id: str,
    document: Mapping[str, Any],
    settings: HydrogenTextSettings,
) -> RetextScope | str:
    """지금 범위 — 재료가 없으면 그 이유(존댓말 한 줄)를 돌려준다. 읽기만 한다(보내지 않는다)."""
    record = _latest_stage1(workspace_root, asset_id, str(document["source_sha256"]))
    if record is None:
        return NO_STAGE1
    folder, body = record
    rate = int(body["asr"]["sample_rate"])
    cache_dir = cache_folder(workspace_root, asset_id)
    rows = list(document["utterances"])
    chunks = []
    placed = 0
    for raw in body["asr"]["chunks"]:
        if raw.get("failure"):  # 실패 청크 — 빈 글이면 성공이다
            continue
        chunk = _chunk(folder, raw, rows, rate, cache_dir, settings)
        if chunk is not None:
            chunks.append(chunk)
            placed += len(chunk.lines)
    if not chunks:
        return NO_LINES
    digest = _digest(str(document["revision"]), chunks, settings)
    return RetextScope(
        asset_id=asset_id,
        revision=str(document["revision"]),
        chunks=tuple(chunks),
        digest=digest,
        pieces=sum(not chunk.cached for chunk in chunks),
        left_out=len(rows) - placed,
    )


def cache_folder(workspace_root: Path, asset_id: str) -> Path:
    """1단계 글자 귀와 같은 캐시 자리 — 같은 열쇠면 같은 답이다."""
    return workspace_root / "cache" / hydrogen_text_pass.TEXT_CACHE_DIRNAME / asset_id


def _chunk(
    folder: Path,
    raw: Mapping[str, Any],
    rows: Sequence[Mapping[str, Any]],
    rate: int,
    cache_dir: Path,
    settings: HydrogenTextSettings,
) -> RetextChunk | None:
    index, start, end = int(raw["chunk_index"]), int(raw["sample_start"]), int(raw["sample_end"])
    wav = chunk_wav_path(folder, index)
    heard = [str(segment.get("text", "")) for segment in raw.get("segments") or ()]
    lines = tuple(
        hio.ChunkLine(
            line_id=str(row["utterance_id"]),
            start=Fraction(int(row["start_sample"]) - start, rate),
            end=Fraction(int(row["end_sample"]) - start, rate),
            draft=str(row["text"]),
        )
        for row in rows
        if _inside(row, index, start, end) and not _typed(row, heard)
    )
    if not lines or not wav.is_file():
        return None
    sha = str(raw["wav_sha256"])
    key = hydrogen_text_pass.chunk_key(sha, lines, settings, ORIGIN_LIVE)
    cached = hydrogen_text_pass.cached_plan(cache_dir / f"{key}.json", key, lines) is not None
    return RetextChunk(index, start, end, sha, wav, lines, key, cached)


def _typed(row: Mapping[str, Any], heard: Sequence[str]) -> bool:
    """사람이 준 글자인가 — 출처가 사람·섞임이거나, 증거 칸이 없는데 1단계가 그 자리에서 들은
    글과 다르다(증거 칸이 없는 줄은 고쳐도 출처가 바뀌지 않는다). 그런 줄은 보내지 않는다."""
    if row.get("text_source") in HUMAN_TEXT_SOURCES:
        return True
    if "text_source" in row:
        return False
    position = row.get("position")
    first = heard[position] if isinstance(position, int) and 0 <= position < len(heard) else None
    return first is None or " ".join(first.split()) != " ".join(str(row["text"]).split())


def _inside(row: Mapping[str, Any], index: int, start: int, end: int) -> bool:
    """그 청크에 든 줄인가 — 1단계 청크 번호·틱이 있고 청크 안이어야 한다."""
    try:
        same = int(row["chunk_index"]) == index
        first, last = int(row["start_sample"]), int(row["end_sample"])
    except (KeyError, TypeError, ValueError):
        return False
    return same and start <= first <= last <= end


def _latest_stage1(
    workspace_root: Path, asset_id: str, source_sha256: str
) -> tuple[Path, dict[str, Any]] | None:
    """가장 최근 1단계 기록(원본 해시가 같고 청크가 있는 것)과 그 폴더 — 없으면 None."""
    found: list[tuple[str, Path, dict[str, Any]]] = []
    for folder in sorted(workspace_root.glob(f"analysis/*/*_{asset_id}")):
        for name in STAGE1_RECORDS:
            path = folder / name
            if not path.is_file():
                continue
            try:
                body = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as error:
                log.warn(f"글자 다시 받기 — 1단계 기록을 읽지 못해 건너뜁니다: {path} ({error})")
                break
            asr = body.get("asr") if isinstance(body, dict) else None
            if (
                body.get("source_sha256") == source_sha256
                and isinstance(asr, dict)
                and asr.get("chunks")
            ):
                found.append((str(body.get("created_at") or ""), folder, body))
            break
    if not found:
        return None
    _created, folder, body = max(found, key=lambda item: item[0])
    return folder, body


def _digest(revision: str, chunks: Sequence[RetextChunk], settings: HydrogenTextSettings) -> str:
    body = {
        "schema": DIGEST_SCHEMA,
        "revision": revision,
        "identity": hydrogen_text_pass.text_identity(settings),
        "chunks": [[chunk.chunk_index, chunk.key] for chunk in chunks],
    }
    text = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ─────────────────────────────────────────────────────────────
#  묻기 — 캐시가 먼저, 물을 청크만 상한 안에서
# ─────────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RetextResult:
    """받은 글자(줄 아이디 → 글자)와 청크마다의 결과 기록."""

    texts: dict[str, str]
    chunks: tuple[dict[str, object], ...]
    asked: int


def run(
    workspace_root: Path,
    scope: RetextScope,
    client: object,
    settings: HydrogenTextSettings,
    *,
    on: date,
) -> RetextResult:
    """청크마다 Hydrogen 글자를 구한다(유료 — 캐시에 없는 청크만). 실패는 그 청크의 기록이다."""
    provider = hydrogen_text_pass.LiveHydrogenProvider(
        client=client, model=settings.model, budget=scope.pieces
    )
    request = hydrogen_text_pass.HydrogenPassRequest(
        settings=settings,
        on=on,
        request_id=f"hydrogen-retext:{scope.asset_id}",
        workdir=workspace_root,
        cache_dir=cache_folder(workspace_root, scope.asset_id),
        origin=ORIGIN_LIVE,
    )
    texts: dict[str, str] = {}
    records: list[dict[str, object]] = []
    for chunk in scope.chunks:
        check_cancel()
        plan, record = _chunk_plan(chunk, request, provider)
        records.append(record)
        if plan is not None:
            texts.update(
                (row.line_id, row.text)
                for row in plan.lines
                if row.source == hydrogen_text.SOURCE_HYDROGEN
            )
    return RetextResult(texts=texts, chunks=tuple(records), asked=provider.sent)


def _chunk_plan(
    chunk: RetextChunk,
    request: hydrogen_text_pass.HydrogenPassRequest,
    provider: hydrogen_text_pass.LiveHydrogenProvider,
) -> tuple[hydrogen_text.LineTextPlan | None, dict[str, object]]:
    record: dict[str, object] = {"chunk_index": chunk.chunk_index, "key": chunk.key}
    cached = hydrogen_text_pass.cached_plan(
        request.cache_dir / f"{chunk.key}.json", chunk.key, chunk.lines
    )
    if cached is not None:
        return cached, {**record, "status": "received", "cached": True}
    attempt: str | None = None
    try:
        wav = chunk.wav_path.read_bytes()
        if hashlib.sha256(wav).hexdigest() != chunk.wav_sha256:
            raise ValueError("청크 WAV가 1단계가 들은 것과 다르다 — 다른 소리에 글자를 얹지 않는다")
        body = hio.payload(wav, chunk.lines, request.settings)
        attempt = request_log.begin(hydrogen_text_pass.TEXT_REQUEST_STAGE, chunk.key)
        response = provider.generate(body)
        request_log.finish(attempt, received=True)
        hydrogen_text_pass.record_usage(response, request, attempt)
    except Exception as exc:  # noqa: BLE001 - 글자 실패는 이 청크의 기록이다(규칙 9)
        category, note, uncertain = provider_error(exc)
        request_log.finish(attempt, received=not uncertain, note=category)
        check_cancel()
        log.warn(f"글자 다시 받기 청크 {chunk.chunk_index} 실패 — 글자 그대로 둡니다: {note}")
        return None, {**record, "status": "unavailable", "error": note}
    plan = hio.read_reply(response, chunk.lines)
    if not plan.accepted:
        note = f"{plan.reason}: {plan.detail}"
        log.warn(f"글자 다시 받기 청크 {chunk.chunk_index} — 받지 않았습니다: {note}")
        return None, {**record, "status": "unavailable", "error": note}
    hydrogen_text_pass.write_cache(request.cache_dir, chunk.key, request.settings, response)
    no_speech = [row.line_id for row in plan.lines if row.no_speech]
    extra = [{"after_id": item.after_id, "text": item.text} for item in plan.extra]
    return plan, {**record, "status": "received", "no_speech": no_speech, "extra": extra}


__all__ = [
    "NO_LINES",
    "NO_STAGE1",
    "NO_TRANSCRIPT",
    "RetextChunk",
    "RetextResult",
    "RetextScope",
    "cache_folder",
    "run",
    "scope_for",
]
