"""The quick-add line (PLAN §5.7): one line in, a task out. Pure, and `parse` never raises.

    PROJ-123 Fix login timeout #backend !p1 ~2h ^fri @today
    Prepare demo for client #demos +Rahul_Shah @today=day

Tokens are whitespace-separated. A token starting with ``\\`` is kept as text (``\\#1``
gives "#1"). Everything that isn't a token is the title. Errors and warnings are plain
sentences for the live preview; the dialog only adds the task when there are no errors.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from gettext import gettext as _

from daybook.core import reschedule, validation
from daybook.core.calendar import WorkCalendar
from daybook.core.errors import ValidationError
from daybook.core.models import (
    DEFAULT_PRIORITY,
    GENERAL_PROJECT_ID,
    PlanRequest,
    Priority,
    Project,
    TaskDraft,
)

_TICKET = re.compile(r"[A-Za-z][A-Za-z0-9]*-\d+")
_TRAILING = ",.:;"  # "PROJ-123:" is still the ticket key
_PRIORITY = re.compile(r"!(?:p|P)?([1-4])")
_DMY = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{4}))?")
_ISO = re.compile(r"(\d{4})-(\d{1,2})-(\d{1,2})")
_WEEKDAYS = (
    ("mon", "monday"),
    ("tue", "tuesday"),
    ("wed", "wednesday"),
    ("thu", "thursday"),
    ("fri", "friday"),
    ("sat", "saturday"),
    ("sun", "sunday"),
)


@dataclass(frozen=True)
class ParseContext:
    today: date
    projects: Sequence[Project]  # active projects and areas
    calendar: WorkCalendar


@dataclass(frozen=True)
class Parsed:
    title: str = ""
    ticket_key: str | None = None
    project_id: int = GENERAL_PROJECT_ID
    tags: tuple[str, ...] = ()
    requested_by: str | None = None
    priority: Priority = DEFAULT_PRIORITY
    estimate_min: int | None = None
    due_date: date | None = None
    plan: PlanRequest | None = None
    plan_allotment_given: bool = False  # `@day=…` rather than the default allotment


@dataclass(frozen=True)
class ParseResult:
    parsed: Parsed
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.errors

    def draft(self) -> TaskDraft:
        p = self.parsed
        return TaskDraft(
            title=p.title,
            project_id=p.project_id,
            priority=p.priority,
            ticket_key=p.ticket_key,
            requested_by=p.requested_by,
            estimate_min=p.estimate_min,
            due_date=p.due_date,
            tags=p.tags,
            plan=p.plan,
        )


def parse_day(text: str, today: date) -> date:
    """``today``, ``tomorrow``, a weekday (next one, today included, D-021), ``dd/mm``
    (next one on or after today), ``dd/mm/yyyy`` or ``yyyy-mm-dd``. Raises ValueError."""
    word = text.casefold()
    if word in ("today", "tod"):
        return today
    if word in ("tomorrow", "tom"):
        return today + timedelta(days=1)
    for index, names in enumerate(_WEEKDAYS):
        if word in names:
            return today + timedelta(days=(index - today.weekday()) % 7)
    if match := _ISO.fullmatch(text):
        return date(int(match[1]), int(match[2]), int(match[3]))
    if match := _DMY.fullmatch(text):
        day, month = int(match[1]), int(match[2])
        if match[3]:
            return date(int(match[3]), month, day)
        candidate = date(today.year, month, day)
        return candidate if candidate >= today else date(today.year + 1, month, day)
    raise ValueError(text)


@dataclass
class _State:
    ctx: ParseContext
    title: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    ticket: str | None = None
    hash_project: Project | None = None
    tags: list[str] = field(default_factory=list)
    requested_by: str | None = None
    priority: Priority | None = None
    estimate: int | None = None
    due: date | None = None
    plan_day: date | None = None
    plan_minutes: int | None = None
    plan_rest: bool = False
    plan_given: bool = False
    seen: set[str] = field(default_factory=set)

    def once(self, kind: str, token: str) -> bool:
        """True the first time a kind of token appears; a repeat is an error."""
        if kind in self.seen:
            self.errors.append(
                _("{token}: only one {kind} per task.").format(token=token, kind=kind)
            )
            return False
        self.seen.add(kind)
        return True


def _project_named(ctx: ParseContext, word: str) -> Project | None:
    wanted = word.replace("_", " ").casefold()
    for project in ctx.projects:
        if project.name.casefold() == wanted:
            return project
        if project.key_prefix and project.key_prefix.casefold() == wanted:
            return project
    return None


def _hash(state: _State, token: str) -> None:
    word = token[1:]
    project = _project_named(state.ctx, word)
    if project is not None:
        if state.hash_project is not None and state.hash_project.id != project.id:
            state.errors.append(
                _("{token}: the task is already in {project}.").format(
                    token=token, project=state.hash_project.name
                )
            )
        else:
            state.hash_project = project
        return
    try:
        name = validation.tag_name(word)
    except ValidationError as error:
        state.errors.append(f"{token}: {error.message}")
        return
    if name.casefold() not in (t.casefold() for t in state.tags):
        state.tags.append(name)


def _person(state: _State, token: str) -> None:
    if not state.once(_("requested-by"), token):
        return
    try:
        state.requested_by = validation.optional_text(
            token[1:].replace("_", " "), "requested_by", validation.MAX_SHORT_TEXT
        )
    except ValidationError as error:
        state.errors.append(f"{token}: {error.message}")


def _priority(state: _State, token: str) -> None:
    match = _PRIORITY.fullmatch(token)
    if match is None:
        state.errors.append(_("{token}: use !p1 to !p4.").format(token=token))
        return
    if state.once(_("priority"), token):
        state.priority = Priority(int(match[1]))


def _estimate(state: _State, token: str) -> None:
    try:
        minutes = validation.estimate(validation.parse_duration(token[1:]))
    except ValidationError as error:
        state.errors.append(f"{token}: {error.message}")
        return
    if state.once(_("estimate"), token):
        state.estimate = minutes


def _due(state: _State, token: str) -> None:
    try:
        day = parse_day(token[1:], state.ctx.today)
    except ValueError:
        state.errors.append(
            _("{token}: use today, tomorrow, a weekday, dd/mm or yyyy-mm-dd.").format(token=token)
        )
        return
    if state.once(_("due date"), token):
        state.due = day


def _plan(state: _State, token: str) -> None:
    day_text, _sep, amount = token[1:].partition("=")
    try:
        day = parse_day(day_text, state.ctx.today)
    except ValueError:
        state.errors.append(
            _("{token}: use @today, @tomorrow, a weekday, dd/mm or yyyy-mm-dd.").format(token=token)
        )
        return
    try:
        reschedule.check_plan_day(day, state.ctx.today, state.ctx.calendar, "plan")
    except ValidationError as error:
        state.errors.append(f"{token}: {error.message}")
        return
    if not state.once(_("plan"), token):
        return
    state.plan_day = day
    if not _sep:
        return
    state.plan_given = True
    if amount.casefold() == "day":
        state.plan_rest = True
        return
    try:
        state.plan_minutes = validation.allotment(validation.parse_duration(amount, "allotment"))
    except ValidationError as error:
        state.errors.append(f"{token}: {error.message}")
        return
    if state.plan_minutes is None:
        state.errors.append(_("{token}: add a time after =, like =2h or =day.").format(token=token))


def _maybe_ticket(state: _State, token: str, first: bool) -> bool:
    """The first token shaped like PROJ-123 is the ticket key when it starts the line or
    its prefix is a known project's (so "covid-19" in a title stays text, D-105)."""
    if state.ticket is not None:
        return False
    bare = token.rstrip(_TRAILING)
    if not _TICKET.fullmatch(bare):
        return False
    prefix = validation.ticket_prefix(bare)
    known = any(p.key_prefix and p.key_prefix.upper() == prefix for p in state.ctx.projects)
    if not (first or known):
        return False
    try:
        state.ticket = validation.ticket_key(bare)
    except ValidationError as error:  # e.g. a 70-digit "ticket": parse never raises
        state.errors.append(f"{bare[:20]}…: {error.message}")
    return True


_HANDLERS: dict[str, Callable[[_State, str], None]] = {
    "#": _hash,
    "+": _person,
    "!": _priority,
    "~": _estimate,
    "^": _due,
    "@": _plan,
}


def _project(state: _State) -> int:
    inferred = None
    if state.ticket is not None:
        prefix = validation.ticket_prefix(state.ticket)
        inferred = next(
            (p for p in state.ctx.projects if p.key_prefix and p.key_prefix.upper() == prefix),
            None,
        )
    if state.hash_project is not None:
        if inferred is not None and inferred.id != state.hash_project.id:
            state.warnings.append(
                _("#{name} overrides {prefix} from {ticket}.").format(
                    name=state.hash_project.name, prefix=inferred.key_prefix, ticket=state.ticket
                )
            )
        return state.hash_project.id
    return inferred.id if inferred is not None else GENERAL_PROJECT_ID


def parse(line: str, ctx: ParseContext) -> ParseResult:
    state = _State(ctx)
    for index, token in enumerate(line.split()):
        if token.startswith("\\") and len(token) > 1:
            state.title.append(token[1:])
            continue
        handler = _HANDLERS.get(token[0]) if len(token) > 1 else None
        if handler is not None:
            handler(state, token)
        elif not _maybe_ticket(state, token, first=index == 0):
            state.title.append(token)
    try:
        title = validation.title(" ".join(state.title))
    except ValidationError as error:
        title = ""
        state.errors.append(error.message)
    project_id = _project(state)
    plan = None
    if state.plan_day is not None:
        if state.plan_rest:
            minutes = None
        elif state.plan_minutes is not None:
            minutes = state.plan_minutes
        else:
            minutes = reschedule.default_allotment(state.estimate)
        plan = PlanRequest(state.plan_day, minutes)
    parsed = Parsed(
        title=title,
        ticket_key=state.ticket,
        project_id=project_id,
        tags=tuple(state.tags),
        requested_by=state.requested_by,
        priority=state.priority or DEFAULT_PRIORITY,
        estimate_min=state.estimate,
        due_date=state.due,
        plan=plan,
        plan_allotment_given=state.plan_given,
    )
    return ParseResult(parsed, tuple(state.errors), tuple(state.warnings))


def token_at(line: str, position: int) -> tuple[int, str]:
    """The token under the cursor and where it starts (for `+name` completion)."""
    start = line.rfind(" ", 0, position) + 1
    end = line.find(" ", position)
    return start, line[start : end if end != -1 else len(line)]
