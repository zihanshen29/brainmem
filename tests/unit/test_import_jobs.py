import sqlite3
from pathlib import Path

from brain.db.migrations import init_db
from brain.import_.cost import cost_estimate
from brain.import_.discovery import discover_files
from brain.import_.jobs import (
    abort_job,
    complete_job,
    create_job,
    find_duplicate_file_hashes,
    find_latest_unfinished_job,
    get_job,
    get_job_detail,
    list_files,
    list_jobs,
    mark_extracted,
    mark_failed,
    mark_skipped,
    register_files,
)


def test_import_job_crud_status_counts_and_completion(tmp_path: Path) -> None:
    conn = _connect_import_db(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("# A\n", encoding="utf-8")
    (source / "b.txt").write_text("B\n", encoding="utf-8")
    files = discover_files(source)
    estimate = cost_estimate(files)

    try:
        job = create_job(conn, source, total_files=len(files), estimate=estimate, metadata={"source": "unit"})
        registered = register_files(conn, job.id, files)

        assert job.status == "running"
        assert job.total_files == 2
        assert job.estimated_tokens == estimate.estimated_extraction_tokens + estimate.estimated_embedding_tokens
        assert job.metadata["source"] == "unit"
        assert {file.status for file in registered} == {"pending"}

        extracted = mark_extracted(conn, job.id, "a.md", "laundry/a.md")
        failed = mark_failed(conn, job.id, "b.txt", "bad encoding")

        assert extracted.status == "extracted"
        assert extracted.laundry_path == "laundry/a.md"
        assert failed.status == "failed"
        assert failed.error == "bad encoding"
        refreshed = get_job(conn, job.id)
        assert refreshed.processed_files == 2
        assert refreshed.failed_files == 1

        completed = complete_job(conn, job.id)

        assert completed.status == "completed"
        assert completed.finished_at is not None
        assert find_latest_unfinished_job(conn) is None
    finally:
        conn.close()


def test_latest_unfinished_and_skipped_files(tmp_path: Path) -> None:
    conn = _connect_import_db(tmp_path)
    first_source = _source_with_file(tmp_path, "first", "a.md", "# A\n")
    second_source = _source_with_file(tmp_path, "second", "b.md", "# B\n")

    try:
        first = create_job(conn, first_source, total_files=1)
        second = create_job(conn, second_source, total_files=1)
        register_files(conn, second.id, discover_files(second_source))

        skipped = mark_skipped(conn, second.id, "b.md", "duplicate")
        latest = find_latest_unfinished_job(conn)

        assert skipped.status == "skipped"
        assert latest is not None
        assert latest.id == second.id
        assert get_job(conn, first.id).status == "running"
    finally:
        conn.close()


def test_cross_job_file_hash_duplicates(tmp_path: Path) -> None:
    conn = _connect_import_db(tmp_path)
    first_source = _source_with_file(tmp_path, "first", "a.md", "# Same\n")
    second_source = _source_with_file(tmp_path, "second", "copy.md", "# Same\n")

    try:
        first_files = discover_files(first_source)
        first = create_job(conn, first_source, total_files=1)
        register_files(conn, first.id, first_files)

        second_files = discover_files(second_source)
        second = create_job(conn, second_source, total_files=1)
        register_files(conn, second.id, second_files)

        duplicates = find_duplicate_file_hashes(
            conn,
            {second_files[0].file_hash},
            exclude_job_id=second.id,
        )

        assert list(duplicates) == [second_files[0].file_hash]
        assert duplicates[second_files[0].file_hash][0].job_id == first.id
        assert duplicates[second_files[0].file_hash][0].file_path == "a.md"
    finally:
        conn.close()


def test_list_files_can_filter_statuses(tmp_path: Path) -> None:
    conn = _connect_import_db(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("# A\n", encoding="utf-8")
    (source / "b.md").write_text("# B\n", encoding="utf-8")

    try:
        job = create_job(conn, source, total_files=2)
        register_files(conn, job.id, discover_files(source))
        mark_extracted(conn, job.id, "a.md", "laundry/a.md")

        assert [file.file_path for file in list_files(conn, job.id, statuses={"pending"})] == ["b.md"]
        assert [file.file_path for file in list_files(conn, job.id, statuses={"extracted"})] == ["a.md"]
    finally:
        conn.close()


def test_abort_job_marks_remaining_files_skipped_and_excludes_resume(tmp_path: Path) -> None:
    conn = _connect_import_db(tmp_path)
    source = tmp_path / "source"
    source.mkdir()
    (source / "a.md").write_text("# A\n", encoding="utf-8")
    (source / "b.md").write_text("# B\n", encoding="utf-8")

    try:
        job = create_job(conn, source, total_files=2)
        register_files(conn, job.id, discover_files(source))
        mark_failed(conn, job.id, "a.md", "temporary")

        aborted = abort_job(conn, job.id)
        detail, files = get_job_detail(conn, job.id)

        assert aborted.status == "failed"
        assert aborted.finished_at is not None
        assert aborted.metadata["aborted"] is True
        assert detail.metadata["aborted"] is True
        assert [(file.file_path, file.status, file.error) for file in files] == [
            ("a.md", "skipped", "aborted"),
            ("b.md", "skipped", "aborted"),
        ]
        assert find_latest_unfinished_job(conn) is None
        assert list_jobs(conn, limit=1)[0].id == job.id
    finally:
        conn.close()


def _connect_import_db(tmp_path: Path) -> sqlite3.Connection:
    db_path = tmp_path / "brain.db"
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _source_with_file(tmp_path: Path, dirname: str, filename: str, text: str) -> Path:
    source = tmp_path / dirname
    source.mkdir()
    (source / filename).write_text(text, encoding="utf-8")
    return source
