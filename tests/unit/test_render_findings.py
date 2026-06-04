"""Finding card + session summary rendering."""
from zero.ui.events import Finding, SessionSummary
from zero.ui.render import render_event


def test_finding_card_includes_all_fields():
    ev = Finding(
        verdict="confirmed",
        finding_id="a1b2c3d",
        title="IDOR on /api/orders/{id}",
        endpoint="/api/orders/{id}",
        method="GET",
        cwe="CWE-639",
        cvss_score=7.5,
        cvss_vector="CVSS:4.0/AV:N/AC:L/AT:N/PR:L/UI:N/VC:H/VI:N/VA:N/SC:N/SI:N/SA:N",
        rationale="Reproduced with two accounts; ownership check missing.",
        reproducer="1) login as alice\n2) GET /api/orders/<bob-id>\n3) observe bob's data",
        file_path="/work/.pentest/findings/confirmed/a1b2c3d.md",
    )
    text = "\n".join(render_event(ev))
    # Every field surfaces.
    assert "CONFIRMED" in text
    assert "IDOR on /api/orders/{id}" in text
    assert "a1b2c3d" in text
    assert "GET /api/orders/{id}" in text
    assert "CWE-639" in text
    assert "7.5" in text
    assert "CVSS:4.0/" in text
    assert "ownership check missing" in text
    assert "GET /api/orders/<bob-id>" in text
    assert "/work/.pentest/findings/confirmed/a1b2c3d.md" in text


def test_finding_card_rejected_label():
    ev = Finding(verdict="rejected", finding_id="x", title="bogus")
    text = "\n".join(render_event(ev))
    assert "REJECTED" in text


def test_finding_card_duplicate_includes_pointer():
    ev = Finding(
        verdict="duplicate",
        title="same as before",
        duplicate_of="prev-id-123",
    )
    text = "\n".join(render_event(ev))
    assert "DUPLICATE" in text
    assert "prev-id-123" in text


def test_session_summary_lists_each_bucket():
    summary = SessionSummary(
        confirmed=[
            {"id": "c1", "title": "Reflected XSS", "cvss_score": 6.1},
            {"id": "c2", "title": "Path traversal", "cvss_score": 8.6},
        ],
        rejected=[{"id": "r1", "title": "Not really a bug"}],
        pending=[],
        findings_dir="/work/.pentest/findings",
        audit_path="/work/.pentest/audit.jsonl",
        turns=42,
    )
    text = "\n".join(render_event(summary))
    assert "2 confirmed · 1 rejected · 0 pending" in text
    assert "42 agent turns" in text
    assert "Reflected XSS" in text and "[c1]" in text and "6.1" in text
    assert "Path traversal" in text and "[c2]" in text and "8.6" in text
    assert "Not really a bug" in text and "[r1]" in text
    assert "/work/.pentest/findings" in text
    assert "/work/.pentest/audit.jsonl" in text


def test_session_summary_empty():
    summary = SessionSummary()
    text = "\n".join(render_event(summary))
    assert "0 confirmed · 0 rejected · 0 pending" in text
    assert "(no findings this session)" in text
