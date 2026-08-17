# OpenAI Codex integration

BrainMem ships a conservative user-scoped Codex integration. It combines:

- the canonical `brain-memory` skill;
- a managed proactive-recall block in the global `AGENTS.md`;
- a deterministic `UserPromptSubmit` hook;
- a local stdio MCP registration limited to read-only-first tools; and
- `install`, `status`, `doctor`, and `uninstall` lifecycle commands.

The installer is a dry run unless `--yes` is passed:

```sh
mem codex install --brain-root "${BRAIN_ROOT}"
mem codex install --brain-root "${BRAIN_ROOT}" --yes
mem codex doctor --brain-root "${BRAIN_ROOT}"
```

After installation or a hook update, open `/hooks` in Codex and review/trust the
exact command. Codex hashes non-managed hooks and skips new or changed hooks
until the user trusts them.

## Managed artifacts

| Artifact | Installation behavior |
| --- | --- |
| `~/.agents/skills/brain-memory/SKILL.md` | Copied from `skills/brainmem/SKILL.md`. A different, unowned file is not replaced unless `--replace-skill` is explicitly supplied. |
| `~/.codex/AGENTS.md` | Adds or replaces only an HTML-comment-delimited BrainMem policy block. Other user instructions are preserved. |
| `~/.codex/hooks.json` | Adds one matcher-free `UserPromptSubmit` group. Other JSON keys, events, groups, and handlers are preserved semantically. |
| `~/.codex/config.toml` | Adds or replaces only a comment-delimited `[mcp_servers.brainmem]` block. An existing unmarked server with that name is treated as a conflict, not overwritten. |
| `~/.codex/brainmem/integration.json` | Records hashes and the owned hook handler for drift detection and safe removal. It contains no prompts, memory bodies, or credentials. |

Codex uses `~/.agents/skills` as the canonical user skill location. If
`mem codex doctor` reports another `brain-memory` skill under
`~/.codex/skills`, review and move or remove the legacy copy; same-name skills
are not merged.

To resolve that duplicate without deleting it, request the explicit reversible
archive during installation:

```sh
mem codex install --brain-root "${BRAIN_ROOT}" --replace-skill \
  --archive-legacy-skill --yes
```

This atomically moves the complete legacy directory to
`~/.codex/skills/brain-memory.disabled-by-brainmem`. The manifest records the
move, and `mem codex uninstall --yes` restores it when the original path is
still free. Existing archive destinations are never overwritten.

The MCP block enables only:

- `brain_status`
- `brain_ask`
- `brain_inject`
- `brain_procedure_list`

The managed policy still requires keyword-only retrieval by default and clear
permission before provider-backed modes. The installer does not override a
user setting that disables hooks.

## Hook privacy and behavior

The hook reads the official `UserPromptSubmit` JSON payload from stdin. It
uses a deterministic gate for prior-context language, preferences, named
projects or people, and high-risk lookbacks. It then builds an allowlisted,
deduplicated query of at most eight terms and runs only:

```text
ask(minimal_query, mode="keyword-only", top=3)
```

Because existing BrainMem pages do not yet have a mandatory
visibility/sensitivity field, the hook never writes retrieved titles, slugs,
timelines, or page bodies to stdout. On a hit it emits only a bounded
`hookSpecificOutput.additionalContext` directive telling Codex to perform the
normal `brain-memory` recall workflow. The directive caps any later
`mem inject` call, disables snapshot injection, and forbids automatic capture,
ingest, rewrite, provider-backed explanation, or review application.

Prompts that look like credential assignments or tokens are skipped. Oversize
prompts, invalid JSON, missing roots, retrieval errors, and empty results are
also skipped silently. The implementation does not log prompts or queries.

The generated configuration has no `matcher`, includes `commandWindows`, uses
a five-second timeout, and limits additional context. To inspect a portable
template without writing configuration:

```sh
mem codex hook-template --brain-root "${BRAIN_ROOT}"
```

For fail-closed Windows shell safety, generated hook arguments reject double
quotes, `%`, `!`, NUL, and newlines. Choose a brain-root and executable path
without those characters; this restriction also applies when generating the
Windows override from another operating system.

A static portable template that resolves `BRAIN_ROOT` from the hook process
environment is available at
[`codex-hooks.template.json`](codex-hooks.template.json).

## Status, drift, and removal

`status` is read-only. `doctor` prints the same report and exits nonzero when
the skill, policy, hook, MCP block, commands, or brain root are missing or have
drifted, when hooks are disabled, or when a duplicate skill is present:

```sh
mem codex status --brain-root "${BRAIN_ROOT}" --json
mem codex doctor --brain-root "${BRAIN_ROOT}"
```

Removal is also a dry run by default. It deletes the skill only when its hash
still matches the integration manifest and removes only owned handlers or
marked blocks:

```sh
mem codex uninstall
mem codex uninstall --yes
```

Locally edited or unowned skill files are left in place and reported.
