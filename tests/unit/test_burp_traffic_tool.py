import base64
from pathlib import Path

from zero.tools.burp_traffic import BurpTrafficArgs, BurpTrafficTool


def _write_xml(tmp_path: Path) -> Path:
    req = base64.b64encode(b"GET /x HTTP/1.1\r\nHost: a.com\r\n\r\n").decode()
    resp = base64.b64encode(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{}").decode()
    xml = f"""<?xml version="1.0"?>
<items>
  <item>
    <url>https://a.com/x</url>
    <host>a.com</host>
    <method>GET</method>
    <status>200</status>
    <mimetype>JSON</mimetype>
    <request base64="true">{req}</request>
    <response base64="true">{resp}</response>
  </item>
</items>"""
    p = tmp_path / "burp.xml"
    p.write_text(xml, encoding="utf-8")
    return p


async def test_list_hosts(tmp_path: Path):
    p = _write_xml(tmp_path)
    r = await BurpTrafficTool().run(
        BurpTrafficArgs(action="list_hosts", burp_path=str(p))
    )
    assert r.ok and "a.com" in r.output


async def test_search_by_host(tmp_path: Path):
    p = _write_xml(tmp_path)
    r = await BurpTrafficTool().run(
        BurpTrafficArgs(action="search", host="a.com", burp_path=str(p))
    )
    assert r.ok and "/x" in r.output


async def test_no_burp_file():
    r = await BurpTrafficTool().run(BurpTrafficArgs(action="list_hosts"))
    assert r.ok is False
    assert "no Burp export" in r.output or "not found" in r.output
