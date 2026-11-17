"""분석 프록시 검증의 프록시 쪽 표본은 ffmpeg 하나로 뽑는다(W-78, D137) — 바이트는 점마다와 같다.

실측(D135): 프록시 검증의 대응 표본 추출이 원본마다 40 프로세스였다 — 원본 5점 + 프록시 5점 +
이동 확인 30점. 프록시(25%, H.264)는 디코드가 싸서 대부분이 프로세스 띄우기였다(원본당 1.6초).
이제 프록시 쪽 35점은 여러 입력(`-ss t -i 프록시`)을 `concat` 하나로 이어 한 번에 뽑는다.
원본 쪽 5점은 그대로 점마다다 — 4K HEVC 디코드가 지배해 묶어도 얻는 게 적고, 한 자리 안에서
디코더 다섯이 돈다.
표본이 모자라면(끝을 넘은 점 등) 조용히 밀지 않고 실패한다.
"""

from __future__ import annotations

import shutil
from fractions import Fraction
from pathlib import Path

import pytest

from tests.raw import test_analysis_proxy as proxy_tests
from vlogsrt import preprocess
from vlogsrt.project.raw import media_refs

_clock, _make_original = proxy_tests._clock, proxy_tests._make_original
imported = proxy_tests.imported  # 25% 프록시를 만들 수 있는 FCP 원본 하나
fresh_process = proxy_tests.fresh_process  # 작은 합성 원본도 프록시를 만들게 한다(autouse)
needs_ffmpeg = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg required")


@needs_ffmpeg
def test_batched_samples_are_the_same_bytes_as_one_by_one(tmp_path) -> None:
    path = tmp_path / "clip.mov"
    _make_original(path)
    clock = _clock(path)
    last = clock.frame_duration * (clock.frame_count - 1)
    points = (Fraction(0), clock.frame_duration * 3, last / 2, last - clock.frame_duration, last)
    points += tuple(p + clock.frame_duration for p in points[:3])  # 같은 점 근처(이동 확인)
    assert preprocess.correspondence_frames(path, points) == preprocess.correspondence_samples(
        path, points
    )


@needs_ffmpeg
def test_a_point_without_a_frame_fails_instead_of_shifting_the_rest(tmp_path) -> None:
    path = tmp_path / "clip.mov"
    _make_original(path)
    clock = _clock(path)
    beyond = clock.frame_duration * (clock.frame_count + 30)
    with pytest.raises(RuntimeError, match="대응 표본"):
        preprocess.correspondence_frames(path, (Fraction(0), beyond, Fraction(1)))


def test_no_points_need_no_process() -> None:
    assert preprocess.correspondence_frames(preprocess.Path("없는.mov"), ()) == ()


def _count_runs(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = preprocess.run_process

    def run(command, *args, **kwargs):
        seen.append([str(part) for part in command])
        return real(command, *args, **kwargs)

    monkeypatch.setattr(preprocess, "run_process", run)
    return seen


@needs_ffmpeg
def test_proxy_validation_decodes_the_proxy_in_one_process(imported, monkeypatch) -> None:
    workspace, asset, _, _ = imported
    original = workspace.source_media_path(asset)
    proxy = media_refs.resolve(workspace, asset, "frames")
    seen = _count_runs(monkeypatch)
    verdict = media_refs.validate_analysis_proxy(original, proxy)
    assert proxy != original and verdict.status == "verified"
    samples = [argv for argv in seen if Path(argv[0]).stem == "ffmpeg" and "rawvideo" in argv]
    on_proxy = [argv for argv in samples if str(proxy) in argv]
    on_original = [argv for argv in samples if str(original) in argv]
    assert len(on_proxy) == 1  # 예전 35 — 같은 점 5 + 이동 6 × 5
    assert len(on_original) == len(media_refs._VIDEO_SAMPLE_POINTS)  # 원본은 점마다 그대로
