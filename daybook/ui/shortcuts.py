"""The keyboard shortcuts dialog (Ctrl+?, SPEC §5)."""

from gettext import gettext as _

from gi.repository import Adw

from daybook.ui.sections import sections


def shortcuts_dialog() -> Adw.ShortcutsDialog:
    dialog = Adw.ShortcutsDialog()
    groups: list[tuple[str, list[tuple[str, str]]]] = [
        (
            _("General"),
            [
                (_("Quick add"), "<Control>k"),
                (_("New task"), "<Control>n"),
                (_("Search"), "<Control>f"),
                (_("Meeting now"), "<Control><Shift>m"),
                (_("Preferences"), "<Control>comma"),
                (_("Keyboard shortcuts"), "<Control>question"),
                (_("Close window"), "<Control>w"),
                (_("Quit"), "<Control>q"),
            ],
        ),
        (
            _("Go To"),
            [
                (section.title, f"<Control>{index}")
                for index, section in enumerate(sections(), start=1)
            ],
        ),
        (
            _("Tasks and Timer"),
            [
                (_("Start or pause the timer on the selected task"), "space"),
                (_("Mark the selected task done"), "<Control>Return"),
                (_("Move a plan item up or down"), "<Alt>Up <Alt>Down"),
                (_("Close the task panel"), "Escape"),
            ],
        ),
        (
            _("Focus Window"),
            [
                (_("Pause or resume"), "space"),
                (_("Mark done"), "<Control>Return"),
                (_("Close"), "<Control>w"),
            ],
        ),
    ]
    for title, items in groups:
        section = Adw.ShortcutsSection.new(title)
        for label, accelerator in items:
            section.add(Adw.ShortcutsItem.new(label, accelerator))
        dialog.add(section)
    return dialog
