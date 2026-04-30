# Pipeline

本文件定义所有 CLI 命令背后的算法。每个步骤明确指出"用代码"还是"调 LLM"。

## 总流程

```
事件/素材 → ingest → [自动入库 | review 队列] → 用户 review → 提交
                ↓
         backlinks 自动重建
                ↓
        定期 lint → review 队列 → 用户决定
```

## 1. `mem init`

初始化一个空的 brain 仓库。

### 步骤

1. **代码**：检查 `~/brain/` 是否存在。存在且非空 → 报错退出。
2. **代码**：创建目录结构（参见 `data-model.md` 第 1 节）。
3. **代码**：写入默认 `config.toml`。
4. **代码**：创建 `brain.db`，运行 `0001_baseline.sql` migration。
5. **代码**：写入空的 `events.jsonl`、`pages/index.md`、`pages/log.md`、`README.md`、`.gitignore`、`.gitattributes`。
6. **代码**：写入 `CLAUDE.md`，模板：

```markdown
# Brain Schema (for LLM consumers)

This brain follows a strict format. When reading or writing pages, follow these rules.

## Page structure
Every page has frontmatter, then four sections separated by `---`:
1. `# Compiled truth` — current best understanding, can be rewritten
2. `# Timeline` — append-only, never edit existing entries
3. `# Sources` — auto-maintained list of references

## Frontmatter
Required: type, slug, title, created, updated.
type ∈ {entity, project, concept, event, experience, conversation}.

## Cross-references
Use `[[slug]]` to link to another page. Use `[[slug|display]]` for custom display text.

## Timeline format
Each entry: `- <YYYY-MM-DD> [event:<ulid>]: <description>`
```

7. **代码**：`git init`，初始 commit "Initialize brain repository"。
8. **代码**：打印简短的 next-steps 给用户。

### 测试

- 在临时目录运行 `mem init`，检查所有文件存在
- 重复运行应失败
- DB schema 版本是 1

## 2. `mem ingest`

主管线。处理 laundry 里的新内容和 events.jsonl 里未处理的事件。

### 输入

- `~/brain/laundry/*.md`（不含 `processed/` 子目录）
- `events.jsonl` 中 cursor 之后的事件

### 步骤

```
ingest():
    1. 收集待处理项
    2. 对每个待处理项:
        a. signal-detect (LLM)         → 抽取候选 facts + 候选 entities
        b. resolve-entity (代码)        → 把候选 entities 映射到 canonical id
        c. tier-decision (代码)         → 检查 mention count，决定要不要建 tier 升级建议
        d. conflict-check (代码 + LLM)  → 新 facts 是否和已有 facts 矛盾
        e. write-decisions             → 入库 / 进 review 队列
    3. auto-link (代码)                 → 重建 backlinks
    4. update index.md / log.md (代码)
    5. git commit (如果 config 里启用了)
    6. 更新 ingest_cursor
    7. 打印总结报告
```

### 步骤细节

#### 2a. signal-detect (LLM)

对一条 laundry 文件或一条事件，用 LLM 抽取结构化候选。Prompt 模板（伪代码）：

```
SYSTEM: You are a fact extractor for a personal knowledge system.
Given the input below, extract:
- entities mentioned (people, organizations, projects, concepts, places, events)
- facts in (subject, predicate, object) form, with valid_from/valid_to if implied
- a brief summary suitable for a timeline entry

Respond as JSON matching this schema: { ... }

USER: <raw text from laundry file or event raw_payload>
```

输出用 Pydantic 校验，校验失败重试一次，再失败标记为"抽取失败"进 review。

#### 2b. resolve-entity (代码)

对每个候选 entity（带名称字符串）：

1. 查 `entity_aliases` 表，找精确匹配 → 命中返回 canonical id
2. 查 `entities.title` 精确匹配 → 命中返回 id
3. 都没命中 → 创建新 entity（type 由 signal-detect 推断），生成 slug，tier=3，title=候选名称
4. 把 mention_count + 1，更新 last_seen

**不要用 LLM 做名称匹配。** 模糊匹配只在用户运行 `mem entity merge` 时手动触发。

#### 2c. tier-decision (代码)

对每个被 mention 的 entity：

```python
def check_tier(entity):
    new_count = entity.mention_count
    current_tier = entity.tier
    if current_tier == 3 and new_count >= config.tier2_threshold:
        propose_tier(entity, target_tier=2, reason=f"mention_count_{new_count}")
    elif current_tier == 2 and new_count >= config.tier1_threshold:
        propose_tier(entity, target_tier=1, reason=f"mention_count_{new_count}")
```

`propose_tier()` 在 `tier_proposals` 表插一行，并在 `review/` 写一个 markdown 文件。

#### 2d. conflict-check

代码层面：对每条候选 fact，查同 `(subject, predicate)` 且 `superseded_by IS NULL AND valid_to IS NULL` 的现有 fact。

无现有 fact → 直接 ADD（按 confidence 阈值决定是否入库或进 review）。

有现有 fact 且 object 相同 → NOOP，只更新 last_seen。

有现有 fact 且 object 不同 → **冲突**：
- 如果新候选 confidence ≥ `confidence_auto_accept` 且现有 fact 是低 confidence → LLM 判断（"这是不是真覆盖"）。LLM 判断不冲突 → ADD；LLM 判断冲突且同意覆盖 → 自动 supersede（旧的 valid_to = 新的 valid_from，superseded_by = 新 id）；LLM 不同意覆盖或 borderline → 进 review。
- 否则 → 进 review，不自动决定。

#### 2e. write-decisions

对每个 fact：

```
if confidence >= auto_accept and not conflict:
    INSERT INTO facts ...
    append to relevant page Timeline (代码)
elif confidence < auto_reject:
    discard, log to log.md
else:
    write to review/ (代码)
```

如果 fact 的 subject 或 entity object 依赖尚未解析的新 entity，写 `pending_fact` review，而不是丢弃该 fact。用户处理对应 `new_entity_review` 后，可以 approve `pending_fact`，系统会复用同一套 classify/write/page 更新逻辑。

页面追加 timeline 项的格式：

```
- <YYYY-MM-DD> [event:<event_id>]: <signal-detect 给出的 timeline summary>
```

如果对应页面不存在，**自动创建 stub 页面**（compiled truth 段是 `(stub — 等待更多证据)`）。

#### 3. auto-link (代码)

零 LLM 调用。算法：

```python
def rebuild_backlinks():
    aliases = load_all_aliases()  # 内存 dict: alias_text → entity_id
    pattern = compile_alias_regex(aliases.keys())

    for page in all_pages():
        content = read_page_content(page)
        existing = backlinks_for_page(page)
        new = set()
        for match in pattern.finditer(content):
            entity_id = aliases[match.group()]
            relation = infer_relation(page.type, entity_id)  # 简单规则
            new.add((entity_id, relation))

        # diff 后只更新变化的
        delete_backlinks(page) if existing != new
        insert_backlinks(page, new)
```

`infer_relation` 是简单规则表，不调 LLM：

| from page type | to entity type | relation |
|---|---|---|
| project | person | works_with |
| project | concept | involves |
| event | person | attended_by |
| conversation | person | participant |
| 其他 | 任何 | mentions |

也扫描 `[[slug]]` 显式链接，relation 默认 `mentions`。

#### 4. update index / log

`index.md`：每次 ingest 后重新生成，分组列出所有 page，按 type 分章节，按 last_updated 倒序排。

`log.md`：append 一行，格式 `- <YYYY-MM-DD HH:MM> ingest: <N> events processed, <M> facts added, <K> review items created`。

### 输出

- 自动入库的 facts 数
- 进 review 队列的项数
- 升级建议数
- 失败/跳过数
- review 队列文件路径（如有）

### 测试

- 给定一条 laundry 文件 + mock LLM 输出 → 期望产生指定 facts + backlinks
- 冲突场景：新 fact 触发旧 fact supersede
- 低 confidence → review
- 重复运行不应重复处理（cursor 起作用）

## 3. `mem review`

处理 review 队列。

### 模式

- `mem review` —— 列出所有 pending 项
- `mem review <id>` —— 打开特定项（用 `$EDITOR` 启动外部编辑器）
- `mem review --apply` —— 扫描所有 pending 项，根据用户勾选执行

### 步骤（apply 模式）

```
for review_file in pending_files():
    parse_review_file(review_file)            # 代码：读出用户的勾选
    if no_decision:
        skip
    elif decision == "approved":
        execute_action(review_file.kind, ...)  # 代码：写 facts / 升级 tier / 等
        mark_decided(review_file)
        move_to_archive(review_file)
    elif decision == "rejected":
        mark_decided(review_file)
        move_to_archive(review_file)
    elif decision == "deferred":
        skip (留在队列)
git commit
```

`execute_action` 不调 LLM，纯代码执行。

### 测试

- 用户勾选"接受冲突的新 fact" → 旧 fact 被 supersede，新 fact 入库
- 用户勾选"接受 tier 升级" → entity.tier 更新，触发该 entity 页面的 compiled truth 重写（**这一步调 LLM**——读 timeline，让 LLM 重新综合 compiled truth，写回页面，git commit）

## 4. `mem lint`

```
mem lint --contradictions
mem lint --stale
mem lint --orphans
mem lint --citations
mem lint --all                    # 跑所有
```

### 4a. contradictions

代码层：扫 `facts` 表，找同 (subject, predicate) 有多条 active fact 但 object 不同的情况。

输出：每组冲突写一个 review item (`kind=fact_conflict`)，引用所有相关 fact。

LLM 不参与，因为 schema 已经能精确判断。

### 4b. stale

代码层：找最近 `stale_days`（默认 90）天没有新 timeline 项的 page，且 page 的 frontmatter `tier=1`（高 tier 才检查）。

输出：写一个 review item 列出所有 stale 页面，让用户决定是否 archive 或主动更新。

### 4c. orphans

代码层：扫所有 timeline 项，找其中提到的 entity 名称在 `entities` 表里**没有对应记录**或对应 entity 没有 page。

输出：列出来，让用户决定是否补建页面。

### 4d. citations

代码层：扫每个 page 的 `# Compiled truth` section，找出**没有出现在 timeline 里的关键名词**。

具体算法（不调 LLM 的版本）：

```python
def lint_citations(page):
    truth_text = page.compiled_truth_section
    timeline_text = page.timeline_section

    # 简单做法：抽取 truth 里所有 [[slug]] 引用
    truth_refs = extract_wikilinks(truth_text)
    timeline_refs = extract_wikilinks(timeline_text)

    unsupported = truth_refs - timeline_refs
    return unsupported
```

更高级的做法（LLM 版）：让 LLM 判断 truth 里的具体断言能不能在 timeline 里找到证据。Phase 1 只做简单版，LLM 版留到 Phase 2。

### 输出

每个 lint kind 写一个 markdown 报告到 `review/<YYYY-MM-DD>_lint_<kind>.md`，并在 `lint_results` 表插一条记录。

## 5. `mem ask`

Phase 1 的简单实现。

### 模式

- `mem ask "..."` —— 检索 + 列出 top-N 相关页面（默认 5）
- `mem ask "..." --explain` —— 把 top-N 喂给 LLM 生成自然语言答案
- `mem ask "..." --sql` —— 强制走 SQL 查询路径
- `mem ask "..." --type=project` —— 限定 page type

### 步骤（默认模式）

```
def ask(query):
    # 1. 关键词提取（代码，简单分词）
    keywords = tokenize(query)

    # 2. SQL 查询：检查关键词是否匹配 entity title / alias
    matched_entities = find_entities_matching(keywords)

    # 3. 关键词检索：在 page 内容上做简单 BM25-like 评分
    page_scores = score_pages_by_keywords(keywords)

    # 4. Backlink 加权：和 matched_entities 有 backlink 的 page 加分
    for entity in matched_entities:
        for page in pages_linked_to(entity):
            page_scores[page] *= 1.5

    # 5. 取 top-N，返回 page 摘要
    top = sorted(page_scores.items(), key=..., reverse=True)[:5]
    return [page_summary(p) for p, _ in top]
```

`page_summary` 返回 page 的 compiled truth section 前 200 字 + 最近 3 条 timeline。

### --explain 模式

把 top-N 页面的全文和原始 query 拼成一个 prompt 给配置的 LLM，强约束："只用提供的 brain 内容回答，没有信息直说不知道，不要编造"。

### 测试

- 关键词 "computer vision" → 应找到 cv-coursework 页面
- entity 别名查询应能解析（"老张" → 找到 zhang-san 页面）

## 6. `mem promote-chat <event-id>`

把一次 AI 对话从 events.jsonl 提升为 conversations 页。

### 步骤

1. **代码**：读 event，验证 `kind == 'ai_chat'`
2. **LLM**：让配置的 LLM 把对话内容转写成 conversations 页（compiled truth = 这次对话的核心结论；timeline = 关键讨论节点）
3. **代码**：写到 `pages/conversations/<YYYY-MM-DD>_<slug>.md`
4. **代码**：在 events.jsonl 追加 `kind=page_edited` 事件
5. **代码**：触发一次 `mem ingest` 增量处理这个新页面（提取里面提到的 entity）
6. **代码**：git commit

### 测试

- 给一个 ai_chat event → 输出页面格式正确
- 重复 promote 同一 event → 应该提示已存在

## 7. `mem rebuild`

```
mem rebuild --db                 # 从 events + 当前 pages 重建 brain.db
mem rebuild --pages <slug>       # 从 events 重建某个 page
mem rebuild --backlinks          # 重新扫描所有 page 内容重建 backlink 表
mem rebuild --index              # 重建 pages/index.md
```

### --db

完全 drop + recreate。从所有 page frontmatter + 内容 + events.jsonl 重新填充 entities / facts / backlinks / tier 表。

### --pages

从 events.jsonl 找所有 affected_pages 包含该 slug 的事件，重新组织成 timeline，让 LLM 重写 compiled truth。**这是危险操作**——会覆盖现有页面，需要 `--force` 标志。

### --backlinks 和 --index

完全自动，纯代码。

## 8. 其他

### `mem status`

打印仓库状态：page 数（按 type 分组）、entity 数（按 tier 分组）、fact 数、review 队列大小、上次 ingest 时间。

### `mem entity merge <slug-a> <slug-b>`

把两个 entity 合并：

1. **代码**：把 `slug-b` 的所有 alias 转移到 `slug-a`
2. **代码**：facts 表中所有 `subject = slug-b` 改成 `subject = slug-a`，object 同理
3. **代码**：backlinks 表中 to_entity 改成 slug-a
4. **代码**：把 `pages/.../slug-b.md` 的 timeline 追加到 slug-a 的 timeline，按时间排序
5. **代码**：删除 slug-b 页面（git commit）
6. **LLM**：让配置的 LLM 重写 slug-a 的 compiled truth，因为有了新信息

### Git 集成

每个会写入数据的命令在结束时自动 commit（如果 `config.git.auto_commit = true`）。Commit message 由命令决定：

- `ingest: process N events, add M facts`
- `review: apply N decisions`
- `lint: <kind>, M issues found`
- `promote-chat: <event-id> → <slug>`
- `entity merge: <a> + <b> → <a>`

## 9. 错误处理

- LLM 调用失败 → 重试 1 次，再失败该项进 review (`kind=ingest_error`)
- DB 操作失败 → 抛异常，回滚事务，事件不标记为已处理（下次 ingest 会重试）
- 文件冲突（git）→ 报错退出，要求用户手动处理
- 解析 markdown 页面失败 → 记录到 `~/brain/errors.log`，跳过该页面

所有 ingest 操作必须包在 SQLite 事务里，要么全成要么全回滚。
