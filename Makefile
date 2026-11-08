.PHONY: check lint test sync export standards-check

sync:
	uv sync

lint:
	uv lock --check
	uv run ruff check .orbit-plugin/src tests
	uv run ruff format --check .orbit-plugin/src tests
	uv run basedpyright

test:
	uv run pytest -q

standards-check:
	sh docs/standards/check.sh

check: lint test standards-check

# Export the committed source (Orbit reads its .orbit-plugin/) for `orbit plugin validate/test/add`.
# Always from HEAD, never the working tree; the path names the commit.
export:
	@sha=$$(git rev-parse --short HEAD); out=/tmp/decision-mail-plugin-$$sha; \
	rm -rf "$$out" && mkdir "$$out" && git archive HEAD | tar -x -C "$$out" && echo "$$out"
