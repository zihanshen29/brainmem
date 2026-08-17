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
def fake_provider_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Satisfy preflight while making accidental real provider calls fail closed."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-only-key")

    def reject_real_provider_call(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("provider call must be mocked in tests using fake_provider_key")

    monkeypatch.setattr("brain.llm.client._extract_impl", reject_real_provider_call)


@pytest.fixture()
def cli_runner() -> CliRunner:
    return CliRunner()
