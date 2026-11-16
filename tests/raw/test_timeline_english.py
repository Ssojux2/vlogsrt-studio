"""영어 열(W-87d, D153) — 이 타임라인을 렌더한 초안의 저장된 번역을 한국어 글로 찾는다.

번역은 사지 않는다(요청 0회 — 저장된 번역 문서를 직접 깐다). 다른 타임라인의 초안 ·
번역이 없는 초안 · 못 읽는 보고서는 짝을 주지 않고, 새 초안의 짝이 이긴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.raw.test_srt_english import _workspace
from vlogsrt.project.raw import render_export, srt_english, timeline_english
from vlogsrt.project.raw import translation_io as tio
from vlogsrt.project.raw.analysis_store import ANALYSIS_ROOT


def _draft(root: Path, stamp: str, timeline_id: str | None, srt: str) -> Path:
    folder = root / render_export.EXPORT_FOLDER / stamp
    folder.mkdir(parents=True)
    report = {} if timeline_id is None else {"timeline": {"id": timeline_id, "revision": 1}}
    (folder / render_export.REPORT_NAME).write_text(json.dumps(report), encoding="utf-8")
    path = folder / render_export.SRT_NAME
    path.write_text(srt, encoding="utf-8")
    return path


def _translated(service, root: Path, srt: Path, english: dict[str, str]) -> None:
    job = srt_english.prepare(service, root, srt)
    document = tio.result_document(job.key, srt.name, "gemini-3.8-flash", english, [])
    tio.write(tio.result_path(service._workspace(root).root, ANALYSIS_ROOT, job.key), document)


OLD = (
    "1\n00:00:01,000 --> 00:00:02,000\n안녕하세요\n여러분\n\n"
    "2\n00:00:02,000 --> 00:00:03,000\n바다 보러 왔어요\n"
)
NEW = (
    "1\n00:00:01,000 --> 00:00:02,000\n안녕하세요   여러분\n\n"
    "2\n00:00:02,000 --> 00:00:03,000\n산\n"
)


def test_saved_translations_of_this_timeline_pair_korean_text_with_english(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root = _workspace(tmp_path, monkeypatch, online=False)
    ws = service._workspace(root).root
    old = _draft(ws, "20261116T000000Z", "t1", OLD)
    _translated(service, root, old, {"1": "Hello everyone", "2": "We came to see the sea"})
    new = _draft(ws, "20261116T010000Z", "t1", NEW)
    _translated(service, root, new, {"1": "Hi all", "2": "Mountain"})
    other = _draft(ws, "20261116T020000Z", "t2", "1\n00:00:01,000 --> 00:00:02,000\n남의 글\n")
    _translated(service, root, other, {"1": "Someone else"})
    _draft(ws, "20261116T030000Z", "t1", "1\n00:00:01,000 --> 00:00:02,000\n번역 없음\n")
    _draft(ws, "20261116T040000Z", None, "1\n00:00:01,000 --> 00:00:02,000\n보고서뿐\n")
    pairs = timeline_english.english_by_text(service, root, "t1")
    assert pairs == {
        "안녕하세요 여러분": "Hi all",  # 새 초안이 이긴다 — 줄바꿈·공백은 한 칸으로
        "산": "Mountain",
        "바다 보러 왔어요": "We came to see the sea",  # 고친 뒤에도 옛 글의 짝은 남는다
    }
    assert timeline_english.english_by_text(service, root, "nope") == {}


def test_the_lookup_never_writes_or_asks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, root = _workspace(tmp_path, monkeypatch, online=False)
    ws = service._workspace(root).root
    srt = _draft(ws, "20261116T000000Z", "t1", OLD)
    before = sorted(p.relative_to(ws) for p in ws.rglob("*") if p.is_file())
    monkeypatch.setattr(srt_english, "translate", lambda *a, **k: pytest.fail("사지 않는다"))
    assert timeline_english.english_by_text(service, root, "t1") == {}
    after = sorted(p.relative_to(ws) for p in ws.rglob("*") if p.is_file())
    assert after == before and srt.read_text(encoding="utf-8") == OLD
