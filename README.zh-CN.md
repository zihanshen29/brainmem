# BrainMem

<p align="center">
  <strong>面向智能体的本地优先记忆系统，基于 Markdown 和 SQLite。</strong>
</p>

<p align="center">
  <a href="./README.md">English</a> · <a href="./README.zh-CN.md">中文</a>
</p>

<p align="center">
  <img alt="Python 3.11" src="https://img.shields.io/badge/Python-3.11-3776AB">
  <img alt="CLI mem" src="https://img.shields.io/badge/CLI-mem-111827">
  <img alt="Tests" src="https://img.shields.io/badge/tests-207%20passed-16A34A">
  <img alt="Local first" src="https://img.shields.io/badge/local--first-Markdown%20%2B%20SQLite-0F766E">
</p>

## 这是什么

BrainMem 是一个给编程智能体和高频知识工作流使用的个人记忆系统。它把长期知识存成 Obsidian 友好的 Markdown wiki，把确定性索引、事实、实体、review 和 lint 结果存进 SQLite，并通过 `mem` 命令行使用。

默认是 local-first：你的真实知识库目录保留在本机。只有在执行抽取、改写、解释等需要模型的命令时，才会调用外部 LLM。

## 核心能力

| 模块 | 能力 |
| --- | --- |
| 知识库 | 带 frontmatter、compiled truth、timeline、sources 的 Markdown 页面 |
| 运行状态 | SQLite 管理实体、事实、反链、review、lint、tier proposal |
| 事件账本 | JSONL append-only event ledger，支持 cursor ingest |
| CLI 工作流 | `init`、`capture`、`ingest`、`review`、`lint`、`ask`、`rebuild`、`status`、`promote-chat`、`entity` |
| 模型接入 | 默认 DeepSeek V4，也保留 OpenAI / Anthropic 配置兼容 |
| 隐私边界 | 普通 `mem ask` 是本地检索；`mem ingest` 和 `mem ask --explain` 可能调用外部模型 |

## 快速开始

需要 Python 3.11。

```powershell
git clone https://github.com/zihanshen29/brainmem.git
cd brainmem

py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[dev]"

mem --version
mem init --root .\brain-root
Set-Location .\brain-root
mem status
```

捕获一条笔记：

```powershell
"Remember to review the Phase 1 closeout notes." | mem capture --stdin
```

本地检索：

```powershell
mem ask "What should I review?"
```

启用模型能力：

```powershell
$env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
mem ingest --source laundry
mem ask "What changed recently?" --explain
```

## 数据结构

```text
brainmem/
  src/brain/           Python 包
  files/               Phase 1 规格和设计文档
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
- 不带 `--explain` 的 `mem ask "query"`
- `mem rebuild --backlinks --index`
- `mem lint --all`

可能把内容发送给配置模型的命令：

- `mem ingest`
- `mem ask --explain`
- `mem promote-chat`
- 会重写 compiled truth 的 review apply
- 强制页面重建

## 验证

当前 Phase 1 构建已通过：

```powershell
.\.venv\Scripts\pytest.exe
.\.venv\Scripts\ruff.exe check .
```

最近本地结果：`207 passed`，`ruff` passed。

## 规格文档

- [SPEC.md](files/SPEC.md)
- [architecture.md](files/architecture.md)
- [data-model.md](files/data-model.md)
- [pipeline.md](files/pipeline.md)
- [cli.md](files/cli.md)
- [tech-stack.md](files/tech-stack.md)
- [phase-1-tasks.md](files/phase-1-tasks.md)
