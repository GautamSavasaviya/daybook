# Daybook

A personal work planner for Ubuntu 26.04 (GNOME, Wayland) that replaces a paper notebook. It's meant to be **simpler than the user's PMS**. It handles:
- PMS tickets, face-to-face requests ("Requested by") and non-project work in **areas** (Docs, Demos, General)
- a daily plan with time allotments or "Rest of day" items, **automatic carry-over** of unfinished work, **On hold** with reasons and follow-ups, and **Move to…**
- time off (full or half days)
- scheduled and **emergency ("Meeting now") meetings**
- a focus timer
- tiered reminders that can't be missed

Python 3.14 + PyGObject, GTK 4.22, libadwaita 1.9, SQLite, Meson.

- Requirements: `docs/SPEC.md`, **including Addendum A, which wins where it conflicts with the main text**. Read both before changing behaviour.
- Architecture, schema, modules, phase checklist: `docs/PLAN.md`.
- Defaults and their reasons: `docs/DECISIONS.md`. Add an entry for every new default; never silently change one.
- Phase status and hand-test lists: `docs/PROGRESS.md` (from Phase 0 on).

## Current phase
**Phase 2 — Calendar, meetings and planning: done** (branch `feature/calendar-meetings-planning`). Next: **Phase 3 — Timer and time tracking**, waiting for the user's go-ahead.
(Update this line at the end of every phase.)

## Commands
The scripts and config below are created in Phase 0.

```bash
# One-time dev setup (PEP 668: never pip-install into the system Python)
scripts/setup-dev.sh            # python3 -m venv --system-site-packages .venv
                                # + ruff mypy pytest pytest-cov; pygobject-stubs (PYGOBJECT_STUB_CONFIG=Gtk4,Gdk4)

# Install to ~/.local and run (required: notification actions only work when installed — D-038)
scripts/dev-install.sh          # meson setup _build --prefix=$HOME/.local && meson install -C _build
daybook                         # or: daybook --debug   (logs to the terminal too)
scripts/dev-uninstall.sh

# Throwaway data + fast time for testing reminders by hand. Runs as a separate "Daybook (Devel)"
# app beside the real one (D-065); fake time is refused without DAYBOOK_DATA_DIR.
DAYBOOK_DATA_DIR=$(mktemp -d) DAYBOOK_TIME_SCALE=60 DAYBOOK_FAKE_NOW=2026-11-03T08:55:00+01:00 daybook --debug

# Everything the phase gate needs
scripts/check.sh                # all of the below

.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy                  # --strict on core + services (configured in pyproject.toml)
.venv/bin/mypy --check-untyped-defs daybook/ui daybook/main.py
.venv/bin/pytest --cov=daybook/core --cov-fail-under=90
.venv/bin/pytest tests/core/test_planner.py -k overflow      # single test

# Packaging validation
desktop-file-validate _build/data/io.github.daybook.Daybook.desktop
appstreamcli validate --no-net _build/data/io.github.daybook.Daybook.metainfo.xml
scripts/build-deb.sh            # → daybook_<version>_all.deb, then lintian
```

Logs: `~/.local/state/daybook/daybook.log`. Data: `~/.local/share/daybook/daybook.db`.
Installed code: `~/.local/share/io.github.daybook.Daybook/` (kept apart from the data, D-058).

## Code conventions
- **Layers** (enforced by `tests/test_layering.py`):
  - `daybook/core` is pure Python (stdlib + dateutil) and never imports `gi`.
  - `daybook/services` may use GLib/Gio/GObject but never Gtk/Gdk/Adw.
  - `daybook/ui` holds widgets only: no SQL, no `sqlite3`, no repositories, no business rules. It calls services and reacts to `EventBus` events.
- **DB is the source of truth:**
  - Every write is one `with transaction(conn):` block in a repository, called by a service that emits `Changed(...)` after commit.
  - Schema changes only through a new `daybook/core/migrations/m000N_*.py`, plus a fixture of the previous schema and a migration test. Never edit a released migration.
- **Test runs:** any `DAYBOOK_*` variable gives the app the separate `DEVEL_APP_ID` (D-065). Never let a test run use the real APP_ID or the real data folder with fake time.
- **Time:**
  - Never call `datetime.now()`, `time.time()` or `date.today()` outside `core/clock.py`. Inject a `Clock`.
  - Store UTC with `timeutil.to_db()`. Convert to local time only for display and for day bucketing (`local_day_bounds`).
  - Arm GLib timeouts with `clock.real_seconds_until()`.
- **Working days:** every "is this a working day / next / previous working day / today's working window" question goes through `core/calendar.py` (`WorkCalendar`). It knows workdays, lunch and time off. Never write `weekday() < 5` or read the workdays setting directly.
- **Reminders:**
  - Every reminder kind declares its **tier** (Stay / Act / Info → URGENT / HIGH / NORMAL, D-053) and its **subject id** for replace/withdraw (D-054). An unknown kind fails a test.
  - At most 3 notification buttons.
  - When a reminder becomes moot, withdraw it.
- **Main loop:** nothing may block it for more than 100 ms. Slow reads, exports, reports and backups go through `services/worker.py`.
- **Errors:**
  - Raise `ValidationError(field, message)` from core; forms show it inline.
  - Wrap GLib callbacks and action handlers with `@safe_callback`.
  - Never swallow an exception without logging it.
- **GTK/Adw:**
  - Banned (deprecated): TreeView, ComboBox, Dialog/MessageDialog, InfoBar, Statusbar, Assistant, EntryCompletion, FileChooser*, ColorChooser*, ShortcutsWindow, `Gtk.show_uri`, per-widget StyleContext, Adw.MessageDialog, Adw.PreferencesWindow, Adw.Leaflet/Flap/Squeezer. The full list is in PLAN §1.
  - Use instead: ListView/ColumnView, DropDown, Adw.Dialog/AlertDialog/PreferencesDialog/ShortcutsDialog, Adw.ToastOverlay, Gtk.FileDialog, Gtk.UriLauncher.
- **UX rules** (the full design is in PLAN §6):
  - **Simpler than the PMS:** the title is the only required field. Never add a required field. Extra fields go under "More details".
  - Use the shared `task_row` and chips for every task list. Use libadwaita named colours and the accent only; no custom theme.
  - Animations last 250 ms or less and respect `gtk-enable-animations`.
  - Every list has an Adw.StatusPage empty state with a useful action.
  - Every destructive action has Undo or a confirmation.
  - Every icon-only button has a tooltip and an accessible label.
  - Everything is reachable by keyboard.
  - The layout works at 360 px.
  - User-visible strings use `_()`.
- **Python style:**
  - Type hints everywhere. `from __future__ import annotations` isn't needed on 3.14.
  - Frozen dataclasses for models; `StrEnum` for statuses and kinds.
  - Ruff-formatted, line length 100.
  - Comments explain *why*, not *what*.
- **No** TODOs, stubs, placeholder buttons or commented-out code in a finished phase. If something can't work reliably on this platform, say so and record the alternative in DECISIONS.md.
- **Tests:**
  - Never skip, delete or weaken a failing test; fix the cause.
  - Core coverage stays ≥ 90%.
  - Time-dependent tests use `ManualClock`, never sleeps.

## Git
- Branch naming (D-064): `main` is stable and changes only through pull requests. Work goes on `feature/<kebab-name>` (one per phase; names listed in D-064), `fix/…`, `docs/…` or `chore/…`. A new branch starts from `main` if the previous phase is merged, otherwise from the previous phase's branch. The user merges. Never push unless asked.
- Commit at the end of a phase only, after the gate passes. Use a clear message (`Phase N: <summary>`) ending with:
  `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`

## End-of-phase checklist (SPEC "How to work" step 3)
1. `scripts/check.sh`: ruff, mypy and pytest all pass.
2. `scripts/dev-install.sh`, then launch from the app grid and from a terminal. No GTK criticals, warnings or deprecation messages.
3. Update `docs/PROGRESS.md` and the "Current phase" line above.
4. Commit on the phase's `feature/…` branch, push it, and tell the user to open a pull request into `main`.
5. **Stop.** Give the user a short list of things to test by hand. Don't start the next phase until they say so.
