from pathlib import Path

from zero.findings.dedup import dedup_hash
from zero.findings.schema import Finding, FindingRecord, Validation
from zero.findings.store import FindingsStore, new_finding_id


def _finding(**kw) -> Finding:
    base = dict(
        title="IDOR on /orders/{id}", summary="...", endpoint="/orders/1041",
        method="GET", cwe="CWE-639", evidence="GET /orders/1041 returns user B's data",
    )
    base.update(kw)
    return Finding(**base)


def _validation(**kw) -> Validation:
    base = dict(
        verdict="confirmed", rationale="reproduced with two accounts",
        reproducer="login as A; GET /orders/B-order-id; receive A→B's data",
        cvss40_vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N",
        cvss40_score=8.7, cwe="CWE-639",
    )
    base.update(kw)
    return Validation(**base)


def test_promote_writes_frontmatter(tmp_path: Path):
    s = FindingsStore(tmp_path)
    f = _finding()
    rec = FindingRecord(id=new_finding_id(), finding=f, validation=_validation(), dedup_hash=dedup_hash(f))
    path = s.promote(rec, "confirmed")
    assert path.exists()
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "## Validator verdict" in text
    assert "CVSS:4.0" in text


def test_index_rebuilds_from_disk(tmp_path: Path):
    s = FindingsStore(tmp_path)
    f = _finding()
    rec = FindingRecord(id=new_finding_id(), finding=f, validation=_validation(), dedup_hash=dedup_hash(f))
    s.promote(rec, "confirmed")
    # Nuke the index file: rebuild_index should recover it.
    s.index_path.unlink()
    idx = s.rebuild_index()
    assert rec.dedup_hash in idx
    assert idx[rec.dedup_hash]["verdict"] == "confirmed"


def test_promote_moves_between_buckets(tmp_path: Path):
    s = FindingsStore(tmp_path)
    f = _finding()
    rec = FindingRecord(id=new_finding_id(), finding=f, validation=None, dedup_hash=dedup_hash(f))
    s.submit_pending(rec)
    assert (tmp_path / "pending" / f"{rec.id}.md").exists()
    rec = FindingRecord(**{**rec.model_dump(), "validation": _validation()})
    s.promote(rec, "confirmed")
    # Old pending file is gone.
    assert not (tmp_path / "pending" / f"{rec.id}.md").exists()
    assert (tmp_path / "confirmed" / f"{rec.id}.md").exists()


def test_all_records_filter(tmp_path: Path):
    s = FindingsStore(tmp_path)
    for verdict in ("confirmed", "rejected", "pending"):
        f = _finding(endpoint=f"/x/{verdict}")
        v = _validation(verdict=verdict) if verdict != "pending" else None
        rec = FindingRecord(id=new_finding_id(), finding=f, validation=v, dedup_hash=dedup_hash(f))
        s.promote(rec, verdict)  # type: ignore[arg-type]
    assert len(s.all_records()) == 3
    assert len(s.all_records("confirmed")) == 1
