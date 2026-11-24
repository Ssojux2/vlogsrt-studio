"""미리보기 · 초안 렌더의 하드웨어 디코드(P-3, D157) — macOS VideoToolbox, 실패하면 소프트웨어로.

- 켜졌고 macOS이고 그 ffmpeg가 `videotoolbox`를 들고 있고 이 꼴이 실패한 적 없을 때만 `-hwaccel`.
- 하드웨어 디코드가 실패하면 같은 인코더로 소프트웨어 디코드를 한 번 더 하고, 그 (코덱, 화소 형식,
  크기)는 이 실행 동안 바로 소프트웨어다. 그것도 실패하면 예전 길(하드웨어 인코더 → libx264)이다.
- 바이트가 요청 신원인 분석 프록시 · 클립 · 썸네일에는 쓰지 않는다 — 부르는 자리는 미리보기와
  렌더 둘뿐.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from vlogsrt import preprocess

HEVC = SimpleNamespace(
    codec_name="hevc", pix_fmt="yuv420p10le", width=3840, height=2160, stream_index=0
)


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(preprocess, "_HW_DECODE_FAILED", set())
    monkeypatch.setattr(
        preprocess, "available_hwaccels", lambda executable: frozenset({"videotoolbox"})
    )
    monkeypatch.setattr(preprocess, "require_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(preprocess.sys, "platform", "darwin")


def test_hardware_decode_only_when_on_mac_listed_and_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert preprocess.hardware_decode_args(True, HEVC) == ["-hwaccel", "videotoolbox"]
    assert preprocess.hardware_decode_args(False, HEVC) == []
    preprocess.remember_hardware_decode_failure(HEVC, RuntimeError("no session"))
    assert preprocess.hardware_decode_args(True, HEVC) == []
    other = SimpleNamespace(**{**vars(HEVC), "width": 1920, "height": 1080})
    assert preprocess.hardware_decode_args(True, other) == ["-hwaccel", "videotoolbox"]
    monkeypatch.setattr(preprocess, "available_hwaccels", lambda executable: frozenset())
    assert preprocess.hardware_decode_args(True, other) == []
    monkeypatch.setattr(
        preprocess, "available_hwaccels", lambda executable: frozenset({"videotoolbox"})
    )
    monkeypatch.setattr(preprocess.sys, "platform", "win32")
    assert preprocess.hardware_decode_args(True, other) == []


def _preview(monkeypatch: pytest.MonkeyPatch, fail: Any) -> tuple[list[list[str]], list[str]]:
    calls: list[list[str]] = []

    def run(argv: list[str], priority: Any, timeout: Any) -> None:
        calls.append(argv)
        if fail(argv):
            raise RuntimeError("ffmpeg 실패")

    monkeypatch.setattr(preprocess, "_run_niced", run)
    encoding = preprocess.PreviewEncoding(
        long_edge=960, preset="veryfast", crf=23, hardware_decode=True
    )
    probe = SimpleNamespace(video=HEVC)
    args = ["-c:v", "libx264", "-preset", "veryfast"]
    used = preprocess._encode_visual_preview(
        Path("in.mov"), Path("out.mp4"), probe, None, args, encoding, None, None
    )
    return calls, used


def test_a_failed_hardware_decode_is_retried_in_software_and_remembered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, used = _preview(monkeypatch, lambda argv: "-hwaccel" in argv)
    assert ["-hwaccel" in argv for argv in calls] == [True, False]
    assert used == ["-c:v", "libx264", "-preset", "veryfast"]  # 같은 인코더 그대로
    assert ("hevc", "yuv420p10le", 3840, 2160) in preprocess._HW_DECODE_FAILED
    calls, _ = _preview(monkeypatch, lambda argv: False)
    assert ["-hwaccel" in argv for argv in calls] == [False]  # 이 꼴은 바로 소프트웨어


def test_a_software_failure_after_the_retry_is_not_hidden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(RuntimeError, match="ffmpeg 실패"):
        _preview(monkeypatch, lambda argv: True)


def test_a_timeout_is_never_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    def run(argv: list[str], priority: Any, timeout: Any) -> None:
        raise preprocess.ProcessTimeout(argv, 1.0, "미리보기")

    monkeypatch.setattr(preprocess, "_run_niced", run)
    encoding = preprocess.PreviewEncoding(
        long_edge=960, preset="veryfast", crf=23, hardware_decode=True
    )
    with pytest.raises(preprocess.ProcessTimeout):
        preprocess._encode_visual_preview(
            Path("in.mov"),
            Path("out.mp4"),
            SimpleNamespace(video=HEVC),
            None,
            ["-c:v", "libx264"],
            encoding,
            None,
            None,
        )
    assert not preprocess._HW_DECODE_FAILED


def test_only_preview_and_render_decode_in_hardware() -> None:
    """바이트가 요청 신원인 길(분석 프록시 · 클립 · 썸네일)은 하드웨어 디코드를 모른다(D-4)."""
    source = inspect.getsource(preprocess)
    assert source.count("hardware_decode_args(") == 3  # 정의 하나 + 미리보기 + 렌더
    for name in ("_analysis_proxy_command", "_analysis_clip_command"):
        assert "hwaccel" not in inspect.getsource(getattr(preprocess, name))
