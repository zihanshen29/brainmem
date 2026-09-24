
import pytest
from git import Repo

from brain.backup import create_backup, source_manifest
from brain.exceptions import GitError
from brain.pipeline.reconcile import apply_reconcile, plan_reconcile


def legacy_ignore(root):
    path = root / '.gitignore'
    path.write_text('# My custom rules\nsecret-notes/\nbrain.db\n', encoding='utf-8')
    repo = Repo(root)
    repo.index.add(['.gitignore'])
    repo.index.commit('Fixture: old ignore rules')
    for directory in ('.brainmem', 'scratch'):
        (root / directory).mkdir(exist_ok=True)
        (root / directory / 'keep.txt').write_text('keep me', encoding='utf-8')
    return path


def test_reconcile_previews_ignore_migration_and_commits_only_its_paths(brain_root, tmp_path):
    ignore = legacy_ignore(brain_root)
    unrelated = brain_root / 'personal.md'
    unrelated.write_text('unrelated personal edit', encoding='utf-8')
    before = source_manifest(brain_root)
    plan = plan_reconcile(brain_root)
    assert source_manifest(brain_root) == before
    assert plan['gitignore']['additions'] == ['/.brainmem/', '/scratch/']
    backup = tmp_path / 'complete.zip'
    create_backup(brain_root, backup)
    result = apply_reconcile(brain_root, plan, backup)
    assert result['commit']
    assert '# My custom rules\nsecret-notes/' in ignore.read_text(encoding='utf-8')
    repo = Repo(brain_root)
    assert 'personal.md' in repo.untracked_files
    assert '.brainmem/keep.txt' not in repo.untracked_files
    assert 'scratch/keep.txt' not in repo.untracked_files
    assert (brain_root / 'scratch/keep.txt').read_text() == 'keep me'
    assert 'personal.md' not in repo.head.commit.stats.files
    assert 'brain.db' not in repo.head.commit.stats.files
    assert plan_reconcile(brain_root)['gitignore']['additions'] == []


def test_reconcile_does_not_modify_data_if_foreign_staged_work_exists(brain_root, tmp_path):
    legacy_ignore(brain_root)
    (brain_root / 'personal.md').write_text('staged personal work', encoding='utf-8')
    Repo(brain_root).index.add(['personal.md'])
    plan = plan_reconcile(brain_root)
    backup = tmp_path / 'complete.zip'
    create_backup(brain_root, backup)
    before = source_manifest(brain_root)
    with pytest.raises(GitError, match='Unrelated staged'):
        apply_reconcile(brain_root, plan, backup)
    assert source_manifest(brain_root) == before
