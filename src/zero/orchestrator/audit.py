"""Append-only JSONL audit log.

One line per event. The audit log is the deterministic source of truth for
e2e tests and `zero replay`. Every write is flushed so that a crash never
leaves a partial line behind (the next write would append a new line at the
truncation point regardless).
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any


class AuditLog:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # If a previous crash left a partial trailing line (no terminating
        # newline), prepend a newline before our first write so the new
        # record sits on its own line. The partial line stays in the file
        # but read_audit skips it because it fails to parse.
        needs_leading_newline = False
        if self.path.exists() and self.path.stat().st_size > 0:
            with open(self.path, "rb") as fh:
                fh.seek(-1, 2)
                if fh.read(1) != b"\n":
                    needs_leading_newline = True
        self._fh = open(self.path, "a", encoding="utf-8")
        if needs_leading_newline:
            self._fh.write("\n")
            self._fh.flush()

    def write(self, kind: str, **fields: Any) -> None:
        record: dict[str, Any] = {
            "ts": time.time(),
            "kind": kind,
        }
        for k, v in fields.items():
            record[k] = _jsonify(v)
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        self._fh.write(line + "\n")
        self._fh.flush()
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> "AuditLog":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()


def read_audit(path: Path) -> list[dict[str, Any]]:
    """Read all records from an audit log; skip blank or partial trailing lines."""
    out: list[dict[str, Any]] = []
    if not path.exists():
        return out
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                # partial trailing line from a crash mid-write; ignore
                continue
    return out


def _jsonify(v: Any) -> Any:
    if is_dataclass(v) and not isinstance(v, type):
        return {k: _jsonify(val) for k, val in asdict(v).items()}
    if isinstance(v, dict):
        return {k: _jsonify(val) for k, val in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonify(x) for x in v]
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    return repr(v)
