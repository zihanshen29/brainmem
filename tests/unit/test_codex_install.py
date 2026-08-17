from __future__ import annotations

import json
from pathlib import Path

import pytest

from brain.exceptions import BrainError
from brain.integrations.codex.install import (
    AGENTS_END,
    AGENTS_START,
    MCP_END,
    MCP_START,
    collect_integration_status,
    desired_hook_handler,
    install_integration,
    uninstall_integration,
)


def test_repository_skill_has_only_required_trigger_frontmatter() -> None:
    skill = Path("skills/brainmem/SKILL.md").read_text(encoding="utf-8")
    _, metadata, _body = skill.split("---", maxsplit=2)
    keys = {
        line.split(":", maxsplit=1)[0].strip()
        for line in metadata.splitlines()
        if line.strip()
    }

    assert keys == {"name", "description"}
    assert "before" in metadata
    assert "preferences" in metadata
    assert "high-risk" in metadata


def _brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain-root"
    (root / "pages").mkdir(parents=True)
    (root / "config.toml").write_text("[paths]\nbrain_root = '.'\n", encoding="utf-8")
    return root


def _source_skill(tmp_path: Path, body: str = "# Brain Memory\n") -> Path:
    source = tmp_path / "source" / "SKILL.md"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(
        "---\nname: brain-memory\ndescription: Test BrainMem skill.\n---\n\n" + body,
        encoding="utf-8",
    )
    return source


def test_install_dry_run_does_not_create_user_files(tmp_path: Path) -> None:
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"

    report = install_integration(
        _brain_root(tmp_path),
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=_source_skill(tmp_path),
        apply=False,
    )

    assert report["applied"] is False
    assert not codex_home.exists()
    assert not agents_home.exists()


def test_install_preserves_unrelated_hooks_agents_and_toml(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("# User policy\n\nKeep this.\n", encoding="utf-8")
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {
                "description": "user hooks",
                "custom": {"keep": True},
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": "python user_stop.py"}]}
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text(
        'model = "gpt-user-choice"\n\n[features]\nhooks = true\n', encoding="utf-8"
    )

    install_integration(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        apply=True,
    )

    installed_skill = agents_home / "skills" / "brain-memory" / "SKILL.md"
    assert installed_skill.read_bytes() == source.read_bytes()
    agents_text = (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    assert "# User policy" in agents_text
    assert "Keep this." in agents_text
    assert agents_text.count(AGENTS_START) == 1
    assert agents_text.count(AGENTS_END) == 1

    hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["description"] == "user hooks"
    assert hooks["custom"] == {"keep": True}
    assert hooks["hooks"]["Stop"][0]["hooks"][0]["command"] == "python user_stop.py"
    prompt_group = hooks["hooks"]["UserPromptSubmit"][0]
    assert "matcher" not in prompt_group
    handler = prompt_group["hooks"][0]
    assert "commandWindows" in handler
    assert handler["additionalContextLimit"] == 1200

    config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model = "gpt-user-choice"' in config_text
    assert "[features]" in config_text
    assert config_text.count(MCP_START) == 1
    assert config_text.count(MCP_END) == 1
    assert 'enabled_tools = ["brain_status", "brain_ask", "brain_inject", "brain_procedure_list"]' in config_text

    status = collect_integration_status(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        check_commands=False,
    )
    assert status["ready"] is True


def test_reinstall_is_idempotent_and_updates_only_owned_handler(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"

    for _ in range(2):
        install_integration(
            root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source,
            apply=True,
        )

    hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert len(hooks["hooks"]["UserPromptSubmit"]) == 1
    assert (codex_home / "AGENTS.md").read_text(encoding="utf-8").count(AGENTS_START) == 1
    assert (codex_home / "config.toml").read_text(encoding="utf-8").count(MCP_START) == 1


def test_install_refuses_to_overwrite_unowned_skill_without_explicit_flag(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    destination = agents_home / "skills" / "brain-memory" / "SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("user-owned content", encoding="utf-8")

    with pytest.raises(BrainError, match="refusing to overwrite"):
        install_integration(
            root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source,
            apply=True,
        )

    assert destination.read_text(encoding="utf-8") == "user-owned content"


def test_install_refuses_unmarked_existing_brainmem_mcp_config(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text(
        '[mcp_servers."brainmem"]\ncommand = "custom-memory"\n', encoding="utf-8"
    )

    with pytest.raises(BrainError, match="existing unmarked"):
        install_integration(
            root,
            codex_home=codex_home,
            agents_home=tmp_path / "agents",
            source_skill=_source_skill(tmp_path),
            apply=False,
        )


def test_status_detects_legacy_duplicate_skill(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    install_integration(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        apply=True,
    )
    legacy = codex_home / "skills" / "brain-memory" / "SKILL.md"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("duplicate", encoding="utf-8")

    status = collect_integration_status(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        check_commands=False,
    )

    assert status["ready"] is False
    assert status["legacy_duplicate_skill"] is True
    assert any("duplicate same-name skill" in issue for issue in status["issues"])


def test_install_can_reversibly_archive_legacy_skill(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    legacy_dir = codex_home / "skills" / "brain-memory"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "SKILL.md").write_text("legacy skill", encoding="utf-8")
    (legacy_dir / "user-resource.txt").write_text("preserve me", encoding="utf-8")

    install_integration(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        archive_legacy_skill=True,
        apply=True,
    )

    archive = codex_home / "skills" / "brain-memory.disabled-by-brainmem"
    assert not legacy_dir.exists()
    assert (archive / "SKILL.md").read_text(encoding="utf-8") == "legacy skill"
    assert (archive / "user-resource.txt").read_text(encoding="utf-8") == "preserve me"
    status = collect_integration_status(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        check_commands=False,
    )
    assert status["ready"] is True
    assert status["legacy_archive_present"] is True

    uninstall_integration(codex_home=codex_home, agents_home=agents_home, apply=True)

    assert (legacy_dir / "SKILL.md").read_text(encoding="utf-8") == "legacy skill"
    assert not archive.exists()


def test_manual_similar_hook_causes_conflict_and_is_not_claimed(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    codex_home.mkdir()
    manual = {
        "type": "command",
        "command": "python manual_codex hook.py",
        "statusMessage": "Checking relevant BrainMem context",
    }
    (codex_home / "hooks.json").write_text(
        json.dumps({"hooks": {"UserPromptSubmit": [{"hooks": [manual]}]}}),
        encoding="utf-8",
    )

    with pytest.raises(BrainError, match="refusing to append a second hook"):
        install_integration(
            root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source,
            apply=True,
        )

    hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert hooks["hooks"]["UserPromptSubmit"][0]["hooks"] == [manual]


def test_reinstall_reports_drift_instead_of_appending_second_hook(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    install_integration(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        apply=True,
    )
    hooks_path = codex_home / "hooks.json"
    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["timeout"] = 99
    hooks_path.write_text(json.dumps(hooks), encoding="utf-8")

    status = collect_integration_status(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        check_commands=False,
    )
    assert status["hook"] == "drifted"
    with pytest.raises(BrainError, match="refusing to append a second hook"):
        install_integration(
            root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source,
            apply=True,
        )
    hooks_after = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert len(hooks_after["hooks"]["UserPromptSubmit"]) == 1


def test_windows_hook_command_quotes_metacharacters_and_rejects_expansion(tmp_path: Path) -> None:
    handler = desired_hook_handler(tmp_path / "brain & notes")

    assert '"' in handler["commandWindows"]
    assert 'brain & notes"' in handler["commandWindows"]
    with pytest.raises(BrainError, match="unsafe Windows"):
        desired_hook_handler(tmp_path / "brain%TEMP%")


def test_uninstall_removes_only_owned_or_marked_content(tmp_path: Path) -> None:
    root = _brain_root(tmp_path)
    source = _source_skill(tmp_path)
    codex_home = tmp_path / "codex"
    agents_home = tmp_path / "agents"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("# Keep me\n", encoding="utf-8")
    (codex_home / "hooks.json").write_text(
        json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}
        ),
        encoding="utf-8",
    )
    (codex_home / "config.toml").write_text('model = "keep"\n', encoding="utf-8")
    install_integration(
        root,
        codex_home=codex_home,
        agents_home=agents_home,
        source_skill=source,
        apply=True,
    )

    uninstall_integration(codex_home=codex_home, agents_home=agents_home, apply=True)

    assert not (agents_home / "skills" / "brain-memory" / "SKILL.md").exists()
    assert "# Keep me" in (codex_home / "AGENTS.md").read_text(encoding="utf-8")
    hooks = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
    assert "Stop" in hooks["hooks"]
    assert "UserPromptSubmit" not in hooks["hooks"]
    config = (codex_home / "config.toml").read_text(encoding="utf-8")
    assert 'model = "keep"' in config
    assert MCP_START not in config
