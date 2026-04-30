$ErrorActionPreference = "Stop"

if (-not $env:DEEPSEEK_API_KEY) {
    throw "DEEPSEEK_API_KEY must be set for ingest/explain-capable LLM workflows."
}
if (-not $env:OPENAI_API_KEY) {
    throw "OPENAI_API_KEY must be set for embedding reindex."
}

$root = Join-Path $env:TEMP ("brain-smoke-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
Write-Host "Smoke test root: $root"

mem init --root $root

"Today I read a Transformer paper by Vaswani. The main contribution was self-attention." |
    mem capture --brain-root $root --stdin
mem ingest --brain-root $root --no-auto-reindex

mem reindex --brain-root $root

$result = mem ask --brain-root $root "Who wrote the Transformer paper?"
if ($result -notmatch "Vaswani") {
    throw "ASK FAILED: result missing expected author. Output: $result"
}

$importDir = Join-Path $env:TEMP ("brain-import-fixture-" + (Get-Date -Format "yyyyMMdd-HHmmss"))
New-Item -ItemType Directory -Path $importDir -Force | Out-Null
"# Note 1`nThis is the first imported note." | Out-File (Join-Path $importDir "note1.md") -Encoding utf8
"# Note 2`nThis is the second imported note." | Out-File (Join-Path $importDir "note2.md") -Encoding utf8

mem cost-estimate $importDir --kind md
mem import --brain-root $root $importDir --kind md --yes

$laundry = Get-ChildItem (Join-Path $root "laundry") -Recurse -Filter "*.md" |
    Where-Object { $_.FullName -notlike "*processed*" }
if ($laundry.Count -lt 2) {
    throw "IMPORT FAILED: expected at least 2 laundry items"
}

mem status --brain-root $root
$status = mem status --brain-root $root --json | ConvertFrom-Json
if ($null -eq $status.embedding_coverage) {
    throw "STATUS missing embedding_coverage"
}
if ($null -eq $status.total_cost_usd) {
    throw "STATUS missing total_cost_usd"
}

Write-Host "All Phase 2 smoke checks passed."
