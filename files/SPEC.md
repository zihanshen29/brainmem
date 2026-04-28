# Brain — Personal Memory System

## 这是什么

`brain` 是一个本地、单用户的个人记忆系统，用来把碎片化的笔记、对话、文档、思考逐步编译成可查询、可演化的知识库。设计灵感来自 Andrej Karpathy 的 LLM Wiki（plain markdown + LLM 作 compiler）和 Garry Tan 的 GBrain（compiled truth + timeline + entity-centric pages），但**没有任何外部依赖**：不连云、不依赖某个 agent runtime、不用付费服务。

核心心智模型：**LLM 是 compiler，原始素材是 source code，知识库是 compiled artifact**。系统不是一个聊天机器人，是一个**离线编译管线 + 本地查询接口**。

## 这不是什么

- 不是常驻 agent。所有处理都由用户手动触发命令完成。
- 不是 SaaS。完全本地运行，数据放在用户家目录。
- 不是自动决策系统。所有冲突、所有 tier 升级、所有重要写入都进入 review 队列等用户批准。
- 不是 RAG 框架。Phase 1 不上向量库，靠 SQL + 结构化检索。

## 阅读顺序（给 Codex）

构建之前请按这个顺序读完所有文件：

1. **`SPEC.md`**（本文件）—— 知道整体目标和边界
2. **`architecture.md`** —— 理解四层结构和设计原则
3. **`data-model.md`** —— 所有数据结构（事件、页面、SQLite schema）
4. **`pipeline.md`** —— ingest / review / lint 算法
5. **`cli.md`** —— 用户接口规范
6. **`tech-stack.md`** —— Python 项目结构、依赖、Windows 注意事项、测试方针
7. **`phase-1-tasks.md`** —— **按这个顺序执行构建**

读完之后，从 `phase-1-tasks.md` 第一个任务开始。每完成一个任务运行对应测试，通过后再进下一个。

## Phase 1 范围

本 spec **只描述 Phase 1**。Phase 1 的目标是一个能跑通的最小系统，包含：

- L0 事件账本（`events.jsonl`）和 raw 素材目录
- L1 markdown wiki，遵循 compiled truth + timeline 模式，六类页面
- L2 SQLite 骨架（实体注册表、bi-temporal 事实、backlink、tier）
- 手动触发的 ingest / review / lint / ask / promote-chat 五条 CLI 命令
- 基于 SQL + 关键词的检索（**不上向量、不上 RRF、不上 graph walk**）
- Laundry 机制处理杂乱素材
- 关键路径的测试

Phase 2（**本 spec 不涉及，将来另写**）会加：向量索引（Chroma）、混合检索（RRF）、可观测性面板、自动化定时任务。Codex 不要提前实现 Phase 2 的内容，即使你认为很容易做。

## 关键原则（贯穿所有文件）

1. **代码做数据，LLM 做判断。** 能用确定性代码完成的事（实体链接、文件读写、SQL 查询、backlink 提取）一律不调 LLM。LLM 只用于真正需要语义理解的步骤（事实提取、冲突判定、tier 升级建议）。
2. **事件账本是唯一的源头真相。** markdown 页面是事件账本的"渲染视图"，可以重建。任何不一致以事件账本为准。
3. **永不静默写入。** 任何 LLM 抽出的事实、任何 tier 升级、任何冲突都要进 review 队列等用户批准。系统不替用户做决定。
4. **append-only 优先。** 事件账本只追加；timeline 只追加；compiled truth 可以重写但要带 git 历史。
5. **每条派生信息都要 provenance。** 每个事实、每条 timeline 项都指回 source event id，可追溯到 raw payload。

## 项目命名

- 仓库名：`brain`
- Python 包名：`brain`
- CLI 入口：`mem`
- 用户数据目录：`~/brain/`（Windows: `%USERPROFILE%\brain\`）
- 配置文件：`~/brain/config.toml`
- 数据库文件：`~/brain/brain.db`

## 用户上下文

用户是单人使用，会自己运行命令、看 review 队列、改 markdown 文件。用户**不会**让系统自动跑、不需要常驻服务、不需要 web UI。CLI 是唯一接口。
