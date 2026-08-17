from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import SimpleNamespace

from brain.integrations.codex.hook import (
    build_minimal_query,
    decide_recall,
    handle_hook_payload,
    probe_prompt,
    resolve_brain_root,
    run_hook_stdio,
)


def test_decide_recall_covers_prior_preference_named_and_high_risk_prompts() -> None:
    cases = [
        "继续上次的 BrainMem 项目决定",
        "我喜欢怎样的中文回复风格?",
        "请检查项目 IsaacLab 的当前计划",
        "部署前先确认过去的回滚失败经验",
    ]

    decisions = [decide_recall(prompt) for prompt in cases]

    assert all(decision is not None for decision in decisions)
    assert all(len(decision.query) <= 96 for decision in decisions if decision is not None)


def test_decide_recall_skips_ordinary_code_and_write_only_memory_requests() -> None:
    assert decide_recall("解释 Python 列表推导式") is None
    assert decide_recall("修复这个项目的测试") is None
    assert decide_recall("检查项目当前代码") is None
    assert decide_recall("请记住这个偏好") is None


def test_secret_bearing_prompt_is_not_probed_or_copied_into_query() -> None:
    prompts = [
        "部署前回忆上次决定, password=correct-horse-battery-staple",
        "继续上次部署 AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
        "继续上次部署 client_secret abcdefghijklmnop",
        "继续上次部署 secret is abcdefghijklmnop",
        "继续上次部署 hf_abcdefghijklmnopqrstuvwxyz",
        '继续上次部署 {"api_key":"abcd1234efgh"}',
        '{"client_secret":"abcdefghijklmnop"} 继续上次部署',
        "继续上次部署 access token abcdefghijklmnop",
    ]

    for prompt in prompts:
        assert decide_recall(prompt) is None
        assert build_minimal_query(prompt) is None


def test_minimal_query_is_allowlisted_deduplicated_and_bounded() -> None:
    prompt = "继续上次关于 BrainMem BrainMem 项目的迁移决定, 顺便检查失败经验"

    query = build_minimal_query(prompt, max_chars=40)

    assert query is not None
    assert len(query) <= 40
    assert query.split().count("brainmem") == 1
    assert prompt not in query


def test_probe_is_keyword_only_and_returns_directive_without_memory_body(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    def fake_ask(root: Path, query: str, **kwargs: object) -> SimpleNamespace:
        captured.update(root=root, query=query, **kwargs)
        return SimpleNamespace(
            results=[SimpleNamespace(title="Secret project", compiled_truth="private truth")]
        )

    directive = probe_prompt(
        "继续上次的 BrainMem 项目",
        tmp_path,
        ask_fn=fake_ask,
        context_limit=1200,
    )

    assert directive is not None
    assert captured["mode"] == "keyword-only"
    assert captured["top"] == 3
    assert "private truth" not in directive
    assert "Secret project" not in directive
    assert "did not inject memory bodies" in directive
    assert len(directive) <= 1200


def test_probe_returns_nothing_when_local_search_has_no_results(tmp_path: Path) -> None:
    def fake_ask(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(results=[])

    assert probe_prompt("继续上次的项目", tmp_path, ask_fn=fake_ask) is None


def test_handle_hook_payload_uses_official_additional_context_shape(tmp_path: Path) -> None:
    def fake_ask(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(results=[object()])

    output = handle_hook_payload(
        {
            "session_id": "thr_1",
            "turn_id": "turn_1",
            "cwd": str(tmp_path),
            "hook_event_name": "UserPromptSubmit",
            "prompt": "继续上次的 BrainMem 项目",
        },
        tmp_path,
        ask_fn=fake_ask,
    )

    assert output is not None
    assert set(output) == {"hookSpecificOutput"}
    assert output["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
    assert isinstance(output["hookSpecificOutput"]["additionalContext"], str)


def test_run_hook_stdio_is_silent_on_bad_input(monkeypatch) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json and never logged"))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    run_hook_stdio(Path("missing-root"))

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_run_hook_stdio_suppresses_dependency_stderr_on_missing_root(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "prompt": "继续上次的中文项目决定",
    }
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload, ensure_ascii=False)))
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)

    run_hook_stdio(tmp_path / "missing-root")

    assert stdout.getvalue() == ""
    assert stderr.getvalue() == ""


def test_run_hook_stdio_outputs_compact_json_on_hit(monkeypatch, tmp_path: Path) -> None:
    payload = {
        "session_id": "thr_1",
        "turn_id": "turn_1",
        "cwd": str(tmp_path),
        "hook_event_name": "UserPromptSubmit",
        "prompt": "继续上次的 BrainMem 项目",
    }
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(
        "brain.integrations.codex.hook.handle_hook_payload",
        lambda *_args, **_kwargs: {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": "bounded directive",
            }
        },
    )

    run_hook_stdio(tmp_path)

    assert json.loads(stdout.getvalue())["hookSpecificOutput"]["additionalContext"] == "bounded directive"


def test_resolve_brain_root_uses_portable_user_config(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("BRAIN_ROOT", raising=False)
    configured_root = tmp_path / "data-root"
    config = tmp_path / ".config" / "brainmem" / "config.toml"
    config.parent.mkdir(parents=True)
    config.write_text(f'data_root = "{configured_root.as_posix()}"\n', encoding="utf-8")

    assert resolve_brain_root(None, home=tmp_path) == configured_root.resolve()
