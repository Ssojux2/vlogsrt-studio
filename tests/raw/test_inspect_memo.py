"""원본 조사(ffprobe)는 같은 파일에 프로세스 안에서 한 번이다(W-24 세션 G).

작업대의 첫 Final Cut 판이 원본·프록시마다 ffprobe를 돌렸다(≈ 36번, 2–8초). 이제 경로와 stat
신원(크기·수정 시각 ns·inode·장치)이 같으면 기억한 조사값을 쓴다 — 하나라도 바뀌면 다시 조사한다.
기억은 디스크에 남지 않고, 실패는 기억하지 않는다. 조사자는 가짜(ffmpeg 없음)다.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vlogsrt import preprocess
from vlogsrt.project.raw import ingest


class _Probe:
    """`probe_raw_media` 대역 — 부른 경로를 적고, 부를 때마다 길이를 하나씩 늘려 돌려준다."""

    def __init__(self) -> None:
        self.calls: list[Path] = []
        self.fail_next = False

    def __call__(self, path: Path) -> SimpleNamespace:
        self.calls.append(path)
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("깨진 파일")
        return SimpleNamespace(
            creation_time=SimpleNamespace(valid=False, utc=None, raw=""),
            duration=float(len(self.calls)),
            video=SimpleNamespace(width=160, height=90),
            audio=(),
        )


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> _Probe:
    fake = _Probe()
    monkeypatch.setattr(preprocess, "probe_raw_media", fake)
    return fake


def _touch(path: Path, mtime_ns: int) -> None:
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_the_same_file_is_probed_once(tmp_path: Path, probe: _Probe) -> None:
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"a" * 10)
    first = ingest.ffprobe_media_inspector(clip)
    assert ingest.ffprobe_media_inspector(clip) is first
    assert len(probe.calls) == 1
    assert not list(tmp_path.glob("*.json"))  # 기억은 프로세스 메모리뿐이다


def test_a_stat_change_probes_again(tmp_path: Path, probe: _Probe) -> None:
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"a" * 10)
    _touch(clip, 1_000_000_000)
    assert ingest.ffprobe_media_inspector(clip).duration_seconds == 1.0
    _touch(clip, 2_000_000_000)  # 수정 시각만
    assert ingest.ffprobe_media_inspector(clip).duration_seconds == 2.0
    clip.write_bytes(b"b" * 11)  # 크기
    _touch(clip, 2_000_000_000)
    assert ingest.ffprobe_media_inspector(clip).duration_seconds == 3.0
    replacement = tmp_path / "next.mov"  # 같은 크기·같은 시각의 다른 파일(inode)
    replacement.write_bytes(b"c" * 11)
    _touch(replacement, 2_000_000_000)
    os.replace(replacement, clip)
    assert ingest.ffprobe_media_inspector(clip).duration_seconds == 4.0
    assert ingest.ffprobe_media_inspector(clip).duration_seconds == 4.0
    assert len(probe.calls) == 4


def test_failures_and_missing_files_are_not_remembered(tmp_path: Path, probe: _Probe) -> None:
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"a")
    probe.fail_next = True
    with pytest.raises(RuntimeError):
        ingest.ffprobe_media_inspector(clip)
    assert ingest.ffprobe_media_inspector(clip).duration_seconds == 2.0  # 실패 뒤엔 다시 조사
    missing = tmp_path / "gone.mov"
    for _ in range(2):  # 없는 파일 — 조사자가 그대로 불린다(실패를 알리는 것은 조사자다)
        ingest.ffprobe_media_inspector(missing)
    assert probe.calls[-2:] == [missing, missing]


def test_a_different_prober_does_not_reuse_the_memo(
    tmp_path: Path, probe: _Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip = tmp_path / "clip.mov"
    clip.write_bytes(b"a")
    ingest.ffprobe_media_inspector(clip)
    other = _Probe()
    monkeypatch.setattr(preprocess, "probe_raw_media", other)
    ingest.ffprobe_media_inspector(clip)
    assert len(probe.calls) == 1 and len(other.calls) == 1
