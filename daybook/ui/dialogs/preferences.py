"""Preferences (Ctrl+,): every setting, in four pages. General holds working hours,
planning and the background behaviour; the rest are Notifications, Time Off and Data."""

from gettext import gettext as _

from gi.repository import Adw

from daybook.services.context import AppContext
from daybook.ui.dialogs.prefs_data import DataPage
from daybook.ui.dialogs.prefs_settings import GeneralPage, NotificationsPage
from daybook.ui.dialogs.time_off import TimeOffPage

PAGES = ("general", "notifications", "time-off", "data")


class PreferencesDialog(Adw.PreferencesDialog):
    def __init__(self, context: AppContext, app_id: str, page: str = "general") -> None:
        super().__init__(title=_("Preferences"), content_width=720)
        self.set_search_enabled(False)
        general = GeneralPage(context, self.toast, app_id)
        notifications = NotificationsPage(context, self.toast, app_id)
        time_off = TimeOffPage(context)
        data = DataPage(context, self.toast)
        pages = dict(zip(PAGES, (general, notifications, time_off, data), strict=True))
        for widget in pages.values():
            self.add(widget)
        self.set_visible_page(pages.get(page, general))

    def toast(self, message: str) -> None:
        self.add_toast(Adw.Toast(title=message, use_markup=False))
