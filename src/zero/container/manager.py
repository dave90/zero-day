"""Docker sandbox lifecycle.

Shells out to the `docker` CLI rather than depending on the Python SDK —
unit tests can then assert on the argv list (`build_run_args`), which is the
property the plan actually cares about (mount layout, CPU/mem caps, host
networking).

The plan called for a unix socket in a shared volume; on Docker Desktop
(macOS) that doesn't work because uvicorn chmods the UDS after bind and
the chmod fails over virtiofs. We use TCP on 127.0.0.1 instead, with the
in-container port published to an ephemeral host port that we discover
with `docker port` after start. Works the same on Linux and macOS.
"""
from __future__ import annotations

import asyncio
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_IMAGE = "zero-sandbox:latest"
DEFAULT_WORK_MOUNT = "/work"
DEFAULT_SOCK_NAME = "zero.sock"  # kept for back-compat; no longer the wire
RPC_PORT_IN_CONTAINER = 7333


@dataclass
class ContainerSpec:
    image: str = DEFAULT_IMAGE
    name: str | None = None  # auto-generated if None

    # Host paths.
    cwd: Path = field(default_factory=Path.cwd)
    claude_dir_host: Path | None = None  # auto: ~/.claude
    burp_dir_host: Path | None = None  # if present, mounted ro at /work/burp

    # Resource caps.
    cpus: float = 4.0
    memory: str = "4g"

    # Networking.
    host_network: bool = False  # for Burp-on-127.0.0.1; otherwise host.docker.internal
    # If 0 (default) Docker picks an ephemeral host port; we discover it via
    # `docker port` after start. Override for deterministic local testing.
    rpc_port_host: int = 0

    def resolved_name(self) -> str:
        return self.name or f"zero-{uuid.uuid4().hex[:8]}"

    def resolved_claude_dir(self) -> Path | None:
        if self.claude_dir_host is not None:
            return self.claude_dir_host
        default = Path.home() / ".claude"
        return default if default.exists() else None


def build_run_args(spec: ContainerSpec, *, name: str) -> list[str]:
    """Pure-function assembly of the `docker run` argv. Easily unit-tested."""
    args: list[str] = [
        "docker", "run", "-d",
        "--name", name,
        "--rm",
        f"--cpus={spec.cpus}",
        f"--memory={spec.memory}",
        "-v", f"{spec.cwd}:{DEFAULT_WORK_MOUNT}:rw",
        "-w", DEFAULT_WORK_MOUNT,
    ]
    claude_dir = spec.resolved_claude_dir()
    if claude_dir is not None:
        args += ["-v", f"{claude_dir}:/root/.claude:ro"]
    if spec.burp_dir_host is not None:
        args += ["-v", f"{spec.burp_dir_host}:{DEFAULT_WORK_MOUNT}/burp:ro"]
    if spec.host_network:
        args += ["--network", "host"]
    else:
        # Linux: --add-host=host.docker.internal:host-gateway makes the host
        # reachable. On Docker Desktop the alias exists by default; passing
        # this flag is a no-op there.
        args += ["--add-host", "host.docker.internal:host-gateway"]
        # Publish the RPC port to 127.0.0.1 on the host. Port 0 ⇒ Docker
        # picks an ephemeral host port; we discover it via `docker port`
        # after start. `host_network` mode skips this — the in-container
        # port is reachable as 127.0.0.1:RPC_PORT_IN_CONTAINER directly.
        host_port = "" if spec.rpc_port_host == 0 else str(spec.rpc_port_host)
        args += ["-p", f"127.0.0.1:{host_port}:{RPC_PORT_IN_CONTAINER}"]
    args.append(spec.image)
    return args


class ContainerError(RuntimeError):
    pass


class ContainerManager:
    def __init__(self, spec: ContainerSpec) -> None:
        self.spec = spec
        self.name: str | None = None
        self.rpc_host: str = "127.0.0.1"
        self.rpc_port: int = 0  # discovered after start
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Create + run the container. Idempotent: no-op if already started."""
        async with self._lock:
            if self.name is not None:
                return
            if shutil.which("docker") is None:
                raise ContainerError("docker CLI not found on PATH")
            name = self.spec.resolved_name()
            argv = build_run_args(self.spec, name=name)
            rc, _out, err = await _run(argv)
            if rc != 0:
                raise ContainerError(f"docker run failed: {err.strip()}")
            self.name = name
            if self.spec.host_network:
                # in host network mode the in-container port is reachable
                # at 127.0.0.1:<port> directly.
                self.rpc_port = RPC_PORT_IN_CONTAINER
            else:
                self.rpc_port = await self._discover_published_port()
            await self._wait_for_rpc_ready()

    async def stop(self) -> None:
        """Stop + remove the container. Idempotent."""
        async with self._lock:
            if self.name is None:
                return
            await _run(["docker", "stop", self.name])
            # --rm in `run` removes on stop, but make doubly sure.
            await _run(["docker", "rm", "-f", self.name])
            self.name = None
            self.rpc_port = 0

    async def is_running(self) -> bool:
        if self.name is None:
            return False
        rc, out, _err = await _run(
            ["docker", "inspect", "-f", "{{.State.Running}}", self.name]
        )
        return rc == 0 and out.strip() == "true"

    def rpc_base_url(self) -> str:
        return f"http://{self.rpc_host}:{self.rpc_port}"

    async def _discover_published_port(self) -> int:
        """Ask Docker which host port it bound RPC_PORT_IN_CONTAINER to.

        Retries briefly because immediately after `docker run -d` the port
        mapping may not yet be populated in the daemon's state.
        """
        assert self.name is not None
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 10.0
        last_err = ""
        while loop.time() < deadline:
            rc, out, err = await _run(
                ["docker", "port", self.name, f"{RPC_PORT_IN_CONTAINER}/tcp"]
            )
            if rc != 0:
                last_err = err.strip()
            else:
                for line in out.splitlines():
                    line = line.strip()
                    if not line or line.startswith("[::]"):
                        continue
                    if ":" in line:
                        _host, _, port = line.rpartition(":")
                        try:
                            return int(port)
                        except ValueError:
                            continue
                last_err = f"empty output: {out!r}"
            await asyncio.sleep(0.2)
        raise ContainerError(f"could not discover published port: {last_err}")

    async def _wait_for_rpc_ready(self, timeout: float = 15.0) -> None:
        """Poll /healthz until the RPC server is up. Captures crash logs."""
        import httpx

        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        url = self.rpc_base_url() + "/healthz"
        last_err: Exception | None = None
        while loop.time() < deadline:
            if not await self.is_running():
                rc, logs, _ = await _run(["docker", "logs", self.name or ""])
                raise ContainerError(
                    "container exited before RPC was ready. logs:\n" + logs
                )
            try:
                async with httpx.AsyncClient(timeout=1.0) as client:
                    resp = await client.get(url)
                if resp.status_code == 200:
                    return
            except httpx.HTTPError as e:
                last_err = e
            await asyncio.sleep(0.2)
        raise ContainerError(f"RPC never came up at {url}: {last_err!r}")


async def _run(argv: list[str]) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out_b, err_b = await proc.communicate()
    return proc.returncode or 0, out_b.decode("utf-8", "replace"), err_b.decode("utf-8", "replace")
