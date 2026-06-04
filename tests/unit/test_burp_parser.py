"""Hand-crafted Burp XML fixtures, no Burp install needed."""
from __future__ import annotations

import base64
from pathlib import Path

from zero.traffic.burp_parser import parse_burp_xml


def _make_burp_xml(
    items: list[dict],
    tmp_path: Path,
) -> Path:
    parts = ["<?xml version=\"1.0\"?>\n<items>"]
    for it in items:
        req = base64.b64encode(it["request"].encode()).decode()
        resp = base64.b64encode(it["response"].encode()).decode()
        parts.append(
            f"""
<item>
  <url>{it['url']}</url>
  <host>{it['host']}</host>
  <method>{it['method']}</method>
  <status>{it['status']}</status>
  <mimetype>{it.get('mimetype', '')}</mimetype>
  <request base64="true">{req}</request>
  <response base64="true">{resp}</response>
</item>"""
        )
    parts.append("</items>")
    p = tmp_path / "burp.xml"
    p.write_text("".join(parts), encoding="utf-8")
    return p


def test_parses_minimal_get(tmp_path: Path):
    items = [
        {
            "url": "https://example.com/api/orders/1041",
            "host": "example.com",
            "method": "GET",
            "status": 200,
            "mimetype": "JSON",
            "request": "GET /api/orders/1041 HTTP/1.1\r\nHost: example.com\r\nAuthorization: Bearer abc\r\n\r\n",
            "response": "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"id\":1041}",
        }
    ]
    p = _make_burp_xml(items, tmp_path)
    out = list(parse_burp_xml(p))
    assert len(out) == 1
    r = out[0]
    assert r.method == "GET"
    assert r.url == "https://example.com/api/orders/1041"
    assert r.status == 200
    assert r.request_headers["authorization"] == "Bearer abc"
    assert r.response_headers["content-type"] == "application/json"
    assert r.response_body == '{"id":1041}'
    assert "json" in r.response_content_type.lower()


def test_parses_post_with_body(tmp_path: Path):
    body = '{"user":"alice","password":"hunter2"}'
    items = [
        {
            "url": "https://api.example.com/login",
            "host": "api.example.com",
            "method": "POST",
            "status": 401,
            "request": f"POST /login HTTP/1.1\r\nHost: api.example.com\r\nContent-Type: application/json\r\n\r\n{body}",
            "response": "HTTP/1.1 401 Unauthorized\r\nContent-Type: application/json\r\n\r\n{\"error\":\"bad creds\"}",
        }
    ]
    p = _make_burp_xml(items, tmp_path)
    out = list(parse_burp_xml(p))
    assert len(out) == 1
    r = out[0]
    assert r.request_body == body
    assert r.status == 401


def test_skips_malformed_xml(tmp_path: Path):
    p = tmp_path / "bad.xml"
    p.write_text("<not-valid", encoding="utf-8")
    assert list(parse_burp_xml(p)) == []


def test_skips_item_without_url(tmp_path: Path):
    p = tmp_path / "x.xml"
    p.write_text(
        '<?xml version="1.0"?>\n<items><item><method>GET</method></item></items>',
        encoding="utf-8",
    )
    assert list(parse_burp_xml(p)) == []
