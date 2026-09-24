# Changelog

## Unreleased

- Keep explicit path and URL objects literal when an older ingest registered
  them as artifact entities; preserve known projects whose names have file
  suffixes, such as Next.js. An identical pending fact review prevents re-queuing
  only when object type and validity also match and the review payload is valid.
- Count every fact left out of a local summary (not only unsupported
  relations), space Han text from Latin values, and record approved summary
  drafts in the event ledger with refreshed backlinks.
- Skill guidance no longer implies plain `mem ask` is local: it follows the
  root's `retrieval.default_mode`, so local recall must pass
  `--mode keyword-only`.
- Reduce ingest review noise by retaining artifact/version objects as literal
  facts, resolving only factual endpoints, and separating fact/entity thresholds.
  Resolved human-approved pending facts no longer return for confidence review.
- Treat free-form status observations as multi-valued; keep explicit lifecycle
  states and other single-valued relationships conflict-checked.
- Rank local summaries by topic and importance, mark unsupported evidence, add
  a read-only summary preview, and supply accepted facts to provider drafts.
- Preview existing-root runtime ignore rules through reconcile and honor scoped
  automatic Git commits after applying a verified migration.
- Repair Codex integration without losing foreign MCP tables, custom options or
  proven skill-description edits. Use available local launchers and save verified
  integration backups before application.
- Block external DNS and UDP as well as TCP in tests and Python test children.
- Keep event collection read-only, preserve retryable ingest work, and avoid
  advancing the main event cursor during targeted replay.
- Stage and validate database rebuilds before publishing through SQLite's
  transactional backup API; preserve the original database on failure.
- Add approved conflict/low-confidence evidence to page timelines and sources,
  including pages not yet created, so local recall sees approved facts.
- Reject missing configured provider keys instead of inheriting another
  provider's credentials; preserve absolute import sources and validate hashes
  on resume, with cumulative job progress.
- Inject complete scratch bodies within budget, use the latest timeline entries,
  and honor snapshot exclusion and deduplication across retrieval paths.
- Move legacy Codex skills outside skill discovery roots, including migration of
  older disabled archives; run HTTP tools in a serialized worker off the SSE loop.
- Report current/stale/missing embedding chunks by content hash and label
  recorded costs as embedding-only.
- Fixed ingest robustness for non-material ledger events, near-valid signal
  extraction payloads, failed laundry quarantine, and noisy transient entity
  stub creation.
- Fixed review apply noise by ignoring undecided pending files before parsing
  their structured payloads.
- Added `--brain-root` to `mem lint` and `mem rebuild`, plus script-friendly
  `mem rebuild --backlinks --index` / `mem rebuild --all` derived index repair.
- Added `mem entity prune-stub` for safe cleanup of mistaken generated stub
  entity pages, and `mem review --quarantine-invalid --yes` for moving corrupt
  undecided review files out of the pending queue.
- Keep deferred review items pending and clear the decision checkbox without
  applying fact/page mutations.
- Added `mem-mcp-http` HTTP/SSE transport for opt-in remote MCP access.
- Added optional shared-token authentication for HTTP/SSE requests.
- Added a remote tool whitelist model for HTTP/SSE exposure, with high-risk
  review apply tools kept local-only and procedure creation/promotion opt-in.
- Added `docs/multi-device.md` with multi-device setup guidance, Tailscale
  topology, client configuration, authentication notes, and troubleshooting.
- Noted that stdio mode and existing MCP client configurations are unchanged.

## 0.2.0 — Phase 2

- Added OpenAI-compatible embedding configuration and client support.
- Added sqlite-vec backed `embeddings`, `embedding_index`, `import_jobs`, `import_files`, and `stats` schema.
- Added `mem reindex` with incremental content-hash based embedding updates.
- Changed `mem ask` default retrieval to hybrid vector + keyword + SQL matching with RRF fusion.
- Added structured SQL direct retrieval for supported fact-style questions.
- Added Markdown/Text bulk import into laundry with resumable jobs and cost estimates.
- Added PDF and JSONL extractors for bulk import.
- Added import progress, `--status`, `--list-jobs`, `--abort`, and `mem cost-estimate`.
- Added Phase 2 `mem status` telemetry and ingest auto-reindex with `--no-auto-reindex`.
