"""D59 19c — 프로젝트 용어집: 한 줄에 하나, 리비전으로 쌓이고 마지막이 이긴다.

용어집은 두 번째 귀(gpt-transcribe)의 `keywords`로만 간다. 설정(`RawWorkflowConfig`)에 들어가지
않으므로 whisper 청크 열쇠는 용어집과 무관하다(규칙 6 · D47·D55의 교훈).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from vlogsrt.project.raw import glossary
from vlogsrt.project.raw import service as raw
from vlogsrt.project.raw.config import RawWorkflowConfig
from vlogsrt.project.raw.models import RawWorkspaceError
from vlogsrt.project.raw.understanding import SampleSpan, asr_chunk_key
from vlogsrt.project.raw.workspace import DB_NAME, RawWorkspace


def _workspace(tmp_path: Path) -> tuple[Path, RawWorkspace]:
    root = tmp_path / "ws"
    raw.RawWorkspaceService().create_workspace(root, "용어집")
    return root, RawWorkspace.open(root)


def _table_exists(root: Path) -> bool:
    with sqlite3.connect(root / DB_NAME) as db:
        found = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (glossary.TABLE,)
        ).fetchone()
    return found is not None


def test_parse_keeps_one_term_per_line_drops_notes_and_duplicates() -> None:
    text = "왕방\n# 메모 줄\n\n  백숙  \n왕방\n닭도리탕 # 음식\n"
    assert glossary.parse(text) == ("왕방", "백숙", "닭도리탕")
    assert glossary.parse("") == ()


@pytest.mark.parametrize("term", ["<왕방>", "왕방>", "a<b", "줄\n바꿈", "캐리지\r리턴", "탭\t"])
def test_terms_the_text_model_cannot_take_are_refused_whole(term: str) -> None:
    with pytest.raises(RawWorkspaceError, match="저장하지 않았습니다"):
        glossary.validated(["백숙", term])
    assert glossary.term_problems(["백숙"]) == []


def test_a_bad_line_saves_nothing(tmp_path: Path) -> None:
    root, workspace = _workspace(tmp_path)
    glossary.save(workspace, "왕방\n")
    with pytest.raises(RawWorkspaceError, match="쓸 수 없는 글자"):
        glossary.save(workspace, "백숙\n<닭>\n")
    assert glossary.current(workspace).terms == ("왕방",)


def test_revisions_append_and_the_latest_wins_without_touching_untouched_workspaces(
    tmp_path: Path,
) -> None:
    root, workspace = _workspace(tmp_path)
    assert glossary.current(workspace) == glossary.Glossary()
    assert glossary.save(workspace, "\n# 메모만\n") == glossary.Glossary()
    assert not _table_exists(root)  # 읽기·빈 저장은 표를 만들지 않는다
    first = glossary.save(workspace, "왕방\n백숙\n")
    assert (first.sequence, first.terms) == (0, ("왕방", "백숙"))
    assert glossary.save(workspace, "왕방\n백숙\n# 같은 말\n") == first  # 같으면 새 리비전이 없다
    second = glossary.save(workspace, "왕방\n닭도리탕\n")
    assert second.sequence == 1
    reopened = RawWorkspace.open(root)
    assert glossary.current(reopened) == second  # 앱을 다시 열어도 마지막이 이긴다
    cleared = glossary.save(reopened, "")
    assert (cleared.sequence, cleared.terms) == (2, ())
    with sqlite3.connect(root / DB_NAME) as db:
        rows = db.execute(f"SELECT sequence FROM {glossary.TABLE} ORDER BY sequence").fetchall()
    assert [row[0] for row in rows] == [0, 1, 2]  # 덧붙이기만 한다
    assert second.text() == "왕방\n닭도리탕"


def test_the_service_reads_and_saves_the_glossary(tmp_path: Path) -> None:
    root, _workspace_handle = _workspace(tmp_path)
    service = raw.RawWorkspaceService()
    assert service.glossary(root) == glossary.Glossary()
    saved = service.save_glossary(root, "왕방\n백숙\n")
    assert service.glossary(root) == saved and saved.terms == ("왕방", "백숙")
    with pytest.raises(RawWorkspaceError):
        service.save_glossary(root, "<왕방>")
    assert service.glossary(root) == saved


def test_the_glossary_sha_binds_terms_and_order() -> None:
    assert glossary.glossary_sha(("왕방", "백숙")) != glossary.glossary_sha(("백숙", "왕방"))
    assert glossary.glossary_sha(()) != glossary.glossary_sha(("왕방",))
    assert glossary.Glossary(0, ("왕방",)).sha == glossary.glossary_sha(("왕방",))


def test_the_glossary_never_enters_the_whisper_chunk_key(tmp_path: Path) -> None:
    """whisper 열쇠는 설정 전체를 해시한다 — 용어집은 거기 없으므로 저장해도 열쇠가 그대로다."""
    _root, workspace = _workspace(tmp_path)
    before = asr_chunk_key("w" * 64, "P", RawWorkflowConfig(), SampleSpan(0, 1))
    glossary.save(workspace, "왕방\n백숙\n")
    assert asr_chunk_key("w" * 64, "P", RawWorkflowConfig(), SampleSpan(0, 1)) == before
    assert RawWorkflowConfig().caption.keywords == ()
