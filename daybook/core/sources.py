"""The interface a PMS plugin (Jira, Redmine, …) will implement. Not implemented in v1.

The SPEC keeps PMS integration out of scope for v1 but asks for the interface now, so the
rest of the app does not have to change when a plugin arrives.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from daybook.core.models import Priority


@dataclass(frozen=True)
class ExternalTask:
    """A ticket as a source reports it. Daybook maps it onto a Task by its key."""

    key: str  # "PROJ-123"
    title: str
    url: str | None = None
    priority: Priority | None = None
    due_date: date | None = None
    estimate_min: int | None = None
    closed: bool = False


@runtime_checkable
class TaskSource(Protocol):
    @property
    def source_id(self) -> str:
        """Stable identifier, e.g. ``"jira:pms.example.com"``."""
        ...

    @property
    def display_name(self) -> str: ...

    def key_prefixes(self) -> Sequence[str]:
        """Ticket prefixes this source owns (``PROJ``), used to pick a source for a key."""
        ...

    def fetch_assigned(self) -> Sequence[ExternalTask]:
        """Tickets assigned to the user. Called from the worker thread; may block."""
        ...

    def fetch(self, key: str) -> ExternalTask | None:
        """One ticket by key, or None if the source doesn't know it."""
        ...
