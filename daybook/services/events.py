"""The event bus that keeps every view in sync with the database.

Services publish `Changed` after each committed write; views and the reminder scheduler
subscribe and re-query. Handlers always run on the main thread: a publish from another
thread is handed to `call_soon` (GLib.idle_add in the app).
"""

import logging
import threading
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from daybook.services.safe import report_error

log = logging.getLogger(__name__)

CallSoon = Callable[[Callable[[], None]], None]


class Entity(StrEnum):
    PROJECT = "project"
    TASK = "task"
    MEETING = "meeting"
    PLAN = "plan"
    TIME_ENTRY = "time_entry"
    TIME_OFF = "time_off"
    TIMER = "timer"
    SETTINGS = "settings"
    REMINDER = "reminder"


@dataclass(frozen=True)
class Changed:
    """Rows of `entity` changed. Empty `ids` means "anything may have changed"."""

    entity: Entity
    ids: frozenset[int] = frozenset()


@dataclass(frozen=True)
class Toast:
    """Show a short message; `undo` adds an Undo button that runs it."""

    message: str
    undo: Callable[[], None] | None = None


class Subscription:
    def __init__(self, unsubscribe: Callable[[], None]) -> None:
        self._unsubscribe: Callable[[], None] | None = unsubscribe

    def unsubscribe(self) -> None:
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None


class EventBus:
    def __init__(self, call_soon: CallSoon, main_thread: threading.Thread | None = None) -> None:
        self._call_soon = call_soon
        self._main_thread = main_thread or threading.main_thread()
        self._handlers: defaultdict[type, list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe[E](self, event_type: type[E], handler: Callable[[E], None]) -> Subscription:
        handlers = self._handlers[event_type]
        handlers.append(handler)

        def remove() -> None:
            if handler in handlers:
                handlers.remove(handler)

        return Subscription(remove)

    def publish(self, event: object) -> None:
        if threading.current_thread() is self._main_thread:
            self._dispatch(event)
        else:
            self._call_soon(lambda: self._dispatch(event))

    def _dispatch(self, event: object) -> None:
        for handler in list(self._handlers.get(type(event), ())):
            try:
                handler(event)
            except Exception as exc:
                if isinstance(event, Toast):
                    # Reporting would publish another Toast; avoid a loop and just log.
                    log.exception("toast handler failed")
                else:
                    report_error(exc)
