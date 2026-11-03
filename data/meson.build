data_conf = configuration_data()
data_conf.set('APP_ID', app_id)
data_conf.set('VERSION', version)
data_conf.set('BINDIR', bindir)

desktop_file = configure_file(
  input: 'daybook.desktop.in',
  output: app_id + '.desktop',
  configuration: data_conf,
  install: true,
  install_dir: get_option('datadir') / 'applications',
)

metainfo_file = configure_file(
  input: 'daybook.metainfo.xml.in',
  output: app_id + '.metainfo.xml',
  configuration: data_conf,
  install: true,
  install_dir: get_option('datadir') / 'metainfo',
)

# D-Bus activation: required for notification actions to reach the app (D-038).
configure_file(
  input: 'daybook.service.in',
  output: app_id + '.service',
  configuration: data_conf,
  install: true,
  install_dir: get_option('datadir') / 'dbus-1' / 'services',
)

install_data(
  'icons/app.svg',
  rename: app_id + '.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'scalable' / 'apps',
)
install_data(
  'icons/app-symbolic.svg',
  rename: app_id + '-symbolic.svg',
  install_dir: get_option('datadir') / 'icons' / 'hicolor' / 'symbolic' / 'apps',
)
# App-private icons, found through the IconTheme search path set at startup.
install_data(
  'icons/actions/daybook-inbox-symbolic.svg',
  'icons/actions/daybook-reports-symbolic.svg',
  install_dir: pkgdatadir / 'icons' / 'hicolor' / 'scalable' / 'actions',
)

# Hidden entry for test runs (DAYBOOK_* variables), which use their own app ID (D-065).
# GNOME Shell only shows notifications from apps that have a desktop entry.
devel_desktop_file = []
if get_option('devel_entry')
  devel_desktop_file = configure_file(
    input: 'daybook-devel.desktop.in',
    output: devel_app_id + '.desktop',
    configuration: data_conf,
    install: true,
    install_dir: get_option('datadir') / 'applications',
  )
endif

desktop_file_validate = find_program('desktop-file-validate', required: false)
if desktop_file_validate.found()
  test('validate-desktop', desktop_file_validate, args: [desktop_file])
  if get_option('devel_entry')
    test('validate-devel-desktop', desktop_file_validate, args: [devel_desktop_file])
  endif
endif

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test('validate-metainfo', appstreamcli, args: ['validate', '--no-net', '--explain', metainfo_file])
endif
