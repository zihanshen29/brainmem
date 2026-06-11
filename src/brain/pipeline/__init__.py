from brain.pipeline.ask import AskModeTrace, AskPageSummary, AskResult, ask
from brain.pipeline.autolink import extract_backlinks
from brain.pipeline.conflict import Decision, classify_fact
from brain.pipeline.entity_prune import EntityPruneReport, prune_stub_entities
from brain.pipeline.ingest import IngestReport, ingest
from brain.pipeline.lint import (
    LintIssue,
    LintKind,
    LintRunReport,
    lint_citations,
    lint_contradictions,
    lint_orphans,
    lint_stale,
    run_lint,
)
from brain.pipeline.procedure import (
    ProcedureReport,
    ProcedureRunResult,
    create_procedure,
    promote_procedure,
    run_procedure,
)
from brain.pipeline.promote_chat import PromoteChatReport, promote_chat
from brain.pipeline.rebuild import (
    RebuildReport,
    rebuild_backlinks,
    rebuild_db,
    rebuild_derived,
    rebuild_index,
    rebuild_pages,
)
from brain.pipeline.resolve import resolve_entity
from brain.pipeline.review import (
    ReviewAction,
    ReviewApplyReport,
    ReviewDecision,
    ReviewItem,
    ReviewKind,
    ReviewQuarantineReport,
    ReviewStatus,
    apply_decision,
    apply_pending,
    list_pending,
    parse_review_file,
    quarantine_invalid_pending,
    resolve_review_path,
)
from brain.pipeline.signal_detect import SignalEntity, SignalExtraction, detect_signal
from brain.pipeline.tier import TierProposal, check_tier_upgrade


def append_working(*args, **kwargs):
    from brain.pipeline.scratch import append_working as _append_working

    return _append_working(*args, **kwargs)


def rebuild_snapshot(*args, **kwargs):
    from brain.pipeline.scratch import rebuild_snapshot as _rebuild_snapshot

    return _rebuild_snapshot(*args, **kwargs)

__all__ = [
    "AskModeTrace",
    "AskPageSummary",
    "AskResult",
    "Decision",
    "EntityPruneReport",
    "IngestReport",
    "LintIssue",
    "LintKind",
    "LintRunReport",
    "ProcedureReport",
    "ProcedureRunResult",
    "PromoteChatReport",
    "RebuildReport",
    "ReviewAction",
    "ReviewApplyReport",
    "ReviewDecision",
    "ReviewItem",
    "ReviewKind",
    "ReviewQuarantineReport",
    "ReviewStatus",
    "SignalEntity",
    "SignalExtraction",
    "TierProposal",
    "append_working",
    "apply_decision",
    "apply_pending",
    "ask",
    "check_tier_upgrade",
    "classify_fact",
    "create_procedure",
    "detect_signal",
    "extract_backlinks",
    "ingest",
    "lint_citations",
    "lint_contradictions",
    "lint_orphans",
    "lint_stale",
    "list_pending",
    "parse_review_file",
    "promote_chat",
    "promote_procedure",
    "prune_stub_entities",
    "quarantine_invalid_pending",
    "rebuild_backlinks",
    "rebuild_db",
    "rebuild_derived",
    "rebuild_index",
    "rebuild_pages",
    "rebuild_snapshot",
    "resolve_entity",
    "resolve_review_path",
    "run_lint",
    "run_procedure",
]
