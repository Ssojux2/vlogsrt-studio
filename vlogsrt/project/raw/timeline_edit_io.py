"""원본 범위 타임라인(W-71b, D126)의 만들기·열기·저장 — 작업공간 `timeline/<UTC 시각>/`에만 쓴다.

「타임라인으로 보내기」는 렌더하지 않는다. 초안 내보내기와 **같은 재료·같은 계획**
(`render_export`의 `gather_sources`·`_build` → `render_plan.build_plan`)에서 추천 조각을 원본 프레임
구간 클립으로 옮기고(`timeline_edit.facts_from_plan`·`Clip`), 클립이 있는 원본마다 지금 원본 자막을
(리비전, 줄)로 심는다(`timeline_edit.seed`). ffmpeg 렌더는 부르지 않는다 — ffprobe 조사만 한다.

- **새 시각 폴더**: 보낼 때마다 `timeline/<시각>/`을 새로 잡는다(있으면 `_2`…) — 다시 보내도 사람이
  고친 타임라인을 덮지 않는다. 열기는 가장 새 폴더(또는 고른 폴더)다.
- **사실은 파일에서만**: 시퀀스 포맷·원본 격자(`facts`)와 심은 리비전(`seeded`)은 만들 때 파일에
  쓰고, 저장할 때 파일에서 읽는다 — 브라우저가 보낸 것으로 바꾸지 않는다(`seeded`가 다르면 거부).
- **쓰기 한 자리**: `_write_atomic`(정해진 이름 하나, 임시 파일 → 이름 바꾸기)뿐이고 폴더는
  `_reserve_folder`의 한 줄로만 만든다(불변식 「원본 범위 타임라인은 timeline/에만 쓴다」).
  원본·초안·원본 자막은 읽기만 한다.
- **저장**: 문서 전체를 순수 모델이 다시 검사하고, 화면이 본 리비전이 지금과 다르면 `conflict`(D117
  `final_edit_io.save_edit`과 같은 꼴).
- **열기는 읽기만**: 심은 원본 자막 리비전이 지금과 다르면 원본 이름으로 알린다(다시 심지 않는다).
- **자막 상한**: 클립이 있는 원본의 자막만 심는다 — 클립이 없는 원본은 지금 어느 연산으로도
  타임라인에 돌아오지 않으므로 잃는 것이 없다(요약에 이름을 남긴다). 그래도 `max_cues`를 넘으면
  수·상한·가장 많은 원본을 대고 거부한다(D126).
- **요약(규칙 9·D35)**: 지원 밖 원본(이름·까닭 = `plan.unsupported`), 정리 뒤 빈 줄(`empty`)·원본 밖
  시각(`clamped`), 심지 않은 원본, 뺀 구간(`removed_ranges`)을 남긴다.
"""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import oscompat
from vlogsrt.project.raw import render_export, render_plan, timeline_edit
from vlogsrt.project.raw.config import RENDER_SETTINGS, OutputFormatSettings, RenderSettings
from vlogsrt.project.raw.final_edit_config import (
    FINAL_EDIT_SETTINGS,
    FinalEditSettings,
    client_settings,
)
from vlogsrt.project.raw.timeline_edit import TimelineDocument, TimelineEditError, TimelineFacts

if TYPE_CHECKING:
    from vlogsrt.project.raw.jobs import JobContext
    from vlogsrt.project.raw.models import SourceAsset
    from vlogsrt.project.raw.service import RawWorkspaceService

TIMELINE_FOLDER = "timeline"
"""작업공간의 타임라인 자리 — 옛 작업공간에는 없어 처음 쓸 때 만든다(`WORKSPACE_LAYOUT` 밖)."""
TIMELINE_NAME = "timeline.json"
TIMELINE_FILES = frozenset({TIMELINE_NAME})
"""`_write_atomic`이 쓸 수 있는 이름의 전부."""
FILE_SCHEMA = "vlogsrt-timeline-file/v1"
NO_TRANSCRIPT = ""
"""심을 때 원본 자막이 없었던 원본의 `seeded` 값 — 나중에 자막이 생기면 바뀐 것으로 알린다."""
NAMED_LARGEST = 3
"""자막 상한 거부가 이름을 대는 원본 수(가장 많은 것부터) — 글의 길이일 뿐 판정 기준이 아니다."""
_STAMP = "%Y%m%dT%H%M%SZ"
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
_FILE_MODE = 0o644
_SAVE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class CreatedTimeline:
    """보낸 타임라인 — 새 폴더와 문서, 계획(지원 밖 원본 등은 요약에 있다)."""

    folder: Path
    document: TimelineDocument
    plan: render_plan.RenderPlan
    record: dict[str, Any]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _rational(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _fraction(value: object) -> Fraction:
    if not isinstance(value, dict):
        raise TimelineEditError(f"유리수 모양이 아니다: {value!r}")
    return Fraction(int(value["num"]), int(value["den"]))


# ─── 만들기 ──────────────────────────────────────────────────────────────


def create_timeline(
    service: RawWorkspaceService,
    root: Path,
    context: JobContext | None = None,
    *,
    settings: RenderSettings = RENDER_SETTINGS,
    edit_settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
    output: OutputFormatSettings | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> CreatedTimeline:
    """추천 조각 → 원본 범위 타임라인 → 새 `timeline/<시각>/timeline.json`. 렌더하지 않는다.

    `output`이 있으면 시퀀스는 그 고정 크기·레이트(D172 — 화면의 「타임라인으로 보내기」는
    `OUTPUT_FORMAT`을 넘긴다), 없으면 렌더 계획이 고른 시퀀스 그대로다."""
    gathered = render_export.gather_sources(service, root, context)
    plan = render_export._build(gathered, settings)
    facts = timeline_edit.facts_from_plan(plan, [item.source for item in gathered], output)
    clips = tuple(
        timeline_edit.Clip(part.source_id, part.start_frame, part.end_frame) for part in plan.parts
    )
    names = {item.asset.asset_id: item.asset.source_name for item in gathered}
    clipped = {clip.source_id for clip in clips}
    transcripts = {
        item.asset.asset_id: _seed_transcript(service, root, item.asset)
        for item in gathered
        if item.asset.asset_id in clipped
    }
    _require_cue_cap(transcripts, names, edit_settings)
    seeded = timeline_edit.seed(facts, clips, transcripts, edit_settings)
    moment = now()
    record = _record(
        seeded.document,
        revision=1,
        created=moment,
        saved=moment,
        facts=_facts_document(facts, names),
        seed=_seed_document(plan, seeded, facts, clipped, names),
    )
    folder = _reserve_folder(root.expanduser() / TIMELINE_FOLDER, moment)
    _write_record(folder, record)
    return CreatedTimeline(folder, seeded.document, plan, record)


def _seed_transcript(
    service: RawWorkspaceService, root: Path, asset: SourceAsset
) -> tuple[str, tuple[render_plan.SourceCue, ...]]:
    revision, cues = render_export.transcript_with_revision(service, root, asset)
    return (NO_TRANSCRIPT if revision is None else revision), cues


def _require_cue_cap(
    transcripts: Mapping[str, tuple[str, Sequence[render_plan.SourceCue]]],
    names: Mapping[str, str],
    settings: FinalEditSettings,
) -> None:
    """심을 자막(정리 뒤 빈 줄 제외)이 상한을 넘으면 수·상한·가장 많은 원본을 대고 거부한다."""
    counts = {
        source_id: sum(1 for cue in cues if render_plan.caption_text(cue.text).strip())
        for source_id, (_, cues) in transcripts.items()
    }
    total = sum(counts.values())
    if total <= settings.max_cues:
        return
    largest = sorted(counts.items(), key=lambda row: (-row[1], names.get(row[0], row[0])))
    listed = ", ".join(f"{names.get(sid, sid)} {count}줄" for sid, count in largest[:NAMED_LARGEST])
    raise TimelineEditError(
        f"타임라인에 심을 자막이 {total}줄로 상한 {settings.max_cues}줄을 넘어 만들지 않았다 —"
        f" 가장 많은 원본: {listed}"
    )


def _facts_document(facts: TimelineFacts, names: Mapping[str, str]) -> dict[str, Any]:
    sequence = facts.sequence
    return {
        "sequence": {
            "orientation": sequence.orientation,
            "width": sequence.width,
            "height": sequence.height,
            "frame_duration": _rational(sequence.frame_duration),
        },
        "sources": [
            {
                "source_id": source.source_id,
                "name": names.get(source.source_id, source.source_id),
                "frame_duration": _rational(source.frame_duration),
                "frame_count": source.frame_count,
            }
            for source in facts.sources
        ],
    }


def _seed_document(
    plan: render_plan.RenderPlan,
    seeded: timeline_edit.Seeded,
    facts: TimelineFacts,
    clipped: set[str],
    names: Mapping[str, str],
) -> dict[str, Any]:
    """만들 때만 아는 것 — 지원 밖 원본, 정리 뒤 빈 줄·원본 밖 시각, 심지 않은 원본, 계획 크기."""
    return {
        "unsupported": [
            {
                "source_id": row.source_id,
                "name": row.name,
                "reason": row.reason,
                "detail": row.detail,
            }
            for row in plan.unsupported
        ],
        "empty": seeded.empty,
        "clamped": seeded.clamped,
        "unseeded": [
            {"source_id": source.source_id, "name": names.get(source.source_id, source.source_id)}
            for source in facts.sources
            if source.source_id not in clipped
        ],
        "plan_parts": len(plan.parts),
        "plan_total_frames": plan.total_frames,
    }


def _record(
    document: TimelineDocument,
    *,
    revision: int,
    created: datetime,
    saved: datetime,
    facts: Mapping[str, Any],
    seed: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "schema": FILE_SCHEMA,
        "document_schema": timeline_edit.SCHEMA,
        "revision": revision,
        "created_at": created.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "saved_at": saved.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "facts": dict(facts),
        "seed": dict(seed),
        "document": timeline_edit.to_payload(document),
        "summary": _summary(document),
    }


def _summary(document: TimelineDocument) -> dict[str, Any]:
    """지금 문서의 무엇이 보이고 빠졌는가 — 규칙 9: 가린·잘린 자막과 뺀 구간을 남긴다."""
    projection = timeline_edit.project(document)
    return {
        "clips": len(document.clips),
        "cues": len(document.cues),
        "visible_cues": len(projection.cues),
        "hidden_by_cut": [cue.cue_id for cue in projection.hidden],
        "trimmed_by_cut": [cue.cue_id for cue in projection.cues if cue.trimmed],
        "removed_ranges": [list(gap) for gap in timeline_edit.removed_ranges(document)],
        "edited_frames": projection.total_frames,
    }


# ─── 읽기 ────────────────────────────────────────────────────────────────


def _stamp_order(folder: Path) -> tuple[str, int]:
    stamp, _, attempt = folder.name.partition("_")
    return stamp, int(attempt) if attempt.isdigit() else 1


def timeline_folders(root: Path) -> list[Path]:
    """다 쓴 타임라인 폴더(파일이 있다), 새것부터. 읽기만 한다."""
    base = root.expanduser() / TIMELINE_FOLDER
    if not base.is_dir():
        return []
    folders = sorted((p for p in base.iterdir() if p.is_dir()), key=_stamp_order, reverse=True)
    return [folder for folder in folders if (folder / TIMELINE_NAME).is_file()]


def _folder_by_id(root: Path, timeline_id: str | None) -> Path:
    folders = timeline_folders(root)
    if not folders:
        raise TimelineEditError("아직 보낸 타임라인이 없다 — 「타임라인으로 보내기」를 먼저 하세요")
    if timeline_id is None:
        return folders[0]
    for folder in folders:
        if folder.name == timeline_id:
            return folder
    raise TimelineEditError(f"타임라인 `{timeline_id}`이(가) 없다")


def _read_record(folder: Path) -> dict[str, Any]:
    try:
        record = json.loads((folder / TIMELINE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise TimelineEditError(f"타임라인 `{folder.name}`을 읽지 못했다 — {error}") from error
    if (
        not isinstance(record, dict)
        or record.get("schema") != FILE_SCHEMA
        or record.get("document_schema") != timeline_edit.SCHEMA
    ):
        raise TimelineEditError(f"타임라인 `{folder.name}`의 판이 다르다")
    return record


def _facts_from_record(record: Mapping[str, Any]) -> TimelineFacts:
    try:
        facts = record["facts"]
        sequence = facts["sequence"]
        return TimelineFacts(
            sequence=render_plan.SequenceFormat(
                str(sequence["orientation"]),
                int(sequence["width"]),
                int(sequence["height"]),
                _fraction(sequence["frame_duration"]),
            ),
            sources=tuple(
                timeline_edit.TimelineSource(
                    str(row["source_id"]), _fraction(row["frame_duration"]), int(row["frame_count"])
                )
                for row in facts["sources"]
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise TimelineEditError(f"타임라인 사실(`facts`)의 모양이 틀렸다 — {error}") from error


def _source_names(record: Mapping[str, Any]) -> dict[str, str]:
    return {str(row["source_id"]): str(row["name"]) for row in record["facts"]["sources"]}


def _document(record: Mapping[str, Any], settings: FinalEditSettings) -> TimelineDocument:
    payload = record.get("document")
    if not isinstance(payload, dict):
        raise TimelineEditError("타임라인 파일에 문서(`document`)가 없다")
    return timeline_edit.from_payload(payload, _facts_from_record(record), settings)


def changed_transcripts(
    service: RawWorkspaceService, root: Path, document: TimelineDocument, names: Mapping[str, str]
) -> list[dict[str, str]]:
    """심은 원본 자막 리비전이 지금과 다른 원본 — 읽기만 한다(다시 심지 않는다)."""
    changed: list[dict[str, str]] = []
    for source_id, seeded in document.seeded:
        current = service.source_transcript(root, source_id)
        now_revision = NO_TRANSCRIPT if current is None else str(current.get("revision", ""))
        if now_revision != seeded:
            changed.append(
                {
                    "source_id": source_id,
                    "name": names.get(source_id, source_id),
                    "seeded": seeded,
                    "current": now_revision,
                }
            )
    return changed


def open_timeline(
    service: RawWorkspaceService,
    root: Path,
    timeline_id: str | None = None,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> dict[str, Any]:
    """화면의 재료 — 파일의 문서·사실·요약과 바뀐 원본 자막. 아무것도 쓰지 않는다."""
    folder = _folder_by_id(root, timeline_id)
    record = _read_record(folder)
    document = _document(record, settings)
    names = _source_names(record)
    return {
        "timelineId": folder.name,
        "revision": int(record["revision"]),
        "savedAt": record.get("saved_at"),
        "facts": record["facts"],
        "doc": timeline_edit.to_payload(document),
        "seed": record.get("seed", {}),
        "summary": _summary(document),
        "changedSources": changed_transcripts(service, root, document, names),
        "timelines": [path.name for path in timeline_folders(root)],
        "settings": client_settings(settings),
    }


KOREAN_SRT_PREFIX = "korean-r"
"""타임라인 폴더 안의 리비전 한국어 SRT(`korean-r<리비전>.srt`, W-99 · D174) — 영어로 옮길 재료."""


def _is_korean_srt(name: str) -> bool:
    number = name.removeprefix(KOREAN_SRT_PREFIX).removesuffix(".srt")
    return name == f"{KOREAN_SRT_PREFIX}{number}.srt" and number.isdigit()


def korean_srt(root: Path, timeline_id: str, revision: int) -> Path:
    """저장한 리비전의 자막을 그 타임라인 폴더의 SRT로(W-99, D174) — 「영어 만들기」가 옮긴다.

    화면이 본 리비전이 지금 파일과 다르면 거부한다. 바이트는 `timeline_edit.srt_text` 그대로라
    그 리비전을 렌더한 `draft.srt`와 같고, 번역 열쇠(큐 글)도 같다 — 공유가 무료로 쓴다. 같은
    리비전이면 같은 바이트라 이미 있으면 그대로 둔다."""
    name, current, document = read_document(root, timeline_id)
    if current != revision:
        raise TimelineEditError(
            f"화면의 리비전 {revision}이 지금 저장본({current})과 다릅니다 — 다시 열어 주세요."
        )
    folder = _folder_by_id(root, name)
    path = folder / f"{KOREAN_SRT_PREFIX}{revision}.srt"
    if not path.is_file():
        _write_atomic(folder, path.name, timeline_edit.srt_text(document).encode("utf-8"))
    return path


def korean_srts(root: Path, timeline_id: str) -> list[Path]:
    """그 타임라인 폴더의 리비전 한국어 SRT — 새 리비전부터(영어 열이 찾는다)."""
    folder = _folder_by_id(root, timeline_id)
    found = []
    for path in folder.glob(f"{KOREAN_SRT_PREFIX}*.srt"):
        number = path.stem.removeprefix(KOREAN_SRT_PREFIX)
        if number.isdigit():
            found.append((int(number), path))
    return [path for _number, path in sorted(found, reverse=True)]


def read_document(
    root: Path, timeline_id: str | None = None, settings: FinalEditSettings = FINAL_EDIT_SETTINGS
) -> tuple[str, int, TimelineDocument]:
    """(타임라인 아이디, 리비전, 문서) — 파일의 사실로 검사한 문서. 렌더(W-71e)가 읽는다. 읽기만."""
    folder = _folder_by_id(root, timeline_id)
    record = _read_record(folder)
    return folder.name, int(record["revision"]), _document(record, settings)


def clipped_sources(root: Path, timeline_id: str | None = None) -> tuple[str, tuple[str, ...]]:
    """(타임라인 아이디, 클립이 있는 원본 — 클립 차례) — 재생 미디어(W-71c)를 고를 재료. 읽기만."""
    folder = _folder_by_id(root, timeline_id)
    record = _read_record(folder)
    payload = record.get("document")
    clips = payload.get("clips") if isinstance(payload, dict) else None
    if not isinstance(clips, list):
        raise TimelineEditError("타임라인 파일에 클립(`document.clips`)이 없다")
    ordered: dict[str, None] = {}
    for clip in clips:
        ordered.setdefault(str(clip[0]), None)
    return folder.name, tuple(ordered)


# ─── 저장 ────────────────────────────────────────────────────────────────


def save_timeline(
    root: Path,
    timeline_id: str,
    base_revision: int,
    payload: Mapping[str, Any],
    *,
    force: bool = False,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """문서 전체를 파일의 사실로 다시 검사하고 같은 폴더의 `timeline.json`을 새 리비전으로 쓴다.

    화면이 본 리비전이 지금과 다르면 쓰지 않고 `conflict`를 돌려준다(`force`면 덮는다). 심은
    리비전은 파일의 것만 — 다르면 거부한다.
    """
    folder = _folder_by_id(root, timeline_id)
    with _SAVE_LOCK:
        record = _read_record(folder)
        document = _checked_payload(record, payload, settings)
        current = int(record["revision"])
        if current != base_revision and not force:
            return {"conflict": True, "revision": current}
        saved = _write_revision(folder, record, document, now)
    warnings = [[w.cue_id, w.kind] for w in timeline_edit.warnings(document, settings)]
    return {"revision": current + 1, "summary": saved["summary"], "warnings": warnings}


def add_source_to_timeline(
    service: RawWorkspaceService,
    root: Path,
    timeline_id: str,
    base_revision: int,
    payload: Mapping[str, Any],
    source_id: str,
    *,
    at: int | None = None,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """클립이 없는 원본을 원본 전체 클립으로 넣고 새 리비전으로 쓴다(W-71g) — 저장과 같은 검사.
    `at`(클립 차례)이 있으면 촬영 차례 자리 대신 그 자리다(끌어 놓기, W-111 · D185).

    한 번도 심지 않은 원본이면 지금 원본 자막을 심는다(그래서 화면 편집이 아니라 서버 쓰기다 —
    `seeded`는 저장으로 바꿀 수 없다). 화면이 본 리비전이 지금과 다르면 쓰지 않고 `conflict`다.
    """
    folder = _folder_by_id(root, timeline_id)
    with _SAVE_LOCK:
        record = _read_record(folder)
        document = _checked_payload(record, payload, settings)
        current = int(record["revision"])
        if current != base_revision:
            return {"conflict": True, "revision": current}
        transcript = _added_transcript(service, root, document, source_id)
        added = timeline_edit.add_source(document, source_id, transcript, settings, at=at)
        saved = _write_revision(folder, record, added.document, now)
    return {
        "revision": current + 1,
        "summary": saved["summary"],
        "doc": timeline_edit.to_payload(added.document),
        "cues": len(added.document.cues) - len(document.cues),
        "empty": added.empty,
        "clamped": added.clamped,
    }


def _added_transcript(
    service: RawWorkspaceService, root: Path, document: TimelineDocument, source_id: str
) -> tuple[str, tuple[render_plan.SourceCue, ...]]:
    """넣을 원본의 (리비전, 원본 자막) — 이미 심은 원본은 읽지 않는다(문서의 자막을 그대로 쓴다)."""
    if source_id in dict(document.seeded):
        return NO_TRANSCRIPT, ()
    assets = {asset.asset_id: asset for asset in service.list_source_assets(root)}
    if source_id not in assets:
        raise TimelineEditError(f"원본 {source_id}이(가) 작업공간에 없다")
    return _seed_transcript(service, root, assets[source_id])


def _checked_payload(
    record: Mapping[str, Any], payload: Mapping[str, Any], settings: FinalEditSettings
) -> TimelineDocument:
    """받은 문서를 파일의 사실로 검사한다 — 심은 리비전은 파일의 것만(다르면 거부)."""
    document = timeline_edit.from_payload(payload, _facts_from_record(record), settings)
    if dict(document.seeded) != dict(_document(record, settings).seeded):
        raise TimelineEditError("심은 원본 자막 리비전(`seeded`)은 저장으로 바꿀 수 없다")
    return document


def _write_revision(
    folder: Path,
    record: Mapping[str, Any],
    document: TimelineDocument,
    now: Callable[[], datetime],
) -> dict[str, Any]:
    saved = _record(
        document,
        revision=int(record["revision"]) + 1,
        created=_parse_moment(record.get("created_at")),
        saved=now(),
        facts=record["facts"],
        seed=record.get("seed", {}),
    )
    _write_record(folder, saved)
    return saved


def _parse_moment(value: object) -> datetime:
    if not isinstance(value, str):
        raise TimelineEditError("타임라인 파일의 만든 시각(`created_at`)이 없다")
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)


# ─── 쓰기 ────────────────────────────────────────────────────────────────


def _reserve_folder(base: Path, moment: datetime) -> Path:
    """새 시각 폴더 하나를 잡는다 — 있으면 덮지 않고 다음 번호(`_2`, `_3`…)."""
    stamp = moment.astimezone(UTC).strftime(_STAMP)
    attempt = 1
    while True:
        candidate = base / (stamp if attempt == 1 else f"{stamp}_{attempt}")
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            attempt += 1
            continue
        return candidate


def _write_record(folder: Path, record: Mapping[str, Any]) -> None:
    text = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
    _write_atomic(folder, TIMELINE_NAME, text.encode("utf-8"))


def _write_atomic(folder: Path, name: str, payload: bytes) -> None:
    """`timeline/<시각>/` 안의 정해진 이름만 — 임시 파일에 다 쓰고 이름을 바꾼다."""
    allowed = name in TIMELINE_FILES or _is_korean_srt(name)
    if not allowed or folder.parent.name != TIMELINE_FOLDER:
        raise TimelineEditError(f"타임라인이 쓸 수 없는 자리다: {folder.parent.name}/{name}")
    temporary = folder / f"{name}.tmp"
    descriptor = os.open(temporary, _WRITE_FLAGS, _FILE_MODE)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    oscompat.replace(temporary, folder / name)
