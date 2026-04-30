# Phase 2 Spec Bundle — How to Apply

这个目录是 **Phase 2 的 spec 增量**，不是新建一个项目。所有内容会合并进现有的 `brainmem/docs/` 目录。

## 文件说明

每个文件对应 brainmem 仓库里的一个 doc。**操作方式都是"覆盖原文件"或"追加段落"**，不要新建独立的 phase-2 子目录——单一 doc 源是 Phase 2 一开始就定的原则，避免 Phase 1 / Phase 2 之间再出现 drift。

| 本目录文件 | 对应仓库路径 | 操作 |
|---|---|---|
| `SPEC.md` | `docs/SPEC.md` | **覆盖** (新增 Phase 2 范围段、刷新 phase-1 状态、更新阅读顺序) |
| `architecture.md` | `docs/architecture.md` | **覆盖** (Phase 1 部分保持等价 + 新增 Phase 2 的 retrieval / import / embedding 三段) |
| `data-model.md` | `docs/data-model.md` | **覆盖** (新增 embeddings / embedding_index / import_jobs 三张表 + content_hash 字段) |
| `pipeline.md` | `docs/pipeline.md` | **覆盖** (新增 reindex / hybrid ask / bulk import 三节算法) |
| `cli.md` | `docs/cli.md` | **覆盖** (新增 `mem import / reindex / cost-estimate` 命令 + `mem ask` 默认行为变更) |
| `tech-stack.md` | `docs/tech-stack.md` | **覆盖** (依赖列表新增 sqlite-vec / pypdf / tiktoken；移除过时的 OpenAI 依赖禁止段落) |
| `phase-2-tasks.md` | `docs/phase-2-tasks.md` | **新建** (Task 17–25 的有序执行清单) |

## 给 Codex 的引导话术

把 spec 应用到仓库前，先让 Codex 这样开工:

> 我要在现有 brainmem 仓库里实施 Phase 2。先做以下事情，然后停下来等我确认：
> 1. 用 `phase-2-spec/` 目录里的 6 个文件覆盖 `docs/` 下的同名文件
> 2. 把 `phase-2-spec/phase-2-tasks.md` 复制到 `docs/phase-2-tasks.md`
> 3. 跑 `pytest && ruff check .` 确认覆盖文档不影响 Phase 1 测试
> 4. 提交一个 docs-only commit: `docs: import phase 2 spec`
> 5. 然后等我说"开始 Task 17"再动代码

这样 spec 应用和代码实现是两个分离的步骤，万一文档需要再改还有缓冲。

## Phase 2 范围一句话

**Hybrid retrieval (vector + keyword + SQL + RRF) + bulk import (.md / .txt / .pdf / .jsonl) + sqlite-vec embedding store**, 默认 `mem ask` 走 hybrid, 旧 keyword-only 通过 flag 保留。

## 非目标 (本 Phase 不做)

- procedural memory / rules pages → Phase 3
- web UI / TUI
- 后台 scheduler / 定时任务
- graph database (neo4j 之类)
- OCR (扫描 PDF / 图片)
- HTML / EPUB / URL 直抓
- 多 brain federation
