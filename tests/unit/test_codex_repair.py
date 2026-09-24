import hashlib
import json
import sys
import tomllib
from pathlib import Path

import pytest

from brain.exceptions import BrainError
from brain.integrations.codex.install import (
    MCP_END,
    MCP_START,
    collect_integration_status,
    install_integration,
)


def install_args(tmp_path, root):
    source = tmp_path / 'source.md'
    source.write_text('---\nname: brain-memory\ndescription: Original description\n---\n\nVersion one.\n', encoding='utf-8')
    return dict(brain_root=root, codex_home=tmp_path / 'codex', agents_home=tmp_path / 'agents', source_skill=source)


def test_repair_keeps_foreign_tables_custom_options_and_comments(brain_root, tmp_path):
    args = install_args(tmp_path, brain_root)
    install_integration(**args, apply=True)
    path = args['codex_home'] / 'config.toml'
    text = path.read_text(encoding='utf-8').replace(MCP_END,
        '# My timeout\nstartup_timeout_sec = 77\n'
        '[mcp_servers.brainmem.env]\nPERSONAL = "keep me"\n'
        '# Foreign service comment\n[mcp_servers.node_repl]\ncommand="keep-node"\n'
        '[mcp_servers.cua_repl]\ncommand="keep-cua"\n' + MCP_END)
    path.write_text(text, encoding='utf-8')
    before = {p: p.read_bytes() for p in args['codex_home'].rglob('*') if p.is_file()}
    preview = install_integration(**args)
    assert not preview['applied']
    assert before == {p: p.read_bytes() for p in args['codex_home'].rglob('*') if p.is_file()}
    result = install_integration(**args, apply=True)
    fixed = path.read_text(encoding='utf-8')
    config = tomllib.loads(fixed)
    assert config['mcp_servers']['node_repl']['command'] == 'keep-node'
    assert config['mcp_servers']['cua_repl']['command'] == 'keep-cua'
    assert config['mcp_servers']['brainmem']['startup_timeout_sec'] == 77
    assert config['mcp_servers']['brainmem']['env'] == {'PERSONAL': 'keep me'}
    assert '# My timeout' in fixed and '# Foreign service comment' in fixed
    managed = fixed.split(MCP_START)[1].split(MCP_END)[0]
    assert 'node_repl' not in managed and 'cua_repl' not in managed
    assert collect_integration_status(**args, check_commands=False)['ready']
    backup = json.loads((Path(result['backup']) / 'manifest.json').read_text())
    saved = backup['files']['config']
    assert saved['sha256'] == hashlib.sha256(before[path]).hexdigest()
    assert (Path(result['backup']) / 'config').read_bytes() == before[path]


def test_skill_upgrade_preserves_only_proven_description_edit(brain_root, tmp_path):
    args = install_args(tmp_path, brain_root)
    install_integration(**args, apply=True)
    skill = args['agents_home'] / 'skills/brain-memory/SKILL.md'
    skill.write_text(skill.read_text().replace('Original description', 'My short description'), encoding='utf-8')
    source = args['source_skill']
    source.write_text(source.read_text().replace('Version one.', 'Version two with current safety guidance.'), encoding='utf-8')
    install_integration(**args, apply=True)
    assert 'My short description' in skill.read_text()
    assert 'Version two with current safety guidance.' in skill.read_text()
    assert collect_integration_status(**args, check_commands=False)['ready']
    skill.write_text(skill.read_text() + '\nMy personal operating rules.\n', encoding='utf-8')
    before = skill.read_bytes()
    with pytest.raises(BrainError, match='locally edited'):
        install_integration(**args, apply=True)
    assert skill.read_bytes() == before


def test_default_launchers_work_without_mem_mcp_on_path(brain_root, tmp_path, monkeypatch):
    args = install_args(tmp_path, brain_root)
    monkeypatch.setenv('PATH', '')
    install_integration(**args, apply=True)
    status = collect_integration_status(**args)
    assert status['ready']
    config = tomllib.loads((args['codex_home'] / 'config.toml').read_text())
    assert config['mcp_servers']['brainmem']['command'] == sys.executable
    assert config['mcp_servers']['brainmem']['args'] == ['-m', 'brain.mcp.server']


def test_old_launcher_with_multiline_args_keeps_comments_and_other_values(brain_root, tmp_path):
    args = install_args(tmp_path, brain_root)
    install_integration(**args, mcp_command='old-mem-mcp', apply=True)
    path = args['codex_home'] / 'config.toml'
    path.write_text(path.read_text().replace('args = []', 'args = [\n  "--old", # My launcher note\n]\nstartup_timeout_sec = 91'), encoding='utf-8')
    install_integration(**args, apply=True)
    text = path.read_text()
    server = tomllib.loads(text)['mcp_servers']['brainmem']
    assert server['command'] == sys.executable
    assert server['args'] == ['-m', 'brain.mcp.server']
    assert server['startup_timeout_sec'] == 91
    assert '# My launcher note' in text


def test_edited_managed_policy_is_preserved_for_explicit_merge(brain_root, tmp_path):
    args = install_args(tmp_path, brain_root)
    install_integration(**args, apply=True)
    path = args['codex_home'] / 'AGENTS.md'
    path.write_text(path.read_text().replace('## BrainMem proactive recall', '## My customized memory rules'), encoding='utf-8')
    before = path.read_bytes()
    with pytest.raises(BrainError, match='locally edited AGENTS'):
        install_integration(**args, apply=True)
    assert path.read_bytes() == before


def test_install_refuses_configuration_changed_during_planning(brain_root, tmp_path, monkeypatch):
    from brain.integrations.codex import install as integration

    args = install_args(tmp_path, brain_root)
    install_integration(**args, apply=True)
    path = args['codex_home'] / 'config.toml'
    original = integration._merge_mcp_config
    edited = path.read_text() + '\n[mcp_servers.new_user_service]\ncommand = "keep-new-work"\n'

    def concurrent_edit(text, desired):
        result = original(text, desired)
        path.write_text(edited, encoding='utf-8')
        return result

    monkeypatch.setattr(integration, '_merge_mcp_config', concurrent_edit)
    with pytest.raises(BrainError, match='changed'):
        install_integration(**args, apply=True)
    assert path.read_text() == edited
