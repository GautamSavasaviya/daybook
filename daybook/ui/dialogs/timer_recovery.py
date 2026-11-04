"""Timer recovery (SPEC §3): the timer was still running when Daybook last stopped, so it
crashed or the computer lost power. Ask what to do with the time since then."""

from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core.errors import NotFoundError
from daybook.core.timer import RecoveryChoice
from daybook.core.timeutil import fmt_day, fmt_duration
from daybook.services.context import AppContext
from daybook.services.safe import safe_callback
from daybook.services.text import short
from daybook.ui.format import clock_format


def ask_recovery(parent: Gtk.Widget, context: AppContext) -> None:
    recovery = context.timer.pending_recovery()
    if recovery is None:
        return
    try:
        task = context.tasks.get(recovery.task_id)
        name = task.ticket_key or f"“{short(task.title)}”"
    except NotFoundError:
        name = _("a deleted task")
    tz = context.clock.local_tz()
    fmt = clock_format()
    today = context.tasks.today()
    since_day = recovery.since.astimezone(tz).date()
    since = fmt.time(recovery.since, tz)
    if since_day != today:
        since = f"{fmt_day(since_day, today)} {since}"
    last_seen = fmt.time(recovery.last_seen, tz)
    worked = int((recovery.last_seen - recovery.since).total_seconds() // 60)
    dialog = Adw.AlertDialog(
        heading=_("Timer Was Running"),
        body=_(
            "The timer was running for {task} since {since} when Daybook stopped "
            "unexpectedly. Daybook last saw it at {last_seen} ({worked} tracked)."
        ).format(task=name, since=since, last_seen=last_seen, worked=fmt_duration(worked)),
    )
    dialog.add_response(RecoveryChoice.DISCARD.value, _("_Discard"))
    dialog.add_response(
        RecoveryChoice.STOP_AT_LAST_SEEN.value,
        _("_Stop at {time}").format(time=last_seen),
    )
    dialog.add_response(RecoveryChoice.KEEP.value, _("_Keep Running"))
    dialog.set_response_appearance(RecoveryChoice.DISCARD.value, Adw.ResponseAppearance.DESTRUCTIVE)
    dialog.set_response_appearance(
        RecoveryChoice.STOP_AT_LAST_SEEN.value, Adw.ResponseAppearance.SUGGESTED
    )
    dialog.set_default_response(RecoveryChoice.STOP_AT_LAST_SEEN.value)
    # Escape keeps only the time Daybook actually saw (D-094).
    dialog.set_close_response(RecoveryChoice.STOP_AT_LAST_SEEN.value)
    dialog.set_prefer_wide_layout(True)

    def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
        context.timer.recover(RecoveryChoice(response))

    dialog.connect("response", safe_callback(on_response))
    dialog.present(parent)
