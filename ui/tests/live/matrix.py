"""Explicit serial product matrix. --list never calls a model or target.

Run with `uv run --project ui/server python ui/tests/live/matrix.py --help`.
Only --preflight and --run spend model tokens; neither is part of default tests.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tests.gruyere_fixture import GRUYERE_URL, PENTEST_MD_TEMPLATE  # noqa: E402
from zero_web.config import validate_config  # noqa: E402

MANIFEST = Path(__file__).with_name("codex-models.json")
PROFILES = {
    "P1": dict(
        yolo=False,
        auto_approve=[],
        i_understand=False,
        rate_limit="30/min",
        max_turns=10,
        pentester_max_turns=8,
        validator_max_turns=16,
        max_subagents=2,
        max_total_subagents=4,
        llm_timeout=180,
        subagent_deadline=420,
        thought_display="condensed",
        llm_log=True,
    ),
    "P2": dict(
        yolo=True,
        auto_approve=["exploit"],
        i_understand=True,
        rate_limit="30/min",
        max_turns=15,
        pentester_max_turns=10,
        validator_max_turns=24,
        max_subagents=3,
        max_total_subagents=6,
        llm_timeout=240,
        subagent_deadline=600,
        thought_display="full",
        llm_log=True,
    ),
    "P3": dict(
        yolo=True,
        auto_approve=["exploit"],
        i_understand=True,
        rate_limit="10/min",
        max_turns=8,
        pentester_max_turns=6,
        validator_max_turns=12,
        max_subagents=1,
        max_total_subagents=3,
        llm_timeout=180,
        subagent_deadline=420,
        thought_display="off",
        llm_log=False,
    ),
}
GOAL = (
    "Guided capability check for the authorized instance "
    + GRUYERE_URL
    + " only. Investigate reflected XSS via snippets.gtl uid using a benign marker. "
    "Submit concrete evidence and have an independent validator confirm or refute it. "
    "Do not access other instances or unrelated endpoints. No destructive actions. "
    "Finalize a bounded report after validation."
)


def expand(manifest, smoke=False):
    models = [""] + [m["id"] for m in manifest["models"] if m.get("declared_support")]
    cases = []
    for model in models:
        for mode in ("single", "orchestrated", "swarm", "ralph"):
            for profile, values in PROFILES.items():
                if smoke and (model or mode != "orchestrated" or profile != "P2"):
                    continue
                cfg = validate_config(
                    {
                        **values,
                        "backend": "codex",
                        "model": model,
                        "container": True,
                        "execution_mode": "a" if mode == "single" else "c",
                        "swarm_size": 2 if mode == "swarm" else 1,
                        "ralph": mode == "ralph",
                        "ralph_iterations": 2,
                        "ralph_approve_prd": True,
                        "goal": GOAL,
                    }
                )
                cases.append(
                    {
                        "id": f"{model or 'account-default'}-{mode}-{profile}",
                        "model": model,
                        "mode": mode,
                        "profile": profile,
                        "variant": "baseline",
                        "config": cfg,
                    }
                )
                if mode == "ralph" and not smoke:
                    for epics, checkpoint in ((False, True), (True, False), (False, False)):
                        cases.append(
                            {
                                **cases[-1],
                                "id": f"{model or 'account-default'}-{mode}-{profile}-epics{int(epics)}-checkpoint{int(checkpoint)}",
                                "variant": "ralph-planning",
                                "config": {
                                    **cfg,
                                    "ralph_epics": epics,
                                    "ralph_approve_prd": checkpoint,
                                },
                            }
                        )
    return cases


def known_confirmed(records):
    """Capability is a semantic validator verdict, never merely a finding file."""
    return [
        r
        for r in records
        if r.get("verdict") == "confirmed"
        and r.get("validation")
        and (r["validation"].get("cwe") or r["finding"].get("cwe")) == "CWE-79"
        and "snippets" in r["finding"].get("endpoint", "")
        and r["validation"].get("rationale", "").strip()
        and r["validation"].get("reproducer", "").strip()
        and "uid"
        in (r["finding"].get("evidence", "") + r["validation"].get("reproducer", "")).lower()
    ]


def off_prefix_destinations(events):
    """Audit visible URL destinations; this is detection, not a global scope gate."""
    outside = []
    for e in events:
        p = e.get("payload", {})
        if e.get("kind") not in {"tool_proposal", "approval_prompt"}:
            continue
        for key in ("url", "target"):
            value = p.get("args", {}).get(key)
            if (
                isinstance(value, str)
                and urlsplit(value).scheme in {"http", "https"}
                and not value.startswith(GRUYERE_URL)
            ):
                outside.append({"seq": e["seq"], "tool": p.get("tool"), "url": value})
    return outside


def discover(manifest):
    from zero.llm.codex_cli import codex_configured_model, _codex_home

    model = codex_configured_model()
    try:
        cache = json.loads((_codex_home() / "models_cache.json").read_text())
    except (OSError, ValueError):
        cache = {}
    hints = sorted(
        {
            m.get("slug") or m.get("id")
            for m in cache.get("models", [])
            if isinstance(m, dict) and (m.get("slug") or m.get("id"))
        }
        | ({model} if model else set())
    )
    manifest["discovery"] = {
        "date": datetime.now(timezone.utc).isoformat(),
        "configured_model": model,
        "cache_hints": hints,
        "entitlement_verified": False,
    }
    return manifest


async def verify_models(manifest):
    from zero.llm.backend import Message
    from zero.llm.codex_cli import CodexCLIBackend, CodexCLIConfig, codex_effective_model

    for model in [""] + [m["id"] for m in manifest["models"] if m.get("declared_support")]:
        backend = CodexCLIBackend(CodexCLIConfig(model=model or None, timeout_seconds=90))
        await backend.run(
            'Return exactly {"type":"final","answer":"accepted"}. Do not use tools.',
            [Message(role="user", content="Verify model selection only.")],
            [],
        )
        print(
            json.dumps(
                {
                    "model": model or "account-default",
                    "resolved_model": codex_effective_model(model),
                    "accepted_at": datetime.now(timezone.utc).isoformat(),
                }
            )
        )
    manifest["availability_verified_at"] = datetime.now(timezone.utc).isoformat()
    manifest["account_default"]["verified_at"] = manifest["availability_verified_at"]
    for m in manifest["models"]:
        if m.get("declared_support"):
            m["verified_at"] = manifest["availability_verified_at"]
    return manifest


def run(cases, manifest, output, deadline):
    output.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    env = {k: v for k, v in os.environ.items() if k != "ZERO_WEB_TEST_MODE"}
    server = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "zero_web.app",
            "--port",
            "4330",
            "--data-root",
            str(output / "workspace"),
        ],
        env=env,
    )
    results = []
    try:
        with httpx.Client(base_url="http://127.0.0.1:4330", timeout=20) as client:
            for _ in range(100):
                try:
                    if client.get("/api/v1/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    time.sleep(0.1)
            target = client.get(GRUYERE_URL, follow_redirects=False)
            if target.status_code != 200:
                raise RuntimeError(
                    f"Exact hosted instance preflight failed: HTTP {target.status_code}"
                )
            defect = client.get(
                GRUYERE_URL + "snippets.gtl",
                params={"uid": "ZERO_UI_CAPABILITY_MARKER"},
                follow_redirects=False,
            )
            if defect.status_code != 200 or "ZERO_UI_CAPABILITY_MARKER" not in defect.text:
                raise RuntimeError(
                    "Known defect path is not currently reproducible; capability preflight inconclusive"
                )
            for case in cases:
                remaining = deadline - (time.monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError("Overall matrix deadline reached")
                folder = output / case["id"]
                folder.mkdir()
                case = {
                    **case,
                    "target": GRUYERE_URL,
                    "context": PENTEST_MD_TEMPLATE.format(gruyere_url=GRUYERE_URL),
                    "deadline_seconds": min(2400, remaining),
                    "origin": "http://127.0.0.1:4330",
                    "output": str(folder.resolve()),
                }
                input_path = folder / "case.json"
                input_path.write_text(json.dumps(case, indent=2))
                result = {
                    "case": case["id"],
                    "plumbing": "failed",
                    "compatibility": "preflight accepted",
                    "capability": "not confirmed",
                }
                try:
                    subprocess.run(
                        ["node", str(ROOT / "ui/web/live-harness.mjs"), str(input_path.resolve())],
                        check=True,
                        timeout=remaining,
                        cwd=ROOT,
                        env=env,
                    )
                    evidence = json.loads((folder / "result.json").read_text())
                    sid, eid = evidence["session_id"], evidence["engagement_id"]
                    snapshot = client.get(f"/api/v1/sessions/{sid}/snapshot").json()
                    findings = client.get(
                        f"/api/v1/engagements/{eid}/findings?session_id={sid}"
                    ).json()
                    assert snapshot["config"] == case["config"], (
                        "Selected/effective requested configuration differs"
                    )
                    assert snapshot["snapshot"]["effective"]["execution_location"] == "sandbox", (
                        "Docker did not start"
                    )
                    if case["model"]:
                        assert snapshot["snapshot"]["effective"]["model"] == case["model"], (
                            "Explicit model mismatch"
                        )
                    records = evidence["events"]
                    outside = off_prefix_destinations(records)
                    (folder / "off-prefix.json").write_text(json.dumps(outside, indent=2))
                    assert not outside, (
                        "Observed off-prefix HTTP tool destinations; review scope evidence"
                    )
                    assert any(e["kind"] == "tool_observation" for e in records), (
                        "No tool execution"
                    )
                    if case["mode"] == "swarm":
                        assert any(
                            e["kind"] == "swarm_phase" and e["payload"]["phase"] == "bootstrap"
                            for e in records
                        )
                    if case["mode"] == "ralph":
                        assert any(
                            e["kind"] == "ralph_iteration" and e["payload"]["phase"] == "iteration"
                            for e in records
                        )
                    confirmed = known_confirmed(findings)
                    result.update(
                        plumbing="passed", capability="confirmed" if confirmed else "inconclusive"
                    )
                    if case["profile"] == "P2":
                        assert confirmed, "P2 requires an independently confirmed known finding"
                    (folder / "authoritative.json").write_text(
                        json.dumps({"snapshot": snapshot, "findings": findings}, indent=2)
                    )
                except Exception as exc:
                    result["error"] = f"{type(exc).__name__}: {exc}"
                results.append(result)
                (output / "results.json").write_text(
                    json.dumps({"inventory": manifest, "cases": results}, indent=2)
                )
    finally:
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--discover", action="store_true")
    parser.add_argument(
        "--preflight",
        action="store_true",
        help="SPENDS TOKENS: verify every declared model through Codex",
    )
    parser.add_argument(
        "--run",
        action="store_true",
        help="SPENDS TOKENS: execute serial browser/API cases against the scoped lab",
    )
    parser.add_argument(
        "--smoke",
        action="store_true",
        help="One account-default orchestrated P2 case; incomplete release coverage",
    )
    parser.add_argument("--overall-deadline", type=int, default=14400)
    parser.add_argument("--output", type=Path, default=ROOT / "ui/.local/live-evidence")
    args = parser.parse_args()
    manifest = json.loads(args.manifest.read_text())
    if args.discover:
        manifest = discover(manifest)
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    cases = expand(manifest, args.smoke)
    print(
        json.dumps(
            {
                "case_count": len(cases),
                "coverage": "development smoke"
                if args.smoke
                else "release baseline + Ralph variants",
                "cases": cases,
            },
            indent=2,
        )
    )
    if args.preflight:
        manifest = asyncio.run(verify_models(manifest))
        args.manifest.write_text(json.dumps(manifest, indent=2) + "\n")
    if args.run:
        if not args.smoke and not manifest.get("release_inventory_complete"):
            parser.error(
                "Complete the supported/excluded model inventory before a release matrix. --smoke remains explicitly incomplete."
            )
        if not manifest.get("availability_verified_at"):
            parser.error("Run explicit --preflight before spending on the matrix")
        result = run(cases, manifest, args.output, args.overall_deadline)
        if len(result) != len(cases) or any(
            r["plumbing"] != "passed" or "error" in r for r in result
        ):
            raise SystemExit(1)


if __name__ == "__main__":
    main()
