#!/usr/bin/env python3
"""记忆消融实验 — v3 LayeredMemory 收益测量。

核心思路：
  引导轮让 Agent 读文件把事实记入 working memory，然后保存 memory state。
  追问轮用全新 Pico 实例（空 history），只注入对应的 memory state。
  这样排除 "事实在 history 里所有人可见" 的干扰，只测 memory 自己的收益。

实验设计：
  18 个任务 (fact_lookup×6 + edit_dependency×6 + history_reference×6)
  每个任务 3 个 variant (memory_on / memory_off / memory_irrelevant) × 5 遍
  共 270 次运行。

三组对照：
  memory_on         → 追问轮带有真实 memory（引导轮攒的）
  memory_off        → feature_flags["memory"]=False，相当于没有记忆系统
  memory_irrelevant → memory 里有内容但全是无关的（"the team mascot is a panda"）
                      排除 "prompt 变长了所以效果好" 的干扰

假模型行为：
  追问轮在整段 prompt 里搜 expected_fact（大小写不敏感）
  → 搜到 → 直接 final，不调工具
  → 没搜到 → 调 read_file 重新读文件，repeated_reads += 1

  注意：因为追问轮是全新 Pico 实例，history 为空的，所以事实只会出现在
  memory_on 组的 prompt 里（从 episcodic_notes / file_summaries / retrieval
  section 里来）
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
from codecat.providers.base import ModelResult


# ── 任务 ────────────────────────────────────────────────────────────

FACT_LOOKUP_TASKS = [
    {"id": "fact_color",   "file": "facts.txt",    "content": "deploy key is red",         "question": "What does facts.txt say?"},
    {"id": "fact_api",     "file": "service.txt",   "content": "API endpoint is /v2/login",  "question": "What endpoint does service.txt define?"},
    {"id": "fact_budget",  "file": "config.txt",    "content": "budget limit is 5000",       "question": "What is the budget limit in config.txt?"},
    {"id": "fact_timeout", "file": "settings.txt",   "content": "timeout is 30 seconds",     "question": "What is the configured timeout?"},
    {"id": "fact_region",  "file": "deploy.txt",    "content": "region is us-west-2",       "question": "Which region is in deploy.txt?"},
    {"id": "fact_owner",   "file": "project.txt",   "content": "owner is backend-team",     "question": "Who owns this project according to project.txt?"},
]

EDIT_DEPENDENCY_TASKS = [
    {"id": "edit_intro",   "file": "intro.md",     "content": "This project is a local coding agent harness.", "question": "What does intro.md say about the project?"},
    {"id": "edit_token",   "file": "auth_cfg.txt", "content": "token_method = PASETO",                        "question": "What token method does auth_cfg.txt specify?"},
    {"id": "edit_field",   "file": "model_def.txt","content": "User fields are id name email role",          "question": "What fields does the User model have according to model_def.txt?"},
    {"id": "edit_line",    "file": "port.txt",     "content": "DEFAULT_PORT = 8080",                          "question": "What is the default port in port.txt?"},
    {"id": "edit_import",  "file": "imports.txt",  "content": "imports: os sys json pathlib",                 "question": "What libraries does imports.txt list?"},
    {"id": "edit_const",   "file": "constants.txt","content": "MAX_RETRY = 5",                                 "question": "What is the value of MAX_RETRY in constants.txt?"},
]

HISTORY_REFERENCE_TASKS = [
    {"id": "history_file",     "file": "notes/audit.txt",   "content": "Audit found 3 critical issues in auth",      "question": "What did the audit find in auth?"},
    {"id": "history_line",     "file": "notes/bug.txt",     "content": "Bug on line 42 of handler.py",                "question": "Where is the bug in handler.py?"},
    {"id": "history_token",    "file": "notes/root.txt",    "content": "Root cause: stale cache after deploy",        "question": "What is the root cause of the deploy issue?"},
    {"id": "history_tool",     "file": "notes/cmd.txt",     "content": "Last pytest exit code was 1",                 "question": "What was the exit code of the last pytest run?"},
    {"id": "history_decision", "file": "notes/decision.txt","content": "Skip integration tests: mock server is down",  "question": "Why did we decide to skip integration tests?"},
    {"id": "history_path",     "file": "notes/path.txt",    "content": "New module path: src/payment/v2/",              "question": "Where should the new module go?"},
]

ALL_TASKS = FACT_LOOKUP_TASKS + EDIT_DEPENDENCY_TASKS + HISTORY_REFERENCE_TASKS
REPETITIONS = 5


# ── 假模型 ──────────────────────────────────────────────────────────

class MemoryExperimentClient:
    """四阶段状态机，只在追问轮搜索 prompt 中的事实。

    prime   → 引导轮，返回 read_file 指令
    prime2  → 引导轮第二步，读完文件后返回 final
    question → 追问轮，在 prompt 里搜 expected_fact
    reread  → 没找到，重新读一次文件
    """

    def __init__(self, task):
        self.expected_fact = task["content"]
        self.expected_file = task["file"]
        self.phase = "prime"
        self.repeated_reads = 0
        self.prompts = []
        self.supports_prompt_cache = False
        self.last_completion_metadata = {}

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        return str(self._next_action())

    def complete_result(self, prompt, max_new_tokens, **kwargs):
        text = str(self.complete(prompt, max_new_tokens, **kwargs))
        return ModelResult(text=text, metadata=dict(self.last_completion_metadata))

    def _next_action(self):
        if self.phase == "prime":
            # 引导轮第一步 — 读文件
            self.phase = "prime2"
            return f'<tool>{{"name":"read_file","args":{{"path":"{self.expected_file}","start":1,"end":200}}}}</tool>'

        if self.phase == "prime2":
            # 引导轮第二步 — 读完，done
            return f"<final>I have read {self.expected_file}.</final>"

        return self._question_action()

    def _question_action(self):
        # 子类会覆盖这个
        raise NotImplementedError

    def _search_and_decide(self, prompt):
        """在 prompt 里搜 expected_fact，找到就 final，找不到就 re-read。"""
        if self.expected_fact.lower() in prompt.lower():
            return f"<final>{self.expected_fact}</final>"

        self.repeated_reads += 1
        self.phase = "reread"
        return f'<tool>{{"name":"read_file","args":{{"path":"{self.expected_file}","start":1,"end":200}}}}</tool>'

    def _reread_action(self):
        return f"<final>{self.expected_fact}</final>"


class QuestionClient(MemoryExperimentClient):
    """追问轮客户端：搜索 prompt，决定是 final 还是重读。"""
    def __init__(self, task):
        super().__init__(task)
        self.phase = "question"

    def _next_action(self):
        if self.phase == "question":
            return self._search_and_decide(self.prompts[-1] if self.prompts else "")
        if self.phase == "reread":
            return self._reread_action()
        return "<final>done</final>"


# ── 实验辅助 ────────────────────────────────────────────────────────

def build_agent(tmp_path, model_client):
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codecat" / "sessions")
    return Pico(
        model_client=model_client,
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=15,
    )


def run_single_variant(task, variant, tmp_base):
    """对一个任务跑一个 variant × REPETITIONS 遍。"""
    task_id = task["id"]
    rows = []

    for rep in range(REPETITIONS):
        tmp_path = tmp_base / task_id / variant / str(rep)
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")

        # 写任务文件（注意：不覆盖 README.md）
        file_path = tmp_path / task["file"]
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(task["content"] + "\n", encoding="utf-8")

        # ── 引导轮：让 agent 读文件，攒 memory ──
        prime_client = MemoryExperimentClient(task)
        prime_client.phase = "prime"
        prime_agent = build_agent(tmp_path, prime_client)
        try:
            prime_agent.ask(f"Read {task['file']} and remember the key fact.")
        except Exception:
            pass

        # 保存引导轮后的 memory state
        saved_memory = prime_agent.memory.to_dict()

        # ── 追问轮：全新 Pico 实例，空 history，只注入对应 memory ──
        question_client = QuestionClient(task)
        question_agent = build_agent(tmp_path, question_client)

        if variant == "memory_on":
            # 注入引导轮攒的 memory
            question_agent.memory.state = saved_memory
            question_agent.refresh_prefix(force=True)

        elif variant == "memory_off":
            # 关闭 memory
            question_agent.feature_flags["memory"] = False
            question_agent.feature_flags["relevant_memory"] = False
            question_agent.refresh_prefix(force=True)

        elif variant == "memory_irrelevant":
            # 清空真实记忆，塞入无关内容
            irrelevant = {
                "working": {"task_summary": "", "recent_files": []},
                "task_board": {},
                "episodic_notes": [
                    {
                        "text": "the team mascot is a panda",
                        "tags": ["unrelated"],
                        "source": "fake",
                        "created_at": "2026-04-07T10:00:00Z",
                        "note_index": 0,
                        "kind": "episodic",
                    },
                    {
                        "text": "the office plant is a monstera",
                        "tags": ["office"],
                        "source": "fake",
                        "created_at": "2026-04-07T10:01:00Z",
                        "note_index": 1,
                        "kind": "episodic",
                    },
                ],
                "file_summaries": {},
                "task": "",
                "files": [],
                "notes": [],
                "next_note_index": 2,
            }
            question_agent.memory.state = irrelevant
            question_agent.refresh_prefix(force=True)

        # 跑追问
        try:
            answer = question_agent.ask(task["question"])
        except Exception:
            answer = "error"

        correct = task["content"].lower() in str(answer).lower()
        repeated_reads = question_client.repeated_reads

        # 计算工具步数：从 question_agent 的 task_state 拿到实际值
        task_state = getattr(question_agent, "current_task_state", None)
        actual_tool_steps = int(getattr(task_state, "tool_steps", 0) or 0)

        rows.append({
            "task_id": task_id,
            "variant": variant,
            "category": task.get("category", "unknown"),
            "correct": correct,
            "repeated_reads": repeated_reads,
            "tool_steps": actual_tool_steps,
            "attempts": actual_tool_steps + 1,
        })

    return rows


# ── 主实验 ──────────────────────────────────────────────────────────

def run_memory_ablation():
    tmp_base = Path(ROOT) / "artifacts" / ".tmp_memory_ablation_v2"
    tmp_base.mkdir(parents=True, exist_ok=True)

    # 按 category 归类
    for t in FACT_LOOKUP_TASKS:
        t["category"] = "fact_lookup"
    for t in EDIT_DEPENDENCY_TASKS:
        t["category"] = "edit_dependency"
    for t in HISTORY_REFERENCE_TASKS:
        t["category"] = "history_reference"

    variants = ["memory_on", "memory_off", "memory_irrelevant"]
    all_rows_by_variant = {v: [] for v in variants}
    total = len(ALL_TASKS) * len(variants) * REPETITIONS
    idx = 0

    for task in ALL_TASKS:
        for variant in variants:
            rows = run_single_variant(task, variant, tmp_base)
            all_rows_by_variant[variant].extend(rows)
            idx += len(rows)

            correct = sum(1 for r in rows if r["correct"])
            rr = sum(r["repeated_reads"] for r in rows)
            ts_avg = sum(r["tool_steps"] for r in rows) / len(rows)
            print(f"[{idx}/{total}] {task['id']:22s} {variant:20s}  "
                  f"correct={correct}/{len(rows)}  repeated_reads={rr}  tool_steps={ts_avg:.1f}")

    # 汇总
    variant_summaries = {}
    for variant in variants:
        rows = all_rows_by_variant[variant]
        rr_total = sum(r["repeated_reads"] for r in rows)
        ts = [r["tool_steps"] for r in rows]
        attempts = [r["attempts"] for r in rows]
        correct_count = sum(1 for r in rows if r["correct"])
        variant_summaries[variant] = {
            "repeated_reads": rr_total,
            "avg_tool_steps": round(sum(ts) / len(ts), 4),
            "avg_attempts": round(sum(attempts) / len(attempts), 4),
            "correct_rate": round(correct_count / len(rows), 4),
            "total_runs": len(rows),
        }

    # 按 category 细分
    category_breakdown = {}
    for cat in ["fact_lookup", "edit_dependency", "history_reference"]:
        category_breakdown[cat] = {}
        for variant in variants:
            cat_rows = [r for r in all_rows_by_variant[variant] if r["category"] == cat]
            rr = sum(r["repeated_reads"] for r in cat_rows)
            cc = sum(1 for r in cat_rows if r["correct"])
            category_breakdown[cat][variant] = {
                "repeated_reads": rr,
                "avg_tool_steps": round(sum(r["tool_steps"] for r in cat_rows) / len(cat_rows), 4) if cat_rows else 0,
                "correct_rate": round(cc / len(cat_rows), 4) if cat_rows else 0,
                "total_runs": len(cat_rows),
            }

    category_counts = {}
    for task in ALL_TASKS:
        cat = task["category"]
        category_counts[cat] = category_counts.get(cat, 0) + 1

    return {
        "artifact_type": "memory-ablation-v3",
        "schema_version": 2,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "task_count": len(ALL_TASKS),
        "runs_per_variant": len(ALL_TASKS) * REPETITIONS,
        "category_counts": category_counts,
        "variants": variant_summaries,
        "category_breakdown": category_breakdown,
        "rows": all_rows_by_variant,
    }


if __name__ == "__main__":
    result = run_memory_ablation()
    output = Path(ROOT) / "artifacts" / "memory-ablation-v3.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print()
    print("=" * 70)
    print(f"{'variant':25s} {'repeated_reads':>14s}  {'avg_tool_steps':>14s}  {'correct_rate':>12s}")
    print("-" * 70)
    for v, s in result["variants"].items():
        print(f"{v:25s} {s['repeated_reads']:14d}  {s['avg_tool_steps']:14.3f}  {s['correct_rate']:11.2%}")
    print()
    for cat, breakdown in result["category_breakdown"].items():
        print(f"--- {cat} ---")
        for v, s in breakdown.items():
            print(f"  {v:25s}  repeated_reads={s['repeated_reads']:3d}  tool_steps={s['avg_tool_steps']:.3f}  correct={s['correct_rate']:.0%}")
    print(f"\n写入 {output}")
