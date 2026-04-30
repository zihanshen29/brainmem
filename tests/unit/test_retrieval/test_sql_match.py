import sqlite3

from brain.pipeline.retrieval import sql_entity_match


def test_sql_entity_match_finds_alias_entity_page_and_backlink() -> None:
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
        CREATE TABLE backlinks (
            from_page TEXT NOT NULL,
            to_entity TEXT NOT NULL,
            relation TEXT NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO entities (id, title, page_path) VALUES (?, ?, ?)",
        ("xiao-zhang", "Xiao Zhang", "pages/entities/xiao-zhang.md"),
    )
    conn.execute(
        "INSERT INTO entity_aliases (alias, entity_id) VALUES (?, ?)",
        ("小张", "xiao-zhang"),
    )
    conn.execute(
        "INSERT INTO backlinks (from_page, to_entity, relation) VALUES (?, ?, ?)",
        ("cv-coursework", "xiao-zhang", "mentions"),
    )

    hits = sql_entity_match(conn, "小张", top=10)

    assert [hit.page_slug for hit in hits] == ["xiao-zhang", "cv-coursework"]
    assert [hit.rank for hit in hits] == [1, 2]
    assert all(hit.path == "sql" for hit in hits)
