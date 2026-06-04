from zero.tools.python_exec import PythonExecArgs, PythonExecTool


async def test_runs_code():
    r = await PythonExecTool().run(PythonExecArgs(code="print(1+2)"))
    assert r.ok
    assert "3" in r.output


async def test_timeout():
    r = await PythonExecTool().run(
        PythonExecArgs(code="import time; time.sleep(5)", timeout_seconds=1)
    )
    assert r.ok is False
    assert "timeout" in r.output


async def test_captures_stderr_on_failure():
    r = await PythonExecTool().run(PythonExecArgs(code="raise ValueError('boom')"))
    assert r.ok is False
    assert "boom" in r.output
