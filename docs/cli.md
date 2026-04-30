# CLI Reference

CLI 入口名为 `mem`。所有命令支持 `--help`。全局选项 `--brain-root` 可覆盖 `~/brain` 路径。Phase 2 新增的命令和选项用 **(P2)** 标注。

## 命令总览

```
# Phase 1
mem init                       # 初始化空仓库
mem ingest                     # 处理 laundry + 新事件
mem review [<id>] [--apply]    # 处理 review 队列
mem lint [--<kind>|--all]      # 跑 lint 检查
mem promote-chat <event-id>    # 提升 AI 对话为页面
mem rebuild --<scope>          # 重建派生数据
mem status                     # 仓库状态
mem entity merge <a> <b>       # 合并实体
mem capture [<kind>]           # 快速记录入口

# Phase 2 (新增)
mem ask <query>                # 默认变成 hybrid retrieval
mem reindex                    # 构建/更新 embedding 索引
mem import <path>              # bulk import
mem cost-estimate <path>       # 估算 import 成本
```

## (P2) `mem ask` (改动: 默认走 hybrid)

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
- `--mode sql` — 强制走 SQL 短路（结构化查询）
- `--top N` — 返回前 N 条，默认 5
- `--type <project|concept|...>` — 限定 page type
- `--debug` — 显示三路各自 top-10 + RRF 融合详情
- `--explain` — LLM 综合答案（保留 Phase 1 行为）

### 自动降级

如果 `embedding_index` 是空的（用户从未 reindex），`mem ask` 自动降级到 `--mode keyword-only` 并提示：

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
mem reindex                       # 增量, 只 embed 变化或新增的 chunk
mem reindex --force               # 全量重 embed (用于换 model)
mem reindex --pages <slug>        # 只重 embed 一个 page
mem reindex --dry-run             # 显示会处理的 chunk 数 + 预估 token, 不真跑
mem reindex --no-commit           # 跑完不自动 git commit
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

Bulk import 一个目录的素材。

```
mem import <path> [--kind md,txt,pdf,jsonl] [--dry-run] [--then-ingest]
                  [--yes] [--batch-size N]
mem import --resume                       # 继续上次中断
mem import --status [<job-id>]            # 查看 job 状态
mem import --abort <job-id>               # 中止 job
mem import --list-jobs                    # 列出所有 import job
```

### 选项

- `<path>` — 要 import 的目录（递归扫描）
- `--kind <list>` — 限定文件类型，逗号分隔，默认 `md,txt,pdf,jsonl`
- `--dry-run` — 只 cost estimate
- `--then-ingest` — import 后自动跑 ingest
- `--yes` — 跳过 cost confirm prompt
- `--batch-size N` — 一批后 commit 一次，默认 50
- `--resume` — 继续 paused/running 的 job
- `--status [job-id]` — 不带 id 显示所有未完成 job
- `--abort <job-id>` — 把指定 job 标记 failed
- `--list-jobs` — 列出所有 job

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
  Files failed:    2 (see ~/brain/imports/01HZA.../errors.log)
  Laundry items:   245 (some files split into multiple docs)

Next steps:
  Run `mem ingest` to process the 245 laundry items
  Or re-run with `--then-ingest` to chain
```

### --status 输出

```
$ mem import --status

Active import jobs:

01HZA... [running]   ~/Documents/vault     processed: 156/234   ~$0.21 / ~$0.35

Recently finished:

01HYB... [completed] ~/Downloads/papers    processed: 24/24     $0.04
01HXC... [failed]    ~/old-notes           processed: 12/89     errors: 6
```

---

## (P2) `mem cost-estimate`

只算成本，不写任何东西。等价于 `mem import <path> --dry-run` 但不要求 path 一定是要 import 的——也可以预估 reindex 成本：

```
mem cost-estimate import <path>           # 预估 import
mem cost-estimate reindex                 # 预估 reindex 当前所有 page
mem cost-estimate reindex --force         # 预估 reindex --force
```

输出格式同 `--dry-run`。

---

## (P2) `mem status` 增强

输出多了几行：

```
$ mem status

Brain root: C:\Users\zihan\brain  (47.3 MB, 142 commits)

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
Pending reviews:      3
Last ingest:          2026-04-30 22:14:33 (UTC)

# === (P2) ===
Embedding coverage:   87% (134/154 chunks indexed)
Last reindex:         2026-04-30 14:23:11 (UTC)
Active import jobs:   0
Token usage:          extraction 1.12M (~$3.21), embedding 84K (~$0.002)
Total cost so far:    ~$3.21
```

---

## (P2) `mem ingest` 微调

加一个选项:

```
mem ingest [--no-auto-reindex] ...
```

默认 `auto_reindex` 受 config 控制。flag 可以临时关掉。

---

## 全局选项（无变化）

适用于所有命令：

- `--brain-root <path>`
- `--config <path>`
- `--verbose, -v`
- `--quiet, -q`
- `--no-color`

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
- cost confirm 用 `[y/N]`，N 是默认

## 帮助文本风格（示例：`mem import --help`）

```
Usage: mem import [OPTIONS] PATH

  Bulk import files from a directory into the brain.

  Recursively scans PATH for supported file types (markdown, text, PDF, JSONL),
  extracts content, and writes them as laundry items for later ingest. Shows a
  cost estimate before proceeding unless --yes is set.

Arguments:
  PATH  Directory to scan (recursive)

Options:
  --kind TEXT             Comma-separated file kinds. Default: md,txt,pdf,jsonl
  --dry-run               Show estimate only, do not write
  --then-ingest           After import, run mem ingest automatically
  --yes                   Skip cost confirmation prompt
  --batch-size INTEGER    Commit every N files. Default: 50
  --resume                Continue an unfinished import job
  --status [JOB_ID]       Show import job status
  --abort JOB_ID          Mark a job as failed
  --list-jobs             List all import jobs
  --verbose / --quiet
  --help                  Show this message and exit

Examples:
  mem import ~/Documents/notes                    # full import
  mem import ~/notes --kind md --dry-run          # md only, estimate
  mem import ~/notes --then-ingest                # import + ingest
  mem import --resume                             # continue unfinished
  mem import --status                             # see active jobs
```
