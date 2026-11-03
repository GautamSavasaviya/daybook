"""Turn unexpected exceptions into a log entry plus a toast instead of a crash."""

import functools
import logging
from collections.abc import Callable
from gettext import gettext as _

log = logging.getLogger(__name__)

ErrorReporter = Callable[[BaseException], None]

_reporter: ErrorReporter | None = None


def error_message() -> str:
    return _("Something went wrong — details in log")


def set_error_reporter(reporter: ErrorReporter | None) -> None:
    """Install the function that tells the user about an unexpected error (the app's toast)."""
    global _reporter  # noqa: PLW0603 — one process-wide reporter, set once at startup
    _reporter = reporter


def report_error(exc: BaseException) -> None:
    """Log `exc` with its traceback and notify the user. Never raises."""
    log.error("unexpected error", exc_info=exc)
    if _reporter is None:
        return
    try:
        _reporter(exc)
    except Exception:
        log.exception("the error reporter itself failed")


def safe_callback[**P, R](fn: Callable[P, R]) -> Callable[P, R | None]:
    """Wrap a signal handler or GLib callback so an exception is reported, not propagated.

    On failure the wrapper returns None, which also removes a GLib source (falsy result).
    """

    @functools.wraps(fn)
    def wrapper(*args: P.args, **kwargs: P.kwargs) -> R | None:
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            report_error(exc)
            return None

    return wrapper
