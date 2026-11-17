"""「공유」(W-88, D147) — 렌더하고 고르면 한/영 자막을 입힌다. 영어는 사지 않는다(D114).

렌더(D130)·번인(D116)·번역 캐시(D114)는 각자의 시험이 실제로 잰다 — 여기서는 공유가 그 셋을 어떤
차례·조건으로 잇는지만 본다(가짜 렌더 결과 · 기록하는 가짜 번인). 유료 호출 0회.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt.project.raw import (
    burn_export,
    burn_in,
    srt_english,
    timeline_edit,
    timeline_edit_io,
    timeline_render,
    timeline_share,
)
from vlogsrt.project.raw.models import RawWorkspaceError

SRT = "1\n00:00:00,000 --> 00:00:01,000\n안녕하세요\n"


class _Calls:
    def __init__(self) -> None:
        self.prepared: list[tuple[Path, Path, Path | None]] = []
        self.burned: list[Any] = []
        self.written: list[Any] = []


_DOC = SimpleNamespace(english=(), cues=())
"""고친 영어가 없는 문서 — 글은 `srt_text` 대역이 정한다."""


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, _Calls]:
    folder = tmp_path / "export" / "20261116T000000Z"
    folder.mkdir(parents=True)
    video, srt = folder / "draft.mp4", folder / "draft.srt"
    video.write_bytes(b"mp4")
    srt.write_bytes(SRT.encode("utf-8"))  # 렌더처럼 LF 바이트 — Windows `write_text`는 CRLF다
    calls = _Calls()
    exported = SimpleNamespace(folder=folder, video=video, srt=srt)
    monkeypatch.setattr(timeline_render, "export_timeline", lambda *a, **k: exported)
    monkeypatch.setattr(timeline_edit_io, "read_document", lambda root, tid: (tid, 4, _DOC))
    monkeypatch.setattr(timeline_edit, "srt_text", lambda document: SRT)

    def prepare(v: Path, k: Path, e: Path | None) -> str:
        calls.prepared.append((v, k, e))
        return "job"

    def burn(service: Any, root: Path, job: Any, style: Any) -> dict[str, Any]:
        calls.burned.append((job, style))
        return {"path": str(folder / "draft.burned.mp4")}

    monkeypatch.setattr(burn_export, "prepare", prepare)
    monkeypatch.setattr(burn_export, "burn", burn)
    return tmp_path, calls


STYLE = burn_in.default_style()


def _share(root: Path, mode: str) -> dict[str, Any]:
    return timeline_share.share(SimpleNamespace(), root, "t1", 4, mode, STYLE)


def test_none_only_renders(world: tuple[Path, _Calls]) -> None:
    root, calls = world
    facts = _share(root, timeline_share.BURN_NONE)
    assert facts["burn"] == "none" and facts["burned"] is None and facts["english"] == "off"
    assert calls.prepared == [] and calls.burned == []


def test_korean_burns_the_rendered_video_and_srt_without_english(
    world: tuple[Path, _Calls],
) -> None:
    root, calls = world
    facts = _share(root, timeline_share.BURN_KOREAN)
    folder = root / "export" / "20261116T000000Z"
    assert calls.prepared == [(folder / "draft.mp4", folder / "draft.srt", None)]
    assert calls.burned == [("job", STYLE)]
    assert facts["english"] == "off" and facts["burned"]["path"].endswith("draft.burned.mp4")


def test_english_uses_a_matching_file_beside_the_srt(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = world
    english = root / "export" / "20261116T000000Z" / "draft.en.srt"
    monkeypatch.setattr(srt_english, "matching_english", lambda s, r, k: (english, ()))
    facts = _share(root, timeline_share.BURN_KOREAN_ENGLISH)
    assert calls.prepared[0][2] == english and facts["english"] == "used"


def test_english_writes_a_saved_translation_for_free(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    """같은 글을 전에 옮겼으면(저장된 번역) 부르지 않고 그 옆에 써서 입힌다."""
    root, calls = world
    written = root / "export" / "20261116T000000Z" / "draft.en.srt"
    monkeypatch.setattr(srt_english, "matching_english", lambda s, r, k: (None, ()))
    monkeypatch.setattr(srt_english, "prepare", lambda s, r, k: "srt-job")
    monkeypatch.setattr(srt_english, "cached", lambda s, r, j: {"english": {}})
    monkeypatch.setattr(srt_english, "write_english", lambda s, r, j, d: {"path": str(written)})
    facts = _share(root, timeline_share.BURN_KOREAN_ENGLISH)
    assert calls.prepared[0][2] == written and facts["english"] == "written"


def test_english_is_never_bought_and_a_missing_one_burns_korean_only(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = world
    monkeypatch.setattr(srt_english, "matching_english", lambda s, r, k: (None, ()))
    monkeypatch.setattr(srt_english, "prepare", lambda s, r, k: "srt-job")
    monkeypatch.setattr(srt_english, "cached", lambda s, r, j: None)

    def bought(*_a: Any) -> Any:
        raise AssertionError("공유는 번역을 사지 않는다(D114)")

    monkeypatch.setattr(srt_english, "convert", bought)
    monkeypatch.setattr(srt_english, "write_english", bought)
    facts = _share(root, timeline_share.BURN_KOREAN_ENGLISH)
    assert calls.prepared[0][2] is None and facts["english"] == "missing"
    assert calls.burned  # 한국어는 입힌다


def test_a_rendered_srt_that_is_not_the_revision_text_is_not_burned(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = world
    monkeypatch.setattr(timeline_edit, "srt_text", lambda document: SRT + "\n2\n")
    with pytest.raises(RawWorkspaceError, match="D118"):
        _share(root, timeline_share.BURN_KOREAN)
    assert calls.burned == []


def test_a_revision_that_moved_while_rendering_is_not_burned(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    root, calls = world
    monkeypatch.setattr(timeline_edit_io, "read_document", lambda root, tid: (tid, 5, "doc"))
    with pytest.raises(RawWorkspaceError, match="리비전 5"):
        _share(root, timeline_share.BURN_KOREAN)
    assert calls.burned == []


def test_unknown_modes_are_refused() -> None:
    with pytest.raises(RawWorkspaceError, match="번인 캡션"):
        timeline_share.require_mode("fr")


# ─── 사람이 고친 영어(D166) ───────────────────────────────────────────────


TWO = "1\n00:00:00,000 --> 00:00:01,000\n안녕\n\n2\n00:00:01,000 --> 00:00:02,000\n잘 가\n"


def _service() -> Any:
    """`srt_english.prepare`가 묻는 용어집과 작업공간 — 번역 열쇠만 짓는다(부르지 않는다)."""
    return SimpleNamespace(
        glossary=lambda root: SimpleNamespace(sha="g"),
        _workspace=lambda root: SimpleNamespace(root=root),
    )


def _with_edits(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch, edits: dict, saved: dict | None
) -> tuple[dict[str, Any], Path]:
    root, _calls = world
    srt = root / "export" / "20261116T000000Z" / "draft.srt"
    srt.write_bytes(TWO.encode("utf-8"))
    monkeypatch.setattr(timeline_edit, "srt_text", lambda document: TWO)
    monkeypatch.setattr(timeline_share, "edited_by_number", lambda document: edits)
    monkeypatch.setattr(srt_english, "cached", lambda s, r, j: saved)
    monkeypatch.setattr(srt_english, "convert", lambda *a: pytest.fail("사지 않는다(D114)"))
    facts = timeline_share.share(
        _service(), root, "t1", 4, timeline_share.BURN_KOREAN_ENGLISH, STYLE
    )
    return facts, srt.with_name("draft.en.srt")


def test_edited_english_alone_is_burned_when_it_covers_every_cue(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, english = _with_edits(world, monkeypatch, {"1": "Hi", "2": "Bye"}, None)
    assert facts["english"] == timeline_share.ENGLISH_EDITED
    assert world[1].prepared[0][2] == english
    assert english.read_text(encoding="utf-8").split("\n")[2::4] == ["Hi", "Bye"]


def test_edited_english_overrides_only_its_cue_of_the_saved_translation(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = {"english": {"1": "Hello", "2": "Goodbye"}}
    facts, english = _with_edits(world, monkeypatch, {"2": "See you"}, saved)
    assert facts["english"] == timeline_share.ENGLISH_EDITED
    assert english.read_text(encoding="utf-8").split("\n")[2::4] == ["Hello", "See you"]


def test_partial_edits_without_a_translation_burn_korean_only(
    world: tuple[Path, _Calls], monkeypatch: pytest.MonkeyPatch
) -> None:
    facts, english = _with_edits(world, monkeypatch, {"1": "Hi"}, None)
    assert facts["english"] == timeline_share.ENGLISH_MISSING and not english.exists()
    assert world[1].prepared[0][2] is None and world[1].burned


def test_edits_are_numbered_by_the_rendered_srt_and_stale_or_hidden_ones_drop() -> None:
    """번호는 렌더한 SRT 차례다 — 낡은 것(한국어가 바뀜)과 컷에 가려진 자막의 것은 쓰지 않는다."""
    import json

    from tests.raw.test_timeline_edit import case_facts
    from tests.raw.test_timeline_edit_js import CASES

    base = next(c for c in json.loads(CASES.read_text("utf-8")) if c["name"] == "cue set text")
    payload = dict(base["doc"], english={"c1": ["옛 글", "old"], "c2": ["가려짐", "hidden"],
                                        "c3": ["둘", "two"]})  # fmt: skip
    document = timeline_edit.from_payload(payload, case_facts(base["facts"]))
    rendered = [cue.utterance_id for cue in timeline_edit.render_cues(document)[0]]
    assert "c2" not in rendered  # 가려졌다
    assert timeline_share.edited_by_number(document) == {str(rendered.index("c3") + 1): "two"}
