"""프록시 없는 원본의 25% 분석 프록시 — 격자 보존·검증 실패 폴백·캐시 키 분리(2026-10-08).

합성 ffmpeg 미디어와 임시 작업공간만 쓴다. 실제 라이브러리·API는 건드리지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from dataclasses import asdict, fields
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_media_refs import transcode
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import log, preprocess
from vlogsrt.project.raw import analysis_workflow, media_refs, source_transcript, sync_report
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_PROXY,
    ANALYSIS_PROXY_LIMITS,
    PAIR_VIDEO_SAMPLE_SIZE,
    AnalysisProxyLimits,
    AnalysisProxyRecipe,
    RawUnderstandingSettings,
    validate_analysis_proxy_limits,
)
from vlogsrt.project.raw.analysis_store import AnalysisStore
from vlogsrt.project.raw.jobs import JobCancelledError
from vlogsrt.project.raw.models import AnalysisRevision, AnalysisSourceResult
from vlogsrt.project.raw.service import RawWorkspaceService
from vlogsrt.project.raw.understanding import VideoGrid, common_timeline, plan_common_windows

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe가 없는 환경",
)
pytestmark = needs_ffmpeg


@pytest.fixture(autouse=True)
def fresh_process(monkeypatch):
    """프로세스 안의 실패 기억은 테스트마다 비운다 — 테스트 하나가 앱 실행 하나다."""
    monkeypatch.setattr(media_refs, "_FAILED", {})


def _make_original(
    path: Path, *, size: str = "640x360", rate: str = "30000/1001", offset: str = "0.5"
) -> None:
    """B프레임이 있고 원점이 프레임 격자 밖(0.5초)인 29.97fps H.264 — 격자 보존의 어려운 쪽."""
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}:duration=4",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=4",
            "-c:v", "libx264", "-preset", "ultrafast", "-bf", "2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-output_ts_offset", offset,
            str(path),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip


def _import(tmp_path: Path, name: str, build) -> tuple[RawWorkspaceService, Path, object]:
    original = tmp_path / f"{name}.fcpbundle/Event/Original Media/clip.mov"
    build(original)
    (original.parent.parent / "CurrentVersion.fcpevent").touch()
    service = RawWorkspaceService()
    state, _ = service.managed_fcp_workspace(tmp_path / f"{name}.fcpbundle")
    root = Path(state.workspace_root)
    scan = service.scan_fcp_library(root, tmp_path / f"{name}.fcpbundle")
    job = service.start_reference_import(root, scan.proposal, fcp_scan=scan)
    assert _wait_terminal(service, root, job.job_id).status == "succeeded"
    (asset,) = service.list_source_assets(root)
    return service, root, asset


@pytest.fixture
def imported(tmp_path):
    service, root, asset = _import(tmp_path, "A", _make_original)
    return service._workspace(root), asset, service, root


def _clock(path: Path):
    return preprocess.probe_extraction_clock(path)


def _gray(path: Path, size: int = 16) -> bytes:
    done = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(path),
            "-vf", f"scale={size}:{size},format=gray", "-f", "rawvideo", "-",
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return done.stdout


def _mean_abs_diff(left: bytes, right: bytes) -> float:
    assert len(left) == len(right) > 0
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / len(left)


def _warnings(monkeypatch) -> list[str]:
    messages: list[str] = []
    monkeypatch.setattr(log, "warn", messages.append)
    return messages


def test_original_only_frames_resolve_to_quarter_scale_proxy_with_same_lattice(imported):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    proxy = media_refs.resolve(workspace, asset, "frames")
    assert proxy != original
    assert proxy.is_relative_to(workspace.root / "cache" / "analysis-proxy")
    source_probe, proxy_probe = (
        preprocess.probe_raw_media(original),
        preprocess.probe_raw_media(proxy),
    )
    assert (source_probe.video.width, source_probe.video.height) == (640, 360)
    assert (proxy_probe.video.width, proxy_probe.video.height) == (160, 90)
    assert proxy_probe.video.codec_name == "h264"
    assert proxy_probe.video.pix_fmt == "yuv420p"
    assert not proxy_probe.audio
    a, b = _clock(original), _clock(proxy)
    assert (b.frame_count, b.frame_duration) == (a.frame_count, a.frame_duration)
    assert a.frame_duration == Fraction(1001, 30000)
    assert b.presentation_origin == a.presentation_origin == Fraction(1, 2)
    assert b.uniform_grid


def test_proxy_edges_are_even_when_quarter_is_odd(tmp_path):
    def build(path):
        _make_original(path, size="338x190", rate="25", offset="0")

    service, root, asset = _import(tmp_path, "Odd", build)
    workspace = service._workspace(root)
    proxy = media_refs.resolve(workspace, asset, "frames")
    video = preprocess.probe_raw_media(proxy).video
    assert (video.width, video.height) == (84, 46)
    assert (preprocess.even_scaled_edge(338, 0.25), preprocess.even_scaled_edge(190, 0.25)) == (
        84,
        46,
    )


def test_frame_sampled_from_proxy_matches_original_frame_at_same_index(imported, tmp_path):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    sampler = media_refs.frame_sampler(workspace, asset)
    assert sampler.source != original
    stream = preprocess.probe_raw_media(original).video.stream_index
    index = 61
    from_proxy = sampler.sample(
        stream_index=stream, frame_index=index, destination=tmp_path / "p.jpg", long_edge=768
    )
    exact = preprocess.extract_frame_index(
        original, stream, index, tmp_path / "o.jpg", long_edge=768
    )
    neighbor = preprocess.extract_frame_index(
        original, stream, index + 6, tmp_path / "n.jpg", long_edge=768
    )
    same = _mean_abs_diff(_gray(from_proxy), _gray(exact))
    other = _mean_abs_diff(_gray(from_proxy), _gray(neighbor))
    assert same < 12.0
    assert other > same * 2  # 같은 번호가 맞는다는 증거 — 이웃 프레임은 확실히 멀다


def test_build_failure_falls_back_to_original_with_warning_once(imported, monkeypatch):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    warned = _warnings(monkeypatch)
    calls = []

    def broken(*args, **kwargs):
        calls.append(args)
        raise RuntimeError("인코더가 죽었다")

    monkeypatch.setattr(preprocess, "build_analysis_proxy", broken)
    assert media_refs.resolve(workspace, asset, "frames") == original
    assert media_refs.resolve(workspace, asset, "frames") == original
    assert len(calls) == 1
    assert len(warned) == 1 and "인코더가 죽었다" in warned[0]
    assert media_refs.frames_fingerprint(workspace, asset.asset_id) == media_refs.fingerprint(
        workspace, asset.asset_id
    )
    assert media_refs.frame_sampler(workspace, asset).source == original


def test_verification_failure_discards_proxy_and_falls_back(imported, monkeypatch):
    workspace, asset, _, _ = imported
    warned = _warnings(monkeypatch)
    monkeypatch.setattr(
        media_refs,
        "validate_analysis_proxy",
        lambda original, proxy: media_refs.PairValidation(
            "rejected", "대응 프레임이 일치하지 않습니다"
        ),
    )
    assert media_refs.resolve(workspace, asset, "frames") == workspace.source_media_path(asset)
    assert warned and "일치하지 않습니다" in warned[0]
    assert not list((workspace.root / "cache" / "analysis-proxy").rglob("*.mp4"))


def test_shifted_proxy_is_rejected_by_analysis_verification(imported, tmp_path):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    shifted = tmp_path / "shifted.mp4"
    subprocess.run(
        [
            "ffmpeg", "-v", "error", "-i", str(original), "-an",
            "-vf", "tpad=start_mode=clone:start_duration=0.12,trim=duration=4,scale=160:90",
            "-c:v", "libx264", str(shifted),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    assert media_refs.validate_analysis_proxy(original, shifted).status == "rejected"


def test_verified_fcp_proxy_is_used_as_is_and_no_analysis_proxy_is_built(tmp_path, monkeypatch):
    proxy = tmp_path / "B.fcpbundle/Event/Transcoded Media/Proxy Media/clip.mp4"

    def build(path):
        _make_original(path, size="320x180", rate="25", offset="0")
        transcode(path, proxy)

    service, root, asset = _import(tmp_path, "B", build)
    workspace = service._workspace(root)
    assert media_refs.load(workspace, asset.asset_id)["validation"]["status"] == "verified"
    monkeypatch.setattr(
        preprocess, "build_analysis_proxy", lambda *a, **k: pytest.fail("프록시를 또 만들었다")
    )
    assert media_refs.resolve(workspace, asset, "frames") == proxy
    assert media_refs.frames_fingerprint(workspace, asset.asset_id) == media_refs.fingerprint(
        workspace, asset.asset_id
    )
    assert not (workspace.root / "cache" / "analysis-proxy").exists()


def test_export_and_preview_purposes_keep_the_original(imported):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    assert media_refs.resolve(workspace, asset, "export") == original
    assert media_refs.resolve(workspace, asset, "preview") == original
    assert media_refs.resolve(workspace, asset, "audio") == original
    assert not (workspace.root / "cache" / "analysis-proxy").exists()
    assert media_refs.resolve(workspace, asset, "frames") != original


def test_frame_cache_key_separates_proxy_frames_but_analysis_fingerprint_does_not(imported):
    """조리법은 디코드 바이트에서 나온 로컬 캐시만 가른다 — 저장된 분석의 키는 그대로다."""
    workspace, asset, _, _ = imported
    old_key = hashlib.sha256(
        (asset.content_sha256 + media_refs.ORIGINAL_ONLY_FINGERPRINT).encode()
    ).hexdigest()
    sampler = media_refs.frame_sampler(workspace, asset)
    assert sampler.source.is_relative_to(workspace.root / "cache" / "analysis-proxy")
    assert sampler.cache_directory.name != old_key
    assert ANALYSIS_PROXY.tag() in media_refs.frames_fingerprint(workspace, asset.asset_id)
    media_fp = media_refs.fingerprint(workspace, asset.asset_id)
    assert media_fp == media_refs.ORIGINAL_ONLY_FINGERPRINT

    settings, workflow = RawUnderstandingSettings(), analysis_workflow.RawWorkflowConfig()
    video = VideoGrid(0, Fraction(0), Fraction(1, 25), 100)
    paid_input = analysis_workflow.source_input_fingerprint(
        source_sha256=asset.content_sha256, video=video, audio=None, settings=settings,
        workflow=workflow, media_fingerprint=media_refs.ORIGINAL_ONLY_FINGERPRINT,
    )  # fmt: skip
    current_input = analysis_workflow.source_input_fingerprint(
        source_sha256=asset.content_sha256, video=video, audio=None, settings=settings,
        workflow=workflow, media_fingerprint=media_fp,
    )  # fmt: skip
    assert current_input == paid_input


def _paid_revision_fingerprint(order_revision: str, asset, models: dict, transcript_fp: str) -> str:
    """프록시 이전 코드가 리비전 지문에 묶던 값 그대로 — 미디어 지문은 `original-only/v1`."""
    payload = {
        "schema": "raw-analysis-revision/v1",
        "order_revision": order_revision,
        "assets": [[asset.asset_id, asset.content_sha256 or ""]],
        "settings": asdict(RawUnderstandingSettings()),
        "models": models,
        "mode": "live",
        "source_bindings": {asset.asset_id: [transcript_fp, media_refs.ORIGINAL_ONLY_FINGERPRINT]},
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _persist_paid_revision(service, root, workspace, asset) -> str:
    """분석 프록시 이전에 끝난(돈을 낸) 리비전 — 그때의 코드가 남긴 모양 그대로 저장한다."""
    order = service.latest_global_source_order(root)
    workflow = service.settings(root)
    models = {"timing": workflow.models.timing_model, "luna": workflow.models.luna_model}
    transcript_fp = source_transcript.fingerprint(workspace, asset.asset_id)
    revision_fp = _paid_revision_fingerprint(order.revision_id, asset, models, transcript_fp)
    document_path = Path("analysis") / "paid" / "document.json"
    (root / document_path).parent.mkdir(parents=True, exist_ok=True)
    (root / document_path).write_text(
        json.dumps(
            {
                "media_fingerprint": media_refs.ORIGINAL_ONLY_FINGERPRINT,
                "transcript_fingerprint": transcript_fp,
                "annotations": [],
            }
        ),
        encoding="utf-8",
    )
    stamp = "2026-10-07T00:00:00Z"
    store = AnalysisStore(workspace)
    store.insert_revision(
        AnalysisRevision(
            "paid", order.revision_id, "global", "live",
            json.dumps(asdict(RawUnderstandingSettings())), json.dumps(models), revision_fp,
            "complete", 1, 1, None, stamp, stamp,
        )
    )  # fmt: skip
    store.upsert_source_result(
        AnalysisSourceResult(
            "paid", asset.asset_id, 0, asset.source_name, "paid-input", "analyzed", None,
            "live", str(document_path), None, 1, 1, None, None, stamp, stamp,
        )
    )  # fmt: skip
    return "paid"


def test_paid_pre_proxy_revision_stays_reusable_after_the_proxy_exists(imported, monkeypatch):
    """재시작 뒤에도 프록시 이전에 낸 전사·Luna 분석을 다시 청구하지 않는다(D47)."""
    workspace, asset, service, root = imported
    # 그 시절 작업공간 — 「다른 귀로 교차 확인」(D59 19d, 새 작업공간의 기본은 켜짐)이 없었다. 켜면
    # 두 번째 귀로 들은 적 없는 분석은 그 이유로 다시 분석 대상이 된다(견적이 말한다). 이 시험은
    # 프록시가 결속을 바꾸지 않는다는 것만 본다.
    service.set_cross_check(root, False)
    revision_id = _persist_paid_revision(service, root, workspace, asset)
    media_refs.resolve(workspace, asset, "frames")  # 분석 프록시가 이제 디스크에 있다
    monkeypatch.setattr(media_refs, "_FAILED", {})  # 새 프로세스
    assert service.cached_selection_analysis(root, (asset.asset_id,)) == revision_id
    (row,) = AnalysisStore(workspace).list_source_results(revision_id)
    assert analysis_workflow._complete_source_current(workspace, asset, row)


def test_build_failure_is_remembered_only_in_this_process(imported, monkeypatch):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    warned = _warnings(monkeypatch)
    calls: list[int] = []
    real = preprocess.build_analysis_proxy

    def broken(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("No space left on device")

    monkeypatch.setattr(preprocess, "build_analysis_proxy", broken)
    assert media_refs.resolve(workspace, asset, "frames") == original
    assert media_refs.resolve(workspace, asset, "frames") == original
    assert len(calls) == 1 and len(warned) == 1
    assert not list((workspace.root / "cache" / "analysis-proxy").rglob("record.json"))

    monkeypatch.setattr(media_refs, "_FAILED", {})  # 앱을 다시 띄운 것과 같다
    monkeypatch.setattr(preprocess, "build_analysis_proxy", real)
    proxy = media_refs.resolve(workspace, asset, "frames")
    assert proxy != original and proxy.is_file()
    assert ANALYSIS_PROXY.tag() in media_refs.frames_fingerprint(workspace, asset.asset_id)


def test_cancelled_build_escapes_and_records_nothing(imported, monkeypatch):
    workspace, asset, _, _ = imported
    warned = _warnings(monkeypatch)

    def cancelled(*args, **kwargs):
        raise JobCancelledError("취소")

    monkeypatch.setattr(preprocess, "build_analysis_proxy", cancelled)
    with pytest.raises(JobCancelledError):
        media_refs.resolve(workspace, asset, "frames")
    assert not warned and not media_refs._FAILED
    assert not list((workspace.root / "cache" / "analysis-proxy").rglob("record.json"))


def test_concurrent_frame_resolves_build_the_proxy_once(imported, monkeypatch):
    workspace, asset, _, _ = imported
    builds: list[int] = []
    real = preprocess.build_analysis_proxy
    monkeypatch.setattr(
        preprocess, "build_analysis_proxy", lambda *a, **k: builds.append(1) or real(*a, **k)
    )
    barrier = threading.Barrier(2)
    paths: list[Path] = []

    def worker() -> None:
        barrier.wait()
        paths.append(media_refs.resolve(workspace, asset, "frames"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert builds == [1]
    assert len(paths) == 2 and paths[0] == paths[1] and paths[0].is_file()


def _one_frame_shift(original: Path, out: Path, vf_prefix: str) -> None:
    """분석 프록시와 같은 인자로, 내용만 한 프레임 미룬 파일 — 개수·원점·격자는 같다."""
    command = preprocess._analysis_proxy_command(
        original, out, preprocess.probe_raw_media(original).video.stream_index,
        scale=ANALYSIS_PROXY.scale, video_codec=ANALYSIS_PROXY.video_codec,
        preset=ANALYSIS_PROXY.preset, crf=ANALYSIS_PROXY.crf, gop=ANALYSIS_PROXY.gop,
        pix_fmt=ANALYSIS_PROXY.pix_fmt,
    )  # fmt: skip
    vf = command.index("-vf") + 1
    command[vf] = vf_prefix + "," + command[vf]
    subprocess.run(command, check=True, capture_output=True)


def test_proxy_shifted_by_exactly_one_frame_is_rejected(imported, tmp_path):
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    clock = _clock(original)
    step = f"{clock.frame_duration.numerator}/{clock.frame_duration.denominator}"
    advanced = tmp_path / "advanced.mp4"  # 프록시 n번 = 원본 n+1번
    _one_frame_shift(
        original, advanced, f"trim=start_frame=1,setpts=PTS-{step}/TB,tpad=stop=1:stop_mode=clone"
    )
    shifted = _clock(advanced)
    assert shifted.uniform_grid
    assert (shifted.frame_count, shifted.frame_duration, shifted.presentation_origin) == (
        clock.frame_count,
        clock.frame_duration,
        clock.presentation_origin,
    )
    verdict = media_refs.validate_analysis_proxy(original, advanced)
    assert verdict.status == "rejected" and "이동" in verdict.reason
    # 반대 방향(프록시 n번 = 원본 n−1번)은 역할을 바꿔 같은 파일 한 쌍으로 잰다.
    plain = media_refs.resolve(workspace, asset, "frames")
    assert media_refs.validate_analysis_proxy(original, plain).status == "verified"
    delayed = media_refs.validate_analysis_proxy(advanced, plain)
    assert delayed.status == "rejected" and "이동" in delayed.reason


def test_correspondence_samples_are_the_luma_plane_without_range_conversion(tmp_path):
    """대응 표본은 32×32로 줄인 **루마 평면 그대로**다 — 회색 환산(`format=gray`)이 끼지 않는다.

    회색은 swscale이 전범위로 늘리고 그 양이 축소 경로마다 달라, 원본·프록시를 다른 눈금으로
    잰다(이 합성 원본에서도 평균 절대 차 8.14). 루마 평면이면 같은 디코드와 바이트가 같다.
    """
    path = tmp_path / "clip.mov"
    _make_original(path)
    second = Fraction(1001, 30000) * 10
    reference = subprocess.run(
        [
            "ffmpeg", "-v", "error", "-ss", str(float(second)), "-i", str(path),
            "-map", "0:v:0", "-frames:v", "1", "-vf", "scale=32:32", "-pix_fmt", "yuv420p",
            "-f", "rawvideo", "-",
        ],
        check=True,
        capture_output=True,
    ).stdout[:PAIR_VIDEO_SAMPLE_SIZE]  # fmt: skip
    assert len(reference) == PAIR_VIDEO_SAMPLE_SIZE
    assert preprocess.correspondence_samples(path, (second,)) == (reference,)


# 4K 10비트 원본과 그 25% 프록시가 같은 프레임에서 내는 표본 오차의 상한.
# 실측: 회색 표본 8.63 → 루마 표본 0.11. 상한 10 근처의 바닥이 사라졌는지를 본다.
_TEN_BIT_PAIR_MAX_ERROR = 2.0


def test_ten_bit_4k_original_verifies_against_its_analysis_proxy(tmp_path):
    """실측 꼴(DJI 4K 10비트 bt709)의 원본·프록시 짝이 우연이 아니라 넉넉히 통과한다."""
    original = tmp_path / "dji.mov"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=3840x2160:rate=30000/1001:duration=2",
            "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18", "-bf", "0",
            "-pix_fmt", "yuv420p10le",
            "-x264-params", "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            str(original),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    proxy = tmp_path / "proxy.mp4"
    recipe = ANALYSIS_PROXY
    preprocess.build_analysis_proxy(
        original, proxy, scale=recipe.scale, video_codec=recipe.video_codec,
        preset=recipe.preset, crf=recipe.crf, gop=recipe.gop, pix_fmt=recipe.pix_fmt,
        timeout_seconds=ANALYSIS_PROXY_LIMITS.proxy_timeout(None),
    )  # fmt: skip
    verdict = media_refs.validate_analysis_proxy(original, proxy)
    assert verdict.status == "verified", verdict.reason
    assert verdict.max_frame_error < _TEN_BIT_PAIR_MAX_ERROR


def test_built_proxy_is_reused_and_a_damaged_one_is_rebuilt(imported, monkeypatch):
    workspace, asset, _, _ = imported
    first = media_refs.resolve(workspace, asset, "frames")
    builds = []
    real = preprocess.build_analysis_proxy
    monkeypatch.setattr(
        preprocess, "build_analysis_proxy", lambda *a, **k: builds.append(1) or real(*a, **k)
    )
    assert media_refs.resolve(workspace, asset, "frames") == first
    assert not builds
    first.write_bytes(first.read_bytes()[:-100])
    rebuilt = media_refs.resolve(workspace, asset, "frames")
    assert rebuilt == first and builds == [1]
    assert _clock(rebuilt).frame_count == _clock(workspace.source_media_path(asset)).frame_count


def _window_plan(workspace, asset):
    clock = _clock(workspace.source_media_path(asset))
    video = VideoGrid(
        clock.stream_index, clock.presentation_origin, clock.frame_duration, clock.frame_count
    )
    return plan_common_windows(
        common_timeline(source_id=asset.asset_id, video=video, audio=None),
        (),
        RawUnderstandingSettings(),
    )


def test_scene_cut_probe_decodes_the_analysis_proxy(imported, monkeypatch):
    workspace, asset, _, _ = imported
    seen = []
    monkeypatch.setattr(
        preprocess, "verified_scene_times", lambda path, **_: seen.append(path) or ()
    )
    analysis_workflow._scene_plan(workspace, asset, _window_plan(workspace, asset))
    assert seen and seen[0].is_relative_to(workspace.root / "cache" / "analysis-proxy")


def test_stage_two_scene_cuts_follow_the_stage_one_frame_source(imported, monkeypatch):
    """프록시 이전(또는 프록시 실패)의 1단계는 원본에서 컷을 쟀다 — 2단계도 원본에서 잰다.

    출처가 바뀌어 컷이 달라지면 2단계의 실측 지문이 1단계와 어긋나 유료 1단계를 다시 돌린다.
    """
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    base = media_refs.fingerprint(workspace, asset.asset_id)
    seen: list[Path] = []
    monkeypatch.setattr(
        preprocess, "verified_scene_times", lambda path, **_: seen.append(path) or (Fraction(1),)
    )
    monkeypatch.setattr(
        preprocess, "build_analysis_proxy", lambda *a, **k: pytest.fail("2단계가 프록시를 만들었다")
    )
    plan = _window_plan(workspace, asset)
    analysis_workflow._scene_plan(workspace, asset, plan, base)
    analysis_workflow._scene_plan(workspace, asset, plan, base)
    assert seen == [original]  # 두 번째는 캐시 — 디코드도 없다
    pre_proxy_key = hashlib.sha256(
        (asset.content_sha256 + media_refs.ORIGINAL_ONLY_FINGERPRINT + "scene/v1").encode()
    ).hexdigest()
    assert (workspace.root / "cache" / (pre_proxy_key + "-scenes.json")).is_file()

    tagged = f"{base}+{ANALYSIS_PROXY.tag()}"  # 1단계가 프록시에서 잰 경우 — 그 캐시를 읽는다
    analysis_workflow._write_json_atomic(
        analysis_workflow._scene_cache(workspace, asset, tagged), {"times": ["2"]}
    )
    planned = analysis_workflow._scene_plan(workspace, asset, plan, tagged)
    assert seen == [original]
    assert [c.position for c in planned.candidates if c.kind == "verified_scene"] == [
        round((Fraction(2) - plan.video.presentation_origin) / plan.video.frame_duration)
        * plan.video.frame_duration
    ]


def test_build_refuses_to_overwrite_an_existing_target(imported, tmp_path):
    workspace, asset, _, _ = imported
    target = tmp_path / "taken.mp4"
    target.write_bytes(b"x")
    recipe = ANALYSIS_PROXY
    with pytest.raises(RuntimeError):
        preprocess.build_analysis_proxy(
            workspace.source_media_path(asset), target, scale=recipe.scale,
            video_codec=recipe.video_codec, preset=recipe.preset, crf=recipe.crf,
            gop=recipe.gop, pix_fmt=recipe.pix_fmt,
            timeout_seconds=ANALYSIS_PROXY_LIMITS.proxy_timeout(None),
        )  # fmt: skip
    assert target.read_bytes() == b"x"


# ─── D59 — 시간 한도는 원본 길이를 따르고, 넘기면 원본으로 물러난다 ─────────────────

PRE_19A_TAG = "analysis-proxy/v1/scale=0.25/libx264/veryfast/crf28/g12/yuv420p"
PRE_19A_KEY_FOR_A64 = "4ee90948c629b183f851843992fc898e06f84ec3193164ee1537b719a454656d"
"""19a 이전 코드로 잰 `_proxy_directory`의 열쇠(원본 해시 `a`×64) — 한도가 열쇠 밖이라는 증거."""


def test_limits_grow_with_the_source_and_keep_the_old_floor():
    limits = AnalysisProxyLimits()
    assert (limits.timeout_floor_seconds, limits.timeout_media_ratio) == (1800.0, 6.0)
    assert (limits.frame_batch_timeout_floor_seconds, limits.frame_batch_timeout_media_ratio) == (
        1800.0,
        6.0,
    )
    assert limits.proxy_timeout(None) == limits.proxy_timeout(20.0) == 1800.0  # 짧은 원본은 그대로
    assert limits.proxy_timeout(545.145) == pytest.approx(3270.87)  # 0016
    assert limits.proxy_timeout(1359.025) == pytest.approx(8154.15)  # 0018
    assert limits.frame_batch_timeout(1359.025) == pytest.approx(8154.15)
    for unknown in (float("nan"), float("inf"), -1.0, 0.0):
        assert limits.proxy_timeout(unknown) == 1800.0
    validate_analysis_proxy_limits(limits)
    for broken in (
        AnalysisProxyLimits(timeout_floor_seconds=0.0),
        AnalysisProxyLimits(timeout_media_ratio=-1.0),
        AnalysisProxyLimits(frame_batch_timeout_floor_seconds=float("nan")),
        AnalysisProxyLimits(frame_batch_timeout_media_ratio=True),
    ):
        with pytest.raises(ValueError, match="디코드 시간 한도"):
            validate_analysis_proxy_limits(broken)


def test_limits_stay_outside_the_recipe_tag_and_every_cache_key(imported, monkeypatch):
    """한도는 기다리는 시간이지 만들어지는 바이트가 아니다 — 열쇠·지문은 19a 이전 그대로다."""
    workspace, asset, _, _ = imported
    assert ANALYSIS_PROXY.tag() == PRE_19A_TAG
    assert media_refs._proxy_directory(workspace, "a" * 64, ANALYSIS_PROXY).name == (
        PRE_19A_KEY_FOR_A64
    )
    assert not [f.name for f in fields(AnalysisProxyRecipe) if "timeout" in f.name]
    assert not [key for key in asdict(RawUnderstandingSettings()) if "timeout" in key]
    first = media_refs.resolve(workspace, asset, "frames")
    before = media_refs.frames_fingerprint(workspace, asset.asset_id)
    monkeypatch.setattr(
        media_refs,
        "ANALYSIS_PROXY_LIMITS",
        AnalysisProxyLimits(timeout_floor_seconds=99.0, timeout_media_ratio=99.0),
    )
    monkeypatch.setattr(
        preprocess, "build_analysis_proxy", lambda *a, **k: pytest.fail("한도가 열쇠를 바꿨다")
    )
    assert media_refs.resolve(workspace, asset, "frames") == first
    assert media_refs.frames_fingerprint(workspace, asset.asset_id) == before


def test_build_hands_the_media_scaled_limit_to_the_encoder(imported, monkeypatch):
    workspace, asset, _, _ = imported
    seen: list[dict] = []
    real_run = preprocess._run

    def spy(command, **kwargs):
        if "-copyts" in command:
            seen.append(kwargs)
        return real_run(command, **kwargs)

    monkeypatch.setattr(preprocess, "_run", spy)
    media_refs.resolve(workspace, asset, "frames")
    (kwargs,) = seen
    assert kwargs == {
        "timeout": ANALYSIS_PROXY_LIMITS.proxy_timeout(asset.duration_seconds),
        "step": "분석 프록시 만들기",
    }
    sampler = media_refs.frame_sampler(workspace, asset)
    assert sampler.decode_timeout_seconds == ANALYSIS_PROXY_LIMITS.frame_batch_timeout(
        asset.duration_seconds
    )


def test_a_proxy_timeout_falls_back_to_the_original_with_a_warning(imported, monkeypatch):
    """0016·0018 꼴 — 인코드가 한도를 넘기면 `ProcessTimeout`(RuntimeError)이 원본 폴백을 탄다.

    예전에는 `TimeoutExpired`(SubprocessError)가 `_build_and_verify`의 그물을 빠져나가 원본
    전체의 분석이 실패했다. 실제 ffmpeg를 한도 0.01초로 멈춘다.
    """
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    warned = _warnings(monkeypatch)
    monkeypatch.setattr(
        media_refs,
        "ANALYSIS_PROXY_LIMITS",
        AnalysisProxyLimits(timeout_floor_seconds=0.01, timeout_media_ratio=0.0),
    )
    assert media_refs.resolve(workspace, asset, "frames") == original
    (warning,) = warned
    assert "원본으로 분석합니다" in warning and "ProcessTimeout" in warning
    assert "분석 프록시 만들기 — 시간 한도" in warning
    proxy_root = workspace.root / "cache" / "analysis-proxy"
    assert not list(proxy_root.rglob("*.part")) and not list(proxy_root.rglob("*.mp4"))
    assert not list(proxy_root.rglob("record.json"))
    assert media_refs.frames_fingerprint(workspace, asset.asset_id) == media_refs.fingerprint(
        workspace, asset.asset_id
    )
    sampler = media_refs.frame_sampler(workspace, asset)
    assert sampler.source == original
    sampled = workspace.root / "sampled"
    sampled.mkdir()
    sampler.prepare(frame_indices=(5, 60), directory=sampled, long_edge=160)
    assert sorted(p.name for p in sampled.glob("frame_*.jpg")) == [
        "frame_00000005.jpg",
        "frame_00000060.jpg",
    ]


def test_leftovers_of_a_killed_build_neither_block_nor_poison_the_next_build(imported):
    """실측 `cache/analysis-proxy`: 잠금만 남은 폴더 둘, `.part`가 남은 폴더 셋(강제 종료).

    `flock`은 프로세스가 죽으면 풀리므로 남은 잠금 파일은 막지 않는다. `.part`는 무작위 새 이름과
    겹치지 않아 막지 않지만 쌓인다 — 두 잠금을 쥔 다음 만들기가 치운다(살아 있는 만들기의 파일은
    그때 있을 수 없다).
    """
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    directory = media_refs._proxy_directory(
        workspace, media_refs._source_sha(workspace, asset, original), ANALYSIS_PROXY
    )
    directory.mkdir(parents=True)
    (directory / "build.lock").touch()
    dead = directory / ".proxy.mp4.74770.668bb83e.part"
    dead.write_bytes(b"\x00" * 4096)
    (directory / "record.json.tmp").write_text("{", encoding="utf-8")  # 반쯤 쓰다 죽은 기록
    proxy = media_refs.resolve(workspace, asset, "frames")
    assert proxy == directory / "proxy.mp4" and proxy.is_file()
    assert not dead.exists()
    assert (directory / "build.lock").exists()
    assert json.loads((directory / "record.json").read_text(encoding="utf-8"))["state"] == "ready"
    assert _clock(proxy).frame_count == _clock(original).frame_count


def test_a_dead_build_next_to_a_ready_proxy_is_swept_and_the_proxy_kept(imported):
    """실측 한 폴더(b453…)는 준비된 프록시 옆에 죽은 만들기의 `.part`(45MB)가 남았다.

    잠금을 쥔 동안에는 그 폴더에서 만드는 중인 것이 없으니 치운다 — 게시본·기록은 그대로다.
    """
    workspace, asset, _, _ = imported
    proxy = media_refs.resolve(workspace, asset, "frames")
    stray = proxy.parent / ".proxy.mp4.40775.b567f817.part"
    stray.write_bytes(b"x")
    before = (proxy.read_bytes(), (proxy.parent / "record.json").read_bytes())
    assert media_refs.resolve(workspace, asset, "frames") == proxy
    assert not stray.exists()
    assert (proxy.read_bytes(), (proxy.parent / "record.json").read_bytes()) == before


def test_a_sweep_that_cannot_clean_warns_and_the_analysis_goes_on(imported, monkeypatch):
    """치우기는 청소일 뿐이다 — 찾기·지우기의 OSError는 WARN으로 남고 프록시는 그대로 쓰인다."""
    workspace, asset, _, _ = imported
    proxy = media_refs.resolve(workspace, asset, "frames")
    warned = _warnings(monkeypatch)
    vanished = proxy.parent / ".proxy.mp4.1.0123abcd.part"  # 찾은 뒤 사라졌다(stat 실패)
    monkeypatch.setattr(preprocess, "staging_leftovers", lambda target: (vanished,))
    assert media_refs.resolve(workspace, asset, "frames") == proxy
    assert len(warned) == 1 and "잔재를 지우지 못해" in warned[0]

    def unreadable(target):
        raise PermissionError("읽을 수 없는 폴더")

    monkeypatch.setattr(preprocess, "staging_leftovers", unreadable)
    assert media_refs.resolve(workspace, asset, "frames") == proxy
    assert len(warned) == 2 and "잔재를 찾지 못해" in warned[1]


def test_a_proxy_timeout_no_longer_fails_the_source_analysis(tmp_path, monkeypatch):
    """0016 꼴 끝까지 — 분석 프록시가 시간 한도를 넘겨도 원본의 분석은 원본 프레임으로 끝난다.

    분석 작업 안(취소를 듣는 실행)의 자체 마감이 `ProcessTimeout`이 되는 길이다. 예전에는
    이 자리에서 원본 행이 `failed`(`TimeoutExpired: Command '[ffmpeg … -copyts …]'`)로 끝났다.
    """
    from tests._sync_fakes import energy_factory, numpy_or_fake
    from tests.raw.test_sync_report import REPLAY_ANALYSIS_MODEL, start_tone_analysis, wait

    numpy_or_fake(monkeypatch)
    monkeypatch.setattr(sync_report, "default_session_factory", energy_factory)
    warned = _warnings(monkeypatch)
    monkeypatch.setattr(
        media_refs,
        "ANALYSIS_PROXY_LIMITS",
        AnalysisProxyLimits(timeout_floor_seconds=0.01, timeout_media_ratio=0.0),
    )
    service, root, asset_id, handle = start_tone_analysis(tmp_path, model=REPLAY_ANALYSIS_MODEL)
    assert wait(service, root, handle.job_id).status == "succeeded"
    _settle_sync_job(service, root)
    store = AnalysisStore(service._workspace(root))
    (revision,) = store.list_revisions()
    (row,) = store.list_source_results(revision.revision_id)
    # 끝까지 분석됐다 — 예전에는 failed · error에 TimeoutExpired · 문서 없음이었다.
    assert row.status == "analyzed" and row.error is None and row.document_path is not None, row
    assert [w for w in warned if "ProcessTimeout" in w and "원본으로 분석합니다" in w]
    document = json.loads((root / row.document_path).read_text(encoding="utf-8"))
    assert document["frame_source"] == media_refs.ORIGINAL_ONLY_FINGERPRINT  # 원본에서 뽑았다
    assert document["frames"]["failures"] == [] and document["frames"]["frames"]
    assert not list((root / "cache" / "analysis-proxy").rglob("*.mp4"))


def _settle_sync_job(service, root) -> None:
    """분석 끝에 걸리는 무료 확인 작업이 끝날 때까지 — 임시 폴더를 치우기 전에."""
    from tests.raw.test_sync_report import wait
    from vlogsrt.project.raw import service as raw_service

    latest = service.latest_job(root, raw_service.SYNC_CHECK_JOB_KIND)
    if latest is not None:
        wait(service, root, latest.job_id)
