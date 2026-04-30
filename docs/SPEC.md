# Brain — Personal Memory System

## 这是什么

`brain` 是一个本地、单用户的个人记忆系统，把碎片化的笔记、对话、文档、思考逐步编译成可查询、可演化的知识库。设计灵感来自 Andrej Karpathy 的 LLM Wiki（plain markdown + LLM 作 compiler）和 Garry Tan 的 GBrain（compiled truth + timeline + entity-centric pages），完全本地、无云依赖、无第三方 SaaS。

核心心智模型：**LLM 是 compiler，原始素材是 source code，知识库是 compiled artifact**。系统不是聊天机器人，是**离线编译管线 + 本地查询接口**。

## 这不是什么

- 不是常驻 agent。所有处理由用户手动触发命令完成。
- 不是 SaaS。完全本地运行，数据在用户家目录。
- 不是自动决策系统。所有冲突、tier 升级、重要写入都进 review 队列等用户批准。
- 不是 RAG 框架。Phase 1 不上向量；Phase 2 上向量但是 sqlite-vec 这种本地嵌入式方案。

## Phase 状态

| Phase | 状态 | 范围 |
|---|---|---|
| Phase 1 | ✅ Done | L0 ledger, L1 wiki, SQLite backbone, manual ingest/review/lint/ask, laundry, page-type 分流, pending-fact 流程 |
| **Phase 2** | 🟢 In progress | Hybrid retrieval (vector + keyword + SQL + RRF), bulk import, sqlite-vec embedding store, cost estimation |
| Phase 3 | 📋 Planned | Procedural memory (rules pages + reflection pipeline), bi-temporal query UX, multi-brain federation |

## 阅读顺序（给 Codex）

1. **`docs/SPEC.md`**（本文件）—— 知道整体目标和边界
2. **`docs/architecture.md`** —— 五层结构 + Phase 2 的 retrieval / import / embedding 三个新组件
3. **`docs/data-model.md`** —— 所有数据结构（事件、页面、SQLite schema，Phase 2 新增 embeddings 表）
4. **`docs/pipeline.md`** —— ingest / review / lint / ask / reindex / import 算法
5. **`docs/cli.md`** —— 用户接口规范（Phase 2 新增 `mem import`, `mem reindex`, `mem cost-estimate`）
6. **`docs/tech-stack.md`** —— Python 项目结构、依赖、Windows 注意事项、测试方针
7. **`docs/phase-2-tasks.md`** —— **按这个顺序执行 Phase 2 构建（Task 17–25）**

读完之后，从 `phase-2-tasks.md` 第一个任务开始。每完成一个任务跑对应测试，通过后再进下一个。

## Phase 2 范围

Phase 2 的目标是让 brain **从"能用的玩具"变成"日常使用的工具"**，关键在两件事：**装得进**（bulk import）和 **问得准**（hybrid retrieval）。具体包含：

### 1. Embedding 索引层 (sqlite-vec)

- 在现有 `brain.db` 里加 `embeddings` 虚表（vec0 类型）+ `embedding_index` 普通映射表
- 不引入第二个数据库进程或第二套存储
- 默认 embedding model: OpenAI `text-embedding-3-small`（1536 维, $0.02 / 1M token）
- Embedding provider 走独立 `[embedding]` 段，默认用 OpenAI 官方 API；也支持 OpenAI-compatible `base_url`（例如阿里百炼、硅基流动、智谱等兼容服务），前提是返回维度与 `config.embedding.dimension` 一致
- 接口设计成 provider 可插拔，将来加本地 BGE / Voyage 不用改调用方

### 2. Page indexer

- 切片单元: `# Compiled truth` 一个 chunk + 每条 timeline entry 一个 chunk
- 不索引 frontmatter、不索引 `# Sources`
- chunk 上限 1500 字符，超长截断
- 增量 reindex 靠 `content_hash` 比对（相同内容不重 embed）

### 3. Hybrid retrieval

- 三路召回：vector (sqlite-vec)、BM25 关键词、SQL 实体匹配
- 各路 top-50 进 RRF 融合（k=60）后取 top-N
- 结构化查询（"我 2025 Q2 在做什么项目"）走确定性 SQL 直查路径，不走 RRF 稀释
- `mem ask` 的检索阶段不调 LLM；只有 `--explain` 会把检索结果交给 LLM 生成回答
- `mem ask` 默认走 hybrid；`--keyword-only` flag 回到 Phase 1 行为

### 4. Bulk import

- `mem import <path>` 递归处理目录，进 laundry 走 ingest
- 支持: `.md` / `.txt` / `.pdf`（pypdf 文本提取）/ `.jsonl`（chat history 格式）
- 每文件独立事务，断点续做（cursor based）
- 速率限制 + cost estimate（dry-run 显示预估 token 数和金额）

### 5. 可观测性补丁（小型）

- `mem stats` 增加：embedding 覆盖率、上次 reindex 时间、累计 token 消费
- `mem ask --debug` 显示 RRF 三路各自的 top-N 和合并后排序
- import 进度条 + ETA + 错误隔离（一个文件失败不阻塞其余）

## Phase 2 非目标（明确不做）

- **Procedural memory / rules pages**：放 Phase 3，需要先用一段时间 Phase 2 才能想清楚需求形态
- **Web UI / TUI**：CLI 是唯一接口，`rich` 美化 stdout 即可
- **后台 scheduler**：所有命令仍由用户手动触发
- **Graph database**：backlink + SQL 关系表已经够用
- **OCR**：扫描 PDF / 图片暂不支持，只处理文本 PDF
- **HTML / EPUB / URL 直抓**：用户先用浏览器导出成 markdown 或 PDF
- **多 brain federation**：单仓库设计

## 关键原则（Phase 1 + Phase 2 都遵守）

1. **代码做数据，LLM 做判断**。能用确定性代码完成的事不调 LLM。
2. **事件账本是唯一源头真相**。markdown 页面是渲染视图，可重建。
3. **永不静默写入**。任何 LLM 抽出的事实、tier 升级、冲突都进 review 队列。
4. **Append-only 优先**。事件账本只追加；timeline 只追加；compiled truth 可重写但带 git 历史。
5. **每条派生信息都有 provenance**。每个事实、每条 timeline 项指回 source event id。
6. **(Phase 2 新增) Spec 是 source of truth**。发现实现偏离 spec 时，先更新 spec 再改实现，或反过来——但不能让 docs 和代码长期不一致。Phase 2 的 Task 24 强制做一次 docs 同步。

## 项目命名

- 仓库名：`brain` (GitHub 上是 `brainmem`)
- Python 包名：`brain`
- CLI 入口：`mem`
- 用户数据目录：`~/brain/`（Windows: `%USERPROFILE%\brain\`）
- 配置文件：`~/brain/config.toml`
- 数据库文件：`~/brain/brain.db`

## LLM Provider 现状（Phase 1 后已稳定）

支持三个 provider，运行时由 config 切换：

- **DeepSeek (默认)**：`deepseek-v4-pro` 用于抽取/判断/重写。OpenAI-compatible API。
- **OpenAI**：通过 Responses API。
- **Anthropic**：通过 Messages API。

config.toml 的 `[anthropic]` / `[openai]` / `[deepseek]` / `[embedding]` 各自独立，可以混搭——例如 LLM 走 DeepSeek、embedding 走 OpenAI 官方或 OpenAI-compatible embedding 服务（这是默认推荐）。
