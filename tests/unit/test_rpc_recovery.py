"""Sandbox RPC recovery: a mid-run transport failure must reconnect / re-create the
container and retry, not kill the engagement.

Regression for a real incident: a running engagement's sandbox container was removed
mid-run, after which EVERY tool call failed with `RPCError: All connection attempts
failed` and the run spun uselessly. Now `ContainerRPC.call` retries with a recover
hook (which re-creates the container and hands back a new base_url), and gives up with
a clear error after a bounded number of attempts instead of spinning forever.
"""
from __future__ import annotations

import httpx
import pytest

from zero.container.rpc import ContainerRPC, RPCError


class _FakeResp:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self._body = body or {"ok": True, "output": "done", "truncated": False, "meta": None}
        self.text = "ok"

    def json(self):
        return self._body


class _FakeClient:
    """One persistent fake httpx client: raises a transport error `fail_times`, then
    returns a 200. Survives reconnects (the RPC drops+recreates its client on failure)
    so we can count total attempts."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.calls = 0

    async def post(self, url, json):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise httpx.ConnectError("All connection attempts failed")
        return _FakeResp()

    async def aclose(self):
        pass


class _RPCUnderTest(ContainerRPC):
    """ContainerRPC whose connect() installs a shared fake client instead of real httpx."""

    def __init__(self, fake: _FakeClient, **kw):
        super().__init__("http://dead-sandbox:1", **kw)
        self._fake = fake

    async def connect(self):
        if self._client is None:
            self._client = self._fake


async def test_recovers_after_container_recreate_and_retries():
    fake = _FakeClient(fail_times=1)         # first call fails (sandbox gone), then works
    recovered = {}

    async def recover(failed_url):
        recovered["url"] = failed_url
        return "http://fresh-sandbox:2"       # the re-created container's new endpoint

    rpc = _RPCUnderTest(fake, recover=recover, retry_backoff=0)
    result = await rpc.call("grep", {"pattern": "x"})

    assert result.ok and result.output == "done"     # the call ultimately SUCCEEDS
    assert recovered["url"] == "http://dead-sandbox:1"  # recover saw the dead url
    assert rpc.base_url == "http://fresh-sandbox:2"     # and the RPC now points at the new one
    assert fake.calls == 2                              # exactly one retry


async def test_transient_drop_recovers_without_new_url():
    # Container still alive (recover returns the SAME url); a reconnect fixes it.
    fake = _FakeClient(fail_times=1)

    async def recover(failed_url):
        return failed_url                     # alive → reconnect to same endpoint

    rpc = _RPCUnderTest(fake, recover=recover, retry_backoff=0)
    result = await rpc.call("list_dir", {"path": "/"})
    assert result.ok
    assert rpc.base_url == "http://dead-sandbox:1"


async def test_gives_up_after_max_attempts_instead_of_spinning():
    fake = _FakeClient(fail_times=99)         # never recovers

    async def recover(failed_url):
        return None                           # unrecoverable

    rpc = _RPCUnderTest(fake, recover=recover, max_attempts=3, retry_backoff=0)
    with pytest.raises(RPCError, match="transport error"):
        await rpc.call("grep", {"pattern": "x"})
    assert fake.calls == 3                     # bounded — did NOT spin forever


async def test_recover_hook_failure_does_not_mask_error():
    fake = _FakeClient(fail_times=99)

    async def recover(failed_url):
        raise RuntimeError("recovery blew up")

    rpc = _RPCUnderTest(fake, recover=recover, max_attempts=2, retry_backoff=0)
    with pytest.raises(RPCError):              # still a clean RPCError, not the RuntimeError
        await rpc.call("grep", {"pattern": "x"})


async def test_non_transport_errors_are_not_retried():
    # A 500 from the tool server is a real result, not a connection problem — no retry.
    class _Fake500(_FakeClient):
        async def post(self, url, json):
            self.calls += 1
            return _FakeResp(status_code=500)

    fake = _Fake500(fail_times=0)
    rpc = _RPCUnderTest(fake, max_attempts=3, retry_backoff=0)
    with pytest.raises(RPCError, match="HTTP 500"):
        await rpc.call("grep", {"pattern": "x"})
    assert fake.calls == 1                      # not retried
