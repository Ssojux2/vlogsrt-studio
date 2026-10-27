"""프로세스가 쓰다 버리는 `$TMPDIR` 폴더의 이름 규칙과 시작 청소(D63 H).

`TemporaryDirectory`는 정상 종료면 스스로 지우지만 프로세스가 죽으면(강제 종료·전원) 남는다.
그래서 이름에 만든 프로세스의 pid를 싣는다 — `<종류><pid>-<무작위>`. 서버가 시작할 때 pid가 더는
살아 있지 않은 폴더만 지운다. pid가 없는 옛 이름은 건드리지 않는다 — 살아 있는 서버의 것일 수
있다. pid가 재사용되어 살아 보이면 남긴다(지우는 쪽으로는 틀리지 않는다).
"""

from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from vlogsrt import log
from vlogsrt.oscompat import process_alive

PROGRESS_SCRATCH = "vlogsrt-progress-"
"""서버가 켜는 ffmpeg `-progress` 파일 폴더(D61)."""

SYNC_SCRATCH = "vlogsrt-sync-"
"""자막 시각 확인이 디코드한 PCM·VAD 칸을 두는 폴더(D55) — 죽으면 audio.pcm(약 2.5 MB)이 남았다."""

PID_SCRATCH_KINDS = (PROGRESS_SCRATCH, SYNC_SCRATCH)
"""pid를 실어 만들고 시작 때 치우는 종류 — 이 밖의 `vlogsrt-*` 폴더는 청소하지 않는다."""

_PID_SCRATCH_NAME = re.compile(
    "(" + "|".join(re.escape(kind) for kind in PID_SCRATCH_KINDS) + r")([1-9][0-9]*)-.+"
)


@dataclass(frozen=True)
class ScratchSweepSettings:
    """시작 청소가 한 번에 살펴볼 항목 수 — 외부 근거 없는 조정 대상(D63 H).

    세는 것은 이름이 맞는 항목뿐이다(`$TMPDIR`의 남의 항목은 이름만 보고 넘긴다). 청소는 서버 시작을
    붙잡는다. 잠금 파일 하나는 수십 µs라 2048개면 0.1초 안이다 — 실측 잠금 파일 1018개가 한 번에
    끝나고, 비정상으로 쌓인 것(수만 개)도 시작을 오래 막지 않는다. 남은 것은 다음 시작이 치운다.
    """

    entry_limit: int = 2048

    def __post_init__(self) -> None:
        if isinstance(self.entry_limit, bool) or not isinstance(self.entry_limit, int):
            raise ValueError(f"entry_limit는 정수여야 한다: {self.entry_limit!r}")
        if self.entry_limit < 1:
            raise ValueError(f"entry_limit는 1 이상이어야 한다: {self.entry_limit}")


SCRATCH_SWEEP = ScratchSweepSettings()
"""지금 쓰는 청소 한도 — 임시 폴더 청소와 UI 잠금 파일 청소가 이 하나를 읽는다."""


def pid_prefix(kind: str) -> str:
    """이 프로세스가 만들 폴더의 이름 머리 — `TemporaryDirectory(prefix=...)`에 넘긴다."""
    if kind not in PID_SCRATCH_KINDS:
        raise ValueError(f"pid를 싣는 임시 폴더 종류가 아니다: {kind!r}")
    return f"{kind}{os.getpid()}-"


def sweep_dead_scratch(
    root: Path | None = None, settings: ScratchSweepSettings = SCRATCH_SWEEP
) -> tuple[Path, ...]:
    """만든 프로세스가 죽은 pid 폴더만 지운다 — 서버 시작에서만 부른다. 지운 폴더를 돌려준다."""
    directory = Path(tempfile.gettempdir()) if root is None else root
    removed: list[Path] = []
    for path in _dead_candidates(directory, settings.entry_limit):
        try:
            shutil.rmtree(path)
        except FileNotFoundError:
            continue  # 다른 서버의 청소가 먼저 지웠다
        except OSError as error:
            log.warn(f"죽은 프로세스의 임시 폴더를 지우지 못했다: {path} ({error})")
            continue
        removed.append(path)
    if removed:
        log.info(f"죽은 프로세스가 남긴 임시 폴더 {len(removed)}개를 지웠다({directory})")
    return tuple(removed)


def _dead_candidates(directory: Path, limit: int) -> list[Path]:
    candidates: list[Path] = []
    try:
        entries = os.scandir(directory)
    except OSError as error:
        log.warn(f"임시 폴더를 훑지 못했다: {directory} ({error})")
        return candidates
    examined = 0
    with entries:
        for entry in entries:
            matched = _PID_SCRATCH_NAME.fullmatch(entry.name)
            if matched is None or not entry.is_dir(follow_symlinks=False):
                continue
            if examined >= limit:
                break
            examined += 1
            if not process_alive(int(matched.group(2))):
                candidates.append(Path(entry.path))
    return candidates
