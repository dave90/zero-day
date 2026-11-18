"""Explicit release-only entry point. Default selections never spend tokens."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.live, pytest.mark.live_codex, pytest.mark.gruyere, pytest.mark.ui_live]


def test_release_browser_and_api_matrix():
    if os.environ.get("ZERO_UI_LIVE") != "1":
        pytest.skip(
            "Set ZERO_UI_LIVE=1 after completing the explicit model preflight and inventory"
        )
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("matrix.py")), "--run"], check=True
    )
