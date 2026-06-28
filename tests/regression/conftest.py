"""Fixtures for the capability regression suite: stand a vulnerable target up in
docker, hand the sandbox-facing URL to the test, tear it down, and collect a
end-of-run scorecard.

Scope note: this conftest lives under `tests/regression/` so it only applies to
this package — the offline unit suite is untouched.
"""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.error
import urllib.request

import pytest

from tests.regression.assertions import CoverageResult, TargetResult
from tests.regression.manifest import TargetSpec

# Module-globals so `pytest_terminal_summary` (which gets no fixtures) can read
# them. The matching fixtures hand tests these same lists.
_SCORECARD: list[TargetResult] = []          # strict suite (PASS/FAIL)
_BLIND_SCORECARD: list[CoverageResult] = []  # blind suite (coverage %)


# --------------------------------------------------------------------- helpers

def _have(binary: str) -> bool:
    return shutil.which(binary) is not None


def _image_exists(tag: str) -> bool:
    if not _have("docker"):
        return False
    # `docker images -q`, NOT `docker image inspect`: the latter flakes on a short tag
    # under Docker Desktop's containerd image store even when the image is present (it
    # wants the fully-qualified ref), spuriously skipping targets. `images -q` matches
    # how `docker run` resolves the name.
    out = subprocess.run(
        ["docker", "images", "-q", tag], capture_output=True, text=True
    )
    return out.returncode == 0 and bool(out.stdout.strip())


def _server_responds(url: str, ok_status: tuple[int, ...]) -> bool:
    """True if `url` returns an HTTP response. A 4xx/5xx still means the server
    is up (we accept any HTTP reply), so we treat HTTPError as 'up' too."""
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:  # noqa: S310 (localhost)
            return resp.status in ok_status or True
    except urllib.error.HTTPError:
        return True  # server answered (e.g. 500/404) — it's alive
    except (urllib.error.URLError, OSError):
        return False


def _wait_healthy(url: str, ok_status: tuple[int, ...], timeout_s: int) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _server_responds(url, ok_status):
            return True
        time.sleep(1.5)
    return False


def _record_skip(spec: TargetSpec, reason: str) -> None:
    _SCORECARD.append(
        TargetResult(
            name=spec.name, difficulty=spec.difficulty, skipped=True, skip_reason=reason
        )
    )


# -------------------------------------------------------------------- fixtures

@pytest.fixture
def regression_backend(request) -> str:
    """The LLM backend the run should use, from `--regression-backend` (default
    'claude'). Both the strict and blind suites read this so a whole regression run
    can be pointed at Codex with `pytest -m regression --regression-backend codex`."""
    return request.config.getoption("--regression-backend")


@pytest.fixture(scope="session")
def regression_scorecard() -> list[TargetResult]:
    """The shared list strict tests append their TargetResult to."""
    return _SCORECARD


@pytest.fixture(scope="session")
def regression_blind_scorecard() -> list[CoverageResult]:
    """The shared list blind tests append their CoverageResult to."""
    return _BLIND_SCORECARD


@pytest.fixture
def regression_target(request):
    """Build/pull a target image, run it detached, health-poll it, and yield the
    URL the SANDBOX should use to reach it. Skips (never fails) on any prereq or
    target-startup problem — only a healthy target that the agent fails to crack
    should redden the suite."""
    spec: TargetSpec = request.param
    if not _have("docker"):
        _record_skip(spec, "docker not available")
        pytest.skip("docker not available")

    # 1) Acquire the image.
    if spec.source == "build":
        if spec.build_context is None or not spec.build_context.exists():
            _record_skip(spec, f"build context missing: {spec.build_context}")
            pytest.skip(f"{spec.name}: build context missing ({spec.build_context})")
        if not _image_exists(spec.image):  # HTB images are slow — build once.
            build_cmd = ["docker", "build", "-t", spec.image]
            if spec.platform:
                build_cmd += ["--platform", spec.platform]
            build_cmd.append(str(spec.build_context))
            r = subprocess.run(
                build_cmd, capture_output=True, text=True, timeout=1800,
            )
            if r.returncode != 0:
                _record_skip(spec, "image build failed")
                pytest.skip(f"{spec.name}: docker build failed\n{r.stderr[-2000:]}")
    else:  # "pull"
        pull_cmd = ["docker", "pull"]
        if spec.platform:
            pull_cmd += ["--platform", spec.platform]
        pull_cmd.append(spec.image)
        r = subprocess.run(pull_cmd, capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            _record_skip(spec, "image pull failed")
            pytest.skip(f"{spec.name}: docker pull failed\n{r.stderr[-2000:]}")

    # 2) Run it detached.
    cname = f"zero-reg-{spec.name}"
    subprocess.run(["docker", "rm", "-f", cname], capture_output=True)  # clear stragglers
    run_cmd = ["docker", "run", "-d", "--name", cname]
    if spec.platform:
        run_cmd += ["--platform", spec.platform]
    if spec.host_network:
        run_cmd += ["--network", "host"]
    else:
        run_cmd += ["-p", f"{spec.published_port}:{spec.container_port}"]
    for key, val in spec.env:
        run_cmd += ["-e", f"{key}={val}"]
    run_cmd.append(spec.image)
    r = subprocess.run(run_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        _record_skip(spec, "docker run failed")
        pytest.skip(f"{spec.name}: docker run failed\n{r.stderr[-1000:]}")

    try:
        # 3) Health-poll host-side (proves the published port works).
        health_url = f"http://127.0.0.1:{spec.published_port}{spec.health_path}"
        if not _wait_healthy(health_url, spec.health_expect_status, spec.health_timeout_s):
            logs = subprocess.run(
                ["docker", "logs", cname], capture_output=True, text=True
            )
            _record_skip(spec, "never became healthy")
            pytest.skip(
                f"{spec.name}: never became healthy at {health_url}\n"
                f"{logs.stdout[-1500:]}\n{logs.stderr[-1500:]}"
            )
        # 4) URL the sandbox container uses to reach the target.
        if spec.host_network:
            sandbox_url = f"http://127.0.0.1:{spec.published_port}"
        else:
            sandbox_url = f"http://host.docker.internal:{spec.published_port}"
        yield sandbox_url
    finally:
        subprocess.run(["docker", "rm", "-f", cname], capture_output=True)


# ---------------------------------------------------------------- terminal summary

def pytest_terminal_summary(terminalreporter, exitstatus, config):
    tr = terminalreporter
    if _SCORECARD:
        tr.write_sep("=", "zero capability regression scorecard")
        by_diff: dict[str, list[int]] = {}
        confirmed = 0
        graded = 0
        for res in sorted(_SCORECARD, key=lambda r: (r.difficulty, r.name)):
            status = res.status()
            detail = res.skip_reason if res.skipped else res.explain()
            tr.write_line(f"  {res.difficulty:<7}{res.name:<14}{status:<5} {detail}")
            if res.skipped:
                continue
            graded += 1
            slot = by_diff.setdefault(res.difficulty, [0, 0])  # [passed, total]
            slot[1] += 1
            if res.passed:
                slot[0] += 1
                confirmed += 1
        tr.write_line("  " + "-" * 49)
        breakdown = "  ".join(f"{d} {p}/{t}" for d, (p, t) in sorted(by_diff.items()))
        tr.write_line(f"  confirmed {confirmed}/{graded}   ({breakdown})")

    if _BLIND_SCORECARD:
        tr.write_sep("=", "zero blind capability coverage")
        by_diff = {}
        tot_found = tot_vulns = 0
        for res in sorted(_BLIND_SCORECARD, key=lambda r: (r.difficulty, r.name)):
            tr.write_line(f"  {res.difficulty:<7}{res.summary()}")
            tot_found += len(res.found)
            tot_vulns += res.total
            slot = by_diff.setdefault(res.difficulty, [0, 0])  # [found, total]
            slot[0] += len(res.found)
            slot[1] += res.total
        tr.write_line("  " + "-" * 49)
        breakdown = "  ".join(f"{d} {f}/{t}" for d, (f, t) in sorted(by_diff.items()))
        pct = (100 * tot_found // tot_vulns) if tot_vulns else 0
        tr.write_line(f"  TOTAL {tot_found}/{tot_vulns} vulns found ({pct}%)   ({breakdown})")
