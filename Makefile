# One entry point for every component. `make test` / `make lint` work from a clean
# clone: uv syncs each Python project on first run, `npm ci` installs hub-web.
PY_PROJECTS := hub-server devbox-daemon

.PHONY: setup test lint test-py test-web lint-py lint-web lint-sh

setup: hub-web/node_modules
	@for p in $(PY_PROJECTS); do (cd $$p && uv sync --locked) || exit 1; done

hub-web/node_modules: hub-web/package-lock.json
	cd hub-web && npm ci --no-fund --no-audit
	@touch $@

test: test-py test-web

test-py:
	@for p in $(PY_PROJECTS); do echo "== pytest $$p"; (cd $$p && uv run --locked pytest -q) || exit 1; done

test-web: hub-web/node_modules
	cd hub-web && npm test

lint: lint-sh lint-py lint-web

lint-sh:
	@files="$$(find . -name '*.sh' -not -path './.git/*' -not -path '*/node_modules/*' -not -path '*/.venv/*' | sort)"; \
	if [ -n "$$files" ]; then echo "== shellcheck"; shellcheck $$files; else echo "== shellcheck: no shell scripts yet"; fi

lint-py:
	@for p in $(PY_PROJECTS); do echo "== ruff $$p"; (cd $$p && uv run --locked ruff check . && uv run --locked ruff format --check .) || exit 1; done

lint-web: hub-web/node_modules
	cd hub-web && npm run lint
