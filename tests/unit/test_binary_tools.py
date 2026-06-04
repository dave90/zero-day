"""Argv construction tests for the binary-CLI wrappers. No subprocess."""
from __future__ import annotations

import pytest

from zero.tools.curl_raw import CurlRawArgs, CurlRawTool
from zero.tools.ffuf import FfufArgs, FfufTool
from zero.tools.httpx_tool import HttpxTool, HttpxToolArgs
from zero.tools.jwt_tool import JwtTool, JwtToolArgs
from zero.tools.nmap import NmapArgs, NmapTool
from zero.tools.nuclei import NucleiArgs, NucleiTool
from zero.tools.sqlmap import SqlmapArgs, SqlmapTool


def test_sqlmap_argv_forces_batch():
    argv = SqlmapTool().args_to_argv(SqlmapArgs(url="http://h/x?id=1"))
    assert "-u" in argv and "http://h/x?id=1" in argv
    assert "--level=1" in argv
    assert "--risk=1" in argv
    assert "--batch" in argv  # auto-injected


def test_sqlmap_passes_extra_flags():
    argv = SqlmapTool().args_to_argv(
        SqlmapArgs(url="http://h", extra_flags=["--dump", "--batch", "--dbs"])
    )
    assert argv.count("--batch") == 1  # not double-added
    assert "--dump" in argv and "--dbs" in argv


def test_nmap_default_flags():
    argv = NmapTool().args_to_argv(NmapArgs(target="1.2.3.4", ports="80,443"))
    assert argv[-1] == "1.2.3.4"
    assert "-Pn" in argv and "-sT" in argv
    i = argv.index("-p")
    assert argv[i + 1] == "80,443"


def test_ffuf_required_flags():
    argv = FfufTool().args_to_argv(
        FfufArgs(url="http://h/FUZZ", wordlist="/wl.txt", match_status="200")
    )
    assert "-u" in argv and "http://h/FUZZ" in argv
    assert "-w" in argv and "/wl.txt" in argv
    i = argv.index("-mc")
    assert argv[i + 1] == "200"
    assert "-noninteractive" in argv


def test_nuclei_severity_and_tags():
    argv = NucleiTool().args_to_argv(
        NucleiArgs(target="http://h", tags=["sqli", "xss"], severity="high,critical")
    )
    i = argv.index("-tags")
    assert argv[i + 1] == "sqli,xss"
    j = argv.index("-severity")
    assert argv[j + 1] == "high,critical"
    assert "-silent" in argv


def test_httpx_flags():
    argv = HttpxTool().args_to_argv(HttpxToolArgs(target="http://h"))
    for f in ("-u", "-fr", "-sc", "-title", "-td", "-server"):
        assert f in argv


def test_jwt_tool_basic():
    argv = JwtTool().args_to_argv(JwtToolArgs(token="abc.def.ghi"))
    assert argv == ["abc.def.ghi", "-S"]


def test_curl_raw_post_with_body():
    argv = CurlRawTool().args_to_argv(
        CurlRawArgs(
            url="http://h/login", method="POST", headers=["Content-Type: application/json"],
            data='{"u":"alice"}', insecure=True,
        )
    )
    assert "-X" in argv and "POST" in argv
    assert "-H" in argv
    assert '{"u":"alice"}' in argv
    assert "-k" in argv
    assert argv[-1] == "http://h/login"


@pytest.mark.parametrize(
    "tool_cls,args",
    [
        (SqlmapTool, SqlmapArgs(url="http://h")),
        (NmapTool, NmapArgs(target="h")),
        (FfufTool, FfufArgs(url="http://h/FUZZ", wordlist="/w")),
        (NucleiTool, NucleiArgs(target="http://h")),
        (HttpxTool, HttpxToolArgs(target="http://h")),
        (JwtTool, JwtToolArgs(token="t")),
        (CurlRawTool, CurlRawArgs(url="http://h")),
    ],
)
def test_each_tool_has_required_metadata(tool_cls, args):
    t = tool_cls()
    assert t.name
    assert t.description
    assert t.risk_level
    schema = t.json_schema()
    assert schema["type"] == "object"
    # args_to_argv runs without raising; result is a list of strings.
    argv = t.args_to_argv(args)
    assert isinstance(argv, list) and all(isinstance(s, str) for s in argv)
