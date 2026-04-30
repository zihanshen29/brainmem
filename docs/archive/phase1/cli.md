# CLI Reference

CLI 入口名为 `mem`。所有命令支持 `--help`。全局选项 `--brain-root` 可覆盖 `~/brain` 路径（用于测试）。

## 命令总览

```
mem init                       # 初始化空仓库
mem ingest                     # 处理 laundry + 新事件
mem review [<id>] [--apply]    # 处理 review 队列
mem lint [--<kind>|--all]      # 跑 lint 检查
mem ask <query> [--explain]    # 查询
mem promote-chat <event-id>    # 提升 AI 对话为页面
mem rebuild --<scope>          # 重建派生数据
mem status                     # 仓库状态
mem entity merge <a> <b>       # 合并实体
mem capture [<kind>]           # 快速记录入口
```

## 详细规范

### `mem init`

```
mem init [--root <path>] [--force]
```

- `--root` —— 创建在指定路径，默认 `~/brain`
- `--force` —— 已存在也强制初始化（会覆盖，需要 `--force` 才允许）

行为见 `pipeline.md` 第 1 节。

### `mem ingest`

```
mem ingest [--dry-run] [--source <laundry|events|all>] [--limit N]
```

- `--dry-run` —— 不实际写入，只打印将做的事
- `--source` —— 限定处理来源，默认 `all`
- `--limit` —— 最多处理 N 个项目（调试用）

退出码：
- 0 —— 成功
- 1 —— 部分失败（已写 partial），错误见 `errors.log`
- 2 —— 完全失败（全部回滚）

### `mem review`

```
mem review                        # 列出 pending
mem review <review-id>            # 用 $EDITOR 打开
mem review --apply                # 扫所有 pending 应用决定
mem review --kind <kind>          # 只列出某类
```

`<review-id>` 可以是完整 id 或唯一前缀。

环境变量 `$EDITOR` 决定编辑器。Windows 默认 `notepad`，Linux/Mac 默认 `vi`。

### `mem lint`

```
mem lint --all
mem lint --contradictions
mem lint --stale [--days N]
mem lint --orphans
mem lint --citations
```

每条 lint 输出一个 review item，并在控制台打印总结。

### `mem ask`

```
mem ask "<query>" [--explain] [--sql] [--type <type>] [--top N]
```

- `--explain` —— LLM 生成自然语言答案
- `--sql` —— 显示生成的 SQL 查询（debug）
- `--type` —— 限定 page type
- `--top` —— 返回前 N 条，默认 5

输出格式（默认）：

```
1. [project] cv-coursework — 计算机视觉作业
   Compiled truth (前 200 字)...
   最近: 2026-04-20: 决定先完成 baseline 报告...
   Score: 4.32

2. [concept] computer-vision-fundamentals — 计算机视觉基础
   ...
```

`--explain` 模式额外输出一段 LLM 综合的回答，结尾标注引用来源。

### `mem promote-chat`

```
mem promote-chat <event-id> [--title <title>] [--slug <slug>]
```

- `<event-id>` —— ULID 或唯一前缀
- `--title` —— 自定义页面 title，默认让 LLM 生成
- `--slug` —— 自定义 slug，默认从 title 派生

如果该 event 已经被 promote 过 → 报错退出。

### `mem rebuild`

```
mem rebuild --db
mem rebuild --pages <slug> [--force]
mem rebuild --backlinks
mem rebuild --index
```

`--pages` 默认要求 `--force` 因为会覆盖。其他默认安全。

### `mem status`

```
mem status [--json]
```

输出（默认人读格式）：

```
Brain root: /home/zihan/brain  (47.3 MB, 142 commits)

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

Facts:        87 active, 12 superseded
Events:       234 in ledger
Review queue: 3 pending
Last ingest:  2026-04-27 22:14:33 (UTC)
```

`--json` 输出机器可读 JSON。

### `mem entity merge`

```
mem entity merge <slug-a> <slug-b> [--into a|b]
```

合并到第一个参数（默认）。`--into b` 反向。

操作前打印将要合并的内容并要求用户确认（除非 `--yes`）。

### `mem capture`

快速记录入口，把内容写到 `laundry/`。

```
mem capture                       # 打开 $EDITOR 写新条目
mem capture --stdin               # 从 stdin 读
mem capture --file <path>         # 从指定文件读
mem capture chat                  # 标记为 human_chat 类型，引导填参与者
mem capture idea                  # 标记为 idea
mem capture meeting               # 标记为会议纪要
```

每次 capture 在 laundry/ 写一个 `<YYYY-MM-DD-HHMMSS>_<slug>.md` 文件，frontmatter 包含 capture 时间和 kind。

## 全局选项

适用于所有命令：

- `--brain-root <path>` —— 覆盖 brain 根目录
- `--config <path>` —— 覆盖 config 文件路径
- `--verbose, -v` —— 详细日志
- `--quiet, -q` —— 只输出错误
- `--no-color` —— 禁用 ANSI 颜色（Windows cmd 默认 detect）

## 退出码

- 0 —— 成功
- 1 —— 业务错误（用户输入错、文件冲突、决策被拒）
- 2 —— 系统错误（DB 损坏、API 失败、磁盘满）
- 130 —— 用户中断 (Ctrl-C)

## 命令行 UX 约定

- 所有破坏性操作（rebuild、entity merge、init --force）默认要求确认，除非 `--yes`
- 长操作显示进度条（用 `rich.progress`）
- 错误信息分两段：第一行简短人话，后面带 traceback（仅 `--verbose`）
- 颜色：green=成功，yellow=警告，red=错误，dim=次要信息

## 帮助文本风格

每个命令的 `--help` 包含：

1. 一行总结
2. 一段说明（这条命令做什么、典型场景）
3. 选项列表
4. 1–2 个示例

示例（`mem ingest --help`）：

```
Usage: mem ingest [OPTIONS]

  Process new content in laundry/ and unprocessed events in events.jsonl.

  Extracts facts via LLM, resolves entities, detects conflicts. New facts
  with high confidence are auto-committed; mid-confidence and conflicts
  go to the review queue. Pages are auto-created or appended to.

Options:
  --dry-run              Show what would be done without writing
  --source [laundry|events|all]
                         Limit to one source (default: all)
  --limit INTEGER        Process at most N items
  --verbose / --quiet
  --help                 Show this message and exit.

Examples:
  mem ingest                     # full ingest
  mem ingest --dry-run           # preview
  mem ingest --source laundry    # only process laundry/
```
