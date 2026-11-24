"""그리기 사이의 파일 바이트 기억(D167) — 같은 파일이면 다시 열지 않고, 바뀌면 언제나 다시 읽는다.

작업대 그리기 한 번이 원본마다 같은 JSON(시각 확인 보고서 · 1단계 기록 · 분석 문서 · 영상 검토
기록)을 약 200번 열었다. 기억하는 것은 **바이트**뿐이다 — 부르는 쪽은 매번 새로 푼다(얼리지 않은
사전을 나눠 쓰지 않는다). 등급 같은 계산 결과는 기억하지 않는다(D51·D52). 열쇠는 파일 신원(장치 ·
inode · 크기 · 수정 · 변경 시각)이라 쓰기·바꿔치기 뒤에는 처음부터 읽는다.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vlogsrt.project.raw import file_memo
from vlogsrt.project.raw.config import FileMemoSettings


@pytest.fixture
def memo(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """새 기억 하나와 실제로 연 파일 목록."""
    monkeypatch.setattr(file_memo, "_MEMO", file_memo.FileMemo(FileMemoSettings()))
    opened: list[Path] = []
    real = Path.read_bytes

    def counted(self: Path) -> bytes:
        opened.append(self)
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", counted)
    return opened


def test_an_unchanged_file_is_read_once(tmp_path: Path, memo: list[Path]) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(b'{"k": 1}')
    assert file_memo.read_bytes(path) == file_memo.read_bytes(path) == b'{"k": 1}'
    assert file_memo.read_text(path) == '{"k": 1}'
    assert memo == [path]


def test_a_rewrite_of_the_same_size_is_read_again(tmp_path: Path, memo: list[Path]) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(b'{"k": 1}')
    file_memo.read_bytes(path)
    path.write_bytes(b'{"k": 2}')
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000))  # 같은 눈금에 겹치지 않게
    assert file_memo.read_bytes(path) == b'{"k": 2}'
    assert memo == [path, path]


def test_an_atomic_replace_is_read_again(tmp_path: Path, memo: list[Path]) -> None:
    path = tmp_path / "a.json"
    path.write_bytes(b"old")
    file_memo.read_bytes(path)
    staged = tmp_path / ".a.json.tmp"
    staged.write_bytes(b"new")
    os.replace(staged, path)  # 다른 inode — 크기·시각이 같아도 다시 읽는다
    assert file_memo.read_bytes(path) == b"new"


def test_a_missing_file_raises_like_reading_it(tmp_path: Path, memo: list[Path]) -> None:
    with pytest.raises(FileNotFoundError):
        file_memo.read_bytes(tmp_path / "none.json")
    path = tmp_path / "gone.json"
    path.write_bytes(b"x")
    file_memo.read_bytes(path)
    path.unlink()
    with pytest.raises(FileNotFoundError):
        file_memo.read_bytes(path)  # 기억이 지운 파일을 살려 두지 않는다


def test_the_memory_keeps_only_the_newest_bytes_under_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    held = file_memo.FileMemo(FileMemoSettings(max_bytes=10, max_file_bytes=6))
    monkeypatch.setattr(file_memo, "_MEMO", held)
    paths = []
    for name in ("a", "b", "c"):
        path = tmp_path / name
        path.write_bytes(b"12345")
        file_memo.read_bytes(path)
        paths.append(path)
    assert held.cached_bytes() <= 10 and held.holds(paths[2]) and not held.holds(paths[0])
    big = tmp_path / "big"
    big.write_bytes(b"1234567")  # 한 파일 상한을 넘으면 기억하지 않는다 — 읽기는 그대로다
    assert file_memo.read_bytes(big) == b"1234567" and not held.holds(big)


@pytest.mark.parametrize(("max_bytes", "max_file_bytes"), [(0, 1), (10, 0), (5, 6), (True, 1)])
def test_settings_refuse_broken_caps(max_bytes: object, max_file_bytes: object) -> None:
    with pytest.raises(ValueError):
        FileMemoSettings(max_bytes=max_bytes, max_file_bytes=max_file_bytes)  # type: ignore[arg-type]
