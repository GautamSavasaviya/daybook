"""First run (SPEC §7): working hours and days, lunch, a first project, optional areas and
sample data. One screen; Skip keeps the defaults. Either way it doesn't come back."""

from gettext import gettext as _

from gi.repository import Adw

from daybook.core import validation
from daybook.core.onboarding import SUGGESTED_AREAS, Choices
from daybook.services.context import AppContext
from daybook.ui.dialogs.form import FormDialog
from daybook.ui.dialogs.prefs_settings import workdays_row


class OnboardingDialog(FormDialog):
    def __init__(self, context: AppContext) -> None:
        super().__init__(
            title=_("Welcome to Daybook"),
            primary_label=_("_Get Started"),
            cancel_label=_("_Skip"),
            width=520,
            height=720,
        )
        self._ctx = context
        self._finished = False
        defaults = Choices()

        intro = Adw.PreferencesGroup(
            description=_(
                "A few answers set Daybook up for your day. Everything can be changed later "
                "in Preferences."
            )
        )
        self.page.add(intro)

        hours = Adw.PreferencesGroup(title=_("Your Working Day"))
        self._start = Adw.EntryRow(title=_("Start of work (e.g. 9:00)"))
        self._start.set_text("09:00")
        self._end = Adw.EntryRow(title=_("End of work (e.g. 18:00)"))
        self._end.set_text("18:00")
        days, self._days = workdays_row(defaults.workdays)
        self._lunch = Adw.EntryRow(title=_("Lunch starts (e.g. 13:00)"))
        self._lunch.set_text("13:00")
        self._lunch_min = Adw.SpinRow.new_with_range(0, 240, 5)
        self._lunch_min.set_title(_("Lunch Length"))
        self._lunch_min.set_subtitle(_("Minutes; 0 for no lunch"))
        self._lunch_min.set_value(defaults.lunch_minutes)
        for row in (self._start, self._end, days, self._lunch, self._lunch_min):
            hours.add(row)
        self.page.add(hours)
        self.register_field("work_start", self._start)
        self.register_field("work_end", self._end)
        self.register_field("workdays", days)
        self.register_field("lunch_start", self._lunch)
        self.register_field("lunch_minutes", self._lunch_min)

        project = Adw.PreferencesGroup(
            title=_("Your First Project"),
            description=_(
                "Optional. A ticket prefix like PROJ files tickets such as PROJ-123 here "
                "automatically. Leave it all empty to start with just the General area."
            ),
        )
        self._project = Adw.EntryRow(title=_("Project name, e.g. PMS"))
        self._prefix = Adw.EntryRow(title=_("Ticket prefix, e.g. PROJ"))
        self._url = Adw.EntryRow(title=_("Ticket link, e.g. https://pms.example.com/browse/{key}"))
        for row in (self._project, self._prefix, self._url):
            project.add(row)
        self.page.add(project)
        self.register_field("project_name", self._project)
        self.register_field("key_prefix", self._prefix)
        self.register_field("url_template", self._url)

        extras = Adw.PreferencesGroup(title=_("Extras"))
        self._areas: dict[str, Adw.SwitchRow] = {}
        for name in SUGGESTED_AREAS:
            row = Adw.SwitchRow(
                title=_("Add a “{name}” area").format(name=name),
                subtitle=_("For work that isn't a ticket"),
            )
            self._areas[name] = row
            extras.add(row)
        self._sample = Adw.SwitchRow(
            title=_("Add Sample Tasks"),
            subtitle=_("Three tasks to try things on; delete them any time"),
        )
        extras.add(self._sample)
        self.page.add(extras)
        self.connect("closed", self._on_closed)

    def _choices(self) -> Choices:
        return Choices(
            work_start=validation.parse_clock(self._start.get_text(), "work_start"),
            work_end=validation.parse_clock(self._end.get_text(), "work_end"),
            workdays=frozenset(i for i, b in enumerate(self._days) if b.get_active()),
            lunch_start=validation.parse_clock(self._lunch.get_text(), "lunch_start"),
            lunch_minutes=int(self._lunch_min.get_value()),
            project_name=self._project.get_text(),
            key_prefix=self._prefix.get_text(),
            url_template=self._url.get_text(),
            areas=tuple(name for name, row in self._areas.items() if row.get_active()),
            sample_data=self._sample.get_active(),
        )

    def submit(self) -> bool | None:
        self._ctx.onboarding.finish(self._choices())  # a ValidationError shows inline
        self._finished = True
        return None

    def _on_closed(self, _dialog: Adw.Dialog) -> None:
        # Skip, Escape or the close button: keep the defaults and don't ask again.
        if not self._finished:
            self._ctx.onboarding.skip()
