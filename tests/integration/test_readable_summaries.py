from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from brain.db.connection import connect
from brain.db.facts import add_fact
from brain.models import Fact, Frontmatter, Page
from brain.pages import write_page
from brain.pipeline.summaries import evidence_summary, propose_summary

NOW = datetime(2026, 9, 24, tzinfo=UTC)
EVENT = '01KQA8R9KVCG906A0203VYEQF7'


def page_and_facts(root, facts):
    page = Page(frontmatter=Frontmatter(type='project', slug='sample', title='Sample',
                                        created=NOW, updated=NOW),
                compiled_truth='(stub - waiting for more evidence)', timeline=[], sources=[])
    path = root / 'pages/projects/sample.md'
    write_page(path, page)
    with closing(connect(root / 'brain.db')) as conn, conn:
        for i, (predicate, value) in enumerate(facts):
            add_fact(conn, Fact(subject='sample', predicate=predicate, object=value,
                                object_type='literal', asserted_at=NOW + timedelta(seconds=i),
                                source_event=EVENT, confidence=0.95))
    return page, path


def test_summary_retains_direction_ahead_of_recent_operational_noise(brain_root):
    page, _ = page_and_facts(brain_root, [
        ('prefers', '小型方法库、两阶段生成、人工编辑'),
        ('runs_on', '本机 Python 服务'),
    ] + [('current_commit', f'commit-{n}') for n in range(12)])
    with closing(connect(brain_root / 'brain.db')) as conn:
        text = evidence_summary(conn, page)
    assert '小型方法库、两阶段生成、人工编辑' in text
    assert '运行于' in text
    assert 'runs_on' not in text
    assert text.index('小型方法库') < text.index('commit-')
    assert text.count('Sample:') == 0


def test_unrenderable_evidence_is_explicitly_a_placeholder(brain_root):
    page, _ = page_and_facts(brain_root, [('obscure_domain_metric_x', '12345')])
    with closing(connect(brain_root / 'brain.db')) as conn:
        text = evidence_summary(conn, page, 'zh')
    assert '占位' in text
    assert 'obscure_domain_metric_x' not in text


def test_local_summary_preview_writes_no_page_or_review(brain_root):
    page_and_facts(brain_root, [('uses', 'SQLite')])
    before = {p.relative_to(brain_root): p.read_bytes() for p in brain_root.rglob('*') if p.is_file()}
    result = propose_summary(brain_root, 'sample', dry_run=True)
    assert 'SQLite' in result['compiled_truth']
    assert result['dry_run'] is True
    after = {p.relative_to(brain_root): p.read_bytes() for p in brain_root.rglob('*') if p.is_file()}
    assert before == after


def test_provider_draft_receives_accepted_facts_even_without_timeline(brain_root, monkeypatch):
    page_and_facts(brain_root, [('prefers', 'human editing'), ('uses', 'SQLite')])
    calls = []

    def rewrite(timeline, current_truth):
        calls.extend(entry.description for entry in timeline)
        return 'The project keeps a human editor and uses SQLite.'

    monkeypatch.setattr('brain.llm.client.rewrite_compiled_truth', rewrite)
    result = propose_summary(brain_root, 'sample', provider=True)
    assert result['review_file']
    assert any('human editing' in value for value in calls)
    assert any('SQLite' in value for value in calls)


def test_dry_run_never_calls_provider(brain_root, monkeypatch):
    page_and_facts(brain_root, [('uses', 'SQLite')])
    monkeypatch.setattr('brain.llm.client.rewrite_compiled_truth', lambda *args: pytest.fail('provider called'))
    from brain.exceptions import BrainError
    with pytest.raises(BrainError, match='local'):
        propose_summary(brain_root, 'sample', provider=True, dry_run=True)


def test_provider_draft_checks_fact_sources_missing_from_page(brain_root, monkeypatch):
    from brain.exceptions import BrainError

    page_and_facts(brain_root, [('uses', 'private evidence')])
    (brain_root / 'laundry/private.md').write_text('privacy: local-only\n\nPrivate note.', encoding='utf-8')
    with closing(connect(brain_root / 'brain.db')) as conn, conn:
        conn.execute("UPDATE facts SET source_ref = 'laundry/private.md'")
    monkeypatch.setattr('brain.llm.client.rewrite_compiled_truth', lambda *args: pytest.fail('provider called'))
    with pytest.raises(BrainError, match='local-only'):
        propose_summary(brain_root, 'sample', provider=True)


def test_footnote_counts_facts_beyond_the_group_limit(brain_root):
    page, _ = page_and_facts(brain_root, [('uses', f'工具{n}') for n in range(5)])
    with closing(connect(brain_root / 'brain.db')) as conn:
        text = evidence_summary(conn, page, 'zh')
    assert text.count('使用工具') == 3
    assert '另有 2 条事实未列入本地摘要' in text


def test_chinese_labels_are_spaced_from_latin_values(brain_root):
    page, _ = page_and_facts(brain_root, [('uses', 'SQLite'), ('frontend_framework', 'React')])
    with closing(connect(brain_root / 'brain.db')) as conn:
        text = evidence_summary(conn, page, 'zh')
    assert '使用 SQLite' in text
    assert '前端使用 React' in text


def test_approved_summary_is_recorded_in_the_ledger(brain_root):
    import json

    from brain.pipeline.review import apply_pending

    page_and_facts(brain_root, [('uses', 'SQLite')])
    draft = brain_root / propose_summary(brain_root, 'sample')['review_file']
    draft.write_text(draft.read_text(encoding='utf-8').replace('[ ] approve', '[x] approve'),
                     encoding='utf-8')
    assert apply_pending(brain_root).applied == 1
    events = [json.loads(line) for line in
              (brain_root / 'events.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    decided = [e for e in events if e['kind'] == 'review_decided']
    assert decided and decided[-1]['metadata']['kind'] == 'summary_refresh'
    assert decided[-1]['metadata']['decision'] == 'approve'
