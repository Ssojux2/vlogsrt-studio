from __future__ import annotations

import pytest

from tests.raw.test_browser_runtime import _make_media
from tests.raw.test_selection_analysis import make_server, wait_until
from vlogsrt.project.raw import source_transcript as captions
from vlogsrt.project.raw.models import RawWorkspaceError


def test_edit_split_merge_stale_revision_and_restart(tmp_path, monkeypatch):
    source = tmp_path / "sentence.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    workspace = server.raw._workspace(server.workspace_root())
    (asset_id,) = server._quick["asset_ids"]
    doc = captions.load(workspace, asset_id)
    cue = doc["utterances"][0]
    split = captions.edit(
        workspace,
        asset_id,
        doc["revision"],
        action="split",
        utterance_id=cue["utterance_id"],
        split_at="1",
        text="앞 문구",
        right_text="뒤 문구",
    )
    assert [u["text"] for u in split["utterances"]] == ["앞 문구", "뒤 문구"]
    with pytest.raises(RawWorkspaceError, match="바뀌었습니다"):
        captions.edit(
            workspace,
            asset_id,
            doc["revision"],
            action="edit",
            utterance_id=cue["utterance_id"],
            text="낡은 요청",
        )
    merged = captions.edit(
        workspace, asset_id, split["revision"], action="merge", utterance_id=cue["utterance_id"]
    )
    assert len(merged["utterances"]) == 1
    assert merged["utterances"][0]["text"] == "앞 문구 뒤 문구"
    assert captions.load(workspace, asset_id)["revision"] == merged["revision"]
    with pytest.raises(RawWorkspaceError, match="시각"):
        captions.edit(
            workspace,
            asset_id,
            merged["revision"],
            action="edit",
            utterance_id=cue["utterance_id"],
            start="-1",
        )


def test_caption_edit_reanalyzes_affected_window_only_and_preserves_cuts(tmp_path, monkeypatch):
    source = tmp_path / "sentence.mov"
    _make_media(source, audio=True, seconds="32")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    root = server.workspace_root()
    (asset_id,) = server._quick["asset_ids"]
    workspace = server.raw._workspace(root)
    doc = captions.load(workspace, asset_id)
    old_revision = server._quick["revision"]
    facade = server.raw.review_workspace_facade(root)
    before = facade.load_head().revision
    from vlogsrt.project.raw.segment_review import selection_edit

    edited = facade.apply_edit(
        before.base_plan_id,
        selection_edit(before.revision_id, before.segments[0].segment_id, "excluded"),
    )
    cue = doc["utterances"][0]
    response = server.visual_workbench_action(
        {
            "signature": server.current_signature(),
            "token": server._visual_lifetime_token,
            "sourceId": asset_id,
            "transcriptRevision": doc["revision"],
            "utteranceId": cue["utterance_id"],
            "action": "edit",
            "text": "수정된 완전한 문장입니다.",
            "start": "0.5",
            "end": "1.5",
        },
        "source-caption",
    )
    assert response["ok"], response
    assert server._quick["stage"] == "자막 변경 · 갱신 필요"
    server.capcut_retry_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert server._quick["revision"] != old_revision
    assert len(sender.calls) == 1
    assert len(client.calls) == 4
    after = facade.load_head().revision
    assert [(s.start_pts, s.end_pts, s.selection) for s in after.segments] == [
        (s.start_pts, s.end_pts, s.selection) for s in edited.segments
    ]
    _, html = server.visual_workbench_render(root)
    assert "subtitles" in html and "수정된 완전한 문장" in html


def _zero_length_doc(tmp_path):
    """길이 0 줄이 낀 원본 자막 — D55 버그 3의 수정이 심는 모양(28.44=28.44, 180/180)."""
    from fractions import Fraction

    from vlogsrt.project.raw import service as raw

    root = tmp_path / "ws"
    service = raw.RawWorkspaceService()
    service.create_workspace(root, "길이 영")
    workspace = service._workspace(root)

    def row(ident, start, end, text):
        return {
            "utterance_id": ident,
            "text": text,
            "start": captions.encoded(Fraction(start)),
            "end": captions.encoded(Fraction(end)),
        }

    rows = [
        row("u0", "1", "2", "앞 문장"),
        row("u1", "28.44", "28.44", "길이 영"),
        row("u2", "30", "32", "뒤 문장"),
    ]
    doc = captions.seed(workspace, "src", rows, Fraction(40), "a" * 64)
    return workspace, doc


def test_a_zero_length_row_does_not_lock_the_other_rows(tmp_path):
    """길이 0 줄이 있어도 다른 줄의 수정·분할·병합은 된다 — 손대지 않은 줄은 a ≤ b면 된다."""
    workspace, doc = _zero_length_doc(tmp_path)
    edited = captions.edit(
        workspace, "src", doc["revision"], action="edit", utterance_id="u2", text="고친 뒷말"
    )
    split = captions.edit(
        workspace,
        "src",
        edited["revision"],
        action="split",
        utterance_id="u0",
        split_at="1.5",
        text="앞",
        right_text="문장",
    )
    merged = captions.edit(
        workspace,
        "src",
        split["revision"],
        action="merge",
        utterance_id=split["utterances"][0]["utterance_id"],
    )
    assert [u["text"] for u in merged["utterances"]] == ["앞 문장", "길이 영", "고친 뒷말"]
    zero = next(u for u in merged["utterances"] if u["utterance_id"] == "u1")
    assert zero["start"] == zero["end"]  # 길이 0 줄은 그대로 남는다(규칙 9)


def test_editing_the_zero_length_row_must_give_it_a_length(tmp_path):
    """바꾸는 줄은 여전히 start < end여야 한다 — 그대로 길이 0이면 그 줄을 짚어 거절한다."""
    workspace, doc = _zero_length_doc(tmp_path)
    with pytest.raises(RawWorkspaceError, match=r"2번 자막\(28\.440~28\.440초\).*시각"):
        captions.edit(
            workspace, "src", doc["revision"], action="edit", utterance_id="u1", text="말"
        )
    fixed = captions.edit(
        workspace, "src", doc["revision"], action="edit", utterance_id="u1", end="29"
    )
    assert captions.rational(fixed["utterances"][1]["end"]) == 29
    with pytest.raises(RawWorkspaceError, match="겹칩니다"):
        captions.edit(
            workspace, "src", fixed["revision"], action="edit", utterance_id="u0", end="29"
        )
