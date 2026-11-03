"""Exception types shared by the core and service layers."""


class DaybookError(Exception):
    """Base class for expected, user-reportable failures."""


class ValidationError(DaybookError):
    """A value failed validation. `field` names the form field the message belongs to."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(f"{field}: {message}")
        self.field = field
        self.message = message


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
