"""Transactional SQLite coordination store for a live swarm.

The store deliberately exposes small synchronous operations.  Each mutation is a
short transaction protected by the process writer lock; SQLite still provides the
cross-process integrity needed for inspection and later recovery.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from zero.swarm.models import SCHEMA_VERSION, EvidenceRecord, WorkClaim


_SPACE = re.compile(r"\s+")


def normalized_work_key(kind: str, target: str, hypothesis: str) -> str:
    raw = "|".join(_SPACE.sub(" ", v.strip().lower()) for v in (kind, target, hypothesis))
    return hashlib.sha256(raw.encode()).hexdigest()


class SwarmStore:
    """Authoritative coordination state with ordered, transactional events."""

    def __init__(
        self,
        path: Path,
        *,
        engagement_id: str | None = None,
        swarm_id: str | None = None,
        clock=time.time,
    ) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engagement_id = engagement_id or ""
        self.swarm_id = swarm_id or ""
        self._clock = clock
        self._lock = threading.RLock()
        self._db = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.execute("PRAGMA busy_timeout=5000")
        self._migrate()
        # An existing database is a recovery store, not an invitation to create a
        # second logical swarm over the same worker/message tables.
        if not self.swarm_id:
            prior = self._db.execute(
                "SELECT swarm_id,engagement_id FROM swarm_runs ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if prior is not None:
                self.swarm_id = prior["swarm_id"]
                self.engagement_id = prior["engagement_id"]
        self.engagement_id = self.engagement_id or uuid.uuid4().hex[:12]
        self.swarm_id = self.swarm_id or uuid.uuid4().hex[:10]
        self._ensure_run()
        self._recover_interrupted_validations()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta(version INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS swarm_runs(
                    swarm_id TEXT PRIMARY KEY, engagement_id TEXT NOT NULL,
                    state TEXT NOT NULL, schema_version INTEGER NOT NULL,
                    created_at REAL NOT NULL, stop_intent INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS workers(
                    worker_id TEXT PRIMARY KEY, actor_id TEXT UNIQUE NOT NULL,
                    name TEXT NOT NULL, generation INTEGER NOT NULL,
                    lane TEXT NOT NULL DEFAULT '', state TEXT NOT NULL,
                    heartbeat REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS groups(
                    group_id TEXT PRIMARY KEY, name TEXT NOT NULL, topic TEXT NOT NULL,
                    creator_id TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS group_members(
                    group_id TEXT NOT NULL REFERENCES groups(group_id),
                    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
                    joined_at REAL NOT NULL, left_at REAL,
                    PRIMARY KEY(group_id, worker_id, joined_at)
                );
                CREATE TABLE IF NOT EXISTS work_items(
                    work_id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL,
                    target TEXT NOT NULL, hypothesis TEXT NOT NULL,
                    normalized_work_key TEXT UNIQUE NOT NULL, state TEXT NOT NULL,
                    priority INTEGER NOT NULL, creator_id TEXT NOT NULL,
                    owner_worker_id TEXT, lease_token TEXT, lease_expires_at REAL,
                    version INTEGER NOT NULL DEFAULT 1, outcome TEXT NOT NULL DEFAULT '',
                    group_id TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS work_dependencies(
                    work_id TEXT NOT NULL REFERENCES work_items(work_id),
                    dependency_id TEXT NOT NULL REFERENCES work_items(work_id),
                    PRIMARY KEY(work_id, dependency_id)
                );
                CREATE TABLE IF NOT EXISTS messages(
                    message_id TEXT PRIMARY KEY, sender_id TEXT NOT NULL,
                    sender_name TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,
                    addressing TEXT NOT NULL, group_id TEXT, work_id TEXT,
                    evidence_ids TEXT NOT NULL, sequence INTEGER NOT NULL UNIQUE,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_recipients(
                    message_id TEXT NOT NULL REFERENCES messages(message_id),
                    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
                    delivered_at REAL, presented_at REAL, acknowledged_at REAL,
                    PRIMARY KEY(message_id, worker_id)
                );
                CREATE TABLE IF NOT EXISTS evidence(
                    evidence_id TEXT PRIMARY KEY, producer_id TEXT NOT NULL,
                    work_id TEXT, kind TEXT NOT NULL, target TEXT NOT NULL,
                    summary TEXT NOT NULL, content TEXT NOT NULL,
                    content_hash TEXT UNIQUE NOT NULL, visibility TEXT NOT NULL,
                    group_id TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS response_cycles(
                    cycle_id TEXT PRIMARY KEY, user_message TEXT NOT NULL,
                    state TEXT NOT NULL, expected_workers TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS contributions(
                    cycle_id TEXT NOT NULL REFERENCES response_cycles(cycle_id),
                    worker_id TEXT NOT NULL REFERENCES workers(worker_id),
                    status TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL,
                    PRIMARY KEY(cycle_id, worker_id)
                );
                CREATE TABLE IF NOT EXISTS finding_candidates(
                    candidate_id TEXT PRIMARY KEY, dedup_hash TEXT UNIQUE NOT NULL,
                    candidate_json TEXT NOT NULL, state TEXT NOT NULL,
                    verdict TEXT, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_sources(
                    candidate_id TEXT NOT NULL REFERENCES finding_candidates(candidate_id),
                    worker_id TEXT NOT NULL, evidence_ids TEXT NOT NULL,
                    PRIMARY KEY(candidate_id, worker_id)
                );
                CREATE TABLE IF NOT EXISTS validation_attempts(
                    attempt_id TEXT PRIMARY KEY, candidate_id TEXT NOT NULL
                      REFERENCES finding_candidates(candidate_id),
                    validator_id TEXT NOT NULL UNIQUE, state TEXT NOT NULL,
                    verdict TEXT, created_at REAL NOT NULL, completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS approvals(
                    approval_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
                    tool TEXT NOT NULL, risk_class TEXT NOT NULL,
                    execution_location TEXT NOT NULL, decision TEXT NOT NULL,
                    state TEXT NOT NULL, created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS interactions(
                    interaction_id TEXT PRIMARY KEY, actor_id TEXT NOT NULL,
                    kind TEXT NOT NULL, state TEXT NOT NULL, prompt TEXT NOT NULL,
                    answer TEXT, created_at REAL NOT NULL, answered_at REAL
                );
                CREATE TABLE IF NOT EXISTS events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT UNIQUE NOT NULL, kind TEXT NOT NULL,
                    actor_id TEXT, payload TEXT NOT NULL, created_at REAL NOT NULL
                );
                """
            )
            row = self._db.execute("SELECT version FROM schema_meta LIMIT 1").fetchone()
            if row is None:
                self._db.execute("INSERT INTO schema_meta(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] != SCHEMA_VERSION:
                raise RuntimeError(
                    f"unsupported swarm schema {row['version']}; expected {SCHEMA_VERSION}"
                )

    def _ensure_run(self) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR IGNORE INTO swarm_runs VALUES (?, ?, 'idle', ?, ?, 0)",
                (self.swarm_id, self.engagement_id, SCHEMA_VERSION, now),
            )

    def _recover_interrupted_validations(self) -> None:
        """Make non-terminal candidates retryable after a process restart.

        A running validator lives only in the host process. If this database is
        being opened again, every persisted ``running`` attempt is necessarily
        stale; retaining it would make the candidate's dedup row suppress all
        future validation attempts forever.
        """
        with self._lock, self._db:
            now = self._clock()
            self._db.execute(
                """UPDATE validation_attempts SET state='interrupted',completed_at=?
                   WHERE state='running'""",
                (now,),
            )
            self._db.execute(
                """UPDATE finding_candidates SET state='retryable'
                   WHERE state!='terminal'"""
            )

    def _event(self, kind: str, actor_id: str | None, payload: dict[str, Any]) -> int:
        cur = self._db.execute(
            "INSERT INTO events(event_id,kind,actor_id,payload,created_at) VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, kind, actor_id, json.dumps(payload, sort_keys=True), self._clock()),
        )
        return int(cur.lastrowid)

    def _actor_for(self, worker_id: str) -> str:
        row = self._db.execute(
            "SELECT actor_id FROM workers WHERE worker_id=?", (worker_id,)
        ).fetchone()
        return row["actor_id"] if row is not None else worker_id

    def events(self, *, after: int = 0) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM events WHERE sequence>? ORDER BY sequence", (after,)
        ).fetchall()
        return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def register_worker(
        self, worker_id: str, actor_id: str, name: str, *, generation: int = 1, lane: str = ""
    ) -> None:
        now = self._clock()
        with self._lock, self._db:
            self._db.execute(
                """INSERT INTO workers(worker_id,actor_id,name,generation,lane,state,heartbeat)
                   VALUES(?,?,?,?,?,'idle',?)
                   ON CONFLICT(worker_id) DO UPDATE SET actor_id=excluded.actor_id,
                     name=excluded.name,generation=excluded.generation,lane=excluded.lane,
                     state='idle',heartbeat=excluded.heartbeat""",
                (worker_id, actor_id, name, generation, lane, now),
            )
            self._event("worker_registered", actor_id, {"worker_id": worker_id, "lane": lane})

    def retire_workers_except(self, active_worker_ids: Iterable[str]) -> None:
        """Retire workers outside the current fixed-size membership.

        Worker rows are retained because historical messages and contributions
        reference them. Active work owned by a retired worker is released so a
        smaller restarted swarm does not wait for a dead peer's lease to expire.
        """
        active = sorted(set(active_worker_ids))
        with self._lock, self._db:
            if active:
                marks = ",".join("?" * len(active))
                retired = [
                    row["worker_id"] for row in self._db.execute(
                        f"SELECT worker_id FROM workers WHERE worker_id NOT IN ({marks}) "
                        "AND state!='retired'",
                        active,
                    )
                ]
                self._db.execute(
                    f"UPDATE workers SET state='retired',heartbeat=? "
                    f"WHERE worker_id NOT IN ({marks})",
                    (self._clock(), *active),
                )
            else:
                retired = [
                    row["worker_id"] for row in self._db.execute(
                        "SELECT worker_id FROM workers WHERE state!='retired'"
                    )
                ]
                self._db.execute(
                    "UPDATE workers SET state='retired',heartbeat=?", (self._clock(),)
                )
            if retired:
                marks = ",".join("?" * len(retired))
                self._db.execute(
                    f"""UPDATE work_items SET state='ready',owner_worker_id=NULL,
                       lease_token=NULL,lease_expires_at=NULL,version=version+1,updated_at=?
                       WHERE owner_worker_id IN ({marks}) AND state IN ('claimed','running')""",
                    (self._clock(), *retired),
                )
                for worker_id in retired:
                    self._event("worker_retired", self._actor_for(worker_id), {
                        "worker_id": worker_id,
                    })

    def set_worker_state(self, worker_id: str, state: str) -> None:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT actor_id FROM workers WHERE worker_id=?", (worker_id,)
            ).fetchone()
            if row is None:
                raise KeyError(worker_id)
            self._db.execute(
                "UPDATE workers SET state=?, heartbeat=? WHERE worker_id=?",
                (state, self._clock(), worker_id),
            )
            self._event("worker_state", row["actor_id"], {"worker_id": worker_id, "state": state})

    def workers(self, *, include_retired: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM workers"
        if not include_retired:
            sql += " WHERE state!='retired'"
        sql += " ORDER BY worker_id"
        return [dict(r) for r in self._db.execute(sql)]

    def create_group(
        self, creator_id: str, name: str, topic: str, member_ids: Iterable[str]
    ) -> str:
        members = sorted(set(member_ids) | {creator_id})
        known = {
            r[0] for r in self._db.execute(
                f"SELECT worker_id FROM workers WHERE state!='retired' "
                f"AND worker_id IN ({','.join('?' * len(members))})",
                members,
            )
        } if members else set()
        if known != set(members):
            raise ValueError("group members must be existing top-level workers")
        gid, now = "grp_" + uuid.uuid4().hex[:10], self._clock()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO groups VALUES(?,?,?,?, 'open', ?)",
                (gid, name, topic, creator_id, now),
            )
            self._db.executemany(
                "INSERT INTO group_members(group_id,worker_id,joined_at) VALUES(?,?,?)",
                [(gid, m, now) for m in members],
            )
            self._event(
                "group_created", self._actor_for(creator_id),
                {"group_id": gid, "members": members},
            )
        return gid

    def group_members(self, group_id: str, *, active_only: bool = False) -> list[str]:
        active_clause = " AND w.state!='retired'" if active_only else ""
        return [
            r[0] for r in self._db.execute(
                "SELECT gm.worker_id FROM group_members gm JOIN workers w USING(worker_id) "
                f"WHERE gm.group_id=? AND gm.left_at IS NULL{active_clause}",
                (group_id,),
            )
        ]

    def update_group(
        self, actor_id: str, group_id: str, *, member_ids: Iterable[str] | None = None,
        close: bool = False,
    ) -> None:
        now = self._clock()
        with self._lock, self._db:
            group = self._db.execute(
                "SELECT state FROM groups WHERE group_id=?", (group_id,)
            ).fetchone()
            if group is None or group["state"] != "open":
                raise ValueError("group is missing or closed")
            if close:
                self._db.execute("UPDATE groups SET state='closed' WHERE group_id=?", (group_id,))
            elif member_ids is not None:
                wanted = set(member_ids)
                known = {
                    r[0] for r in self._db.execute(
                        "SELECT worker_id FROM workers WHERE state!='retired'"
                    )
                }
                if not wanted <= known:
                    raise ValueError("group members must be existing top-level workers")
                current = set(self.group_members(group_id))
                for worker_id in current - wanted:
                    self._db.execute(
                        "UPDATE group_members SET left_at=? WHERE group_id=? AND worker_id=? AND left_at IS NULL",
                        (now, group_id, worker_id),
                    )
                self._db.executemany(
                    "INSERT INTO group_members(group_id,worker_id,joined_at) VALUES(?,?,?)",
                    [(group_id, worker_id, now) for worker_id in wanted - current],
                )
            self._event(
                "group_updated", self._actor_for(actor_id),
                {"group_id": group_id, "closed": close, "members": sorted(member_ids or [])},
            )

    def groups(self) -> list[dict[str, Any]]:
        out = []
        for row in self._db.execute("SELECT * FROM groups ORDER BY created_at"):
            item = dict(row)
            item["members"] = self.group_members(row["group_id"])
            out.append(item)
        return out

    def send_message(
        self,
        sender_id: str,
        *,
        body: str,
        kind: str = "intel",
        to: str = "broadcast",
        group_id: str | None = None,
        work_id: str | None = None,
        evidence_ids: Iterable[str] = (),
    ) -> str:
        sender = self._db.execute(
            "SELECT name,actor_id FROM workers WHERE worker_id=?", (sender_id,)
        ).fetchone()
        if sender is None:
            raise KeyError(sender_id)
        if to == "broadcast":
            recipients = [r[0] for r in self._db.execute(
                "SELECT worker_id FROM workers WHERE worker_id<>? AND state!='retired' "
                "ORDER BY worker_id", (sender_id,)
            )]
        elif to == "group":
            if not group_id:
                raise ValueError("group_id is required for group delivery")
            recipients = [
                w for w in self.group_members(group_id, active_only=True) if w != sender_id
            ]
        else:
            recipients = [to]
            if self._db.execute(
                "SELECT 1 FROM workers WHERE worker_id=? AND state!='retired'", (to,)
            ).fetchone() is None:
                raise ValueError(f"unknown recipient {to!r}")
        mid, now = "msg_" + uuid.uuid4().hex[:12], self._clock()
        with self._lock, self._db:
            seq = self._event("message_accepted", sender["actor_id"], {"message_id": mid})
            self._db.execute(
                """INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (mid, sender_id, sender["name"], kind, body, to, group_id, work_id,
                 json.dumps(sorted(set(evidence_ids))), seq, now),
            )
            self._db.executemany(
                "INSERT INTO message_recipients(message_id,worker_id) VALUES(?,?)",
                [(mid, r) for r in recipients],
            )
        return mid

    def inbox(self, worker_id: str, *, present: bool = True) -> list[dict[str, Any]]:
        now = self._clock()
        with self._lock, self._db:
            rows = self._db.execute(
                """SELECT m.* FROM messages m JOIN message_recipients r USING(message_id)
                   WHERE r.worker_id=? AND r.acknowledged_at IS NULL ORDER BY m.sequence""",
                (worker_id,),
            ).fetchall()
            if rows:
                ids = [r["message_id"] for r in rows]
                marks = ",".join("?" * len(ids))
                self._db.execute(
                    f"UPDATE message_recipients SET delivered_at=COALESCE(delivered_at,?)"
                    f" WHERE worker_id=? AND message_id IN ({marks})",
                    (now, worker_id, *ids),
                )
                if present:
                    self._db.execute(
                        f"UPDATE message_recipients SET presented_at=COALESCE(presented_at,?)"
                        f" WHERE worker_id=? AND message_id IN ({marks})",
                        (now, worker_id, *ids),
                    )
            return [{**dict(r), "evidence_ids": json.loads(r["evidence_ids"])} for r in rows]

    def acknowledge(self, worker_id: str, message_ids: Iterable[str]) -> None:
        ids = list(dict.fromkeys(message_ids))
        if not ids:
            return
        with self._lock, self._db:
            marks = ",".join("?" * len(ids))
            self._db.execute(
                f"UPDATE message_recipients SET acknowledged_at=COALESCE(acknowledged_at,?)"
                f" WHERE worker_id=? AND message_id IN ({marks})",
                (self._clock(), worker_id, *ids),
            )

    def messages(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM messages ORDER BY sequence DESC LIMIT ?", (limit,)
        ).fetchall()
        return [{**dict(row), "evidence_ids": json.loads(row["evidence_ids"])} for row in rows]

    def create_work(
        self, creator_id: str, *, kind: str, title: str, target: str, hypothesis: str,
        priority: int = 0, dependency_ids: Iterable[str] = (), group_id: str | None = None,
    ) -> tuple[str, bool]:
        key, now = normalized_work_key(kind, target, hypothesis), self._clock()
        wid = "work_" + uuid.uuid4().hex[:12]
        deps = list(dict.fromkeys(dependency_ids))
        state = "blocked" if deps else "ready"
        with self._lock, self._db:
            existing = self._db.execute(
                "SELECT work_id FROM work_items WHERE normalized_work_key=?", (key,)
            ).fetchone()
            if existing:
                return existing["work_id"], False
            self._db.execute(
                """INSERT INTO work_items(work_id,kind,title,target,hypothesis,
                   normalized_work_key,state,priority,creator_id,group_id,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (wid, kind, title, target, hypothesis, key, state, priority, creator_id,
                 group_id, now, now),
            )
            self._db.executemany(
                "INSERT INTO work_dependencies VALUES(?,?)", [(wid, d) for d in deps]
            )
            self._event(
                "work_created", self._actor_for(creator_id),
                {"work_id": wid, "state": state},
            )
        return wid, True

    def _unblock_work(self) -> None:
        self._db.execute(
            """UPDATE work_items SET state='ready',updated_at=? WHERE state='blocked'
               AND NOT EXISTS (
                 SELECT 1 FROM work_dependencies d JOIN work_items p
                 ON p.work_id=d.dependency_id WHERE d.work_id=work_items.work_id
                 AND p.state!='completed')""",
            (self._clock(),),
        )

    def list_work(self, *, state: str | None = None, owner: str | None = None) -> list[dict[str, Any]]:
        with self._lock, self._db:
            self._unblock_work()
        where, args = [], []
        if state:
            where.append("state=?")
            args.append(state)
        if owner:
            where.append("owner_worker_id=?")
            args.append(owner)
        sql = "SELECT * FROM work_items" + (" WHERE " + " AND ".join(where) if where else "")
        sql += " ORDER BY priority DESC,created_at"
        return [dict(r) for r in self._db.execute(sql, args)]

    def claim_work(self, worker_id: str, work_id: str, *, lease_seconds: float = 300) -> WorkClaim | None:
        now, token = self._clock(), uuid.uuid4().hex
        with self._lock, self._db:
            self._unblock_work()
            cur = self._db.execute(
                """UPDATE work_items SET state='claimed',owner_worker_id=?,lease_token=?,
                   lease_expires_at=?,version=version+1,updated_at=? WHERE work_id=?
                   AND (state='ready' OR (state IN ('claimed','running') AND lease_expires_at<?))""",
                (worker_id, token, now + lease_seconds, now, work_id, now),
            )
            if cur.rowcount != 1:
                return None
            self._event("work_claimed", self._actor_for(worker_id), {"work_id": work_id})
        return WorkClaim(work_id, token, now + lease_seconds)

    def update_work(
        self, worker_id: str, work_id: str, lease_token: str, *, action: str,
        outcome: str = "", lease_seconds: float = 300,
    ) -> bool:
        states = {"start": "running", "renew": None, "release": "ready",
                  "block": "blocked", "complete": "completed", "fail": "failed",
                  "cancel": "cancelled"}
        if action not in states:
            raise ValueError(f"unknown work action {action!r}")
        now, state = self._clock(), states[action]
        if action == "renew":
            sql = "UPDATE work_items SET lease_expires_at=?,updated_at=? WHERE work_id=? AND owner_worker_id=? AND lease_token=?"
            vals = (now + lease_seconds, now, work_id, worker_id, lease_token)
        elif action == "release":
            sql = "UPDATE work_items SET state=?,owner_worker_id=NULL,lease_token=NULL,lease_expires_at=NULL,outcome=?,version=version+1,updated_at=? WHERE work_id=? AND owner_worker_id=? AND lease_token=?"
            vals = (state, outcome, now, work_id, worker_id, lease_token)
        else:
            sql = "UPDATE work_items SET state=?,outcome=?,version=version+1,updated_at=? WHERE work_id=? AND owner_worker_id=? AND lease_token=?"
            vals = (state, outcome, now, work_id, worker_id, lease_token)
        with self._lock, self._db:
            cur = self._db.execute(sql, vals)
            if cur.rowcount:
                self._event(
                    "work_updated", self._actor_for(worker_id),
                    {"work_id": work_id, "action": action},
                )
                if action == "complete":
                    self._unblock_work()
            return cur.rowcount == 1

    def publish_evidence(
        self, producer_id: str, *, kind: str, target: str, summary: str, content: str,
        visibility: str = "swarm", work_id: str | None = None, group_id: str | None = None,
    ) -> EvidenceRecord:
        if visibility not in {"private", "group", "swarm", "validator"}:
            raise ValueError("invalid evidence visibility")
        digest = hashlib.sha256(content.encode()).hexdigest()
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT * FROM evidence WHERE content_hash=?", (digest,)
            ).fetchone()
            if row is None:
                eid = "ev_" + digest[:16]
                self._db.execute(
                    "INSERT INTO evidence VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (eid, producer_id, work_id, kind, target, summary[:2000], content[:100_000],
                     digest, visibility, group_id, self._clock()),
                )
                self._event(
                    "evidence_published", self._actor_for(producer_id),
                    {"evidence_id": eid, "work_id": work_id},
                )
                row = self._db.execute("SELECT * FROM evidence WHERE evidence_id=?", (eid,)).fetchone()
        return EvidenceRecord(
            evidence_id=row["evidence_id"], producer_id=row["producer_id"], kind=row["kind"],
            target=row["target"], summary=row["summary"], content=row["content"],
            content_hash=row["content_hash"], visibility=row["visibility"], work_id=row["work_id"],
        )

    def query_evidence(
        self, viewer_id: str, *, evidence_id: str | None = None, work_id: str | None = None
    ) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT * FROM evidence WHERE (? IS NULL OR evidence_id=?) AND (? IS NULL OR work_id=?) ORDER BY created_at",
            (evidence_id, evidence_id, work_id, work_id),
        ).fetchall()
        visible = []
        for r in rows:
            if r["visibility"] in {"swarm", "validator"} or r["producer_id"] == viewer_id:
                visible.append(dict(r))
            elif r["visibility"] == "group" and r["group_id"] in {
                g[0] for g in self._db.execute(
                    "SELECT group_id FROM group_members WHERE worker_id=? AND left_at IS NULL",
                    (viewer_id,),
                )
            }:
                visible.append(dict(r))
        return visible

    def start_cycle(self, user_message: str, expected_workers: Iterable[str]) -> str:
        cid = "cycle_" + uuid.uuid4().hex[:12]
        expected = sorted(set(expected_workers))
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO response_cycles VALUES(?,?, 'collecting', ?, '', ?, NULL)",
                (cid, user_message, json.dumps(expected), self._clock()),
            )
            self._event("cycle_started", None, {"cycle_id": cid, "workers": expected})
        return cid

    def contribute(self, cycle_id: str, worker_id: str, body: str, *, status: str = "complete") -> None:
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO contributions VALUES(?,?,?,?,?)",
                (cycle_id, worker_id, status, body, self._clock()),
            )
            self._event(
                "contribution", self._actor_for(worker_id),
                {"cycle_id": cycle_id, "status": status},
            )

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        row = self._db.execute("SELECT * FROM response_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise KeyError(cycle_id)
        out = dict(row)
        out["expected_workers"] = json.loads(out["expected_workers"])
        out["contributions"] = [dict(r) for r in self._db.execute(
            "SELECT * FROM contributions WHERE cycle_id=? ORDER BY worker_id", (cycle_id,)
        )]
        return out

    def complete_cycle(self, cycle_id: str, summary: str) -> None:
        with self._lock, self._db:
            self._db.execute(
                "UPDATE response_cycles SET state='completed',summary=?,completed_at=? WHERE cycle_id=?",
                (summary, self._clock(), cycle_id),
            )
            self._event("cycle_completed", None, {"cycle_id": cycle_id})

    def reserve_candidate(
        self, dedup_hash: str, candidate: dict[str, Any], worker_id: str,
        evidence_ids: Iterable[str] = (),
    ) -> tuple[str, bool]:
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT candidate_id FROM finding_candidates WHERE dedup_hash=?", (dedup_hash,)
            ).fetchone()
            acquired = row is None
            cid = "cand_" + uuid.uuid4().hex[:12] if acquired else row["candidate_id"]
            if acquired:
                self._db.execute(
                    "INSERT INTO finding_candidates VALUES(?,?,?,'reserved',NULL,?)",
                    (cid, dedup_hash, json.dumps(candidate, sort_keys=True), self._clock()),
                )
            else:
                cur = self._db.execute(
                    """UPDATE finding_candidates SET state='reserved',verdict=NULL
                       WHERE candidate_id=? AND state='retryable'""",
                    (cid,),
                )
                acquired = cur.rowcount == 1
            self._db.execute(
                "INSERT OR REPLACE INTO candidate_sources VALUES(?,?,?)",
                (cid, worker_id, json.dumps(sorted(set(evidence_ids)))),
            )
            self._event(
                "candidate_reserved", self._actor_for(worker_id),
                {"candidate_id": cid, "acquired": acquired},
            )
        return cid, acquired

    def commit_candidate_verdict(self, candidate_id: str, verdict: str, validator_id: str) -> bool:
        if verdict not in {"confirmed", "rejected", "pending", "duplicate"}:
            raise ValueError(f"invalid validator verdict {verdict!r}")
        with self._lock, self._db:
            cur = self._db.execute(
                """UPDATE finding_candidates SET state='terminal',verdict=?
                   WHERE candidate_id=? AND state!='terminal'""",
                (verdict, candidate_id),
            )
            if cur.rowcount:
                self._db.execute(
                    """UPDATE validation_attempts SET state='terminal',verdict=?,completed_at=?
                       WHERE candidate_id=? AND validator_id=? AND state!='terminal'""",
                    (verdict, self._clock(), candidate_id, validator_id),
                )
                self._event(
                    "candidate_verdict", validator_id,
                    {"candidate_id": candidate_id, "verdict": verdict},
                )
            return cur.rowcount == 1

    def start_validation(self, candidate_id: str, validator_id: str) -> str:
        attempt_id = "val_" + uuid.uuid4().hex[:12]
        with self._lock, self._db:
            cur = self._db.execute(
                """UPDATE finding_candidates SET state='validating'
                   WHERE candidate_id=? AND state='reserved'""",
                (candidate_id,),
            )
            if cur.rowcount != 1:
                raise ValueError("candidate is not reserved for validation")
            self._db.execute(
                "INSERT INTO validation_attempts VALUES(?,?,?,'running',NULL,?,NULL)",
                (attempt_id, candidate_id, validator_id, self._clock()),
            )
            self._event(
                "validation_started", validator_id,
                {"candidate_id": candidate_id, "attempt_id": attempt_id},
            )
        return attempt_id

    def abandon_validation(
        self, candidate_id: str, validator_id: str, *, reason: str = "interrupted"
    ) -> bool:
        """Release a non-terminal candidate whose in-process validator stopped."""
        with self._lock, self._db:
            attempt = self._db.execute(
                """UPDATE validation_attempts SET state='interrupted',verdict=?,completed_at=?
                   WHERE candidate_id=? AND validator_id=? AND state='running'""",
                (reason, self._clock(), candidate_id, validator_id),
            )
            if attempt.rowcount != 1:
                return False
            self._db.execute(
                """UPDATE finding_candidates SET state='retryable',verdict=NULL
                   WHERE candidate_id=? AND state!='terminal'""",
                (candidate_id,),
            )
            self._event(
                "validation_interrupted", validator_id,
                {"candidate_id": candidate_id, "reason": reason},
            )
            return True

    def candidates(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self._db.execute(
            "SELECT * FROM finding_candidates ORDER BY created_at"
        )]
