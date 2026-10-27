-- mt database schema, version 1 (matches PRAGMA user_version in a DB mt created).
--
-- This is the CONTRACT. A fresh install gets exactly this (`mt init`). A database you
-- already had can be used instead (config.json "db"), but it must contain every table and
-- column below — `mt doctor` lists anything missing. Extra tables/columns are fine; mt
-- ignores them. To bring your own data in, see docs/DATABASE.md.
--
-- Changing this file is a schema bump: add columns only (never rename/drop), bump the
-- version line above and SCHEMA_VERSION in dbschema.py, and note it in CHANGELOG.md.

CREATE TABLE IF NOT EXISTS customers (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    short_name TEXT,                 -- the Customers/<short_name> folder slug
    email_domain TEXT,
    gmail_label TEXT,
    slack_internal TEXT,
    slack_external TEXT,
    jira_issue_key TEXT,
    drive_folder_url TEXT,
    notes TEXT,
    status TEXT DEFAULT 'active',
    relationship_start TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS contacts (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    name TEXT,
    email TEXT,
    role TEXT,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    verification_status TEXT DEFAULT 'verified',   -- 'verified' | 'needs_confirmation'
    source TEXT,
    slack_user_id TEXT,
    phone TEXT,
    company TEXT,
    aliases TEXT,
    updated_at TEXT,
    merged_into INTEGER,
    reports_to INTEGER,
    not_duplicate_of TEXT,
    archived_at TEXT,
    archived_reason TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    name TEXT NOT NULL,
    type TEXT,
    rail_url TEXT,
    status TEXT DEFAULT 'active',
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT,
    updated_by TEXT,
    priority TEXT,
    archived_at TEXT
);

CREATE TABLE IF NOT EXISTS action_items (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    project_id INTEGER REFERENCES projects(id),
    description TEXT NOT NULL,
    owner TEXT,
    due_date TEXT,
    status TEXT DEFAULT 'open',
    priority TEXT DEFAULT 'normal',
    source TEXT,                     -- 'meeting:<session-id>' links an item to a recording
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    report_scope TEXT DEFAULT 'unset',
    parent_id INTEGER REFERENCES action_items(id),
    owner_contact_id INTEGER,
    updated_by TEXT
);

CREATE TABLE IF NOT EXISTS external_links (
    id INTEGER PRIMARY KEY,
    local_table TEXT NOT NULL,
    local_id INTEGER NOT NULL,
    system TEXT NOT NULL,
    role TEXT NOT NULL,
    external_container_id TEXT,
    external_item_id TEXT NOT NULL,
    notes TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_external_links_local ON external_links(local_table, local_id);
CREATE INDEX IF NOT EXISTS idx_external_links_external
    ON external_links(system, external_container_id, external_item_id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_external_links_dedup
    ON external_links(local_table, local_id, system, role, external_item_id);

CREATE TABLE IF NOT EXISTS weekly_updates (
    id INTEGER PRIMARY KEY,
    customer_id INTEGER REFERENCES customers(id),
    week_of TEXT,
    jira_key TEXT,
    slack_channel TEXT,
    content TEXT,
    posted_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    rag_status TEXT,
    rag_reason_code TEXT,
    jira_comment_id TEXT,
    slack_ts TEXT,
    posted_by TEXT,
    edited_at TEXT
);

CREATE TABLE IF NOT EXISTS hygiene_log (
    id INTEGER PRIMARY KEY,
    area TEXT NOT NULL,
    group_key TEXT NOT NULL,
    title TEXT,
    choice TEXT,
    rows_json TEXT NOT NULL,
    n INTEGER NOT NULL,
    backup TEXT,
    applied_at TEXT NOT NULL,
    applied_by TEXT,
    undone_at TEXT,
    undone_by TEXT
);

CREATE TABLE IF NOT EXISTS hygiene_skip (
    area TEXT NOT NULL,
    group_key TEXT NOT NULL,
    sha TEXT NOT NULL,
    skipped_at TEXT NOT NULL,
    PRIMARY KEY (area, group_key)
);
