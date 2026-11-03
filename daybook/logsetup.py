"""Logging and the global exception hooks.

Logs rotate at 5 x 1 MB in the state directory. ``--debug`` also logs to the terminal.
Uncaught exceptions — including those raised inside GLib callbacks, which PyGObject routes
through `sys.excepthook` — are logged with their traceback and reported to the UI, so the
app never fails silently (SPEC, Architecture).
"""

import logging
import sys
import threading
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 4  # plus the active file = 5 files
_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"

log = logging.getLogger("daybook")


def setup_logging(log_file: Path, *, debug: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    file_handler = RotatingFileHandler(
        log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)

    if debug:
        console = logging.StreamHandler(sys.stderr)
        console.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console)
    # Without --debug the terminal stays quiet; everything still reaches the log file.
    logging.captureWarnings(capture=True)


ErrorCallback = Callable[[BaseException], None]


def install_excepthooks(on_error: ErrorCallback) -> None:
    """Log every uncaught exception and pass it to `on_error` (which must not raise)."""

    def report(exc: BaseException) -> None:
        try:
            on_error(exc)
        except Exception:
            log.exception("error while reporting an uncaught exception")

    def excepthook(
        exc_type: type[BaseException], exc: BaseException, tb: TracebackType | None
    ) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("uncaught exception", exc_info=(exc_type, exc, tb))
        report(exc)

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_value is None:
            return
        log.critical(
            "uncaught exception in thread %s",
            args.thread.name if args.thread else "?",
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
        report(args.exc_value)

    sys.excepthook = excepthook
    threading.excepthook = thread_hook
