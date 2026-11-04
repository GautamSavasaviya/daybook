"""First-run setup (SPEC §7). Everything is checked before anything is written."""

import logging
from datetime import timedelta
from gettext import gettext as _

from daybook.core import onboarding
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.models import GENERAL_PROJECT_ID, PlanRequest, Priority, ProjectDraft, TaskDraft
from daybook.core.onboarding import Choices
from daybook.core.repo import app_state
from daybook.core.repo import projects as projects_repo
from daybook.services.projects import ProjectService
from daybook.services.settings import SettingsService
from daybook.services.tasks import TaskService

log = logging.getLogger(__name__)

DONE = "onboarding_done"


class OnboardingService:
    def __init__(
        self,
        conn: Connection,
        clock: Clock,
        *,
        settings: SettingsService,
        projects: ProjectService,
        tasks: TaskService,
    ) -> None:
        self._conn = conn
        self._clock = clock
        self._settings = settings
        self._projects = projects
        self._tasks = tasks

    def needed(self) -> bool:
        done = app_state.get(self._conn, DONE) is True
        if done:
            return False
        count = self._conn.execute
        tasks = count("SELECT count(*) FROM task").fetchone()[0]
        meetings = count("SELECT count(*) FROM meeting").fetchone()[0]
        projects = len(projects_repo.list_all(self._conn, include_archived=True)) - 1
        if onboarding.needs_onboarding(done, tasks, projects, meetings):
            return True
        app_state.put(self._conn, DONE, True)  # existing data: never ask
        return False

    def skip(self) -> None:
        app_state.put(self._conn, DONE, True)

    def finish(self, choices: Choices) -> None:
        """Apply the choices. Raises ValidationError (writing nothing) if one is invalid."""
        new_settings = onboarding.settings_for(self._settings.current, choices)
        first = onboarding.project_draft(choices)
        areas = onboarding.area_names(choices)
        self._settings.save(new_settings)
        project_id = self._projects.create(first) if first is not None else GENERAL_PROJECT_ID
        for name in areas:
            self._projects.create(ProjectDraft(name))
        if choices.sample_data:
            self._sample_data(project_id)
        app_state.put(self._conn, DONE, True)
        log.info("onboarding finished (sample data: %s)", choices.sample_data)

    def _sample_data(self, project_id: int) -> None:
        plan_day = self._tasks.default_plan_day()
        due = self._tasks.today() + timedelta(days=1)
        for draft in (
            TaskDraft(
                _("Sample: plan your first day"),
                project_id=project_id,
                plan=PlanRequest(plan_day, 30),
                notes=_("Press Space to start the timer on a task. Delete the samples any time."),
            ),
            TaskDraft(
                _("Sample: add a task with Ctrl+K"),
                project_id=project_id,
                priority=Priority.P2,
                due_date=due,
            ),
            TaskDraft(_("Sample: help a colleague"), requested_by="Sam"),
        ):
            self._tasks.create(draft)
