# BrainMem Runtime SOP

Use this skill when the user asks to save, recall, search, ingest, review, or
operate BrainMem long-term memory. BrainMem is the user's durable personal
memory system, not a general code search tool.

This public skill uses portable placeholders. The user's data root is
`${BRAIN_ROOT}` and must be resolved at runtime from:

1. An explicit `--brain-root` flag or MCP `brain_root` argument.
2. The `BRAIN_ROOT` environment variable.
3. A local user config such as `~/.config/brainmem/config.toml` with
   `data_root`.
4. Default: `~/brain`.

Machine-specific paths, direct virtualenv executables, API key setup, and
private operating notes belong in a local override such as
`skills/brainmem/SKILL.local.md`, which must not be committed.

Always pass `--brain-root "${BRAIN_ROOT}"` unless the command is already running
inside that data root. BrainMem must work from any project directory.

## Task Intake Decision Flow

### Step 1 - Task Type

- "remember/save/capture this": use `mem capture --stdin` with source context.
- "what did we decide / what do we know / search memory": start with
  `mem ask --mode keyword-only`.
- "put relevant memory into a prompt / prepare context for another LLM": use
  `mem inject`, not plain `mem ask`.
- "track what is happening in this session": use `mem scratch append`.
- "summarize current working state": use `mem snapshot rebuild`, then
  `mem inject`.
- "create/update a reusable runbook": use `mem procedure new/run/promote`.
- Generic code reading, API syntax, or facts in the active workspace: do not
  use BrainMem.

### Step 2 - Context Relevance

Ask whether durable personal/project history could materially affect the
answer. Query BrainMem for projects, preferences, people, teams, customers,
decisions, prior failures, deployments, and procedures. Do not query memory
when the answer should come from current files, logs, tests, or the active
conversation.

### Step 3 - Information Solidification

Capture only information the user asks to remember or clearly intends to
persist. Include source context in agent-created notes:

```sh
printf '%s\n' \
  'source_agent: codex' \
  'source_context: <short context>' \
  '' \
  '<note text>' |
  mem capture --brain-root "${BRAIN_ROOT}" --stdin
```

Use scratch for temporary session state that should not yet become wiki truth:

```sh
printf '%s\n' '<working context>' |
  mem scratch append --brain-root "${BRAIN_ROOT}" --stdin --source codex
```

### Step 4 - Entity Disambiguation

Before answering questions involving people, projects, customers, deployments,
or short names:

1. If the name is ambiguous, run local recall with a wider result set.
2. If multiple plausible entities appear, ask the user which one they mean.
3. If one clear match appears, proceed using that entity's compiled truth.

```sh
mem ask "name or alias" --brain-root "${BRAIN_ROOT}" --mode keyword-only --top 10
```

### Step 5 - High-Risk Lookback

Before destructive or high-risk actions such as deletion, migration, large
refactors, deploys, credentials, or data movement, search memory and procedure
capsules for prior failures and runbooks:

```sh
mem ask "migration deploy rollback failure" --brain-root "${BRAIN_ROOT}" --mode keyword-only --top 10
mem ask "deploy" --brain-root "${BRAIN_ROOT}" --mode keyword-only --type procedure --top 10
```

If relevant history is found, mention it and reuse the procedure rather than
guessing.

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

Use `mem inject` instead of `mem ask` when the result will be pasted into
another LLM context. `mem inject` is token-bounded and includes the current
snapshot by default.

## When To Write

Write to BrainMem when:

- The user says to save, remember, capture, ingest, promote, or persist
  something.
- The user provides a durable project decision, preference, or long-lived
  context and asks you to store it.
- The task explicitly includes creating memory artifacts.
- A repeatable workflow should become a procedure capsule.

Do not capture secrets, credentials, large raw logs, or unverified speculation.

## Privacy Boundary

Local-only commands:

- `mem status --brain-root "${BRAIN_ROOT}"`
- `mem ask "query" --brain-root "${BRAIN_ROOT}" --mode keyword-only`
- `mem inject --brain-root "${BRAIN_ROOT}" --mode keyword-only`
- `mem scratch append --brain-root "${BRAIN_ROOT}" ...`
- `mem snapshot rebuild --brain-root "${BRAIN_ROOT}"`
- `mem procedure new/run/promote --brain-root "${BRAIN_ROOT}"`
- `mem cost-estimate ...`
- deterministic rebuild/lint commands, including `mem lint --all` and
  `mem rebuild --backlinks --index`

Provider-backed commands that need clear permission for sensitive content:

- default hybrid `mem ask "query"`
- semantic `mem ask`
- `mem ask "query" --explain`
- `mem ingest`
- `mem reindex`
- `mem promote-chat`
- review apply rewrites or other flows that rewrite compiled truth with model
  help

Before provider-backed commands, hydrate API keys from the user's environment.
Never print, store, or commit API keys.

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
| Injection context | `mem inject` | Local by default; token-bounded markdown/text for prompt context. |
| Capture | `mem capture --stdin` | Durable write to laundry; require source context. |
| Working buffer | `mem scratch append` | Local session-progress notes, not wiki truth. |
| Distill current state | `mem snapshot rebuild` | Local deterministic snapshot from scratch. |
| Procedure runbook | `mem procedure new/run/promote` | Manual procedure SOP with maturity state. |
| Recall procedure | `mem ask --type procedure --mode keyword-only` | Find reusable runbooks before risky work. |
| Cost planning | `mem cost-estimate` | Local-only estimate before import/ingest. |
| Ingest | `mem ingest` | Provider-backed extraction/write pipeline; requires permission. |
| Reindex | `mem reindex` | Embedding provider call; requires permission. |
| Promote chat | `mem promote-chat` | Provider-backed durable write; requires permission. |
| Review | `mem review` | Inspect/summarize by default; apply only on explicit instruction. |
| Maintenance | `mem lint`, `mem rebuild` | Local deterministic maintenance unless options change that boundary. |

## Minimal Command Examples

```sh
mem status --brain-root "${BRAIN_ROOT}"
mem ask "query" --brain-root "${BRAIN_ROOT}" --mode keyword-only --top 5
mem inject --query "query" --brain-root "${BRAIN_ROOT}" --mode keyword-only
printf '%s\n' '<working note>' | mem scratch append --brain-root "${BRAIN_ROOT}" --stdin --source codex
mem snapshot rebuild --brain-root "${BRAIN_ROOT}"
mem procedure run deploy-staging --brain-root "${BRAIN_ROOT}" --result success --note "validated"
```

Provider-backed examples, run only after the consent checks above:

```sh
mem ask "query" --brain-root "${BRAIN_ROOT}"
mem ask "query" --brain-root "${BRAIN_ROOT}" --explain
mem ingest --brain-root "${BRAIN_ROOT}"
mem reindex --brain-root "${BRAIN_ROOT}"
mem promote-chat <event-id> --brain-root "${BRAIN_ROOT}"
```
