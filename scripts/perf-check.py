#!/usr/bin/env python3
"""Performance check (PLAN Phase 8): seed 5,000 tasks and 50,000 time entries, then time
what the app does with them.

Operations that run on the GTK main thread must stay under 100 ms (CLAUDE.md); the ones that
run on the worker thread are only held to a looser limit. Run with the project venv:

    .venv/bin/python scripts/perf-check.py            # prints a table, exit 1 on a miss
    .venv/bin/python scripts/perf-check.py --tasks 500 --entries 5000   # a quick run

It never touches your real data: everything happens in a temporary directory.
"""

import argparse
import random
import sqlite3
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from daybook.core import timeutil
from daybook.core.clock import ManualClock
from daybook.core.repo import tasks as tasks_repo
from daybook.core.tasklists import ListKind, TaskQuery
from daybook.paths import Paths
from daybook.services.context import AppContext

MAIN_BUDGET_MS = 100.0
# Worker jobs are bulk reads or whole-database copies; a short message while they run
# is enough, so they get a looser limit.
WORKER_BUDGET_MS = 2500.0
TZ = ZoneInfo("Europe/Berlin")
NOW = datetime(2026, 11, 4, 11, 0, tzinfo=TZ).astimezone(UTC)


@dataclass
class Result:
    name: str
    thread: str  # "main" or "worker"
    ms: float

    @property
    def budget(self) -> float:
        return MAIN_BUDGET_MS if self.thread == "main" else WORKER_BUDGET_MS

    @property
    def ok(self) -> bool:
        return self.ms <= self.budget


def seed(db_file: Path, task_count: int, entry_count: int) -> None:  # noqa: PLR0912
    """Bulk-insert plausible data with plain SQL (the app would take minutes to do it
    one transaction at a time). The schema's triggers and constraints still apply."""
    rng = random.Random(7)
    conn = sqlite3.connect(db_file)
    conn.execute("PRAGMA foreign_keys = ON")
    stamp = timeutil.to_db(NOW - timedelta(days=800))
    with conn:
        for index in range(1, 21):
            prefix = f"P{index:02d}" if index <= 12 else None
            conn.execute(
                "INSERT INTO project (name, color, key_prefix, created_at, sort_order) "
                "VALUES (?, ?, ?, ?, ?)",
                (f"Project {index}", "blue", prefix, stamp, index),
            )
        tags = [f"tag{n}" for n in range(60)]
        conn.executemany("INSERT INTO tag (name) VALUES (?)", [(t,) for t in tags])
        people = ["Rahul", "Priya", "Sam", "Alex", "Mei", None, None, None]
        statuses = ["done"] * 70 + ["todo"] * 15 + ["in_progress"] * 10 + ["on_hold"] * 5
        today = NOW.astimezone(TZ).date()
        for n in range(1, task_count + 1):
            status = rng.choice(statuses)
            project = rng.randint(2, 21)
            created = NOW - timedelta(days=rng.randint(1, 790))
            completed = (
                timeutil.to_db(created + timedelta(days=rng.randint(0, 20)))
                if status == "done"
                else None
            )
            hold = (
                ("blocked", "waiting for QA", timeutil.to_db(created))
                if status == "on_hold"
                else (None, None, None)
            )
            due = (
                (today + timedelta(days=rng.randint(-20, 40))).isoformat()
                if rng.random() < 0.4
                else None
            )
            ticket = f"P{project - 1:02d}-{n}" if project <= 13 and rng.random() < 0.8 else None
            conn.execute(
                "INSERT INTO task (project_id, title, ticket_key, requested_by, priority, status, "
                "hold_reason, hold_note, hold_since, estimate_min, due_date, notes, created_at, "
                "updated_at, completed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    project,
                    f"Task number {n}: {rng.choice(['fix', 'write', 'review', 'deploy', 'plan'])} "
                    f"{rng.choice(['login', 'report', 'API', 'docs', 'build', 'search'])} thing",
                    ticket,
                    rng.choice(people),
                    rng.randint(1, 4),
                    status,
                    *hold,
                    rng.choice([None, 30, 60, 120, 240]),
                    due,
                    "Some notes about this task. " * rng.randint(0, 6),
                    timeutil.to_db(created),
                    timeutil.to_db(created),
                    completed,
                ),
            )
        for n in range(1, task_count + 1):
            for tag in rng.sample(range(1, 61), rng.randint(0, 3)):
                conn.execute("INSERT INTO task_tag (task_id, tag_id) VALUES (?, ?)", (n, tag))
            for position in range(rng.randint(0, 3)):
                conn.execute(
                    "INSERT INTO checklist_item (task_id, text, done, position) "
                    "VALUES (?, ?, ?, ?)",
                    (n, f"step {position}", rng.randint(0, 1), position),
                )
        # Time entries: back to back through the working days, ending before `now`.
        per_day = max(1, entry_count // 700)
        written = 0
        day = today
        while written < entry_count:
            day -= timedelta(days=1)
            if day.weekday() >= 5:
                continue
            clock = datetime(day.year, day.month, day.day, 8, 0, tzinfo=TZ)
            for _ in range(per_day):
                if written >= entry_count:
                    break
                length = timedelta(minutes=rng.randint(2, 9))
                start = clock.astimezone(UTC)
                end = (clock + length).astimezone(UTC)
                conn.execute(
                    "INSERT INTO time_entry (task_id, start_utc, end_utc, note, source) "
                    "VALUES (?, ?, ?, '', ?)",
                    (
                        rng.randint(1, task_count),
                        timeutil.to_db(start),
                        timeutil.to_db(end),
                        rng.choice(["timer", "manual"]),
                    ),
                )
                clock += length + timedelta(minutes=rng.randint(0, 2))
                written += 1
        # Today's plan, and a plan history.
        open_tasks = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM task WHERE status IN ('todo', 'in_progress') LIMIT 400"
            )
        ]
        for offset in range(0, 90):
            plan_day = today - timedelta(days=offset)
            if plan_day.weekday() >= 5:
                continue
            for position, task in enumerate(rng.sample(open_tasks, min(8, len(open_tasks)))):
                conn.execute(
                    "INSERT INTO day_plan_item "
                    "(plan_date, task_id, allotted_min, position, carry_count) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        plan_day.isoformat(),
                        task,
                        rng.choice([15, 30, 60, 120, None]),
                        position,
                        rng.randint(0, 3),
                    ),
                )
        for n in range(40):
            conn.execute(
                "INSERT INTO meeting (title, start_utc, duration_min, tzid, link, recurrence, "
                "reminder_offsets, created_at, updated_at) "
                "VALUES (?, ?, ?, 'Europe/Berlin', NULL, ?, '[10, 1]', ?, ?)",
                (
                    f"Meeting {n}",
                    timeutil.to_db(NOW - timedelta(days=rng.randint(10, 300), hours=n % 5)),
                    rng.choice([15, 30, 60]),
                    '{"freq": "weekdays", "interval": 1, "days": []}' if n % 2 else None,
                    stamp,
                    stamp,
                ),
            )
    conn.close()


def timed(
    results: list[Result], name: str, thread: str, fn: Callable[[], object], repeat: int = 3
) -> object:
    """Best of `repeat` runs, so one slow moment of the machine doesn't count."""
    best = float("inf")
    value: object = None
    for _ in range(repeat):
        start = time.perf_counter()
        value = fn()
        best = min(best, (time.perf_counter() - start) * 1000)
    results.append(Result(name, thread, best))
    return value


def run(task_count: int, entry_count: int) -> list[Result]:
    results: list[Result] = []
    with tempfile.TemporaryDirectory(prefix="daybook-perf-") as folder:
        root = Path(folder)
        paths = Paths(data_dir=root / "data", log_dir=root / "logs", config_dir=root / "config")
        clock = ManualClock(NOW, tz=TZ)
        context = AppContext.open(paths, clock, lambda callback: callback())
        context.conn.close()
        start = time.perf_counter()
        seed(paths.db_file, task_count, entry_count)
        print(
            f"seeded {task_count} tasks, {entry_count} time entries "
            f"in {time.perf_counter() - start:.1f}s"
        )
        context.worker.shutdown()
        context = AppContext.open(paths, clock, lambda callback: callback())
        today = context.tasks.today()
        conn = context.conn
        # What a view does on the worker thread.
        for name, kind in (
            ("list: Inbox", ListKind.INBOX),
            ("list: Upcoming", ListKind.UPCOMING),
            ("list: Done", ListKind.DONE),
            ("list: a project", ListKind.PROJECT),
        ):
            project = 5 if kind == ListKind.PROJECT else None
            query = TaskQuery(kind, today, project_id=project)
            timed(results, name, "worker", lambda q=query: tasks_repo.list_tasks(conn, q))
        timed(
            results,
            "search: 'login' (title, ticket, notes)",
            "worker",
            lambda: tasks_repo.list_tasks(
                conn, TaskQuery(ListKind.SEARCH, today, text="login build")
            ),
        )
        horizon = today + timedelta(days=14)
        timed(results, "sidebar counts", "worker", lambda: tasks_repo.counts(conn, today, horizon))
        week = context.reports.week_of(today)
        timed(results, "weekly report (computed)", "worker", lambda: _weekly(context, week))
        # Everything the main thread does on a change or a tick.
        timed(results, "today: day plan + schedule", "main", lambda: context.plan.day_plan(today))
        timed(results, "today: timeline runs", "main", lambda: context.timer.day_runs(today))
        timed(results, "plan: today's entries", "main", lambda: context.plan.entries(today))
        timed(results, "meetings: two weeks", "main", lambda: _meetings(context, today))
        timed(results, "timesheet: week", "main", lambda: context.reports.timesheet(week))
        timed(results, "timesheet: day", "main", lambda: context.reports.timesheet((today,)))
        timed(results, "standup", "main", context.reports.standup)
        timed(results, "reminder snapshot + compute", "main", lambda: _reminders(context))
        timed(results, "tray status", "main", lambda: _tray(context))
        timed(results, "open a task: get + tracked + log", "main", lambda: _detail(context))
        timed(
            results,
            "create a task (quick add)",
            "main",
            lambda: context.tasks.quick_add("Perf task P01-9999 ~1h @today"),
            repeat=1,
        )
        some_open = tasks_repo.list_tasks(conn, TaskQuery(ListKind.INBOX, today))[0].task.id
        timed(results, "timer: start + pause", "main", lambda: _timer(context, some_open))
        timed(
            results, "complete a task", "main", lambda: context.tasks.complete(some_open), repeat=1
        )
        # Data safety: all of it runs on the worker, so the main loop stays free.
        backup_path = context.backup.backup_now().path
        timed(
            results,
            "backup (daily)",
            "worker",
            lambda: _await(context, context.backup.check, backup_path),
            repeat=1,
        )
        export = _await(context, context.backup.export_json)
        timed(
            results,
            "export JSON",
            "worker",
            lambda: _await(context, context.backup.export_json),
            repeat=1,
        )
        timed(
            results,
            "time entries CSV",
            "worker",
            lambda: _await(context, context.backup.time_entries_csv),
            repeat=1,
        )
        timed(
            results,
            "check an import (validate the whole file)",
            "worker",
            lambda: _await(context, context.backup.check_import, export),
            repeat=1,
        )
        newest = context.backup.backups()[0].path
        timed(
            results,
            "restore from a backup",
            "worker",
            lambda: _await(context, context.backup.restore, newest),
            repeat=1,
        )
        timed(
            results,
            "import (replace everything)",
            "worker",
            lambda: _await(context, context.backup.import_json, export),
            repeat=1,
        )
        stall = _during_restore(context, newest)
        results.append(Result("a main-thread read while a restore runs", "main", stall * 1000))
        context.close()
    return results


def _await(context: AppContext, method: Callable[..., object], *args: object) -> object:
    """Run an asynchronous service call to completion and return its result."""
    done: list[object] = []
    failed: list[BaseException] = []
    future = method(*args, done.append, failed.append)  # type: ignore[misc]
    future.result(timeout=120)  # type: ignore[attr-defined]
    del context
    if failed:
        raise failed[0]
    return done[0] if done else None


def _during_restore(context: AppContext, path: Path) -> float:
    """Seconds a main-thread read takes while the worker is replacing the data: it must not
    wait for the restore to finish (WAL lets readers through)."""
    future = context.backup.restore(path, lambda _checked: None, lambda _error: None)
    start = time.perf_counter()
    context.plan.day_plan(context.tasks.today())
    elapsed = time.perf_counter() - start
    future.result(timeout=120)
    return elapsed


def _weekly(context: AppContext, week: tuple[date, ...]) -> object:
    done: list[object] = []
    context.reports.weekly(week, done.append).result(timeout=60)
    return done


def _meetings(context: AppContext, today: date) -> object:
    tz = context.clock.local_tz()
    start, _ = timeutil.local_day_bounds(today, tz)
    _, end = timeutil.local_day_bounds(today + timedelta(days=13), tz)
    return context.meetings.occurrences(start, end)


def _reminders(context: AppContext) -> object:
    from daybook.core import reminders  # noqa: PLC0415

    now = context.clock.now()
    return reminders.compute(context.reminders.snapshot(now), now)


def _tray(context: AppContext) -> object:
    from daybook.services.status import tray_status  # noqa: PLC0415

    return tray_status(context.conn, context.clock.now(), context.clock.local_tz())


def _detail(context: AppContext) -> object:
    task = context.tasks.get(100)
    context.tasks.tracked(task.id)
    return context.timer.entries(task.id)


def _timer(context: AppContext, task_id: int) -> None:
    context.timer.start(task_id)
    context.clock.advance(timedelta(seconds=30))
    context.timer.pause()
    context.clock.advance(timedelta(minutes=3))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tasks", type=int, default=5000)
    parser.add_argument("--entries", type=int, default=50000)
    args = parser.parse_args()
    results = run(args.tasks, args.entries)
    width = max(len(r.name) for r in results)
    print(f"\n{'operation':<{width}}  thread   budget      time")
    misses = 0
    for r in results:
        flag = "ok" if r.ok else "SLOW"
        misses += not r.ok
        print(f"{r.name:<{width}}  {r.thread:<7}  {r.budget:>5.0f} ms  {r.ms:>7.1f} ms  {flag}")
    print(f"\n{len(results) - misses} within budget, {misses} over")
    return 1 if misses else 0


if __name__ == "__main__":
    raise SystemExit(main())
