from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import ClassVar

from typer.testing import CliRunner

from brain.cli.main import app
from brain.models import EntityType, FactCandidate, FactObjectType, PageType
from brain.pipeline.signal_detect import SignalEntity, SignalExtraction

DIMENSION = 1536
EVENT_ID = "01KQA8R9KVCG906A0203VYEQF7"


class FakeEmbeddingClient:
    calls: ClassVar[list[list[str]]] = []

    def __init__(self, _config) -> None:
        self.last_call_tokens = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        self.last_call_tokens = sum(max(1, len(text) // 4) for text in texts)
        return [[1.0] * DIMENSION for _ in texts]


def test_phase_2_smoke_playbook(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    root = tmp_path / "brain-smoke"
    import_dir = tmp_path / "import-fixture"
    import_dir.mkdir()
    (import_dir / "note1.md").write_text("# Note 1\nThis is the first imported note.\n", encoding="utf-8")
    (import_dir / "note2.md").write_text("# Note 2\nThis is the second imported note.\n", encoding="utf-8")
    _patch_llm(monkeypatch)
    _patch_embeddings(monkeypatch)

    assert runner.invoke(app, ["init", "--root", str(root)]).exit_code == 0
    capture = runner.invoke(
        app,
        ["capture", "--brain-root", str(root), "--stdin"],
        input="Today I read a Transformer paper by Vaswani about self-attention.",
    )
    assert capture.exit_code == 0, capture.stderr

    ingest = runner.invoke(app, ["ingest", "--brain-root", str(root), "--no-auto-reindex"])
    assert ingest.exit_code == 0, ingest.stderr
    assert "processed=1" in ingest.stdout

    reindex = runner.invoke(app, ["reindex", "--brain-root", str(root)])
    assert reindex.exit_code == 0, reindex.stderr
    assert "added=" in reindex.stdout

    ask = runner.invoke(app, ["ask", "--brain-root", str(root), "Who wrote the Transformer paper?"])
    assert ask.exit_code == 0, ask.stderr
    assert "Vaswani" in ask.stdout

    estimate = runner.invoke(app, ["cost-estimate", str(import_dir), "--kind", "md"])
    assert estimate.exit_code == 0, estimate.stderr
    assert "files=2" in estimate.stdout

    imported = runner.invoke(app, ["import", "--brain-root", str(root), str(import_dir), "--kind", "md", "--yes"])
    assert imported.exit_code == 0, imported.stderr
    assert "processed=2" in imported.stdout

    laundry_files = [
        path
        for path in (root / "laundry").rglob("*.md")
        if "processed" not in path.parts
    ]
    assert len(laundry_files) >= 2

    status = runner.invoke(app, ["status", "--brain-root", str(root), "--json"])
    assert status.exit_code == 0, status.stderr
    payload = json.loads(status.stdout)
    assert payload["embedding_coverage"] is not None
    assert payload["total_cost_usd"] is not None


def _patch_llm(monkeypatch) -> None:
    def fake_detect_signal(_text: str, hint: dict[str, object] | None = None) -> SignalExtraction:
        source_event = str((hint or {}).get("source_event") or EVENT_ID)
        source_ref = str((hint or {}).get("source_ref") or "smoke")
        return SignalExtraction(
            entities=[
                SignalEntity(
                    name="Vaswani",
                    type=EntityType.PERSON,
                    confidence=0.98,
                    metadata={},
                ),
            ],
            facts=[
                FactCandidate(
                    subject="vaswani",
                    predicate="authored",
                    object="Transformer paper",
                    object_type=FactObjectType.LITERAL,
                    valid_from="2026-04-30",
                    valid_to=None,
                    source_event=source_event,
                    source_ref=source_ref,
                    confidence=0.95,
                )
            ],
            timeline_summary="Vaswani authored the Transformer paper about self-attention.",
            suggested_page_type=PageType.ENTITY,
        )

    ingest_module = importlib.import_module("brain.pipeline.ingest")
    monkeypatch.setattr(ingest_module, "detect_signal", fake_detect_signal)


def _patch_embeddings(monkeypatch) -> None:
    FakeEmbeddingClient.calls = []
    reindex_module = importlib.import_module("brain.pipeline.reindex")
    monkeypatch.setattr(reindex_module, "OpenAICompatibleEmbeddingClient", FakeEmbeddingClient)
    monkeypatch.setattr("brain.llm.embedding.OpenAICompatibleEmbeddingClient", FakeEmbeddingClient)
