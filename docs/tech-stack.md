# Tech Stack & Project Conventions

本文件锁定技术选型和项目惯例。Phase 2 在 Phase 1 基础上**增量添加**，不替换原有依赖。

## 1. 运行环境（无变化）

- **Python 3.11**
- **Windows 10/11** 主要目标平台
- 用户从 PowerShell / Windows Terminal 跑命令

## 2. 依赖（Phase 2 增量）

完整 `pyproject.toml`（标注 P2 新增）：

```toml
[project]
name = "brain"
version = "0.2.0"  # bump
description = "Personal memory system with markdown wiki + SQLite backbone"
requires-python = ">=3.11,<3.12"
dependencies = [
    # Phase 1
    "typer>=0.12.0",
    "pydantic>=2.6.0",
    "anthropic>=0.40.0",
    "openai>=1.66.0",
    "python-frontmatter>=1.1.0",
    "python-ulid>=2.2.0",
    "rich>=13.7.0",
    "tomli-w>=1.0.0",
    "GitPython>=3.1.40",
    "platformdirs>=4.2.0",

    # Phase 2 新增
    "sqlite-vec>=0.1.6",        # vec0 虚表扩展, 单文件 SQLite 内嵌向量
    "pypdf>=4.3.0",             # PDF 文本提取 (不含 OCR)
    "tiktoken>=0.7.0",          # token 计数 (用于 cost estimate)
    "jieba>=0.42.1",            # 中文分词 (BM25 用)
    "rank-bm25>=0.2.2",         # BM25 评分库 (避免自己实现)
]

[project.scripts]
mem = "brain.cli.main:app"

[build-system]
requires = ["setuptools>=68.0", "wheel"]
build-backend = "setuptools.build_meta"

[tool.setuptools.packages.find]
where = ["src"]

[tool.setuptools.package-data]
"brain.db" = ["migrations/*.sql"]

[project.optional-dependencies]
dev = [
    "pytest>=8.0.0",
    "pytest-cov>=4.1.0",
    "ruff>=0.5.0",
    "mypy>=1.10.0",
]
```

### Phase 2 不引入

明确说明 Phase 2 **仍然不要** 加这些（节制依赖）：

- `chromadb` / `qdrant-client` / `faiss-cpu` —— 用 sqlite-vec 替代
- `sentence-transformers` / `torch` —— 不上本地 embedding 模型
- `langchain` / `llama-index` —— 自己写检索逻辑，不用框架
- `celery` / `redis` —— 没后台进程
- `fastapi` / `streamlit` —— 没 UI

## 3. 项目结构（Phase 2 增量）

Phase 1 结构基本保留，新增一个 `import_/` 包和几个文件：

```
src/brain/
├── ... (Phase 1 全部不变) ...
│
├── llm/
│   ├── __init__.py
│   ├── client.py             # Phase 1 (LLM client 抽象)
│   ├── prompts.py
│   └── embedding.py          # === (P2) Embedding client 抽象 ===
│
├── db/
│   ├── ... (Phase 1) ...
│   ├── migrations/
│   │   ├── 0001_baseline.sql
│   │   └── 0002_phase2.sql   # === (P2) ===
│   ├── embeddings.py         # === (P2) embedding_index CRUD ===
│   └── stats.py              # === (P2) stats 表 CRUD ===
│
├── pipeline/
│   ├── ... (Phase 1) ...
│   ├── reindex.py            # === (P2) reindex 算法 ===
│   ├── ask.py                # === (P2) 重写为 hybrid ===
│   ├── retrieval/            # === (P2) 三路召回 + RRF ===
│   │   ├── __init__.py
│   │   ├── vector.py
│   │   ├── keyword.py
│   │   ├── sql_match.py
│   │   ├── rrf.py
│   │   ├── classifier.py     # query classifier (规则版)
│   │   └── sql_direct.py     # 结构化短路
│   └── chunking.py           # === (P2) split_page_into_chunks ===
│
├── import_/                  # === (P2) 整个包 ===
│   ├── __init__.py
│   ├── importer.py           # 主流程
│   ├── discovery.py          # 文件扫描
│   ├── cost.py               # cost estimate
│   ├── jobs.py               # import_jobs / import_files CRUD
│   └── extractors/
│       ├── __init__.py
│       ├── base.py           # Protocol + ExtractedDocument
│       ├── markdown.py
│       ├── pdf.py
│       └── jsonl.py
│
└── cli/
    ├── ... (Phase 1) ...
    ├── reindex.py            # === (P2) ===
    ├── import_.py            # === (P2) (注意下划线避开 keyword) ===
    └── cost_estimate.py      # === (P2) ===
```

注意 `import_` 包名后缀下划线——`import` 是 Python keyword，不能直接用。CLI 模块 `cli/import_.py` 同理。

## 4. 路径处理（无变化）

继续使用 `pathlib.Path`、`Path.home()`、显式 `encoding="utf-8"`、`.gitattributes` 强制 LF。

## 5. API key（Phase 2 增量）

`config.toml` 现在有四个 provider 段：

```toml
[anthropic]
api_key_env = "ANTHROPIC_API_KEY"

[openai]
api_key_env = "OPENAI_API_KEY"

[deepseek]
api_key_env = "DEEPSEEK_API_KEY"

[embedding]
provider = "openai_compatible"
base_url = "https://api.openai.com/v1"
model = "text-embedding-3-small"
dimension = 1536
api_key_env = "OPENAI_API_KEY"   # 默认借用 OPENAI key；国产兼容服务可改成 EMBEDDING_API_KEY
```

**注意一个微妙点**：`[embedding]` 默认用 OpenAI 官方 embedding API，所以 `api_key_env` 一般指向 `OPENAI_API_KEY`。如果使用阿里百炼、硅基流动、智谱等 OpenAI-compatible embedding 服务，把 `base_url` / `model` / `api_key_env` 改成对应值即可。无论用哪家，返回向量维度必须等于 `dimension`。

`mem init` 完成后提示：

```
Phase 2 requires an embedding provider. Set:
  $env:OPENAI_API_KEY = "sk-..."
Or persist:
  setx OPENAI_API_KEY "sk-..."

For an OpenAI-compatible embedding provider, set:
  $env:EMBEDDING_API_KEY = "..."
and edit config.toml [embedding].base_url / model / api_key_env.
```

## 6. 日志（无变化）

继续用 stdlib logging + RotatingFileHandler，路径 `~/brain/brain.log`。

Phase 2 给 import 单独写一个文件 `~/brain/imports/<job-id>/errors.log` 装文件级失败信息——避免污染主日志。

## 7. 测试（Phase 2 增量）

### 7a. 范围（Phase 1 + Phase 2）

Phase 1 关键路径继续要测。Phase 2 新增必测：

- `pipeline/chunking.py` — split_page_into_chunks 各种 page 形态
- `pipeline/retrieval/rrf.py` — RRF 公式正确性
- `pipeline/retrieval/classifier.py` — 结构化 vs 开放查询识别
- `pipeline/reindex.py` — 增量逻辑（content_hash 一致跳过）
- `db/embeddings.py` — sqlite-vec 加载 + upsert + delete + query
- `import_/importer.py` — 端到端 import (mock LLM 和 embedding API)
- `import_/extractors/markdown.py` — 大文件按 heading 切
- `import_/extractors/pdf.py` — 文本 PDF + 跳过扫描 PDF
- `import_/extractors/jsonl.py` — 格式 A 和 B 自动 detect

### 7b. Mock embedding API

新加 fixture：

```python
# tests/conftest.py

@pytest.fixture
def mock_embedding(monkeypatch):
    """deterministic fake embeddings, hash-based."""
    def fake_embed(texts, model="test-model"):
        # 用 sha256 + 取前 1536 byte 当向量, 保证内容相同向量相同
        return [_text_to_vector(t, dim=1536) for t in texts]

    from brain.llm import embedding
    monkeypatch.setattr(embedding.OpenAICompatibleEmbeddingClient, "embed", fake_embed)
```

### 7c. sqlite-vec 在测试里

测试用 in-memory SQLite：

```python
@pytest.fixture
def vec_conn():
    conn = sqlite3.connect(":memory:")
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.executescript(BASELINE_SQL)
    conn.executescript(PHASE2_SQL)
    yield conn
    conn.close()
```

### 7d. 覆盖率目标

继续 ≥ 80% 关键路径模块。Phase 2 新增模块同样标准。

## 8. 代码规范（无变化）

ruff 配置、type 注解、docstring、错误层级保持 Phase 1 约定。

Phase 2 新增异常：

```python
# brain/exceptions.py 增量

class EmbeddingError(BrainError): ...
class ImportError(BrainError): ...     # 注意名字冲突: 用 BulkImportError
class BulkImportError(BrainError): ...
```

避开 `ImportError` 这个 builtin，新异常叫 `BulkImportError`。

## 9. Git 操作（无变化）

GitPython 继续。Phase 2 的两处变化：

- `mem reindex` 默认不 commit（embedding 是派生数据，不影响 git）。`--commit` 显式开启。
- `mem import` 按 batch commit，message: `import: batch K/N for job <id> (M files)`

## 10. 依赖锁定

Phase 2 升级时：

```powershell
pip install -e ".[dev]"
pip-compile pyproject.toml -o requirements.lock
```

## 11. README 更新

Phase 2 文档需要在 README 里加：

1. Phase 2 features 段落（hybrid retrieval / bulk import / cost-aware）
2. Quick start 加 `mem reindex` 步骤
3. 多 provider 说明：DeepSeek 默认 + OpenAI 用于 embedding + 三 provider 混搭
4. 一个 `mem import ~/Documents/notes` 的实际示例

## 12. 不做的事 (Phase 2 仍然不做)

显式声明 Phase 2 **不**做：

- Web UI、TUI（rich 美化 stdout 即可）
- 后台守护进程 / 定时任务
- 跨设备同步（git remote 自己解决）
- 加密
- 多用户支持
- 插件系统
- OCR
- HTML / EPUB / URL 直抓
- Procedural memory / rules pages
- LLM-driven query classifier（规则够用）

## 13. 升级路径（Phase 1 → Phase 2）

用户运行 `pip install --upgrade brain`（或 `git pull && pip install -e .`），然后第一次跑任意命令：

1. `migrations.py` 检测到 `user_version=1`，自动跑 `0002_phase2.sql`
2. `user_version` 升到 2
3. CLI 提示：`Phase 2 schema applied. Run mem reindex to enable hybrid retrieval.`
4. 用户跑 `mem reindex`，第一次会 embed 所有 page（可能要几分钟，看页面数）
5. 之后 `mem ask` 默认走 hybrid

升级**不会触动**任何 markdown 文件、events.jsonl、Phase 1 的表数据。完全增量。
