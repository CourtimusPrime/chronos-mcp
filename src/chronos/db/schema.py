"""DDL strings for all tables, indexes, FTS5 virtual tables, and triggers."""

ACCOUNTS_DDL = """
CREATE TABLE IF NOT EXISTS accounts (
  id            TEXT    NOT NULL PRIMARY KEY,
  provider      TEXT    NOT NULL,
  email         TEXT    NOT NULL,
  display_name  TEXT,
  auth_type     TEXT    NOT NULL,
  auth_data     TEXT    NOT NULL,
  sync_enabled  INTEGER NOT NULL DEFAULT 1,
  last_synced_at INTEGER,
  sync_cursor   TEXT,
  created_at    INTEGER NOT NULL,
  UNIQUE(provider, email)
);
"""

THREADS_DDL = """
CREATE TABLE IF NOT EXISTS threads (
  id                   TEXT    NOT NULL PRIMARY KEY,
  account_id           TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  provider_thread_id   TEXT    NOT NULL,
  subject              TEXT,
  participant_addresses TEXT,
  message_count        INTEGER NOT NULL DEFAULT 0,
  last_message_at      INTEGER,
  provider_raw         TEXT,
  created_at           INTEGER NOT NULL,
  UNIQUE(account_id, provider_thread_id)
);
"""

MESSAGES_DDL = """
CREATE TABLE IF NOT EXISTS messages (
  id                  TEXT    NOT NULL PRIMARY KEY,
  account_id          TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  thread_id           TEXT    REFERENCES threads(id) ON DELETE SET NULL,
  provider_message_id TEXT    NOT NULL,
  subject             TEXT,
  from_address        TEXT    NOT NULL,
  from_name           TEXT,
  to_addresses        TEXT    NOT NULL,
  cc_addresses        TEXT,
  bcc_addresses       TEXT,
  date_unix           INTEGER NOT NULL,
  body_text           TEXT,
  body_html           TEXT,
  labels              TEXT,
  has_attachments     INTEGER NOT NULL DEFAULT 0,
  attachment_names    TEXT,
  in_reply_to         TEXT,
  references_header   TEXT,
  web_url             TEXT,
  sync_state          TEXT    NOT NULL DEFAULT 'synced',
  created_at          INTEGER NOT NULL,
  provider_raw        TEXT,
  UNIQUE(account_id, provider_message_id)
);
"""

CALENDARS_DDL = """
CREATE TABLE IF NOT EXISTS calendars (
  id                   TEXT    NOT NULL PRIMARY KEY,
  account_id           TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  provider_calendar_id TEXT    NOT NULL,
  name                 TEXT    NOT NULL,
  description          TEXT,
  color                TEXT,
  is_primary           INTEGER NOT NULL DEFAULT 0,
  is_read_only         INTEGER NOT NULL DEFAULT 0,
  timezone             TEXT,
  provider_raw         TEXT,
  created_at           INTEGER NOT NULL,
  UNIQUE(account_id, provider_calendar_id)
);
"""

EVENTS_DDL = """
CREATE TABLE IF NOT EXISTS events (
  id                      TEXT    NOT NULL PRIMARY KEY,
  account_id              TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  calendar_id             TEXT    NOT NULL REFERENCES calendars(id) ON DELETE CASCADE,
  provider_event_id       TEXT    NOT NULL,
  title                   TEXT    NOT NULL,
  description             TEXT,
  location                TEXT,
  start_unix              INTEGER NOT NULL,
  end_unix                INTEGER NOT NULL,
  is_all_day              INTEGER NOT NULL DEFAULT 0,
  timezone                TEXT,
  status                  TEXT    NOT NULL DEFAULT 'confirmed',
  organizer_address       TEXT,
  attendees               TEXT,
  rrule                   TEXT,
  is_recurring_master     INTEGER NOT NULL DEFAULT 0,
  recurrence_master_id    TEXT    REFERENCES events(id) ON DELETE CASCADE,
  recurrence_instance_date INTEGER,
  conference_url          TEXT,
  source_message_id       TEXT    REFERENCES messages(id) ON DELETE SET NULL,
  sync_state              TEXT    NOT NULL DEFAULT 'synced',
  created_at              INTEGER NOT NULL,
  provider_raw            TEXT,
  UNIQUE(account_id, provider_event_id)
);
"""

PENDING_CHANGES_DDL = """
CREATE TABLE IF NOT EXISTS pending_changes (
  id               TEXT    NOT NULL PRIMARY KEY,
  resource_type    TEXT    NOT NULL,
  resource_id      TEXT    NOT NULL,
  account_id       TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  operation        TEXT    NOT NULL,
  payload          TEXT    NOT NULL,
  status           TEXT    NOT NULL DEFAULT 'pending',
  created_at       INTEGER NOT NULL,
  submitted_at     INTEGER,
  confirmed_at     INTEGER,
  rejection_reason TEXT
);
"""

SYNC_LOG_DDL = """
CREATE TABLE IF NOT EXISTS sync_log (
  id              TEXT    NOT NULL PRIMARY KEY,
  account_id      TEXT    NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
  sync_type       TEXT    NOT NULL,
  started_at      INTEGER NOT NULL,
  completed_at    INTEGER,
  records_synced  INTEGER NOT NULL DEFAULT 0,
  status          TEXT    NOT NULL DEFAULT 'running',
  error_message   TEXT
);
"""

INDEXES_DDL = [
    "CREATE INDEX IF NOT EXISTS idx_messages_account_date ON messages(account_id, date_unix DESC);",
    "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id);",
    "CREATE INDEX IF NOT EXISTS idx_messages_from ON messages(from_address);",
    "CREATE INDEX IF NOT EXISTS idx_events_account_start ON events(account_id, start_unix);",
    "CREATE INDEX IF NOT EXISTS idx_events_calendar ON events(calendar_id);",
    "CREATE INDEX IF NOT EXISTS idx_events_recurring_master ON events(recurrence_master_id);",
    "CREATE INDEX IF NOT EXISTS idx_pending_status ON pending_changes(status, created_at);",
    "CREATE INDEX IF NOT EXISTS idx_sync_log_account ON sync_log(account_id, started_at DESC);",
]

MESSAGES_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
  subject,
  body_text,
  from_address,
  labels,
  content='messages',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
"""

MESSAGES_FTS_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS messages_fts_insert
      AFTER INSERT ON messages BEGIN
        INSERT INTO messages_fts(rowid, subject, body_text, from_address, labels)
        VALUES (new.rowid, new.subject, new.body_text, new.from_address, new.labels);
      END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_fts_delete
      AFTER DELETE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_address, labels)
        VALUES ('delete', old.rowid, old.subject, old.body_text, old.from_address, old.labels);
      END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS messages_fts_update
      AFTER UPDATE ON messages BEGIN
        INSERT INTO messages_fts(messages_fts, rowid, subject, body_text, from_address, labels)
        VALUES ('delete', old.rowid, old.subject, old.body_text, old.from_address, old.labels);
        INSERT INTO messages_fts(rowid, subject, body_text, from_address, labels)
        VALUES (new.rowid, new.subject, new.body_text, new.from_address, new.labels);
      END;
    """,
]

EVENTS_FTS_DDL = """
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(
  title,
  description,
  location,
  content='events',
  content_rowid='rowid',
  tokenize='unicode61 remove_diacritics 2'
);
"""

EVENTS_FTS_TRIGGERS = [
    """
    CREATE TRIGGER IF NOT EXISTS events_fts_insert
      AFTER INSERT ON events BEGIN
        INSERT INTO events_fts(rowid, title, description, location)
        VALUES (new.rowid, new.title, new.description, new.location);
      END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_fts_delete
      AFTER DELETE ON events BEGIN
        INSERT INTO events_fts(events_fts, rowid, title, description, location)
        VALUES ('delete', old.rowid, old.title, old.description, old.location);
      END;
    """,
    """
    CREATE TRIGGER IF NOT EXISTS events_fts_update
      AFTER UPDATE ON events BEGIN
        INSERT INTO events_fts(events_fts, rowid, title, description, location)
        VALUES ('delete', old.rowid, old.title, old.description, old.location);
        INSERT INTO events_fts(rowid, title, description, location)
        VALUES (new.rowid, new.title, new.description, new.location);
      END;
    """,
]

ALL_DDL = [
    ACCOUNTS_DDL,
    THREADS_DDL,
    MESSAGES_DDL,
    CALENDARS_DDL,
    EVENTS_DDL,
    PENDING_CHANGES_DDL,
    SYNC_LOG_DDL,
    *INDEXES_DDL,
    MESSAGES_FTS_DDL,
    *MESSAGES_FTS_TRIGGERS,
    EVENTS_FTS_DDL,
    *EVENTS_FTS_TRIGGERS,
]
