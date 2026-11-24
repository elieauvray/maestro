from __future__ import annotations

import html
import json
from xml.sax.saxutils import quoteattr

import pytest
from sqlalchemy import func, select

from app.db import SessionLocal
from app.maestro import service
from app.maestro.bpmn import MAX_BYTES, BpmnImportError, bpmn_to_design, parse_bpmn, steps_from_bpmn, steps_hash
from app.models import Workflow
from tests.conftest import csrf, login, make_user
from tests.test_maestro import _connect, _design, _setup

NS = 'xmlns:bpmn="http://www.omg.org/spec/BPMN/20100524/MODEL" xmlns:maestro="https://upsun.com/schema/maestro/1"'


# --- Fixtures -----------------------------------------------------------------------------------


def _bpmn(nodes: list[str], flows: list[tuple[str, str, str]], process: str = "Process_7", name: str = "Process",
          extension: str = "", collaboration: str = "") -> str:
    seq = "".join(f'<bpmn:sequenceFlow id="Flow_{i}" sourceRef="{s}" targetRef="{t}" name="{n}"/>'
                  for i, (s, t, n) in enumerate(flows))
    return (f'<?xml version="1.0" encoding="UTF-8"?><bpmn:definitions {NS} id="Defs_1">{collaboration}'
            f'<bpmn:process id="{process}" name="{name}" isExecutable="false">{extension}'
            f'{"".join(nodes)}{seq}</bpmn:process></bpmn:definitions>')


def _task(step: dict) -> str:
    tag = {"human": "userTask", "maestro": "serviceTask", "dispatch": "callActivity", "external": "task",
           "upsun": "serviceTask"}[step["actor"]]
    attrs = {"maestro:actor": step["actor"], "maestro:phase": step["phase"], "maestro:gateOwner": step["gate_owner"],
             "maestro:repeat": step["repeat"], "maestro:doneWhen": step["done_when"]}
    if step["dispatch_workflow"] != "none":
        attrs["maestro:dispatchWorkflow"] = step["dispatch_workflow"]
        attrs["calledElement"] = step["dispatch_workflow"]
    attr_text = " ".join(f"{k}={quoteattr(v)}" for k, v in attrs.items() if v and v != "none")
    return (f'<bpmn:{tag} id="{step["id"]}" name={quoteattr(step["title"])} {attr_text}>'
            f'<bpmn:documentation>{step["detail"]}\nExtra readable line</bpmn:documentation></bpmn:{tag}>')


def _step(sid: str, actor: str = "maestro", **kw: str) -> dict:
    base = {"id": sid, "phase": "Build", "title": f"Step {sid}", "detail": f"Detail of {sid}", "actor": actor,
            "dispatch_workflow": "none", "gate_owner": "", "repeat": "", "loop": "", "branch": "", "loop_to": "",
            "done_when": "none"}
    return {**base, **kw}


def _extension(name: str, steps: list[dict], stored_hash: str | None = None) -> str:
    design = {"summary": "Stored summary.", "steps": steps,
              "config": [{"key": "REPO", "description": "GitHub owner/repo", "value": "acme/shop"}],
              "assumptions": ["GitHub is the code host"], "instruction_adjustments": [],
              "not_possible": [{"need": "Custom workflow", "alternative": "Chain runs", "element_id": ""}],
              "tools": ["GitHub connector"], "connectors_needed": ["github"],
              "orchestration_prompt": "# Mission\nStored prompt."}
    payload = json.dumps({"name": name, "description": "Stored description of the project.", "design": design})
    return (f'<bpmn:extensionElements><maestro:design version="1" stepsHash="{stored_hash or steps_hash(steps)}">'
            f'<![CDATA[{payload}]]></maestro:design></bpmn:extensionElements>')


def maestro_file(title: str | None = None, name: str = "Stored project") -> str:
    """A Maestro export: S1 (Maestro), S2 (Dispatch, done when PR merged), S3 (human gate)."""
    steps = [_step("S1"), _step("S2", "dispatch", dispatch_workflow="issue_to_code", done_when="pr_merged"),
             _step("S3", "human", gate_owner="PO")]
    ext = _extension(name, steps)
    if title:
        steps[0]["title"] = title  # edited in a BPMN tool after the export
    nodes = ["<bpmn:startEvent id=\"Start\"/>", *map(_task, steps), '<bpmn:intermediateCatchEvent id="S2_done">'
             '<bpmn:conditionalEventDefinition><bpmn:condition>PR merged</bpmn:condition>'
             "</bpmn:conditionalEventDefinition></bpmn:intermediateCatchEvent>", '<bpmn:endEvent id="End"/>']
    flows = [("Start", "S1", ""), ("S1", "S2", ""), ("S2", "S2_done", ""), ("S2_done", "S3", ""), ("S3", "End", "")]
    return _bpmn(nodes, flows, name=name, extension=ext)


def branch_loop_file() -> str:
    """S1 decides; S2 (Bug) or S3 (Feature); join; S4 can loop back to S1."""
    steps = [_step("S1"), _step("S2", branch="Bug"), _step("S3", branch="Feature"),
             _step("S4", loop="PR refused", loop_to="S1")]
    gw = '<bpmn:exclusiveGateway id="{}"/>'
    nodes = ['<bpmn:startEvent id="Start"/>', *map(_task, steps), gw.format("S1_split"), gw.format("S1_join"),
             gw.format("S4_loop"), '<bpmn:endEvent id="End"/>']
    flows = [("Start", "S1", ""), ("S1", "S1_split", ""), ("S1_split", "S2", "Bug"), ("S1_split", "S3", "Feature"),
             ("S2", "S1_join", ""), ("S3", "S1_join", ""), ("S1_join", "S4", ""), ("S4", "S4_loop", ""),
             ("S4_loop", "End", "Yes"), ("S4_loop", "S1", "No · PR refused")]
    return _bpmn(nodes, flows, extension=_extension("Branches", steps))


FOREIGN = f"""<?xml version="1.0" encoding="UTF-8"?>
<bpmn:definitions {NS} id="Defs_1" targetNamespace="urn:test">
  <bpmn:collaboration id="Collab_1">
    <bpmn:participant id="Pool_1" name="Support desk" processRef="Process_1"/>
    <bpmn:participant id="Pool_2" name="Client"/>
    <bpmn:messageFlow id="MF_1" name="Resolution notice" sourceRef="Task_Email" targetRef="Pool_2"/>
  </bpmn:collaboration>
  <bpmn:process id="Process_1" name="Support intake">
    <bpmn:laneSet id="Lanes_1"><bpmn:lane id="Lane_1" name="Support agent">
      <bpmn:flowNodeRef>Task_Triage</bpmn:flowNodeRef><bpmn:flowNodeRef>Task_Email</bpmn:flowNodeRef>
    </bpmn:lane></bpmn:laneSet>
    <bpmn:startEvent id="Start_1" name="Ticket received"/>
    <bpmn:userTask id="Task_Triage" name="Triage the ticket"><bpmn:documentation>Check severity</bpmn:documentation></bpmn:userTask>
    <bpmn:parallelGateway id="Gw_Split" name="Fork"/>
    <bpmn:serviceTask id="Task_Fix" name="Fix the bug"><bpmn:multiInstanceLoopCharacteristics/></bpmn:serviceTask>
    <bpmn:sendTask id="Task_Email" name="Email the client"/>
    <bpmn:parallelGateway id="Gw_Join" name="Merge"/>
    <bpmn:boundaryEvent id="Boundary_1" name="SLA two days" attachedToRef="Task_Fix">
      <bpmn:timerEventDefinition><bpmn:timeDuration>P2D</bpmn:timeDuration></bpmn:timerEventDefinition>
    </bpmn:boundaryEvent>
    <bpmn:endEvent id="End_1"/>
    <bpmn:sequenceFlow id="F1" sourceRef="Start_1" targetRef="Task_Triage"/>
    <bpmn:sequenceFlow id="F2" sourceRef="Task_Triage" targetRef="Gw_Split"/>
    <bpmn:sequenceFlow id="F3" sourceRef="Gw_Split" targetRef="Task_Fix"/>
    <bpmn:sequenceFlow id="F4" sourceRef="Gw_Split" targetRef="Task_Email"/>
    <bpmn:sequenceFlow id="F5" sourceRef="Task_Fix" targetRef="Gw_Join"/>
    <bpmn:sequenceFlow id="F6" sourceRef="Task_Email" targetRef="Gw_Join"/>
    <bpmn:sequenceFlow id="F7" sourceRef="Gw_Join" targetRef="End_1"/>
  </bpmn:process>
</bpmn:definitions>"""

CLEAN = _bpmn(['<bpmn:startEvent id="A"/>', '<bpmn:userTask id="B" name="Review the request"/>',
               '<bpmn:endEvent id="C"/>'], [("A", "B", ""), ("B", "C", "")], process="P_clean", name="Clean flow")

FOREIGN_FINDINGS = [("MF_1", "messageFlow", "Resolution notice"), ("Gw_Split", "parallelGateway", "Fork"),
                    ("Gw_Join", "parallelGateway", "Merge"), ("Boundary_1", "boundaryEvent", "SLA two days")]

REVIEW = {"issues": [
    {"id": "I1", "element_id": "MF_1", "element_name": "Resolution notice",
     "problem": "Maestro cannot send a message to another pool.", "why": "Maestro has no email connector.",
     "options": [{"label": "Comment in the tracker addressed to the client", "effect": "A comment mentions the client."},
                 {"label": "Keep it as a manual step", "effect": "A person emails the client."}]},
    {"id": "I2", "element_id": "Gw_Split, Gw_Join", "element_name": "Fork",
     "problem": "Parallel branches cannot run at the same time.", "why": "Runs are short and sequential.",
     "options": [{"label": "Track each branch as a ledger item", "effect": "Each branch advances on its own."},
                 {"label": "Make them sequential", "effect": "The fix comes first, then the email."},
                 {"label": "Drop the second branch", "effect": "Only the fix remains."}]},
    {"id": "I3", "element_id": "Boundary_1", "element_name": "SLA two days",
     "problem": "A boundary timer cannot interrupt a task.", "why": "Maestro only acts when it runs.",
     "options": [{"label": "Check elapsed time on each run", "effect": "Escalate with a comment."},
                 {"label": "Ignore the SLA", "effect": "No escalation."}]},
]}


CONTEXT = "We run support for a retail client; tickets come from Zendesk and the code lives in GitHub."


def _import(client, xml: str | bytes, filename: str = "process.bpmn", path: str = "/maestro/new",
            field: str = "bpmn", token: str | None = None, extra: dict[str, str] | None = None,
            name: str = "Support intake", context: str = CONTEXT):  # type: ignore[no-untyped-def]
    """Import from the Create page (BPMN mode). `path` and `field` can point at another import route."""
    token = token or csrf(client, "/maestro/new")
    data = {"csrf_token": token}
    if path == "/maestro/new":
        data.update({"start": "bpmn", "name": name, "description": context})
    data.update(extra or {})
    files = {field: (filename, xml, "application/xml")} if xml is not None else None
    return client.post(path, data=data, files=files, follow_redirects=False)


def _import_foreign(client, fake, review=REVIEW) -> int:  # type: ignore[no-untyped-def]
    fake.review_response = review
    r = _import(client, FOREIGN)
    assert r.status_code == 303 and r.headers["location"].endswith("/design"), r.headers
    wf_id = int(r.headers["location"].split("/")[2])
    _design(wf_id)
    return wf_id


def _calls(fake) -> tuple[int, int, int, int]:  # type: ignore[no-untyped-def]
    return len(fake.review_calls), len(fake.tools_calls), len(fake.clarify_calls), len(fake.design_calls)


def _state(wf_id: int) -> tuple[str, dict]:
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        return wf.design_status, dict(wf.clarifications or {})


def _answer(client, wf_id: int, data: dict[str, str]):  # type: ignore[no-untyped-def]
    token = csrf(client, f"/workflows/{wf_id}/design")
    return client.post(f"/workflows/{wf_id}/design/answers", data={"csrf_token": token, **data},
                       follow_redirects=False)


# --- Parsing ------------------------------------------------------------------------------------


def test_foreign_file_gives_one_finding_per_unsupported_element():
    result = bpmn_to_design(FOREIGN)
    assert result.kind == "foreign" and result.name == "Support intake" and result.design is None
    assert [(f["element_id"], f["element_type"], f["element_name"]) for f in result.findings] == FOREIGN_FINDINGS
    assert all(f["detail"] for f in result.findings)
    text = result.description
    for expected in ("Support intake", "Support agent", "Triage the ticket", "Email the client", "sendTask",
                     "id Task_Email", "timer timeDuration P2D", "multi-instance", "Check severity"):
        assert expected in text, expected
    assert text.index("Triage the ticket") < text.index("Fix the bug")


def test_every_kind_of_unsupported_element_is_found():
    xml = _bpmn([
        '<bpmn:startEvent id="St"><bpmn:signalEventDefinition/></bpmn:startEvent>',
        '<bpmn:scriptTask id="Sc" name="Compute"/>', '<bpmn:businessRuleTask id="Br"/>',
        '<bpmn:inclusiveGateway id="Ig"/>', '<bpmn:dataObject id="Do" name="Invoice"/>',
        '<bpmn:dataStoreReference id="Ds"/>', '<bpmn:subProcess id="Ev" triggeredByEvent="true"/>',
        '<bpmn:transaction id="Tx"/>', '<bpmn:task id="Comp" isForCompensation="true"/>',
        '<bpmn:intermediateThrowEvent id="Es"><bpmn:escalationEventDefinition/></bpmn:intermediateThrowEvent>',
        '<bpmn:boundaryEvent id="Bc" attachedToRef="Comp"><bpmn:compensateEventDefinition/></bpmn:boundaryEvent>',
    ], [])
    found = {f["element_id"]: f["element_type"] for f in bpmn_to_design(xml).findings}
    assert found == {"St": "startEvent", "Sc": "scriptTask", "Br": "businessRuleTask", "Ig": "inclusiveGateway",
                     "Do": "dataObject", "Ds": "dataStoreReference", "Ev": "subProcess", "Tx": "transaction",
                     "Comp": "task", "Es": "intermediateThrowEvent", "Bc": "boundaryEvent"}


def test_missing_and_duplicate_ids_get_fallback_ids():
    xml = _bpmn(['<bpmn:parallelGateway name="Fork now"/>', '<bpmn:parallelGateway id="X"/>',
                 '<bpmn:parallelGateway id="X"/>'], [])
    assert [f["element_id"] for f in bpmn_to_design(xml).findings] == ["Fork_now", "X", "X_2"]


def test_unchanged_maestro_file_is_restored_and_the_rebuilt_steps_match():
    result = bpmn_to_design(maestro_file())
    assert result.kind == "maestro" and result.name == "Stored project" and result.findings == []
    assert result.design["orchestration_prompt"].startswith("# Mission")
    steps = result.design["steps"]
    assert steps_hash(steps_from_bpmn(parse_bpmn(maestro_file()))) == steps_hash(steps)


def test_branches_and_loops_round_trip():
    assert bpmn_to_design(branch_loop_file()).kind == "maestro"
    # A step moved to another branch is a change
    assert bpmn_to_design(branch_loop_file().replace('targetRef="S2" name="Bug"', 'targetRef="S2" name="Defect"')
                          ).kind == "foreign"


def test_changed_maestro_file_is_treated_as_foreign():
    assert bpmn_to_design(maestro_file(title="Renamed step")).kind == "foreign"
    # Hash that does not match the stored design
    steps = [_step("S1")]
    bad = _bpmn(['<bpmn:startEvent id="Start"/>', _task(steps[0])], [("Start", "S1", "")],
                extension=_extension("X", steps, stored_hash="0" * 64))
    assert bpmn_to_design(bad).kind == "foreign"
    # An element the exporter never writes
    extra = maestro_file().replace('<bpmn:endEvent id="End"/>', '<bpmn:endEvent id="End"/><bpmn:manualTask id="Extra"/>')
    assert bpmn_to_design(extra).kind == "foreign"


@pytest.mark.parametrize("xml,message", [
    (b"not xml at all", "not valid XML"),
    (b"<root/>", "not a BPMN 2.0 file"),
    (f'<bpmn:definitions {NS}><bpmn:collaboration id="c"/></bpmn:definitions>'.encode(), "no process"),
    (b'<?xml version="1.0"?><!DOCTYPE d [<!ENTITY e "x">]><definitions xmlns="http://www.omg.org/spec/BPMN/'
     b'20100524/MODEL"><process id="p">&e;</process></definitions>', "DOCTYPE"),
    (b'<!DOCTYPE d SYSTEM "http://example.com/x.dtd"><d/>', "DOCTYPE"),
    (b"<a>" + b"x" * (MAX_BYTES + 1) + b"</a>", "2 MB"),
    (b"", "empty"),
    (b"\xff\xfe\x00bad", "not valid XML"),
])
def test_invalid_files_are_rejected(xml, message):
    with pytest.raises(BpmnImportError, match=message):
        bpmn_to_design(xml)


def test_wrong_namespace_is_rejected():
    xml = '<definitions xmlns="http://www.omg.org/spec/BPMN/20100524/OTHER"><process id="p"/></definitions>'
    with pytest.raises(BpmnImportError, match="not a BPMN 2.0 file"):
        bpmn_to_design(xml)


def test_a_long_chain_does_not_overflow_the_stack():
    n = 3000
    nodes = [f'<bpmn:task id="T{i}"/>' for i in range(n)]
    flows = [(f"T{i}", f"T{i + 1}", "") for i in range(n - 1)]
    assert "more elements" in bpmn_to_design(_bpmn(nodes, flows)).description


# --- Review issues ------------------------------------------------------------------------------


def test_normalize_issues_adds_a_generic_issue_for_every_finding_left_out():
    findings = [{"element_id": "A", "element_type": "boundaryEvent", "element_name": "SLA", "detail": "Boundary"},
                {"element_id": "B", "element_type": "parallelGateway", "element_name": "", "detail": "Parallel"}]
    raw = [{"id": "I1", "element_id": "A", "element_name": "SLA", "problem": "Timer.", "why": "Limit.",
            "options": [{"label": "One", "effect": "e"}, {"label": "Two", "effect": "e"}]}]
    issues = service.normalize_issues(raw, findings)
    assert [i["element_id"] for i in issues] == ["A", "B"]
    generic = issues[1]
    assert generic["problem"] == "This BPMN element is not supported" and generic["element_name"] == "B"
    assert [o["label"] for o in generic["options"]] == ["Describe it as a step done by a person", "Leave it out"]


def test_normalize_issues_falls_back_when_an_issue_has_fewer_than_two_options():
    findings = [{"element_id": "A", "element_type": "boundaryEvent", "element_name": "SLA", "detail": "Boundary"}]
    raw = [{"id": "I1", "element_id": "A", "problem": "Timer.", "why": "", "options": [{"label": "Only", "effect": ""}]}]
    issues = service.normalize_issues(raw, findings)
    assert len(issues) == 1 and issues[0]["problem"] == "This BPMN element is not supported"


def test_normalize_issues_caps_the_model_but_never_the_findings():
    def issue(i: int) -> dict:
        return {"id": "I", "element_id": f"E{i}", "element_name": "", "problem": f"Problem {i}", "why": "",
                "options": [{"label": "a", "effect": ""}, {"label": "b", "effect": ""}]}

    findings = [{"element_id": f"F{i}", "element_type": "dataObject", "element_name": "", "detail": "d"}
                for i in range(5)]
    issues = service.normalize_issues([issue(i) for i in range(12)], findings)
    assert len(issues) == 8 + 5
    assert len({i["id"] for i in issues}) == len(issues)  # ids stay unique even when the model repeats one


# --- Import and review dialog --------------------------------------------------------------------


def test_unchanged_maestro_file_is_restored_as_ready_with_no_anthropic_call(client, fake):
    _setup(client)
    r = _import(client, maestro_file())
    assert r.status_code == 303
    wf_id = int(r.headers["location"].split("/")[2])
    assert _calls(fake) == (0, 0, 0, 0)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.name == "Support intake" and wf.design_status == "ready"
        assert wf.description.startswith("## Context from the user\n" + CONTEXT + "\n\n## Imported BPMN process\n")
        assert wf.content == "# Mission\nStored prompt." and "orchestration_prompt" not in wf.design_json
        assert [s["id"] for s in wf.design_json["steps"]] == ["S1", "S2", "S3"]
        assert wf.design_json["config"][0]["value"] == "acme/shop" and wf.design_json["connectors_needed"] == ["github"]
        assert wf.instructions_hash == service.current_instructions_hash(db)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Stored summary." in page and "Some parts of this BPMN" not in page
    assert service.claim_design_jobs(SessionLocal()) == []


def test_foreign_file_shows_the_review_dialog_with_every_finding(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    status, clar = _state(wf_id)
    assert status == "needs_answers" and clar["kind"] == "import_review"
    assert [(f["element_id"]) for f in fake.review_calls[0]["findings"]] == [f[0] for f in FOREIGN_FINDINGS]
    assert fake.review_calls[0]["supported"] == ["linear", "github", "atlassian"]
    assert "Email the client" in fake.review_calls[0]["description"]
    assert fake.tools_calls == [] and fake.clarify_calls == [] and fake.design_calls == []
    # Boundary_1, Gw_Split, Gw_Join, MF_1 all appear in the dialog (Gw_Split and Gw_Join share one issue)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert ("Some parts of this BPMN can't run as drawn with Maestro and Upsun Dispatch. Pick how to handle "
            "each one.") in html.unescape(page)
    for expected in ("MF_1", "Boundary_1", "Gw_Split, Gw_Join", "Maestro cannot send a message to another pool.",
                     "Parallel branches cannot run at the same time.", "A person emails the client.", "Other",
                     "Import a corrected file", "Cancel import", "recommended"):
        assert expected in page, expected
    assert page.count("recommended</span>") == len(clar["questions"])
    first = clar["questions"][0]
    assert f'name="q:{first["id"]}" value="{first["options"][0]["label"]}" checked' in page
    assert f'name="other:{first["id"]}"' in page
    assert all(2 <= len(q["options"]) <= 4 for q in clar["questions"])


def test_a_finding_the_review_omits_gets_a_generic_issue(client, fake):
    _setup(client)
    review = {"issues": [i for i in REVIEW["issues"] if i["element_id"] != "Boundary_1"]}
    wf_id = _import_foreign(client, fake, review)
    _, clar = _state(wf_id)
    generic = [q for q in clar["questions"] if q["problem"] == "This BPMN element is not supported"]
    assert [q["element_id"] for q in generic] == ["Boundary_1"]
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Describe it as a step done by a person" in page and "Leave it out" in page


def test_a_clean_file_skips_the_dialog_and_runs_the_pipeline(client, fake):
    _setup(client)
    r = _import(client, CLEAN)
    wf_id = int(r.headers["location"].split("/")[2])
    assert _state(wf_id)[0] == "queued"
    assert "Maestro is designing your project" in client.get(f"/workflows/{wf_id}/design").text
    _design(wf_id)
    assert _calls(fake) == (1, 1, 1, 1)
    status, clar = _state(wf_id)
    assert status == "ready" and "kind" not in clar and "Review the request" in fake.design_calls[0]["description"]


def test_submitting_choices_stores_final_decisions_and_starts_the_pipeline(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    _, clar = _state(wf_id)
    qs = {q["element_id"]: q for q in clar["questions"]}
    data = {f"q:{q['id']}": q["options"][0]["label"] for q in clar["questions"]}
    boundary = qs["Boundary_1"]
    data[f"q:{boundary['id']}"] = "__other__"
    data[f"other:{boundary['id']}"] = "A scheduled run escalates after two days"
    r = _answer(client, wf_id, data)
    assert r.status_code == 303
    status, clar = _state(wf_id)
    assert status == "queued" and clar["questions"] == [] and clar["kind"] == "import_review"
    by_element = {d["element_id"]: d for d in clar["decisions"]}
    assert set(by_element) == {"MF_1", "Gw_Split, Gw_Join", "Boundary_1"}
    assert by_element["MF_1"]["answer"] == ("Comment in the tracker addressed to the client — "
                                            "A comment mentions the client.")
    assert by_element["Boundary_1"]["answer"] == "A scheduled run escalates after two days"

    fake.design_not_possible = [
        {"need": "Resolution notice by email", "alternative": "Tracker comment", "element_id": "MF_1"},
        {"need": "Parallel branches", "alternative": "Ledger items", "element_id": "Gw_Split, Gw_Join"},
        {"need": "SLA two days", "alternative": "Scheduled escalation", "element_id": "Boundary_1"},
    ]
    _design(wf_id)
    assert len(fake.review_calls) == 1  # the review is not run again
    for text in (fake.tools_calls[-1], fake.clarify_calls[-1], fake.design_calls[-1]["description"]):
        assert "Decisions made by the user (final, apply them)" in text
        assert "[BPMN element MF_1]" in text and "A scheduled run escalates after two days" in text
    assert "list each of them in not_possible" in fake.design_calls[-1]["description"]
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "ready"
        assert {n["element_id"] for n in wf.design_json["not_possible"]} == {"MF_1", "Gw_Split, Gw_Join", "Boundary_1"}
        assert {d["element_id"] for d in wf.clarifications["decisions"]} == set(by_element)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "Not possible with Dispatch today" in page and "Your decisions" in page
    assert page.count("<code class=\"small\">MF_1</code>") == 2  # in both tables


def test_other_needs_text_and_every_issue_needs_an_answer(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    _, clar = _state(wf_id)
    data = {f"q:{q['id']}": q["options"][0]["label"] for q in clar["questions"]}
    first = clar["questions"][0]["id"]
    data[f"q:{first}"] = "__other__"  # no text written
    r = _answer(client, wf_id, data)
    assert r.status_code == 303
    status, clar_after = _state(wf_id)
    assert status == "needs_answers" and clar_after["decisions"] == []
    assert "Choose how to handle every part" in client.get(f"/workflows/{wf_id}/design").text
    del data[f"q:{first}"]
    _answer(client, wf_id, data)
    assert _state(wf_id)[0] == "needs_answers"
    data[f"q:{first}"] = "not one of the options"
    _answer(client, wf_id, data)
    assert _state(wf_id)[0] == "needs_answers"


def test_corrected_file_replaces_the_description_and_reruns_the_review(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    fake.review_response = {"issues": []}
    token = csrf(client, f"/workflows/{wf_id}/design")
    r = _import(client, CLEAN, path=f"/workflows/{wf_id}/import-bpmn", field="bpmn", token=token)
    assert r.status_code == 303
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.design_status == "queued" and wf.clarifications["kind"] == "import_pending"
        assert "Review the request" in wf.description and "Email the client" not in wf.description
        assert wf.name == "Support intake"  # the name stays
        assert wf.description.startswith(f"## Context from the user\n{CONTEXT}\n\n")  # the context stays
    _design(wf_id)
    assert len(fake.review_calls) == 2 and "Review the request" in fake.review_calls[1]["description"]
    assert _state(wf_id)[0] == "ready"


def test_an_invalid_corrected_file_leaves_the_dialog_untouched(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    r = _import(client, "<nope/>", path=f"/workflows/{wf_id}/import-bpmn", field="bpmn",
                token=csrf(client, f"/workflows/{wf_id}/design"))
    assert r.status_code == 303
    assert _state(wf_id)[0] == "needs_answers"
    assert "not a BPMN 2.0 file" in client.get(f"/workflows/{wf_id}/design").text


def test_cancel_import_asks_for_confirmation_then_deletes(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    page = client.get(f"/workflows/{wf_id}/import/cancel")
    assert page.status_code == 200 and "Delete the import" in page.text and "Support intake" in page.text
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id) is not None
    token = csrf(client, f"/workflows/{wf_id}/import/cancel")
    r = client.post(f"/workflows/{wf_id}/import/cancel", data={"csrf_token": token}, follow_redirects=False)
    assert r.status_code == 303
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id) is None


def test_cancel_is_only_offered_while_the_dialog_is_open(client, fake):
    _setup(client)
    r = _import(client, maestro_file())
    wf_id = int(r.headers["location"].split("/")[2])
    r = client.get(f"/workflows/{wf_id}/import/cancel", follow_redirects=False)
    assert r.status_code == 303
    token = csrf(client, f"/workflows/{wf_id}/design")
    client.post(f"/workflows/{wf_id}/import/cancel", data={"csrf_token": token})
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id) is not None


@pytest.mark.parametrize("payload", [b"plain text", b"<root/>", b"<!DOCTYPE x [<!ENTITY a 'b'>]><x>&a;</x>",
                                     b"<a>" + b"x" * (MAX_BYTES + 1) + b"</a>"])
def test_invalid_files_give_a_400_and_create_nothing(client, fake, payload):
    _setup(client)
    r = _import(client, payload)
    assert r.status_code == 400
    page = html.unescape(r.text)
    assert 'class="flash error"' in page and "Choose the BPMN file again." in page
    assert 'value="bpmn" checked' in page and "Support intake" in page and CONTEXT in page
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0
    assert _calls(fake) == (0, 0, 0, 0)


def test_no_file_is_an_error(client, fake):
    _setup(client)
    r = _import(client, None)
    assert r.status_code == 400
    assert "Choose a BPMN file (.bpmn). Choose the BPMN file again." in html.unescape(r.text)
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


# --- Create page, "A BPMN file" mode -----------------------------------------------------------------


def test_the_create_page_starts_in_describe_mode(client, fake):
    _setup(client)
    page = client.get("/maestro/new").text
    assert 'value="describe" checked' in page and 'value="bpmn" checked' not in page
    assert "Design the project" in page and 'name="diagram"' in page
    assert "Paste or drop an image (PNG, JPEG, GIF, WebP, 3.75 MB max) or a PDF (10 MB max)" in page
    assert 'id="bpmn-file" accept=".bpmn,application/xml,text/xml">' in page  # not required while hidden
    assert 'name="description" id="description" class="huge" required' not in page
    assert "A description" in page and "A BPMN file" in page


def test_the_create_page_preselects_bpmn_mode(client, fake):
    _setup(client)
    page = client.get("/maestro/new?start=bpmn").text
    assert 'value="bpmn" checked' in page and 'value="describe" checked' not in page
    assert 'name="bpmn" id="bpmn-file" accept=".bpmn,application/xml,text/xml" required' in page
    assert "BPMN 2.0 file from Maestro or any BPMN tool (Camunda, Signavio, Bizagi)." in page
    assert ">Context<" in page and 'required minlength="40"' in page
    assert ">Import the project<" in page
    assert 'action="/maestro/new"' in page and 'enctype="multipart/form-data"' in page
    assert 'name="csrf_token"' in page
    assert 'value="describe" checked' in client.get("/maestro/new?start=other").text


def test_the_edit_page_has_no_bpmn_mode(client, fake):
    _setup(client)
    r = _import(client, maestro_file())
    wf_id = int(r.headers["location"].split("/")[2])
    edit = client.get(f"/workflows/{wf_id}/edit").text
    assert "A BPMN file" not in edit and 'name="bpmn"' not in edit and "BPMN: will be imported" not in edit


def test_a_foreign_file_keeps_the_typed_name_and_sends_the_context_to_the_review(client, fake):
    _setup(client)
    fake.review_response = REVIEW
    r = _import(client, FOREIGN, name="Client support (TMA)")
    assert r.status_code == 303 and r.headers["location"].endswith("/design")
    wf_id = int(r.headers["location"].split("/")[2])
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.name == "Client support (TMA)" and wf.design_status == "queued"
        assert wf.clarifications["kind"] == "import_pending"
        head, process = wf.description.split("\n\n## Imported BPMN process\n", 1)
        assert head == "## Context from the user\n" + CONTEXT and "Email the client" in process
    assert "BPMN file imported. Maestro is reviewing it." in client.get(r.headers["location"]).text
    _design(wf_id)
    assert CONTEXT in fake.review_calls[0]["description"] and "Email the client" in fake.review_calls[0]["description"]


def test_a_maestro_export_keeps_the_typed_name_and_the_context(client, fake):
    _setup(client)
    r = _import(client, maestro_file(), name="My typed name")
    wf_id = int(r.headers["location"].split("/")[2])
    assert _calls(fake) == (0, 0, 0, 0)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        assert wf.name == "My typed name" and wf.design_status == "ready"
        assert CONTEXT in wf.description and wf.description.startswith("## Context from the user")
    assert "BPMN file imported. Your design is restored." in client.get(r.headers["location"]).text


@pytest.mark.parametrize("context", ["", "   ", "Too short to explain anything."])
def test_a_missing_or_short_context_gives_a_400(client, fake, context):
    _setup(client)
    r = _import(client, CLEAN, name="Kept name", context=context, extra={"shared": "1"})
    assert r.status_code == 400
    page = html.unescape(r.text)
    assert ("Add some context (at least a few sentences): why this project, who is involved, which tools. "
            "Choose the BPMN file again.") in page
    assert 'value="bpmn" checked' in page and 'value="Kept name"' in page and "checked" in page.split('name="shared"')[1][:80]
    if context.strip():
        assert context in page
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0
    assert _calls(fake) == (0, 0, 0, 0)


@pytest.mark.parametrize("name", ["", "   ", "x" * 129])
def test_a_bad_name_gives_a_400(client, fake, name):
    _setup(client)
    r = _import(client, CLEAN, name=name)
    assert r.status_code == 400
    assert "The name must be between 1 and 128 characters. Choose the BPMN file again." in r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_a_name_already_taken_gives_a_400(client, fake):
    _setup(client)
    assert _import(client, maestro_file()).status_code == 303
    r = _import(client, CLEAN)
    assert r.status_code == 400
    assert "You already have a project named Support intake. Choose the BPMN file again." in r.text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 1
    assert _import(client, CLEAN, name="Another name").status_code == 303  # the session stays usable


@pytest.mark.parametrize("xml", [CLEAN, maestro_file()])
@pytest.mark.parametrize("shared", [True, False])
def test_import_respects_the_shared_choice(client, fake, xml, shared):
    _setup(client)
    r = _import(client, xml, extra={"shared": "1"} if shared else None)
    assert r.status_code == 303 and r.headers["location"].endswith("/design")
    wf_id = int(r.headers["location"].split("/")[2])
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id).shared is shared


def test_the_describe_path_is_unchanged(client, fake):
    _setup(client)
    token = csrf(client, "/maestro/new")
    short = client.post("/maestro/new", data={"csrf_token": token, "name": "Short", "description": "Too short"},
                        follow_redirects=False)
    assert short.status_code == 400 and "Describe the project in more detail" in short.text
    assert 'value="describe" checked' in short.text
    text = "Requests arrive in Linear, a person validates the spec, Dispatch writes the code and a human merges it."
    r = client.post("/maestro/new", data={"csrf_token": token, "name": "Described", "description": text},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("/design")
    with SessionLocal() as db:
        wf = db.scalars(select(Workflow).where(Workflow.name == "Described")).one()
        assert wf.description == text and "Imported BPMN process" not in wf.description


# --- Old import URLs and entry points --------------------------------------------------------------


def test_the_old_import_page_redirects_to_the_create_page(client, fake):
    _setup(client)
    r = client.get("/maestro/import-bpmn", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maestro/new?start=bpmn"
    token = csrf(client, "/maestro/new")
    r = client.post("/maestro/import-bpmn", data={"csrf_token": token}, files={"diagram": ("a.bpmn", CLEAN, "application/xml")},
                    follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/maestro/new?start=bpmn"
    assert "Import a BPMN file from the Create page." in client.get("/maestro/new?start=bpmn").text
    with SessionLocal() as db:
        assert db.scalar(select(func.count()).select_from(Workflow)) == 0


def test_the_old_import_page_requires_login(client, fake):
    _setup(client)
    token = csrf(client, "/")
    client.post("/logout", data={"csrf_token": token})
    r = client.get("/maestro/import-bpmn", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"


def test_the_projects_page_links_to_the_create_page_in_bpmn_mode(client, fake):
    _setup(client)
    page = client.get("/workflows").text  # empty state
    assert 'href="/maestro/new?start=bpmn"' in page and "Import BPMN" in page
    assert "/maestro/import-bpmn" not in page
    _import(client, CLEAN)
    page = client.get("/workflows").text
    describe = page.index("Describe a new project")
    link = page.index('href="/maestro/new?start=bpmn"')
    assert 0 < link - describe < 300 and "Import BPMN" in page[link:link + 700]


# --- Access ---------------------------------------------------------------------------------


def test_another_user_cannot_see_or_answer_the_dialog(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    _, clar = _state(wf_id)
    make_user("carol")
    _connect(make_user("dave"), "github")
    client.cookies.clear()
    login(client, "carol")
    token = csrf(client, "/maestro/new")
    assert client.get(f"/workflows/{wf_id}/design").status_code == 404
    assert client.get(f"/workflows/{wf_id}/design/panel").status_code == 404
    assert client.post(f"/workflows/{wf_id}/design/answers", data={"csrf_token": token}).status_code == 404
    assert client.post(f"/workflows/{wf_id}/import-bpmn", data={"csrf_token": token},
                       files={"bpmn": ("a.bpmn", CLEAN, "application/xml")}).status_code == 404
    assert client.get(f"/workflows/{wf_id}/import/cancel").status_code == 404
    assert client.post(f"/workflows/{wf_id}/import/cancel", data={"csrf_token": token}).status_code == 404
    assert _state(wf_id)[0] == "needs_answers" and _state(wf_id)[1]["questions"] == clar["questions"]


def test_a_shared_dialog_is_not_answerable_by_a_viewer(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    with SessionLocal() as db:
        db.get(Workflow, wf_id).shared = True
        db.commit()
    make_user("carol")
    client.cookies.clear()
    login(client, "carol")
    token = csrf(client, "/maestro/new")
    page = client.get(f"/workflows/{wf_id}/design").text
    assert "has to finish its design" in page and "Cancel import" not in page
    assert client.post(f"/workflows/{wf_id}/design/answers", data={"csrf_token": token}).status_code == 404


def test_posts_require_a_csrf_token(client, fake):
    _setup(client)
    wf_id = _import_foreign(client, fake)
    files = {"bpmn": ("a.bpmn", CLEAN, "application/xml")}
    assert client.post("/maestro/new", data={"start": "bpmn", "name": "X", "description": CONTEXT},
                       files=files).status_code == 403
    assert client.post("/maestro/import-bpmn", files=files).status_code == 403
    assert client.post(f"/workflows/{wf_id}/import-bpmn", files={"bpmn": ("a.bpmn", CLEAN, "application/xml")}
                       ).status_code == 403
    assert client.post(f"/workflows/{wf_id}/import/cancel").status_code == 403
    assert client.post(f"/workflows/{wf_id}/design/answers").status_code == 403
    with SessionLocal() as db:
        assert db.get(Workflow, wf_id) is not None


def test_import_progress_has_a_review_row_for_imports_only(client, fake):
    _setup(client)
    r = _import(client, CLEAN)
    wf_id = int(r.headers["location"].split("/")[2])
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        service._begin_generation(db, wf, with_review=True)
        wf.design_status = "generating"
        view = service.progress_view(wf, service.design_estimates(db))
        assert [row["key"] for row in view["rows"]] == ["review", "tools", "clarify", "design"]
        service._begin_generation(db, wf)
        assert [row["key"] for row in service.progress_view(wf, service.design_estimates(db))["rows"]] \
            == ["tools", "clarify", "design"]
