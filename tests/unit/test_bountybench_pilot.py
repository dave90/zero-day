"""Offline, free tests for the BountyBench pilot manifest filtering (`pilot.py`)."""
from __future__ import annotations

import json

from benchmarks.bountybench.pilot import Bounty, list_available


def _seed_bounty(root, system, number, *, compatible_exploits=None, compatible_patches=None):
    """Write a minimal bounty_metadata.json under a fake BB checkout."""
    bdir = root / "bountytasks" / system / "bounties" / f"bounty_{number}"
    bdir.mkdir(parents=True, exist_ok=True)
    meta = {"disclosure_bounty": 100}
    if compatible_exploits is not None:
        meta["compatible_exploits"] = compatible_exploits
    if compatible_patches is not None:
        meta["compatible_patches"] = compatible_patches
    (bdir / "bounty_metadata.json").write_text(json.dumps(meta), encoding="utf-8")


def test_list_available_skips_detect_without_compatible_exploits(tmp_path, capsys):
    # Regression: BB's DetectPhase hard-raises without `compatible_exploits`, so a Detect
    # run on such a bounty crashes at setup and looks like a $-losing fail. It must be
    # filtered out (with a note), while the Exploit task on the same bounty stays.
    _seed_bounty(tmp_path, "lunary", 0)  # no compatible_exploits
    bounties = [Bounty("lunary", 0, "exploit"), Bounty("lunary", 0, "detect")]
    avail = list_available(tmp_path, bounties)
    assert [b.label for b in avail] == ["lunary#0/exploit"]
    assert "skipping lunary#0/detect" in capsys.readouterr().out


def test_list_available_keeps_detect_with_compatible_exploits(tmp_path):
    _seed_bounty(tmp_path, "mlflow", 0, compatible_exploits=["0", "1"])
    avail = list_available(tmp_path, [Bounty("mlflow", 0, "detect")])
    assert [b.label for b in avail] == ["mlflow#0/detect"]


def test_list_available_keeps_detect_with_compatible_patches(tmp_path):
    # Public BountyBench metadata currently ships this field instead of
    # compatible_exploits; the zero adapter maps it for Detect scoring.
    _seed_bounty(tmp_path, "lunary", 0, compatible_patches=["0", "1"])
    avail = list_available(tmp_path, [Bounty("lunary", 0, "detect")])
    assert [b.label for b in avail] == ["lunary#0/detect"]


def test_list_available_drops_bounties_missing_on_disk(tmp_path):
    _seed_bounty(tmp_path, "lunary", 0)
    avail = list_available(tmp_path, [Bounty("lunary", 0, "exploit"),
                                      Bounty("ghost", 9, "exploit")])
    assert [b.label for b in avail] == ["lunary#0/exploit"]
