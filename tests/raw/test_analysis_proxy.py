"""프록시 없는 원본의 25% 분석 프록시 — 격자 보존·검증 실패 폴백·캐시 키 분리(2026-10-08).

합성 ffmpeg 미디어와 임시 작업공간만 쓴다. 실제 라이브러리·API는 건드리지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import threading
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw.test_media_refs import transcode
from tests.raw.test_review_workspace import _wait_terminal
from vlogsrt import log, preprocess
from vlogsrt.project.raw import analysis_workflow, media_refs, source_transcript
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_PROXY,
    PAIR_VIDEO_SAMPLE_SIZE,
    RawUnderstandingSettings,
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
    monkeypatch.setattr(preprocess, "verified_scene_times", lambda path: seen.append(path) or ())
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
        preprocess, "verified_scene_times", lambda path: seen.append(path) or (Fraction(1),)
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
        )  # fmt: skip
    assert target.read_bytes() == b"x"
