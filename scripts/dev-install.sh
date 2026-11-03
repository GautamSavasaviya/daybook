#!/usr/bin/env bash
# Build and install Daybook into ~/.local. Notification actions only work for an
# installed app with matching .desktop and D-Bus service files (D-038), so this is the
# normal way to run Daybook during development.
set -euo pipefail
cd "$(dirname "$0")/.."

prefix="${HOME}/.local"
app_id="$(python3 -c 'import runpy; print(runpy.run_path("daybook/const.py")["APP_ID"])')"
# devel_entry: hidden "Daybook (Devel)" entry so test runs can show notifications (D-065).
if [[ -f _build/build.ninja ]]; then
  meson setup _build --prefix="$prefix" -Ddevel_entry=true --reconfigure >/dev/null
else
  meson setup _build --prefix="$prefix" -Ddevel_entry=true >/dev/null
fi
meson compile -C _build >/dev/null
meson install -C _build --quiet

# Let the session bus see the D-Bus service file right away.
gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
  --method org.freedesktop.DBus.ReloadConfig >/dev/null 2>&1 || true

if gdbus call --session --dest org.freedesktop.DBus --object-path /org/freedesktop/DBus \
     --method org.freedesktop.DBus.NameHasOwner "$app_id" 2>/dev/null \
     | grep -q true; then
  echo "Note: Daybook is running; quit it (Ctrl+Q) and start it again to use the new build."
fi
echo "Installed to ${prefix}. Start Daybook from the app grid or run: ${prefix}/bin/daybook"
