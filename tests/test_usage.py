from pathlib import Path
import pytest

from codecat.testing import ScriptedModelClient
from codecat import Pico, SessionStore, WorkspaceContext


def build_agent(tmp_path, outputs=None, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    return Pico(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=SessionStore(tmp_path / ".codecat" / "sessions"),
        approval_policy="auto",
        **kwargs,
    )


def test_usage_command_reports_provider_model_and_last_usage(tmp_path):
    from codecat.cli import handle_repl_command

    agent = build_agent(tmp_path, ["<final>Done.</final>"])
    agent.model_client.model = "gpt-test"
    agent.model_client.base_url = "https://example.com/v1"
    agent.model_client.last_completion_metadata = {
        "input_tokens": 10,
        "output_tokens": 5,
        "cached_tokens": 3,
        "provider_attempts": 2,
        "provider_retry_count": 1,
    }
    agent.ask("hello")

    handled, _, output = handle_repl_command(agent, "/usage")

    assert handled is True
    assert "model: gpt-test" in output
    assert "base url host: example.com" in output
    assert "last input tokens: 10" in output
    assert "last output tokens: 5" in output
    assert "last cached tokens: 3" in output


def test_model_command_updates_current_runtime_only(tmp_path):
    from codecat.cli import handle_repl_command

    agent = build_agent(tmp_path, [])
    agent.model_client.model = "old-model"

    handled, _, output = handle_repl_command(agent, "/model new-model")

    assert handled is True
    assert output == "model: new-model"
    assert agent.model_client.model == "new-model"
    assert not (Path(tmp_path) / ".codecat.toml").exists()


def test_session_history_resume_and_clear_commands(tmp_path):
    from codecat.cli import handle_repl_command

    first = build_agent(tmp_path, ["<final>First.</final>"])
    assert first.ask("first request") == "First."
    first_id = first.session["id"]

    second = Pico.from_session(
        model_client=ScriptedModelClient(["<final>Second.</final>"]),
        workspace=first.workspace,
        session_store=first.session_store,
        session_id=first_id,
        approval_policy="auto",
    )
    assert second.ask("second request") == "Second."

    handled, _, output = handle_repl_command(second, "/history")
    assert handled is True
    assert first_id in output
    assert "Second." in output

    handled, _, output = handle_repl_command(second, f"/resume {first_id}")
    assert handled is True
    assert output == f"resumed session {first_id}"
    assert second.session["id"] == first_id

    old_id = second.session["id"]
    handled, _, output = handle_repl_command(second, "/clear")
    assert handled is True
    assert output.startswith("new session ")
    assert second.session["id"] != old_id
    assert second.current_task_state is None
    assert second.current_run_id == ""
    assert second.current_run_dir is None
    assert second.session_store.path(old_id).exists()


def test_resume_rejects_path_traversal_session_id(tmp_path):
    from codecat.cli import handle_repl_command

    agent = build_agent(tmp_path, [])

    handled, _, output = handle_repl_command(agent, "/resume ../outside")

    assert handled is True
    assert output == "error: session not found"


def test_session_store_rejects_path_traversal_ids(tmp_path):
    store = SessionStore(tmp_path / ".codecat" / "sessions")

    with pytest.raises(ValueError, match="invalid session id"):
        store.load("../outside")
