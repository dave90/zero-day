"""Offline tests for the Ralph PRD models + tolerant parsers."""
from __future__ import annotations

import json

from zero.ralph.prd import (
    PRD,
    Epic,
    PRDTask,
    load_prd,
    parse_critique,
    parse_epics,
    parse_prd,
    parse_task_verdicts,
    save_prd,
)


def test_parse_prd_full_object():
    text = json.dumps(
        {
            "goal": "find auth bugs",
            "tasks": [
                {"id": "t1", "priority": 1, "title": "IDOR on /orders",
                 "acceptanceCriteria": "all order ids probed cross-user"},
                {"id": "t2", "priority": 2, "title": "XSS in search"},
            ],
        }
    )
    prd = parse_prd(text)
    assert prd.goal == "find auth bugs"
    assert [t.id for t in prd.tasks] == ["t1", "t2"]
    assert prd.tasks[0].status == "pending"


def test_parse_prd_tolerant_bare_list_and_fences_and_ids():
    # Bare list, wrapped in a code fence, with a missing id and a string priority.
    text = "```json\n" + json.dumps(
        [
            {"title": "no id task", "priority": "high"},
            {"id": "x", "title": "has id", "priority": 2, "status": "done"},
        ]
    ) + "\n```"
    prd = parse_prd(text, goal="g")
    assert prd.goal == "g"
    assert prd.tasks[0].id == "t1"          # auto-assigned
    assert prd.tasks[0].priority == 2       # "high" -> 2 on the critical=1..low=4 ladder
    assert prd.tasks[1].status == "completed"  # "done" normalized


def test_parse_prd_garbage_yields_empty():
    prd = parse_prd("the model rambled with no json at all")
    assert prd.tasks == []


def test_parse_prd_wrapped_under_prd_key_with_p_priorities():
    # Regression: the analyzer nested the PRD under a "prd" wrapper, used "P0"-style
    # priorities, and put the body under "approach"/"acceptanceCriteria". The parser
    # must descend into the wrapper and still find the tasks.
    text = json.dumps({
        "prd": {
            "goal": "find all the vulnerabilities",
            "priority_order": ["recon", "xss", "ssti"],   # string list — NOT tasks
            "tasks": [
                {"id": "R1", "title": "Recon", "priority": "P0",
                 "approach": "spider the app", "acceptanceCriteria": "all routes hit"},
                {"id": "X1", "title": "Stored XSS", "priority": "P1"},
            ],
        }
    })
    prd = parse_prd(text)
    assert prd.goal == "find all the vulnerabilities"
    assert [t.id for t in prd.tasks] == ["R1", "X1"]
    assert prd.tasks[0].priority == 0                 # "P0" → 0
    assert prd.tasks[1].priority == 1                 # "P1" → 1
    assert prd.tasks[0].description == "spider the app"  # from "approach"


def test_parse_prd_list_valued_acceptance_criteria_not_dropped():
    # Regression (evoting phase-1 loop): the analyzer emitted every task with
    # `acceptanceCriteria` as a LIST of bullet strings (plus "P0" priority and extra
    # fields like attack_class/objective). The parser dropped ALL such tasks (PRDTask
    # rejects a list for a str field), yielding an empty PRD the validator rejected
    # forever. A list body must be flattened, not dropped.
    text = json.dumps({
        "goal": "break the e-voting system",
        "tasks": [
            {"id": "T1", "title": "return-code bypass", "priority": "P0",
             "attack_class": "client integrity", "objective": "individual-verifiability",
             "description": ["trace the casting sequence", "enumerate server checks"],
             "acceptanceCriteria": [
                 "full casting sequence traced end-to-end",
                 "every acceptance check tested for a bypass",
             ]},
            {"id": "T2", "title": "mixnet integrity", "priority": "P1",
             "acceptanceCriteria": ["every verification equation checked"]},
        ],
    })
    prd = parse_prd(text)
    assert [t.id for t in prd.tasks] == ["T1", "T2"]      # neither dropped
    assert prd.tasks[0].priority == 0
    assert "full casting sequence" in prd.tasks[0].acceptanceCriteria
    assert "every acceptance check" in prd.tasks[0].acceptanceCriteria
    assert "trace the casting sequence" in prd.tasks[0].description
    # flattened to a bullet list (not a stringified python list)
    assert "['" not in prd.tasks[0].acceptanceCriteria


def test_parse_prd_wrapped_targets_key_with_severity_priority():
    # First analyzer attempt used "targets" (not "tasks") + severity words + "success".
    text = json.dumps({
        "prd": {
            "engagement": "Gruyere assessment",
            "targets": [
                {"id": "T1", "title": "SSTI", "priority": "critical",
                 "success": "reproduce disclosure with a fresh marker"},
            ],
        }
    })
    prd = parse_prd(text)
    assert prd.goal == "Gruyere assessment"           # from "engagement"
    assert prd.tasks[0].id == "T1"
    assert prd.tasks[0].priority == 1                 # "critical" → 1
    assert prd.tasks[0].acceptanceCriteria == "reproduce disclosure with a fresh marker"


def test_parse_prd_ignores_string_lists_when_finding_tasks():
    # A wrapper whose only list is strings must yield no tasks (not treat it as tasks).
    text = json.dumps({"prd": {"goal": "g", "priority_order": ["a", "b", "c"]}})
    assert parse_prd(text).tasks == []


def test_incomplete_sorted_by_priority_and_all_done():
    prd = PRD(
        tasks=[
            PRDTask(id="a", priority=3, status="pending"),
            PRDTask(id="b", priority=1, status="completed"),
            PRDTask(id="c", priority=2, status="pending"),
        ]
    )
    assert [t.id for t in prd.incomplete()] == ["c", "a"]
    assert not prd.all_done()
    for t in prd.tasks:
        t.status = "completed"
    assert prd.all_done()


def test_parse_critique_json_and_prose():
    j = parse_critique(json.dumps({"approved": True, "feedback": "", "missing": []}))
    assert j.approved is True
    no = parse_critique(json.dumps({"approved": False, "feedback": "add SSRF", "missing": ["SSRF"]}))
    assert no.approved is False and no.missing == ["SSRF"]
    prose_ok = parse_critique("Looks complete. APPROVED.")
    assert prose_ok.approved is True
    prose_no = parse_critique("This is NOT approved — missing SQLi coverage.")
    assert prose_no.approved is False


def test_parse_task_verdicts_tolerant():
    v = parse_task_verdicts(json.dumps({"verdicts": [
        {"task_id": "t1", "completed": True, "reason": "covered"},
        {"task_id": "t2", "completed": "no", "reason": "untouched"},
    ]}))
    assert {x.task_id: x.completed for x in v} == {"t1": True, "t2": False}
    # bare list + id alias
    v2 = parse_task_verdicts(json.dumps([{"id": "t3", "completed": "yes"}]))
    assert v2[0].task_id == "t3" and v2[0].completed is True
    assert parse_task_verdicts("no json here") == []


def test_save_and_load_roundtrip(tmp_path):
    prd = PRD(goal="g", tasks=[PRDTask(id="t1", title="x", priority=1)])
    p = tmp_path / ".ralph" / "prd.json"
    save_prd(p, prd)
    loaded = load_prd(p)
    assert loaded is not None
    assert loaded.goal == "g" and loaded.tasks[0].id == "t1"
    assert load_prd(tmp_path / "missing.json") is None


# ------------------------------------------------------------------- epics (Job A)

def test_parse_epics_full_and_wrapper_keys():
    # Bare {"goal","epics":[...]} and a wrapped {"plan":{"areas":[...]}} both resolve,
    # with auto-ids and coerced priorities.
    bare = parse_epics(json.dumps({"goal": "g", "epics": [
        {"id": "e1", "priority": "P1", "title": "Uploads", "acceptanceCriteria": "all upload endpoints"},
        {"title": "Authz", "priority": "high"},   # no id → auto e2; "high" → 2
    ]}))
    assert [e.id for e in bare] == ["e1", "e2"]
    assert bare[0].priority == 1 and bare[1].priority == 2
    assert bare[1].title == "Authz"
    # wrapper key "areas" under a "plan" wrapper
    wrapped = parse_epics(json.dumps({"plan": {"areas": [{"id": "a1", "title": "Tally"}]}}))
    assert [e.id for e in wrapped] == ["a1"]


def test_parse_epics_list_valued_acceptance_not_dropped():
    # Same regression class as tasks: a list-valued acceptanceCriteria must flatten to
    # bullets, not drop the epic.
    epics = parse_epics(json.dumps({"epics": [
        {"id": "e1", "title": "Uploads", "acceptanceCriteria": [
            "every upload endpoint checked for signature verification",
            "authz enforced before persistence",
        ]},
    ]}))
    assert [e.id for e in epics] == ["e1"]
    assert "signature verification" in epics[0].acceptanceCriteria
    assert "authz enforced" in epics[0].acceptanceCriteria
    assert "['" not in epics[0].acceptanceCriteria       # flattened, not a python list repr


def test_parse_epics_malformed_skipped_and_garbage_empty():
    epics = parse_epics(json.dumps({"epics": [
        {"id": "e1", "title": "ok"},
        ["not", "an", "epic"],                 # non-dict entry skipped
        {"id": "e2", "title": "also ok"},
    ]}))
    assert [e.id for e in epics] == ["e1", "e2"]
    assert parse_epics("the model wrote prose, no json") == []
    # a wrapper whose only list is strings is NOT mistaken for epics
    assert parse_epics(json.dumps({"plan": {"priority_order": ["a", "b"]}})) == []


def test_parse_prd_captures_epic_id_aliases():
    text = json.dumps({"tasks": [
        {"id": "t1", "title": "a", "epic_id": "e1"},
        {"id": "t2", "title": "b", "epicId": "e2"},
        {"id": "t3", "title": "c", "epic": "e3"},
        {"id": "t4", "title": "d"},                 # none → empty
    ]})
    prd = parse_prd(text)
    assert [t.epic_id for t in prd.tasks] == ["e1", "e2", "e3", ""]


def test_epic_roundtrip_and_progress_helpers():
    prd = PRD(goal="g",
              epics=[Epic(id="e1", title="Uploads", priority=1),
                     Epic(id="e2", title="Tally", priority=2)],
              tasks=[PRDTask(id="t1", epic_id="e1", status="completed"),
                     PRDTask(id="t2", epic_id="e1", status="pending"),
                     PRDTask(id="t3", epic_id="e2", status="pending")])
    assert prd.epic_progress("e1") == (1, 2)
    assert [t.id for t in prd.tasks_for_epic("e2")] == ["t3"]


def test_epic_bearing_prd_save_load_roundtrip(tmp_path):
    prd = PRD(goal="g", epics=[Epic(id="e1", title="Uploads", acceptanceCriteria="all uploads")],
              tasks=[PRDTask(id="t1", epic_id="e1", title="x")])
    p = tmp_path / ".ralph" / "prd.json"
    save_prd(p, prd)
    loaded = load_prd(p)
    assert loaded is not None
    assert [e.id for e in loaded.epics] == ["e1"]
    assert loaded.tasks[0].epic_id == "e1"


def test_load_legacy_epicless_prd_is_backward_compatible(tmp_path):
    # A pre-feature prd.json (no `epics`, no per-task `epic_id`) must load unchanged.
    p = tmp_path / ".ralph" / "prd.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "goal": "legacy run",
        "tasks": [{"id": "t1", "priority": 1, "title": "old task", "status": "pending"}],
    }), encoding="utf-8")
    loaded = load_prd(p)
    assert loaded is not None
    assert loaded.epics == []
    assert loaded.tasks[0].epic_id == ""
    assert loaded.incomplete()[0].id == "t1"       # existing paths still work
