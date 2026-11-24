PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

.PHONY: check test lint types types-win invariant ui venv clean

check: lint types test invariant

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check vlogsrt ui tests scripts
	$(PY) -m ruff format --check vlogsrt ui tests scripts

types:
	$(PY) -m mypy

# Windows 분기의 정적 검사(D100 §1) — 여기서는 Windows를 돌려 볼 수 없다. check에는 넣지 않는다.
types-win:
	$(PY) -m mypy --platform win32

# 불변 규칙(CLAUDE.md)을 기계적으로 검사한다. 사람 눈보다 싸다.
invariant:
	$(PY) scripts/check_invariants.py

# 웹 UI. gradio·numpy·onnxruntime이 필요하다 — make venv가 함께 깐다(pip install -e ".[ui,project]").
ui:
	$(PY) -u -m ui.app

# 실행 파일(실행.command·실행.bat)과 같은 꾸러미 + 개발 도구. 표지를 남겨 실행 파일이 다시 깔지 않는다(D100 §8).
venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e ".[dev,ui,project]"
	.venv/bin/python scripts/launch_check.py write-stamp

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache out .vlogsrt-cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
