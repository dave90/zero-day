"""Worker supervision. No orchestration or global tool state lives in the server."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path

from zero_web.storage import Store, TERMINAL, identity


async def terminate_tree(process):
    """Escalate only after cooperative shutdown of an owned worker times out."""
    if process.returncode is not None:
        return
    if os.name == "nt":
        # terminate()/kill() only stop the Python parent on Windows. taskkill's
        # tree option also stops model/tool children left behind by a stuck worker.
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(process.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await killer.wait()
        except OSError:
            pass
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
        await process.wait()
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), 3)
    except TimeoutError:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        await process.wait()


class Worker:
    def __init__(self, store: Store, sid: str, scripted: bool):
        self.store, self.sid, self.scripted = store, sid, scripted
        self.process = None
        self.pending = {}
        self.tasks = []
        self.closing = False
        self.write_lock = asyncio.Lock()
        self.startup = asyncio.Event()
        self.stderr_tail = ""

    async def start(self):
        session = self.store.session(self.sid)
        env = {
            k: v
            for k, v in os.environ.items()
            if k
            not in {"ZERO_FILES_ROOT", "ZERO_MEMORY_ROOT", "ZERO_TODO_ROOT", "ZERO_LLM_LOG_DIR"}
        }
        override = os.environ.get("ZERO_LLM_LOG_DIR")
        if override:
            log_root = (
                Path(override).expanduser().resolve()
                / "zero-studio"
                / session["engagement_id"]
                / self.sid
            )
            env["ZERO_LLM_LOG_DIR"] = str(log_root)
            self.store.annotate_session(self.sid, {"model_log_root": str(log_root)})
        package_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = (
            package_root + os.pathsep + str(Path(__file__).resolve().parents[3] / "src")
        )
        self.process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "zero_web.worker",
            cwd=self.store.work(session["engagement_id"]),
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=16 * 1024 * 1024,
            start_new_session=os.name != "nt",
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
        )
        self.tasks = [
            asyncio.create_task(self.read()),
            asyncio.create_task(self.errors()),
            asyncio.create_task(self.watch()),
        ]
        try:
            await self.command(
                "init",
                config=session["config"],
                scripted=self.scripted,
                replay_path=session.get("replay_path"),
                run_id=session["run_id"],
                timeout=90,
            )
        except BaseException:
            await self.close()
            raise

    async def command(self, action: str, body: dict | None = None, *, timeout=15, **top):
        if self.process is None or self.process.returncode is not None:
            raise ValueError("Runtime session is no longer active")
        cid = identity()
        future = asyncio.get_running_loop().create_future()
        self.pending[cid] = future
        try:
            async with self.write_lock:
                self.process.stdin.write(
                    (
                        json.dumps(
                            {
                                "version": 1,
                                "command_id": cid,
                                "action": action,
                                "body": body or {},
                                **top,
                            }
                        )
                        + "\n"
                    ).encode()
                )
                await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(cid, None)

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                data = json.loads(line)
                if data.get("version") != 1:
                    raise ValueError("Unsupported worker protocol")
                if data["type"] == "event":
                    if (
                        data["payload"].get("kind") == "runtime_state"
                        and data["payload"].get("lifecycle") == "idle"
                    ):
                        session = self.store.session(self.sid)
                        run_id = data.get("run_id") or session["snapshot"]["run_id"]
                        relative = await asyncio.to_thread(
                            self.seal_files,
                            session["engagement_id"],
                            session["context"],
                            session["config"]["files_root"],
                            run_id,
                        )
                        runs = session.get("runs", [])
                        for run in runs:
                            if run["id"] == run_id:
                                run["artifact_path"] = relative
                        self.store.annotate_session(self.sid, {"runs": runs})
                    self.store.append(
                        self.sid,
                        data["payload"],
                        message_id=data.get("message_id", ""),
                        run_id=data.get("run_id", ""),
                    )
                elif data["type"] == "ack":
                    future = self.pending.get(data["command_id"])
                    if future and not future.done():
                        if "error" in data:
                            future.set_exception(ValueError(data["error"]))
                        else:
                            future.set_result(data.get("result", {}))
        except Exception as exc:
            self.store.append(self.sid, {"kind": "runtime_error", "error": str(exc)})
            if self.process.returncode is None:
                self.process.terminate()

    async def errors(self):
        while chunk := await self.process.stderr.read(4096):
            # Keep bounded diagnostics in memory, never duplicate raw model logs.
            self.stderr_tail = (self.stderr_tail + chunk.decode(errors="replace"))[-8000:]

    async def watch(self):
        code = await self.process.wait()
        await self.tasks[0]
        for future in self.pending.values():
            if not future.done():
                future.set_exception(ValueError(f"Worker exited ({code})"))
        if not self.closing and code:
            self.store.append(
                self.sid, {"kind": "runtime_error", "error": f"Worker exited unexpectedly ({code})"}
            )
        self.store.append(
            self.sid,
            {
                "kind": "runtime_state",
                "activity": "idle",
                "lifecycle": "closed" if self.closing or code == 0 else "interrupted",
            },
        )
        session = self.store.session(self.sid)
        relative = await asyncio.to_thread(
            self.seal_files,
            session["engagement_id"],
            session["context"],
            session["config"]["files_root"],
        )
        self.store.seal_path(self.sid, relative)

    def seal_files(self, eid, context, files_root="zero_files", goal_id=None):
        # Only file copying leaves the event loop; SQLite stays on its owner thread.
        work = self.store.root / "engagements" / eid / "work"
        destination = work.parent / "history" / self.sid
        if goal_id:
            destination = destination / "goals" / goal_id
        destination.mkdir(parents=True, exist_ok=True)
        for name in (".pentest", ".ralph", ".ralph_logs", ".recordings", "pentest.md", files_root):
            source = work / name
            if source.is_symlink() or not source.exists():
                continue
            if source.is_dir():
                shutil.copytree(source, destination / name, symlinks=True, dirs_exist_ok=True)
            else:
                shutil.copy2(source, destination / name)
        override = os.environ.get("ZERO_LLM_LOG_DIR")
        if override:
            log_root = Path(override).expanduser().resolve() / "zero-studio" / eid / self.sid
            if log_root.is_dir() and not log_root.is_symlink():
                shutil.copytree(
                    log_root, destination / ".pentest/llm", symlinks=True, dirs_exist_ok=True
                )
        (destination / "pentest.md").write_text(context, encoding="utf-8", newline="")
        return destination.relative_to(self.store.root).as_posix()

    async def close(self):
        self.closing = True
        self.store.append(self.sid, {"kind": "runtime_state", "lifecycle": "stopping"})
        if self.process is not None and self.process.returncode is None:
            try:
                await self.command("close", timeout=2)
                self.process.stdin.close()
                await asyncio.wait_for(asyncio.shield(self.tasks[2]), timeout=18)
            except (TimeoutError, ValueError, BrokenPipeError):
                await terminate_tree(self.process)
        if self.tasks:
            await asyncio.gather(*self.tasks, return_exceptions=True)


class Manager:
    def __init__(self, store: Store, scripted: bool = False):
        self.store, self.scripted = store, scripted
        self.workers: dict[str, Worker] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        for engagement in store.engagements():
            for session in store.sessions(engagement["id"]):
                if session["snapshot"]["lifecycle"] not in TERMINAL:
                    store.append(
                        session["id"],
                        {"kind": "runtime_state", "lifecycle": "interrupted", "activity": "idle"},
                    )

    def lock(self, eid: str):
        return self.locks.setdefault(eid, asyncio.Lock())

    async def start(self, session: dict):
        worker = Worker(self.store, session["id"], self.scripted)
        self.workers[session["id"]] = worker
        await worker.start()
        return worker

    def worker(self, sid: str):
        worker = self.workers.get(sid)
        if worker is None or worker.process.returncode is not None:
            raise ValueError("Session expired; start a new session or resume Ralph")
        return worker

    async def close(self):
        await asyncio.gather(
            *(
                w.close()
                for w in self.workers.values()
                if w.process and w.process.returncode is None
            ),
            return_exceptions=True,
        )
