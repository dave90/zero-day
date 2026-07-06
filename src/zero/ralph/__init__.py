"""Ralph mode: run zero in an iterative "Ralph Wiggum" loop.

An outer driver that wraps the orchestrated (mode c) machinery. It first builds a
prioritized PRD of pentest tasks (an `analyzer` agent, refined in a bounded
discussion with an `analyzer_validator`), then runs repeated bounded orchestrated
passes — each a fresh context — where the orchestrator picks the highest-priority
incomplete task, works it, and records progress. Durable state (`prd.json`,
`progress.txt`) lives under `.ralph/`; confirmed findings carry forward; everything
else under `.pentest/` is snapshotted to `.ralph_logs/.pentest_<n>` and reset between
iterations. See architecture.md / plan.md.
"""
