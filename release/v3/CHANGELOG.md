# Changelog

## v0.3.0 - v3 Runtime Engine

完整的架构重写。从单循环 agent 升级为分层治理的 runtime engine。

### Architecture

- 全新 v3 event bus + plan mode 引擎 (`b994bad`)
- Runtime control plane 拆分为三层独立治理 (`f998485`)
  - Permission governance - 工具调用权限准入 (`ae0bfab`)
  - Context governance - compact 边界与上下文预算 (`c7eb1ab`)
  - Evidence plane - 运行时证据收集与追踪 (`117814a`)
- Coordinator-worker 多 agent 管理 (`2afdc1d`)
- Tool policy 层 - shell 命令分类准入 (`0a5192c`)
- Provider config profiles 重构 (`cc13e06`)
- 移除 delegate tool，简化调度 (`93e0bca`)

### Features

- Textual TUI 界面 - `codecat-tui` 命令启动 (`7fd5654`)
- Skills / slash workflow 系统 - 可扩展的技能注册 (`4267888`, `a56b871`)
- File-based memory + auto-dream - 后台整理会话记忆 (`92a2de3`, `ff93384`)
- Task ledger 工具 - 结构化任务追踪 (`461702d`)
- Provider reliability evidence - 模型可靠性追踪 (`03a6550`)
- Real session acceptance harness + dogfood 套件 (`d76863e`, `669394b`)

### Bug Fixes

- Shell policy 误判管道后的 head/tail/grep - 只在命令起始位置禁止 (`c6f7810`)
- Step limit 冷消息改为三段式总结：已完成、未完成、如何继续 (`c6f7810`)
- Plan path 绝对路径自动转相对，不再浪费一步 (`c6f7810`)
- Auto-dream 写文件被 tool_policy 拒绝 - file freshness 从 memory flag 解耦 (`4a0f22c`)
- 长 shell 输出 artifact 丢失 (`c6ef16f`)
- Real-session runtime contract gaps (`848d851`)

### Defaults & DX

- `max_steps`: 6 -> 50
- `max_new_tokens`: 512 -> 按 provider 推断（Anthropic 32000 / OpenAI 与 DeepSeek 8192）
- `total_budget`: 12000 -> 60000
- `max_attempts`: 从 `max_steps * 3` 改为 `max_steps + 2`，删除隐形重试
- 中文 README 重写（274 行）
- 4 篇用户文档：configuration / memory / skills / sandbox
- `install.sh` 一键安装脚本
- 模型错误不再静默，改为中文诊断信息

### Tests

- v3 human scenario suite (`abf9c4f`)
- Release smoke tests（离线 4 个 + 可选 live dream）
- 27 个测试文件覆盖所有子系统
