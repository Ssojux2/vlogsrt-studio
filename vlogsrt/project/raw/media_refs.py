"""Logical clips with read-only, measured original/proxy references.

Names select candidates only. A proxy is usable only after matching clocks, decoded
frames and (when present) audio. Existing source-only projects need no migration.
"""

from __future__ import annotations

import array
import contextlib
import fcntl
import hashlib
import json
import math
import shutil
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import log, preprocess
from vlogsrt.project.raw.analysis_config import (
    ANALYSIS_PROXY,
    PAIR_AUDIO_MIN_CORRELATION,
    PAIR_AUDIO_TAIL_SECONDS,
    PAIR_SHIFT_MARGIN,
    PAIR_VIDEO_MAX_ERROR,
    PAIR_VIDEO_SAMPLE_SIZE,
    AnalysisProxyRecipe,
)
from vlogsrt.project.raw.models import (
    MediaNotFoundError,
    MediaTargetChangedError,
    RawWorkspaceError,
)
from vlogsrt.task_runtime import check_cancel

ORIGINAL_ONLY_FINGERPRINT = "original-only/v1"
"""미디어 연결 기록이 없거나, 있어도 입력이 원본 바이트 그대로일 때의 정본 지문."""

_PROXY_UNUSED_STATUSES = ("original_only", "rejected")
"""프록시를 미리보기·프레임에 쓰지 않는 검증 상태 — `resolve`가 원본을 돌려주는 경우."""


@dataclass(frozen=True)
class PairValidation:
    status: str
    reason: str
    frame_duration: str = "0"
    source_origin: str = "0"
    proxy_origin: str = "0"
    offset_frames: int = 0
    audio_verified: bool = False
    max_frame_error: float = 0.0


def _facts(path: Path) -> dict[str, Any]:
    from vlogsrt.project.raw.fcp import _resolve_path_safely

    target, error, stat = _resolve_path_safely(path)
    if error or stat is None or target != path:
        raise RawWorkspaceError(error or "미디어 연결 대상이 바뀌었습니다")
    digest = hashlib.sha256()
    with target.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            check_cancel()
            digest.update(chunk)
    return {
        "path": str(path),
        "sha256": digest.hexdigest(),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "inode": stat.st_ino,
    }


def _unchanged(ref: dict) -> Path:
    from vlogsrt.project.raw.fcp import _resolve_path_safely

    path = Path(ref["path"])
    target, error, stat = _resolve_path_safely(path)
    if (
        error
        or stat is None
        or target != path
        or (stat.st_size, stat.st_mtime_ns, stat.st_ino)
        != (ref["size"], ref["mtime_ns"], ref["inode"])
    ):
        raise MediaTargetChangedError("연결된 미디어가 바뀌었습니다. 다시 연결하세요")
    return path


_VIDEO_SAMPLE_POINTS = (0.05, 0.25, 0.5, 0.75, 0.95)
_SHIFT_PROBES = (-3, -2, -1, 1, 2, 3)


def _sample_points(clock: Any) -> tuple[Fraction, ...]:
    return tuple(
        clock.frame_duration * min(clock.frame_count - 1, int((clock.frame_count - 1) * p))
        for p in _VIDEO_SAMPLE_POINTS
    )


def _lattice_rejection(a: Any, b: Any, exact_origin: bool) -> PairValidation | None:
    """격자·원점이 대응을 허락하는가 — 프레임을 하나도 디코드하기 전의 검사."""
    if not a.uniform_grid or not b.uniform_grid:
        return PairValidation("rejected", "불균일 프레임 시간은 자동 연결하지 않습니다")
    origin_gap = abs(a.presentation_origin - b.presentation_origin)
    if (
        a.frame_duration != b.frame_duration
        or a.frame_count != b.frame_count
        or origin_gap > (0 if exact_origin else a.frame_duration)
    ):
        return PairValidation("rejected", "길이·시작 시각·프레임 시간이 일치하지 않습니다")
    return None


def _mean_error(left: bytes, right: bytes) -> float:
    return sum(abs(v - w) for v, w in zip(left, right, strict=True)) / len(left)


def _shift_rejection(
    proxy: Path, a: Any, points: tuple[Fraction, ...], x: tuple[bytes, ...], errors: list[float]
) -> PairValidation | None:
    # A low spatial difference alone cannot detect a temporally shifted proxy.
    # Compare nearby frames too; only the identity frame mapping is auto-linked.
    duration = a.frame_duration * a.frame_count
    zero_error = sum(errors) / len(errors)
    for shift in _SHIFT_PROBES:
        shifted_points = tuple(
            max(Fraction(0), min(duration - a.frame_duration, p + shift * a.frame_duration))
            for p in points
        )
        neighbors = preprocess.correspondence_samples(proxy, shifted_points)
        neighbor_error = sum(_mean_error(m, n) for m, n in zip(x, neighbors, strict=True)) / len(x)
        if neighbor_error + PAIR_SHIFT_MARGIN < zero_error:
            return PairValidation("rejected", "프레임 대응 시각이 이동되어 자동 연결하지 않습니다")
    return None


def _video_pair_check(
    original: Path,
    proxy: Path,
    a: Any,
    b: Any,
    *,
    exact_origin: bool,
) -> tuple[PairValidation | None, tuple[bytes, ...], list[float]]:
    """격자·원점·대응 프레임·이동 여부 — 짝 검증과 분석 프록시 검증이 함께 쓰는 영상 절반.

    거절 사유가 있으면 첫 값에 담아 돌려준다. `exact_origin`은 분석 프록시용 엄격 모드다:
    장면 컷 시각을 원본 원점으로 환산하므로 프레임 한 칸의 어긋남도 허용하지 않는다.
    """
    rejection = _lattice_rejection(a, b, exact_origin)
    if rejection is not None:
        return rejection, (), []
    points = _sample_points(a)
    x, y = (
        preprocess.correspondence_samples(original, points),
        preprocess.correspondence_samples(proxy, points),
    )
    errors = [
        _mean_error(m, n)
        for m, n in zip(x, y, strict=True)
        if len(m) == len(n) == PAIR_VIDEO_SAMPLE_SIZE
    ]
    if len(errors) != len(points) or max(errors) > PAIR_VIDEO_MAX_ERROR:
        return PairValidation("rejected", "대응 프레임이 일치하지 않습니다"), x, errors
    return _shift_rejection(proxy, a, points, x, errors), x, errors


def _audio_rejection(
    original: Path, proxy: Path, left: Any, right: Any, a: Any, points: tuple[Fraction, ...]
) -> PairValidation | None:
    """짝 검증의 소리 절반 — 둘 다 소리가 있을 때 시작 시각과 대응 표본 상관을 본다."""
    if not left.audio or not right.audio:
        return PairValidation("rejected", "오디오 트랙 대응을 확인할 수 없습니다")
    if abs(left.audio[0].start_time - right.audio[0].start_time) > a.frame_duration:
        return PairValidation("rejected", "오디오 시작 시각이 다릅니다")
    duration = a.frame_duration * a.frame_count
    audio_points = tuple(
        min(p, max(Fraction(0), duration - PAIR_AUDIO_TAIL_SECONDS)) for p in points
    )
    la = preprocess.correspondence_samples(original, audio_points, audio=True)
    ra = preprocess.correspondence_samples(proxy, audio_points, audio=True)
    correlations = []
    for m, n in zip(la, ra, strict=True):
        lm, rn = array.array("h", m), array.array("h", n)
        if len(lm) != len(rn) or not lm:
            return PairValidation("rejected", "오디오 표본 길이가 다릅니다")
        energy = math.sqrt(sum(v * v for v in lm) * sum(v * v for v in rn))
        correlations.append(
            sum(v * w for v, w in zip(lm, rn, strict=True)) / energy if energy else float(lm == rn)
        )
    if not min(correlations) >= PAIR_AUDIO_MIN_CORRELATION:
        return PairValidation("rejected", "대응 오디오가 일치하지 않습니다")
    return None


def validate_pair(original: Path, proxy: Path) -> PairValidation:
    """Conservatively reject any unproven correspondence; never rename/copy media."""
    try:
        left, right = preprocess.probe_raw_media(original), preprocess.probe_raw_media(proxy)
        a = preprocess.probe_extraction_clock(original, probe=left)
        b = preprocess.probe_extraction_clock(proxy, probe=right)
        rejection, x, errors = _video_pair_check(original, proxy, a, b, exact_origin=False)
        if rejection is not None:
            return rejection
        if len(set(x)) == 1 and not left.audio:
            return PairValidation("rejected", "정지 영상만으로 시간 대응이 불명확합니다")
        audio_verified = not left.audio and not right.audio
        if left.audio or right.audio:
            rejection = _audio_rejection(original, proxy, left, right, a, _sample_points(a))
            if rejection is not None:
                return rejection
            audio_verified = True
        return PairValidation(
            "verified",
            "시간·프레임·오디오 대응 확인",
            str(a.frame_duration),
            str(a.presentation_origin),
            str(b.presentation_origin),
            0,
            audio_verified,
            max(errors),
        )
    except (OSError, RuntimeError, ValueError) as error:
        return PairValidation("rejected", f"짝 확인 실패: {type(error).__name__}")


def _save(workspace: Any, asset_id: str, record: dict) -> None:
    with workspace.connect() as connection:
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("media_refs:" + asset_id, json.dumps(record, ensure_ascii=False)),
        )
        connection.commit()


def load(workspace: Any, asset_id: str) -> dict | None:
    with workspace.connect() as connection:
        row = connection.execute(
            "SELECT value FROM meta WHERE key=?", ("media_refs:" + asset_id,)
        ).fetchone()
    return json.loads(row[0]) if row else None


def connect(workspace: Any, asset_id: str, *, original: Path | None, proxy: Path | None) -> dict:
    record: dict[str, Any] = {
        "original": _facts(original) if original else None,
        "proxy": _facts(proxy) if proxy else None,
        "validation": asdict(PairValidation("original_only", "프록시 없음")),
    }
    if original and proxy:
        record["validation"] = asdict(validate_pair(original, proxy))
    elif proxy:
        record["validation"] = asdict(
            PairValidation("proxy_only", "원본 미연결 · 검토 가능, 원본 출력 전 재연결 필요")
        )
    if not original and not proxy:
        raise RawWorkspaceError("연결할 미디어가 없습니다")
    _save(workspace, asset_id, record)
    return record


def _resolve_media(workspace: Any, asset: Any, purpose: str) -> tuple[Path, bool]:
    """(경로, FCP 프록시인가) — 분석 프록시는 여기서 모른다."""
    record = load(workspace, asset.asset_id)
    if record:
        state = record["validation"]["status"]
        if purpose == "export" and not record["original"]:
            raise RawWorkspaceError("원본 미연결: Final Cut 전달 전에 원본을 재연결하세요")
        if purpose in ("preview", "frames") and state in ("verified", "proxy_only"):
            try:
                return _unchanged(record["proxy"]), True
            except RawWorkspaceError:
                if not record["original"]:
                    raise
        if record["original"]:
            return _unchanged(record["original"]), False
    return workspace.source_media_path(asset), False


def resolve(workspace: Any, asset: Any, purpose: str) -> Path:
    path, is_fcp_proxy = _resolve_media(workspace, asset, purpose)
    if purpose != "frames" or is_fcp_proxy:
        return path
    # 사용할 수 있는 FCP 프록시가 없다 — 원본을 4K 그대로 프레임마다 디코드하지 않고,
    # 한 번 만든 저해상도 분석 프록시를 쓴다. 못 만들면 원본이다(경고는 거기서 남긴다).
    return ensure_analysis_proxy(workspace, asset, path) or path


def bind_library(workspace: Any, scan: Any, asset_ids: tuple[str, ...]) -> None:
    for asset_id in asset_ids:
        asset = workspace.source_asset(asset_id)
        path = str(Path(asset.source_root) / asset.source_relative_path)
        matches = [
            c for c in scan.clips if any(f and f.media_path == path for f in (c.original, c.proxy))
        ]
        if len(matches) != 1:
            continue
        clip = matches[0]
        connect(
            workspace,
            asset_id,
            original=Path(clip.original.media_path) if clip.original else None,
            proxy=Path(clip.proxy.media_path) if clip.proxy else None,
        )


def register_direct(workspace: Any, asset_id: str, path: Path) -> str:
    """Only an already connected, unchanged video may get a browser media ID."""
    asset = workspace.source_asset(asset_id)
    if resolve(workspace, asset, "preview") != path:
        raise RawWorkspaceError("현재 검증된 미디어만 등록할 수 있습니다")
    record = load(workspace, asset_id)
    if not record or record["validation"]["status"] not in ("verified", "proxy_only"):
        raise RawWorkspaceError("검증된 프록시가 아닙니다")
    key = "proxy_" + hashlib.sha256((asset_id + str(path)).encode()).hexdigest()[:24]
    with workspace.connect() as connection:
        connection.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps({"asset_id": asset_id, "reference": _facts(path)})),
        )
        connection.commit()
    return key


def resolve_direct(workspace: Any, file_id: str) -> tuple[Path, int, str]:
    with workspace.connect() as connection:
        row = connection.execute("SELECT value FROM meta WHERE key=?", (file_id,)).fetchone()
    if row is None:
        raise MediaNotFoundError("등록되지 않은 프록시입니다")
    payload = json.loads(row[0])
    path = _unchanged(payload["reference"])
    if resolve(workspace, workspace.source_asset(payload["asset_id"]), "preview") != path:
        raise RawWorkspaceError("프록시 연결이 바뀌었습니다")
    return path, payload["reference"]["size"], "preview"


def _uses_source_bytes(workspace: Any, asset_id: str, record: dict) -> bool:
    """브라우저·분석이 받는 입력이 원본 자산의 바이트 그대로인가.

    프록시를 안 쓰는 기록이고 연결된 원본 해시가 자산의 내용 해시와 같으면, 기록이
    새로 생겨도 인코딩 입력은 같다 — 지문을 바꾸면 이미 만든 미리보기와 저장된 분석을
    까닭 없이 버리게 된다.
    """
    original = record.get("original")
    if not original or record["validation"]["status"] not in _PROXY_UNUSED_STATUSES:
        return False
    return original["sha256"] == workspace.source_asset(asset_id).content_sha256


def fingerprint(workspace: Any, asset_id: str) -> str:
    record = load(workspace, asset_id)
    if record is None:
        return ORIGINAL_ONLY_FINGERPRINT
    if record.get("original"):
        _unchanged(record["original"])
    if record.get("proxy") and record["validation"]["status"] != "rejected":
        try:
            _unchanged(record["proxy"])
        except RawWorkspaceError:
            if not record.get("original"):
                raise
            record["validation"] = asdict(
                PairValidation("rejected", "프록시가 바뀌어 원본을 사용합니다")
            )
            _save(workspace, asset_id, record)
    if _uses_source_bytes(workspace, asset_id, record):
        return ORIGINAL_ONLY_FINGERPRINT
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


_PROXY_FILE = "proxy.mp4"
_PROXY_RECORD = "record.json"
_PROXY_LOCK = "build.lock"
_BUILD_LOCKS: dict[str, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()
_FAILED: dict[str, str] = {}
"""이 프로세스에서 만들기·검증에 실패한 분석 프록시 폴더 → 사유. 디스크에는 남기지 않는다."""
_LOCK_POLL_SECONDS = 0.2


def _source_sha(workspace: Any, asset: Any, original: Path) -> str:
    """분석 프록시의 키가 되는 원본 해시 — 연결된 원본이 자산과 다르면 그 파일의 해시다."""
    record = load(workspace, asset.asset_id)
    original_ref = record.get("original") if record else None
    if original_ref and Path(original_ref["path"]) == original:
        return str(original_ref["sha256"])
    return str(asset.content_sha256 or "")


def _proxy_directory(workspace: Any, source_sha: str, recipe: AnalysisProxyRecipe) -> Path:
    key = hashlib.sha256(f"{source_sha}\0{recipe.tag()}".encode()).hexdigest()
    return workspace.root / "cache" / "analysis-proxy" / key


def _read_proxy_state(directory: Path, recipe: AnalysisProxyRecipe) -> dict | None:
    try:
        state = json.loads((directory / _PROXY_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if state.get("recipe") == recipe.tag() else None


def _write_proxy_state(directory: Path, recipe: AnalysisProxyRecipe, **fields: Any) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    staged = directory / (_PROXY_RECORD + ".tmp")
    staged.write_text(
        json.dumps({"recipe": recipe.tag(), **fields}, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )
    staged.replace(directory / _PROXY_RECORD)


def _proxy_intact(directory: Path, state: dict) -> bool:
    """게시된 파일이 기록한 크기·시각 그대로인가 — 아니면 다시 만든다."""
    try:
        stat = (directory / _PROXY_FILE).stat()
    except OSError:
        return False
    return (stat.st_size, stat.st_mtime_ns) == (state.get("size"), state.get("mtime_ns"))


def _ready(directory: Path, recipe: AnalysisProxyRecipe) -> bool:
    state = _read_proxy_state(directory, recipe)
    return bool(state and state.get("state") == "ready" and _proxy_intact(directory, state))


def _fall_back(directory: Path, original: Path, reason: str) -> None:
    _FAILED[str(directory)] = reason
    log.warn(
        f"분석 프록시를 쓰지 않고 원본으로 분석합니다(느릴 수 있음): {original.name} — {reason}"
    )


def validate_analysis_proxy(original: Path, proxy: Path) -> PairValidation:
    """분석 프록시가 원본의 프레임 격자를 그대로 가졌는지 — 영상 절반만, 원점은 정확히.

    소리는 프록시에 없고(분석 음성은 원본에서 따로 뽑는다) 정지 영상이어도 개수·길이·
    원점의 정확한 일치가 증거라 짝 검증의 두 조항을 뺀다.
    """
    try:
        a = preprocess.probe_extraction_clock(original)
        b = preprocess.probe_extraction_clock(proxy)
        rejection, _, errors = _video_pair_check(original, proxy, a, b, exact_origin=True)
    except (OSError, RuntimeError, ValueError) as error:
        return PairValidation("rejected", f"분석 프록시 확인 실패: {type(error).__name__}: {error}")
    if rejection is not None:
        return rejection
    return PairValidation(
        "verified",
        "프레임 격자·원점 일치",
        str(a.frame_duration),
        str(a.presentation_origin),
        str(b.presentation_origin),
        0,
        False,
        max(errors),
    )


def _build_lock(directory: Path) -> threading.Lock:
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault(str(directory), threading.Lock())


def _acquire(lock: threading.Lock) -> None:
    """같은 프록시를 두 작업이 동시에 만들지 않는다 — 기다리는 동안에도 취소를 듣는다."""
    while not lock.acquire(timeout=_LOCK_POLL_SECONDS):
        check_cancel()


def _acquire_file(handle: Any) -> None:
    """다른 프로세스(CLI와 앱)가 같은 폴더에서 만드는 중이면 기다린다 — 취소는 듣는다."""
    while True:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            check_cancel()
            time.sleep(_LOCK_POLL_SECONDS)


@contextlib.contextmanager
def _exclusive(directory: Path) -> Iterator[None]:
    """스레드 잠금과 파일 잠금을 함께 쥔다 — 파일은 닫히면서 풀린다."""
    lock = _build_lock(directory)
    _acquire(lock)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with (directory / _PROXY_LOCK).open("a", encoding="utf-8") as handle:
            _acquire_file(handle)
            yield
    finally:
        lock.release()


def _build_and_verify(
    directory: Path, original: Path, recipe: AnalysisProxyRecipe
) -> tuple[bool, str]:
    """만들고 검증한 결과 — 실패는 (False, 사유)로, 취소 예외는 그대로 올라간다.

    두 잠금 안에서, 남은 파일을 잔재로 치운 뒤에만 불린다(`_clear_leftover`). 만들기는
    이미 있는 대상을 덮어쓰지 않는다.
    """
    target = directory / _PROXY_FILE
    started = time.monotonic()
    try:
        preprocess.build_analysis_proxy(
            original,
            target,
            scale=recipe.scale,
            video_codec=recipe.video_codec,
            preset=recipe.preset,
            crf=recipe.crf,
            gop=recipe.gop,
            pix_fmt=recipe.pix_fmt,
        )
    except (OSError, RuntimeError, ValueError) as error:
        return False, f"{type(error).__name__}: {error}"
    verdict = validate_analysis_proxy(original, target)
    if verdict.status != "verified":
        target.unlink(missing_ok=True)
        return False, verdict.reason
    log.info(f"분석 프록시 준비: {original.name} ({time.monotonic() - started:.1f}초)")
    return True, verdict.reason


def _clear_leftover(directory: Path, recipe: AnalysisProxyRecipe) -> None:
    """준비된 기록이 없거나 파일이 기록과 다를 때만 남은 파일을 지운다.

    준비되고 온전한 프록시는 다른 프로세스가 지금 읽고 있을 수 있다 — 그것은 절대 지우지 않는다.
    """
    state = _read_proxy_state(directory, recipe)
    if state and state.get("state") == "ready" and _proxy_intact(directory, state):
        return
    (directory / _PROXY_FILE).unlink(missing_ok=True)


def ensure_analysis_proxy(workspace: Any, asset: Any, original: Path) -> Path | None:
    """원본의 25% 분석 프록시 — 없으면 한 번 만들고 검증한다. 못 쓰면 None(원본으로 간다).

    성공(`ready`)만 디스크에 남는다. 실패는 이 프로세스 안에서만 기억한다 — 한 실행이
    같은 실패를 되풀이하지 않게 하되, 디스크 부족·강제 종료 같은 일시 실패가 그 원본을
    영원히 원본 디코드에 묶지 않도록 다음 실행에서 다시 시도한다.
    """
    recipe = ANALYSIS_PROXY
    directory = _proxy_directory(workspace, _source_sha(workspace, asset, original), recipe)
    with _exclusive(directory):
        # 실패를 준비보다 먼저 본다 — 한 실행 안에서 프레임 출처(와 캐시 키)가 뒤집히지 않게.
        if str(directory) in _FAILED:
            return None
        if _ready(directory, recipe):
            return directory / _PROXY_FILE
        _clear_leftover(directory, recipe)
        ok, reason = _build_and_verify(directory, original, recipe)
        if not ok:
            _fall_back(directory, original, reason)
            return None
        stat = (directory / _PROXY_FILE).stat()
        _write_proxy_state(
            directory, recipe, state="ready", size=stat.st_size, mtime_ns=stat.st_mtime_ns
        )
        return directory / _PROXY_FILE


def _frames_original(workspace: Any, asset: Any) -> Path | None:
    """프레임이 원본에서 나오는가 — 그렇다면 그 원본 경로, FCP 프록시를 쓰면 None."""
    path, is_fcp_proxy = _resolve_media(workspace, asset, "frames")
    return None if is_fcp_proxy else path


def frames_fingerprint(workspace: Any, asset_id: str) -> str:
    """프레임을 디코드하는 바이트의 지문 — 프레임·장면 캐시처럼 디코드에서 나온 로컬 캐시 전용.

    분석 프록시에서 디코드한 프레임은 원본 프레임과 다른 바이트라 조리법을 얹어 그 캐시를
    가른다. 저장된 분석의 재사용·견적·과금 키는 `fingerprint`다 — 거기에 조리법을 실으면
    프록시 이전에 끝낸(돈을 낸) 분석이 낡아 보여 다시 청구된다(D47). 이 프로세스에서
    만들기에 실패해 원본으로 가는 중이면 `fingerprint` 그대로다 — `resolve` 뒤에 읽는다.
    """
    base = fingerprint(workspace, asset_id)
    asset = workspace.source_asset(asset_id)
    original = _frames_original(workspace, asset)
    if original is None:
        return base
    directory = _proxy_directory(workspace, _source_sha(workspace, asset, original), ANALYSIS_PROXY)
    if str(directory) in _FAILED:
        return base
    return f"{base}+{ANALYSIS_PROXY.tag()}"


def frames_decode_source(
    workspace: Any, asset: Any, recorded: str | None = None
) -> tuple[Path, str]:
    """프레임을 디코드할 경로와 그 출처의 `frames_fingerprint`.

    `recorded`는 앞선 단계(1단계 기록)가 실제로 디코드한 출처다. 그것이 분석 프록시를 거치지
    않은 출처(`fingerprint` 그대로 — 프록시 이전이거나 그 실행에서 만들기에 실패했다)면 지금
    프록시가 있어도 같은 출처를 다시 읽는다. 장면 컷이 출처에 따라 달라지면 2단계의 실측 지문이
    1단계와 어긋나 유료 1단계를 다시 돌려야 하기 때문이다.
    """
    base = fingerprint(workspace, asset.asset_id)
    if recorded is not None and recorded == base:
        return _resolve_media(workspace, asset, "frames")[0], base
    path = resolve(workspace, asset, "frames")
    return path, frames_fingerprint(workspace, asset.asset_id)


def relink_original(workspace: Any, asset_id: str, original: Path) -> dict:
    record = load(workspace, asset_id)
    if not record or not record.get("proxy"):
        raise RawWorkspaceError("재연결할 프록시가 없습니다")
    proxy = _unchanged(record["proxy"])
    result = validate_pair(original, proxy)
    if result.status != "verified":
        raise RawWorkspaceError(result.reason)
    return connect(workspace, asset_id, original=original, proxy=proxy)


@dataclass(frozen=True)
class _FrameSampler:
    source: Path
    stream_index: int
    cache_directory: Path
    batch_attempts: dict[tuple[int, int], str] = field(default_factory=dict, compare=False)

    def _cached(self, index: int, edge: int) -> Path | None:
        cached = self.cache_directory / f"{self.stream_index}-{index}-{edge}.jpg"
        manifest = cached.with_suffix(".sha256")
        if (
            cached.is_file()
            and manifest.is_file()
            and hashlib.sha256(cached.read_bytes()).hexdigest() == manifest.read_text()
        ):
            return cached
        return None

    def _cache(self, index: int, edge: int, path: Path) -> None:
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        cached = self.cache_directory / f"{self.stream_index}-{index}-{edge}.jpg"
        staged = cached.with_suffix(".tmp")
        shutil.copyfile(path, staged)
        staged.replace(cached)
        manifest = cached.with_suffix(".sha256")
        staged_manifest = manifest.with_suffix(".tmp-hash")
        staged_manifest.write_text(hashlib.sha256(path.read_bytes()).hexdigest())
        staged_manifest.replace(manifest)

    def prepare(self, *, frame_indices: tuple[int, ...], directory: Path, long_edge: int) -> None:
        missing = []
        for index in sorted(set(frame_indices)):
            cached = self._cached(index, long_edge)
            if cached is not None:
                shutil.copyfile(cached, directory / f"frame_{index:08d}.jpg")
            else:
                missing.append(index)
                self.batch_attempts[(index, long_edge)] = "묶음 추출에서 프레임을 얻지 못했습니다"
        if not missing:
            return
        try:
            preprocess.extract_frame_indices(
                self.source,
                self.stream_index,
                missing,
                directory,
                long_edge=long_edge,
                on_frame=lambda index, path: self._cache(index, long_edge, path),
            )
        except Exception as error:
            for index in missing:
                self.batch_attempts[(index, long_edge)] = str(error)
            raise

    def sample(
        self, *, stream_index: int, frame_index: int, destination: Path, long_edge: int
    ) -> Path:
        cached = self._cached(frame_index, long_edge)
        if cached is not None:
            if cached != destination:
                shutil.copyfile(cached, destination)
            return destination
        if (frame_index, long_edge) in self.batch_attempts:
            raise RuntimeError(self.batch_attempts[(frame_index, long_edge)])
        path = preprocess.extract_frame_index(
            self.source, self.stream_index, frame_index, destination, long_edge=long_edge
        )
        self._cache(frame_index, long_edge, path)
        return path


def frame_sampler(workspace: Any, asset: Any) -> Any:
    path = resolve(workspace, asset, "frames")
    # Candidate frames remain on the source frame lattice; validated pairs have
    # exactly matching rate/count and a measured origin mapping. 분석 프록시의 프레임은
    # 다른 바이트라 지문(조리법 포함)이 캐시 폴더를 가른다 — resolve가 먼저 정한 뒤에 읽는다.
    key = hashlib.sha256(
        (asset.content_sha256 + frames_fingerprint(workspace, asset.asset_id)).encode()
    ).hexdigest()
    return _FrameSampler(
        path,
        preprocess.probe_raw_media(path).video.stream_index,
        workspace.root / "cache" / "source-frames" / key,
    )
