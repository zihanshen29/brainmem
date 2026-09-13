# MCP concurrency implementation plan

Status: design refined by independent review; implementation in progress.

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
   query pool instead. Only query workers open read-only graphs; the daemon retains index
   writing and one watcher per project. A bounded SDK-based stdio router connects explicit
   project requests to their per-project daemons, since a workspace with multiple indexed
   children otherwise falls back to direct mode. Preserve all ten original tools, schemas
   and lifecycle; never initialize indexes or arbitrarily choose an ambiguous project.
5. **Activation:** retain rollback evidence for the original executable/configuration.
   Activate only verified code and configuration, then validate new MCP processes against
   the actual paths. Existing client processes may require a fresh session to load updated
   code; distinguish staged, configured and running versions in the handoff.

## Acceptance evidence

- [ ] Reproduce serial execution using a disposable slow/fast request check.
- [ ] BrainMem stdio and HTTP keep transport responsive; independent reads overlap.
- [ ] Bounded queue rejects excess work; timeout/cancellation cannot oversubscribe workers.
- [ ] HTTP authentication, tool allowlists, hidden root and sanitized errors still pass.
- [ ] Read connections reject writes while keyword and local vector retrieval remain valid.
- [ ] Independent processes allow overlapping readers and exclude conflicting mutations;
      nested calls do not deadlock and exceptions release locks.
- [ ] Provider configuration remains isolated between simultaneous roots.
- [ ] SQLite sidecars are never manually removed while another connection may use them.
- [ ] Real MCP probes cover 1, 4 and 8 concurrent calls and slow/fast mixed requests.
- [ ] CodeGraph base artifact/backport integrity, all original tools, one daemon, worker pool and watched
      index updates are verified on disposable fixtures.
- [ ] Workspace/project routing and active configuration point to the verified executables.
- [ ] Relevant regression checks, scoped diff review and completion audit are recorded.

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

This document is the single ongoing plan for this change. External installation artifacts,
configuration backups and benchmark reports remain outside the source repository.

## Operational boundaries

- Each BrainMem MCP process admits at most 16 calls, executes at most four, and waits
  at most ten seconds for an execution slot. Root-lock acquisition has a separate ten
  second budget. HTTP overload uses `error.code = busy`; stdio reports a tool error.
- A cancelled running Python function retains its slot and root lock until it exits.
  Orderly service shutdown drains admitted work. A forced process kill does not provide
  cross-file transaction recovery.
- All cooperating processes on one machine must use the same lock directory (default:
  the user's temporary directory under `brainmem-locks`; override: `BRAINMEM_LOCK_DIR`).
  These locks do not coordinate another machine or programs editing files directly.
- Root locks cover complete operations. Long imports, provider calls inside mutation
  pipelines, or review editor sessions can make queries return busy. This preserves
  consistency between Markdown, events, Git and SQLite; shortening these write windows
  requires a separate transaction design.
- Read operations preserve canonical data. SQLite may create or update its own WAL/SHM
  runtime files; these are not manually removed and are not immutable database snapshots.
- Existing MCP sessions retain already-imported code. Fresh sessions load the configured
  implementation. An incompatible live CodeGraph daemon is reported explicitly instead
  of being killed or replaced by a second watcher.
