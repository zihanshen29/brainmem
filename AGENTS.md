# BrainMem Agent Operating Notes

This repository contains the BrainMem codebase. The user's active BrainMem data
root is:

```powershell
E:\docu\brain-root
```

When operating against the user's memory data from this repo or any other
working directory, pass:

```powershell
--brain-root E:\docu\brain-root
```

Use `mem` for normal CLI examples. If the wrapper is unavailable, use the direct
CLI at `E:\docu\brain\.venv\Scripts\mem.exe`.

## Contribution Rules

- Do not edit `brain.db` or `events.jsonl` manually.
- Do not rewrite or delete user memory files except through the supported
  BrainMem commands and only when the user has asked for that operation.
- Git commands in this workspace must set `$env:GIT_CONFIG_GLOBAL='NUL'`.
- Keep docs consistent with the user-level BrainMem policy. Repository docs may
  add operational detail, but must not weaken privacy, review, or consent rules.
- Prefer ASCII in agent-facing docs unless a target file already uses another
  character set or the user explicitly asks otherwise.

## When Agents Should Query Memory

Query BrainMem when the user asks to recall, search, summarize, compare, or use
long-term personal/project memory. Also query when durable memory would
materially reduce uncertainty for a task the user has delegated, such as
checking known preferences, project status, prior decisions, or open threads.

Use local-only retrieval first unless the user has explicitly allowed provider
use:

```powershell
mem ask "query" --brain-root E:\docu\brain-root --mode keyword-only --top 5
```

Do not query BrainMem for routine code reading, normal repository navigation,
or facts that are already present in the current conversation or workspace
files. Do not use memory as a substitute for reading the relevant code.

## When Agents Should Write Memory

Write memory only when the user asks to save, remember, ingest, promote, or
otherwise update BrainMem, or when the task clearly includes creating durable
memory artifacts. Agent-created notes must include source context in the note
text, for example:

```text
source_agent: codex
source_context: <short task or conversation summary>
```

Use `mem capture` for quick, raw notes that should land in laundry for later
ingest and review. Capture is appropriate for user-stated preferences, durable
project decisions, handoff notes, todo/context snapshots, and observations the
user explicitly wants remembered. Capture is not appropriate for transient
debug output, secrets, API keys, or unverified guesses.

Example:

```powershell
@"
source_agent: codex
source_context: agent handoff

<memory text>
"@ | mem capture --brain-root E:\docu\brain-root --stdin
```

## When Agents Should Not Query Or Write

- Do not query or write memory when the user asks to avoid memory use.
- Do not write secrets, credentials, private keys, tokens, or raw sensitive
  content unless the user explicitly instructs it and understands the storage
  implications.
- Do not run provider-backed commands over sensitive content without clear
  permission.
- Do not automatically approve, reject, or apply review queue items.
- Do not use deprecated MemPalace commands for new memory work unless the user
  explicitly requests an old-backup audit.

## Privacy Boundary

Local-only commands do not call an external model or embedding provider:

- `mem status --brain-root E:\docu\brain-root`
- `mem ask "query" --brain-root E:\docu\brain-root --mode keyword-only`
- `mem cost-estimate ...`
- deterministic rebuild/lint commands such as `mem lint --all` and
  `mem rebuild --backlinks --index`

Provider-backed commands require explicit user permission before use on
sensitive or user-provided content:

- default hybrid `mem ask "query"` because the query is embedded
- `mem ask "query" --explain`
- `mem ingest`
- `mem reindex`
- `mem promote-chat`
- review apply flows that rewrite compiled truth

Before provider-backed commands, hydrate API keys from the Windows user
environment. Never print, store, or commit API keys.

## Review Queue Rule

The review queue is human-controlled. Agents may list, inspect, or summarize
pending review items when asked. Agents must not approve, reject, select a
decision, or apply review items unless the user explicitly instructs that exact
action.

## CLI And MCP Tool Correspondence

Every MCP tool or skill wrapper for BrainMem should map cleanly to a supported
CLI operation and preserve the same privacy and consent boundary:

| Agent intent | CLI operation | MCP/tool behavior |
| --- | --- | --- |
| Check health | `mem status` | Safe local status check. |
| Local recall | `mem ask --mode keyword-only` | Default recall path when consent for providers is absent. |
| Hybrid recall | `mem ask` | Provider-backed; call only after explicit permission where needed. |
| Explain answer | `mem ask --explain` | Provider-backed; call only after explicit permission. |
| Capture note | `mem capture --stdin` | Writes raw note to laundry; include source context. |
| Estimate import | `mem cost-estimate` | Local cost planning before ingest/import. |
| Ingest laundry | `mem ingest` | Provider-backed; requires explicit permission. |
| Build embeddings | `mem reindex` | Provider-backed; requires explicit permission. |
| Promote chat | `mem promote-chat` | Provider-backed and writes memory; requires explicit permission. |
| Review queue | `mem review` | Inspect only unless the user explicitly asks to approve/reject/apply. |
| Repair indexes | `mem lint`, `mem rebuild` | Local deterministic maintenance unless a future option calls a provider. |

MCP docstrings and skill instructions must emphasize when to call the tool, not
only what it does. They should state whether the tool is local-only,
provider-backed, or writes durable memory, and should require
`--brain-root E:\docu\brain-root` or the equivalent structured argument.
