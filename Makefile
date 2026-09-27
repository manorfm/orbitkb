.PHONY: help install dev hooks test coverage lint sast sca security dast verify benchmark-scale evaluate-static evaluate-change-surface validate-real-corpus integration-containers readiness-audit \
        build clean release release-patch release-minor release-major _release

# Defaults to the project's own .venv when one exists, so `make verify`/
# `make release` use the interpreter `make dev` installed into -- not
# whatever `python3`/`ruff`/`bandit`/etc. happen to be first on PATH (which,
# unactivated, can silently be an unrelated interpreter with none of this
# project's dev dependencies installed). Override with `make PYTHON=...`.
ifeq ($(origin PYTHON),undefined)
PYTHON := $(shell test -x .venv/bin/python3 && echo .venv/bin/python3 || echo python3)
endif

# Also put .venv/bin first on PATH for every recipe, so things spawned as a
# bare command (the `orbitkb` console script itself, in tests/test_rename_smoke.py)
# resolve to the venv's copy too, without requiring `source .venv/bin/activate`.
ifneq ($(wildcard .venv/bin),)
export PATH := $(abspath .venv/bin):$(PATH)
endif

DB_DEFAULT := $(HOME)/.orbitkb/orbitkb.db
SCALE_FILES ?= 100 500 1000
SCALE_REPEAT ?= 3
ANALYSIS_TESTS := tests/test_static_analysis.py tests/test_self_index_e2e.py tests/test_depth_provider.py
NON_MCP_TEST_ARGS := tests/ --ignore=tests/test_static_analysis.py --ignore=tests/test_self_index_e2e.py --ignore=tests/test_depth_provider.py --ignore-glob=tests/test_mcp_*.py --ignore=tests/test_dast_adversarial_inputs.py
MCP_TESTS := $(shell find tests -name 'test_mcp_*.py' -type f | sort)

help:
	@echo "orbitkb — available targets:"
	@echo "  install         Install the package (runtime only)"
	@echo "  dev             Install the package with dev/test/security tooling + git hooks"
	@echo "  hooks           Install git hooks (commit-msg: enforces Conventional Commits)"
	@echo "  test            Run the deterministic test suite"
	@echo "  coverage        Run tests with coverage report"
	@echo "  lint            ruff (unused imports/vars) + vulture (dead code)"
	@echo "  sast            bandit static security scan"
	@echo "  sca             pip-audit dependency vulnerability scan"
	@echo "  security        sast + sca together"
	@echo "  dast            live MCP server adversarial-input test"
	@echo "  verify          test, then lint + sast + sca + dast in parallel"
	@echo "  benchmark-scale Profile static-analysis time and peak memory"
	@echo "  evaluate-static Run cross-stack golden facts with time/memory report"
	@echo "  evaluate-change-surface Run deterministic change-surface candidate goldens"
	@echo "  validate-real-corpus Validate a local, redacted real-change corpus (CORPUS=path)"
	@echo "  integration-containers Run opt-in RabbitMQ/Postgres/MongoDB/LocalStack container E2E"
	@echo "  readiness-audit Report deterministic evidence and production conditions"
	@echo "  build           Build sdist + wheel into dist/"
	@echo "  clean           Remove build artifacts"
	@echo "  release         Auto-computed bump (from commit history), verify, commit, tag, push"
	@echo "  release-patch   Force a patch release (same pipeline as release)"
	@echo "  release-minor   Force a minor release (same pipeline as release)"
	@echo "  release-major   Force a major release (same pipeline as release)"

install:
	$(PYTHON) -m pip install .

dev:
	$(PYTHON) -m pip install -e ".[dev]"
	@$(MAKE) hooks

hooks:
	git config core.hooksPath scripts/githooks
	@echo "Git hooks installed (core.hooksPath=scripts/githooks)."

test:
	$(PYTHON) -m pytest $(NON_MCP_TEST_ARGS)
	$(PYTHON) -m pytest $(MCP_TESTS)
	$(PYTHON) -m pytest tests/test_dast_adversarial_inputs.py
	$(PYTHON) -m pytest $(ANALYSIS_TESTS)

coverage:
	$(PYTHON) -m coverage erase
	$(PYTHON) -m coverage run -p -m pytest $(NON_ANALYSIS_TEST_ARGS)
	$(PYTHON) -m coverage run -p -m pytest $(ANALYSIS_TESTS)
	$(PYTHON) -m coverage combine
	$(PYTHON) -m coverage report -m

lint:
	$(PYTHON) -m ruff check --select F401,F841 orbitkb tests scripts
	$(PYTHON) -m vulture orbitkb --min-confidence 80

sast:
	$(PYTHON) -m bandit -c pyproject.toml -r orbitkb

sca:
	$(PYTHON) -m pip_audit

security: sast sca

dast:
	$(PYTHON) -m pytest tests/test_dast_adversarial_inputs.py -v

# Fan-out/fan-in release gate: test runs first, then lint/sast/sca/dast run in
# parallel in scripts/preflight.sh; a single failure aborts release with no git side
# effects. See the script for why this isn't the *authoritative* gate.
verify:
	@scripts/preflight.sh

benchmark-scale:
	$(PYTHON) scripts/run_scale_benchmark.py --files $(SCALE_FILES) --repeat $(SCALE_REPEAT)

evaluate-static:
	$(PYTHON) scripts/run_static_evaluation.py

evaluate-change-surface:
	$(PYTHON) scripts/run_benchmark_report.py

validate-real-corpus:
	@test -n "$(CORPUS)" || (echo "Set CORPUS to a local manifest path." >&2; exit 2)
	$(PYTHON) scripts/validate_real_corpus.py --corpus "$(CORPUS)"

integration-containers:
	ORBITKB_CONTAINER_E2E=1 $(PYTHON) -m pytest tests/test_container_integrations.py tests/test_container_cloud_integrations.py -v

readiness-audit:
	$(PYTHON) scripts/run_readiness_audit.py

build: clean
	$(PYTHON) -m build

clean:
	rm -rf build dist *.egg-info orbitkb.egg-info

# `release` computes the bump (major/minor/patch) from Conventional Commits
# since the last tag (scripts/versioning.py — the same logic the commit-msg
# hook previews on every commit). `release-patch/minor/major` force a kind
# instead of computing it. Both feed the same `_release` pipeline: fan-out/
# fan-in verify -> bump version -> refresh README badges -> commit -> tag ->
# push. Pushing the tag is what triggers .github/workflows/publish.yml, which
# reruns test/lint/sast/sca/dast in CI (the real gate) before build/publish.
release:
	@kind=$$($(PYTHON) scripts/versioning.py next) || exit 1; \
	$(MAKE) _release KIND=$$kind

release-patch:
	@$(MAKE) _release KIND=patch

release-minor:
	@$(MAKE) _release KIND=minor

release-major:
	@$(MAKE) _release KIND=major

_release: verify
	@scripts/cut_release.sh $(KIND)
