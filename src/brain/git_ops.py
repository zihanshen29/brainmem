import os
from pathlib import Path

from git import Repo
from git.exc import GitError as GitPythonError

from brain.exceptions import GitError


def is_dirty(root: Path) -> bool:
    """Return whether the repository has staged, unstaged, or untracked changes."""
    try:
        repo = _open_repo(root)
        return repo.is_dirty(untracked_files=True)
    except GitPythonError as exc:
        raise GitError(f"Could not check git status for {root}") from exc


def commit(root: Path, message: str, paths: list[Path] | None = None) -> str | None:
    """Stage changes and create a commit, returning a short SHA when one is made."""
    try:
        repo = _open_repo(root)
        from brain.config import load_config
        config_path = root / "config.toml"
        track_db = load_config(config_path).git.track_database if config_path.exists() else False
        if paths is not None:
            paths = [p for p in paths if track_db or _repo_relative_path(root, p) != "brain.db"]
            if not paths:
                return None
            check_commit_paths(root, paths)
        if paths is None:
            if not track_db and "brain.db" in repo.git.diff("--cached", "--name-only").splitlines():
                raise GitError("brain.db is staged but database tracking is disabled")
            selection = ["."] if track_db else [".", ":(exclude)brain.db", ":(exclude).brainmem/**", ":(exclude)scratch/**"]
            repo.git.add("--all", "--", *selection)
        elif paths:
            repo.git.add("--", *[_repo_relative_path(root, path) for path in paths])

        if not repo.is_dirty(index=True, working_tree=False, untracked_files=False):
            return None

        repo.git.commit("-m", message)
        return repo.head.commit.hexsha[:7]
    except GitPythonError as exc:
        raise GitError(f"Could not commit changes in {root}") from exc


def check_commit_paths(root: Path, paths: list[Path]) -> None:
    """Check staging before a data operation so automatic commit cannot absorb other work."""
    from brain.config import load_config

    config_path = root / "config.toml"
    track_db = load_config(config_path).git.track_database if config_path.exists() else False
    permitted = [_repo_relative_path(root, p) for p in paths]
    if not track_db:
        permitted = [p for p in permitted if p != "brain.db"]
    staged = _open_repo(root).git.diff("--cached", "--name-only").splitlines()
    if any(not any(name == p or name.startswith(p.rstrip("/") + "/") for p in permitted)
           for name in staged):
        raise GitError("Unrelated staged changes exist; finish that commit before automatic commit")


def _open_repo(root: Path) -> Repo:
    repo = Repo(root, search_parent_directories=False)
    repo.git.update_environment(GIT_CONFIG_GLOBAL=os.environ.get("GIT_CONFIG_GLOBAL", os.devnull),
                                GIT_OPTIONAL_LOCKS="0")
    if repo.working_tree_dir is None:
        raise GitError(f"Git repository at {root} does not have a working tree")
    return repo


def _repo_relative_path(root: Path, path: Path) -> str:
    candidate = path if path.is_absolute() else root / path
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise GitError(f"Path {path} is outside git repository {root}") from exc
