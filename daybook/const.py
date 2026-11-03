"""Application identity. The single definition of APP_ID and VERSION (D-005).

meson.build reads this file to name and fill the .desktop, metainfo, D-Bus service and
icon files, so keep it free of imports and logic.
"""

APP_ID = "io.github.daybook.Daybook"
# Test runs (DAYBOOK_* variables set) use their own ID, so they never mix with the real app.
DEVEL_APP_ID = APP_ID + ".Devel"
APP_NAME = "Daybook"
VERSION = "0.1.0"
GETTEXT_DOMAIN = "daybook"
