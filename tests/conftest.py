"""Top-level test conftest.

`pytest_addoption` must live in a conftest at the tests root (or a plugin) to be
discovered during startup — a hook in a subpackage conftest (e.g.
`tests/regression/conftest.py`) is ignored. So the regression suite's
`--regression-backend` switch is declared here even though only the regression
tests read it.
"""
from __future__ import annotations


def pytest_addoption(parser):
    parser.addoption(
        "--regression-backend",
        action="store",
        default="claude",
        choices=("claude", "codex"),
        help="LLM backend to drive the capability regression / blind suites against"
        " (default: claude). Use 'codex' to run the SAME vulnerable targets through the"
        " OpenAI Codex CLI. Requires that backend's CLI on PATH + authenticated.",
    )
