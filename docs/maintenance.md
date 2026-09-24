# Safe maintenance and privacy

`brain.db` is primary data. Accepted facts, review history, cursors and import
state cannot be reconstructed from the current event ledger. Keep it in a
verified complete backup alongside Markdown and raw sources.

## Backups and reconciliation

Use paths outside the data root for backups and dry-run output:

```sh
mem backup /backups/brain-before.zip --brain-root "${BRAIN_ROOT}"
mem restore /backups/brain-before.zip --verify
mem restore /backups/brain-before.zip --destination /new/restore-test
mem reconcile --brain-root "${BRAIN_ROOT}" --output /backups/reconcile.json
```

Backups include ignored files, raw sources and Git history. SQLite is snapshotted
consistently; transient SHM, locks and unfinished transaction journals are not
restored. Each archive has a SHA-256 manifest, database integrity/foreign-key
checks and ledger validation. Restore refuses an existing destination. Archives
are local and unencrypted: choose an encrypted off-device destination separately.
Store the returned archive hash separately if tamper detection is required.

After explicit user approval, apply the exact reviewed plan with a fresh backup:

```sh
mem reconcile --brain-root "${BRAIN_ROOT}" --apply \
  --plan /backups/reconcile.json --backup /backups/brain-before.zip
```

Application refuses changed inputs, stale backups and ambiguous page/alias
identities. It registers non-procedure pages, repairs authoritative page fields,
adds missing aliases and repairs uniquely located archived source references.
It does not delete entities, normalize historical predicates, merge semantic
lookalikes, clean archives, approve reviews or rewrite existing summaries.
Those proposals are listed separately for human decisions.

The plan also previews missing `/.brainmem/` and `/scratch/` ignore rules for
older roots, preserving existing custom rules. It never untracks or deletes
files. With `git.auto_commit = true`, apply commits only its changed paths and
honors `git.track_database`. Unrelated staged work is rejected before mutation.

### Artifact entities and project fragments

Older ingests registered some paths and file names as entities. Turn them into
literal fact values with the same reviewed-plan pattern:

```sh
mem entity literalize e-docu-sample-dir report-md --predicate 523=code_path \
  --brain-root "${BRAIN_ROOT}" --output /backups/literalize.json
mem entity literalize --apply --plan /backups/literalize.json \
  --backup /backups/brain-before.zip --brain-root "${BRAIN_ROOT}"
```

Referencing facts keep their evidence and receive the entity title as a literal
value; `--predicate` corrects a mislabeled relation in the same step. The
registry row, aliases, backlinks and vectors of each entity are removed and the
change is recorded in the ledger. Ids that are already gone are listed as
`absent`, so re-planning after apply shows no work.

Experiment stages, file names and similar labels sometimes carry facts of their
own. `--fold-into PROJECT` moves those facts to the project and keeps the label
in the relation (`d15 verdict: red`), moves a generated stub page's timeline and
sources into the project page and removes the stub. Pages with their own text
are refused; merge them instead. `--dangling` also turns references to entities
that no longer exist into literal values.

`mem entity merge` also accepts project and concept pages, so an extracted
sub-stage can be folded into its real project. A placeholder summary is never
appended to the kept page, the loser's vectors are deleted, the merge is
recorded in the ledger, and a machine-owned summary follows the moved facts.

`rebuild --db` now starts from a healthy database snapshot and preserves primary
records. A missing or corrupt database requires restoration. `lint --all` is
read-only and reports registry/source drift as well as contradictions; it no
longer creates review noise or Git commits.

## Summary ownership and review

Accepted entity-specific facts produce a short local evidence summary for new
or untouched generated pages. `summary_hash` records the generated text.
Any edited summary, legacy non-placeholder summary, or `curated: true` page is
protected from automatic replacement. Tier approval changes importance only.

Local summaries consider all active facts, grouping direction/decisions ahead
of progress, implementation, validation and version details. Each group is
bounded so operational noise cannot displace the direction. Unknown relation
types are retained in the database; incomplete drafts say so, and a draft with
no supported relations is explicitly a placeholder. `output_language` selects
the labels; literal evidence and names are preserved rather than translated.

```sh
mem summarize project-slug --brain-root "${BRAIN_ROOT}" --dry-run
mem summarize project-slug --brain-root "${BRAIN_ROOT}" --apply
mem summarize project-slug --brain-root "${BRAIN_ROOT}"
# Only with permission to send allowed evidence to the configured model:
mem summarize project-slug --brain-root "${BRAIN_ROOT}" --provider
```

The first command previews locally without writing a page or review. It cannot
be combined with `--provider`. `--apply` writes the local summary at once when
the page is a stub or still holds unedited generated text; the text stays
machine-owned, so later accepted facts keep refreshing it. Edited, approved and
curated summaries are refused. The remaining commands create a `summary_refresh`
review with a diff; provider drafts include accepted facts as well as timeline
evidence and obey the privacy of their sources. Approval verifies
the entire original page hash and refuses stale drafts. `rebuild --pages SLUG
--force` is a compatibility shortcut for a local summary draft. Approval,
rejection and application of reviews still need explicit user instruction.
`defer` leaves the file pending, records the deferral and clears its checkbox.
Rejected tier proposals require meaningful mention growth before reappearing.
Approving a resolved pending fact bypasses its confidence gate, retaining its
recorded confidence. A newly detected single-valued conflict still needs a
separate decision; an already stored fact does not get reviewed again.

## Content-level provider boundaries

Mark a note header or page frontmatter with `privacy: local-only`. Capture and
MCP wrappers preserve nested labels; a restrictive label cannot be relaxed by
another wrapper. Optional directory rules use root-relative glob patterns:

```toml
[privacy]
default = "provider-allowed"
local_only_paths = ["raw/private/**", "laundry/private/**", "pages/private/**"]

[ingest]
confidence_auto_accept = 0.80
confidence_auto_reject = 0.50
entity_confidence_auto_accept = 0.85
chunk_max_chars = 4000
output_language = "source"

[llm]
max_output_tokens = 4096
```

Fact acceptance and new-entity creation have separate thresholds. Existing
explicit thresholds are preserved; review a configuration change before lowering
them. Unused extracted mentions do not create entities or reviews. Concrete
paths, files and versioned descriptions used as objects remain literal evidence
unless an existing identity matches. Ambiguous durable identities still need
review. Free-form `status` observations may coexist; use `lifecycle_state` for
mutually exclusive states. Known single-valued predicates keep conflict checks.

`default = "local-only"` disables provider use for the entire root. Local-only
notes stay pending; they can still be searched locally. Page eligibility also
checks its local/event sources. Ingest, reindex, explanation and summary/chat
generation enforce these checks. Capture refuses likely credentials. Detection
is a safeguard, not a guarantee that all sensitive text can be recognized.

CLI and MCP default to keyword-only recall. An explicit existing retrieval
configuration is preserved. `--mode hybrid`, `--mode semantic`, `--explain`,
ingest, reindex and provider summary/chat generation may send permitted content
externally. Permission labels do not replace user consent. Local retrieval and
MCP responses can contain local-only text; an agent receiving it must not send
it to another external service without permission.

## Concurrent processes and interrupted writes

All local clients resolve the same root: explicit argument, `BRAIN_ROOT`, a
current directory containing both `config.toml` and `brain.db`, user config
`~/.config/brainmem/config.toml`, then `~/brain`. HTTP MCP still fixes the root
server-side.

The default lock directory is `.brainmem-locks` beside the canonical root,
independent of Windows identity and TEMP. It inherits parent permissions. If
`BRAINMEM_LOCK_DIR` is set, every client must use the same shared directory.
Restart CLI wrappers/MCP/Claude Code sessions after upgrading so all processes
load the same lock protocol. Read operations create no SQLite runtime files in
the data root; a live WAL is read from a private temporary snapshot.

Ingest and reindex serialize their own slow workflows but release the root lock
during provider calls. Application rechecks source/configuration hashes and
privacy. Completed note extractions are cached locally to survive later batch
failures. Truncated outputs split into smaller inputs; identical truncated
requests are not retried. A note's SQLite changes, pages, reviews, ledger and
archive move have a recoverable transaction journal and commit marker.

After a killed ingest/reconcile operation, readers fail explicitly until a
writer or `mem recover --brain-root "${BRAIN_ROOT}"` recovers the journal.
Recovery preserves unrelated files and refuses to overwrite intervening manual
edits. Other commands still use root locks but do not all have cross-file crash
recovery. Locks cover cooperating processes on one machine; direct editors,
old clients, network filesystems and power/storage failures require backups.

Git auto-commits omit `brain.db` by default (`[git] track_database = false`) and
refuse unrelated staged files. Existing tracked database history is retained;
untracking it or rewriting history is a separate user decision. Git snapshots
do not replace full database/raw-source backups.

## Offline verification

Run `pytest -p no:cacheprovider`, `ruff check src tests`, and `mypy src`.
Tests use disposable roots, isolated lock directories, mocked providers and a
socket/DNS/UDP guard that also reaches Python subprocesses. Loopback is reserved for
local MCP tests and asyncio IPC; no live provider requests are permitted.
