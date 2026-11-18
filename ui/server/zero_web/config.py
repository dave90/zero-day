"""Versioned UI configuration. CLI metadata is checked for parity in tests."""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path, PurePosixPath

from pydantic import ConfigDict, StrictBool, StrictFloat, StrictInt, StrictStr, create_model

from zero.orchestrator.orchestrator import OrchestratorConfig
from zero.orchestrator.rate_limit import parse_rate

SCHEMA = json.loads(Path(__file__).with_name("config-schema.json").read_text())
TYPES = {
    "text": StrictStr,
    "select": StrictStr,
    "boolean": StrictBool,
    "number": StrictInt,
    "list": list[StrictStr],
    "multi-select": list[StrictStr],
}
FIELDS = {}
for field in SCHEMA:
    typ = StrictFloat | StrictInt if field["key"] == "container_cpus" else TYPES[field["type"]]
    if field["default"] is None:
        typ = typ | None
    FIELDS[field["key"]] = (typ, field["default"])
RunConfig = create_model("RunConfig", __config__=ConfigDict(extra="forbid"), **FIELDS)


def relative_name(value: str) -> str:
    """A portable relative path, never an absolute path, drive name or traversal."""
    path = PurePosixPath(value)
    if not value or "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Use a relative path inside this engagement")
    if path.is_absolute() or any(part in {"..", "."} for part in value.split("/")):
        raise ValueError("Path escapes the engagement directory")
    if any(part.startswith(".") for part in path.parts):
        raise ValueError("Hidden paths are reserved for runtime state")
    return str(path)


def validate_config(raw: dict) -> dict:
    cfg = RunConfig.model_validate(raw).model_dump()
    for field in SCHEMA:
        value = cfg[field["key"]]
        if field["choices"]:
            values = value if isinstance(value, list) else [value]
            if any(v not in field["choices"] for v in values):
                raise ValueError(f"Invalid {field['label']}")
        if field["type"] == "number" and value is not None:
            minimum = 0 if field["key"] == "max_total_subagents" else 1
            if value < minimum or value > 10_000_000:
                raise ValueError(f"{field['label']} must be between {minimum} and 10000000")
    if cfg["swarm_size"] > 1 and (cfg["execution_mode"] != "c" or cfg["ralph"]):
        raise ValueError("Swarm requires orchestrated mode and cannot be combined with Ralph")
    if cfg["ralph"] and cfg["execution_mode"] != "c":
        raise ValueError("Ralph requires orchestrated mode")
    if cfg["ralph_resume"] and not cfg["ralph"]:
        raise ValueError("Enable Ralph to resume its durable state")
    if "exploit" in cfg["auto_approve"] and not cfg["i_understand"]:
        raise ValueError("Exploit auto-approval requires explicit acknowledgement")
    parse_rate(cfg["rate_limit"])
    if not re.fullmatch(r"[1-9]\d*(?:\.\d+)?[bkmgBKMG]?", cfg["container_memory"]):
        raise ValueError("Invalid container memory; use a value such as 4g")
    cfg["files_root"] = relative_name(cfg["files_root"])
    if cfg["burp_dir"]:
        cfg["burp_dir"] = relative_name(cfg["burp_dir"])
    return cfg


RENAMES = {
    "scope_host": "scope_hosts",
    "llm_timeout": "llm_timeout_seconds",
    "subagent_deadline": "subagent_deadline_seconds",
    "max_subagents": "max_concurrent_subagents",
    "container": "use_container",
}


def core_config(cwd: Path, raw: dict) -> OrchestratorConfig:
    cfg = validate_config(raw)
    values = {RENAMES.get(k, k): v for k, v in cfg.items()}
    values["auto_approve"] = set(values["auto_approve"])
    values["scope_hosts"] = set(values["scope_hosts"])
    values["files_root"] = Path(values["files_root"])
    if values["burp_dir"]:
        values["burp_dir"] = cwd / values["burp_dir"]
    return OrchestratorConfig(cwd=cwd, **values)


def command_for(cfg: dict) -> str:
    args = ["uv", "run", "zero", "run"]
    for field in SCHEMA:
        value = cfg[field["key"]]
        flag = field["flag"].split("/")[0]
        if field["type"] == "boolean":
            if "/" in field["flag"]:
                args.append(field["flag"].split("/")[0 if value else 1])
            elif value:
                args.append(flag)
        elif isinstance(value, list):
            for item in value:
                args.extend([flag, item])
        elif value is not None:
            args.extend([flag, str(value)])
    return shlex.join(args)
