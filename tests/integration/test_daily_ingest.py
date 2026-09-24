from collections import Counter
from contextlib import closing
from datetime import UTC, datetime

from brain.config import load_config
from brain.db.connection import connect
from brain.db.entities import upsert_entity
from brain.models import Entity, Event, FactCandidate
from brain.paths import BrainPaths
from brain.pipeline.ingest import (
    IngestItem,
    IngestReport,
    ReviewWriter,
    _apply_extraction,
    _write_pending_fact_review,
)
from brain.pipeline.review import apply_pending, list_pending
from brain.pipeline.signal_detect import SignalEntity, SignalExtraction

NOW = datetime(2026, 9, 24, tzinfo=UTC)
EVENT = '01KQA8R9KVCG906A0203VYEQF7'


def candidate(**changes):
    data = dict(subject='project', predicate='uses', object='SQLite', object_type='literal',
                source_event=EVENT, source_ref='laundry/note.md', confidence=0.8)
    return FactCandidate(**(data | changes))


def seed(conn):
    upsert_entity(conn, Entity(id='project', title='示例项目', type='project',
                              page_path='pages/projects/project.md',
                              first_seen=NOW, last_seen=NOW))


def apply(root, extraction, *, threshold=0.75):
    config = load_config(root / 'config.toml')
    config.ingest.confidence_auto_accept = threshold
    report = IngestReport()
    paths = BrainPaths(root)
    event = Event(id=EVENT, timestamp=NOW, kind='laundry_ingested', source_ref='laundry/note.md')
    item = IngestItem(source='laundry', source_ref=event.source_ref,
                      text='示例项目的日常进展。', event=event)
    with closing(connect(paths.db_path)) as conn, conn:
        _apply_extraction(conn, paths, config, item, extraction,
                          ReviewWriter.create(paths, report), report)
    return report


def test_incidental_mentions_remain_values_without_entity_reviews(brain_root):
    with closing(connect(brain_root / 'brain.db')) as conn, conn:
        seed(conn)
    names = ['service.py', 'docs/progress.md', 'start-service.ps1', 'release-v3.2', 'GPU 123']
    facts = [candidate(object=value, object_type='entity') for value in
             ['profile/v0.1.2 contains 12 methods', r'C:\private\notes', 'Provider model3.7-2026-05-26']]
    report = apply(brain_root, SignalExtraction(
        entities=[SignalEntity(name=name, type='project', confidence=0.7) for name in names],
        facts=facts, timeline_summary='本地工具与配置更新。'))
    assert report.facts_added == 3
    assert report.review_items_created == 0
    with closing(connect(brain_root / 'brain.db')) as conn:
        assert conn.execute('SELECT count(*) FROM entities').fetchone()[0] == 1
        assert {r['object'] for r in conn.execute('SELECT object FROM facts')} == {f.object for f in facts}
        assert {r[0] for r in conn.execute('SELECT object_type FROM facts')} == {'literal'}


def test_fact_threshold_does_not_lower_new_entity_gate(brain_root):
    report = apply(brain_root, SignalExtraction(
        entities=[SignalEntity(name='新项目', type='project', confidence=0.8)],
        facts=[candidate(subject='新项目')], timeline_summary='新项目使用 SQLite。'))
    assert report.facts_added == 0
    assert Counter(item.kind.value for item in list_pending(brain_root)) == {
        'new_entity_review': 1, 'pending_fact': 1}


def test_status_observations_coexist_but_single_value_conflicts_still_review(brain_root):
    with closing(connect(brain_root / 'brain.db')) as conn, conn:
        seed(conn)
    report = apply(brain_root, SignalExtraction(facts=[
        candidate(predicate='status', object='仅监听本机回环地址'),
        candidate(predicate='status', object='尚未部署到服务器'),
        candidate(predicate='current_commit', object='abc123'),
        candidate(predicate='current_commit', object='def456'),
    ], timeline_summary='项目运行情况。'))
    assert report.facts_added == 3
    assert [item.kind.value for item in list_pending(brain_root)] == ['fact_conflict']


def test_explicit_pending_approval_does_not_ask_for_confidence_again(brain_root):
    paths = BrainPaths(brain_root)
    with closing(connect(paths.db_path)) as conn, conn:
        seed(conn)
    event = Event(id=EVENT, timestamp=NOW, kind='laundry_ingested', source_ref='laundry/note.md')
    item = IngestItem(source='laundry', source_ref=event.source_ref, text='', event=event)
    writer = ReviewWriter.create(paths, IngestReport())
    _write_pending_fact_review(writer, candidate(confidence=0.65), item, '项目使用 SQLite。', None, ['project'])
    review_path = next(paths.review_dir.glob('*.md'))
    review_path.write_text(review_path.read_text(encoding='utf-8').replace('[ ] approve', '[x] approve'), encoding='utf-8')
    result = apply_pending(brain_root)
    assert result.applied == 1
    assert list_pending(brain_root) == []
    with closing(connect(paths.db_path)) as conn:
        row = conn.execute('SELECT object, confidence FROM facts').fetchone()
        assert tuple(row) == ('SQLite', 0.65)


def test_known_fact_does_not_return_as_low_confidence_review(brain_root):
    with closing(connect(brain_root / 'brain.db')) as conn, conn:
        seed(conn)
    apply(brain_root, SignalExtraction(facts=[candidate(confidence=0.95)], timeline_summary='项目使用 SQLite。'))
    report = apply(brain_root, SignalExtraction(facts=[candidate(confidence=0.65)], timeline_summary='再次提及 SQLite。'))
    assert report.facts_added == 0
    assert report.review_items_created == 0
