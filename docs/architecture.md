# Architecture

## 五层结构（Phase 2 后）

```
┌──────────────────────────────────────────────────────────────────┐
│  L3 · Retrieval (Phase 2: hybrid)                                 │
│  vector (sqlite-vec) + BM25 + SQL 实体匹配 → RRF 融合              │
│  brain-ops: 先查 brain，没有就说不知道                             │
└──────────────────────────────────────────────────────────────────┘
                                ▲
┌────────────────────────────────────────┐ ┌──────────────────────┐
│  L1 · Wiki (人读 + LLM 读)              │ │  L2 · Backbone        │
│  ~/brain/pages/*.md                     │ │  brain.db (SQLite)    │
│  compiled truth + timeline              │ │  ├── entity registry  │
│  六类页面                                │ │  ├── facts (bi-temp)  │
│                                         │ │  ├── backlinks · tier │
│                                         │ │  └── embeddings (P2)  │
│                                         │ │      embedding_index  │
└────────────────────────────────────────┘ └──────────────────────┘
                                ▲
┌──────────────────────────────────────────────────────────────────┐
│  Pipelines (手动触发)                                              │
│  ingest: signal-detect → resolve-entity → tier 决策 → 写 wiki      │
│  reindex (P2): chunk pages → embed → upsert sqlite-vec             │
│  import (P2): walk path → laundry → ingest → reindex               │
└──────────────────────────────────────────────────────────────────┘
                                ▲
┌──────────────────────────────────────────────────────────────────┐
│  L0 · Source of Truth (不可变, append-only)                       │
│  ~/brain/events.jsonl  (事件账本)                                  │
│  ~/brain/raw/          (原始材料: PDF, 剪藏, 音频转文本)            │
│  ~/brain/laundry/      (杂乱待处理素材, 处理后归档)                  │
└──────────────────────────────────────────────────────────────────┘
```

新增组件用 (P2) 标注。L0 / L1 / L2 的核心结构不变，只在 L2 内部加了 `embeddings` 和 `embedding_index` 两张表，并多一个 reindex / import 管线。

## L0 — Source of Truth (Phase 1, 未变)

L0 是整个系统的"地基"，**不可变**。所有上层数据都可以从 L0 重建。结构、event 格式、laundry 归档逻辑保持 Phase 1 设计。

Phase 2 的一个微小调整：`mem import` 大批量导入素材时，会把它们先放进 `~/brain/laundry/import-<job-id>/` 子目录，便于追溯素材来源、查看 job 状态和做 cost estimate。手动 capture 的素材仍然直接放 laundry 根目录。

## L1 — Wiki (Phase 1, 未变)

六类页面、compiled truth + timeline 模式、`[[slug]]` 引用语法保持不变。Phase 2 不改 page format，只是把 page 内容拿去做 embedding。

## L2 — Backbone (Phase 2 扩展)

Phase 1 的核心职责保留：

1. 实体注册表（处理别名）
2. Bi-temporal 事实表
3. Backlink 表
4. Tier 状态

Phase 2 在同一个 `brain.db` 里**新增三张表**：

- **`embeddings`** (vec0 虚表)：每行 `(rowid, embedding BLOB)`，由 sqlite-vec 提供。
- **`embedding_index`** (普通表)：rowid → (page_slug, chunk_kind, chunk_id, content_hash, model, created_at) 的映射。这是连接 vec 表和业务概念的桥梁。
- **`import_jobs`**：bulk import 的进度记录（断点续做）。

详细 schema 见 `data-model.md`。

**为什么不引入 Chroma**：Chroma 多一个进程、多一份数据、多一个备份目标。sqlite-vec 直接挂在 `brain.db` 上，备份就是复制一个文件，进程依然只有 CLI 自己。代价是功能少（不支持 metadata filter 复杂查询、不支持 hybrid query 内置），但我们的 retrieval 路径本来就是手写融合，用不到 Chroma 的高级功能。

## L3 — Retrieval (Phase 2 重写)

Phase 1 的 `mem ask` 是关键词 + SQL + backlink 加权。Phase 2 把它升级成 **hybrid retrieval**。

### 三路召回

```
query
  │
  ├──> vector path: embed(query) → sqlite-vec top-50
  │
  ├──> keyword path: tokenize(query) → BM25 score 所有 page → top-50
  │
  └──> SQL path: 提取 query 中的实体名/别名 → 命中 entities/aliases → 关联 page top-50

三路结果各自排序后进 RRF 融合：
  RRF score = Σ 1 / (k + rank_i),  k = 60

取融合后 top-N 返回
```

### 结构化短路

并非所有查询都该走 RRF。形如以下的查询应该**直接走 SQL**：

- "我 2025 Q2 在做什么项目"（subject=i, predicate=works_on, valid_from in range）
- "小张当前在哪家公司"（subject=xiao-zhang, predicate=works_at, active）
- "我什么时候开始学 Python"（subject=i, predicate=studies, object=python）

这类查询答案在 facts 表里就是确切的，向量相似度反而会引入"差不多但不对"的页面。`mem ask` 里有一个轻量的 query classifier（纯规则，不调 LLM）判断是否走 SQL 短路，否则进入三路 RRF。规则识别不了的结构化查询直接走 hybrid fallback。

### `--mode keyword-only` 兜底

Phase 1 的纯关键词路径保留，作为 `mem ask --mode keyword-only` 触发。这给将来 hybrid 出 bug 时提供 fallback。

### `--debug` 模式

`mem ask --debug` 显示三路各自的 top-10 和 RRF 后的最终排序，便于诊断"为什么这个页面没出来"。

## 新增管线 1: Reindex (Phase 2)

负责把 page 内容转成 embedding 写入 `embeddings` 表。

```
reindex():
    for page in all_pages():
        chunks = split_page(page)
            # → 1 chunk for compiled_truth
            # → 1 chunk per timeline entry
        for chunk in chunks:
            content_hash = sha256(chunk.text + model + version)
            if hash exists in embedding_index:
                continue  # 内容未变, 跳过
            vector = embedding_client.embed(chunk.text)
            insert into embeddings (rowid, embedding)
            upsert embedding_index (rowid, page_slug, chunk_kind, ...)
        prune chunks no longer present in this page
    update last_reindex_at in stats table
```

触发方式：

- **自动增量**：`mem ingest` 写入新 page 或修改现有 page 后自动调用，只 embed 新增/变更的 chunk
- **手动全量**：`mem reindex` 显式跑，可加 `--force` 重 embed 所有（用于换 model 时）
- **selective**：`mem reindex --pages <slug>` 重 embed 特定页面

## 新增管线 2: Bulk Import (Phase 2)

负责把外部目录递归处理成 brain 内容。

```
import(path):
    1. discover_files(path)
       → list[(file_path, kind)]  # md / txt / pdf / jsonl
    2. cost_estimate(files)
       → 估 token, 提示用户确认
    3. for each file (in 持久化的 import_jobs cursor 控制下):
       a. extract_text(file)            # 不同 kind 走不同提取器
       b. 写入 ~/brain/laundry/<job>/   # 进 laundry, 不是直接进 events
       c. 标记 import_jobs cursor       # 断点续做
    4. 提示用户跑 mem ingest 处理 laundry
```

**故意不自动 ingest**：bulk import 把素材放进 laundry 后**停下来**，让用户跑 `mem ingest` 单独处理。理由：
- ingest 调 LLM，是 import 之外的另一笔成本
- 用户可能想在 ingest 之前预览/筛选 laundry 里的内容
- 错误隔离更清晰：import 出问题就重试 import，ingest 出问题就重试 ingest

如果用户希望一气呵成，当前推荐显式串联：`mem import <path> --yes`，然后 `mem ingest`。`mem ingest` 会按配置自动触发增量 reindex；需要跳过时使用 `mem ingest --no-auto-reindex`。

### 文件类型处理器

每种 kind 对应一个 extractor，输入文件路径，输出 `list[LaundryItem]`：

| kind | 提取方式 | 一个文件 → 几个 laundry item |
|---|---|---|
| `.md` / `.txt` | 直接读 | 1 个（除非超过 chunk 上限按段切） |
| `.pdf` | pypdf 提取文本 | 1 个或多个（按页或按章节） |
| `.jsonl` | 逐行解析为 message | 每个 conversation 1 个 |

extractor 共享一个 base interface 在 `brain/import/extractors/base.py`。

## 新增组件 3: Embedding Client (Phase 2)

`brain/llm/embedding.py` 是 embedding provider 抽象，跟 `brain/llm/client.py` 的 LLM 抽象同形。

```python
class EmbeddingClient(Protocol):
    def embed(self, texts: list[str], model: str) -> list[list[float]]: ...
    @property
    def dimension(self) -> int: ...

class OpenAICompatibleEmbeddingClient: ...  # Phase 2 实现: OpenAI 官方或兼容 base_url
class LocalBGEEmbeddingClient: ...  # 留位, Phase 2 不实现
class VoyageEmbeddingClient: ...    # 留位, Phase 2 不实现
```

config.toml 里 `[embedding]` 独立段：

```toml
[embedding]
provider = "openai_compatible"
base_url = "https://api.openai.com/v1"  # 可改成阿里百炼 / 硅基流动 / 智谱等兼容端点
model = "text-embedding-3-small"
dimension = 1536
api_key_env = "OPENAI_API_KEY"
batch_size = 100        # 单次 embed 调用最多 N 条文本
```

**注意 dimension 字段必须和 provider 返回的向量维度一致**。换 model 或换到国产兼容 provider 时，先确认维度；维度变化需要清空/重建 vec 表并 `mem reindex --force`，旧维度不能和新维度共存于同一个 vec 表。

## 设计原则（继承 Phase 1）

下面五条不变，仅强调 Phase 2 的相关含义：

1. **代码做数据，LLM 做判断。** Phase 2 加强这条：embedding 是确定性代码（输入相同，向量相同），不需要 LLM；retrieval 的 RRF / SQL 短路 / chunking 都是确定性代码。LLM 只在 ingest 抽取、conflict judge、compiled truth rewrite 三处出现。
2. **Source of truth 不可变。** Phase 2 的 embedding 是派生数据，丢了可以从 page 重建（`mem reindex --force`）。
3. **永不静默写入。** Phase 2 的 bulk import 在跑 LLM ingest 之前会停下让用户检查 laundry，cost estimate 也要显式确认。
4. **Append-only 优先。** Phase 2 的 `embeddings` 表是真正可重建的派生层，content_hash 不匹配就重 embed，不需要 append-only。
5. **每条派生信息有 provenance。** Phase 2 的每个 embedding 通过 `embedding_index.page_slug` 指回页面，page 的 timeline 再指回 event id。链路完整。

## 与 Phase 1 的兼容性

Phase 2 不破坏 Phase 1：

- 现有 `brain.db` schema 通过 `0002_phase2.sql` migration 增量升级，不需要重建
- 现有 markdown page 不需要修改格式
- 现有 `mem ask` 在没跑过 reindex 时退化成 Phase 1 行为（只 keyword + SQL）
- 升级后第一次需要用户跑 `mem reindex` 把现有 page 索引一遍
