# Architecture

## 四层结构

```
┌─────────────────────────────────────────────────────────┐
│  L3 · Retrieval                                         │
│  SQL + 关键词检索 (Phase 1, 不上向量)                    │
│  brain-ops: 先查 brain，没有就说不知道                    │
└─────────────────────────────────────────────────────────┘
                          ▲
┌────────────────────────────────┐ ┌────────────────────┐
│  L1 · Wiki (人读 + LLM 读)      │ │  L2 · Backbone     │
│  ~/brain/pages/*.md             │ │  brain.db (SQLite) │
│  compiled truth + timeline      │ │  entity registry   │
│  六类页面                        │ │  facts (bi-temp)   │
│                                 │ │  backlinks · tier  │
└────────────────────────────────┘ └────────────────────┘
                          ▲
┌─────────────────────────────────────────────────────────┐
│  Pipeline (手动触发)                                     │
│  signal-detect → resolve-entity → tier 决策              │
│  → 写 wiki + facts → review queue                       │
└─────────────────────────────────────────────────────────┘
                          ▲
┌─────────────────────────────────────────────────────────┐
│  L0 · Source of Truth (不可变, append-only)              │
│  ~/brain/events.jsonl  (事件账本)                        │
│  ~/brain/raw/          (原始材料: PDF, 剪藏, 音频转文本)   │
│  ~/brain/laundry/      (杂乱待处理素材, 处理后归档)        │
└─────────────────────────────────────────────────────────┘
```

## L0 — Source of Truth

L0 是整个系统的"地基"，**不可变**。所有上层数据都可以从 L0 重建（见 `pipeline.md` 的 rebuild 章节）。

### Event Ledger

`~/brain/events.jsonl` 是单文件 JSONL，每行一条事件。每个事件代表一个外部信号被系统观察到的时刻：

- 一次和别人的对话
- 一次和 AI 的对话
- 一次手动笔记追加
- 一次文档导入
- 一次 review 决定
- 一次 ingest 操作

事件颗粒度：**中等**——一次原始素材里如果包含多个独立话题，由 pipeline 的 signal-detect 步骤切分成多个事件，每个话题一个事件。粗 / 细的颗粒度都不取。

### Raw 目录

`~/brain/raw/` 存放原始素材：导入的 PDF、网页剪藏、聊天记录原文、会议录音转文本等。每个文件不可变，命名遵循 `<YYYY-MM-DD>_<source>_<slug>.<ext>` 格式。

### Laundry

`~/brain/laundry/` 是"未消化"区。新的杂乱素材（一段乱写的思考、一份会议纪要、一段聊天截图转文本）先放这里，等 `mem ingest` 处理。处理完后**归档**到 `~/brain/laundry/processed/`，不删除——这样原始素材永远可回看。

## L1 — Wiki

L1 是 markdown 文件的集合，组织在 `~/brain/pages/` 下，按类型分子目录：

```
~/brain/pages/
├── entities/      人和组织
├── projects/      项目
├── concepts/      研究话题（投资、健身营养、ML 等）
├── events/        重要事件
├── experiences/   经历（旅行、参加的活动、去过的地方）
└── conversations/ 与他人的对话/会议（不是 AI 对话）
```

每个页面遵循 **compiled truth + timeline** 模式（详见 `data-model.md`）。

### 与 L0 的关系

L1 是 L0 的"派生视图"。每条 timeline 项都引用一个或多个 source event id；compiled truth 是基于这些 events 由 LLM 综合出来的。原则上 L1 可以从 L0 完全重建（rebuild 命令）。

### 与 AI 对话的处理

默认情况下，AI 对话（与 Claude / Codex 等的会话）只进 `events.jsonl`，**不自动建 conversations 页**。如果用户认为某次 AI 对话特别有价值（比如这次设计讨论），运行 `mem promote-chat <event-id>` 显式把它提升为 conversations 页。

## L2 — Backbone

L2 是 SQLite 数据库 `~/brain/brain.db`，**只存索引和 metadata，不存事实主体**。事实主体在 markdown 页面里。

L2 的核心职责：

1. **实体注册表**：每个 entity 一个 canonical id，所有别名指向它。处理"张三 = Zhang San = 老张"这类问题，用 DB 操作而非文件合并。
2. **Bi-temporal 事实表**：可被 SQL 精确查询的结构化事实，每条带 `valid_from / valid_to / asserted_at / source_ref / confidence` 五个字段。
3. **Backlink 表**：页面之间的类型化链接（mentioned_in、works_on、part_of 等），由确定性代码从 markdown 提取，零 LLM 调用。
4. **Tier 状态**：每个 entity 当前的 tier (1/2/3) 和 mention count。

详细 schema 见 `data-model.md`。

## L3 — Retrieval (Phase 1)

Phase 1 不上向量。检索靠：

1. **SQL 直查**：facts 表的精确查询（"我 2025 Q2 在做什么项目"）
2. **关键词匹配**：在 markdown 页面内容上做 ripgrep / 内置 BM25-like 简单评分
3. **Backlink 遍历**：从一个 entity 出发，找到所有提到它的页面

`mem ask` 命令把这三种结果融合（简单加权，不上 RRF），返回 top-N 页面摘要。

`mem ask --explain` 模式会把 top-N 页面作为 context 喂给 Claude，让它生成自然语言答案，并强制要求"先查 brain，没有就直说不知道"（brain-ops 原则）。

Phase 2 会加 Chroma 向量、RRF、graph walk，但本 spec 不涉及。

## 设计原则

### 1. 代码做数据，LLM 做判断

下面的事 **必须** 用代码做：

- 文件读写、git commit
- 实体名称匹配（基于 alias 表）
- Backlink 提取（regex 扫描页面内容找已知 entity 名称）
- 关键词检索、SQL 查询
- Tier 升级触发（mention count 阈值）
- 冲突检测（新事实和已有事实的 subject + predicate 相同但 object 不同）

下面的事 **可以** 调 LLM：

- 从 raw / laundry 素材里抽取事实候选
- 从素材里识别新 entity（首次出现）
- 判断"新事实是不是真的覆盖旧事实"（borderline 的冲突）
- 改写 compiled truth（用户批准后才执行）
- Lint 报告里的判断（例如"这两条 timeline 项是不是讲同一件事"）

### 2. Source of Truth 是不可变的

事件账本只追加。raw 文件不修改。Markdown 页面可以改但每次改都进 git，所以历史可追。SQLite 是派生数据，理论上可以删了重建。

### 3. 永不静默写入

四种情况进 review 队列：

- 抽出的事实和已有事实**矛盾**
- 抽出事实的 confidence 处于中等区间（默认 0.5–0.85）
- 一个 entity 被提及的次数达到 tier 升级阈值（3 次或 8 次）
- Lint 发现可能的不一致

review 队列形态是 markdown 文件（`~/brain/review/<timestamp>_<slug>.md`），用户用任何编辑器打开，编辑后运行 `mem review` 处理。

### 4. Append-only 优先

- 事件账本：append-only，永远不改也不删
- Timeline section：append-only
- Compiled truth：可重写，但每次重写都 git commit，历史可追
- Facts 表：通过 `superseded_by` 字段做软删除，原记录保留

### 5. Provenance 强制

每条 timeline 项都必须带 `event_id` 引用。每条 fact 都必须带 `source_ref` 引用。Compiled truth 段不强制 inline citation，但页面底部要有 source events 列表。

## Rebuild 能力

任何上层数据丢失都不致命：

- SQLite 损坏 → `mem rebuild --db` 从 events.jsonl + 当前 markdown 页面重建
- Markdown 页面丢失 → `mem rebuild --pages <slug>` 从 events.jsonl 重建该页面
- backlinks 不一致 → `mem rebuild --backlinks` 从所有 markdown 内容重新扫描

只有 events.jsonl 和 raw/ 不能丢。这两个目录在 git 仓库里，加上定期备份就稳了。

## 与 Karpathy LLM Wiki 的关系

借鉴：raw / wiki / schema 三层 + ingest / query / lint 三操作 + index 和 log 文件。
不借鉴：纯文件方案（我们加了 SQLite 骨架）、用 grep 做检索（我们 Phase 2 会上向量）、依赖 frontier model 自动维护一切。

## 与 GBrain 的关系

借鉴：compiled truth + timeline 模式、entity 中心化页面、tier 化丰富、event ledger 做源头真相、entity registry 处理别名、auto-link 零 LLM 实体提取、brain-ops（先查再答）原则。
不借鉴：dream cycle（用户选择手动触发）、绑定特定 agent（OpenClaw / Hermes）、Postgres + Supabase（本地 SQLite 够用）、自动 tier 升级（改成 review 制）、依赖 frontier model 执行所有自动化步骤（我们用代码 + LLM 混合）。
