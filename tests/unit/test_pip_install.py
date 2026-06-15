"""pip_install argument sanitization (offline — the reject paths return before
pip is ever spawned, so no network). The accept/functional path is covered by
the docker test (test_tools_in_sandbox.py)."""
from zero.tools.pip_install import PipInstallArgs, PipInstallTool


async def test_rejects_option_injection():
    """A 'package' that is actually a pip flag (option injection) must be refused
    — otherwise an agent (or a confused model) could pass --index-url to a
    malicious mirror, etc."""
    t = PipInstallTool()
    r = await t.run(PipInstallArgs(packages=["--index-url=http://evil/", "requests"]))
    assert r.ok is False
    assert "unsafe" in r.output


async def test_rejects_leading_dash_and_metachars():
    t = PipInstallTool()
    for spec in (["-rrequirements.txt"], ["foo; rm -rf /"], ["$(touch x)"]):
        r = await t.run(PipInstallArgs(packages=spec))
        assert r.ok is False, spec
        assert "unsafe" in r.output


async def test_rejects_empty():
    t = PipInstallTool()
    r = await t.run(PipInstallArgs(packages=["   "]))
    assert r.ok is False
    assert "no packages" in r.output


async def test_rejects_too_many():
    t = PipInstallTool()
    r = await t.run(PipInstallArgs(packages=[f"pkg{i}" for i in range(50)]))
    assert r.ok is False
    assert "too many" in r.output


def test_normal_specs_pass_sanitization():
    """Valid names/extras/version pins match the allowlist regex (so they reach
    pip); injection-y ones don't."""
    from zero.tools.pip_install import _SPEC_RE

    for good in ("pdfminer.six", "pypdf==4.2.0", "requests>=2", "foo[extra]", "PyJWT"):
        assert _SPEC_RE.match(good), good
    for bad in ("--index-url=x", "-r req.txt", "; ls", "&&id"):
        assert not _SPEC_RE.match(bad), bad
