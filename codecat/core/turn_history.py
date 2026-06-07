"""Turn-aware transcript rendering."""

import json
from collections import OrderedDict


def tail_clip(text, limit):
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


class TurnHistoryBuilder:
    def __init__(self, agent):
        self.agent = agent

    def enrich(self, item):
        item = dict(item)
        if not item.get("turn_id"):
            current_turn = str(getattr(self.agent, "current_turn_id", "") or "")
            if not current_turn:
                if item.get("role") == "user" or not self.agent.session.get("_manual_turn_id"):
                    self.agent.session["_manual_turn_seq"] = int(self.agent.session.get("_manual_turn_seq", 0)) + 1
                    self.agent.session["_manual_turn_id"] = f"manual_{self.agent.session['_manual_turn_seq']:06d}"
                current_turn = str(self.agent.session.get("_manual_turn_id", "legacy"))
            item["turn_id"] = current_turn
        if not item.get("run_id"):
            item["run_id"] = str(getattr(self.agent, "current_run_id", "") or "")
        if not item.get("event_id"):
            self.agent.session["_event_seq"] = int(self.agent.session.get("_event_seq", 0)) + 1
            item["event_id"] = f"event_{self.agent.session['_event_seq']:06d}"
        item.setdefault("source", "runtime")
        return item

    def raw_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        return "\n".join(["Transcript:", *self._render_turn_lines(history, line_limit=2000)])

    def render_section(self, budget):
        history = list(getattr(self.agent, "session", {}).get("history", []))
        raw = self.raw_text(history)
        if not history:
            return raw, {
                "rendered_entries": [],
                "older_entries_count": 0,
                "collapsed_duplicate_reads": 0,
                "reused_file_summary_count": 0,
                "summarized_tool_count": 0,
                "rendered_turns": 0,
            }

        turns = self._group_turns(history)
        recent_window = 3
        recent_turns = set(list(turns)[-recent_window:])
        entries, details = self._compressed_turn_entries(turns, recent_turns)
        rendered_entries = []
        for entry in reversed(entries):
            candidate = entry["lines"] + rendered_entries
            if len("\n".join(["Transcript:", *candidate])) <= budget:
                rendered_entries = candidate
                continue
            if entry["turn_id"] in recent_turns:
                clipped = [tail_clip(line, max(40, budget // max(1, len(entry["lines"])))) for line in entry["lines"]]
                candidate = clipped + rendered_entries
                if len("\n".join(["Transcript:", *candidate])) <= budget:
                    rendered_entries = candidate
        rendered = "\n".join(["Transcript:", *rendered_entries])
        if len(rendered) > budget and budget > 0:
            rendered = tail_clip(raw, budget)
        details["rendered_entries"] = rendered_entries
        details["rendered_turns"] = sum(1 for line in rendered_entries if line.startswith("Turn "))
        return rendered, details

    def _group_turns(self, history):
        turns = OrderedDict()
        for item in history:
            turn_id = str(item.get("turn_id") or "legacy")
            turns.setdefault(turn_id, []).append(item)
        return turns

    def _compressed_turn_entries(self, turns, recent_turns):
        entries = []
        seen_older_reads = set()
        details = {
            "recent_window": len(recent_turns),
            "older_entries_count": 0,
            "collapsed_duplicate_reads": 0,
            "reused_file_summary_count": 0,
            "summarized_tool_count": 0,
        }
        for turn_id, items in turns.items():
            recent = turn_id in recent_turns and any(item.get("role") != "tool" for item in items)
            lines = [f"Turn {turn_id}:"]
            for item in items:
                if item.get("kind") == "compact_summary":
                    lines.extend(str(item.get("content", "")).splitlines())
                    continue
                if not recent and item.get("role") == "tool" and item.get("name") == "read_file":
                    path = str(item.get("args", {}).get("path", "")).strip()
                    if path in seen_older_reads:
                        details["collapsed_duplicate_reads"] += 1
                        continue
                    seen_older_reads.add(path)
                    summary = self._reusable_file_summary(path)
                    if summary:
                        lines.append(f"{path} -> {summary}")
                        details["reused_file_summary_count"] += 1
                        continue
                if not recent and item.get("role") == "tool":
                    lines.append(self._summarize_old_tool_item(item))
                    details["summarized_tool_count"] += 1
                    continue
                lines.extend(self._render_item(item, 900 if recent else 80))
            if not recent:
                details["older_entries_count"] += 1
            entries.append({"turn_id": turn_id, "lines": lines})
        return entries, details

    def _render_turn_lines(self, history, line_limit):
        lines = []
        for turn_id, items in self._group_turns(history).items():
            lines.append(f"Turn {turn_id}:")
            for item in items:
                lines.extend(self._render_item(item, line_limit))
        return lines

    def _render_item(self, item, line_limit):
        if item.get("kind") == "compact_summary":
            return str(item.get("content", "")).splitlines()
        if item.get("role") == "tool":
            prefix = f"[tool:{item.get('name', '')}] {json.dumps(item.get('args', {}), sort_keys=True)}"
            content = tail_clip(item.get("content", ""), max(20, line_limit))
            return [prefix, content]
        return [f"[{item.get('role', '')}] {tail_clip(item.get('content', ''), line_limit)}"]

    def _reusable_file_summary(self, path):
        memory = getattr(self.agent, "memory", None)
        if memory is None or not hasattr(memory, "to_dict"):
            return ""
        summary = memory.to_dict().get("file_summaries", {}).get(str(path), {})
        return str(summary.get("summary", "")).strip()

    def _summarize_old_tool_item(self, item):
        if item.get("name") == "run_shell":
            command = str(item.get("args", {}).get("command", "")).strip() or "shell"
            lines = [line.strip() for line in str(item.get("content", "")).splitlines() if line.strip()]
            return f"{command} -> {' | '.join(lines[:3]) if lines else '(empty)'}"
        return self._render_item(item, 80)[0]
