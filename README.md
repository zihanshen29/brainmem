# Brain

Brain is a local-first personal memory system backed by a markdown knowledge base and SQLite runtime state.

## Installation

Requires Python 3.11.

Windows PowerShell:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

macOS/Linux:

```sh
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[dev]"
```

## Quick Start

API-free validation from the repository root:

```powershell
mem --version
mem init --root .\brain-root
Set-Location .\brain-root
mem status
```

To capture a note into the current brain root:

```powershell
"Remember to review the Phase 1 closeout notes." | mem capture --stdin
```

New brain roots use DeepSeek V4 Pro and Flash by default. Set the API key before commands that call the LLM, such as real ingest, review tier rewrites, `ask --explain`, and promote-chat:

```powershell
$env:DEEPSEEK_API_KEY = "sk-..."
```

`mem ingest --dry-run` does not write pages, update the database, or commit changes, but it may still call the configured LLM when there is pending content.

After a real ingest and review flow has populated `pages/`, query the brain root with:

```powershell
mem ask "What should I review?"
```

Task 20 manual smoke has not been executed yet.

## Directory Structure

Repository:

```text
brain/
  src/brain/           Python package
  files/               Phase 1 specs and planning docs
  tests/               Test suite
  README.md            Project overview and closeout notes
  pyproject.toml       Packaging, dependencies, and tool config
```

Runtime brain root:

```text
brain-root/
  raw/                 Raw captured inputs
  laundry/             Pending ingest material
  laundry/processed/   Processed ingest material
  pages/               Markdown memory pages
  review/              Review queue material
  brain.db             SQLite database
  events.jsonl         Append-only event log
```

## Specs

- [SPEC.md](files/SPEC.md)
- [architecture.md](files/architecture.md)
- [data-model.md](files/data-model.md)
- [pipeline.md](files/pipeline.md)
- [cli.md](files/cli.md)
- [tech-stack.md](files/tech-stack.md)
- [phase-1-tasks.md](files/phase-1-tasks.md)

## Quality / Verification

- `.\.venv\Scripts\pytest.exe`: 200 passed.
- `.\.venv\Scripts\ruff.exe check .`: passed.

No open questions were found during Task 19 closeout, so `OPEN_QUESTIONS.md` was not created.

## Phase 1 Checklist

- Task 0: complete
- Task 1: complete
- Task 2: complete
- Task 3: complete
- Task 4: complete
- Task 5: complete
- Task 6: complete
- Task 7: complete
- Task 8: complete
- Task 9: complete
- Task 10: complete
- Task 11: complete
- Task 12: complete
- Task 13: complete
- Task 14: complete
- Task 15: complete
- Task 16: complete
- Task 17: complete
- Task 18: complete
- Task 19: complete
- Task 20: incomplete, pending manual smoke
