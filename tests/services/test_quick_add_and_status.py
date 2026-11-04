from datetime import timedelta

import pytest

from daybook.core.clock import ManualClock
from daybook.core.errors import ValidationError
from daybook.core.models import PlanRequest, ProjectDraft, TaskDraft, TaskStatus
from daybook.main import offline_status
from daybook.paths import Paths
from daybook.services.context import AppContext
from daybook.services.projects import ProjectService
from daybook.services.status import status_line
from tests.services.test_planning_services import BERLIN, START, TODAY, Queue
from tests.services.test_timer_service import TimerEnv


@pytest.fixture
def env(conn, db_path):
    environment = TimerEnv(conn, db_path)
    yield environment
    environment.worker.shutdown()


def status(env) -> str:
    return status_line(env.conn, env.clock.now(), BERLIN)


def test_quick_add_creates_the_task_and_plans_it(env, conn):
    projects = ProjectService(conn, env.bus, env.clock)
    pms = projects.create(ProjectDraft("PMS", key_prefix="PROJ"))
    task_id = env.tasks.quick_add("PROJ-123 Fix login #backend !p1 ~1h +Rahul_Shah @today")
    task = env.tasks.get(task_id)
    assert (task.title, task.ticket_key, task.project_id) == ("Fix login", "PROJ-123", pms)
    assert task.requested_by == "Rahul Shah"
    assert env.tasks.tags(task_id) == ("backend",)
    assert [(i.plan_date, i.allotted_min) for i in env.tasks.plan_items(task_id)] == [(TODAY, 60)]


def test_quick_add_warns_about_a_ticket_key_in_use_and_refuses_errors(env):
    env.tasks.create(TaskDraft("Earlier", ticket_key="ABC-1"))
    result = env.tasks.parse_quick("ABC-1 Again")
    assert result.ok
    assert result.warnings == ("ABC-1 is already on another task.",)
    with pytest.raises(ValidationError, match="use !p1 to !p4"):
        env.tasks.quick_add("Broken !p9")
    with pytest.raises(ValidationError, match="isn't a working day"):
        env.tasks.quick_add("Weekend @sat")


def test_status_line_for_every_timer_state(env):
    assert status(env) == "No timer running"
    planned = env.tasks.create(
        TaskDraft("Fix login", ticket_key="PROJ-1", plan=PlanRequest(TODAY, 30))
    )
    env.timer.start(planned)
    env.clock.advance(timedelta(minutes=7))
    assert status(env) == "PROJ-1 Fix login · 23m left · running"
    env.clock.advance(timedelta(minutes=30))
    assert status(env) == "PROJ-1 Fix login · 7m over · running"
    env.timer.pause()
    assert status(env) == "PROJ-1 Fix login · 7m over · paused"
    loose = env.tasks.create(TaskDraft("Unplanned"))
    env.timer.start(loose)
    env.clock.advance(timedelta(minutes=12))
    assert status(env) == "Unplanned · 12m today · running"
    env.conn.execute("DELETE FROM task WHERE id = ?", (loose,))
    assert status(env) == "No timer running"


def test_offline_status_reads_without_starting_daybook(tmp_path):
    paths = Paths(data_dir=tmp_path / "data", log_dir=tmp_path / "logs", config_dir=tmp_path / "c")
    clock = ManualClock(START, tz=BERLIN)
    assert offline_status(paths.db_file, clock) == ("No timer running", 0)
    context = AppContext.open(paths, clock, Queue())
    task_id = context.tasks.create(TaskDraft("Write docs"))
    context.timer.start(task_id)
    clock.advance(timedelta(minutes=5))
    context.close()  # a normal quit pauses the timer
    assert offline_status(paths.db_file, clock) == ("Write docs · 5m today · paused", 0)
    paths.db_file.write_bytes(b"not a database")
    line, code = offline_status(paths.db_file, clock)
    assert (line, code) == ("Daybook could not read its data", 1)


def test_quick_added_tasks_are_open(env):
    task_id = env.tasks.quick_add("Simple one")
    assert env.tasks.get(task_id).status == TaskStatus.TO_DO
