#!/usr/bin/env bash
# Runs lint + tests before allowing a push (install as .git/hooks/pre-push).
#
# Install once:
#   scripts/pre-push.sh install
# Bypass in an emergency:
#   git push --no-verify
set -u

ROOT="$(git rev-parse --show-toplevel)"

if [ "${1:-}" = "install" ]; then
    cp "$0" "$(git rev-parse --git-dir)/hooks/pre-push"
    chmod +x "$(git rev-parse --git-dir)/hooks/pre-push"
    echo "pre-push hook installed."
    exit 0
fi

cd "$ROOT" || exit 1

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"

RUFF="$ROOT/.venv/bin/ruff"
[ -x "$RUFF" ] || RUFF="$(command -v ruff || true)"

TARGETS="$ROOT/src $ROOT/tests $ROOT/examples $ROOT/benchmarks"
FAILED=0

echo "pre-push: running lint + tests..."

if [ -n "$RUFF" ]; then
    "$RUFF" check $TARGETS || FAILED=1
    "$RUFF" format --check $TARGETS || FAILED=1
else
    echo "pre-push: ruff not found, skipping lint"
fi

"$PY" -m pytest -q "$ROOT/tests" || FAILED=1

if [ "$FAILED" -ne 0 ]; then
    echo
    echo "pre-push: FAILED - fix the issues above, then push again"
    echo "          (use 'git push --no-verify' to bypass)"
    exit 1
fi

echo "pre-push: all checks passed"
exit 0
