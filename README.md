# BrainMem

<p align="center">
  <strong>Local-first memory for agents, backed by Markdown and SQLite.</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-CN.md">中文</a>
</p>

<p align="center">
  <img alt="Python 3.11" src="https://img.shields.io/badge/Python-3.11-3776AB">
  <img alt="CLI mem" src="https://img.shields.io/badge/CLI-mem-111827">
  <img alt="Tests" src="https://img.shields.io/badge/tests-207%20passed-16A34A">
  <img alt="Local first" src="https://img.shields.io/badge/local--first-Markdown%20%2B%20SQLite-0F766E">
</p>

## What It Is

BrainMem is a personal memory system designed for coding agents and power users. It keeps durable knowledge in an Obsidian-friendly Markdown wiki, uses SQLite for deterministic indexes and decisions, and exposes everything through a `mem` CLI.

It is local-first by default: your runtime brain root stays on your machine. LLM calls are explicit and only happen for commands that need extraction, rewriting, or explanation.

## Highlights

| Area | What BrainMem provides |
| --- | --- |
| Knowledge base | Markdown pages with frontmatter, compiled truth, timeline, and sources |
| Runtime state | SQLite schema for entities, facts, backlinks, reviews, lint results, and tier proposals |
| Event ledger | Append-only JSONL event log with cursor-based ingest |
| CLI workflow | `init`, `capture`, `ingest`, `review`, `lint`, `ask`, `rebuild`, `status`, `promote-chat`, `entity` |
| LLM support | DeepSeek V4 by default, with OpenAI and Anthropic-compatible config support |
| Privacy boundary | Plain `mem ask` is local retrieval; `mem ingest` and `mem ask --explain` can call the configured LLM |

## Quick Start

Requires Python 3.11.

```powershell
git clone https://github.com/zihanshen29/brainmem.git
cd brainmem

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

mem --version
mem init --root .\brain-root
Set-Location .\brain-root
mem status
```

Capture a note:

```powershell
"Remember to review the Phase 1 closeout notes." | mem capture --stdin
```

Use local retrieval:

```powershell
mem ask "What should I review?"
```

Enable LLM-backed workflows:

```powershell
$env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
mem ingest --source laundry
mem ask "What changed recently?" --explain
```

## Data Layout

```text
brainmem/
  src/brain/           Python package
  docs/                Specs and design docs
  tests/               Test suite
  pyproject.toml       Packaging and dependencies

brain-root/            Local runtime data, not for GitHub
  raw/                 Raw captured inputs
  laundry/             Pending ingest material
  pages/               Markdown wiki pages
  review/              Pending review queue
  brain.db             SQLite runtime/index state
  events.jsonl         Append-only event log
```

## Privacy

Do not commit runtime memory data:

- `brain-root/`
- `brain.db`, `brain.db-wal`, `brain.db-shm`
- `events.jsonl`
- `raw/`, `laundry/`, `pages/`, `review/`
- `.env` or real API keys

Local-only commands:

- `mem status`
- `mem ask "query"` without `--explain`
- `mem rebuild --backlinks --index`
- `mem lint --all`

Commands that may send content to the configured LLM:

- `mem ingest`
- `mem ask --explain`
- `mem promote-chat`
- review apply actions that rewrite compiled truth
- forced page rebuilds

## Verification

The current Phase 1 build has been checked with:

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
```

Latest local result: `207 passed`, `ruff` passed.

## Specs

- [SPEC.md](docs/SPEC.md)
- [architecture.md](docs/architecture.md)
- [data-model.md](docs/data-model.md)
- [pipeline.md](docs/pipeline.md)
- [cli.md](docs/cli.md)
- [tech-stack.md](docs/tech-stack.md)
