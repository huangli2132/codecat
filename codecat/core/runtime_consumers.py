"""Derived runtime state consumers."""

from .artifacts import build_artifact_graph, build_verifier_suggestions
from .workspace import clip


class ArtifactGraphConsumer:
    def handle(self, runtime, task_state, event):
        if event.get("event") not in {"tool_executed", "run_finished", "checkpoint_created"}:
            return
        if not task_state.changed_paths and not event.get("artifact_paths"):
            return
        graph = build_artifact_graph(runtime.root, task_state.changed_paths)
        task_state.artifact_graph = graph


class VerifierSuggestionConsumer:
    def handle(self, runtime, task_state, event):
        if event.get("event") not in {"tool_executed", "run_finished", "checkpoint_created"}:
            return
        graph = task_state.artifact_graph or build_artifact_graph(runtime.root, task_state.changed_paths)
        task_state.verifier_suggestions = build_verifier_suggestions(runtime.root, graph)


class ReminderConsumer:
    def handle(self, runtime, task_state, event):
        if event.get("event") != "tool_executed":
            return
        status = str(event.get("status", ""))
        if status in {"", "ok"}:
            return
        reminder = {
            "event": "tool_executed",
            "tool": str(event.get("name", "")),
            "status": status,
            "error_type": str(event.get("error_type", "")),
            "message": clip(str(event.get("result", "")), 240),
            "created_at": event.get("created_at", ""),
        }
        task_state.runtime_reminders.append(reminder)


def default_runtime_consumers():
    return [ArtifactGraphConsumer(), VerifierSuggestionConsumer(), ReminderConsumer()]
