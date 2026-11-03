"""v1: the initial schema (docs/PLAN.md §4)."""

from typing import TYPE_CHECKING

from daybook.core.db import Connection, execute_script

if TYPE_CHECKING:
    from daybook.core.migrations import MigrationContext

GENERAL_PROJECT_ID = 1

SCHEMA = """
-- A project with no key_prefix/url_template is an "area" (D-043).
CREATE TABLE project (
  id            INTEGER PRIMARY KEY,
  name          TEXT    NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) > 0),
  color         TEXT    NOT NULL,
  key_prefix    TEXT    UNIQUE COLLATE NOCASE,
  url_template  TEXT,
  archived      INTEGER NOT NULL DEFAULT 0 CHECK (archived IN (0, 1)),
  sort_order    INTEGER NOT NULL DEFAULT 0,
  created_at    TEXT    NOT NULL
);

-- General (id 1) is the default area: renameable, never deleted or archived.
CREATE TRIGGER project_general_no_delete BEFORE DELETE ON project
WHEN OLD.id = 1
BEGIN SELECT RAISE(ABORT, 'general_project_protected'); END;

CREATE TRIGGER project_general_no_archive BEFORE UPDATE OF archived ON project
WHEN NEW.id = 1 AND NEW.archived = 1
BEGIN SELECT RAISE(ABORT, 'general_project_protected'); END;

CREATE TABLE task (
  id              INTEGER PRIMARY KEY,
  project_id      INTEGER NOT NULL DEFAULT 1 REFERENCES project(id) ON DELETE RESTRICT,
  title           TEXT    NOT NULL CHECK (length(trim(title)) > 0),
  ticket_key      TEXT    COLLATE NOCASE,
  requested_by    TEXT,
  priority        INTEGER NOT NULL DEFAULT 3 CHECK (priority BETWEEN 1 AND 4),
  status          TEXT    NOT NULL DEFAULT 'todo'
                  CHECK (status IN ('todo', 'in_progress', 'on_hold', 'done')),
  hold_reason     TEXT    CHECK (hold_reason IN
                            ('blocked', 'approval', 'someone', 'postponed', 'other')),
  hold_note       TEXT,
  hold_since      TEXT,
  follow_up_date  TEXT,
  estimate_min    INTEGER CHECK (estimate_min IS NULL OR estimate_min >= 0),
  due_date        TEXT,
  notes           TEXT    NOT NULL DEFAULT '',
  recurrence      TEXT,
  recurs_from_id  INTEGER REFERENCES task(id) ON DELETE SET NULL,
  moved_count     INTEGER NOT NULL DEFAULT 0 CHECK (moved_count >= 0),
  created_at      TEXT    NOT NULL,
  updated_at      TEXT    NOT NULL,
  completed_at    TEXT,
  deleted_at      TEXT,
  CHECK ((status = 'done') = (completed_at IS NOT NULL)),
  CHECK ((status = 'on_hold') = (hold_reason IS NOT NULL)),
  CHECK ((status = 'on_hold') = (hold_since IS NOT NULL)),
  CHECK (status = 'on_hold' OR (hold_note IS NULL AND follow_up_date IS NULL))
);

CREATE TABLE checklist_item (
  id        INTEGER PRIMARY KEY,
  task_id   INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  text      TEXT    NOT NULL CHECK (length(trim(text)) > 0),
  done      INTEGER NOT NULL DEFAULT 0 CHECK (done IN (0, 1)),
  position  INTEGER NOT NULL
);

CREATE TABLE tag (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE COLLATE NOCASE CHECK (length(trim(name)) > 0)
);

CREATE TABLE task_tag (
  task_id  INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tag(id)  ON DELETE CASCADE,
  PRIMARY KEY (task_id, tag_id)
) WITHOUT ROWID;

CREATE TABLE meeting (
  id                INTEGER PRIMARY KEY,
  title             TEXT    NOT NULL CHECK (length(trim(title)) > 0),
  start_utc         TEXT    NOT NULL,
  duration_min      INTEGER NOT NULL CHECK (duration_min > 0),
  tzid              TEXT    NOT NULL,
  link              TEXT,
  recurrence        TEXT,
  reminder_offsets  TEXT    NOT NULL,
  project_id        INTEGER REFERENCES project(id) ON DELETE SET NULL,
  is_adhoc          INTEGER NOT NULL DEFAULT 0 CHECK (is_adhoc IN (0, 1)),
  open_ended        INTEGER NOT NULL DEFAULT 0 CHECK (open_ended IN (0, 1)),
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL,
  deleted_at        TEXT,
  CHECK (open_ended = 0 OR (is_adhoc = 1 AND recurrence IS NULL))
);

CREATE TABLE meeting_exception (
  id                  INTEGER PRIMARY KEY,
  meeting_id          INTEGER NOT NULL REFERENCES meeting(id) ON DELETE CASCADE,
  original_start_utc  TEXT    NOT NULL,
  cancelled           INTEGER NOT NULL DEFAULT 0 CHECK (cancelled IN (0, 1)),
  title               TEXT,
  start_utc           TEXT,
  duration_min        INTEGER CHECK (duration_min IS NULL OR duration_min > 0),
  link                TEXT,
  reminder_offsets    TEXT,
  UNIQUE (meeting_id, original_start_utc)
);

CREATE TABLE day_plan_item (
  id            INTEGER PRIMARY KEY,
  plan_date     TEXT    NOT NULL,
  task_id       INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  allotted_min  INTEGER CHECK (allotted_min IS NULL OR allotted_min > 0),
  position      INTEGER NOT NULL,
  carry_count   INTEGER NOT NULL DEFAULT 0 CHECK (carry_count >= 0),
  carried_from  TEXT,
  UNIQUE (plan_date, task_id)
);

CREATE TABLE time_off (
  day   TEXT PRIMARY KEY,
  part  TEXT NOT NULL CHECK (part IN ('full', 'am', 'pm')),
  note  TEXT NOT NULL DEFAULT ''
) WITHOUT ROWID;

CREATE TABLE time_entry (
  id         INTEGER PRIMARY KEY,
  task_id    INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  start_utc  TEXT    NOT NULL,
  end_utc    TEXT    NOT NULL,
  note       TEXT    NOT NULL DEFAULT '',
  source     TEXT    NOT NULL CHECK (source IN ('timer', 'manual')),
  CHECK (end_utc > start_utc)
);

-- Entries never overlap (half-open intervals), so only the latest entry starting before
-- NEW.end_utc can overlap: a single index probe even with 50k rows.
CREATE TRIGGER time_entry_no_overlap_ins BEFORE INSERT ON time_entry
WHEN EXISTS (SELECT 1 FROM (SELECT end_utc FROM time_entry
                            WHERE start_utc < NEW.end_utc
                            ORDER BY start_utc DESC LIMIT 1)
             WHERE end_utc > NEW.start_utc)
BEGIN SELECT RAISE(ABORT, 'time_entry_overlap'); END;

CREATE TRIGGER time_entry_no_overlap_upd BEFORE UPDATE OF start_utc, end_utc ON time_entry
WHEN EXISTS (SELECT 1 FROM (SELECT end_utc FROM time_entry
                            WHERE start_utc < NEW.end_utc AND id <> NEW.id
                            ORDER BY start_utc DESC LIMIT 1)
             WHERE end_utc > NEW.start_utc)
BEGIN SELECT RAISE(ABORT, 'time_entry_overlap'); END;

CREATE TABLE timer_state (
  id                     INTEGER PRIMARY KEY CHECK (id = 1),
  task_id                INTEGER NOT NULL REFERENCES task(id) ON DELETE CASCADE,
  state                  TEXT    NOT NULL CHECK (state IN ('running', 'paused')),
  segment_start_utc      TEXT,
  last_seen_utc          TEXT    NOT NULL,
  pause_reason           TEXT    CHECK (pause_reason IN ('user', 'meeting', 'idle')),
  paused_by_meeting_key  TEXT,
  updated_at             TEXT    NOT NULL,
  CHECK ((state = 'running') = (segment_start_utc IS NOT NULL))
);

CREATE TABLE reminder_log (
  id          INTEGER PRIMARY KEY,
  key         TEXT NOT NULL UNIQUE,
  kind        TEXT NOT NULL,
  due_utc     TEXT NOT NULL,
  logged_utc  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('fired', 'missed', 'suppressed')),
  action      TEXT,
  action_utc  TEXT,
  payload     TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE pending_reminder (
  id       INTEGER PRIMARY KEY,
  key      TEXT NOT NULL UNIQUE,
  kind     TEXT NOT NULL,
  due_utc  TEXT NOT NULL,
  payload  TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE settings  (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE app_state (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

CREATE INDEX task_active_idx      ON task (deleted_at, status, priority, due_date);
CREATE INDEX task_project_idx     ON task (project_id);
CREATE INDEX task_ticket_idx      ON task (ticket_key);
CREATE INDEX task_completed_idx   ON task (completed_at);
CREATE INDEX task_follow_up_idx   ON task (follow_up_date) WHERE status = 'on_hold';
CREATE INDEX checklist_task_idx   ON checklist_item (task_id, position);
CREATE INDEX task_tag_tag_idx     ON task_tag (tag_id);
CREATE INDEX meeting_start_idx    ON meeting (deleted_at, start_utc);
CREATE INDEX plan_date_idx        ON day_plan_item (plan_date, position);
CREATE INDEX plan_task_idx        ON day_plan_item (task_id);
CREATE INDEX time_entry_start_idx ON time_entry (start_utc);
CREATE INDEX time_entry_task_idx  ON time_entry (task_id, start_utc);
CREATE INDEX reminder_due_idx     ON reminder_log (due_utc);
CREATE INDEX pending_due_idx      ON pending_reminder (due_utc);
"""


def apply(conn: Connection, context: MigrationContext) -> None:
    execute_script(conn, SCHEMA)
    conn.execute(
        "INSERT INTO project (id, name, color, created_at, sort_order) VALUES (?, ?, ?, ?, ?)",
        (GENERAL_PROJECT_ID, "General", "slate", context.now, 1_000_000),
    )
