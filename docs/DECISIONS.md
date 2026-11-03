# Decisions

Defaults chosen where [`SPEC.md`](SPEC.md) (including Addendum A) is silent or ambiguous. Each entry gives the decision and the reason. Entries marked **(user)** were answered by the user. Later changes add a new entry that supersedes the old one; old entries are not edited.

Format: **D-NNN — Title**. Decision. *Why.*

---

## Process and tooling

**D-001 — `#word` in quick-add (user).** `#word` sets the project when `word` matches an existing project's name or key prefix (case-insensitive). Otherwise it becomes a tag. An explicit `#project` beats the project inferred from the ticket prefix, and the preview shows a warning ("#backend overrides PROJ from PROJ-123"). *The spec's example uses both a ticket prefix and `#backend`, and tasks have tags but no other quick-add syntax.*

**D-002 — Git branch flow (user).** *(Branch names superseded by D-064.)*
- Create `main` with an initial docs commit (SPEC, PLAN, DECISIONS, CLAUDE.md) at the start of Phase 0.
- Each phase is committed on its own branch `phase-N`, created from the previous phase's branch, so phases build on each other even before merges.
- The user merges. Nothing is pushed unless the user asks.

**D-003 — Version targets.** Minimum GTK 4.22, libadwaita 1.9, Python 3.14, as installed on Ubuntu 26.04.1 (see PLAN §1). No runtime version checks or fallbacks for older libraries. *The spec says to target the installed versions. Adw 1.9 provides ShortcutsDialog, BottomSheet, ToggleGroup and others, which replace deprecated GTK widgets.*

**D-004 — No GResource, no Blueprint.** UI is built in Python. `style.css` is installed to `pkgdatadir` and loaded with `Gtk.CssProvider.load_from_path`. Icons are installed into the hicolor theme. *`glib-compile-resources` and `blueprint-compiler` aren't installed, and avoiding them keeps the build and .deb dependencies to meson/ninja only.*

**D-005 — Install layout and the single APP_ID.** *(Install directory amended by D-058.)* This is the standard GNOME Python app layout:
- The `daybook` package is installed to `$pkgdatadir` (`<prefix>/share/daybook/`).
- `bin/daybook` is configured by meson with that path and inserts it into `sys.path`.
- `APP_ID` and `VERSION` are defined once in `daybook/const.py`. `meson.build` reads them with `run_command(python3, '-c', …)` and substitutes them into the .desktop, metainfo, D-Bus service and icon file names.

*The Python and packaging files can't drift apart, and it matches GNOME Builder's template.*

**D-006 — .deb without debhelper.**
- `scripts/build-deb.sh` does `meson setup --prefix=/usr`, `meson install --destdir`, writes `DEBIAN/control` from `packaging/deb/control.in`, then `dpkg-deb --root-owner-group --build`, then runs `lintian`.
- `Architecture: all`.
- `Depends: python3 (>= 3.14), python3-gi (>= 3.56), python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.22), gir1.2-adw-1 (>= 1.9), python3-dateutil`.
- `Recommends: gir1.2-gsound-1.0`.
- Includes `usr/share/doc/daybook/copyright` and `changelog.gz`.

*debhelper and dh-python aren't installed. A pure-Python, arch-independent package doesn't need them, and lintian still validates the result.*

**D-007 — Dev tooling.**
- `.venv` is created with `python3 -m venv --system-site-packages`.
- Tools: ruff, mypy, pytest, pytest-cov, and `pygobject-stubs` installed with `PYGOBJECT_STUB_CONFIG=Gtk4,Gdk4`.
- `.venv/` is git-ignored.
- `pyproject.toml` holds tool configuration only. Meson does the packaging.

*PEP 668 forbids system pip installs, and the stubs make `mypy --strict` work on services that touch GLib/Gio.*

**D-008 — Strings marked for translation, English only in v1.** User-visible strings go through `_()` (gettext, domain `daybook`). No `po/` directory or translations ship in v1. *Translating later then needs no code changes, and v1 avoids the meson i18n machinery.*

## Time

**D-009 — Timestamp storage.**
- Instants are stored as TEXT in the fixed-width form `YYYY-MM-DDTHH:MM:SSZ` (UTC, second precision).
- Local calendar dates are stored as `YYYY-MM-DD`.
- Only `core/timeutil.py` converts between the stored strings and datetimes.

*The spec requires UTC ISO 8601. A fixed width makes string comparison chronological, so indexes and range queries work.*

**D-010 — Meetings recur in their own wall-clock time.**
- Each meeting stores `tzid`: the local IANA zone when it was created.
- Occurrences are expanded in that zone's wall-clock time and converted to UTC, so a 10:00 standup stays at 10:00 across DST.
- If the user changes the system timezone, meetings stay anchored to their zone and are shown converted. A 10:00 Berlin standup shows as 09:00 in London.

*This is how calendars behave and how real meetings work.*

**D-011 — Monthly recurrence on days 29–31** falls on the month's last day in shorter months: Jan 31 → Feb 28/29 → Mar 31. *The alternative of skipping months, which is what RFC 5545 `BYMONTHDAY` does, surprises users. The spec asks for month-end tests.*

**D-012 — "Weekdays" recurrence means Monday–Friday.** *(Amended by D-048: leave days are not working days either.)* The *workdays* setting is used only for planning and reminders: start/end of day, "previous workday", carry-over. *A recurring meeting's schedule belongs to the meeting, not to the user's personal workdays.*

**D-013 — Editing a whole series after single-occurrence changes.** If the series start or recurrence changes, exceptions whose `original_start_utc` no longer matches an occurrence are deleted, after a confirmation that names how many ("2 single-occurrence changes will be discarded"). Editing title, link or offsets keeps all exceptions. Deleting offers "This occurrence", which adds a cancelled exception, or "All occurrences", a soft delete. *There's no reliable way to map an orphaned exception onto a new rule, and the confirmation prevents silent loss.*

**D-014 — Timezone changes.** The local zone is resolved from `/etc/localtime` (via `zoneinfo`) and re-read on every 30 s scheduler tick and on `org.freedesktop.timedate1` PropertiesChanged. When it changes, `time.tzset()` is called, a `Changed(settings)` event refreshes all views, and the scheduler recomputes. *The spec requires correct handling of timezone changes. A day is a local calendar date, so views must re-bucket.*

**D-015 — Planner details.**
- Free gaps shorter than 5 minutes are not filled with task segments.
- Nothing is scheduled past `work_end`; the rest is reported as overflow.
- Planning starts at `max(work_start, now)`.
- On a non-workday the window is empty and everything overflows, with a "Not a workday" note.
- Lunch is a busy block like a meeting.

*This avoids useless 2-minute slivers and gives the user an honest capacity signal.*

**D-034 — Clock format.** Times are shown in 24h or 12h following `org.gnome.desktop.interface clock-format`, read through `Gio.Settings` in the UI layer. Durations always use the `1h 05m` form, and relative times use "in 12 min" / "12 min ago". Weeks start on Monday (ISO). *The app follows the system setting, as the GNOME HIG asks.*

## Tasks, projects and planning

**D-016 — Recurring tasks.** Completing a task with a recurrence creates the next instance in the same transaction:
- Copied: title, project, ticket key, priority, estimate, tags, notes and checklist (all unchecked).
- Status: To do.
- Due date: the next occurrence after the *previous due date* (or after the completion date if the task had none).
- `recurs_from_id` links the new instance to the old one.

Undoing the completion deletes the new instance. *A notebook-style "do this every Monday" gives one live task at a time instead of a pile of future copies.*

**D-017 — Delete = soft delete + Undo.**
- Delete sets `deleted_at` and shows a toast with Undo, which clears it.
- Rows deleted more than 24 h ago are purged on startup, including their time entries, plan items and checklist.
- Deleting a task that has logged time also asks for confirmation, naming the hours.
- Soft-deleted tasks are hidden everywhere, including reports and timesheets.

*Undo survives a crash, and deletion still really happens.*

**D-018 — Project identity.**
- Project names and key prefixes are unique (case-insensitive). The prefix is letters and digits, starting with a letter, and stored upper-case.
- A project with tasks can only be archived. Archived projects are hidden from the sidebar and pickers but keep their tasks visible in Done and search.
- Ticket keys are *not* unique across tasks, since several tasks may share one ticket. Quick-add and the new-task dialog warn when the key already exists.
- The URL template must contain `{key}`.

*The prefix must map to exactly one project for inference to work.*

**D-019 — Project colours** come from a fixed palette of GNOME named colours (blue, teal, green, yellow, orange, red, pink, purple, slate), each with tuned light and dark variants in `style.css`. A free-form colour picker isn't offered. *Arbitrary colours break contrast in one of the two themes.*

**D-022 — Navigation and list definitions.** *(Superseded by D-051.)*
- **Inbox:** active (not Done, not deleted) tasks with no plan item on today or a later date.
- **Upcoming:** active tasks due or planned within the next 14 days, grouped by date.
- **Plan view:** plans *today* by default. A switcher can plan the next workday.
- **Ctrl+1…7:** Today, Plan, Inbox, Upcoming, Meetings, Reports, Done. Projects are reached with the sidebar or search.
- **Review:** a page (not a sidebar section) reached from a "Review day" button on the Today view and from the "Wrap up" notification.
- **Standup and Timesheet:** pages inside Reports.
- **Sort:** priority, then due date (no due date last), then created time.

*There are exactly 7 fixed sections for the 7 shortcuts. Review is a daily action, not a place to browse.*

**D-037 — Carry-over and review actions.** *(Superseded by D-045.)*
- "Carry-over" = unfinished plan items from the previous *workday*.
- Adding a carry-over to today keeps its original allotment (editable).
- "Move to tomorrow" plans the task on the next workday with its original allotment.
- "Back to inbox" just leaves it unplanned.
- "Apply to all" does the chosen action for every unfinished item in one transaction, with Undo.

*The notebook habit is to copy yesterday's leftovers forward unchanged.*

**D-040 — Blocked reason** *(Superseded by D-046.)* is required (inline validation) when the status is set to Blocked, and is cleared when the status leaves Blocked. *The DB CHECK and the standup "Blockers" section both rely on this.*

## Timer and tracking

**D-023 — Timer persistence.**
- The live timer is the singleton `timer_state` row. A running segment isn't in `time_entry` until it is closed.
- Pause and stop each write one `time_entry` for the closed segment, so pauses are real gaps.
- A heartbeat updates `last_seen_utc` every 60 s and on every change. "Stop at last activity" uses it.
- Manual entries may not end in the future and may not overlap entries or the live segment. A newly started timer therefore can't overlap anything, and the DB trigger is a backstop.
- If the clock moved backwards so that a segment end ≤ its start, the segment is dropped and logged.

*Entries never overlap, every state change is saved, and recovery has one fact to rely on.*

**D-042 — Idle handling.**
- Going idle past the threshold, or locking the screen, while a timer runs records the moment the idle period started (`now − idle_time`).
- On return, the user is asked "You were away 14 min — Keep / Discard that time".
- Discard closes the segment at the idle start and starts a new one at the return time.
- If the Mutter IdleMonitor is unavailable, idle detection is off and Preferences says so. Screen-lock detection works independently.

*Discarding is a split, not an edit, so it never creates overlaps.*

**D-028 — Timesheet.** *(Row labels superseded by D-052; rounding unchanged.)*
- One row per ticket key. Tasks without a key get their own row labelled "Project — Title".
- Each day cell is rounded to the nearest multiple of the rounding setting, but any non-zero cell rounds to at least one increment. Week totals are sums of rounded cells.
- The unrounded total is shown next to the rounded one.
- Rounding 0 = off.

*Small work isn't silently dropped, and the totals match what gets typed into the PMS.*

## Reminders and notifications

**D-024 — At-most-once delivery.** A reminder is logged (`INSERT … ON CONFLICT(key) DO NOTHING RETURNING id`) and committed *before* the notification is sent, and it is sent only when the insert won. If the app crashes between the insert and the send, that one notification is lost, but it then shows up as "fired" in the log. *The spec's hard rule is "never fire the same reminder twice". The window between commit and send is microseconds.*

**D-025 — Allotment and drift rules.** *(Amended by D-050: Rest-of-day items get no allotment reminders.)*
- Allotment warnings, used-up notices and drift nudges apply only to tasks in *today's* plan.
- The warning is skipped when the allotment ≤ the warning threshold.
- "+15 min" adds 15 to `allotted_min`. The allotment is part of the reminder key, so a new warning and used-up notice can fire later.
- "Switch to next" starts the next unfinished plan item.
- "Mark done" completes the task and stops its timer.
- A drift "segment" = the planned item × the free window between busy blocks (meetings or lunch), so a nudge fires at most once per segment.
- Drift **Skip** moves that item to the end of today's plan, so "planned now" becomes the next item.

*Following the spec, these are based on tracked time, not the wall clock. Skip needs a visible effect, or the timeline keeps saying the same task is "now".*

**D-027 — Missed reminders.**
- A reminder counts as missed when its due time is more than 2 min before the moment it is noticed (startup, resume or a clock jump).
- Missed reminders for a meeting that hasn't ended collapse into one live reminder ("Standup in 3 min" or "started 4 min ago", with [Join]).
- Everything else that was missed is listed in one "While you were away" notification, which opens the app.
- The first launch on a new DB has no summary.

*The user still gets the reminder that matters (join the meeting now) without a burst of stale ones.*

**D-029 — Reminders need the app process.**
- No systemd user timer or separate daemon.
- "Run in background on close" (default on) plus "Start at login" keep the process alive.
- If the app was quit, anything missed is summarised on the next start (D-027).
- Notification actions D-Bus-activate the app if it isn't running.

*The spec builds on Gio.Application. A second process would duplicate the scheduler and risk double-firing.*

**D-030 — Sounds.** *(Superseded by D-053.)*
- Sounds play through GSound event IDs: `message-new-instant` for normal reminders, `alarm-clock-elapsed` for allotment-used-up and meeting reminders.
- If GSound or the event is missing, the failure is logged once at debug level and nothing plays.
- The "Sound" setting turns sounds off.
- Sounds are not played while GNOME Do Not Disturb is on. This is read from `org.gnome.desktop.notifications show-banners`, and only for non-urgent reminders.

*Freedesktop sound-theme names are what GNOME ships, and the app shouldn't ring while DND hides the banner.*

**D-038 — Notification actions need an installed app.** Notification actions are routed as `app.*` actions with a target. That only works when the `.desktop` file (`DBusActivatable=true`) and the D-Bus `.service` file named after APP_ID are installed. So the dev workflow is `scripts/dev-install.sh` into `~/.local`, and running straight from the source tree isn't supported. *This is required by GNOME Shell's org.gtk.Notifications handling. CLAUDE.md and the README document it.*

## Integration and CLI

**D-020 — `DAYBOOK_DATA_DIR`.** When set, it holds the DB, the backups and the logs (`<dir>/logs/`), so a throwaway run leaves nothing in the real XDG dirs. If an instance is already running, a launch with `DAYBOOK_DATA_DIR` (or the time variables) prints "Another Daybook instance is running; quit it first to use DAYBOOK_* overrides" and exits 1, instead of silently forwarding. *Forwarding would apply the command to the real database, which is the opposite of what the user wanted.*

**D-021 — Quick-add date and plan semantics.**
- `^mon`…`^sun` means the next such day, **including today** when today matches.
- `^dd/mm` means the next occurrence on or after today, which may fall next year.
- `^dd/mm/yyyy` and `^yyyy-mm-dd` are exact.
- Invalid dates (31/02) are errors.
- `@today` plans the task with an allotment equal to its estimate, or 30 min without one. The preview shows this and it can be edited.

*Matches common to-do apps. The live preview makes the resolved date visible before saving.*

**D-031 — `--status`.**
- With a running instance, the status comes from that instance's live state through the forwarded command line.
- Otherwise the DB is opened read-only (`mode=ro`) and the persisted timer state is printed.
- Output is one line, `PROJ-123 Fix login · 23m left · running`, or `No timer running`. Exit code 0.

*Usable from shell prompts and scripts.*

**D-032 — Tray icon.** It is implemented in Phase 7 as a StatusNotifierItem plus `com.canonical.dbusmenu` over Gio D-Bus, with no GTK 3 libraries. It is skipped silently when no `org.kde.StatusNotifierWatcher` exists, or when the host goes away. *The AppIndicator host is present on this Ubuntu install, so the optional feature is worth doing.*

**D-039 — Undo mechanics.**
- An action that supports Undo (complete, delete, plan changes, review "apply to all") returns an `UndoToken` holding the inverse operation.
- The toast's Undo button runs it in one transaction.
- Tokens live in memory and expire when the toast does. Soft delete (D-017) covers crash safety for deletes.

## Data safety

**D-026 — JSON import replaces all data.**
- The import file is fully parsed and validated in memory first: format version, schema, references, overlaps and constraints. Any error is reported with its path, and nothing is written.
- Then a safety backup is taken, and the data is replaced in a single transaction.
- Merging is out of scope for v1.

*A merge has no well-defined conflict rules. Replace plus a safety backup is predictable and reversible.*

**D-036 — Backups and restore.**
- Backups use `sqlite3.Connection.backup` into `backups/daybook-YYYYMMDD-HHMMSS.db`. Each is verified with `PRAGMA integrity_check`.
- The 14 newest automatic backups are kept. Manual backups count toward the 14.
- Restore:
  1. Validates the file (integrity check, schema version ≤ current).
  2. Confirms with the user.
  3. Takes a safety backup.
  4. Stops the scheduler and timer heartbeat and closes connections.
  5. Copies the file over the DB through the backup API.
  6. Migrates and restarts the services.
- The automatic backup runs at the first scheduler tick of each local day.

## Quality

**D-033 — Search.** Search is case-insensitive `LIKE` over title, ticket key and notes, run in the worker with a 150 ms debounce. Done tasks are included and marked. No FTS5. *5,000 tasks scan in a few milliseconds, and FTS adds triggers and migration cost for no gain.*

**D-035 — Testing scope.**
- Automated tests cover `core` (≥ 90% line coverage, enforced) and `services` (GLib main loop driven with `ManualClock`).
- UI code is checked by the launch check (no warnings), the layering and banned-API tests, and the per-phase manual test list.
- No headless GTK UI tests.

*Automated GTK UI testing on Wayland is fragile, and all logic lives below the UI by design.*

**D-041 — Thread model.**
- The main thread owns the main DB connection.
- One worker thread with its own connection runs slow reads and exports. Results come back via `GLib.idle_add`.
- Bulk writes (import, restore) pause the services and show a progress page.

*This keeps the 100 ms main-loop budget at 5k tasks / 50k entries without locking complexity.*

## Real-world workflow (SPEC Addendum A, 2026-11-03)

**D-043 — Areas are projects without ticket settings (user).**
- Non-project work (docs, demos, internal work) lives in **areas**. An area is a `project` row with no `key_prefix` and no `url_template`. There's no separate entity, and the UI calls the group "Projects & areas".
- Migration 0001 seeds **General** (id 1). It can be renamed but not deleted or archived, and it's the default for every new task, so `task.project_id` is NOT NULL.

*One concept instead of two keeps the app simpler than the PMS, and reports, timesheets and filters group everything the same way.*

**D-044 — "Requested by" for face-to-face tasks.**
- An optional free-text field, with autocomplete from values used before. There are no people records.
- Quick-add sets it with `+name`, where `_` becomes a space.
- It shows as a "from Rahul" chip (with Adw.Avatar initials), in the standup ("for Rahul") and in the review.

*Directly assigned work needs to be remembered and reported, and a contacts system would be PMS-like overhead.*

**D-045 — Automatic carry-over (user).** Unfinished plan items (not Done, not On hold, not deleted) are added to today's plan at the first tick of each working day:
- remaining time = `max(15, ceil_to_15(allotted − tracked))`
- Rest-of-day items stay Rest of day
- `carry_count + 1`, shown as "carried ×N"
- appended after today's existing items, in their original order
- a task already planned today is left alone
- the old plan item stays as history
- it covers weekends, leave and days the app wasn't running, carrying each task once, from its latest planned day
- idempotent through `app_state.last_rollover_date`
- can be turned off with `auto_carry_over`

This replaces D-037's "suggest as candidate" behaviour. *The user asked for unfinished work to continue automatically. Carrying the remaining time, not the original allotment, keeps capacity honest.*

**D-046 — Four statuses and On hold.**
- Statuses: To do / In progress / **On hold** / Done. These replace Blocked and In review.
- On hold needs a reason: `blocked`, `approval` (waiting for approval/review), `someone` (waiting for someone), `postponed` or `other`. It also takes an optional note and an optional follow-up date. `hold_since` records when it started, for "waiting 3d".
- Putting a task on hold, in one transaction with Undo:
  - stops its timer
  - removes its plan items for today and later days
  - withdraws its notifications
- Resuming, or starting its timer after a confirmation, sets In progress. Starting a timer on a To do task sets In progress automatically.
- On or after the follow-up date, the task appears as a "Follow up" candidate in Plan my day and is counted in the start-of-day notification. There's no separate pop-up.
- On-hold tasks are the standup's Blockers.

*The user's postponements have several causes (blockers, approvals, other people). One status with a reason is simpler than a status per cause and still reports well.*

**D-047 — "Move to…" and the moved counter.**
- Move to offers Tomorrow (= next working day), Pick a date (a non-working day is refused with the next working day suggested) and Unplanned.
- Each move of a planned item adds 1 to `task.moved_count`. The chip "moved ×N" appears from N = 2.
- The reason is optional and is stored in the task's notes as a dated line ("3 Nov: moved — waiting for API").
- Automatic carry-over doesn't count as a move; it has its own counter.

*This makes repeated postponement visible without nagging, and needs no extra tables.*

**D-048 — Time off.**
- `time_off(day, part)` with `full`, `am` or `pm`, plus an optional note. Holidays are entered the same way.
- Half-day leave cuts the working window at lunch: `am` off → work starts after lunch; `pm` off → work ends at lunch start. Without a lunch break, the cut is at the midpoint.
- Leave days aren't working days anywhere. `core/calendar.py` is the only code that decides this.
- Meeting reminders on leave are off by default (`meeting_reminders_on_leave`).
- Marking a full day moves that day's plan to the next working day, with Undo, and lists the tasks due during the leave.
- There's no import from GNOME Calendar in v1.

*The user takes leave; plans, reminders, carry-over and the standup must all agree on which days count.*

**D-049 — Ad-hoc ("Meeting now") meetings.**
- An ad-hoc meeting is a one-off `meeting` with `is_adhoc = 1`.
- **Unknown length:** `open_ended = 1`. The provisional length is `adhoc_default_min` (30), and the planner treats the end as `max(start + length, now + 1 min)` until the meeting is ended.
- **Started up to 30 min ago:**
  - Timer time after the real start is removed, after a preview ("removes 12 min from PROJ-123").
  - Manual entries are never trimmed. If one overlaps, the start is refused with an inline message.
- **Ending** stores the real length. An open meeting at end-of-day or on restart raises the Stay-tier "Still in the meeting?" reminder.
- **Not a timesheet row:** ad-hoc meetings don't appear in the timesheet. They count as meeting time in reports, shown separately.

*Emergency meetings are the main thing that breaks a plan. Recording them in seconds keeps both the plan and the tracked time truthful.*

**D-050 — "Rest of day" plan items.**
- `day_plan_item.allotted_min IS NULL` marks a Rest-of-day item.
- The planner gives Rest-of-day items the free time left after timed items, split equally (leftover minutes go to the earliest item). A Rest-of-day item never overflows; at worst it gets 0 minutes.
- No allotment warning or "time is up" reminder. Drift nudges and end-of-day still apply.
- The timer shows elapsed time and the free time left in the day.
- Rest-of-day items carry over as Rest of day until Done.
- Capacity reads "Planned 3h 30m + rest of day / free 5h 45m".

*This covers whole-day and multi-day work with no known end, without inventing a fake allotment.*

**D-051 — Navigation and list definitions** (replaces D-022).
- **Inbox:** tasks that are To do or In progress, not deleted, with no plan item today or later.
- **Upcoming:** active tasks due or planned within the next 14 days, grouped by date, plus an **On hold** group sorted by follow-up date.
- **Ctrl+1…7:** Today, Plan, Inbox, Upcoming, Meetings, Reports, Done.
- **Sidebar:** "Projects & areas" lists active projects and areas, with General last.
- **Review:** a page reached from "Review day" on Today and from the "Wrap up" notification.
- **Standup and Timesheet:** pages inside Reports.
- **Plan view:** Today/Tomorrow toggle ("Tomorrow" = next working day).
- **Sort:** priority, then due date (none last), then created time.
- **Filters:** project/area, status, priority, tag, requested by.

**D-052 — Timesheet rows for non-ticket work** (replaces D-028's row labels; rounding is unchanged). Tasks without a ticket key get a row labelled "Area — Title" (for example "Docs — API guide", or "General — Laptop setup"). Ad-hoc and scheduled meetings are not timesheet rows. *The PMS timesheet is per ticket. Everything else still has to be visible so it can be booked against an internal or overhead code.*

**D-053 — Reminder tiers (user).**

| Tier | Kinds | Priority | Sound |
|---|---|---|---|
| Stay | meeting offsets and snoozes, allotment used up, open ad-hoc meeting, test | URGENT | `alarm-clock-elapsed` |
| Act | drift, back-to-task after a meeting, plan overflow | HIGH | `message-new-instant` |
| Info | 10-min warning (silent), start of day, end of day, missed summary | NORMAL | `message-new-instant` |

- Each reminder kind declares its tier in `core/reminders.py`, and an unknown kind fails a test.
- The Sound setting turns all sounds off. Non-urgent sounds aren't played while GNOME Do Not Disturb hides banners (`org.gnome.desktop.notifications show-banners = false`). This replaces D-030.

*Why:* on Wayland, an app can't raise or keep its own window on top, so the GNOME banner is the only reliable pop-up. GNOME Shell turns `URGENT` notifications from apps into critical-urgency banners, which don't time out. Keeping that tier to what really needs action keeps it from becoming noise. **To be verified on this machine in Phase 4.** If a tier doesn't behave as described, the observed behaviour and the fallback go into a new entry.

**D-054 — One notification per subject.**
- Notifications are sent with stable ids: `meeting:{mid}:{occurrence}`, `task:{task_id}`, `day:{date}`, `summary`. A newer reminder replaces the older banner instead of stacking.
- `withdraw_notification` runs as soon as a reminder is moot:
  - a meeting is joined or has ended
  - a task is done, on hold or moved
  - a timer starts on the nudged task
  - the allotment is extended
  - the plan is reviewed after an overflow
- At most 3 buttons per notification (GNOME shows only 3), plus a default click action that opens the relevant view.
- Titles start with the subject and stay ≤ 40 characters.

*This is the difference between a reminder that helps and a pile of stale banners.*

**D-055 — The Focus window is the in-app alert surface.** When open, the Focus window shows any live reminder as an accent-coloured card with the same buttons, until the reminder is handled or withdrawn. It gets one short pulse animation and no blinking. When the user pins it with GNOME's "Always on Top", it gives an always-visible pop-up within Wayland's rules. *It's the closest reliable thing to a forced pop-up window, without hacks.*

**D-056 — Overflow reminder.** After a reflow (ad-hoc meeting, meeting end, plan edit, a timer running long), if the set of non-fitting plan items is non-empty and differs from the last set reported today, one Act-tier reminder fires: "2 tasks no longer fit today (1h 30m) [Move to tomorrow] [Review plan]". The key includes a hash of the item ids, so the same set is never reported twice. *The user should hear about a broken plan once, when it breaks, not every minute.*

**D-057 — UI design rules.**
- PLAN §6 is the reference for principles, visual language, screens and breakpoints (360 / 600 / 900 px).
- The minimal task form has Title (the only required field), Area/Project, Priority, and Today + time. Everything else goes under a collapsed "More details".
- The same `task_row` is used in every list.
- libadwaita named colours and the system accent only, with no custom theme.
- Animations last 250 ms or less and respect `gtk-enable-animations`.

*The user asked for a modern UI that's simpler than their PMS. Consistency and few fields do more for that than decoration.*

## Phase 0 (2026-11-03)

**D-058 — Code is installed to `<prefix>/share/<APP_ID>/`, not `<prefix>/share/daybook/`.** With the `~/.local` prefix, `share/daybook` *is* the user's data directory (`~/.local/share/daybook/`, required by the SPEC), so code and database would have shared one folder. The package and the app-private icons now go to `<prefix>/share/io.github.daybook.Daybook/`, and `dev-uninstall.sh` removes only that folder. *Backup, restore and "delete data" must never be able to touch the code, and uninstalling must never touch the data.*

**D-059 — License, homepage and git identity (user).**
- License: GPL-3.0-or-later (metainfo, About dialog, and the .deb copyright file in Phase 8).
- Homepage, VCS and bug tracker URLs: `https://github.com/GautamSavasaviya/daybook`. The repository doesn't exist yet; the user creates it.
- Commits use a **repository-local** identity, `GautamSavasaviya <gautam4tech@gmail.com>`. The global git config is untouched. The initial docs commit was re-authored to it before anything was pushed.

**D-060 — Migration tests build each earlier version from the earlier migrations.** "Migrations from every earlier schema version" is tested by applying `MIGRATIONS[:n]` for every `n`, then migrating to the latest and checking integrity and foreign keys. Released migrations are immutable, so this reproduces each old schema exactly. SQL snapshot fixtures (PLAN §3) are added only if a migration ever needs to transform real rows, and then they carry sample data.

**D-061 — Settings are saved as a whole object.** `SettingsService.save(new_settings)` takes a complete `Settings` (built with `dataclasses.replace`), validates it, writes every key in one transaction and publishes `Changed(SETTINGS)`. *This keeps the API fully typed under `mypy --strict`; partial keyword updates can't be typed without duplicating every field.*

**D-062 — Second launches with dev overrides are refused before registering.** `main()` asks the session bus (`NameHasOwner`) whether Daybook is running *before* creating the application, so a refused launch (D-020) exits with its message and no GLib warning.

**D-063 — Ruff rule FBT003 is not enabled.** It flags every boolean positional argument, which is how most GTK setters work (`set_visible(True)`, `add_setter(obj, "collapsed", True)`). All other selected rule sets stay on, including the full annotation rules (`ANN`).

**D-064 — Branch naming: GitHub Flow with typed prefixes (user).** Replaces the `phase-N` names from D-002.
- `main` is always stable and changes only through pull requests. The user reviews and merges.
- Work branches are `<type>/<short-kebab-description>`:
  - `feature/…`: a phase or feature
  - `fix/…`: a bug fix
  - `docs/…`: documentation only
  - `chore/…`: tooling, build, dependencies
- Branch names for the phases:

  | Phase | Branch |
  |---|---|
  | 0 | `feature/scaffold` |
  | 1 | `feature/projects-areas-tasks` |
  | 2 | `feature/calendar-meetings-planning` |
  | 3 | `feature/timer-time-tracking` |
  | 4 | `feature/reminder-engine` |
  | 5 | `feature/fast-input` |
  | 6 | `feature/review-reporting` |
  | 7 | `feature/background-data-safety` |
  | 8 | `feature/hardening-packaging` |

- A new branch starts from `main` if the previous phase is merged. Otherwise it starts from the previous phase's branch, so the work stacks.
- Each finished phase is pushed and gets a pull request into `main`. When Phase 8 is merged, `main` is tagged `v0.1.0`.
- Commit messages: `<Area>: <summary>` (for example `Phase 1: projects, areas and tasks`), ending with the attribution trailer.

*These are conventional, self-describing names. GitHub sorts and filters by prefix, and the names say what a branch holds without a lookup table.*
