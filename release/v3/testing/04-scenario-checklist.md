# 50 场景检查清单

这份清单用于复盘、补跑和扩展。详细设计见 `01-test-design.md`，真实执行记录见 `02-execution-record.md`。

## 真实业务场景

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| R01 | 学生管理系统 CRUD 脚手架 | one-shot CLI / DeepSeek | write_file、run_shell、artifact graph、pytest | passed |
| R02 | 订单价格折扣 bugfix | one-shot CLI / DeepSeek | read-before-write、patch_file、真实测试修复 | passed |
| R03 | 发布就绪审查报告 | REPL project skill / DeepSeek | project skill、allowed-tools、skill events | passed |
| R04 | 线上事故续接修复 | two one-shot CLI runs / DeepSeek | resume、todo ledger、patch_file、session persistence | passed |
| R05 | 库存 CSV 导入器 | one-shot CLI / DeepSeek | approval auto、写代码、写测试、跑测试 | passed |

## 入口与交互

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| S06 | TTY 默认进入 TUI | PTY TUI smoke | interaction mode、TUI 启动 | passed |
| S07 | `--repl` 进入普通 REPL | PTY REPL | `/help`、slash help | passed |
| S08 | prompt 参数走 one-shot | one-shot CLI / DeepSeek | one-shot 入口、run artifacts | passed |
| S09 | piped stdin 使用 REPL | PTY-style stdin | 非 TTY REPL 输入退出 | passed |
| S10 | TUI slash suggestion | Python registry smoke | slash registry、`/subagent` 可发现性 | passed |
| S11 | `/session` 展示 runtime 状态 | PTY REPL | session id、events path、runtime mode | passed |
| S12 | `/usage` 展示 provider metadata | one-shot + REPL resume | usage、provider/model metadata、key 不泄漏 | passed |
| S13 | `/model` 只改当前 runtime | PTY REPL | runtime-only model switch | passed |
| S14 | `/clear` 开新 session | PTY REPL | session lifecycle、旧 session 保留 | passed |

## Plan Mode

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| S15 | plan mode 只能写 active plan | REPL + resume / DeepSeek | plan artifact write、坏写入拒绝、重复坏调用收口 | passed |
| S16 | plan 未写前不能 final | PTY REPL / DeepSeek | plan final gate、runtime notice 或计划先写入 | passed |
| S17 | 绝对路径 plan artifact | PTY REPL / DeepSeek | active plan path、绝对路径归一 | passed |
| S18 | plan path escape 被拒绝 | PTY REPL slash command | `/plan` path 校验、REPL 不 crash | passed |
| S19 | plan mode 允许 Explore | PTY REPL / DeepSeek | plan + read-only subagent | passed |
| S20 | plan mode 禁止 worker 写入 | PTY REPL slash command | worker write 禁止、src 不创建 | passed |

## Tool Policy / Permission / Sandbox

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| S21 | 改文件前必须先读 | one-shot CLI / DeepSeek | prior_read_required、补 read 后允许 retry | passed |
| S22 | 新文件可写，覆盖旧文件前必须读 | one-shot CLI / DeepSeek | write_file policy、overwrite guard | passed |
| S23 | 自己刚写的文件可 patch | one-shot CLI / DeepSeek | self-authored freshness、成功改写不能被同参重放回滚 | passed |
| S24 | shell 搜索被拒绝 | one-shot CLI / DeepSeek | shell_search_should_use_tool | passed |
| S25 | pipe 输出管理允许 | one-shot CLI / DeepSeek | `head/tail/grep` 输出管理不误拒 | passed |
| S26 | 长 shell 输出落 artifact | one-shot CLI / DeepSeek | full_output_artifact、trace evidence | passed |
| S27 | `approval=never` 拒绝 risky tool | one-shot CLI / DeepSeek | approval_denied、no file change | passed |
| S28 | required sandbox fail closed | one-shot CLI / DeepSeek | sandbox required unavailable | passed |
| S29 | best_effort sandbox degrade | one-shot CLI / DeepSeek | sandbox_unavailable event、命令仍可跑 | passed |
| S30 | `/skills` 列本地 skill | PTY REPL slash command | no model run、skill discovery | passed |

## Skills / Subagent / Worker

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| S31 | 内置 review skill 参数 | PTY REPL builtin skill / DeepSeek | builtin skill、argument substitution | passed |
| S32 | 项目 skill 参数替换 | REPL slash skill / DeepSeek | project skill、write_file report | passed |
| S33 | skill allowed-tools 限制写入 | PTY REPL project skill / DeepSeek | allowed-tools、只读限制 | passed |
| S34 | fork skill 保留父历史 | one-shot + REPL fork skill | resume latest、fork history | passed |
| S35 | prompt-only skill 不启动模型 | PTY REPL prompt-only skill | prompt-only、no run_* | passed |
| S36 | invalid skill frontmatter 诊断 | PTY REPL slash command | skill loader diagnostic | passed |
| S37 | Explore 子 agent 只读探索 | one-shot CLI / DeepSeek | read-only subagent、src 不写 | passed |
| S38 | Worker write scope | one-shot CLI / DeepSeek | worker scope、指定路径写入 | passed |

## Todo / Memory / Context

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| S39 | worker continuation | one-shot CLI / DeepSeek | worker send/continue、report evidence | passed |
| S40 | running worker send guard | one-shot CLI / DeepSeek | worker 状态保护 | passed |
| S41 | task_stop worker | one-shot CLI / DeepSeek | task_stop、worker completion | passed |
| S42 | `/clear` stops worker | one-shot + REPL clear | clear session、worker cleanup | passed |
| S43 | `/remember` 写 daily log | PTY-style stdin REPL | durable memory、daily log | passed |
| S44 | `/dream` 写 memory | PTY REPL / DeepSeek | dream/consolidation、memory artifacts | passed |
| S45 | secret-shaped memory rejected | one-shot CLI / DeepSeek | durable rejection、secret redaction | passed |

## Provider / Recovery / Safety

| ID | 场景 | Driver | 覆盖重点 | 最终状态 |
|---|---|---|---|---|
| S46 | manual compact | PTY REPL slash command | `/compact`、context reduction | passed |
| S47 | resume workspace mismatch | one-shot + resume | checkpoint/runtime identity mismatch | passed |
| S48 | provider profiles | one-shot slash `/usage` | deepseek/openai/anthropic profile metadata | passed |
| S49 | provider error metadata | one-shot CLI bad endpoint | model_error、provider_error metadata | passed |
| S50 | path traversal 与 secret redaction | one-shot CLI / DeepSeek | path_escape、trace/report redaction | passed |

## 复盘时优先看的场景

| 关注点 | 场景 |
|---|---|
| 真实业务可用性 | R01、R02、R04、R05 |
| 入口层是否像真人使用 | S06、S07、S08、S09、S14 |
| plan mode 是否像一个受控工作流 | S15、S16、S18、S20 |
| 工具安全和回滚防护 | S21、S22、S23、S24、S27、S50 |
| skills/subagent 是否形成 runtime control plane | S31、S32、S34、S37、S38 |
| 长输出、memory、provider 这些真实运行边界 | S26、S43、S44、S45、S48、S49 |
