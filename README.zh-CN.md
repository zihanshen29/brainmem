# BrainMem

<p align="center">
  <strong>面向人和智能体的本地优先生命记忆系统。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-CN.md">中文</a>
</p>

<p align="center">
  <img alt="Python 3.11" src="https://img.shields.io/badge/Python-3.11-3776AB">
  <img alt="CLI mem" src="https://img.shields.io/badge/CLI-mem-111827">
  <img alt="Tests" src="https://img.shields.io/badge/tests-338%20passed-16A34A">
  <img alt="Local first" src="https://img.shields.io/badge/local--first-Markdown%20%2B%20SQLite-0F766E">
</p>

## 这是什么

BrainMem 是一个本地优先的个人长期记忆系统，用来记录一个人的生活、工作、学习、项目、关系、决策和对话。它既服务于希望拥有私有记忆库的人，也服务于需要可靠个人上下文的编程智能体。

它的核心思想是 **memory compiler**：把原始笔记、导入文件、聊天记录和事件日志，编译成 Obsidian 友好的 Markdown wiki 与 SQLite 索引。系统保留来源、时间线、review 队列和结构化事实，而不是把记忆静默写进一个看不见的向量库。

默认是 local-first：你的真实知识库目录保留在本机。只有在执行抽取、改写、解释等需要模型的命令时，才会调用外部 LLM。

Phase 2 增加了 hybrid retrieval、sqlite-vec embedding、Markdown/Text/PDF/JSONL 批量导入、import 进度追踪、成本估算和状态观测。

## 独特之处

多数 AI memory 项目优先解决应用集成：写入一条消息、搜索 memory、注入上下文。BrainMem 优先解决的是 **个人记忆所有权**：长期记忆应该可读、可改、可重建、可审计，几年后仍然能知道一条记忆从哪里来、什么时候变成事实。

不同于 Mem0 式的逐消息 memory extraction，也不同于 Letta 式由智能体自主管理上下文，BrainMem 把记忆视为一个由用户和智能体共同维护的长期个人 wiki。

关键设计：

- **面向人生尺度**：可以长期记录笔记、阅读、聊天、项目历史、决策、观察、偏好和个人知识。
- **本地 source of truth**：Markdown 页面、SQLite、append-only JSONL 都留在你的目录里。
- **人参与记忆写入**：冲突、低置信事实、新实体、tier 变化可以进入 review 队列，不静默污染长期记忆。
- **默认保留 provenance**：页面有 timeline 和 sources，派生索引可以从源文件重建。
- **不是纯向量召回**：`mem ask` 组合 vector、BM25 keyword、SQL/entity match，再用 RRF 融合。
- **智能体可用，但记忆不归智能体所有**：智能体可以读写流程，但长期事实由用户可审计地控制。

## 适用范围

BrainMem 适合：

- 建一个私有的人生日志和长期个人知识库；
- 给编程智能体稳定提供你的项目、偏好、决策和历史上下文；
- 导入旧笔记、Markdown vault、PDF、文本文件和 AI 聊天导出；
- 为研究、写作、工程实践、个人运营保留可追溯记忆链；
- 偏好文件和 SQLite，而不是黑盒托管 memory 平台的用户。

它不试图成为多用户 SaaS memory backend、托管聊天机器人平台、图数据库服务，或完全自治的自编辑智能体运行时。

## 核心能力

| 模块 | 能力 |
| --- | --- |
| 知识库 | 带 frontmatter、compiled truth、timeline、sources 的 Markdown 页面 |
| 运行状态 | SQLite 管理实体、事实、反链、review、lint、tier proposal |
| 事件账本 | JSONL append-only event ledger，支持 cursor ingest |
| Hybrid retrieval | `mem ask` 默认用 vector + keyword + SQL 召回并 RRF 融合，保留 keyword-only fallback |
| 批量导入 | `mem import` 支持 `.md`、`.txt`、`.pdf`、`.jsonl`，并记录可恢复 job |
| CLI 工作流 | `init`、`capture`、`ingest`、`reindex`、`import`、`cost-estimate`、`ask`、`review`、`lint`、`rebuild`、`status`、`promote-chat`、`entity` |
| 模型接入 | LLM 默认 DeepSeek V4；embedding 默认 OpenAI 或 OpenAI-compatible；Anthropic 仍可配置 |
| 隐私边界 | keyword-only `mem ask` 是本地的；默认 hybrid `mem ask` 会把 query 发给 embedding provider 生成向量；`mem reindex` 调用 embedding provider；`mem ingest` 和 `mem ask --explain` 可能调用外部模型 |

## 快速开始

需要 Python 3.11。

```powershell
git clone https://github.com/zihanshen29/brainmem.git
cd brainmem

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

$env:OPENAI_API_KEY = "<your-openai-or-compatible-embedding-key>"
mem --version
mem init --root .\brain-root
Set-Location .\brain-root
mem status
```

捕获、ingest、reindex，然后提问：

```powershell
"Remember to review the Phase 1 closeout notes." | mem capture --stdin
mem ingest --source laundry
mem reindex
mem ask "What should I review?"
```

启用模型能力：

```powershell
$env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
mem ask "What changed recently?" --explain
```

批量导入已有资料：

```powershell
mem cost-estimate .\notes --kind md,txt,pdf,jsonl
mem import .\notes --kind md,txt,pdf,jsonl --yes
mem ingest --source laundry
mem reindex
```

`mem init` 默认写入 DeepSeek LLM 配置和 OpenAI-compatible embedding 配置。高级用户可以在 `config.toml` 里混搭 provider：`[deepseek]` 用于 DeepSeek 或兼容 chat API，`[openai]` 用于 OpenAI，`[anthropic]` 用于 Anthropic，`[embedding]` 用于 OpenAI 或其他 OpenAI-compatible embedding endpoint。配置文件只保存环境变量名，真实 key 放在环境变量里。

## 数据结构

```text
brainmem/
  src/brain/           Python 包
  docs/                规格和设计文档
  tests/               测试
  pyproject.toml       打包和依赖配置

brain-root/            本地运行数据，不要发布到 GitHub
  raw/                 原始输入
  laundry/             待 ingest 材料
  pages/               Markdown wiki 页面
  review/              待确认 review 队列
  brain.db             SQLite 运行状态和索引
  events.jsonl         append-only 事件日志
```

## 隐私说明

不要提交真实记忆数据：

- `brain-root/`
- `brain.db`、`brain.db-wal`、`brain.db-shm`
- `events.jsonl`
- `raw/`、`laundry/`、`pages/`、`review/`
- `.env` 或真实 API key

本地安全命令：

- `mem status`
- `mem ask "query" --mode keyword-only`
- `mem cost-estimate`
- `mem rebuild --backlinks --index`
- `mem lint --all`

可能把内容发送给配置的 LLM 或 embedding provider 的命令：

- `mem reindex`
- 默认 hybrid 模式的 `mem ask "query"`，因为 query 会发送给 embedding provider 生成向量
- `mem ingest`
- `mem ask --explain`
- `mem promote-chat`
- 会重写 compiled truth 的 review apply
- 强制页面重建

## 验证

当前已实现的 Phase 2 构建用下面命令验证：

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
```

最近本地结果：`338 passed`，`ruff` passed。

## 规格文档

`docs/` 根目录里的文件是当前产品与设计文档：

- [SPEC.md](docs/SPEC.md)
- [architecture.md](docs/architecture.md)
- [data-model.md](docs/data-model.md)
- [pipeline.md](docs/pipeline.md)
- [cli.md](docs/cli.md)
- [tech-stack.md](docs/tech-stack.md)

Phase 1 和 Phase 2 的历史实施说明归档在 [docs/archive/](docs/archive/)。
