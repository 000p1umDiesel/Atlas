"""Frozen historical SQLite schemas used only to test the one-way importer.

Application stores never construct these databases. The queue intentionally predates
write_state so the migration's recovery backfill is exercised.
"""

import sqlite3
from contextlib import contextmanager

SCHEMAS = {
    "state": """
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, doc_id TEXT NOT NULL, size INTEGER, mtime REAL,
    status TEXT, updated_at TEXT);
CREATE INDEX IF NOT EXISTS files_doc ON files(doc_id);
CREATE TABLE IF NOT EXISTS stages(
    doc_id TEXT, stage TEXT, status TEXT, attempts INTEGER DEFAULT 0, error TEXT,
    updated_at TEXT, PRIMARY KEY(doc_id, stage));
CREATE TABLE IF NOT EXISTS chunk_extract(
    chunk_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER DEFAULT 0, error TEXT);
CREATE TABLE IF NOT EXISTS extraction_cache(
    chunk_id TEXT, prompt_version TEXT, model TEXT, result_json TEXT,
    PRIMARY KEY(chunk_id, prompt_version, model));
CREATE TABLE IF NOT EXISTS dirty_entities(entity_id TEXT PRIMARY KEY, marked_at TEXT);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS pending_removals(doc_id TEXT PRIMARY KEY, plan_json TEXT NOT NULL);
""",
    "service": """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0, 1))
);
CREATE TABLE IF NOT EXISTS teams (id TEXT PRIMARY KEY, name TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS memberships (
    team_id TEXT NOT NULL REFERENCES teams(id), user_id TEXT NOT NULL REFERENCES users(id),
    role TEXT NOT NULL CHECK(role IN ('admin', 'member')), PRIMARY KEY(team_id, user_id)
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id),
    expires_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS sessions_user ON sessions(user_id);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY, team_id TEXT NOT NULL REFERENCES teams(id),
    user_id TEXT NOT NULL REFERENCES users(id), title TEXT NOT NULL, created_at TEXT NOT NULL,
    UNIQUE(id, team_id, user_id)
);
CREATE INDEX IF NOT EXISTS conversations_owner ON conversations(team_id, user_id, created_at);
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, team_id TEXT NOT NULL,
    user_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
    mode TEXT NOT NULL, sources TEXT NOT NULL, status TEXT NOT NULL, query TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(conversation_id, team_id, user_id)
        REFERENCES conversations(id, team_id, user_id)
);
CREATE INDEX IF NOT EXISTS messages_owner ON messages(team_id, user_id, conversation_id, created_at);
""",
    "jobs": """
            CREATE TABLE IF NOT EXISTS jobs(
                id INTEGER PRIMARY KEY, path TEXT NOT NULL, fingerprint TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
                available_at REAL NOT NULL DEFAULT 0, error TEXT, progress TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL DEFAULT 'ingest', payload_json TEXT, UNIQUE(path, fingerprint));
            CREATE INDEX IF NOT EXISTS jobs_ready ON jobs(status, available_at, id);
            CREATE TABLE IF NOT EXISTS desired(path TEXT PRIMARY KEY, fingerprint TEXT NOT NULL);
        """,
    "uploads": """CREATE TABLE uploads(doc_id TEXT PRIMARY KEY, name TEXT NOT NULL, path TEXT NOT NULL);""",
}


@contextmanager
def legacy_database(path, kind):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.executescript(SCHEMAS[kind])
        yield connection
        connection.commit()
    finally:
        connection.close()
