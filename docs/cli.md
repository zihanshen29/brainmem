# CLI Reference

CLI 入口名为 `mem`。所有命令支持 `--help`。全局选项 `--brain-root` 可覆盖 `~/brain` 路径。Phase 2 新增的命令和选项用 **(P2)** 标注。

## 命令总览

```
# Phase 1
mem init                       # 初始化空仓库
mem ingest                     # 处理 laundry + 新事件
mem review [<id>] [--brain-root <root>] [--apply] [--kind <kind>]  # 处理 review 队列
mem lint [--<kind>|--all]      # 执行 lint 检查
mem promote-chat <event-id>    # 提升 AI 对话为页面
mem rebuild --<scope>          # 重建派生数据
mem status                     # 仓库状态
mem entity merge <a> <b>       # 合并实体
mem capture [<kind>]           # 快速记录入口

# Phase 2 (新增)
mem ask <query>                # 默认变成 hybrid retrieval
mem inject --query <query>     # 生成 token-bounded prompt context
mem scratch append             # 追加本地 working buffer
mem snapshot rebuild           # 从 scratch 重建当前状态快照
mem procedure <slug>           # 手动维护 procedure 页面
mem reindex                    # 构建/更新 embedding 索引
mem import <path>              # bulk import
mem cost-estimate <path>       # 估算 import 成本
```

## (P2) `mem ask` (改动: 默认使用 hybrid)

```
mem ask "<query>" [--mode hybrid|keyword-only|semantic|sql] [--top N] [--type <type>]
                  [--debug] [--explain]
```

### 行为变化

- **Phase 1 默认**: 关键词 + SQL + backlink 加权
- **Phase 2 默认**: vector + keyword + SQL → RRF 融合

### 选项

- `--mode hybrid` (默认) — 三路 RRF 融合
- `--mode keyword-only` — Phase 1 行为，仅 keyword + SQL backlink
- `--mode semantic` — 仅 vector path
- `--mode sql` — 强制使用 SQL 短路（结构化查询）
- `--top N` — 返回前 N 条，默认 5
- `--type <project|concept|procedure|...>` — 限定 page type
- `--debug` — 显示三路各自 top-10 + RRF 融合详情
- `--explain` — LLM 综合答案（保留 Phase 1 行为）

---

## `mem inject`

生成可直接放进 LLM prompt 的上下文包。默认使用 `--mode keyword-only`，因此不会调用 embedding provider；如果显式选择 `hybrid` 或 `semantic`，就按 `mem ask` 的 provider 边界处理。

```
mem inject --query "<query>" [--budget N] [--format markdown|text]
           [--mode keyword-only|hybrid|semantic|sql] [--top N]
           [--type <page-type>] [--include-slug <slug> ...]
           [--snapshot|--no-snapshot]
```

- `--budget N` — 输出 token 预算，默认 10000。
- `--format markdown|text` — 输出格式，默认 markdown。
- `--snapshot/--no-snapshot` — 是否先注入 `scratch/SNAPSHOT.md`，默认开启。

`mem inject` 适合 agent 把检索结果继续交给另一个模型时使用；普通面向人的查询仍用 `mem ask`。

---

## `mem scratch`

本地-only working buffer，用来记录当前会话进展，不会把内容提升为 wiki truth，也不会调用外部 provider。

```
mem scratch append "<text>" [--source <source>]
mem scratch append --stdin [--source <source>]
```

---

## `mem snapshot`

从 scratch working buffer 重建 `scratch/SNAPSHOT.md`。当前实现是确定性的本地摘要，不调用 LLM。

```
mem snapshot rebuild [--max-items N] [--max-chars N] [--strategy dedup|recent]
```

`mem snapshot rebuild` defaults to `--strategy dedup`, which keeps the latest
scratch entry per source and records how many earlier entries were collapsed.
Use `--strategy recent` to keep the previous newest-N log behavior. `mem inject`
默认会读取 snapshot，因此涉及“当前状态”的 agent 查询应先运行 `mem snapshot rebuild`。

---

## `mem procedure`

手动维护可复用流程页面。Procedure 有 `raw`、`tested`、`stable` 三个成熟度状态；`run` 会记录成功/失败次数，并按 `config.toml` 的 `[procedure]` 阈值自动升级或降级。

```
mem procedure new <slug> --title "<title>"
mem procedure run <slug> --result success|fail --note "<note>"
mem procedure promote <slug> --status raw|tested|stable
mem ask "<query>" --type procedure
```

Procedure 页面会进入普通 page 路径：`mem status` 的 `pages_by_type` 会统计 `procedure`，`mem ask --type procedure` 可过滤检索，`mem reindex --dry-run` 会按 compiled truth 和 timeline 估算 chunks。

默认成熟度阈值：

```toml
[procedure]
stable_success_threshold = 5
stable_fail_threshold = 2
```

---

## (P2) `mem ask` 自动降级

如果 `embedding_index` 是空的（用户尚未运行 reindex），`mem ask` 自动降级到 `--mode keyword-only` 并提示：

```
⚠ No embeddings found. Falling back to keyword-only mode.
  Run `mem reindex` to enable hybrid retrieval.
```

如果 sqlite-vec 扩展加载失败，同样降级。

### 输出示例（默认模式）

```
Query: 我跟小张最近讨论过什么
Mode: hybrid (3 paths fused via RRF)

1. [conversation] 2026-04-29-lunch-with-xiaozhang
   小张计划下个月跳槽到字节做推荐算法...
   Latest: 2026-04-29: 系统设计建议...
   RRF score: 0.058 [v1, k1, s2]

2. [entity] xiaozhang
   计划跳槽到字节, 做推荐算法...
   Latest: 2026-04-29: 给了 system design 建议
   RRF score: 0.043 [v3, k_, s1]

...
```

`[v1, k1, s2]` 表示这条结果在 vector path 里排第 1、keyword path 里排第 1、SQL path 里排第 2。`_` 表示该 path 没命中。

---

## (P2) `mem reindex`

构建/更新 embedding 索引。

```
mem reindex                       # 增量, 只为变化或新增的 chunk 生成 embedding
mem reindex --force               # 全量重新生成 embedding (用于换 model)
mem reindex --pages <slug>        # 只重新生成一个 page 的 embedding
mem reindex --dry-run             # 显示会处理的 chunk 数 + 预估 token, 不写入
mem reindex --commit              # 完成后自动 git commit；默认不 commit
```

### 输出示例

```
$ mem reindex
Scanning 35 pages...
Chunks: 142 total, 12 new, 5 changed, 125 unchanged.

Embedding 17 chunks via openai_compatible/text-embedding-3-small...
[████████████████████] 17/17 (100%)

Reindex complete:
  Chunks added:    12
  Chunks updated:  5
  Chunks removed:  3
  Unchanged:       125
  Tokens used:     3,421
  Cost:            ~$0.00007
  Duration:        4.2s
```

### --dry-run 输出

```
$ mem reindex --dry-run
Would embed 17 chunks (~3,400 tokens, ~$0.00007).
Would delete 3 orphan chunks.
Run without --dry-run to execute.
```

---

## (P2) `mem import`

批量导入目录素材。

```
mem import <path> [--kind md,txt,pdf,jsonl] [--dry-run]
                  [--yes] [--batch-size N]
mem import --resume                       # 继续上次中断
mem import --status [<job-id>]            # 查看 job 状态；可选 job id 通过 PATH 参数传入
mem import --abort <job-id>               # 中止 job
mem import --list-jobs                    # 列出所有 import job
```

### 选项

- `<path>` — 要 import 的目录（递归扫描）
- `--kind <list>` — 限定文件类型，逗号分隔，默认 `md,txt,pdf,jsonl`
- `--dry-run` — 只做成本估算，不写入
- `--yes` — 跳过成本确认提示
- `--batch-size N` — 一批后 commit 一次，默认 50
- `--resume` — 继续 paused/running 的 job
- `--status [job-id]` — 不带 id 显示最近一个 job；带 id 时写成 `mem import <job-id> --status`
- `--abort <job-id>` — 把指定 job 标记 failed
- `--list-jobs` — 列出最近 job

### 输出示例

```
$ mem import ~/Documents/obsidian-vault

Discovering files...
Found 234 files:
  .md     198
  .txt    12
  .pdf    24

Cost estimate:
  Extraction (DeepSeek):  ~488,000 tokens   ~$0.34
  Embedding:              ~234,000 tokens   ~$0.005
  Total estimate:                            ~$0.35

Continue? [y/N]: y

Job 01HZA... started.

Importing batch 1/5...
[████████░░░░░░░░░░░] 50/234 (21%)  ETA: 2m 18s
...

Job 01HZA... completed:
  Files processed: 232
  Files failed:    2 (see `mem import <job-id> --status` for file errors)
  Laundry items:   245 (some files split into multiple docs)

Next steps:
  Run `mem ingest` to process the 245 laundry items.
  Ingest auto-reindex is controlled by config `[import].auto_reindex`.
```

### --status 输出

```
$ mem import --status

Import job:
job_id=01HZA... status=running progress=156/234 failed=0 estimated_usd=$0.3500 source=~/Documents/vault
Files:
- extracted md notes/a.md
- pending pdf papers/b.pdf
```

---

## (P2) `mem cost-estimate`

只估算 import 成本，不写入任何内容。等价于 `mem import <path> --dry-run` 的成本估算部分。

```
mem cost-estimate <path>                  # 预估 import
mem cost-estimate <path> --kind md,pdf    # 限定文件类型
```

输出格式包含文件数、kind 分布、extraction/embedding token 估算和总美元估算。

---

## (P2) `mem status` 增强

输出多了几行：

```
$ mem status

Brain root: ~/brain  (47.3 MB, 142 commits)

Pages:
  entities      8
  projects      3
  concepts      12
  events        2
  experiences   4
  conversations 6
  total         35

Entities:
  Tier 1        5
  Tier 2        9
  Tier 3        14
  total         28

Facts:                87 active, 12 superseded
Events:               234 in ledger
Laundry pending/failed: 3/2
Pending reviews:      3
Pending reviews by kind: fact_conflict=1, ingest_error=2
Scratch working:      present (updated 2026-04-30T22:10:00+00:00)
Scratch snapshot:     missing
Last ingest:          2026-04-30 22:14:33 (UTC)

# === (P2) ===
Embedding coverage:   87% (134/154 chunks indexed)
Last reindex:         2026-04-30 14:23:11 (UTC)
Active import jobs:   0
Token usage:          extraction 1.12M (~$3.21), embedding 84K (~$0.002)
Total cost:           $3.210000
```

Laundry 和 scratch 的健康信息只统计文件数量、是否存在和更新时间；status
不会读取或输出这些文件的正文。Review 分类只读取有界的 frontmatter，正文不会进入
status 输出。

---

## (P2) `mem ingest` 微调

新增选项：

```
mem ingest [--no-auto-reindex] ...
mem ingest --dry-run [--source laundry|events|all] [--limit N]
mem ingest --requeue-failed [--limit N]
```

默认 `auto_reindex` 受 config 控制。该 flag 可临时关闭自动 reindex。

`--dry-run` 只在本地枚举本次会处理的队列项，不调用 LLM/provider、不要求 API key，
也不会写文件、数据库、事件、cursor 或 Git commit。

`--requeue-failed` 是显式的本地恢复操作：它把 `laundry/failed/` 中的文件移回
待处理的 `laundry/`，但不会自动再次 ingest。若待处理区已有同名文件，会生成带编号的
新文件名，绝不覆盖已有内容。确认恢复结果后，再显式运行 `mem ingest`。

---

## 常见选项（无变化）

多数会读取 brain root 的命令支持：

- `--brain-root <path>`
- `--quiet, -q`

## 退出码（无变化）

- 0 — 成功
- 1 — 业务错误
- 2 — 系统错误
- 130 — 用户中断 (Ctrl-C)

(P2) 新增情况：

- `mem import` 被 Ctrl-C 退出 130 时，job 状态置 `paused`，可 `--resume`
- `mem ask` 降级到 keyword-only 时仍返回 0，但 stderr warn

## 命令行 UX 约定（无变化）

- 破坏性操作要 `--yes` 跳过确认
- import 长操作显示进度条 (`rich.progress`)
- 成本确认使用 `[y/N]`，N 是默认值

## 帮助文本风格（示例：`mem import --help`）

```
Usage: mem import [OPTIONS] [PATH]

  Bulk import files from a directory into the brain.

  Recursively scans PATH for supported file types (markdown, text, PDF, JSONL),
  extracts content, and writes them as laundry items for later ingest. Shows a
  cost estimate before proceeding unless --yes is set.

Arguments:
  PATH  File or directory to scan, or a job id when used with --status

Options:
  --brain-root PATH       Brain repository root.
  --kind TEXT             Comma-separated file kinds: md,txt,pdf,jsonl.
  --dry-run               Show estimate only, do not write
  --yes                   Skip cost confirmation
  --batch-size INTEGER    Commit every N files. Default: 50
  --resume                Continue an unfinished import job
  --status                Show import job status; optional job id is PATH
  --abort JOB_ID          Mark a job as failed
  --list-jobs             List all import jobs
  --help                  Show this message and exit

Examples:
  mem import ~/Documents/notes                    # full import
  mem import ~/notes --kind md --dry-run          # md only, estimate
  mem import --resume                             # continue unfinished
  mem import --status                             # see active jobs
  mem import 01HZA... --status                    # see one job
```
