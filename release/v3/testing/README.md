# Pico v3 真人场景测试包

这个目录把 Pico v3 相对 `main` 的真人使用场景测试整理成一个可以复盘、重跑、继续扩展的测试包。

它不是单元测试说明，也不是 pytest 清单。它记录的是：像真实用户一样从 `uv run codecat`、`--repl`、slash command、resume、provider profile、skills、worker、memory 等入口使用 Pico，然后只读检查 `.codecat/runs` 和 `.codecat/sessions` 产物。

## 目录结构

| 文件 | 用途 |
|---|---|
| `01-test-design.md` | 50 个真人场景的原始设计，覆盖 v3 的 runtime、REPL/TUI、plan mode、tool policy、skills、subagent、memory、provider、安全等改动面 |
| `02-execution-record.md` | 2026-05-13 全量执行记录，包含最终 50/50 结果、暴露的问题、产品修复和验证命令 |
| `03-runner-and-evidence.md` | runner 怎么跑、证据目录怎么看、如何只跑单场景、如何定位失败 |
| `04-scenario-checklist.md` | 50 个场景的分组检查清单，用于复盘和后续补跑 |

相关代码入口：

| 路径 | 说明 |
|---|---|
| `scripts/run_v3_human_scenario_gate.py` | 真实 CLI/REPL 场景 runner |
| `codecat/evaluation/run_evidence.py` | 从真实 `.codecat/runs`、`.codecat/sessions` 读取证据 |
| `tests/test_run_evidence.py` | evidence adapter 的回归测试 |
| `tests/test_tool_policy_acceptance.py` | 重复工具调用、read-before-write 等产品回归 |
| `tests/test_permissions_acceptance.py` | plan mode 和 permission gate 产品回归 |

## 最终状态

最后一次干净全量结果：

```text
uv run python scripts/run_v3_human_scenario_gate.py --suite full
{"failed": 0, "output_dir": "/private/tmp/codecat-v3-human-scenarios/20260513-170838", "passed": 50, "status": "passed"}
```

代码验证：

```text
uv run ruff check .
All checks passed!

uv run pytest tests -q
224 passed, 2 skipped, 6 warnings in 68.50s
```

## 快速重跑

跑 12 个优先 gate：

```bash
uv run python scripts/run_v3_human_scenario_gate.py
```

跑完整 50 场景：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full
```

只跑指定场景：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full --scenario S21 --scenario S23
```

指定输出目录时必须放在 Pico repo 外：

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full --output-dir /tmp/codecat-v3-human-scenarios/manual-run
```

## 复盘顺序

1. 先读 `02-execution-record.md`，看最终结论和修过的问题。
2. 再读 `03-runner-and-evidence.md`，理解 runner 怎么模拟真人入口、证据怎么落盘。
3. 对照 `04-scenario-checklist.md` 找某个场景，再去最终输出目录看对应 `logs/`、`report.json`、`trace.jsonl`、`events.jsonl`。
4. 如果要扩展新场景，先补 `01-test-design.md`，再补 `scripts/run_v3_human_scenario_gate.py` 和 checklist。

## 关键原则

- 场景必须从用户入口驱动 Pico，不能 import `Pico` 直接调 runtime。
- 验证器可以读文件，但只能读 Pico 自己写出的 artifacts 和 scenario workspace。
- 输出目录必须在 repo 外，避免 Pico 向上发现真实 repo root。
- live provider 默认用 DeepSeek，配置来自项目 `.codecat.toml`，不把 key 写进文档或产物。
- 发现产品问题时先修产品，再补 narrow regression；不要通过放宽场景断言掩盖问题。
