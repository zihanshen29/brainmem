# BrainMem

<p align="center">
  <strong>A local-first life memory system for people and agents.</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-CN.md">中文</a>
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB">
  <img alt="CLI mem" src="https://img.shields.io/badge/CLI-mem-111827">
  <img alt="Tests" src="https://img.shields.io/badge/tests-438%20passed-16A34A">
  <img alt="Local first" src="https://img.shields.io/badge/local--first-Markdown%20%2B%20SQLite-0F766E">
</p>

## What It Is

BrainMem is a local-first memory system for recording a person's long-running life, work, learning, projects, relationships, decisions, and conversations as durable knowledge. It is designed for people who want their own private memory base, and for coding agents that need a reliable, inspectable source of personal context.

The core idea is a **memory compiler**: raw notes, imported files, chats, and event logs are compiled into an Obsidian-friendly Markdown wiki plus SQLite indexes. The system keeps provenance, timeline history, review queues, and structured facts instead of silently rewriting a hidden vector database.

It is local-first by default: your runtime brain root stays on your machine. LLM calls are explicit and only happen for commands that need extraction, rewriting, or explanation.

Phase 2 adds hybrid retrieval, sqlite-vec embeddings, bulk import for Markdown/Text/PDF/JSONL, import progress tracking, cost estimates, status telemetry, and agent-runtime workflows such as token-bounded injection, scratch notes, snapshots, and reusable procedures.

## Why It Is Different

Most AI memory systems optimize for application integration: add a message, search memories, inject context. BrainMem optimizes for **personal memory ownership**. It treats memory as a long-term knowledge base that should be readable, editable, rebuildable, and auditable years later.

Different from Mem0-style per-message memory extraction or Letta-style agent-managed context, BrainMem treats memory as a long-form personal wiki that you co-author with agents.

Key design choices:

- **Life-scale scope:** meant to hold notes, reading, chats, project history, decisions, observations, preferences, and personal knowledge over years.
- **Local-first source of truth:** Markdown pages, SQLite, and append-only JSONL stay in your own directory.
- **Human-in-the-loop memory:** conflicts, low-confidence facts, new entities, and tier changes can go through review before becoming durable truth.
- **Provenance by default:** pages carry timeline entries and sources; derived indexes can be rebuilt from the source files.
- **Hybrid retrieval instead of vector-only recall:** `mem ask` combines vector search, BM25 keyword search, SQL/entity matching, and RRF.
- **Agent-friendly but not agent-owned:** agents can use it, but they do not silently control your long-term memory.

## Good Fit

BrainMem is useful for:

- building a private life log and long-term personal knowledge base;
- giving coding agents stable context about your projects, preferences, decisions, and history;
- importing old notes, Markdown vaults, PDFs, text files, and exported AI chats;
- keeping an auditable memory trail for research, writing, engineering, or personal operations;
- users who prefer files and SQLite over opaque hosted memory platforms.

It is not trying to be a multi-user SaaS memory backend, a hosted chatbot platform, a graph database service, or a fully autonomous self-editing agent runtime.

## Highlights

| Area | What BrainMem provides |
| --- | --- |
| Knowledge base | Markdown pages with frontmatter, compiled truth, timeline, and sources, including procedure pages for reusable operating steps |
| Runtime state | SQLite schema for entities, facts, backlinks, reviews, lint results, tier proposals, and local scratch/snapshot context |
| Event ledger | Append-only JSONL event log with cursor-based ingest |
| Hybrid retrieval | `mem ask` uses vector + keyword + SQL matching with RRF, with keyword-only fallback |
| Bulk import | `mem import` turns `.md`, `.txt`, `.pdf`, and `.jsonl` files into laundry items with resumable jobs |
| CLI workflow | `init`, `capture`, `ingest`, `reindex`, `import`, `cost-estimate`, `ask`, `inject`, `scratch`, `snapshot`, `procedure`, `review`, `lint`, `rebuild`, `status`, `promote-chat`, `entity` |
| LLM support | DeepSeek V4 by default for LLM work; OpenAI or OpenAI-compatible embeddings; Anthropic remains configurable |
| Privacy boundary | Keyword-only `mem ask` is local; default hybrid `mem ask` embeds the query through the embedding provider; `mem reindex` calls the embedding provider; `mem ingest` and `mem ask --explain` can call the configured LLM |

## Quick Start

Requires Python 3.11 or newer.

```powershell
git clone https://github.com/zihanshen29/brainmem.git
cd brainmem

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

$env:OPENAI_API_KEY = "<your-openai-or-compatible-embedding-key>"
mem --version
mem init --root .\brain-root
Set-Location .\brain-root
mem status
```

Capture, ingest, reindex, and ask:

```powershell
"Remember to review the Phase 1 closeout notes." | mem capture --stdin
mem ingest --source laundry
mem reindex
mem ask "What should I review?"
```

Prepare local context for an agent prompt:

```powershell
"source_agent: codex`nChecked the deploy checklist." | mem scratch append --stdin --source codex
mem snapshot rebuild
mem inject --query "deploy checklist" --mode keyword-only --budget 4000
```

Enable LLM-backed workflows:

```powershell
$env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
mem ask "What changed recently?" --explain
```

Bulk import existing material:

```powershell
mem cost-estimate .\notes --kind md,txt,pdf,jsonl
mem import .\notes --kind md,txt,pdf,jsonl --yes
mem ingest --source laundry
mem reindex
```

`mem init` writes a DeepSeek config by default for LLM extraction/rewrite work, and an OpenAI-compatible embedding config for `mem reindex`. Advanced users can mix providers by editing `config.toml`: `[deepseek]` for DeepSeek or compatible chat APIs, `[openai]` for OpenAI, `[anthropic]` for Anthropic, and `[embedding]` for OpenAI or another OpenAI-compatible embedding endpoint. Store only environment variable names in config; keep real API keys in the environment.

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
- `mem ask "query" --mode keyword-only`
- `mem inject --query "query" --mode keyword-only`
- `mem scratch append`
- `mem snapshot rebuild`
- `mem procedure new`, `mem procedure run`, `mem procedure promote`
- `mem cost-estimate`
- `mem rebuild --backlinks --index`
- `mem lint --all`

Commands that may send content to the configured LLM or embedding provider:

- `mem reindex`
- `mem ask "query"` in the default hybrid mode, because the query is embedded
- `mem ingest`
- `mem ask --explain`
- `mem promote-chat`
- review apply actions that rewrite compiled truth
- forced page rebuilds

## Verification

The implemented Phase 2 build is checked with:

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
```

Latest local result: `438 passed`, `ruff` passed.

## Specs

The root `docs/` files are the current product and design documentation:

- [SPEC.md](docs/SPEC.md)
- [architecture.md](docs/architecture.md)
- [data-model.md](docs/data-model.md)
- [pipeline.md](docs/pipeline.md)
- [cli.md](docs/cli.md)
- [tech-stack.md](docs/tech-stack.md)

Phase 1 and Phase 2 historical implementation notes are archived under [docs/archive/](docs/archive/).
