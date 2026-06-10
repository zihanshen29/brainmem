# BrainMem Agent Operating Notes

This repository contains the BrainMem codebase. Public repository instructions
must stay portable and must not contain a contributor's private filesystem
paths.

The user's BrainMem data root is referenced as `${BRAIN_ROOT}`. Resolve it in
this order:

1. An explicit `--brain-root` flag or MCP `brain_root` argument.
2. The `BRAIN_ROOT` environment variable.
3. A local user config such as `~/.config/brainmem/config.toml` with
   `data_root`.
4. Default: `~/brain`.

For HTTP/SSE MCP remote mode, the server fixes `${BRAIN_ROOT}` at startup.
Remote clients must not pass or override `brain_root`; the server-side value is
authoritative. This remote-mode rule does not change stdio behavior.

Use `AGENTS.local.md` for machine-specific notes such as Windows paths,
private data roots, or local wrapper locations. `AGENTS.local.md` is ignored by
Git and must not be committed.

## Contribution Rules

- Do not edit `brain.db` or `events.jsonl` manually.
- Do not rewrite or delete user memory files except through supported BrainMem
  commands and only when the user has asked for that operation.
- Keep docs consistent with BrainMem privacy, review, and consent rules.
- Do not commit API keys, local paths, user data roots, private usernames, or
  machine-specific wrapper paths.
- Prefer portable examples using `mem` and `${BRAIN_ROOT}`.

## Brain Root Usage

BrainMem must work from any project directory. When operating on memory data,
pass the root explicitly:

```sh
mem ask "query" --brain-root "${BRAIN_ROOT}" --mode keyword-only --top 5
```

If `mem` is not on PATH, the user should configure a local wrapper outside the
repository or document it in `AGENTS.local.md`.

## Decision Framework For Agents

Use this sequence when a user task may involve long-term memory.

### Step 1 - Task Type

- Recall/search/summarize saved context: use local recall first.
- Feed retrieved memory into another LLM prompt: use `mem inject`.
- Save a raw note or user-stated durable fact: use `mem capture`.
- Track current session progress without committing wiki truth: use
  `mem scratch append`.
- Consolidate current session state before injection: use
  `mem snapshot rebuild`, then `mem inject`.
- Manage reusable procedures/runbooks: use `mem procedure new/run/promote`.
- Ordinary code navigation or facts already in the workspace: do not query
  BrainMem.

### Step 2 - Context Relevance

Before answering, ask whether the user's personal/project history may contain
the answer. Query BrainMem for user projects, preferences, prior decisions,
people, teams, customers, long-running tasks, or remembered procedures. Do not
query for generic API syntax or code facts that should come from the current
repository.

`mem ask` and `mem inject` also surface `scratch/working.md` entries and
`scratch/SNAPSHOT.md` content at low weight, so cross-session working state can
be found through local recall before it becomes durable wiki truth.

### Step 3 - Information Capture

Capture only durable information the user asks to save or clearly intends to
persist. Agent-created notes must include source context:

```sh
printf '%s\n' \
  'source_agent: codex' \
  'source_context: <short task or conversation summary>' \
  '' \
  '<memory text>' |
  mem capture --brain-root "${BRAIN_ROOT}" --stdin
```

Use scratch instead of capture for temporary session-progress notes:

```sh
printf '%s\n' '<working context>' |
  mem scratch append --brain-root "${BRAIN_ROOT}" --stdin --source codex
```

### Step 4 - Entity Disambiguation

Before answering questions involving names of people, projects, teams,
customers, deployments, or short ambiguous labels:

1. If the name is short or ambiguous, run local recall with a wider top count.
2. If multiple plausible matches appear, ask the user which one they mean.
3. If one clear match appears, proceed using that entity's compiled truth and
   sources.

Example:

```sh
mem ask "Alex" --brain-root "${BRAIN_ROOT}" --mode keyword-only --top 10
```

### Step 5 - High-Risk Lookback

Before destructive or high-risk work such as deletion, migrations, broad
refactors, credential changes, deploys, or data movement, search for relevant
prior decisions, failed attempts, and procedure capsules:

```sh
mem ask "deploy rollback migration failure" --brain-root "${BRAIN_ROOT}" --mode keyword-only --top 10
mem ask "deploy" --brain-root "${BRAIN_ROOT}" --mode keyword-only --type procedure --top 10
```

If a previous failure or runbook is found, mention the risk and reuse the
procedure rather than guessing.

## Privacy Boundary

Local-only commands do not call an external model or embedding provider:

- `mem status --brain-root "${BRAIN_ROOT}"`
- `mem ask "query" --brain-root "${BRAIN_ROOT}" --mode keyword-only`
- `mem inject --brain-root "${BRAIN_ROOT}" --mode keyword-only`
- `mem scratch append --brain-root "${BRAIN_ROOT}" ...`
- `mem snapshot rebuild --brain-root "${BRAIN_ROOT}"`
- `mem procedure new/run/promote --brain-root "${BRAIN_ROOT}"`
- `mem cost-estimate ...`
- deterministic rebuild/lint commands such as
  `mem lint --brain-root "${BRAIN_ROOT}" --all` and
  `mem rebuild --brain-root "${BRAIN_ROOT}" --backlinks --index`

Provider-backed commands require explicit user permission before use on
sensitive or user-provided content:

- default hybrid `mem ask "query"` because the query is embedded
- semantic `mem ask`
- `mem ask "query" --explain`
- `mem ingest`
- `mem reindex`
- `mem promote-chat`
- review apply flows that rewrite compiled truth

Before provider-backed commands, hydrate API keys from the user's environment.
Never print, store, or commit API keys.

HTTP/SSE MCP transport changes only how a client reaches BrainMem. It does not
change the provider consent boundary above. BrainMem does not provide built-in
TLS for HTTP/SSE; use an outer private network, tunnel, or reverse proxy for
transport encryption and access control.

## Review Queue Rule

The review queue is human-controlled. Agents may list, inspect, or summarize
pending review items when asked. Agents must not approve, reject, select a
decision, or apply review items unless the user explicitly instructs that exact
action.

Remote MCP deployments must not expose high-risk review apply tools. Procedure
creation and promotion tools, such as `procedure_new` and `procedure_promote`,
are opt-in for remote mode and should be enabled only when the user's operating
policy explicitly allows them.

## CLI And MCP Tool Correspondence

Every MCP tool or skill wrapper for BrainMem should map cleanly to a supported
CLI operation and preserve the same privacy and consent boundary.

| Agent intent | CLI operation | MCP/tool behavior |
| --- | --- | --- |
| Check health | `mem status` | Safe local status check. |
| Local recall | `mem ask --mode keyword-only` | Default recall path when provider consent is absent. |
| Hybrid recall | `mem ask` | Provider-backed; call only after explicit permission where needed. |
| Injection context | `mem inject` | Local by default; token-bounded prompt context, includes snapshot by default. |
| Capture note | `mem capture --stdin` | Writes raw note to laundry; include source context. |
| Working buffer | `mem scratch append` | Local session-progress note, not compiled wiki truth. |
| Distill current state | `mem snapshot rebuild` | Local deterministic snapshot from scratch. |
| Procedure capsule | `mem procedure new/run/promote` | Manual procedure SOP with maturity state. |
| Recall procedures | `mem ask --type procedure --mode keyword-only` | Find reusable runbooks before high-risk work. |
| Review procedure candidates | `mem review --brain-root "${BRAIN_ROOT}" --kind procedure_candidate` | After ingest, reusable workflow candidates need explicit user approval before becoming procedure pages. |
| Estimate import | `mem cost-estimate` | Local cost planning before ingest/import. |
| Ingest laundry | `mem ingest` | Provider-backed; requires explicit permission. |
| Build embeddings | `mem reindex` | Provider-backed; requires explicit permission. |
| Promote chat | `mem promote-chat` | Provider-backed and writes memory; requires explicit permission. |
| Review queue | `mem review` | Inspect only unless the user explicitly asks to approve/reject/apply. |
| Repair indexes | `mem lint`, `mem rebuild` | Local deterministic maintenance unless a future option calls a provider. |

MCP docstrings and skill instructions must emphasize when to call the tool, not
only what it does. They should state whether the tool is local-only,
provider-backed, or writes durable memory.
