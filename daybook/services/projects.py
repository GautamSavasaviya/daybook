"""Projects and areas: validate → repository → publish `Changed(PROJECT)`."""

from gettext import gettext as _
from gettext import ngettext

from daybook.core import tickets
from daybook.core.clock import Clock
from daybook.core.db import Connection
from daybook.core.errors import NotFoundError
from daybook.core.models import Project, ProjectDraft, Task
from daybook.core.repo import projects as projects_repo
from daybook.services.events import Changed, Entity, EventBus, Toast
from daybook.services.text import short


class ProjectService:
    def __init__(self, conn: Connection, bus: EventBus, clock: Clock) -> None:
        self._conn = conn
        self._bus = bus
        self._clock = clock

    def all(self, *, include_archived: bool = False) -> list[Project]:
        return projects_repo.list_all(self._conn, include_archived=include_archived)

    def archived(self) -> list[Project]:
        return [p for p in self.all(include_archived=True) if p.archived]

    def get(self, project_id: int) -> Project:
        return projects_repo.get(self._conn, project_id)

    def create(self, draft: ProjectDraft) -> int:
        project_id = projects_repo.create(self._conn, draft, self._clock.now())
        self._changed(project_id)
        return project_id

    def update(self, project_id: int, draft: ProjectDraft) -> None:
        projects_repo.update(self._conn, project_id, draft)
        self._changed(project_id)

    def open_task_count(self, project_id: int) -> int:
        return projects_repo.open_task_count(self._conn, project_id)

    def can_delete(self, project_id: int) -> bool:
        """Every project but General can be deleted (D-116)."""
        return not self.get(project_id).is_general

    def delete_impact(self, project_id: int) -> projects_repo.DeleteImpact:
        return projects_repo.delete_impact(self._conn, project_id)

    def archive(self, project_id: int) -> None:
        """Hide the project and its open tasks from the active views, with Undo (D-070)."""
        project = self.get(project_id)
        projects_repo.set_archived(self._conn, project_id, True)
        self._changed(project_id)
        self._bus.publish(
            Toast(
                _("Archived “{name}”").format(name=short(project.name)),
                undo=lambda: self._undo_archive(project_id),
            )
        )

    def unarchive(self, project_id: int) -> None:
        projects_repo.set_archived(self._conn, project_id, False)
        self._changed(project_id)

    def _undo_archive(self, project_id: int) -> None:
        try:
            self.unarchive(project_id)
        except NotFoundError:
            self._bus.publish(Toast(_("Couldn't undo: the project no longer exists")))

    def delete(self, project_id: int) -> int:
        """Delete the project and its tasks for good. The UI confirms first and says what
        goes (no Undo: D-116). Returns how many tasks were deleted."""
        project = self.get(project_id)
        tasks = projects_repo.delete(self._conn, project_id)
        self._changed(project_id)
        for entity in (Entity.TASK, Entity.PLAN, Entity.TIME_ENTRY, Entity.TIMER):
            self._bus.publish(Changed(entity))
        self._bus.publish(Toast(self._deleted_message(project.name, tasks)))
        return tasks

    @staticmethod
    def _deleted_message(name: str, tasks: int) -> str:
        if not tasks:
            return _("Deleted “{name}”").format(name=short(name))
        return ngettext(
            "Deleted “{name}” and its {n} task", "Deleted “{name}” and its {n} tasks", tasks
        ).format(name=short(name), n=tasks)

    def project_for_key(self, key: str) -> Project | None:
        return tickets.project_for_key(key, self.all())

    def ticket_url(self, task: Task) -> str:
        """Where "Open ticket" goes for this task, or "" if no project has a URL template."""
        projects = self.all(include_archived=True)
        own = next((p for p in projects if p.id == task.project_id), None)
        return tickets.ticket_url(task.ticket_key, own, projects)

    def _changed(self, project_id: int) -> None:
        # Task lists show project names and colours, so they refresh too.
        self._bus.publish(Changed(Entity.PROJECT, frozenset({project_id})))
