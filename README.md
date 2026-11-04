# Daybook

A personal work planner for GNOME that replaces a paper notebook. It holds tickets, face-to-face
requests and non-project work, plans the day around your meetings, carries unfinished work
over, times what you work on, and reminds you at the right moment.

Built with Python 3.14, GTK 4.22, libadwaita 1.9, SQLite and Meson, for Ubuntu 26.04 (GNOME,
Wayland).

## Install and run

Daybook has to be **installed** to work properly: GNOME only delivers notification buttons
(Join, Snooze, Mark done…) to an app whose `.desktop` and D-Bus service files are installed
(D-038). Running it straight from the source tree isn't supported.

```bash
scripts/setup-dev.sh      # once: a venv with ruff, mypy and pytest
scripts/dev-install.sh    # installs to ~/.local
daybook                   # or open "Daybook" from the app grid
scripts/dev-uninstall.sh  # removes it again (your data stays)
```

Your data is in `~/.local/share/daybook/daybook.db` and the log in
`~/.local/state/daybook/daybook.log`.

### Trying it with fake time

A throwaway data folder and fast time make reminders easy to test. With any `DAYBOOK_*`
variable set, Daybook runs as a separate "Daybook (Devel)" app next to the real one:

```bash
DAYBOOK_DATA_DIR=$(mktemp -d) DAYBOOK_TIME_SCALE=60 DAYBOOK_FAKE_NOW=2026-11-03T08:55:00 daybook
```

`DAYBOOK_TIME_SCALE=60` makes one real second one minute.

## Command line

| Command | What it does |
|---|---|
| `daybook` | Open Daybook (or bring its window to the front). |
| `daybook --quick-add [text]` | Open Quick Add, with the text filled in if given. |
| `daybook --meeting-now` | Start an unplanned meeting. |
| `daybook --background` | Start without a window, so reminders still come (used at login). |
| `daybook --status` | Print one line, e.g. `PROJ-123 Fix login · 23m left · running`, then exit. |
| `daybook --debug` | Also write the log to the terminal. |

A second `daybook …` is handed to the Daybook that is already running, so these work from
anywhere. `--status` works even when Daybook isn't running: it reads the saved timer.

## Quick add

Press **Ctrl+K** and type one line; a preview shows what the task will be before you press
Enter.

```
PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today
Prepare demo for client #demos +Rahul_Shah @today=day
```

| Write | Means |
|---|---|
| `PROJ-123` | Ticket key. Its prefix picks the project that uses it. |
| `#name` | Project or area with that name or key prefix; otherwise a tag. |
| `+Rahul_Shah` | Requested by (`_` is a space). Suggestions come from earlier names. |
| `!p1` … `!p4` | Priority. |
| `~2h`, `~30m`, `~1h30m` | Estimate. |
| `^today`, `^tomorrow`, `^fri`, `^25/12`, `^2026-12-25` | Due date. |
| `@today`, `@fri`, `@25/12` | Plan it on that day (for its estimate, or 30 min). |
| `@today=2h`, `@today=day` | …for 2 hours, or for the rest of the day. |
| `\#1` | A backslash keeps the word as text. |

## Keyboard shortcuts

Press **Ctrl+?** in Daybook for the full list. The main ones:

| Keys | Action |
|---|---|
| Ctrl+K | Quick add |
| Ctrl+N | New task |
| Ctrl+F | Search |
| Ctrl+Shift+M | Meeting now |
| Space | Start or pause the timer on the selected task |
| Ctrl+Enter | Mark the selected task done |
| Ctrl+1 … Ctrl+7 | Today, Plan, Inbox, Upcoming, Meetings, Reports, Done |
| Ctrl+, | Preferences |

### A key for Quick Add from anywhere

GNOME doesn't let an app grab a key for the whole desktop, but you can bind one yourself:

1. Open **Settings → Keyboard → View and Customize Shortcuts → Custom Shortcuts**.
2. Click **+** (or **Add Shortcut**).
3. **Name:** `Daybook quick add`. **Command:** `/home/YOUR-USER/.local/bin/daybook --quick-add`
   (use the full path; the command doesn't run in a shell, so `~` isn't expanded).
4. Click **Set Shortcut…** and press the keys you want, for example **Super+K**.

Do the same with `--meeting-now` (for example **Super+M**) to log an emergency meeting in two
key presses.

The same from a terminal:

```bash
KEY=/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/daybook-quick-add/
gsettings set org.gnome.settings-daemon.plugins.media-keys custom-keybindings "['$KEY']"
SCHEMA=org.gnome.settings-daemon.plugins.media-keys.custom-keybinding:$KEY
gsettings set "$SCHEMA" name 'Daybook quick add'
gsettings set "$SCHEMA" command "$HOME/.local/bin/daybook --quick-add"
gsettings set "$SCHEMA" binding '<Super>k'
```

The first `gsettings set` replaces your list of custom shortcuts; if you already have some,
add the new path to the existing list instead (`gsettings get …` shows it).

## Development

```bash
scripts/check.sh    # ruff, mypy, pytest (core coverage ≥ 90 %), packaging checks
```

Requirements are in `docs/SPEC.md`, the design and phase plan in `docs/PLAN.md`, the decisions
behind every default in `docs/DECISIONS.md`, and progress in `docs/PROGRESS.md`.
