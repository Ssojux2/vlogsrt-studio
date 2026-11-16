from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.raw.test_browser_runtime import _CountingSender, _make_media
from ui.workspace_service import RawWorkspaceUi
from vlogsrt.config import Config
from vlogsrt.project.raw.analysis_approval import LiveAnalysisTransport
from vlogsrt.project.raw.config import OPENAI_LUNA_MODEL, TIMING_ENGINE_GEMINI
from vlogsrt.project.raw.segment_review import unreviewed_auto_exclusions
from vlogsrt.project.raw.service import RawWorkspaceError

# D104 — whisper 응답 모양의 재생으로 재는 시험이다. 새로 듣는 엔진을 whisper로 둔다.
pytestmark = pytest.mark.usefixtures("whisper_for_new_hearings")


class SuitabilityClient:
    def __init__(self, pause_second: bool = False):
        self.calls = []
        self.second_entered = threading.Event()
        self.resume = threading.Event()
        self.pause_second = pause_second
        self.responses = SimpleNamespace(create=self._create)

    def _create(self, **payload):
        self.calls.append(payload)
        text = "\n".join(
            c["text"] for m in payload["input"] for c in m["content"] if c["type"] == "input_text"
        )
        window = re.search(r"window_id: (\S+)", text).group(1)
        frames = re.search(r"프레임 증거\(순서대로\): ([^\n]+)", text).group(1).split(", ")
        index = int(window.rsplit("_", 1)[1])
        if index == 1 and self.pause_second:
            self.second_entered.set()
            assert self.resume.wait(15), "test did not release second request"
        body = {
            "window_id": window,
            "speech_summary": "합성 설명",
            "visual_summary": "합성 풍경",
            "scene_tags": ["scenery"],
            "evidence_ids": frames[:1],
            "suggested_boundary_ids": [],
            "editing_notes": [],
            "needs_review": index == 1,
            "uncertainties": [],
            "suitability": ["recommended", "review", "usable"][min(index, 2)],
            "suitability_reason": [
                "안정된 구도와 선명한 풍경",
                "움직임을 확인해 주세요",
                "연결 장면으로 사용 가능",
            ][min(index, 2)],
        }
        return {"output_text": json.dumps(body, ensure_ascii=False)}


def wait_until(check, timeout=45):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = check()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition did not settle")


def keep_luna(server: RawWorkspaceUi) -> None:
    """W-18(D58) 이전에 만든 작업공간처럼 — 의미 분석 모델로 gpt-6-luna를 저장해 둔다.

    새 작업공간의 기본은 Claude Sonnet 5.5다. OpenAI 가짜 클라이언트로 Luna 흐름을 보는 시험은
    Luna를 저장한 기존 작업공간에서 출발한다(그 작업공간은 조용히 Sonnet으로 옮겨 가지 않는다).
    """
    root = server.workspace_root()
    workflow = server.raw.settings(root)
    models = replace(workflow.models, luna_model=OPENAI_LUNA_MODEL)
    server.raw.update_settings(root, replace(workflow, models=models))


def keep_whisper_only(server: RawWorkspaceUi) -> None:
    """실제(가짜) 호출 흐름 시험 — 처음 듣는 영상을 Gemini 시간 엔진으로 듣는 작업공간(D163).

    whisper는 은퇴해 실제 1단계는 Gemini 문으로만 듣는다. 이 파일의 재생 시험은 whisper 모양
    선언을 쓰므로(`whisper_for_new_hearings`) 실제 호출 흐름의 작업공간만 Gemini로 둔다.
    """
    server.raw.set_new_hearing_engine(server.workspace_root(), TIMING_ENGINE_GEMINI)


def make_server(
    tmp_path: Path,
    monkeypatch,
    source: Path,
    client=None,
    parallel_analysis_limit=None,
    gemini: bool = True,
):
    monkeypatch.setattr("vlogsrt.env.is_offline", lambda: False)
    sender = _CountingSender([{"start": 0.5, "end": 1.5, "text": "합성 영상입니다."}])
    client = client or SuitabilityClient()
    server = RawWorkspaceUi(
        state_path=tmp_path / "ui-state.json",
        path_chooser=lambda kind: str(source),
        live=LiveAnalysisTransport(
            sender=sender,
            cfg=Config(),
            client=client,
            asr_approvals=lambda *a: {},
            luna_approvals=lambda *a: {},
            timing_client=sender.client(),
        ),
        parallel_analysis_limit=parallel_analysis_limit,
    )
    server.create_workspace_view("선택 자동 분석", str(tmp_path / "workspace"))
    keep_luna(server)
    if gemini:
        keep_whisper_only(server)
    else:  # 옛 whisper 작업공간(교차 확인을 끈 19d 이전) — 이미 들은 작업공간의 바이트 시험
        from vlogsrt.project.raw import cross_check

        workspace = server.raw._workspace(server.workspace_root())
        workspace.set_meta_value(cross_check.META_KEY, cross_check.SETTING_OFF)
    server._selection_auto_approval = lambda quote: True
    return server, sender, client


def test_selection_runs_both_providers_and_publishes_progress_and_recommendations(
    tmp_path, monkeypatch
):
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="32")
    client = SuitabilityClient(pause_second=True)
    server, sender, client = make_server(tmp_path, monkeypatch, source, client)
    server.capcut_choose_view(server.current_signature())
    try:
        assert client.second_entered.wait(35), server._quick
        state = server._quick
        root = server.workspace_root()
        progress = server.raw.selection_suitability(root, state["revision"], state["asset_ids"])
        ranges = progress[state["asset_ids"][0]]
        # 3단 등급(D51·D52) — Luna가 도는 동안은 자막 칸이 아직 없어 빨강이 없고(자막 전),
        # 판정 전 창은 보류다. 발화 끝(1.5초)+여유 0.3초까지가 발화 구역이다. 그 1.8초 녹색은
        # 3초보다 짧아 추천하지 않는다(D60 — 표의 칸은 그대로 `both-good`, 등급만 보통).
        assert len(ranges) == 3
        assert ranges[0]["end"] == {"num": 9, "den": 5}
        assert (ranges[0]["tier"], ranges[0]["recommendation"]) == ("yellow", "normal")
        assert (ranges[0]["rule"], ranges[0]["caps"]) == (
            "both-good",
            [{"code": "too_short", "text": "3초보다 짧아 추천하지 않습니다"}],
        )
        assert ranges[1]["end"] == {"num": 15, "den": 1}
        assert (ranges[1]["tier"], ranges[1]["audio"]["status"]) == ("yellow", "subtitle_pending")
        assert ranges[2]["recommendation"] == "review_required"
        assert (ranges[2]["tier"], ranges[2]["hold"]) == ("yellow", True)
        assert ranges[2]["video"]["status"] == "unanalyzed"
        assert all(r["tier"] != "red" for r in ranges)
    finally:
        client.resume.set()
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 1
    assert len(client.calls) == 3
    assert all(call["model"] == OPENAI_LUNA_MODEL for call in client.calls)
    assert all(
        any(c["type"] == "input_image" for m in call["input"] for c in m["content"])
        for call in client.calls
    )
    head = server.raw.review_workspace_facade(root).load_head().revision
    # 3단 등급(D51·D52): Luna의 확인 메모가 더는 전부를 '검토 필요'로 끌어내리지 않는다.
    # 합성 소리 트랙은 4초뿐이라(`_make_media`) 4–32초는 자막 공백이 아니라 소리가 없는 자리다 —
    # 무음 후보가 아니므로 제외 제안이 없고, 원본 하나가 한 조각('보통')으로 남는다.
    assert [
        (s.recommendation, [t for t in s.visual_tags if t.startswith("등급: ")])
        for s in head.segments
    ] == [("normal", ["등급: 보통"])]
    assert all("등급판 raw-tier/v7" in s.visual_tags for s in head.segments)
    assert all(s.selection == "included" for s in head.segments)  # no scenic deletion
    assert unreviewed_auto_exclusions(head) == ()
    final = server.raw.selection_suitability(root, state["revision"], state["asset_ids"])
    statuses = {r["audio"]["status"] for r in final[state["asset_ids"][0]]}
    assert statuses == {"speech_sparse", "unconfirmed_empty"}
    # D79 — 추천 화면 × 발화 적음은 초록(`good-sparse`, v5는 노랑), 자막 판정 전 칸은 노랑 그대로다.
    assert [(r["tier"], r["rule"]) for r in final[state["asset_ids"][0]]] == [
        ("green", "good-sparse"),
        ("yellow", "speech-unconfirmed"),
        ("yellow", "speech-unconfirmed"),
    ]
    assert not list(
        (root / "clips").glob("package_*")
    )  # showing results doesn't wait for extraction
    _, html = server.visual_workbench_render(root)
    assert "suggestions" in html and "안정된 구도" in html
    # Same selected source reuses a valid cache without another approval/provider call.
    ids = state["asset_ids"]
    server._start_quick(root, (), asset_ids=ids)
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 1 and len(client.calls) == 3


def _make_media_with_full_audio(path: Path, seconds: int) -> None:
    """영상 길이만큼 소리가 있는 합성 원본 — 자막 없는 긴 공백이 소리 범위 안에 놓인다.

    색 좌표를 명시해야 Final Cut 출력이 받는다(모르는 색 좌표는 거부한다).
    """
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size=320x180:rate=25:duration={seconds}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={seconds}",
            "-c:a", "aac", "-b:a", "128k",
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


def test_analysis_completion_proposes_red_as_unreviewed_exclusion_and_export_waits(
    tmp_path, monkeypatch
):
    """분석 완료 경로(publish_review → initialize_review) 끝까지 — 빨강은 미검토 제외 제안이다.

    소리는 32초 내내 있고 발화는 0.5–1.5초뿐이라 1.8–32초가 자막 없는 긴 공백이다. Luna가 창 0은
    '추천'(풍경 → 노랑), 창 1은 '검토', 창 2는 '사용 가능'으로 본다 — 15–32초가 제외 제안이 되고,
    사람이 확인하기 전에는 Final Cut 출력이 열리지 않는다(D35·D52). 유료 호출 0회(가짜 제공자).
    """

    source = tmp_path / "long.mov"
    _make_media_with_full_audio(source, 32)
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert (len(sender.calls), len(client.calls)) == (1, 3)
    root = server.workspace_root()
    facade = server.raw.review_workspace_facade(root)
    head = facade.load_head().revision
    clock = head.clocks[0]

    def seconds(segment) -> tuple[Fraction, Fraction]:
        return tuple(
            (pts - clock.origin_pts) * clock.time_base
            for pts in (segment.start_pts, segment.end_pts)
        )

    pending = unreviewed_auto_exclusions(head)
    assert [seconds(s) for s in pending] == [(15, 30), (30, 32)]
    for segment in pending:
        assert segment.selection == "excluded" and "등급: 제외 제안" in segment.visual_tags
        provenance = json.loads(segment.notes.removeprefix("자동 제외: "))
        assert (provenance["reason"], provenance["version"]) == ("tier_silence", "raw-tier/v7")
        assert provenance["evidence"][:2] == ["tier:silent-not-scenery", "audio:subtitle_silence"]
        assert provenance["source_id"] == segment.source_id
        assert provenance["analysis_revision_id"] == server._quick["revision"]
    kept = [s for s in head.segments if s not in pending]
    assert [(seconds(s), s.selection) for s in kept] == [((0, 15), "included")]
    # 잘라 내는 확정 내보내기의 관문 — 사람이 보기 전에는 영상에서 아무것도 지우지 않는다(D35).
    # 관문은 패키지를 보기 전에 선다(이 흐름은 아직 조각을 뽑지 않았다).
    with pytest.raises(RawWorkspaceError, match="자동 제외 2개를 먼저 확인"):
        facade.export_confirmed("clips/package_never_built")
    # Final Cut 보내기는 지우지 않으므로 막지 않는다 — 미검토 빨강은 끈 채 키워드와 간다(D56·D80).
    opened: list[Path] = []
    server._finalcut_opener = opened.append  # 시험은 실제 Final Cut을 열지 않는다
    sent = _send(server, root)
    assert [_state(sent, seconds(s)[0]) for s in pending] == [
        ("0", ["제외 제안", "제외 제안(미검토)"])
    ] * 2
    assert _state(sent, seconds(kept[0])[0])[0] is None
    facade.confirm_automatic_exclusions()
    confirmed = _send(server, root)
    assert [_state(confirmed, seconds(s)[0])[0] for s in pending] == ["0", "0"]
    assert all("제외 확정" in _state(confirmed, seconds(s)[0])[1] for s in pending)
    assert _state(confirmed, seconds(kept[0])[0])[0] is None
    assert opened == [sent, confirmed]


def _send(server, root: Path) -> Path:
    from tests.raw.test_review_workspace import _wait_terminal

    before = {j.job_id for j in server.raw.list_jobs(root)}
    server.finalcut_send_view(server.current_signature())
    (job,) = [
        j
        for j in server.raw.list_jobs(root)
        if j.kind == "finalcut_send" and j.job_id not in before
    ]
    finished = _wait_terminal(server.raw, root, job.job_id)
    assert finished.status == "succeeded", finished.error
    return Path(server._finalcut_sent[str(server.current_signature())]["path"])


def _state(xml_path: Path, start: Fraction) -> tuple[str | None, list[str]]:
    """그 시각에 시작하는 스파인 클립의 (enabled, 키워드) — 시작 타임코드 없는 합성 원본 하나."""
    import xml.etree.ElementTree as ET

    spine = ET.parse(xml_path).getroot().find("./library/event/project/sequence/spine")
    assert spine is not None
    for clip in spine:
        if Fraction(clip.attrib["start"].removesuffix("s")) == start:
            return clip.get("enabled"), [k.get("value") for k in clip.findall("keyword")]
    raise AssertionError(f"{start}초에 시작하는 클립이 없다")


def test_unapproved_selection_prepares_preview_but_never_calls_providers(tmp_path, monkeypatch):
    source = tmp_path / "scene.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("승인 대기", "분석 확인 필요"))
    assert server._quick["stage"] == "승인 대기", server._quick
    assert not sender.calls and not client.calls
    assert "한 번 승인" in server._quick_status()


def test_one_approval_continues_without_a_second_phase_button(tmp_path, monkeypatch):
    source = tmp_path / "one.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server._selection_auto_approval = None
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "승인 대기")
    server.quick_approve_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert len(sender.calls) == 1 and len(client.calls) == 1


def test_initial_luna_request_ceiling_stops_semantic_calls(tmp_path, monkeypatch):
    from dataclasses import replace

    source = tmp_path / "bounded.mov"
    _make_media(source, audio=True, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    original = server.raw.analysis_semantic_estimate
    monkeypatch.setattr(
        server.raw,
        "analysis_semantic_estimate",
        lambda *args, **kwargs: tuple(
            replace(e, luna_request_count=e.luna_request_count + 1)
            for e in original(*args, **kwargs)
        ),
    )
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") == "분석 확인 필요")
    assert "상한" in server._quick["error"]
    assert len(sender.calls) == 1 and not client.calls


def test_new_source_keeps_approved_analysis_and_return_reuses_state(tmp_path, monkeypatch):
    from tests.raw.test_review_workspace import _wait_terminal

    source, other = tmp_path / "old.mov", tmp_path / "new.mov"
    _make_media(source, audio=True, seconds="32")
    _make_media(other, audio=True, seconds="3")
    client = SuitabilityClient(pause_second=True)
    server, sender, client = make_server(tmp_path, monkeypatch, source, client)
    root = server.workspace_root()
    for path in (source, other):
        handle = server.raw.import_selected_video(root, path)
        assert _wait_terminal(server.raw, root, handle.job_id).status == "succeeded"
    server.raw.ensure_source_order(root)
    assets = {a.source_name: a.asset_id for a in server.raw.list_source_assets(root)}
    server._start_quick(root, (), asset_ids=(assets[source.name],))
    assert client.second_entered.wait(35), server._quick
    old = server._quick
    server._selection_auto_approval = None
    server.quick_source_view(server.current_signature(), assets[other.name])
    try:
        wait_until(lambda: server._quick.get("stage") == "승인 대기")
        assert not server.raw.get_job(root, old["job_id"]).cancellation_requested
        assert server._quick["asset_ids"] == (assets[other.name],)
        assert "백그라운드 분석 1개" in server.capcut_status_view(server.current_signature())
    finally:
        client.resume.set()
    assert _wait_terminal(server.raw, root, old["job_id"]).status == "succeeded"
    assert old["stage"] == "추천 구간 준비 완료"
    assert server._quick["asset_ids"] == (assets[other.name],)
    assert server._quick["stage"] == "승인 대기" and server._quick["revision"] is None
    calls = (len(sender.calls), len(client.calls))
    server.quick_source_view(server.current_signature(), assets[source.name])
    assert server._quick is old and server._quick["stage"] == "추천 구간 준비 완료"
    assert (len(sender.calls), len(client.calls)) == calls == (1, 3)
    states = server.raw.load_selection_states(root)
    assert {a for task in states for a in task["asset_ids"]} == set(assets.values())


def test_scenery_without_audio_still_gets_luna_recommendation(tmp_path, monkeypatch):
    source = tmp_path / "scenery.mov"
    _make_media(source, audio=False, seconds="3")
    server, sender, client = make_server(tmp_path, monkeypatch, source)
    server.capcut_choose_view(server.current_signature())
    wait_until(lambda: server._quick.get("stage") in ("추천 구간 준비 완료", "분석 확인 필요"))
    assert server._quick["stage"] == "추천 구간 준비 완료", server._quick
    assert not sender.calls and len(client.calls) == 1
    head = server.raw.review_workspace_facade(server.workspace_root()).load_head().revision
    assert head.segments[0].recommendation == "recommended"
    assert head.segments[0].selection == "included"
