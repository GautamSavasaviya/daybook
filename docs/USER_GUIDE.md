# Daybook user guide

Daybook is a personal work planner for Ubuntu (GNOME). It replaces a paper notebook: you
write down work, plan your day around meetings, time what you do, and get reminded.

## 1. Install

### Option A — from a `.deb` (for colleagues; recommended)

You need the file `daybook_<version>_all.deb` (see [`PACKAGING.md`](PACKAGING.md) for how to
get or build it). Requires **Ubuntu 26.04** (GNOME, Wayland).

```bash
sudo apt install ./daybook_0.1.0_all.deb
```

`apt` installs the dependencies (Python 3.14, GTK 4.22, libadwaita 1.9, python3-gi) itself.
Then open **Daybook** from the app grid, or run `daybook`.

For the **tray icon**, also install GNOME's AppIndicator extension (optional):

```bash
sudo apt install gnome-shell-extension-appindicator
```

and enable it in the *Extensions* app (log out and in once).

### Option B — from the source folder (developers)

```bash
scripts/setup-dev.sh      # once
scripts/dev-install.sh    # installs into ~/.local — no sudo
daybook
```

Daybook must be **installed** (either way) rather than run from the source tree, because
GNOME only delivers notification buttons (Join, Snooze, Mark done…) to installed apps.

## 2. Uninstall

| Installed with | Remove with | Your data |
|---|---|---|
| `.deb` | `sudo apt remove daybook` (or `sudo apt purge daybook`) | kept |
| `dev-install.sh` | `scripts/dev-uninstall.sh` | kept |

Your data is **never** deleted by removal. It lives in `~/.local/share/daybook/`
(the database and `backups/`), the log in `~/.local/state/daybook/daybook.log`. To erase
everything: also delete `~/.local/share/daybook`, `~/.local/state/daybook` and
`~/.config/autostart/io.github.daybook.Daybook.desktop`.

## 3. First start

A welcome dialog asks for your working hours, workdays, lunch, an optional first project
(a ticket prefix like `PROJ` and a link pattern), optional Docs/Demos areas and optional
sample tasks. Everything can be changed later in Preferences (**Ctrl+,**).

## 4. Daily use

### Capture work
- **Ctrl+N** — new task. **Only the title is required.** Everything else is under *More details*.
- **Ctrl+K** — Quick Add, one line:
  `PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today`
  (`#` project/area, `!p1`–`!p4` priority, `~2h` estimate, `^fri` due, `@today` plan it today,
  `+Name` requested by). A preview shows the result before you press Enter.
- From anywhere: `daybook --quick-add` (bind it to a key — see the README).
- Work comes from three places: **PMS tickets** (ticket key), **face-to-face requests**
  (*Requested by*), and **non-project work** (areas such as Docs, Demos, General).

### Plan your day
- **Plan** view: candidates on the left (carry-overs, overdue, due today, P1/P2 in the inbox),
  today's plan on the right. Give each task a time (15m / 30m / 1h / 2h / custom) or **Rest of
  day**. Reorder by dragging or **Alt+↑/↓**.
- The **capacity bar** shows planned vs free time and warns when the plan doesn't fit.
- **Today** shows a timeline of meetings and task segments with a "now" line, and a Now card.
- **Unfinished work carries over automatically** to the next working day, with the remaining time.
- **Move to…** (any task or plan item): Tomorrow, Next working day, a date, or Unplanned.
- **On hold**: choose a reason and an optional follow-up date; it leaves today's plan and comes
  back on that date.

### Time
- **Space** starts/pauses the timer on the selected task; starting another stops the first.
- The timer survives a crash or reboot; on the next start you're asked to keep it, stop at the
  last activity, or discard.
- The **Focus window** is a small always-on-top-able timer with Pause / Done / Next.
- Add or edit time by hand on a task (overlaps are refused).

### Meetings and time off
- **Meetings** view for scheduled meetings (links, reminder offsets, recurrence).
- **Meeting now** (**Ctrl+Shift+M**, tray menu, `daybook --meeting-now`) for an unplanned one.
  The timer pauses; when it ends you're asked "Back to …?" and the plan reflows.
- **Time off** (Preferences → Time Off): full or half days. They aren't working days anywhere.

### Reminders
Three tiers: meeting reminders and "time is up" **stay** until you act; nudges and
"back to task?" show briefly; heads-ups and start/end-of-day show briefly. Test them in
**Preferences → Notifications → Send Test Reminder**. Use GNOME Do Not Disturb as usual.

### Review and reports
- **Reports** → End-of-day review, Standup (Yesterday / Today / Blockers; copy as text or
  Markdown), Timesheet (day/week, rounded as you set; copy or CSV) and the weekly report.

## 5. Running in the background

Closing the window hides it; Daybook keeps running so reminders still arrive. Bring it back with
the tray icon or by running `daybook`. **Quit** (menu, **Ctrl+Q**, tray) really stops it. Turn
this off or enable *Start at login* in Preferences → General.

## 6. Your data and backups

One SQLite file: `~/.local/share/daybook/daybook.db`. A backup is made daily (last 14 kept).
**Preferences → Data**: Back Up Now, Restore… (a safety copy is saved first), Export Everything
(JSON), Import…, Export Time Entries (CSV). To move to another computer: export, install there,
import.

## 7. Command line

```
daybook                       open
daybook --quick-add [text]    Quick Add
daybook --meeting-now         start an unplanned meeting
daybook --background          start without a window
daybook --status              one line: current task, time left
daybook --debug               also log to the terminal
```

## 8. Troubleshooting

| Problem | What to do |
|---|---|
| No notification buttons / "Notifications can't work" | Daybook isn't installed properly; reinstall (see 1). Preferences → Notifications says what's wrong. |
| No tray icon | Install and enable `gnome-shell-extension-appindicator`, then log out and in. |
| Something went wrong toast | See `~/.local/state/daybook/daybook.log`; run `daybook --debug`. |
| Want to start fresh | Quit Daybook, move `~/.local/share/daybook` away, start again. |
| Installer says Python 3.14 not available | Daybook needs Ubuntu 26.04 or newer. |

Keyboard shortcuts: press **Ctrl+?** in the app.
