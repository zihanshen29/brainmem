# Changelog

## Unreleased

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
