# Build "Daybook" — a production-grade personal work planner for Ubuntu 26.04

## Role and standard
You are a senior Linux desktop engineer. Build a real, shippable app, not a demo. Reliable reminders and correct time tracking matter more than the number of features. Work in phases (listed at the end). Finish, test and commit each phase before you start the next. A finished phase must not contain TODOs, stubs, placeholder buttons or commented-out code. If a feature can't work reliably on this platform, say so and propose the closest reliable alternative. Don't fake it.

## The user and the problem
I'm a software professional. My work comes as tickets in a PMS tool, grouped by project. Right now I plan my day in a paper notebook. It holds my tasks with their priorities, how much time each task gets today, and the meetings I must join. I want an app that replaces this notebook. I plan my day in it, it keeps me on schedule, and it reminds me with messages like:
- "15 min left on PROJ-123"
- "Time for PROJ-123 is used up for today. Switch to PROJ-456"
- "Standup in 5 minutes [Join]"

I'll keep using pen and paper for scratch notes.

## Platform and stack (fixed)
- Ubuntu 26.04 LTS with GNOME in a **Wayland** session (there is no X11). The app must work on Wayland without hacks.
- Python 3 (the system version) with PyGObject, **GTK 4 and libadwaita** from the Ubuntu repos. Follow the GNOME HIG, and follow the system's light/dark mode and accent color.
- Storage: SQLite through the stdlib `sqlite3` module. Turn on WAL mode and foreign keys, and use versioned migrations. Store settings in a SQLite `settings` table so that backups include them.
- Runtime dependencies: Ubuntu apt packages only (`python3-gi`, `gir1.2-gtk-4.0`, `gir1.2-adw-1`, `python3-dateutil`, and optionally `gir1.2-gsound-1.0`). No pip packages at runtime, because Ubuntu's Python is externally managed (PEP 668). Put dev tools (pytest, ruff, mypy) in a venv created with `--system-site-packages`.
- Build and install with Meson + Ninja, using the standard GNOME app layout. App ID: `io.github.daybook.Daybook`, defined once as a constant.
- **Before writing any code**, check which versions of Python, GTK and libadwaita are installed, and target those versions. Don't use APIs that are deprecated in GTK 4.10+:
  - Don't use: GtkTreeView, GtkComboBox, GtkDialog, GtkMessageDialog.
  - Use instead: Gtk.ListView/ColumnView, Gtk.DropDown, Adw.Dialog, Adw.AlertDialog, Adw.PreferencesDialog and Adw.ToastOverlay.

Wayland limits. Design around them rather than fighting them:
- Apps can't register global hotkeys. Provide a `daybook --quick-add` command instead. The user binds it under GNOME Settings → Keyboard → Custom Shortcuts. Document this.
- Apps can't force themselves on top or set their window position. Provide a compact "Focus" window that the user can pin with GNOME's window menu ("Always on Top").
- GTK 4 has no tray API. Notifications are the main channel. A tray icon is optional (Phase 7).

## Architecture
- `daybook/core/`: pure Python with **no GTK imports**. It contains the models (dataclasses), the repository (SQLite), the planner (auto-schedule), recurrence, reminder rules, the quick-add parser, and the timesheet, standup and report generators. Unit-test all of it.
- `daybook/services/`: the glue layer. It contains the reminder scheduler, timer service, notifications, idle and suspend monitoring, and backups. Each service takes an injectable `Clock` so tests can control time.
- `daybook/ui/`: GTK widgets only. No SQL and no business logic in the UI.
- The DB is the single source of truth. The UI reacts to change signals from the service layer (an event bus or GObject signals), which keeps every view in sync.
- Store times in UTC (ISO 8601) and display them in local time. A "day" is a local calendar date. Handle DST and timezone changes correctly.
- Use Gio.Application for a single instance. A second launch, or `--quick-add`, is forwarded to the running instance.
- Use XDG paths. Data goes in `~/.local/share/daybook/`. Logs go in `~/.local/state/daybook/`, rotating at 5 × 1 MB.
- Install a global exception hook. It logs the traceback and shows a toast: "Something went wrong — details in log". The app must never crash silently or lose data.
- Every DB write runs in a transaction. No single UI action may block the main loop for more than 100 ms.

## Data model (minimum)
- **Project**
  - name, color, ticket key prefix (e.g. `PROJ`)
  - ticket URL template (e.g. `https://pms.example.com/browse/{key}`)
  - archived flag
- **Task**
  - title, project
  - ticket key: optional, e.g. `PROJ-123`. It is a clickable link that opens in the browser.
  - priority: P1–P4
  - status: To do / In progress / Blocked / In review / Done, plus a reason when Blocked
  - total estimate, due date
  - notes (plain text), checklist items, tags
  - created and completed timestamps
  - recurrence (optional)
- **Meeting**
  - title, start, duration
  - link: any URL (Meet, Teams, Zoom, …)
  - recurrence: none / daily / weekdays / weekly on chosen days / monthly
  - reminder offsets: default 10 min and 1 min
  - linked project (optional)
  - Editing **one occurrence or the whole series** must work. Store single-occurrence changes in an exceptions table.
- **DayPlanItem**: date, task, minutes allotted for that day, order.
- **TimeEntry**
  - task, start, end, note
  - source: timer / manual
  - Entries may never overlap.
- **ReminderLog**
  - What fired, when, and what the user did.
  - A unique key per reminder occurrence prevents duplicates and powers the "missed" summary.
- **Settings**
  - Working hours and workdays, lunch break.
  - Default meeting reminder offsets; warning before an allotment runs out (default 10 min).
  - Sound on/off; auto-pause the timer during meetings.
  - Timesheet rounding (default 15 min).
  - Start at login, idle threshold, run in background on close.

## Features

### 1. Inbox, projects and tasks
- Sidebar (Adw.NavigationSplitView) with these sections and badge counts:
  - Today, Plan, Inbox (unplanned tasks), Upcoming
  - One entry per project
  - Meetings, Reports, Done
- Task lists:
  - Sort by priority, then by due date.
  - Filter by project, status, priority and tag.
  - Instant search (Ctrl+F) across title, ticket key and notes.
- Task detail pane:
  - Edit every field inline, including the checklist.
  - "Open ticket" button.
  - Time logged today and in total, compared with the estimate.
- Make overdue and due-today tasks look different from other tasks. Hide Done tasks from the active views but keep them searchable.
- Delete and complete show a toast with Undo.

### 2. Day planning (the notebook replacement)
- **"Plan my day" view**:
  - Left side: candidate tasks. These are carry-overs from the previous workday, overdue tasks, tasks due today, and P1/P2 tasks in the Inbox.
  - Right side: today's plan.
- To add a task to today, give it an allotted time: a preset (15m / 30m / 1h / 2h) or a custom value. Reorder the plan with drag-and-drop **and** with the keyboard (Alt+Up/Down).
- **Auto-schedule** is a pure function in `core/planner.py`.
  - Inputs: working hours, lunch break, today's meetings (fixed anchors), the ordered plan items with their allotted minutes, and the current time.
  - Output: timeline segments. A task may be split around a meeting.
  - It must be deterministic and unit-tested, including these cases:
    - planning starts mid-day
    - a meeting overlaps lunch
    - two meetings back to back
    - the plan overflows past the end of the day
- **Capacity bar**: e.g. "Planned 7h 30m / free 5h 45m". Warn when the plan is over capacity, and show which items won't fit.
- **Today view**:
  - A vertical timeline of meetings and task segments, with a "now" line.
  - A **Now card** at the top showing:
    - the current task and its timer
    - time left in today's allotment
    - what's next
    - a countdown to the next meeting

### 3. Focus timer and time tracking
- Start, pause and stop the timer on any task (Space starts or pauses the selected task). Only one timer can run at a time. Starting another task stops the current timer and saves its entry.
- Save the timer state on every change, so it survives an app restart, a crash or a reboot. On restart, ask: "Timer was running for PROJ-123 since 14:05 — Keep / Stop at last activity / Discard".
- Manual time entries can be added, edited and deleted. Validate them against overlaps.
- **Compact Focus window** showing:
  - the task
  - a large remaining-time display and a progress ring
  - Pause / Done / Next buttons

### 4. Reminders and notifications (the most important part: it must be reliable)
Use Gio.Notification with action buttons. These only work when the app is installed with a `.desktop` file and a D-Bus service file that match the app ID. That means the dev workflow must install to `~/.local`. Document this.

**Reminder types**
- **Allotment warning**: when N minutes remain (default 10), send a heads-up. This is based on time actually **tracked** today, not on the wall clock.
- **Allotment used up**: at 0 minutes left, send "Time for PROJ-123 is up for today (planned 1h, spent 1h 02m). Next: PROJ-456" with the actions **[Switch to next] [+15 min] [Mark done]**.
- **Drift nudge**: if no timer runs during a planned segment for more than 5 minutes within working hours, send "Planned now: PROJ-456 — start it?" with **[Start] [Skip]**. Send it at most once per segment.
- **Meetings**:
  - At each reminder offset, send a notification with **[Join]** (opens the link) and **[Snooze 2 min]**. Use urgent priority.
  - If auto-pause is on and a timer is running when the meeting starts, pause it.
  - After the meeting ends, ask "Back to PROJ-123?" with **[Resume] [Pick another]**.
- **Start of day**: at work start, send "Plan your day". It opens the Plan view.
- **End of day**: 15 minutes before work end, send "Wrap up". It opens the Review view.

**Scheduler design**
- One scheduler computes the next due reminder from DB state and arms a single GLib timeout for it. Recompute whenever data changes.
- Re-check every 30 s as well, to catch jumps in the wall clock.
- Listen for logind `PrepareForSleep` on the system bus, and recompute on resume.
- If reminders were missed during sleep or while the app was closed, **don't fire them one by one**. Show a single summary instead: "While you were away: Standup 10:00 (missed), PROJ-123 allotment ended".
- **Never fire the same reminder twice.** Use the ReminderLog unique key.

**Delivery**
- Leave GNOME Do Not Disturb to the shell and don't try to get around it. Meeting reminders use urgent priority.
- Play a sound through GSound if it's available, and skip it silently if it isn't. Sound has an on/off toggle in Settings.
- When the main window has focus, also show an in-app banner.

### 5. Fast input
- **Quick add** opens with Ctrl+K in the app, or from anywhere with `daybook --quick-add`. It takes one line, which `core/quickadd.py` parses:
  `PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today`
  - The ticket key is detected automatically, and the project is inferred from its prefix.
  - `#` sets the project, `!` the priority and `~` the estimate.
  - `^` sets the due date. It accepts today, tomorrow, weekday names, dd/mm and yyyy-mm-dd.
  - `@today` adds the task to today's plan.
  - Show a live preview of the parsed result before saving. The parser needs exhaustive unit tests.
- Keyboard shortcuts for everything, plus a shortcuts window (Ctrl+?):
  - Ctrl+N new task, Ctrl+K quick add, Ctrl+F search
  - Space start/pause, Ctrl+Enter mark done
  - Ctrl+1…7 switch sections, Ctrl+, preferences
- Command-line options:
  - `--quick-add`
  - `--background`: start hidden; used for autostart
  - `--status`: print the current task and its remaining time, for use in scripts
  - `--debug`

### 6. Review, standup, timesheet, reports
- **End-of-day review**:
  - What was done today, and time spent per task.
  - Unfinished planned tasks, each with **[Move to tomorrow] [Back to inbox]**, plus "apply to all".
- **Standup generator**:
  - Three sections: "Yesterday / Today / Blockers".
  - Yesterday means the previous workday, so on Monday it shows Friday.
  - Built from completed tasks, time entries, today's plan, and blocked tasks with their reasons.
  - Copy to the clipboard as plain text or Markdown.
- **Timesheet**:
  - Day or week view, with one row per ticket, rounded according to Settings, and totals.
  - Copy as text or export as CSV, so the PMS timesheet can be filled in quickly.
- **Reports** (weekly):
  - hours per project
  - planned vs actual per task (estimation accuracy)
  - tasks completed
  - meeting hours vs focus hours

  Use simple Cairo charts (GtkDrawingArea) or clean tables, with no heavy dependencies.

### 7. Background, settings, data safety
- Closing the window keeps the app running so reminders still fire (this is a setting, on by default). "Quit" in the menu really quits, and warns first if a timer is running.
- **Start at login**: the toggle writes or removes `~/.config/autostart/<app-id>.desktop` with `--background`.
- **Idle detection**:
  - Use `org.gnome.Mutter.IdleMonitor` on the session bus. If the user is idle longer than the threshold (default 10 min) while a timer runs, ask on their return: "You were away 14 min — Keep / Discard that time".
  - Detect screen lock through `org.gnome.ScreenSaver`.
  - Degrade gracefully if either interface is unavailable.
- **Tray icon (optional)**:
  - Implement a StatusNotifierItem over D-Bus. Ubuntu's AppIndicator extension displays it. Don't use any GTK 3 libraries.
  - It shows the current task and remaining time, with a menu: Show, Quick add, Pause/Resume, Quit.
  - Skip it silently if there's no host.
- **Backups**:
  - Back up the DB automatically every day with the sqlite3 backup API, and keep the last 14.
  - Provide "Backup now" and "Restore from backup" (with confirmation).
  - Provide full JSON export/import, and CSV export of time entries. Validate an import before writing anything.
- Every setting is in an Adw.PreferencesDialog.
- **First-run onboarding**: set working hours and workdays, create the first project, and optionally load sample data.

### Out of scope for v1 (keep the architecture open for these)
- Cloud sync, mobile, team features, AI.
- PMS API integration (importing from Jira, Redmine, etc.) comes later as a plugin. Define a `TaskSource` interface now, but don't implement it.

## UI/UX quality bar
- Native, clean GNOME look. Adaptive layout down to 360 px width (Adw breakpoints). Dark mode and HiDPI.
- Every list that can be empty has an empty state (Adw.StatusPage) with a useful action.
- Every destructive action has a confirmation or an Undo. Every form validates with inline messages. No exception ever reaches the user as a crash.
- The app is fully usable with the keyboard only. Icon-only buttons have accessible labels and tooltips.
- Show times in a human-friendly form ("in 12 min", "1h 05m").

## Engineering quality bar
- Type hints everywhere. Ruff (lint + format) passes with no warnings. Run mypy `--strict` on `daybook/core` and `daybook/services`.
- pytest coverage of `daybook/core` must be at least 90%. These tests are mandatory:
  - planner
  - recurrence, including DST, month-end and single-occurrence exceptions
  - quick-add parser
  - reminder computation, including suspend/resume, the missed summary and no duplicates
  - timer recovery
  - timesheet rounding
  - migrations from every earlier schema version
  - backup/restore round-trip
- Inject the clock everywhere time matters. Add these dev env vars:
  - `DAYBOOK_TIME_SCALE=60` (1 real second = 1 minute) and `DAYBOOK_FAKE_NOW=<iso>`, so reminders can be tested by hand without waiting
  - `DAYBOOK_DATA_DIR=<path>`, to run against a throwaway database

  None of these have any effect unless they are set.
- Validate the packaging files: `desktop-file-validate` for the .desktop file and `appstreamcli validate` for the metainfo.
- Shut down cleanly: save the timer state and close the DB.
- Launching the app prints no GTK criticals, warnings or deprecation messages in the terminal.

## Packaging
- Meson installs:
  - the Python package and launcher
  - the .desktop file
  - the SVG app icon and its symbolic variant
  - metainfo.xml
  - the D-Bus service file (`DBusActivatable=true`, so notification actions work)
- `scripts/dev-install.sh` and `scripts/dev-uninstall.sh` install to and remove from `~/.local`.
- `scripts/build-deb.sh` builds `daybook_<version>_all.deb` with correct `Depends`:
  - Installs cleanly with `sudo apt install ./daybook_*.deb` and removes cleanly.
  - lintian reports no errors.
- README covers:
  - install and uninstall
  - binding the quick-add shortcut
  - where data lives
  - backup and restore
  - troubleshooting notifications

## How to work
1. Read this whole spec. Write `docs/PLAN.md` with the architecture, DB schema, module list and phase checklist. Ask me only about real ambiguities. For everything else, choose a sensible default and record it in `docs/DECISIONS.md`.
2. Create `CLAUDE.md`: how to run, test, lint and install; code conventions; the current phase.
3. Implement **one phase at a time**. At the end of each phase:
   - Run ruff, mypy and pytest; all must pass.
   - Launch the app and confirm the terminal shows no warnings.
   - Update `docs/PROGRESS.md`.
   - Commit to git with a clear message.
   - **Stop**, and give me a short list of things to test by hand.
4. Never skip, delete or weaken a failing test to make it pass. Fix the cause.

### Phases
- **Phase 0, Scaffold**: Meson layout, Adw.Application with a main window and sidebar, logging, exception hook, DB with a migration framework, settings, clock abstraction, test/lint/type tooling, dev-install script. Done when the app launches from the GNOME app grid.
- **Phase 1, Projects and tasks**: models, repository, CRUD UI, task detail, filters, search, undo, empty states.
- **Phase 2, Meetings and planning**: meetings with recurrence and exceptions, Plan view, auto-scheduler, capacity bar, Today timeline with the Now card.
- **Phase 3, Timer and time tracking**: timer service, persistence and recovery, manual entries, Focus window.
- **Phase 4, Reminder engine**: scheduler, every notification type with its actions, suspend/resume, missed summary, start/end-of-day prompts, auto-pause during meetings.
- **Phase 5, Fast input**: quick-add parser and dialog, CLI options, single-instance forwarding, shortcuts window.
- **Phase 6, Review and reporting**: end-of-day review, carry-over, standup generator, timesheet, reports.
- **Phase 7, Background and safety**: run in background, autostart, idle detection, tray (optional), backups, export/import, preferences, onboarding.
- **Phase 8, Hardening and packaging**:
  - Write `docs/QA_CHECKLIST.md` from this spec and work through every item.
  - Test performance with 5,000 tasks and 50,000 time entries.
  - Build the .deb and finish the README.

### Definition of done (whole app)
- It installs from the .deb on a clean Ubuntu 26.04 and shows up in the app grid.
- It survives reboot and suspend without missing or duplicating any reminder.
- All tests and linters pass, and the terminal shows no warnings.
- Every item in this spec works, or is listed in `docs/DECISIONS.md` with the reason it doesn't.

---

## Addendum A — Requirements added 2026-11-03
These come from how my work actually goes. **Wherever this addendum differs from the sections above, the addendum wins.** The goal is an app that's *simpler than my PMS*, with reminders I can't miss and a modern UI.

### A1. Simpler than the PMS
- Creating a task needs only a title. A new task defaults to the **General** area and priority P3. Every other field (ticket, due date, estimate, requested by, notes, checklist, tags, recurrence) is under "More details", collapsed by default.
- Quick add stays the fastest way in. Never add a required field.

### A2. Where tasks come from
- **PMS tickets:** a ticket key is optional, as before.
- **Face-to-face requests:** a task can record who asked for it in an optional **Requested by** field (free text with autocomplete). It shows in task rows, the standup and the review.
- **Non-project work** (new docs, demos, internal work and so on) belongs to an **area**. An area is a project with no ticket prefix or URL, such as "Docs" or "Demos". A built-in **General** area catches everything else. It can be renamed but not deleted or archived. Reports, timesheets and filters treat areas exactly like projects.

### A3. Statuses: To do / In progress / On hold / Done
- These four replace "Blocked" and "In review".
- **On hold** needs a reason: *Blocked*, *Waiting for approval/review*, *Waiting for someone*, *Postponed* or *Other*. It also takes an optional note and an optional **follow-up date**.
- Putting a task on hold stops its timer and takes it out of today's remaining plan (with Undo).
- On its follow-up date the task appears again in "Plan my day" and is mentioned in the start-of-day reminder.
- On-hold tasks feed the standup's "Blockers" section, with the reason and how many days they've been waiting.

### A4. Reschedule ("Move to…")
- Every task, plan item, review row and the relevant notifications offer **Move to…: Tomorrow / Next working day / Pick a date / Unplanned**, with an optional reason.
- The app counts how often a task was moved and shows it ("moved ×3"), so repeatedly postponed work is visible.

### A5. Unfinished work carries over automatically
- At the start of each working day, every unfinished item from the last planned working day is added to today's plan with its **remaining** time and marked "carried over (N days)". Unfinished means not Done, not On hold and not deleted.
- The original day keeps its history, so plans can be compared with what actually happened.
- This also works after days when the app wasn't running, and after leave. It can be turned off in Settings.

### A6. Time off
- I can mark **leave or holidays**: a full day, or half a day (morning or afternoon).
- Leave days are not working days anywhere: planning, carry-over, "previous/next workday", the standup's "Yesterday" (it shows my last *worked* day), start/end-of-day prompts, drift and allotment reminders.
- Meeting reminders are also silent on leave days unless I turn them on in Settings.
- Marking leave moves that day's plan to the next working day (with Undo) and lists the tasks that fall due while I'm away.

### A7. Emergency meetings
- **"Meeting now"** (button on the Today view and Now card, Ctrl+Shift+M, tray menu, `daybook --meeting-now`) starts an ad-hoc meeting immediately.
- The meeting has a title, an optional link and a duration: 15 / 30 / 60 min or *Unknown*. It can also have **already started** 5–30 min ago. In that case, timer time recorded after the real start is removed, after the app shows how much.
- The running timer pauses. When I end the meeting, its real duration is saved and the app asks "Back to PROJ-123?".
- The plan then reflows around the meeting. If items no longer fit, the app says so and offers **[Move to tomorrow] [Review plan]**.
- If an ad-hoc meeting is still open at wrap-up time or on restart, the app asks whether I'm still in it.

### A8. Whole-day tasks
- A plan item can be **"Rest of day"** instead of a fixed time. It fills the free time left after the timed items. It gets no "time is up" reminders, its timer shows elapsed time, and it keeps carrying over until it's Done. This covers work that takes a whole day or several days with no known end.

### A9. Reminders I can't miss
- On Wayland, an app can't force a window in front of others, so the reliable pop-up is the GNOME notification banner. Reminders come in three tiers:

  | Tier | Reminders | Behaviour |
  |---|---|---|
  | **Stay** | meeting reminders, "time is up", "still in the meeting?" | Urgent priority. The banner stays on screen until I act, with sound. |
  | **Act** | drift nudge, "back to task?", plan overflow | High priority. The banner hides after a few seconds and stays in the notification list. |
  | **Info** | "10 min left", start of day, wrap up, "while you were away" | Normal priority. Same as Act. |

- One notification per subject. A newer reminder replaces the older one, so "Standup in 1 min" replaces "Standup in 10 min". A reminder that no longer applies is withdrawn: the meeting is over, the task is done, the timer started, or the time was extended.
- Each notification has at most three buttons and a click action that opens the right view.
- When the compact Focus window is open (pinned with "Always on Top"), it shows the same reminder inline with the same buttons.
- Settings has **"Send test reminder"** and a diagnostics row that explains what's wrong if notifications can't work (for example, the app isn't installed).

### A10. Modern UI
- Calm, fast and modern, within the GNOME HIG and libadwaita's own styling (accent colour, light/dark):
  - one primary action per screen
  - lists you can scan, with no table chrome
  - colour dots for projects and areas
  - small chips for priority, "carried over", "moved", "on hold"
  - big, clear numbers for the timer and countdowns
  - smooth but short animations that respect the system's reduce-animation setting
- Detail panels slide in beside the list on wide windows and become full pages on narrow ones.
- The screen designs are in `docs/PLAN.md` §6.
