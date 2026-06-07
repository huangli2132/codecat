# 模块学习指南：从最小 Agent 到 Pico v3

这篇文档不先罗列目录，而是先问“这个模块为什么需要存在”。读 Pico 时，先把最小 Agent 的四件事抓住，再看 Pico v3 是怎么把它们扩展成一个本地 coding agent runtime harness。

最小 Agent 通常只有四块：

```text
Context -> LLM -> Tools -> Agent loop
```

Pico v3 仍然是这条链，但每一块都长出了工程边界：

```text
CLI/TUI
  -> Pico runtime object graph
  -> Engine turn loop
  -> ContextManager prompt assembly
  -> Provider complete()
  -> model_output parser
  -> tool_executor boundary
  -> session/run/memory/evidence writes
```

学习 Pico 时，不要从“文件很多”开始。应该从下面 8 个学习问题开始。

## 1. Agent loop：一次请求怎么推进

最小 Agent 里，`agent.py` 往往直接写一个 ReAct 循环：调用模型、判断是否有工具、执行工具、把结果塞回上下文。

Pico 把这件事拆成两层：

| 层 | 文件 | 该看什么 |
| --- | --- | --- |
| 运行现场 | `codecat/core/runtime.py` | `Pico.__init__()` 挂载 workspace、session、memory、tools、workers、permissions、context manager。 |
| turn 循环 | `codecat/core/engine.py` | `Engine.run_turn()` 创建 run、构建 prompt、调用 provider、执行工具、写 report。 |
| 协议解析 | `codecat/core/model_output.py` | 把模型文本解析成 `tool`、`tools`、`final`、`retry`。 |

读的时候先看 `Pico.ask()`，它很薄，只把请求交给 `Engine`。真正的控制流在 `Engine.run_turn()`。这能看出 Pico 的核心取舍：`Pico` 负责持有状态，`Engine` 负责推进状态。

配套测试先看：

- `tests/test_v3_runtime.py`
- `tests/test_engine_acceptance.py`
- `tests/test_runtime_evidence_acceptance.py`

## 2. Context：为什么不是简单 messages list

最小 Agent 的 context 通常只是消息列表，加一个裁剪函数。Pico 的 context 要处理更多来源：系统前缀、工作记忆、durable memory、skills、相关记忆、历史、当前请求、todo、runtime mode。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/core/context_manager.py` | prompt section、预算、floor、裁剪顺序、metadata。 |
| `codecat/core/turn_history.py` | history 渲染、tail clip、重复读取折叠、文件摘要复用。 |
| `codecat/core/context_usage.py` | prompt/token 估算和 usage metadata。 |
| `codecat/core/compact.py` | 手动 compact，把旧历史折成 summary。 |

这一层要重点看两个问题。

第一，prompt 不是一段字符串，而是一组 section。每个 section 有预算，超出时按规则裁剪。Pico 不追求无限上下文，而是让不同信息有不同寿命。

第二，context metadata 会写进 run report。也就是说，prompt 构建不是黑盒，后面可以复盘“为什么模型当时看到了这些信息”。

配套测试：

- `tests/test_context_manager.py`
- `tests/test_context_governance_acceptance.py`
- `tests/test_usage.py`

## 3. Tool boundary：工具为什么不能只是函数调用

最小 Agent 的 tools 往往是一个 registry：工具名、schema、Python 函数。Pico 里的工具边界更厚，因为 coding agent 会真的读写仓库、跑 shell、改文件。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/tools/registry.py` | 内置工具定义、参数校验、runner。 |
| `codecat/core/tool_executor.py` | 工具执行总闸口。 |
| `codecat/core/permissions.py` | approval、tool profile、plan mode、worker write scope。 |
| `codecat/core/tool_policy.py` | read-before-write、不要用 shell 做搜索/读文件等行为策略。 |
| `codecat/core/tool_repetition.py` | 重复工具调用保护。 |
| `codecat/features/sandbox/` | `run_shell` 的 bubblewrap/降级执行层。 |

读这层时按执行顺序看：

```text
tool name
  -> registry lookup
  -> validate args/path
  -> permission decision
  -> policy decision
  -> repetition guard
  -> workspace snapshot
  -> runner
  -> affected paths / trace metadata
```

这里的关键认知是：工具不是“模型可以调用的函数”，而是“可审计的外部动作”。Pico 的很多 v3 修复都在这一层，比如 shell policy 对管道后 `head/tail/grep` 的处理、写文件前 fresh read 的要求、plan mode 写边界。

配套测试：

- `tests/test_tool_policy_acceptance.py`
- `tests/test_permissions_acceptance.py`
- `tests/test_safety_invariants.py`
- `tests/test_sandbox_runner.py`
- `tests/test_sandbox_config.py`

## 4. Memory：短期上下文和长期记忆怎么分开

最小 Agent 常见做法是把历史都放进 messages。Pico v3 的记忆分两条线：session 内 working memory 和跨 session durable memory。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/features/memory.py` | working memory、daily log、durable topics、retrieval、promotion、auto-dream。 |
| `codecat/core/runtime.py` | `memory_text()`、`maintain_memory_after_turn()`、`run_dream()` 的 runtime 接入。 |
| `codecat/core/context_manager.py` | 把 memory 和 relevant memory 注入 prompt。 |

先看 `LayeredMemory`，再看文件级 durable memory：

```text
working memory
  -> current task summary
  -> touched files
  -> file summaries
  -> episodic notes

.codecat/memory/
  -> MEMORY.md
  -> topics/
  -> logs/
  -> auto-dream lock
```

Pico 的核心取舍是：不是把所有历史塞回 prompt，而是把稳定事实提升为 durable topic，把临时上下文留在 session 里。auto-dream 是后台整理器，不是主循环必须等待的步骤。

配套测试：

- `tests/test_memory.py`
- `tests/test_context_governance_acceptance.py`
- `tests/test_release_smoke.py`

## 5. Provider：模型后端为什么要收口成一个接口

最小 Agent 往往只支持一个模型 API。Pico v3 支持 OpenAI-compatible、Anthropic-compatible、DeepSeek profile，但 runtime 不应该知道这些协议细节。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/config/__init__.py` | `.env`、`.codecat.toml`、全局配置、环境变量、CLI override。 |
| `codecat/providers/clients.py` | OpenAI `/responses`、Anthropic `/messages`、SSE/JSON、usage/cache/error 抽取。 |
| `codecat/providers/errors.py` | ProviderError 和错误 metadata。 |
| `codecat/cli.py` | `_build_model_client()` 和 `model_client_factory()`。 |
| `codecat/core/runtime_secrets.py` | secret redaction。 |

学习重点不是“怎么调 API”，而是看 provider 层怎么保护 runtime：

- runtime 只调用 `complete(prompt, max_new_tokens, ...)`。
- provider 负责 HTTP、重试、usage、cache metadata、错误分类。
- CLI 负责把配置 profile 装成具体 client。
- trace/report 里不能泄漏 key，所以 secret redaction 是 provider 配套能力。

配套测试：

- `tests/test_pico.py`
- `tests/test_release_smoke.py`
- `tests/test_usage.py`

## 6. User surface：CLI、slash、skills、TUI 怎么分工

交互层会改变系统形态。Pico 里 CLI、slash command、skills、TUI 都不是主循环，但它们决定用户怎么驱动主循环。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/cli.py` | 参数解析、provider/sandbox/session 装配、one-shot/REPL/TUI 分流。 |
| `codecat/commands/slash.py` | slash command 元信息、补全、`/subagent` 参数解析。 |
| `codecat/features/skills.py` | skill frontmatter、发现、prompt section、slash 解析。 |
| `codecat/features/skills_runtime.py` | skill invoke。 |
| `codecat/tui/app.py` | Textual app，驱动同一个 runtime event stream。 |
| `codecat/tui/widgets.py` | ChatLog、ToolCard、ConfirmPrompt、AskUserPrompt、StatusBar 等控件。 |

这里要分清三种东西：

- CLI/REPL/TUI 是入口和呈现层。
- slash command 是本地控制命令，有些不进模型。
- skill 是 prompt-driven workflow，不给 agent 新工具，而是告诉 agent 如何组合已有工具。

Pico 的 TUI 不重新实现 agent loop。它调用同一个 runtime，把 runtime events 渲染成消息、工具卡片、审批框和状态栏。这里的原则是：UI 层应该事件驱动，但不能抢走 runtime 的决策权。

配套测试：

- `tests/test_skills_acceptance.py`
- `tests/test_tui.py`
- `tests/test_ask_user.py`

## 7. Plan、Todo、Worker：复杂任务怎么有控制面

最小 Agent 只有一个循环。复杂任务需要计划、任务账本和子 agent，否则所有探索、修改、验证都会挤进同一个上下文。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/core/plan_mode.py` | plan artifact、plan profile、final gate。 |
| `codecat/core/todo_ledger.py` | session-scoped todo ledger。 |
| `codecat/core/worker_manager.py` | worker spawn/continue/stop、状态保存、notification queue。 |
| `codecat/core/worker_runtime.py` | child runtime 构造，区分 Explore 和 worker。 |
| `codecat/core/worker_execution.py` | 后台执行、结果写回、通知生成。 |
| `codecat/tools/agents.py` | `agent`、`send_message`、`task_stop` 工具面。 |
| `codecat/tools/todos.py` | todo 工具面。 |
| `codecat/tools/plan.py` | plan mode 工具面。 |

学习顺序：

1. 先看 `PlanModeManager.enter()`，理解 plan mode 不是提示词，而是 runtime mode 和 tool profile。
2. 再看 `TodoLedger`，理解 todo 会进 prompt，也会写 task state。
3. 最后看 `WorkerManager.spawn()`，理解子 agent 是受限 child runtime，不是另一个无边界 agent。

这一层的核心边界是：

- plan mode 下只能写 active plan artifact。
- Explore worker 是只读。
- worker 能写，但必须受 write scope 限制。
- worker 结果通过 notification 回到主循环。

配套测试：

- `tests/test_agent_workers_acceptance.py`
- `tests/test_todo_ledger_acceptance.py`
- `tests/test_ask_user.py`
- `tests/test_task_state.py`

## 8. Evidence：怎么证明 agent 真的跑过

最小 Agent 通常只返回一段最终文本。Pico v3 的目标不是“看起来回答了”，而是能复盘：这次 run 做了什么、改了什么、为什么停。

关键文件：

| 文件 | 角色 |
| --- | --- |
| `codecat/core/session_store.py` | `.codecat/sessions/<id>.json` 和 event JSONL。 |
| `codecat/core/session_events.py` | session event bus。 |
| `codecat/core/run_store.py` | `.codecat/runs/<run_id>/`、trace、report、artifacts。 |
| `codecat/core/task_state.py` | status、stop_reason、tool_steps、attempts、changed_paths。 |
| `codecat/evaluation/run_evidence.py` | 从真实 run/session artifacts 读证据。 |
| `codecat/evaluation/evaluator.py` | benchmark fixture、scripted outputs、verifier。 |
| `codecat/evaluation/metrics.py` | metrics 聚合和 report。 |
| `scripts/run_v3_human_scenario_gate.py` | 50 个真人场景 gate。 |

读这层要抓住三个产物：

```text
.codecat/sessions/<session_id>.json
.codecat/sessions/<session_id>.events.jsonl
.codecat/runs/<run_id>/{task_state.json,trace.jsonl,report.json}
```

`task_state` 回答“现在到哪一步了”，`trace` 回答“每一步发生了什么”，`report` 回答“最后怎么收口”。v3 的真人场景测试就是围绕这些证据文件做验收。

配套测试：

- `tests/test_run_store.py`
- `tests/test_run_evidence.py`
- `tests/test_evaluator.py`
- `tests/test_metrics.py`
- `tests/test_business_scenario_dogfood.py`
- `tests/test_real_session_acceptance.py`

## 推荐阅读路线

如果你是第一次读 Pico：

1. `release/v3/learning/01-overall-architecture.md`
2. `release/v3/learning/10-module-learning-guide.md`
3. `release/v3/learning/02-runtime-engine.md`
4. `release/v3/learning/04-tools-permissions-sandbox.md`
5. `release/v3/learning/08-session-run-evaluation.md`
6. `release/v3/learning/09-module-map.md`

如果你已经懂最小 Agent，直接按源码读：

```text
codecat/cli.py
  -> codecat/core/runtime.py
  -> codecat/core/engine.py
  -> codecat/core/context_manager.py
  -> codecat/core/tool_executor.py
  -> codecat/tools/registry.py
  -> codecat/features/memory.py
  -> codecat/core/run_store.py
```

如果你要准备面试讲法，按问题读：

| 面试问题 | 先读 |
| --- | --- |
| 你这个项目不就是套 API 吗？ | `01-overall-architecture.md`、本篇第 1 节和第 8 节 |
| 长任务上下文怎么控制？ | `03-context-memory-compact.md`、本篇第 2 节 |
| 工具调用怎么保证安全？ | `04-tools-permissions-sandbox.md`、本篇第 3 节 |
| 记忆不是把历史塞回去吗？ | `03-context-memory-compact.md`、本篇第 4 节 |
| 多模型怎么接？ | `06-providers-config.md`、本篇第 5 节 |
| 子 agent 怎么不是乱跑？ | `05-workers-plan-todo.md`、本篇第 7 节 |
| 怎么证明它真的可用？ | `08-session-run-evaluation.md`、`release/v3/TESTING.md`、本篇第 8 节 |

## 一句话总结

这条学习路线不是停在 minimal loop，而是从 minimal loop 长出 runtime harness。前者解决“Agent 怎么跑起来”，后者解决“Agent 怎么在真实仓库里可控、可恢复、可复盘地跑下去”。
