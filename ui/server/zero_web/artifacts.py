"""Read-only artifact inventory with explicit containment and inert previews."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from zero_web.config import relative_name
from zero_web.storage import Store

MAX_PREVIEW = 1_000_000


def contained(root: Path, path: Path) -> Path:
    root = root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root) or path.is_symlink():
        raise ValueError("Artifact escapes its registered directory")
    # Reject symlinks even if their current destination happens to be in the jail.
    for parent in path.parents:
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError("Symlink artifacts are not supported")
    return resolved


def base_for(store: Store, eid: str, sid: str | None = None) -> Path:
    work = store.work(eid)
    if sid:
        session = store.session(sid)
        if session["engagement_id"] != eid:
            raise ValueError("Session belongs to another engagement")
        if session["history_path"]:
            return contained(store.root, store.root / session["history_path"])
        if store.engagement(eid)["session_id"] != sid:
            raise ValueError("Historical artifacts are not sealed yet")
    return work


def inventory(store: Store, eid: str, sid: str | None = None) -> list[dict]:
    root = base_for(store, eid, sid)
    engagement = store.engagement(eid)
    config = store.session(sid)["config"] if sid else engagement["config"]
    files_root = relative_name(config.get("files_root", "zero_files"))
    out = []
    for name in (
        "pentest.md",
        ".pentest",
        ".ralph",
        ".ralph_logs",
        ".recordings",
        "goals",
        files_root,
    ):
        path = root / name
        paths = path.rglob("*") if path.is_dir() and not path.is_symlink() else [path]
        for p in paths:
            if len(out) >= 5000:
                break
            try:
                safe = contained(root, p)
                if (
                    not safe.is_file()
                    or safe.suffix in {".sqlite3", ".db"}
                    or safe.name.endswith(("-wal", "-shm"))
                ):
                    continue
                rel = p.relative_to(root).as_posix()
                stat = safe.stat()
                out.append(
                    {
                        "id": hashlib.sha256(rel.encode()).hexdigest()[:24],
                        "path": rel,
                        "size": stat.st_size,
                        "modified_at": stat.st_mtime,
                        "sealed": root != store.work(eid),
                        "session_id": sid,
                    }
                )
            except (OSError, ValueError):
                continue
    return sorted(out, key=lambda a: a["path"])


def artifact(store: Store, eid: str, aid: str, sid: str | None = None):
    item = next((a for a in inventory(store, eid, sid) if a["id"] == aid), None)
    if item is None:
        raise KeyError("Artifact not found")
    path = contained(base_for(store, eid, sid), base_for(store, eid, sid) / item["path"])
    return item, path


def findings(store: Store, eid: str, sid: str | None = None):
    from zero.findings.store import FindingsStore

    root = base_for(store, eid, sid)
    records = []
    # Use the core reader without constructing a store (which creates directories).
    reader = object.__new__(FindingsStore)
    for item in inventory(store, eid, sid):
        if not item["path"].startswith(".pentest/findings/") or not item["path"].endswith(".md"):
            continue
        record = reader._read(contained(root, root / item["path"]))
        if record:
            verdict = item["path"].split("/")[2]
            records.append(
                {
                    **record.model_dump(mode="json"),
                    "verdict": verdict,
                    "artifact_id": item["id"],
                    "session_id": sid,
                }
            )
    return records


def swarm(store: Store, eid: str, sid: str | None = None):
    root = base_for(store, eid, sid)
    path = contained(root, root / ".pentest/swarm/state.sqlite3")
    if not path.exists():
        return {}
    db = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    try:
        result = {}
        for table in ("workers", "work_items", "messages", "groups", "evidence", "events"):
            try:
                rows = db.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT 200").fetchall()
                result[table] = [dict(r) for r in rows]
            except sqlite3.OperationalError:
                result[table] = []
        return result
    finally:
        db.close()


def logs(store: Store, eid: str, kind: str, *, sid=None, offset=0, query="", actor="", limit=100):
    root = base_for(store, eid, sid)
    names = {"audit": ".pentest/audit.jsonl", "reasoning": ".pentest/thinking.log"}
    paths = []
    if kind == "model":
        selected_sid = sid or store.engagement(eid)["session_id"]
        selected = store.session(selected_sid) if selected_sid else {}
        if selected.get("model_log_root") and not selected.get("history_path"):
            root = Path(selected["model_log_root"]).resolve()
            directory = root
        else:
            directory = contained(root, root / ".pentest/llm")
        if directory.is_dir():
            paths = list(directory.glob("*.log"))
    elif kind in names:
        paths = [root / names[kind]]
    result, matched, more = [], 0, False
    for path in paths:
        safe = contained(root, path)
        if not safe.is_file():
            continue
        with safe.open(errors="replace") as handle:
            for line in handle:
                if query.lower() not in line.lower() or (
                    actor and actor not in line and actor not in path.name
                ):
                    continue
                matched += 1
                if matched <= offset:
                    continue
                if len(result) >= limit:
                    more = True
                    break
                try:
                    record = json.loads(line) if kind == "audit" else None
                except json.JSONDecodeError:
                    record = {"malformed": True}
                result.append(
                    {
                        "line": line[:64000],
                        "record": record,
                        "source": path.relative_to(root).as_posix(),
                        "truncated": len(line) > 64000,
                    }
                )
        if more:
            break
    return {
        "items": result,
        "next_offset": offset + len(result) if more else None,
        "available": any(p.exists() for p in paths),
    }
