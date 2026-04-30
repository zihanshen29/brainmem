import sqlite3

from brain.pipeline.retrieval import sql_direct_query


def conn_with_schema() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE entities (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            page_path TEXT
        );
        CREATE TABLE entity_aliases (
            alias TEXT NOT NULL,
            entity_id TEXT NOT NULL
        );
        CREATE TABLE facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            subject TEXT NOT NULL,
            predicate TEXT NOT NULL,
            object TEXT NOT NULL,
            object_type TEXT NOT NULL,
            valid_from TEXT,
            valid_to TEXT,
            asserted_at TEXT NOT NULL,
            source_event TEXT NOT NULL,
            source_ref TEXT,
            confidence REAL NOT NULL,
            superseded_by INTEGER
        );
        """
    )
    return conn


def test_sql_direct_parse_failure_returns_empty() -> None:
    conn = conn_with_schema()

    assert sql_direct_query(conn, "DROP TABLE facts; SELECT * FROM facts", top=5) == []
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_sql_direct_does_not_execute_arbitrary_sql_inside_structured_query() -> None:
    conn = conn_with_schema()

    assert sql_direct_query(conn, "\u5217\u51fa DROP TABLE facts", top=5) == []
    assert conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0] == 0


def test_sql_direct_maps_fact_subject_to_canonical_page_not_source_ref() -> None:
    conn = conn_with_schema()
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("xiao-zhang", "Xiao Zhang", "pages/entities/xiao-zhang.md"),
    )
    conn.execute(
        "INSERT INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
        ("\u5c0f\u5f20", "xiao-zhang"),
    )
    conn.execute(
        """
        INSERT INTO facts (
            subject, predicate, object, object_type, valid_from, valid_to,
            asserted_at, source_event, source_ref, confidence, superseded_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "xiao-zhang",
            "helped",
            "cv-coursework",
            "entity",
            None,
            None,
            "2026-04-30T12:00:00",
            "event-1",
            "events.jsonl",
            0.95,
            None,
        ),
    )

    results = sql_direct_query(conn, "\u5217\u51fa\u5c0f\u5f20\u7684\u4e8b", top=5)

    assert [result.page_slug for result in results] == ["xiao-zhang"]


def test_sql_direct_maps_entity_object_when_subject_has_no_page() -> None:
    conn = conn_with_schema()
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("cv-coursework", "CV Coursework", "pages/projects/cv-coursework.md"),
    )
    conn.execute(
        "INSERT INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
        ("coursework", "cv-coursework"),
    )
    conn.execute(
        """
        INSERT INTO facts (
            subject, predicate, object, object_type, valid_from, valid_to,
            asserted_at, source_event, source_ref, confidence, superseded_by
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "unmapped-subject",
            "mentions",
            "cv-coursework",
            "entity",
            None,
            None,
            "2026-04-30T12:00:00",
            "event-2",
            "laundry/foo.md",
            0.9,
            None,
        ),
    )

    results = sql_direct_query(conn, "\u5217\u51fa coursework", top=5)

    assert [result.page_slug for result in results] == ["cv-coursework"]


def test_sql_direct_groups_multiple_fact_hits_by_page_and_reranks_pages() -> None:
    conn = conn_with_schema()
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("xiao-zhang", "Xiao Zhang", "pages/entities/xiao-zhang.md"),
    )
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("cv-coursework", "CV Coursework", "pages/projects/cv-coursework.md"),
    )
    facts = [
        (
            "xiao-zhang",
            "did",
            "review",
            "literal",
            "2026-04-30T12:02:00",
            "event-1",
            "events.jsonl",
        ),
        (
            "cv-coursework",
            "did",
            "baseline",
            "literal",
            "2026-04-30T12:01:00",
            "event-2",
            "laundry/foo.md",
        ),
        (
            "xiao-zhang",
            "did",
            "planning",
            "literal",
            "2026-04-30T12:00:00",
            "event-3",
            "raw/bar.md",
        ),
    ]
    for subject, predicate, object_value, object_type, asserted_at, event, source_ref in facts:
        conn.execute(
            """
            INSERT INTO facts (
                subject, predicate, object, object_type, valid_from, valid_to,
                asserted_at, source_event, source_ref, confidence, superseded_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject,
                predicate,
                object_value,
                object_type,
                None,
                None,
                asserted_at,
                event,
                source_ref,
                0.9,
                None,
            ),
        )

    results = sql_direct_query(conn, "\u5217\u51fa 2026 \u5e74\u7684\u4e8b", top=5)

    assert [result.page_slug for result in results] == ["xiao-zhang", "cv-coursework"]
    assert [result.final_rank for result in results] == [1, 2]
    assert len(results[0].chunks) == 2
    assert len(results[1].chunks) == 1


def test_sql_direct_sorts_grouped_pages_by_accumulated_score() -> None:
    conn = conn_with_schema()
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("page-a", "Page A", "pages/entities/page-a.md"),
    )
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("page-b", "Page B", "pages/entities/page-b.md"),
    )
    facts = [
        ("page-a", "rank1", "2026-04-30T12:04:00", "event-a1"),
        ("page-b", "rank2", "2026-04-30T12:03:00", "event-b1"),
        ("page-b", "rank3", "2026-04-30T12:02:00", "event-b2"),
        ("page-b", "rank4", "2026-04-30T12:01:00", "event-b3"),
    ]
    for subject, object_value, asserted_at, event in facts:
        conn.execute(
            """
            INSERT INTO facts (
                subject, predicate, object, object_type, valid_from, valid_to,
                asserted_at, source_event, source_ref, confidence, superseded_by
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                subject,
                "did",
                object_value,
                "literal",
                None,
                None,
                asserted_at,
                event,
                "events.jsonl",
                0.9,
                None,
            ),
        )

    results = sql_direct_query(conn, "\u5217\u51fa 2026 \u5e74\u7684\u4e8b", top=5)

    assert [result.page_slug for result in results] == ["page-b", "page-a"]
    assert [result.final_rank for result in results] == [1, 2]
    assert len(results[0].chunks) == 3
    assert results[0].rrf_score > results[1].rrf_score
