-- ==============================================================
-- Entities — entity registry
-- ==============================================================
CREATE TABLE entities (
    id              TEXT PRIMARY KEY,
    type            TEXT NOT NULL,
    title           TEXT NOT NULL,
    page_path       TEXT,
    tier            INTEGER NOT NULL DEFAULT 3,
    mention_count   INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL,
    metadata        TEXT,
    CHECK (tier IN (1, 2, 3))
);

CREATE TABLE entity_aliases (
    alias           TEXT NOT NULL,
    entity_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,
    PRIMARY KEY (alias, entity_id),
    UNIQUE (alias)
);

CREATE INDEX idx_aliases_lookup ON entity_aliases(alias);

-- ==============================================================
-- Facts — bi-temporal structured facts
-- ==============================================================
CREATE TABLE facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT NOT NULL,
    predicate       TEXT NOT NULL,
    object          TEXT NOT NULL,
    object_type     TEXT NOT NULL,
    valid_from      TEXT,
    valid_to        TEXT,
    asserted_at     TEXT NOT NULL,
    source_event    TEXT NOT NULL,
    source_ref      TEXT,
    confidence      REAL NOT NULL,
    superseded_by   INTEGER REFERENCES facts(id),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX idx_facts_subject ON facts(subject);
CREATE INDEX idx_facts_predicate ON facts(predicate);
CREATE INDEX idx_facts_active ON facts(subject, predicate)
    WHERE superseded_by IS NULL AND valid_to IS NULL;

-- ==============================================================
-- Backlinks — typed links extracted from markdown pages
-- ==============================================================
CREATE TABLE backlinks (
    from_page       TEXT NOT NULL,
    to_entity       TEXT NOT NULL REFERENCES entities(id),
    relation        TEXT NOT NULL,
    line_number     INTEGER,
    extracted_at    TEXT NOT NULL,
    PRIMARY KEY (from_page, to_entity, relation)
);

CREATE INDEX idx_backlinks_to ON backlinks(to_entity);

-- ==============================================================
-- Tier Proposals — tier upgrade proposals pending review
-- ==============================================================
CREATE TABLE tier_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    proposed_tier   INTEGER NOT NULL,
    current_tier    INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    proposed_at     TEXT NOT NULL,
    decided_at      TEXT,
    decision        TEXT,
    review_file     TEXT NOT NULL,
    CHECK (proposed_tier IN (1, 2, 3)),
    CHECK (current_tier IN (1, 2, 3))
);

-- ==============================================================
-- Ingest Cursor — pipeline progress
-- ==============================================================
CREATE TABLE ingest_cursor (
    source          TEXT PRIMARY KEY,
    last_processed  TEXT NOT NULL,
    last_run_at     TEXT NOT NULL
);

-- ==============================================================
-- Lint Results — lint history
-- ==============================================================
CREATE TABLE lint_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL,
    kind            TEXT NOT NULL,
    issue_count     INTEGER NOT NULL,
    report_file     TEXT NOT NULL
);
