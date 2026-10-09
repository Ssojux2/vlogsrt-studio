#!/usr/bin/env python3
"""실제 작업공간의 원본 자막을 **읽기만 하며** 자막 시각을 잰다(D55) — 쓰기는 `--scratch`뿐.

    python scripts/verify_sync_workspace.py <작업공간> --scratch <빈 폴더> [--json]

작업공간 DB는 `file:...?mode=ro` URI로만 열고, 저장 PCM·1단계 기록은 읽기만 한다. 원본은
ffmpeg가 표준출력으로 디코드하거나(표본 대조) 스크래치로 디코드한다(1단계 기록이 없을 때).
`RawWorkspaceService`/`RawWorkspace`를 거치지 않는다 — 그 둘은 열기만 해도 DB를 쓴다.
판정은 제품 코드(`sync_report.measure_source`) 그대로다. 유료 호출 0.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from vlogsrt.project.raw import sync_report  # noqa: E402


def _connect(workspace: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"file:{workspace / 'project.sqlite3'}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _latest_transcripts(connection: sqlite3.Connection) -> dict[str, dict]:
    latest: dict[str, tuple[int, dict]] = {}
    rows = connection.execute("SELECT asset_id, sequence, payload FROM source_transcripts")
    for row in rows:
        if row["asset_id"] not in latest or row["sequence"] > latest[row["asset_id"]][0]:
            latest[row["asset_id"]] = (row["sequence"], json.loads(row["payload"]))
    return {asset_id: payload for asset_id, (_, payload) in latest.items()}


def _references(connection: sqlite3.Connection) -> dict[str, str]:
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='source_references'"
    ).fetchone()
    if exists is None:
        return {}
    return dict(connection.execute("SELECT asset_id, absolute_path FROM source_references"))


def _sources(workspace: Path) -> list[sync_report.SyncSource]:
    connection = _connect(workspace)
    try:
        transcripts = _latest_transcripts(connection)
        references = _references(connection)
        assets = connection.execute("SELECT * FROM source_assets ORDER BY source_name").fetchall()
    finally:
        connection.close()
    sources: list[sync_report.SyncSource] = []
    for asset in assets:
        transcript = transcripts.get(asset["asset_id"])
        if transcript is None:
            continue
        original = Path(references.get(asset["asset_id"], workspace / asset["relative_path"]))
        sha = asset["content_sha256"] or ""
        sources.append(
            sync_report.SyncSource(
                source_id=asset["asset_id"],
                source_name=asset["source_name"],
                source_sha256=sha,
                original=original if original.is_file() else None,
                original_note="" if original.is_file() else f"원본 없음: {original}",
                transcript_revision=str(transcript["revision"]),
                cues=sync_report.cues_of(transcript),
                stage1=sync_report.stage1_audio(workspace, asset["asset_id"], sha),
            )
        )
    return sources


def _share(value: str | None) -> str:
    if value is None:
        return "  - "
    numerator, denominator = (int(part) for part in value.split("/"))
    return f"{numerator / denominator:4.2f}"


def _row(document: dict) -> str:
    metrics = document.get("metrics") or {}
    offset = metrics.get("offset") or {}
    seconds = offset.get("seconds")
    shift = "  -  " if seconds is None else f"{_share(seconds).strip():>5}"
    pcm = document.get("pcm") or {}
    return (
        f"{document['source']['source_name'][-10:-6]}  {document['verdict'].upper():5}"
        f"  cues {metrics.get('cue_count', len(document.get('cues', []))):>3}"
        f"  P {_share(metrics.get('precision'))}  R {_share(metrics.get('recall'))}"
        f"  early {_share(metrics.get('early_share'))}"
        f"  nospeech {_share(metrics.get('no_speech_share'))}"
        f"  offset {shift} {offset.get('status', '-'):<14}"
        f"  pcm {pcm.get('basis', '-'):<7}  {' / '.join(r['text'] for r in document['reasons'])}"
    )


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--scratch", type=Path, required=True, help="쓰기가 허용된 유일한 폴더")
    parser.add_argument("--json", action="store_true", help="보고서 문서를 그대로 인쇄")
    args = parser.parse_args(argv)
    workspace = args.workspace.expanduser().resolve()
    scratch = args.scratch.expanduser().resolve()
    if scratch == workspace or workspace in scratch.parents or scratch in workspace.parents:
        parser.error("--scratch는 작업공간 밖이어야 한다")
    scratch.mkdir(parents=True, exist_ok=True)
    documents = []
    for source in _sources(workspace):
        document = sync_report.measure_source(source, cache_root=scratch)
        sync_report.write_report(scratch, document, sync_report.SYNC_CHECK_SETTINGS)
        documents.append(document)
        print(json.dumps(document, ensure_ascii=False) if args.json else _row(document))
    counts: dict[str, int] = {}
    for document in documents:
        counts[document["verdict"]] = counts.get(document["verdict"], 0) + 1
    print(" · ".join(f"{level} {count}" for level, count in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
