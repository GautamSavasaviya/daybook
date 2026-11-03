#!/usr/bin/env bash
# Create the development virtualenv. It sees the system PyGObject/GTK (via
# --system-site-packages) and adds the dev tools only; nothing is installed
# into the system Python (PEP 668, D-007).
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv --system-site-packages .venv
fi

.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet ruff mypy pytest pytest-cov
# Stubs for GLib/Gio/Gtk 4 so mypy --strict can check code that touches gi.repository.
PYGOBJECT_STUB_CONFIG=Gtk4,Gdk4 .venv/bin/python -m pip install --quiet --no-cache-dir pygobject-stubs

echo "Dev environment ready: .venv ($(.venv/bin/python --version))"
