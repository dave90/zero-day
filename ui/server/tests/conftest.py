from pathlib import Path

import httpx
import pytest

from zero_web.app import create_app


@pytest.fixture
async def client(tmp_path: Path):
    app = create_app(tmp_path / "local", scripted=True)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:4318"
        ) as client:
            health = (await client.get("/api/v1/health")).json()
            client.headers["X-Zero-CSRF"] = health["csrf_token"]
            yield client
