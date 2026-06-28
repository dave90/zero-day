"""The BountyBench pilot manifest: a small set of web-shaped bounties to prove the
harness end-to-end before scaling up.

Each entry names a BountyBench `bountytasks/<system>` directory, a bounty number, and
the task type to run (`exploit` or `detect`). Keep this small and cheap — every entry is
a live, token-spending `zero` engagement.

The systems/bounties below are starting candidates (web apps, OWASP-Top-10 classes,
lower difficulty). Validate each is present and pullable in your `bountytasks` checkout
before a real run — `list_available()` filters to those that exist on disk.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Bounty:
    system: str          # bountytasks/<system>
    bounty_number: int
    task_type: str       # "exploit" | "detect"
    note: str = ""

    @property
    def task_dir(self) -> str:
        return f"bountytasks/{self.system}"

    @property
    def workflow_type(self) -> str:
        return f"zero_{self.task_type}_workflow"

    @property
    def label(self) -> str:
        return f"{self.system}#{self.bounty_number}/{self.task_type}"


# Pilot set — start with Exploit (the scorer ships an authoritative verify.sh), add
# Detect once the exploit path is proven. Trim/extend to taste; 3–5 is the sweet spot.
PILOT: list[Bounty] = [
    Bounty("lunary", 0, "exploit", note="IDOR / CWE-639 (authz bypass via user-controlled key)"),
    Bounty("lunary", 0, "detect", note="same bug, harder self-verifying-exploit task"),
    Bounty("gunicorn", 0, "exploit", note="HTTP request smuggling class"),
    Bounty("mlflow", 0, "exploit", note="path traversal / arbitrary file access class"),
]


def bounty_metadata_path(bountybench_home: Path, b: Bounty) -> Path:
    """Path to a bounty's metadata JSON (holds CWE + the dollar reward)."""
    return (
        bountybench_home / "bountytasks" / b.system / "bounties"
        / f"bounty_{b.bounty_number}" / "bounty_metadata.json"
    )


def _detect_supported(bountybench_home: Path, b: Bounty) -> bool:
    """BB's DetectPhase hard-raises if the bounty metadata lacks a non-empty
    compatible list — so a Detect run on such a bounty crashes at setup (0 tokens)
    and would otherwise score as a spurious $-losing fail. Upstream reads
    `compatible_exploits`; the public task metadata in this checkout usually ships the
    same patch list as `compatible_patches`, which the zero adapter maps for Detect."""
    try:
        meta = json.loads(bounty_metadata_path(bountybench_home, b).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return bool(meta.get("compatible_exploits") or meta.get("compatible_patches"))


def list_available(bountybench_home: Path, bounties: list[Bounty] | None = None) -> list[Bounty]:
    """Filter the manifest to bounties actually RUNNABLE in the checkout: present on
    disk, and — for Detect tasks — with the `compatible_exploits` BB's scorer requires."""
    items = bounties if bounties is not None else PILOT
    out = []
    for b in items:
        if not bounty_metadata_path(bountybench_home, b).is_file():
            continue
        if b.task_type == "detect" and not _detect_supported(bountybench_home, b):
            print(f"[pilot] skipping {b.label}: bounty has no compatible exploit/patch "
                  f"list in metadata (BountyBench's Detect scorer requires one)")
            continue
        out.append(b)
    return out
