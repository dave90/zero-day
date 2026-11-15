import shlex
from pathlib import Path

import pytest
from click.testing import CliRunner

from zero.cli import main
from zero_web.config import RunConfig, SCHEMA, command_for, core_config, validate_config


def test_every_cli_option_and_effective_default_is_mapped():
    command = main.commands["run"]
    options = {p.name: p for p in command.params}
    assert len(SCHEMA) == 38
    assert {f["key"] for f in SCHEMA} == set(options)
    cfg = RunConfig().model_dump()
    runtime = core_config(Path("/tmp/test"), cfg)
    for key in ("max_turns", "pentester_max_turns", "validator_max_turns"):
        assert cfg[key] == getattr(runtime, key) == 50
    with command.make_context("run", []) as context:
        defaults = context.params
    for field in SCHEMA:
        default = defaults[field["key"]]
        if options[field["key"]].multiple:
            default = list(default or ())
        if isinstance(default, Path):
            default = str(default)
        assert cfg[field["key"]] == default, field["key"]


@pytest.mark.parametrize(
    "changes",
    [
        {"swarm_size": 0},
        {"swarm_size": 2, "execution_mode": "a"},
        {"swarm_size": 2, "ralph": True},
        {"execution_mode": "b", "backend": "codex"},
        {"ralph": True, "execution_mode": "a"},
        {"auto_approve": ["exploit"]},
        {"max_turns": "ten"},
        {"max_turns": True},
        {"max_total_subagents": -1},
        {"rate_limit": "infinity"},
        {"files_root": "../secrets"},
        {"burp_dir": "/tmp"},
        {"files_root": ".pentest"},
        {"container_memory": "unlimited"},
        {"extra": True},
        {"ralph_resume": True},
        {"auto_approve": ["destructive"]},
    ],
)
def test_invalid_configuration_is_rejected(changes):
    with pytest.raises(ValueError):
        validate_config(changes)


def test_empty_model_lists_and_shell_values_roundtrip():
    cfg = validate_config(
        {
            "model": "",
            "goal": "literal $(touch /tmp/nope); `echo x` 'value'",
            "scope_host": ["a.example", "b.example"],
            "auto_approve": ["safe", "active"],
            "max_total_subagents": 0,
        }
    )
    args = shlex.split(command_for(cfg))
    assert args[args.index("--model") + 1] == ""
    assert args[args.index("--goal") + 1] == cfg["goal"]
    assert args.count("--scope-host") == 2
    assert args.count("--auto-approve") == 2
    assert core_config(Path("/tmp/test"), cfg).validator_claude_max_turns == 1
    assert core_config(Path("/tmp/test"), cfg).max_total_subagents == 0


def test_ui_configuration_does_not_change_cli():
    result = CliRunner().invoke(main, ["run", "--help"])
    assert result.exit_code == 0
    assert "--execution-mode" in result.output
