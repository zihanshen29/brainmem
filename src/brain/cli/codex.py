from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

import typer

from brain.exceptions import BrainError
from brain.integrations.codex.hook import (
    DEFAULT_BUDGET,
    DEFAULT_CONTEXT_LIMIT,
    DEFAULT_MAX_QUERY_CHARS,
    DEFAULT_TOP,
    resolve_brain_root,
    run_hook_stdio,
)
from brain.integrations.codex.install import (
    collect_integration_status,
    desired_hook_handler,
    install_integration,
    uninstall_integration,
)

codex_app = typer.Typer(
    add_completion=False,
    help="Install and diagnose the conservative OpenAI Codex integration.",
)


@codex_app.command("hook", hidden=True)
def hook_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    top: Annotated[
        int,
        typer.Option("--top", min=1, help="Maximum local probe result count."),
    ] = DEFAULT_TOP,
    budget: Annotated[
        int,
        typer.Option("--budget", min=1, help="Maximum token budget the recall directive permits."),
    ] = DEFAULT_BUDGET,
    max_query_chars: Annotated[
        int,
        typer.Option("--max-query-chars", min=8, help="Maximum minimized local query length."),
    ] = DEFAULT_MAX_QUERY_CHARS,
    context_limit: Annotated[
        int,
        typer.Option("--context-limit", min=200, help="Maximum additionalContext characters."),
    ] = DEFAULT_CONTEXT_LIMIT,
) -> None:
    """Handle one Codex UserPromptSubmit event; failures are intentionally silent."""
    run_hook_stdio(
        brain_root,
        top=top,
        budget=budget,
        max_query_chars=max_query_chars,
        context_limit=context_limit,
    )


@codex_app.command("install")
def install_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    codex_home: Annotated[
        Path | None,
        typer.Option("--codex-home", help="Codex config root; defaults to CODEX_HOME or ~/.codex."),
    ] = None,
    agents_home: Annotated[
        Path | None,
        typer.Option("--agents-home", help="Canonical user skill root; defaults to ~/.agents."),
    ] = None,
    source_skill: Annotated[
        Path | None,
        typer.Option("--source-skill", help="Override the repository canonical SKILL.md source."),
    ] = None,
    mem_command: Annotated[
        str,
        typer.Option("--mem-command", help="Executable or absolute path used by the hook."),
    ] = "mem",
    mcp_command: Annotated[
        str,
        typer.Option("--mcp-command", help="Executable or absolute path used by stdio MCP."),
    ] = "mem-mcp",
    replace_skill: Annotated[
        bool,
        typer.Option(
            "--replace-skill",
            help="Explicitly replace a different canonical skill after reviewing it.",
        ),
    ] = False,
    archive_legacy_skill: Annotated[
        bool,
        typer.Option(
            "--archive-legacy-skill",
            help=(
                "Move an active ~/.codex/skills/brain-memory directory to a reversible "
                ".disabled-by-brainmem archive."
            ),
        ),
    ] = False,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply the plan; without this flag installation is read-only."),
    ] = False,
    json_output: Annotated[
        bool,
        typer.Option("--json", help="Print the plan/result as JSON."),
    ] = False,
) -> None:
    """Install skill, AGENTS policy, UserPromptSubmit hook, and read-only-first MCP config."""
    try:
        report = install_integration(
            brain_root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source_skill,
            mem_command=mem_command,
            mcp_command=mcp_command,
            replace_skill=replace_skill,
            archive_legacy_skill=archive_legacy_skill,
            apply=yes,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _print_report(report, json_output=json_output)
    if not yes and not json_output:
        typer.echo("Dry run only. Review the paths and rerun with --yes to apply.")


@codex_app.command("status")
def status_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home")] = None,
    agents_home: Annotated[Path | None, typer.Option("--agents-home")] = None,
    source_skill: Annotated[Path | None, typer.Option("--source-skill")] = None,
    mem_command: Annotated[str, typer.Option("--mem-command")] = "mem",
    mcp_command: Annotated[str, typer.Option("--mcp-command")] = "mem-mcp",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Show read-only Codex integration and drift status."""
    try:
        report = collect_integration_status(
            brain_root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source_skill,
            mem_command=mem_command,
            mcp_command=mcp_command,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _print_report(report, json_output=json_output)


@codex_app.command("doctor")
def doctor_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    codex_home: Annotated[Path | None, typer.Option("--codex-home")] = None,
    agents_home: Annotated[Path | None, typer.Option("--agents-home")] = None,
    source_skill: Annotated[Path | None, typer.Option("--source-skill")] = None,
    mem_command: Annotated[str, typer.Option("--mem-command")] = "mem",
    mcp_command: Annotated[str, typer.Option("--mcp-command")] = "mem-mcp",
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Check whether proactive recall can run; exit nonzero on actionable drift."""
    try:
        report = collect_integration_status(
            brain_root,
            codex_home=codex_home,
            agents_home=agents_home,
            source_skill=source_skill,
            mem_command=mem_command,
            mcp_command=mcp_command,
        )
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _print_report(report, json_output=json_output)
    if not report["ready"]:
        raise typer.Exit(1)


@codex_app.command("uninstall")
def uninstall_command(
    codex_home: Annotated[Path | None, typer.Option("--codex-home")] = None,
    agents_home: Annotated[Path | None, typer.Option("--agents-home")] = None,
    yes: Annotated[
        bool,
        typer.Option("--yes", help="Apply removal; without this flag the command is read-only."),
    ] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Remove only manifest-owned files and marked config blocks."""
    try:
        report = uninstall_integration(codex_home=codex_home, agents_home=agents_home, apply=yes)
    except BrainError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from exc
    _print_report(report, json_output=json_output)
    if not yes and not json_output:
        typer.echo("Dry run only. Review the actions and rerun with --yes to apply.")


@codex_app.command("hook-template")
def hook_template_command(
    brain_root: Annotated[
        Path | None,
        typer.Option("--brain-root", help="Brain repository root."),
    ] = None,
    mem_command: Annotated[str, typer.Option("--mem-command")] = "mem",
) -> None:
    """Print a valid matcher-free UserPromptSubmit hooks.json template."""
    handler = desired_hook_handler(resolve_brain_root(brain_root), mem_command=mem_command)
    payload = {
        "description": "BrainMem proactive local recall hook.",
        "hooks": {"UserPromptSubmit": [{"hooks": [handler]}]},
    }
    typer.echo(json.dumps(payload, ensure_ascii=False, indent=2))


def _print_report(report: dict[str, Any], *, json_output: bool) -> None:
    if json_output:
        typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return
    if "ready" in report:
        typer.echo(f"Ready: {str(report['ready']).lower()}")
        typer.echo(f"Brain root: {report['brain_root']}")
        typer.echo(f"Skill: {report['skill']}")
        typer.echo(f"Global AGENTS policy: {report['agents_policy']}")
        typer.echo(f"UserPromptSubmit hook: {report['hook']}")
        typer.echo(f"BrainMem MCP: {report['mcp']}")
        for issue in report["issues"]:
            typer.echo(f"Issue: {issue}")
        return
    typer.echo(f"Applied: {str(report['applied']).lower()}")
    for action in report["actions"]:
        typer.echo(f"- {action}")
    for warning in report["warnings"]:
        typer.echo(f"Warning: {warning}")
