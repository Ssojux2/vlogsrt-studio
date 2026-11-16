"""번들 엔진·FFmpeg 공급 계약의 실패-닫힘 회귀 시험.

다운로드도 실 바이너리도 없는 환경에서 검증 가능한 것만 검증한다: 계약이
비어 있으면 닫히고, manifest의 필드(파이썬·패키지·FFmpeg·데이터·파일 해시)가
부족하면 닫히고, 서명 없는 Mach-O는 통과하지 못한다. 진짜 빌드·정식 서명은
사람이 공급한 뒤 수동으로 본다.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

# 엔진 번들 서명 검증은 macOS `codesign`이 하는 일이다 — 다른 운영체제에는 그 도구가 없다(W-84a).
needs_codesign = pytest.mark.skipif(
    shutil.which("codesign") is None, reason="codesign(macOS)이 없는 환경"
)

ROOT = Path(__file__).resolve().parent.parent


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_verify_ffmpeg = _load("verify_ffmpeg", ROOT / "scripts" / "verify_ffmpeg.py")
_build_engine = _load("build_engine2", ROOT / "scripts" / "build_engine.py")

_MACHO_MAGIC = b"\xcf\xfa\xed\xfe"


def _synthetic_engine(tmp_path: Path, *, with_macho: bool = False) -> Path:
    tree = tmp_path / "Engine"
    (tree / "Contents" / "MacOS").mkdir(parents=True)
    executable = tree / "Contents" / "MacOS" / "vlogsrt-engine"
    executable.write_bytes(_MACHO_MAGIC + b"-mach-o-bytes" if with_macho else b"script-bytes")
    (tree / "Contents" / "libc.dylib").write_bytes(b"libc-bytes")
    return tree


def test_ffmpeg_contract_with_blank_sha256_fails_closed() -> None:
    """sha256 미기입 계약은 어떤 바이너리도 통과시키지 않는다."""
    problems = _verify_ffmpeg.verify()
    assert problems and "sha256" in problems[0]


def _two_binary_contract() -> dict:
    contract = json.loads((ROOT / "presets" / "ffmpeg-manifest.json").read_text(encoding="utf-8"))
    for name in ("ffmpeg", "ffprobe"):
        contract["binaries"][name]["sha256"] = "0" * 64
    return contract


def test_ffmpeg_contract_requires_both_binaries_and_closes_on_bad_ones(
    tmp_path: Path,
) -> None:
    """채운 계약도 바이너리가 없거나 Mach-O가 아니면 닫힌다 — ffprobe도 예외 아니다."""
    contract = _two_binary_contract()
    problems = _verify_ffmpeg.verify(
        ffmpeg=tmp_path / "absent-ffmpeg", ffprobe=tmp_path / "absent-ffprobe", contract=contract
    )
    assert any("ffmpeg 바이너리가 없다" in problem for problem in problems)

    fake = tmp_path / "fake-ffmpeg"
    fake.write_text("#!/bin/sh\necho 'ffmpeg version 9.0.1'\n", encoding="utf-8")
    problems = _verify_ffmpeg.verify(ffmpeg=fake, contract=contract)
    assert any("Mach-O" in problem for problem in problems)

    # ffprobe만 비어 있어도 닫힌다.
    problems = _verify_ffmpeg.verify(
        ffmpeg=tmp_path / "any", ffprobe=tmp_path / "absent-ffprobe", contract=contract
    )
    assert any("ffprobe" in problem for problem in problems)

    # 계약에서 한 바이너리가 빠지면 형태 검사에서 닫힌다.
    broken = _two_binary_contract()
    del broken["binaries"]["ffprobe"]
    assert _verify_ffmpeg.verify(contract=broken)
    # 저장소의 정본 계약은 아직 열려 있다 — sha256 미기입이 통합을 닫는다.
    assert _build_engine.ffmpeg_supply_state() is not None


def test_stage_ffmpeg_binaries_copies_both_into_bin_with_exec_permission(
    tmp_path: Path,
) -> None:
    """공급된 두 바이너리가 엔진 bin/에 실행권한으로 들어가 manifest 해시에 묶인다."""
    supplied = tmp_path / "FFmpeg"
    supplied.mkdir()
    ffmpeg, ffprobe = supplied / "ffmpeg", supplied / "ffprobe"
    ffmpeg.write_bytes(b"ffmpeg-bytes")
    ffprobe.write_bytes(b"ffprobe-bytes")

    engine = _synthetic_engine(tmp_path / "stage")
    staged = _build_engine.stage_ffmpeg_binaries(engine, {"ffmpeg": ffmpeg, "ffprobe": ffprobe})
    assert set(staged) == {"ffmpeg", "ffprobe"}
    bin_dir = engine / "bin"
    assert (bin_dir / "ffmpeg").read_bytes() == b"ffmpeg-bytes"
    assert (bin_dir / "ffprobe").read_bytes() == b"ffprobe-bytes"
    for name in ("ffmpeg", "ffprobe"):
        mode = (bin_dir / name).stat().st_mode & 0o111
        assert mode == 0o111, f"{name}이 실행권한이 없다"

    manifest = _build_engine.manifest_for(
        engine, ffmpeg_report=None, python_version="3.12.14", packages={}
    )
    paths = {row["path"] for row in manifest["files"]}
    assert {"bin/ffmpeg", "bin/ffprobe"} <= paths
    hashes = {row["path"]: row["sha256"] for row in manifest["files"]}
    import hashlib

    assert hashes["bin/ffmpeg"] == hashlib.sha256(b"ffmpeg-bytes").hexdigest()
    assert hashes["bin/ffprobe"] == hashlib.sha256(b"ffprobe-bytes").hexdigest()


def test_engine_manifest_carries_build_packages_ffmpeg_and_data_fields(
    tmp_path: Path,
) -> None:
    """의존성 선언만 있는 옛 manifest가 통과하는 회귀를 잡는다."""
    tree = _synthetic_engine(tmp_path)
    ffmpeg = {
        "binaries": {"ffmpeg": "1" * 64, "ffprobe": "2" * 64},
        "configure": ["--enable-shared", "--disable-gpl"],
        "license": "LGPL",
        "version": "9.0.1",
        "version_line": "ffmpeg version 9.0.1",
    }
    manifest = _build_engine.manifest_for(
        tree,
        ffmpeg_report=ffmpeg,
        python_version="3.12.14",
        packages={"vlogsrt": "1.0.0", "onnxruntime": None},
    )
    assert manifest["build"]["python"] == "3.12.14"
    assert manifest["build"]["packages"] == {"vlogsrt": "1.0.0", "onnxruntime": None}
    assert manifest["build"]["pyinstaller"] is None or isinstance(
        manifest["build"]["pyinstaller"], str
    )
    assert manifest["ffmpeg"] == ffmpeg
    assert manifest["data_payloads"]
    files = manifest["files"]
    assert {row["path"] for row in files} == {
        "Contents/MacOS/vlogsrt-engine",
        "Contents/libc.dylib",
    }
    assert all(len(row["sha256"]) == 64 for row in files)
    assert not _build_engine.manifest_field_problems(manifest)

    # 옛 형태(파일 목록만)는 필드 검사에서 닫힌다.
    assert _build_engine.manifest_field_problems({"files": files})
    assert _build_engine.manifest_field_problems({"build": {"python": "3.12"}, "files": []})

    # 내용 변조·미등록 파일은 대조에서 닫힌다.
    problems = _build_engine.compare_tree(tree, manifest)
    assert problems == []
    (tree / "Contents" / "libc.dylib").write_bytes(b"tampered")
    assert any("해시 다름" in problem for problem in _build_engine.compare_tree(tree, manifest))
    (tree / "Contents" / "libc.dylib").write_bytes(b"libc-bytes")
    (tree / "sneaked.txt").write_bytes(b"")
    assert any(
        "manifest에 없음" in problem for problem in _build_engine.compare_tree(tree, manifest)
    )


@needs_codesign
def test_unsigned_embedded_macho_fails_the_verify_gate(tmp_path: Path) -> None:
    """서명 없는 내장 Mach-O는 codesign 검증에서 닫힌다 — 반쪽 번들은 못 나간다."""
    tree = _synthetic_engine(tmp_path, with_macho=True)
    assert _build_engine.is_macho(tree / "Contents" / "MacOS" / "vlogsrt-engine")
    assert not _build_engine.is_macho(tree / "Contents" / "libc.dylib")
    problems = _build_engine.signature_problems(tree)
    assert problems == ["서명 없음/불일치: Contents/MacOS/vlogsrt-engine"]

    # Mach-O가 하나도 없으면 서명 게이트는 조용히 통과한다(스크립트 산물 등).
    plain = _synthetic_engine(tmp_path.parent / "plain", with_macho=False)
    assert _build_engine.signature_problems(plain) == []


def test_preflight_closes_on_open_ffmpeg_contract_and_missing_data() -> None:
    """빌드 전 단계 — 열린 FFmpeg 계약·빠진 데이터로 시작부터 닫힌다."""
    problems = _build_engine.preflight_problems()
    assert any("FFmpeg" in problem for problem in problems)


def test_finalize_stages_binaries_before_signing_and_manifest(tmp_path, monkeypatch) -> None:
    """bin 복사 → staged 전체 서명 → manifest 순서 — 서명이 bin 두 개를 놓치지 않는다."""
    built = tmp_path / "built"
    (built / "Contents" / "MacOS").mkdir(parents=True)
    (built / "Contents" / "MacOS" / "vlogsrt-engine").write_bytes(b"engine-bytes")
    supplied = {
        "ffmpeg": tmp_path / "supplied-ffmpeg",
        "ffprobe": tmp_path / "supplied-ffprobe",
    }
    for path in supplied.values():
        path.write_bytes(b"binary-bytes")
    calls: list[tuple[str, str]] = []

    def fake_stage(destination, binaries):
        calls.append(("stage", destination.name))
        # 진짜 복사 동작을 흉내 낸다 — 서명 대상에 bin 파일이 들어가는지 본다.
        bin_dir = destination / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        for name, path in binaries.items():
            (bin_dir / name).write_bytes(path.read_bytes())
        return {name: bin_dir / name for name in binaries}

    def fake_sign(tree, *, identity="-"):
        calls.append(("sign", tree.name))
        assert (tree / "bin" / "ffmpeg").is_file(), "서명 시점에 bin/ffmpeg가 있어야 한다"
        assert (tree / "bin" / "ffprobe").is_file(), "서명 시점에 bin/ffprobe가 있어야 한다"
        return []

    def fake_manifest(tree, **kwargs):
        calls.append(("manifest", tree.name))
        return {"files": []}

    monkeypatch.setattr(_build_engine, "stage_ffmpeg_binaries", fake_stage)
    monkeypatch.setattr(_build_engine, "sign_embedded", fake_sign)
    monkeypatch.setattr(_build_engine, "manifest_for", fake_manifest)
    monkeypatch.setattr(_build_engine, "compare_tree", lambda tree, recorded: [])

    staged = tmp_path / "staged-engine"
    manifest, problems = _build_engine.finalize_staged_engine(built, staged, binaries=supplied)
    assert problems == []
    assert manifest == {"files": []}
    # 순서가 계약이다: 복사 → 서명 → manifest, 전부 staged 트리에서.
    assert calls == [
        ("stage", "staged-engine"),
        ("sign", "staged-engine"),
        ("manifest", "staged-engine"),
    ]


def test_finalize_never_returns_manifest_when_signing_fails(tmp_path, monkeypatch) -> None:
    """서명 실패면 manifest 없이 문제만 — 호출자는 Resources/Engine을 바꾸지 않는다."""
    built = tmp_path / "built"
    (built / "Contents" / "MacOS").mkdir(parents=True)
    (built / "Contents" / "MacOS" / "vlogsrt-engine").write_bytes(b"engine-bytes")
    supplied = {"ffmpeg": tmp_path / "f", "ffprobe": tmp_path / "p"}
    for path in supplied.values():
        path.write_bytes(b"b")

    monkeypatch.setattr(
        _build_engine, "sign_embedded", lambda tree, *, identity="-": ["bin/ffmpeg 깨짐"]
    )
    reached_manifest: list[str] = []
    monkeypatch.setattr(
        _build_engine,
        "manifest_for",
        lambda *args, **kwargs: reached_manifest.append("manifest") or {},
    )
    manifest, problems = _build_engine.finalize_staged_engine(
        built, tmp_path / "staged", binaries=supplied
    )
    assert manifest is None
    assert problems and "Resources/Engine을 바꾸지 않는다" in problems[0]
    assert reached_manifest == []


@needs_codesign
def test_staged_bin_ffmpeg_and_ffprobe_are_signature_targets(tmp_path) -> None:
    """bin에 실린 두 바이너리는 Mach-O로 인식돼 서명·검증 대상이 된다."""
    engine = _synthetic_engine(tmp_path, with_macho=False)
    supplied = {
        "ffmpeg": tmp_path / "s-ffmpeg",
        "ffprobe": tmp_path / "s-ffprobe",
    }
    for name, path in supplied.items():
        path.write_bytes(_MACHO_MAGIC + f"{name}-payload".encode())
    _build_engine.stage_ffmpeg_binaries(engine, supplied)
    assert _build_engine.is_macho(engine / "bin" / "ffmpeg")
    assert _build_engine.is_macho(engine / "bin" / "ffprobe")
    problems = _build_engine.signature_problems(engine)
    assert sorted(problems) == [
        "서명 없음/불일치: bin/ffmpeg",
        "서명 없음/불일치: bin/ffprobe",
    ]


def test_preflight_runs_the_real_ffmpeg_verification_and_keeps_reasons() -> None:
    """preflight의 FFmpeg 문제는 verify_ffmpeg의 실제 검증 이유를 보존한다."""
    problems = _build_engine.preflight_problems()
    ffmpeg_problems = [row for row in problems if row.startswith("FFmpeg 공급 검증 실패")]
    assert ffmpeg_problems, "실검증 결과가 preflight에 흘러들어야 한다"
    assert any("sha256" in row for row in ffmpeg_problems)
    # 지연 로딩된 검증 모듈은 같은 실패를 그대로 말한다 — 이유가 환산되지 않았다.
    assert _build_engine.ffmpeg_verification_problems() == _verify_ffmpeg.verify()
