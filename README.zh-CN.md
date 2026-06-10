# BrainMem

<p align="center">
  <strong>面向人和智能体的本地优先长期记忆系统。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-CN.md">中文</a>
</p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/Python-3.11%2B-3776AB">
  <img alt="CLI mem" src="https://img.shields.io/badge/CLI-mem-111827">
  <img alt="Tests" src="https://img.shields.io/badge/tests-438%20passed-16A34A">
  <img alt="Local first" src="https://img.shields.io/badge/local--first-Markdown%20%2B%20SQLite-0F766E">
</p>

## 这是什么

BrainMem 是一个本地优先的个人长期记忆系统，用来把生活、工作、学习、项目、关系、决策和对话沉淀成可读、可审计、可重建的知识库。它适合希望掌握自己记忆数据的人，也适合需要稳定个人上下文的编码智能体。

核心思路是 **memory compiler**：把原始笔记、导入文件、聊天记录和事件日志编译成 Obsidian 友好的 Markdown wiki 与 SQLite 索引。系统保留来源、时间线、review 队列和结构化事实，而不是把记忆静默写进一个看不见的向量库。

BrainMem 默认本地优先：真实数据根目录留在你的机器上。只有执行抽取、改写、embedding 或解释等需要模型的命令时，才会调用外部 provider。

Phase 2 增加了 hybrid retrieval、sqlite-vec embedding、Markdown/Text/PDF/JSONL 批量导入、导入进度、成本估算、状态观测，以及面向 agent runtime 的 token-bounded 注入、scratch、snapshot 和 procedure 工作流。

## 设计取舍

- **面向长期个人记忆**：记录项目历史、偏好、阅读、对话、决策和观察，而不是只保存零散 message memory。
- **本地 source of truth**：Markdown、SQLite 和 append-only JSONL 都在你自己的目录里。
- **人参与的记忆写入**：冲突、低置信事实、新实体和 tier 变化可以进入 review 队列。
- **默认保留 provenance**：页面包含 timeline 和 sources，派生索引可以从源文件重建。
- **混合检索**：`mem ask` 结合 vector、BM25 keyword、SQL/entity matching 与 RRF。
- **可选远程 MCP 访问**：可信多设备场景可启用 HTTP/SSE transport；本地 stdio 仍是默认模式。
- **智能体可用，但不拥有记忆**：agent 可以调用 BrainMem，但长期事实仍由用户可审计地控制。

## 核心能力

| 模块 | 能力 |
| --- | --- |
| 知识库 | 带 frontmatter、compiled truth、timeline、sources 的 Markdown 页面，并支持 procedure 页面 |
| 运行状态 | SQLite 管理实体、事实、反链、review、lint、tier proposal，以及本地 scratch/snapshot 上下文 |
| 事件账本 | append-only JSONL event ledger，支持 cursor ingest |
| 混合检索 | `mem ask` 使用 vector + keyword + SQL + RRF，并保留 keyword-only 本地模式 |
| Agent 上下文 | `mem inject` 生成 token-bounded prompt context，默认可包含 `scratch/SNAPSHOT.md` |
| 工作缓冲 | `mem scratch append` 记录当前会话进展，不直接写入 wiki truth |
| 当前快照 | `mem snapshot rebuild` 从 scratch 生成本地 deterministic snapshot |
| 可复用流程 | `mem procedure new/run/promote` 维护 raw/tested/stable 状态的 SOP |
| 批量导入 | `mem import` 支持 `.md`、`.txt`、`.pdf`、`.jsonl` |
| MCP 访问 | 默认使用 stdio；可信远程 MCP 客户端可选启用 HTTP/SSE transport |
| 隐私边界 | `mem ask --mode keyword-only`、`mem inject --mode keyword-only`、scratch、snapshot、procedure 是本地命令；默认 hybrid ask、reindex、ingest、explain 可能调用 provider |

## 快速开始

需要 Python 3.11 或更新版本。

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

记录、ingest、reindex 并查询：

```powershell
"Remember to review the Phase 1 closeout notes." | mem capture --stdin
mem ingest --source laundry
mem reindex
mem ask "What should I review?"
```

为 agent prompt 准备本地上下文：

```powershell
"source_agent: codex`nChecked the deploy checklist." | mem scratch append --stdin --source codex
mem snapshot rebuild
mem inject --query "deploy checklist" --mode keyword-only --budget 4000
```

启用 LLM-backed 工作流：

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

`mem init` 默认写入 DeepSeek LLM 配置和 OpenAI-compatible embedding 配置。真实 API key 放在环境变量里，`config.toml` 只保存环境变量名。

## 多设备访问

BrainMem 的本地 MCP 默认仍使用 stdio transport。可信多设备场景可以用
`mem-mcp-http` 把同一组 MCP 工具通过 HTTP/SSE 暴露给远程客户端。服务端启动时固定
`brain_root`，远程客户端不能覆盖；provider-backed 命令仍遵守与本地 CLI 相同的显式许可规则。

推荐拓扑、共享 token 鉴权、远程工具白名单和已知限制见
[docs/multi-device.md](docs/multi-device.md)。

## 数据结构

```text
brainmem/
  src/brain/           Python package
  docs/                规格与设计文档
  tests/               测试
  pyproject.toml       打包与依赖配置

brain-root/            本地运行数据，不要发布到 GitHub
  raw/                 原始输入
  laundry/             待 ingest 材料
  pages/               Markdown wiki 页面
  review/              待确认 review 队列
  scratch/             working buffer 与 SNAPSHOT.md
  brain.db             SQLite 运行状态和索引
  events.jsonl         append-only 事件日志
```

## 隐私说明

不要提交真实记忆数据：

- `brain-root/`
- `brain.db`、`brain.db-wal`、`brain.db-shm`
- `events.jsonl`
- `raw/`、`laundry/`、`pages/`、`review/`、`scratch/`
- `.env` 或真实 API key
- `AGENTS.local.md`、`skills/*/*.local.md`

本地命令：

- `mem status`
- `mem ask "query" --mode keyword-only`
- `mem inject --query "query" --mode keyword-only`
- `mem scratch append`
- `mem snapshot rebuild`
- `mem procedure new`、`mem procedure run`、`mem procedure promote`
- `mem cost-estimate`
- `mem rebuild --backlinks --index`
- `mem lint --all`

可能把内容发送给配置的 LLM 或 embedding provider 的命令：

- `mem reindex`
- 默认 hybrid 模式的 `mem ask "query"`
- `mem ingest`
- `mem ask --explain`
- `mem promote-chat`
- 会重写 compiled truth 的 review apply 流程
- 强制页面重建中使用 provider 的路径

## 验证

当前构建用下面的命令验证：

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
```

最新本地结果：`438 passed`，`ruff` passed。

## 文档

当前产品和设计文档在 `docs/` 根目录：

- [SPEC.md](docs/SPEC.md)
- [architecture.md](docs/architecture.md)
- [data-model.md](docs/data-model.md)
- [pipeline.md](docs/pipeline.md)
- [cli.md](docs/cli.md)
- [tech-stack.md](docs/tech-stack.md)
- [multi-device.md](docs/multi-device.md)

Phase 1 和 Phase 2 历史实施说明归档在 [docs/archive/](docs/archive/)。
