# Phase 2 Spec Bundle — Application Record

ARCHIVED: applied in commit `a29e57059e46ab333e51e25c0f741c6b89e16e9e` on 2026-04-30. This file is preserved for historical context; it is not a pending implementation checklist.

这个记录描述 Phase 2 spec 增量在 `brainmem/docs/` 中的应用方式。

## 文件说明

每个文件对应 brainmem 仓库里的一个 doc。应用时采用"覆盖原文件"或"追加段落"方式，保持单一 doc 源。

| 本目录文件 | 对应仓库路径 | 操作 |
|---|---|---|
| `SPEC.md` | `docs/SPEC.md` | **覆盖** (新增 Phase 2 范围段、刷新 phase-1 状态、更新阅读顺序) |
| `architecture.md` | `docs/architecture.md` | **覆盖** (Phase 1 部分保持等价 + 新增 Phase 2 的 retrieval / import / embedding 三段) |
| `data-model.md` | `docs/data-model.md` | **覆盖** (新增 embeddings / embedding_index / import_jobs 三张表 + content_hash 字段) |
| `pipeline.md` | `docs/pipeline.md` | **覆盖** (新增 reindex / hybrid ask / bulk import 三节算法) |
| `cli.md` | `docs/cli.md` | **覆盖** (新增 `mem import / reindex / cost-estimate` 命令 + `mem ask` 默认行为变更) |
| `tech-stack.md` | `docs/tech-stack.md` | **覆盖** (依赖列表新增 sqlite-vec / pypdf / tiktoken；移除过时的 OpenAI 依赖禁止段落) |
| `phase-2-tasks.md` | `docs/archive/phase2/phase-2-tasks.md` | **归档** (Phase 2 历史执行清单) |

## 历史应用流程

Phase 2 spec 应用和代码实现分为两个步骤：

1. 用 `phase-2-spec/` 目录里的 6 个文件覆盖 `docs/` 下的同名文件。
2. 将 Phase 2 执行清单作为历史实施说明归档。
3. 运行 `pytest && ruff check .` 确认文档覆盖不影响 Phase 1 测试。
4. 提交 docs-only commit: `docs: import phase 2 spec`。

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
