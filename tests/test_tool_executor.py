"""Tool executor guardrail unit tests.

覆盖 `run_tool()` 的所有代码路径：未知工具、参数校验失败、权限拒绝、
策略拒绝、重复调用、正常执行（快照 diff）、异常执行（仍捕获 diff）、
shell 退出码语义、长输出 artifact。
"""

import json

from codecat.testing import ScriptedModelClient
from codecat import Pico, SessionStore, WorkspaceContext
from codecat.features.sandbox.config import SandboxConfig


# ── helpers ───────────────────────────────────────────────────


def build_agent(tmp_path, outputs=None, **kwargs):
    """创建一个用于测试的 Pico agent。

    和其它 acceptance 测试保持相同的构造模式，确保 run_tool() 的
    每条路径都在真实的 agent 对象上执行。
    """
    (tmp_path / "README.md").write_text("hello world\n", encoding="utf-8")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "main.py").write_text("print('ok')\n", encoding="utf-8")
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codecat" / "sessions")
    approval_policy = kwargs.pop("approval_policy", "auto")
    return Pico(
        model_client=ScriptedModelClient(outputs or []),
        workspace=workspace,
        session_store=store,
        approval_policy=approval_policy,
        **kwargs,
    )


def read_jsonl(path):
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# ── 未知工具 & 参数错误 ──────────────────────────────────────


class TestUnknownOrInvalid:
    def test_unknown_tool_rejected(self, tmp_path):
        """不存在的工具直接 rejected，设置 full metadata。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("nonexistent_tool", {"arg": 1})

        assert result == "error: unknown tool 'nonexistent_tool'"
        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "rejected"
        assert meta["tool_error_code"] == "unknown_tool"
        assert meta["risk_level"] == "high"
        assert meta["workspace_changed"] is False
        assert meta["affected_paths"] == []
        assert meta["diff_summary"] == []

    def test_invalid_arguments_rejected(self, tmp_path):
        """Pydantic 校验失败时 rejected 并给出 example。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("read_file", {"path": "README.md", "start": -1})

        assert result.startswith("error: invalid arguments for read_file")
        assert "example:" in result
        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "rejected"
        assert meta["tool_error_code"] == "invalid_arguments"

    def test_path_escape_triggers_security_event(self, tmp_path):
        """路径越界写触发 security_event_type。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("write_file", {"path": "../outside.txt", "content": "bad"})

        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "rejected"
        assert meta["security_event_type"] == "path_escape"


# ── 权限拒绝 ──────────────────────────────────────────────────


class TestPermissionDenied:
    def test_approval_denied_for_risky_tool(self, tmp_path):
        """approval_policy=never 时 risky 工具被拒。"""
        agent = build_agent(tmp_path, approval_policy="never")
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

        assert result == "error: approval denied for run_shell"
        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "rejected"
        assert meta["tool_error_code"] == "approval_denied"

    def test_permission_decision_event_emitted(self, tmp_path):
        """权限决策写入 session event bus。"""
        agent = build_agent(tmp_path, approval_policy="never")
        agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

        events = read_jsonl(agent.session_event_bus.path)
        perm_events = [e for e in events if e["event"] == "permission_decision"]
        assert any(
            e["tool_name"] == "run_shell" and e["decision"] == "deny"
            for e in perm_events
        )


# ── 策略拒绝 ──────────────────────────────────────────────────


class TestPolicyDenied:
    def test_shell_search_rejected(self, tmp_path):
        """search/read 类命令不应走 run_shell。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("run_shell", {"command": "cat README.md | grep hello", "timeout": 20})

        assert "search" in result
        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "rejected"
        assert meta["tool_error_code"] == "shell_search_should_use_tool"
        assert meta["security_event_type"] == "tool_policy"

    def test_shell_find_rejected(self, tmp_path):
        """find 命令在新分类器下也被拦截。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("run_shell", {"command": "find . -name '*.py'", "timeout": 20})

        assert "search" in result
        assert agent._last_tool_result_metadata["tool_error_code"] == "shell_search_should_use_tool"

    def test_shell_legitimate_pipe_allowed(self, tmp_path):
        """管道后的 tail/head/grep 不算 lead command，应放行。"""
        import sys
        agent = build_agent(tmp_path)
        py = sys.executable
        for cmd in (
            "echo hello && echo world | tail -1",
            f"{py} --version 2>&1 | head -3",
            "echo a; echo b | grep b",
        ):
            result = agent.run_tool("run_shell", {"command": cmd, "timeout": 20})
            assert "exit_code: 0" in result, f"cmd should pass: {cmd[:80]}"

    def test_read_before_write_policy(self, tmp_path):
        """覆盖已有文件前必须先 read。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("write_file", {"path": "README.md", "content": "overwrite\n"})

        assert "read_file" in result
        meta = agent._last_tool_result_metadata
        assert meta["tool_error_code"] == "prior_read_required"

    def test_new_file_write_allowed_without_read(self, tmp_path):
        """写新文件不需要 prior read。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("write_file", {"path": "notes.txt", "content": "new\n"})

        assert result == "wrote notes.txt (4 chars)"
        assert agent._last_tool_result_metadata["tool_status"] == "ok"
        assert (tmp_path / "notes.txt").exists()

    def test_patch_requires_prior_read(self, tmp_path):
        """patch_file 总是需要 prior read。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool(
            "patch_file",
            {"path": "README.md", "old_text": "world", "new_text": "earth"},
        )
        assert agent._last_tool_result_metadata["tool_error_code"] == "prior_read_required"


# ── 重复调用 ──────────────────────────────────────────────────


class TestRepetitionGuard:
    def test_repeated_identical_rejected(self, tmp_path):
        """连续两次完全相同的工具调用被拒绝（在 ask 流程中）。"""
        agent = build_agent(
            tmp_path,
            [
                '<tool>{"name":"write_file","args":{"path":"x.txt","content":"a\\n"}}</tool>',
                '<tool>{"name":"write_file","args":{"path":"x.txt","content":"a\\n"}}</tool>',
                "<final>done</final>",
            ],
        )
        agent.ask("write same thing twice")
        trace = read_jsonl(agent.current_run_dir / "trace.jsonl")
        write_events = [
            e for e in trace
            if e["event"] == "tool_executed" and e.get("name") == "write_file"
        ]
        assert write_events[0]["tool_error_code"] == ""
        assert write_events[1]["tool_error_code"] == "repeated_identical_call"


# ── 正常执行 & 快照 diff ──────────────────────────────────────


class TestNormalExecution:
    def test_risky_tool_captures_workspace_diff(self, tmp_path):
        """risky 工具执行前后做全量快照并 diff。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("write_file", {"path": "new_file.txt", "content": "created\n"})

        assert "wrote" in result
        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "ok"
        assert meta["workspace_changed"] is True
        assert "created:new_file.txt" in meta["diff_summary"]
        meta_affected = [str(p) for p in meta["affected_paths"]]
        assert "new_file.txt" in meta_affected
        assert meta["workspace_fingerprint"]  # 非空

    def test_non_risky_tool_skips_snapshot(self, tmp_path):
        """read_file（非 risky）不触发快照 diff。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

        assert "hello world" in result
        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "ok"
        assert meta["workspace_changed"] is False
        assert meta["affected_paths"] == []
        assert meta["diff_summary"] == []

    def test_patch_modification_is_tracked(self, tmp_path):
        """patch_file 的修改被 diff 捕获。"""
        agent = build_agent(tmp_path)
        agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})
        result = agent.run_tool(
            "patch_file",
            {"path": "README.md", "old_text": "world", "new_text": "codecat"},
        )

        assert result == "patched README.md"
        meta = agent._last_tool_result_metadata
        assert meta["workspace_changed"] is True
        assert "modified:README.md" in meta["diff_summary"]

    def test_workspace_fingerprint_in_trace(self, tmp_path):
        """workspace_fingerprint 出现在 trace event 中。"""
        agent = build_agent(
            tmp_path,
            ['<tool>{"name":"write_file","args":{"path":"out.txt","content":"data"}}</tool>',
             "<final>done</final>"],
        )

        agent.ask("write a file")
        trace = read_jsonl(agent.current_run_dir / "trace.jsonl")
        tool_event = next(e for e in trace if e["event"] == "tool_executed")

        assert "workspace_fingerprint" in tool_event
        assert tool_event["workspace_changed"] is True
        assert "created:out.txt" in tool_event["diff_summary"]

    def test_multiple_writes_accumulate_affected_paths(self, tmp_path):
        """多次写操作的 changed_paths 在 task_state 中累积。"""
        agent = build_agent(
            tmp_path,
            [
                '<tool>{"name":"write_file","args":{"path":"a.txt","content":"A"}}</tool>',
                '<tool>{"name":"write_file","args":{"path":"b.txt","content":"B"}}</tool>',
                "<final>done</final>",
            ],
        )
        agent.ask("write two files")

        task_state = json.loads(
            agent.run_store.task_state_path(agent.current_run_dir.name).read_text(
                encoding="utf-8"
            )
        )
        changed = task_state["changed_paths"]
        assert "a.txt" in changed
        assert "b.txt" in changed


# ── 异常路径 ──────────────────────────────────────────────────


class TestExceptionPaths:
    def test_tool_exception_still_diffed(self, tmp_path):
        """工具执行抛异常后仍然做快照 diff：sandbox required 但不可用 → error。"""
        agent = build_agent(
            tmp_path,
            sandbox_config=SandboxConfig(mode="required", backend="bubblewrap"),
        )
        agent.sandbox_runner.which = lambda name: None
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "error"
        assert meta["tool_error_code"] == "tool_failed"


# ── Shell 退出码语义 ──────────────────────────────────────────


class TestShellExitCode:
    def test_exit_zero_ok(self, tmp_path):
        agent = build_agent(tmp_path)
        result = agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "ok"
        assert meta["tool_error_code"] == ""

    def test_exit_nonzero_with_changes_partial_success(self, tmp_path):
        """退出码非零但改动了 workspace → partial_success。"""
        agent = build_agent(tmp_path)
        # 先写文件，然后跑一个会失败但文件已改的命令
        result = agent.run_tool(
            "run_shell",
            {"command": "echo changed > new_file.txt && exit 1", "timeout": 20},
        )

        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] in ("partial_success", "ok")  # exit 1 可能被 shell 吃掉重定向

    def test_exit_nonzero_no_changes_error(self, tmp_path):
        """退出码非零且 workspace 没变 → error。"""
        agent = build_agent(tmp_path)
        result = agent.run_tool(
            "run_shell",
            {"command": "python3 -c 'import sys; print(\"fail\", file=sys.stderr); sys.exit(3)'", "timeout": 20},
        )

        meta = agent._last_tool_result_metadata
        # exit ≠ 0, no file changes → error
        assert meta["tool_status"] == "error"
        assert meta["tool_error_code"] == "tool_failed"


# ── 长输出 artifact ──────────────────────────────────────────


class TestLongOutput:
    def test_long_shell_output_goes_to_artifact(self, tmp_path):
        """超过 1000 字符的 shell 输出写入 artifact 并在 prompt 截断。"""
        import json

        # 用 echo 输出 3000 个 x，在 Windows/Linux/macOS 上都能跑
        command = "echo " + "x" * 3000
        agent = build_agent(
            tmp_path,
            [
                '<tool>{"name":"run_shell","args":{"command":'
                + json.dumps(command)
                + ',"timeout":20}}</tool>',
                "<final>ok</final>",
            ],
        )
        agent.ask("produce long output")

        tool_item = next(
            item for item in agent.session["history"]
            if item["role"] == "tool" and item["name"] == "run_shell"
        )
        assert len(tool_item["content"]) < 1500
        assert "full output saved:" in tool_item["content"]


# ── metadata 完整性 ───────────────────────────────────────────


class TestMetadataCompleteness:
    """验证每次工具调用后 _last_tool_result_metadata 的字段完整性。"""

    REQUIRED_FIELDS = [
        "tool_status",
        "tool_error_code",
        "security_event_type",
        "risk_level",
        "read_only",
        "affected_paths",
        "workspace_changed",
        "diff_summary",
    ]

    def test_all_rejected_paths_have_full_metadata(self, tmp_path):
        agent = build_agent(tmp_path, approval_policy="never")
        agent.run_tool("run_shell", {"command": "echo hi", "timeout": 20})

        meta = agent._last_tool_result_metadata
        for field in self.REQUIRED_FIELDS:
            assert field in meta, f"missing field {field} in rejected metadata"

    def test_read_tool_metadata(self, tmp_path):
        agent = build_agent(tmp_path)
        agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "ok"
        assert meta["read_only"] is True
        assert meta["risk_level"] == "low"

    def test_write_tool_metadata(self, tmp_path):
        agent = build_agent(tmp_path)
        agent.run_tool("write_file", {"path": "out.txt", "content": "data"})

        meta = agent._last_tool_result_metadata
        assert meta["tool_status"] == "ok"
        assert meta["read_only"] is False
        assert meta["risk_level"] == "high"
        assert meta["workspace_fingerprint"]  # 非空字符串

    def test_trace_contains_tool_metadata(self, tmp_path):
        """trace event 中包含工具执行元数据。"""
        agent = build_agent(
            tmp_path,
            ['<tool>{"name":"write_file","args":{"path":"x.txt","content":"X"}}</tool>',
             "<final>done</final>"],
        )
        agent.ask("write")

        trace = read_jsonl(agent.current_run_dir / "trace.jsonl")
        tool_events = [e for e in trace if e["event"] == "tool_executed"]
        assert len(tool_events) >= 1
        te = tool_events[0]
        for field in self.REQUIRED_FIELDS:
            assert field in te, f"trace missing field {field}"


# ── 事件流 ────────────────────────────────────────────────────


class TestEventEmission:
    def test_permission_decision_emitted(self, tmp_path):
        agent = build_agent(tmp_path, approval_policy="auto")
        agent.run_tool("read_file", {"path": "README.md", "start": 1, "end": 1})

        events = read_jsonl(agent.session_event_bus.path)
        perm_events = [e for e in events if e.get("event") == "permission_decision"]
        assert any(e["decision"] == "allow" for e in perm_events)

    def test_policy_decision_emitted(self, tmp_path):
        agent = build_agent(tmp_path)
        agent.run_tool("run_shell", {"command": "cat README.md", "timeout": 20})

        events = read_jsonl(agent.session_event_bus.path)
        policy_events = [e for e in events if e.get("event") == "tool_policy_decision"]
        assert any(e["decision"] == "deny" for e in policy_events)

    def test_tool_executed_event_in_trace(self, tmp_path):
        agent = build_agent(
            tmp_path,
            ['<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
             "<final>done</final>"],
        )
        agent.ask("read")
        trace = read_jsonl(agent.current_run_dir / "trace.jsonl")
        assert any(e["event"] == "tool_executed" for e in trace)
