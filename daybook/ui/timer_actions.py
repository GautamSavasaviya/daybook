"""Starting and toggling the timer from any view, with the on-hold question (D-046)."""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core import hold as hold_rules
from daybook.core.errors import NotFoundError, ValidationError
from daybook.services.context import AppContext
from daybook.services.events import Toast
from daybook.services.safe import safe_callback
from daybook.services.text import short


def start_timer(parent: Gtk.Widget, context: AppContext, task_id: int) -> None:
    """Start timing a task. An on-hold task is resumed only after the user agrees."""
    hold = context.timer.hold_to_confirm(task_id)
    if hold is None:
        _run(context, task_id)
        return
    task = context.tasks.get(task_id)
    name = task.ticket_key or f"“{short(task.title)}”"
    reason = hold_rules.reason_label(hold.reason).lower()
    dialog = Adw.AlertDialog(
        heading=_("Resume {name}?").format(name=name),
        body=_("It is on hold ({reason}). Starting the timer puts it back in progress.").format(
            reason=reason
        ),
    )
    dialog.add_response("cancel", _("_Cancel"))
    dialog.add_response("resume", _("_Resume and Start"))
    dialog.set_response_appearance("resume", Adw.ResponseAppearance.SUGGESTED)
    dialog.set_default_response("resume")
    dialog.set_close_response("cancel")

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        if response == "resume":
            _run(context, task_id)

    dialog.connect("response", safe_callback(on_response))
    dialog.present(parent)


def toggle_timer(parent: Gtk.Widget, context: AppContext, task_id: int) -> None:
    """Space: pause or resume the task's timer, or start it."""
    state = context.timer.state()
    if state is not None and state.task_id == task_id:
        try:
            context.timer.toggle(task_id)
        except ValidationError as error:
            context.bus.publish(Toast(error.message))
        return
    start_timer(parent, context, task_id)


def _run(context: AppContext, task_id: int) -> None:
    try:
        context.timer.start(task_id)
    except ValidationError as error:
        context.bus.publish(Toast(error.message))
    except NotFoundError:
        context.bus.publish(Toast(_("That task no longer exists")))
