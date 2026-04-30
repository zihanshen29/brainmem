from __future__ import annotations

import importlib
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import frontmatter
import pytest
from typer.testing import CliRunner

from brain.cli.main import app
from brain.exceptions import BrainError
from brain.import_.importer import import_path
from brain.ledger import read_all
from brain.models import EventKind
from brain.pipeline.ingest import ingest

runner = CliRunner()


def test_import_five_markdown_files_writes_five_laundry_docs(brain_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    for index in range(5):
        (source / f"note-{index}.md").write_text(
            f"# Note {index}\n\nBody {index}\n",
            encoding="utf-8",
            newline="\n",
        )

    report = import_path(brain_root, source, kinds=["md"], yes=True)

    assert report.processed == 5
    assert report.skipped == 0
    assert report.failed == 0
    assert report.laundry == 5
    laundry_files = _import_laundry_files(brain_root, report.job_id)
    assert len(laundry_files) == 5
    for path in laundry_files:
        post = frontmatter.loads(path.read_text(encoding="utf-8"))
        assert post.metadata["kind"] == "note"
        assert post.metadata["source"] == "import"
        assert post.metadata["import_job_id"] == report.job_id
        assert "captured" in post.metadata
        assert "imported" in post.metadata
        assert post.content.startswith("# Note")


def test_long_markdown_splits_by_heading(brain_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "long.md"
    source.write_text(
        "\n\n".join(f"# Section {index}\n\n{('content ' + str(index) + ' ') * 900}" for index in range(3)),
        encoding="utf-8",
        newline="\n",
    )

    report = import_path(brain_root, source, kinds=["md"], yes=True)

    assert report.processed == 1
    assert report.laundry == 3
    contents = [path.read_text(encoding="utf-8") for path in _import_laundry_files(brain_root, report.job_id)]
    assert any("# Section 0" in content for content in contents)
    assert any("# Section 1" in content for content in contents)
    assert any("# Section 2" in content for content in contents)


def test_repeat_import_skips_previously_extracted_file_hashes(brain_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    for index in range(2):
        (source / f"note-{index}.md").write_text(f"# Note {index}\n", encoding="utf-8", newline="\n")

    first = import_path(brain_root, source, kinds=["md"], yes=True)
    second = import_path(brain_root, source, kinds=["md"], yes=True)

    assert first.processed == 2
    assert second.processed == 0
    assert second.skipped == 2
    assert second.laundry == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_files WHERE status = 'extracted'") == 2


def test_partial_laundry_write_failure_cleans_files_and_resume_does_not_duplicate(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "long.md"
    source.write_text(
        "\n\n".join(f"# Section {index}\n\n{('content ' + str(index) + ' ') * 900}" for index in range(3)),
        encoding="utf-8",
        newline="\n",
    )
    importer = __import__("brain.import_.importer", fromlist=["_write_laundry_document"])
    original_write = importer._write_laundry_document
    calls = 0

    def fail_second_write(*args: Any, **kwargs: Any) -> Path:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("disk full")
        return original_write(*args, **kwargs)

    monkeypatch.setattr("brain.import_.importer._write_laundry_document", fail_second_write)
    report = import_path(brain_root, source, kinds=["md"], yes=True)

    assert report.processed == 0
    assert report.failed == 1
    assert list((brain_root / "laundry").glob("import-*/*.md")) == []
    rows = _rows(brain_root, "SELECT status, error FROM import_files")
    assert rows[0]["status"] == "failed"
    assert "disk full" in rows[0]["error"]

    monkeypatch.setattr("brain.import_.importer._write_laundry_document", original_write)
    resume_report = import_path(brain_root, resume=True, yes=True)

    assert resume_report.processed == 1
    assert resume_report.failed == 0
    assert len(list((brain_root / "laundry").glob("import-*/*.md"))) == 3
    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_files WHERE status = 'extracted'") == 1


def test_laundry_document_write_failure_after_target_created_cleans_half_written_file(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Atomic Failure\n", encoding="utf-8", newline="\n")

    def fail_after_target_created(temp_path: Path, target_path: Path) -> None:
        temp_path.replace(target_path)
        raise OSError("replace interrupted")

    monkeypatch.setattr("brain.import_.importer._replace_laundry_temp", fail_after_target_created)

    report = import_path(brain_root, source, kinds=["md"], yes=True)

    assert report.processed == 0
    assert report.failed == 1
    assert list((brain_root / "laundry").glob("import-*/*.md")) == []
    assert list((brain_root / "laundry").glob("import-*/.*.tmp")) == []
    rows = _rows(brain_root, "SELECT status, error FROM import_files")
    assert rows[0]["status"] == "failed"
    assert "replace interrupted" in rows[0]["error"]


def test_successful_import_writes_bulk_event_and_ingest_all_skips_it(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Event Note\n", encoding="utf-8", newline="\n")

    report = import_path(brain_root, source, kinds=["md"], yes=True)

    events = list(read_all(brain_root / "events.jsonl"))
    assert len(events) == 1
    event = events[0]
    assert event.kind is EventKind.BULK_IMPORTED
    assert event.raw_payload is None
    assert event.raw_payload_path is None
    assert event.metadata["job_id"] == report.job_id
    assert event.metadata["source_kind"] == "md"
    assert event.metadata["original_path"] == str(source.resolve())

    for path in (brain_root / "laundry").glob("import-*/*.md"):
        path.unlink()

    detector_calls: list[str] = []

    def fake_detect_signal(text: str, hint: dict[str, Any] | None = None) -> None:
        del hint
        detector_calls.append(text)
        raise AssertionError("bulk_imported event should not be ingested")

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "detect_signal", fake_detect_signal)

    ingest_report = ingest(brain_root, source="all", auto_commit=False)

    assert ingest_report.processed == 0
    assert detector_calls == []


def test_progress_callback_failure_does_not_change_import_result(brain_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Progress Note\n", encoding="utf-8", newline="\n")

    def fail_progress(_update: object) -> None:
        raise RuntimeError("ui progress failed")

    report = import_path(brain_root, source, kinds=["md"], yes=True, progress_callback=fail_progress)

    assert report.processed == 1
    assert report.failed == 0
    assert report.laundry == 1
    assert len(_import_laundry_files(brain_root, report.job_id)) == 1
    rows = _rows(brain_root, "SELECT status, error FROM import_files")
    assert rows[0]["status"] == "extracted"
    assert rows[0]["error"] is None
    events = list(read_all(brain_root / "events.jsonl"))
    assert len(events) == 1
    assert events[0].kind is EventKind.BULK_IMPORTED
    assert events[0].metadata["job_id"] == report.job_id


def test_import_files_store_relative_paths_and_resume_restores_absolute_source(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes"
    nested = source / "nested"
    nested.mkdir(parents=True)
    first_path = source / "first.md"
    second_path = nested / "second.md"
    first_path.write_text("# First\n", encoding="utf-8", newline="\n")
    second_path.write_text("# Second\n", encoding="utf-8", newline="\n")

    calls = 0
    original_extract = __import__("brain.import_.importer", fromlist=["_extract_documents"])._extract_documents

    def interrupt_second(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original_extract(path)

    monkeypatch.setattr("brain.import_.importer._extract_documents", interrupt_second)
    with pytest.raises(KeyboardInterrupt):
        import_path(brain_root, source, kinds=["md"], yes=True)

    rows = _rows(brain_root, "SELECT file_path, status FROM import_files ORDER BY file_path")
    assert [row["file_path"] for row in rows] == ["first.md", "nested/second.md"]
    assert _scalar(brain_root, "SELECT status FROM import_jobs") == "paused"

    monkeypatch.setattr("brain.import_.importer._extract_documents", original_extract)
    resume_report = import_path(brain_root, resume=True, yes=True)

    assert resume_report.processed == 1
    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_files WHERE status = 'extracted'") == 2


def test_new_job_skips_any_existing_file_hash_even_pending(brain_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Existing Hash\n", encoding="utf-8", newline="\n")
    file_hash = _sha256(source)

    with _db(brain_root) as conn:
        conn.execute(
            """
            INSERT INTO import_jobs (id, source_path, started_at, status, total_files, metadata)
            VALUES ('existing-job', ?, '2026-04-30T00:00:00+00:00', 'paused', 1, '{}')
            """,
            (str(source),),
        )
        conn.execute(
            """
            INSERT INTO import_files (job_id, file_path, file_hash, kind, status)
            VALUES ('existing-job', 'note.md', ?, 'md', 'pending')
            """,
            (file_hash,),
        )
        conn.commit()

    report = import_path(brain_root, source, kinds=["md"], yes=True)

    assert report.processed == 0
    assert report.skipped == 1
    assert report.laundry == 0
    assert len(list((brain_root / "laundry").glob("import-*/*.md"))) == 0


def test_default_batch_size_comes_from_config_import_section(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Batch Config\n", encoding="utf-8", newline="\n")
    _replace_config_value(brain_root, "batch_size = 50", "batch_size = 7")
    captured: list[int] = []

    def fake_process_job_files(**kwargs: Any) -> None:
        captured.append(kwargs["batch_size"])

    monkeypatch.setattr("brain.import_.importer._process_job_files", fake_process_job_files)

    import_path(brain_root, source, kinds=["md"], yes=True)

    assert captured == [7]


def test_import_cost_confirmation_uses_config_threshold_and_callback(
    brain_root: Path,
    tmp_path: Path,
) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Cost Confirm\n", encoding="utf-8", newline="\n")
    _replace_config_value(brain_root, "cost_confirm_threshold_usd = 1.0", "cost_confirm_threshold_usd = 0.0")
    confirmations: list[tuple[int, float]] = []

    report = import_path(
        brain_root,
        source,
        kinds=["md"],
        confirm_callback=lambda estimate, threshold: confirmations.append((estimate.total_files, threshold)) or True,
    )

    assert report.processed == 1
    assert report.estimated_usd > 0
    assert confirmations == [(1, 0.0)]


def test_import_cost_confirmation_can_cancel(
    brain_root: Path,
    tmp_path: Path,
) -> None:
    source = tmp_path / "note.md"
    source.write_text("# Cost Cancel\n", encoding="utf-8", newline="\n")
    _replace_config_value(brain_root, "cost_confirm_threshold_usd = 1.0", "cost_confirm_threshold_usd = 0.0")

    with pytest.raises(BrainError, match="Import cancelled"):
        import_path(
            brain_root,
            source,
            kinds=["md"],
            confirm_callback=lambda _estimate, _threshold: False,
        )

    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_jobs") == 0


def test_dry_run_does_not_write_laundry_or_db(brain_root: Path, tmp_path: Path) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    for index in range(3):
        (source / f"note-{index}.md").write_text(f"# Note {index}\n", encoding="utf-8", newline="\n")

    report = import_path(brain_root, source, kinds=["md"], dry_run=True)

    assert report.dry_run is True
    assert report.files_found == 3
    assert report.estimated_docs == 3
    assert report.processed == 0
    assert list((brain_root / "laundry").glob("import-*/*.md")) == []
    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_jobs") == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_files") == 0


def test_resume_continues_pending_and_failed_files(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    first_path = source / "first.md"
    second_path = source / "second.md"
    first_path.write_text("# First\n", encoding="utf-8", newline="\n")
    second_path.write_text("# Second\n", encoding="utf-8", newline="\n")

    calls = 0
    original_extract = __import__("brain.import_.importer", fromlist=["_extract_documents"])._extract_documents

    def interrupt_after_first(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original_extract(path)

    monkeypatch.setattr("brain.import_.importer._extract_documents", interrupt_after_first)
    with pytest.raises(KeyboardInterrupt):
        import_path(brain_root, source, kinds=["md"], yes=True)

    monkeypatch.setattr("brain.import_.importer._extract_documents", original_extract)
    report = import_path(brain_root, resume=True, yes=True)

    assert report.processed == 1
    assert report.failed == 0
    assert len(list((brain_root / "laundry").glob("import-*/*.md"))) == 2
    statuses = _rows(brain_root, "SELECT status FROM import_files ORDER BY file_path")
    assert [row["status"] for row in statuses] == ["extracted", "extracted"]


def test_cli_import_status_list_jobs_and_cost_estimate(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    (source / "note.md").write_text("# CLI Status\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(brain_root)

    import_result = runner.invoke(app, ["import", str(source), "--kind", "md", "--yes"])
    assert import_result.exit_code == 0
    job_id = str(_scalar(brain_root, "SELECT id FROM import_jobs"))

    status_result = runner.invoke(app, ["import", "--status", job_id])
    list_result = runner.invoke(app, ["import", "--list-jobs"])
    estimate_result = runner.invoke(app, ["cost-estimate", str(source), "--kind", "md"])

    assert status_result.exit_code == 0
    assert "Import job:" in status_result.stdout
    assert f"job_id={job_id}" in status_result.stdout
    assert "progress=1/1" in status_result.stdout
    assert "extracted md note.md" in status_result.stdout
    assert list_result.exit_code == 0
    assert "Import jobs:" in list_result.stdout
    assert f"job_id={job_id}" in list_result.stdout
    assert estimate_result.exit_code == 0
    assert "Cost estimate:" in estimate_result.stdout
    assert "files=1" in estimate_result.stdout
    assert "estimated_total_usd=$" in estimate_result.stdout


def test_cli_abort_marks_job_failed_and_resume_ignores_aborted_job(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    (source / "first.md").write_text("# First\n", encoding="utf-8", newline="\n")
    (source / "second.md").write_text("# Second\n", encoding="utf-8", newline="\n")
    calls = 0
    original_extract = __import__("brain.import_.importer", fromlist=["_extract_documents"])._extract_documents

    def interrupt_after_first(path: Path) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise KeyboardInterrupt
        return original_extract(path)

    monkeypatch.setattr("brain.import_.importer._extract_documents", interrupt_after_first)
    with pytest.raises(KeyboardInterrupt):
        import_path(brain_root, source, kinds=["md"], yes=True)
    monkeypatch.setattr("brain.import_.importer._extract_documents", original_extract)
    monkeypatch.chdir(brain_root)
    job_id = str(_scalar(brain_root, "SELECT id FROM import_jobs"))

    abort_result = runner.invoke(app, ["import", "--abort", job_id])
    resume_result = runner.invoke(app, ["import", "--resume"])

    assert abort_result.exit_code == 0
    assert "Import job aborted:" in abort_result.stdout
    assert "status=failed" in abort_result.stdout
    assert "aborted=true" in abort_result.stdout
    assert resume_result.exit_code == 1
    assert "No unfinished import job to resume" in resume_result.stderr
    job_rows = _rows(brain_root, "SELECT status, metadata FROM import_jobs")
    file_rows = _rows(brain_root, "SELECT file_path, status, error FROM import_files ORDER BY file_path")
    assert job_rows[0]["status"] == "failed"
    assert '"aborted": true' in job_rows[0]["metadata"]
    assert [(row["file_path"], row["status"], row["error"]) for row in file_rows] == [
        ("first.md", "extracted", None),
        ("second.md", "skipped", "aborted"),
    ]

    fresh_report = import_path(brain_root, source, kinds=["md"], yes=True)

    assert fresh_report.processed == 1
    assert fresh_report.skipped == 1
    assert fresh_report.failed == 0
    assert _scalar(brain_root, "SELECT COUNT(*) FROM import_files WHERE status = 'extracted'") == 2
    assert len(list((brain_root / "laundry").glob("import-*/*.md"))) == 2


def test_cli_import_basic_success_path(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "notes"
    source.mkdir()
    (source / "note.md").write_text("# CLI Note\n", encoding="utf-8", newline="\n")
    monkeypatch.chdir(brain_root)

    result = runner.invoke(app, ["import", str(source), "--kind", "md", "--yes"])

    assert result.exit_code == 0
    assert "Import summary:" in result.stdout
    assert "processed=1" in result.stdout
    assert "laundry=1" in result.stdout
    assert "dry_run=false" in result.stdout
    assert len(list((brain_root / "laundry").glob("import-*/*.md"))) == 1


def _import_laundry_files(brain_root: Path, job_id: str | None) -> list[Path]:
    assert job_id is not None
    return sorted((brain_root / "laundry" / f"import-{job_id}").glob("*.md"))


def _rows(brain_root: Path, sql: str) -> list[sqlite3.Row]:
    with _db(brain_root, readonly=True) as conn:
        return conn.execute(sql).fetchall()


def _scalar(brain_root: Path, sql: str) -> object:
    return _rows(brain_root, sql)[0][0]


def _sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _replace_config_value(brain_root: Path, old: str, new: str) -> None:
    path = brain_root / "config.toml"
    path.write_text(path.read_text(encoding="utf-8").replace(old, new), encoding="utf-8", newline="\n")


@contextmanager
def _db(brain_root: Path, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    if readonly:
        conn = sqlite3.connect(f"file:{(brain_root / 'brain.db').as_posix()}?mode=ro&immutable=1", uri=True)
    else:
        conn = sqlite3.connect(brain_root / "brain.db")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
