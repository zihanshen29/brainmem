# Data Model

本文件定义所有数据结构。Codex 在写代码时必须严格遵循这里的字段名和类型。Phase 2 的新增内容用 **(P2)** 标注。

## 1. 目录布局

```
~/brain/
├── config.toml              # 配置（API key 引用、阈值等）
├── brain.db                 # SQLite 主库 (Phase 2 内含 vec 扩展表)
├── events.jsonl             # 事件账本 (append-only)
├── CLAUDE.md
├── README.md
├── .gitignore
├── .gitattributes
│
├── raw/                     # 原始素材 (不可变)
│
├── laundry/                 # 待处理素材
│   ├── <slug>.md            # 手动 capture 的杂乱内容
│   ├── obsidian-import/     # (P2) bulk import 自动建子目录
│   ├── chatgpt-history/     # (P2) bulk import 自动建子目录
│   └── processed/           # 处理后归档
│
├── pages/                   # L1 wiki
│   ├── index.md
│   ├── log.md
│   ├── entities/
│   ├── projects/
│   ├── concepts/
│   ├── events/
│   ├── experiences/
│   └── conversations/
│
├── review/                  # review 队列
│   └── archive/
│
└── imports/                 # (P2) bulk import 进度记录
    └── <job-id>/
        ├── manifest.json    # 该次 import 的所有文件清单 + 估算 + 状态
        └── errors.log       # 失败文件的错误信息
```

## 2. config.toml (Phase 2 新增 [embedding] 段)

```toml
[anthropic]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-opus-4-7"

[openai]
api_key_env = "OPENAI_API_KEY"
model = "gpt-5.5"

[deepseek]
api_key_env = "DEEPSEEK_API_KEY"
model = "deepseek-v4-pro"

[llm]
provider = "deepseek"               # 默认 LLM provider
fast_model_provider = "deepseek"    # 用于轻量任务的备选

[embedding]                         # === (P2) 新增 ===
provider = "openai_compatible"      # openai_compatible / local-bge / voyage (后两者 Phase 2 不实现)
base_url = "https://api.openai.com/v1"  # 可换成阿里百炼/硅基流动/智谱等兼容端点
model = "text-embedding-3-small"
dimension = 1536
api_key_env = "OPENAI_API_KEY"
batch_size = 100                    # 单次 API 调用最多 embed N 条
chunk_max_chars = 1500              # chunk 上限
unit_cost_per_1m_tokens = 0.02      # 用于 cost estimate

[paths]
brain_root = "~/brain"

[ingest]
confidence_auto_accept = 0.85
confidence_auto_reject = 0.50

[tier]
tier3_threshold = 1
tier2_threshold = 3
tier1_threshold = 8

[lint]
stale_days = 90

[git]
auto_commit = true

[retrieval]                         # === (P2) 新增 ===
default_mode = "hybrid"             # hybrid / keyword-only
rrf_k = 60                          # RRF 融合的 k 参数
top_per_path = 50                   # 三路各自取 top-N 进 RRF
final_top = 5                       # 用户最终看到的页面数
sql_shortcut_enabled = true         # 结构化查询是否短路

[import]                            # === (P2) 新增 ===
batch_size = 50                     # 一批文件后 commit 一次
auto_reindex = true                 # ingest 后自动增量 reindex
cost_confirm_threshold_usd = 1.00   # 估算超过此金额需用户确认
```

## 3. Event Ledger (events.jsonl)

Phase 2 新增两个 EventKind：

```python
EventKind = Literal[
    # Phase 1
    "raw_imported",
    "laundry_ingested",
    "note_appended",
    "ai_chat",
    "human_chat",
    "review_decided",
    "page_edited",
    "rebuild",
    # Phase 2
    "bulk_imported",        # bulk import 把一个文件转成 laundry item
    "reindexed",            # 一次 reindex 操作
]
```

`bulk_imported` 事件的 `metadata` 包含: `{"job_id": "...", "source_kind": "md/pdf/jsonl/...", "original_path": "..."}`。
`reindexed` 事件的 `metadata` 包含: `{"chunks_added": N, "chunks_updated": M, "chunks_removed": K, "model": "...", "tokens_used": T}`。

## 4. Page Format (Phase 1, 未变)

Page 结构、frontmatter 字段、section 顺序保持不变。Phase 2 不改 page format。

## 5. SQLite Schema

### 5a. Phase 1 表（保持不变）

`entities`, `entity_aliases`, `facts`, `backlinks`, `tier_proposals`, `ingest_cursor`, `lint_results` —— 都不动。

### 5b. Phase 2 新增表

通过 migration `0002_phase2.sql` 加上以下表：

```sql
-- =============================================================
-- Phase 2 migration
-- 加载 sqlite-vec 扩展后才能创建虚表
-- =============================================================

-- 注意: vec 虚表的 dimension 必须匹配 config.embedding.dimension
-- 换到不同维度的 model/provider 必须 DROP 这张表和 embedding_index 后重建
CREATE VIRTUAL TABLE embeddings USING vec0(
    embedding float[1536]
);

-- 把 vec 表的 rowid 映射到业务概念
CREATE TABLE embedding_index (
    rowid           INTEGER PRIMARY KEY,         -- 对应 embeddings.rowid
    page_slug       TEXT NOT NULL,                -- 引用 entities.id 或者 page filename
    chunk_kind      TEXT NOT NULL,                -- 'compiled_truth' | 'timeline_entry'
    chunk_id        TEXT NOT NULL,                -- compiled_truth: 'main'
                                                  -- timeline_entry: event_id
    content_hash    TEXT NOT NULL,                -- sha256(text + model + dim_version)
    model           TEXT NOT NULL,                -- 用了哪个 embedding model
    text_preview    TEXT NOT NULL,                -- 前 200 字, 检索时回显用
    created_at      TEXT NOT NULL,
    UNIQUE (page_slug, chunk_kind, chunk_id)      -- 一个 chunk 只能有一个当前 embedding
);

CREATE INDEX idx_embedding_index_page ON embedding_index(page_slug);
CREATE INDEX idx_embedding_index_hash ON embedding_index(content_hash);

-- Bulk import 的进度
CREATE TABLE import_jobs (
    id              TEXT PRIMARY KEY,             -- ULID
    source_path     TEXT NOT NULL,                -- 用户指定的 path
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    status          TEXT NOT NULL,                -- 'running' | 'completed' | 'failed' | 'paused'
    total_files     INTEGER NOT NULL,
    processed_files INTEGER NOT NULL DEFAULT 0,
    failed_files    INTEGER NOT NULL DEFAULT 0,
    estimated_tokens INTEGER,
    estimated_usd   REAL,
    actual_tokens   INTEGER NOT NULL DEFAULT 0,
    metadata        TEXT,                         -- JSON: {kinds_breakdown, ...}
    CHECK (status IN ('running', 'completed', 'failed', 'paused'))
);

-- 每个文件在 import 中的状态
CREATE TABLE import_files (
    job_id          TEXT NOT NULL REFERENCES import_jobs(id) ON DELETE CASCADE,
    file_path       TEXT NOT NULL,                -- 相对于 source_path 的相对路径
    file_hash       TEXT NOT NULL,                -- sha256 of file content
    kind            TEXT NOT NULL,                -- 'md' | 'txt' | 'pdf' | 'jsonl'
    status          TEXT NOT NULL,                -- 'pending' | 'extracted' | 'ingested' | 'failed' | 'skipped'
    laundry_path    TEXT,                          -- 写入 laundry 后的路径
    error           TEXT,
    processed_at    TEXT,
    PRIMARY KEY (job_id, file_path),
    CHECK (status IN ('pending', 'extracted', 'ingested', 'failed', 'skipped'))
);

-- 全局统计 (用于 mem stats 显示)
CREATE TABLE stats (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 初始化几条统计记录
INSERT INTO stats (key, value, updated_at) VALUES
    ('last_reindex_at', '', datetime('now')),
    ('total_embedding_tokens', '0', datetime('now')),
    ('total_extraction_tokens', '0', datetime('now')),
    ('total_cost_usd', '0', datetime('now'));
```

### 5c. sqlite-vec 加载

每次 `connect()` 时必须加载扩展：

```python
import sqlite_vec
import sqlite3

def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    return conn
```

Phase 1 的 `connect()` 函数要改成上面的形态。已有调用方不需要改动。

## 6. Embedding Index Models (Phase 2 新增 Pydantic)

```python
# brain/models/embedding.py

from datetime import datetime
from typing import Literal
from pydantic import BaseModel, Field

ChunkKind = Literal["compiled_truth", "timeline_entry"]

class EmbeddingChunk(BaseModel):
    """一个待 embed 的文本块。"""
    page_slug: str
    chunk_kind: ChunkKind
    chunk_id: str               # compiled_truth: 'main'; timeline_entry: event_id
    text: str                   # 实际要 embed 的内容
    text_preview: str           # 前 200 字

class EmbeddingRecord(BaseModel):
    """已写入 DB 的 embedding 记录。"""
    rowid: int
    page_slug: str
    chunk_kind: ChunkKind
    chunk_id: str
    content_hash: str
    model: str
    text_preview: str
    created_at: datetime

class RetrievalHit(BaseModel):
    """单条检索命中, 用于 RRF 融合。"""
    page_slug: str
    chunk_kind: ChunkKind
    chunk_id: str
    score: float                # 路径自身的分数 (vector: distance, BM25: score)
    rank: int                   # 在该路径里的排名 (1 = top)
    path: Literal["vector", "keyword", "sql"]

class FusedResult(BaseModel):
    """RRF 融合后的最终结果。"""
    page_slug: str
    chunks: list[RetrievalHit]  # 同一页面的多个 chunk 命中
    rrf_score: float
    final_rank: int
```

## 7. Import Job Models (Phase 2 新增)

```python
# brain/models/import_job.py

ImportFileKind = Literal["md", "txt", "pdf", "jsonl"]
ImportFileStatus = Literal["pending", "extracted", "ingested", "failed", "skipped"]
ImportJobStatus = Literal["running", "completed", "failed", "paused"]

class ImportFile(BaseModel):
    job_id: str
    file_path: str          # 相对 source_path
    file_hash: str
    kind: ImportFileKind
    status: ImportFileStatus
    laundry_path: str | None = None
    error: str | None = None
    processed_at: datetime | None = None

class ImportJob(BaseModel):
    id: str                 # ULID
    source_path: str
    started_at: datetime
    finished_at: datetime | None = None
    status: ImportJobStatus
    total_files: int
    processed_files: int = 0
    failed_files: int = 0
    estimated_tokens: int | None = None
    estimated_usd: float | None = None
    actual_tokens: int = 0
    metadata: dict = Field(default_factory=dict)

class CostEstimate(BaseModel):
    """import dry-run 的预估结果。"""
    total_files: int
    by_kind: dict[ImportFileKind, int]
    estimated_extraction_tokens: int
    estimated_embedding_tokens: int
    estimated_extraction_usd: float
    estimated_embedding_usd: float
    estimated_total_usd: float
```

## 8. CLAUDE.md（增量）

Phase 2 不改 CLAUDE.md 的核心内容（page format 没变）。但 `mem init` 和 `mem rebuild --schema` 时要确保 CLAUDE.md 仍然反映当前 schema 规范。

## 9. 命名规范（Phase 1 + Phase 2）

- Phase 1 规则保留：slug 全小写 ASCII、event id 用 ULID、fact id 自增
- **(P2)** import_job id：ULID
- **(P2)** content_hash：sha256(text + model + dim_version)，hex 编码
- **(P2)** chunk_id：compiled_truth chunk 用字面量 `main`；timeline_entry 用 event_id

## 10. Migration 规则

Phase 2 新增 migration `0002_phase2.sql`，由 `brain/db/migrations.py` 在打开旧 brain 时自动检测 `PRAGMA user_version` 升级。

升级流程:
1. 检测 user_version=1 → 跑 0002_phase2.sql → 设 user_version=2
2. 用户在升级后第一次跑 `mem ask` 或 `mem stats` 时，如果 `embedding_index` 是空的，提示用户 "Run `mem reindex` to enable hybrid retrieval"
3. **不在 migration 中自动 reindex**——这会在升级时调 embedding API 收钱，必须用户显式触发

## 11. 失败模式与回滚

| 场景 | 行为 |
|---|---|
| migration 0002 跑到一半失败 | 回滚事务, user_version 保持 1, 报错让用户重试 |
| sqlite-vec 扩展加载失败 | `mem ask` 优雅降级到 `--keyword-only`, warn 一次 |
| embedding API 失败 | 该 chunk 跳过, 写入 `errors.log`, 整个 reindex 不中断 |
| import 中文件失败 | 该文件标记 `failed`, 整个 import 继续 |
| dimension 不匹配（换了 model/provider 但 vec 表仍是旧维度）| `mem reindex` 报错要求用户确认重建 vec 表并 `--force`，防止旧维度污染 |
