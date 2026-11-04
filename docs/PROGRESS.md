# Progress

One section per phase: what was built, how it was verified, what to test by hand.
Plan and checklist: [`PLAN.md`](PLAN.md) §7. Decisions: [`DECISIONS.md`](DECISIONS.md).

| Phase | Status | Branch |
|---|---|---|
| 0 — Scaffold | ✅ Done (2026-11-03) | `feature/scaffold` |
| 1 — Projects, areas and tasks | ✅ Done (2026-11-03) | `feature/projects-areas-tasks` |
| 2 — Calendar, meetings and planning | ✅ Done (2026-11-03) | `feature/calendar-meetings-planning` |
| 3 — Timer and time tracking | ✅ Done (2026-11-03) | `feature/timer-time-tracking` |
| 4 — Reminder engine | ✅ Done (2026-11-03) | `feature/reminder-engine` |
| 5 — Fast input | ✅ Done (2026-11-04) | `feature/fast-input` |
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

---

## Phase 2 — Calendar, meetings and planning ✅

### Built
- **Core:**
  - `calendar.py`: working window per day (hours, half-day leave cut at lunch or the midpoint), lunch, the time-off span, `with_time_off`.
  - `recurrence.py`: daily / weekdays / weekly on chosen days / monthly, intervals, `until`; wall-clock expansion across DST; month-end clamping; JSON form; `next_date` for tasks; readable descriptions.
  - `meetings.py`: occurrences with cancelled and moved single occurrences, orphaned changes after a series edit, ad-hoc helpers.
  - `planner.py`: the auto-scheduler of PLAN §5.1 (busy blocks, free windows, 5-min fragments, Rest-of-day shares, overflow, capacity).
  - `carryover.py`: the rollover of PLAN §5.2.
  - Plan-my-day candidates with reasons (overdue, due, follow-up, P1/P2 Inbox).
  - Recurring tasks: completing one creates the next instance (D-016).
  - Repositories for meetings and their exceptions, time off and leave moves, plan edits, carried items, tracked time per day.
- **Services:** `MeetingService` (series, single occurrences, Meeting now), `PlanService` (day plan + schedule, add/reorder/remove, move overflow), `TimeOffService`, `RolloverService` (startup + day change). Every destructive action has Undo, and Undo never overwrites a later change (D-078).
- **UI:**
  - **Today:** Now card (current task and time left, or the meeting you're in with End meeting / Join, or what's next), capacity bar, "doesn't fit" warning with Move to next day, and a timeline with meetings (Join), lunch, time off, task segments and a "now" line. It refreshes about once a minute.
  - **Plan:** Today / Tomorrow (next working day) toggle, suggestions with 15m / 30m / 1h / 2h / Day / other presets and a task search, the ordered plan with drag-and-drop and Alt+↑/↓, allotment menu (presets, typed time, Rest of day, remove), "doesn't fit" divider, Start Day. Narrow: suggestions slide in (D-083).
  - **Meetings:** next two weeks by day, repeating series, later one-offs; editor (date, time, length, link, repeat with days / every N / ends on, reminders, project); "This occurrence / All occurrences"; confirmation before discarding changed occurrences.
  - **Meeting now** (Ctrl+Shift+M, Today, Meetings): title, link, 15m / 30m / 1h / Unknown, started now or 5–30 min ago.
  - **Time Off…** (main menu): full days or half a day, note, tasks due while away, upcoming list with remove.
  - "Repeats" for tasks; Today badge.

### Verified
- `scripts/check.sh` passes: ruff, mypy (strict core and services, plus the UI), **694 tests**, **97.8 % core coverage**, packaging validation, no markers. All mandatory planner, recurrence and carry-over cases from PLAN §5 are tests.
- `scripts/dev-install.sh`, then a launch from the terminal: **0 bytes of output**.
- The scratch harness (not committed) drove Today, Plan (today and tomorrow), Meetings, the meeting editor, Meeting now, Time off and the detail panel. It ran at 1180 px and 380 px with seeded data and fake time (`DAYBOOK_FAKE_NOW`), and printed no GTK warnings. Snapshots led to four fixes:
  - The Now card compared against a slightly earlier `now` than the schedule's.
  - The capacity bar turned green when full; it now uses the accent colour.
  - The suggestion presets wrapped badly.
  - A series' first date was labelled "Date"; it now reads "Starts on".

### Not in this phase (by plan)
- The timer arrives in Phase 3: Start/Pause on the Now card, tracked time in the timeline, trimming timer time for a "Meeting now" that started in the past.
- Reminders arrive in Phase 4: meeting notifications, "Still in the meeting?", the overflow reminder, start- and end-of-day prompts.

### Test by hand
1. `scripts/dev-install.sh`, then open Daybook. For a quick look with fake time: `DAYBOOK_DATA_DIR=$(mktemp -d) DAYBOOK_FAKE_NOW=2026-11-03T09:40:00 daybook`.
2. **Meetings → New Meeting**: "Standup" on weekdays at 10:00 for 15 min with a link. It shows for each weekday of the next two weeks. Make a weekly meeting on Mon + Wed every 2 weeks and a monthly one on the 31st; check the dates listed.
3. Open today's Standup → **This Occurrence** → move it to 11:00. Only today changes. Open it again → **All Occurrences** → change the start time: Daybook asks before discarding the moved occurrence. Cancel one occurrence (Undo brings it back).
4. **Plan**: add suggestions with the presets and with "Find any open task"; give one "Day" (Rest of day). Reorder by dragging and with Alt+↑/↓. Over-plan the day: items below "Doesn't fit" and the "⚠ … over" warning appear; **Move to Next Day** moves them (Undo works). Switch to **Tomorrow** and plan there.
5. **Today**: the timeline shows the standup with **Join**, lunch and your tasks split around them, with the "now" line. The Now card shows the current task and time left, and "Next: Standup in … min".
6. **Ctrl+Shift+M**: start a meeting with Unknown length that started 10 min ago. Today shows "In meeting · 10m so far" and the plan moves after it. **End Meeting** (Undo works).
7. Main menu → **Time Off…**: mark tomorrow as a full day off. Tomorrow's planned tasks move to the next working day ("1 task moved", Undo works), and the Plan view's "Tomorrow" becomes the day after. Mark this afternoon off: Today's free time shrinks to the morning.
8. Plan a task for "today", don't finish it, then start Daybook the next working day (or test with `DAYBOOK_FAKE_NOW` a day later and the same data folder): it is in the new day's plan, marked "carried ×1", with the remaining time.
9. Give a task a due date and **Repeats → Weekly**, then mark it done: the toast names the next due date, and the new task appears. Undo removes it again.
10. Narrow the window to ~360 px: Plan shows **Add Tasks** to slide in the suggestions; Today stacks cleanly.

---

## Phase 3 — Timer and time tracking ✅

### Built
- **Core:**
  - `timer.py`: the pure state machine (start, switch, pause, resume, stop, heartbeat), with segments under a second and backwards clocks dropped. It also holds recovery (Keep / Stop at last activity / Discard) and the meeting trim (shorten or delete timer entries after the real start, keep live time before it, refuse when manual time is in the way).
  - `tracking.py`: overlap checks for manual entries (against entries and the live segment, ending by now, at most 16 h), typed day and times to UTC (DST-safe, 00:00 = end of day), day clipping, and the timeline's merged "runs".
  - Repositories:
    - `repo/timer.py`: every transition is one transaction that writes the closed segment and the new `timer_state` row. Starting sets In progress.
    - `repo/time_entries.py`: list, add, edit and delete entries, reinsert for Undo, and the trigger mapped to an inline error.
    - Hold, Done and Delete stop the timer inside their own transactions. "Meeting now" creates the meeting, pauses the timer and trims, all in one transaction.
- **Services:**
  - `TimerService`: start, pause, resume, stop, toggle; a heartbeat every 60 s; recovery held until answered; and a normal quit pauses the timer. Manual entries have Undo on delete.
  - `MeetingService.adhoc_effect` previews the trim.
  - The plan schedules the running task first (D-091).
  - `PlanService.first_open` powers Start Day.
- **UI:**
  - **Now card:** the timed task, planned or not, with Pause / Resume / Start, Mark Done, Focus and Open Task. Timed items show time left of the allotment, or how far over it you are. Rest-of-day items show time today and the free time left. A timer paused by a meeting asks "Back to it?".
  - **Timeline:** tracked time joins it, merged per task, with the running block marked.
  - **Task detail:**
    - a Start / Pause / Resume button, with the time counting up while it runs;
    - a **Time Log** with Add Time…, edit (activate a row) and delete (with Undo);
    - a time-entry dialog with inline errors that name what an overlap is with.
  - **Focus window** (Now card → Focus, main menu, or the header indicator):
    - the task, a large countdown or elapsed time, and a Cairo progress ring;
    - Pause / Done / Next, plus Space and Ctrl+Enter;
    - a hint to pin it with "Always on Top", and the reminder slot for Phase 4.
  - **Keys:**
    - **Space** starts or pauses the focused task row (lists, Plan, timeline), otherwise the open task, otherwise the Now card's task.
    - **Ctrl+Enter** marks it done.
  - **Header indicator:** "● PROJ-123 · 35m" while a timer exists; it is hidden below 600 sp.
  - **Recovery dialog** at startup.
  - **Meeting now** previews "This removes 8m from WEB-12" and turns Start off when manual time is in the way.
  - **Start Day** starts the timer on the first plan item (D-084).

### Verified
- `scripts/check.sh` passes:
  - ruff, plus mypy (strict on core and services, and on the UI);
  - **768 tests** at **97.8 % core coverage**, including the mandatory timer recovery tests;
  - packaging validation, and no markers.
- `scripts/dev-install.sh`, then launches of the Devel and the real app from the terminal: **0 bytes of output**. SIGTERM closes the database cleanly.
- The scratch harness (not committed) drove Today with a running timer, the Focus window (running and paused), the detail panel and its time log, an overlapping manual entry, the Meeting now preview, the paused-for-meeting Now card, the recovery dialog, and 380 px Today, with no GTK warnings. A probe drove Space on Today and on a list row, Ctrl+Space (ignored), Ctrl+Enter, Space in a text field (types a space) and Start Day. Fixes found this way:
  - The Focus window had no background: a `css_classes=` constructor argument replaced the window's own classes.
  - The header indicator squeezed the title on narrow windows.
  - The Meeting now dialog was too short (since Phase 2), and the Add Time dialog was too.
  - Undo of a deleted entry could remove a newer entry that SQLite gave the same id. It now reinserts under a new id, and a test covers it.

### Not in this phase (by plan)
- Reminders arrive in Phase 4: allotment warnings and "time is up", drift nudges, auto-pause at a scheduled meeting's start, "Back to PROJ-123?" after a meeting, and reminder cards in the Focus window.
- Idle and screen-lock handling, "close keeps running" and the Quit warning while a timer runs arrive in Phase 7 (D-094).

### Test by hand
1. `scripts/dev-install.sh`, then open Daybook. For throwaway data with fake time: `DAYBOOK_DATA_DIR=$(mktemp -d) DAYBOOK_FAKE_NOW=2026-11-03T09:40:00 daybook`.
2. Plan two tasks for today, then **Today → Start**. The Now card reads "TRACKING", the header shows "● PROJ-123 · 0m", and the time left counts down. **Pause** / **Resume** work, and so does **Space** with nothing focused.
3. Open the other task and press **Start Timer**. The first task's time is saved: it appears in its Time Log and on the timeline as "… tracked". Only one timer runs.
4. Put an on-hold task's timer on: Daybook asks "Resume …?" first. Put the running task **On hold**, or press **Ctrl+Enter**: the timer stops and the time so far is kept. Undo brings the task back, but not the timer.
5. **Focus** (Now card or main menu): the countdown and ring move every second; Pause / Done / Next work. Right-click its title bar → **Always on Top**.
6. Task detail → **Add Time…**: 9:00 to 9:30. Try an overlapping time, a time in the future and an end before the start: each shows an inline message. Edit an entry by clicking it; delete one and **Undo**.
7. With the timer running, press **Ctrl+Shift+M** and pick "10m ago". The dialog says "This removes 10m from …". Start it: the timer pauses ("Paused for a meeting · back to it?") and the last 10 minutes are gone from the log. End the meeting, pause the timer and add a manual entry for the last 5 minutes: Meeting now then refuses "10m ago" with an inline message.
8. **Recovery:** start a timer, then kill Daybook (`pkill -9 -f bin/daybook`) and start it again. "Timer Was Running" offers Discard / Stop at <last activity> / Keep Running. A normal **Quit** with a running timer pauses it instead, so nothing is asked next time.
9. **Plan → Start Day** with no timer running: you land on Today with the first planned item running.
10. Narrow the window to ~360 px: the header indicator hides, and the Now card and timeline stack cleanly.

---

## Phase 4 — Reminder engine ✅

### Built
- **Core:**
  - `reminders.py` (pure):
    - `compute`: every kind from a snapshot of the day. That covers meetings at each offset, snoozes, the 10-min warning and "time's up" (tracked time, not Rest of day), drift nudges, "Back to …?", overflow, the open-meeting check, start and end of day, and auto-pause.
    - `triage`: on time, late (one live meeting), or the missed summary.
    - `still_relevant`: when a shown reminder is moot.
    - `render`: titles at most 40 characters and subject-first, at most 3 buttons, and the view a click opens.
    - Every kind declares its tier, its renderer and its relevance rule; a test checks all three.
  - `repo/reminders.py`: an at-most-once log (`INSERT … ON CONFLICT DO NOTHING RETURNING`), stored payloads, actions, snoozes and the watermark.
- **Services:**
  - `ReminderScheduler`: a snapshot, one timeout (at most 30 s), recompute on changes, day change and resume, the missed summary, withdrawal of moot reminders, and every button.
  - `Notifier`: Gio notifications with tier priority and the subject as id, GSound sounds (silent without GSound; urgent only under Do Not Disturb), in-app events and diagnostics.
  - `PowerMonitor`: logind PrepareForSleep / PrepareForShutdown.
  - Auto-pause and the "ended at last activity" end for open meetings.
- **UI:**
  - The reminder card in the main window and the Focus window (D-098).
  - **Preferences** (Ctrl+,) with the Notifications page (D-102).
  - The minimal **Review** page and Today's **Review Day** (D-103).
  - Notification clicks bring up the right view (Plan, Review or Today).

### Verified
- `scripts/check.sh` passes:
  - ruff, plus mypy (strict on core and services, and on the UI);
  - **843 tests** at **97.9 % core coverage**, including the mandatory reminder cases (every kind and tier, keys, leave and half days, Rest of day, overflow sets, open meetings, missed while closed or asleep, no duplicates across a restart, snooze, +15 min);
  - packaging, and no markers.
- **D-053 tiers on this machine (D-097):** checked in the installed GNOME Shell 50.1 code (urgent never auto-hides, high and normal hide after 4 s). An end-to-end run of the installed app with `DAYBOOK_TIME_SCALE=60` against a private headless shell captured the notifications on its bus: the right priorities, one id per subject (replace, not stack), and the buttons. Nothing reached the real desktop.
- **Harness:** the screen was locked, so the scratch harness ran against a private headless GNOME Shell. It showed the meeting card, the Focus-window card, the test card, Preferences, Review and 380 px, with no GTK warnings. Fixes found this way:
  - Card buttons stretched; they now sit at their natural width.
  - The card is now clamped to the page width.
  - A newer, less urgent card could hide a meeting card; the card now shows the most urgent one.
- **Tests caught two design gaps:**
  - A drift nudge whose time had passed before the first check was dropped. It is now due "now".
  - Reminders due exactly "now" were skipped on the first check. They now always count.

### Not in this phase (by plan)
- Quick add and the CLI arrive in Phase 5.
- The full end-of-day review, standup and reports arrive in Phase 6.
- Background running on close, start at login, idle detection and the other Preferences pages arrive in Phase 7. Until then, reminders fire while Daybook is open, and anything missed while it was closed is summarised at the next start (D-029).

### Test by hand
Reminders need the installed app (D-038): run `scripts/dev-install.sh` first.
1. **Preferences** (Ctrl+,) → Notifications: the row reads "Notifications work". Press **Send Test Reminder**: a banner appears and **stays** until you click Works or Snooze. With Snooze it comes back 2 minutes later. The card in the window shows the same buttons.
2. Fast time: `DAYBOOK_DATA_DIR=$(mktemp -d) DAYBOOK_TIME_SCALE=60 DAYBOOK_FAKE_NOW=2026-11-03T09:40:00 daybook` (1 s = 1 min). Add a meeting at 10:00 with a link, and plan a task for 20 min.
   - At 09:50 "Standup in 10 min" **stays** on screen. At 09:59 "Standup in 1 min" replaces it, with no second banner. **Join** opens the link.
3. Start the task's timer before 10:00: at 10:00 the timer pauses for the meeting. At 10:15 "Back to …?" appears; **Resume** restarts the timer.
4. Let the timer run: "10 min left" arrives quietly (no sound), then "Time's up" stays. Press **+15 min**: the banner goes, and a new "Time's up" comes 15 minutes later. **Mark Done** stops the timer.
5. Don't start anything during planned time: after 5 minutes "Planned now: … — start it?" arrives (Act tier: it hides after a few seconds and stays in the list). **Skip** moves that item to the end of today's plan.
6. Over-plan the day: "N tasks no longer fit today" arrives once. **Move to Tomorrow** moves them.
7. Quit, wait past a meeting in fast time, and start again: one "While you were away" notification lists it. If a meeting is still running, you get one "started N min ago" instead.
8. Start "Meeting now" with Unknown length, quit, and restart: "Still in “Meeting”?" offers End Now / Ended at <time> / Still in It.
9. Turn on Do Not Disturb: Info and Act reminders don't pop up (they're in the list); meeting reminders still do, which is GNOME's rule for urgent ones.
10. Suspend the laptop across a reminder time and wake it: you get the summary (or one live meeting reminder), never a burst.
11. Click "Wrap up" (or **Review Day** on Today): the Review page lists today's tasks and what carries over.

---

## Phase 5 — Fast input ✅

### Built
- **Core:** `quickadd.py`, the quick-add parser of PLAN §5.7.
  - Tokens: ticket key, `#project`/`#tag`, `+requested_by`, `!p1–!p4`, `~estimate`, `^due`, and `@plan` with `=2h` or `=day`.
  - It never raises, reports every error at once, warns about overrides, and has a `\` escape (D-105).
- **Services:**
  - `TaskService.parse_quick` and `quick_add` add the ticket-in-use warning and the same plan rule as the New Task dialog.
  - `services/status.py` builds the `--status` line from a live or a read-only connection.
- **UI:**
  - **Quick Add** (Ctrl+K and the main menu): the live preview with chips, errors and warnings, Add off while there are errors, `+name` completion, and Enter to add.
  - The **Keyboard Shortcuts** dialog (Ctrl+?), Ctrl+1…7 for the sections, and the new menu items.
- **CLI:** `--quick-add [text]`, `--meeting-now`, `--background`, `--status` and `--debug`, all forwarded to the running instance. `--status` also works offline (D-106, D-107).
- **README**, new: install and run, fake time, the command line, the quick-add syntax, shortcuts, and binding `daybook --quick-add` / `--meeting-now` as GNOME custom shortcuts.

### Verified
- `scripts/check.sh` passes:
  - ruff, plus mypy (strict on core and services, and on the UI);
  - **951 tests** at **98.0 % core coverage** (the parser at 100 %, table-driven over every token, malformed variants, ordering, duplicates, unicode and empty input);
  - packaging, and no markers.
- **Forwarding, end to end:** the installed app ran under its real app ID, with XDG folders in a scratch directory, on a private headless GNOME Shell and session bus, so your data and desktop weren't touched.
  - `--status` printed "No timer running" offline before any database existed, forwarded to the running instance, and offline again afterwards.
  - `--quick-add`, `--meeting-now` and `--background` reached the running instance; the log shows each as "forwarded".
  - An unknown option was reported. The primary printed nothing.
- **Harness** (headless shell, no GTK warnings): Quick Add with a full line, with errors, with the override and ticket-in-use warnings, and empty; adding with Enter; the shortcuts dialog; and a narrower window. A probe checked `+ra` → `+Rahul_Shah`, replacing only that word. Fixes found this way:
  - A forwarded `--meeting-now` was ignored while Quick Add was open (D-108).
  - The preview card had no padding.
  - Text passed with `--quick-add` came up selected, so the first keystroke would have replaced it.

### Not in this phase (by plan)
- The review, standup, timesheet and reports arrive in Phase 6.
- Start at login (`--background` autostart), close-keeps-running, the tray and the other Preferences pages arrive in Phase 7.

### Test by hand
1. `scripts/dev-install.sh`, then open Daybook and press **Ctrl+K**. Type `PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today` (with a project whose key prefix is PROJ): the preview shows the project, ticket, P1, ~2h, due Fri and "Today: 2h". Press Enter: the task is in Today's plan.
2. Try errors: `x !p9`, `x ^31/02`, `x @sat`, `#pms !p1` (no title). Each shows its message and Add stays off. `PROJ-1 Fix #OtherProject` warns that the project overrides the prefix.
3. Type `Prepare demo +Ra` (after a task "from Rahul Shah" exists): a suggestion appears. Down and Enter complete it to `+Rahul_Shah`.
4. **Ctrl+?** shows all shortcuts. **Ctrl+1…7** switch sections; Ctrl+, opens Preferences.
5. Bind a key as the README describes (Settings → Keyboard → Custom Shortcuts, command `/home/<you>/.local/bin/daybook --quick-add`, for example Super+K). Press it from another app: Daybook comes up with Quick Add. Do the same with `--meeting-now`.
6. In a terminal: `daybook --status` with Daybook running and a timer on, then after quitting (it reads the saved, paused timer). `daybook --quick-add Call the bank @today` opens Quick Add with that text.
7. `daybook --background` (with Daybook closed): no window opens, but reminders still arrive. Open Daybook from the app grid and Quit to stop it.

