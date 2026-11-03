"""The "Repeats" choice for tasks (D-016): none, daily, weekdays, weekly or monthly.

Weekly repeats on the due date's weekday, monthly on its day of the month. Rules made
elsewhere with more detail (every 2 weeks…) are shown as written and kept unless changed.
"""

from collections.abc import Callable
from datetime import date
from gettext import gettext as _

from gi.repository import Adw, Gtk

from daybook.core import recurrence as recurrence_rules
from daybook.core.models import Recurrence, RecurrenceFreq
from daybook.services.safe import safe_callback

CHOICES: tuple[RecurrenceFreq | None, ...] = (None, *RecurrenceFreq)


def _label(freq: RecurrenceFreq | None) -> str:
    return {
        None: _("Doesn't repeat"),
        RecurrenceFreq.DAILY: _("Daily"),
        RecurrenceFreq.WEEKDAYS: _("Weekdays (Mon–Fri)"),
        RecurrenceFreq.WEEKLY: _("Weekly"),
        RecurrenceFreq.MONTHLY: _("Monthly"),
    }[freq]


class RepeatRow(Adw.ComboRow):
    def __init__(self, on_change: Callable[[Recurrence | None], None] | None = None) -> None:
        super().__init__(
            title=_("Repeats"),
            model=Gtk.StringList.new([_label(f) for f in CHOICES]),
        )
        self._on_change = on_change
        self._rule: Recurrence | None = None
        self._setting = False
        self.connect("notify::selected", self._on_selected)

    @property
    def rule(self) -> Recurrence | None:
        return self._rule

    def set_rule(self, rule: Recurrence | None, anchor: date) -> None:
        """Show `rule` without reporting a change."""
        self._rule = rule
        self._setting = True
        try:
            self.set_selected(CHOICES.index(rule.freq if rule else None))
        finally:
            self._setting = False
        detailed = rule is not None and (rule.interval > 1 or rule.until or rule.days)
        self.set_subtitle(recurrence_rules.describe(rule, anchor) if detailed else "")

    @safe_callback
    def _on_selected(self, _row: Adw.ComboRow, _param: object) -> None:
        if self._setting:
            return
        freq = CHOICES[self.get_selected()]
        self._rule = Recurrence(freq) if freq is not None else None
        self.set_subtitle(_("Done makes the next one, due on its next date") if freq else "")
        if self._on_change is not None:
            self._on_change(self._rule)
