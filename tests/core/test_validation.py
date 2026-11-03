from datetime import date

import pytest

from daybook.core import validation
from daybook.core.errors import ValidationError
from daybook.core.models import PlanRequest, Priority, ProjectColor, ProjectDraft, TaskDraft


def field_of(fn, *args):
    with pytest.raises(ValidationError) as info:
        fn(*args)
    return info.value.field


def test_title_collapses_whitespace_and_requires_text():
    assert validation.title("  Fix   login\ttimeout \n") == "Fix login timeout"
    assert field_of(validation.title, "   ") == "title"
    assert field_of(validation.title, "x" * 501) == "title"
    assert validation.title("x" * 500) == "x" * 500


def test_optional_text_turns_blank_into_none():
    assert validation.optional_text(None, "f") is None
    assert validation.optional_text("   ", "f") is None
    assert validation.optional_text(" Rahul  Shah ", "f") == "Rahul Shah"
    assert field_of(validation.optional_text, "x" * 11, "requested_by", 10) == "requested_by"


def test_notes_keep_line_breaks_and_trim_the_end():
    assert validation.notes("line 1\n\nline 2  \n\n") == "line 1\n\nline 2"
    assert field_of(validation.notes, "x" * (validation.MAX_NOTES + 1)) == "notes"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("proj", "PROJ"), (" Web2 ", "WEB2"), ("", None), ("  ", None), (None, None)],
)
def test_key_prefix_is_upper_cased(raw, expected):
    assert validation.key_prefix(raw) == expected


@pytest.mark.parametrize("raw", ["1PROJ", "PRO-J", "PR OJ", "ÄB", "ABCDEFGHIJK"])
def test_key_prefix_rejects_bad_values(raw):
    assert field_of(validation.key_prefix, raw) == "key_prefix"


def test_url_template_needs_http_and_key_placeholder():
    url = "https://pms.example.com/browse/{key}"
    assert validation.url_template(f"  {url} ") == url
    assert validation.url_template("") is None
    assert validation.url_template(None) is None
    for bad in ("ftp://x/{key}", "https://pms.example.com/browse/", "https://a b/{key}"):
        assert field_of(validation.url_template, bad) == "url_template"


def test_project_draft_normalises_every_field():
    clean = validation.project_draft(ProjectDraft(" Web ", ProjectColor.TEAL, "web", " "))
    assert clean == ProjectDraft("Web", ProjectColor.TEAL, "WEB", None)
    assert field_of(validation.project_name, " ") == "name"
    assert field_of(validation.project_name, "x" * 81) == "name"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("proj-123", "PROJ-123"),
        (" #4521 ", "#4521"),
        ("", None),
        (None, None),
        ("ab-12x", "ab-12x"),
    ],
)
def test_ticket_key_upper_cases_pms_style_keys_only(raw, expected):
    assert validation.ticket_key(raw) == expected


def test_ticket_key_rejects_spaces_and_long_keys():
    assert field_of(validation.ticket_key, "PROJ 123") == "ticket_key"
    assert field_of(validation.ticket_key, "K" * 65) == "ticket_key"


def test_ticket_prefix():
    assert validation.ticket_prefix("proj-12") == "PROJ"
    assert validation.ticket_prefix("#12") is None


def test_priority_estimate_and_allotment_ranges():
    assert validation.priority(1) is Priority.P1
    assert field_of(validation.priority, 5) == "priority"
    assert validation.estimate(None) is None
    assert validation.estimate(90) == 90
    assert field_of(validation.estimate, 0) == "estimate"
    assert validation.allotment(None) is None
    assert validation.allotment(24 * 60) == 24 * 60
    assert field_of(validation.allotment, 24 * 60 + 1) == "allotment"


def test_tags_are_single_words_and_deduplicated_case_insensitively():
    assert validation.tag_name(" #backend ") == "backend"
    assert validation.tag_names(["UI", "ui", "#api"]) == ("UI", "api")
    assert validation.split_tags("backend, ui  #urgent,,") == ("backend", "ui", "urgent")
    assert validation.split_tags("  ") == ()
    for bad in ("", "#", "two words", "a,b", "x" * 41):
        assert field_of(validation.tag_name, bad) == "tags"


def test_checklist_text():
    assert validation.checklist_text("  write  tests ") == "write tests"
    assert field_of(validation.checklist_text, " ") == "checklist"
    assert field_of(validation.checklist_text, "x" * 501) == "checklist"


def test_task_draft_validates_everything_and_keeps_the_plan():
    plan = PlanRequest(date(2026, 11, 3), 30)
    clean = validation.task_draft(
        TaskDraft(
            " Fix  it ",
            priority=2,
            ticket_key="proj-1",
            requested_by=" ",
            tags=("a", "A"),
            plan=plan,
        )
    )
    assert clean.title == "Fix it"
    assert clean.priority is Priority.P2
    assert clean.ticket_key == "PROJ-1"
    assert clean.requested_by is None
    assert clean.tags == ("a",)
    assert clean.plan == plan


@pytest.mark.parametrize(
    ("text", "minutes"),
    [
        ("30m", 30),
        ("2h", 120),
        ("1h30m", 90),
        ("1h 30m", 90),
        ("1.5h", 90),
        ("1,5h", 90),
        ("90", 90),
        ("45 min", 45),
        (" 2H ", 120),
        ("", None),
        ("   ", None),
    ],
)
def test_parse_duration(text, minutes):
    assert validation.parse_duration(text) == minutes


@pytest.mark.parametrize("text", ["abc", "h", "2x", "-5", "0", "0h", "1h30", "m"])
def test_parse_duration_rejects_garbage(text):
    assert field_of(validation.parse_duration, text) == "estimate"


def test_parse_duration_reports_the_given_field():
    assert field_of(validation.parse_duration, "x", "allotment") == "allotment"


def test_follow_up_date_cannot_be_in_the_past():
    today = date(2026, 11, 3)
    assert validation.follow_up_date(None, today) is None
    assert validation.follow_up_date(today, today) == today
    assert field_of(validation.follow_up_date, date(2026, 11, 2), today) == "follow_up"
