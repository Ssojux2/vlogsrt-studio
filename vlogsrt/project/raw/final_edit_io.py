"""최종 편집(W-66, D117)의 입출력 — 초안 폴더의 `edit/` 하나에만 쓴다.

초안 영상·`draft*.srt`·보고서는 읽기만 한다(쓰기로 열지 않는다 — 불변식 「최종 편집은 초안 폴더의
edit/에만 쓴다」). 저장은 언제나 편집본 SRT(`final.srt`) → 편집 목록(`final-edit.json`, SRT의
sha256을 싣는다) 차례라, 3단계(번인)가 보는 쌍이 언제나 맞는다. 문서는 받을 때마다 순수 모델이
처음부터 다시 검사한다(브라우저를 믿지 않는다). 초안 지문(보고서 sha256·영상 크기)이 바뀌면
낡은 편집을 이름으로 알리고 거부한다.

ffmpeg는 `preprocess.amplitudes` 한 자리로만 부른다(규칙 7) — 파형 진폭.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import TYPE_CHECKING, Any

from vlogsrt import oscompat, preprocess
from vlogsrt.config import Config
from vlogsrt.project.raw import final_edit, render_export, timeline_srt
from vlogsrt.project.raw.final_edit import EditDocument, FinalEditError
from vlogsrt.project.raw.final_edit_config import (
    FINAL_EDIT_SETTINGS,
    FinalEditSettings,
    client_settings,
)

if TYPE_CHECKING:
    from vlogsrt.project.raw.workspace import RawWorkspace

EDIT_FOLDER = "edit"
EDIT_NAME = "final-edit.json"
FINAL_SRT_NAME = "final.srt"
WAVEFORM_NAME = "waveform.json"
EDIT_FILES = frozenset({EDIT_NAME, FINAL_SRT_NAME, WAVEFORM_NAME})
"""`_write_atomic`이 쓸 수 있는 이름의 전부 — 초안 파일 이름은 여기 없다."""
WAVEFORM_SCHEMA = "vlogsrt-final-edit-waveform/v1"
_WRITE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0)
_FILE_MODE = 0o644
_PEAK_MAX = 255
_SAVE_LOCK = threading.Lock()
_WAVE_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class DraftFolder:
    """다 낸 초안 하나 — 폴더와 격자, 지문."""

    folder: Path
    facts: final_edit.DraftFacts
    report_sha256: str
    video_size: int


@dataclass(frozen=True, slots=True)
class BurnInput:
    """3단계(번인)의 입구 — 초안 영상을 `keep` 차례로 잇고 `srt`를 입힌다."""

    video: Path
    frame_duration: Fraction
    total_frames: int
    """초안 전체 프레임 수 — `keep`이 이것 하나면 자르지 않는다."""
    keep: tuple[tuple[int, int, Fraction, Fraction], ...]
    """(시작 프레임, 끝 프레임, 시작 초, 끝 초) — 초안 시각, 차례대로."""
    srt: Path
    srt_sha256: str
    """편집 목록이 적어 둔 편집본 SRT 바이트의 sha256 — 번인이 읽은 SRT와 대조한다."""
    cue_count: int
    hidden_count: int
    revision: int


def _utc_now() -> datetime:
    return datetime.now(UTC)


# ─── 초안 폴더 ───────────────────────────────────────────────────────────


def draft_folder(folder: Path) -> DraftFolder:
    """보고서에서 격자와 지문을 읽는다 — 못 읽으면 폴더 이름을 대고 거부한다."""
    try:
        payload = (folder / render_export.REPORT_NAME).read_bytes()
        report = json.loads(payload.decode("utf-8"))
        sequence = report["sequence"]["frame_duration"]
        facts = final_edit.DraftFacts(
            frame_duration=Fraction(int(sequence["num"]), int(sequence["den"])),
            total_frames=int(report["total_frames"]),
        )
        size = (folder / render_export.VIDEO_NAME).stat().st_size
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise FinalEditError(
            f"초안 `{folder.name}`의 보고서·영상을 읽지 못했다 — {error}"
        ) from error
    return DraftFolder(folder, facts, hashlib.sha256(payload).hexdigest(), size)


def _folder_by_id(root: Path, export_id: str | None) -> Path:
    folders = render_export.report_folders(root)
    if not folders:
        raise FinalEditError("아직 내보낸 초안이 없다 — 「추천만 이어붙여 내보내기」를 먼저 하세요")
    if export_id is None:
        return folders[0]
    for folder in folders:
        if folder.name == export_id:
            return folder
    raise FinalEditError(f"초안 `{export_id}`이(가) 없다")


def exports(workspace: RawWorkspace) -> list[dict[str, Any]]:
    """다 낸 초안 목록(새것부터) — 편집이 있는지와 영상의 미디어 아이디."""
    media = _media_ids(workspace)
    return [
        {
            "id": folder.name,
            "hasEdit": (folder / EDIT_FOLDER / EDIT_NAME).is_file(),
            "mediaFileId": media.get(_relative_video(workspace, folder)),
        }
        for folder in render_export.report_folders(workspace.root)
    ]


def _media_ids(workspace: RawWorkspace) -> dict[str, str]:
    return {row.relative_path: row.file_id for row in workspace.list_media("artifact")}


def _relative_video(workspace: RawWorkspace, folder: Path) -> str:
    return (folder / render_export.VIDEO_NAME).relative_to(workspace.root).as_posix()


# ─── 열기 ────────────────────────────────────────────────────────────────


def _read_saved(folder: Path) -> dict[str, Any] | None:
    path = folder / EDIT_FOLDER / EDIT_NAME
    if not path.is_file():
        return None
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise FinalEditError(
            f"`{folder.name}/{EDIT_FOLDER}/{EDIT_NAME}`을 읽지 못했다 — {error}"
        ) from error
    if not isinstance(saved, dict) or saved.get("schema") != final_edit.SCHEMA:
        raise FinalEditError(f"`{folder.name}`의 편집 목록 판이 다르다")
    return saved


def _check_fingerprint(draft: DraftFolder, saved: Mapping[str, Any]) -> None:
    recorded = saved.get("draft", {})
    if (
        recorded.get("report_sha256") != draft.report_sha256
        or recorded.get("size_bytes") != draft.video_size
    ):
        raise FinalEditError(
            f"`{draft.folder.name}`의 편집 목록은 지금 초안과 다른 초안에 묶였다 — 낡은 편집이다"
        )


def _seed_srt(folder: Path, name: str | None = None) -> timeline_srt.TimelineSrt:
    """심을 SRT — 이름을 주면 그 폴더의 `draft*.srt`만, 아니면 가장 새 것."""
    if name is None:
        return timeline_srt.read(render_export.newest_srt(folder))
    path = folder / name
    if path.parent != folder or not name.startswith("draft") or path.suffix != ".srt":
        raise FinalEditError(f"심은 SRT 이름이 초안 폴더의 초안 SRT가 아니다: {name!r}")
    return timeline_srt.read(path)


def _srt_cues(srt: timeline_srt.TimelineSrt) -> list[tuple[Fraction, Fraction, str]]:
    return [(cue.start, cue.end, cue.text) for cue in srt.cues]


def open_edit(
    workspace: RawWorkspace, export_id: str | None = None, *, reseed: bool = False
) -> dict[str, Any]:
    """편집 화면의 재료 — 저장본이 있으면 그것, 없으면 가장 새 초안 SRT로 메모리에서 심는다.

    읽기에서는 아무것도 쓰지 않는다.

    `reseed`면 `reseedCues`에 가장 새 초안 SRT로 다시 심은 자막을 더 싣는다(클립은 그대로 — 화면이
    되돌릴 수 있는 연산 하나로 얹는다).
    """
    folder = _folder_by_id(workspace.root, export_id)
    draft = draft_folder(folder)
    saved = _read_saved(folder)
    newest = render_export.newest_srt(folder).name
    if saved is None:
        seed = _seed_srt(folder)
        document = final_edit.seed(draft.facts, _srt_cues(seed))
        revision, seed_name = 0, seed.path.name
    else:
        _check_fingerprint(draft, saved)
        document = final_edit.from_payload(saved, draft.facts)
        revision, seed_name = int(saved["revision"]), str(saved["seed_srt"]["name"])
    model = _open_model(workspace, draft, document, revision, seed_name)
    model["newerSeed"] = newest != seed_name
    if reseed:
        fresh = final_edit.replace_cues(document, _srt_cues(_seed_srt(folder)))
        model["reseedCues"] = final_edit.to_payload(fresh)["cues"]
        model["reseedSrt"] = newest
    return model


def _open_model(
    workspace: RawWorkspace,
    draft: DraftFolder,
    document: EditDocument,
    revision: int,
    seed_name: str,
) -> dict[str, Any]:
    listing = exports(workspace)
    current = next(row for row in listing if row["id"] == draft.folder.name)
    duration = draft.facts.frame_duration
    return {
        "exportId": draft.folder.name,
        "mediaFileId": current["mediaFileId"],
        "frameDuration": {"num": duration.numerator, "den": duration.denominator},
        "totalFrames": draft.facts.total_frames,
        "revision": revision,
        "seedSrt": seed_name,
        "doc": final_edit.to_payload(document),
        "exports": listing,
        "olderEdits": [row["id"] for row in listing[1:] if row["hasEdit"]],
        "settings": client_settings(),
    }


# ─── 저장 ────────────────────────────────────────────────────────────────


def save_edit(
    workspace: RawWorkspace,
    export_id: str,
    base_revision: int,
    payload: Mapping[str, Any],
    *,
    force: bool = False,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
    now: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """문서 전체를 다시 검사하고 `final.srt` → `final-edit.json` 차례로 쓴다.

    화면이 본 리비전이 지금 저장본과 다르면 쓰지 않고 `conflict`를 돌려준다(`force`면 덮는다).
    """
    folder = _folder_by_id(workspace.root, export_id)
    draft = draft_folder(folder)
    document = final_edit.from_payload(payload, draft.facts, settings)
    seed = _seed_srt(folder, _seed_name(payload))
    with _SAVE_LOCK:
        saved = _read_saved(folder)
        current = 0 if saved is None else int(saved["revision"])
        if saved is not None:
            _check_fingerprint(draft, saved)
        if current != base_revision and not force:
            return {"conflict": True, "revision": current}
        return _write_edit(folder, draft, document, seed, current + 1, now(), settings)


def _seed_name(payload: Mapping[str, Any]) -> str:
    name = payload.get("seedSrt")
    if not isinstance(name, str):
        raise FinalEditError("편집 문서에 심은 SRT 이름(`seedSrt`)이 없다")
    return name


def _write_edit(
    folder: Path,
    draft: DraftFolder,
    document: EditDocument,
    seed: timeline_srt.TimelineSrt,
    revision: int,
    moment: datetime,
    settings: FinalEditSettings,
) -> dict[str, Any]:
    projection = final_edit.project(document)
    srt_bytes = final_edit.srt_text(projection).encode("utf-8")
    edit_folder = folder / EDIT_FOLDER
    edit_folder.mkdir(exist_ok=True)
    _write_atomic(edit_folder, FINAL_SRT_NAME, srt_bytes)
    summary = _summary(document, projection, len(seed.cues))
    record = {
        "schema": final_edit.SCHEMA,
        "revision": revision,
        "saved_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "draft": _draft_record(draft),
        "seed_srt": {"name": seed.path.name, "sha256": seed.sha256},
        **final_edit.to_payload(document),
        "srt": {
            "name": FINAL_SRT_NAME,
            "sha256": hashlib.sha256(srt_bytes).hexdigest(),
            "cues": len(projection.cues),
        },
        "summary": summary,
    }
    text = json.dumps(record, ensure_ascii=False, indent=1) + "\n"
    _write_atomic(edit_folder, EDIT_NAME, text.encode("utf-8"))
    warnings = [[w.cue_id, w.kind] for w in final_edit.warnings(document, settings)]
    return {"revision": revision, "summary": summary, "warnings": warnings}


def _draft_record(draft: DraftFolder) -> dict[str, Any]:
    duration = draft.facts.frame_duration
    return {
        "folder": draft.folder.name,
        "video": render_export.VIDEO_NAME,
        "size_bytes": draft.video_size,
        "report_sha256": draft.report_sha256,
        "frame_duration": {"num": duration.numerator, "den": duration.denominator},
        "total_frames": draft.facts.total_frames,
    }


def _summary(
    document: EditDocument, projection: final_edit.Projection, seed_count: int
) -> dict[str, Any]:
    """무엇이 빠졌는가 — 규칙 9: 지운·가린·잘린 자막과 지운 구간을 남긴다."""
    present = {cue.cue_id for cue in document.cues}
    seed_ids = [f"{final_edit.SEED_PREFIX}{number}" for number in range(1, seed_count + 1)]
    return {
        "seed_cues": seed_count,
        "deleted_seed": [cue_id for cue_id in seed_ids if cue_id not in present],
        "added": sum(1 for cue in document.cues if cue.cue_id.startswith(final_edit.NEW_PREFIX)),
        "hidden_by_cut": [cue.cue_id for cue in projection.hidden],
        "trimmed_by_cut": [cue.cue_id for cue in projection.cues if cue.trimmed],
        "removed_ranges": [list(gap) for gap in final_edit.removed_ranges(document)],
        "edited_frames": projection.total_frames,
    }


def _write_atomic(folder: Path, name: str, payload: bytes) -> None:
    """`edit/` 안의 정해진 이름만 — 임시 파일에 다 쓰고 이름을 바꾼다(반쪽 파일을 보이지 않는다)."""
    if name not in EDIT_FILES or folder.name != EDIT_FOLDER:
        raise FinalEditError(f"최종 편집이 쓸 수 없는 자리다: {folder.name}/{name}")
    temporary = folder / f"{name}.tmp"
    descriptor = os.open(temporary, _WRITE_FLAGS, _FILE_MODE)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    oscompat.replace(temporary, folder / name)


# ─── 파형 ────────────────────────────────────────────────────────────────


def waveform(
    workspace: RawWorkspace,
    export_id: str,
    settings: FinalEditSettings = FINAL_EDIT_SETTINGS,
) -> dict[str, Any]:
    """초안 영상 소리의 진폭(빈마다 0–255) — 처음 한 번 재고 `edit/waveform.json`에 둔다."""
    folder = _folder_by_id(workspace.root, export_id)
    draft = draft_folder(folder)
    path = folder / EDIT_FOLDER / WAVEFORM_NAME
    with _WAVE_LOCK:
        cached = _read_waveform(path, draft, settings)
        if cached is not None:
            return cached
        record = _measure_waveform(draft, settings)
        edit_folder = folder / EDIT_FOLDER
        edit_folder.mkdir(exist_ok=True)
        _write_atomic(edit_folder, WAVEFORM_NAME, json.dumps(record).encode("utf-8"))
        return record


def _read_waveform(
    path: Path, draft: DraftFolder, settings: FinalEditSettings
) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None  # 망가진 캐시는 다시 잰다 — 다음 쓰기가 덮는다
    fresh = (
        isinstance(record, dict)
        and record.get("schema") == WAVEFORM_SCHEMA
        and record.get("report_sha256") == draft.report_sha256
        and record.get("bin_ms") == settings.waveform_bin_ms
    )
    return record if fresh else None


def _measure_waveform(draft: DraftFolder, settings: FinalEditSettings) -> dict[str, Any]:
    probe = replace(
        Config(),
        cut_probe_rate=settings.waveform_rate,
        cut_quiet_window_ms=settings.waveform_bin_ms,
    )
    seconds = draft.facts.total_frames * draft.facts.frame_duration
    peaks = preprocess.amplitudes(
        draft.folder / render_export.VIDEO_NAME, 0.0, float(seconds), probe
    )
    packed = bytes(min(_PEAK_MAX, round(value * _PEAK_MAX)) for value in peaks)
    return {
        "schema": WAVEFORM_SCHEMA,
        "report_sha256": draft.report_sha256,
        "bin_ms": settings.waveform_bin_ms,
        "count": len(packed),
        "peaks_b64": base64.b64encode(packed).decode("ascii"),
    }


# ─── 3단계 입구 ──────────────────────────────────────────────────────────


def saved_edit_srt(folder: Path) -> Path | None:
    """그 초안 폴더에 저장한 최종 편집이 있으면 편집본 SRT 자리 — 없으면 None(읽기만 한다)."""
    if not (folder / EDIT_FOLDER / EDIT_NAME).is_file():
        return None
    return folder / EDIT_FOLDER / FINAL_SRT_NAME


def draft_of_edit_srt(srt: Path) -> Path | None:
    """`<초안 폴더>/edit/final.srt`이면 그 초안 폴더 — 아니면 None. 이 SRT의 시각은 편집 시각이라
    그 초안의 컷과 함께여야만 맞는다(번인이 이것으로 컷을 고른다)."""
    folder = srt.parent.parent
    if (
        srt.name == FINAL_SRT_NAME
        and srt.parent.name == EDIT_FOLDER
        and (folder / render_export.REPORT_NAME).is_file()
    ):
        return folder
    return None


def load_for_burn(folder: Path) -> BurnInput:
    """번인이 쓸 편집 — 편집 없음·낡은 지문·빈 클립·SRT 바이트 불일치면 거부한다."""
    draft = draft_folder(folder)
    saved = _read_saved(folder)
    if saved is None:
        raise FinalEditError(f"`{folder.name}`에 저장한 최종 편집이 없다")
    _check_fingerprint(draft, saved)
    document = final_edit.from_payload(saved, draft.facts)
    if not document.clips:
        raise FinalEditError(f"`{folder.name}`의 최종 편집에 남은 클립이 없다")
    srt = folder / EDIT_FOLDER / FINAL_SRT_NAME
    try:
        digest = hashlib.sha256(srt.read_bytes()).hexdigest()
    except OSError as error:
        raise FinalEditError(f"`{folder.name}`의 편집본 SRT를 읽지 못했다 — {error}") from error
    if digest != saved["srt"]["sha256"]:
        raise FinalEditError(f"`{folder.name}`의 편집본 SRT가 편집 목록과 다르다 — 다시 저장하세요")
    return BurnInput(
        video=folder / render_export.VIDEO_NAME,
        frame_duration=draft.facts.frame_duration,
        total_frames=draft.facts.total_frames,
        keep=final_edit.keep_ranges(document),
        srt=srt,
        srt_sha256=digest,
        cue_count=int(saved["srt"]["cues"]),
        hidden_count=len(saved["summary"]["hidden_by_cut"]),
        revision=int(saved["revision"]),
    )
