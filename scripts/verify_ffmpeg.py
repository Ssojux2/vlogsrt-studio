#!/usr/bin/env python3
"""FFmpeg 9.0.1 LGPL 공급 계약 검증 — 다운로드 없이, 실패는 닫힌다.

`presets/ffmpeg-manifest.json`이 계약의 정본이고 **ffmpeg·ffprobe 두 바이너리**가
계약 대상이다(엔진의 `require_ffmpeg`가 PATH에서 둘을 다 찾는다). 하나라도
계약과 다르면(없음·아키텍처·버전·라이선스·sha256) 1로 끝난다. 이 검사를 통과한
빌드만 번들 엔진을 실을 수 있다(build_engine.py가 같은 계약을 다시 본다).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "presets" / "ffmpeg-manifest.json"
REQUIRED_BINARIES = ("ffmpeg", "ffprobe")

# Mach-O arm64 매직 (CPU_TYPE_ARM64 = 0x0100000C).
_MACHO_MAGICS = {b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca"}
_CPU_TYPE_ARM64 = 0x0100000C


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args(argv)

    problems = verify()
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if problems:
        return 1
    print("OK FFmpeg·ffprobe 공급이 계약과 일치한다")
    return 0


def verify(
    ffmpeg: Path | None = None, ffprobe: Path | None = None, contract: dict | None = None
) -> list[str]:
    """바이너리·계약 오버라이드는 시험용이다 — 기본은 저장소의 정본을 읽는다."""
    if contract is None:
        if not MANIFEST.is_file():
            return [f"계약 파일이 없다: {MANIFEST.relative_to(ROOT)}"]
        contract = json.loads(MANIFEST.read_text(encoding="utf-8"))
    problems: list[str] = []
    for key in ("version", "arch", "license"):
        if not contract.get(key):
            problems.append(f"계약의 {key}가 비어 있다 — 사람이 기입해야 한다")
    binaries = contract.get("binaries")
    if not isinstance(binaries, dict) or set(binaries) != set(REQUIRED_BINARIES):
        problems.append(f"계약의 binaries는 {list(REQUIRED_BINARIES)}를 정확히 담아야 한다")
        return problems
    for name in REQUIRED_BINARIES:
        entry = binaries.get(name)
        if not isinstance(entry, dict) or not entry.get("resource_path"):
            problems.append(f"계약의 binaries.{name}.resource_path가 비어 있다")
        elif not entry.get("sha256"):
            problems.append(f"계약의 binaries.{name}.sha256가 비어 있다 — 사람이 기입해야 한다")
    if problems:
        return problems

    for name in REQUIRED_BINARIES:
        override = ffmpeg if name == "ffmpeg" else ffprobe
        path = override or (ROOT / binaries[name]["resource_path"])
        # 조기 반환하지 않는다 — 한쪽만 빠진 공급이 반쪽 성공으로 읽히면 안 된다.
        problems += _verify_binary(
            path,
            name=name,
            version=str(contract["version"]),
            sha256=str(binaries[name]["sha256"]),
        )
    # 라이선스 표기는 ffmpeg 본체에서만 본다(ffprobe는 같은 빌드에서 나온다).
    ffmpeg_path = ffmpeg or (ROOT / binaries["ffmpeg"]["resource_path"])
    report = _version_report(ffmpeg_path)
    if str(contract["license"]).lower() not in report.lower():
        problems.append(f"라이선스 표기에 {contract['license']}가 없다")
    return problems


def _verify_binary(path: Path, *, name: str, version: str, sha256: str) -> list[str]:
    if not path.is_file():
        return [f"{name} 바이너리가 없다: {path}"]
    problems = _check_arm64_macho(path, name=name)
    if problems:
        # Mach-O가 아니면 실행도 하지 않는다 — 깨진 파일을 돌리지 않는다.
        return problems
    report = _version_report(path)
    if f"{name} version {version}" not in report:
        problems.append(f"{name} 버전이 {version}이 아니다: {report.splitlines()[:1]}")
    digest = _sha256(path)
    if digest != sha256:
        problems.append(f"{name} sha256이 다르다: {digest} ≠ {sha256}")
    return problems


def _version_report(path: Path) -> str:
    try:
        completed = subprocess.run(
            [str(path), "-version"], capture_output=True, text=True, check=False
        )
    except OSError:
        # 없거나 실행 불가인 공급 — 호출자가 이미 문제로 기록했다.
        return ""
    return completed.stdout if completed.returncode == 0 else ""


def _check_arm64_macho(path: Path, *, name: str) -> list[str]:
    head = path.read_bytes()[:4096]
    if head[:4] not in _MACHO_MAGICS:
        return [f"{name}이(가) Mach-O가 아니다: {path}"]
    if head[:4] == b"\xcf\xfa\xed\xfe":
        cpu_type = struct.unpack_from("<I", head, 4)[0]
        if cpu_type != _CPU_TYPE_ARM64:
            return [f"{name}이(가) arm64가 아니다 (cputype=0x{cpu_type:x})"]
        return []
    # fat/universal — arm64 슬라이스가 하나 있으면 통과한다. 슬라이스 해석은
    # file(1)에 맡긴다.
    completed = subprocess.run(
        ["file", "-b", str(path)], capture_output=True, text=True, check=False
    )
    if "arm64" not in completed.stdout:
        return [f"{name} universal 바이너리에 arm64 슬라이스가 없다: {completed.stdout.strip()}"]
    return []


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
