import json
from contextlib import closing
from datetime import UTC, datetime

import pytest
from git import Repo
from typer.testing import CliRunner

from brain.backup import create_backup, source_manifest
from brain.cli.main import app
from brain.db.connection import connect
from brain.db.entities import add_alias, upsert_entity
from brain.db.facts import add_fact
from brain.exceptions import BrainError
from brain.models import Entity, EntityAliasSource, Fact, Frontmatter, Page
from brain.pages import write_page
from brain.pipeline.entity_literalize import apply_literalize, plan_literalize

NOW = datetime(2026, 9, 25, tzinfo=UTC)
EVENT = '01KQA8R9KVCG906A0203VYEQF7'
runner = CliRunner()


def seed(root):
    with closing(connect(root / 'brain.db')) as conn, conn:
        for entity_id, entity_type, title in [
            ('kb', 'project', 'Counseling KB'),
            ('e-docu-kb', 'project', r'E:\docu\kb'),
            ('report-md', 'person', 'report.md'),
            ('notes-json', 'concept', 'notes.json'),
        ]:
            upsert_entity(conn, Entity(id=entity_id, type=entity_type, title=title,
                                       first_seen=NOW, last_seen=NOW))
        add_alias(conn, 'kb folder', 'e-docu-kb', EntityAliasSource.AUTO_DETECTED)
        for subject, predicate, value, kind in [
            ('kb', 'works_at', 'e-docu-kb', 'entity'),
            ('kb', 'wrote report to', 'report-md', 'entity'),
            ('notes-json', 'lists', 'tasks', 'literal'),
        ]:
            add_fact(conn, Fact(subject=subject, predicate=predicate, object=value, object_type=kind,
                                asserted_at=NOW, source_event=EVENT, confidence=0.9))
        conn.execute("INSERT INTO backlinks VALUES ('kb', 'e-docu-kb', 'mentions', 1, ?)",
                     (NOW.isoformat(),))


def fact_rows(root):
    with closing(connect(root / 'brain.db', read_only=True)) as conn:
        return {row['id']: (row['predicate'], row['object'], row['object_type'])
                for row in conn.execute('SELECT * FROM facts')}


def test_plan_is_read_only_and_reports_blocked_entities(brain_root):
    seed(brain_root)
    before = source_manifest(brain_root)
    plan = plan_literalize(brain_root, ['e-docu-kb', 'report-md', 'notes-json', 'gone'], {1: 'code_path'})
    assert source_manifest(brain_root) == before
    assert plan['absent'] == ['gone']
    assert [change['after'] for change in plan['facts']] == [
        {'predicate': 'code_path', 'object': r'E:\docu\kb', 'object_type': 'literal'},
        {'predicate': 'wrote report to', 'object': 'report.md', 'object_type': 'literal'},
    ]
    assert plan['errors'] == [{'entity': 'notes-json',
                               'error': 'subject of 1 facts; a literal value cannot be a subject'}]
    with pytest.raises(BrainError, match='snake_case'):
        plan_literalize(brain_root, ['e-docu-kb'], {1: 'code path'})


def test_apply_converts_references_and_removes_only_planned_entities(brain_root, tmp_path):
    seed(brain_root)
    plan = plan_literalize(brain_root, ['e-docu-kb', 'report-md'], {1: 'code_path'})
    assert plan['errors'] == [] and plan['counts']['predicate_corrections'] == 1
    backup = tmp_path / 'before.zip'
    create_backup(brain_root, backup)
    # The reviewed plan travels as JSON; string keys and lists must still match.
    result = apply_literalize(brain_root, json.loads(json.dumps(plan)), backup)
    assert result['entities_removed'] == 2 and result['commit']
    assert fact_rows(brain_root) == {
        1: ('code_path', r'E:\docu\kb', 'literal'),
        2: ('wrote report to', 'report.md', 'literal'),
        3: ('lists', 'tasks', 'literal'),
    }
    with closing(connect(brain_root / 'brain.db', read_only=True)) as conn:
        assert sorted(r[0] for r in conn.execute('SELECT id FROM entities')) == ['kb', 'notes-json']
        assert conn.execute('SELECT COUNT(*) FROM entity_aliases').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM backlinks').fetchone()[0] == 0
    events = [json.loads(line) for line in
              (brain_root / 'events.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    assert events[-1]['metadata']['action'] == 'entity_literalize'
    assert Repo(brain_root).head.commit.message.startswith('entity literalize')
    again =plan_literalize(brain_root, ['e-docu-kb', 'report-md'])
    assert again['absent'] == ['e-docu-kb', 'report-md'] and again['facts'] == []


def test_apply_refuses_stale_plans_and_backups(brain_root, tmp_path):
    seed(brain_root)
    plan = plan_literalize(brain_root, ['report-md'])
    backup = tmp_path / 'before.zip'
    create_backup(brain_root, backup)
    tampered = json.loads(json.dumps(plan))
    tampered['facts'][0]['after']['object'] = 'other.md'
    with pytest.raises(BrainError, match='stale'):
        apply_literalize(brain_root, tampered, backup)
    (brain_root / 'pages' / 'log.md').write_text('edited after the backup\n', encoding='utf-8')
    with pytest.raises(BrainError, match='stale'):
        apply_literalize(brain_root, plan, backup)
    assert fact_rows(brain_root)[2] == ('wrote report to', 'report-md', 'entity')


def test_entities_with_pages_are_left_to_merge_or_prune(brain_root):
    seed(brain_root)
    write_page(brain_root / 'pages/concepts/report-md.md',
               Page(frontmatter=Frontmatter(type='concept', slug='report-md', title='report.md',
                                            created=NOW, updated=NOW),
                    compiled_truth='(stub - waiting for more evidence)', timeline=[], sources=[]))
    with closing(connect(brain_root / 'brain.db')) as conn, conn:
        conn.execute("UPDATE entities SET page_path = 'pages/concepts/report-md.md' WHERE id = 'report-md'")
    plan = plan_literalize(brain_root, ['report-md'])
    assert plan['errors'] == [{'entity': 'report-md', 'error': 'has a page; merge it or prune the stub instead'}]


def test_cli_dry_run_writes_plan_outside_root_and_apply_needs_both_files(brain_root, tmp_path):
    seed(brain_root)
    output = tmp_path / 'plan.json'
    result = runner.invoke(app, ['entity', 'literalize', 'e-docu-kb', '--predicate', '1=code_path',
                                 '--brain-root', str(brain_root), '--output', str(output)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)['predicate_corrections'] == 1
    assert json.loads(output.read_text(encoding='utf-8'))['request']['predicates'] == {'1': 'code_path'}
    inside = runner.invoke(app, ['entity', 'literalize', 'e-docu-kb', '--brain-root', str(brain_root),
                                 '--output', str(brain_root / 'plan.json')])
    assert inside.exit_code != 0 and not (brain_root / 'plan.json').exists()
    missing = runner.invoke(app, ['entity', 'literalize', '--apply', '--plan', str(output),
                                  '--brain-root', str(brain_root)])
    assert missing.exit_code != 0
    bad = runner.invoke(app, ['entity', 'literalize', 'e-docu-kb', '--predicate', 'code_path',
                              '--brain-root', str(brain_root)])
    assert bad.exit_code != 0
