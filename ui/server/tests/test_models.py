"""Model catalog and per-Zero model selection: offline, no LLM calls or real CLI."""

import asyncio
import json
import sys

import pytest

from zero.llm.codex_cli import build_codex_argv
from zero.orchestrator.orchestrator import Orchestrator
from zero_web import models
from zero_web.config import core_config


@pytest.fixture
def codex_cli(tmp_path, monkeypatch):
    cli = tmp_path / "codex-fixture.py"
    monkeypatch.setattr(models.shutil, "which", lambda name: str(cli))
    spawn = asyncio.create_subprocess_exec

    async def launch(binary, *args, **kwargs):
        assert binary == str(cli)
        assert args == ("app-server", "--listen", "stdio://")
        # Windows CreateProcess cannot execute a Python shebang script directly.
        # Keep the real pipe/RPC lifecycle while using the interpreter on every OS.
        return await spawn(sys.executable, binary, *args, **kwargs)

    monkeypatch.setattr(models.asyncio, "create_subprocess_exec", launch)
    return cli


def codex_home(tmp_path, monkeypatch):
    home = tmp_path / "codex-home"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    (home / "config.toml").write_text('model = "coding-default"\n')
    return home


def test_picker_metadata_is_dynamic_and_does_not_expose_cache_contents():
    rows = [
        {
            "id": "opaque",
            "model": "new-model-id",
            "displayName": "New model",
            "description": "A fresh catalog entry",
        },
        {
            "slug": "cached-id",
            "display_name": "Cached name",
            "visibility": "list",
            "base_instructions": "private",
        },
        {"model": "hidden-id", "hidden": True},
        {"slug": "internal-id", "visibility": "hide"},
        {"model": "new-model-id", "displayName": "Duplicate"},
        {"id": "bad\x00id"},
        {"id": 7},
        None,
    ]
    result = models.normalize_models(rows)
    assert [m["id"] for m in result] == ["new-model-id", "cached-id"]
    assert result[0]["name"] == "New model"
    assert all(set(m) == {"id", "name", "description"} for m in result)


async def test_rpc_lists_every_page_without_threads_turns_or_settings_writes(
    tmp_path, monkeypatch, codex_cli
):
    home = codex_home(tmp_path, monkeypatch)
    original = (home / "config.toml").read_bytes()
    transcript = tmp_path / "requests.jsonl"
    codex_cli.write_text(f"""
import json,sys
with open({str(transcript)!r},'a') as output:
    for line in sys.stdin:
        req=json.loads(line)
        output.write(json.dumps(req)+'\\n');output.flush()
        if req.get('method')=='initialize':
            print(json.dumps({{'id':req['id'],'result':{{'userAgent':'fixture'}}}}),flush=True)
        elif req.get('method')=='model/list':
            if req['params']['cursor'] is None:
                print(json.dumps({{'method':'catalog/notification'}}),flush=True)
                print(json.dumps({{'id':99,'method':'approval/request','params':{{}}}}),flush=True)
                data=[{{'model':'fixture-first','id':'opaque-1','displayName':'First'}}]
                cursor='next-page'
            else:
                data=[{{'model':'fixture-future-model','id':'opaque-2','displayName':'Future model'}},{{'model':'hidden','hidden':True}}]
                cursor=None
            print(json.dumps({{'id':req['id'],'result':{{'data':data,'nextCursor':cursor}}}}),flush=True)
""")
    result = await models.query_codex_models(tmp_path)
    assert [m["id"] for m in result] == ["fixture-first", "fixture-future-model"]
    requests = [json.loads(line) for line in transcript.read_text().splitlines()]
    assert [r["method"] for r in requests if "method" in r] == [
        "initialize",
        "initialized",
        "model/list",
        "model/list",
    ]
    assert all(not r.get("params", {}).get("includeHidden") for r in requests)
    assert next(r for r in requests if r.get("id") == 99)["error"]["code"] == -32601
    assert (home / "config.toml").read_bytes() == original


async def test_catalog_cache_refresh_and_default_are_read_only(tmp_path, monkeypatch):
    home = codex_home(tmp_path, monkeypatch)
    calls = []

    async def fetch(_):
        calls.append(True)
        return [{"id": f"catalog-{len(calls)}", "name": "Catalog model", "description": ""}]

    monkeypatch.setattr(models, "query_codex_models", fetch)
    catalog = models.CodexCatalog(tmp_path)
    one, two = await asyncio.gather(catalog.get(), catalog.get())
    assert len(calls) == 1 and one["models"] == two["models"]
    (home / "config.toml").write_text('model = "different-coding-default"\n')
    assert (await catalog.get())["configured_default"] == "different-coding-default"
    assert len(calls) == 1
    refreshed = await catalog.get(refresh=True)
    assert refreshed["models"][0]["id"] == "catalog-2"
    assert len(calls) == 2
    assert (home / "config.toml").read_text() == 'model = "different-coding-default"\n'


async def test_unavailable_rpc_uses_explicitly_labeled_cache_hints(tmp_path, monkeypatch):
    home = codex_home(tmp_path, monkeypatch)
    cache = {
        "identity": {"private": "never expose"},
        "fetched_at": "2026-11-14T00:00:00Z",
        "models": [
            {"slug": "cached-future-model", "display_name": "Cached future", "visibility": "list"},
            {"slug": "hidden", "visibility": "hide"},
        ],
    }
    (home / "models_cache.json").write_text(json.dumps(cache))

    async def unavailable(_):
        raise models.DiscoveryUnavailable("Codex model query timed out.")

    monkeypatch.setattr(models, "query_codex_models", unavailable)
    result = await models.CodexCatalog(tmp_path).get()
    assert result["source"] == "codex_cache"
    assert [m["id"] for m in result["models"]] == ["cached-future-model"]
    assert "cached" in result["warning"].lower()
    assert "private" not in json.dumps(result)
    (home / "models_cache.json").write_text("broken")
    empty = await models.CodexCatalog(tmp_path).get()
    assert empty["source"] == "unavailable" and empty["models"] == []


async def test_catalog_process_is_reaped_on_timeout(tmp_path, monkeypatch, codex_cli):
    codex_cli.write_text("import time\ntime.sleep(60)\n")
    spawn = asyncio.create_subprocess_exec
    children = []

    async def capture(*args, **kwargs):
        process = await spawn(*args, **kwargs)
        children.append(process)
        return process

    monkeypatch.setattr(models.asyncio, "create_subprocess_exec", capture)
    with pytest.raises(models.DiscoveryUnavailable):
        await models.query_codex_models(tmp_path, timeout=0.15)
    assert children and children[0].returncode is not None


async def test_models_endpoint_and_saved_selection_are_engagement_local(
    client, tmp_path, monkeypatch
):
    home = codex_home(tmp_path, monkeypatch)

    async def fetch(_):
        return [
            {
                "id": "fixture-discovered",
                "name": "Discovered model",
                "description": "From fixture RPC",
            }
        ]

    monkeypatch.setattr(models, "query_codex_models", fetch)
    response = await client.get("/api/v1/models?backend=codex&refresh=1")
    assert response.status_code == 200
    assert response.json()["models"][0]["id"] == "fixture-discovered"
    assert (await client.get("/api/v1/models?backend=claude")).status_code == 422
    attacks = []
    for name in ("One", "Two"):
        result = await client.post(
            "/api/v1/engagements", json={"name": name, "target": "https://fixture.example"}
        )
        attacks.append(result.json())
    result = await client.patch(
        f"/api/v1/engagements/{attacks[0]['id']}", json={"config": {"model": "fixture-discovered"}}
    )
    assert result.status_code == 200
    second = (await client.get(f"/api/v1/engagements/{attacks[1]['id']}")).json()
    assert second["config"] == {}
    assert (home / "config.toml").read_text() == 'model = "coding-default"\n'


def test_selected_model_is_only_an_invocation_override_for_every_zero_actor(tmp_path, monkeypatch):
    home = codex_home(tmp_path, monkeypatch)
    original = (home / "config.toml").read_bytes()
    orch = Orchestrator(
        core_config(tmp_path, {"model": "selected-catalog-model", "container": False})
    )
    try:
        assert all(
            backend.cfg.model == "selected-catalog-model"
            for backend in (orch.backend, orch.subagent_backend, orch.validator_backend)
        )
        for resumed in (False, True):
            argv = build_codex_argv(
                binary="codex",
                last_message_path="/tmp/result",
                model=orch.backend.cfg.model,
                session_id="zero-only-thread",
                resume=resumed,
            )
            assert argv[argv.index("--model") + 1] == "selected-catalog-model"
            assert "config" not in argv
        assert orch._make_session("first").id != orch._make_session("second").id
        assert (home / "config.toml").read_bytes() == original
    finally:
        orch.audit.close()
        orch.thinking.close()
