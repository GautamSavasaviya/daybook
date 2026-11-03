#!/usr/bin/env bash
# The phase gate: lint, format, types, tests (core coverage >= 90%), packaging validation,
# and no leftover TODO markers. Run scripts/setup-dev.sh once first.
set -euo pipefail
cd "$(dirname "$0")/.."
venv=.venv/bin

echo "== ruff check";        "$venv/ruff" check .
echo "== ruff format";       "$venv/ruff" format --check .
echo "== mypy --strict";     "$venv/mypy"
echo "== mypy (ui)";         "$venv/mypy" --check-untyped-defs daybook/ui daybook/main.py
echo "== pytest";            "$venv/pytest"

echo "== packaging validation"
if [[ -f _build/build.ninja ]]; then
  meson setup _build --reconfigure >/dev/null
else
  meson setup _build --prefix="${HOME}/.local" >/dev/null
fi
meson test -C _build --print-errorlogs

echo "== no TODO/FIXME/XXX markers"
if grep -rnE --exclude=check.sh '\b(TODO|FIXME|XXX)\b' daybook tests scripts data bin meson.build; then
  echo "Remove the markers above before finishing a phase." >&2
  exit 1
fi
echo "All checks passed."
