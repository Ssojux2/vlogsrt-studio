"""등급의 입력과 보관 — 저장 PCM의 공백 RMS, 저장 프레임의 화질, 보고서 곁파일(W-11, D51).

`tiering.py`를 순수하게 두려고 파일 읽기·쓰기를 여기로 뗐다. 새로 묻는 것은 없다 —
이미 저장된 `analysis.pcm`과 `frames/*.jpg`만 읽고, ffmpeg는 `preprocess`를 거친다(규칙 7).
쓰는 것은 곁파일 둘뿐이다(`frames/quality.json`·`tier_report.json`). 분석 기록
(`analysis_transcript.json`·`analysis_record.json`)은 절대 고치지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from array import array
from collections.abc import Mapping
from dataclasses import asdict
from fractions import Fraction
from pathlib import Path
from typing import Any

from vlogsrt import log, preprocess
from vlogsrt.project.raw import tiering
from vlogsrt.project.raw.analysis_config import PCM_FULL_SCALE, TIER_SETTINGS, RawTierSettings

QUALITY_NAME = "quality.json"
QUALITY_VERSION = "frame-quality/v1"
REPORT_NAME = "tier_report.json"
_SAMPLE_BYTES = 2


def _rms_dbfs(raw: bytes) -> float:
    """s16le 교차 표본 전체(모든 채널)의 RMS — 한 단계(1 LSB)를 바닥으로 둔다.

    디지털 무음의 −∞는 JSON에 실을 수 없고 비교에서도 뜻이 같다(어떤 기준보다 조용하다).
    """
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 20 * math.log10(1 / PCM_FULL_SCALE)
    mean_square = sum(value * value for value in samples) / len(samples)
    return 20 * math.log10(max(math.sqrt(mean_square), 1.0) / PCM_FULL_SCALE)


def measure_gap_levels(
    document: Mapping[str, Any],
    pcm_path: Path,
    *,
    audio_start: Fraction,
    settings: RawTierSettings = TIER_SETTINGS,
) -> dict[tuple[Fraction, Fraction], float]:
    """긴 발화 공백마다 저장 PCM의 RMS dBFS — PCM이 없거나 값이 이상하면 빈 사전이다."""
    audio = document.get("audio")
    if not pcm_path.is_file() or not isinstance(audio, Mapping):
        return {}
    channels, rate = int(audio.get("channels", 0)), int(audio.get("sample_rate", 0))
    if channels < 1 or rate < 1 or audio_start < 0:
        return {}
    levels: dict[tuple[Fraction, Fraction], float] = {}
    with pcm_path.open("rb") as handle:
        for start, end in tiering.quiet_gap_candidates(document, settings, audio_start=audio_start):
            first = math.ceil((start - audio_start) * rate)
            last = math.floor((end - audio_start) * rate)
            if first < 0 or last <= first:
                continue
            handle.seek(first * channels * _SAMPLE_BYTES)
            raw = handle.read((last - first) * channels * _SAMPLE_BYTES)
            if len(raw) == (last - first) * channels * _SAMPLE_BYTES:
                levels[(start, end)] = _rms_dbfs(raw)
    return levels


# ── 프레임 화질 곁파일 ─────────────────────────────────────────────────────


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    """곁파일을 원자적으로 쓴다 — 화면 그리기와 정리 작업이 동시에 써도 반쪽이 안 보인다."""
    body = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, prefix=path.name + ".", delete=False
    ) as handle:
        handle.write(body)
    try:
        os.replace(handle.name, path)
    finally:
        Path(handle.name).unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, object] | None:
    """곁파일 읽기 — 없거나 깨졌으면 None(다시 계산하면 된다; 원본 기록이 아니다)."""
    try:
        body = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None
    return body if isinstance(body, dict) else None


def _stored_frames(document: Mapping[str, Any]) -> dict[str, str]:
    section = document.get("frames")
    rows = section.get("frames", []) if isinstance(section, Mapping) else []
    return {
        str(row["frame_id"]): str(row.get("image_sha256", ""))
        for row in rows or []
        if isinstance(row, Mapping) and "frame_id" in row
    }


def _measure_frame(path: Path) -> dict[str, Any]:
    """한 장 측정 — 실패는 값으로 남긴다(그 장은 Luna 판정만으로 본다)."""
    try:
        quality = preprocess.frame_quality_stats((path,))[0]
    except RuntimeError as error:
        log.warn(f"프레임 화질 측정 실패 {path.name}: {error} — 이 장은 Luna 판정만으로 본다")
        return {"error": str(error)}
    return {"yavg": quality.yavg, "blur": quality.blur}


def frame_quality(source_dir: Path, document: Mapping[str, Any]) -> dict[str, dict[str, float]]:
    """저장 프레임의 YAVG·흐림 — `frames/quality.json`에 재사용 캐시로 남긴다.

    이미 잰 장은 이미지 해시가 같으면 다시 재지 않는다. 결과 사전에는 잰 값만 실린다.
    """
    frames_dir = source_dir / "frames"
    stored = _stored_frames(document)
    cache_path = frames_dir / QUALITY_NAME
    cached = _read_json(cache_path) or {}
    old = cached.get("frames", {}) if cached.get("version") == QUALITY_VERSION else {}
    rows: dict[str, dict[str, Any]] = {}
    for frame_id, digest in sorted(stored.items()):
        image = frames_dir / f"{frame_id}.jpg"
        previous = old.get(frame_id) if isinstance(old, Mapping) else None
        if isinstance(previous, Mapping) and previous.get("image_sha256") == digest:
            rows[frame_id] = dict(previous)
        elif image.is_file():
            rows[frame_id] = {**_measure_frame(image), "image_sha256": digest}
    if rows and rows != old:
        _write_json_atomic(
            cache_path,
            {
                "version": QUALITY_VERSION,
                "frames": rows,
                "windows": _window_summary(document, rows),
            },
        )
    return {
        frame_id: {"yavg": float(row["yavg"]), "blur": float(row.get("blur") or 0.0)}
        for frame_id, row in rows.items()
        if "yavg" in row
    }


def _window_summary(
    document: Mapping[str, Any], rows: Mapping[str, Mapping[str, Any]]
) -> dict[str, dict[str, object]]:
    """사람이 읽는 창별 요약 — 판정은 장별 값으로 다시 한다(기준이 바뀌어도 재측정 없음)."""
    summary: dict[str, dict[str, object]] = {}
    for window in tiering.window_rows(document):
        measured = [rows[f] for f in window.get("frame_ids", []) or [] if "yavg" in rows.get(f, {})]
        if measured:
            summary[str(window["window_id"])] = {
                "frames": len(measured),
                "yavg_min": min(float(r["yavg"]) for r in measured),
                "blur_max": max(float(r.get("blur") or 0.0) for r in measured),
            }
    return summary


# ── 보고서 ───────────────────────────────────────────────────────────────


def _rational_json(value: Fraction) -> dict[str, int]:
    return {"num": value.numerator, "den": value.denominator}


def _cache_matches(
    cached: Mapping[str, Any] | None, settings: RawTierSettings, sha: str, audio_start: Fraction
) -> bool:
    """판정판·설정·문서·PCM 원점이 모두 같아야 보관본을 쓴다 — 원점이 다르면 공백이 달라진다."""
    return (
        cached is not None
        and cached.get("tier_version") == settings.tier_version
        and cached.get("settings") == asdict(settings)
        and cached.get("document_sha256") == sha
        and cached.get("audio_start") == _rational_json(audio_start)
    )


def tier_report(
    document_path: Path,
    *,
    audio_start: Fraction,
    settings: RawTierSettings = TIER_SETTINGS,
    compute: bool = True,
) -> tiering.TierReport | None:
    """완성 문서의 등급 보고서 — 판정판·설정·문서 해시·PCM 원점이 같으면 곁파일을 그대로 쓴다.

    `compute=False`는 화면 그리기의 길이다 — 보관본이 없으면 재지 않고 None을 돌려준다
    (첫 측정은 PCM·프레임 수백 장이라 수십 초다).
    """
    body = document_path.read_bytes()
    sha = hashlib.sha256(body).hexdigest()
    source_dir = document_path.parent
    cache_path = source_dir / REPORT_NAME
    cached = _read_json(cache_path)
    if cached is not None and _cache_matches(cached, settings, sha, audio_start):
        return tiering.report_from_document(cached)
    if not compute:
        return None
    document = json.loads(body)
    pcm = source_dir / "analysis.pcm"
    report = tiering.classify(
        document,
        gap_levels=(
            measure_gap_levels(document, pcm, audio_start=audio_start, settings=settings)
            if pcm.is_file()
            else None
        ),
        local_video=frame_quality(source_dir, document),
        settings=settings,
        audio_start=audio_start,
        document_sha256=sha,
    )
    _write_json_atomic(
        cache_path, {**report.document(), "audio_start": _rational_json(audio_start)}
    )
    return report


def progress_report(
    progress: Mapping[str, Any], settings: RawTierSettings = TIER_SETTINGS
) -> tiering.TierReport:
    """Luna가 도는 동안의 등급 — 소리를 아직 재지 않으므로 소리 쪽 빨강이 없다(곁파일 없음)."""
    return tiering.classify(progress, gap_levels={}, settings=settings)
