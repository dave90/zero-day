"""Argv-only tests for ContainerManager. No Docker required."""
from __future__ import annotations

from pathlib import Path

import zero.container.manager as mgr
from zero.container.manager import (
    DEFAULT_IMAGE,
    DEFAULT_WORK_MOUNT,
    FILES_ROOT_IN_CONTAINER,
    ContainerManager,
    ContainerSpec,
    build_run_args,
)


def _idx(args: list[str], needle: str) -> int:
    for i, a in enumerate(args):
        if a == needle:
            return i
    raise AssertionError(f"{needle!r} not in {args}")


def test_minimal_args_image_is_last(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    args = build_run_args(spec, name="zero-test")
    assert args[0] == "docker" and args[1] == "run"
    assert args[-1] == DEFAULT_IMAGE
    assert "--name" in args
    name_idx = _idx(args, "--name")
    assert args[name_idx + 1] == "zero-test"


def test_cwd_mounted_rw(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    args = build_run_args(spec, name="x")
    expected = f"{tmp_path}:{DEFAULT_WORK_MOUNT}:rw"
    assert expected in args


def test_files_root_mounted_and_env_set(tmp_path: Path):
    """The read-only fs tools' jail root is bind-mounted at /zero_files and
    advertised via ZERO_FILES_ROOT so the tools confine to it."""
    root = tmp_path / "loot"
    root.mkdir()
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, files_root=root)
    args = build_run_args(spec, name="x")
    assert f"{root}:{FILES_ROOT_IN_CONTAINER}:rw" in args
    env_idx = args.index("-e")
    # find the ZERO_FILES_ROOT -e (there may be others later)
    assert any(
        args[i] == "-e" and args[i + 1] == f"ZERO_FILES_ROOT={FILES_ROOT_IN_CONTAINER}"
        for i in range(len(args) - 1)
    ), args
    assert env_idx  # sanity: an -e exists


def test_rpc_port_published(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    args = build_run_args(spec, name="x")
    # default rpc_port_host=0 ⇒ Docker picks ephemeral host port.
    assert "-p" in args
    pub_idx = args.index("-p")
    assert args[pub_idx + 1] == "127.0.0.1::7333"


def test_rpc_port_fixed_when_set(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, rpc_port_host=18765)
    args = build_run_args(spec, name="x")
    pub_idx = args.index("-p")
    assert args[pub_idx + 1] == "127.0.0.1:18765:7333"


def test_host_network_skips_port_publish(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, host_network=True)
    args = build_run_args(spec, name="x")
    assert "-p" not in args  # host network mode shares the host's stack


def test_claude_dir_mounted_ro_when_present(tmp_path: Path):
    claude = tmp_path / "claude_home"
    claude.mkdir()
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=claude)
    args = build_run_args(spec, name="x")
    assert f"{claude}:/root/.claude:ro" in args


def test_no_claude_mount_when_missing(tmp_path: Path):
    # Explicitly pass a missing dir => resolved_claude_dir returns it as-is
    # only if you set it; passing None and HOME having no .claude => skipped.
    # Use the explicit-None path to avoid depending on the test machine's HOME.
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=tmp_path / "nonexistent")
    # The explicit override is honored even if it doesn't exist on the host —
    # docker will fail to start, but build_run_args is pure.
    args = build_run_args(spec, name="x")
    assert any(":/root/.claude:ro" in a for a in args)


def test_burp_dir_mount_ro(tmp_path: Path):
    burp = tmp_path / "burp"
    burp.mkdir()
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, burp_dir_host=burp)
    args = build_run_args(spec, name="x")
    assert f"{burp}:{DEFAULT_WORK_MOUNT}/burp:ro" in args


def test_cpu_and_mem_caps(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, cpus=2.5, memory="1g")
    args = build_run_args(spec, name="x")
    assert "--cpus=2.5" in args
    assert "--memory=1g" in args


def test_host_network_flag(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, host_network=True)
    args = build_run_args(spec, name="x")
    i = _idx(args, "--network")
    assert args[i + 1] == "host"
    # And no host.docker.internal alias is added (host network makes it redundant).
    assert not any(a.startswith("host.docker.internal") for a in args)


def test_add_host_alias_by_default(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    args = build_run_args(spec, name="x")
    i = _idx(args, "--add-host")
    assert args[i + 1] == "host.docker.internal:host-gateway"


def test_detached_and_rm(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    args = build_run_args(spec, name="x")
    assert "-d" in args
    assert "--rm" in args


def test_workdir_set(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    args = build_run_args(spec, name="x")
    i = _idx(args, "-w")
    assert args[i + 1] == DEFAULT_WORK_MOUNT


def test_custom_image(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, image="my/img:dev")
    args = build_run_args(spec, name="x")
    assert args[-1] == "my/img:dev"


def test_resolved_name_is_unique():
    spec = ContainerSpec()
    a = spec.resolved_name()
    b = spec.resolved_name()
    assert a.startswith("zero-")
    assert b.startswith("zero-")
    assert a != b  # uuid-based


def test_network_field_defaults_none(tmp_path: Path):
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None)
    assert spec.network is None
    assert "--network" not in build_run_args(spec, name="x")  # default bridge


def test_extra_network_joined_at_create(tmp_path: Path):
    """spec.network is joined at `docker run` time (so the sandbox gets Docker's
    embedded DNS to resolve target container names), while RPC stays published."""
    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, network="shared_net")
    args = build_run_args(spec, name="x")
    i = _idx(args, "--network")
    assert args[i + 1] == "shared_net"
    # RPC publish is preserved on the user-defined network.
    assert "-p" in args
    assert "127.0.0.1::7333" in args
    # host gateway alias still added.
    assert "host.docker.internal:host-gateway" in args


def test_extra_network_ignored_under_host_network(tmp_path: Path):
    # host_network shares the host stack — the extra network is redundant/invalid.
    spec = ContainerSpec(
        cwd=tmp_path, claude_dir_host=None, network="shared_net", host_network=True
    )
    args = build_run_args(spec, name="x")
    i = _idx(args, "--network")
    assert args[i + 1] == "host"
    assert "shared_net" not in args


async def test_start_with_network_runs_clean(tmp_path: Path, monkeypatch):
    """start() no longer issues a post-start `docker network connect`."""
    calls: list[list[str]] = []

    async def fake_run(argv):
        calls.append(argv)
        return 0, "", ""

    monkeypatch.setattr(mgr, "_run", fake_run)
    monkeypatch.setattr(mgr.shutil, "which", lambda _name: "/usr/bin/docker")

    async def _no_discover(self):
        return 12345

    async def _no_wait(self, timeout: float = 15.0):
        return None

    monkeypatch.setattr(ContainerManager, "_discover_published_port", _no_discover)
    monkeypatch.setattr(ContainerManager, "_wait_for_rpc_ready", _no_wait)

    spec = ContainerSpec(cwd=tmp_path, claude_dir_host=None, network="shared_net")
    cm = ContainerManager(spec)
    await cm.start()
    # The network is in `docker run` argv, not a separate connect call.
    assert not any(c[:3] == ["docker", "network", "connect"] for c in calls)
    run_call = next(c for c in calls if c[:2] == ["docker", "run"])
    assert "shared_net" in run_call
