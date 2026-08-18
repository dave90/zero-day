"""User-facing CLI defaults that must stay aligned with production config."""

from zero.cli import main


def test_run_backend_defaults_to_codex():
    run_command = main.commands["run"]
    backend_option = next(param for param in run_command.params if param.name == "backend")

    assert backend_option.default == "codex"
