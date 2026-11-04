#!/usr/bin/env bash
# Remove a Daybook installed by dev-install.sh from ~/.local. Your data
# (~/.local/share/daybook) and logs (~/.local/state/daybook) are kept. The login entry goes.
set -euo pipefail
cd "$(dirname "$0")/.."

prefix="${HOME}/.local"
app_id="$(python3 -c 'import runpy; print(runpy.run_path("daybook/const.py")["APP_ID"])')"
if [[ -f _build/build.ninja ]]; then
  ninja -C _build uninstall >/dev/null
fi
# meson uninstall removes files, not the directories it created. The code directory is
# named after the app ID; the data directory (share/daybook) is never touched.
rm -rf "${prefix:?}/share/${app_id:?}"

# "Start at login" leaves an entry that would now point at a missing program.
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/autostart/${app_id}.desktop"

update-desktop-database -q "${prefix}/share/applications" 2>/dev/null || true
gtk4-update-icon-cache -q -t -f "${prefix}/share/icons/hicolor" 2>/dev/null || true
echo "Daybook removed from ${prefix}. Data in ~/.local/share/daybook was kept."
