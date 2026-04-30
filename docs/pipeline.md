# Pipeline

本文件定义所有 CLI 命令背后的算法。Phase 1 的算法保持不变，Phase 2 新增管线在末尾加入。

## Phase 1 管线（保留, 简述）

- `mem init` — 创建目录、init DB、git init
- `mem ingest` — signal-detect → resolve-entity → tier-decision → conflict-check → write-decisions → auto-link → update index/log
- `mem review [--apply]` — 处理 review 队列，故障隔离
- `mem lint --<kind>` — contradictions / stale / orphans / citations
- `mem promote-chat <event-id>` — AI 对话提升为 conversations 页
- `mem rebuild --<scope>` — 重建派生数据
- `mem capture` — 写入 laundry
- `mem entity merge` — 合并 entity
- `mem status` — 仓库状态

完整算法见 git 历史里的 Phase 1 spec。下面只描述 **Phase 2 新增/改动** 的部分。

---

## (P2) `mem reindex`

负责把 page 内容转成 embedding 写入 sqlite-vec。

### 模式

```
mem reindex                       # 增量, 只 embed 变化或新增的 chunk
mem reindex --force               # 全量重 embed (用于换 embedding model)
mem reindex --pages <slug>        # 只重 embed 一个 page
mem reindex --dry-run             # 显示会处理的 chunk 数和预估 token, 不真跑
```

### 增量算法

```python
def reindex(brain_root, force=False, page_filter=None, dry_run=False):
    config = load_config(...)
    embedding_client = build_embedding_client(config)
    conn = connect(brain_root.db_path)

    pages = list_pages(brain_root, filter=page_filter)

    chunks_to_embed = []
    chunks_to_delete = []
    chunks_unchanged = 0

    for page in pages:
        # 1. chunk 这个 page
        new_chunks = split_page_into_chunks(page, config.embedding.chunk_max_chars)
        new_keys = {(c.chunk_kind, c.chunk_id) for c in new_chunks}

        # 2. 查 DB 里这个 page 现有的 chunk
        existing = query_embedding_index_for_page(conn, page.slug)
        existing_keys = {(e.chunk_kind, e.chunk_id) for e in existing}

        # 3. 找出: 删除的 / 新增的 / 可能变化的
        to_delete = [e for e in existing if (e.chunk_kind, e.chunk_id) not in new_keys]
        chunks_to_delete.extend(to_delete)

        for chunk in new_chunks:
            content_hash = sha256(chunk.text + config.embedding.model + str(config.embedding.dimension))
            existing_match = next(
                (e for e in existing
                 if (e.chunk_kind, e.chunk_id) == (chunk.chunk_kind, chunk.chunk_id)),
                None
            )
            if existing_match and existing_match.content_hash == content_hash and not force:
                chunks_unchanged += 1
                continue
            chunks_to_embed.append((chunk, content_hash))

    if dry_run:
        report_dry_run(chunks_to_embed, chunks_to_delete, chunks_unchanged)
        return

    # 4. batch embed
    for batch in chunks(chunks_to_embed, n=config.embedding.batch_size):
        texts = [c[0].text for c in batch]
        vectors = embedding_client.embed(texts, model=config.embedding.model)
        with conn:
            for (chunk, content_hash), vector in zip(batch, vectors):
                upsert_embedding(conn, chunk, content_hash, vector, config.embedding.model)
        track_token_usage(conn, len(texts), embedding_client.last_call_tokens)

    # 5. 删除孤儿 chunk
    with conn:
        for orphan in chunks_to_delete:
            delete_embedding(conn, orphan.rowid)

    # 6. 写 reindex event 到 ledger
    append_event(events.jsonl, Event(
        kind=EventKind.REINDEXED,
        metadata={
            "chunks_added": len(chunks_to_embed) - n_updated,
            "chunks_updated": n_updated,
            "chunks_removed": len(chunks_to_delete),
            "model": config.embedding.model,
            "tokens_used": total_tokens,
        }
    ))

    # 7. 更新 stats
    update_stat(conn, "last_reindex_at", now_iso())

    return ReindexReport(...)
```

### split_page_into_chunks

```python
def split_page_into_chunks(page: Page, max_chars: int) -> list[EmbeddingChunk]:
    chunks = []

    # 1. compiled_truth 永远 1 个 chunk
    if page.compiled_truth.strip() and page.compiled_truth.strip() != "(stub - waiting for more evidence)":
        text = page.compiled_truth.strip()
        if len(text) > max_chars:
            text = text[:max_chars]
        chunks.append(EmbeddingChunk(
            page_slug=page.frontmatter.slug,
            chunk_kind="compiled_truth",
            chunk_id="main",
            text=f"{page.frontmatter.title}\n\n{text}",  # 标题 + 正文一起 embed
            text_preview=text[:200],
        ))

    # 2. timeline 每条 1 个 chunk
    for entry in page.timeline:
        text = entry.description
        if len(text) > max_chars:
            text = text[:max_chars]
        chunks.append(EmbeddingChunk(
            page_slug=page.frontmatter.slug,
            chunk_kind="timeline_entry",
            chunk_id=entry.event_id,
            text=f"{entry.date} - {page.frontmatter.title}: {text}",  # 日期 + 标题 + 描述
            text_preview=text[:200],
        ))

    return chunks
```

**为什么标题要拼进 chunk text**：embed compiled_truth 时如果只 embed 正文，搜"我的计算机视觉作业"找不到——因为正文里可能没出现"作业"两个字。把 page title 拼进去解决这种 hint loss。

### 自动增量触发

`mem ingest` 完成后如果 `config.import.auto_reindex == True`（默认开），自动调一次 `reindex(page_filter=touched_slugs)`。这让用户感觉"ingest 之后立刻能 hybrid 查到新页面"。

`mem ingest --no-auto-reindex` 可以跳过。

---

## (P2) `mem ask` (重写, 改成 hybrid)

### 算法概览

```
ask(query, mode='hybrid', top=5, debug=False, type_filter=None):
    1. classify_query(query)
       → 'structured' | 'open_ended'

    2. if structured and config.retrieval.sql_shortcut_enabled:
        return sql_direct_query(query)

    3. 三路并行召回 (各取 top-50):
       vector_hits  = vector_search(query)
       keyword_hits = bm25_search(query)
       sql_hits     = sql_entity_match(query)

    4. RRF 融合:
       fused = rrf_fuse(vector_hits, keyword_hits, sql_hits, k=60)

    5. 后过滤:
       if type_filter:
           fused = [r for r in fused if page_type(r.page_slug) == type_filter]

    6. 取 top-N, 拉每页的预览
       return [page_summary(r) for r in fused[:top]]
```

### Query Classifier

`classify_query(query)` 用规则 + LLM 兜底：

```python
STRUCTURED_PATTERNS = [
    r"我.{0,5}(\d{4}年|\d月|Q[1-4]|周|天|月)",   # 时间限定
    r"(谁|什么人).{0,5}(在|是)",                  # 主语查询
    r"(什么时候|何时)",                           # 时间问 ate
    r"^(列出|列举|有哪些)",                      # 枚举
]

def classify_query(query: str) -> Literal["structured", "open_ended"]:
    if any(re.search(p, query) for p in STRUCTURED_PATTERNS):
        return "structured"
    # 否则默认 open_ended; 可选 LLM 兜底, 但 Phase 2 先不调 LLM
    return "open_ended"
```

**Phase 2 故意不在 classifier 里调 LLM**：每次 ask 都先调一次 LLM 判断会让 ask 变慢且烧钱。先用规则路径，等真实查询暴露规则覆盖不到的 case 再加 LLM。

### Vector Search

```python
def vector_search(conn, query: str, top: int = 50) -> list[RetrievalHit]:
    embedding_client = build_embedding_client()
    [query_vector] = embedding_client.embed([query], model=config.embedding.model)

    rows = conn.execute("""
        SELECT
            ei.page_slug, ei.chunk_kind, ei.chunk_id, ei.text_preview,
            distance
        FROM embeddings e
        JOIN embedding_index ei ON ei.rowid = e.rowid
        WHERE e.embedding MATCH ?
        ORDER BY distance ASC
        LIMIT ?
    """, (serialize_vector(query_vector), top)).fetchall()

    return [
        RetrievalHit(
            page_slug=row["page_slug"],
            chunk_kind=row["chunk_kind"],
            chunk_id=row["chunk_id"],
            score=row["distance"],   # 越小越相关
            rank=i + 1,
            path="vector",
        )
        for i, row in enumerate(rows)
    ]
```

### BM25 Search

```python
def bm25_search(conn, query: str, top: int = 50) -> list[RetrievalHit]:
    """简单 BM25, 在 page 内容文本上跑。"""
    keywords = tokenize(query)  # 中文用 jieba, 英文 split

    # 加载所有 page 的 chunk text + preview (从 embedding_index 拿)
    chunks = conn.execute("""
        SELECT page_slug, chunk_kind, chunk_id, text_preview FROM embedding_index
    """).fetchall()

    # 但 text_preview 只是前 200 字, BM25 应该对完整 chunk 评分
    # 所以从 page 文件里读完整内容来打分
    scored = []
    for chunk in chunks:
        page = read_page(brain_root, chunk["page_slug"])
        full_text = chunk_text_from_page(page, chunk["chunk_kind"], chunk["chunk_id"])
        score = bm25_score(keywords, full_text)
        if score > 0:
            scored.append((chunk, score))

    scored.sort(key=lambda x: x[1], reverse=True)
    return [
        RetrievalHit(
            page_slug=c["page_slug"],
            chunk_kind=c["chunk_kind"],
            chunk_id=c["chunk_id"],
            score=s,
            rank=i + 1,
            path="keyword",
        )
        for i, (c, s) in enumerate(scored[:top])
    ]
```

注意 BM25 跑在 page 内容上，不是 embedding_index 的 text_preview——preview 太短不够准。

### SQL Entity Match

```python
def sql_entity_match(conn, query: str, top: int = 50) -> list[RetrievalHit]:
    """识别 query 中的 entity 名/别名, 找包含它们的 page。"""
    keywords = tokenize(query)

    matched_entities = set()
    for kw in keywords:
        # 严格匹配 alias 表
        rows = conn.execute(
            "SELECT entity_id FROM entity_aliases WHERE alias = ? COLLATE NOCASE",
            (kw,)
        ).fetchall()
        matched_entities.update(r["entity_id"] for r in rows)

        # 也查 entities.title
        rows = conn.execute(
            "SELECT id FROM entities WHERE title = ? COLLATE NOCASE",
            (kw,)
        ).fetchall()
        matched_entities.update(r["id"] for r in rows)

    if not matched_entities:
        return []

    # 找所有 backlinks 指向这些 entity 的 page
    placeholders = ",".join("?" for _ in matched_entities)
    rows = conn.execute(f"""
        SELECT DISTINCT from_page, COUNT(*) as link_count
        FROM backlinks
        WHERE to_entity IN ({placeholders})
        GROUP BY from_page
        ORDER BY link_count DESC
        LIMIT ?
    """, [*matched_entities, top]).fetchall()

    # 也把 entity 自己的 page 加进去
    entity_pages = []
    for eid in matched_entities:
        entity = get_entity(conn, eid)
        if entity and entity.page_path:
            entity_pages.append(eid)

    hits = []
    for i, eid in enumerate(entity_pages):
        hits.append(RetrievalHit(
            page_slug=eid, chunk_kind="compiled_truth", chunk_id="main",
            score=10.0, rank=i + 1, path="sql"
        ))
    for i, row in enumerate(rows, start=len(entity_pages)):
        hits.append(RetrievalHit(
            page_slug=row["from_page"], chunk_kind="compiled_truth", chunk_id="main",
            score=float(row["link_count"]), rank=i + 1, path="sql"
        ))

    return hits[:top]
```

### RRF Fusion

```python
def rrf_fuse(*paths: list[RetrievalHit], k: int = 60) -> list[FusedResult]:
    """Reciprocal Rank Fusion."""
    page_scores: dict[str, dict] = {}  # page_slug -> {score, chunks}

    for hits in paths:
        for hit in hits:
            slug = hit.page_slug
            if slug not in page_scores:
                page_scores[slug] = {"score": 0.0, "chunks": []}
            page_scores[slug]["score"] += 1.0 / (k + hit.rank)
            page_scores[slug]["chunks"].append(hit)

    fused = [
        FusedResult(
            page_slug=slug,
            chunks=data["chunks"],
            rrf_score=data["score"],
            final_rank=0,  # 填充见下
        )
        for slug, data in page_scores.items()
    ]
    fused.sort(key=lambda r: r.rrf_score, reverse=True)
    for i, r in enumerate(fused, start=1):
        r.final_rank = i
    return fused
```

### SQL Direct Query (结构化短路)

```python
def sql_direct_query(query: str) -> list[FusedResult]:
    """对 'I 在 2025 Q2 做什么' 这种结构化查询, 不走 RRF。"""
    # Phase 2 的实现: 让 LLM 把 query 转成 SQL 查 facts 表
    sql = llm_client.translate_to_sql(query, schema=FACTS_SCHEMA_DESC)
    rows = conn.execute(sql).fetchall()

    # 把命中的 fact 关联回它们的 source_event 和对应 page
    return assemble_fact_results(rows)
```

**这里调一次 LLM 是必要的**——结构化查询的多样性（中英文混合、各种时间表达）规则匹配做不全。但只调一次，且只在 classifier 判定为 structured 时触发。

### `--debug` 输出

```
$ mem ask "我的项目最近怎么样" --debug

Query: 我的项目最近怎么样
Classifier: open_ended

Vector path (top 10):
  1. cv-coursework / compiled_truth (distance=0.31)
  2. recommendation-algorithm / compiled_truth (distance=0.34)
  ...

Keyword path (top 10):
  1. cv-coursework / compiled_truth (BM25=4.21)
  ...

SQL path (top 10):
  1. cv-coursework / compiled_truth (link_count=8)
  ...

RRF fusion (top 5):
  1. cv-coursework      (score=0.0473)  [v1, k1, s1]
  2. recommendation-... (score=0.0287)  [v2, k3, s_]
  ...
```

---

## (P2) `mem import`

### 模式

```
mem import <path>                              # 走完整流程
mem import <path> --dry-run                    # 只 cost estimate, 不写
mem import <path> --kind md,txt,pdf,jsonl      # 限定文件类型
mem import <path> --then-ingest                # import 后自动 ingest
mem import --resume                            # 继续上次中断的 import
mem import --status                            # 查看进行中的 import
mem import --abort <job-id>                    # 中止
```

### 算法

```
import(path, kinds=None, dry_run=False, then_ingest=False):
    1. discover_files(path, kinds)
       → list[(file_path, kind, hash)]
       skip 已经在 import_files 里的 (基于 file_hash, 跨 job 去重)

    2. cost_estimate(files)
       → CostEstimate

    3. if dry_run:
        print estimate; return

    4. if estimate.total_usd >= threshold:
        prompt user "估计 $X.XX, 继续? [y/N]"
        if not confirmed: abort

    5. job = create_import_job(...)

    6. for batch in chunks(files, n=config.import.batch_size):
        for file in batch:
            try:
                text_or_chunks = extract(file)
                laundry_path = write_to_laundry(text_or_chunks, job.id)
                mark_extracted(file, laundry_path)
                append_event(EventKind.BULK_IMPORTED, ...)
            except Exception as exc:
                mark_failed(file, exc)
        git_commit(f"import: batch {batch_num} for job {job.id}")

    7. if all files done:
        update job status = 'completed'

    8. if then_ingest:
        ingest(brain_root, source='laundry')
        # auto_reindex 在 ingest 里自动触发
```

### Extractors

每种 kind 一个 extractor，base interface:

```python
# brain/import_/extractors/base.py

class Extractor(Protocol):
    def can_handle(self, path: Path) -> bool: ...
    def extract(self, path: Path) -> list[ExtractedDocument]: ...
    def estimate_tokens(self, path: Path) -> int: ...

class ExtractedDocument(BaseModel):
    title: str
    content: str            # markdown
    metadata: dict          # original_path, page_range, conversation_id, etc.
    suggested_kind: EventKind  # raw_imported / human_chat / ai_chat / etc.
```

**Markdown / Text** (`brain/import_/extractors/markdown.py`)：

```python
def extract(self, path: Path) -> list[ExtractedDocument]:
    text = path.read_text(encoding="utf-8")
    title = path.stem
    # 如果文件 > 8000 字, 按段落分成多个 doc
    if len(text) <= 8000:
        return [ExtractedDocument(title=title, content=text, ...)]
    return split_by_heading(text, title)
```

**PDF** (`brain/import_/extractors/pdf.py`)：

```python
def extract(self, path: Path) -> list[ExtractedDocument]:
    import pypdf
    reader = pypdf.PdfReader(path)

    # 一份 PDF 拆成 N 个 doc, 默认按 5 页一组
    pages_per_doc = 5
    docs = []
    for i in range(0, len(reader.pages), pages_per_doc):
        chunk_pages = reader.pages[i:i+pages_per_doc]
        text = "\n\n".join(p.extract_text() for p in chunk_pages)
        if not text.strip():
            continue   # 扫描 PDF 跳过, OCR 是 Phase 3
        docs.append(ExtractedDocument(
            title=f"{path.stem} pp.{i+1}-{i+len(chunk_pages)}",
            content=text,
            metadata={"original_path": str(path), "page_range": [i+1, i+len(chunk_pages)]},
        ))
    return docs
```

**JSONL** (`brain/import_/extractors/jsonl.py`)：

支持两种格式（自动 detect）：

```python
# 格式 A: 一个 conversation 一行
{"id": "conv-1", "title": "...", "messages": [{"role": "user", "content": "..."}, ...]}

# 格式 B: 一条 message 一行
{"conversation_id": "conv-1", "role": "user", "content": "...", "timestamp": "..."}

def extract(self, path: Path) -> list[ExtractedDocument]:
    lines = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    if self._looks_like_format_a(lines):
        # 每行一个 conversation
        return [self._render_conversation(l) for l in lines]

    # 格式 B: groupby conversation_id
    grouped = defaultdict(list)
    for msg in lines:
        grouped[msg["conversation_id"]].append(msg)
    return [self._render_conversation_from_messages(msgs) for msgs in grouped.values()]

def _render_conversation(self, conv: dict) -> ExtractedDocument:
    # 拼成 markdown
    title = conv.get("title") or f"Conversation {conv.get('id', 'unknown')}"
    lines = []
    for msg in conv["messages"]:
        lines.append(f"**{msg['role']}**: {msg['content']}\n")
    return ExtractedDocument(
        title=title,
        content="\n".join(lines),
        metadata={"conversation_id": conv.get("id")},
        suggested_kind=EventKind.AI_CHAT,  # 大概率是 AI 对话
    )
```

### Cost Estimate

```python
def cost_estimate(files: list[ImportFile]) -> CostEstimate:
    by_kind = Counter(f.kind for f in files)

    # 估 extraction tokens (LLM ingest)
    # 经验: 1 个文件平均 input 2000 token + output 500 token = 2500 token
    # 这是 DeepSeek 价格, 实际看 config
    avg_input_per_file = 2000
    avg_output_per_file = 500
    extraction_tokens = sum(f.estimate_tokens() for f in files) + len(files) * avg_output_per_file

    extraction_cost = (
        extraction_tokens / 1_000_000
        * config.llm.unit_cost_per_1m_tokens   # 假设 config 里有
    )

    # 估 embedding tokens
    # 假设 reindex 平均每文件产生 5 个 chunk, 每 chunk 200 token
    embedding_tokens = len(files) * 5 * 200
    embedding_cost = (
        embedding_tokens / 1_000_000
        * config.embedding.unit_cost_per_1m_tokens
    )

    return CostEstimate(
        total_files=len(files),
        by_kind=dict(by_kind),
        estimated_extraction_tokens=extraction_tokens,
        estimated_embedding_tokens=embedding_tokens,
        estimated_extraction_usd=extraction_cost,
        estimated_embedding_usd=embedding_cost,
        estimated_total_usd=extraction_cost + embedding_cost,
    )
```

### Resume

```python
def resume_import():
    # 找 status='running' 或 'paused' 的最近 job
    job = find_latest_unfinished_job(conn)
    if not job:
        print("No unfinished import to resume")
        return

    # 从 import_files 里捡出 status='pending' 或 'failed' 的
    pending = list_pending_files(conn, job.id)

    print(f"Resuming job {job.id}: {len(pending)} files to process")
    process_files(pending, job)
```

---

## 改动 1: `mem ingest` (P2 微调)

只有一处加了 `auto_reindex`：

```
ingest():
    ... (原 Phase 1 流程) ...
    git_commit

    # === (P2) ===
    if config.import.auto_reindex and not dry_run:
        touched_pages = report.pages_touched
        if touched_pages:
            reindex(brain_root, page_filter=touched_pages, dry_run=False)
```

`mem ingest --no-auto-reindex` 跳过这步。

---

## 改动 2: `mem status` (P2 增强)

加几行输出:

```
Embedding coverage: 87% (134/154 chunks indexed)
Last reindex: 2026-04-30 14:23:11
Total embedding tokens: 84,231 (~$0.0017)
Total extraction tokens: 1,123,456 (~$3.21)
Total cost so far: ~$3.21
```

---

## 错误处理（继承 Phase 1 + Phase 2 新增）

| 场景 | 行为 |
|---|---|
| sqlite-vec 加载失败 | warn, `mem ask` 自动降级到 `--keyword-only`，`mem reindex` 报错退出 |
| embedding API 失败 | 单 chunk 跳过，写 `errors.log`，不阻塞 reindex |
| import extractor 失败 | 单文件标 failed，import 继续 |
| dimension 不匹配 | `mem reindex` 报错要求 `--force` |
| `mem ask` 在没 reindex 过时 | 自动降级到 keyword-only，warn "Run mem reindex for hybrid retrieval" |
| import 中用户 Ctrl-C | 当前文件标 failed/pending，job 状态置 paused，可 `--resume` |
