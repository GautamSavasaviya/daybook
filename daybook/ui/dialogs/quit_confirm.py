"""Quit warns first when a timer is running (SPEC §7)."""

from collections.abc import Callable
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core.errors import NotFoundError
from daybook.services.context import AppContext
from daybook.services.safe import safe_callback
from daybook.services.text import short


def confirm_quit(parent: Gtk.Widget, context: AppContext, quit_now: Callable[[], None]) -> None:
    """Quit now, unless a timer is running: then ask, saying what quitting does to it."""
    state = context.timer.state()
    if state is None or not state.running:
        quit_now()
        return
    try:
        task = context.tasks.get(state.task_id)
        name = task.ticket_key or f"“{short(task.title)}”"
    except NotFoundError:
        name = _("a task")
    dialog = Adw.AlertDialog(
        heading=_("Quit Daybook?"),
        body=_(
            "The timer is running on {task}. Quitting pauses it and keeps the time so far. "
            "Reminders stop until you open Daybook again."
        ).format(task=name),
    )
    dialog.add_response("cancel", _("_Keep Running"))
    dialog.add_response("quit", _("_Quit"))
    dialog.set_response_appearance("quit", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("cancel")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "quit":
            quit_now()

    dialog.connect("response", safe_callback(on_response))
    dialog.present(parent)
