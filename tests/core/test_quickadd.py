"""Quick-add parser (mandatory, SPEC): every token, malformed variants, order, duplicates,
unicode and empty input. Table-driven."""

from datetime import UTC, date, datetime

import pytest

from daybook.core import quickadd
from daybook.core.calendar import WorkCalendar
from daybook.core.models import (
    DEFAULT_PRIORITY,
    GENERAL_PROJECT_ID,
    PlanRequest,
    Priority,
    Project,
    ProjectColor,
    TimeOffPart,
)
from daybook.core.quickadd import ParseContext, parse, parse_day

TODAY = date(2026, 11, 3)  # Tuesday
CREATED = datetime(2026, 1, 1, tzinfo=UTC)


def project(pid, name, prefix=None, url=None) -> Project:
    return Project(pid, name, ProjectColor.BLUE, prefix, url, False, pid, CREATED)


PMS = project(2, "PMS", "PROJ", "https://pms/{key}")
WEB = project(3, "Web site", "WEB")
DEMOS = project(4, "Demos")
CALENDAR = WorkCalendar(frozenset(range(5)), {date(2026, 11, 5): TimeOffPart.FULL})
CTX = ParseContext(TODAY, (PMS, WEB, DEMOS), CALENDAR)


def ok(line: str) -> quickadd.Parsed:
    result = parse(line, CTX)
    assert result.errors == (), result.errors
    return result.parsed


def errors(line: str) -> tuple[str, ...]:
    result = parse(line, CTX)
    assert result.errors, f"expected an error for {line!r}"
    return result.errors


# --- The spec's examples --------------------------------------------------------------------


def test_spec_example_with_ticket_hash_priority_estimate_due_and_plan():
    result = parse("PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today", CTX)
    assert result.errors == ()
    p = result.parsed
    assert p.title == "Fix login timeout"
    assert p.ticket_key == "PROJ-123"
    assert p.project_id == PMS.id  # inferred from the prefix
    assert p.tags == ("backend",)  # no project called "backend": a tag
    assert p.priority == Priority.P1
    assert p.estimate_min == 120
    assert p.due_date == date(2026, 11, 6)
    assert p.plan == PlanRequest(TODAY, 120)  # the estimate is the default allotment
    assert result.warnings == ()


def test_spec_example_with_area_person_and_rest_of_day():
    p = ok("Prepare demo for client #demos +Rahul_Shah @today=day")
    assert (p.title, p.project_id, p.requested_by) == (
        "Prepare demo for client",
        DEMOS.id,
        "Rahul Shah",
    )
    assert p.plan == PlanRequest(TODAY, None)
    assert p.plan_allotment_given


def test_draft_carries_every_field():
    draft = parse("ABC-9 Write it #docs +Priya !2 ~30m ^tomorrow @tomorrow=45m", CTX).draft()
    assert draft.title == "Write it"
    assert draft.ticket_key == "ABC-9"
    assert draft.project_id == GENERAL_PROJECT_ID
    assert draft.tags == ("docs",)
    assert draft.requested_by == "Priya"
    assert draft.priority == Priority.P2
    assert draft.estimate_min == 30
    assert draft.due_date == date(2026, 11, 4)
    assert draft.plan == PlanRequest(date(2026, 11, 4), 45)


def test_plain_title_gets_the_defaults():
    p = ok("  Call   the   bank  ")
    assert p == quickadd.Parsed(title="Call the bank")
    assert p.priority == DEFAULT_PRIORITY
    assert p.project_id == GENERAL_PROJECT_ID


# --- Ticket keys ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "ticket", "title", "project_id"),
    [
        ("PROJ-123 Fix it", "PROJ-123", "Fix it", PMS.id),
        ("proj-7 lower case", "PROJ-7", "lower case", PMS.id),
        ("PROJ-123: with a colon", "PROJ-123", "with a colon", PMS.id),
        ("Fix WEB-12 footer", "WEB-12", "Fix footer", WEB.id),  # known prefix, mid-line
        ("ABC-1 unknown prefix first", "ABC-1", "unknown prefix first", GENERAL_PROJECT_ID),
        ("Fix covid-19 page", None, "Fix covid-19 page", GENERAL_PROJECT_ID),  # not a ticket
        ("PROJ-1 and PROJ-2 both", "PROJ-1", "and PROJ-2 both", PMS.id),  # first one only
        ("PROJ-x not a key", None, "PROJ-x not a key", GENERAL_PROJECT_ID),
    ],
)
def test_ticket_keys(line, ticket, title, project_id):
    p = ok(line)
    assert (p.ticket_key, p.title, p.project_id) == (ticket, title, project_id)


# --- #project or #tag ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "project_id", "tags"),
    [
        ("x #pms", PMS.id, ()),  # by name, any case
        ("x #PMS", PMS.id, ()),
        ("x #proj", PMS.id, ()),  # by key prefix
        ("x #web_site", WEB.id, ()),  # underscores stand for spaces
        ("x #Demos #demos", DEMOS.id, ()),  # the same project twice is fine
        ("x #backend #ui", GENERAL_PROJECT_ID, ("backend", "ui")),
        ("x #ui #UI", GENERAL_PROJECT_ID, ("ui",)),  # tags de-duplicate
        ("x #café", GENERAL_PROJECT_ID, ("café",)),
    ],
)
def test_hash_words(line, project_id, tags):
    p = ok(line)
    assert (p.project_id, p.tags) == (project_id, tags)


def test_an_explicit_project_beats_the_ticket_prefix_with_a_warning():
    result = parse("PROJ-123 Fix it #demos", CTX)
    assert result.errors == ()
    assert result.parsed.project_id == DEMOS.id
    assert result.warnings == ("#Demos overrides PROJ from PROJ-123.",)


def test_two_different_projects_are_an_error():
    (message,) = errors("x #pms #demos")
    assert message == "#demos: the task is already in PMS."


def test_a_bad_tag_is_an_error():
    (message,) = errors("x #" + "a" * 50)
    assert "Keep tags under" in message


# --- +name, !priority, ~estimate -----------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "person"),
    [("x +Rahul", "Rahul"), ("x +Rahul_Shah", "Rahul Shah"), ("x +José", "José")],
)
def test_requested_by(line, person):
    assert ok(line).requested_by == person


def test_an_overlong_name_is_an_error():
    (message,) = errors("x +" + "a" * 600)
    assert "Keep it under" in message


@pytest.mark.parametrize(
    ("token", "priority"),
    [("!p1", Priority.P1), ("!P2", Priority.P2), ("!3", Priority.P3), ("!p4", Priority.P4)],
)
def test_priorities(token, priority):
    assert ok(f"x {token}").priority == priority


@pytest.mark.parametrize("token", ["!p5", "!0", "!high", "!p", "!!"])
def test_bad_priorities(token):
    (message,) = errors(f"x {token}")
    assert message == f"{token}: use !p1 to !p4."


@pytest.mark.parametrize(
    ("token", "minutes"),
    [("~2h", 120), ("~30m", 30), ("~1h30m", 90), ("~1.5h", 90), ("~90", 90)],
)
def test_estimates(token, minutes):
    assert ok(f"x {token}").estimate_min == minutes


@pytest.mark.parametrize("token", ["~abc", "~0", "~-1h", "~9999h"])
def test_bad_estimates(token):
    (message,) = errors(f"x {token}")
    assert message.startswith(f"{token}: ")


# --- ^due and @plan ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "day"),
    [
        ("today", TODAY),
        ("Tomorrow", date(2026, 11, 4)),
        ("tue", TODAY),  # the next Tuesday includes today (D-021)
        ("wednesday", date(2026, 11, 4)),
        ("mon", date(2026, 11, 9)),
        ("10/11", date(2026, 11, 10)),
        ("1/11", date(2027, 11, 1)),  # already passed this year: next year
        ("03/11", TODAY),
        ("25/12/2027", date(2027, 12, 25)),
        ("2026-12-01", date(2026, 12, 1)),
    ],
)
def test_parse_day(text, day):
    assert parse_day(text, TODAY) == day
    assert ok(f"x ^{text}").due_date == day


@pytest.mark.parametrize("text", ["31/02", "2026-13-01", "someday", "32/1", "1/1/26"])
def test_bad_days(text):
    with pytest.raises(ValueError, match=r"."):
        parse_day(text, TODAY)
    (message,) = errors(f"x ^{text}")
    assert message.startswith(f"^{text}: use today")


@pytest.mark.parametrize(
    ("token", "plan", "given"),
    [
        ("@today", PlanRequest(TODAY, 30), False),  # no estimate: 30 min (D-021)
        ("@today=2h", PlanRequest(TODAY, 120), True),
        ("@today=30m", PlanRequest(TODAY, 30), True),
        ("@today=1h30m", PlanRequest(TODAY, 90), True),
        ("@today=day", PlanRequest(TODAY, None), True),
        ("@today=DAY", PlanRequest(TODAY, None), True),
        ("@tomorrow", PlanRequest(date(2026, 11, 4), 30), False),
        ("@fri=1h", PlanRequest(date(2026, 11, 6), 60), True),
        ("@10/11", PlanRequest(date(2026, 11, 10), 30), False),
    ],
)
def test_plans(token, plan, given):
    p = ok(f"x {token}")
    assert (p.plan, p.plan_allotment_given) == (plan, given)


def test_planning_uses_the_estimate_capped_at_a_day():
    assert ok("x ~3h @today").plan == PlanRequest(TODAY, 180)
    assert ok("x ~40h @today").plan == PlanRequest(TODAY, 24 * 60)


@pytest.mark.parametrize(
    ("token", "start"),
    [
        ("@sat", "@sat: Sat 7 Nov 2026 isn't a working day. The next one is Mon 9 Nov 2026."),
        (
            "@thu",
            "@thu: Thu 5 Nov 2026 isn't a working day. The next one is Fri 6 Nov 2026.",
        ),  # leave
        ("@2026-11-02", "@2026-11-02: Pick today or a later date."),
        ("@someday", "@someday: use @today"),
        ("@today=", "@today=: add a time after ="),
        ("@today=0", "@today=0: Enter a time longer than zero."),
        ("@today=30h", "@today=30h: Enter a time between"),
        ("@today=abc", "@today=abc: Use a time like"),
    ],
)
def test_bad_plans(token, start):
    (message,) = errors(f"x {token}")
    assert message.startswith(start)


# --- Duplicates, order, escapes, empty -------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "kind"),
    [
        ("x !p1 !p2", "priority"),
        ("x ~1h ~2h", "estimate"),
        ("x ^mon ^tue", "due date"),
        ("x @today @tomorrow", "plan"),
        ("x +Ann +Bob", "requested-by"),
    ],
)
def test_a_second_token_of_a_kind_is_an_error(line, kind):
    (message,) = errors(line)
    assert message.endswith(f"only one {kind} per task.")


def test_order_does_not_matter():
    a = ok("!p1 @today ~1h Fix it #pms ^fri +Ann")
    b = ok("Fix it +Ann ^fri #pms ~1h @today !p1")
    assert a == b
    assert a.title == "Fix it"


def test_escapes_and_lone_symbols_stay_in_the_title():
    p = ok(r"Read \#1 hit \@today now ! @ # + ~ ^ a@b.com C#")
    assert p.title == "Read #1 hit @today now ! @ # + ~ ^ a@b.com C#"
    assert p.plan is None


def test_unicode_titles_are_kept():
    assert ok("Écrire la doc 📄 für Kunden").title == "Écrire la doc 📄 für Kunden"


@pytest.mark.parametrize("line", ["", "   ", "#pms !p1 @today"])
def test_no_title_is_an_error(line):
    assert "Enter a title." in errors(line)


def test_several_errors_are_all_reported():
    result = parse("!p9 ^never", CTX)
    assert len(result.errors) == 3  # priority, due date, no title
    assert not result.ok


def test_token_at_finds_the_word_under_the_cursor():
    line = "Fix it +Ra today"
    assert quickadd.token_at(line, 10) == (7, "+Ra")
    assert quickadd.token_at(line, len(line)) == (11, "today")
    assert quickadd.token_at("", 0) == (0, "")
