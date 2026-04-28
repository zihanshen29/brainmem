# Phase 1 Build Tasks

本文件是给 Codex 的**有序执行清单**。每个任务完成后跑对应测试通过，再进下一个。

## 总原则

- **先骨架，后血肉**。先把项目结构搭起来再填实现。
- **每个任务做一个 PR-sized 的提交**。任务结束跑 `git commit`。
- **不要跨任务**。任务 N 不要碰任务 N+5 的代码。
- **遇到 spec 不清楚的地方停下来记录**，写到 `OPEN_QUESTIONS.md`，继续做下一个不依赖该问题的任务。
- **Phase 2 的功能不要提前做**，即使你认为顺手。

## Task 0 — Bootstrap

**目标**：可安装、CLI 能跑 `mem --version`。

1. 创建 `pyproject.toml`（内容见 `tech-stack.md` 第 2 节）
2. 创建 `src/brain/__init__.py` 包含 `__version__ = "0.1.0"`
3. 创建 `src/brain/cli/main.py`，定义 `typer.Typer()` 并注册一个 `version` 子命令
4. 创建 `.ruff.toml`（见 `tech-stack.md`）
5. 创建 `.gitignore`（包含 `.venv/`、`__pycache__/`、`*.egg-info`、`brain.log`、`.pytest_cache/`）
6. 创建 `README.md` 占位
7. 在仓库根 `pip install -e ".[dev]"`，验证 `mem --version` 输出 `0.1.0`

**Done 的标志**：`mem --version` 在 PowerShell 里运行成功输出版本号。

## Task 1 — Pydantic 模型

**目标**：所有 Pydantic 模型定义好，能 round-trip 序列化。

1. `src/brain/models/event.py` —— `Event`、`EventKind` (见 `data-model.md` 第 3 节)
2. `src/brain/models/page.py` —— `Page`、`Frontmatter`、`PageType`、`Tier`
3. `src/brain/models/fact.py` —— `Fact`、`FactCandidate`
4. `src/brain/models/entity.py` —— `Entity`、`EntityAlias`、`EntityType`
5. `src/brain/models/__init__.py` 导出所有上述类型

**测试** (`tests/unit/test_models.py`)：

- 每个模型构造一个有效实例，序列化到 dict 再反序列化，断言相等
- 边界：缺必填字段应抛 ValidationError
- `Event.id` 必须是合法 ULID 格式

## Task 2 — Paths & Config

**目标**：能解析 brain root 和读 config.toml。

1. `src/brain/paths.py` —— `BrainPaths` 数据类，提供 `root / events_jsonl / pages_dir / db_path / ...` 属性
2. `src/brain/config.py` —— `Config` Pydantic 模型对应 `config.toml` 全部字段，`load_config(path) -> Config`
3. `src/brain/exceptions.py` —— 定义异常层级 (见 `tech-stack.md` 8d)

**测试**：

- 给定一个临时 toml 文件，加载后字段值正确
- 缺失必填字段抛 ConfigError

## Task 3 — SQLite 层

**目标**：能初始化 DB、运行 migrations、做基本 CRUD。

1. `src/brain/db/migrations/0001_baseline.sql` —— 完整 DDL (见 `data-model.md` 第 5 节)
2. `src/brain/db/migrations.py` —— `init_db(path) -> None`，运行所有 migration，更新 `PRAGMA user_version`
3. `src/brain/db/connection.py` —— `connect(path) -> sqlite3.Connection`，开启外键、WAL 模式
4. `src/brain/db/entities.py`：
   - `upsert_entity(conn, entity) -> str` (返回 id)
   - `get_entity(conn, id) -> Entity | None`
   - `add_alias(conn, alias, entity_id, source)`
   - `lookup_by_alias(conn, alias) -> str | None`
   - `increment_mention(conn, id)`
5. `src/brain/db/facts.py`：
   - `add_fact(conn, fact) -> int`
   - `find_active_facts(conn, subject, predicate) -> list[Fact]`
   - `supersede(conn, old_fact_id, new_fact_id)`
6. `src/brain/db/backlinks.py`：
   - `replace_backlinks_for_page(conn, page_slug, links)`
   - `get_backlinks_to(conn, entity_id) -> list[Backlink]`
7. `src/brain/db/tier.py`：
   - `propose_tier(conn, entity_id, target_tier, reason, review_file) -> int`
   - `record_tier_decision(conn, proposal_id, decision)`

**测试** (`tests/unit/test_db.py`)：

- init 后 schema 版本正确，所有表存在
- entity upsert + lookup round-trip
- alias 唯一约束生效
- supersede 后旧 fact 的 superseded_by 正确
- 事务回滚：故意 raise 后断言数据没写入

## Task 4 — Event Ledger

**目标**：能 append event、按 cursor 读取。

1. `src/brain/ledger/writer.py` —— `append_event(path, event) -> None`，原子追加
2. `src/brain/ledger/reader.py`：
   - `read_all(path) -> Iterator[Event]`
   - `read_after(path, last_id) -> Iterator[Event]`
   - `find_event(path, id) -> Event | None`

注意：

- 写入用 `with open(path, "a", encoding="utf-8") as f`，每条用 `json.dumps(...) + "\n"`
- 读取宽容损坏行：log warning 跳过
- ULID 排序：因为 ULID 自带时间戳前缀，字符串比较即时间排序

**测试**：

- 写入 100 个 event，全部读回顺序一致
- `read_after(last_id)` 严格在 last_id 之后
- 损坏行不阻塞读取

## Task 5 — Page Parser & Writer

**目标**：能解析和写入 page 文件。

1. `src/brain/pages/parser.py`：
   - `parse_page(path) -> Page`：用 python-frontmatter 读 frontmatter，按 `---` 分割三个 section
   - 严格校验四个 section 顺序：`# Compiled truth` / `# Timeline` / `# Sources`
   - 缺失 section 报错（除了 `# Sources` 可缺）
2. `src/brain/pages/writer.py`：
   - `write_page(path, page) -> None`：渲染回 markdown
   - `append_timeline(path, entry: TimelineEntry)`：在 `# Timeline` section 末尾插入新行
   - `update_compiled_truth(path, new_text)`：替换 compiled truth section
   - `update_sources(path, sources)`：重写 Sources section（自动维护）
3. `src/brain/pages/timeline.py`：
   - `TimelineEntry` model: `date / event_id / description`
   - `parse_entry(line) -> TimelineEntry`
   - `format_entry(entry) -> str`
4. `src/brain/pages/index.py`：
   - `regenerate_index(brain_root)`：扫所有 page 重写 `pages/index.md`
   - `append_log(brain_root, message)`：追加一行到 `pages/log.md`

**测试**：

- Round-trip：parse → write → parse 应得到相同 Page
- Append timeline 不破坏其他 section
- Update compiled truth 不动 timeline
- 缺失 section 抛 PageParseError

## Task 6 — `mem init`

**目标**：完整跑通 `mem init`，产生符合 spec 的目录。

1. `src/brain/cli/init.py`：
   - `init_brain(root: Path, force: bool = False)` 函数
   - 创建所有目录、写所有种子文件、init DB、git init、初始 commit
2. `CLAUDE.md` 模板见 `pipeline.md` 第 1 节
3. 在 `cli/main.py` 注册 `init` 子命令

**测试** (`tests/integration/test_init.py`)：

- 在临时目录跑 init，断言所有文件/目录存在
- DB schema 版本 = 1
- git log 有一条 commit "Initialize brain repository"
- 重复跑应报错（除非 --force）

## Task 7 — Git Ops

**目标**：封装 git commit。

1. `src/brain/git_ops.py`：
   - `commit(root, message, paths=None) -> str | None`
   - `is_dirty(root) -> bool`
   - 异常包成 GitError

**测试**：

- 修改文件后 commit 成功，返回 sha
- 没修改时 commit 返回 None
- 在非 git 目录抛 GitError

## Task 8 — LLM Client

**目标**：封装 Anthropic 调用，强制结构化输出。

1. `src/brain/llm/client.py`：
   - `extract_signal(text: str) -> SignalExtraction`：返回 Pydantic 模型，包含 entities + facts + summary
   - `judge_conflict(old: Fact, new: FactCandidate) -> ConflictJudgment`
   - `rewrite_compiled_truth(timeline: list[TimelineEntry], current_truth: str | None) -> str`
   - 内部用 Anthropic Python SDK，model 从 config 读
   - 失败重试 1 次（指数退避），仍失败抛 LLMError
2. `src/brain/llm/prompts.py`：
   - 各个任务的 prompt 模板（Python f-string 或 Jinja-style 字符串）
   - 强制 JSON 输出，schema 描述放在 system prompt

注意：

- 不要把 API key 写到日志
- mock 用：客户端有一个 `_extract_impl` 函数指针，测试时 monkeypatch 它

**测试**：

- monkeypatch `_extract_impl` 返回固定 dict，断言上层函数解析正确
- LLM 返回非法 JSON 抛 LLMError
- LLM 返回不符 schema 抛 ValidationError

## Task 9 — Resolve & Autolink

**目标**：实体解析和 backlink 提取。

1. `src/brain/pipeline/resolve.py`：
   - `resolve_entity(conn, name: str, hint_type: EntityType | None) -> Entity`
   - 流程：alias 查表 → title 查表 → 创建新 entity（slug 由代码生成）
   - 中文 entity slug 生成：用 pinyin 库还是手动转？**Phase 1 用简化方案**：中文直接保留拼音化的字符串方案太复杂，先约定 slug 必须是 ASCII，用户首次见到中文 entity 时由 LLM 给出建议 slug 让用户在 review 里确认。代码层面 slug 生成函数对中文返回 None，由调用方决定（通常进 review）。
2. `src/brain/pipeline/autolink.py`：
   - `extract_backlinks(content: str, alias_map: dict[str, str]) -> list[Backlink]`
   - 算法：编译 alias map 成 trie 或 alternation regex，扫描文本
   - 也扫描 `[[slug]]` 和 `[[slug|display]]` 显式语法
   - relation 推断：用规则表（见 `pipeline.md` 第 2.3 节）
   - 0 LLM 调用

**测试**：

- 已知 alias "老张" → 解析到 zhang-san
- 全文里出现 "[[zhang-san]]" 也建立 backlink
- 中文 entity 首次出现返回 None slug（让上层处理）

## Task 10 — Signal Detect

**目标**：把一段文本送给 LLM 抽取候选 entities + facts。

1. `src/brain/pipeline/signal_detect.py`：
   - `SignalExtraction` Pydantic 模型（entities, facts, timeline_summary, suggested_page_type）
   - `detect_signal(text: str, hint: dict | None = None) -> SignalExtraction`
   - 内部调 `llm.client.extract_signal`
2. Prompt 在 `llm/prompts.py` 里定义，包含示例 (few-shot)

**测试**：

- mock LLM 返回，断言 Pydantic 校验过
- 实际不连 API（CI 时不联网）

## Task 11 — Conflict & Tier 决策

**目标**：给定候选 fact，决定 ADD/UPDATE/NOOP/CONFLICT。

1. `src/brain/pipeline/conflict.py`：
   - `classify_fact(conn, candidate) -> Decision`，Decision 是 enum: ADD / NOOP / SUPERSEDE / CONFLICT
   - SUPERSEDE 需要 LLM 二次确认 (`llm.judge_conflict`)
2. `src/brain/pipeline/tier.py`：
   - `check_tier_upgrade(conn, entity_id) -> TierProposal | None`
   - 阈值从 config 读

**测试**：

- 同 (subject, predicate) 不同 object 且高 confidence → CONFLICT
- 完全相同 → NOOP
- mock LLM 同意 supersede → SUPERSEDE
- mention count 跨过阈值 → 返回 proposal

## Task 12 — Ingest 主管线

**目标**：完整的 `mem ingest` 跑通。

1. `src/brain/pipeline/ingest.py`：
   - `ingest(brain_root, source, dry_run, limit) -> IngestReport`
   - 集成 Task 4–11 的所有步骤
   - 生成 review 文件（用 `pages/writer.py` 类似的写入工具）
   - 自动 git commit
2. `src/brain/cli/ingest.py`：CLI 包装

Review 文件模板见 `data-model.md` 第 7 节。

**测试** (`tests/integration/test_ingest.py`)：

- 放一个 laundry 文件，mock LLM 返回固定结果，跑 ingest
- 断言：facts 入库、page 创建、timeline 追加、laundry 文件归档
- 冲突场景：放第二个 laundry 触发冲突 → review 文件存在
- dry-run：所有写入都跳过
- cursor：第二次 ingest 不重复处理

## Task 13 — Review 命令

1. `src/brain/pipeline/review.py`：
   - `list_pending(brain_root) -> list[ReviewItem]`
   - `parse_review_file(path) -> ReviewDecision`
   - `apply_decision(conn, decision)` —— 根据 kind 派发到具体执行函数
   - 支持的 kind：fact_conflict / low_confidence_fact / tier_proposal / lint_finding / new_entity_review
2. `src/brain/cli/review.py`：CLI 包装，包括 `--apply` 模式和单 id 模式

特别处理：tier 升级 → 触发 compiled truth 重写（调 LLM）。

**测试**：

- 模拟用户勾选 → apply → DB 状态正确变化
- tier proposal approved → entity tier 字段更新 + page compiled truth 重写
- archived review 文件移到 `review/archive/`

## Task 14 — Lint

1. `src/brain/pipeline/lint.py`：四个 lint 函数（contradictions / stale / orphans / citations）
2. `src/brain/cli/lint.py`：CLI 包装

每个 lint 写一个 review 报告文件，并在 `lint_results` 表插一条记录。

**测试**：

- 故意制造矛盾 → contradictions lint 找到
- 90+ 天没 timeline 项的 page → stale lint 找到
- 提到没建页面的 entity → orphans lint 找到

## Task 15 — Ask

1. `src/brain/pipeline/ask.py`：
   - 简单 BM25-like 评分（按词频，取 log）
   - SQL 查询 entity title / alias 命中
   - backlink 加权
   - top-N 排序
   - `--explain` 模式调 LLM
2. `src/brain/cli/ask.py`：CLI 包装，输出格式见 `cli.md`

**测试**：

- 已知关键词 → 期望页面在 top-3
- 别名查询命中

## Task 16 — Promote-Chat

1. `src/brain/pipeline/promote_chat.py`
2. `src/brain/cli/promote_chat.py`

LLM 把对话转成 page，然后内部触发一次 ingest 增量处理新页面。

**测试**：

- 给定一个 mock ai_chat event → 输出 conversations 页格式正确
- 重复 promote 同 event → 报错

## Task 17 — Rebuild

1. `src/brain/pipeline/rebuild.py`：四个子命令（db / pages / backlinks / index）
2. `src/brain/cli/rebuild.py`

**测试**：

- 删 DB 后 rebuild --db → 数据从 events + pages 还原
- rebuild --backlinks 后 backlinks 表内容和上一次一致

## Task 18 — Status, Entity Merge, Capture

剩余 CLI：

1. `src/brain/cli/status.py`
2. `src/brain/cli/entity.py` —— `merge` 子命令
3. `src/brain/cli/capture.py`

**测试**：

- merge 后 alias / fact / backlink 全部转移到 canonical
- merged 页面被删除（git 历史保留）

## Task 19 — README & 收尾

1. 完善 `README.md`：安装、quick start、目录结构、链接到 spec
2. 写 `tests/conftest.py` 的最终版（如果之前散在各 test 文件）
3. 跑全部测试，记录覆盖率
4. 记录 `OPEN_QUESTIONS.md`（如果有）

## Task 20 — Smoke Test

人工跑一遍完整流程：

1. `mem init` 在临时目录
2. `mem capture` 写一段思考
3. `mem ingest`（需要真 API key）
4. 检查 review 队列
5. `mem review --apply`
6. `mem ask "..."`
7. `mem lint --all`
8. `mem status`

如果有 step 失败，回到对应 Task 修复。

## 进度追踪

每完成一个 Task 在 commit message 写 `[Task N] <summary>`。最后 README 末尾加一个 checklist 标记进度。

## 给 Codex 的元规则

1. **不要重写 spec**。如果你觉得某个设计不对，写到 `OPEN_QUESTIONS.md`，继续按 spec 做。
2. **不要乱加依赖**。`tech-stack.md` 已经锁定。
3. **不要省略测试**。spec 里写了测试的任务必须有测试。
4. **不要跨任务修改**。任务 N 不要改任务 K (K < N) 的代码，除非有 bug 必须修。
5. **遇到不确定**优先看 spec 文件，再看不到记到 OPEN_QUESTIONS。
6. **每个任务完成后跑 ruff + pytest 关键测试**，全绿才进下一个。
7. **不要实现 Phase 2 功能**。本 spec 没提到的功能不要做，等 Phase 2 spec。
