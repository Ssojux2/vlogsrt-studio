#!/usr/bin/env python3
"""arm64 PyInstaller onedir 엔진 빌드·manifest·서명·Resources/Engine 통합.

실패-닫힘 경계가 이 스크립트의 본체다:

- 무엇도 **다운로드하지 않는다**. PyInstaller가 없으면 안내만 남기고 1로 끝난다.
- 번들에 필요한 데이터(vlogsrt 설정·프리셋·키워드·VAD 모델)가 저장소에 없으면
  빌드를 시작하지 않는다.
- FFmpeg 계약(presets/ffmpeg-manifest.json)이 채워지지 않았으면 통합이 닫힌다 —
  엔진은 ffmpeg 없이는 돌지 않으므로 반쪽 번들을 만들지 않는다.
- manifest는 파일 해시 전부와 파이썬·패키지·FFmpeg 버전·configure·해시를 기록한다.
- 모든 내장 Mach-O(dylib·헬퍼)는 codesign으로 서명하고, `--verify`는 서명·해시·
  필드를 전부 대조한다. 하나라도 어긋나면 1로 끝난다.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import platform
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DEST = ROOT / "macos" / "Resources" / "Engine"
MANIFEST_NAME = "engine-manifest.json"
FFMPEG_MANIFEST = ROOT / "presets" / "ffmpeg-manifest.json"
ENTRY_SCRIPT = ROOT / "macos" / "scripts" / "vlogsrt-engine.py"
REQUIRED_ARCH = "arm64"
REQUIRED_PACKAGES = ("vlogsrt", "openai", "rich", "tomli-w", "numpy", "onnxruntime")
# 번들에 실어야 하는 데이터 — 상대경로:목적지(PyInstaller --add-data 형식과 같다).
DATA_PAYLOADS = (
    f"{ROOT / 'vlogsrt.toml'}:.",
    f"{ROOT / 'presets'}:presets",
    f"{ROOT / 'keywords'}:keywords",
)
# Mach-O 매직 — 내장 실행파일·dylib를 골라 서명하는 데 쓴다.
_MACHO_MAGICS = (b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"\xbe\xba\xfe\xca", b"\xfe\xdf\xfa\xcf")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--verify", action="store_true", help="빌드 없이 manifest 대조만")
    parser.add_argument(
        "--sign-identity", default="-", help="내장 Mach-O 서명 신원(기본: 임시 ad-hoc '-')"
    )
    parser.add_argument("--workdir", type=Path, default=None, help="PyInstaller 작업 디렉터리")
    args = parser.parse_args(argv)

    _require_macos_arm64()
    if args.verify:
        return _verify_existing()

    problems = preflight_problems()
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1

    pyinstaller = shutil.which("pyinstaller")
    if pyinstaller is None:
        print(
            "pyinstaller가 없다 — 새 의존성을 추가하지 않으므로 사람이 설치한다:\n"
            "  uvx pyinstaller ... (또는 수동 설치 후 재실행)",
            file=sys.stderr,
        )
        return 1

    workdir = args.workdir or (ROOT / "build" / "engine")
    workdir.mkdir(parents=True, exist_ok=True)
    completed = subprocess.run(
        [
            pyinstaller,
            "--noconfirm",
            "--clean",
            "--onedir",
            "--windowed",
            "--name",
            "vlogsrt-engine",
            # vlogsrt 전체(패키지 데이터 — VAD 모델 vlogsrt/project/vad/data 포함).
            "--collect-submodules",
            "vlogsrt",
            "--collect-data",
            "vlogsrt",
            # onnxruntime는 네이티브 라이브러리까지 통째로 필요하다.
            "--collect-all",
            "onnxruntime",
            # numpy는 훅이 잡지만 숨은 import를 못 박는다(VAD 계열에서 동적 경로).
            "--hidden-import",
            "numpy",
            *sum((["--add-data", payload] for payload in DATA_PAYLOADS), []),
            "--distpath",
            str(workdir / "dist"),
            "--workpath",
            str(workdir / "work"),
            "--specpath",
            str(workdir),
            str(ENTRY_SCRIPT),
        ],
        check=False,
    )
    if completed.returncode != 0:
        print(f"PyInstaller 빌드 실패 (exit {completed.returncode})", file=sys.stderr)
        return 1

    built = workdir / "dist" / "vlogsrt-engine"
    if not (built / "Contents" / "MacOS" / "vlogsrt-engine").is_file():
        print(f"onedir 산물이 계약과 다르다: {built}", file=sys.stderr)
        return 1
    staged = workdir / "staged-engine"
    manifest, problems = finalize_staged_engine(
        built, staged, binaries=supplied_binaries(), identity=args.sign_identity
    )
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    assert manifest is not None
    if ENGINE_DEST.exists():
        shutil.rmtree(ENGINE_DEST)
    shutil.copytree(staged, ENGINE_DEST)
    print(f"엔진 통합 완료: {ENGINE_DEST}")
    return 0


def finalize_staged_engine(
    built: Path, staged: Path, *, binaries: dict[str, Path], identity: str = "-"
) -> tuple[dict[str, object] | None, list[str]]:
    """staged 트리 완성 — 순서가 계약이다: bin 복사 → 전체 Mach-O 서명 → manifest.

    bin/ffmpeg·ffprobe가 **트리 안에 들어온 뒤에** 서명한다(복사 전 서명은 새로
    실린 두 바이너리를 놓친다). 서명·대조가 하나라도 실패하면 manifest 없이
    문제만 돌려주고, 호출자는 Resources/Engine을 절대 바꾸지 않는다.
    """
    if staged.exists():
        shutil.rmtree(staged)
    shutil.copytree(built, staged)
    stage_ffmpeg_binaries(staged, binaries)
    signature_failures = sign_embedded(staged, identity=identity)
    if signature_failures:
        return None, [
            f"서명 실패 — Resources/Engine을 바꾸지 않는다: {row}" for row in signature_failures
        ]
    manifest = manifest_for(staged, ffmpeg_report=ffmpeg_supply_report())
    (staged / MANIFEST_NAME).write_text(_manifest_text(manifest), encoding="utf-8")
    return manifest, compare_tree(staged, json.loads(_manifest_text(manifest)))


def _require_macos_arm64() -> None:
    if platform.system() != "Darwin" or platform.machine() != REQUIRED_ARCH:
        print(
            f"엔진 빌드는 macOS/{REQUIRED_ARCH}에서만 한다: "
            f"{platform.system()}/{platform.machine()}",
            file=sys.stderr,
        )
        raise SystemExit(1)


def preflight_problems() -> list[str]:
    """빌드를 시작하기 전에 본다 — FFmpeg은 존재가 아니라 실제 검증을 통과해야 한다.

    `verify_ffmpeg.verify()`(Mach-O arm64·두 version 9.0.1·LGPL·각 sha256)을
    그대로 돌려 실패 이유를 보존한다. import는 형제 스크립트의 지연 로딩 —
    순환 import가 없고 verify_ffmpeg는 이 모듈을 모른다.
    """
    problems: list[str] = []
    for problem in ffmpeg_verification_problems():
        problems.append(f"FFmpeg 공급 검증 실패 — {problem}")
    for payload in DATA_PAYLOADS:
        # 「원본:자리」의 자리에는 `:`이 없다 — 오른쪽에서 가른다.
        # Windows 원본(`D:\…`)의 드라이브 `:`에서 가르면 안 된다(W-84a).
        source = Path(payload.rsplit(":", 1)[0])
        if not source.exists():
            problems.append(f"번들 데이터가 없다: {source.relative_to(ROOT)}")
    return problems


def ffmpeg_verification_problems() -> list[str]:
    """scripts/verify_ffmpeg.py의 실제 검증 — 이유 문장을 그대로 돌려준다."""
    return _verify_ffmpeg_module().verify()


def _verify_ffmpeg_module():
    import importlib.util

    path = ROOT / "scripts" / "verify_ffmpeg.py"
    spec = importlib.util.spec_from_file_location("vlogsrt_verify_ffmpeg", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ffmpeg_supply_state() -> str | None:
    """ffmpeg·ffprobe 계약의 빈 칸을 하나라도 찾으면 그 이름을 돌려준다."""
    if not FFMPEG_MANIFEST.is_file():
        return f"{FFMPEG_MANIFEST.relative_to(ROOT)} 없음"
    contract = json.loads(FFMPEG_MANIFEST.read_text(encoding="utf-8"))
    for key in ("version", "arch", "license"):
        if not contract.get(key):
            return f"{key} 미기입"
    binaries = contract.get("binaries")
    if not isinstance(binaries, dict) or set(binaries) != {"ffmpeg", "ffprobe"}:
        return "binaries(ffmpeg·ffprobe) 미기입"
    for name in ("ffmpeg", "ffprobe"):
        entry = binaries[name]
        if not entry.get("sha256"):
            return f"binaries.{name}.sha256 미기입"
        binary = ROOT / entry.get("resource_path", "")
        if not binary.is_file():
            return f"{binary.relative_to(ROOT)} 없음"
    return None


def supplied_binaries() -> dict[str, Path]:
    """계약이 가리키는 공급된 두 바이너리 — supply_state가 닫혀 있으면 부르지 않는다."""
    contract = json.loads(FFMPEG_MANIFEST.read_text(encoding="utf-8"))
    return {name: ROOT / entry["resource_path"] for name, entry in contract["binaries"].items()}


def stage_ffmpeg_binaries(destination: Path, binaries: dict[str, Path]) -> dict[str, Path]:
    """공급된 ffmpeg·ffprobe를 엔진의 bin/에 실행권한으로 복사한다.

    엔진의 `require_ffmpeg`는 PATH에서 도구를 찾는다 — 번들 실행의 PATH는
    `<engineRoot>/bin`(BundledEngineInvocation)이므로 여기가 그 자리다. 복사는
    manifest 계산 **앞에** 와야 파일 해시에 묶인다.
    """
    bin_dir = destination / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    staged: dict[str, Path] = {}
    for name in ("ffmpeg", "ffprobe"):
        target = bin_dir / name
        if target.exists():
            target.unlink()
        shutil.copyfile(binaries[name], target)
        target.chmod(0o755)
        staged[name] = target
    return staged


def ffmpeg_supply_report() -> dict[str, object] | None:
    """공급된 두 바이너리에서 버전·라이선스·configure·해시를 뽑아 manifest에 싣는다."""
    if ffmpeg_supply_state() is not None:
        return None
    contract = json.loads(FFMPEG_MANIFEST.read_text(encoding="utf-8"))
    binaries = supplied_binaries()
    completed = subprocess.run(
        [str(binaries["ffmpeg"]), "-version"], capture_output=True, text=True, check=False
    )
    if completed.returncode != 0:
        return None
    lines = completed.stdout.splitlines()
    configure: list[str] = []
    for line in lines:
        if line.startswith("  configuration:"):
            configure = line[len("  configuration:") :].split()
    return {
        "binaries": {name: _sha256(path) for name, path in sorted(binaries.items())},
        "configure": configure,
        "license": contract["license"],
        "version": contract["version"],
        "version_line": lines[0] if lines else "",
    }


def package_versions() -> dict[str, str | None]:
    """번들에 실리는 패키지의 실제 버전 — 설치 안 된 패키지는 None(빌드 전 단계)."""
    versions: dict[str, str | None] = {}
    for name in REQUIRED_PACKAGES:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def manifest_for(
    tree: Path,
    *,
    ffmpeg_report: dict[str, object] | None = None,
    python_version: str | None = None,
    packages: dict[str, str | None] | None = None,
) -> dict[str, object]:
    files = []
    for path in sorted(tree.rglob("*")):
        if path.is_dir() or path.name == MANIFEST_NAME:
            continue
        files.append(
            {
                "path": path.relative_to(tree).as_posix(),
                "sha256": _sha256(path),
                "size": path.stat().st_size,
                "mach_o": is_macho(path),
            }
        )
    return {
        "build": {
            "packages": packages if packages is not None else package_versions(),
            "platform": f"{platform.system()}/{platform.machine()}",
            "python": python_version or platform.python_version(),
            "pyinstaller": _tool_version("pyinstaller"),
        },
        "data_payloads": [payload.split("/", 1)[-1] for payload in DATA_PAYLOADS],
        "ffmpeg": ffmpeg_report,
        "files": files,
        "kind": "pyinstaller-onedir",
    }


def _tool_version(name: str) -> str | None:
    try:
        completed = subprocess.run([name, "--version"], capture_output=True, text=True, check=False)
    except FileNotFoundError:
        return None
    if completed.returncode != 0:
        return None
    return (completed.stdout or completed.stderr).strip().splitlines()[-1] or None


def _manifest_text(manifest: dict[str, object]) -> str:
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def is_macho(path: Path) -> bool:
    try:
        return path.open("rb").read(4) in _MACHO_MAGICS
    except OSError:
        return False


def sign_embedded(tree: Path, *, identity: str = "-") -> list[str]:
    """내장 Mach-O 전부를 codesign한다 — 임시 신원('-')이 기본(사람이 정식 신원 준다).

    문제 목록만 돌려주고 출력은 하지 않는다 — 호출자가 한 번만 print한다.
    """
    problems: list[str] = []
    for path in sorted(tree.rglob("*")):
        if path.is_dir() or not is_macho(path):
            continue
        completed = subprocess.run(
            ["codesign", "--force", "--sign", identity, str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            problems.append(f"서명 실패 {path.relative_to(tree)}: {completed.stderr.strip()}")
    return problems


def _verify_existing() -> int:
    if not ENGINE_DEST.is_dir():
        print(f"엔진이 없다: {ENGINE_DEST}", file=sys.stderr)
        return 1
    manifest_path = ENGINE_DEST / MANIFEST_NAME
    if not manifest_path.is_file():
        print(f"manifest가 없다: {manifest_path}", file=sys.stderr)
        return 1
    recorded = json.loads(manifest_path.read_text(encoding="utf-8"))
    problems = manifest_field_problems(recorded) + compare_tree(ENGINE_DEST, recorded)
    problems += signature_problems(ENGINE_DEST)
    if problems:
        for problem in problems:
            print(f"FAIL {problem}", file=sys.stderr)
        return 1
    files = recorded.get("files") or []
    print(f"OK {len(files)}개 파일이 manifest와 일치하고 서명이 있다")
    return 0


def manifest_field_problems(recorded: dict[str, object]) -> list[str]:
    """manifest가 계획의 필드를 전부 담고 있는지 — 의존성 선언만 있는 옛 형태를 잡는다."""
    problems: list[str] = []
    build = recorded.get("build")
    if not isinstance(build, dict):
        return ["manifest에 build 블록이 없다"]
    for key in ("python", "packages", "pyinstaller"):
        if key not in build:
            problems.append(f"manifest.build에 {key}가 없다")
    if not recorded.get("files"):
        problems.append("manifest에 파일 목록이 없다")
    if "ffmpeg" not in recorded:
        problems.append("manifest에 ffmpeg 블록이 없다")
    if not recorded.get("data_payloads"):
        problems.append("manifest에 data_payloads가 없다")
    return problems


def compare_tree(tree: Path, recorded: dict[str, object]) -> list[str]:
    problems: list[str] = []
    actual = {row["path"]: row for row in manifest_for(tree)["files"]}  # type: ignore[index]
    recorded_files = {row["path"]: row for row in recorded.get("files", [])}  # type: ignore[union-attr]
    for name, row in recorded_files.items():
        if name not in actual:
            problems.append(f"파일 없음: {name}")
        elif actual[name]["sha256"] != row["sha256"]:
            problems.append(f"해시 다름: {name}")
    for name in actual:
        if name not in recorded_files:
            problems.append(f"manifest에 없음: {name}")
    return problems


def signature_problems(tree: Path) -> list[str]:
    """내장 Mach-O마다 codesign 검증 — 서명 없는 산물은 통과하지 못한다."""
    problems: list[str] = []
    for path in sorted(tree.rglob("*")):
        if path.is_dir() or not is_macho(path):
            continue
        completed = subprocess.run(
            ["codesign", "--verify", "--strict", str(path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            problems.append(f"서명 없음/불일치: {path.relative_to(tree)}")
    return problems


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
