"""`keywords` 3계층 로더 — 고정 사전 + 회차 사전 + CLI.

순서를 유지한다. API가 순서를 참고할 수 있고, 사람이 읽을 때도 계층이 보인다.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

BASE_KEYWORDS = Path("keywords/base.txt")


def parse_keywords(text: str) -> list[str]:
    """빈 줄과 `#` 주석은 버린다. 한 줄에 하나."""
    entries = []
    for raw_line in text.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line:
            entries.append(line)
    return entries


def read_keywords(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        return parse_keywords(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError(f"사전을 읽지 못했습니다: {path} ({exc})") from exc


def dedupe(entries: Iterable[str]) -> list[str]:
    """중복 제거하되 처음 등장 순서를 지킨다."""
    return list(dict.fromkeys(entry.strip() for entry in entries if entry.strip()))


def load_keywords(
    episode_file: Path | None = None,
    inline: Sequence[str] = (),
    base_file: Path = BASE_KEYWORDS,
) -> list[str]:
    return dedupe([*read_keywords(base_file), *_episode(episode_file), *inline])


def _episode(episode_file: Path | None) -> list[str]:
    if episode_file is None:
        return []
    if not episode_file.is_file():
        raise RuntimeError(f"회차 사전을 찾지 못했습니다: {episode_file}")
    return read_keywords(episode_file)


def split_inline(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]
