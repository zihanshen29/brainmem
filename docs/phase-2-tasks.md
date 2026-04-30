# Phase 2 Build Tasks

本文件是给 Codex 的 **Phase 2 有序执行清单**。任务编号从 17 开始，接续 Phase 1 的 0–16。每个任务完成后跑对应测试通过，再进下一个。

## 总原则（继承 Phase 1）

- **先骨架，后血肉**。
- **每个任务一个 PR-sized 提交**。
- **不要跨任务**。
- **遇到 spec 不清楚的地方写到 `OPEN_QUESTIONS.md` 跳过继续**。
- **Phase 3 功能不要提前做**（procedural memory / rules / OCR / HTML 等）。
- **Spec 是 source of truth**。如果实现需要偏离 spec，先更新 spec 再写代码。Phase 2 这次特别强调，因为 Phase 1 出过 doc drift。

## Task 17 — Embedding Config & Client

**目标**：能调用 OpenAI 官方或 OpenAI-compatible embedding API，返回向量。

1. `src/brain/llm/embedding.py`：
   - `EmbeddingClient` Protocol（`embed`, `dimension`, `last_call_tokens`）
   - `OpenAICompatibleEmbeddingClient` 实现（用 openai SDK 的 `client.embeddings.create`，支持可配置 `base_url`）
   - 失败重试 1 次，再失败抛 `EmbeddingError`
   - batch 调用：单次最多 `batch_size` 条（按 config）
   - 用 `tiktoken` 在调用前估 token 数（用于 cost tracking）
2. `src/brain/config.py` 加 `[embedding]` 段的 Pydantic 模型 + 默认值
3. `src/brain/exceptions.py` 加 `EmbeddingError`

**测试** (`tests/unit/test_embedding.py`)：

- mock OpenAI SDK 返回固定向量，断言上层接口拿到的形状正确
- `base_url` 从 config 透传，保证国产/中转 OpenAI-compatible provider 可接入
- 失败重试逻辑（mock 第一次失败、第二次成功）
- batch 切分（给 250 条文本、batch_size=100，应该调 3 次 API）

## Task 18 — Embedding Store with sqlite-vec

**目标**：能在 brain.db 里存取 embedding。

1. `src/brain/db/migrations/0002_phase2.sql` —— 完整 DDL（见 `data-model.md` 5b）
2. `src/brain/db/migrations.py`：检测 `user_version` 自动升级（1 → 2）
3. `src/brain/db/connection.py`：加载 sqlite-vec 扩展（见 `data-model.md` 5c）
4. `src/brain/db/embeddings.py`：
   - `upsert_embedding(conn, chunk, content_hash, vector, model) -> int`
   - `delete_embedding(conn, rowid)`
   - `find_embeddings_for_page(conn, page_slug) -> list[EmbeddingRecord]`
   - `vector_search(conn, query_vector, top_k) -> list[RetrievalHit]`
5. `src/brain/db/stats.py` —— stats 表的 `get_stat`, `set_stat`, `increment_stat`
6. `src/brain/models/embedding.py` —— `EmbeddingChunk`, `EmbeddingRecord`, `RetrievalHit`, `FusedResult`

**测试** (`tests/unit/test_embeddings.py` + `test_embedding_migration.py`)：

- migration 0001 → 0002 升级后所有 Phase 1 表完好 + 新表存在
- upsert + lookup round-trip
- vector search 返回距离排序的结果
- dimension 不匹配时插入失败（vec 表的内置约束）
- delete 之后 vec 表和 embedding_index 都没了

## Task 19 — Page Indexer & Reindex

**目标**：`mem reindex` 能跑，能增量。

1. `src/brain/pipeline/chunking.py` —— `split_page_into_chunks(page, max_chars)` (见 `pipeline.md`)
2. `src/brain/pipeline/reindex.py`：
   - `reindex(brain_root, force, page_filter, dry_run, no_commit) -> ReindexReport`
   - 增量逻辑（content_hash 比对）
   - 删除 orphan chunk
   - 写 `reindexed` event 到 ledger
   - 更新 stats
3. `src/brain/cli/reindex.py` —— CLI 包装
4. 在 `cli/main.py` 注册

**测试** (`tests/integration/test_reindex.py`)：

- 第一次 reindex：所有 chunk 入库
- 第二次 reindex (没改动)：全部 unchanged，0 API 调用
- 改一个 page 的 timeline 加一行：增量只 embed 那一行
- 删一个 timeline entry：orphan 被清理
- `--force`：所有 chunk 重 embed
- `--pages <slug>`：只处理指定 page
- `--dry-run`：不写任何东西，但报告数字正确

**集成测试** (用 mock embedding)：

- 创建 3 个 page → reindex → DB 里有正确数量的 embedding
- reindex 后再 reindex → unchanged 计数 = 上次的 added + updated

## Task 20 — Hybrid Ask

**目标**：`mem ask` 默认走 hybrid 三路 + RRF。

1. `src/brain/pipeline/retrieval/__init__.py`
2. `src/brain/pipeline/retrieval/vector.py` —— `vector_search(conn, query, top)`
3. `src/brain/pipeline/retrieval/keyword.py` —— BM25 评分
   - 用 `rank-bm25` 库
   - 中文用 jieba 分词，英文用空格 split
4. `src/brain/pipeline/retrieval/sql_match.py` —— entity match + backlink
5. `src/brain/pipeline/retrieval/rrf.py` —— RRF 融合
6. `src/brain/pipeline/retrieval/classifier.py` —— 规则版 query classifier
7. `src/brain/pipeline/retrieval/sql_direct.py` —— LLM 翻译 query 到 SQL（仅在结构化分类下触发）
8. `src/brain/pipeline/ask.py` —— 重写主流程，整合上述
9. `src/brain/cli/ask.py` —— 加 `--mode`, `--debug` 选项

降级：

- 如果 sqlite-vec 未加载 → mode 自动变 keyword-only + warn
- 如果 embedding_index 空 → 同上

**测试** (`tests/unit/test_retrieval/`)：

- RRF 公式：手动构造三路结果，断言融合后排序符合公式
- classifier：给定 5 个结构化查询和 5 个开放查询，分类正确率 100%
- BM25：已知 corpus + query → 期望页面在 top-3
- sql_match：query "小张" → 找到 xiao-zhang 的 page

**集成测试** (`tests/integration/test_ask.py`)：

- 准备一个有 embedding 的 brain → 跑 hybrid ask → 期望页面在 top-N
- 同样 query 跑 `--mode keyword-only` 也能命中（fallback 健康）
- `--debug` 输出包含三路的命中详情
- 没 reindex 的 brain 跑 ask → 自动降级 + warn

## Task 21 — Bulk Import: Markdown / Text

**目标**：`mem import` 处理 .md 和 .txt 目录，进 laundry。

1. `src/brain/import_/__init__.py`
2. `src/brain/import_/extractors/base.py` —— `Extractor` Protocol + `ExtractedDocument`
3. `src/brain/import_/extractors/markdown.py`：
   - `MarkdownExtractor`
   - 文件 ≤ 8000 字 → 1 个 doc
   - 文件 > 8000 字 → 按 `# heading` 切
   - 处理 frontmatter（如果有就保留，没有就生成）
4. `src/brain/import_/discovery.py` —— `discover_files(path, kinds)` 递归扫描
5. `src/brain/import_/jobs.py` —— `import_jobs` / `import_files` CRUD
6. `src/brain/import_/cost.py` —— `cost_estimate(files) -> CostEstimate`
7. `src/brain/import_/importer.py` —— 主流程（不含 ingest）
8. `src/brain/cli/import_.py` —— CLI 包装
9. `src/brain/models/import_job.py` —— Pydantic 模型

**测试** (`tests/integration/test_import_md.py`)：

- 给定一个有 5 个 .md 文件的目录 → import → laundry/ 下应该有 5 个文件
- 一个长 markdown 文件按 heading 切成多个 doc
- 重复 import 同目录：第二次应该跳过已 ingested 的（基于 file_hash）
- `--dry-run`：不写任何 laundry 文件，但报告对
- `--resume`：第一次跑到一半中断，第二次接着跑

## Task 22 — Bulk Import: PDF + JSONL

**目标**：`mem import` 支持 PDF 和 JSONL。

1. `src/brain/import_/extractors/pdf.py`：
   - 用 pypdf 提取每页文本
   - 默认 5 页一组成一个 doc
   - 文本为空（扫描 PDF）→ 跳过该 PDF，标 `failed` 写明 "no text extracted (likely scanned)"
2. `src/brain/import_/extractors/jsonl.py`：
   - 自动 detect 格式 A vs 格式 B
   - 拼成 markdown，每条 message 一段
   - 填 `suggested_kind=ai_chat` 或 `human_chat`（看是否有 model 字段）

**测试**：

- 一个文本 PDF → 期望 N 个 doc 对应 ceil(总页数/5)
- 一个扫描 PDF（fixture: 空 text 的 PDF）→ 标失败，import 继续
- jsonl 格式 A → 1 行 = 1 个 conversation
- jsonl 格式 B → groupby conversation_id

## Task 23 — Import Progress, Cost Estimate, Status

**目标**：用户能看到 import 进度、成本、状态。

1. `src/brain/cli/cost_estimate.py` —— `mem cost-estimate`
2. `src/brain/cli/import_.py` 增强：
   - `--status [job-id]`
   - `--list-jobs`
   - `--abort <job-id>`
   - `--resume`
   - 进度条（`rich.progress`）
   - cost confirm prompt
3. `src/brain/cli/status.py` —— Phase 1 status 增加 Phase 2 信息：
   - embedding coverage
   - last reindex
   - active import jobs
   - token usage
   - total cost
4. `mem ingest` 加 `--no-auto-reindex` flag

**测试**：

- 中断 import (KeyboardInterrupt) → job 状态 `paused` → resume 接着跑
- `--abort`：job 状态 `failed`，剩余 pending 文件不再处理
- `mem status` 输出包含 Phase 2 字段
- `mem ingest` 默认完成后 auto-reindex 跑了
- `mem ingest --no-auto-reindex` 不跑 reindex

## Task 24 — Backwards-compat & Docs Sync

**目标**：所有文档反映当前实现，README 升级，Phase 1 / Phase 2 spec 一致。

这个任务**专门修 Phase 1 那次的 doc drift 教训**。Codex 必须：

1. **跑 `git grep` 找所有提到 Phase 1 / Phase 2 的地方**，确认状态描述准确
2. **审一遍 docs 里所有命令示例**，确认能在当前代码下跑通
3. 更新 `README.md`：
   - Top-level 加一段 Phase 2 features
   - Install 段加 `pip install -e ".[dev]"` 之后要 set `OPENAI_API_KEY`
   - Quick start 改成包含 `mem reindex` 的版本：
     ```
     mem init
     mem capture
     mem ingest
     mem reindex          # 新加
     mem ask "..."
     ```
   - 加一段 Multi-provider 说明（DeepSeek 默认 + OpenAI embedding + 三 provider 混搭）
   - 加 `mem import` 的一个实际例子
4. 更新 `docs/SPEC.md` 的 Phase 状态表格（Phase 2 改成 Done）
5. 检查所有 docs 里**没有矛盾**：
   - 不再说"不要加 openai 依赖"
   - 不再单独说"Anthropic API key" 而是 multi-provider
   - `mem ask` 默认行为说明全部更新成 hybrid
6. 写一个 `CHANGELOG.md`（如果没有）：
   - `## 0.2.0 — Phase 2`
   - 列 9 个 task 的成果
7. **跑 `pytest && ruff check .` 确认绿**

**Done 的标志**：

- `git grep -i "不要加 openai" docs/` 返回空
- `git grep -i "Anthropic API key" docs/ | grep -v multi-provider` 返回空
- README 里能找到 reindex / import / hybrid 三个关键词
- `mem --help` 输出和 `cli.md` 里描述一致

## Task 25 — Smoke Test Playbook

**目标**：写一个端到端验收脚本，**运行通过**才算 Phase 2 真完成。

1. 创建 `scripts/smoke_phase_2.ps1`（PowerShell, Windows-first）：

```powershell
# 完整 Phase 2 验收脚本
# 假设: $env:DEEPSEEK_API_KEY 和 $env:OPENAI_API_KEY 都已设置

$root = "$env:TEMP\brain-smoke-$(Get-Date -Format 'yyyyMMdd-HHmmss')"
Write-Host "Smoke test root: $root"

# Phase 1 baseline
mem init --root $root
"今天读了一篇关于 Transformer 的论文, 作者是 Vaswani, 主要贡献是 self-attention。" `
    | mem capture --brain-root $root --stdin
mem ingest --brain-root $root

# 假装用户 review 了 entity slug + facts
# (smoke 脚本里这一步可以跳过, 用 manual review 准备好的 fixture)

# Phase 2 reindex
mem reindex --brain-root $root

# Phase 2 hybrid ask
$result = mem ask --brain-root $root "Transformer 的作者是谁"
if ($result -notmatch "Vaswani") { throw "ASK FAILED: result missing expected entity" }

# Phase 2 import (mini fixture)
$importDir = "$env:TEMP\brain-import-fixture"
New-Item -ItemType Directory -Path $importDir -Force | Out-Null
"# Note 1`n这是第一篇笔记。" | Out-File "$importDir\note1.md" -Encoding utf8
"# Note 2`n这是第二篇笔记。" | Out-File "$importDir\note2.md" -Encoding utf8

mem import --brain-root $root $importDir --yes
$laundry = Get-ChildItem "$root\laundry" -Recurse -Filter "*.md" |
    Where-Object { $_.FullName -notlike "*processed*" }
if ($laundry.Count -lt 2) { throw "IMPORT FAILED: expected ≥2 laundry items" }

# Phase 2 status
mem status --brain-root $root

# 验证关键字段
$status = mem status --brain-root $root --json | ConvertFrom-Json
if ($status.embedding_coverage -eq $null) { throw "STATUS missing embedding_coverage" }

Write-Host "✅ All smoke tests passed"
```

2. 在 `tests/smoke/test_phase_2_smoke.py` 写一个 pytest 版（mock LLM 和 embedding API），**CI 跑这个**。

3. README 加一段："运行 `pwsh scripts/smoke_phase_2.ps1` 做完整验收"。

**Done 的标志**：

- 脚本在干净的环境里跑过（不要求 API key 有钱，但要能完整走完不抛异常——如果 API 失败应该看到优雅降级）
- pytest 版的 smoke 加进 CI（如果有 CI），跑全绿

---

## 进度追踪

每完成一个 Task 在 commit message 写 `[Task N] <summary>`。最后在 README 末尾标记进度：

```
Phase 2 progress:
- [x] Task 17 — Embedding Config & Client
- [x] Task 18 — Embedding Store with sqlite-vec
- [x] Task 19 — Page Indexer & Reindex
- [x] Task 20 — Hybrid Ask
- [x] Task 21 — Bulk Import: Markdown / Text
- [x] Task 22 — Bulk Import: PDF + JSONL
- [x] Task 23 — Import Progress, Cost Estimate, Status
- [x] Task 24 — Backwards-compat & Docs Sync
- [x] Task 25 — Smoke Test Playbook
```

## 给 Codex 的元规则

1. **不要重写 spec**。觉得设计不对，写到 `OPEN_QUESTIONS.md`，按 spec 做。
2. **不要乱加依赖**。tech-stack.md 已经锁定。
3. **不要省略测试**。
4. **不要跨任务**。
5. **每个 Task 完成跑 ruff + pytest 关键测试**，全绿才进下一个。
6. **不要实现 Phase 3 功能**。
7. **Spec 出现矛盾时停下来问**，不要自己脑补。
8. **Phase 1 的代码尽量不动**。Phase 2 主要是新增模块，少数几处必要修改（`db/connection.py` 加扩展、`mem ingest` 加 auto-reindex flag、`mem ask` 重写）才碰 Phase 1 文件。每改一处 Phase 1 代码都要确认 Phase 1 测试仍然全绿。
