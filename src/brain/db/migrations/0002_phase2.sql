-- =============================================================
-- Phase 2 migration
-- Requires sqlite-vec extension to be loaded before execution.
-- =============================================================

CREATE VIRTUAL TABLE embeddings USING vec0(
    embedding float[1536]
);

CREATE TABLE embedding_index (
    rowid           INTEGER PRIMARY KEY,
    page_slug       TEXT NOT NULL,
    chunk_kind      TEXT NOT NULL,
    chunk_id        TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    model           TEXT NOT NULL,
    text_preview    TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    UNIQUE (page_slug, chunk_kind, chunk_id)
);

CREATE INDEX idx_embedding_index_page ON embedding_index(page_slug);
CREATE INDEX idx_embedding_index_hash ON embedding_index(content_hash);

CREATE TABLE import_jobs (
    id               TEXT PRIMARY KEY,
    source_path      TEXT NOT NULL,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,
    total_files      INTEGER NOT NULL,
    processed_files  INTEGER NOT NULL DEFAULT 0,
    failed_files     INTEGER NOT NULL DEFAULT 0,
    estimated_tokens INTEGER,
    estimated_usd    REAL,
    actual_tokens    INTEGER NOT NULL DEFAULT 0,
    metadata         TEXT,
    CHECK (status IN ('running', 'completed', 'failed', 'paused'))
);

CREATE TABLE import_files (
    job_id        TEXT NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL,
    file_hash     TEXT NOT NULL,
    kind          TEXT NOT NULL,
    status        TEXT NOT NULL,
    laundry_path  TEXT,
    error         TEXT,
    processed_at  TEXT,
    PRIMARY KEY (job_id, file_path),
    CHECK (status IN ('pending', 'extracted', 'ingested', 'failed', 'skipped'))
);

CREATE TABLE stats (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

INSERT INTO stats (key, value, updated_at) VALUES
    ('last_reindex_at', '', datetime('now')),
    ('total_embedding_tokens', '0', datetime('now')),
    ('total_extraction_tokens', '0', datetime('now')),
    ('total_cost_usd', '0', datetime('now'));
