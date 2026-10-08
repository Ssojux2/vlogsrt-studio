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
