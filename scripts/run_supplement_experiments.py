#!/usr/bin/env python3
"""补充实验：召回精度 + 压缩语义保真度"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codecat import Pico, SessionStore, WorkspaceContext
from codecat.testing import ScriptedModelClient
from codecat.core.context_manager import ContextManager


def build_agent(tmp_path):
    workspace = WorkspaceContext.build(tmp_path)
    store = SessionStore(tmp_path / ".codecat" / "sessions")
    return Pico(
        model_client=ScriptedModelClient(["<final>done</final>"]),
        workspace=workspace,
        session_store=store,
        approval_policy="auto",
        max_steps=10,
    )


# ══════════════════════════════════════════════════════════════════════
# 实验 4：召回精度
# ══════════════════════════════════════════════════════════════════════

def experiment_recall_precision(tmp_base):
    noise_counts = [5, 10, 20, 50]
    query_types = ["exact_tag", "keyword_only", "vague"]
    repetitions = 5
    total = len(noise_counts) * len(query_types) * repetitions

    results = []
    idx = 0

    for n_noise in noise_counts:
        for q_type in query_types:
            hits = {1: 0, 2: 0, 3: 0, "miss": 0}
            total_hit = 0
            for rep in range(repetitions):
                idx += 1
                tmp_path = tmp_base / "recall" / str(n_noise) / q_type / str(rep)
                tmp_path.mkdir(parents=True, exist_ok=True)
                (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")

                agent = build_agent(tmp_path)

                # 1 条正确信息
                correct_note = "deploy key is red and must be set before startup"
                agent.memory.append_note(correct_note, tags=("deploy", "config", "critical"), source="deploy.md")

                # N 条噪声
                noise_templates = [
                    "the {} module has {} tests with {} coverage",
                    "{} configuration is stored in {} directory",
                    "CI pipeline runs {} on every {} push",
                ]
                for i in range(n_noise):
                    a, b, c = [f"noise-{i}-{j}" for j in range(3)]
                    agent.memory.append_note(
                        noise_templates[i % 3].format(a, b, c),
                        tags=(f"noise-{i % 7}", f"cat-{i % 5}"),
                        source=f"noise_{i}.md",
                    )

                if q_type == "exact_tag":
                    query = "What is the deploy key?"
                elif q_type == "keyword_only":
                    query = "Where is the red startup key configured?"
                else:
                    query = "How do I set up the environment?"

                candidates = agent.memory.retrieval_candidates(query, limit=3)

                found_rank = None
                for rank, c in enumerate(candidates, 1):
                    if correct_note == c["text"]:
                        found_rank = rank
                        break

                if found_rank:
                    hits[found_rank] += 1
                    total_hit += 1
                else:
                    hits["miss"] += 1

                print(f"[{idx}/{total}] noise={n_noise:3d} query={q_type:15s} rep={rep + 1} → {'rank-' + str(found_rank) if found_rank else 'MISS'}")

            results.append({
                "noise_count": n_noise,
                "query_type": q_type,
                "top1_hits": hits[1],
                "top2_hits": hits[2],
                "top3_hits": hits[3],
                "misses": hits["miss"],
                "total": repetitions,
                "recall_rate": round(total_hit / repetitions, 4),
            })

    print()
    print("=" * 70)
    print(f"{'noise':>6s} {'query':15s} {'top1':>5s} {'top2':>5s} {'top3':>5s} {'miss':>5s} {'recall':>7s}")
    print("-" * 70)
    for r in results:
        print(f"{r['noise_count']:6d} {r['query_type']:15s} {r['top1_hits']:5d} {r['top2_hits']:5d} {r['top3_hits']:5d} {r['misses']:5d} {r['recall_rate']:7.1%}")

    return results


# ══════════════════════════════════════════════════════════════════════
# 实验 1：压缩语义保真度
# ══════════════════════════════════════════════════════════════════════

MODS = ["authentication", "serialization", "middleware", "repository", "validation"]
ASPS = ["configuration", "error-handling", "logging", "caching", "retry-logic"]
QUALS = ["consistency", "reliability", "performance", "observability", "security"]
COMPS = ["request-handler", "response-builder", "token-validator", "session-manager"]
EDGES = ["concurrent writes", "partial failures", "network timeouts", "large payloads"]
MECHS = ["circuit-breaker", "exponential-backoff", "distributed-lock", "saga-pattern"]
FILS = ["handler.py", "middleware.py", "repository.py", "service.py", "validator.py"]
PATS = ["dependency-injection", "factory-method", "strategy", "observer", "decorator"]

LONG_CONTENT = (
    "The {module} module requires careful {aspect} to ensure {quality}. "
    "Specifically, the {component} must handle {edge_case} gracefully "
    "through {mechanism}. Current implementation in {file} uses {pattern}."
)


def experiment_semantic_fidelity(tmp_base):
    total_turns = 30
    hidden_turn = 8
    secret_codes = [
        f"SECRET-{i:04d}-{['ALPHA','BETA','GAMMA','DELTA','EPSILON','ZETA','ETA','THETA'][i]}"
        for i in range(8)
    ]
    repetitions = 3

    results = []
    for rep in range(repetitions):
        for si, secret in enumerate(secret_codes):
            tmp_path = tmp_base / "fidelity" / str(rep) / str(si)
            tmp_path.mkdir(parents=True, exist_ok=True)
            (tmp_path / "README.md").write_text("# test\n", encoding="utf-8")

            agent = build_agent(tmp_path)

            for turn_idx in range(total_turns):
                if turn_idx == hidden_turn:
                    user_text = f"Task {turn_idx}: Read auth_config.txt and remember the secret code."
                    tool_text = f"# auth_config.txt\n   1: secret_code = {secret}\n"
                    assistant_text = f"The secret code is {secret}. I'll remember this."
                else:
                    user_text = f"Task {turn_idx}: Review {FILS[turn_idx % 5]} and fix {EDGES[turn_idx % 4]}."
                    tool_text = f"# {FILS[turn_idx % 5]}\n   1: import {MODS[turn_idx % 5]}\n   2: \n   3: def handle(): pass\n"
                    assistant_text = LONG_CONTENT.format(
                        module=MODS[turn_idx % 5], aspect=ASPS[turn_idx % 5],
                        quality=QUALS[turn_idx % 5], component=COMPS[turn_idx % 4],
                        edge_case=EDGES[turn_idx % 4], mechanism=MECHS[turn_idx % 4],
                        file=FILS[turn_idx % 5], pattern=PATS[turn_idx % 5],
                    )

                agent.record({"role": "user", "content": user_text, "created_at": f"2026-01-{(turn_idx % 28) + 1:02d}T10:{turn_idx % 60:02d}:00Z"})
                agent.record({"role": "tool", "name": "read_file", "args": {"path": FILS[turn_idx % 5]}, "content": tool_text, "created_at": f"2026-01-{(turn_idx % 28) + 1:02d}T10:{turn_idx % 60:02d}:30Z"})
                agent.record({"role": "assistant", "content": assistant_text, "created_at": f"2026-01-{(turn_idx % 28) + 1:02d}T10:{turn_idx % 60:02d}:59Z"})

            agent.refresh_prefix(force=True)
            manager = ContextManager(agent)
            prompt, metadata = manager.build(f"What was the secret code we found in turn {hidden_turn + 1}?")

            in_prompt = secret in prompt
            hist_raw = metadata["sections"]["history"]["raw_chars"]
            hist_rendered = metadata["sections"]["history"]["rendered_chars"]

            results.append({
                "secret": secret, "rep": rep, "in_prompt": in_prompt,
                "history_raw_chars": hist_raw, "history_rendered_chars": hist_rendered,
            })

            status = "OK" if in_prompt else "LOST!"
            print(f"secret={secret[:12]}...  in_prompt={in_prompt}  "
                  f"history raw={hist_raw} rendered={hist_rendered}  {status}")

    found = sum(1 for r in results if r["in_prompt"])
    total = len(results)
    print(f"\n保真度: {found}/{total} ({found / total * 100:.1f}%)")

    return results


# ══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    tmp_base = Path(ROOT) / "artifacts" / ".tmp_supplement"
    tmp_base.mkdir(parents=True, exist_ok=True)

    print("=" * 60)
    print("实验 ④ 召回精度 — 噪声中捞出正确记忆")
    print("=" * 60)
    recall = experiment_recall_precision(tmp_base)

    print()
    print("=" * 60)
    print("实验 ① 压缩语义保真度 — 压缩后关键事实是否还在 prompt 里")
    print("=" * 60)
    fidelity = experiment_semantic_fidelity(tmp_base)

    out_path = Path(ROOT) / "artifacts" / "supplement-experiments-v3.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({
        "artifact_type": "supplement-experiments-v3",
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "recall_precision": recall,
        "semantic_fidelity": {
            "total_secrets": len(fidelity),
            "secrets_preserved": sum(1 for r in fidelity if r["in_prompt"]),
            "preservation_rate": round(sum(1 for r in fidelity if r["in_prompt"]) / len(fidelity), 4),
        },
    }, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\n写入 {out_path}")
