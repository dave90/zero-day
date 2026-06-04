"""TrafficStore + CapturedRequest.

Holds captured HTTP requests/responses (Burp/ZAP/HAR — Phase 1 only Burp)
in memory plus an optional on-disk index under ./.pentest/traffic/.

Persistence is intentionally simple: a single JSONL file. Searches run in
Python; for the dataset sizes we expect (one user session) this is plenty.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass
class CapturedRequest:
    id: str
    host: str
    method: str
    url: str
    status: int
    request_headers: dict[str, str] = field(default_factory=dict)
    request_body: str = ""           # text or base64-decoded utf-8 (best-effort)
    response_headers: dict[str, str] = field(default_factory=dict)
    response_body: str = ""
    response_content_type: str = ""
    tags: list[str] = field(default_factory=list)
    captured_at: float = field(default_factory=time.time)
    source: str = "burp"

    def matches(
        self,
        *,
        host: str | None = None,
        url_substring: str | None = None,
        status: int | None = None,
        content_type: str | None = None,
        tag: str | None = None,
        text: str | None = None,
        method: str | None = None,
    ) -> bool:
        if host is not None and host.lower() != self.host.lower():
            return False
        if url_substring is not None and url_substring not in self.url:
            return False
        if status is not None and status != self.status:
            return False
        if content_type is not None and content_type.lower() not in self.response_content_type.lower():
            return False
        if tag is not None and tag not in self.tags:
            return False
        if method is not None and method.upper() != self.method.upper():
            return False
        if text is not None:
            hay = " ".join(
                [
                    self.url,
                    self.request_body or "",
                    self.response_body or "",
                    " ".join(f"{k}:{v}" for k, v in self.request_headers.items()),
                    " ".join(f"{k}:{v}" for k, v in self.response_headers.items()),
                ]
            )
            if text.lower() not in hay.lower():
                return False
        return True

    def summary(self) -> str:
        return (
            f"[{self.id}] {self.method} {self.url} → {self.status}"
            f" ({self.response_content_type or '?'}; "
            f"req {len(self.request_body)}B / resp {len(self.response_body)}B)"
        )


class TrafficStore:
    def __init__(self, persist_path: Path | None = None) -> None:
        self._items: list[CapturedRequest] = []
        self._by_id: dict[str, CapturedRequest] = {}
        self._persist = persist_path

    def __len__(self) -> int:
        return len(self._items)

    def add(self, req: CapturedRequest) -> None:
        if req.id in self._by_id:
            return
        self._items.append(req)
        self._by_id[req.id] = req
        if self._persist is not None:
            self._persist.parent.mkdir(parents=True, exist_ok=True)
            with open(self._persist, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(asdict(req), ensure_ascii=False) + "\n")

    def extend(self, reqs: Iterable[CapturedRequest]) -> int:
        n = 0
        for r in reqs:
            self.add(r)
            n += 1
        return n

    def get(self, id_: str) -> CapturedRequest | None:
        return self._by_id.get(id_)

    def search(
        self,
        *,
        host: str | None = None,
        url_substring: str | None = None,
        status: int | None = None,
        content_type: str | None = None,
        tag: str | None = None,
        text: str | None = None,
        method: str | None = None,
        limit: int = 50,
    ) -> list[CapturedRequest]:
        out = []
        for item in self._items:
            if item.matches(
                host=host,
                url_substring=url_substring,
                status=status,
                content_type=content_type,
                tag=tag,
                text=text,
                method=method,
            ):
                out.append(item)
                if len(out) >= limit:
                    break
        return out

    def hosts(self) -> list[str]:
        return sorted({i.host for i in self._items})

    def all(self) -> list[CapturedRequest]:
        return list(self._items)

    @classmethod
    def load_jsonl(cls, path: Path) -> "TrafficStore":
        store = cls(persist_path=None)
        if not path.exists():
            return store
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                store.add(CapturedRequest(**obj))
        return store


def new_request_id() -> str:
    return uuid.uuid4().hex[:12]
