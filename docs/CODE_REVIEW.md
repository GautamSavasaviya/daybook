# Code review — Phase 8

Four reviewers read the whole code base in parallel (core, services, UI, packaging/scripts);
every finding below was then checked against the code, and the serious ones reproduced
before being fixed. Each fixed item has a regression test unless it is UI/packaging, where
the check is stated. **Status:** Fixed, Open (known, not fixed in v1, with why).

## High

| # | Where | Problem | Status |
|---|---|---|---|
| H1 | `core/repo/plan.py` `since()` | Carry-over only looked at the *previous* planned day, so after a gap of several working days (app closed, leave) older unfinished items were silently dropped. Reproduced. | **Fixed** (`plan_date >= after`), 2 regression tests |

## Medium

| # | Where | Problem | Status |
|---|---|---|---|
| M1 | `services/timer.py` | After a crash, if the user pressed Stop/Pause/Done *before* answering the recovery dialog, the whole downtime (e.g. 2 h) was logged as work. | **Fixed**: `settle_recovery()` stops at the last heartbeat first; tasks that stop the timer do the same; a stale dialog answer is ignored. 2 tests |
| M2 | `services/backup.py` | Restore/import blocked the main loop (seconds at 50,000 entries). | **Fixed**: run on the worker; main-thread read during restore 5.8 ms. Perf script |
| M3 | `services/scheduler.py`, `backup.py` | Reminders about the old data stayed on screen after a restore or import. | **Fixed**: `ReminderScheduler.reset()` on data replacement. Test |
| M4 | `core/recurrence.py`, `repo/tasks.py` | A monthly task on the 31st drifted to the 28th forever after February. | **Fixed**: anchor day is kept (`monthly_anchor`). Tests |
| M5 | `core/reports.py`, `repo/reporting.py` | Overlapping meetings were double-counted in meeting hours; deleted tasks leaked into reports/CSV. | **Fixed**. Tests |
| M6 | `core/exchange.py`, `timesheet.py` | CSV export allowed spreadsheet formula injection (`=cmd…` in a title). | **Fixed**: `csv_safe` prefixes `'`. Tests |
| M7 | `core/meetings.py`, `repo/meetings.py` | Clearing the link of one meeting occurrence was ignored. | **Fixed** (empty-string sentinel). Tests |
| M8 | `core/backup.py` | A damaged backup with a bad timestamp raised an unhandled `ValueError`. | **Fixed**. Test |
| M9 | UI `prefs_data.py` | A failed backup/restore showed two toasts and re-raised. | **Fixed** (`report_error`) |
| M10 | UI | Refreshing a list can drop keyboard focus from the selected row. | **Open** — not fixed; fixing means keeping selection identity across model rebuilds |

## Low

| # | Where | Problem | Status |
|---|---|---|---|
| L1 | `core/quickadd.py` | A malformed ticket-like word raised `ValidationError` out of the parser. | Fixed |
| L2 | `core/repo/time_entries.py` | `update()` didn't check the task existed. | Fixed |
| L3 | `core/hold.py` | An invalid hold reason raised a plain error, not `ValidationError`. | Fixed |
| L4 | `core/reschedule.py` | Reschedule note had an untranslated/locale-fragile date. | Fixed |
| L5 | `core/timeutil.py` | `fmt_relative` rounded half-down; `from_db` was slow at 50,000 rows. | Fixed |
| L6 | `services/idle.py`, `power.py` | `stop()` before the async D-Bus connection finished still set things up. | Fixed (`_stopped`) |
| L7 | `services/scheduler.py` | An exception while showing a reminder was swallowed; a restart didn't withdraw a stale one. | Fixed (logged + reported; single withdraw) |
| L8 | `services/tray.py` | `stop()` didn't unsubscribe from the bus. | Fixed |
| L9 | `services/onboarding.py` | The sample "due" task was due today, not tomorrow. | Fixed |
| L10 | `services/tasks.py` | `move_many` could move half the tasks on an error. | Fixed (one transaction) |
| L11 | `services/plan.py` | `remove` read the task title after it could have been deleted. | Fixed |
| L12 | `services/meetings.py` | Undo of "end meeting" on a vanished meeting raised. | Fixed (toast) |
| L13 | `services/backup.py` | A damaged "last backup" value crashed Preferences → Data. | Fixed |
| L14 | `services/context.py` | A failing day check skipped the reminder check on resume. | Fixed (`finally`) |
| L15 | UI `prefs_settings.py` | One untranslated string. | Fixed |
| L16 | `scripts/dev-uninstall.sh` | Left the login autostart entry pointing at a removed program. | Fixed |
| L17 | `meson.build` | An install prefix with spaces would produce a broken `Exec=` line. | Fixed (clear error) |
| L18 | `data/daybook.metainfo.xml.in` | Placeholder release notes. | Fixed |
| L19 | UI Focus window | Lifecycle: the window can outlive a restore (its task may be gone). | **Open** — reported by review, not reproduced; revisit in Phase 9 |
| L20 | UI | Closing the window with *Keep running* off while a timer runs doesn't ask first (the timer is paused and kept, never lost). | **Open** by design (D-120) |
| L21 | UI | A few plurals use `{n} items` forms without `ngettext`. | **Open** — English-only app today |
| L22 | UI `export.py` | File read/write for export/import runs on the main loop (a file of a few MB — milliseconds). | **Open** — not measured at large sizes; export files are normally small |
| L23 | UI | Duplicate dialogs can be opened by pressing a shortcut twice quickly. | **Open** — not fixed; the extra dialog can simply be closed |
| L24 | UI overflow card / divider | Long text in one overflow card isn't wrapped at 360 px. | **Open** — cosmetic |
| L25 | UI | A few icon-only controls lack an accessible name beyond the tooltip. | **Open** — tooltip is used as the name by GTK |
| L26 | `ui/suggest.py` | A timer for suggestions isn't cancelled on close. | **Open** — not fixed; low impact |

## Verified, no problem found
SQL is parameterised everywhere; no `shell=True`; the .desktop/Exec and D-Bus service files
use absolute paths; layering tests enforce core/services/UI boundaries; migrations are
transactional and refuse newer databases; no `datetime.now()` outside `core/clock.py`; no
TODO/stub/commented-out code markers (checked by `scripts/check.sh`).

## Method
- Static review of each module against SPEC and the project conventions.
- Reproduction of every claimed high/medium defect with a failing test *before* the fix.
- Full suite (≈1,230 tests), `ruff`, strict `mypy`, performance script at 5,000 tasks/50,000
  entries, `lintian --pedantic`, and runs of the installed app on a private headless GNOME Shell.
