# Pico v3 testing

The v3 acceptance package is a real-use scenario suite, not a unit-test index.
It drives Pico through CLI, REPL, slash command, resume, provider profile,
skills, worker, memory, and artifact flows, then verifies the files Pico wrote.

## Final Result

```text
uv run python scripts/run_v3_human_scenario_gate.py --suite full
{"failed": 0, "output_dir": "/private/tmp/codecat-v3-human-scenarios/20260513-170838", "passed": 50, "status": "passed"}
```

```text
uv run ruff check .
All checks passed!

uv run pytest tests -q
224 passed, 2 skipped, 6 warnings in 68.50s
```

## Quick Rerun

Run the prioritized gate:

```bash
uv run python scripts/run_v3_human_scenario_gate.py
```

Run all 50 human scenarios:

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full
```

Run selected scenarios:

```bash
uv run python scripts/run_v3_human_scenario_gate.py --suite full --scenario S21 --scenario S23
```

## Detailed Records

| Path | Purpose |
| --- | --- |
| `release/v3/testing/01-test-design.md` | 50 human scenarios and coverage matrix. |
| `release/v3/testing/02-execution-record.md` | Full execution record, found issues, fixes, and verification. |
| `release/v3/testing/03-runner-and-evidence.md` | Runner behavior, output directory, evidence adapter, failure triage. |
| `release/v3/testing/04-scenario-checklist.md` | Scenario checklist for review and future reruns. |
