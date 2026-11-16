"""Loopback-only HTTP/SSE service. The browser never executes CLI command text."""

from __future__ import annotations

import argparse
import base64
import binascii
import asyncio
import json
import os
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import FileResponse, JSONResponse, StreamingResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

from zero.cli import DEFAULT_PENTEST_MD
from zero_web.artifacts import MAX_PREVIEW, artifact, contained, findings, inventory, logs, swarm
from zero_web.config import RunConfig, SCHEMA, command_for, relative_name, validate_config
from zero_web.sessions import Manager, Worker
from zero_web.models import CodexCatalog
from zero_web.storage import Store, TERMINAL, identity

API = "/api/v1"


class LocalBoundary(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return JSONResponse({"error": "Only loopback hosts are accepted"}, 403)
        origin = request.headers.get("origin")
        allowed = {f"{request.url.scheme}://{request.url.netloc}", *request.app.state.origins}
        if origin and origin not in allowed:
            return JSONResponse({"error": "Cross-origin requests are not permitted"}, 403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            token = request.app.state.csrf
            if not (
                secrets.compare_digest(request.headers.get("x-zero-csrf", ""), token)
                and secrets.compare_digest(request.cookies.get("zero_local", ""), token)
            ):
                return JSONResponse({"error": "Refresh Studio to renew the local session"}, 403)
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 6_000_000:
                return JSONResponse({"error": "Import exceeds the 6 MB request limit"}, 413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Cache-Control"] = (
            "no-store" if request.url.path.startswith(API) else "no-cache"
        )
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; object-src 'none'"
        )
        return response


async def body(request):
    raw = await request.body()
    if len(raw) > 6_000_000:
        raise ValueError("Request exceeds import limit")
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("Expected a JSON object")
    return data


def text_value(data, name, maximum=100_000, required=True):
    value = data.get(name, "")
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise ValueError(f"Invalid {name}")
    return value


async def health(request):
    response = JSONResponse(
        {
            "status": "ready",
            "schema_version": 1,
            "csrf_token": request.app.state.csrf,
            "scripted": request.app.state.manager.scripted,
            "backends": {name: bool(shutil.which(name)) for name in ("codex", "claude")},
            "docker": bool(shutil.which("docker")),
            "template": DEFAULT_PENTEST_MD,
            "operations": ["sessions", "approvals", "questions", "ralph", "swarm", "artifacts"],
        }
    )
    response.set_cookie("zero_local", request.app.state.csrf, httponly=True, samesite="strict")
    return response


async def schema(request):
    return JSONResponse({"version": 1, "fields": SCHEMA, "defaults": RunConfig().model_dump()})


async def models(request):
    if request.query_params.get("backend", "codex") != "codex":
        raise ValueError("Automatic model discovery is available for the Codex backend")
    refresh = request.query_params.get("refresh", "").lower() in {"1", "true"}
    return JSONResponse(await request.app.state.model_catalog.get(refresh=refresh))


async def configuration(request):
    cfg = validate_config(await body(request))
    return JSONResponse({"config": cfg, "command": command_for(cfg)})


async def engagements(request):
    store = request.app.state.store
    if request.method == "GET":
        data = store.engagements()
        for row in data:
            if row["session_id"]:
                state = store.session(row["session_id"])["snapshot"]
                row["state"] = {k: state[k] for k in ("lifecycle", "activity", "findings")}
                row["state"]["interactions"] = {
                    rid: {"kind": p["kind"]} for rid, p in state["interactions"].items()
                }
            else:
                row["state"] = None
        return JSONResponse(data)
    data = await body(request)
    template = store.template(text_value(data, "template_id", 100)) if data.get("template_id") else {}
    name = text_value(data, "name", 200)
    target = text_value(data, "target", 2000)
    if urlsplit(target).scheme not in {"http", "https"} or not urlsplit(target).netloc:
        raise ValueError("Target must be an HTTP or HTTPS URL")
    context = data.get("context", template.get("context") or DEFAULT_PENTEST_MD)
    if not isinstance(context, str) or len(context.encode()) > MAX_PREVIEW:
        raise ValueError("Scope context exceeds 1 MB")
    raw_config = data.get("config", template.get("config"))
    cfg = validate_config(raw_config) if raw_config is not None else {}
    draft = text_value(data, "draft", required=False) if "draft" in data else cfg.get("goal") or ""
    row = store.create(name, target, context, config=cfg, draft=draft)
    return JSONResponse(row, 201)


async def engagement(request):
    if request.method == "DELETE":
        return await delete_engagement(request)
    async with request.app.state.manager.lock(request.path_params["eid"]):
        return await engagement_record(request)


async def engagement_record(request):
    store, eid = request.app.state.store, request.path_params["eid"]
    row = store.engagement(eid)
    if request.method == "PATCH":
        data = await body(request)
        for key in ("name", "draft", "view"):
            if key in data:
                row[key] = text_value(data, key, 200 if key == "name" else 100_000, required=key == "name")
        if "archived" in data:
            if type(data["archived"]) is not bool:
                raise ValueError("Archive must be boolean")
            row["archived"] = data["archived"]
        if "config" in data:
            row["config"] = validate_config(data["config"])
        store.save_engagement(row)
    return JSONResponse(row)


async def templates(request):
    store, manager = request.app.state.store, request.app.state.manager
    if request.method == "GET":
        return JSONResponse(store.templates())
    data = await body(request)
    eid = text_value(data, "source_id", 100)
    name = text_value(data, "name", 200)
    include = data.get("include_context", False)
    if type(include) is not bool:
        raise ValueError("Include context must be boolean")
    async with manager.lock(eid):
        row = store.engagement(eid)
        cfg = validate_config(row["config"])
        # A new workspace has no durable Ralph state to resume. All other settings,
        # including the model and the original goal, are independent saved values.
        cfg["ralph_resume"] = False
        if not row["session_id"] and not cfg["goal"]:
            cfg["goal"] = row["draft"] or None
        content = None
        if include:
            path = contained(store.work(eid), store.work(eid) / "pentest.md")
            if path.stat().st_size > MAX_PREVIEW:
                raise ValueError("Scope context exceeds 1 MB")
            content = path.read_text(encoding="utf-8")
        template = {
            "id": identity(), "name": name, "target": row["target"],
            "config": cfg, "context": content, "created_at": time.time(),
        }
        store.save_template(template)
    return JSONResponse(template, 201)


async def delete_template(request):
    store = request.app.state.store
    store.delete_template(request.path_params["tid"])
    return JSONResponse({"deleted": True})


async def delete_engagement(request):
    store, manager, eid = (
        request.app.state.store, request.app.state.manager, request.path_params["eid"]
    )
    data = await body(request)
    async with manager.lock(eid):
        row = store.engagement(eid)
        if data.get("confirm_name") != row["name"]:
            return JSONResponse({"error": "Type the current attack name to confirm deletion"}, 409)
        sessions = store.sessions(eid)
        running = [s for s in sessions if s["snapshot"]["lifecycle"] not in TERMINAL]
        if running and data.get("close_session") is not True:
            return JSONResponse({"error": "Confirm ending this attack's runtime before deleting"}, 409)
        # Close only workers owned by this attack, and wait for readers and sealing
        # before touching their files or journal. Other Zero/Codex sessions are untouched.
        for session in sessions:
            worker = manager.workers.get(session["id"])
            if worker:
                if worker.process and worker.process.returncode is None:
                    await worker.close()
                elif worker.tasks:
                    await asyncio.gather(*worker.tasks, return_exceptions=True)
        from zero.orchestrator.ownership import WorkspaceOwnership

        parent = store.root / "engagements"
        path = contained(store.root, parent / eid)
        try:
            if path.exists():
                with WorkspaceOwnership(contained(path, path / "work")):
                    await asyncio.to_thread(shutil.rmtree, path)
        except RuntimeError as exc:
            return JSONResponse({"error": str(exc)}, 409)
        except OSError:
            return JSONResponse(
                {"error": "Attack files could not be fully removed. Check permissions and retry."}, 503
            )
        store.delete_engagement(eid)
        for session in sessions:
            manager.workers.pop(session["id"], None)
        return JSONResponse({"deleted": True, "id": eid, "session_ids": [s["id"] for s in sessions]})


async def context(request):
    async with request.app.state.manager.lock(request.path_params["eid"]):
        return await context_document(request)


async def context_document(request):
    store, eid = request.app.state.store, request.path_params["eid"]
    row = store.engagement(eid)
    path = contained(store.work(eid), store.work(eid) / "pentest.md")
    if request.method == "PUT":
        data = await body(request)
        if data.get("revision") != row["context_revision"]:
            return JSONResponse(
                {"error": "Scope was changed in another tab. Reload before saving."}, 409
            )
        content = text_value(data, "content", MAX_PREVIEW, required=False)
        path.write_text(content, encoding="utf-8")
        row["context_revision"] += 1
        store.save_engagement(row)
    return JSONResponse(
        {
            "content": path.read_text(),
            "revision": row["context_revision"],
            "applies": "next session",
        }
    )


async def files(request):
    async with request.app.state.manager.lock(request.path_params["eid"]):
        return await context_files(request)


async def context_files(request):
    store, eid = request.app.state.store, request.path_params["eid"]
    row = store.engagement(eid)
    if request.method == "POST":
        data = await body(request)
        name = relative_name(text_value(data, "name", 1000))
        root_name = relative_name(row["config"].get("files_root", "zero_files"))
        root = contained(store.work(eid), store.work(eid) / root_name)
        root.mkdir(parents=True, exist_ok=True)
        path = contained(root, root / name)
        if path.exists():
            return JSONResponse({"error": "A context file with that name already exists"}, 409)
        if "data_base64" in data:
            try:
                raw = base64.b64decode(
                    text_value(data, "data_base64", 1_400_000, required=False), validate=True
                )
            except (ValueError, binascii.Error):
                raise ValueError("Invalid encoded file content") from None
        else:
            raw = text_value(data, "content", MAX_PREVIEW, required=False).encode()
        if len(raw) > MAX_PREVIEW:
            raise ValueError("Context file exceeds 1 MB")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    return JSONResponse(inventory(store, eid, request.query_params.get("session_id")))


async def preflight(cfg: dict, scripted: bool):
    if scripted:
        return
    binary = cfg["backend"]
    if shutil.which(binary) is None:
        raise ValueError(f"{binary} CLI is not on PATH. Install and authenticate it first.")
    args = [binary, "login", "status"] if binary == "codex" else [binary, "auth", "status"]
    process = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        await asyncio.wait_for(process.communicate(), 12)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError(f"{binary} authentication check timed out") from None
    if process.returncode:
        raise ValueError(f"{binary} authentication check failed. Authenticate the CLI and retry.")
    if cfg["container"]:
        if not shutil.which("docker"):
            raise ValueError(
                "Docker is unavailable. Install/start Docker or explicitly choose host execution."
            )
        process = await asyncio.create_subprocess_exec(
            "docker",
            "image",
            "inspect",
            cfg["container_image"],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(process.communicate(), 12)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise ValueError("Docker readiness check timed out") from None
        if process.returncode:
            raise ValueError("Sandbox image unavailable. Build docker/Dockerfile and start Docker.")


async def start_session(request):
    return await begin_session(request, await body(request))


async def begin_session(request, data, *, replay_path=None):
    store, manager = request.app.state.store, request.app.state.manager
    eid = request.path_params["eid"]
    key = text_value(data, "idempotency_key", 200)
    key = f"{eid}:start:{key}"
    async with manager.lock(eid):
        prior = store.command(key)
        if prior is not None:
            return JSONResponse(prior)
        row = store.engagement(eid)
        if row["session_id"]:
            old = store.session(row["session_id"])
            if old["snapshot"]["lifecycle"] not in TERMINAL:
                return JSONResponse(
                    {"error": "End the current session before starting a new configuration"}, 409
                )
        cfg = validate_config(data["config"])
        if not cfg["goal"] or not cfg["goal"].strip():
            raise ValueError("Enter the first assessment goal")
        for key_name in ("files_root", "burp_dir"):
            if cfg[key_name]:
                path = contained(store.work(eid), store.work(eid) / cfg[key_name])
                if key_name == "burp_dir" and not path.is_dir():
                    raise ValueError("Import the Burp directory into this engagement first")
        if cfg["ralph_resume"] and not (store.work(eid) / ".ralph/prd.json").is_file():
            raise ValueError("No durable Ralph plan exists to resume")
        if replay_path is None:
            await preflight(cfg, manager.scripted)
        # Seal interrupted service sessions before allowing a new generation to
        # overwrite their artifact paths. A CLI ownership lock is never bypassed.
        if row["session_id"]:
            old = store.session(row["session_id"])
            if not old["history_path"]:
                from zero.orchestrator.ownership import WorkspaceOwnership

                with WorkspaceOwnership(store.work(eid)):
                    archiver = Worker(store, old["id"], manager.scripted)
                    relative = await asyncio.to_thread(archiver.seal_files, eid, old["context"])
                    store.seal_path(old["id"], relative)
        row["config"] = cfg
        store.save_engagement(row)
        session = store.create_session(eid, cfg)
        if replay_path is not None:
            store.annotate_session(session["id"], {"kind": "replay", "replay_path": replay_path})
            session = store.session(session["id"])
        store.append(
            session["id"], {"kind": "user_text", "text": cfg["goal"]}, message_id=identity()
        )
        try:
            await manager.start(session)
        except Exception as exc:
            store.append(session["id"], {"kind": "runtime_error", "error": str(exc)})
            store.append(session["id"], {"kind": "runtime_state", "lifecycle": "failed"})
            raise ValueError(f"Runtime startup failed: {exc}") from None
        result = {"session_id": session["id"], "run_id": session["run_id"]}
        store.remember(key, result)
        return JSONResponse(result, 201)


async def import_recording(request):
    async with request.app.state.manager.lock(request.path_params["eid"]):
        return await recording_document(request)


async def recording_document(request):
    store, eid = request.app.state.store, request.path_params["eid"]
    store.engagement(eid)
    data = await body(request)
    content = text_value(data, "content", 5_000_000)
    rows = []
    for number, line in enumerate(content.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError:
            raise ValueError(f"Malformed audit JSON on line {number}") from None
        if not isinstance(row, dict):
            raise ValueError(f"Audit line {number} is not an object")
        rows.append(row)
    if not any(row.get("kind") == "agent_step" and isinstance(row.get("raw"), str) for row in rows):
        raise ValueError("The audit has no recorded agent_step envelopes to replay")
    directory = contained(store.work(eid), store.work(eid) / ".recordings")
    directory.mkdir(exist_ok=True)
    path = directory / (identity() + ".jsonl")
    path.write_text(content, encoding="utf-8")
    item = next(
        a
        for a in inventory(store, eid)
        if a["path"] == path.relative_to(store.work(eid)).as_posix()
    )
    return JSONResponse(item, 201)


async def replay_session(request):
    store, eid = request.app.state.store, request.path_params["eid"]
    data = await body(request)
    item, path = artifact(store, eid, text_value(data, "artifact_id", 100))
    if not item["path"].startswith(".recordings/"):
        raise ValueError("Import a recorded audit before replaying it")
    cfg = validate_config(data.get("config", {}))
    cfg.update(
        execution_mode="c",
        swarm_size=1,
        ralph=False,
        ralph_resume=False,
        yolo=False,
        auto_approve=[],
        allow_host_exec=False,
        goal=data.get("goal") or "Replay the imported audit under the configured approval policy",
    )
    return await begin_session(
        request,
        {"config": cfg, "idempotency_key": data["idempotency_key"]},
        replay_path=str(path.relative_to(store.work(eid))),
    )


async def reset_engagement(request):
    store, manager, eid = (
        request.app.state.store,
        request.app.state.manager,
        request.path_params["eid"],
    )
    data = await body(request)
    if data.get("scope") != ".pentest":
        raise ValueError("Reset requires explicit .pentest scope")
    async with manager.lock(eid):
        row = store.engagement(eid)
        if (
            row["session_id"]
            and store.session(row["session_id"])["snapshot"]["lifecycle"] not in TERMINAL
        ):
            return JSONResponse(
                {"error": "End the active session before resetting runtime artifacts"}, 409
            )
        from zero.orchestrator.ownership import WorkspaceOwnership

        with WorkspaceOwnership(store.work(eid)):
            path = contained(store.work(eid), store.work(eid) / ".pentest")
            if path.is_dir():
                await asyncio.to_thread(shutil.rmtree, path)
        return JSONResponse(
            {
                "removed": ".pentest",
                "preserved": [
                    "conversation",
                    "sealed history",
                    "pentest.md",
                    "context files",
                    ".ralph",
                    ".ralph_logs",
                ],
            }
        )


async def snapshot(request):
    return JSONResponse(request.app.state.store.session(request.path_params["sid"]))


async def events(request):
    store, sid = request.app.state.store, request.path_params["sid"]
    store.session(sid)
    cursor = int(request.headers.get("last-event-id") or request.query_params.get("after", 0))

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            try:
                store.session(sid)
            except KeyError:
                return
            batch = store.events(sid, cursor)
            for event in batch:
                cursor = event["seq"]
                yield f"id: {cursor}\ndata: {json.dumps(event)}\n\n"
            if not batch:
                yield ": keepalive\n\n"
                await asyncio.sleep(0.4)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"},
    )


async def session_action(request):
    store, manager = request.app.state.store, request.app.state.manager
    sid, action = request.path_params["sid"], request.path_params["action"]
    data = await body(request)
    session = store.session(sid)
    key = f"{sid}:{action}:{text_value(data, 'idempotency_key', 200)}"
    async with manager.lock(session["engagement_id"]):
        prior = store.command(key)
        if prior is not None:
            return JSONResponse(prior)
        worker = manager.worker(sid)
        if action == "close":
            await worker.close()
            result = {"outcome": "closed"}
        elif action == "cancel-turn":
            result = await worker.command("cancel")
        elif action == "messages":
            text = text_value(data, "text")
            run_id = (
                identity()
                if session["snapshot"]["lifecycle"] == "idle"
                else session["snapshot"]["run_id"]
            )
            result = await worker.command(
                "message", {"text": text, "run_id": run_id, "message_id": identity()}
            )
        elif action == "respond-now":
            result = await worker.command("respond-now")
        else:
            raise ValueError("Unknown session operation")
        store.remember(key, result)
        return JSONResponse(result)


async def decision(request):
    store, manager = request.app.state.store, request.app.state.manager
    sid, rid, kind = (request.path_params[k] for k in ("sid", "rid", "kind"))
    data = await body(request)
    session = store.session(sid)
    key = f"{sid}:{kind}:{rid}:{text_value(data, 'idempotency_key', 200)}"
    async with manager.lock(session["engagement_id"]):
        prior = store.command(key)
        if prior is not None:
            return JSONResponse(prior)
        interaction = store.session(sid)["snapshot"]["interactions"].get(rid)
        expected = {
            "approvals": "approval_prompt",
            "questions": "ask_user",
            "plans": "plan_checkpoint",
        }
        if not interaction or interaction["kind"] != expected.get(kind):
            return JSONResponse(
                {"error": "This request expired or was resolved in another tab"}, 409
            )
        if kind == "approvals":
            if data.get("decision") not in {"yes", "no", "always"}:
                raise ValueError("Invalid approval decision")
            if data["decision"] == "always" and not interaction["session_grant_allowed"]:
                raise ValueError("Session grants are unavailable for this execution boundary")
            action = "approval"
        elif kind == "questions":
            answer = text_value(data, "answer")
            if not interaction["free_form"] and answer not in interaction["options"]:
                raise ValueError("Choose one of the available answers")
            action = "answer"
        else:
            action = "plan"
        result = await manager.worker(sid).command(action, {**data, "request_id": rid})
        store.remember(key, result)
        return JSONResponse(result)


async def resources(request):
    store, eid, resource = (
        request.app.state.store,
        request.path_params["eid"],
        request.path_params["resource"],
    )
    sid = request.query_params.get("session_id")
    if resource == "findings":
        result = findings(store, eid, sid)
    elif resource == "artifacts":
        result = inventory(store, eid, sid)
    elif resource == "conversation":
        result = [
            {
                "session_id": s["id"],
                "created_at": s["created_at"],
                "messages": s["snapshot"]["messages"],
            }
            for s in reversed(store.sessions(eid))
        ]
    elif resource == "runs":
        result = store.sessions(eid)
    elif resource == "swarm":
        result = swarm(store, eid, sid)
    elif resource == "logs":
        result = logs(
            store,
            eid,
            request.query_params.get("kind", "audit"),
            sid=sid,
            offset=max(0, int(request.query_params.get("offset", 0))),
            query=request.query_params.get("q", ""),
            actor=request.query_params.get("actor", ""),
        )
    else:
        raise KeyError("Resource not found")
    return JSONResponse(result)


async def read_artifact(request):
    eid, aid = request.path_params["eid"], request.path_params["aid"]
    item, path = artifact(request.app.state.store, eid, aid, request.query_params.get("session_id"))
    if request.query_params.get("download") == "1":
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
            content_disposition_type="attachment",
        )
    with path.open("rb") as handle:
        raw = handle.read(MAX_PREVIEW + 1)
    return JSONResponse(
        {
            **item,
            "content": raw[:MAX_PREVIEW].decode(errors="replace"),
            "truncated": len(raw) > MAX_PREVIEW,
        }
    )


async def error(request, exc):
    status = 404 if isinstance(exc, KeyError) else 422
    return JSONResponse({"error": str(exc)}, status)


def create_app(data_root: Path | None = None, *, scripted=False, dev_origin=None):
    root = data_root or Path(__file__).resolve().parents[2] / ".local"
    from zero.orchestrator.ownership import WorkspaceOwnership

    ownership = WorkspaceOwnership(root)
    ownership.__enter__()
    try:
        store = Store(root)
        manager = Manager(store, scripted)
    except BaseException:
        ownership.__exit__(None, None, None)
        raise

    @asynccontextmanager
    async def lifespan(app):
        yield
        await manager.close()
        store.db.close()
        ownership.__exit__(None, None, None)

    routes = [
        Route(API + "/health", health),
        Route(API + "/capabilities", health),
        Route(API + "/config-schema", schema),
        Route(API + "/models", models),
        Route(API + "/config/validate", configuration, methods=["POST"]),
        Route(API + "/templates", templates, methods=["GET", "POST"]),
        Route(API + "/templates/{tid}", delete_template, methods=["DELETE"]),
        Route(API + "/engagements", engagements, methods=["GET", "POST"]),
        Route(API + "/engagements/{eid}", engagement, methods=["GET", "PATCH", "DELETE"]),
        Route(API + "/engagements/{eid}/context", context, methods=["GET", "PUT"]),
        Route(API + "/engagements/{eid}/files", files, methods=["GET", "POST"]),
        Route(API + "/engagements/{eid}/sessions", start_session, methods=["POST"]),
        Route(API + "/engagements/{eid}/recordings", import_recording, methods=["POST"]),
        Route(API + "/engagements/{eid}/replay", replay_session, methods=["POST"]),
        Route(API + "/engagements/{eid}/reset", reset_engagement, methods=["POST"]),
        Route(API + "/engagements/{eid}/artifacts/{aid}", read_artifact),
        Route(API + "/engagements/{eid}/{resource}", resources),
        Route(API + "/sessions/{sid}/snapshot", snapshot),
        Route(API + "/sessions/{sid}/events", events),
        Route(API + "/sessions/{sid}/{kind}/{rid}", decision, methods=["POST"]),
        Route(API + "/sessions/{sid}/{action}", session_action, methods=["POST"]),
    ]
    dist = Path(__file__).resolve().parents[2] / "web/dist"
    if dist.is_dir():
        routes.append(Mount("/assets", StaticFiles(directory=dist / "assets")))

        async def index(request):
            return FileResponse(dist / "index.html")

        routes.append(Route("/{path:path}", index))
    app = Starlette(
        routes=routes,
        lifespan=lifespan,
        exception_handlers={ValueError: error, KeyError: error, ValidationError: error},
    )
    app.state.store, app.state.manager, app.state.csrf = store, manager, secrets.token_urlsafe(32)
    app.state.model_catalog = CodexCatalog(store.root)
    app.state.origins = {dev_origin} if dev_origin else set()
    app.add_middleware(LocalBoundary)
    return app


def main():
    parser = argparse.ArgumentParser(description="Zero Studio · local desktop interface")
    parser.add_argument("--port", type=int, default=4318)
    parser.add_argument("--data-root", type=Path)
    parser.add_argument("--dev-origin", choices=["http://127.0.0.1:4319", "http://localhost:4319"])
    args = parser.parse_args()
    print(f"Zero Studio · http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(
        create_app(
            args.data_root,
            scripted=os.environ.get("ZERO_WEB_TEST_MODE") == "1",
            dev_origin=args.dev_origin,
        ),
        host="127.0.0.1",
        port=args.port,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
