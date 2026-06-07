"""命令行入口。

这个模块负责把“用户怎么启动 codecat”翻译成 runtime 能理解的对象：
解析参数、挑模型后端、构建工作区快照、恢复或新建 session，
最后进入 one-shot 或交互式循环。
"""

import argparse
import json
import os
import shutil
import sys
import textwrap
from urllib.parse import urlparse

from .commands.slash import command_help_text, parse_subagent_args, resolve_command
from .config import (
    DEFAULT_PROVIDER,
    PROVIDER_DEFAULTS,
    default_max_tokens_for_provider,
    load_project_env,
    resolve_project_sandbox_config,
    resolve_provider_config,
)
from .features import skills as skillslib
from .features.skills_runtime import invoke_skill
from .providers import AnthropicCompatibleModelClient, OpenAICompatibleModelClient
from .core.runtime import Pico, SessionStore
from .core.workspace import WorkspaceContext, middle

DEFAULT_SECRET_ENV_NAMES = (
    "CODECAT_API_KEY",
    "CODECAT_OPENAI_API_KEY",
    "OPENAI_API_KEY",
    "OPENAI_API_TOKEN",
    "CODECAT_ANTHROPIC_API_KEY",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "CODECAT_DEEPSEEK_API_KEY",
    "DEEPSEEK_API_KEY",
    "CODECAT_RIGHT_CODES_API_KEY",
    "RIGHT_CODES_API_KEY",
    "GITHUB_PAT",
    "GH_PAT",
)

WELCOME_ART = (
    "        /\\___/\\\\",
    "       (  o o  )",
    "       /   ^   \\\\",
    "      /|       |\\\\",
)
WELCOME_NAME = "codecat"
WELCOME_SUBTITLE = "local coding agent"
WELCOME_STATUS = "calm shell, ready for work"
HELP_DETAILS = (
    command_help_text()
    + "\n\n"
    + textwrap.dedent(
        """\
    Skill workflows:
    /skill <name> [args] Run a user-invocable skill.
    """
    ).strip()
)


DEFAULT_OPENAI_MODEL = PROVIDER_DEFAULTS["openai"]["model"]
DEFAULT_OPENAI_BASE_URL = PROVIDER_DEFAULTS["openai"]["base_url"]
SECRET_ENV_NAMES_VAR = "CODECAT_SECRET_ENV_NAMES"


def _configured_secret_names(args):
    configured_secret_names = set(DEFAULT_SECRET_ENV_NAMES)
    configured_secret_names.update(str(name).upper() for name in args.secret_env_names)
    extra_names = os.environ.get(SECRET_ENV_NAMES_VAR, "")
    if extra_names.strip():
        configured_secret_names.update(
            item.strip().upper() for item in extra_names.split(",") if item.strip()
        )
    return sorted(configured_secret_names)


def _build_model_client(args):
    config = resolve_provider_config(
        getattr(args, "provider", None),
        start=getattr(args, "cwd", "."),
        config_path=getattr(args, "config", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
    )
    # CLI 只负责把 provider profile 翻译成具体协议 client。
    # 例如 deepseek 是 profile，protocol=anthropic 才决定走 Messages API。
    if config.protocol == "openai":
        return OpenAICompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", 300),
        )
    if config.protocol == "anthropic":
        return AnthropicCompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=args.temperature,
            timeout=getattr(args, "openai_timeout", 300),
        )

    raise ValueError(f"unknown provider protocol: {config.protocol}")


def build_welcome(agent, model, host):
    width = max(68, min(shutil.get_terminal_size((80, 20)).columns, 84))
    inner = width - 4
    gap = 3
    left_width = (inner - gap) // 2
    right_width = inner - gap - left_width

    def row(text):
        body = middle(text, width - 4)
        return f"| {body.ljust(width - 4)} |"

    def divider(char="-"):
        return "+" + char * (width - 2) + "+"

    def center(text):
        body = middle(text, inner)
        return f"| {body.center(inner)} |"

    def cell(label, value, size):
        body = middle(f"{label:<9} {value}", size)
        return body.ljust(size)

    def pair(left_label, left_value, right_label, right_value):
        left = cell(left_label, left_value, left_width)
        right = cell(right_label, right_value, right_width)
        return f"| {left}{' ' * gap}{right} |"

    line = divider("=")
    rows = [center(text) for text in WELCOME_ART]
    rows.extend(
        [
            center(WELCOME_NAME),
            center(WELCOME_SUBTITLE),
            center(WELCOME_STATUS),
            divider("-"),
            row(""),
            row("WORKSPACE  " + middle(agent.workspace.cwd, inner - 11)),
            pair("MODEL", model, "BRANCH", agent.workspace.branch),
            pair("APPROVAL", agent.approval_policy, "SESSION", agent.session["id"]),
            row(""),
        ]
    )
    return "\n".join([line, *rows, line])


def build_agent(args):
    """根据 CLI 参数装配出一个可运行的 Pico 实例。

    为什么存在：
    命令行参数只是字符串和开关，runtime 需要的是已经装配好的对象图：
    model client、workspace snapshot、session store、secret 配置等。
    这个函数负责把“启动参数”翻译成“agent 运行现场”。

    输入 / 输出：
    - 输入：`argparse` 解析后的 `args`
    - 输出：一个新的 `Pico`，或一个从旧 session 恢复出来的 `Pico`

    在 agent 链路里的位置：
    它是整个程序启动链路里最靠近 runtime 的装配点。`main()` 先调它，
    得到 agent 后，后面无论是 one-shot 还是 REPL 模式，都会落到 `ask()`。
    """
    # 这里是 CLI 到 runtime 的装配点：
    # 先采集工作区快照，再整理 secret 名单、模型后端和 session。
    workspace = WorkspaceContext.build(args.cwd)
    store = SessionStore(workspace.repo_root + "/.codecat/sessions")
    provider_config = resolve_provider_config(
        getattr(args, "provider", None),
        start=getattr(args, "cwd", "."),
        config_path=getattr(args, "config", None),
        model=getattr(args, "model", None),
        base_url=getattr(args, "base_url", None),
        api_key=getattr(args, "api_key", None),
    )
    model = _build_model_client(args)

    def model_client_factory():
        return _build_model_client(args)

    if args.max_new_tokens is None:
        args.max_new_tokens = default_max_tokens_for_provider(provider_config.name)

    sandbox_config = resolve_project_sandbox_config(
        start=workspace.repo_root,
        config_path=getattr(args, "config", None),
        mode=getattr(args, "sandbox", None),
        backend=getattr(args, "sandbox_backend", None),
    )
    load_project_env(workspace.repo_root, override=False)
    configured_secret_names = _configured_secret_names(args)
    session_id = args.resume
    if session_id == "latest":
        session_id = store.latest()
    memory_dir = getattr(args, "memory_dir", None)
    auto_dream = not getattr(args, "no_auto_dream", False)
    dream_interval = getattr(args, "dream_interval", 24.0)
    dream_min_sessions = getattr(args, "dream_min_sessions", 5)
    ask_user_callback = None if getattr(args, "prompt", None) else _cli_ask_user
    if session_id:
        return Pico.from_session(
            model_client=model,
            workspace=workspace,
            session_store=store,
            session_id=session_id,
            approval_policy=args.approval,
            max_steps=args.max_steps,
            max_new_tokens=args.max_new_tokens,
            secret_env_names=configured_secret_names,
            memory_dir=memory_dir,
            auto_dream=auto_dream,
            dream_interval_hours=dream_interval,
            dream_min_sessions=dream_min_sessions,
            model_client_factory=model_client_factory,
            sandbox_config=sandbox_config,
            ask_user_callback=ask_user_callback,
        )
    return Pico(
        model_client=model,
        workspace=workspace,
        session_store=store,
        approval_policy=args.approval,
        max_steps=args.max_steps,
        max_new_tokens=args.max_new_tokens,
        secret_env_names=configured_secret_names,
        memory_dir=memory_dir,
        auto_dream=auto_dream,
        dream_interval_hours=dream_interval,
        dream_min_sessions=dream_min_sessions,
        model_client_factory=model_client_factory,
        sandbox_config=sandbox_config,
        ask_user_callback=ask_user_callback,
    )


def build_arg_parser():
    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        description="Minimal coding agent for provider profiles backed by OpenAI-compatible or Anthropic-compatible APIs.",
    )
    parser.add_argument("prompt", nargs="*", help="Optional one-shot prompt.")
    parser.add_argument("--cwd", default=".", help="Workspace directory.")
    parser.add_argument(
        "--config", default=None, help="Path to a Pico TOML config file."
    )
    parser.add_argument(
        "--provider",
        default=None,
        help=f"Provider profile to use. Defaults to config provider or {DEFAULT_PROVIDER}.",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="API key override for the selected provider profile.",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name override for the selected provider profile.",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="API base URL override for the selected provider profile.",
    )
    parser.add_argument(
        "--openai-timeout",
        type=int,
        default=300,
        help="Provider request timeout in seconds.",
    )
    parser.add_argument(
        "--resume", default=None, help="Session id to resume or 'latest'."
    )
    parser.add_argument(
        "--memory-dir",
        default=None,
        help="Memory directory. Defaults to .codecat/memory in the workspace.",
    )
    parser.add_argument(
        "--no-auto-dream",
        action="store_true",
        help="Disable automatic memory consolidation.",
    )
    parser.add_argument(
        "--dream-interval",
        type=float,
        default=24.0,
        help="Hours between automatic dream runs.",
    )
    parser.add_argument(
        "--dream-min-sessions",
        type=int,
        default=5,
        help="Minimum new sessions before automatic dream runs.",
    )
    parser.add_argument(
        "--approval",
        choices=("ask", "auto", "never"),
        default="ask",
        help="Approval policy for risky tools.",
    )
    parser.add_argument(
        "--sandbox",
        choices=("off", "best_effort", "required"),
        default=None,
        help="Sandbox mode for run_shell.",
    )
    parser.add_argument(
        "--sandbox-backend",
        choices=("auto", "bubblewrap", "none"),
        default=None,
        help="Sandbox backend for run_shell.",
    )
    parser.add_argument(
        "--secret-env-name",
        dest="secret_env_names",
        action="append",
        default=[],
        help="Extra environment variable names to treat as secrets for trace/report redaction.",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=50,
        help="Maximum tool/model iterations per request.",
    )
    parser.add_argument(
        "--max-new-tokens",
        type=int,
        default=None,
        help="Maximum model output tokens per step. Defaults to a provider-aware value (anthropic 32000, openai/deepseek 8192).",
    )
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.2,
        help="Sampling temperature sent to the provider.",
    )
    parser.add_argument(
        "--tui", action="store_true", help="Start the Textual terminal UI."
    )
    parser.add_argument(
        "--repl",
        action="store_true",
        help="Use the plain line-oriented REPL instead of the TUI.",
    )
    return parser


def handle_repl_command(agent, user_input):
    raw_command = ""
    command_args = ""
    command_name = ""
    if str(user_input).startswith("/"):
        raw_command, _, command_args = str(user_input)[1:].partition(" ")
        resolved = resolve_command(raw_command)
        command_name = resolved.name if resolved else raw_command.strip().lower()
        command_args = command_args.strip()

    if user_input in {"/exit", "/quit"}:
        return True, True, ""
    if user_input == "/help":
        return True, False, HELP_DETAILS
    if user_input == "/memory":
        return True, False, agent.memory_command_text()
    if user_input == "/working-memory":
        return True, False, agent.memory_text()
    if user_input.startswith("/remember"):
        _, _, note = user_input.partition(" ")
        if not note.strip():
            return True, False, "Usage: /remember <text>"
        agent.remember_durable_note(note)
        return True, False, "Saved to daily log."
    if user_input == "/dream":
        return True, False, agent.run_dream()
    if user_input == "/skills":
        return True, False, skillslib.render_skills_list(agent.skills)
    if user_input == "/plan" or user_input.startswith("/plan "):
        _, _, raw_topic = user_input.partition(" ")
        topic = raw_topic.strip()
        if not topic:
            return True, False, _format_mode_status(agent)
        path = None
        if " " in topic:
            topic, _, path = topic.partition(" ")
            path = path.strip() or None
        try:
            plan_path = agent.enter_plan_mode(topic, path=path)
        except ValueError as exc:
            return True, False, f"error: {exc}"
        return True, False, f"mode: plan\nplan path: {plan_path}"
    if user_input == "/plan-exit":
        agent.exit_plan_mode()
        return True, False, "mode: default"
    if user_input == "/mode":
        return True, False, _format_mode_status(agent)
    if user_input == "/session":
        return True, False, _format_session_status(agent)
    if command_name == "agents":
        return True, False, _format_subagent_status(agent)
    if command_name == "subagent":
        payload, error = parse_subagent_args(command_args)
        if error:
            return True, False, error
        return True, False, agent.run_tool("agent", payload)
    if user_input == "/context":
        return (
            True,
            False,
            json.dumps(
                agent.prompt_metadata("", "")["context_usage"], indent=2, sort_keys=True
            ),
        )
    if user_input == "/usage":
        return True, False, _format_usage(agent)
    if user_input == "/model" or user_input.startswith("/model "):
        _, _, model = user_input.partition(" ")
        model = model.strip()
        if not model:
            return True, False, _format_model(agent)
        setattr(agent.model_client, "model", model)
        agent.session_event_bus.emit("model_changed", {"model": model})
        agent.refresh_prefix(force=True)
        return True, False, f"model: {model}"
    if user_input == "/history":
        return True, False, _format_history(agent)
    if user_input.startswith("/resume "):
        _, _, target = user_input.partition(" ")
        session_id = _resolve_session_id(agent, target.strip())
        if not session_id:
            return True, False, "error: session not found"
        agent.resume_session(session_id)
        return True, False, f"resumed session {session_id}"
    if user_input == "/clear":
        session_id = agent.clear_session()
        return True, False, f"new session {session_id}"
    if user_input == "/compact":
        return (
            True,
            False,
            json.dumps(
                agent.compact_history(trigger="manual"), indent=2, sort_keys=True
            ),
        )
    if user_input == "/reset":
        agent.reset()
        return True, False, "session reset"
    command, arguments = skillslib.parse_slash_command(user_input)
    if command and command in agent.skills:
        return True, False, invoke_skill(agent, command, arguments)
    return False, False, ""


def _format_mode_status(agent):
    lines = [f"runtime mode: {agent.runtime_mode}"]
    plan_path = getattr(agent.plan_mode, "plan_path", "")
    if plan_path:
        lines.append(f"plan path: {plan_path}")
    return "\n".join(lines)


def _format_session_status(agent):
    task_state = getattr(agent, "current_task_state", None)
    run_id = getattr(task_state, "run_id", "") or ""
    run_dir = str(agent.run_store.run_dir(run_id)) if run_id else "-"
    workers = agent.worker_manager.to_dict()
    items = workers.get("items", [])
    worker_summary = "none"
    if items:
        counts = {}
        for item in items:
            status = str(item.get("status", "unknown") or "unknown")
            counts[status] = counts.get(status, 0) + 1
        worker_summary = ", ".join(
            f"{status}={count}" for status, count in sorted(counts.items())
        )
    return "\n".join(
        [
            f"session id: {agent.session.get('id', '')}",
            f"session path: {agent.session_path}",
            f"events path: {agent.session_event_bus.path}",
            f"runtime mode: {agent.runtime_mode}",
            f"plan path: {getattr(agent.plan_mode, 'plan_path', '') or '-'}",
            f"last run id: {run_id or '-'}",
            f"last run dir: {run_dir}",
            f"resume status: {agent.resume_state.get('status', '-')}",
            f"worker summary: {worker_summary}",
        ]
    )


def _format_subagent_status(agent):
    return "\n".join(
        [
            "subagent tools: agent(description, prompt, subagent_type='Explore|worker', write_scope=[]), send_message(to, message), task_stop(task_id)",
            f"worker summary: {_worker_summary(agent)}",
        ]
    )


def _worker_summary(agent):
    items = agent.worker_manager.to_dict().get("items", [])
    if not items:
        return "none"
    return ", ".join(f"{item.get('id')}:{item.get('status')}" for item in items)


def _format_usage(agent):
    metadata = dict(getattr(agent, "last_completion_metadata", {}) or {})
    context_usage = dict(
        (getattr(agent, "last_prompt_metadata", {}) or {}).get("context_usage", {})
        or {}
    )
    base_url = str(getattr(agent.model_client, "base_url", "") or "")
    host = urlparse(base_url).netloc or "-"
    lines = [
        f"provider profile: {getattr(agent.model_client, 'provider', '-') or '-'}",
        f"provider protocol: {getattr(agent.model_client, 'protocol', '-') or '-'}",
        f"model: {getattr(agent.model_client, 'model', '-') or '-'}",
        f"base url host: {host}",
        f"prompt cache supported: {bool(getattr(agent.model_client, 'supports_prompt_cache', False))}",
        f"last input tokens: {metadata.get('input_tokens', 'unavailable')}",
        f"last output tokens: {metadata.get('output_tokens', 'unavailable')}",
        f"last cached tokens: {metadata.get('cached_tokens', 'unavailable')}",
        f"last provider attempts: {metadata.get('provider_attempts', 'unavailable')}",
        f"last provider retry count: {metadata.get('provider_retry_count', 'unavailable')}",
        f"last provider error: {metadata.get('provider_error', 'unavailable')}",
        f"context usage: {context_usage.get('total_estimated_tokens', '-')}/{context_usage.get('context_window', '-')}",
    ]
    return "\n".join(lines)


def _format_model(agent):
    return f"model: {getattr(agent.model_client, 'model', '-') or '-'}"


def _format_history(agent):
    rows = agent.session_store.list_sessions()
    if not rows:
        return "(no sessions)"
    lines = []
    for row in rows:
        lines.append(
            f"{row['index']}. {row['id']} mode={row['runtime_mode']} turns={row['history_count']} "
            f"updated={row['updated_at']} {row['last_final_answer']}"
        )
    return "\n".join(lines)


def _resolve_session_id(agent, target):
    if target == "latest":
        return agent.session_store.latest()
    rows = agent.session_store.list_sessions()
    if target.isdigit():
        index = int(target)
        for row in rows:
            if row["index"] == index:
                return row["id"]
    for row in rows:
        if row["id"] == target:
            return row["id"]
    return ""


def _cli_ask_user(question, choices):
    if choices:
        print(question)
        for index, choice in enumerate(choices, start=1):
            print(f"{index}. {choice}")
        answer = input("> ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(choices):
            return choices[int(answer) - 1]
        return answer
    return input(question + " ").strip()


def _drain_idle_worker_notifications(agent):
    notifications = agent.engine.drain_worker_notifications()
    for notification in notifications:
        print(f"\n[worker notification]\n{notification}")
    return notifications


def interaction_mode(args):
    if args.prompt:
        return "one_shot"
    if getattr(args, "repl", False):
        return "repl"
    if getattr(args, "tui", False) or sys.stdin.isatty():
        return "tui"
    return "repl"


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    try:
        agent = build_agent(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    mode = interaction_mode(args)
    if mode == "tui":
        from .tui.app import PicoTuiApp

        PicoTuiApp(agent).run()
        return 0

    model = getattr(
        agent.model_client, "model", getattr(args, "model", DEFAULT_OPENAI_MODEL)
    )
    host = getattr(
        agent.model_client,
        "base_url",
        getattr(args, "base_url", DEFAULT_OPENAI_BASE_URL),
    )
    print(build_welcome(agent, model=model, host=host))

    if mode == "one_shot":
        # one-shot 模式：只跑一次 ask，不进入 REPL 循环。
        prompt = " ".join(args.prompt).strip()
        if prompt:
            print()
            try:
                handled, _, output = handle_repl_command(agent, prompt)
                print(output if handled else agent.ask(prompt))
            except RuntimeError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        return 0

    while True:
        # 交互模式：每次读取一条用户输入，交给同一个 agent，
        # 因此 session history 和 working memory 会跨轮延续。
        _drain_idle_worker_notifications(agent)
        try:
            user_input = input("\npico> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("")
            return 0

        if not user_input:
            continue
        handled, should_exit, output = handle_repl_command(agent, user_input)
        if should_exit:
            return 0
        if handled:
            print(output)
            continue

        print()
        try:
            print(agent.ask(user_input))
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
