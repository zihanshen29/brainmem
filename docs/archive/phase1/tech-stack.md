# Tech Stack & Project Conventions

本文件锁定技术选型和项目惯例。Codex 不要替换 spec 中指定的库，除非该库无法在 Windows 上正常工作（这种情况标注后再选替代）。

## 1. 运行环境

- **Python 3.11**（不要用 3.12+ 因为部分依赖兼容性，不要用 3.10 因为缺少某些 typing 特性）
- **Windows 10/11 桌面**为主要目标平台。代码也要在 macOS / Linux 跑，但首要保证 Windows 跑通。
- 用户从 PowerShell 或 Windows Terminal 运行命令。

## 2. 依赖（锁定）

完整 `pyproject.toml`（项目根）：

```toml
[project]
name = "brain"
version = "0.1.0"
description = "Personal memory system with markdown wiki + SQLite backbone"
requires-python = ">=3.11,<3.12"
dependencies = [
    "typer>=0.12.0",                 # CLI 框架
    "pydantic>=2.6.0",               # schema 校验
    "anthropic>=0.40.0",             # Anthropic provider
    "openai>=1.66.0",                # OpenAI SDK and OpenAI-compatible providers such as DeepSeek
    "python-frontmatter>=1.1.0",     # 解析 markdown frontmatter
    "python-ulid>=2.2.0",            # ULID
    "rich>=13.7.0",                  # 终端格式化
    "tomli-w>=1.0.0",                # 写 toml (3.11 stdlib 只能读)
    "GitPython>=3.1.40",             # git 操作
    "platformdirs>=4.2.0",           # 跨平台路径
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

**Phase 1 暂不加入** 的依赖（这些是 Phase 2 才需要的）。`openai` 已经是 Phase 1 依赖，因为 DeepSeek 和其他 OpenAI-compatible provider 通过 OpenAI SDK 调用；保留它：
- chromadb / qdrant-client / faiss-cpu
- networkx / neo4j（Phase 1 不上图谱）
- sentence-transformers
- celery / redis / rq
- fastapi / flask / streamlit

## 3. 项目结构

```
brainmem/                             # 仓库根
├── pyproject.toml
├── README.md
├── README.zh-CN.md
├── docs/
├── .gitignore
├── .ruff.toml
│
├── src/
│   └── brain/
│       ├── __init__.py
│       ├── config.py                # 读 config.toml, env vars
│       ├── paths.py                 # ~/brain 各路径解析
│       │
│       ├── models/                  # Pydantic 模型
│       │   ├── __init__.py
│       │   ├── event.py
│       │   ├── page.py
│       │   ├── fact.py
│       │   └── entity.py
│       │
│       ├── db/                      # SQLite 层
│       │   ├── __init__.py
│       │   ├── connection.py
│       │   ├── migrations.py
│       │   ├── migrations/
│       │   │   └── 0001_baseline.sql
│       │   ├── entities.py          # entity / alias 操作
│       │   ├── facts.py
│       │   ├── backlinks.py
│       │   └── tier.py
│       │
│       ├── ledger/                  # events.jsonl 读写
│       │   ├── __init__.py
│       │   ├── reader.py
│       │   └── writer.py
│       │
│       ├── pages/                   # markdown page 读写
│       │   ├── __init__.py
│       │   ├── parser.py            # frontmatter + section split
│       │   ├── writer.py
│       │   ├── timeline.py
│       │   └── index.py             # 维护 index.md / log.md
│       │
│       ├── pipeline/                # 业务管线
│       │   ├── __init__.py
│       │   ├── ingest.py
│       │   ├── signal_detect.py    # LLM 抽取
│       │   ├── resolve.py           # entity 解析
│       │   ├── tier.py              # tier 决策
│       │   ├── conflict.py
│       │   ├── autolink.py
│       │   ├── review.py
│       │   ├── lint.py
│       │   ├── ask.py
│       │   ├── promote_chat.py
│       │   └── rebuild.py
│       │
│       ├── llm/                     # LLM provider 调用封装
│       │   ├── __init__.py
│       │   ├── client.py
│       │   └── prompts.py
│       │
│       ├── git_ops.py               # GitPython 封装
│       │
│       └── cli/                     # CLI 入口
│           ├── __init__.py
│           ├── main.py              # typer app
│           ├── init.py              # mem init
│           ├── ingest.py
│           ├── review.py
│           ├── lint.py
│           ├── ask.py
│           ├── promote_chat.py
│           ├── rebuild.py
│           ├── status.py
│           ├── entity.py
│           └── capture.py
│
└── tests/
    ├── conftest.py                  # 临时 brain root fixture
    ├── unit/
    │   ├── test_models.py
    │   ├── test_pages_parser.py
    │   ├── test_resolve.py
    │   └── test_autolink.py
    └── integration/
        ├── test_init.py
        ├── test_ingest.py
        ├── test_review.py
        └── test_ask.py
```

## 4. 路径处理（Windows 重点）

### 4a. 永远用 `pathlib.Path`

```python
from pathlib import Path

# 对：
brain_root = Path.home() / "brain"
page_path = brain_root / "pages" / "entities" / "zhang-san.md"

# 错：
brain_root = "~/brain"  # 不要用字符串拼接，不要直接传 ~
brain_root = "/home/zihan/brain"  # 不要硬编码 Linux 路径
```

### 4b. 用 `platformdirs` 做跨平台默认

```python
from platformdirs import user_data_dir
default_brain_root = Path.home() / "brain"  # 用户家目录下的 brain
```

家目录在 Windows 是 `C:\Users\<name>`，Mac 是 `/Users/<name>`，Linux 是 `/home/<name>`。`Path.home()` 自动处理。

### 4c. 文件读写显式编码

```python
# 对：
content = page_path.read_text(encoding="utf-8")
page_path.write_text(content, encoding="utf-8", newline="\n")

# 错：
content = page_path.read_text()  # Windows 默认可能是 cp1252
```

写入时显式 `newline="\n"` 避免 Windows 自动加 `\r\n`。

### 4d. 配置 `.gitattributes` 强制 LF

`mem init` 时写入：

```
* text=auto eol=lf
*.py text eol=lf
*.md text eol=lf
*.toml text eol=lf
*.sql text eol=lf
*.jsonl text eol=lf
```

## 5. LLM provider and API keys

默认 provider 是 DeepSeek V4。实现支持三条路径：

- `[deepseek]`：默认路径，使用 OpenAI SDK 的 chat completions 接口和可配置 `base_url`，用于 DeepSeek / OpenAI-compatible provider。
- `[openai]`：OpenAI 官方接口，使用 OpenAI SDK responses 接口。
- `[anthropic]`：Anthropic 官方接口，用于 Claude 模型兼容。

Provider 选择优先级为：`deepseek` > `openai` > `anthropic`。`mem init` 写入 DeepSeek 默认配置；没有 `config.toml` 时，LLM client 也回退到 DeepSeek 默认环境变量和模型名。

**不要硬编码 API key**。从环境变量读，`config.toml` 里只放变量名：

```toml
[deepseek]
api_key_env = "DEEPSEEK_API_KEY"
base_url = "https://api.deepseek.com"
model = "deepseek-v4-pro"
fast_model = "deepseek-v4-flash"

[openai]  # optional: OpenAI official API
api_key_env = "OPENAI_API_KEY"
model = "gpt-5.5"
fast_model = "gpt-5.4-mini"

[anthropic]  # optional: Anthropic official API
api_key_env = "ANTHROPIC_API_KEY"
model = "claude-3-5-haiku-latest"
fast_model = "claude-3-5-haiku-latest"
```

环境变量边界：

- DeepSeek 默认读取 `DEEPSEEK_API_KEY`，模型可用 `BRAIN_DEEPSEEK_MODEL` / `DEEPSEEK_MODEL` 和 `BRAIN_DEEPSEEK_FAST_MODEL` / `DEEPSEEK_FAST_MODEL` 覆盖，base URL 可用 `BRAIN_DEEPSEEK_BASE_URL` / `DEEPSEEK_BASE_URL` 覆盖。
- OpenAI 读取 `OPENAI_API_KEY`，模型可用 `BRAIN_OPENAI_MODEL` / `OPENAI_MODEL` 和 `BRAIN_OPENAI_FAST_MODEL` / `OPENAI_FAST_MODEL` 覆盖。
- Anthropic 读取 `ANTHROPIC_API_KEY`，模型可用 `BRAIN_ANTHROPIC_MODEL` / `ANTHROPIC_MODEL` 和 `BRAIN_ANTHROPIC_FAST_MODEL` / `ANTHROPIC_FAST_MODEL` 覆盖。
- `BRAIN_CONFIG` 可指向显式配置文件；否则当前工作目录下的 `config.toml` 优先于默认回退。

```python
import os
key = os.environ.get(config.deepseek.api_key_env)
if not key:
    raise click.UsageError(
        f"Set ${config.deepseek.api_key_env} before running LLM-backed commands."
    )
```

`mem init` 完成后提示用户：

```
Set your API key in PowerShell:
  $env:DEEPSEEK_API_KEY = "<your-deepseek-api-key>"
Or persist:
  setx DEEPSEEK_API_KEY "<your-deepseek-api-key>"
```

Plain `mem ask` is local retrieval and does not require an API key. `mem ingest`, `mem ask --explain`, `mem promote-chat`, tier compiled-truth rewrites, and forced page rewrites may call the configured provider.

## 6. 日志

用 stdlib `logging`，不要装 loguru。Logger 名字按模块层级 (`brain.pipeline.ingest`)。

默认输出到 `~/brain/brain.log`（rotating，10 MB × 3）。CLI `--verbose` 时也复制到 stderr。

```python
import logging
from logging.handlers import RotatingFileHandler

def setup_logging(brain_root: Path, verbose: bool):
    log_path = brain_root / "brain.log"
    handlers = [RotatingFileHandler(log_path, maxBytes=10*1024*1024, backupCount=3)]
    if verbose:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s: %(message)s",
        handlers=handlers,
    )
```

## 7. 测试

### 7a. 范围

只测**关键路径**：

- `models/` 全部（Pydantic 校验）
- `pages/parser.py` —— frontmatter 和 section 解析
- `pipeline/resolve.py` —— entity 解析的边角情况
- `pipeline/autolink.py` —— backlink 提取
- `pipeline/conflict.py` —— 冲突检测
- `pipeline/ingest.py` —— 端到端 ingest（mock LLM）
- `db/` 操作的事务正确性
- `cli/init.py` —— 完整 init 流程
- `cli/review.py` —— apply 模式

不测：

- `llm/client.py`（mock 即可）
- `cli/main.py` 的 typer 路由（手测）
- `git_ops.py` 单独测（被其他测试间接覆盖）

### 7b. 测试基础设施

**conftest.py 提供 `tmp_brain` fixture**：

```python
import pytest
from pathlib import Path
from brain.cli.init import init_brain

@pytest.fixture
def tmp_brain(tmp_path: Path):
    """临时 brain 根目录，每个测试独立。"""
    root = tmp_path / "brain"
    init_brain(root, force=False)
    return root
```

**Mock LLM 调用**：

```python
@pytest.fixture
def mock_llm(monkeypatch):
    from brain.llm import client

    def fake_extract(text: str, schema):
        # 返回固定测试数据
        return {"entities": [...], "facts": [...]}

    monkeypatch.setattr(client, "extract_signal", fake_extract)
```

### 7c. 命令

```
pytest                           # 全部
pytest tests/unit                # 只单元测试
pytest -k "ingest"               # 名字匹配
pytest --cov=brain               # 带覆盖率
```

CI 在 GitHub Actions（如果用户想加）跑 `pytest && ruff check && mypy src/brain`。

### 7d. 覆盖率目标

关键路径模块（上面列的）≥ 80%。其他无要求。

## 8. 代码规范

### 8a. ruff 配置 (`.ruff.toml`)

```toml
line-length = 100
target-version = "py311"

[lint]
select = [
    "E", "W",        # pycodestyle
    "F",             # pyflakes
    "I",             # isort
    "B",             # bugbear
    "UP",            # pyupgrade
    "SIM",           # simplify
    "TID",           # tidy imports
    "RUF",           # ruff-specific
]
ignore = ["E501"]    # ruff format 处理行长

[format]
quote-style = "double"
```

### 8b. 类型注解

所有公共函数加类型注解。Pydantic 模型代替 `dataclass`（已经在依赖里）。

```python
# 对：
def resolve_entity(name: str, conn: sqlite3.Connection) -> Entity | None: ...

# 错：
def resolve_entity(name, conn): ...
```

### 8c. Docstrings

公共函数和类用 Google 风格 docstring：

```python
def ingest(brain_root: Path, source: Source = "all", dry_run: bool = False) -> IngestReport:
    """Run the ingest pipeline.

    Args:
        brain_root: Path to brain repository root.
        source: Which sources to process.
        dry_run: If True, don't write changes.

    Returns:
        IngestReport summarizing what was processed.

    Raises:
        IngestError: If a non-recoverable error occurred.
    """
```

私有函数（`_` 前缀）一行注释即可。

### 8d. 错误处理

定义自己的异常层级：

```python
# brain/exceptions.py

class BrainError(Exception):
    """Base for all brain-specific errors."""

class ConfigError(BrainError): ...
class DBError(BrainError): ...
class IngestError(BrainError): ...
class LLMError(BrainError): ...
class GitError(BrainError): ...
```

CLI 层捕获 `BrainError` 显示友好信息，其他异常打 traceback。

## 9. Git 操作

用 `GitPython`，但只用最基础的功能：

```python
from git import Repo

def commit(brain_root: Path, message: str, paths: list[Path] | None = None) -> str:
    repo = Repo(brain_root)
    if paths:
        repo.index.add([str(p) for p in paths])
    else:
        repo.git.add(A=True)
    if not repo.is_dirty(untracked_files=True):
        return ""  # nothing to commit
    commit = repo.index.commit(message)
    return commit.hexsha[:8]
```

不要用 GitPython 做复杂操作（rebase / merge），那些让用户自己处理。

## 10. 依赖锁定

用 `pip-compile` 或 `uv pip compile` 生成 `requirements.lock`，CI 安装时用 lock 文件。

第一次安装：

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
```

## 11. README 内容

`README.md` 至少包含：

1. 一句话简介
2. 安装步骤（Windows + macOS/Linux 各一段）
3. 快速开始（init → capture → ingest → ask）
4. 目录结构图（简版）
5. 链接到 spec/ 各文件做更详细说明

## 12. 不做的事

显式声明 Phase 1 **不**做：

- Web UI、TUI（用 `rich` 美化 stdout 即可）
- 后台守护进程
- 跨设备同步（用户用 git remote 自己解决）
- 加密
- 多用户支持
- 插件系统
- 国际化（界面字符串中英混排即可）
