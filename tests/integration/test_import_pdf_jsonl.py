from __future__ import annotations

import sqlite3
import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import frontmatter
import pytest
from typer.testing import CliRunner

from brain.cli.main import app
from brain.import_.importer import import_path


class _FakePage:
    def __init__(self, text: str | None) -> None:
        self.text = text

    def extract_text(self) -> str | None:
        return self.text


def test_import_path_imports_pdf_and_jsonl_to_laundry_with_chat_kind(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed"
    source.mkdir()
    (source / "paper.pdf").write_bytes(b"fake")
    (source / "chat.jsonl").write_text(
        '{"id":"chat-1","model":"gpt","messages":[{"role":"user","content":"Question"},{"role":"assistant","content":"Answer"}]}\n',
        encoding="utf-8",
    )
    _install_fake_pypdf(monkeypatch, ["p1", "p2", "p3", "p4", "p5", "p6"])

    report = import_path(brain_root, source, kinds=["pdf", "jsonl"], yes=True)

    assert report.processed == 2
    assert report.failed == 0
    assert report.laundry == 3
    posts = [frontmatter.loads(path.read_text(encoding="utf-8")) for path in _import_laundry_files(brain_root, report.job_id)]
    kinds = sorted(post.metadata["kind"] for post in posts)
    assert kinds == ["ai_chat", "note", "note"]
    chat = next(post for post in posts if post.metadata["kind"] == "ai_chat")
    assert chat.metadata["import_conversation_id"] == "chat-1"
    assert chat.content == "**user**: Question\n\n**assistant**: Answer"
    pdf_posts = [post for post in posts if post.metadata["kind"] == "note"]
    assert [post.metadata["import_page_range"] for post in pdf_posts] == [[1, 5], [6, 6]]


def test_scanned_pdf_failure_does_not_stop_other_files(
    brain_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "mixed"
    source.mkdir()
    (source / "scan.pdf").write_bytes(b"fake")
    (source / "chat.jsonl").write_text(
        '{"conversation_id":"human-1","role":"user","content":"hello"}\n',
        encoding="utf-8",
    )
    _install_fake_pypdf(monkeypatch, ["", None])

    report = import_path(brain_root, source, kinds=["pdf", "jsonl"], yes=True)

    assert report.processed == 1
    assert report.failed == 1
    assert report.laundry == 1
    assert "no text extracted (likely scanned)" in report.errors[0]
    rows = _rows(brain_root, "SELECT kind, status, error FROM import_files ORDER BY kind")
    assert [(row["kind"], row["status"]) for row in rows] == [("jsonl", "extracted"), ("pdf", "failed")]
    assert "no text extracted (likely scanned)" in rows[1]["error"]
    post = frontmatter.loads(_import_laundry_files(brain_root, report.job_id)[0].read_text(encoding="utf-8"))
    assert post.metadata["kind"] == "human_chat"


def test_cli_import_jsonl_writes_laundry(brain_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "chat.jsonl"
    source.write_text('{"conversation_id":"c","role":"user","content":"hello"}\n', encoding="utf-8")
    monkeypatch.chdir(brain_root)

    result = CliRunner().invoke(app, ["import", str(source), "--kind", "jsonl", "--yes"])

    assert result.exit_code == 0
    assert "processed=1" in result.stdout
    assert "laundry=1" in result.stdout
    post = frontmatter.loads(next((brain_root / "laundry").glob("import-*/*.md")).read_text(encoding="utf-8"))
    assert post.metadata["kind"] == "human_chat"


def _install_fake_pypdf(monkeypatch: pytest.MonkeyPatch, page_texts: list[str | None]) -> None:
    class FakeReader:
        def __init__(self, path: Path) -> None:
            del path
            self.pages = [_FakePage(text) for text in page_texts]

    monkeypatch.setitem(sys.modules, "pypdf", types.SimpleNamespace(PdfReader=FakeReader))


def _import_laundry_files(brain_root: Path, job_id: str | None) -> list[Path]:
    assert job_id is not None
    return sorted((brain_root / "laundry" / f"import-{job_id}").glob("*.md"))


def _rows(brain_root: Path, sql: str) -> list[sqlite3.Row]:
    with _db(brain_root) as conn:
        return conn.execute(sql).fetchall()


@contextmanager
def _db(brain_root: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{(brain_root / 'brain.db').as_posix()}?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
