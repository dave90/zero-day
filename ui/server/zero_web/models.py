"""Discover the installed Codex catalog without starting a thread or changing settings."""

from __future__ import annotations

import asyncio
import json
import shutil
import time
from pathlib import Path

from zero.llm.codex_cli import _codex_home, codex_configured_model


class DiscoveryUnavailable(Exception):
    """A bounded catalog query failed; cached hints may still be useful."""


def normalize_models(rows: list) -> list[dict]:
    """Expose only picker metadata, never cache instructions, identity or auth data."""
    models = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("hidden") is True:
            continue
        if row.get("visibility", "list") != "list":
            continue
        model = row.get("model") or row.get("slug") or row.get("id")
        if not isinstance(model, str) or not model.strip() or len(model) > 256:
            continue
        if any(ord(char) < 32 for char in model):
            continue
        name = row.get("displayName") or row.get("display_name") or model
        description = row.get("description", "")
        models.setdefault(
            model,
            {
                "id": model,
                "name": name if isinstance(name, str) else model,
                "description": description if isinstance(description, str) else "",
            },
        )
    return list(models.values())


def cached_models() -> tuple[list[dict], str | None]:
    path = _codex_home() / "models_cache.json"
    try:
        if path.stat().st_size > 16_000_000:
            return [], None
        cache = json.loads(path.read_text(encoding="utf-8"))
        rows = cache.get("models", [])
        fetched = cache.get("fetched_at")
        return (
            normalize_models(rows) if isinstance(rows, list) else [],
            fetched if isinstance(fetched, str) else None,
        )
    except (OSError, ValueError, AttributeError):
        return [], None


async def query_codex_models(cwd: Path, *, timeout: float = 12) -> list[dict]:
    """Use Codex's versioned app-server model/list API, with no thread/turn commands.

    `model` is the identifier accepted by --model; an app-server record's `id` may
    be a different opaque picker identity. Notifications are not RPC responses.
    """
    binary = shutil.which("codex")
    if binary is None:
        raise DiscoveryUnavailable("Codex CLI is not installed or is not on PATH.")
    process = None
    try:
        async with asyncio.timeout(timeout):
            process = await asyncio.create_subprocess_exec(
                binary,
                "app-server",
                "--listen",
                "stdio://",
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                limit=4_000_000,
            )
            next_id = 0

            async def send(payload):
                process.stdin.write((json.dumps(payload) + "\n").encode())
                await process.stdin.drain()

            async def request(method, params):
                nonlocal next_id
                next_id += 1
                request_id = next_id
                await send({"id": request_id, "method": method, "params": params})
                while line := await process.stdout.readline():
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(reply, dict):
                        continue
                    if "method" in reply:
                        # Discovery never answers approval, login or tool requests.
                        if "id" in reply:
                            await send(
                                {
                                    "id": reply["id"],
                                    "error": {"code": -32601, "message": "Catalog-only client"},
                                }
                            )
                        continue
                    if reply.get("id") != request_id:
                        continue
                    if "error" in reply or not isinstance(reply.get("result"), dict):
                        raise DiscoveryUnavailable("Codex could not provide its model catalog.")
                    return reply["result"]
                raise DiscoveryUnavailable("Codex exited before returning its model catalog.")

            await request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "zero_studio",
                        "title": "Zero Studio",
                        "version": "0.1.0",
                    },
                    "capabilities": {"experimentalApi": False, "explicitGatewayOauth": True},
                },
            )
            await send({"method": "initialized", "params": {}})
            rows, cursor, seen = [], None, set()
            for _ in range(25):
                page = await request(
                    "model/list", {"includeHidden": False, "limit": 100, "cursor": cursor}
                )
                if not isinstance(page.get("data"), list):
                    raise DiscoveryUnavailable("Codex returned an unsupported model catalog.")
                rows.extend(page["data"])
                cursor = page.get("nextCursor")
                if not cursor:
                    return normalize_models(rows)
                if not isinstance(cursor, str) or cursor in seen:
                    raise DiscoveryUnavailable("Codex returned an invalid model catalog cursor.")
                seen.add(cursor)
            raise DiscoveryUnavailable("Codex model catalog exceeded its page limit.")
    except (OSError, TimeoutError, ValueError) as exc:
        raise DiscoveryUnavailable("Codex model discovery is unavailable. Try refreshing.") from exc
    finally:
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 1)
            except TimeoutError:
                if process.returncode is None:
                    process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 1)
                except TimeoutError:
                    if process.returncode is None:
                        process.kill()
                    await process.wait()


class CodexCatalog:
    """Short-lived, per-service metadata cache. Explicit refresh queries Codex again."""

    def __init__(self, cwd: Path):
        self.cwd = cwd
        self.lock = asyncio.Lock()
        self.value = None
        self.expires = 0.0
        self.home = None

    async def get(self, *, refresh=False):
        async with self.lock:
            home = _codex_home()
            if (
                not refresh
                and self.value is not None
                and self.home == home
                and time.monotonic() < self.expires
            ):
                return {**self.value, "configured_default": codex_configured_model() or None}
            warning, cache_time = "", None
            try:
                models = await query_codex_models(self.cwd)
                source = "codex_app_server"
            except DiscoveryUnavailable as exc:
                models, cache_time = cached_models()
                source = "codex_cache" if models else "unavailable"
                warning = str(exc)
                if models:
                    warning += " Showing cached model hints; Codex checks access when a run starts."
            self.value = {
                "backend": "codex",
                "models": models,
                "source": source,
                "warning": warning,
                "retrieved_at": time.time(),
                "cached_at": cache_time,
                "configured_default": codex_configured_model() or None,
            }
            self.home, self.expires = (
                home,
                time.monotonic() + (60 if source == "codex_app_server" else 10),
            )
            return self.value
