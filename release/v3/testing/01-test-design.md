# Pico v3 真人使用场景测试设计

## 结论

v3 相对 `main` 不是一个小补丁，而是把 Pico 从一个单层 CLI agent 推到了本地 coding agent runtime harness：入口层多了 TUI/REPL/one-shot 的选择，控制层拆出了 Engine、run artifacts、session event bus、plan mode、worker manager，能力层多了 skills、todo、sandbox、provider profile、分层记忆和 auto-dream。

所以这批测试不应该继续只走 `Pico(...)` 单元对象。它应该模拟人从终端打开 Pico、输入自然语言或 slash command、确认审批、退出、恢复 session，然后再检查 `.codecat/sessions/`、`.codecat/runs/`、工作区文件和终端/TUI 展示。

本文档只设计场景，不实现 runner。

## 范围

### Building

设计 50 个端到端场景，每个场景都从用户入口驱动 Pico：

- Computer Use 操作 macOS Terminal / iTerm / Codex 内置终端里的 TUI。
- PTY/expect 操作 `uv run codecat --repl`，模拟键盘输入、回车、等待输出。
- one-shot CLI 操作 `uv run codecat "<prompt>"`，模拟用户直接执行一次任务。
- 事后验证只读检查 `.codecat/sessions/*.json`、`.codecat/sessions/*.events.jsonl`、`.codecat/runs/*/{task_state.json,trace.jsonl,report.json}` 和工作区文件。

### Not Building

- 不写 pytest、runner、fixture、Computer Use 脚本。
- 不跑 50 个场景。
- 不提交真实 API key，不在文档里写密钥值。
- 不把现有 unit tests 全部替换掉；这批场景是人机入口层的 acceptance 补充。

## v3 改动面

从当前 `v3` 分支对 `main` 的 diff 看，核心改动覆盖 111 个文件，约 12562 行新增、2310 行删除。场景设计按以下能力面覆盖：

| 改动面 | 代表模块 / 文档 | 必须覆盖的用户行为 |
|---|---|---|
| Engine 与 run artifacts | `codecat/core/engine.py`, `codecat/core/runtime.py`, `codecat/core/run_store.py` | 一次任务从输入到 final 全程写 session events、trace、report |
| TUI / REPL / one-shot 入口 | `codecat/cli.py`, `codecat/tui/*` | TTY 默认 TUI、`--repl` 退回普通 REPL、prompt 参数走 one-shot |
| Slash commands | `codecat/commands/slash.py` | `/help`、`/session`、`/usage`、`/context`、`/model`、`/history`、`/resume` 等 |
| Plan mode | `codecat/core/plan_mode.py`, `codecat/tools/plan.py` | 只能写 active plan artifact，未写计划不能 final，退出后恢复 default |
| Tool policy / permission | `codecat/core/tool_policy.py`, `codecat/core/permissions.py`, `codecat/core/tool_executor.py` | read-before-write、shell search 拒绝、审批 ask/auto/never |
| Sandbox | `codecat/features/sandbox/*` | `required` 缺 backend fail closed，`best_effort` degrade 可见 |
| Skills | `codecat/features/skills*.py`, `docs/skills.md` | 内置 skill、项目 skill、参数替换、allowed-tools、fork、prompt-only |
| Subagent / worker | `codecat/core/worker_*.py`, `codecat/tools/agents.py` | Explore 只读、worker 写 scope、续接、停止、计划模式禁止写 worker |
| Todo ledger | `codecat/core/todo_ledger.py`, `codecat/tools/todos.py` | todo_add/update/list 写入 report 和 prompt |
| Memory / auto-dream | `codecat/features/memory.py`, `docs/memory.md` | `/remember`、`/dream`、topic 文件、secret-shaped 内容拒绝、auto-dream gate |
| Context governance | `codecat/core/context_manager.py`, `codecat/core/compact.py` | `/compact`、自动 compact、context usage 进 report |
| Provider profiles | `codecat/config/__init__.py`, `codecat/providers/clients.py` | OpenAI-compatible、Anthropic-compatible、DeepSeek profile、usage/cache/error 元数据 |
| Safety / redaction | `codecat/core/runtime_secrets.py`, `codecat/core/workspace.py` | path traversal 拒绝、symlink 越界拒绝、trace/report 脱敏 |
| Release dogfood | `scripts/run_business_scenario_dogfood.py` | 真实业务任务跑完后有代码、测试、report、trace、events |

## 执行方式

### 推荐 runner 形态

```
scenario.yaml
  -> 创建临时 workspace
  -> 启动 uv run codecat --cwd <workspace> --repl 或 --tui
  -> Computer Use / PTY 逐行输入
  -> 等待 final / prompt 回来
  -> 退出 Pico
  -> 只读验证工作区和 .codecat artifacts
```

测试入口必须像人一样使用 Pico。验证器可以直接读文件，但不能绕过入口去 import `Pico` 调 runtime 方法。

### 驱动选择

| Driver | 用途 |
|---|---|
| Computer Use | TUI、审批 prompt、ask_user 选择、slash suggestion、Terminal 真实键盘行为 |
| PTY/expect | REPL 稳定输入输出、`/resume`、`/history`、`/compact`、多轮对话 |
| one-shot CLI | 一次性任务、provider 配置、release smoke、失败出口 |
| artifact verifier | 读取 JSON/JSONL、检查 changed paths、trace events、redaction、session id |

Computer Use 当前环境可用；设计依赖的 UI 动作是 `get_app_state`、`type_text`、`press_key`、`click`。PTY 驱动用本机 shell 即可。

### 外部依赖

| 依赖 | 用途 | 降级策略 |
|---|---|---|
| `uv` | 启动本地 Pico、临时 pytest | 没有 `uv` 时场景标记 environment failure |
| Python 3.10+ | Pico 运行要求 | 低版本直接失败 |
| provider profile / API key | live provider 场景 | 非 live 场景不依赖；live 场景只读取本地配置，不把 key 写进产物 |
| Computer Use | TUI/审批/ask_user 场景 | PTY 可覆盖 REPL，但不能替代 TUI 可视行为 |
| bubblewrap | Linux sandbox required 场景 | macOS 上验证 required fail closed / best_effort degrade，不要求安装 |

## 通过标准

每个场景至少满足四类证据：

1. 用户入口证据：有真实 CLI/TUI/REPL 输入输出记录，或 Computer Use 操作步骤。
2. 行为证据：工作区文件、命令输出或 UI 文本符合预期。
3. Runtime 证据：`.codecat/runs/<run_id>/report.json`、`trace.jsonl`、`task_state.json` 存在且字段正确。
4. Session 证据：`.codecat/sessions/<session_id>.json` 和 `.events.jsonl` 记录关键事件。

## 50 个场景

### 真实业务场景 1-5

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| R01 | 学生管理系统 CRUD 脚手架 | PTY REPL | 在空 workspace 输入“写一个学生管理系统，包含 Student dataclass、增删改查、pytest 测试，跑测试后总结” | 默认 max_steps=50、read/write/run_shell、artifact graph、verifier suggestions | `students.py`、`tests/test_students.py` 存在；pytest pass；report 有 changed_paths、verifier_suggestions、status completed |
| R02 | 订单价格折扣 bugfix | one-shot CLI | 在已有 `src/order_pricing.py` 和失败测试上输入“定位折扣计算错误并修复，跑 pytest” | read-before-write、patch_file、run_shell、真实业务 dogfood | 公式变成 `subtotal - discount + tax`；trace 有 read_file/patch_file/run_shell；外部 pytest pass |
| R03 | 发布就绪审查报告 | PTY REPL + project skill | 创建 `.codecat/skills/release/SKILL.md`，用户输入 `/release billing-api` | project skill、allowed-tools、write_file、skill_invoked/skill_completed | `reports/release-readiness.md` 写出；events 有 skill_invoked/skill_completed；业务文件未被改 |
| R04 | 线上事故续接修复 | PTY REPL 两次启动 | 第一次让 Pico 读事故测试并 `todo_add` 后退出；第二次 `uv run codecat --resume latest --repl` 输入“继续修复并跑测试” | `/resume`、session persistence、todo ledger、patch_file、run artifacts | 同一 session id；todo_1 done；`classify_latency` 修复；第二次 report 有 todos 和 todo_changes |
| R05 | 库存 CSV 导入器 | TUI + Computer Use | 在 TUI 输入“写库存 CSV 导入器，跳过坏行，生成测试，运行测试”，审批写文件 | TUI tool card、approval prompt、write_file、run_shell、final rendering | TUI 出现 tool card success；`inventory_importer.py` 和测试存在；pytest pass；events 有 permission_decision allow |

### 入口与交互场景 6-14

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S06 | TTY 默认进入 TUI | Computer Use | 在 Terminal 执行 `uv run codecat --cwd <workspace>` | `interaction_mode` 默认 TUI | 屏幕有 Pico TUI status bar；没有直接执行 one-shot |
| S07 | `--repl` 进入普通 REPL | PTY | 执行 `uv run codecat --cwd <workspace> --repl`，输入 `/help` | REPL fallback、slash help | 输出含 `Commands:`、`/memory`、`/subagent` |
| S08 | prompt 参数走 one-shot | one-shot CLI | 执行 `uv run codecat --cwd <workspace> "列出 README 摘要"` | one-shot 入口、无交互 session | 命令退出码为 0；report status completed；没有等待 REPL 输入 |
| S09 | piped stdin 使用 REPL | PTY | `printf '/help\n/exit\n' | uv run codecat --cwd <workspace> --repl` | 非 TTY 行为 | 输出命令列表并正常退出 |
| S10 | TUI slash suggestion | Computer Use | TUI 输入 `/sub`，按 Tab | `SlashSuggestions`、slash registry | 输入框变成 `/subagent `；suggestion 面板消失 |
| S11 | `/session` 展示 runtime 状态 | PTY REPL | 输入 `/plan refactor-auth` 后输入 `/session` | session command、runtime mode、worker summary | 输出含 session id、events path、runtime mode plan、plan path、worker summary |
| S12 | `/usage` 展示 provider metadata | PTY REPL | 完成一次简单任务后输入 `/usage` | usage command、provider/model/cached tokens 字段 | 输出含 model、base url host、last input/output tokens；report 不泄露 key |
| S13 | `/model` 只改当前 runtime | PTY REPL | 输入 `/model gpt-test-local`，再 `/model` | runtime-only model switch | 输出 `model: gpt-test-local`；workspace 没有新增 `.codecat.toml` |
| S14 | `/clear` 开新 session | PTY REPL | 完成一轮任务，输入 `/clear`，再 `/session` | session lifecycle、worker cleanup | 新 session id 不同；旧 session 文件仍存在；current run state 清空 |

### Plan mode 场景 15-20

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S15 | 计划模式只能写 active plan | PTY REPL | `/plan auth-refactor` 后要求“直接改 src/auth.py” | plan tool profile、write path gate | 输出拒绝写源文件；`src/auth.py` 不存在或未变；events 有 `plan_mode_path_mismatch` |
| S16 | 未写计划不能 final | PTY REPL | `/plan cache` 后要求“只口头说计划完成，不写文件” | `plan_mode.can_finish()` final gate | history 有 runtime_notice；最终必须写 `.codecat/plans/cache-plan.md` 才完成 |
| S17 | absolute plan path 自动归一 | PTY REPL | `/plan student` 后提示写绝对路径 `<workspace>/.codecat/plans/student-plan.md` | plan path normalize | 实际写入 `.codecat/plans/student-plan.md`；未触发越界错误 |
| S18 | 越界 plan path 被拒 | PTY REPL | 让模型尝试写 `.codecat/plans/../escape.md` | plan path traversal guard | 输出 `plan path must stay`；workspace 外无文件 |
| S19 | plan mode 允许 Explore 子 agent | PTY REPL | `/plan payments` 后输入 `/subagent explore inspect README` | plan mode + Explore only | worker 完成或 started；plan mode 保持；events 有 worker_started/worker_finished |
| S20 | plan mode 禁止 worker 写入 | PTY REPL | `/plan payments` 后输入 `/subagent worker --scope src change code` | plan mode worker restriction | 输出 `plan mode only allows Explore agents`；`src/` 未改变 |

### 工具策略、审批与 sandbox 场景 21-29

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S21 | 改文件前必须先读 | one-shot CLI | 要求“把 README 的 world 改成 codecat”，但提示不能先读 | prior_read_required | 第一次 patch 被拒并提示 read_file；随后读文件再 patch；report 有 rejected reminder |
| S22 | 新文件可直接写，覆盖必须读 | PTY REPL | 输入“新建 notes.txt，然后覆盖 README.md” | write_file freshness | `notes.txt` 成功；README 覆盖前被拒，读后才允许 |
| S23 | 自己刚写的文件可 patch | PTY REPL | 输入“写 scripts/check.py 后把 False 改 True” | self-authored freshness | patch 成功；没有额外 read_file 要求 |
| S24 | shell 搜索类命令被拒 | PTY REPL | 输入“用 grep -R 找 TODO” | shell_search_should_use_tool | run_shell 被拒；提示使用 search；events 有 tool_policy_decision deny |
| S25 | pipe 后 head/tail/grep 用于输出管理允许 | PTY REPL | 输入“运行 python --version 2>&1 | head -3” | shell policy 精细化 | run_shell exit_code 0；不被当成 workspace search |
| S26 | 长 shell 输出落 artifact | one-shot CLI | 让 Pico 运行输出 6000 字符的命令 | output clipping、full output artifact | tool history 被裁剪；report/trace 指向 full_output_artifact；artifact 文件含完整输出 |
| S27 | approval `never` 拒绝 risky tool | PTY REPL | `uv run codecat --approval never --repl` 后要求写文件 | PermissionChecker single gate | 输出 approval denied；文件未写；events 有 permission_decision deny |
| S28 | sandbox required 缺 backend fail closed | one-shot CLI | macOS 上执行 `uv run codecat --sandbox required "运行 echo hi"` | sandbox fail closed | 输出 sandbox unavailable；report status failed 或 tool_failed；没有静默直跑 |
| S29 | sandbox best_effort degrade 可见 | one-shot CLI | macOS 上执行 `uv run codecat --sandbox best_effort "运行 echo hi"` | sandbox degrade event | 命令成功输出 hi；events 有 sandbox_unavailable |

### Skills 与命令扩展场景 30-36

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S30 | `/skills` 不调用模型 | PTY REPL | 输入 `/skills` | skill catalog 本地列出 | 输出含 `/review`、`/test`、`/commit`、`/simplify`；无新 run 目录 |
| S31 | 内置 `/review` 带参数 | PTY REPL | 输入 `/review focus auth` | bundled skills dynamic arguments | prompt 中有 Additional Focus；events 有 skill_invoked |
| S32 | 项目 skill 参数替换 | PTY REPL | 创建 `.codecat/skills/deploy/SKILL.md`，输入 `/deploy staging` | `$ARGUMENTS`、`${CODECAT_SKILL_DIR}` | final 提到 staging；events 有 skill_invoked；prompt 含 skill dir |
| S33 | allowed-tools 限制写操作 | PTY REPL | 创建只允许 `read_file` 的 `/readonly` skill，用户要求它写文件 | skill allowed_tools | run_shell/write_file 被拒；目标文件不存在；events reason `tool_not_allowed` |
| S34 | fork skill 不污染主 history | PTY REPL | 先输入普通消息，再执行 context=fork 的 `/inspect README.md` | fork isolated session | 主 session history 保持原对话；events 有 skill_completed context fork |
| S35 | prompt-only skill 不发模型请求 | PTY REPL | 创建 `disable-model-invocation: true` 的 `/template hello` | prompt-only skill | 直接输出渲染文本；无 model_requested 事件 |
| S36 | invalid skill frontmatter 可诊断 | PTY REPL | 创建缺 `description` 的 skill，输入 `/skills` | skill loader robustness | 输出不展示坏 skill 或展示错误；session 不崩溃 |

### Subagent / worker 场景 37-42

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S37 | Explore 子 agent 只读探索 | PTY REPL | `/subagent explore read README and summarize` | Explore child runtime、readonly profile | worker notification 完成；没有 workspace changed；report workers 有 Explore |
| S38 | worker 只能写 scope 内 | PTY REPL | `/subagent worker --scope notes create two notes` | worker write_scope | `notes/` 内文件写入；scope 外文件不存在 |
| S39 | worker 续接同一个 child context | PTY REPL | 先让 worker 写 `notes/first.txt`，再 `send_message` 写 `notes/second.txt` | child runtime continuation | 两个通知都是 `agent_1`；第二轮 prompt 包含第一轮结果 |
| S40 | running worker 不能 send_message | PTY/Computer Use | 启动长任务 worker，运行中输入继续消息 | running worker guard | 输出 `worker is running`；没有并发续接 |
| S41 | task_stop 中止 worker | PTY/Computer Use | 启动阻塞 worker 后输入 `task_stop agent_1` 或对应用户请求 | abort propagation | worker status stopped；child client abort 事件或 trace stop |
| S42 | `/clear` 停掉后台 worker | PTY REPL | 启动 worker 后输入 `/clear` | session clear worker cleanup | worker list 清空；没有旧 notification 混入新 session |

### Memory、context 与恢复场景 43-47

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S43 | `/remember` 写 daily log | PTY REPL | 输入 `/remember 这个项目用 pytest，不用 unittest` | daily log、memory event | `.codecat/memory/logs/YYYY/MM/*.md` 有内容；events 有 memory_note_appended |
| S44 | `/dream` 写 topic 和 MEMORY.md | PTY REPL + live/model | 先 `/remember` 三条稳定事实，再输入 `/dream` | dream child run、memory write scope | `.codecat/memory/MEMORY.md` 和 `topics/*.md` 非空；不会写 memory 外文件 |
| S45 | secret-shaped 记忆拒绝 | PTY REPL | 输入“请记住 API key 是 sk-live-secret-abc” | durable promotion reject secret | topic 文件不含 secret；report 有 durable rejection reason secret_shaped |
| S46 | `/compact` 手动压缩历史 | PTY REPL | 连续输入 16 轮长对话后输入 `/compact` | manual compaction、future history shortening | 输出 JSON 有 pre_tokens > post_tokens；events 有 compaction_created |
| S47 | resume 检测 workspace mismatch | PTY REPL 两次启动 | 第一次完成任务并退出；外部修改文件；第二次 `--resume latest` | checkpoint/runtime identity、resume_state | report prompt_metadata 是 partial-stale 或 workspace-mismatch；trace 有 checkpoint_created |

### Provider、错误和审计场景 48-50

| ID | 场景 | Driver | 用户动作 | 覆盖 v3 改动 | 验收证据 |
|---|---|---|---|---|---|
| S48 | provider profile 切换 | one-shot CLI | 分别执行 `--provider openai`、`--provider anthropic`、`--provider deepseek` 的短任务 | config precedence、provider client selection | `/usage` 或 report 中 provider_protocol/model 正确；base_url 已脱敏；不泄露 key |
| S49 | empty provider response 用户可见 | one-shot CLI + fault endpoint | 用本地假 provider 返回空响应，用户输入“hello” | empty_response retry / failure wording | 最终输出含 `empty_response` 或中文空响应；不再是冷冰冰 `Stopped after model error` |
| S50 | path traversal 与 secret redaction | one-shot CLI | 要求读取 `../outside` 并运行会打印 secret 的命令 | workspace safety、redaction | 越界路径被拒；trace/report 中 secret 值替换为 `<redacted>` |

## 覆盖矩阵

| 能力面 | 覆盖场景 |
|---|---|
| TUI / REPL / one-shot | R05, S06-S14 |
| Session events / run artifacts | R01-R05, S08, S11-S14, S26, S37-S42, S47-S50 |
| Plan mode | S15-S20 |
| Tool policy / permission / sandbox | S21-S29 |
| Skills | R03, S30-S36 |
| Subagent / worker | S19-S20, S37-S42 |
| Todo ledger | R04 |
| Memory / auto-dream | S43-S45 |
| Context / compact / resume | S46-S47 |
| Provider profiles / errors / usage | S12, S48-S49 |
| Safety / redaction | S50 |
| 真实业务 workflow | R01-R05 |

## 优先级

先实现 12 个最小 smoke 场景，作为 v3 release gate：

1. R01 学生管理系统 CRUD 脚手架
2. R02 订单价格折扣 bugfix
3. R04 线上事故续接修复
4. R05 TUI 审批写文件
5. S07 `--repl` + `/help`
6. S15 plan mode 只能写 active plan
7. S21 改文件前必须先读
8. S26 长 shell 输出落 artifact
9. S32 项目 skill 参数替换
10. S37 Explore 子 agent 只读探索
11. S43 `/remember` 写 daily log
12. S50 path traversal 与 secret redaction

再补齐剩余 38 个场景，形成完整 v3 acceptance matrix。

## 风险与变形

### Provider 失败

live provider 场景可能因为网络、限流或模型格式不稳定失败。设计上把 live provider 只放在 R02-R04、S44、S48-S49 等少数场景；其余场景可以用本地假 provider 或严格 prompt 降低不确定性。失败时不把它当成 runtime 失败，除非 report/trace 没有记录 provider error。

### 50 个场景执行时间过长

全量跑 50 个真人入口场景会慢。最小 release gate 是 12 个场景；完整套件可以 nightly 或手动 release 前跑。

### Computer Use 不稳定

TUI/审批/suggestion 只能靠 Computer Use 或 Textual 测试才像真人。为降低脆弱性，Computer Use 场景只验证屏幕上稳定文本和明确键盘动作，不依赖像素级坐标；REPL 场景优先用 PTY。

### 回滚成本

本文档只是设计，无数据迁移。后续实现 runner 时应该新增在 `tests/human_scenarios/` 或 `scripts/run_human_scenarios.py`，不改现有 runtime 逻辑；需要回滚时删除新增测试/脚本即可。

## 需要评审的判断

我的判断是：v3 的 acceptance 不应该追求“50 个都是真模型 live call”。那会测出 provider 抖动，不会更好地测出 runtime。正确做法是入口真人化、证据 runtime 化、provider live 少量抽样。

如果要推翻这个判断，需要证明真实模型随机性本身就是 v3 的主要风险，而不是 Engine、tool policy、memory、subagent 和 artifact 链路的稳定性。
