# Building the `.deb` and sharing it with colleagues

## 1. Build

On Ubuntu 26.04 with the build tools:

```bash
sudo apt install meson ninja-build dpkg-dev lintian gzip desktop-file-utils appstream
scripts/build-deb.sh              # builds and runs lintian
scripts/build-deb.sh --no-lint    # build only
```

Result: `dist/daybook_<version>_all.deb` (about 170 KB; `dist/` is git-ignored).

What the script does (D-006, no debhelper — the package is pure Python and
architecture-independent):

1. `meson install --prefix=/usr --destdir <staging>`;
2. fills `packaging/deb/control.in` (version from `daybook/const.py`, the single place the
   version is written), adds the man page, changelog, DEP-5 copyright, `md5sums`;
3. installs the maintainer scripts `postinst` (byte-compiles the app once, since a normal user
   can't write under `/usr`) and `prerm` (removes those compiled files again);
4. `dpkg-deb --root-owner-group --build`;
5. `lintian --pedantic` must report no errors or warnings.

### Releasing a new version
1. Change `VERSION` in `daybook/const.py`.
2. Update the release entry (version date and notes) in `data/daybook.metainfo.xml.in` and the entry in `packaging/deb/changelog.in`.
3. `scripts/check.sh`, then `scripts/build-deb.sh`.

## 2. Inspect before sharing

```bash
dpkg-deb --info dist/daybook_0.1.0_all.deb       # metadata and Depends
dpkg-deb --contents dist/daybook_0.1.0_all.deb   # file list
sha256sum dist/daybook_0.1.0_all.deb             # give this to your colleagues
```

## 3. Share it with colleagues

**Requirement:** the colleague must run **Ubuntu 26.04 (GNOME, Wayland)** — the package
depends on Python 3.14, GTK 4.22 and libadwaita 1.9, which older releases don't have. `apt`
will refuse to install otherwise and says which dependency is missing.

1. Send them `daybook_<version>_all.deb` (shared drive, email, chat, USB stick) and the SHA-256
   from above, plus [`USER_GUIDE.md`](USER_GUIDE.md).
2. They install it (needs their sudo password; `./` is required so apt reads the file):

   ```bash
   cd ~/Downloads
   sha256sum daybook_0.1.0_all.deb          # compare with the one you sent
   sudo apt install ./daybook_0.1.0_all.deb
   ```
3. They open **Daybook** from the app grid. Their data is their own, in their home folder.
4. Optional, for the tray icon: `sudo apt install gnome-shell-extension-appindicator`, then
   log out and in.

**Upgrade:** send the newer `.deb`; `sudo apt install ./daybook_<newer>_all.deb` replaces the
old one. Their data and backups are kept, and the database is migrated on first start (older
databases are upgraded; a database from a *newer* version is refused, not damaged).

**Uninstall:** `sudo apt remove daybook`. Their data stays in `~/.local/share/daybook`.

### A shared folder as an "office repository" (optional)
For many colleagues you can publish an apt repository, but for a handful of people sending the
file is simpler. If you want one: put the `.deb` in a folder, run
`dpkg-scanpackages . | gzip > Packages.gz` there (from `dpkg-dev`), serve it over HTTP(S) or a
mounted share and add `deb [trusted=yes] file:/path ./` to a file in `/etc/apt/sources.list.d/`.
Use signed repositories if it leaves your office network.

## 4. Verifying an install without touching your system

```bash
# Unpack into a scratch folder and look (no root, nothing installed):
dpkg-deb -x dist/daybook_0.1.0_all.deb /tmp/daybook-root
find /tmp/daybook-root -type f | head
```

`scripts/build-deb.sh` was also tested by installing, removing and purging the package in a
scratch root with `dpkg --instdir`; nothing was left behind after removal.

## 5. Known limits
- Architecture-independent (`all`); runs on x86-64 and arm64 alike if the dependencies exist.
- Not signed; colleagues install a file you gave them, so share the checksum.
- The tray icon needs the AppIndicator extension (Ubuntu's default GNOME has it available but
  it may need enabling).
- Source-tree runs are unsupported: notification buttons require an installed app (D-038).
