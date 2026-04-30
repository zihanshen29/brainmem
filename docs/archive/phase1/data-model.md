# Data Model

本文件定义所有数据结构。Codex 在写代码时必须严格遵循这里的字段名和类型。

## 1. 目录布局

`mem init` 创建以下结构：

```
~/brain/
├── config.toml              # 配置（API key 引用、阈值等）
├── brain.db                 # SQLite 主库
├── events.jsonl             # 事件账本（append-only）
├── CLAUDE.md                # 给 LLM 看的 schema 说明（文件名沿用 Claude/LLM Wiki 习惯，不表示只支持 Anthropic）
├── README.md                # 给用户看的简介
├── .gitignore
├── .gitattributes           # 强制 LF 换行
│
├── raw/                     # 原始素材（不可变）
│   └── <YYYY-MM-DD>_<source>_<slug>.<ext>
│
├── laundry/                 # 待处理素材
│   ├── <slug>.md            # 待 ingest 的杂乱内容
│   └── processed/           # 处理后归档
│       └── <YYYY-MM-DD>_<slug>.md
│
├── pages/                   # L1 wiki
│   ├── index.md             # 总目录（自动维护）
│   ├── log.md               # 全局活动 log（append-only）
│   ├── entities/
│   ├── projects/
│   ├── concepts/
│   ├── events/
│   ├── experiences/
│   └── conversations/
│
└── review/                  # review 队列（markdown 文件）
    ├── <YYYY-MM-DD>_<seq>_<kind>.md
    └── archive/             # 已处理的 review item
```

## 2. config.toml

当前实现支持多 provider。`mem init` 默认只写入 `[deepseek]`，无 `config.toml` 时 LLM client 也默认 DeepSeek。配置文件可以保留一个或多个 provider 段；自动选择优先级是 `deepseek` > `openai` > `anthropic`。API key 只通过环境变量读取，`config.toml` 里只存变量名。

```toml
[deepseek]
api_key_env = "DEEPSEEK_API_KEY"     # 从环境变量读 key，不存文件
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"            # 用于 extract / judgment / rewrite 调用
fast_model = "deepseek-v4-flash"     # 用于轻量任务

# 可选：OpenAI 官方接口。DeepSeek 等 OpenAI-compatible provider 走 [deepseek] + base_url。
[openai]
api_key_env = "OPENAI_API_KEY"
model = "gpt-5.5"
fast_model = "gpt-5.4-mini"

# 可选：Anthropic provider。保留用于 Claude 生态兼容，不是唯一支持路径。
[anthropic]
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-3-5-haiku-latest"
fast_model = "claude-3-5-haiku-latest"

[paths]
brain_root = "~/brain"               # 用户可改

[ingest]
confidence_auto_accept = 0.85        # 高于此值自动入库
confidence_auto_reject = 0.50        # 低于此值丢弃
                                     # 中间区间进 review 队列

[tier]
tier3_threshold = 1                  # 1 次提及 → stub 页面
tier2_threshold = 3                  # 3 次提及 → 提议升级到 Tier 2
tier1_threshold = 8                  # 8 次提及 → 提议升级到 Tier 1

[lint]
stale_days = 90                      # 超过此天数没新事件视为 stale

[git]
auto_commit = true                   # ingest / review 后自动 commit
```

## 3. Event Ledger (events.jsonl)

每行是一个 JSON 对象，遵循以下 Pydantic 模型：

```python
# brain/models/event.py

from datetime import datetime
from typing import Literal, Optional
from pydantic import BaseModel, Field

EventKind = Literal[
    "raw_imported",      # 用户把文件放进 raw/ 后被 ingest 检测到
    "laundry_ingested",  # laundry 里某个素材被处理
    "note_appended",     # 用户直接对某个 page 追加内容
    "ai_chat",           # 一次和 AI 的对话
    "human_chat",        # 一次和真实人的聊天/会议
    "review_decided",    # 用户在 review 队列做了一个决定
    "page_edited",       # 用户手改了某个 page
    "rebuild",           # rebuild 操作记录
]

class Event(BaseModel):
    id: str = Field(..., description="ULID, monotonic & sortable")
    timestamp: datetime
    kind: EventKind
    source_ref: str = Field(..., description="raw 文件路径 / laundry 文件路径 / page 路径 / 'cli'")
    raw_payload: Optional[str] = Field(
        None,
        description="原始文本/JSON。短于 8KB 直接存；长于 8KB 存 raw/ 路径"
    )
    raw_payload_path: Optional[str] = Field(
        None,
        description="若 raw_payload 太长，存到 raw/ 后这里放路径"
    )
    extracted_facts: list[str] = Field(default_factory=list, description="本事件 ingest 时产生的 fact id 列表")
    affected_pages: list[str] = Field(default_factory=list, description="本事件改动的 page slug 列表")
    confidence: float = Field(1.0, ge=0.0, le=1.0, description="事件本身的可信度（不是抽出事实的）")
    metadata: dict = Field(default_factory=dict)
```

字段约束：

- `id` 用 ULID（参见 python-ulid 库）。比 UUID 好，因为带时间戳，可排序。
- `timestamp` 用 UTC ISO8601。
- `raw_payload` 短文本直接存 jsonl，长内容存 raw/ 文件后引用——避免 jsonl 单行过长。
- `extracted_facts` 和 `affected_pages` 在 ingest 时填充，初次写入可为空。

写入方式：**只追加**。永远不要修改已有行。如果一个事件被发现错了，写一个新的 `kind=correction` 事件指向旧 event id，而不是改原行。

## 4. Page Format

每个 page 是一个 markdown 文件，结构严格固定：

```markdown
---
type: project
slug: cv-coursework
title: 计算机视觉作业
tier: 1
created: 2026-03-15T10:00:00Z
updated: 2026-04-20T14:30:00Z
tags: [coursework, image-processing, uk-final-year]
aliases: ["CV 作业", "图像处理 assignment"]
---

# Compiled truth

当前对这个项目的最佳理解，由 LLM 综合 timeline 中的 events 生成。
长度建议 100–500 字。
可被重写。每次重写自动 git commit，历史可追。

下一步、关键决策、未解决问题等放在这里。

---

# Timeline

- 2026-03-15 [event:01HXX...]: 收到作业描述，PDF 存于 raw/2026-03-15_uni_cv-brief.pdf
- 2026-04-01 [event:01HYY...]: 第一次 OpenCV baseline 跑通，准确率 78%
- 2026-04-12 [event:01HZZ...]: 讨论加入 CNN 对照实验
- 2026-04-20 [event:01H00...]: 决定先完成 baseline 报告，CNN 留作扩展

---

# Sources

- raw/2026-03-15_uni_cv-brief.pdf
- conversations/2026-04-12_advisor-meeting.md
- events.jsonl: 01HXX..., 01HYY..., 01HZZ..., 01H00...
```

### Frontmatter 规范

| 字段 | 类型 | 必填 | 说明 |
|------|------|-----|------|
| `type` | enum | 是 | `entity` / `project` / `concept` / `event` / `experience` / `conversation` |
| `slug` | string | 是 | 文件名（不含 .md），全小写，连字符分隔 |
| `title` | string | 是 | 显示名称，可中可英 |
| `tier` | int | entity 必填，其他选填 | 1 / 2 / 3 |
| `created` | datetime | 是 | UTC ISO8601 |
| `updated` | datetime | 是 | UTC ISO8601，每次写入更新 |
| `tags` | list[string] | 否 | 自由 tag |
| `aliases` | list[string] | 否 | 别名，进入 entity registry |
| `external_ids` | dict | 否 | 例如 `{"github": "username"}` |

### Section 规范

四个 section，**严格按顺序**，用三连横杠 `---` 分隔：

1. `# Compiled truth` — 当前最佳理解，可重写
2. `# Timeline` — append-only，每条格式 `- <date> [event:<id>]: <description>`
3. `# Sources` — 自动维护，列出 raw 文件、相关 page、event id

`# Compiled truth` section 第一次创建时如果还没生成，可以是 `(stub — 等待更多证据)`。

### 跨页引用

在 markdown 内容里引用其他页用 `[[<slug>]]` 双方括号语法（Obsidian 兼容）。auto-link 步骤会扫描这些引用建立 backlink 索引。

也可以引用 entity 别名：`[[Zhang San|老张]]` —— 显示"老张"，链接到 `Zhang San` 这个 slug。

## 5. SQLite Schema (brain.db)

完整 DDL：

```sql
-- ==============================================================
-- Entities — 实体注册表
-- ==============================================================
CREATE TABLE entities (
    id              TEXT PRIMARY KEY,           -- canonical slug, e.g. "zhang-san"
    type            TEXT NOT NULL,              -- 'person' | 'org' | 'concept' | 'project' | 'event' | 'place'
    title           TEXT NOT NULL,              -- 显示名
    page_path       TEXT,                       -- pages/ 下的相对路径，stub 阶段可为 NULL
    tier            INTEGER NOT NULL DEFAULT 3, -- 1 / 2 / 3
    mention_count   INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT NOT NULL,              -- ISO8601
    last_seen       TEXT NOT NULL,
    metadata        TEXT,                       -- JSON
    CHECK (tier IN (1, 2, 3))
);

CREATE TABLE entity_aliases (
    alias           TEXT NOT NULL,
    entity_id       TEXT NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
    source          TEXT NOT NULL,              -- 'frontmatter' | 'auto_detected' | 'manual'
    PRIMARY KEY (alias, entity_id)
);

CREATE INDEX idx_aliases_lookup ON entity_aliases(alias);

-- ==============================================================
-- Facts — bi-temporal 结构化事实
-- ==============================================================
CREATE TABLE facts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subject         TEXT NOT NULL,              -- entity_id
    predicate       TEXT NOT NULL,              -- e.g. 'location' | 'works_on' | 'studied_at'
    object          TEXT NOT NULL,              -- 可以是 entity_id 或字面值
    object_type     TEXT NOT NULL,              -- 'entity' | 'literal' | 'date' | 'number'
    valid_from      TEXT,                       -- ISO8601, NULL = 一直有效
    valid_to        TEXT,                       -- ISO8601, NULL = 仍有效
    asserted_at     TEXT NOT NULL,              -- 何时被记录
    source_event    TEXT NOT NULL,              -- event_id
    source_ref      TEXT,                       -- 具体来源（page 路径或 raw 文件）
    confidence      REAL NOT NULL,              -- 0.0–1.0
    superseded_by   INTEGER REFERENCES facts(id),
    CHECK (confidence >= 0.0 AND confidence <= 1.0)
);

CREATE INDEX idx_facts_subject ON facts(subject);
CREATE INDEX idx_facts_predicate ON facts(predicate);
CREATE INDEX idx_facts_active ON facts(subject, predicate)
    WHERE superseded_by IS NULL AND valid_to IS NULL;

-- ==============================================================
-- Backlinks — 类型化链接
-- ==============================================================
CREATE TABLE backlinks (
    from_page       TEXT NOT NULL,              -- page slug
    to_entity       TEXT NOT NULL REFERENCES entities(id),
    relation        TEXT NOT NULL,              -- 'mentions' | 'works_on' | 'attended' | etc.
    line_number     INTEGER,                    -- 大致行号，便于定位
    extracted_at    TEXT NOT NULL,
    PRIMARY KEY (from_page, to_entity, relation)
);

CREATE INDEX idx_backlinks_to ON backlinks(to_entity);

-- ==============================================================
-- Tier Proposals — pipeline 产生的 tier 升级建议（待 review）
-- ==============================================================
CREATE TABLE tier_proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       TEXT NOT NULL REFERENCES entities(id),
    proposed_tier   INTEGER NOT NULL,
    current_tier    INTEGER NOT NULL,
    reason          TEXT NOT NULL,              -- 'mention_count_3' | 'mention_count_8' | 'manual'
    proposed_at     TEXT NOT NULL,
    decided_at      TEXT,                       -- NULL = 未处理
    decision        TEXT,                       -- 'approved' | 'rejected' | 'deferred'
    review_file     TEXT NOT NULL,              -- review 队列里对应文件
    CHECK (proposed_tier IN (1, 2, 3))
);

-- ==============================================================
-- Ingest Cursor — pipeline 进度记录
-- ==============================================================
CREATE TABLE ingest_cursor (
    source          TEXT PRIMARY KEY,           -- 'events.jsonl' | 'laundry'
    last_processed  TEXT NOT NULL,              -- event_id 或 文件路径
    last_run_at     TEXT NOT NULL
);

-- ==============================================================
-- Lint Results — lint 历史记录
-- ==============================================================
CREATE TABLE lint_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at          TEXT NOT NULL,
    kind            TEXT NOT NULL,              -- 'contradictions' | 'stale' | 'orphans' | 'citations'
    issue_count     INTEGER NOT NULL,
    report_file     TEXT NOT NULL
);
```

迁移管理：

- 用 `alembic` 太重，**自己写一个简单的 migration runner**：`brain/db/migrations/<NNNN>_<description>.sql`，schema 版本存在 `PRAGMA user_version`。
- Phase 1 只需要一个 baseline migration `0001_baseline.sql` 包含上述全部 DDL。

## 6. CLAUDE.md（项目根的 schema 说明）

`mem init` 时生成 `~/brain/CLAUDE.md`，用来给将来调用的 LLM 当 context 用。内容包括：

- Page 格式规范的简述
- Frontmatter 字段
- compiled truth + timeline 的写作要求
- entity 引用语法 `[[slug]]`

具体内容由 Codex 在实现 `mem init` 时按本 spec 生成，模板见 `pipeline.md` 的 init 章节。

## 7. Review 队列文件格式

每个 review item 是一个 markdown 文件 `~/brain/review/<YYYY-MM-DD>_<seq>_<kind>.md`：

```markdown
---
review_id: 2026-04-28_001_fact_conflict
kind: fact_conflict
created: 2026-04-28T10:23:45Z
status: pending
---

# 冲突: Zihan 当前位置

## 已有事实
- subject: zihan
- predicate: location
- object: UK
- valid_from: 2024-09-01
- source: events:01HXX...

## 新候选
- subject: zihan
- predicate: location
- object: Singapore
- valid_from: 2026-05-15
- source: events:01H99...
- confidence: 0.92

## 建议动作
[ ] 接受新事实，把旧事实的 valid_to 设为 2026-05-15
[ ] 拒绝新事实
[ ] 编辑后接受
[ ] 推迟决定

请勾选一个并保存，然后运行 `mem review` 处理。
```

`kind` 可选值：`fact_conflict` / `low_confidence_fact` / `pending_fact` / `tier_proposal` / `lint_finding` / `new_entity_review` / `ingest_error`。

- `pending_fact` 保存因 unresolved entity 暂时无法规范化的候选 fact。用户先处理对应 `new_entity_review`，再 approve `pending_fact`，系统会重新走 fact 分类、写入和页面更新逻辑。
- `ingest_error` 保存事件 ingest 失败时的 event payload、错误类型、错误信息和 traceback。cursor 可以继续推进，但失败不会只停留在一次性控制台输出里。

## 8. 命名规范

- **slug**：全小写，ASCII 字母数字和连字符。中文 entity 用拼音作 slug，title 保留中文。
- **event id**：ULID（26 字符大写）。
- **fact id**：自增整数。
- **review_id**：`<YYYY-MM-DD>_<seq>_<kind>` 格式。
- **页面文件名**：和 slug 相同，加 .md 后缀。
