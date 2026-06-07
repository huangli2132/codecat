from pathlib import Path


def test_core_modules_stay_below_entropy_budget():
    root = Path(__file__).resolve().parents[1]
    budgets = {
        "codecat/core/runtime.py": 950,
        "codecat/core/runtime_events.py": 90,
        "codecat/core/runtime_consumers.py": 90,
        "codecat/core/artifacts.py": 130,
        "codecat/core/task_state.py": 140,
        "codecat/core/todo_ledger.py": 120,
        "codecat/core/worker_manager.py": 220,
        "codecat/core/context_manager.py": 420,
        "codecat/core/context_usage.py": 120,
        "codecat/core/compact.py": 180,
        "codecat/core/engine.py": 470,
        "codecat/core/model_errors.py": 100,
        "codecat/core/permissions.py": 140,
        "codecat/core/tool_policy.py": 90,
        "codecat/core/plan_mode.py": 140,
        "codecat/core/tool_executor.py": 181,
        "codecat/core/tool_profiles.py": 80,
        "codecat/core/turn_history.py": 250,
        "codecat/features/skills.py": 220,
        "codecat/features/skills_bundled.py": 120,
        "codecat/features/skills_runtime.py": 140,
        "codecat/tools/registry.py": 360,
        "codecat/tools/todos.py": 80,
        "codecat/tools/agents.py": 90,
    }

    for relative_path, max_lines in budgets.items():
        line_count = len((root / relative_path).read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines, budget is {max_lines}"
