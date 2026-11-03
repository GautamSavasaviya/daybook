project('daybook', meson_version: '>= 1.4.0', license: 'GPL-3.0-or-later')

python = import('python').find_installation('python3')
gnome = import('gnome')

# APP_ID and VERSION are defined once, in daybook/const.py (D-005).
const = run_command(
  python, '-c',
  'import runpy, sys; c = runpy.run_path(sys.argv[1]); print(c["APP_ID"]); print(c["VERSION"])',
  meson.project_source_root() / 'daybook' / 'const.py',
  check: true,
).stdout().strip().split('\n')
app_id = const[0]
version = const[1]

prefix = get_option('prefix')
bindir = prefix / get_option('bindir')
datadir = prefix / get_option('datadir')
# Code lives in its own directory named after the app ID, never in share/daybook: with the
# ~/.local prefix that path is the user's data directory (XDG data home + "daybook").
pkgdatadir = datadir / app_id

# The Python package is installed to pkgdatadir and put on sys.path by the launcher
# (standard GNOME Python layout). Files are listed at configure time; dev-install.sh
# reconfigures, so new modules are picked up.
sources = run_command(
  python, '-c',
  '''
import pathlib, sys
root = pathlib.Path(sys.argv[1])
for path in sorted((root / "daybook").rglob("*")):
    if path.is_file() and path.suffix in (".py", ".css") and "__pycache__" not in path.parts:
        print(path.relative_to(root))
''',
  meson.project_source_root(),
  check: true,
).stdout().strip().split('\n')
install_data(sources, install_dir: pkgdatadir, preserve_path: true)

launcher_conf = configuration_data()
launcher_conf.set('PKGDATADIR', pkgdatadir)
configure_file(
  input: 'bin/daybook.in',
  output: 'daybook',
  configuration: launcher_conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)

subdir('data')

gnome.post_install(
  gtk_update_icon_cache: true,
  update_desktop_database: true,
)

summary({
  'App ID': app_id,
  'Version': version,
  'Prefix': prefix,
}, section: 'Daybook')
