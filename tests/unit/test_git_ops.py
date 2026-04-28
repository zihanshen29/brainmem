import os
from pathlib import Path

import pytest
from git import Repo

from brain.exceptions import GitError
from brain.git_ops import commit, is_dirty


def initialized_repo(root: Path, monkeypatch: pytest.MonkeyPatch) -> Repo:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root.mkdir()
    repo = Repo.init(root)

    with repo.config_writer(config_level="repository") as writer:
        writer.set_value("user", "name", "Brain Tests")
        writer.set_value("user", "email", "brain@example.test")

    return repo


def test_modified_file_commit_returns_sha(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "repo"
    repo = initialized_repo(root, monkeypatch)
    note = root / "note.md"
    note.write_text("before\n", encoding="utf-8", newline="\n")
    repo.git.add("--", "note.md")
    repo.index.commit("Initial commit")

    note.write_text("after\n", encoding="utf-8", newline="\n")

    assert is_dirty(root) is True

    sha = commit(root, "Update note", paths=[Path("note.md")])

    assert sha is not None
    assert len(sha) == 7
    assert repo.head.commit.hexsha.startswith(sha)
    assert is_dirty(root) is False


def test_commit_with_no_changes_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "repo"
    initialized_repo(root, monkeypatch)

    assert commit(root, "Nothing to commit") is None


def test_non_git_directory_raises_git_error(tmp_path: Path) -> None:
    with pytest.raises(GitError):
        is_dirty(tmp_path)

    with pytest.raises(GitError):
        commit(tmp_path, "Attempt commit")
