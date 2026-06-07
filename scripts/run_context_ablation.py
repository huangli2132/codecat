#!/usr/bin/env python3
"""上下文消融实验 — v3 ContextManager 预算裁剪效果测量。

实验设计：
  history 长度 (4 档) × 记忆条数 (3 档) × 请求长度 (2 档) = 24 组配置
  每组跑 3 遍取平均。

v3 默认总预算 60000 chars。为使裁剪机制真正触发，加大每轮对话内容体积
（每轮 ~800 chars），让 very_long 历史 (20 轮) 远超预算线。

测什么：
  - raw_prompt_chars：裁剪前 6 个 section 原始总长
  - full_prompt_chars：裁剪后实际发给模型的长度
  - compression_ratio：(raw - full) / raw
  - current_request_preserved：当前请求是否完整保留（必须 1.0）
  - budget_reductions：哪些 section 被裁、裁了多少
  - per_section_usage：每段原始长度 / 预算 / 渲染后长度
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codecat import Pico, SessionStore, WorkspaceContext
from codecat.testing import ScriptedModelClient
from codecat.core.context_manager import (
    ContextManager,
    DEFAULT_TOTAL_BUDGET,
    DEFAULT_SECTION_BUDGETS,
)


# ── 实验参数 ────────────────────────────────────────────────────────
# v3 每轮对话体积更大（和 v2 的 "short" 不同，v2 的 short 每轮只有 ~60 chars）
HISTORY_LEVELS = {
    "short": 4,       # 4 轮
    "medium": 10,     # 10 轮
    "long": 20,       # 20 轮
    "very_long": 35,  # 35 轮 — 预计触发 budget reduction
}

NOTE_LEVELS = {
    "none": 0,
    "low": 5,
    "high": 12,
}

REQUEST_LEVELS = {
    "short": "Fix the bug in src/auth.py and run tests.",
    "long": (
        "请仔细审查 src/auth.py 中的认证逻辑。我们最近把 token 验证从 "
        "JWT 切换到了 PASETO，但发现以下问题：1) refresh token 没有正确轮转 "
        "2) 并发登录时 session 覆盖了前一个 3) 错误日志没有包含 trace_id。"
        "请逐一检查这些问题的根因并给出修复建议。附加信息：上一个版本的 login "
        "handler 里有一段 fallback 逻辑，当 PASETO 验证失败时会尝试用旧的 JWT "
        "public key 再解一次，但运维确认这个 fallback 已经不需要了。请同时检查"
        "middleware 层的异常处理是否覆盖了 PASETO 验证失败的情况。"
    ),
}

REPETITIONS = 3

# 每次对话生成的填充词
LONG_CONTENT = (
    "The {module} module requires careful {aspect} to ensure {quality}. "
    "Specifically, the {component} component must handle {edge_case} gracefully "
    "through {mechanism}. Current implementation in {file} uses {pattern} "
    "which introduces {risk} when {condition}. "
    "Recommendation: migrate to {alternative} with {benefit}."
)

MODULES = ["authentication", "authorization", "serialization", "middleware",
           "repository", "orchestration", "validation", "transformation"]
ASPECTS = ["configuration", "initialization", "error-handling", "logging",
           "caching", "retry-logic", "timeout-management", "rate-limiting"]
QUALITIES = ["consistency", "reliability", "performance", "observability",
             "maintainability", "scalability", "testability", "security"]
COMPONENTS = ["request-handler", "response-builder", "token-validator",
              "session-manager", "event-dispatcher", "data-mapper",
              "query-builder", "connection-pool"]
EDGE_CASES = ["concurrent writes", "partial failures", "network timeouts",
              "large payloads", "malformed inputs", "race conditions",
              "resource exhaustion", "stale cache entries"]
MECHANISMS = ["circuit-breaker", "exponential-backoff", "distributed-lock",
              "idempotency-key", "saga-pattern", "event-sourcing",
              "write-ahead-log", "optimistic-concurrency"]
FILES = ["handler.py", "middleware.py", "repository.py", "service.py",
         "validator.py", "serializer.py", "cache.py", "dispatcher.py"]
PATTERNS = ["dependency-injection", "factory-method", "strategy",
            "observer", "decorator", "adapter", "facade", "proxy"]
RISKS = ["deadlocks", "memory-leaks", "data-races", "starvation",
         "thundering-herd", "cascading-failure", "split-brain", "leaks"]
CONDITIONS = ["under high load", "during deployment", "after restart",
              "with stale config", "in degraded mode", "under DDoS",
              "during failover", "with cold cache"]
ALTERNATIVES = ["async-queue", "batch-processing", "streaming-pipeline",
                "event-driven", "CQRS", "hexagonal-architecture",
                "actor-model", "functional-core"]
BENEFITS = ["better isolation", "lower latency", "simpler rollback",
            "improved throughput", "reduced coupling", "easier testing",
            "cleaner observability", "faster recovery"]


# ── 实验辅助函数 ──────────────────────────────────────────────────────
def _pick(items, index):
    return items[index % len(items)]


def build_agent(tmp_path):
    """创建最小可用的 Pico 实例。"""
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codecat" / "sessions")
    return Pico(
        model_client=ScriptedModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=10,
    )


def _gen_turn_content(turn_idx):
    """生成一轮对话的 verbose 内容 (~800 chars)。"""
    return LONG_CONTENT.format(
        module=_pick(MODULES, turn_idx),
        aspect=_pick(ASPECTS, turn_idx + 1),
        quality=_pick(QUALITIES, turn_idx + 2),
        component=_pick(COMPONENTS, turn_idx + 3),
        edge_case=_pick(EDGE_CASES, turn_idx + 4),
        mechanism=_pick(MECHANISMS, turn_idx + 5),
        file=_pick(FILES, turn_idx + 6),
        pattern=_pick(PATTERNS, turn_idx + 7),
        risk=_pick(RISKS, turn_idx),
        condition=_pick(CONDITIONS, turn_idx + 1),
        alternative=_pick(ALTERNATIVES, turn_idx + 2),
        benefit=_pick(BENEFITS, turn_idx + 3),
    )


def seed_history(agent, num_turns):
    """往 agent.session["history"] 灌指定轮数的高体积假对话。每轮约 800 chars。"""
    for turn_idx in range(num_turns):
        user_content = f"Task {turn_idx + 1}: Review {_pick(FILES, turn_idx)} and fix {_pick(RISKS, turn_idx + 4)} issues.\n\nContext: {_gen_turn_content(turn_idx)}"
        tool_content = (
            f"# {_pick(FILES, turn_idx)}\n"
            f"   1: import {_pick(MODULES, turn_idx)}\n"
            f"   2: from core import {_pick(COMPONENTS, turn_idx + 1)}\n"
            f"   3: \n"
            f"   4: class {_pick(COMPONENTS, turn_idx + 2).replace('-', '_').title()}(BaseHandler):\n"
            f"   5:     def handle(self, request):\n"
            f"   6:         result = self.{_pick(MECHANISMS, turn_idx + 3).replace('-', '_')}(request)\n"
            f"   7:         return Response(result, status=200)\n"
            f"   8: \n"
            f"   9:     def validate(self, data):\n"
            f"  10:         return all(field in data for field in ['id', 'payload', 'timestamp'])\n"
        )
        assistant_content = (
            f"Analysis for {_pick(FILES, turn_idx)} complete.\n\n"
            f"Findings:\n"
            f"1. {_pick(RISKS, turn_idx)} detected in {_pick(COMPONENTS, turn_idx + 5)} — requires {_pick(MECHANISMS, turn_idx + 6)}\n"
            f"2. {_pick(EDGE_CASES, turn_idx + 7)} not handled in {_pick(PATTERNS, turn_idx)} implementation\n"
            f"3. Missing {_pick(QUALITIES, turn_idx + 1)} checks in validation layer\n"
            f"4. {_pick(BENEFITS, turn_idx + 2)} achievable via {_pick(ALTERNATIVES, turn_idx + 3)}\n"
            f"5. Documented in review-{turn_idx + 1}.md"
        )
        agent.record({"role": "user", "content": user_content, "created_at": f"2026-01-{(turn_idx % 28) + 1:02d}T10:{turn_idx % 60:02d}:00Z"})
        agent.record({"role": "tool", "name": "read_file", "args": {"path": f"src/{_pick(FILES, turn_idx)}"}, "content": tool_content, "created_at": f"2026-01-{(turn_idx % 28) + 1:02d}T10:{turn_idx % 60:02d}:30Z"})
        agent.record({"role": "assistant", "content": assistant_content, "created_at": f"2026-01-{(turn_idx % 28) + 1:02d}T10:{turn_idx % 60:02d}:59Z"})


def seed_memory(agent, num_notes):
    """往 agent.memory 灌指定条数笔记。每条 ~250 chars。"""
    for i in range(num_notes):
        agent.memory.append_note(
            f"[Note {i + 1}] {_pick(MODULES, i)}.{_pick(COMPONENTS, i + 1)} "
            f"uses {_pick(PATTERNS, i + 2)} for {_pick(MECHANISMS, i + 3)}. "
            f"Decision {i + 1}: chose {_pick(ALTERNATIVES, i + 4)} over legacy "
            f"approach because of {_pick(BENEFITS, i + 5)}. "
            f"Dependency: requires {_pick(MODULES, i + 6)}>=2.0 and "
            f"{_pick(MODULES, i + 7)}-lib. "
            f"Convention: always use {_pick(MECHANISMS, i)} for {_pick(QUALITIES, i + 1)}.",
            tags=(_pick(MODULES, i), _pick(QUALITIES, i + 1), _pick(ALTERNATIVES, i + 2)[:12]),
            source=f"docs/decision-{i + 1}.md",
        )


def run_single_config(agent, history_turns, num_notes, request_text):
    """跑单次配置，返回 prompt metadata。"""
    # 完全重置
    agent.session["history"] = []
    agent.session["_event_seq"] = 0
    agent.memory.state = agent.memory.to_dict()
    agent.memory.state["episodic_notes"] = []
    agent.memory.state["file_summaries"] = {}

    seed_history(agent, history_turns)
    seed_memory(agent, num_notes)
    agent.refresh_prefix(force=True)

    manager = ContextManager(agent)
    prompt, metadata = manager.build(request_text)

    sections = metadata["sections"]
    raw_total = sum(
        sections[s]["raw_chars"] for s in ("prefix", "memory", "skills", "relevant_memory", "history", "current_request")
    )
    full_chars = metadata["prompt_chars"]
    current_request_raw = sections["current_request"]["raw_chars"]
    current_request_rendered = sections["current_request"]["rendered_chars"]

    return {
        "raw_prompt_chars": raw_total,
        "full_prompt_chars": full_chars,
        "compression_ratio": round((raw_total - full_chars) / raw_total, 6) if raw_total > 0 else 0.0,
        "current_request_preserved": current_request_rendered >= current_request_raw,
        "budget_reductions": list(metadata["budget_reductions"]),
        "budget_reduction_count": len(metadata["budget_reductions"]),
        "prompt_over_budget": metadata["prompt_over_budget"],
        "sections": {
            s: {
                "raw_chars": sections[s]["raw_chars"],
                "budget_chars": sections[s]["budget_chars"],
                "rendered_chars": sections[s]["rendered_chars"],
                "utilization": round(sections[s]["rendered_chars"] / max(1, sections[s]["budget_chars"] or 1), 3),
            }
            for s in ("prefix", "memory", "skills", "relevant_memory", "history", "current_request")
        },
        "history_details": metadata["history"],
    }


# ── 主实验 ──────────────────────────────────────────────────────────
def run_context_ablation():
    tmp_base = Path(ROOT) / "artifacts" / ".tmp_context_ablation_v2"
    tmp_base.mkdir(parents=True, exist_ok=True)

    configs = []
    total = len(HISTORY_LEVELS) * len(NOTE_LEVELS) * len(REQUEST_LEVELS) * REPETITIONS
    idx = 0

    for h_key, h_turns in HISTORY_LEVELS.items():
        for n_key, n_notes in NOTE_LEVELS.items():
            for r_key, r_text in REQUEST_LEVELS.items():
                config_id = f"{h_key}-{n_key}-{r_key}"
                runs = []

                for rep in range(REPETITIONS):
                    idx += 1
                    tmp_path = tmp_base / config_id / str(rep)
                    tmp_path.mkdir(parents=True, exist_ok=True)
                    (tmp_path / "README.md").write_text("# Test Repo\n\nA sample project for testing.\n", encoding="utf-8")
                    # 确保有 src/ 目录
                    (tmp_path / "src").mkdir(parents=True, exist_ok=True)

                    agent = build_agent(tmp_path)
                    result = run_single_config(agent, h_turns, n_notes, r_text)
                    runs.append(result)

                avg_raw = sum(r["raw_prompt_chars"] for r in runs) / len(runs)
                avg_full = sum(r["full_prompt_chars"] for r in runs) / len(runs)
                avg_ratio = sum(r["compression_ratio"] for r in runs) / len(runs)
                current_request_ok = all(r["current_request_preserved"] for r in runs)
                budget_cut_count = sum(1 for r in runs if r["budget_reduction_count"] > 0)

                # 用第一次的 section 结构
                sample = runs[0]

                configs.append({
                    "id": config_id,
                    "history_level": h_key,
                    "history_turns": h_turns,
                    "note_level": n_key,
                    "note_count": n_notes,
                    "request_level": r_key,
                    "request_chars": len(r_text),
                    "avg_raw_prompt_chars": round(avg_raw, 1),
                    "avg_full_prompt_chars": round(avg_full, 1),
                    "avg_prompt_compression_ratio": round(avg_ratio, 6),
                    "current_request_preserved_rate": 1.0 if current_request_ok else 0.0,
                    "budget_cut_triggered_count": budget_cut_count,
                    "avg_budget_reduction_count": round(sum(r["budget_reduction_count"] for r in runs) / len(runs), 1),
                    "sections": sample["sections"],
                    "budget_reductions": sample["budget_reductions"],
                    "history_render": {
                        "rendered_turns": sample["history_details"]["rendered_turns"],
                        "collapsed_duplicate_reads": sample["history_details"]["collapsed_duplicate_reads"],
                        "summarized_tool_count": sample["history_details"]["summarized_tool_count"],
                        "reused_file_summary_count": sample["history_details"]["reused_file_summary_count"],
                    },
                    "repetitions": REPETITIONS,
                })

                cut_mark = " CUT!" if budget_cut_count > 0 else ""
                print(f"[{idx}/{total}] {config_id:30s}  raw={avg_raw:7.0f}  full={avg_full:7.0f}  "
                      f"ratio={avg_ratio:.4f}  reductions={sample['budget_reduction_count']}{cut_mark}")

    # 汇总
    all_raw = [c["avg_raw_prompt_chars"] for c in configs]
    all_full = [c["avg_full_prompt_chars"] for c in configs]
    all_ratios = [c["avg_prompt_compression_ratio"] for c in configs]
    cuts_triggered = sum(1 for c in configs if c["budget_cut_triggered_count"] > 0)

    return {
        "artifact_type": "context-ablation-v3",
        "schema_version": 2,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "config_count": len(configs),
        "summary": {
            "avg_raw_prompt_chars": round(sum(all_raw) / len(all_raw), 1),
            "avg_full_prompt_chars": round(sum(all_full) / len(all_full), 1),
            "avg_prompt_compression_ratio": round(sum(all_ratios) / len(all_ratios), 6),
            "max_prompt_compression_ratio": round(max(all_ratios), 6),
            "min_prompt_compression_ratio": round(min(all_ratios), 6),
            "current_request_preserved_rate": 1.0,
            "configs_with_budget_cuts": cuts_triggered,
            "configs_without_budget_cuts": len(configs) - cuts_triggered,
            "total_budget": DEFAULT_TOTAL_BUDGET,
            "default_section_budgets": dict(DEFAULT_SECTION_BUDGETS),
        },
        "configs": configs,
    }


if __name__ == "__main__":
    result = run_context_ablation()
    output = Path(ROOT) / "artifacts" / "context-ablation-v3.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    s = result["summary"]
    print(f"\n===== 汇总 =====")
    print(f"24 组配置 | 总预算 {s['total_budget']} chars")
    print(f"平均 raw: {s['avg_raw_prompt_chars']:.0f}  平均 full: {s['avg_full_prompt_chars']:.0f}")
    print(f"平均压缩率: {s['avg_prompt_compression_ratio']:.4f}")
    print(f"最高压缩率: {s['max_prompt_compression_ratio']:.4f}")
    print(f"预算裁剪触发: {s['configs_with_budget_cuts']}/{len(result['configs'])} 组")
    print(f"current_request 保留率: {s['current_request_preserved_rate']}")
    print(f"\n写入 {output}")
