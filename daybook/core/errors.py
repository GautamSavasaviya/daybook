"""Exception types shared by the core and service layers."""

from datetime import datetime


class DaybookError(Exception):
    """Base class for expected, user-reportable failures."""


class ValidationError(DaybookError):
    """A value failed validation. `field` names the form field the message belongs to."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


class OverlapError(ValidationError):
    """Time would overlap time already tracked: an entry (`entry_id`) or the running
    timer (`entry_id` None). The UI names what it overlaps."""

    def __init__(
        self,
        field: str,
        message: str,
        *,
        start: datetime,
        end: datetime,
        task_id: int,
        entry_id: int | None,
    ) -> None:
        super().__init__(field, message)
        self.start = start
        self.end = end
        self.task_id = task_id
        self.entry_id = entry_id


class SchemaTooNewError(DaybookError):
    """The database was written by a newer Daybook; it is left untouched."""

    def __init__(self, found: int, supported: int) -> None:
        super().__init__(
            f"database schema version {found} is newer than this app supports ({supported})"
        )
        self.found = found
        self.supported = supported


class MigrationError(DaybookError):
    """A schema migration failed and was rolled back."""


class ChangedSinceError(DaybookError):
    """An Undo was refused because the row changed after the action it would undo."""


class NotFoundError(DaybookError):
    """A row the caller referred to no longer exists (deleted, purged or never created)."""

    def __init__(self, entity: str, entity_id: int) -> None:
        super().__init__(f"{entity} {entity_id} not found")
        self.entity = entity
        self.entity_id = entity_id
