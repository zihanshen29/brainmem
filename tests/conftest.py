from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from brain.cli.init import init_brain


@pytest.fixture(autouse=True)
def isolated_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)


@pytest.fixture()
def brain_root(tmp_path: Path) -> Path:
    root = tmp_path / "brain"
    init_brain(root)
    return root


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()
