# BrainMem Runtime SOP

Use this skill when the user asks to save, recall, search, ingest, review, or
operate BrainMem long-term memory. BrainMem is the user's durable personal
memory system, not a general code search tool.

Canonical paths:

- Data root: `E:\docu\brain-root`
- Code repo: `E:\docu\brain`
- CLI wrapper: `mem`
- Direct CLI: `E:\docu\brain\.venv\Scripts\mem.exe`
- Operating guide: `E:\docu\brain-root\AI_USAGE_GUIDE.md`

Always pass `--brain-root E:\docu\brain-root` unless the command is already
running inside that data root. BrainMem must work from any project directory.

## Fast Decision SOP

1. If the user asks to recall/search/use memory, start with local-only recall:

   ```powershell
   mem ask "query" --brain-root E:\docu\brain-root --mode keyword-only --top 5
   ```

2. If the user asks to save/remember/capture a note, use `mem capture --stdin`
   and include source context in the note text.

3. If the user asks to ingest, reindex, explain, promote, or use hybrid
   retrieval, treat it as provider-backed unless they explicitly selected a
   local-only mode. Get clear permission before sending sensitive content to a
   provider.

4. If the user asks about review items, inspect or summarize only. Do not
   approve, reject, select decisions, or apply review items without explicit
   instruction.

5. If the request is non-trivial memory work, read
   `E:\docu\brain-root\AI_USAGE_GUIDE.md` before acting.

## When To Query

Query BrainMem when:

- The user asks to recall, search, summarize, or compare saved memory.
- The user asks what is known about a project, preference, person, decision, or
  previous thread.
- A task would materially benefit from durable context that is unlikely to be in
  the current repository or conversation.

Prefer local-only retrieval first. Use hybrid/default `mem ask` only when the
user has allowed provider-backed retrieval or when the content is not sensitive
and permission is clear from the request.

Do not query BrainMem when:

- The user says not to use memory.
- The answer should come from current workspace files, tests, logs, or the
  active conversation.
- You are only doing ordinary code navigation.
- The query would disclose sensitive content to a provider and permission is
  absent.

## When To Write

Write to BrainMem when:

- The user says to save, remember, capture, ingest, promote, or persist
  something.
- The user provides a handoff, durable project decision, preference, or
  long-lived context and asks you to store it.
- The task explicitly includes creating memory artifacts.

Use capture for raw memory intake:

```powershell
@"
source_agent: codex
source_context: <short context>

<note text>
"@ | mem capture --brain-root E:\docu\brain-root --stdin
```

Capture is appropriate for durable notes that should enter laundry for later
ingest. It is not a final truth write. Do not capture secrets, credentials,
large raw logs, or unverified speculation.

## Privacy Boundary

Local-only commands:

- `mem status --brain-root E:\docu\brain-root`
- `mem ask "query" --brain-root E:\docu\brain-root --mode keyword-only`
- `mem cost-estimate ...`
- deterministic rebuild/lint commands, including `mem lint --all` and
  `mem rebuild --backlinks --index`

Provider-backed commands that need clear permission for sensitive content:

- default hybrid `mem ask "query"`
- `mem ask "query" --explain`
- `mem ingest`
- `mem reindex`
- `mem promote-chat`
- review apply rewrites or other flows that rewrite compiled truth with model
  help

Before provider-backed commands, hydrate API keys from the Windows user
environment. Never print, store, or commit API keys.

## Review Queue SOP

The review queue is not agent-autonomous.

Allowed without decision permission:

- Show pending review count/status.
- Read review items when the user asks.
- Summarize options and risks.

Not allowed without explicit user instruction:

- Approve a review item.
- Reject a review item.
- Select or edit a review decision.
- Run apply flows.
- Rewrite compiled truth through review application.

## CLI And MCP Tool Mapping

MCP tools should be thin, consent-aware wrappers around the CLI or equivalent
pipeline calls. Tool docstrings must tell agents when to call the tool and must
label privacy/write behavior.

| Need | CLI | MCP tool expectation |
| --- | --- | --- |
| Health/status | `mem status` | Local-only status; safe default check. |
| Local recall | `mem ask --mode keyword-only` | Default search tool when provider consent is absent. |
| Hybrid recall | `mem ask` | Provider-backed search; requires permission where content is sensitive. |
| Explanation | `mem ask --explain` | Provider-backed answer synthesis; requires permission. |
| Capture | `mem capture --stdin` | Durable write to laundry; require source context. |
| Cost planning | `mem cost-estimate` | Local-only estimate before import/ingest. |
| Ingest | `mem ingest` | Provider-backed extraction/write pipeline; requires permission. |
| Reindex | `mem reindex` | Embedding provider call; requires permission. |
| Promote chat | `mem promote-chat` | Provider-backed durable write; requires permission. |
| Review | `mem review` | Inspect/summarize by default; apply only on explicit instruction. |
| Maintenance | `mem lint`, `mem rebuild` | Local deterministic maintenance unless options change that boundary. |

Every CLI or MCP call must target `E:\docu\brain-root` through
`--brain-root E:\docu\brain-root` or an equivalent structured argument.

## Minimal Command Examples

```powershell
mem status --brain-root E:\docu\brain-root
mem ask "query" --brain-root E:\docu\brain-root --mode keyword-only --top 5
mem cost-estimate .\notes --brain-root E:\docu\brain-root --kind md,txt,pdf,jsonl
```

Provider-backed examples, run only after the consent checks above:

```powershell
mem ask "query" --brain-root E:\docu\brain-root
mem ask "query" --brain-root E:\docu\brain-root --explain
mem ingest --brain-root E:\docu\brain-root
mem reindex --brain-root E:\docu\brain-root
mem promote-chat <event-id> --brain-root E:\docu\brain-root
```
