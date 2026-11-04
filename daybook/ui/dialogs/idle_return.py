"""You were away (D-042): the timer kept running while the user was gone. Keep that time,
or throw it away (a split of the segment, so it never overlaps anything)."""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core.errors import NotFoundError
from daybook.core.timeutil import fmt_duration
from daybook.services.context import AppContext
from daybook.services.events import AwayDetected
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.format import clock_format


def ask_away(parent: Gtk.Widget, context: AppContext, away: AwayDetected) -> None:
    minutes = int((away.end - away.start).total_seconds() // 60)
    try:
        task = context.tasks.get(away.task_id)
        name = task.ticket_key or f"“{short(task.title)}”"
    except NotFoundError:
        name = _("a deleted task")
    tz = context.clock.local_tz()
    fmt = clock_format()
    length = fmt_duration(minutes)
    dialog = Adw.AlertDialog(
        heading=_("You Were Away {length}").format(length=length),
        body=_(
            "The timer kept running on {task} from {start} to {end}. Keep that time, or "
            "discard it and carry on from now?"
        ).format(task=name, start=fmt.time(away.start, tz), end=fmt.time(away.end, tz)),
    )
    dialog.add_response("discard", _("_Discard {length}").format(length=length))
    dialog.add_response("keep", _("_Keep"))
    dialog.set_response_appearance("discard", Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_default_response("keep")
    dialog.set_close_response("keep")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "discard":
            context.timer.discard_away(away.start)

    dialog.connect("response", safe_callback(on_response))
    dialog.present(parent)
