#!/usr/bin/env python3
"""CLAUDE.md의 불변 규칙을 기계적으로 검사한다.

사람 눈보다 싸고 잊지 않는다. `make check`가 이걸 부른다.
원본/파생(source ↔ vlogsrt.project) 계약의 정본은 docs/PROJECT_CONTRACTS.md다.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "vlogsrt"
UI_DIR = ROOT / "ui"

# 파생 계층 vlogsrt.project. 아직 없어도 계약은 먼저 선다(docs/PROJECT_CONTRACTS.md).
PROJECT_PACKAGE = PACKAGE / "project"
PROJECT_OPTIONAL_DEPENDENCIES = {"numpy", "onnxruntime"}
XML_VALIDATION_MODULE = PROJECT_PACKAGE / "xml_validation.py"

BANNED_IMPORTS = (
    "jamo", "konlpy", "pysrt", "ffmpeg", "pydantic", "dotenv", "regex", "numpy",
    # UI는 ui/ 에만 산다. 핵심 패키지가 gradio를 끌어오면 규칙 7이 무너진다.
    "gradio",
)  # fmt: skip
ALLOWED_DEPENDENCIES = {"openai", "tomli-w", "rich"}

# 예외는 전부 **정확 상대 경로**로 판정한다. 파일 이름으로 판정하면 같은 이름의
# 파일이 예외를 물려받는다 — `vlogsrt/project/transcribe.py`가 openai를 얻는 식이다.
OPENAI_BOUNDARY_PATH = "vlogsrt/transcribe.py"
SUBPROCESS_BOUNDARY_PATHS = ("vlogsrt/preprocess.py", "vlogsrt/project/xml_validation.py")
MODEL_LITERAL_PATHS = ("vlogsrt/config.py", "vlogsrt/project/config.py")
# 레거시가 project를 import할 수 있는 유일한 진입점 — 문서화된 CLI 조립 지점.
CLI_COMPOSITION_ENTRYPOINT = "vlogsrt/cli.py"
XMLLINT_EXECUTABLE = "xmllint"


def _rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def _sources() -> list[Path]:
    return sorted(PACKAGE.rglob("*.py"))


def _legacy_sources() -> list[Path]:
    """원본 측 소스 — 패키지 최상위와 UI. vlogsrt/project/ 아래는 없다."""
    return sorted(PACKAGE.glob("*.py")) + sorted(UI_DIR.rglob("*.py"))


def _project_sources() -> list[Path]:
    """파생 측 소스 — vlogsrt/project/ 아래 전부. 디렉터리가 없으면 빈 목록."""
    return sorted(PROJECT_PACKAGE.rglob("*.py")) if PROJECT_PACKAGE.is_dir() else []


def _import_roots(body: str) -> list[tuple[int, str]]:
    """import문의 뿌리 모듈 (줄번호, 이름). 절대·상대·별명 전부 여기서 뽑는다.

    `import subprocess as sp`·`from numpy import ndarray`·`import numpy.typing`이
    전부 뿌리 subprocess/numpy로 귀결한다. 정규식은 별명과 상대 import를 놓치므로
    쓰지 않는다.
    """
    rows: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.Import):
            rows.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                rows.append((node.lineno, node.module.split(".")[0]))
            rows.extend((node.lineno, alias.name.split(".")[0]) for alias in node.names)
    return rows


def check_openai_boundary() -> list[str]:
    """규칙 5 — API 호출은 transcribe.py에서만. 허용은 정확 경로 하나뿐이다."""
    failures = []
    for path in _sources():
        rel = _rel(path)
        if rel == OPENAI_BOUNDARY_PATH:
            continue
        for lineno, name in _import_roots(path.read_text(encoding="utf-8")):
            if name == "openai":
                failures.append(f"{rel}:{lineno} openai import는 transcribe.py에만")
    return failures


def check_autojunk() -> list[str]:
    """규칙 4 — SequenceMatcher는 항상 autojunk=False."""
    failures = []
    for path in _sources():
        body = path.read_text(encoding="utf-8")
        for match in re.finditer(r"SequenceMatcher\((?P<args>[^)]*)\)", body, re.DOTALL):
            if "autojunk=False" not in match.group("args"):
                line = body[: match.start()].count("\n") + 1
                failures.append(
                    f"{path.relative_to(ROOT)}:{line} SequenceMatcher에 autojunk=False 없음"
                )
    return failures


def check_banned_imports() -> list[str]:
    """규칙 7 — 의존성은 openai, tomli-w, rich 뿐.

    numpy·onnxruntime는 선택 extra다 — vlogsrt/project/ 아래에서만 허용하고
    나머지 금지 이름은 (project 안을 포함해) 어디서도 허용하지 않는다.
    """
    project_paths = set(_project_sources())
    failures = []
    for path in _sources():
        rel = _rel(path)
        for lineno, name in _import_roots(path.read_text(encoding="utf-8")):
            if name not in BANNED_IMPORTS:
                continue
            if path in project_paths and name in PROJECT_OPTIONAL_DEPENDENCIES:
                continue
            failures.append(f"{rel}:{lineno} 금지된 의존성 {name}")
    return failures


def check_dependencies() -> list[str]:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        declared = tomllib.load(handle)["project"]["dependencies"]
    names = {re.split(r"[<>=\[ ]", entry)[0] for entry in declared}
    if names != ALLOWED_DEPENDENCIES:
        return [f"pyproject 의존성이 {sorted(names)}. 허용: {sorted(ALLOWED_DEPENDENCIES)}"]
    return []


def check_frozen_models() -> list[str]:
    """규칙 2 — spine은 수정 불가. 데이터클래스가 frozen이어야 강제된다."""
    body = (PACKAGE / "models.py").read_text(encoding="utf-8")
    failures = []
    for name in ("Segment", "Cue", "Trace"):
        pattern = rf"@dataclass\(frozen=True[^)]*\)\s*\nclass {name}\b"
        if not re.search(pattern, body):
            failures.append(f"models.py: {name}이 frozen dataclass가 아니다")
    return failures


def check_env_untouched() -> list[str]:
    """`.env`는 읽지도 고치지도 않는다 — 코드가 경로를 하드코딩해 덮어쓰지 않는지 본다."""
    failures = []
    for path in _sources():
        body = path.read_text(encoding="utf-8")
        if re.search(r"write_text\([^)]*\)\s*#\s*\.env", body):
            failures.append(f"{path.relative_to(ROOT)}: .env에 쓰기 시도")
    return failures


def check_source_does_not_import_project() -> list[str]:
    """원본/파생 방향 — 레거시(vlogsrt 최상위·ui)는 vlogsrt.project를 import 못 한다.

    절대(`import vlogsrt.project`, `from vlogsrt import project`)와
    상대(`from .project import x`, `from . import project`) 전부 거부한다.
    예외는 CLI 조립 진입점 `vlogsrt/cli.py` 하나(정확 경로)다. 방향은
    project → 레거시 공개 경계로만 열려 있다.
    """
    failures = []
    for path in _legacy_sources():
        rel = _rel(path)
        if rel == CLI_COMPOSITION_ENTRYPOINT:
            continue
        for lineno, form in _project_imports(path.read_text(encoding="utf-8")):
            failures.append(f"{rel}:{lineno} 레거시가 vlogsrt.project를 import한다 ({form})")
    return failures


def check_project_dependency_scope() -> list[str]:
    """선택 extra numpy·onnxruntime는 vlogsrt/project/ 아래에서만 쓸 수 있다."""
    failures = []
    for path in _legacy_sources():
        rel = _rel(path)
        for lineno, name in _import_roots(path.read_text(encoding="utf-8")):
            if name in PROJECT_OPTIONAL_DEPENDENCIES:
                failures.append(f"{rel}:{lineno} 선택 의존성 {name}은 vlogsrt/project/ 아래에서만")
    return failures


def _project_imports(body: str) -> list[tuple[int, str]]:
    """vlogsrt.project를 참조하는 import문 (줄번호, 형태 서술)."""
    hits: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[:2] == ["vlogsrt", "project"]:
                    hits.append((node.lineno, f"import {alias.name}"))
        elif isinstance(node, ast.ImportFrom):
            hit = _project_importfrom(node)
            if hit is not None:
                hits.append(hit)
    return hits


def _project_importfrom(node: ast.ImportFrom) -> tuple[int, str] | None:
    module = node.module or ""
    dots = "." * node.level
    if module == "vlogsrt.project" or module.startswith("vlogsrt.project."):
        return node.lineno, f"from {dots}{module} import …"
    if node.level >= 1 and (module == "project" or module.startswith("project.")):
        return node.lineno, f"from {dots}{module} import …"
    if module in ("", "vlogsrt") and any(alias.name == "project" for alias in node.names):
        return node.lineno, f"from {dots}{module or 'vlogsrt'} import project"
    return None


# 자막을 **고치거나 빼던** 계층. 검증(표시 전용)과 달리 출력을 건드렸다.
DELETED_MODULES = {
    "mapping.py": "D19 — 사상(mapping)",
    "gapfill.py": "D19 — 놓친 구간 되살리기",
    "loops.py": "D25 — 되풀이 강등. 검출은 repetition.py, 처리는 repair.py로 갈렸다(D27)",
    "validate.py": "D21 — 정제 결과 검사",
    "report.py": "D21 — 검토 리포트",
}

# 검증은 표시만 한다. 출력을 만드는 모듈을 알면 그 선이 무너진다(규칙 5).
VERIFY_MUST_NOT_IMPORT = ("refine", "output", "layout", "polish")

# 재전사는 **골격이 조립되기 전**에 끝난다. 조판 계층을 알면 그 선이 무너진다(D27).
REPAIR_MUST_NOT_IMPORT = ("refine", "output", "layout", "polish", "verify")

# 조판 이후 계층이 되풀이 판정을 알면, 판정이 파이프라인 아래로 새어나간다(D27).
DOWNSTREAM_MUST_NOT_IMPORT = {
    "refine.py": ("repair", "repetition"),
    "output.py": ("repair", "repetition"),
    "layout.py": ("repair", "repetition"),
    "polish.py": ("repair", "repetition"),
}


def check_inspection_stays_dead() -> list[str]:
    """규칙 5 — 판정으로 자막을 빼던 계층은 되살리지 않는다."""
    failures = []
    for name, why in DELETED_MODULES.items():
        if (PACKAGE / name).exists():
            failures.append(f"vlogsrt/{name}: 삭제된 계층이 부활했다 ({why})")
    return failures


def check_verify_cannot_touch_output() -> list[str]:
    """규칙 5 — `verify.py`는 큐를 만들지도 고치지도 못한다.

    검증 on/off에 SRT 바이트가 같아야 한다. 출력 계층을 import하는 순간 그 보장이
    사람의 주의력에 달리게 된다.
    """
    path = PACKAGE / "verify.py"
    if not path.exists():
        return []
    failures = []
    body = path.read_text(encoding="utf-8")
    failures += [
        f"verify.py: {name}을 import한다 — 검증이 출력을 건드릴 수 있다"
        for name in _imports(body, VERIFY_MUST_NOT_IMPORT)
    ]
    if re.search(r"^\s*def (save|write)_", body, re.MULTILINE):
        failures.append("verify.py: 파일을 쓰는 함수가 생겼다 — 검증은 표시만 한다")
    return failures


def _imported_names(body: str) -> set[str]:
    """실제로 import된 모듈 이름. **정규식으로 세지 않는다.**

    이 프로젝트가 가장 많이 쓰는 형태가 `from . import log, refine, transcribe`인데,
    `^(from|import)\\s+\\.?refine` 같은 정규식은 그걸 못 잡는다. 검사가 통과하는데
    선은 이미 넘어가 있는 상태가 된다 — 그게 기계 검사의 최악이다.
    """
    seen: set[str] = set()
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.ImportFrom):
            seen.add((node.module or "").split(".")[-1])
            seen.update(alias.name.split(".")[-1] for alias in node.names)
        elif isinstance(node, ast.Import):
            seen.update(alias.name.split(".")[-1] for alias in node.names)
    return seen


def _imports(body: str, names: tuple[str, ...]) -> list[str]:
    imported = _imported_names(body)
    return [name for name in names if name in imported]


def check_repair_stays_upstream() -> list[str]:
    """D27 — 되풀이 재전사는 전사 단계 안에서 끝난다.

    조판(`refine`·`layout`·`output`)을 알면 "판정으로 자막을 고치는" 옛 계층과 같아진다.
    반대로 조판 쪽이 재전사를 알면 판정이 파이프라인 아래로 샌다.
    """
    failures = []
    path = PACKAGE / "repair.py"
    if path.exists():
        body = path.read_text(encoding="utf-8")
        failures += [
            f"repair.py: {name}을 import한다 — 재전사가 조판까지 내려왔다"
            for name in _imports(body, REPAIR_MUST_NOT_IMPORT)
        ]
    for name, forbidden in DOWNSTREAM_MUST_NOT_IMPORT.items():
        target = PACKAGE / name
        if not target.exists():
            continue
        failures += [
            f"{name}: {leaked}를 import한다 — 되풀이 판정이 조판까지 샜다"
            for leaked in _imports(target.read_text(encoding="utf-8"), forbidden)
        ]
    return failures


def check_confidence_meta_survives() -> list[str]:
    """D27 — whisper가 준 되풀이 신호를 버리지 않는다.

    `compression_ratio`가 되풀이를 짚는 유일한 공짜 신호다. 예전에는 응답에서 읽지도
    않고 버렸고, 그래서 되풀이를 사람 눈으로만 찾을 수 있었다.
    """
    body = (PACKAGE / "transcribe.py").read_text(encoding="utf-8")
    match = re.search(r"def parse_segments\(.*?\n(?=\ndef |\n# )", body, re.DOTALL)
    if not match:
        return ["transcribe.py: parse_segments를 찾지 못했다 — 메타 검사가 무력해졌다"]
    missing = [
        field
        for field in ("compression_ratio", "avg_logprob", "no_speech_prob")
        if field not in match.group(0)
    ]
    if missing:
        return [f"transcribe.py: parse_segments가 {missing}를 버린다 (D27)"]
    return []


PURE_MODULES = ("cutpoints.py", "repetition.py", "polish.py")

# D24의 시각 유도 계층. 되살아나면 초반·마지막이 다시 날아간다(D25).
BURIED_MODULES = {
    "spans.py": "D25 — 무음으로 발화 구간을 고르던 계층",
    "align.py": "D25 — 글자를 구간에 배분하던 계층",
}


def check_pure_layer() -> list[str]:
    """규칙 7 — 자를 자리를 고르는 계층은 순수해야 한다.

    `cutpoints.py`가 ffmpeg를 직접 부르기 시작하면 진폭을 지어내며 시험할 수 없게 된다.
    """
    failures = []
    forbidden = re.compile(r"^\s*(?:import|from)\s+(openai|subprocess|shutil)\b")
    for name in PURE_MODULES:
        path = PACKAGE / name
        if not path.exists():
            failures.append(f"vlogsrt/{name}: 순수 계층이 사라졌다 (D25)")
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            match = forbidden.match(line)
            if match:
                where = f"vlogsrt/{name}:{number}"
                failures.append(f"{where} 순수 계층이 {match.group(1)}을 import한다")
    return failures


def check_time_deriving_layers_stay_buried() -> list[str]:
    """규칙 4 — 시각을 유도하던 계층은 되살리지 않는다.

    무음으로 "어느 구간이 말인가"를 판정하다가 초반·마지막 3~6분을 날렸다(D25).
    """
    return [
        f"vlogsrt/{name}: {why}. 되살리지 마라"
        for name, why in BURIED_MODULES.items()
        if (PACKAGE / name).exists()
    ]


def _subprocess_bindings(tree: ast.Module) -> tuple[set[str], set[str], list[int]]:
    """subprocess 묶음 — (모듈 별명들, from-import 심볼들, 별표 줄들).

    `import subprocess as sp`와 `from subprocess import run`을 모른 척하면
    경계 검사가 이름 몇 개로 우회된다.
    """
    module_aliases: set[str] = set()
    symbols: set[str] = set()
    star_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "subprocess":
                    module_aliases.add(alias.asname or "subprocess")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
            "subprocess"
        ):
            for alias in node.names:
                if alias.name == "*":
                    star_lines.append(node.lineno)
                else:
                    symbols.add(alias.asname or alias.name)
    return module_aliases, symbols, star_lines


def _subprocess_calls(
    tree: ast.Module, module_aliases: set[str], symbols: set[str]
) -> list[ast.Call]:
    """별명 붙은 형태까지 포함해 subprocess 호출 지점을 모은다."""
    calls: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        named = isinstance(func, ast.Name) and func.id in symbols
        attributed = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
        )
        if named or attributed:
            calls.append(node)
    return calls


def _which_calls(tree: ast.Module) -> list[ast.Call]:
    """`shutil.which(...)` 호출 전부(`from shutil import which` 형태도)."""
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (isinstance(func, ast.Attribute) and func.attr == "which") or (
            isinstance(func, ast.Name) and func.id == "which"
        ):
            calls.append(node)
    return calls


def _single_const(call: ast.Call) -> object | None:
    if len(call.args) != 1 or not isinstance(call.args[0], ast.Constant):
        return None
    return call.args[0].value


def _assigned_names(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign) and node.value is not None:
        return [node.target.id] if isinstance(node.target, ast.Name) else []
    return []


def _xmllint_names(tree: ast.Module, which_calls: list[ast.Call]) -> set[str]:
    """`exe = shutil.which("xmllint")` 꼴로 **만** 대입받는 이름들.

    다른 무엇으로 한 번이라도 덮어쓰인 이름은 자격을 잃는다. 노드 비교는
    같은 파스 트리 안에서만 성립하므로 `tree`를 통째로 받는다.
    """
    sanctioned: set[str] = set()
    tainted: set[str] = set()
    for node in ast.walk(tree):
        names = _assigned_names(node)
        if not names:
            continue
        if isinstance(node.value, ast.Call) and any(
            node.value is call and _single_const(call) == XMLLINT_EXECUTABLE for call in which_calls
        ):
            sanctioned.update(names)
        else:
            tainted.update(names)
    return sanctioned - tainted


def _xmllint_argv_failures(
    rel: str, site: ast.Call, sanctioned: set[str], which_calls: list[ast.Call]
) -> list[str]:
    """호출 하나의 argv 형태를 본다 — 고정 실행파일, 고정 argv, 셸 금지."""
    where = f"{rel}:{site.lineno}"
    shell = next((kw for kw in site.keywords if kw.arg == "shell"), None)
    if shell is not None and not (
        isinstance(shell.value, ast.Constant) and shell.value.value is False
    ):
        return [f"{where} 셸은 열 수 없다 — shell은 생략하거나 리터럴 False"]
    if not site.args:
        return [f"{where} argv 없는 호출"]
    argv = site.args[0]
    if not isinstance(argv, (ast.List, ast.Tuple)):
        return [f"{where} argv는 리스트 리터럴이어야 한다 — 문자열·변수 조립 금지"]
    head = argv.elts[0] if argv.elts else None
    head_ok = isinstance(head, ast.Name) and head.id in sanctioned
    if not head_ok:
        head_ok = any(
            head is call and _single_const(call) == XMLLINT_EXECUTABLE for call in which_calls
        )
    problems = [] if head_ok else [f"{where} 실행파일이 shutil.which('xmllint') 결과가 아니다"]
    problems += [
        f"{rel}:{element.lineno} argv를 조립했다 — 고정 argv만 허용"
        for element in argv.elts
        if isinstance(element, (ast.JoinedStr, ast.BinOp, ast.Starred))
    ]
    return problems


def _xmllint_boundary_failures(rel: str, body: str) -> list[str]:
    """XML 검증 경계의 계약 — xmllint 하나, 고정 argv, 셸 금지.

    실행파일은 `shutil.which("xmllint")`로만 얻고 argv는 리스트 리터럴로 쓴다.
    명령 이름을 상수로 박거나 조립하거나 셸에 넘기는 형태는 전부 거부한다.
    """
    tree = ast.parse(body)
    failures: list[str] = []
    which_calls = _which_calls(tree)
    failures += [
        f"{rel}:{call.lineno} which가 xmllint 외 실행파일을 찾는다"
        for call in which_calls
        if _single_const(call) != XMLLINT_EXECUTABLE
    ]
    module_aliases, symbols, star_lines = _subprocess_bindings(tree)
    failures += [f"{rel}:{lineno} 별표 import는 호출 추적을 무력화한다" for lineno in star_lines]
    sanctioned = _xmllint_names(tree, which_calls)
    for call in _subprocess_calls(tree, module_aliases, symbols):
        failures += _xmllint_argv_failures(rel, call, sanctioned, which_calls)
    return failures


def check_subprocess_boundaries() -> list[str]:
    """규칙 7 — subprocess는 정확히 두 파일에서만.

    `vlogsrt/preprocess.py`(ffmpeg/ffprobe)와 `vlogsrt/project/xml_validation.py`
    (xmllint 하나, 고정 argv, 셸 금지). 파일 **이름**이 아니라 정확 경로로 판정한다.
    """
    failures: list[str] = []
    for path in _sources():
        rel = _rel(path)
        if rel in SUBPROCESS_BOUNDARY_PATHS:
            continue
        lines = {
            lineno
            for lineno, name in _import_roots(path.read_text(encoding="utf-8"))
            if name == "subprocess"
        }
        failures += [
            f"{rel}:{lineno} subprocess는 preprocess.py와 project/xml_validation.py에만"
            for lineno in sorted(lines)
        ]
    # 경계 파일이 아직 없어도 조용히 통과하지 않는다 — 위의 전역 검사가 그대로 산다.
    if XML_VALIDATION_MODULE.exists():
        failures += _xmllint_boundary_failures(
            _rel(XML_VALIDATION_MODULE), XML_VALIDATION_MODULE.read_text(encoding="utf-8")
        )
    return failures


def check_ffmpeg_boundary() -> list[str]:
    """규칙 7 — ffmpeg 호출은 preprocess.py에서만. 세부 계약은 위임한다."""
    return check_subprocess_boundaries()


def check_no_prompt_to_whisper() -> list[str]:
    """규칙 6 — whisper에 `prompt`를 보내지 마라(D13, D16).

    whisper는 `prompt`를 지시가 아니라 직전 자막 텍스트로 읽는다. 한 줄이
    `'한국 여행 브이로그'` 138회·564초를 낳았다. 힌트는 검증 모델로만 간다.
    """
    body = (PACKAGE / "transcribe.py").read_text(encoding="utf-8")
    match = re.search(r"def _extras\(.*?\n(?=\ndef |\n# )", body, re.DOTALL)
    if not match:
        return ["transcribe.py: _extras를 찾지 못했다 — 인자 검사가 무력해졌다"]
    sent = re.findall(r'extras\[["\'](\w+)["\']\]', match.group(0))
    leaked = sorted({name for name in sent if name in {"prompt", "keywords"}})
    if leaked:
        return [f"transcribe.py: 자막을 만드는 호출에 {leaked}가 실린다 (규칙 6, D16)"]
    return []


def check_one_transcription_call() -> list[str]:
    """규칙 1 — 모델은 하나다. 호출 지점이 둘이면 출처가 둘이라는 뜻이다."""
    body = (PACKAGE / "transcribe.py").read_text(encoding="utf-8")
    calls = re.findall(r"transcriptions\.create\(", body)
    if len(calls) != 1:
        return [f"transcribe.py: transcriptions.create 호출이 {len(calls)}개다 (1개여야 한다)"]
    return []


def check_no_hardcoded_model() -> list[str]:
    """규칙 6 — 모델 이름도 config에서 온다. 코드에 박으면 되돌리기가 커밋이 된다."""
    failures = []
    for path in _sources():
        rel = _rel(path)
        if rel in MODEL_LITERAL_PATHS:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if re.search(r"[\"'](whisper-1|gpt-[\w.-]*transcribe[\w.-]*)[\"']", line):
                failures.append(f"{rel}:{number} 모델 이름이 코드에 박혀 있다")
    return failures


CHECKS = (
    ("openai 경계", check_openai_boundary),
    ("ffmpeg 경계", check_ffmpeg_boundary),
    ("순수 계층", check_pure_layer),
    ("시각 유도 계층 부활 금지", check_time_deriving_layers_stay_buried),
    ("whisper에 prompt 금지", check_no_prompt_to_whisper),
    ("autojunk=False", check_autojunk),
    ("금지 의존성", check_banned_imports),
    ("의존성 3개", check_dependencies),
    ("frozen 모델", check_frozen_models),
    (".env 보호", check_env_untouched),
    ("판정 계층 부활 금지", check_inspection_stays_dead),
    ("검증은 출력을 못 건드린다", check_verify_cannot_touch_output),
    ("재전사는 전사 단계에 머문다", check_repair_stays_upstream),
    ("신뢰도 메타 보존", check_confidence_meta_survives),
    ("전사 호출 1개", check_one_transcription_call),
    ("모델 이름 비하드코딩", check_no_hardcoded_model),
    ("레거시는 project를 import하지 않는다", check_source_does_not_import_project),
    ("선택 의존성 범위", check_project_dependency_scope),
)


def main() -> int:
    failures: list[str] = []
    for name, check in CHECKS:
        found = check()
        status = "OK" if not found else "FAIL"
        print(f"[{status}] {name}")
        failures.extend(found)
    for failure in failures:
        print(f"  - {failure}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
