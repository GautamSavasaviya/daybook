#!/usr/bin/env bash
# Build dist/daybook_<version>_all.deb (D-006): meson install into a staging folder, a
# hand-written DEBIAN/control, then dpkg-deb and lintian. No debhelper is needed, because the
# package is pure Python and architecture-independent.
#
#   scripts/build-deb.sh            # build and run lintian
#   scripts/build-deb.sh --no-lint  # build only
#
# Needs: meson, ninja, dpkg-deb, fakeroot (optional), lintian (optional), gzip.
set -euo pipefail
cd "$(dirname "$0")/.."

lint=1
[[ "${1:-}" == "--no-lint" ]] && lint=0

for tool in meson ninja dpkg-deb gzip; do
  command -v "$tool" >/dev/null || { echo "Missing tool: $tool" >&2; exit 1; }
done

# VERSION and APP_ID are defined once, in daybook/const.py (D-005).
read -r app_id version < <(/usr/bin/python3 - <<'PY'
import runpy
c = runpy.run_path("daybook/const.py")
print(c["APP_ID"], c["VERSION"])
PY
)

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
stage="$work/stage"
mkdir -p "$stage" dist

echo "== meson install (prefix /usr) into a staging folder"
meson setup "$work/build" --prefix=/usr -Ddevel_entry=false >/dev/null
meson install -C "$work/build" --destdir "$stage" >/dev/null

doc="$stage/usr/share/doc/daybook"
man="$stage/usr/share/man/man1"
mkdir -p "$doc" "$man" "$stage/DEBIAN"

echo "== docs, man page, changelog"
install -m 644 packaging/deb/copyright "$doc/copyright"
date_rfc="$(git log -1 --format=%aD 2>/dev/null || date -R)"
sed -e "s/@VERSION@/$version/g" -e "s/@DATE@/$date_rfc/g" packaging/deb/changelog.in \
  | gzip -9n > "$doc/changelog.gz"
sed "s/@VERSION@/$version/g" packaging/deb/daybook.1 | gzip -9n > "$man/daybook.1.gz"
chmod 644 "$doc/changelog.gz" "$man/daybook.1.gz"

echo "== DEBIAN/control and maintainer scripts"
size_kb="$(du -sk --exclude=DEBIAN "$stage" | cut -f1)"
sed -e "s/@VERSION@/$version/g" -e "s/@INSTALLED_SIZE@/$size_kb/g" packaging/deb/control.in \
  > "$stage/DEBIAN/control"
install -m 755 packaging/deb/postinst packaging/deb/prerm "$stage/DEBIAN/"
# Everything is root-owned and not group/world-writable, whoever builds it.
find "$stage" -type d -exec chmod 755 {} +
find "$stage/usr" -type f ! -perm -u+x -exec chmod 644 {} +

(cd "$stage" && find usr -type f -print0 | sort -z | xargs -0 md5sum > DEBIAN/md5sums)

deb="dist/daybook_${version}_all.deb"
echo "== dpkg-deb --build"
dpkg-deb --root-owner-group --build "$stage" "$deb" >/dev/null
echo "Built $deb ($(du -h "$deb" | cut -f1))"

if [[ $lint == 1 ]]; then
  if command -v lintian >/dev/null; then
    echo "== lintian"
    lintian --tag-display-limit 0 "$deb" || status=$?
    echo "lintian finished (exit ${status:-0})"
  else
    echo "lintian is not installed; skipping the check." >&2
  fi
fi
echo "App ID: $app_id"
