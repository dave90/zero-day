"""Durable web journal; Zero artifacts remain authoritative for assessment evidence."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path

TERMINAL = {"closed", "failed", "interrupted"}


def identity() -> str:
    return uuid.uuid4().hex


def blank_state() -> dict:
    return {
        "seq": 0,
        "lifecycle": "starting",
        "activity": "idle",
        "messages": [],
        "actors": {},
        "tools": {},
        "interactions": {},
        "effective": {},
        "phase": "",
        "error": "",
        "findings": {},
        "run_id": "",
    }


def project(state: dict, event: dict) -> dict:
    """Deterministic projection replayed from the journal, including expired waits."""
    kind, p = event["kind"], event["payload"]
    state["seq"] = event["seq"]
    if kind == "user_text" or not state["run_id"]:
        state["run_id"] = event["run_id"]
    if kind == "runtime_state":
        for key in ("lifecycle", "activity"):
            if p.get(key):
                state[key] = p[key]
        state["effective"].update({k: v for k, v in p.items() if v not in ("", None)})
    elif kind in {"user_text", "agent_text_delta"}:
        mid = event["message_id"]
        msg = next((m for m in state["messages"] if m["id"] == mid), None)
        if msg is None:
            msg = {
                "id": mid,
                "role": "user" if kind == "user_text" else "assistant",
                "actor": event["actor_id"],
                "text": "",
                "complete": kind == "user_text",
                "timestamp": event["timestamp"],
                "run_id": event["run_id"],
            }
            state["messages"].append(msg)
        msg["text"] += p.get("text", "")
    elif kind == "agent_text_end":
        for msg in state["messages"]:
            if msg["id"] == event["message_id"]:
                msg["complete"] = True
    elif kind in {"approval_prompt", "ask_user", "plan_checkpoint"}:
        rid = p.get("call_id") or p.get("ask_id") or p.get("request_id")
        state["interactions"][rid] = {**p, "id": rid, "kind": kind, "actor_id": event["actor_id"]}
    elif kind == "interaction_resolved":
        state["interactions"].pop(p["request_id"], None)
    elif kind in {"tool_proposal", "tool_running", "tool_observation"}:
        rid = p["call_id"]
        state["tools"][rid] = {
            **state["tools"].get(rid, {}),
            **p,
            "actor_id": event["actor_id"],
            "state": kind,
        }
    elif kind == "actor_snapshot":
        for actor in p["actors"]:
            aid = actor["id"]
            state["actors"][aid] = {**state["actors"].get(aid, {}), **actor}
    elif kind.startswith("subagent_"):
        aid = p.get("subagent_id", event["actor_id"])
        state["actors"][aid] = {
            **state["actors"].get(aid, {}),
            **p,
            "id": aid,
            "state": "done" if kind == "subagent_finished" else "running",
        }
    elif kind == "status":
        state["status"] = p
    elif kind in {"ralph_iteration", "swarm_phase"}:
        state["phase"] = p.get("phase", "")
    elif kind == "finding" and p.get("finding_id"):
        state["findings"][p["finding_id"]] = p
    elif kind == "runtime_error":
        state["error"] = p["error"]
    if state["lifecycle"] in TERMINAL:
        state["interactions"] = {}
        for msg in state["messages"]:
            msg["complete"] = True
        for tool in state["tools"].values():
            if tool.get("state") == "tool_running":
                tool["state"] = "tool_interrupted"
        for actor in state["actors"].values():
            if actor.get("state") in {"active", "running", "queued"}:
                actor["state"] = state["lifecycle"]
    return state


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.root / "workspace.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS engagements(id TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, engagement_id TEXT,
            data TEXT NOT NULL, state TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT NOT NULL, data TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS event_sessions ON events(session_id,seq);
          CREATE TABLE IF NOT EXISTS commands(key TEXT PRIMARY KEY, data TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS templates(id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)
        self.db.commit()

    def work(self, eid: str) -> Path:
        self.engagement(eid)
        return self.root / "engagements" / eid / "work"

    def engagement(self, eid: str) -> dict:
        row = self.db.execute("SELECT data FROM engagements WHERE id=?", (eid,)).fetchone()
        if row is None:
            raise KeyError("Attack not found")
        return json.loads(row[0])

    def engagements(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM engagements")]

    def save_engagement(self, data: dict):
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO engagements VALUES (?,?)", (data["id"], json.dumps(data))
            )

    def create(self, name: str, target: str, context: str, *, config=None, draft="") -> dict:
        data = {
            "id": identity(),
            "name": name,
            "target": target,
            "archived": False,
            "created_at": time.time(),
            "context_revision": 1,
            "session_id": None,
            "config": config or {},
            "draft": draft,
            "view": "conversation",
        }
        self.save_engagement(data)
        work = self.work(data["id"])
        work.mkdir(parents=True, mode=0o700)
        (work / "pentest.md").write_text(context, encoding="utf-8", newline="")
        (work / "zero_files").mkdir()
        return data

    def delete_engagement(self, eid: str):
        """Remove the journal and idempotency responses together, after worker teardown."""
        prefixes = [eid + ":", *(s["id"] + ":" for s in self.sessions(eid))]
        with self.db:
            self.db.execute(
                "DELETE FROM events WHERE session_id IN "
                "(SELECT id FROM sessions WHERE engagement_id=?)", (eid,)
            )
            self.db.execute("DELETE FROM sessions WHERE engagement_id=?", (eid,))
            for prefix in prefixes:
                self.db.execute("DELETE FROM commands WHERE substr(key,1,?)=?", (len(prefix), prefix))
            self.db.execute("DELETE FROM engagements WHERE id=?", (eid,))

    def templates(self) -> list[dict]:
        return [json.loads(r[0]) for r in self.db.execute("SELECT data FROM templates ORDER BY rowid DESC")]

    def template(self, tid: str) -> dict:
        row = self.db.execute("SELECT data FROM templates WHERE id=?", (tid,)).fetchone()
        if row is None:
            raise KeyError("Template not found")
        return json.loads(row[0])

    def save_template(self, data: dict):
        with self.db:
            self.db.execute("INSERT INTO templates VALUES (?,?)", (data["id"], json.dumps(data)))

    def delete_template(self, tid: str):
        self.template(tid)
        with self.db:
            self.db.execute("DELETE FROM templates WHERE id=?", (tid,))

    def session(self, sid: str) -> dict:
        row = self.db.execute("SELECT data,state FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise KeyError("Session not found")
        return {**json.loads(row[0]), "snapshot": json.loads(row[1])}

    def sessions(self, eid: str) -> list[dict]:
        return [
            self.session(r[0])
            for r in self.db.execute(
                "SELECT id FROM sessions WHERE engagement_id=? ORDER BY rowid DESC", (eid,)
            )
        ]

    def create_session(self, eid: str, config: dict) -> dict:
        data = {
            "id": identity(),
            "engagement_id": eid,
            "config": config,
            "created_at": time.time(),
            "run_id": identity(),
            "history_path": None,
            "context_revision": self.engagement(eid)["context_revision"],
            "context": (self.work(eid) / "pentest.md").read_text(),
        }
        data["runs"] = [
            {
                "id": data["run_id"],
                "goal": config.get("goal"),
                "started_at": data["created_at"],
                "artifact_path": None,
            }
        ]
        state = blank_state()
        state["run_id"] = data["run_id"]
        with self.db:
            self.db.execute(
                "INSERT INTO sessions VALUES (?,?,?,?)",
                (data["id"], eid, json.dumps(data), json.dumps(state)),
            )
        engagement = self.engagement(eid)
        engagement["session_id"] = data["id"]
        self.save_engagement(engagement)
        return self.session(data["id"])

    def append(self, sid: str, payload: dict, *, message_id: str = "", run_id: str = "") -> dict:
        session = self.session(sid)
        event = {
            "schema_version": 1,
            "id": identity(),
            "session_id": sid,
            "engagement_id": session["engagement_id"],
            "run_id": run_id or session["snapshot"]["run_id"] or session["run_id"],
            "timestamp": time.time(),
            "kind": payload["kind"],
            "payload": payload,
            "actor_id": payload.get("actor_id")
            or payload.get("agent")
            or payload.get("label")
            or payload.get("subagent_id")
            or "orchestrator",
            "message_id": message_id,
        }
        if payload["kind"] == "user_text" and not any(
            r["id"] == event["run_id"] for r in session.get("runs", [])
        ):
            runs = session.get("runs", []) + [
                {
                    "id": event["run_id"],
                    "goal": payload.get("text"),
                    "started_at": event["timestamp"],
                    "artifact_path": None,
                }
            ]
            self.annotate_session(sid, {"runs": runs})
        with self.db:
            cur = self.db.execute("INSERT INTO events(session_id,data) VALUES (?,?)", (sid, "{}"))
            event["seq"] = cur.lastrowid
            state = project(session["snapshot"], event)
            self.db.execute(
                "UPDATE events SET data=? WHERE seq=?", (json.dumps(event), event["seq"])
            )
            self.db.execute("UPDATE sessions SET state=? WHERE id=?", (json.dumps(state), sid))
        return event

    def events(self, sid: str, after: int = 0, limit: int = 500) -> list[dict]:
        return [
            json.loads(r[0])
            for r in self.db.execute(
                "SELECT data FROM events WHERE session_id=? AND seq>? ORDER BY seq LIMIT ?",
                (sid, after, limit),
            )
        ]

    def command(self, key: str):
        row = self.db.execute("SELECT data FROM commands WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def remember(self, key: str, data: dict):
        with self.db:
            self.db.execute("INSERT INTO commands VALUES (?,?)", (key, json.dumps(data)))

    def annotate_session(self, sid: str, metadata: dict):
        session = self.session(sid)
        session.pop("snapshot")
        session.update(metadata)
        with self.db:
            self.db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), sid))

    def seal_path(self, sid: str, relative: str):
        session = self.session(sid)
        session.pop("snapshot")
        session["history_path"] = relative
        with self.db:
            self.db.execute("UPDATE sessions SET data=? WHERE id=?", (json.dumps(session), sid))
