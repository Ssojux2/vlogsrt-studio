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


def _under_project_vad(path: Path) -> bool:
    """선택 extra의 유일한 허용 지점 — `vlogsrt/project/vad/` 디렉터리 아래다.

    VAD만 numpy·onnxruntime를 쓴다(계획서 VAD guard). 디렉터리 아래가 아니면
    같은 이름의 파일(`project/vad.py`)도 예외를 물려받지 못한다.
    """
    return path.is_relative_to(PROJECT_PACKAGE / "vad")


BANNED_IMPORTS = (
    "jamo", "konlpy", "pysrt", "ffmpeg", "pydantic", "dotenv", "regex", "numpy", "onnxruntime",
    # UI는 ui/ 에만 산다. 핵심 패키지가 gradio를 끌어오면 규칙 7이 무너진다.
    "gradio",
    # Claude는 표준 라이브러리 urllib로 부른다(D54) — SDK·HTTP 클라이언트 의존성은 없다.
    "anthropic", "httpx", "requests", "urllib3", "aiohttp",
)  # fmt: skip
ALLOWED_DEPENDENCIES = {"openai", "tomli-w", "rich"}

# 예외는 전부 **정확 상대 경로**로 판정한다. 파일 이름으로 판정하면 같은 이름의
# 파일이 예외를 물려받는다 — `vlogsrt/project/transcribe.py`가 openai를 얻는 식이다.
OPENAI_BOUNDARY_PATH = "vlogsrt/transcribe.py"
# 네트워크 모듈을 import할 수 있는 유일한 파일(D54) — Claude Messages의 HTTP 전송.
# `urllib.parse`는 네트워크가 아니라 문자열 해석이라 어디서나 쓴다.
NETWORK_BOUNDARY_PATH = "vlogsrt/anthropic_messages.py"
NETWORK_MODULES = ("urllib.request", "urllib.error", "http.client", "socket", "ssl")
# ui/의 로컬 브라우저 서버는 파일마다 정해진 네트워크 모듈 하나만 쓴다(정확 경로) —
# 인스턴스 확인(루프백 HTTP)과 빈 포트 찾기. 같은 파일의 다른 모듈은 허용하지 않는다.
UI_NETWORK_ALLOWLIST = {"ui/instance.py": "urllib.request", "ui/service.py": "socket"}
# 그 전송 파일을 import할 수 있는 유일한 파일 — 승인 문·관찰·취소를 건너뛰는 길이 없다.
CLAUDE_TRANSPORT_MODULE = "anthropic_messages"
CLAUDE_TRANSPORT_ENTRY_PATH = "vlogsrt/transcribe.py"
SUBPROCESS_BOUNDARY_PATHS = ("vlogsrt/preprocess.py", "vlogsrt/project/xml_validation.py")

# 제거 계층(규칙 4·9) — P6는 이 프로젝트에서 처음으로 구간을 실제 제거하는 계층이다.
# edit.py는 오디오·전사·VAD·조판 계층을 import하지 못한다. 유일한 예외는
# FrameSampleRequest 데이터 타입 하나: **타입은 이음매를 건너도 되고 행동은 안
# 된다** — 브리프 계약이 EditBlock에 그 순수 기록을 실으므로(코디네이터 재결).
EDIT_MODULE_PATH = PROJECT_PACKAGE / "edit.py"
REMOVAL_LAYER_MODULES = ("edit.py", "export.py")
"""제거 계층의 파일들 — P6-02 계획과 P6-04 내보내기. 같은 선을 함께 지킨다.

export.py도 이 계획을 직렬화한다 — 오디오·전사·판정 계층을 알면 렌더가
판정에 오염된다. 경로는 호출 시점에 PROJECT_PACKAGE에서 푼다(가짜 트리 시험이
PROJECT_PACKAGE를 통째로 바꾸므로 상수로 두면 진짜 트리을 보게 된다).
"""
EDIT_BANNED_IMPORTS = (
    "subprocess", "openai", "numpy", "onnxruntime",
    "vlogsrt.transcribe", "vlogsrt.preprocess",
    "vlogsrt.project.pcm", "vlogsrt.project.speech",
    "vlogsrt.project.vad.inference", "vlogsrt.project.vad.runtime",
    "vlogsrt.project.vision.sampling",
    "vlogsrt.repetition", "vlogsrt.repair", "vlogsrt.refine",
    "vlogsrt.output", "vlogsrt.layout", "vlogsrt.anthropic_messages",
)  # fmt: skip
EDIT_SAMPLE_TYPE_MODULE = "vlogsrt.project.vision.sampling"
EDIT_SAMPLE_TYPE = "FrameSampleRequest"
EDIT_PACKAGE = "vlogsrt.project"

MODEL_LITERAL_PATHS = (
    "vlogsrt/config.py",
    "vlogsrt/project/config.py",
    "vlogsrt/project/raw/config.py",
)
# glm-*·gemini-*은 파생 계층의 비전 모델이다 — 새 튜너블은 프로젝트 config에만 산다(재검토).
# 공급자를 바꿔도 난간은 좁아지지 않는다: 쓰지 않게 된 접두사도 목록에 남겨 둔다.
PROJECT_MODEL_LITERAL_PATHS = ("vlogsrt/project/config.py",)
# 원본 영상 브라우저 v2의 모델 리터럴(문구·시간·의미 분석 역할)은 새 설정 경로
# 하나에만 산다(2026-10-02 계획 §3.2·§15.1). 다른 config도 이 예외를 물려받지
# 않는다 — 중첩 경로의 같은 이름 파일도 마찬가지다.
RAW_MODEL_LITERAL_PATHS = ("vlogsrt/project/raw/config.py",)
ENGINE_MODEL_LITERAL = re.compile(r"[\"'](whisper-1|gpt-[\w.-]*transcribe[\w.-]*)[\"']")
PROJECT_MODEL_LITERAL = re.compile(r"[\"'](?:glm|gemini)-[\w.-]+[\"']")
RAW_MODEL_LITERAL = re.compile(r"[\"']gpt-6-luna[\w.-]*[\"']")
# Claude 모델 이름(D54)도 raw 설정 경로 하나에만 산다 — 등록표가 단가와 함께 정한다.
CLAUDE_MODEL_LITERAL = re.compile(r"[\"']claude-[\w.-]+[\"']")
# 레거시가 project를 import할 수 있는 유일한 진입점 — 문서화된 CLI 조립 지점.
# 예외도 정확히 `project.commands` 모듈로 좁다: CLI는 명령 묶음만 조립한다(재조정).
CLI_COMPOSITION_ENTRYPOINT = "vlogsrt/cli.py"
CLI_ALLOWED_PROJECT_TARGET: tuple[str, ...] = ("commands",)
# 브라우저 작업공간 서비스의 진입점 — v2 계획이 정확 경로 예외로 허가했다
# (2026-10-02 계획 §3.2). 예외도 정확히 `project.raw.service` 모듈로 좁고,
# 상대 import는 인정하지 않는다: ui/는 vlogsrt 패키지 안이 아니라 `.project`가
# vlogsrt.project로 귀결하지 않는다.
UI_RAW_SERVICE_ENTRYPOINT = "ui/workspace_service.py"
UI_ALLOWED_PROJECT_TARGET: tuple[str, ...] = ("raw", "service")
XMLLINT_EXECUTABLE = "xmllint"
# XML 검증 경계의 subprocess API는 run 하나로 얼어 있다(재검토). 별명은 정적으로 푼다.
SUBPROCESS_API_SYMBOLS = ("run", "Popen", "call", "check_call", "check_output")


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


def _network_root(name: str) -> str | None:
    """점 이름이 속한 네트워크 모듈 — `socket.socket`은 `socket`, 아니면 None."""
    return next(
        (root for root in NETWORK_MODULES if name == root or name.startswith(root + ".")), None
    )


def _static_import_names(tree: ast.Module) -> list[tuple[int, str]]:
    """정적 import의 (줄번호, 점 이름) — `from urllib import request`는 `urllib.request`로."""
    rows: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            rows.extend((node.lineno, alias.name) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            rows.append((node.lineno, node.module))
            rows.extend((node.lineno, f"{node.module}.{alias.name}") for alias in node.names)
    return rows


def _dynamic_import_names(tree: ast.Module) -> list[tuple[int, str]]:
    """문자열 상수로 부르는 import — `importlib.import_module("…")`·`__import__("…")`.

    `from importlib import import_module as im`처럼 별명으로 묶은 이름도 같은 호출이다.
    상수가 아닌 인자(f-string·변수)는 판정하지 않는다 — 정적으로 이름을 알 수 없다.
    """
    importers = {"import_module", "__import__"} | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "importlib"
        for alias in node.names
        if alias.name == "import_module" and alias.asname
    }
    rows: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        # 속성 호출(`importlib.import_module`·`builtins.__import__`)은 이름 그대로, 맨 이름
        # 호출은 별명까지 본다 — 별명은 그 파일이 importlib에서 묶은 것뿐이다.
        called = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        target = getattr(node.args[0], "value", None)
        if called in importers and isinstance(target, str):
            rows.append((node.lineno, target))
    return rows


def _package_attribute_names(tree: ast.Module) -> list[tuple[int, str]]:
    """맨 패키지 import 뒤의 하위 모듈 접근 — `import urllib` 다음 `urllib.request.…`.

    `import urllib.parse`도 이름 `urllib`을 묶는다 — 그 뒤의 `urllib.request`는 같은 우회다.
    """
    bound: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is None:
                    bound[alias.name.split(".")[0]] = alias.name.split(".")[0]
                elif "." not in alias.name:
                    bound[alias.asname] = alias.name
    return [
        (node.lineno, f"{bound[node.value.id]}.{node.attr}")
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in bound
    ]


def _network_imports(body: str) -> list[tuple[int, str]]:
    """네트워크 모듈 사용 (줄번호, 모듈) — 정적·문자열 상수 import·패키지 속성 접근 전부.

    한 파일에서 같은 모듈은 처음 한 번만 센다.
    """
    tree = ast.parse(body)
    names = [
        *_static_import_names(tree),
        *_dynamic_import_names(tree),
        *_package_attribute_names(tree),
    ]
    seen: dict[str, int] = {}
    for lineno, name in sorted(names):
        root = _network_root(name)
        if root is not None:
            seen.setdefault(root, lineno)
    return sorted((lineno, root) for root, lineno in seen.items())


def check_network_boundary() -> list[str]:
    """규칙 7(D54) — 네트워크 모듈은 정확 경로 하나에서만. 같은 이름의 다른 경로는 안 된다.

    ui/도 검사한다 — 로컬 브라우저 서버의 두 파일만 각자 정해진 모듈 하나를 쓴다
    (`UI_NETWORK_ALLOWLIST`, 정확 경로). 일괄 면제는 없다.
    """
    failures = []
    for path in _sources() + sorted(UI_DIR.rglob("*.py")):
        rel = _rel(path)
        if rel == NETWORK_BOUNDARY_PATH:
            continue
        allowed = UI_NETWORK_ALLOWLIST.get(rel)
        failures.extend(
            f"{rel}:{lineno} 네트워크 모듈 {name}은 {NETWORK_BOUNDARY_PATH}에만"
            for lineno, name in _network_imports(path.read_text(encoding="utf-8"))
            if name != allowed
        )
    return failures


def _imports_claude_transport(body: str) -> list[int]:
    """전송 파일에 닿는 줄 — 절대·상대·모듈 핸들·하위 이름·문자열 상수·패키지 속성 전부.

    패키지 속성(`vlogsrt.anthropic_messages`)은 누가 한 번 import한 뒤에는 실제로 닿는다 —
    어떤 객체의 속성으로든 그 이름을 부르면 잡는다.
    """
    tree = ast.parse(body)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            parts = [part for alias in node.names for part in alias.name.split(".")]
        elif isinstance(node, ast.ImportFrom):
            parts = [*(node.module or "").split("."), *(alias.name for alias in node.names)]
        elif isinstance(node, ast.Attribute):
            parts = [node.attr]
        else:
            continue
        if CLAUDE_TRANSPORT_MODULE in parts:
            lines.append(node.lineno)
    lines.extend(
        lineno
        for lineno, name in _dynamic_import_names(tree)
        if CLAUDE_TRANSPORT_MODULE in name.split(".")
    )
    return sorted(lines)


def check_claude_transport_entry() -> list[str]:
    """규칙 7(D54) — Claude HTTP 전송은 transcribe.py만 안다. 승인 문을 건너뛰는 길이 없다."""
    failures = []
    for path in _sources() + sorted(UI_DIR.rglob("*.py")):
        rel = _rel(path)
        if rel in (CLAUDE_TRANSPORT_ENTRY_PATH, NETWORK_BOUNDARY_PATH):
            continue
        failures.extend(
            f"{rel}:{lineno} {CLAUDE_TRANSPORT_MODULE}는 {CLAUDE_TRANSPORT_ENTRY_PATH}만 import한다"
            for lineno in _imports_claude_transport(path.read_text(encoding="utf-8"))
        )
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

    numpy·onnxruntime는 선택 extra다 — VAD가 쓰므로 `vlogsrt/project/vad/`
    아래에서만 허용한다(재검토). 나머지 금지 이름은 project 안을 포함해
    어디서도 허용하지 않는다.
    """
    failures = []
    for path in _sources():
        rel = _rel(path)
        for lineno, name in _import_roots(path.read_text(encoding="utf-8")):
            if name not in BANNED_IMPORTS:
                continue
            if name in PROJECT_OPTIONAL_DEPENDENCIES and _under_project_vad(path):
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


def _dataclass_bindings(tree: ast.Module) -> tuple[set[str], set[str], set[str], set[str]]:
    """dataclasses 묶음 — (참 모듈 별명들, 참 dataclass 별명들, 묶였던 모듈·심볼 이름 전부).

    `import dataclasses as dc`의 `@dc.dataclass`와 `from dataclasses import
    dataclass as dc`의 `@dc` 같은 평범한 별명까지 알아본다(재조정). 한 번이라도
    다시 묶이면(그림자) 자격을 끊는다.
    """
    module_kinds: dict[str, set[str]] = {}
    symbol_kinds: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "dataclasses":
                    module_kinds.setdefault(alias.asname or "dataclasses", set()).add("import")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "dataclasses":
            for alias in node.names:
                if alias.name == "dataclass":
                    symbol_kinds.setdefault(alias.asname or "dataclass", set()).add("import")
    for node in ast.walk(tree):
        for name in _rebound_names(node):
            if name in module_kinds:
                module_kinds[name].add("shadow")
            if name in symbol_kinds:
                symbol_kinds[name].add("shadow")
    genuine_modules = {name for name, kinds in module_kinds.items() if kinds == {"import"}}
    genuine_symbols = {name for name, kinds in symbol_kinds.items() if kinds == {"import"}}
    return genuine_modules, genuine_symbols, set(module_kinds), set(symbol_kinds)


def _dataclass_decorators(
    node: ast.ClassDef,
    genuine_modules: set[str],
    genuine_symbols: set[str],
    ever_modules: set[str],
    ever_symbols: set[str],
) -> list[tuple[ast.expr, str | None]]:
    """클래스의 dataclass 장식자들 — (장식자, 문제) 쌍. 문제 None이면 참 dataclass다.

    dataclass 모양인데 출처를 입증 못 하면(덮어쓰인 별명·수입 증명 없음) 그대로
    거부한다. 별명이 아닌 다른 이름까지 좇지는 않는다 — 이 정도가 된 AST 검사다.
    """
    found: list[tuple[ast.expr, str | None]] = []
    for decorator in node.decorator_list:
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Attribute) and target.attr == "dataclass":
            if isinstance(target.value, ast.Name) and (
                target.value.id == "dataclasses" or target.value.id in ever_modules
            ):
                problem = None if target.value.id in genuine_modules else "장식자 출처 불명"
                found.append((decorator, problem))
        elif isinstance(target, ast.Name) and (
            target.id == "dataclass" or target.id in ever_symbols
        ):
            problem = None if target.id in genuine_symbols else "장식자 출처 불명"
            found.append((decorator, problem))
    return found


def _frozen_literal(decorator: ast.expr) -> bool:
    """장식자가 frozen=True 리터럴을 담았는가 — bare 장식·변수 참조는 가변으로 본다."""
    if not isinstance(decorator, ast.Call):
        return False
    return any(
        keyword.arg == "frozen"
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in decorator.keywords
    )


def check_project_models_frozen() -> list[str]:
    """파생 계층의 데이터클래스도 frozen이다 — 정확 경로 `vlogsrt/project/models.py`.

    파일이 없는 동안은 통과가 맞다 — P0-04가 만들 때 이 선이 먼저 서 있다.
    frozen은 선언의 얕은 불변이다: 필드가 리스트를 들면 내용까지는 못 지킨다.
    깊은 불변은 행동 테스트의 몫이지 이 검사가 증명하는 게 아니다.
    """
    module = PROJECT_PACKAGE / "models.py"
    if not module.exists():
        return []
    tree = ast.parse(module.read_text(encoding="utf-8"))
    genuine_modules, genuine_symbols, ever_modules, ever_symbols = _dataclass_bindings(tree)
    failures = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for decorator, problem in _dataclass_decorators(
            node, genuine_modules, genuine_symbols, ever_modules, ever_symbols
        ):
            if problem is not None:
                failures.append(f"{_rel(module)}:{node.lineno} {node.name} {problem}")
            elif not _frozen_literal(decorator):
                failures.append(
                    f"{_rel(module)}:{node.lineno} {node.name} 데이터클래스가 frozen=True가 아니다"
                )
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
    예외는 둘이고 둘 다 정확 경로다 — CLI 조립 진입점 `vlogsrt/cli.py`는
    **정확히 `project.commands` 모듈**, 브라우저 작업공간 진입점
    `ui/workspace_service.py`는 **정확히 `project.raw.service` 모듈**까지다
    (2026-10-02 계획 §3.2). 어느 쪽 예외도 하위 모듈(commands.parser·
    raw.service.models)과 패키지 핸들은 거부한다. UI 예외는 절대 형태만 인정한다 —
    ui/는 vlogsrt 안이 아니라 상대 형태가 vlogsrt.project로 귀결하지 않는다.
    방향은 project → 레거시 공개 경계로만 열려 있다.
    """
    failures = []
    for path in _legacy_sources():
        rel = _rel(path)
        allowance = _legacy_project_import_allowance(rel)
        for lineno, form, level, tail, names in _project_imports(path.read_text(encoding="utf-8")):
            if allowance is not None and _import_targets_allowed_module(
                level, tail, names, allowance
            ):
                continue
            failures.append(f"{rel}:{lineno} {_project_import_failure_reason(rel)} ({form})")
    return failures


def _legacy_project_import_allowance(rel: str) -> tuple[tuple[str, ...], bool] | None:
    """정확 경로 예외의 허용 모듈 — (프로젝트 아래 경로, 절대 형태 한정 여부).

    CLI는 vlogsrt 패키지 안에 살아 `.project` 상대 형태가 project로 귀결한다.
    ui/는 그렇지 않으므로 절대 형태만 인정한다.
    """
    if rel == CLI_COMPOSITION_ENTRYPOINT:
        return CLI_ALLOWED_PROJECT_TARGET, False
    if rel == UI_RAW_SERVICE_ENTRYPOINT:
        return UI_ALLOWED_PROJECT_TARGET, True
    return None


def _import_targets_allowed_module(
    level: int,
    tail: tuple[str, ...],
    names: tuple[str, ...],
    allowance: tuple[tuple[str, ...], bool],
) -> bool:
    """import 하나가 허용 모듈로 귀결하는가 — 모듈 자체만, 하위·패키지 핸들은 안 된다.

    `from …project.raw import service`는 마지막 한 칸을 가져온 이름이 채워
    `project.raw.service`로 귀결하므로 통과한다(가져온 이름 전부가 그 한 칸일 때만).
    """
    allowed, absolute_only = allowance
    if absolute_only and level != 0:
        return False
    if tail == allowed:
        return True
    return tail == allowed[:-1] and bool(names) and all(name == allowed[-1] for name in names)


def _project_import_failure_reason(rel: str) -> str:
    if rel == CLI_COMPOSITION_ENTRYPOINT:
        return "CLI 예외는 project.commands로 좁다"
    if rel == UI_RAW_SERVICE_ENTRYPOINT:
        return "UI 예외는 절대 형태의 project.raw.service로 좁다"
    return "레거시가 vlogsrt.project를 import한다"


def check_project_dependency_scope() -> list[str]:
    """선택 extra numpy·onnxruntime는 vlogsrt/project/vad/ 아래에서만 쓸 수 있다.

    legacy(vlogsrt 최상위·ui)뿐 아니라 파생(vlogsrt/project/) 소스도 훑는다 —
    extra는 project 어디에나 스며들 수 있고, 허용 지점은 vad/ 디렉터리 아래뿐이다.
    legacy만 보면 `project/vision/policy.py`의 onnxruntime가 조용히 지나간다(P0-02c).
    """
    failures = []
    for path in _legacy_sources() + _project_sources():
        rel = _rel(path)
        for lineno, name in _import_roots(path.read_text(encoding="utf-8")):
            if name in PROJECT_OPTIONAL_DEPENDENCIES and not _under_project_vad(path):
                failures.append(
                    f"{rel}:{lineno} 선택 의존성 {name}은 vlogsrt/project/vad/ 아래에서만"
                )
    return failures


ProjectImportRef = tuple[int, str, int, tuple[str, ...], tuple[str, ...]]
"""project를 참조하는 import문 하나 — (줄번호, 형태 서술, 상대 단계, 프로젝트 아래
모듈 경로, 가져온 이름들). 경로가 비면 project 패키지(또는 그 핸들) 자체다."""


def _project_imports(body: str) -> list[ProjectImportRef]:
    """vlogsrt.project를 참조하는 import문 전부 — 절대·상대·별명을 AST로 뽑는다.

    허용 판정은 `_import_targets_allowed_module`이 파일별 허용 모듈로 내린다 —
    CLI는 `project.commands`, 브라우저 작업공간 서비스는 `project.raw.service`
    (2026-10-02 계획 §3.2). 어느 쪽도 하위 모듈이나 패키지 핸들은 통과하지 못한다.
    """
    hits: list[ProjectImportRef] = []
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[:2] == ["vlogsrt", "project"]:
                    hits.append((node.lineno, f"import {alias.name}", 0, tuple(parts[2:]), ()))
        elif isinstance(node, ast.ImportFrom):
            hit = _project_importfrom(node)
            if hit is not None:
                hits.append(hit)
    return hits


def _project_importfrom(node: ast.ImportFrom) -> ProjectImportRef | None:
    module = node.module or ""
    dots = "." * node.level
    if _targets_project_package(module, node.level):
        # from 모듈의 프로젝트 아래 경로. `import`문과 달리 이름이 마지막 한 칸을
        # 채울 수 있다 — 허용 판정이 그 조합을 본다.
        tail = tuple(module.split(".")[2 if node.level == 0 else 1 :])
        names = tuple(alias.name for alias in node.names)
        return node.lineno, f"from {dots}{module} import …", node.level, tail, names
    if module in ("", "vlogsrt") and any(alias.name == "project" for alias in node.names):
        # project 패키지 핸들을 직접 묶는 형태 — 경로 없음(패키지 자체)으로 귀결한다.
        return (
            node.lineno,
            f"from {dots}{module or 'vlogsrt'} import project",
            node.level,
            (),
            ("project",),
        )
    return None


def _targets_project_package(module: str, level: int) -> bool:
    if level == 0:
        return module == "vlogsrt.project" or module.startswith("vlogsrt.project.")
    return module == "project" or module.startswith("project.")


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

# 글자 교정은 **계획만 세우는 순수 함수**다(D37). 응답 형태·호출·조판·재전사를 알면
# 교체 판정이 다른 계층의 행동에 기대게 된다 — 입력은 값으로만 받는다.
RETEXT_MUST_NOT_IMPORT = (
    "transcribe", "preprocess", "verify", "refine", "output", "layout", "repair", "polish",
    "openai", "subprocess", "anthropic_messages",
)  # fmt: skip

# 3단 등급은 **저장된 분석 문서(AI 판정 + 생성 자막) 위의 결정적 재계산**이다(D51·D52).
# 프로세스·제공자·서비스·검토 저장소를 알면 같은 증거가 다른 등급을 낳거나 등급 계산이 유료
# 호출이나 로컬 측정으로 번진다. 문서 읽기는 `service.py`의 몫이다.
TIERING_MUST_NOT_IMPORT = (
    "subprocess", "openai", "service", "review_workspace", "understanding_luna", "preprocess",
    "anthropic_messages",
)  # fmt: skip
TIERING_FILE_IO = re.compile(r"\.(open|read_text|read_bytes|write_text|write_bytes)\(|\bopen\(")

# Final Cut 타임라인 빌더는 **값만 받아 바이트를 돌려주는 순수 함수**다(D56). 원본·라이브러리·
# 검토 저장소·ffprobe를 알면 같은 계획이 디스크 상태에 따라 다른 문서를 낳고, 확정 패키지를
# 알면 지우지 않는 보내기가 잘라내는 내보내기의 관문에 다시 묶인다. `clips`·`media`는 그
# 자체가 ffprobe·내용 해시 경계를 끌고 오므로 함께 막는다 — 철자 표는 `fcp_tables`에서 읽는다.
# 모으고 쓰는 일은 내보내기 계층(`finalcut_export.py`)의 몫이다.
FINALCUT_TIMELINE_MUST_NOT_IMPORT = (
    "subprocess", "openai", "os", "shutil", "tempfile", "sqlite3", "service",
    "review_workspace", "preprocess", "confirmed_package", "library", "media_refs", "clips",
    "media",
)  # fmt: skip
# Final Cut 보내기(D56)는 원본을 **읽지 않는다** — 29GB를 다시 해시하거나 복사하는 길이 생기면
# 지우지 않는 보내기가 확정 패키지(잘라내는 내보내기)처럼 무거워지고, 원본 대신 사본을 가리킬 수
# 있다. 저장된 해시끼리와 stat만 대조한다. 덮어쓰는 `os.replace`도 막는다 — 보내기는 언제나
# 새 파일이다(`os.link`는 대상이 있으면 실패한다). ffprobe는 `preprocess`, xmllint는
# `xml_validation`을 지난다.
FINALCUT_EXPORT_MUST_NOT_IMPORT = (
    "shutil", "hashlib", "subprocess", "confirmed_package", "final_cut_handoff", "clips",
    "media",
)  # fmt: skip
FINALCUT_EXPORT_FORBIDDEN = re.compile(
    r"\b(probe_clips|sha256_file|_sha256_file|_facts|_verified_asset|export_confirmed"
    r"|copyfile|copy2|copytree)\b|\bos\.(replace|rename)\(|\.(replace|rename)\("
)
"""`os.replace`·`os.rename`·`Path.replace`·`Path.rename`은 대상이 있으면 덮어쓴다. 텍스트로는
`str.replace`와 갈라 볼 수 없으므로 이 모듈에서는 `.replace(` 자체를 쓰지 않는다."""

FINALCUT_TIMELINE_FILE_IO = re.compile(
    r"\.(open|read_text|read_bytes|write_text|write_bytes|write|exists|is_file|is_dir|stat"
    r"|lstat|resolve|iterdir|glob|rglob|unlink|mkdir|touch|rename)\(|\bopen\("
)

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


def check_retext_stays_pure() -> list[str]:
    """D37 — `retext.py`는 세그먼트·조각 글자·repairs를 **값으로** 받아 계획만 돌려준다.

    파일이 사라지면 조용히 통과하지 않는다 — 교정 권한이 어디로 옮겨 갔는지 모르게 된다.
    """
    path = PACKAGE / "retext.py"
    if not path.exists():
        return ["vlogsrt/retext.py: 글자 교정 계층이 사라졌다 (D37)"]
    return [
        f"retext.py: {name}을 import한다 — 교정 계획이 순수 함수가 아니게 된다 (D37)"
        for name in _imports(path.read_text(encoding="utf-8"), RETEXT_MUST_NOT_IMPORT)
    ]


def check_tiering_stays_pure() -> list[str]:
    """D51·D52 — `tiering.py`는 증거 문서를 **값으로** 받아 등급만 돌려준다.

    파일이 사라지면 조용히 통과하지 않는다 — 등급 권한이 어디로 옮겨 갔는지 모르게 된다.
    """
    path = PROJECT_PACKAGE / "raw" / "tiering.py"
    if not path.exists():
        return ["vlogsrt/project/raw/tiering.py: 3단 등급 계층이 사라졌다 (D51)"]
    body = path.read_text(encoding="utf-8")
    failures = [
        f"tiering.py: {name}을 import한다 — 등급 판정이 순수 함수가 아니게 된다 (D51)"
        for name in _imports(body, TIERING_MUST_NOT_IMPORT)
    ]
    failures += [
        f"tiering.py:{number}: 파일을 직접 읽거나 쓴다 — 입출력은 service.py의 몫이다 (D51)"
        for number, line in enumerate(body.splitlines(), start=1)
        if TIERING_FILE_IO.search(line)
    ]
    return failures


def check_finalcut_timeline_stays_pure() -> list[str]:
    """D56 — `finalcut_timeline.py`는 원본 사실·조각 상태·등급·자막을 **값으로** 받아 문서만 낸다.

    파일이 사라지면 조용히 통과하지 않는다 — 문서를 짜는 권한이 어디로 옮겨 갔는지 모르게 된다.
    """
    path = PROJECT_PACKAGE / "raw" / "finalcut_timeline.py"
    if not path.exists():
        return [
            "vlogsrt/project/raw/finalcut_timeline.py: Final Cut 타임라인 빌더가 사라졌다 (D56)"
        ]
    body = path.read_text(encoding="utf-8")
    failures = [
        f"finalcut_timeline.py: {name}을 import한다 — 타임라인 빌더가 순수 함수가 아니게 된다 (D56)"
        for name in _imports(body, FINALCUT_TIMELINE_MUST_NOT_IMPORT)
    ]
    failures += [
        f"finalcut_timeline.py:{number}: 파일을 직접 읽거나 쓴다 — 입출력은 내보내기 계층의 몫이다"
        " (D56)"
        for number, line in enumerate(body.splitlines(), start=1)
        if FINALCUT_TIMELINE_FILE_IO.search(line)
    ]
    return failures


# D56이 대신한 옛 「선택한 컷 Final Cut으로 출력」 길 — 확정 패키지를 뽑고(materialize) 그 옆에
# XML을 썼다. 지금 보내기는 원본 전체를 지우지 않고 보낸다. 옛 이름이 돌아오면 미검토 관문이
# 걸린 두 갈래 출력이 다시 생긴다.
OLD_FINALCUT_MODULE = "project/raw/final_cut_handoff.py"
OLD_FINALCUT_NAMES = re.compile(
    r"\b(export_final_cut|materialize_review|capcut_handoff_view|capcut_open_view)\b"
)


def check_old_finalcut_path_stays_dead() -> list[str]:
    """D56 — 옛 출력 모듈·이름이 `vlogsrt`·`ui`에 돌아오지 않는다."""
    failures = []
    if (PACKAGE / OLD_FINALCUT_MODULE).exists():
        failures.append(
            f"vlogsrt/{OLD_FINALCUT_MODULE}: 지운 옛 Final Cut 출력 모듈이 돌아왔다 (D56)"
        )
    for path in sorted([*PACKAGE.rglob("*.py"), *UI_DIR.rglob("*.py")]):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if OLD_FINALCUT_NAMES.search(line):
                failures.append(
                    f"{_rel(path)}:{number}: 옛 「선택한 컷」 Final Cut 출력 이름이 돌아왔다 (D56)"
                )
    return failures


def check_finalcut_export_never_copies() -> list[str]:
    """D56 — `finalcut_export.py`는 원본을 복사·재해시하지 않고, 쓴 파일을 덮어쓰지 않는다.

    파일이 사라지면 조용히 통과하지 않는다 — 보내기가 어디로 옮겨 갔는지 모르게 된다.
    """
    path = PROJECT_PACKAGE / "raw" / "finalcut_export.py"
    if not path.exists():
        return ["vlogsrt/project/raw/finalcut_export.py: Final Cut 보내기 계층이 사라졌다 (D56)"]
    body = path.read_text(encoding="utf-8")
    failures = [
        f"finalcut_export.py: {name}을 import한다 — 보내기가 원본을 복사·재해시할 수 있다 (D56)"
        for name in _imports(body, FINALCUT_EXPORT_MUST_NOT_IMPORT)
    ]
    failures += [
        f"finalcut_export.py:{number}: 재해시·복사·덮어쓰기 경로를 부른다 (D56)"
        for number, line in enumerate(body.splitlines(), start=1)
        if FINALCUT_EXPORT_FORBIDDEN.search(line)
    ]
    return failures


# 자막 시각 확인(D55)은 **표시만** 한다 — 순수 계산은 프로세스·모델·전사·조판을 모르고, 결과는
# 등급·자동 정리·선택·출력·분할로 흘러가지 않으며, 기준은 어느 분석·전사·자막 지문에도 없다.
SYNC_CHECK_MUST_NOT_IMPORT = (
    "subprocess", "openai", "numpy", "onnxruntime", "preprocess", "output", "refine", "repair",
    "transcribe", "service", "sync_report", "source_transcript",
)  # fmt: skip
SYNC_CHECK_IMPORTERS = (
    "vlogsrt/project/raw/sync_report.py",
    "vlogsrt/project/raw/service.py",
    "vlogsrt/project/raw/commands.py",
    "vlogsrt/project/commands.py",
)
SYNC_REPORT_IMPORTERS = (
    "vlogsrt/project/raw/service.py",
    "vlogsrt/project/raw/commands.py",
    "vlogsrt/project/commands.py",
)
SYNC_REPORT_MUST_NOT_IMPORT = (
    "output", "refine", "repair", "caption_artifacts", "caption_review", "final_caption",
    "finalcut_export", "finalcut_timeline", "tiering", "auto_cleanup", "segmentation",
    "selection_analysis",
)  # fmt: skip
# SQL 쓰기 문장의 모양 — 대소문자 무관. `dict.update(`·`replace(...)`는 SQL이 아니라 걸리지 않는다.
SYNC_WRITES_SQL = re.compile(
    r"\b(INSERT\s+(OR\s+\w+\s+)?INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|REPLACE\s+INTO)\b",
    re.IGNORECASE,
)
# sync_report가 원본 자막에서 가져올 수 있는 것 — 이름으로 가져온 읽기 함수뿐(모듈 객체는 안 된다).
SYNC_TRANSCRIPT_READS = ("load",)
# 이 파일들은 분석·전사·자막의 지문·캐시 열쇠를 만든다 — 확인 기준을 알면 기준을 바꿀 때마다
# 이미 받은 결과가 낡는다(D47).
SYNC_FREE_FINGERPRINT_PATHS = (
    "vlogsrt/project/config.py:ProjectConfig",
    "vlogsrt/project/raw/config.py",
    "vlogsrt/project/raw/analysis_config.py",
    "vlogsrt/project/raw/analysis_approval.py",
    "vlogsrt/project/raw/analysis_workflow.py",
    "vlogsrt/project/raw/understanding.py",
    "vlogsrt/project/raw/final_caption.py",
    "vlogsrt/project/raw/caption_workspace.py",
)
SYNC_NAMES = re.compile(r"SyncCheckSettings|SYNC_CHECK_SETTINGS|sync_check|sync_report")


def check_sync_check_stays_pure() -> list[str]:
    """D55 — `sync_check.py`는 큐·말소리 칸을 **값으로** 받아 판정만 돌려준다(입출력 없음)."""
    path = PROJECT_PACKAGE / "sync_check.py"
    if not path.exists():
        return ["vlogsrt/project/sync_check.py: 자막 시각 확인 계층이 사라졌다 (D55)"]
    body = path.read_text(encoding="utf-8")
    failures = [
        f"sync_check.py: {name}을 import한다 — 시각 확인이 순수 함수가 아니게 된다 (D55)"
        for name in _imports(body, SYNC_CHECK_MUST_NOT_IMPORT)
    ]
    failures += [
        f"sync_check.py:{number}: 파일을 직접 읽거나 쓴다 — 입출력은 sync_report.py의 몫이다 (D55)"
        for number, line in enumerate(body.splitlines(), start=1)
        if TIERING_FILE_IO.search(line)
    ]
    return failures


def check_sync_check_only_displays() -> list[str]:
    """D55 — 확인 결과는 정해진 곳에서만 읽히고, 자막·전사·지문을 쓰지 않는다."""
    failures: list[str] = []
    for path in _project_sources():
        rel = _rel(path)
        imported = _imported_names(path.read_text(encoding="utf-8"))
        if (
            "sync_check" in imported
            and rel not in SYNC_CHECK_IMPORTERS
            and rel != _rel(PROJECT_PACKAGE / "sync_check.py")
        ):
            failures.append(f"{rel}: sync_check를 import한다 — 판정이 표시 밖으로 샌다 (D55)")
        if "sync_report" in imported and rel not in SYNC_REPORT_IMPORTERS:
            failures.append(f"{rel}: sync_report를 import한다 — 판정이 표시 밖으로 샌다 (D55)")
    failures += _sync_report_failures()
    failures += _sync_fingerprint_failures()
    return failures


def _sync_report_failures() -> list[str]:
    path = PROJECT_PACKAGE / "raw" / "sync_report.py"
    if not path.exists():
        return ["vlogsrt/project/raw/sync_report.py: 자막 시각 확인 보고서 계층이 사라졌다 (D55)"]
    body = path.read_text(encoding="utf-8")
    failures = [
        f"sync_report.py: {name}을 import한다 — 확인이 자막·출력 계층을 안다 (D55)"
        for name in _imports(body, SYNC_REPORT_MUST_NOT_IMPORT)
    ]
    failures += _sync_transcript_import_failures(ast.parse(body))
    failures += [
        f"sync_report.py:{number}: DB에 쓰는 SQL이 있다 — 확인은 보고서 파일만 쓴다 (D55)"
        for number, line in enumerate(body.splitlines(), start=1)
        if SYNC_WRITES_SQL.search(line)
    ]
    return failures


def _sync_transcript_import_failures(tree: ast.Module) -> list[str]:
    """원본 자막은 이름으로 가져온 읽기 함수로만 — 모듈 객체를 쥐면 `.edit()`·`.seed()`가 열린다."""
    failures: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            failures += [
                f"sync_report.py:{node.lineno}: source_transcript 모듈을 들여온다 — "
                "읽기 함수만 이름으로 가져온다 (D55)"
                for alias in node.names
                if alias.name.split(".")[-1] == "source_transcript"
            ]
        elif isinstance(node, ast.ImportFrom):
            from_transcript = (node.module or "").split(".")[-1] == "source_transcript"
            failures += [
                f"sync_report.py:{node.lineno}: source_transcript"
                f"{'.' + alias.name if from_transcript else ''}를 가져온다 — "
                f"읽기({', '.join(SYNC_TRANSCRIPT_READS)})만 허용이다 (D55)"
                for alias in node.names
                if (from_transcript and alias.name not in SYNC_TRANSCRIPT_READS)
                or (not from_transcript and alias.name == "source_transcript")
            ]
    return failures


def _sync_fingerprint_failures() -> list[str]:
    failures: list[str] = []
    for entry in SYNC_FREE_FINGERPRINT_PATHS:
        rel, _, class_name = entry.partition(":")
        path = ROOT / rel
        if not path.exists():
            continue
        body = path.read_text(encoding="utf-8")
        if class_name:
            body = _class_source(body, class_name)
        if SYNC_NAMES.search(body):
            failures.append(
                f"{entry}: 자막 시각 확인 기준을 안다 — 지문·캐시 열쇠 밖이어야 한다 (D55)"
            )
    return failures


def _class_source(body: str, class_name: str) -> str:
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return ast.get_source_segment(body, node) or ""
    return ""


def check_edit_layer_ignores_audio() -> list[str]:
    """규칙 4·9 — 제거 계층(edit.py·export.py)은 오디오·전사·판정 계층을 import하지 못한다.

    데이터 타입은 이음매를 건너도 되고 행동은 안 된다. 유일한 허용 형태는
    `from vlogsrt.project.vision.sampling import FrameSampleRequest` 한 줄이다 —
    EditBlock 계약이 실는 순수 기록(frame_id·source_id·source_time). 모듈 통째로
    드는 형태와 그 밖의 이름(sample_frames 같은 추출·프로브 함수)은 전부 거부한다.
    상대 import·별명도 풀 경로로 귀결해 잡는다. 파일이 없는 동안은 통과다 —
    P6-01·P6-04가 만들 때 이 선이 먼저 서 있다.
    """
    failures: list[str] = []
    for name in REMOVAL_LAYER_MODULES:
        path = PROJECT_PACKAGE / name
        if not path.exists():
            continue
        rel = _rel(path)
        failures.extend(
            failure
            for lineno, module, names in _edit_imports(path.read_text(encoding="utf-8"))
            for failure in _edit_import_failures(rel, lineno, module, names)
        )
    return failures


def _edit_imports(body: str) -> list[tuple[int, str, tuple[str, ...]]]:
    """edit.py의 import문 — (줄, 풀 모듈 경로, 가져온 이름들). 상대 import도 푼다.

    `ast.Import` 행의 "가져온 이름"은 **모듈 경로 그 자체**(`alias.name`)다 —
    asname을 적으면 `import …sampling as FrameSampleRequest`가 허용 형태처럼
    보여 별명 우회가 열린다(P6-01b). 별명은 그저 묶는 이름일 뿐 가져온 것이
    아니다.
    """
    rows: list[tuple[int, str, tuple[str, ...]]] = []
    for node in ast.walk(ast.parse(body)):
        if isinstance(node, ast.Import):
            rows.extend((node.lineno, alias.name, (alias.name,)) for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            rows.append(
                (node.lineno, _edit_from_module(node), tuple(alias.name for alias in node.names))
            )
    return rows


def _edit_from_module(node: ast.ImportFrom) -> str:
    """from-import의 풀 모듈 경로 — edit.py는 vlogsrt/project/ 안에 산다."""
    if node.level == 0:
        return node.module or ""
    parts = EDIT_PACKAGE.split(".")
    if node.level >= 2:
        parts = parts[: len(parts) - (node.level - 1)]
    prefix = ".".join(parts)
    return f"{prefix}.{node.module}" if node.module else prefix


def _edit_import_failures(rel: str, lineno: int, module: str, names: tuple[str, ...]) -> list[str]:
    where = f"{rel}:{lineno}"
    if module == EDIT_SAMPLE_TYPE_MODULE:
        if names != (EDIT_SAMPLE_TYPE,):
            return [
                f"{where} sampling에서 {EDIT_SAMPLE_TYPE} 외의 이름 {list(names)}을(를)"
                " 가져온다 — 데이터 타입은 이음매를 건너도 행동은 못 건넌다 (규칙 4·9)"
            ]
        return []
    failures = [
        f"{where} 제거 계층이 {banned}을(를) import한다 — 오디오·판정을 몰라야 한다 (규칙 4·9)"
        for banned in EDIT_BANNED_IMPORTS
        if module == banned or module.startswith(banned + ".")
    ]
    # 모듈 핸들 우회 — `from vlogsrt.project.vision import sampling`은 경로가
    # vision이지만 묶는 이름 자체가 금지 모듈이다.
    failures.extend(
        f"{where} 제거 계층이 {banned} 모듈 핸들을 가져온다 — 행동이 새어 든다 (규칙 4·9)"
        for banned in EDIT_BANNED_IMPORTS
        for name in names
        if f"{module}.{name}" == banned
    )
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
    forbidden = re.compile(
        r"^\s*(?:import|from)\s+(openai|subprocess|shutil|urllib|http|socket|ssl)\b"
    )
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


def _subprocess_run_bindings(
    tree: ast.Module,
) -> tuple[set[str], set[str], set[str], set[str], list[int]]:
    """subprocess 묶음 — (참 모듈 별명들, 참 run 별명들, run 외 심볼 이름 전부,
    run 별명이었던 이름 전부, 별표 줄들).

    `import subprocess as sp`와 `from subprocess import run as launch`을 따라가되,
    한 번이라도 다시 묶이면(그림자) 자격을 끊는다. run 외 API 심볼(Popen·call·
    check_call·check_output)은 확보 자체가 위반이다(재검토). 이름 호출 판정은
    별명을 문자 그대로 못 박을 수 없으므로 `ever_run`까지 내준다(P0-02c).
    """
    module_kinds: dict[str, set[str]] = {}
    run_kinds: dict[str, set[str]] = {}
    symbol_kinds: dict[str, set[str]] = {}
    star_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "subprocess":
                    module_kinds.setdefault(alias.asname or "subprocess", set()).add("import")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == (
            "subprocess"
        ):
            for alias in node.names:
                if alias.name == "*":
                    star_lines.append(node.lineno)
                elif alias.name == "run":
                    run_kinds.setdefault(alias.asname or "run", set()).add("import")
                elif alias.name in SUBPROCESS_API_SYMBOLS:
                    symbol_kinds.setdefault(alias.asname or alias.name, set()).add("import")
    for node in ast.walk(tree):
        for name in _rebound_names(node):
            if name in module_kinds:
                module_kinds[name].add("shadow")
            if name in run_kinds:
                run_kinds[name].add("shadow")
            if name in symbol_kinds:
                symbol_kinds[name].add("shadow")
    genuine_modules = {name for name, kinds in module_kinds.items() if kinds == {"import"}}
    # 이름 하나에 run과 run 외 심볼이 함께 묶이면 어느 쪽인지 입증 못 한다 — 참 run에서 뺀다.
    genuine_run = {
        name
        for name, kinds in run_kinds.items()
        if kinds == {"import"} and name not in symbol_kinds
    }
    return genuine_modules, genuine_run, set(symbol_kinds), set(run_kinds), star_lines


def _call_target_name(call: ast.Call) -> str:
    """호출 지점의 이름 — 메시지용. `sp.run`은 run, `Popen`은 Popen."""
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return "<식>"


def _subprocess_api_calls(
    tree: ast.Module,
    genuine_modules: set[str],
    genuine_run: set[str],
    other_symbols: set[str],
    ever_run: set[str],
) -> tuple[list[ast.Call], list[ast.Call], list[ast.Call]]:
    """subprocess API 호출을 (검증할 run 호출, run 외 호출, 출처 불명 호출)로 갈라 돌려준다.

    참 모듈 별명의 `.run`과 참 run 별명의 `run(...)`만 검증 대상이다. 이름 호출은
    별명까지 따라간다 — `from subprocess import run as launch`의 `launch(...)`가
    문 형태 검사를 몰래 건너뛰는 구멍이었다(P0-02c). run 외 심볼 별명
    (`Popen as pop`)은 run 외 호출이고, 그림자가 지거나 수입 증명이 없는 이름은
    출처 불명이다 — 어느 쪽이든 조용히 지나가면 안 된다.
    """
    run_sites: list[ast.Call] = []
    unsupported: list[ast.Call] = []
    unknown: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in SUBPROCESS_API_SYMBOLS:
            on_module = isinstance(func.value, ast.Name) and func.value.id in genuine_modules
            if not on_module:
                unknown.append(node)
            elif func.attr == "run":
                run_sites.append(node)
            else:
                unsupported.append(node)
        elif isinstance(func, ast.Name) and (
            func.id in SUBPROCESS_API_SYMBOLS or func.id in ever_run or func.id in other_symbols
        ):
            if func.id in genuine_run:
                run_sites.append(node)
            elif func.id in other_symbols:
                unsupported.append(node)
            else:
                unknown.append(node)
    return run_sites, unsupported, unknown


def _rebound_names(node: ast.AST) -> list[str]:
    """이 노드가 다시 묶는(그림자) 이름들. 대입·def·class 전부."""
    if isinstance(node, ast.Assign):
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AnnAssign):
        if isinstance(node.target, ast.Name) and node.value is not None:
            return [node.target.id]
        return []
    if isinstance(node, (ast.AugAssign, ast.NamedExpr)) and isinstance(node.target, ast.Name):
        return [node.target.id]
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return [node.name]
    return []


def _shutil_which_bindings(tree: ast.Module) -> tuple[set[str], set[str], set[str], list[int]]:
    """shutil 묶음 — (참 모듈 별명들, 참 which 별명들, which였던 이름 전부, 별표 줄들).

    `import shutil as sh`·`from shutil import which as find`까지 따라간다. 이름이
    다른 무엇으로 한 번이라도 다시 묶이면(그림자) 자격을 끊는다. 어떤 객체의
    `.which`든 믿는 순간 `unrelated.which('xmllint')`가 실행파일 검사를 통과한다.
    """
    module_kinds: dict[str, set[str]] = {}
    which_kinds: dict[str, set[str]] = {}
    star_lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "shutil":
                    module_kinds.setdefault(alias.asname or "shutil", set()).add("import")
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module == "shutil":
            for alias in node.names:
                if alias.name == "*":
                    star_lines.append(node.lineno)
                elif alias.name == "which":
                    which_kinds.setdefault(alias.asname or "which", set()).add("import")
    for node in ast.walk(tree):
        for name in _rebound_names(node):
            if name in module_kinds:
                module_kinds[name].add("shadow")
            if name in which_kinds:
                which_kinds[name].add("shadow")
    genuine_modules = {name for name, kinds in module_kinds.items() if kinds == {"import"}}
    genuine_which = {name for name, kinds in which_kinds.items() if kinds == {"import"}}
    return genuine_modules, genuine_which, set(which_kinds), star_lines


def _which_calls(
    tree: ast.Module, module_names: set[str], which_names: set[str], ever_names: set[str]
) -> tuple[list[ast.Call], list[ast.Call]]:
    """which 호출을 (shutil 것이 확실한 호출, 출처 불명 호출)로 갈라 돌려준다.

    참 shutil 모듈 별명의 `.which`와 참 별명의 `which(...)`만 확실하다. 그 밖의
    `.which` 전부와 덮어쓰였거나 수입 증명이 없는 `which` 이름 호출은 출처 불명이다.
    """
    genuine: list[ast.Call] = []
    unknown: list[ast.Call] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "which":
            on_module = isinstance(func.value, ast.Name) and func.value.id in module_names
            (genuine if on_module else unknown).append(node)
        elif isinstance(func, ast.Name) and (func.id == "which" or func.id in ever_names):
            (genuine if func.id in which_names else unknown).append(node)
    return genuine, unknown


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


def _keyword_failures(rel: str, site: ast.Call) -> list[str]:
    """키워드 인자의 금지 — 셸, 실행파일 오버라이드, 불투명 전개."""
    where = f"{rel}:{site.lineno}"
    problems: list[str] = []
    shell = next((kw for kw in site.keywords if kw.arg == "shell"), None)
    if shell is not None and not (
        isinstance(shell.value, ast.Constant) and shell.value.value is False
    ):
        problems.append(f"{where} 셸은 열 수 없다 — shell은 생략하거나 리터럴 False")
    if any(kw.arg == "executable" for kw in site.keywords):
        problems.append(f"{where} 실행파일 오버라이드(executable=)는 금지다")
    if any(kw.arg is None for kw in site.keywords):
        problems.append(f"{where} `**` 전개는 키워드 검사를 무력화한다")
    return problems


def _literal_str(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _xmllint_argv_failures(
    rel: str, site: ast.Call, sanctioned: set[str], which_calls: list[ast.Call]
) -> list[str]:
    """호출 하나의 argv 형태를 본다 — 고정 실행파일, 정해진 두 형태, 위치 인자는 argv 하나.

    `run`은 남는 위치 인자를 `Popen`에 그대로 넘긴다 — 뒤쪽 슬롯(bufsize·executable)으로
    실행파일이 흘러드는 것을 "위치 인자는 argv 하나" 규칙으로 끊는다.
    """
    where = f"{rel}:{site.lineno}"
    problems = _keyword_failures(rel, site)
    if not site.args:
        return problems + [f"{where} argv 없는 호출"]
    if len(site.args) > 1:
        return problems + [f"{where} argv는 유일한 위치 인자여야 한다 — executable이 흘러든다"]
    argv = site.args[0]
    if not isinstance(argv, (ast.List, ast.Tuple)):
        return problems + [f"{where} argv는 리스트 리터럴이어야 한다 — 문자열·변수 조립 금지"]
    problems += _xmllint_argv_element_failures(rel, argv, sanctioned, which_calls)
    problems += _xmllint_flag_failures(rel, argv)
    return problems


def _xmllint_argv_element_failures(
    rel: str, argv: ast.List | ast.Tuple, sanctioned: set[str], which_calls: list[ast.Call]
) -> list[str]:
    """머리 실행파일과 원소 조립 — 원소는 f-string·연결·starred로 만들지 않는다."""
    where = f"{rel}:{argv.lineno}"
    problems: list[str] = []
    head = argv.elts[0] if argv.elts else None
    head_ok = isinstance(head, ast.Name) and head.id in sanctioned
    if not head_ok:
        head_ok = any(
            head is call and _single_const(call) == XMLLINT_EXECUTABLE for call in which_calls
        )
    if not head_ok:
        problems.append(f"{where} 실행파일이 shutil.which('xmllint') 결과가 아니다")
    problems += [
        f"{rel}:{element.lineno} argv를 조립했다 — 고정 argv만 허용"
        for element in argv.elts
        if isinstance(element, (ast.JoinedStr, ast.BinOp, ast.Starred))
    ]
    return problems


def _xmllint_flag_failures(rel: str, argv: ast.List | ast.Tuple) -> list[str]:
    """argv의 깃발 형태 — 정해진 둘뿐이다(재검토).

    [exe, "--nonet", "--noout", path]
    [exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, path]
    마지막 칸들(path·dtd_uri)만 동적 표현식을 쓴다. DTD URI 변환·경로 이스케이프·
    오류 처리 같은 행동은 P1-04가 따로 시험한다 — 여기는 형태만 본다.
    """
    where = f"{rel}:{argv.lineno}"
    elts = argv.elts
    if len(elts) == 4:
        literal_slots = ((1, "--nonet"), (2, "--noout"))
    elif len(elts) == 6:
        literal_slots = ((1, "--nonet"), (2, "--noout"), (3, "--dtdvalid"))
    else:
        return [f"{where} argv는 정해진 두 형태의 길이(4·6)가 아니다"]
    if any(_literal_str(elts[slot]) != want for slot, want in literal_slots):
        return [
            f"{where} argv 깃발이 정해진 형태와 다르다 — "
            '`[exe, "--nonet", "--noout", path]` 또는 '
            '`[exe, "--nonet", "--noout", "--dtdvalid", dtd_uri, path]`'
        ]
    return []


def _xmllint_boundary_failures(rel: str, body: str) -> list[str]:
    """XML 검증 경계의 계약 — xmllint 하나, subprocess.run 하나, 고정 argv, 셸 금지.

    실행파일은 `shutil.which("xmllint")`로만 얻고, 호출은 `subprocess.run`(정적으로
    풀린 별명 포함)으로만, argv는 정해진 두 형태로 쓴다. run 외 메서드·출처 불명
    호출·명령 조립·셸 전달은 전부 거부한다(재검토).
    """
    tree = ast.parse(body)
    failures: list[str] = []
    module_names, which_names, ever_names, shutil_stars = _shutil_which_bindings(tree)
    genuine_modules, genuine_run, other_symbols, ever_run, subprocess_stars = (
        _subprocess_run_bindings(tree)
    )
    failures += [
        f"{rel}:{lineno} 별표 import는 호출 추적을 무력화한다"
        for lineno in subprocess_stars + shutil_stars
    ]
    genuine_which, unknown_which = _which_calls(tree, module_names, which_names, ever_names)
    failures += [
        f"{rel}:{call.lineno} which 출처를 입증 못 했다 — shutil.which만 허용한다"
        for call in unknown_which
    ]
    failures += [
        f"{rel}:{call.lineno} which가 xmllint 외 실행파일을 찾는다"
        for call in genuine_which
        if _single_const(call) != XMLLINT_EXECUTABLE
    ]
    sanctioned = _xmllint_names(tree, genuine_which)
    run_sites, unsupported, unknown = _subprocess_api_calls(
        tree, genuine_modules, genuine_run, other_symbols, ever_run
    )
    failures += [
        f"{rel}:{call.lineno} subprocess.run 외의 호출({_call_target_name(call)})이다"
        for call in unsupported
    ]
    failures += [f"{rel}:{call.lineno} subprocess.run 출처를 입증 못 했다" for call in unknown]
    for call in run_sites:
        failures += _xmllint_argv_failures(rel, call, sanctioned, genuine_which)
    return failures


def check_subprocess_boundaries() -> list[str]:
    """규칙 7 — subprocess는 정확히 두 파일에서만.

    `vlogsrt/preprocess.py`(ffmpeg/ffprobe)와 `vlogsrt/project/xml_validation.py`
    (xmllint 하나, `subprocess.run` 하나, 정해진 두 argv 형태, 셸 금지).
    파일 **이름**이 아니라 정확 경로로 판정한다.
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
    """규칙 6 — 모델 이름도 config에서 온다. 코드에 박으면 되돌리기가 커밋이 된다.

    경로별 허용 폭: `vlogsrt/config.py`·`vlogsrt/project/raw/config.py`는 엔진 모델
    (whisper-1·gpt-*transcribe*)을 놓아도 되지만 glm·gemini는 금지다.
    `vlogsrt/project/config.py`는 셋 다 놓아도 되나 raw 전용 리터럴(gpt-6-luna)은
    금지다. gpt-6-luna는 정확히 `vlogsrt/project/raw/config.py`에만 있을 수 있다
    (2026-10-02 계획 §3.2 — 모델 리터럴은 허용된 설정 경로에만 둔다).
    """
    failures = []
    for path in _sources():
        rel = _rel(path)
        patterns = (
            ENGINE_MODEL_LITERAL,
            PROJECT_MODEL_LITERAL,
            RAW_MODEL_LITERAL,
            CLAUDE_MODEL_LITERAL,
        )
        if rel in RAW_MODEL_LITERAL_PATHS:
            patterns = (PROJECT_MODEL_LITERAL,)
        elif rel in PROJECT_MODEL_LITERAL_PATHS:
            patterns = (RAW_MODEL_LITERAL, CLAUDE_MODEL_LITERAL)
        elif rel in MODEL_LITERAL_PATHS:
            patterns = (PROJECT_MODEL_LITERAL, RAW_MODEL_LITERAL, CLAUDE_MODEL_LITERAL)
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                failures.append(f"{rel}:{number} 모델 이름이 코드에 박혀 있다")
    return failures


CHECKS = (
    ("openai 경계", check_openai_boundary),
    ("네트워크 모듈 경계", check_network_boundary),
    ("Claude 전송은 transcribe만 안다", check_claude_transport_entry),
    ("ffmpeg 경계", check_ffmpeg_boundary),
    ("순수 계층", check_pure_layer),
    ("시각 유도 계층 부활 금지", check_time_deriving_layers_stay_buried),
    ("whisper에 prompt 금지", check_no_prompt_to_whisper),
    ("autojunk=False", check_autojunk),
    ("금지 의존성", check_banned_imports),
    ("의존성 3개", check_dependencies),
    ("frozen 모델", check_frozen_models),
    ("파생 frozen 모델", check_project_models_frozen),
    (".env 보호", check_env_untouched),
    ("판정 계층 부활 금지", check_inspection_stays_dead),
    ("검증은 출력을 못 건드린다", check_verify_cannot_touch_output),
    ("재전사는 전사 단계에 머문다", check_repair_stays_upstream),
    ("글자 교정은 순수하다", check_retext_stays_pure),
    ("3단 등급은 순수하다", check_tiering_stays_pure),
    ("자막 시각 확인은 순수하다", check_sync_check_stays_pure),
    ("자막 시각 확인은 표시만 한다", check_sync_check_only_displays),
    ("Final Cut 타임라인 빌더는 순수하다", check_finalcut_timeline_stays_pure),
    ("Final Cut 보내기는 원본을 복사·재해시하지 않는다", check_finalcut_export_never_copies),
    ("옛 선택한 컷 Final Cut 출력은 되살리지 않는다", check_old_finalcut_path_stays_dead),
    ("신뢰도 메타 보존", check_confidence_meta_survives),
    ("전사 호출 1개", check_one_transcription_call),
    ("모델 이름 비하드코딩", check_no_hardcoded_model),
    ("레거시는 project를 import하지 않는다", check_source_does_not_import_project),
    ("선택 의존성 범위", check_project_dependency_scope),
    ("제거 계층은 오디오를 듣지 않는다", check_edit_layer_ignores_audio),
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
