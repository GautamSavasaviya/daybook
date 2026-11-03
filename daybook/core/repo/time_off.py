"""Time off (D-048): read side for the WorkCalendar. Editing arrives in Phase 2."""

from datetime import date

from daybook.core import timeutil
from daybook.core.db import Connection
from daybook.core.models import TimeOffPart


def all_days(conn: Connection) -> dict[date, TimeOffPart]:
    return {
        timeutil.date_from_db(row["day"]): TimeOffPart(row["part"])
        for row in conn.execute("SELECT day, part FROM time_off")
    }
