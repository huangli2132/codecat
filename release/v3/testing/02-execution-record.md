# Pico v3 真人场景全量执行记录

执行日期：2026-05-13  
执行分支：`v3`  
默认 provider：`deepseek`  
主计划文档：`release/v3/testing/01-test-design.md`

## 最终结论

50 个真人使用场景已经用进程级 Pico 入口执行完成，最终结果为：

- Human scenario full suite：`50 passed / 0 failed`
- Runner 输出目录：`/private/tmp/codecat-v3-human-scenarios/20260513-170838`
- Summary JSON：`/private/tmp/codecat-v3-human-scenarios/20260513-170838/summary.json`
- Summary Markdown：`/private/tmp/codecat-v3-human-scenarios/20260513-170838/summary.md`
- 代码验证：`uv run ruff check .` 通过
- 回归测试：`uv run pytest tests -q` -> `224 passed, 2 skipped`

这次不是只改测试代码。过程中暴露出的 runtime 行为问题已经修在产品代码里，runner 只承担“像人一样跑 Pico 并收集证据”的职责。

## 本次新增 / 修改

| 路径 | 作用 |
|---|---|
| `release/v3/testing/01-test-design.md` | 50 个真人使用场景设计，含 5 个真实业务场景 |
| `release/v3/testing/02-execution-record.md` | 本次全量执行、缺陷、修复和验证记录 |
| `release/v3/testing/03-runner-and-evidence.md` | runner、证据目录、重跑方法和排错说明 |
| `release/v3/testing/04-scenario-checklist.md` | 50 个场景的复盘检查清单 |
| `scripts/run_v3_human_scenario_gate.py` | 进程级场景 runner，支持 `--suite gate/full` 和按场景筛选 |
| `codecat/evaluation/run_evidence.py` | 从 `.codecat/runs`、`.codecat/sessions` 读取真实运行证据的复用适配层 |
| `codecat/core/tool_repetition.py` | 重复工具调用守卫，按当前用户轮次和工具语义判断 |
| `codecat/core/tool_executor.py` | 在 permission / policy 前拦截重复调用，避免重复坏调用打满 step |
| `codecat/core/runtime.py` | 接入重复调用守卫，保持 runtime 主体行数预算 |
| `codecat/cli.py` | `/plan <topic> <bad_path>` 不再让 REPL 崩溃，改为用户可见错误 |
| `tests/test_tool_policy_acceptance.py` | 覆盖“被拒后补 read 可重试”和“成功写入后不能同参重放” |
| `tests/test_permissions_acceptance.py` | 覆盖 plan mode 坏写入重复拦截 |
| `tests/test_run_evidence.py` | 覆盖 run evidence 读取逻辑 |
| `.gitignore` | 白名单 internal 场景文档和执行记录 |

## 最终覆盖

| 分组 | 场景 | 最终状态 |
|---|---|---|
| 真实业务场景 | R01-R05 | 5/5 passed |
| 入口与交互 | S06-S14 | 9/9 passed |
| Plan mode | S15-S20 | 6/6 passed |
| Tool policy / permission / sandbox | S21-S30 | 10/10 passed |
| Skills / subagent / worker | S31-S38 | 8/8 passed |
| Todo / memory / context | S39-S45 | 7/7 passed |
| Provider / recovery / safety | S46-S50 | 5/5 passed |

关键证据统一落在最终输出目录下，每个场景至少保留：

- `logs/<scenario>.command.json`
- `logs/<scenario>.stdout.txt`
- `logs/<scenario>.stderr.txt`
- `workspaces/<scenario>/.codecat/runs/<run_id>/report.json`
- `workspaces/<scenario>/.codecat/runs/<run_id>/trace.jsonl`
- `workspaces/<scenario>/.codecat/sessions/*.events.jsonl`

## 过程中发现的问题与处理

### 1. 场景 workspace 不能放在 Pico repo 内

现象：早期 runner 把 workspace 放在 repo 子目录时，Pico 向上发现 `/Users/martinlos/code/codecat/.git`，导致场景文件可能写进真实项目。

根因：`WorkspaceContext.build()` 会向上找 git root，测试 workspace 放在当前 repo 下会破坏隔离。

处理：

- runner 默认输出到 `/tmp` / `/private/tmp` 下。
- 每个 scenario workspace 内部执行 `git init -q`，让 Pico 识别到隔离 repo root。
- runner 拒绝把 `--output-dir` 放在 Pico repo 内。

### 2. 重复坏工具调用会打满 step

现象：plan mode 正确拒绝写非 active plan 文件后，DeepSeek 会重复同一个坏 `write_file`，直到 step limit。

根因：重复调用守卫原先在 permission / policy 之后生效，重复的 permission/policy 拒绝没有被有效收口。

处理：

- `tool_executor` 在参数校验后、permission/policy 前检查重复工具调用。
- 重复拒绝在 trace/report 中记录为 `tool_error_code=repeated_identical_call`。

### 3. 同参文件改写的重复拦截必须区分“补充信息后的重试”

现象一：S23 中 DeepSeek 已经 `write_file VALUE=False`、`patch_file False->True`，之后又重放同一个 `write_file VALUE=False`，把成功 patch 回滚。  
现象二：直接把 `write_file/patch_file` 第二次同参调用全部拒绝后，S21 中“先被 prior_read_required 拒绝，补 `read_file` 后重试同一 patch”也被误拒。

根因：重复调用规则只按“同参出现次数”判断，没有理解工具调用的状态语义。

处理：

- 读类工具：同一轮允许一次复查，第三次同参才拒绝。
- `write_file/patch_file`：同一轮成功执行后，同参重放直接拒绝，避免回滚后续修改。
- `write_file/patch_file`：如果上一次同参调用是错误结果，并且之后补了同路径成功 `read_file`，允许重试。
- 逻辑抽到 `codecat/core/tool_repetition.py`，避免继续膨胀 `runtime.py`。

### 4. `/plan` 非法路径会让 REPL 崩溃

现象：`/plan topic ../x.md` 类输入会抛出 `ValueError`，REPL 直接异常退出。

根因：slash command handler 没有把 plan path 校验异常转换成用户可见错误。

处理：`handle_repl_command()` 捕获 `ValueError`，返回 `error: plan path must stay under .codecat/plans/`。

### 5. Runner 的 evidence 判断要读真实 artifacts

现象：一些场景早期误判，原因包括：

- trace 的工具名字段是 `name`，不是旧 harness 里的 `tool_name`。
- 长 shell 输出 artifact 写在 `trace.jsonl` 的 `full_output_artifact`。
- “没有启动模型 run”不能用 `.codecat/runs` 目录是否存在判断，只能看是否存在 `run_*`。
- REPL 输出里 session id 可能带 `codecat>` prompt 前缀。

处理：新增 `RunEvidence`，统一从真实 `.codecat/runs` 和 `.codecat/sessions` 读取证据，runner 不再散落 ad hoc JSON 读取逻辑。

## 最终验证命令

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full
uv run ruff check .
uv run pytest tests -q
```

最终输出：

```text
{"failed": 0, "output_dir": "/private/tmp/codecat-v3-human-scenarios/20260513-170838", "passed": 50, "status": "passed"}
All checks passed!
224 passed, 2 skipped, 6 warnings in 68.50s
```
