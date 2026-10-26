"""Free loopback provider fixture; the actual LiteLLM transport is exercised."""
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest


@pytest.fixture
def api_server(monkeypatch):
    monkeypatch.setenv("ZERO_TEST_API_KEY", "fake-loopback-provider-key")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    state = SimpleNamespace(requests=[], external=[],
                            respond=lambda body: '{"type":"final","answer":"done"}')

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            state.requests.append({"path": self.path,
                                   "headers": {k.lower(): v for k, v in self.headers.items()},
                                   "body": body})
            answer = state.respond(body)
            status = 200
            if isinstance(answer, tuple):
                status, result = answer
            elif self.path.endswith("/messages"):
                result = {"id": "msg-loopback", "type": "message", "role": "assistant",
                          "model": body["model"], "content": [{"type": "text", "text": answer}],
                          "stop_reason": "end_turn", "stop_sequence": None,
                          "usage": {"input_tokens": 100, "output_tokens": 10}}
            else:
                result = {"id": "chatcmpl-loopback", "object": "chat.completion", "created": 0,
                          "model": body["model"], "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": answer}}],
                          "usage": {"prompt_tokens": 100, "completion_tokens": 10,
                                    "total_tokens": 110}}
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Retry-After", "0")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    state.base_url = f"http://127.0.0.1:{server.server_port}/v1"
    original = socket.getaddrinfo

    def local_only(host, *args, **kwargs):
        if host not in {"localhost", "127.0.0.1", b"127.0.0.1", "::1"}:
            state.external.append(host)
            raise AssertionError(f"Free API test attempted external access: {host}")
        return original(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", local_only)
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        assert not state.external, f"Unexpected external lookups: {state.external}"
