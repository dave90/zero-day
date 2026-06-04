"""Parse Burp Suite "Save items" XML exports.

Burp's export looks like:

    <items>
      <item>
        <time>...</time>
        <url>https://example.com/api/orders/1041</url>
        <host ip="...">example.com</host>
        <port>443</port>
        <protocol>https</protocol>
        <method>GET</method>
        <path>/api/orders/1041</path>
        <extension>null</extension>
        <request base64="true">PGJhc2U2NCBibG9iPg==</request>
        <status>200</status>
        <responselength>1234</responselength>
        <mimetype>JSON</mimetype>
        <response base64="true">PGJhc2U2NCBibG9iPg==</response>
        <comment></comment>
      </item>
      ...
    </items>

We decode request/response bodies (best-effort utf-8), skip malformed
items, and produce CapturedRequest objects.
"""
from __future__ import annotations

import base64
import gzip
from pathlib import Path
from typing import Iterator

from lxml import etree

from zero.traffic.store import CapturedRequest, new_request_id


def parse_burp_xml(path: Path) -> Iterator[CapturedRequest]:
    """Stream CapturedRequest instances from a Burp XML export."""
    try:
        tree = etree.parse(str(path))
    except etree.XMLSyntaxError:
        return
    root = tree.getroot()
    for item in root.findall("item"):
        try:
            yield _parse_item(item)
        except _SkipItem:
            continue


class _SkipItem(Exception):
    pass


def _parse_item(item) -> CapturedRequest:
    method = _text(item.find("method")) or "GET"
    url = _text(item.find("url"))
    if not url:
        raise _SkipItem
    host = _text(item.find("host")) or ""
    status_str = _text(item.find("status")) or "0"
    try:
        status = int(status_str)
    except ValueError:
        status = 0
    mimetype = _text(item.find("mimetype")) or ""

    req_elem = item.find("request")
    resp_elem = item.find("response")
    raw_req = _decode_blob(req_elem)
    raw_resp = _decode_blob(resp_elem)
    req_headers, req_body = _split_http_message(raw_req)
    resp_headers, resp_body = _split_http_message(raw_resp)

    content_type = resp_headers.get("content-type", "") or mimetype

    return CapturedRequest(
        id=new_request_id(),
        host=host,
        method=method,
        url=url,
        status=status,
        request_headers=req_headers,
        request_body=req_body,
        response_headers=resp_headers,
        response_body=_maybe_gunzip_text(resp_body, resp_headers),
        response_content_type=content_type,
        source="burp",
    )


def _text(elem) -> str:
    if elem is None:
        return ""
    return (elem.text or "").strip()


def _decode_blob(elem) -> bytes:
    if elem is None:
        return b""
    data = elem.text or ""
    if elem.get("base64") == "true":
        try:
            return base64.b64decode(data)
        except (ValueError, base64.binascii.Error):
            return b""
    return data.encode("utf-8", "replace")


def _split_http_message(raw: bytes) -> tuple[dict[str, str], str]:
    """Split an HTTP request/response into headers dict + body string."""
    if not raw:
        return {}, ""
    sep = b"\r\n\r\n" if b"\r\n\r\n" in raw else b"\n\n"
    if sep in raw:
        head, _, body = raw.partition(sep)
    else:
        head, body = raw, b""
    headers: dict[str, str] = {}
    for line in head.splitlines()[1:]:  # skip start-line (request line / status)
        try:
            line_s = line.decode("utf-8", "replace")
        except UnicodeDecodeError:
            continue
        if ":" not in line_s:
            continue
        k, _, v = line_s.partition(":")
        headers[k.strip().lower()] = v.strip()
    return headers, body.decode("utf-8", "replace")


def _maybe_gunzip_text(body: str, headers: dict[str, str]) -> str:
    if headers.get("content-encoding", "").lower() == "gzip" and body:
        try:
            raw = body.encode("latin-1", "replace")
            return gzip.decompress(raw).decode("utf-8", "replace")
        except OSError:
            return body
    return body
