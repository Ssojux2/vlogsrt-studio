PY := $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)
GOLDEN := golden

.PHONY: check test lint types invariant eval fixtures ui venv clean

check: lint types test invariant

test:
	$(PY) -m pytest

lint:
	$(PY) -m ruff check vlogsrt ui tests scripts
	$(PY) -m ruff format --check vlogsrt ui tests scripts

types:
	$(PY) -m mypy

# 불변 규칙(CLAUDE.md)을 기계적으로 검사한다. 사람 눈보다 싸다.
invariant:
	$(PY) scripts/check_invariants.py

# 웹 UI. gradio가 필요하다: pip install -e ".[ui]"
ui:
	$(PY) -m ui.app

eval:
	$(PY) -m vlogsrt.cli evalset --golden $(GOLDEN) --out docs/DASHBOARD.md

# 사람만 실행한다. 실제 API 호출이고 유료다.
fixtures:
	@echo "실제 API를 호출합니다. 약 \$$0.06/편."
	@echo "  $(PY) -m vlogsrt.cli record $(GOLDEN)/ep01.mp4"

venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install -e ".[dev]"

clean:
	rm -rf .pytest_cache .mypy_cache .ruff_cache out .vlogsrt-cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
