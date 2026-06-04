from pathlib import Path

from zero.orchestrator.audit import AuditLog, read_audit


def test_audit_round_trip(tmp_path: Path):
    log = AuditLog(tmp_path / "a.jsonl")
    log.write("foo", a=1, b="x")
    log.write("bar", obj={"k": [1, 2]})
    log.close()

    rows = read_audit(tmp_path / "a.jsonl")
    assert [r["kind"] for r in rows] == ["foo", "bar"]
    assert rows[0]["a"] == 1
    assert rows[0]["b"] == "x"
    assert rows[1]["obj"] == {"k": [1, 2]}
    for r in rows:
        assert "ts" in r


def test_audit_appends_after_partial_line(tmp_path: Path):
    p = tmp_path / "a.jsonl"
    # write a valid line + a partial line by hand
    p.write_text('{"ts":1,"kind":"x"}\n{"ts":2,"kind":"oops","x":', encoding="utf-8")
    # opening with AuditLog must not erase prior content
    log = AuditLog(p)
    log.write("next", v=1)
    log.close()
    rows = read_audit(p)
    # The partial line is skipped (json decode fail); two clean lines remain
    assert [r["kind"] for r in rows] == ["x", "next"]


def test_read_missing_file(tmp_path: Path):
    assert read_audit(tmp_path / "nope.jsonl") == []
