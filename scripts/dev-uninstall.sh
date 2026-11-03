#!/usr/bin/env bash
# Remove a Daybook installed by dev-install.sh from ~/.local. Your data
# (~/.local/share/daybook) and logs (~/.local/state/daybook) are kept.
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

update-desktop-database -q "${prefix}/share/applications" 2>/dev/null || true
gtk4-update-icon-cache -q -t -f "${prefix}/share/icons/hicolor" 2>/dev/null || true
echo "Daybook removed from ${prefix}. Data in ~/.local/share/daybook was kept."
