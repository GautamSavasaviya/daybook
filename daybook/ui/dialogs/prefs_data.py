"""The Data page of Preferences: backups and restore (D-036), and JSON/CSV export and
import (D-026). Anything that replaces data says what it will do and saves a safety copy
first."""

from collections.abc import Callable
from gettext import gettext as _
from gettext import ngettext
from pathlib import Path

from gi.repository import Adw, Gio, GLib, Gtk

from daybook.core.backup import BackupError, BackupFile
from daybook.core.errors import DaybookError
from daybook.core.exchange import ImportRefusedError
from daybook.core.timeutil import fmt_day
from daybook.services.context import AppContext
from daybook.services.events import Changed, Entity, Subscription
from daybook.services.safe import safe_callback
from daybook.ui.export import open_text, save_text
from daybook.ui.format import clock_format
from daybook.ui.widgets.buttons import Kind, button_bar, text_button

ShowToast = Callable[[str], None]


def _size(num: int) -> str:
    return f"{num / 1024:.0f} KB" if num < 1024 * 1024 else f"{num / 1024 / 1024:.1f} MB"


class DataPage(Adw.PreferencesPage):
    def __init__(self, context: AppContext, toast: ShowToast) -> None:
        super().__init__(title=_("Data"), icon_name="drive-harddisk-symbolic")
        self._ctx = context
        self._toast = toast
        self._rows: list[Gtk.Widget] = []
        self._subscriptions: list[Subscription] = []

        self._status = Adw.ActionRow(title=_("Backups"), subtitle_lines=0)
        backups = Adw.PreferencesGroup(
            title=_("Backups"),
            description=_(
                "Daybook backs up your data once a day and keeps the last 14. A safety copy "
                "is also kept before every restore or import."
            ),
        )
        backups.add(self._status)
        backups.add(
            button_bar(
                text_button(_("Back Up Now"), self._on_backup_now, kind=Kind.SUGGESTED),
                text_button(_("Open Backups Folder"), self._on_open_folder),
            )
        )
        self.add(backups)
        self._list = Adw.PreferencesGroup(title=_("Restore"))
        self.add(self._list)

        exchange = Adw.PreferencesGroup(
            title=_("Export and Import"),
            description=_(
                "Export everything as a file you can keep or move to another computer. "
                "Importing replaces all your current data."
            ),
        )
        exchange.add(
            button_bar(
                text_button(_("Export Everything…"), self._on_export),
                text_button(_("Import…"), self._on_import),
                text_button(_("Export Time Entries (CSV)…"), self._on_csv),
            )
        )
        self.add(exchange)

        self._subscriptions.append(context.bus.subscribe(Changed, self._on_changed))
        self.connect("destroy", self._on_destroy)
        self._refill()

    # Listing ----------------------------------------------------------------------------

    def _refill(self) -> None:
        service = self._ctx.backup
        last = service.last_backup_date()
        today = self._ctx.tasks.today()
        self._status.set_title(
            _("Last backup: {day}").format(day=fmt_day(last, today)) if last else _("No backup yet")
        )
        self._status.set_subtitle(GLib.markup_escape_text(str(service.directory)))
        for row in self._rows:
            self._list.remove(row)
        self._rows.clear()
        found = service.backups()
        if not found:
            row = Adw.ActionRow(title=_("No backups to restore"), activatable=False)
            self._list.add(row)
            self._rows.append(row)
            return
        tz = self._ctx.clock.local_tz()
        fmt = clock_format()
        for item in found:
            local = item.made_at.astimezone(tz)
            title = _("{day}, {time}").format(
                day=fmt_day(local.date(), today).capitalize(), time=fmt.time(item.made_at, tz)
            )
            bits = [_size(item.size)]
            if item.safety:
                bits.append(_("safety copy"))
            row = Adw.ActionRow(title=title, subtitle=" · ".join(bits), use_markup=False)
            restore = text_button(_("Restore…"), self._restore_handler(item))
            row.add_suffix(restore)
            self._list.add(row)
            self._rows.append(row)

    # Actions ----------------------------------------------------------------------------

    @safe_callback
    def _on_backup_now(self, _button: Gtk.Button) -> None:
        try:
            made = self._ctx.backup.backup_now()
        except DaybookError as error:
            self._toast(str(error))
            return
        self._toast(_("Backed up as {name}").format(name=made.name))
        self._refill()

    @safe_callback
    def _on_open_folder(self, _button: Gtk.Button) -> None:
        folder = self._ctx.backup.directory
        folder.mkdir(parents=True, exist_ok=True)
        launcher = Gtk.FileLauncher.new(Gio.File.new_for_path(str(folder)))
        root = self.get_root()
        launcher.launch(root if isinstance(root, Gtk.Window) else None, None, None)

    def _restore_handler(self, item: BackupFile) -> Callable[[Gtk.Button], None]:
        def clicked(_button: Gtk.Button) -> None:
            self._on_restore(item)

        return safe_callback(clicked)

    def _on_restore(self, item: BackupFile) -> None:
        try:
            checked = self._ctx.backup.check(item.path)
        except BackupError as error:
            self._toast(error.message if hasattr(error, "message") else str(error))
            return
        body = ngettext(
            "This backup has {n} task. Your current data is replaced by it, after a "
            "safety copy of it is saved, so you can come back.",
            "This backup has {n} tasks. Your current data is replaced by it, after a "
            "safety copy of it is saved, so you can come back.",
            checked.tasks,
        ).format(n=checked.tasks)
        self._confirm(
            _("Restore This Backup?"),
            body,
            _("_Restore"),
            lambda: self._restore(item.path),
        )

    def _restore(self, path: Path) -> None:
        try:
            self._ctx.backup.restore(path)
        except DaybookError as error:
            self._toast(str(error))
            return
        self._toast(_("Restored {name}").format(name=path.name))

    @safe_callback
    def _on_export(self, _button: Gtk.Button) -> None:
        stamp = self._ctx.tasks.today().isoformat()
        save_text(
            self,
            self._ctx,
            self._ctx.backup.export_json(),
            filename=f"daybook-export-{stamp}.json",
            title=_("Export Everything"),
            kind="json",
        )

    @safe_callback
    def _on_csv(self, _button: Gtk.Button) -> None:
        stamp = self._ctx.tasks.today().isoformat()
        save_text(
            self,
            self._ctx,
            self._ctx.backup.time_entries_csv(),
            filename=f"daybook-time-entries-{stamp}.csv",
            title=_("Export Time Entries"),
            kind="csv",
        )

    @safe_callback
    def _on_import(self, _button: Gtk.Button) -> None:
        open_text(self, self._ctx, title=_("Import"), kind="json", on_text=self._import_text)

    def _import_text(self, text: str) -> None:
        # Check the whole file first, and say what is wrong before asking anything.
        try:
            counts = self._ctx.backup.check_import(text)
        except ImportRefusedError as error:
            self._explain_refusal(error)
            return
        body = ngettext(
            "The file has {n} task. All your current data is replaced by it, after a safety "
            "copy of it is saved, so you can come back.",
            "The file has {n} tasks. All your current data is replaced by it, after a safety "
            "copy of it is saved, so you can come back.",
            counts["task"],
        ).format(n=counts["task"])
        self._confirm(
            _("Import and Replace Everything?"), body, _("_Import"), lambda: self._import(text)
        )

    def _import(self, text: str) -> None:
        try:
            self._ctx.backup.import_json(text)
        except (ImportRefusedError, BackupError) as error:
            self._toast(str(error))
            return
        self._toast(_("Imported. Your data was replaced."))

    def _explain_refusal(self, error: ImportRefusedError) -> None:
        dialog = Adw.AlertDialog(
            heading=_("This File Can't Be Imported"),
            body=_("Nothing was changed. What's wrong:\n\n{problems}").format(
                problems="\n".join(f"• {p}" for p in error.problems)
            ),
        )
        dialog.add_response("ok", _("_OK"))
        dialog.set_default_response("ok")
        dialog.present(self)

    def _confirm(self, heading: str, body: str, label: str, action: Callable[[], None]) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", _("_Cancel"))
        dialog.add_response("go", label)
        dialog.set_response_appearance("go", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")

        def on_response(_dialog: Adw.AlertDialog, response: str) -> None:
            if response == "go":
                action()

        dialog.connect("response", safe_callback(on_response))
        dialog.present(self)

    # Events -----------------------------------------------------------------------------

    def _on_changed(self, event: Changed) -> None:
        if event.entity == Entity.SETTINGS:
            GLib.idle_add(self._refill_idle)

    @safe_callback
    def _refill_idle(self) -> bool:
        self._refill()
        return GLib.SOURCE_REMOVE

    @safe_callback
    def _on_destroy(self, _widget: Gtk.Widget) -> None:
        for subscription in self._subscriptions:
            subscription.unsubscribe()
        self._subscriptions.clear()
