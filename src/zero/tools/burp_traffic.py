"""Query a TrafficStore loaded from a Burp export."""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from zero.tools.base import RiskLevel, Tool, ToolResult
from zero.traffic.burp_parser import parse_burp_xml
from zero.traffic.store import TrafficStore


_STORE_CACHE: dict[str, TrafficStore] = {}


def _load_store(burp_path: Path) -> TrafficStore:
    key = str(burp_path.resolve())
    if key in _STORE_CACHE:
        return _STORE_CACHE[key]
    store = TrafficStore()
    if burp_path.exists():
        store.extend(parse_burp_xml(burp_path))
    _STORE_CACHE[key] = store
    return store


class BurpTrafficArgs(BaseModel):
    action: Literal["list_hosts", "search", "get"] = Field(
        description="list_hosts: known hosts; search: filter requests; get: full record by id"
    )
    # Search filters (all optional, AND-ed together).
    host: str | None = None
    url_substring: str | None = None
    status: int | None = None
    content_type: str | None = None
    tag: str | None = None
    text: str | None = None
    method: str | None = None
    limit: int = Field(default=20, ge=1, le=100)
    # get-by-id
    id: str | None = None
    # Source. Defaults to ./burp/session.xml or ./burp.xml in cwd.
    burp_path: str | None = None


def _default_burp_path() -> Path | None:
    candidates = [
        Path.cwd() / "burp" / "session.xml",
        Path.cwd() / "burp.xml",
        Path.cwd() / "burp" / "burp.xml",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


class BurpTrafficTool(Tool):
    name = "burp_traffic"
    description = (
        "Query a Burp Suite XML export. Actions: list_hosts | search | get."
        " Search filters: host, url_substring, status, content_type, tag, text, method."
    )
    risk_level = RiskLevel.SAFE
    requires_approval = False  # read-only local Burp XML export — never prompt
    Args = BurpTrafficArgs

    async def run(self, args: BaseModel) -> ToolResult:
        assert isinstance(args, BurpTrafficArgs)
        path = Path(args.burp_path) if args.burp_path else _default_burp_path()
        if path is None:
            return ToolResult(
                ok=False,
                output="no Burp export found; pass burp_path or place one at ./burp/session.xml",
            )
        if not path.exists():
            return ToolResult(ok=False, output=f"burp export not found: {path}")

        store = _load_store(path)

        if args.action == "list_hosts":
            hosts = store.hosts()
            return ToolResult(
                ok=True,
                output="\n".join(hosts) if hosts else "(no traffic captured)",
            )
        if args.action == "get":
            if not args.id:
                return ToolResult(ok=False, output="id required for action=get")
            item = store.get(args.id)
            if item is None:
                return ToolResult(ok=False, output=f"no such request: {args.id}")
            lines = [
                item.summary(),
                "--- request headers ---",
                *[f"{k}: {v}" for k, v in item.request_headers.items()],
                "--- request body ---",
                item.request_body,
                "--- response headers ---",
                *[f"{k}: {v}" for k, v in item.response_headers.items()],
                "--- response body ---",
                item.response_body,
            ]
            return ToolResult(ok=True, output="\n".join(lines))

        # search
        hits = store.search(
            host=args.host,
            url_substring=args.url_substring,
            status=args.status,
            content_type=args.content_type,
            tag=args.tag,
            text=args.text,
            method=args.method,
            limit=args.limit,
        )
        if not hits:
            return ToolResult(ok=True, output="(no matches)")
        return ToolResult(
            ok=True, output="\n".join(h.summary() for h in hits)
        )
