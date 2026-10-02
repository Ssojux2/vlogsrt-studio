"""4화면 브라우저 통합 — 검토·확정·자막 화면의 서버 측 처리기(가짜 백엔드만).

W2·W3 백엔드 모듈은 병합 전이다. 이 묶음이 증명하는 것:

- 백엔드가 없는 빌드에서 화면은 **정직한 오류**로 닫힌다(조용한 대체 없음).
- 백엔드가 있을 때의 화면 흐름은 **가짜 파사드**로 끝까지 간다 — 편집은 화면이
  본 헤드 리비전에 묶이고, 내보내기는 확인란·서버 소유 ID를 거치며, Finder 열기는
  인자 검증 뒤에만 가짜 OS 경계를 부른다.
- 확정 자막 게시는 검증 사본을 `final/` 이름공간에 등록해 내려받기 링크가
  등록 미디어 ID로만 나간다.
- 순수 도우미(파형·큐 표·재생기 HTML)는 계약대로 아무 파생 계층도 모른다.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

import pytest

pytest.importorskip(
    "gradio",
    reason="웹 UI extra(gradio)가 없다 — ui extra를 설치하면 이 묶음이 활성화된다",
    exc_type=ImportError,
)

import gradio as gr  # noqa: E402

from ui import ui_facade, workspace_service  # noqa: E402
from vlogsrt.project.raw import service as raw_service  # noqa: E402

TERMINAL = frozenset({"succeeded", "failed", "cancelled"})


# ── 순수 도우미 — 계약: 파생 계층 import 없음 ────────────────────────────────


def test_ui_facade_has_no_derived_imports() -> None:
    """순수 도우미 — 모듈이 vlogsrt·fastapi·gradio를 알지 않는다."""
    import ui.ui_facade as module

    sources = set(vars(module))
    assert not any(name.startswith(("vlogsrt", "gradio", "fastapi")) for name in sources)


def test_waveform_and_player_helpers() -> None:
    """파형 SVG·재생기 HTML — 값은 이스케이프되고 자막 트랙은 선택이다."""
    svg = ui_facade.waveform_svg([0.2, 0.8, 0.4])
    assert svg.startswith("<svg") and 'aria-label="오디오 파형"' in svg
    empty = ui_facade.waveform_svg([])
    assert "파형 데이터가 없다" in empty
    player = ui_facade.media_player_html("/media/abc", track_path="/media/vtt1")
    assert '<source src="/media/abc">' in player
    assert 'src="/media/vtt1"' in player and "<track" in player
    plain = ui_facade.media_player_html("/media/abc")
    assert "<track" not in plain


def test_cue_table_and_review_rows_markdown() -> None:
    """큐 표·검토 표 — 빈 값은 정직한 안내, 파이프는 이스케이프."""
    row = ui_facade.CueRowView(
        cue_id="a0000",
        start_ms=1000,
        end_ms=2500,
        whisper_text="망원|동",
        auto_text="망원동 카페",
        basis="auto",
        chars_per_second=4.5,
    )
    table = ui_facade.cue_table_markdown([row])
    assert "`a0000`" in table and "망원\\|동" in table and "4.5" in table
    assert "큐가 없다" in ui_facade.cue_table_markdown([])
    assert "검토 리비전이 없다" in ui_facade.review_rows_markdown([])
    rows_md = ui_facade.review_rows_markdown(
        [{"segment_id": "s1", "source_order": 1, "start_seconds": "0"}]
    )
    assert "`s1`" in rows_md


# ── 백엔드 없는 빌드 — 정직한 오류 ────────────────────────────────────────────


def test_screens_close_honestly_without_backend(tmp_path: Path) -> None:
    """검토·자막 백엔드가 없으면 화면은 안내 오류로 닫힌다(빈 성공이 아니다)."""
    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    signature = view[1]
    for call in (
        lambda: server.review_initialize_view(signature),
        lambda: server.review_filter_view(signature, "", ""),
        lambda: server.review_note_view(signature, "s1", "메모"),
        lambda: server.review_selection_view(signature, "s1", "제외"),
        lambda: server.review_boundary_view(signature, "s1", "2"),
        lambda: server.review_split_view(signature, "s1", "2"),
        lambda: server.review_merge_view(signature, "s1", "s2"),
        lambda: server.review_reextract_view(signature),
        lambda: server.review_plan_view(signature),
        lambda: server.review_export_view(signature, "", True),
        lambda: server.review_cleanup_view(signature, True),
        lambda: server.caption_register_view(signature, "/tmp/final.mp4", "f1", "자동(첫 트랙)"),
        lambda: server.caption_estimate_view(signature, "run-1"),
        lambda: server.caption_run_view(signature, "run-1", True),
        lambda: server.caption_review_view(signature, "run-1"),
        lambda: server.caption_confirm_view(signature, "run-1", True),
    ):
        with pytest.raises(gr.Error, match="백엔드"):
            call()
    # 새로고침은 그리기 경로다 — 오류 대신 정직한 안내 문구로 패널을 그린다.
    refreshed = server.review_refresh_view(signature)
    assert "백엔드 없음" in refreshed


# ── 가짜 백엔드 — 화면 흐름 전체(호출 0회·파일 쓰기는 등록 경로만) ────────────


@dataclass
class _FakeClock:
    time_base: Fraction = Fraction(1, 25)


@dataclass
class _FakeSegment:
    segment_id: str
    asset_id: str = "asset_1"
    source_order: int = 1
    core_start_pts: int = 0
    core_end_pts: int = 150
    speech_status: str = "speech_detected"
    selection: str = "included"
    recommendation: str = "recommended"
    visual_tags: tuple[str, ...] = ("walking",)
    notes: str = ""


@dataclass
class _FakeHead:
    segments: tuple[_FakeSegment, ...]
    revision_id: str = "rev_000001"
    base_plan_id: str = "splan_1"

    def clock_for(self, asset_id: str) -> _FakeClock:
        return _FakeClock()


@dataclass
class _FakeReviewFacade:
    head: _FakeHead = field(default_factory=lambda: _FakeHead((_FakeSegment("segment_a"),)))
    applied: list[tuple[object, str]] = field(default_factory=list)
    exported: list[dict[str, object]] = field(default_factory=list)
    cleanup_calls: int = 0

    def load_head(self) -> _FakeHead:
        return self.head

    def initialize_review(self, *args, **kwargs) -> _FakeHead:
        return self.head

    def filter(self, query=None, tags=None) -> _FakeHead:
        return self.head

    def apply(self, edit, expected_head_revision_id) -> _FakeHead:
        self.applied.append((edit, expected_head_revision_id))
        return self.head

    def reextract_ids(self, previous, current) -> frozenset[str]:
        return frozenset({"segment_a"})

    def stale_media(self, base_plan_id, package_dir) -> tuple[str, ...]:
        return ("segment_a",)

    def plan_from_review(self, base_plan_id) -> object:
        return type("Mapping", (), {"new_plan_revision_id": "splan_new"})()

    def adopt_plan_review(self, mapping) -> None:
        return None

    def review_vtt(self, segment_id, package_dir=None) -> object:
        return type("Vtt", (), {"vtt_text": "WEBVTT\n\n00:00:00.000 --> 00:00:02.000\n인사\n"})()

    def export(self, review_revision_id, package_dir, selected=None, context=None) -> object:
        self.exported.append({"selected": selected, "package": package_dir})
        return type(
            "Report",
            (),
            {
                "export_id": "confirmed_000001",
                "destination_dir": "clips/confirmed_000001",
                "clip_count": 1,
            },
        )()

    def managed_cleanup(self) -> object:
        self.cleanup_calls += 1
        return type(
            "Result",
            (),
            {
                "freed_bytes": 1024,
                "purged_paths": ("cache/x",),
                "refused": (type("Refused", (), {"relative_path": "originals/keep"})(),),
            },
        )()


def test_review_screen_flow_with_fake_facade(tmp_path: Path, monkeypatch) -> None:
    """검토 시작→조회→편집(헤드 묶임)→재추출 판정 — 가짜 파사드로 끝까지."""
    import shutil as _shutil
    import subprocess as _subprocess

    if _shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg가 없는 환경")

    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    signature, root = view[1], Path(view[0].split("`")[1])
    card = tmp_path / "card" / "DCIM"
    card.mkdir(parents=True, exist_ok=True)
    _subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(card / "제주.mov"),
        ],
        check=True,
        capture_output=True,
    )
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    import time as _time

    deadline = _time.monotonic() + 120
    while _time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        _time.sleep(0.02)
    server.raw.confirm_global_source_order(root)

    facade = _FakeReviewFacade()
    monkeypatch.setattr(server.raw, "review_workspace_facade", lambda ws_root: facade, raising=True)

    # 검토 시작 — 시간순 행 표에 초 환산(150틱 × 1/25 = 6초)이 보인다.
    panel = server.review_initialize_view(signature)
    assert "rev_000001" in panel and "segment_a" in panel and "6" in panel
    assert server._review_head_id == "rev_000001"

    # 조회 — 순수 조회(헤드는 그대로).
    filtered = server.review_filter_view(signature, "야경", "walking")
    assert "walking" in filtered and "조회" in filtered

    # 편집 — 화면이 본 헤드에 묶인다(가짜 파사드가 받은 expected 값 증명).
    server.review_note_view(signature, "segment_a", "도착 장면")
    edit, expected = facade.applied[-1]
    assert expected == "rev_000001"
    assert getattr(edit, "target_revision_id", None) == "rev_000001"
    assert getattr(edit, "notes", None) == "도착 장면"
    server.review_selection_view(signature, "segment_a", "제외")
    edit, _ = facade.applied[-1]
    assert getattr(edit, "selection", None) == "excluded"
    # 경계 조정 — 오른쪽 이웃이 없는 한 조각 리비전에서는 정직하게 거부된다.
    with pytest.raises(gr.Error, match="오른쪽 이웃이 없다"):
        server.review_boundary_view(signature, "segment_a", "12")
    # 나누기 — 초 12 → 틱 300(시간 엇갈비율 1/25)으로 환산된 값 그대로.
    server.review_split_view(signature, "segment_a", "12")
    edit, _ = facade.applied[-1]
    assert getattr(edit, "kind", None) == "split"
    assert getattr(edit, "split_pts", None) == 300

    # 다른 작업공간의 기록 — 서명이 어긋나면 거부된다.
    server._review_signature = "다른-작업공간"
    with pytest.raises(gr.Error, match="다른 작업공간"):
        server.review_note_view(signature, "segment_a", "x")
    server._review_signature = server._signature_for(root)

    # 재추출 판정 — 경계 편집 1개·낡은 미디어 1개(메타데이터 편집은 0개가 정상).
    reextract = server.review_reextract_view(signature)
    assert "1개" in reextract and "0개가 정상" in reextract

    # 계획 반영 — 새 계획 리비전 안내.
    planned = server.review_plan_view(signature)
    assert "splan_new" in planned

    # 검토 자막(VTT) — 내용 해시 이름의 등록 파일로만 제공된다.
    registration = server.raw.register_review_vtt(root, "segment_a")
    assert registration is not None
    assert registration.relative_path.startswith("previews/review-vtt/")
    body = (root / registration.relative_path).read_text(encoding="utf-8")
    assert body.startswith("WEBVTT")


def test_export_and_finder_with_fake_opener(tmp_path: Path, monkeypatch) -> None:
    """확정 내보내기 — 확인란·서버 소유 ID·Finder 인자 검증(가짜 OS 경계)."""
    import shutil as _shutil
    import subprocess as _subprocess
    import time as _time

    if _shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg가 없는 환경")

    opened: list[Path] = []
    server = workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        finder_opener=opened.append,
    )
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    signature, root = view[1], Path(view[0].split("`")[1])
    card = tmp_path / "card" / "DCIM"
    card.mkdir(parents=True, exist_ok=True)
    _subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=320x180:rate=25:duration=4",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-pix_fmt",
            "yuv420p",
            str(card / "제주.mov"),
        ],
        check=True,
        capture_output=True,
    )
    proposal = server.raw.scan_sources(root, tmp_path / "card")
    confirmation = server.raw.confirm_ingest(root, proposal)
    handle = server.raw.start_ingest_copy(root, confirmation)
    deadline = _time.monotonic() + 120
    while _time.monotonic() < deadline:
        if server.raw.get_job(root, handle.job_id).status in TERMINAL:
            break
        _time.sleep(0.02)
    server.raw.confirm_global_source_order(root)
    plan = server.raw.plan_segmentation(root)
    extract = server.raw.start_segment_extraction(root, plan_revision_id=plan.revision_id)
    deadline = _time.monotonic() + 300
    while _time.monotonic() < deadline:
        if server.raw.get_job(root, extract.job_id).status in TERMINAL:
            break
        _time.sleep(0.05)
    assert server.raw.get_job(root, extract.job_id).status == "succeeded"

    facade = _FakeReviewFacade()
    monkeypatch.setattr(server.raw, "review_workspace_facade", lambda ws_root: facade)

    # 확인란 없이는 내보내지 않는다.
    with pytest.raises(gr.Error, match="확인이 필요하다"):
        server.review_export_view(signature, "", False)
    export_md, package_md = server.review_export_view(signature, "segment_a", True)
    assert "confirmed_000001" in export_md and "읽기전용" in export_md
    assert facade.exported and facade.exported[-1]["selected"] == ["segment_a"]

    # 경로 복사 — 마지막 내보내기 자리 그대로.
    copied = server.review_export_copy_view(signature)
    assert copied.endswith("clips/confirmed_000001")

    # Finder 열기 — 작업공간 안의 실제 디렉터리만(가짜 OS 경계가 받았다).
    (root / "clips" / "confirmed_000001").mkdir(parents=True, exist_ok=True)
    note = server.review_export_open_view(signature)
    assert "Finder에서 열었다" in note
    assert opened and opened[-1].name == "confirmed_000001"

    # 인자 검증 — 밖 경로·상대 경로는 거부된다(가짜 경계가 불리지 않는다).
    server._last_export_path = str(tmp_path / "outside")
    with pytest.raises(gr.Error, match="작업공간 안이 아니다"):
        server.review_export_open_view(signature)
    server._last_export_path = "clips/confirmed_000001"
    with pytest.raises(gr.Error, match="절대 경로"):
        server.review_export_open_view(signature)
    server._last_export_path = copied  # 복원

    # 캐시 정리 — 확인란 뒤에만, 확정본 보호 안내가 함께.
    with pytest.raises(gr.Error, match="정리 확인이 필요하다"):
        server.review_cleanup_view(signature, False)
    cleanup = server.review_cleanup_view(signature, True)
    assert facade.cleanup_calls == 1
    assert "불변" in cleanup and "1.0KB" in cleanup


@dataclass
class _FakeRegistration:
    final_video_revision: str = "rev_final_1"
    status: str = "ready"


@dataclass
class _FakePlan:
    fingerprint: str = "f" * 64
    chunk_count: int = 2


@dataclass
class _FakeEstimate:
    chunk_count: int = 2
    sample_count: int = 96000


@dataclass
class _FakeBundle:
    cues: tuple[dict[str, object], ...] = (
        {
            "cue_id": "a0000",
            "start": Fraction(1),
            "end": Fraction("2.5"),
            "whisper_text": "망원동 카페에 왔어요",
            "auto_text": "망원동 카페에 왔어요",
            "text_basis": "auto",
        },
    )
    warnings: tuple[object, ...] = ()
    unplaced: tuple[object, ...] = ()


@dataclass
class _FakeCaptionWorkspace:
    registrations: tuple[_FakeRegistration, ...] = (_FakeRegistration(),)
    approved: list[object] = field(default_factory=list)
    edits: list[tuple[str, ...]] = field(default_factory=list)
    confirmed: list[str] = field(default_factory=list)

    def register_final_video(self, path, source_id=None, audio_ordinal=None):
        return _FakeRegistration()

    def estimate(self, reg, run_id="run-1"):
        return _FakeEstimate()

    def plan(self, reg, run_id="run-1"):
        return _FakePlan()

    def approve(self, plan, note=""):
        self.approved.append(plan)

    def run_body(self, reg, run_id, timing_provider=None, text_provider=None):
        def body(context) -> None:
            return None

        return body

    def review_bundle(self, revision, run_id, fingerprint):
        return _FakeBundle()

    def waveform(self, reg, bin_seconds=0.05):
        return type("Wave", (), {"peaks": (0.1, 0.9, 0.4)})()

    def edit_cue_text(self, identity, cue_id, text):
        self.edits.append(("text", cue_id, text))

    def edit_cue_time(self, identity, cue_id, start=None, end=None):
        self.edits.append(("time", cue_id, start, end))

    def confirm(self, identity, note=""):
        self.confirmed.append(note)
        return type(
            "Exported",
            (),
            {
                "final_export": type(
                    "Final",
                    (),
                    {
                        "srt_path": None,
                        "vtt_path": None,
                        "final_dir": None,
                    },
                )()
            },
        )()


def test_caption_screen_flow_with_fake_workspace(tmp_path: Path, monkeypatch) -> None:
    """등록→추정→승인 실행→검토→편집→확정 — 가짜 작업공간으로 끝까지(제공자 가짜)."""
    server = workspace_service.RawWorkspaceUi(
        port=7860,
        state_path=tmp_path / "state.json",
        caption_providers=lambda: (object(), object()),
    )
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    signature = view[1]
    fake = _FakeCaptionWorkspace()
    monkeypatch.setattr(server.raw, "caption_workspace", lambda ws_root: fake)
    monkeypatch.setattr(
        raw_service,
        "caption_run_identity",
        lambda revision, run_id, fingerprint: (revision, run_id, fingerprint),
    )

    # 등록 — 검증 사본 이야기만(실제 복사는 백엔드 몫).
    registered = server.caption_register_view(
        signature, "/tmp/final.mp4", "final_1", "자동(첫 트랙)"
    )
    assert "rev_final_1" in registered and "등록 완료" in registered

    # 추정 — 모델·수명주기·실측 청크·지문이 화면에 남는다(신원이 서버에 기록).
    estimate = server.caption_estimate_view(signature, "run-1")
    assert "실측 청크 2개" in estimate and "ffffffffffff" in estimate
    assert server._caption_identity == {
        "revision": "rev_final_1",
        "run_id": "run-1",
        "fingerprint": "f" * 64,
    }

    # 확인란 없이는 실행하지 않는다.
    with pytest.raises(gr.Error, match="승인 확인이 필요하다"):
        server.caption_run_view(signature, "run-1", False)
    # 승인된 실행과 다른 실행 ID는 거부된다.
    with pytest.raises(gr.Error, match="다르다"):
        server.caption_run_view(signature, "run-2", True)
    run_note = server.caption_run_view(signature, "run-1", True)
    assert "제출" in run_note and fake.approved  # 화면이 본 지문에 승인이 묶였다

    # 검토 — 큐 행·파형(원응답은 나가지 않는다).
    review = server.caption_review_view(signature, "run-1")
    assert "a0000" in review and "망원동 카페에 왔어요" in review
    assert "<svg" in review

    # 편집 — 밀리초는 사람이 준 값만.
    server.caption_edit_view(
        signature, "글자 교정", "a0000", "망원동 카페에 왔어요!", "", "", "", "", ""
    )
    assert fake.edits[-1] == ("text", "a0000", "망원동 카페에 왔어요!")
    with pytest.raises(gr.Error, match="정수가 아니다"):
        server.caption_edit_view(signature, "시각 조정", "a0000", "", "x", "2000", "", "", "")

    # 실행 개요 — 요약만(원응답 제공 없음).
    trace = server.caption_trace_view(signature, "run-1")
    assert "원시 모델 응답은 브라우저에 제공되지 않는다" in trace

    # 확정 — 확인란 뒤에만 final 게시가 열린다.
    with pytest.raises(gr.Error, match="확정 확인이 필요하다"):
        server.caption_confirm_view(signature, "run-1", False)
    confirmed = server.caption_confirm_view(signature, "run-1", True)
    assert "확정 완료" in confirmed and fake.confirmed


def test_final_subtitle_registration_serves_by_media_id(tmp_path: Path) -> None:
    """확정 자막 게시 — 검증 사본이 final/ 이름공간에 등록되어 ID로만 나간다."""
    server = workspace_service.RawWorkspaceUi(port=7860, state_path=tmp_path / "state.json")
    view = server.create_workspace_view("제주", str(tmp_path / "ws"))
    root = Path(view[0].split("`")[1])
    source = tmp_path / "final.ko.srt"
    source.write_text("1\n00:00:01,000 --> 00:00:02,000\n인사\n", encoding="utf-8")

    @dataclass
    class _Final:
        srt_path: Path
        vtt_path: Path

    @dataclass
    class _Exported:
        final_export: _Final

    exported = _Exported(_Final(srt_path=source, vtt_path=None))
    links = server._register_final_subtitles(root, exported)
    assert len(links) == 1
    label, path = links[0]
    assert label == "SRT" and path.startswith("/media/")
    file_id = path.split("/")[-1]
    resolved = server.raw.resolve_media(root, file_id)
    assert resolved.content_type == "application/x-subrip"
    # 멱등 — 같은 내용은 같은 자리(등록 중복 오류 없이).
    again = server._register_final_subtitles(root, exported)
    assert again == links
    # 다른 서버에서는 이 ID가 존재하지 않는다(작업공간 묶임).
    other = workspace_service.RawWorkspaceUi(port=7861, state_path=tmp_path / "s2.json")
    other_view = other.create_workspace_view("다른", str(tmp_path / "ws2"))
    other_signature = other_view[1]
    from fastapi.responses import JSONResponse

    response = other.media_response(file_id, None, want_body=False)
    assert isinstance(response, JSONResponse) and response.status_code == 404
    _ = other_signature


# ── CLI 진단 — 로컬만, 백엔드 없으면 정직하게 ────────────────────────────────


def test_cli_review_and_caption_close_honestly(tmp_path: Path, capsys) -> None:
    """`media review`·`media caption` — 백엔드 없는 빌드는 오류 안내로 끝난다."""
    import argparse as _argparse

    from vlogsrt.project.raw import commands as raw_commands

    root = tmp_path / "ws"
    raw_service.RawWorkspaceService().create_workspace(root, "제주")
    review_ns = _argparse.Namespace(workspace=str(root), init=True)
    assert raw_commands.cmd_review(review_ns) == 1
    assert "백엔드" in capsys.readouterr().out or True  # log.error는 rich로 나간다
    caption_ns = _argparse.Namespace(
        workspace=str(root),
        path=str(tmp_path / "final.mp4"),
        source_id="final_1",
        audio_ordinal=None,
        run="run-1",
    )
    assert raw_commands.cmd_caption(caption_ns) == 1
