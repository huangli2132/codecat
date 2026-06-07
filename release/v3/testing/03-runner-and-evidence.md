# Runner 与证据说明

## Runner 定位

`scripts/run_v3_human_scenario_gate.py` 是 Pico v3 真人场景 runner。它刻意走 Pico 的公开进程入口：

- one-shot CLI：`uv run codecat --cwd <workspace> "<prompt>"`
- REPL：`uv run codecat --cwd <workspace> --repl`
- PTY-style stdin：模拟用户逐行输入 slash command
- TTY smoke：验证默认 TTY 入口能进入 TUI

runner 不 import `Pico`，也不直接调用 runtime 方法。它只创建临时 workspace、启动 Pico 进程、收集 stdout/stderr，然后读取 Pico 自己写出的 `.codecat` artifacts。

## 输出目录

默认输出目录：

```text
/tmp/codecat-v3-human-scenarios/<YYYYMMDD-HHMMSS>
```

macOS 上通常会显示为：

```text
/private/tmp/codecat-v3-human-scenarios/<YYYYMMDD-HHMMSS>
```

目录结构：

```text
<output-dir>/
  summary.json
  summary.md
  logs/
    S21.command.json
    S21.stdout.txt
    S21.stderr.txt
  workspaces/
    s21/
      .git/
      .codecat/
        runs/
          run_<timestamp>-<id>/
            task_state.json
            trace.jsonl
            report.json
            artifacts/
        sessions/
          <session>.json
          <session>.events.jsonl
      README.md
```

每个 scenario workspace 都会先 `git init -q`。这是必要的：Pico 会根据 git root 识别 workspace，如果把测试目录放在 Pico repo 内且不初始化独立 git root，Pico 会向上找到真实项目根目录，导致测试污染 repo。

## 常用命令

跑 12 个优先 gate：

```bash
uv run python scripts/run_v3_human_scenario_gate.py
```

跑完整 50 场景：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full
```

只跑一个或多个场景：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full --scenario S21
uv run python scripts/run_v3_human_scenario_gate.py --suite full --scenario S21 --scenario S23
```

使用指定配置：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full --config /Users/martinlos/code/codecat/.codecat.toml
```

指定输出目录：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full --output-dir /tmp/codecat-v3-human-scenarios/manual-run
```

`--output-dir` 不能位于 `/Users/martinlos/code/codecat` 下面。runner 会直接拒绝这种路径。

## Summary 怎么看

`summary.json` 是机器可读汇总，核心字段：

```json
{
  "status": "passed",
  "scenario_count": 50,
  "passed": 50,
  "failed": 0,
  "suite": "full",
  "provider": "deepseek",
  "output_dir": "/private/tmp/codecat-v3-human-scenarios/20260513-170838"
}
```

每个 `results[]` 条目包含：

- `id`：场景编号，如 `S21`
- `title`：场景标题
- `driver`：one-shot / REPL / PTY / slash 的入口方式
- `status`：`passed` 或 `failed`
- `checks`：具体验收项
- `commands`：实际执行的 Pico 命令、return code、stdout/stderr 路径
- `evidence`：report、trace、events 路径

`summary.md` 是给人快速浏览的表格，适合复盘时先扫失败场景和 evidence 路径。

## Evidence Adapter

`codecat/evaluation/run_evidence.py` 提供 `RunEvidence.latest(workspace)`，只从真实 artifacts 取证：

- `.codecat/runs/run_*/report.json`
- `.codecat/runs/run_*/trace.jsonl`
- `.codecat/sessions/*.events.jsonl`

它封装的判断包括：

- run status / stop reason
- changed paths
- tool events / tool names / tool error codes
- runtime reminders
- long shell output artifacts
- session event 是否出现

这么做是为了避免 runner 里到处写 ad hoc JSON 查询，也避免把单测内部对象状态当成真人场景证据。

## 失败定位流程

如果 full suite 失败，按这个顺序看：

1. 打开 `<output-dir>/summary.md`，找到 `failed-check` 行。
2. 打开对应 `logs/<scenario>.stdout.txt` 和 `logs/<scenario>.stderr.txt`，看用户可见行为。
3. 打开 `workspaces/<scenario>/.codecat/runs/<run_id>/report.json`，看 status、stop_reason、runtime_reminders。
4. 打开 `trace.jsonl`，按 `tool_executed` 搜索 `tool_error_code`、`name`、`result`。
5. 打开 `.codecat/sessions/*.events.jsonl`，看 slash command、permission_decision、skill_invoked、worker 事件。
6. 判断是产品问题、runner 取证问题，还是 live model 没按场景执行。

处理原则：

- 产品问题：修产品代码，再补 narrow regression，再重跑相关 live 场景。
- runner 取证问题：把读取逻辑下沉到 `RunEvidence`，不要在场景里散写解析。
- live model 偏航：优先收紧场景 prompt 或 step budget；不要放宽产品语义。

## 本次修复过的关键失败

| 场景 | 失败表现 | 根因 | 修复 |
|---|---|---|---|
| S15 | plan mode 坏写入重复到 step limit | 重复调用拦截在 permission 后，拒绝类重复调用没有收口 | repeated guard 前移到 permission/policy 前 |
| S21 | 被 `prior_read_required` 拒绝后，补读再重试仍被误判重复 | 重复规则只看同参次数，没有理解“错误后补 read” | 允许错误调用在同路径成功 read 后重试 |
| S23 | patch 成功后模型又重放同一个 write，把 True 回滚成 False | 文件改写工具同参成功调用不应重放 | `write_file/patch_file` 成功后同参重放直接拒绝 |
| S18 | `/plan` 非法路径导致 REPL 崩溃 | slash command 没捕获 path 校验异常 | REPL 返回用户可见 error |
| S26 | 长 shell 输出已落 artifact 但 runner 找不到 | artifact 路径在 trace 的 `full_output_artifact` | `RunEvidence.full_output_artifacts()` 统一读取 |

## 最终证据

最后一次干净 full suite：

```text
/private/tmp/codecat-v3-human-scenarios/20260513-170838
```

最终命令输出：

```text
{"failed": 0, "output_dir": "/private/tmp/codecat-v3-human-scenarios/20260513-170838", "passed": 50, "status": "passed"}
```
