"""자막 입히기가 최종 편집을 입력으로 쓴다(W-67, D118) — 컷과 편집본 SRT를 한 번의 인코딩으로.

시험이 못 박는 것:

- 칸을 다 비우면 마지막 초안의 저장한 최종 편집이다 — 그 컷(`BurnCut`)과 `edit/final.srt`,
  길이는 편집 길이, 소리는 다시 인코딩, 결과는 초안 폴더의 `final.burned.mp4`.
- 편집본 SRT는 그 초안의 컷과만 간다 — 다른 영상을 고르면 거부한다(시각이 어긋난다). 이전 초안의
  편집본 SRT를 고르면 그 초안의 컷이다. 초안 SRT나 영상을 직접 고르면 예전처럼 자르지 않는다.
- 편집 목록이 적어 둔 SRT와 읽은 SRT가 다르면(그 사이 저장) 거부한다.
- 남은 클립이 초안 전체면 자르지 않는다(소리는 그대로 싣는다).
- 기본 영어는 지금 편집본 글을 옮긴 파일뿐이다 — 옛 영어 SRT는 빼고 그 이름을 알린다.
- 미리보기는 컷 없는 필터로 편집 시각에 맞는 초안 프레임을 그린다.

ffmpeg는 부르지 않는다 — `preprocess`의 경계 함수 자리에 가짜가 앉는다(실측은 D118).
"""

from __future__ import annotations

import json
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from tests.raw.test_burn_export import _fake_tools, _FakeBurn
from tests.raw.test_burn_export import _workspace as _burn_workspace
from tests.raw.test_final_edit_io import _plant, _save
from vlogsrt import preprocess
from vlogsrt.project.raw import burn_export, burn_in, final_edit_io, srt_english
from vlogsrt.project.raw import service as raw_service
from vlogsrt.project.raw.models import RawWorkspaceError

CUT_CLIPS = [[0, 90], [120, 300]]
"""3초–4초를 지운다 — c2(3–3.5초)가 가려지고 c3(6–7초)는 5–6초가 된다."""


def _draft_probe(audio: str | None = "aac") -> Any:
    video = SimpleNamespace(
        width=1920, height=1080, rotation_degrees=0, avg_frame_rate=Fraction(30),
        r_frame_rate=Fraction(30), color_transfer="bt709", codec_name="h264",
    )  # fmt: skip
    tracks = (SimpleNamespace(codec_name=audio),) if audio else ()
    return SimpleNamespace(video=video, duration=Fraction(10), audio=tracks)


@pytest.fixture
def probed(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    probes = [_draft_probe()]
    monkeypatch.setattr(preprocess, "probe_raw_media", lambda path: probes[0])
    return probes


def _edited(
    tmp_path: Path, clips: list[list[int]] | None = None, stamp: str = "20261112T010203Z"
) -> tuple[raw_service.RawWorkspaceService, Path, Path]:
    service, root = _burn_workspace(tmp_path)
    workspace = service._workspace(root)
    folder = _plant(workspace, stamp)
    model = final_edit_io.open_edit(workspace, folder.name)
    _save(workspace, model, {**model["doc"], "clips": clips or CUT_CLIPS}, 0)
    return service, root, folder


def test_empty_boxes_burn_the_saved_edit_with_its_cuts(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, folder = _edited(tmp_path)
    job = burn_export.plan(service, root, None, None, None)
    assert job.korean.path == folder / "edit" / "final.srt" and job.english is None
    assert job.edit is not None and job.edit.cut is not None
    assert job.edit.cut.ranges == ((0, 90), (120, 300))
    assert job.edit.cut.frame_duration == Fraction(1, 30)
    assert (job.duration, job.audio, job.edit.source_duration) == (9, "encode", 10)
    assert (job.edit.revision, job.edit.clips, job.edit.hidden) == (1, 2, 1)
    seen: list[dict[str, Any]] = []
    fake = _FakeBurn()

    def burn(request: Any, output: Path, target: Any, encoding: Any, **kw: Any) -> Any:
        seen.append({"cut": request.cut, **kw})
        return fake(request, output, target, encoding, **kw)

    _fake_tools(monkeypatch, fake)
    monkeypatch.setattr(preprocess, "burn_subtitles", burn)
    facts = burn_export.burn(service, root, job, burn_in.default_style())
    output = folder / "final.burned.mp4"
    assert facts["path"] == str(output)
    assert seen == [{"cut": job.edit.cut, "duration": 9, "audio": "encode", "timeout_basis": 10}]
    assert "Dialogue: 0,0:00:05.00,0:00:06.00,KO,,0,0,0,,뒤" in fake.ass  # 편집 시각
    report = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert report["final_edit"] == {
        "folder": folder.name, "revision": 1, "clips": 2, "hidden_cues": 1, "cut": True,
        "english_left_out": [],
    }  # fmt: skip
    assert facts["edit"] == {"revision": 1, "clips": 2, "hidden": 1, "english_left_out": []}
    assert burn_export.fingerprint(job).endswith("draft.mp4#edit-1")


def test_without_a_saved_edit_the_draft_is_burned_as_before(
    tmp_path: Path, probed: list[Any]
) -> None:
    service, root = _burn_workspace(tmp_path)
    folder = _plant(service._workspace(root))
    job = burn_export.plan(service, root, None, None, None)
    assert job.edit is None and job.korean.path == folder / "draft.srt"
    assert job.audio == "copy" and job.duration == 10
    assert burn_export.fingerprint(job).endswith("draft.mp4")


def test_the_edit_srt_goes_only_with_its_own_draft(tmp_path: Path, probed: list[Any]) -> None:
    service, root, older = _edited(tmp_path)
    newer = _plant(service._workspace(root), "20261112T020304Z")  # 편집 없는 새 초안
    edit_srt = older / "edit" / "final.srt"
    with pytest.raises(RawWorkspaceError, match="draft.mp4"):
        burn_export.plan(service, root, newer / "draft.mp4", edit_srt, None)
    picked = burn_export.plan(service, root, None, edit_srt, None)
    assert picked.edit is not None and picked.edit.folder == older
    assert picked.video == older / "draft.mp4"
    same = burn_export.plan(service, root, older / "draft.mp4", edit_srt, None)
    assert same.edit is not None and same.edit.cut == picked.edit.cut
    latest = burn_export.plan(service, root, None, None, None)
    assert latest.edit is None and latest.video == newer / "draft.mp4"


def test_choosing_the_draft_itself_burns_it_uncut(tmp_path: Path, probed: list[Any]) -> None:
    service, root, folder = _edited(tmp_path)
    by_srt = burn_export.plan(service, root, None, folder / "draft.srt", None)
    assert by_srt.edit is None and by_srt.video == folder / "draft.mp4"
    by_video = burn_export.plan(service, root, folder / "draft.mp4", None, None)
    assert by_video.edit is None and by_video.korean.path == folder / "draft.srt"


def test_a_save_between_reading_and_burning_is_refused(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, folder = _edited(tmp_path)
    real = final_edit_io.load_for_burn

    def racing(draft: Path) -> final_edit_io.BurnInput:
        loaded = real(draft)
        (draft / "edit" / "final.srt").write_text(
            "1\n00:00:01,000 --> 00:00:02,000\n고친 글\n", encoding="utf-8"
        )
        return loaded

    monkeypatch.setattr(final_edit_io, "load_for_burn", racing)
    with pytest.raises(RawWorkspaceError, match="다시"):
        burn_export.plan(service, root, None, None, None)


def test_an_edit_that_keeps_the_whole_draft_does_not_cut(tmp_path: Path, probed: list[Any]) -> None:
    service, root, _folder = _edited(tmp_path, clips=[[0, 300]])
    job = burn_export.plan(service, root, None, None, None)
    assert job.edit is not None and job.edit.cut is None
    assert job.audio == "copy" and job.duration == 10
    probed[0] = _draft_probe(audio=None)
    silent = burn_export.plan(service, root, None, None, None)
    assert silent.audio == "none"


def test_a_cut_draft_without_sound_stays_silent(tmp_path: Path, probed: list[Any]) -> None:
    service, root, _folder = _edited(tmp_path)
    probed[0] = _draft_probe(audio=None)
    job = burn_export.plan(service, root, None, None, None)
    assert job.edit is not None and job.edit.cut is not None and job.audio == "none"


def test_only_english_of_the_current_edit_text_is_burned(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, folder = _edited(tmp_path)
    english = folder / "edit" / "final.en.srt"
    english.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nFront\n\n2\n00:00:05,000 --> 00:00:06,000\nBack\n",
        encoding="utf-8",
    )
    stale = burn_export.plan(service, root, None, None, None)
    assert stale.english is None and stale.edit is not None
    assert stale.edit.english_left_out == (english,)
    monkeypatch.setattr(srt_english, "matching_english", lambda *args: (english, ()))
    fresh = burn_export.plan(service, root, None, None, None)
    assert fresh.english is not None and fresh.english.path == english
    assert fresh.edit is not None and fresh.edit.english_left_out == ()
    monkeypatch.setattr(srt_english, "matching_english", lambda *args: (None, (english,)))
    chosen = burn_export.plan(service, root, None, None, english)  # 사람이 고르면 그대로 쓴다
    assert chosen.english is not None and chosen.english.path == english


def test_the_preview_draws_the_draft_frame_of_the_edited_time(
    tmp_path: Path, probed: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    service, root, _folder = _edited(tmp_path)
    job = burn_export.plan(service, root, None, None, None)
    assert burn_export.source_seconds(job, Fraction(11, 2)) == Fraction(195, 30) + Fraction(1, 60)
    assert burn_export.source_seconds(job, Fraction(1)) == Fraction(1) + Fraction(1, 60)
    assert burn_export.source_seconds(job, Fraction(99)) == Fraction(299, 30) + Fraction(1, 60)
    seen: list[tuple[Any, Fraction]] = []

    def frame(request: Any, at: Fraction, **_: Any) -> bytes:
        seen.append((request.cut, at))
        return b"\xff\xd8"

    monkeypatch.setattr(preprocess, "subtitle_ffmpeg", lambda candidates: "/opt/ffmpeg-full")
    monkeypatch.setattr(preprocess, "burn_preview_frame", frame)
    assert burn_export.preview(job, burn_in.default_style()) == b"\xff\xd8"
    assert seen == [(None, Fraction(3, 2) + Fraction(1, 60))]  # 첫 큐 「앞」(1–2초)의 가운데


def test_the_default_korean_srt_is_the_saved_edit(tmp_path: Path) -> None:
    """D118 — 「영어 SRT로 변환」의 빈 칸도 마지막 초안의 편집본 SRT다(없으면 가장 새 SRT)."""
    service, root = _burn_workspace(tmp_path)
    assert service.default_korean_srt(root) is None
    workspace = service._workspace(root)
    folder = _plant(workspace)
    assert service.default_korean_srt(root) == folder / "draft.srt"
    model = final_edit_io.open_edit(workspace)
    _save(workspace, model, model["doc"], 0)
    assert service.default_korean_srt(root) == folder / "edit" / "final.srt"
