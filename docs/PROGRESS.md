# Progress

One section per phase: what was built, how it was verified, what to test by hand.
Plan and checklist: [`PLAN.md`](PLAN.md) §7. Decisions: [`DECISIONS.md`](DECISIONS.md).

| Phase | Status | Branch |
|---|---|---|
| 0 — Scaffold | ✅ Done (2026-11-03) | `feature/scaffold` |
| 1 — Projects, areas and tasks | ✅ Done (2026-11-03) | `feature/projects-areas-tasks` |
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
8. Test run: `DAYBOOK_DATA_DIR=$(mktemp -d) daybook --debug`. A second window titled **"Daybook (Devel)"** with striped header bars opens next to the real one (both may run at once). Opening Daybook from the app grid still brings up the *real* window. `DAYBOOK_TIME_SCALE=60 daybook` without a data folder is refused with an explanation.

### Code-review fixes (`/code-review`, all 10 findings)
| # | Fix | Test |
|---|---|---|
| 1 | Toast text is never treated as markup (titles may contain `&` or `<`). | — (UI) |
| 2 | A failing toast is logged, never reported as another toast, so there's no loop. | `test_failing_toast_display_cannot_loop_through_the_error_reporter` |
| 3 | A failed COMMIT rolls back and leaves the connection usable. A SQLite-side rollback no longer hides the real error (D-066). | 4 new tests in `test_db.py` |
| 4 | Several damaged settings are repaired one by one, and valid settings are kept (D-067). | `test_several_independent_problems_keep_every_valid_setting` |
| 5 | Worker results arriving during shutdown are dropped. The pending-job set is locked. | `test_results_finishing_around_shutdown_are_not_delivered` |
| 6 | A timezone change is detected even when `/etc/localtime` is a copied file. | `test_change_between_two_unnamed_zone_files_is_detected` |
| 7 | Meson builds against `/usr/bin/python3` and fails on Python older than 3.14. The launcher exits with a clear message on old Python. | checked by hand |
| 8 | Main-menu button has an accessible label. | — (UI) |
| 9 | Window destroy handler wrapped in `safe_callback`. Timezone lookup failures are logged, not swallowed. | — |
| 10 | Test runs use their own app ID `…Daybook.Devel`, so they never mix with the real app. Fake time requires a throwaway data folder. A hidden Devel desktop entry lets test runs notify (D-065). | 9 new tests; checked live with the real and test instances running side by side |

Gate after the fixes: **253 tests** pass, 98.8 % core coverage. ruff, mypy and the packaging validation (now including the Devel entry) are clean. The live launch printed 0 bytes to the terminal.

---

## Phase 1 — Projects, areas and tasks ✅

### Built
- **Core** (no GTK):
  - `models.py`: projects/areas, tasks with the four statuses and hold details, checklist items, plan items, list summaries.
  - `validation.py`: every field validator, plus a duration parser (`30m`, `2h`, `1h 30m`, `1.5h`, `90`).
  - `calendar.py`: `WorkCalendar` with workdays and full-day time off (half days arrive with the planner in Phase 2).
  - `hold.py`, `reschedule.py` (Move to…, D-069), `tickets.py` (prefix → project, "Open ticket" link), `chips.py` (every chip rule), `tasklists.py` (list definitions and grouping), `suggest.py` (autocomplete), `sources.py` (the `TaskSource` protocol for future PMS plugins; not implemented, per SPEC).
  - Repositories for projects, tasks (with tags, checklist, lists, counts, Undo snapshots and purge), plan items, tracked time and time off.
- **Services:** `ProjectService`, `TaskService` (Undo toasts for complete, hold, move, delete, removing a checklist item and archiving), `CalendarService`, `DayWatcher` (lists refresh after midnight or a timezone change). Soft-deleted tasks are purged at startup after 24 h. Lists and badge counts load on the worker thread.
- **UI:**
  - `Adw.Sidebar` with badges, coloured project dots, a right-click menu, "New Project or Area" and "Archived Projects" (D-075).
  - One shared `TaskRow` and chips in every list. Inbox, Upcoming (Overdue / days / On hold), per-project (In progress / To do / On hold) and Done (by day) lists, each with an empty state and an action.
  - Filters (project/area, status, priority, tag, requested by), and search with Ctrl+F across title, ticket key and notes, done tasks included.
  - New task (Ctrl+N): title is the only required field; "Plan for today" with presets; "More details" collapsed. The project is inferred from the ticket prefix, with a warning when another task already has that ticket.
  - Task detail panel: inline editing of every field, checklist, tags and Requested-by with autocomplete, notes, "Open ticket", time today and total against the estimate, and Put on hold / Resume / Move to… / Mark done / Delete.
  - Project dialog (colour palette, prefix and link validation, archive, delete).
  - Adaptive layout: three panes ≥ 900 sp, detail over the list below that, single-page below 600 sp.

### Verified
- `scripts/check.sh` passes: ruff, `mypy --strict` (core and services) plus the UI check, **542 tests** (after the review fixes below), **98.8 % core coverage**, packaging validation, no markers.
- `scripts/dev-install.sh`, then a launch from the terminal: **0 bytes of terminal output**.
- A scratch harness (not committed) seeded sample data and drove the real window through every list, the detail panel, search, filters and all dialogs, at 1180 px, 800 px and 380 px and in dark mode, saving snapshots to check the layout. It also ran autocomplete, ticket-prefix inference, archive/restore and choosing "On hold" from the status drop-down. It found and fixed two problems: a GTK warning when the window narrowed past 600 sp (D-077), and archiving the open project jumping to Today instead of Inbox. One warning remains, and only in the harness: navigating in the same main-loop iteration as an instant resize from 380 to 1180 px. A person can't do both at once.

### Not in this phase (by plan)
- The timer arrives in Phase 3. "Put on hold" already says it stops the timer, which becomes true once a timer exists. The time shown in the detail panel is a real query, and reads 0 until then.
- Task **recurrence** (a "More details" field in SPEC A1) ships in Phase 2 together with `core/recurrence.py`, so there is no field until it works.
- The Today, Plan, Meetings and Reports pages still show their Phase 0 explanations. Today's badge and the "carried ×N" chips get data with the Plan view and carry-over in Phase 2.
- Notifications are withdrawn on hold or move in Phase 4.

### Test by hand
1. `scripts/dev-install.sh`, then open Daybook from the app grid.
2. **Ctrl+N** → type only a title → **Add**. It appears in **Inbox** under General, P3, and the toast says "Added …". Try an empty title: Add shows "Enter a title.", and nothing is saved.
3. Sidebar → **New Project or Area**: create "PMS" with prefix `proj` and link `https://example.com/browse/{key}`, then an area "Docs" with neither. Try a link without `{key}`: it is refused inline. PMS shows its prefix as `PROJ`.
4. Ctrl+N → expand **More details** → ticket `PROJ-12`. The project switches to PMS ("Filed under PMS"). Add the same key again: "Another task already has this ticket".
5. Open a task (click it). Edit the title (click it, type, Enter), priority, due date (yesterday → "overdue 1d" chip; today → "due today"), estimate `1h 30m`, Requested by (type a few letters of an earlier name: suggestions; Down + Enter takes one), tags, notes, and checklist items (tick one; the row shows "1/2"). **Open ticket** opens the browser.
6. **Put on Hold…** → "Waiting for approval", note, follow-up date → the task leaves Inbox and shows under **Upcoming → On hold** with "on hold · approval · 0d". Undo the toast, then hold again and use **Resume** (→ In progress).
7. **Move To…** on a weekday: the first option is "Tomorrow" with its date. Pick a Saturday: it is refused and names the next working day. Move twice with a reason: the notes get dated lines and the row shows "moved ×2".
8. Tick a row's round check: it moves to **Done** ("done today"), Undo brings it back. **Delete Task** from the detail panel's ⋮ menu → toast → Undo.
9. **Ctrl+F**: search a word in a note or a ticket key; done tasks show under "Done". Search nonsense → "Create “…”" opens New Task with that title. Escape returns to the previous page.
10. **Filter** (funnel button): combine priority + tag; with no match, "Clear Filters" resets them.
11. Right-click PMS in the sidebar → **Archive**: you land on Inbox, PMS's open tasks disappear from Inbox/Upcoming but stay in search, and Undo restores. **Archived Projects** → Restore. An empty project's Edit dialog offers **Delete** (with confirmation); General offers neither.
12. Narrow the window to ~360 px: the sidebar becomes a page, the list fills the window, and a task opens full-screen with a back button. Switch light/dark and the accent colour: chips, dots and the selection follow.

### Code-review fixes (`/code-review`, all 10 findings)
| # | Fix | Test |
|---|---|---|
| 1 | Opening another task while a field has focus now shows the new task's values everywhere; pending title and notes edits are saved to the task being left. | checked live (harness) |
| 2 | Notes waiting for their 0.8 s save are saved on window close and before the database closes at quit. | checked live: notes typed just before Quit were saved |
| 3 | Undo refuses instead of overwriting edits made after the action (D-078). | `test_undo_never_overwrites_an_edit_made_after_the_action` + 2 repo tests |
| 4 | Resume and reopen have Undo (D-079). | 3 service tests |
| 5 | An over-long or odd ticket key in New Task shows a hint instead of the error toast. | checked live |
| 6 | Task and project names with `&` or `<` are escaped in group titles and empty-state descriptions and are never parsed as markup in rows. | checked live: no GTK warning |
| 7 | Closing search after its project was deleted or archived goes to Inbox. | checked live |
| 8 | The hold dialog's close handler is wrapped in `safe_callback`. | — (UI) |
| 9 | A filter whose value disappears (last tag removed) is cleared, not kept invisibly. | checked live |
| 10 | Planning on a non-working day is refused by `TaskService.create` (D-080). | `test_create_refuses_planning_on_a_non_working_day` |
