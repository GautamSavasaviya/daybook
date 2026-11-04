# QA checklist — testing the whole system

How to test Daybook end to end. Part A is what was already tested by automated means (and
how to re-run it). Part B is the hand-test script: every row has two result columns,
**Auto** (covered by the automated suite, or checked by Claude on a private headless GNOME Shell in Phases 5–8; ✔ means the behaviour is tested, not that a person has seen it) and **You** (to fill
in by hand). Mark **✔** pass, **✘** fail (note what you saw), **–** not applicable.

Requirements come from [`SPEC.md`](SPEC.md) (Addendum A wins). Known issues from the code
review are in [`CODE_REVIEW.md`](CODE_REVIEW.md).

---

## Part A — Automated checks (run these first)

```bash
scripts/check.sh                       # ruff, mypy (strict), packaging checks, the full pytest suite
.venv/bin/python scripts/perf-check.py # 5,000 tasks / 50,000 entries: main loop < 100 ms
scripts/build-deb.sh                   # builds dist/daybook_<version>_all.deb and runs lintian
```

| # | Check | Pass criterion | Auto | You |
|---|---|---|---|---|
| A1 | `scripts/check.sh` | ruff, mypy, pytest all pass; core coverage ≥ 90 % (it is ~98 %) | ✔ | |
| A2 | `perf-check.py` | every main-thread operation < 100 ms, every worker job < 2.5 s; restore/import run on the worker | ✔ (27 in budget, 0 over) | |
| A3 | `scripts/build-deb.sh` | `.deb` built; `lintian --pedantic` has no errors or warnings | ✔ | |
| A4 | Install / remove / purge of the `.deb` in a scratch root | nothing left behind after remove; purge removes `/usr/share/io.github.daybook.Daybook` | ✔ | |
| A5 | `desktop-file-validate`, `appstreamcli validate` | no errors | ✔ | |
| A6 | Layer tests (`tests/test_layering.py`) | core has no `gi`; services no Gtk; UI no SQL | ✔ | |
| A7 | Installed app, started with `daybook --debug` in a terminal | **0 bytes** of GTK criticals, warnings or deprecations | ✔ | |

---

## Part B — Hand-test script

**Setup.** Use throwaway data so nothing real is at risk:

```bash
scripts/dev-install.sh
DAYBOOK_DATA_DIR=$(mktemp -d) daybook --debug     # separate "Daybook (Devel)" app, empty data
```

For reminders use fast time (one real second = one minute):

```bash
DAYBOOK_DATA_DIR=$(mktemp -d) DAYBOOK_TIME_SCALE=60 \
  DAYBOOK_FAKE_NOW=2026-11-03T08:55:00+01:00 daybook --debug
```

### B1. First run and settings

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 1.1 | Start with an empty data folder | Welcome dialog: hours, workdays, lunch, first project, Docs/Demos areas, sample data. Skip works | ✔ | |
| 1.2 | Finish the welcome with sample data | Sample tasks appear; the sample "due" task is due tomorrow, not today | ✔ | |
| 1.3 | Ctrl+, → change work end to `17:00`; type `25:99` | The first applies; the second shows an inline/toast error and keeps the old value | ✔ | |
| 1.4 | Untick all workdays | Refused, buttons restored | ✔ | |
| 1.5 | Change rounding, idle time, sound | Each applies at once and survives a restart | ✔ | |

### B2. Tasks, projects and areas (SPEC §1, A1, A2)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 2.1 | Ctrl+N, type only a title, Save | Task in **General**, priority P3; no other field required | ✔ | |
| 2.2 | Open "More details": ticket, due date, estimate, Requested by, notes, checklist, tags, recurrence | All editable; Requested by autocompletes earlier names | ✔ | |
| 2.3 | Create a project with prefix `PROJ` and a URL; make a task `PROJ-1` | Project inferred from the key; "Open ticket" opens the URL | ✔ | |
| 2.4 | Create areas Docs and Demos; try to delete/archive General | Areas work like projects; General can be renamed only | ✔ | |
| 2.5 | Delete a project that has tasks | Confirmation; tasks handled as the dialog says; Undo where offered | ✔ | |
| 2.6 | Complete, then delete a task | Toast with Undo each time; Undo restores | ✔ | |
| 2.7 | Ctrl+F, type part of a title / ticket / note | Instant results; Done tasks are findable | ✔ | |
| 2.8 | Overdue and due-today tasks | Look different from other tasks | ✔ | |
| 2.9 | Open a task with a very long title at a narrow window | Nothing overflows or hides the window buttons; trimmed text has a hover tooltip | ✔ | |
| 2.10 | Recurring task (monthly on the 31st), complete it | Next instance is on the last day of the next month, then returns to the 31st | ✔ | |

### B3. Planning, carry-over, move, hold (SPEC §2, A3–A5, A8)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 3.1 | Plan view: add tasks with 15m/30m/1h/2h/custom | Capacity bar updates; over-capacity warns and names what won't fit | ✔ | |
| 3.2 | Reorder with drag-and-drop and Alt+Up/Down | Order changes both ways | ✔ | |
| 3.3 | Add a meeting inside the plan | Timeline reflows; a task can split around it | ✔ | |
| 3.4 | Add a "Rest of day" item | Fills the free time; no "time is up" reminder; timer shows elapsed | ✔ | |
| 3.5 | Leave a task unfinished; start Daybook next working day (or fake the date) | Carried over with remaining time, "carried over (N days)" chip | ✔ | |
| 3.6 | Leave the app closed for **several** working days, then start | The unfinished item still carries over to today (once, not duplicated) | ✔ | |
| 3.7 | Move to… Tomorrow / Next working day / Pick a date / Unplanned, with a reason | Moves; "moved ×N" chip grows | ✔ | |
| 3.8 | Put a task On hold (each reason, with follow-up date) | Timer stops, leaves today's plan, Undo works; reappears on the follow-up date | ✔ | |
| 3.9 | Turn carry-over off in Preferences | Nothing is carried over | ✔ | |

### B4. Timer and time entries (SPEC §3)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 4.1 | Space on a task | Starts; Space again pauses | ✔ | |
| 4.2 | Start another task while one runs | The first stops and its entry is saved | ✔ | |
| 4.3 | Kill Daybook (`pkill -9 -f daybook`) during a timer, start again | Asks: Keep / Stop at last activity / Discard. Pick Stop: the time is up to the last beat, not including the gap | ✔ | |
| 4.4 | Same, then ignore the dialog and click Pause/Stop/Done instead | Gap is **not** logged as work (stops at the last heartbeat) | ✔ | |
| 4.5 | Add a manual entry that overlaps another | Refused with a clear message | ✔ | |
| 4.6 | Edit / delete a time entry | Applies; totals update | ✔ | |
| 4.7 | Open the Focus window | Task, big remaining time, progress ring, Pause / Done / Next | ✔ | |
| 4.8 | Quit while a timer runs | Asks first; the time so far is kept | ✔ | |

### B5. Meetings and time off (SPEC A6, A7)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 5.1 | Add a meeting with a link and offsets | Reminders at each offset with Join / Snooze | ✔ | |
| 5.2 | Edit one occurrence of a recurring meeting; clear its link | That occurrence has no link; the rest keep theirs | ✔ | |
| 5.3 | **Meeting now** (button, Ctrl+Shift+M, tray, `daybook --meeting-now`) | Starts immediately; timer pauses | ✔ | |
| 5.4 | Meeting now with "started 15 min ago" | Shows how much timer time is removed, then removes it | ✔ | |
| 5.5 | End the meeting | Real duration saved; asks "Back to PROJ-…?"; plan reflows; overflow offers Move / Review | ✔ | |
| 5.6 | Leave an ad-hoc meeting open and restart | Asks if you're still in it | ✔ | |
| 5.7 | Mark a full day and a half day of leave | Not a working day anywhere; the day's plan moves (with Undo); due tasks listed | ✔ | |
| 5.8 | Standup the day after leave | "Yesterday" shows the last *worked* day | ✔ | |

### B6. Reminders (SPEC §4, A9) — the most important part

Needs the **installed** app (`scripts/dev-install.sh`) and fast time.

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 6.1 | Preferences → Notifications → **Send Test Reminder** | A banner appears; diagnostics row says "Notifications work" | ✔ | |
| 6.2 | Meeting reminder | Urgent: stays on screen, sound if enabled, buttons Join / Snooze 2 min | ✔ | |
| 6.3 | Allotment "10 min left", then "time is up" | Warning is Info; time-is-up stays with Switch to next / +15 min / Mark done; based on *tracked* time | ✔ | |
| 6.4 | Don't start a planned task for 5+ minutes | One drift nudge per segment, Start / Skip | ✔ | |
| 6.5 | Newer reminder for the same subject | Replaces the older one ("in 1 min" replaces "in 10 min") | ✔ | |
| 6.6 | Complete the task / start the timer / end the meeting | The now-moot reminder is withdrawn | ✔ | |
| 6.7 | Suspend the laptop across a reminder, resume | One "While you were away" summary, not a burst | ✔ | |
| 6.8 | Restore a backup or import a file while reminders are on screen | Reminders about the old data are withdrawn | ✔ | |
| 6.9 | Focus window pinned (Always on Top) | Shows the same reminder inline with the same buttons | ✔ | |
| 6.10 | Do Not Disturb on | Banners hidden by GNOME (not bypassed); the list keeps them | – | |

### B7. Fast input and CLI (SPEC §5)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 7.1 | Ctrl+K: `PROJ-123 Fix login #backend !p1 ~2h ^fri @today` | Live preview matches; Enter creates it and plans it today | ✔ | |
| 7.2 | `daybook --quick-add "text"` while running | Opens Quick Add with the text | ✔ | |
| 7.3 | `daybook --status` (running and not running) | One line, nothing on stderr | ✔ | |
| 7.4 | `daybook --background` | No window; stays alive; reminders still work | ✔ | |
| 7.5 | Ctrl+? | Shortcut list; Ctrl+1…7 switch sections | ✔ | |

### B8. Review, standup, timesheet, reports (SPEC §6)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 8.1 | End-of-day review | Done items, time per task, unfinished with Move to tomorrow / Back to inbox / apply to all | ✔ | |
| 8.2 | Standup (also on a Monday) | Yesterday = previous workday; Today; Blockers with reason and days waiting; copy as text and Markdown | ✔ | |
| 8.3 | Timesheet day and week; CSV export | Rows per ticket, rounded per Settings, totals; CSV opens in a spreadsheet; a title starting with `=` is not executed (prefixed with `'`) | ✔ | |
| 8.4 | Weekly report | Hours per project, planned vs actual, tasks completed, meeting vs focus (overlapping meetings not double-counted) | ✔ | |
| 8.5 | Weekly report after moving tasks between days | Shows "Tasks planned" for the week, carried items marked | ✔ | |

### B9. Background and data safety (SPEC §7)

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 9.1 | Close the window | Hides; app keeps running; tray icon (with AppIndicator extension) | ✔ | |
| 9.2 | Click tray / run `daybook` | Same window returns | ✔ | |
| 9.3 | Tray menu | Show, Quick add, Meeting now, Pause/Resume, Quit; tooltip = `--status` line | ✔ | |
| 9.4 | Start at login on → check `~/.config/autostart/` → log out/in | Entry exists; starts with no window | ✔ (entry) | |
| 9.5 | Timer + lock screen longer than the idle threshold | On return: "You were away 14m — Keep / Discard" | ✔ | |
| 9.6 | Preferences → Data → Back Up Now | Backup appears in the list; at most 14 kept | ✔ | |
| 9.7 | Restore a backup | Confirmation with task count; safety copy created; UI stays responsive meanwhile | ✔ | |
| 9.8 | Export Everything, change data, Import the file | Confirmation with count; data replaced | ✔ | |
| 9.9 | Import a broken file (e.g. `project_id` 999) | Nothing changes; message says where | ✔ | |
| 9.10 | Damaged backup file | "Can't read this file", app unharmed | ✔ | |

### B10. UI quality (SPEC A10)

| # | Check | Expected | Auto | You |
|---|---|---|---|---|
| 10.1 | Window at 360 px wide | Layout still works; detail is a full page | ✔ | |
| 10.2 | Light and dark mode | Both look right; only named colours and the accent | ✔ | |
| 10.3 | Keyboard only | Everything reachable; focus visible | partial | |
| 10.4 | Icon-only buttons | Tooltip and screen-reader name | ✔ | |
| 10.5 | Reduce animations in GNOME settings | No animation | ✔ | |
| 10.6 | Empty lists | Each shows a status page with a useful action | ✔ | |
| 10.7 | Click outside a form dialog | It stays open | ✔ | |
| 10.8 | Buttons everywhere | Same size and look; Cancel/OK rows have air above | ✔ | |

### B11. Install, upgrade, uninstall

| # | Steps | Expected | Auto | You |
|---|---|---|---|---|
| 11.1 | `sudo apt install ./daybook_0.1.0_all.deb` on a clean Ubuntu 26.04 | Installs with its dependencies; "Daybook" appears in the app grid | ✔ (fake root) | |
| 11.2 | Run it, create a task, quit | Data in `~/.local/share/daybook/daybook.db` | ✔ | |
| 11.3 | `sudo apt remove daybook` | App gone; your data stays | ✔ | |
| 11.4 | `scripts/dev-uninstall.sh` (dev install) | App and login entry gone; data stays | ✔ | |

---

## Reporting a failure

Note the row number, what you did, what you saw, and attach the end of
`~/.local/state/daybook/daybook.log`. Run `daybook --debug` from a terminal to see the log live.
