"""Deterministic dedup over (normalized_endpoint, cwe, payload_shape, method).

`payload_shape` is hand-wavy in v0.1 (per plan §8 #3); we keep the simplest
useful normalizer: lowercase, strip whitespace. The endpoint normalizer
collapses numeric path segments to `{id}` and common UUID/hex tokens to
`{uuid}` / `{hex}`.
"""
from __future__ import annotations

import hashlib
import re
from typing import NamedTuple

from zero.findings.schema import Finding


_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.I)
_HEX_RE = re.compile(r"\b[0-9a-f]{16,}\b", re.I)
_INT_SEG_RE = re.compile(r"/(\d+)(?=/|$)")
_QUERY_VAL_RE = re.compile(r"=([^&]*)")


class DedupKey(NamedTuple):
    endpoint: str
    cwe: str
    payload_shape: str
    method: str

    def fingerprint(self) -> str:
        s = "|".join([self.endpoint, self.cwe, self.payload_shape, self.method])
        return hashlib.sha256(s.encode("utf-8")).hexdigest()[:24]


def normalize_endpoint(endpoint: str) -> str:
    """`/orders/1041` → `/orders/{id}`; query-string values redacted to `{v}`."""
    path, _, query = endpoint.partition("?")
    # Strip method-prefix if the caller passed "GET /orders/1".
    parts = path.strip().split()
    if len(parts) >= 2 and parts[0].isupper():
        path = parts[1]
    path = _UUID_RE.sub("{uuid}", path)
    path = _HEX_RE.sub("{hex}", path)
    path = _INT_SEG_RE.sub("/{id}", path)
    norm = path.rstrip("/")
    if query:
        # Sort param names for stability; redact values.
        params = sorted(set(p.split("=", 1)[0] for p in query.split("&") if p))
        if params:
            norm = norm + "?" + "&".join(f"{p}={{v}}" for p in params)
    return norm or "/"


def key_for(finding: Finding) -> DedupKey:
    return DedupKey(
        endpoint=normalize_endpoint(finding.endpoint),
        cwe=(finding.cwe or "CWE-?").upper().strip(),
        payload_shape=(finding.payload_shape or "unknown").lower().strip(),
        method=finding.method.upper().strip() or "GET",
    )


def dedup_hash(finding: Finding) -> str:
    return key_for(finding).fingerprint()
