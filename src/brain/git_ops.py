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
        if paths is None:
            repo.git.add("--all")
        elif paths:
            repo.git.add("--", *[_repo_relative_path(root, path) for path in paths])

        if not repo.is_dirty(index=True, working_tree=False, untracked_files=False):
            return None

        new_commit = repo.index.commit(message)
        return new_commit.hexsha[:7]
    except GitPythonError as exc:
        raise GitError(f"Could not commit changes in {root}") from exc


def _open_repo(root: Path) -> Repo:
    repo = Repo(root, search_parent_directories=False)
    if repo.working_tree_dir is None:
        raise GitError(f"Git repository at {root} does not have a working tree")
    return repo


def _repo_relative_path(root: Path, path: Path) -> str:
    candidate = path if path.is_absolute() else root / path
    try:
        return candidate.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise GitError(f"Path {path} is outside git repository {root}") from exc
