# Daybook — Implementation Plan

Source of truth for requirements: [`SPEC.md`](SPEC.md), including **Addendum A**, which wins where it differs from the main text. Defaults chosen where the spec is silent: [`DECISIONS.md`](DECISIONS.md). Phase status: `PROGRESS.md` (created at the end of Phase 0).

---

## 1. Target platform

Checked on the development machine on 2026-11-03, before any code was written:

| Component | Version | Notes |
|---|---|---|
| OS / desktop | Ubuntu 26.04.1 LTS, GNOME Shell 50.1, Wayland | No X11 code paths. |
| Python | 3.14.4 (system) | |
| PyGObject | 3.56.2 | |
| pycairo | 1.27.0 (`python3-gi-cairo`) | Used for charts, the timeline and the progress ring. |
| GTK | 4.22.4 | Minimum target: 4.22. |
| libadwaita | 1.9.1 | Minimum target: 1.9. |
| GLib / Gio | 2.88.0 | |
| SQLite | 3.46.1 | Supports `RETURNING`, `STRICT`, JSON functions, WAL. |
| python3-dateutil | 2.9.0 | |
| GSound | `gir1.2-gsound-1.0` 1.0.3 | Optional at runtime. |
| meson / ninja | 1.10.1 / 1.13.2 | |
| desktop-file-validate / appstreamcli / lintian | 0.28 / 1.1.2 / 2.129 | |
| D-Bus services present | `org.gnome.Mutter.IdleMonitor`, StatusNotifierWatcher (AppIndicator extension) | Both still treated as optional at runtime. |

Not installed and **not required**: `glib-compile-resources`, `blueprint-compiler`, `debhelper`, `dh-python` (see D-004, D-006).

### Widgets we use (all current in GTK 4.22 / Adw 1.9)
Adw.ApplicationWindow, Adw.NavigationSplitView, Adw.OverlaySplitView (task detail side panel), Adw.NavigationView, Adw.Breakpoint, Adw.Clamp, Adw.Avatar (initials for "Requested by"), Adw.TimedAnimation/Adw.SpringAnimation (≤ 250 ms, skipped when `gtk-enable-animations` is off), Adw.ToolbarView, Adw.HeaderBar, Adw.ToastOverlay, Adw.Banner, Adw.StatusPage, Adw.Dialog, Adw.AlertDialog, Adw.PreferencesDialog, Adw.ShortcutsDialog, Adw.ActionRow/EntryRow/SpinRow/SwitchRow/ComboRow/ExpanderRow, Adw.ButtonRow, Adw.ToggleGroup, Adw.WrapBox, Adw.Spinner, Adw.BottomSheet, Adw.StyleManager; Gtk.ListView, Gtk.ColumnView, Gtk.DropDown, Gtk.SearchEntry, Gtk.DrawingArea, Gtk.DragSource/DropTarget, Gtk.FileDialog, Gtk.UriLauncher, Gtk.ColorDialogButton, Gtk.ShortcutController.

### Banned APIs (deprecated in GTK ≥ 4.10 or replaced by libadwaita)
GtkTreeView, GtkTreeModel/ListStore/TreeStore, GtkCellRenderer*, GtkComboBox(Text), GtkDialog, GtkMessageDialog, GtkInfoBar, GtkStatusbar, GtkAssistant, GtkEntryCompletion, GtkFileChooser*/FileChooserDialog/Native, GtkColorChooser*/ColorButton, GtkFontChooser*, GtkAppChooser*, GtkLockButton, GtkVolumeButton, GtkShortcutsWindow, `Gtk.show_uri`, per-widget `GtkStyleContext` (use CSS classes; `Gtk.StyleContext.add_provider_for_display` is the one allowed exception), `Adw.MessageDialog`, `Adw.PreferencesWindow`, `Adw.Leaflet`, `Adw.Flap`, `Adw.Squeezer`, `Adw.ViewSwitcherTitle`. A test greps `daybook/` for these names.

---

## 2. Architecture

```
            ┌────────────────────────────────────────────────────────┐
            │ daybook/ui      GTK 4 + libadwaita widgets only        │
            │  application · window · views · dialogs · widgets      │
            └───────────────▲───────────────────────┬────────────────┘
               Changed(...) │ events                │ calls
            ┌───────────────┴───────────────────────▼────────────────┐
            │ daybook/services   GLib/Gio glue, no Gtk/Adw imports   │
            │  AppContext · EventBus · TaskService · PlanService ·   │
            │  TimerService · ReminderScheduler · Notifier · Power · │
            │  Idle · BackupService · Autostart · Tray · Worker      │
            └───────────────────────────────┬────────────────────────┘
                                            │ calls (pure functions, repos)
            ┌───────────────────────────────▼────────────────────────┐
            │ daybook/core       pure Python, no `gi` imports        │
            │  models · db · migrations · repositories · planner ·   │
            │  recurrence · reminders · timer · quickadd · reports … │
            └───────────────────────────────┬────────────────────────┘
                                            ▼
                     SQLite (WAL, foreign_keys=ON)  ~/.local/share/daybook/daybook.db
```

### Layer rules (enforced by `tests/test_layering.py`, which parses imports with `ast`)
- `daybook/core` imports only the stdlib and `dateutil`. It never imports `gi`.
- `daybook/services` may import `gi.repository.GLib`, `Gio` and `GObject` (and `GSound` lazily). It never imports `Gtk`, `Gdk` or `Adw`.
- `daybook/ui` never imports `sqlite3` or `daybook.core.db`/repositories, and never runs SQL. It talks only to services, and may use core *models* and *formatting helpers*.

### Data flow and change propagation
- The DB is the single source of truth. UI widgets hold no authoritative state. They re-query through services when they get an event.
- `services/events.py` is a small typed pub/sub (`EventBus`). Every service mutation:
  1. runs one repository call inside one transaction,
  2. after commit, emits `Changed(entity: Entity, ids: frozenset[int])` (entities: project, task, meeting, plan, time_entry, time_off, timer, settings, reminder). It also emits domain events such as `TimerChanged`, `ReminderFired` and `Toast(message, undo)`.
- Events are always dispatched on the GLib main loop. A worker-thread publisher marshals through `GLib.idle_add`.
- Each view subscribes in `__init__` and unsubscribes on `unrealize`/destroy. Views coalesce bursts with a 0-ms idle refresh.
- The ReminderScheduler subscribes to every `Changed` and recomputes.

### Threading and the 100 ms budget
- Small reads and writes (single row, one day's plan, list of ≤ a few hundred rows) run on the main thread through the main connection.
- Reports, timesheet ranges, exports/imports, backups/restores, the 5k-task list's initial load, and search run through `services/worker.py`. It runs a callable on a `ThreadPoolExecutor(max_workers=1)` with its **own** read connection (WAL allows concurrent readers), and delivers the result or exception with `GLib.idle_add`. Writes from workers (import, restore) take an exclusive path: the UI shows a blocking `Adw.Spinner` page and the main connection is closed and reopened afterwards.
- Lists use `Gio.ListStore` + `Gtk.ListView` factories, so 5,000 rows render lazily.

### Application lifecycle
- `Adw.Application(application_id=APP_ID, flags=HANDLES_COMMAND_LINE)`. A second launch forwards its command line to the primary instance. `--status` output is printed in the calling terminal through `Gio.ApplicationCommandLine.print_literal`.
- Startup order:
  1. Parse env.
  2. Resolve paths.
  3. Set up logging and the exception hook.
  4. Open the DB and migrate. If this fails, show an error window and keep the DB untouched.
  5. Build AppContext/services.
  6. Run timer recovery.
  7. Arm the scheduler.
  8. Run the missed-reminder summary.
  9. Show the window, unless `--background`.
- `--background` and "close keeps running" both use `Gio.Application.hold()`. "Quit" releases the hold, after confirming when a timer runs.
- Shutdown (`shutdown` signal, SIGTERM via `GLib.unix_signal_add`, logind `PrepareForShutdown`):
  1. Flush the timer heartbeat.
  2. Persist the `last_check_utc` watermark.
  3. `PRAGMA optimize`.
  4. Close connections.

### Cross-cutting infrastructure
- **Paths (XDG):**
  - data `~/.local/share/daybook/` (DB at `daybook.db`, backups at `backups/`)
  - logs `~/.local/state/daybook/daybook.log`, `RotatingFileHandler(1 MB, backupCount=4)` = 5 files
  - autostart `~/.config/autostart/io.github.daybook.Daybook.desktop`
  - `DAYBOOK_DATA_DIR` overrides data and logs (D-020).
- **Exception hook:**
  - `sys.excepthook` and `threading.excepthook` log the full traceback.
  - On the main loop, the hook emits `Toast("Something went wrong — details in log")` with a "Show log" action that opens the log folder via `Gtk.FileLauncher`.
  - Every GLib callback and action handler is wrapped by `safe_callback()`, so an exception never escapes into PyGObject's own printing.
  - DB errors roll back the transaction. No partial writes.
- **Clock** (`core/clock.py`):
  - `Clock` Protocol: `now() -> datetime` (aware UTC), `local_tz() -> tzinfo`, `monotonic() -> float`, `real_seconds_until(when: datetime) -> float`.
  - Implementations:
    - `SystemClock`.
    - `ScaledClock(scale, fake_start)` from `DAYBOOK_TIME_SCALE` and `DAYBOOK_FAKE_NOW`. Fake time = `fake_start + (monotonic − t0) × scale`.
    - `ManualClock` for tests.
  - `clock_from_env(environ)` returns `SystemClock` unless a variable is set. Invalid values are logged and ignored.
  - Services arm GLib timeouts with `clock.real_seconds_until(due)`.
- **Time utilities** (`core/timeutil.py`):
  - `to_db(dt)` / `from_db(s)` with the fixed format `YYYY-MM-DDTHH:MM:SSZ`.
  - `local_day_bounds(date, tz) -> (utc_start, utc_end)`. DST-safe: midnight resolved with `zoneinfo` and fold handling.
  - `fmt_duration(min) -> "1h 05m"` and `fmt_relative(delta) -> "in 12 min"`.
  - `fmt_clock(dt, use_24h)`.
- **Settings** (`core/settings.py`): a frozen `Settings` dataclass with defaults, stored as one row per key in `settings` (JSON values). Unknown keys are ignored and missing keys fall back to defaults, so migrations never need to backfill settings.

---

## 3. Module list

```
daybook/
├── __init__.py
├── const.py                # APP_ID = "io.github.daybook.Daybook", APP_NAME, VERSION — the single definition
├── main.py                 # entry point: env → paths → logging → excepthook → DaybookApplication.run()
├── paths.py                # XDG resolution incl. DAYBOOK_DATA_DIR
├── logsetup.py             # rotating file handler, --debug console handler, excepthooks
├── core/
│   ├── clock.py            # Clock protocol, SystemClock, ScaledClock, ManualClock, clock_from_env
│   ├── timeutil.py         # UTC ISO (de)serialisation, local-day bounds, duration/relative formatting
│   ├── errors.py           # DaybookError, ValidationError(field, msg), OverlapError, NotFound, ImportValidationError
│   ├── models.py           # frozen dataclasses: Project, Task, ChecklistItem, Meeting, MeetingException,
│   │                       #   Occurrence, PlanItem, TimeEntry, TimerState, ReminderRecord, Recurrence, enums
│   ├── db.py               # connect(path) (WAL, foreign_keys, busy_timeout), transaction() ctx manager
│   ├── migrations/
│   │   ├── __init__.py     # MIGRATIONS registry, migrate(conn) — runs each step in its own transaction
│   │   └── m0001_initial.py
│   ├── repo/
│   │   ├── projects.py  tasks.py  meetings.py  plan.py  time_entries.py  time_off.py
│   │   ├── timer.py  reminders.py  settings.py  app_state.py
│   ├── settings.py         # Settings dataclass + defaults + (de)serialisation
│   ├── validation.py       # field validators shared by repos and forms (title, ticket key, URL template…)
│   ├── calendar.py         # WorkCalendar: is_working(day), next/prev_working_day, day_window(day) —
│   │                       #   the ONLY place that knows workdays, half-day and full-day time off
│   ├── recurrence.py       # Recurrence rule (de)serialise, expand(rule, dtstart, tz, window, exceptions)
│   ├── planner.py          # schedule(...) -> Schedule; capacity(); candidates()
│   ├── carryover.py        # rollover(...) -> new plan items for the next working day (pure)
│   ├── hold.py             # On-hold rules, follow-ups due, days waiting
│   ├── reminders.py        # pure reminder computation: due list, next due, missed summary
│   ├── timer.py            # timer state machine transitions + recovery options (pure)
│   ├── tracking.py         # tracked minutes per task/day (clipped to local day), overlap checks
│   ├── quickadd.py         # parse(line, context) -> ParsedTask (+ warnings)
│   ├── review.py           # end-of-day review data, carry-over operations
│   ├── standup.py          # Yesterday / Today / Blockers → text / Markdown
│   ├── timesheet.py        # day/week rows per ticket, rounding, CSV/text
│   ├── reports.py          # weekly aggregates
│   ├── backup.py           # sqlite3 backup API, rotation (keep 14), verify, restore
│   ├── exchange.py         # JSON export/import (versioned, validated), time-entry CSV export
│   └── sources.py          # TaskSource Protocol (PMS plugins, not implemented in v1)
├── services/
│   ├── events.py           # EventBus, event dataclasses
│   ├── context.py          # AppContext: wires clock, db, bus, all services; lifecycle
│   ├── worker.py           # run_in_worker(fn, on_done, on_error)
│   ├── safe.py             # safe_callback decorator → log + Toast event
│   ├── projects.py  tasks.py  meetings.py  plan.py  time_entries.py  time_off.py  settings.py
│   │                       # thin transactional services: validate → repo → emit Changed (+ undo tokens)
│   ├── rollover.py         # runs core.carryover once per working day (first tick / startup)
│   ├── timer_service.py    # start/pause/stop/switch, heartbeat, persistence, recovery prompt data
│   ├── scheduler.py        # ReminderScheduler: one GLib timeout + 30 s tick + resume + tz refresh
│   ├── notifier.py         # Gio.Notification, action routing, GSound, in-app banner event
│   ├── power.py            # logind PrepareForSleep / PrepareForShutdown (system bus)
│   ├── idle.py             # Mutter IdleMonitor + org.gnome.ScreenSaver (session bus), graceful fallback
│   ├── backup_service.py   # daily auto-backup, backup now, restore orchestration
│   ├── autostart.py        # write/remove ~/.config/autostart/<APP_ID>.desktop
│   ├── tray.py             # StatusNotifierItem + com.canonical.dbusmenu over Gio D-Bus (optional)
│   └── status.py           # one-line status for --status (live instance or read-only DB)
└── ui/
    ├── application.py      # DaybookApplication(Adw.Application): actions, accels, CLI, hold/release
    ├── window.py           # main window: ToastOverlay, NavigationSplitView, breakpoints, banner
    ├── sidebar.py          # sections with badge counts, project entries
    ├── format.py           # thin UI helpers over core.timeutil (12/24h from GNOME setting)
    ├── views/
    │   ├── task_list.py    # shared list (Inbox, Upcoming, Project, Done, search results), filters
    │   ├── today.py  plan.py  meetings.py  reports.py  review.py  timesheet.py  standup.py
    ├── task_detail.py      # inline editing of all task fields + checklist + time summary
    ├── dialogs/
    │   ├── quick_add.py  task_new.py  meeting_edit.py  occurrence_scope.py  time_entry_edit.py
    │   ├── meeting_now.py  reschedule.py  hold.py  time_off.py
    │   ├── timer_recovery.py  idle_return.py  preferences.py  onboarding.py  restore.py
    ├── focus_window.py     # compact always-on-top-able window; inline reminder card (D-055)
    ├── widgets/
    │   ├── task_row.py     # the one shared task row (dot, title, chips, time) used by every list
    │   ├── timeline.py  now_card.py  capacity_bar.py  progress_ring.py  charts.py
    │   ├── chips.py        # priority / carried / moved / on-hold / due chips
    │   ├── project_dot.py  duration_entry.py  empty_state.py  reminder_card.py
    ├── shortcuts.py        # Adw.ShortcutsDialog definition
    └── style.css
bin/daybook.in              # launcher; configured by meson with pkgdatadir
data/
├── io.github.daybook.Daybook.desktop.in
├── io.github.daybook.Daybook.metainfo.xml.in
├── io.github.daybook.Daybook.service.in      # D-Bus activation
├── icons/hicolor/scalable/apps/io.github.daybook.Daybook.svg
├── icons/hicolor/symbolic/apps/io.github.daybook.Daybook-symbolic.svg
└── meson.build
scripts/
├── setup-dev.sh            # create .venv (--system-site-packages) + dev tools
├── check.sh                # ruff check, ruff format --check, mypy --strict, pytest --cov
├── dev-install.sh          # meson setup/install to ~/.local + cache refresh
├── dev-uninstall.sh
└── build-deb.sh            # meson DESTDIR install → dpkg-deb → lintian
packaging/deb/control.in, copyright, changelog
tests/
├── conftest.py             # tmp DB, ManualClock, fixtures
├── core/…                  # one test module per core module
├── services/…              # GLib main-loop driven tests with ManualClock
├── migrations/fixtures/    # SQL snapshots of every past schema version
└── test_layering.py  test_banned_apis.py  test_packaging.py
meson.build  meson_options.txt  pyproject.toml (ruff/mypy/pytest config only)  README.md
```

---

## 4. Database schema (migration 0001)

Conventions:
- `PRAGMA user_version` = schema version. `migrate()` applies `m000N` steps in order, each in its own `BEGIN IMMEDIATE` transaction that also bumps `user_version`.
- Opening a DB **newer** than the app knows is refused with a clear message; the file is never touched.
- Connection pragmas on every connect: `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, `synchronous=NORMAL`.
- Timestamps: TEXT, UTC, fixed width `YYYY-MM-DDTHH:MM:SSZ` (lexicographic order = chronological order). Dates: local `YYYY-MM-DD`. Durations: integer minutes, except time entries, which keep second precision through their timestamps.
- Every write goes through `db.transaction(conn)` (`BEGIN IMMEDIATE` … `COMMIT`/`ROLLBACK`).

```sql
-- A project with no key_prefix/url_template is an "area" (Docs, Demos…; D-043).
-- Migration 0001 seeds id 1 = 'General' (renameable; never deleted or archived).
CREATE TABLE project (
  id            INTEGER PRIMARY KEY,
  name          TEXT    NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) > 0),
  color         TEXT    NOT NULL,                       -- palette token, e.g. 'blue' (D-019)
  key_prefix    TEXT    UNIQUE COLLATE NOCASE,          -- 'PROJ'; NULL = none
  url_template  TEXT,                                   -- must contain '{key}' (validated in core)
  archived      INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
  sort_order    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT    NOT NULL
);

CREATE TABLE task (
  id              INTEGER PRIMARY KEY,
  project_id      INTEGER NOT NULL DEFAULT 1 REFERENCES project(id) ON DELETE RESTRICT,  -- 1 = General
  title           TEXT    NOT NULL CHECK (length(trim(title)) > 0),
  ticket_key      TEXT    COLLATE NOCASE,               -- 'PROJ-123'; NULL for direct/non-ticket work
  requested_by    TEXT,                                 -- face-to-face requester (D-044)
  priority        INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 4),
  status          TEXT    NOT NULL DEFAULT 'todo'
                  CHECK (status IN ('todo', 'in_progress', 'on_hold', 'done')),        -- D-046
  hold_reason     TEXT    CHECK (hold_reason IN ('blocked', 'approval', 'someone', 'postponed', 'other')),
  hold_note       TEXT,
  hold_since      TEXT,                                 -- UTC; for "waiting 3d"
  follow_up_date  TEXT,                                 -- local date; resurfaces the task
  estimate_min    INTEGER CHECK (estimate_min IS NULL OR estimate_min >= 0),
  due_date        TEXT,                                 -- local date
  notes           TEXT    NOT NULL DEFAULT '',
  recurrence      TEXT,                                 -- JSON Recurrence, NULL = none
  recurs_from_id  INTEGER REFERENCES task(id) ON DELETE SET NULL,
  moved_count     INTEGER NOT NULL DEFAULT 0 CHECK (moved_count >= 0),                  -- D-047
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL,
  completed_at    TEXT,
  deleted_at      TEXT,                                 -- soft delete for Undo (D-017)
  CHECK ((status = 'done') = (completed_at IS NOT NULL)),
  CHECK ((status = 'on_hold') = (hold_reason IS NOT NULL)),
  CHECK ((status = 'on_hold') = (hold_since IS NOT NULL)),
  CHECK (status = 'on_hold' OR (hold_note IS NULL AND follow_up_date IS NULL))
);

CREATE TABLE checklist_item (
  id        INTEGER PRIMARY KEY,
  task_id   INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  text      TEXT    NOT NULL CHECK (length(trim(text)) > 0),
  done      INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
  position  INTEGER NOT NULL
);

CREATE TABLE tag (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) > 0)
);

CREATE TABLE task_tag (
  task_id  INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tag(id)  ON DELETE CASCADE,
  PRIMARY KEY (task_id, tag_id)
) WITHOUT ROWID;

CREATE TABLE meeting (
  id                INTEGER PRIMARY KEY,
  title             TEXT    NOT NULL CHECK (length(trim(title)) > 0),
  start_utc         TEXT    NOT NULL,                   -- first occurrence (series anchor)
  duration_min      INTEGER NOT NULL CHECK (duration_min > 0),
  tzid              TEXT    NOT NULL,                   -- IANA zone the series recurs in (D-010)
  link              TEXT,
  recurrence        TEXT,                               -- JSON Recurrence, NULL = one-off
  reminder_offsets  TEXT    NOT NULL,                   -- JSON list of minutes, e.g. [10, 1]
  project_id        INTEGER REFERENCES project(id) ON DELETE SET NULL,
  is_adhoc          INTEGER NOT NULL DEFAULT 0 CHECK (is_adhoc IN (0, 1)),   -- "Meeting now" (D-049)
  open_ended        INTEGER NOT NULL DEFAULT 0 CHECK (open_ended IN (0, 1)), -- end unknown until "End meeting"
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL,
  deleted_at        TEXT,
  CHECK (open_ended = 0 OR (is_adhoc = 1 AND recurrence IS NULL))
);

CREATE TABLE meeting_exception (          -- single-occurrence changes
  id                  INTEGER PRIMARY KEY,
  meeting_id          INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
  original_start_utc  TEXT    NOT NULL,                 -- identifies the occurrence
  cancelled           INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
  title               TEXT,                             -- NULL = inherit from series
  start_utc           TEXT,
  duration_min        INTEGER CHECK (duration_min IS NULL OR duration_min > 0),
  link                TEXT,
  reminder_offsets    TEXT,
  UNIQUE (meeting_id, original_start_utc)
);

CREATE TABLE day_plan_item (
  id            INTEGER PRIMARY KEY,
  plan_date     TEXT    NOT NULL,                       -- local date
  task_id       INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  allotted_min  INTEGER CHECK (allotted_min IS NULL OR allotted_min > 0),  -- NULL = "Rest of day" (D-050)
  position      INTEGER NOT NULL,
  carry_count   INTEGER NOT NULL DEFAULT 0 CHECK (carry_count >= 0),       -- consecutive carry-overs (D-045)
  carried_from  TEXT,                                                       -- plan_date it came from
  UNIQUE (plan_date, task_id)
);

CREATE TABLE time_off (                   -- leave and holidays (D-048)
  day   TEXT PRIMARY KEY,                                -- local date
  part  TEXT NOT NULL CHECK (part IN ('full', 'am', 'pm')),
  note  TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE time_entry (
  id         INTEGER PRIMARY KEY,
  task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  start_utc  TEXT    NOT NULL,
  end_utc    TEXT    NOT NULL,
  note       TEXT    NOT NULL DEFAULT '',
  source     TEXT    NOT NULL CHECK (source IN ('timer', 'manual')),
  CHECK (end_utc > start_utc)
);

-- Overlap guard (half-open intervals). Entries never overlap, so only the latest entry
-- starting before NEW.end_utc can overlap: an O(log n) index probe, even at 50k rows.
CREATE TRIGGER time_entry_no_overlap_ins BEFORE INSERT ON time_entry
WHEN EXISTS (SELECT 1 FROM (SELECT end_utc FROM time_entry
                            WHERE start_utc < NEW.end_utc
                            ORDER BY start_utc DESC LIMIT 1)
             WHERE end_utc > NEW.start_utc)
BEGIN SELECT RAISE(ABORT, 'time_entry_overlap'); END;

CREATE TRIGGER time_entry_no_overlap_upd BEFORE UPDATE OF start_utc, end_utc ON time_entry
WHEN EXISTS (SELECT 1 FROM (SELECT end_utc FROM time_entry
                            WHERE start_utc < NEW.end_utc AND id <> NEW.id
                            ORDER BY start_utc DESC LIMIT 1)
             WHERE end_utc > NEW.start_utc)
BEGIN SELECT RAISE(ABORT, 'time_entry_overlap'); END;

CREATE TABLE timer_state (                -- no row = no timer
  id                     INTEGER PRIMARY KEY CHECK (id = 1),
  task_id                INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  state                  TEXT    NOT NULL CHECK (state IN ('running', 'paused')),
  segment_start_utc      TEXT,                          -- set iff running
  last_seen_utc          TEXT    NOT NULL,              -- heartbeat (D-023)
  pause_reason           TEXT    CHECK (pause_reason IN ('user', 'meeting', 'idle')),
  paused_by_meeting_key  TEXT,                          -- meeting occurrence that auto-paused it
  updated_at             TEXT    NOT NULL,
  CHECK ((state = 'running') = (segment_start_utc IS NOT NULL))
);

CREATE TABLE reminder_log (
  id          INTEGER PRIMARY KEY,
  key         TEXT NOT NULL UNIQUE,                     -- one row per reminder occurrence
  kind        TEXT NOT NULL,                            -- meeting | allot_warn | allot_up | drift | meeting_end | sod | eod
                                                        --   | overflow | open_meeting | summary | test
  due_utc     TEXT NOT NULL,
  logged_utc  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('fired', 'missed', 'suppressed')),
  action      TEXT,                                     -- join | snooze | switch | extend | done | start | skip | resume | pick | open | dismissed
  action_utc  TEXT,
  payload     TEXT NOT NULL DEFAULT '{}'                -- JSON: titles, ticket keys, link…
);

CREATE TABLE pending_reminder (           -- snoozes; survive restarts
  id       INTEGER PRIMARY KEY,
  key      TEXT NOT NULL UNIQUE,                        -- '<original key>:snooze<n>'
  kind     TEXT NOT NULL,
  due_utc  TEXT NOT NULL,
  payload  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE settings  (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;  -- JSON values
CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;  -- last_check_utc, last_backup_date, onboarding_done…

CREATE INDEX task_active_idx      ON task (deleted_at, status, priority, due_date);
CREATE INDEX task_project_idx     ON task (project_id);
CREATE INDEX task_ticket_idx      ON task (ticket_key);
CREATE INDEX task_completed_idx   ON task (completed_at);
CREATE INDEX task_follow_up_idx   ON task (follow_up_date) WHERE status = 'on_hold';
CREATE INDEX checklist_task_idx   ON checklist_item (task_id, position);
CREATE INDEX task_tag_tag_idx     ON task_tag (tag_id);
CREATE INDEX meeting_start_idx    ON meeting (deleted_at, start_utc);
CREATE INDEX plan_date_idx        ON day_plan_item (plan_date, position);
CREATE INDEX plan_task_idx        ON day_plan_item (task_id);
CREATE INDEX time_entry_start_idx ON time_entry (start_utc);
CREATE INDEX time_entry_task_idx  ON time_entry (task_id, start_utc);
CREATE INDEX reminder_due_idx     ON reminder_log (due_utc);
CREATE INDEX pending_due_idx      ON pending_reminder (due_utc);
```

### Recurrence JSON (tasks and meetings)
```json
{"freq": "daily" | "weekdays" | "weekly" | "monthly",
 "interval": 1,
 "days": [0, 2, 4],          // weekly only; 0 = Monday
 "until": "2027-06-30"}      // optional local date, inclusive
```
"weekdays" means Monday–Friday (not the user's workday setting; D-012). Monthly repeats on the anchor's day of month, clamped to the month's last day (D-011).

### Settings keys and defaults
| Key | Default |
|---|---|
| `work_start` / `work_end` | `"09:00"` / `"18:00"` |
| `workdays` | `[0,1,2,3,4]` |
| `lunch_start` / `lunch_minutes` | `"13:00"` / `60` (`lunch_minutes = 0` disables) |
| `meeting_offsets` | `[10, 1]` |
| `allotment_warning_min` | `10` |
| `drift_after_min` | `5` |
| `sound_enabled` | `true` |
| `auto_pause_meetings` | `true` |
| `timesheet_rounding_min` | `15` (rounding: nearest; D-028) |
| `start_at_login` | `false` |
| `idle_threshold_min` | `10` |
| `run_in_background` | `true` |
| `auto_carry_over` | `true` (D-045) |
| `meeting_reminders_on_leave` | `false` (D-048) |
| `adhoc_default_min` | `30` (D-049) |

---

## 5. Key algorithms

### 5.1 Auto-schedule (`core/planner.py`)
```python
def schedule(
    day: date, tz: tzinfo,
    window: DayWindow | None,            # from WorkCalendar.day_window(day): working span after
                                         #   half-day leave; None = not a working day (D-048)
    lunch: Interval | None,              # local
    meetings: Sequence[Busy],            # today's occurrences (UTC), fixed anchors; an open ad-hoc
                                         #   meeting is passed with end = max(start+duration, now+1min)
    items: Sequence[PlanInput],          # ordered: plan_item_id, task_id, remaining_min | None (Rest of day)
    now: datetime,                       # UTC
) -> Schedule                            # segments, overflow, capacity
```
1. Window = `[max(window.start, now), window.end]`, converted to UTC through `tz`. A DST-gap or DST-fold start/end is resolved with `fold=0`. If `window is None`, nothing is scheduled and every item overflows, with the note "Not a working day".
2. Busy = meetings ∪ lunch, clipped to the window, sorted and merged. Overlapping or back-to-back meetings and a meeting overlapping lunch merge into one busy block. Each original meeting and lunch is still emitted as its own display segment.
3. Free windows = window − busy, numbered `0..n` (the drift-segment index, §5.5). Windows shorter than `MIN_FRAGMENT_MIN = 5` are not filled (D-015).
4. **Sizing.** Timed items get `remaining_min = max(0, allotted − tracked_today)`. Then `leftover = Σ free − Σ timed remaining`. If `leftover > 0`, each **Rest-of-day** item gets an equal share (remainder minutes go to the earliest item); otherwise each gets 0 (D-050). Callers never pass on-hold tasks (D-046).
5. **Placement.** Walk items in plan order and fill free windows greedily. A task that hits a busy block continues after it, as `part=1, 2, …`.
6. Items, or the tail of an item, that don't fit before the window end go into `overflow` with their unscheduled minutes. Nothing is scheduled past the window end. Rest-of-day items never overflow; at worst they get 0 minutes.
7. `capacity = Capacity(planned_min = Σ timed remaining, flexible_count, free_min = Σ free windows)`, with `over_by = max(0, planned − free)`. The capacity bar reads "Planned 7h 30m / free 5h 45m", or "Planned 3h 30m + rest of day / free 5h 45m" when there are flexible items.
8. Pure and deterministic: same inputs give the same output, and there is no I/O and no clock.

Mandatory tests:
- planning starts mid-day
- a meeting overlaps lunch
- two meetings back to back
- the plan overflows past the end of the day

Also tested:
- non-working day and full-day leave (empty window)
- half-day leave, morning and afternoon
- `now` after the window end
- zero items
- a meeting fully outside working hours
- a DST-change day
- sub-5-min gaps
- an item with 0 remaining
- one Rest-of-day item, several sharing leftover time, and Rest-of-day mixed with overflowing timed items
- an open ad-hoc meeting that keeps pushing the plan

Candidates for "Plan my day" (`planner.candidates`), each with the reason it's listed:
- overdue tasks
- tasks due today
- **on-hold tasks whose follow-up date is today or earlier** ("Follow up")
- P1/P2 Inbox tasks

Carry-overs are no longer candidates: they're already in the plan (§5.2). Candidates are de-duplicated and exclude tasks already planned for the target date.

### 5.2 Working calendar and day rollover (`core/calendar.py`, `core/carryover.py`, `services/rollover.py`)
**WorkCalendar** is built from the settings (workdays, hours, lunch) and the `time_off` rows. It is the only code that answers "is this a working day?":
- `is_working(day)` is false on non-workdays and full-day leave.
- `day_window(day)` cuts half-day leave at lunch: `am` off → window starts at lunch end; `pm` off → window ends at lunch start. Without a lunch break, the cut is at the midpoint of working hours.
- `next_working_day(day)` and `prev_working_day(day)` skip leave and non-workdays. They're used by carry-over, "Move to tomorrow", the review and the standup.

**Rollover** (`carryover.rollover(plans_since_last, tracked, today) -> list[NewPlanItem]`, pure):
1. Runs at startup and at the first scheduler tick on a working day where `app_state.last_rollover_date < today`. It runs only if `auto_carry_over` is on.
2. Source items: every plan item dated after `last_rollover_date` and before today whose task is not Done, not On hold and not deleted. A task planned on several past days carries once, from its latest day.
3. Each one gets a plan item on **today**, appended after today's existing items in their original order:
   - timed items: `allotted = max(15, ceil_to_15(allotted − tracked_that_day))`
   - Rest-of-day items stay Rest of day
   - `carry_count = previous carry_count + 1`, `carried_from = source date`
   - items the user already planned for today are left as they are
4. The old plan items stay as history for the review and "planned vs actual".
5. The whole rollover runs in one transaction that also sets `last_rollover_date = today`, so running it twice changes nothing.
6. The start-of-day notification says what happened ("Plan your day — 3 carried over, 2 follow-ups").

Mandatory tests:
- normal next day
- weekend gap
- leave gap
- app closed for 5 days
- a partly tracked item
- an item fully tracked but not Done (carries with 15 min)
- a Rest-of-day item
- a Done, On hold or deleted task (not carried)
- a task already planned today
- run twice (idempotent)
- `auto_carry_over` off

**Marking time off** (`TimeOffService.set(day, part)`), in one transaction with Undo:
- Plan items on a full-day leave move to `next_working_day`. Each move adds to the task's `moved_count`.
- On a half-day leave, the items stay and the planner simply has less time.
- The dialog lists tasks whose due date falls inside the leave, so they can be moved too.

### 5.3 Ad-hoc ("Meeting now") meetings (`services/meetings.py`)
- **Start:**
  - Inputs: title (default "Meeting"), optional link, duration (15 / 30 / 60 / Unknown), and started (now, or 5 / 10 / 15 / 30 min ago).
  - It creates `meeting(is_adhoc=1, open_ended = duration unknown, start_utc = now − ago, tzid = local)`. An Unknown meeting gets `duration_min = adhoc_default_min` as its provisional length.
- **Timer interaction:**
  - A running timer pauses with `pause_reason='meeting'` and `paused_by_meeting_key`.
  - If the meeting started in the past, the dialog first shows "This removes 12 min from PROJ-123". On confirmation, timer segments and timer entries after the real start are trimmed or deleted in the same transaction. Manual entries are never touched; if one overlaps, the start is refused with an inline message.
- **While open:**
  - The planner treats the end as `max(start + duration, now + 1 min)`, so the plan keeps reflowing.
  - The Now card shows "In meeting · 12 min · [End meeting]".
- **End:**
  - Ending sets `duration_min` to the real length and clears `open_ended`.
  - Then the "Back to PROJ-123?" reminder fires (`meeting_end`).
- **Forgotten:**
  - An open meeting at the end-of-day reminder or on restart raises the Stay-tier `open_meeting` reminder: "Still in 'Meeting' (1h 40m)? [End now] [Still in it]".
  - After a restart, "End now" offers "ended at last activity" (`timer_state.last_seen_utc`, or the app's last check) as well as "now".
- **Overflow:** after any reflow (meeting end, ad-hoc start, plan edit), if the schedule's overflow set is non-empty and different from the last one reported today, the Act-tier `overflow` reminder fires: "2 tasks no longer fit today (1h 30m) [Move to tomorrow] [Review plan]". "Move to tomorrow" moves exactly the overflowing items to `next_working_day`.

### 5.4 Recurrence (`core/recurrence.py`)
- `expand(rule, anchor_utc, tzid, window_start_utc, window_end_utc, exceptions) -> list[Occurrence]`.
- Expansion runs in **wall-clock time** in `tzid`, using `dateutil.rrule` with local naive datetimes, so the anchor's local time-of-day is kept. Each local datetime is then localised with `zoneinfo`:
  - a nonexistent time (spring-forward) is shifted forward by the gap
  - an ambiguous time (fall-back) takes `fold=0`
- Monthly with day > 28 uses the custom clamp, not `bymonthday`, which would skip months.
- Exceptions overlay by `original_start_utc`:
  - cancelled occurrences are dropped
  - overrides replace fields
  - a moved occurrence is placed at its new time, even outside the original window, if the new time is inside the query window
- An occurrence identity is `(meeting_id, original_start_utc)`, used in reminder keys and exceptions.
- Edit scope:
  - "This occurrence" upserts an exception.
  - "All occurrences" updates the series. If the series anchor or rule changes, exceptions that no longer match an occurrence are discarded after a confirmation that names how many (D-013).
- Task recurrence: completing a recurring task creates the next instance (D-016).
- Ad-hoc meetings never recur.

Mandatory tests:
- DST forward/back in Europe/Berlin and America/New_York
- monthly on the 31st
- Feb 29
- weekly on chosen days
- `until`
- cancelled and moved single occurrences
- series edit after exceptions

### 5.5 Reminder engine (`core/reminders.py` pure + `services/scheduler.py` glue)
**Snapshot → reminders.** `compute(snapshot, now) -> list[Reminder]`, where `Reminder(key, kind, tier, subject, due_utc, payload)`. The snapshot holds:
- settings
- the WorkCalendar
- today's and tomorrow's occurrences (with offsets), including open ad-hoc meetings
- the plan with tracked minutes
- timer state
- the schedule
- pending snoozes
- the set of already-logged keys

| Kind | Tier | Key | Due when |
|---|---|---|---|
| Meeting | Stay | `meeting:{mid}:{orig_start}:{offset}` | `start − offset` |
| Meeting snooze | Stay | `meeting:{mid}:{orig_start}:{offset}:snooze{n}` | `snoozed_at + 2 min` (from `pending_reminder`) |
| Allotment warning | Info | `allot_warn:{date}:{item}:{allotted}` | only while the timer runs on the item's task: `now + (remaining − warn_min)`. Skipped if `allotted ≤ warn_min`, and for Rest-of-day items. |
| Allotment used up | Stay | `allot_up:{date}:{item}:{allotted}` | only while the timer runs on that task: `now + remaining`. Never for Rest-of-day items. |
| Drift nudge | Act | `drift:{date}:{item}:{window}` | first moment in free window `window` when no timer has run for `drift_after_min` and the schedule's "planned now" item is `item`, within the working window |
| Meeting ended | Act | `meeting_end:{mid}:{orig_start}` | meeting end (ad-hoc: when ended), only if `timer_state.paused_by_meeting_key` matches |
| Plan overflow | Act | `overflow:{date}:{sha1(sorted item ids)[:8]}` | right after a reflow that produces a new non-empty overflow set (§5.3) |
| Open ad-hoc meeting | Stay | `open_meeting:{mid}:{date}` | the end-of-day time, or at startup, while `open_ended = 1` |
| Start of day | Info | `sod:{date}` | window start on a working day. The body includes carry-overs and follow-ups due. |
| End of day | Info | `eod:{date}` | window end − 15 min on a working day |
| Test | Stay | `test:{now}` | only from Preferences → "Send test reminder" |

Allotment reminders are based on **tracked** time, so their due times move when the timer pauses. That is why they are recomputed rather than stored. "+15 min" raises `allotted_min`, which changes the key, so a fresh warning and used-up notice can fire later.

**Leave days** (WorkCalendar): on a full-day leave, `sod`, `eod`, drift and allotment reminders aren't generated. Meeting reminders aren't generated either unless `meeting_reminders_on_leave` is on. On a half-day leave, the same applies to anything due inside the off half.

**Presentation** (`services/notifier.py`, D-053 – D-055):

| Tier | `Gio.NotificationPriority` | On screen | Sound |
|---|---|---|---|
| Stay | `URGENT` | banner stays until acted on or dismissed | `alarm-clock-elapsed` |
| Act | `HIGH` | banner hides after a few seconds; stays in the list | `message-new-instant` |
| Info | `NORMAL` | same as Act | `message-new-instant` (none for the 10-min warning) |

- **One notification per subject.** `send_notification(id, …)` uses a stable id:
  - `meeting:{mid}:{orig_start}` for meeting reminders
  - `task:{task_id}` for allotment, drift and back-to-task reminders
  - `day:{date}` for start-of-day, end-of-day and overflow
  - `summary` for the missed summary

  A newer reminder for the same subject replaces the older banner; it doesn't stack.
- **Withdraw when moot.** `withdraw_notification(id)` runs on these events:
  - a meeting is joined or has ended
  - a task is done, put on hold or moved
  - a timer starts on the nudged task
  - the allotment is extended
  - the plan is reviewed after an overflow
- **Buttons.** At most 3 buttons, plus a default action that opens the relevant view. Titles stay short (≤ 40 characters, subject first: "Standup in 1 min"). The body holds the details.
- **Also shown in the app:**
  - When the main window is focused, the reminder also appears as an `Adw.Banner` with the same actions.
  - When the Focus window is open, it shows a `reminder_card` (accent background, same actions) until the reminder is handled or withdrawn.
- **Diagnostics.** At startup, Preferences → Notifications checks that the `.desktop` file and the D-Bus service file for APP_ID are installed and that `org.gtk.Notifications` exists on the session bus. If something is missing, a diagnostics row explains it ("Daybook is not installed — run scripts/dev-install.sh"). "Send test reminder" fires a Stay-tier test notification with [Works] [Snooze 2 min].

**Scheduler loop (`ReminderScheduler`):**
1. `recompute()`:
   1. Build the snapshot.
   2. Fire everything with `due ≤ now` that isn't logged (see firing).
   3. Find the earliest future `due`.
   4. Arm a single `GLib.timeout_add` for `clock.real_seconds_until(due)`, capped at 30 s, replacing the previous source.
2. Triggers:
   - the armed timeout
   - every `Changed` event (coalesced with an idle source)
   - a 30 s tick
   - logind `PrepareForSleep(false)` (resume)
   - the timezone refresh (D-014)
3. The 30 s tick compares the wall clock with the monotonic clock. A jump of more than 60 s counts as resume and goes through the missed path.
4. **Firing:**
   1. `INSERT INTO reminder_log … ON CONFLICT(key) DO NOTHING RETURNING id` inside a transaction.
   2. Only if a row came back, send the notification.
   3. Then advance the persisted `app_state.last_check_utc` watermark.

   The unique key makes delivery **at most once**, across restarts, races and resume (D-024).
5. **Missed summary.** On startup or resume, every reminder with `last_check_utc < due ≤ now − 2 min` is logged with `status='missed'`, and none of them fire individually.
   - A meeting that is still upcoming or in progress fires **one** live reminder ("Standup in 3 min [Join]", or "Standup started 4 min ago [Join]"). Its older offsets are marked missed but left out of the summary.
   - Everything else goes into one summary notification, key `summary:{now}`: "While you were away: Standup 10:00 (missed), PROJ-123 allotment ended".
   - Reminders due within the 2-min grace fire normally.
   - The first run on a fresh DB sets the watermark to now, so there is no summary.
6. **Auto-pause.** At a meeting start (key `autopause:{mid}:{orig_start}`, logged the same way), if `auto_pause_meetings` is on and a timer runs, TimerService pauses with `pause_reason='meeting'` and `paused_by_meeting_key`. At the meeting end, the "Back to PROJ-123?" notification comes up with [Resume] [Pick another].

### 5.6 Timer (`core/timer.py` pure, `services/timer_service.py`)
- States: no row → `running(task, segment_start)` ⇄ `paused(task, reason)` → stopped (row deleted).
- **Status side effects** (D-046):
  - Starting a timer on a To do or On hold task sets it to In progress. Starting an On hold task asks first: "PROJ-123 is on hold (waiting for approval). Resume it?"
  - Putting the timed task on hold, or marking it Done, stops the timer.
- **Rest-of-day items** (D-050): the Now card and Focus window show *elapsed today* and "free time left today" instead of the remaining allotment. The progress ring shows the working day's progress.
- **Pause or stop** closes the segment: one transaction inserts `time_entry(segment_start, now, source='timer')` and updates or deletes `timer_state`. A segment shorter than 1 s is dropped.
- **Start on another task** stops the current task and starts the new one, in one transaction.
- **Heartbeat:** while running, `last_seen_utc` is updated every 60 s and on every state change. Every change is persisted immediately.
- **Recovery** runs on startup when a `running` row exists. A dialog asks "Timer was running for PROJ-123 since 14:05 — Keep / Stop at last activity / Discard":
  - Keep: the segment continues.
  - Stop at last activity: an entry is written up to `last_seen_utc`, and the timer is stopped.
  - Discard: the segment is dropped and the timer is stopped.

  If the app was quit normally, the timer was already paused or stopped, so no prompt appears.
- Manual entries:
  - Validated by `core/tracking.py`: `end > start`, `end ≤ now`, and no overlap with entries **or the live running segment**.
  - Errors map to inline form messages.
  - The DB trigger is the backstop.
- Tracked today = Σ entries clipped to `local_day_bounds(today)`, plus the live segment.

### 5.7 Quick-add grammar (`core/quickadd.py`)
`PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today`
`Prepare demo for client #demos +Rahul @today=day`

| Token | Meaning |
|---|---|
| `[A-Za-z][A-Za-z0-9]*-\d+` (first match, word-bounded) | ticket key, upper-cased. Its prefix infers the project when a project has that `key_prefix`. |
| `#word` | project or area if `word` matches a project name or key prefix (case-insensitive); otherwise a tag. An explicit `#project` overrides the prefix inference, with a warning in the preview (D-001). A second matching `#project` is an error. With no project at all, the task goes to **General**. |
| `+name` | Requested by. `_` becomes a space (`+Rahul_Shah`). The preview autocompletes from past values (D-044). |
| `!p1`…`!p4` (also `!1`…`!4`) | priority. Anything else is an error. |
| `~2h`, `~30m`, `~1h30m`, `~1.5h`, `~90` (minutes) | total estimate |
| `^today`, `^tomorrow`, `^mon`…`^sun` / full names, `^dd/mm`, `^dd/mm/yyyy`, `^yyyy-mm-dd` | due date (D-021) |
| `@<day>` where `<day>` is `today`, `tomorrow`, a weekday, `dd/mm` or `yyyy-mm-dd` | plan on that day. A non-working target day is an error that names the next working day. |
| `@<day>=2h` / `=30m` / `=1h30m` | plan with that allotment (default: estimate if set, else 30 min) |
| `@<day>=day` | plan as **Rest of day** (D-050) |
| the rest | title (whitespace collapsed). An empty title is an error. |

`parse(line, ctx: ParseContext(today, projects, calendar)) -> ParseResult(fields, errors, warnings)` never raises. The dialog shows a live preview and disables Save while there are errors. Tests are table-driven and cover every token type, its malformed variants, ordering, duplicates, unicode and empty input.

---

## 6. UI design — calm, fast, modern (SPEC A1, A10; D-057)

### 6.1 Principles
1. **Simpler than the PMS.** The only required field is a title. A new task form shows Title, Area/Project, Priority, and "Today" with a time; everything else sits under a collapsed **More details**. Defaults: General, P3, unplanned.
2. **One primary action per screen.** It is the `suggested-action` button: Today → *Start* / *Pause*; Plan → *Start day*; Review → *Finish day*; dialogs → *Save*.
3. **Scannable lists, no table chrome.** Every task everywhere uses the same `task_row` (§6.3). ColumnView is used only for the timesheet and report tables.
4. **Keyboard first.** Every action has a shortcut or is reachable with Tab and arrow keys. Focus is always visible. The quick-add palette (Ctrl+K) is the fastest way to do anything.
5. **libadwaita only.** No custom theme. Colours come from Adw named colours and the system accent (`--accent-bg-color`). The app looks right in light and dark mode and with every accent colour. Custom CSS is limited to layout, chips and timeline geometry.
6. **Calm motion.** Animations last 250 ms or less (Adw.TimedAnimation for transitions, Adw.SpringAnimation for drag-and-drop settling). When `gtk-enable-animations` is off, they're skipped. Nothing blinks; a Stay-tier alert in the Focus window gets a single accent pulse.
7. **Honest status.** Carried-over, moved, on-hold and overdue states are always visible as chips, so nothing silently slides.

### 6.2 Visual language
| Element | Treatment |
|---|---|
| Project/area | 10 px colour dot (palette, D-019) and name in `dim-label` |
| Priority | pill: P1 `error` colour, P2 `warning`, P3 neutral (hidden in rows), P4 `dim-label` |
| Chips | small rounded `caption` labels: `carried ×2`, `moved ×3`, `on hold · approval · 3d`, `due today`, `overdue 2d`, `from Rahul` (with Adw.Avatar initials) |
| Times | `numeric` (tabular figures). The Now-card countdown uses `title-1`; row times use `caption`. |
| Cards | `.card` for the Now card, timeline blocks and the capacity bar; `boxed-list` for settings-like lists |
| Timeline | meetings: hatched block in the project colour. Tasks: solid accent-tinted block. Lunch and leave: neutral block. "Now" line: 2 px accent with a time label. |
| Empty states | Adw.StatusPage with a symbolic icon, one sentence and one action button |

### 6.3 Screens (wide ≥ 900 px; adaptive notes below each)

**Main window, Today**
```
┌──────────────────┬────────────────────────────────────────────────────────────┐
│ Daybook      ☰   │  Today · Tue 3 Nov            [⚡ Meeting now] [＋] [⌕]    │
│──────────────────│ ┌────────────────────────────────────────────────────────┐ │
│ ◉ Today       5  │ │ NOW  ● PROJ-123  Fix login timeout                     │ │
│ ○ Plan           │ │      0:42:10 tracked  ·  18 min left today      ◔      │ │
│ ○ Inbox       12 │ │      [ ⏸ Pause ]  [ ✓ Done ]  [ ⇥ Move to… ]           │ │
│ ○ Upcoming     3 │ │      Next: Standup in 12 min  ·  then PROJ-456 (1h)    │ │
│ ○ Meetings       │ └────────────────────────────────────────────────────────┘ │
│ ○ Reports        │  Planned 6h 30m / free 5h 10m  ████████████░░░  ⚠ 1 over  │
│ ○ Done           │                                                            │
│──────────────────│  09:00 ┃ ▌PROJ-101 Review PR            done   1h 00m      │
│ PROJECTS & AREAS │  10:00 ┃ ▒ Standup                      Join   15 min      │
│ ● PROJ        8  │  10:15 ┃ ▌PROJ-123 Fix login…  carried ×1  ←now            │
│ ● WEB         4  │ ───────┃──────────────────────────── 10:41 ─────────       │
│ ● Docs        2  │  11:30 ┃ ▌Prepare demo  from Rahul  rest of day            │
│ ● Demos       1  │  13:00 ┃ ░ Lunch                                           │
│ ● General     3  │  …                                                         │
│ ＋ New project   │                                    [ Review day ]          │
└──────────────────┴────────────────────────────────────────────────────────────┘
```
- Below 900 px the sidebar collapses (NavigationSplitView) and the task detail becomes a pushed page.
- Below 600 px the header buttons move into an overflow menu, except *Meeting now*. The Now card stacks vertically.

**Plan**
```
┌ Plan · [Today | Tomorrow]  (Adw.ToggleGroup) ─────────────────────── [Start day] ┐
│ CANDIDATES                         │ TODAY'S PLAN               drag ⇅ / Alt+↑↓   │
│ ● PROJ-130 Payment bug   overdue   │ 1 ● PROJ-123 Fix login   carried ×1  [1h ▾]  │
│   [15m][30m][1h][2h][…][Day]       │ 2 ● PROJ-456 API tests              [2h ▾]  │
│ ● Write API docs   follow up       │ 3 ● Prepare demo  from Rahul        [Day ▾] │
│ ● WEB-12 Footer   due today        │ ── doesn't fit (40 min) ───────────────────  │
│                                    │ 4 ● WEB-9 Cleanup                   [1h ▾]  │
│                                    │ Planned 4h + rest of day / free 5h 10m       │
└────────────────────────────────────┴──────────────────────────────────────────────┘
```
- Below 600 px the candidates move into an Adw.BottomSheet ("Add tasks ▴") and the plan fills the screen.
- Non-fitting items sit below a "doesn't fit" divider, with a *Move to tomorrow* button on the divider.

**Task detail** (Adw.OverlaySplitView sidebar on the right, 360 px; a pushed page when narrow)
```
┌ PROJ-123 ↗ Open ticket                                           [ ✕ ] ┐
│ Fix login timeout                                         (inline edit)│
│ ● PROJ   P2 ▾   In progress ▾   Today: 1h ▾                            │
│ 0:42 today · 3h 10m total of 4h estimate  ▓▓▓▓▓▓▓▓░░                   │
│ ▸ More details  (ticket, due, estimate, requested by, tags, recurrence)│
│ Notes …                                                                │
│ Checklist  ☑ repro  ☐ fix  ☐ test                                     │
│ [ Put on hold… ]   [ Move to… ]                         [ Mark done ]  │
└────────────────────────────────────────────────────────────────────────┘
```

**Quick add** (Ctrl+K: a centred Adw.Dialog, 560 px, like a command palette)
```
┌──────────────────────────────────────────────────────────────┐
│ ⌕ Prepare demo for client #demos +Rahul @today=day           │
│ ──────────────────────────────────────────────────────────── │
│ Prepare demo for client                                      │
│ ● Demos  · P3 · from Rahul · Today: rest of day              │
│ ⚠ (warnings and errors appear here, inline)      [ Add ↵ ]   │
└──────────────────────────────────────────────────────────────┘
```

**Focus window** (≈ 320 × 360 px; the user pins it with "Always on Top")
```
┌ ● PROJ-123 ───────────── ┐
│   Fix login timeout      │
│        ╭──────╮          │
│        │ 18:04│  left    │
│        ╰──────╯ (ring)   │
│ [⏸] [✓ Done] [⇥ Next]    │
│ ┌──────────────────────┐ │ ← reminder card (accent) when a reminder is live
│ │ Standup in 1 min     │ │
│ │ [Join] [Snooze 2m]   │ │
│ └──────────────────────┘ │
└──────────────────────────┘
```

**Stay-tier notification** (GNOME banner; stays until acted on)
```
┌ Daybook ─────────────────────────────────────────┐
│ Time's up: PROJ-123                              │
│ Planned 1h, spent 1h 02m. Next: PROJ-456         │
│ [Switch to next]   [+15 min]   [Mark done]       │
└──────────────────────────────────────────────────┘
```

**Meeting now** (Ctrl+Shift+M: a small Adw.Dialog)
```
Title [Meeting            ]  Link [optional          ]
Length  (15m)(30m)(1h)(Unknown)      Started (Now)(5)(10)(15)(30 min ago)
ⓘ Removes 12 min from PROJ-123                              [ Start meeting ]
```

### 6.4 Accessibility
- Every icon-only button has a tooltip and an accessible label.
- Chips have accessible descriptions ("Carried over 2 days").
- Timeline blocks are focusable rows with a full text label.
- The colour dot is never the only signal: the project name is always visible or in the label.
- Contrast is checked in both themes.

## 7. Phase checklist

Exit gate for **every** phase:
- [ ] `scripts/check.sh` passes: ruff check, ruff format --check, mypy `--strict` on core + services, pytest with core coverage ≥ 90%
- [ ] `scripts/dev-install.sh` and launch from the app grid. The terminal shows no GTK criticals, warnings or deprecations.
- [ ] `desktop-file-validate` + `appstreamcli validate` clean
- [ ] No TODO, stub, placeholder button or commented-out code (`grep` check in `check.sh`)
- [ ] `docs/PROGRESS.md` updated, plus `CLAUDE.md` "Current phase"
- [ ] Committed and pushed on the phase's `feature/…` branch (D-064); pull request into `main`
- [ ] Stop; give the user a short manual test list

### Phase 0 — Scaffold
- [ ] Before the branch: initial docs commit on `main` (SPEC, PLAN, DECISIONS, CLAUDE.md)
- [ ] `pyproject.toml` (ruff, mypy, pytest, coverage config), `scripts/setup-dev.sh`, `scripts/check.sh`
- [ ] `meson.build`, `meson_options.txt`, `bin/daybook.in`. APP_ID and VERSION are read from `daybook/const.py`.
- [ ] `data/`: .desktop, metainfo, D-Bus service, app icon + symbolic icon
- [ ] `scripts/dev-install.sh` / `dev-uninstall.sh` (including `update-desktop-database`, `gtk-update-icon-cache`)
- [ ] `paths.py` (XDG + `DAYBOOK_DATA_DIR`), `logsetup.py` (rotation, `--debug`), exception hooks, `services/safe.py`
- [ ] `core/clock.py` (System/Scaled/Manual, env parsing), `core/timeutil.py`
- [ ] `core/db.py`, migration framework, `m0001_initial` (full schema above), migration tests
- [ ] `core/settings.py` + settings repo
- [ ] `services/events.py`, `services/context.py`, `services/worker.py`
- [ ] `Adw.Application` (single instance, `HANDLES_COMMAND_LINE`, `--debug`) with a main window, NavigationSplitView and sidebar listing every section. Each section shows its final empty state (a StatusPage explaining what will appear there; no dead buttons).
- [ ] Layering and banned-API tests
- [ ] Done when the app launches from the GNOME app grid

### Phase 1 — Projects, areas and tasks
- [ ] Models + repositories: project/area (General seeded), task, checklist, tags; soft delete + purge
- [ ] `core/sources.py` `TaskSource` Protocol
- [ ] Services with undo tokens (complete / delete / hold / move → Toast with Undo)
- [ ] `task_row` + chips widgets (§6.2); sidebar badges with a "Projects & areas" group; Inbox, Upcoming (with an On hold group), per-project, Done lists (ListView, priority → due sort; overdue and due-today chips)
- [ ] Filters (project/area, status, priority, tag, requested by); instant search (Ctrl+F) across title, ticket key and notes, including Done
- [ ] Minimal new-task dialog (Ctrl+N): Title*, Area, Priority, Today + time; "More details" holds the rest (D-057)
- [ ] Task detail side panel (OverlaySplitView): inline edit of all fields, checklist, Requested by with autocomplete, "Open ticket" (UriLauncher), time today/total vs estimate (0 until Phase 3, real query)
- [ ] Statuses To do / In progress / On hold / Done; "Put on hold…" dialog (reason, note, follow-up date); `core/hold.py` + tests
- [ ] "Move to…" dialog (Tomorrow / Next working day / Pick date / Unplanned + optional reason), `moved_count` chip. Until Phase 2, "next working day" uses workdays only.
- [ ] Project/area create, edit and archive (colour palette, optional key prefix and URL template with validation; General can't be deleted)
- [ ] Empty states; adaptive layout at 360 / 600 / 900 px

### Phase 2 — Calendar, meetings and planning
- [ ] `core/calendar.py` (WorkCalendar) + tests; `time_off` repo/service; "Time off" dialog (full / morning / afternoon, note) with plan moves and the due-during-leave list
- [ ] `core/recurrence.py` + tests (DST, month-end, exceptions)
- [ ] Meeting repo/service, Meetings view, editor (link, recurrence, offsets, project), "this occurrence / all" scope dialog
- [ ] **Meeting now** dialog + ad-hoc lifecycle (start, open-ended, End meeting); past-start timer trimming arrives in Phase 3
- [ ] Day plan repo/service; Plan view per §6.3 (candidates with reasons incl. follow-ups | plan, presets 15m/30m/1h/2h/custom/**Rest of day**, DnD + Alt+Up/Down, Today/Tomorrow toggle)
- [ ] `core/planner.py` + mandatory tests (including half-day leave, Rest-of-day items, open ad-hoc meetings); capacity bar with the over-capacity warning, a "doesn't fit" divider and *Move to tomorrow*
- [ ] `core/carryover.py` + mandatory tests; `services/rollover.py` (startup + first tick), "carried ×N" chips
- [ ] Today view per §6.3: timeline (meetings, ad-hoc meetings, task segments, lunch, leave, now line) + Now card (current task, timer, allotment left or elapsed, next, next-meeting countdown, *Meeting now*)

### Phase 3 — Timer and time tracking
- [ ] `core/timer.py`, `core/tracking.py`, timer repo, TimerService (single timer, switch, heartbeat, status side effects D-046)
- [ ] Space start/pause, Ctrl+Enter done; Now card and task detail wired to the live timer; elapsed mode for Rest-of-day items
- [ ] Recovery dialog on startup (Keep / Stop at last activity / Discard) + tests
- [ ] Manual time entries: list per task/day, add/edit/delete with overlap validation
- [ ] Ad-hoc meeting started in the past: timer pause + trimming with the preview message + tests
- [ ] Focus window per §6.3 (task, large remaining/elapsed time, progress ring, Pause / Done / Next, reminder-card slot); documented pin via "Always on Top"

### Phase 4 — Reminder engine
- [ ] `core/reminders.py` + tests (every kind and tier, keys, leave suppression, Rest-of-day exclusion, overflow set changes, open ad-hoc meeting, suspend/resume, missed summary, no duplicates, snooze, +15 min)
- [ ] ReminderScheduler (single timeout, 30 s tick, change-driven recompute, clock-jump detection, tz refresh)
- [ ] `services/power.py` (PrepareForSleep, PrepareForShutdown)
- [ ] Notifier: Gio.Notification with app actions (Join, Snooze 2 min, Switch to next, +15 min, Mark done, Start, Skip, Resume, Pick another, Move to tomorrow, Review plan, End now, Still in it, open Plan/Review); tier → priority + sound (D-053); stable ids, replace + withdraw (D-054); GSound with fallback; in-app Adw.Banner when focused
- [ ] Focus-window reminder card (D-055)
- [ ] **Verify on this machine:** URGENT banners stay until acted on and HIGH/NORMAL banners hide; record the result in D-053
- [ ] Auto-pause during meetings + "Back to …?"; start-of-day (with carry-overs + follow-ups) → Plan view; end-of-day → Review page (a minimal Review page ships here and is completed in Phase 6)
- [ ] Preferences → Notifications: diagnostics row + "Send test reminder"
- [ ] Manual verification with `DAYBOOK_TIME_SCALE=60` / `DAYBOOK_FAKE_NOW`

### Phase 5 — Fast input
- [ ] `core/quickadd.py` + exhaustive tests (including `+name`, `@<day>`, `@<day>=2h`, `@<day>=day`); quick-add palette with live preview chips (Ctrl+K)
- [ ] CLI: `--quick-add`, `--meeting-now`, `--background`, `--status`, `--debug`; forwarding to the running instance
- [ ] All shortcuts (Ctrl+N/K/F, Ctrl+Shift+M, Space, Ctrl+Enter, Ctrl+1…7, Ctrl+,, Ctrl+?) + Adw.ShortcutsDialog
- [ ] README section: binding `daybook --quick-add` (and optionally `--meeting-now`) in GNOME Settings

### Phase 6 — Review and reporting
- [ ] End-of-day review: done today + time per task; unfinished items with Move to tomorrow (next *working* day) / Back to inbox / Put on hold / apply to all; shows what will carry over automatically
- [ ] Standup generator ("Yesterday" = last *worked* day via WorkCalendar; Blockers = On hold tasks with reason and days waiting; "from Rahul" for direct requests) → clipboard as text or Markdown
- [ ] Timesheet day/week, rows per ticket (non-ticket: "Area — Title", D-052), rounding, totals, copy text / CSV export (Gtk.FileDialog)
- [ ] Weekly reports: hours per project/area, planned vs actual per task, completed count, meeting vs focus hours (ad-hoc meetings shown separately), carry-over and move counts (Cairo charts + tables), computed in the worker

### Phase 7 — Background and safety
- [ ] Close-to-background (setting), Quit with running-timer warning
- [ ] Start at login (autostart file with `--background`)
- [ ] Idle detection (Mutter IdleMonitor + ScreenSaver, graceful fallback), "You were away 14 min — Keep / Discard"
- [ ] Tray: StatusNotifierItem + dbusmenu (Show, Quick add, Meeting now, Pause/Resume, Quit), current task + remaining time; skipped silently without a host
- [ ] Backups: daily automatic (keep 14), Backup now, Restore with confirmation + safety backup; round-trip tests
- [ ] JSON export/import (validated before writing; replaces data, D-026), CSV export of time entries
- [ ] Adw.PreferencesDialog with every setting, including a **Time off** page (upcoming leave list, add/remove) and Notifications diagnostics
- [ ] First-run onboarding (working hours, workdays, lunch, first project, optional areas "Docs"/"Demos", optional sample data)

### Phase 8 — Hardening and packaging
- [ ] `docs/QA_CHECKLIST.md` from SPEC; work through every item
- [ ] Performance: seed 5,000 tasks / 50,000 time entries; measure main-loop stalls (< 100 ms) and view load times; fix hot paths
- [ ] `scripts/build-deb.sh` → `daybook_<version>_all.deb`; lintian has no errors; install/remove test with apt
- [ ] README complete (install/uninstall, shortcut, data location, backup/restore, notification troubleshooting)
- [ ] Final pass over the Definition of done; any spec item that doesn't work goes into DECISIONS.md with the reason
