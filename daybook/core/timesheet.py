"""The timesheet (D-028, D-052): tracked time per ticket and day, rounded the way the PMS
wants it. Pure.

One row per ticket key; a task without one gets its own row, "Area — Title". Each day's
cell is rounded to the nearest increment but never to zero when time was tracked, and the
totals add up the rounded cells, so what is typed into the PMS adds up.
"""

import csv
import io
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta, tzinfo
from gettext import gettext as _

from daybook.core import timeutil, tracking
from daybook.core.exchange import csv_safe
from daybook.core.models import TimeSlice
from daybook.core.timeutil import fmt_day_month, fmt_duration


def round_minutes(seconds: int, step: int) -> int:
    """Minutes for `seconds`, rounded to the nearest `step` (0 = just whole minutes), and
    at least one step for any time at all (D-028)."""
    if seconds <= 0:
        return 0
    minutes = seconds / 60
    unit = step if step > 0 else 1
    return max(unit, int(minutes / unit + 0.5) * unit)


@dataclass
class Row:
    key: str
    label: str
    detail: str  # a ticket row's title
    seconds: dict[date, int] = field(default_factory=dict)
    minutes: dict[date, int] = field(default_factory=dict)

    @property
    def raw_seconds(self) -> int:
        return sum(self.seconds.values())

    @property
    def total_min(self) -> int:
        return sum(self.minutes.values())


@dataclass(frozen=True)
class Timesheet:
    days: tuple[date, ...]
    rows: tuple[Row, ...]
    rounding_min: int

    def day_total_min(self, day: date) -> int:
        return sum(row.minutes.get(day, 0) for row in self.rows)

    @property
    def total_min(self) -> int:
        return sum(row.total_min for row in self.rows)

    @property
    def raw_seconds(self) -> int:
        return sum(row.raw_seconds for row in self.rows)

    @property
    def raw_min(self) -> int:
        return round(self.raw_seconds / 60)

    @property
    def empty(self) -> bool:
        return not self.rows


def build(
    slices: Sequence[TimeSlice], days: Sequence[date], tz: tzinfo, rounding_min: int
) -> Timesheet:
    rows: dict[str, Row] = {}
    for item in slices:
        key = item.ticket_key.upper() if item.ticket_key else f"task:{item.task_id}"
        if key not in rows:
            if item.ticket_key:
                rows[key] = Row(key, item.ticket_key.upper(), item.title)
            else:
                label = _("{area} — {title}").format(area=item.project_name, title=item.title)
                rows[key] = Row(key, label, "")
        row = rows[key]
        for day in days:
            lo, hi = timeutil.local_day_bounds(day, tz)
            seconds = tracking.overlap_s(item.start, item.end, lo, hi)
            if seconds:
                row.seconds[day] = row.seconds.get(day, 0) + seconds
    for row in rows.values():
        row.minutes = {d: round_minutes(s, rounding_min) for d, s in row.seconds.items()}
    # Ticket rows first, then the rest, each in the order of their labels.
    ordered = sorted(
        (row for row in rows.values() if row.raw_seconds),
        key=lambda r: (r.key.startswith("task:"), r.label.casefold()),
    )
    return Timesheet(tuple(days), tuple(ordered), rounding_min)


def hours(minutes: int) -> str:
    return f"{minutes / 60:.2f}"


def _short(day: date) -> str:
    return f"{timeutil.fmt_weekday(day)} {fmt_day_month(day)}"


def to_text(sheet: Timesheet) -> str:
    """Plain text for pasting: tab-separated, one row per ticket."""
    if len(sheet.days) == 1:
        day = sheet.days[0]
        lines = [timeutil.fmt_date(day)]
        for row in sheet.rows:
            lines.append(f"{row.label}\t{fmt_duration(row.minutes.get(day, 0))}")
        lines.append(f"{_('Total')}\t{fmt_duration(sheet.total_min)}")
        return "\n".join(lines) + "\n"
    header = ["", *(_short(d) for d in sheet.days), _("Total")]
    lines = ["\t".join(header)]
    for row in sheet.rows:
        cells = [fmt_duration(row.minutes[d]) if d in row.minutes else "" for d in sheet.days]
        lines.append("\t".join([row.label, *cells, fmt_duration(row.total_min)]))
    totals = [fmt_duration(sheet.day_total_min(d)) for d in sheet.days]
    lines.append("\t".join([_("Total"), *totals, fmt_duration(sheet.total_min)]))
    return "\n".join(lines) + "\n"


def to_csv(sheet: Timesheet) -> str:
    """CSV in decimal hours (1.50), the form timesheet systems take."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(
        [_("Ticket or task"), _("Title"), *(d.isoformat() for d in sheet.days), _("Total hours")]
    )
    for row in sheet.rows:
        cells = [hours(row.minutes[d]) if d in row.minutes else "" for d in sheet.days]
        writer.writerow([csv_safe(row.label), csv_safe(row.detail), *cells, hours(row.total_min)])
    totals = [hours(sheet.day_total_min(d)) for d in sheet.days]
    writer.writerow([_("Total"), "", *totals, hours(sheet.total_min)])
    return out.getvalue()


def week_days(day: date) -> tuple[date, ...]:
    """Monday to Sunday of the week containing `day`."""
    monday = day - timedelta(days=day.weekday())
    return tuple(monday + timedelta(days=i) for i in range(7))


def week_title(days: Sequence[date]) -> str:
    return _("{first} – {last}").format(first=fmt_day_month(days[0]), last=fmt_day_month(days[-1]))
