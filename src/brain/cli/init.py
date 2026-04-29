from __future__ import annotations

import importlib
import json
import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from brain.db.migrations import init_db
from brain.exceptions import BrainError, GitError
from brain.paths import BrainPaths

INITIAL_COMMIT_MESSAGE = "Initialize brain repository"
DEFAULT_GIT_USER_NAME = "Brain CLI"
DEFAULT_GIT_USER_EMAIL = "brain@example.invalid"

CLAUDE_TEMPLATE = """# Brain Schema (for LLM consumers)

This brain follows a strict format. When reading or writing pages, follow these rules.

## Page structure
Every page has frontmatter, then four sections separated by `---`:
1. `# Compiled truth` - current best understanding, can be rewritten
2. `# Timeline` - append-only, never edit existing entries
3. `# Sources` - auto-maintained list of references

## Frontmatter
Required: type, slug, title, created, updated.
type in {entity, project, concept, event, experience, conversation}.

## Cross-references
Use `[[slug]]` to link to another page. Use `[[slug|display]]` for custom display text.

## Timeline format
Each entry: `- <YYYY-MM-DD> [event:<ulid>]: <description>`
"""

README_TEMPLATE = """# Brain

Personal memory repository initialized by `mem init`.

Key files:
- `events.jsonl`: append-only event ledger
- `brain.db`: SQLite index and structured data
- `pages/`: markdown wiki pages
- `review/`: pending review queue
"""

GITIGNORE_TEMPLATE = """brain.log
*.tmp
*.db-wal
*.db-shm
"""

GITATTRIBUTES_TEMPLATE = """* text=auto eol=lf
*.py text eol=lf
*.md text eol=lf
*.toml text eol=lf
*.sql text eol=lf
*.jsonl text eol=lf
*.db binary
"""


def init_brain(root: Path, force: bool = False) -> None:
    """Initialize an empty brain repository at root.

    Args:
        root: Filesystem root for the brain repository.
        force: Remove existing root contents before rebuilding.

    Raises:
        BrainError: If the root cannot be safely initialized.
    """
    resolved_root = Path(root).expanduser().resolve()
    paths = BrainPaths(resolved_root)

    _prepare_root(paths.root, force=force)
    _create_directories(paths)
    _write_seed_files(paths)
    init_db(paths.db_path)
    _init_git_repository(paths.root)
    _commit_initial_repository(paths.root)


def _prepare_root(root: Path, *, force: bool) -> None:
    if root.exists() and not root.is_dir():
        raise BrainError(f"Brain root exists and is not a directory: {root}")

    if root.exists() and any(root.iterdir()):
        if not force:
            raise BrainError(f"Brain root exists and is not empty: {root}")
        _clear_root_contents(root)

    root.mkdir(parents=True, exist_ok=True)


def _clear_root_contents(root: Path) -> None:
    for child in root.iterdir():
        _ensure_deletion_target_is_child(child, root)
        try:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child, onerror=_remove_readonly)
            else:
                child.unlink()
        except OSError as exc:
            raise BrainError(f"Could not remove existing brain path: {child}") from exc


def _ensure_deletion_target_is_child(target: Path, root: Path) -> None:
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise BrainError(f"Refusing to delete path outside brain root: {target}") from exc
    if target == root:
        raise BrainError(f"Refusing to delete brain root directly: {root}")


def _remove_readonly(function: Callable[[str], object], path: str, _exc_info: object) -> None:
    os.chmod(path, stat.S_IWRITE)
    function(path)


def _create_directories(paths: BrainPaths) -> None:
    directories = [
        paths.raw_dir,
        paths.laundry_processed_dir,
        paths.pages_dir,
        paths.entities_dir,
        paths.projects_dir,
        paths.concepts_dir,
        paths.events_dir,
        paths.experiences_dir,
        paths.conversations_dir,
        paths.review_archive_dir,
    ]
    try:
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BrainError(f"Could not create brain directory structure: {paths.root}") from exc


def _write_seed_files(paths: BrainPaths) -> None:
    files = {
        paths.config_path: _config_text(paths.root),
        paths.events_jsonl: "",
        paths.pages_index: "# Brain Index\n\n",
        paths.pages_log: "# Brain Log\n\n",
        paths.readme_md: README_TEMPLATE,
        paths.gitignore: GITIGNORE_TEMPLATE,
        paths.gitattributes: GITATTRIBUTES_TEMPLATE,
        paths.claude_md: CLAUDE_TEMPLATE,
    }

    try:
        for path, content in files.items():
            path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise BrainError(f"Could not write seed brain files: {paths.root}") from exc


def _config_text(root: Path) -> str:
    root_value = json.dumps(root.as_posix())
    return f"""[openai]
api_key_env = "OPENAI_API_KEY"
model = "gpt-5.5"
fast_model = "gpt-5.4-mini"

[paths]
brain_root = {root_value}

[ingest]
confidence_auto_accept = 0.85
confidence_auto_reject = 0.50

[tier]
tier3_threshold = 1
tier2_threshold = 3
tier1_threshold = 8

[lint]
stale_days = 90

[git]
auto_commit = true
"""


def _init_git_repository(root: Path) -> None:
    _run_git(root, ["init"])
    _ensure_git_config(root, "user.name", DEFAULT_GIT_USER_NAME)
    _ensure_git_config(root, "user.email", DEFAULT_GIT_USER_EMAIL)


def _ensure_git_config(root: Path, key: str, value: str) -> None:
    result = _run_git(root, ["config", "--local", "--get", key], check=False)
    if result.returncode == 0 and result.stdout.strip():
        return
    if result.returncode not in {0, 1}:
        raise GitError(_git_error_message(result, f"Could not read git config {key}"))
    _run_git(root, ["config", "--local", key, value])


def _commit_initial_repository(root: Path) -> None:
    with _temporary_global_git_config(os.devnull):
        try:
            git_ops = importlib.import_module("brain.git_ops")
        except ModuleNotFoundError as exc:
            if exc.name != "brain.git_ops":
                raise
            _fallback_commit(root, INITIAL_COMMIT_MESSAGE)
            return

        git_ops.commit(root, INITIAL_COMMIT_MESSAGE)


def _fallback_commit(root: Path, message: str) -> str | None:
    _run_git(root, ["add", "--all"])
    _run_git(root, ["commit", "-m", message])
    result = _run_git(root, ["rev-parse", "HEAD"])
    return result.stdout.strip() or None


def _run_git(root: Path, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        encoding="utf-8",
        env=_git_env(),
        check=False,
    )
    if check and result.returncode != 0:
        raise GitError(_git_error_message(result, "Git command failed"))
    return result


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    return env


def _git_error_message(result: subprocess.CompletedProcess[str], fallback: str) -> str:
    details = result.stderr.strip() or result.stdout.strip()
    if details:
        return details
    return fallback


@contextmanager
def _temporary_global_git_config(path: str) -> Iterator[None]:
    previous = os.environ.get("GIT_CONFIG_GLOBAL")
    os.environ["GIT_CONFIG_GLOBAL"] = path
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("GIT_CONFIG_GLOBAL", None)
        else:
            os.environ["GIT_CONFIG_GLOBAL"] = previous
