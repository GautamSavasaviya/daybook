# Progress

One section per phase: what was built, how it was verified, what to test by hand.
Plan and checklist: [`PLAN.md`](PLAN.md) §7. Decisions: [`DECISIONS.md`](DECISIONS.md).

| Phase | Status | Branch |
|---|---|---|
| 0 — Scaffold | ✅ Done (2026-11-03) | `feature/scaffold` |
| 1 — Projects, areas and tasks | Not started | `feature/projects-areas-tasks` |
| 2 — Calendar, meetings and planning | Not started | `feature/calendar-meetings-planning` |
| 3 — Timer and time tracking | Not started | `feature/timer-time-tracking` |
| 4 — Reminder engine | Not started | `feature/reminder-engine` |
| 5 — Fast input | Not started | `feature/fast-input` |
| 6 — Review and reporting | Not started | `feature/review-reporting` |
| 7 — Background and safety | Not started | `feature/background-data-safety` |
| 8 — Hardening and packaging | Not started | `feature/hardening-packaging` |

---

## Phase 0 — Scaffold ✅

### Built
- **Tooling:**
  - `pyproject.toml` (ruff, mypy, pytest and coverage config only)
  - `scripts/setup-dev.sh`: a `.venv` with `--system-site-packages`, ruff, mypy, pytest, pytest-cov and pygobject-stubs
  - `scripts/check.sh`: the whole phase gate in one command
- **Build and install:**
  - `meson.build` reads APP_ID and VERSION from `daybook/const.py`, the only place they are written (a test enforces this). It installs the package, the launcher, the .desktop file (`DBusActivatable=true`), the metainfo, the D-Bus service file, the app icon, the symbolic icon and two app-private section icons.
  - `meson test` runs `desktop-file-validate` and `appstreamcli validate`.
  - `scripts/dev-install.sh` installs to `~/.local`; `scripts/dev-uninstall.sh` removes it and keeps the data. Code goes to `~/.local/share/io.github.daybook.Daybook/`, apart from the data (D-058).
- **Core** (`daybook/core`, no GTK):
  - `clock.py`: `SystemClock`, `ScaledClock` (`DAYBOOK_TIME_SCALE` / `DAYBOOK_FAKE_NOW`), `ManualClock`, and timezone detection with change tracking.
  - `timeutil.py`: fixed-width UTC storage format, DST-safe `localize` and `local_day_bounds`, and `1h 05m` / `in 12 min` / 12-24 h formatting.
  - `db.py`: WAL, foreign keys, `BEGIN IMMEDIATE` transactions with nested savepoints, and a trigger-safe script runner.
  - `migrations/`: versioned through `PRAGMA user_version`, one transaction per step, refuses newer databases. `m0001_initial` is the full schema from PLAN §4, including Addendum A: areas with General seeded and protected by triggers, On hold, Rest-of-day items, time off, ad-hoc meetings, and the time-entry overlap triggers.
  - `settings.py` and the settings and app-state repositories: typed defaults, lenient loading (a damaged value falls back to its default), strict validation that names the field.
- **Services:**
  - `events.py`: a typed bus; events from other threads are handed to the main loop.
  - `safe.py`: `safe_callback` and an error reporter.
  - `worker.py`: one background thread with its own connection.
  - `settings.py` and `context.py`: open, migrate and wire everything; a database from a newer version, or a corrupt one, is refused and left untouched.
- **App shell:**
  - `paths.py` (XDG paths, `DAYBOOK_DATA_DIR`) and `logsetup.py` (5 × 1 MB rotation; `--debug` also logs to the terminal).
  - Global exception hooks: the error is logged and the toast "Something went wrong — details in log" appears.
  - Clean shutdown on Quit, Ctrl+C and SIGTERM.
- **UI:**
  - `Adw.Application`: single instance, command line handled by the running instance, `--debug`, Ctrl+Q / Ctrl+W, About dialog with debug info.
  - Main window: `Adw.NavigationSplitView` sidebar with the 7 sections, each showing its empty state; collapses below 600 sp (works at 360 px); toast overlay.
  - An error page when the database can't be opened, with "Open Log Folder".
  - A second launch with `DAYBOOK_*` overrides is refused while another instance runs (D-020, D-062).

### Verified
- `scripts/check.sh` passes:
  - ruff check and format
  - `mypy --strict` on core and services, plus a mypy check of the UI
  - **237 tests**, with **98.8 % coverage** of `daybook/core`
  - desktop and metainfo validation
  - no TODO markers
- Installed to `~/.local` and launched:
  - from a terminal: **0 bytes of terminal output**, including after opening the About dialog
  - through D-Bus activation, as the GNOME app grid does: started as `daybook --gapplication-service`
  - a second launch hands over to the running window
  - Quit, SIGINT and SIGTERM all close the database cleanly
- A database marked as coming from a newer version was left byte-identical, with no WAL file created, and the error page was shown.
- `dev-uninstall.sh` removes every installed file and keeps the data. Reinstalling works.

### Not in this phase (by plan)
- Badge counts, projects in the sidebar and section actions arrive with their features (Phase 1 onwards).
- Ctrl+1…7 and the shortcuts dialog are Phase 5. Preferences is Phase 7.
- Closing the window currently quits the app. Running in the background is Phase 7.

### Test by hand
1. `scripts/dev-install.sh`, then open **Daybook** from the app grid (search "Daybook"). The window opens, and the icon looks right in the grid and the dock.
2. Click each of the 7 sections. Each shows its own icon, title and description, and the header title changes.
3. Make the window narrow (drag to about 360 px). The sidebar becomes its own page; picking a section opens it, and the back button returns.
4. Switch GNOME between light and dark style and change the accent colour (Settings → Appearance). Daybook follows both immediately.
5. Main menu → **About Daybook**. Check the version, the license (GPL-3.0-or-later), and Troubleshooting → Debug Info (log and data paths).
6. Launch Daybook again from the app grid or with `daybook` in a terminal while it runs. The existing window comes to the front; no second window opens.
7. Press **Ctrl+Q**. The app quits. `~/.local/share/daybook/daybook.db` exists, and `~/.local/state/daybook/daybook.log` shows "database closed".
8. Optional: `DAYBOOK_DATA_DIR=$(mktemp -d) daybook --debug` (with the normal instance quit) runs against an empty throwaway database and logs to the terminal.
