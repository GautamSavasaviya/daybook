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

**D-020 — `DAYBOOK_DATA_DIR`.** *(Instance handling superseded by D-065.)* When set, it holds the DB, the backups and the logs (`<dir>/logs/`), so a throwaway run leaves nothing in the real XDG dirs. If an instance is already running, a launch with `DAYBOOK_DATA_DIR` (or the time variables) prints "Another Daybook instance is running; quit it first to use DAYBOOK_* overrides" and exits 1, instead of silently forwarding. *Forwarding would apply the command to the real database, which is the opposite of what the user wanted.*

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

**D-062 — Second launches with dev overrides are refused before registering.** *(Superseded by D-065.)* `main()` asks the session bus (`NameHasOwner`) whether Daybook is running *before* creating the application, so a refused launch (D-020) exits with its message and no GLib warning.

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

## Code-review fixes (2026-11-03)

**D-065 — Test runs use their own application ID (user: "fix all").** Replaces the instance rules of D-020 and D-062.
- **Separate ID:** when any `DAYBOOK_*` dev variable is set, the app registers as `io.github.daybook.Daybook.Devel` (`DEVEL_APP_ID`, derived from APP_ID in `const.py`). A test run and the real app can run side by side and never receive each other's launches, `--quick-add` commands or notification clicks.
- **Fake time needs a throwaway folder:** `DAYBOOK_TIME_SCALE` and `DAYBOOK_FAKE_NOW` are refused (exit 2, with an example command) unless `DAYBOOK_DATA_DIR` is also set. Fake timestamps never reach the real database.
- **One test instance at a time:** a second test instance is refused (exit 1), since it would otherwise be forwarded to the first, which may use a different data folder.
- **Visible marking:** a test window is titled "Daybook (Devel)" and uses libadwaita's striped `devel` style.
- **Notifications in test runs:** the Meson option `devel_entry` (on in `dev-install.sh`, off for the `.deb`) installs a hidden `…Daybook.Devel.desktop` entry, because GNOME Shell shows notifications only for apps with a desktop entry. It is deliberately not D-Bus-activatable, so a notification click can never start a test instance without its variables. Notification actions reaching a *running* test instance get verified in Phase 4.

*Before this, a test instance owned the real app ID: opening Daybook from the app grid while it ran would have opened the throwaway, fake-time copy, and real work typed there would have been lost.*

**D-066 — Transactions recover from a failed COMMIT.** If COMMIT fails (disk full, deferred constraint, I/O error), `transaction()` rolls back, resets its nesting counter and re-raises, so the connection stays usable for the next write. A rollback runs only if SQLite still has a transaction open, and a failing rollback is logged without hiding the original error.

**D-067 — Damaged settings are repaired one problem at a time.** Each invalid field (or its group, such as the four work-hour fields) is reset to its default, and validation repeats until the settings are valid. Valid settings are always kept. Only if a group's defaults still fail does everything fall back to defaults.

**D-068 — Toasts are plain text, and a failing toast is only logged.** Toast titles never go through Pango markup, because task titles may contain `&` or `<`. The toast handler is deliberately not wrapped in `safe_callback`: a failure there is logged by the event bus and never reported back as another toast, which would loop.

## Phase 1 — Projects, areas and tasks (2026-11-03)

**D-069 — "Tomorrow" and "Next working day" are one option in Move to….** SPEC A4 lists both; D-047 says Tomorrow means the next working day. The dialog therefore shows exactly one of them, with its date: **Tomorrow** when tomorrow is a working day, otherwise **Next working day** (on a Friday: "Next working day · Mon 9 Nov"). Both resolve through `WorkCalendar.next_working_day`, so leave days are skipped too. *Two buttons that always lead to the same date would only make the user wonder what the difference is.*

**D-070 — Archiving and deleting projects.**
- Archiving hides the project **and its open tasks** from the sidebar, the pickers, Inbox, Upcoming and the badge counts. Its tasks stay in Done and in search, and a task already in an archived project can still be edited (it just can't be moved *into* one). Archiving shows a toast with Undo; "Archived Projects" in the sidebar restores it later.
- A project with no tasks can be deleted, after a confirmation (no Undo, since nothing is lost but the name). Tasks already in the trash (D-017) are purged with it. A project with tasks can only be archived (D-018). General can be renamed and recoloured only.

*Open tasks of a project the user archived are exactly what they asked not to see; keeping them in Done and search means nothing is lost.*

**D-071 — Upcoming starts with an "Overdue" group.** D-051 lists tasks due or planned in the next 14 days, plus On hold. Overdue open tasks are shown first, in their own group, because they are the most urgent and would otherwise appear in no dated list at all. The Upcoming badge counts the overdue and dated groups, not On hold.

**D-072 — "Plan for today" in the new-task dialog.** The switch plans the task for today with a preset (15m / 30m / 1h / 2h / Rest of day, default 30m) or a typed time. On a non-working day it reads "Plan for Mon" and plans the next working day, matching the rule that nothing is planned on a non-working day (D-047).

**D-073 — Search details** (adds to D-033). Every word typed must appear in the title, ticket key or notes (in any of them, in any order). Matching is Unicode-aware: a `fold()` SQL function using Python's `casefold()` is registered on every connection, because SQLite's `LIKE` only folds ASCII. Results come in two groups, Open and Done. Archived projects' tasks are included.

**D-074 — How lists and the detail panel behave.**
- A click or Enter on a row opens the detail panel; the round check on each row completes the task (toast with Undo) or reopens it.
- Detail fields save on their own: text rows on Enter or their apply button, drop-downs and dates immediately, notes 0.8 s after typing stops and when leaving the field. Changes from elsewhere never overwrite a field being typed in. Escape closes the panel.
- Choosing "On hold" in the status drop-down opens the hold dialog; cancelling puts the drop-down back.
- Deleting a task asks for confirmation only when it has tracked time (D-017); otherwise the toast's Undo is enough.

**D-075 — The sidebar is `Adw.Sidebar` (libadwaita 1.9).** Sections: the seven fixed sections; "Projects & areas" with a colour dot and an open-task badge per project (General last); then "New Project or Area" and, when there are any, "Archived Projects". Right-clicking a project offers Edit and Archive; the project page's menu offers the same for keyboard users. Inbox and Upcoming have badges; Today's badge arrives with the Today view in Phase 2.

**D-076 — Gate and tooling details.**
- The task status member is `TaskStatus.TO_DO` (stored value `'todo'`, unchanged) because the gate's marker check in `check.sh` rejects the word TODO anywhere in code.
- Ruff allows the `×` character (`allowed-confusables`), which the chips use ("moved ×3").
- pytest ignores one warning, "GLib.unix_signal_add_full is deprecated", which PyGObject 3.56's own GLib override raises on import. It is not from Daybook code, and the app never prints it.

**D-077 — Layout sizes.** The detail panel is 340–420 sp wide beside the list at ≥ 900 sp, slides over the list below that, and covers the whole window below 600 sp (where the sidebar becomes its own page). The panel sits in a scroller that reports a minimum width of 0, because while the window narrows past a breakpoint libadwaita measures the hidden panel at width 0, which otherwise prints a GTK warning.

## Code-review fixes on Phase 1 (2026-11-03)

**D-078 — Undo never overwrites a later edit (user: "fix all").** Amends D-039. Each Undo token holds the task's state before *and* right after the action. If the task row or its plan from today on differs from the "after" state when Undo is clicked, nothing is written and the toast says "Couldn't undo: “…” was changed since". *Restoring the whole row would silently throw away notes or a title typed after completing the task.*

**D-079 — Resume and reopen have Undo.** Leaving On hold (Resume, or To do / In progress from the status drop-down) drops the hold reason, note and follow-up date; reopening a Done task drops its completion time. Both now show a toast with Undo. Switching between To do and In progress loses nothing and shows no toast.

**D-080 — The non-working-day rule lives in the service.** Amends D-072. `TaskService.create` refuses a plan on a past or non-working day (`reschedule.check_plan_day`, the same check Move to… uses), and `TaskService.default_plan_day()` gives the day "Plan for today" uses. The dialog only displays it, so quick-add and the Plan view in later phases get the same rule.

## Phase 2 — Calendar, meetings and planning (2026-11-03)

**D-081 — When carry-over runs.** At startup and whenever the local date changes, and only on a working day (on a weekend or a day off it waits for the next working day). It carries items dated after the last rollover (`app_state.last_rollover_date`) and before today. On the very first run there is no watermark, so it looks back 31 days. With `auto_carry_over` off, the watermark still advances, so turning it back on later doesn't bring old days back.

**D-082 — "Meeting now" details** (adds to D-049).
- "Unknown" length opens the meeting with `adhoc_default_min` (30) as its provisional length. The planner treats its end as `max(start + 30 min, now + 1 min)`.
- Ending stores the real length rounded **down** to whole minutes (at least 1), so the meeting is over the moment it is ended. Ending has Undo.
- "Started … ago" moves the start back; removing timer time after the real start arrives with the timer in Phase 3.

**D-083 — The Plan view's narrow layout uses an overlay side panel, not a bottom sheet.** PLAN §6.3 suggested an Adw.BottomSheet below 600 px. Below 640 sp the suggestions slide in from the side ("Add Tasks"), using the same Adw.OverlaySplitView as the wide layout. *One widget at all widths keeps keyboard focus order and state identical, and the panel is easier to reach with the keyboard than a sheet.* The suggestions also have a "Find any open task" search, so any task can be planned, not only the suggested ones.

**D-084 — Today and Plan before the timer exists.**
- "Start Day" (Plan) goes to Today. From Phase 3 it also starts the timer on the first item.
- The Now card shows what is planned now, the time left in today's allotment, what's next and the next meeting, with Mark done / Open task / Meeting now. Start and Pause arrive with the timer in Phase 3; there is no dead button meanwhile.
- The timeline shows meetings, lunch and time off for the whole day, and task segments from now on (the schedule starts at `max(work start, now)`, PLAN §5.1). Tracked time joins the timeline in Phase 3.

**D-085 — Time off details** (adds to D-048).
- A range of full days skips non-workdays inside it, and is limited to 60 days at a time. A half day is a single day.
- Marking leave moves the plans of open tasks on full days off to the next working day, each counting as a move, with Undo. The dialog lists tasks that fall due while away.
- Removing a day off has Undo but doesn't move plans back; they stay where they went.
- Time off is managed from the main menu → "Time Off…" until Preferences arrives in Phase 7.

**D-086 — Single-occurrence changes store only what differs** (refines D-013). Editing one occurrence stores only the fields that differ from the series, so a later series edit (say, a new link) still reaches occurrences that were only moved. Editing a recurring meeting from Today or Meetings first asks "This occurrence" or "All occurrences".

**D-087 — A meeting's zone that can't be loaded falls back to the local zone.** Meetings recur in their own `tzid` (D-010). If that name can't be loaded (an unnamed system zone, or a zone removed from tzdata), occurrences are computed in the current local zone instead of UTC, so the wall-clock time stays right.

**D-088 — Recurring tasks in the UI** (D-016). "Repeats" offers Doesn't repeat / Daily / Weekdays / Weekly / Monthly, under More details in the new-task dialog and the detail panel. Weekly repeats on the due date's weekday (today's, without a due date), monthly on its day of the month. Completing creates the next instance; the toast names its due date, and Undo removes it unless it was changed since (D-078).

**D-089 — Planning rules in the Plan view.**
- Adding a follow-up that is on hold resumes it (In progress, D-046); a Done task can't be added.
- Done and on-hold items stay listed in the day's plan (dimmed) but take no time in the schedule.
- "Move to Next Day" on the "doesn't fit" divider and on Today moves exactly the overflowing items, counts each as a move, and has Undo.
- The Today badge counts open tasks in today's plan.
