# MCP concurrency implementation plan

Status: completed on Windows; configured launches and fresh MCP processes verified.

## Objective

Make routine BrainMem and CodeGraph queries responsive under concurrent MCP clients,
without trading away repository consistency, privacy, request isolation, or index freshness.
Preserve existing local changes and user data. No provider-backed memory operations are
needed for implementation or acceptance.

## Design and scope

1. **BrainMem dispatch:** use the same bounded worker dispatcher for stdio and HTTP.
   Synchronous operations must not execute on the transport event loop. Default to four
   executing calls, bounded pending work and bounded admission wait. Report overload
   explicitly. Cancellation must not release execution capacity or repository locks while
   a synchronous operation is still running. Log queue and execution duration without
   query text, credentials, or memory contents.
2. **Read isolation:** each query opens its own truly read-only SQLite connection in its
   worker, with vector-extension support preserved. Retain keyword fallback when the
   database or optional vector support is unavailable. Never change journal mode while
   opening a reader. Preserve HTTP fixed-root signatures and error sanitization.
3. **Repository consistency:** coordinate shared readers and exclusive mutation pipelines
   by canonical brain root across CLI and MCP processes. Cover Markdown, ledger, Git and
   SQLite mutations, including nested pipeline calls. Avoid process-global provider
   configuration changes. Let SQLite own WAL/SHM lifecycle; do not unlink live sidecars.
4. **CodeGraph:** official v1.6.0 was verified and staged, but release inspection found that
   all pool-capable 1.x releases remove the existing context/trace APIs. Preserve the
   installed 0.9.7 API/index format in an isolated bundle and backport the pinned upstream
   query pool instead. Bound the daemon-wide queue to 64 outstanding jobs, remove expired
   queued work, and retain occupied workers until their actual result. Only query workers open read-only graphs; the daemon retains index
   writing and one watcher per project. A bounded SDK-based stdio router connects explicit
   project requests to their per-project daemons, since a workspace with multiple indexed
   children otherwise falls back to direct mode. Preserve all ten original tools, schemas
   and lifecycle; never initialize indexes or arbitrarily choose an ambiguous project.
   On Windows, claim the native named pipe before index initialization so simultaneous
   stale-PID recovery cannot start duplicate writers. Initial reads share the reconcile
   gate, and cold workers queue queries instead of running them on the transport thread.
5. **Activation:** retain rollback evidence for the original executable/configuration.
   Activate only verified code and configuration, then validate new MCP processes against
   the actual paths. Existing client processes may require a fresh session to load updated
   code; distinguish staged, configured and running versions in the handoff.

## Acceptance evidence

- [x] Reproduce serial execution using a disposable slow/fast request check.
- [x] BrainMem stdio and HTTP keep transport responsive; independent reads overlap.
- [x] Bounded queue rejects excess work; timeout/cancellation cannot oversubscribe workers.
- [x] HTTP authentication, tool allowlists, hidden root and sanitized errors still pass.
- [x] Read connections reject writes while keyword and local vector retrieval remain valid.
- [x] Independent processes allow overlapping readers and exclude conflicting mutations;
      nested calls do not deadlock and exceptions release locks.
- [x] Provider configuration remains isolated between simultaneous roots.
- [x] SQLite sidecars are never manually removed while another connection may use them.
- [x] Real MCP probes cover 1, 4 and 8 concurrent calls and slow/fast mixed requests.
- [x] CodeGraph base artifact/backport integrity, all original tools, one daemon, worker pool and watched
      index updates are verified on disposable fixtures.
- [x] Workspace/project routing and active configuration point to the verified executables.
- [x] Relevant regression checks, scoped diff review and completion audit are recorded.

## Initial evidence and implementation notes

- BrainMem stdio directly registered synchronous functions; installed FastMCP executed
  them inline. Three status dispatches ran sequentially. HTTP used a single worker token.
- `ask` preferred the writable connection helper, including a journal-mode pragma.
- Writers span SQLite and files; an MCP-only semaphore cannot coordinate CLI writers.
- Existing mutation finalizers manually removed SQLite WAL/SHM files; these require review
  before enabling concurrent readers.
- Installed CodeGraph 0.9.7 has shared-daemon support but no read query pool. Official
  v1.6.0 includes a worker query pool and improved workspace-root discovery.
- Native v1.6.0 disposable probes passed 1/4/8 concurrent clients, but upgrade was rejected
  for this task because context/trace were removed. Backport acceptance must be rerun;
  the native upgrade's passing probes do not establish completion of the final design.
- A cold Windows stdio probe reproduced native NumPy initialization waiting behind an
  already-blocked stdin reader. Both server builders now initialize the optional SQLite
  extension before starting transport I/O; missing optional support retains its fallback.
- BrainMem regression batches passed (143 main checks and 195 targeted coordination and
  pipeline checks, with overlap). The final task-only checkpoint `c4f84c9` was independently
  exported and passed 92 checks, including all 20 new dispatch/startup regressions.
- Real BrainMem stdio and HTTP 1/4/8-call probes observed 1/4/4 executing readers. With a
  controlled 300 ms delay inside real status reads, transport ping remained 1.9–3.7 ms and
  real keyword recall finished in 10–12 ms before the slow read. These are disposable
  fixture results, not a production throughput guarantee.
- The final Windows CodeGraph compatibility bundle passed eight focused backport checks
  and eleven router checks. Eight simultaneous native clients with a stale PID produced
  one daemon, one watcher and four active query workers. Read-only writes were rejected;
  all ten original tool definitions and the nine query handlers retained their results.
- Literal JSON-RPC router acceptance covered independent clients, two projects, context
  and trace, relative paths, session isolation, error preservation, idle eviction, and
  survival after another client closed. The 1/4/8-call fixture batches took 34/45/81 ms.
  Watched updates were required to return the actual new symbol location, not merely echo
  its name in an empty search result. All test clients and daemons exited normally.
- Active-config validation started fresh processes from the saved launch configuration.
  BrainMem served four concurrent status requests on the actual data root; CodeGraph
  returned the new fixture symbol through its configured router. Tool counts remained
  12 and 10, and the configured BrainMem enabled-tool restriction remained unchanged.

This document is the single ongoing plan for this change. External installation artifacts,
configuration backups and benchmark reports remain outside the source repository.

## Operational boundaries

- Each BrainMem MCP process admits at most 16 calls, executes at most four, and waits
  at most ten seconds for an execution slot. Root-lock acquisition has a separate ten
  second budget. HTTP overload uses `error.code = busy`; stdio reports a tool error.
- A cancelled running Python function retains its slot and root lock until it exits.
  Orderly service shutdown drains admitted work. Ingest/reconcile now have a durable
  cross-file recovery journal; other mutation flows still require backup-based recovery
  after a forced kill.
- All cooperating processes on one machine must use the same lock directory (default:
  `.brainmem-locks` beside the canonical data root; override: `BRAINMEM_LOCK_DIR`).
  Restart all existing clients after upgrading; old processes keep the old protocol.
  These locks do not coordinate another machine or programs editing files directly.
- Ingest and reindex stage provider work outside the root lock, then revalidate
  inputs/privacy and apply under a short writer lock. Separate operation locks serialize
  competing ingesters/reindexers. Other long mutation workflows can still return busy.
- Read operations do not create or update source WAL/SHM files. A nonempty live WAL
  is copied with the database to a private read snapshot under the shared lock;
  immutable reads are used only without a nonempty WAL. Uncoordinated edits require
  retry instead of automatic overwrite.
- Existing MCP sessions retain already-imported code. Fresh sessions load the configured
  implementation. An incompatible live CodeGraph daemon is reported explicitly instead
  of being killed or replaced by a second watcher.
- The CodeGraph bundle is a local Windows compatibility build, not an official release.
  Its stale-startup fix is Windows-specific. Global CLI updates do not update this pinned
  MCP bundle; a future replacement must repeat API, worker, routing and watcher checks.

## Local activation and rollback evidence

The Codex configuration now launches both servers through the existing BrainMem Python
environment. BrainMem uses `-m brain.mcp.server`; CodeGraph uses the staged
`codegraph_router.py`, whose default is `compat/codegraph-win32-x64` version
`0.9.7+concurrency-backport`. The original CodeGraph installation was not changed.
Only the two launch commands/argument lists changed; other configuration, credentials,
tool restrictions and authentication settings were preserved.

The backup is `config.toml.before-mcp-concurrency-20260914` beside the active Codex config.
If rollback is needed, restore only these two server launch entries from that backup
(BrainMem originally used `mem-mcp.exe`, CodeGraph `codegraph serve --mcp`) and reconnect
clients. This restores launch configuration only: the BrainMem editable source still
contains its repair. Do not blindly reset that repository or overwrite later config
changes; the task-only source patch and original working diff are retained separately.

External evidence is under the local `_tools` directories:

- `brainmem-concurrency-20260914`: original working diff, reviewed task-only patch,
  isolated checkpoint export, real transport probe results, configuration activation
  script and `active-config-validation.json`.
- `codegraph-concurrency-20260914`: pinned upstream distribution/checksums, copied original
  compatibility bundle, `backport.patch`, SHA256 manifests, native/router regression
  harnesses, JSON reports and `BACKPORT-VALIDATION.md`.

BrainMem's source checkpoint is `c4f84c9`. Prior unrelated changes remain uncommitted;
the existing untracked runtime-regression test was adjusted to expect concurrent reads.
No memory content was captured, no provider-backed request was needed, no existing
CodeGraph user index was migrated, and no user MCP process was forcibly terminated.
