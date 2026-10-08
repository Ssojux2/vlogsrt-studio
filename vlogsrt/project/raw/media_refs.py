"""Logical clips with read-only, measured original/proxy references.

Names select candidates only. A proxy is usable only after matching clocks, decoded
frames and (when present) audio. Existing source-only projects need no migration.
"""

from __future__ import annotations

import array
import hashlib
import json
import math
import shutil
from dataclasses import asdict, dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import preprocess
from vlogsrt.project.raw.models import (
    MediaNotFoundError,
    MediaTargetChangedError,
    RawWorkspaceError,
)
from vlogsrt.task_runtime import check_cancel


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


def validate_pair(original: Path, proxy: Path) -> PairValidation:
    """Conservatively reject any unproven correspondence; never rename/copy media."""
    try:
        left, right = preprocess.probe_raw_media(original), preprocess.probe_raw_media(proxy)
        a = preprocess.probe_extraction_clock(original, probe=left)
        b = preprocess.probe_extraction_clock(proxy, probe=right)
        if not a.uniform_grid or not b.uniform_grid:
            return PairValidation("rejected", "불균일 프레임 시간은 자동 연결하지 않습니다")
        if (
            a.frame_duration != b.frame_duration
            or a.frame_count != b.frame_count
            or abs(a.presentation_origin - b.presentation_origin) > a.frame_duration
        ):
            return PairValidation("rejected", "길이·시작 시각·프레임 시간이 일치하지 않습니다")
        duration = a.frame_duration * a.frame_count
        points = tuple(
            a.frame_duration * min(a.frame_count - 1, int((a.frame_count - 1) * p))
            for p in (0.05, 0.25, 0.5, 0.75, 0.95)
        )
        x, y = (
            preprocess.correspondence_samples(original, points),
            preprocess.correspondence_samples(proxy, points),
        )
        errors = [
            sum(abs(v - w) for v, w in zip(m, n, strict=True)) / len(m)
            for m, n in zip(x, y, strict=True)
            if len(m) == len(n) == 1024
        ]
        if len(errors) != len(points) or max(errors) > 10:
            return PairValidation("rejected", "대응 프레임이 일치하지 않습니다")
        # A low spatial difference alone cannot detect a temporally shifted proxy.
        # Compare nearby frames too; only the identity frame mapping is auto-linked.
        zero_error = sum(errors) / len(errors)
        for shift in (-3, -2, -1, 1, 2, 3):
            shifted_points = tuple(
                max(Fraction(0), min(duration - a.frame_duration, p + shift * a.frame_duration))
                for p in points
            )
            neighbors = preprocess.correspondence_samples(proxy, shifted_points)
            neighbor_error = sum(
                sum(abs(v - w) for v, w in zip(m, n, strict=True)) / len(m)
                for m, n in zip(x, neighbors, strict=True)
            ) / len(x)
            if neighbor_error + 0.1 < zero_error:
                return PairValidation(
                    "rejected", "프레임 대응 시각이 이동되어 자동 연결하지 않습니다"
                )
        if len(set(x)) == 1 and not left.audio:
            return PairValidation("rejected", "정지 영상만으로 시간 대응이 불명확합니다")
        audio_verified = not left.audio and not right.audio
        if left.audio or right.audio:
            if not left.audio or not right.audio:
                return PairValidation("rejected", "오디오 트랙 대응을 확인할 수 없습니다")
            if abs(left.audio[0].start_time - right.audio[0].start_time) > a.frame_duration:
                return PairValidation("rejected", "오디오 시작 시각이 다릅니다")
            audio_points = tuple(
                min(p, max(Fraction(0), duration - Fraction(1, 4))) for p in points
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
                    sum(v * w for v, w in zip(lm, rn, strict=True)) / energy
                    if energy
                    else float(lm == rn)
                )
            audio_verified = min(correlations) >= 0.98
            if not audio_verified:
                return PairValidation("rejected", "대응 오디오가 일치하지 않습니다")
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


def resolve(workspace: Any, asset: Any, purpose: str) -> Path:
    record = load(workspace, asset.asset_id)
    if record:
        state = record["validation"]["status"]
        if purpose == "export" and not record["original"]:
            raise RawWorkspaceError("원본 미연결: Final Cut 전달 전에 원본을 재연결하세요")
        if purpose in ("preview", "frames") and state in ("verified", "proxy_only"):
            try:
                return _unchanged(record["proxy"])
            except RawWorkspaceError:
                if not record["original"]:
                    raise
        if record["original"]:
            return _unchanged(record["original"])
    return workspace.source_media_path(asset)


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


def fingerprint(workspace: Any, asset_id: str) -> str:
    record = load(workspace, asset_id)
    if record is None:
        return "original-only/v1"
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
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode()).hexdigest()


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
    # exactly matching rate/count and a measured origin mapping.
    key = hashlib.sha256(
        (asset.content_sha256 + fingerprint(workspace, asset.asset_id)).encode()
    ).hexdigest()
    return _FrameSampler(
        path,
        preprocess.probe_raw_media(path).video.stream_index,
        workspace.root / "cache" / "source-frames" / key,
    )
