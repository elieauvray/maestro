from __future__ import annotations

import re
from datetime import UTC, datetime
from itertools import pairwise
from types import SimpleNamespace
from xml.etree import ElementTree as ET

import pytest

from app.db import SessionLocal
from app.maestro.bpmn import MAESTRO_NS, MODEL_NS, bpmn_to_design, design_to_bpmn, steps_hash
from app.maestro.render import ACTOR_LABELS, compute_layout
from app.models import Workflow
from tests.conftest import csrf, login, make_user
from tests.test_maestro import DESCRIPTION, _connect, _create, _design, _setup

M = "{" + MODEL_NS + "}"
MA = "{" + MAESTRO_NS + "}"
DI = "{http://www.omg.org/spec/BPMN/20100524/DI}"
DC = "{http://www.omg.org/spec/DD/20100524/DC}"
DD = "{http://www.omg.org/spec/DD/20100524/DI}"
NOW = datetime(2026, 11, 23, 9, 30, 0, tzinfo=UTC)
NCNAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _step(sid: str, actor: str = "maestro", **kw: str) -> dict:
    base = {"id": sid, "phase": "Build", "title": f"Step {sid}", "detail": f"Detail of {sid}", "actor": actor,
            "dispatch_workflow": "none", "gate_owner": "", "repeat": "", "loop": "", "branch": "", "loop_to": "",
            "done_when": "none"}
    return {**base, **kw}


def _design_of(steps: list[dict], **kw) -> dict:  # type: ignore[no-untyped-def]
    return {"summary": "A stored summary.", "steps": steps,
            "config": [{"key": "REPO", "description": "GitHub owner/repo", "value": "acme/shop"}],
            "assumptions": ["GitHub is the code host"], "instruction_adjustments": [], "not_possible": [],
            "tools": ["GitHub connector"], "orchestration_prompt": "# Mission\nStored prompt.", **kw}


def _wf(**kw) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    return SimpleNamespace(**{"id": 7, "name": "Page migration", "description": "Migrate page by page.",
                              "schedule_cron": "", **kw})


LINEAR = [_step("S1"), _step("S2", "dispatch", dispatch_workflow="issue_to_code", done_when="pr_merged"),
          _step("S3", "human", gate_owner="PO", detail=""), _step("S4", "upsun", repeat="per page"),
          _step("S5", "external")]
BRANCHES = [_step("S1", phase="Triage"), _step("S2", "human", gate_owner="PO", branch="Bug", phase="Fix"),
            _step("S3", "upsun", branch="Feature", phase="Fix"), _step("S4", "human", branch="Feature", phase="Fix"),
            _step("S5", "dispatch", dispatch_workflow="code_review", done_when="pr_approved", phase="Ship",
                  loop="PR refused", loop_to="S1")]
LOOPS = [_step("S1"), _step("S2", loop="tests fail", loop_to="S1"), _step("S3", "human", loop_to="S2")]
DESIGNS = {"linear": LINEAR, "branches": BRANCHES, "loops": LOOPS}


def _export(steps: list[dict] | None = None, wf: SimpleNamespace | None = None, **kw) -> str:  # type: ignore[no-untyped-def]
    return design_to_bpmn(wf or _wf(), _design_of(steps if steps is not None else BRANCHES, **kw), now=NOW)


def _all_ids(root: ET.Element) -> list[str]:
    return [el.get("id") for el in root.iter() if el.get("id")]


def _parts(xml: str):  # type: ignore[no-untyped-def]
    root = ET.fromstring(xml)
    process = root.find(M + "process")
    nodes = {el.get("id"): el for el in process if el.tag.removeprefix(M) not in
             ("sequenceFlow", "laneSet", "documentation", "extensionElements", "group")}
    flows = {el.get("id"): el for el in process.findall(M + "sequenceFlow")}
    return root, process, nodes, flows


def _bounds(root: ET.Element) -> dict[str, tuple[int, int, int, int]]:
    out = {}
    for shape in root.iter(DI + "BPMNShape"):
        b = shape.find(DC + "Bounds")
        out[shape.get("bpmnElement")] = tuple(int(b.get(k)) for k in ("x", "y", "width", "height"))
    return out


# --- Structure and diagram interchange ---------------------------------------------------------------


@pytest.mark.parametrize("name", list(DESIGNS))
def test_export_is_valid_and_complete(name):
    root, process, nodes, flows = _parts(_export(DESIGNS[name]))
    ids = _all_ids(root)
    assert len(ids) == len(set(ids)) and all(NCNAME.match(i) for i in ids)
    assert process.get("isExecutable") == "false"
    participant = root.find(M + "collaboration").find(M + "participant")
    assert participant.get("processRef") == process.get("id") and participant.get("name") == "Page migration"
    for f in flows.values():
        assert f.get("sourceRef") in nodes and f.get("targetRef") in nodes
    lanes = process.find(M + "laneSet").findall(M + "lane")
    refs = [r.text for lane in lanes for r in lane.findall(M + "flowNodeRef")]
    assert sorted(refs) == sorted(nodes)  # every node is in exactly one lane

    bounds = _bounds(root)
    pool = bounds["Participant_1"]
    for node_id in nodes:
        assert node_id in bounds, node_id
    for lane in lanes:
        x, y, w, h = bounds[lane.get("id")]
        assert pool[0] <= x and x + w <= pool[0] + pool[2] and pool[1] <= y and y + h <= pool[1] + pool[3]
        for ref in lane.findall(M + "flowNodeRef"):
            nx, ny, nw, nh = bounds[ref.text]
            assert x <= nx and nx + nw <= x + w and y <= ny and ny + nh <= y + h, ref.text
    shapes = {s.get("bpmnElement"): s for s in root.iter(DI + "BPMNShape")}
    assert shapes["Participant_1"].get("isHorizontal") == "true"
    assert all(shapes[lane.get("id")].get("isHorizontal") == "true" for lane in lanes)

    edges = {e.get("bpmnElement"): e for e in root.iter(DI + "BPMNEdge")}
    assert set(edges) == set(flows)
    for fid, edge in edges.items():
        points = [(int(p.get("x")), int(p.get("y"))) for p in edge.findall(DD + "waypoint")]
        assert len(points) >= 2, fid
        assert all(a[0] == b[0] or a[1] == b[1] for a, b in pairwise(points)), fid  # orthogonal


def test_lanes_follow_actor_order_and_names():
    root, process, *_ = _parts(_export(LINEAR))
    lanes = process.find(M + "laneSet").findall(M + "lane")
    assert [lane.get("id") for lane in lanes] == ["Lane_human", "Lane_maestro", "Lane_dispatch", "Lane_upsun",
                                                  "Lane_external"]
    assert [lane.get("name") for lane in lanes] == [ACTOR_LABELS[a] for a in
                                                     ("human", "maestro", "dispatch", "upsun", "external")]
    bounds = _bounds(root)
    tops = [bounds[lane.get("id")][1] for lane in lanes]
    assert tops == sorted(tops) and len(set(tops)) == len(tops)  # horizontal bands, top to bottom


def test_actors_map_to_bpmn_elements():
    _, _, nodes, _ = _parts(_export(LINEAR))
    assert {k: nodes[k].tag.removeprefix(M) for k in ("S1", "S2", "S3", "S4", "S5")} == {
        "S1": "serviceTask", "S2": "callActivity", "S3": "userTask", "S4": "serviceTask", "S5": "task"}
    s2 = nodes["S2"]
    assert s2.get("calledElement") == "issue_to_code" and s2.get(MA + "dispatchWorkflow") == "issue_to_code"
    assert "Dispatch workflow: Issue to code" in s2.find(M + "documentation").text
    assert s2.find(M + "documentation").text.splitlines()[0] == "Detail of S2"
    done = nodes["S2_done"]
    assert done.tag == M + "intermediateCatchEvent"
    assert done.find(M + "conditionalEventDefinition").find(M + "condition").text == "PR merged"
    assert nodes["S3"].get(MA + "gateOwner") == "PO"
    assert any("Gate owner: PO" in (d.text or "") for d in nodes["S3"].findall(M + "documentation"))


def test_maestro_attributes_are_omitted_when_empty_or_none():
    _, _, nodes, _ = _parts(_export(LINEAR))
    s1 = nodes["S1"]
    assert s1.get(MA + "actor") == "maestro" and s1.get(MA + "phase") == "Build"
    for attr in ("dispatchWorkflow", "doneWhen", "gateOwner", "repeat"):
        assert MA + attr not in s1.attrib, attr
    assert nodes["S2"].get(MA + "doneWhen") == "pr_merged"


def test_repeat_is_a_multi_instance_but_a_placeholder_is_not():
    _, _, nodes, _ = _parts(_export(LINEAR))
    multi = nodes["S4"].find(M + "multiInstanceLoopCharacteristics")
    assert multi is not None and multi.find(M + "documentation").text == "per page"
    assert nodes["S4"].get(MA + "repeat") == "per page"
    _, _, nodes, _ = _parts(_export([_step("S1", repeat="n/a"), _step("S2", repeat="none")]))
    assert nodes["S1"].find(M + "multiInstanceLoopCharacteristics") is None
    assert nodes["S1"].get(MA + "repeat") == "n/a" and MA + "repeat" not in nodes["S2"].attrib


def test_branches_have_a_split_one_flow_per_path_and_a_join():
    _, _, nodes, flows = _parts(_export(BRANCHES))
    assert nodes["S1_split"].tag == M + "exclusiveGateway" and nodes["S1_join"].tag == M + "exclusiveGateway"
    out = [(f.get("targetRef"), f.get("name")) for f in flows.values() if f.get("sourceRef") == "S1_split"]
    assert out == [("S2", "Bug"), ("S3", "Feature")]
    into_join = {f.get("sourceRef") for f in flows.values() if f.get("targetRef") == "S1_join"}
    assert into_join == {"S2", "S4"}
    assert [f.get("targetRef") for f in flows.values() if f.get("sourceRef") == "S1_join"] == ["S5"]
    # The two paths of the Feature/Bug block that use the human lane sit in different sub-rows of it.
    bounds = _bounds(ET.fromstring(_export(BRANCHES)))
    assert bounds["S2"][1] != bounds["S4"][1]


def test_a_loop_has_a_yes_flow_and_a_named_way_back():
    _, _, nodes, flows = _parts(_export(LOOPS))
    assert nodes["S2_loop"].tag == M + "exclusiveGateway"
    out = {f.get("name"): f.get("targetRef") for f in flows.values() if f.get("sourceRef") == "S2_loop"}
    assert out == {"Yes": "S3", "No · tests fail": "S1"}
    out = {f.get("name"): f.get("targetRef") for f in flows.values() if f.get("sourceRef") == "S3_loop"}
    assert out == {"Yes": "End", "No ·": "S2"}  # no condition written: the name stays "No ·"
    root = ET.fromstring(_export(LOOPS))
    back = next(e for e in root.iter(DI + "BPMNEdge") if e.get("bpmnElement") == "Flow_S2_loop_S1")
    ys = [int(p.get("y")) for p in back.findall(DD + "waypoint")]
    pool = _bounds(root)["Participant_1"]
    assert ys[1] > max(b[1] + b[3] for k, b in _bounds(root).items() if k in ("S1", "S2", "S3"))  # below the steps
    assert ys[1] < pool[1] + pool[3]  # but inside the pool


def test_phases_become_groups_with_category_values():
    root, process, _, _ = _parts(_export(BRANCHES))
    values = {v.get("id"): v.get("value") for v in root.iter(M + "categoryValue")}
    # As in the SVG, a phase band starts at a main step: "Fix" only has branch steps, so it runs inside "Triage".
    assert list(values.values()) == ["Triage", "Ship"]
    groups = process.findall(M + "group")
    assert [values[g.get("categoryValueRef")] for g in groups] == ["Triage", "Ship"]
    bounds = _bounds(root)
    pool = bounds["Participant_1"]
    for g in groups:
        assert bounds[g.get("id")][1] == pool[1] and bounds[g.get("id")][3] == pool[3]  # the full lane height
    first, second = (bounds[g.get("id")] for g in groups)
    assert first[0] < bounds["S1"][0] and first[0] + first[2] > bounds["S1_join"][0] + 50  # S1 up to the join
    assert first[0] + first[2] < second[0] <= bounds["S5"][0]
    assert _all_ids(root).index("Group_1") < _all_ids(root).index("BPMNDiagram_1")


def test_a_schedule_becomes_a_timer_start_event():
    _, _, nodes, _ = _parts(_export(LINEAR, wf=_wf(schedule_cron="0 9 * * 1")))
    timer = nodes["Start"].find(M + "timerEventDefinition")
    assert timer.find(M + "timeCycle").text == "0 9 * * 1"
    _, _, nodes, _ = _parts(_export(LINEAR))
    assert list(nodes["Start"]) == [] and nodes["End"].tag == M + "endEvent"
    _, _, nodes, _ = _parts(_export(LINEAR, wf=_wf(schedule_cron=""), schedule={"cron": "30 6 * * *"}))
    assert nodes["Start"].find(M + "timerEventDefinition").find(M + "timeCycle").text == "30 6 * * *"


def test_an_empty_design_is_a_start_and_an_end():
    _, process, nodes, flows = _parts(_export([]))
    assert sorted(nodes) == ["End", "Start"] and [(f.get("sourceRef"), f.get("targetRef")) for f in flows.values()] == [
        ("Start", "End")]
    assert [lane.get("id") for lane in process.find(M + "laneSet")] == ["Lane_maestro"]
    assert bpmn_to_design(_export([])).kind == "maestro"  # still restorable: the design has no steps to rebuild


def test_a_branch_block_that_opens_the_design_hangs_off_start():
    steps = [_step("S1", branch="Left"), _step("S2", "human", branch="Right"), _step("S3")]
    _, _, nodes, flows = _parts(_export(steps))
    assert nodes["Start_split"].tag == M + "exclusiveGateway" and "Start_join" in nodes
    assert next((f.get("sourceRef"), f.get("targetRef")) for f in flows.values()) == ("Start", "Start_split")


# --- Ids -----------------------------------------------------------------------------------------------


def test_ids_are_ncnames_unique_and_stable():
    steps = [_step("3a"), _step("S 1"), _step("S 1"), _step("Start"), _step(""), _step("S1_done", done_when="x"),
             _step("S1", "dispatch", done_when="pr_opened"), _step("-x")]
    xml = _export(steps)
    root, _, nodes, _ = _parts(xml)
    ids = _all_ids(root)
    assert len(ids) == len(set(ids)) and all(NCNAME.match(i) for i in ids)
    assert {"S_3a", "S_1", "S_1_2", "Start_2", "S_5", "S1_done", "S1", "S_-x"} <= set(nodes)
    assert "S1_done_2" in nodes  # the generated done event does not take the id of a step
    assert xml == _export(steps)  # same input, same ids and bytes
    assert "Process_7" in ids and "Participant_1" in ids and "Lane_maestro" in ids


# --- The stored design ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", list(DESIGNS))
def test_the_export_restores_the_design_exactly(name):
    design = _design_of(DESIGNS[name])
    xml = design_to_bpmn(_wf(), design, now=NOW)
    result = bpmn_to_design(xml)
    assert result.kind == "maestro" and result.design == design and result.findings == []
    assert result.name == "Page migration" and result.description == "Migrate page by page."
    root = ET.fromstring(xml)
    stored = next(root.iter(MA + "design"))
    assert stored.get("version") == "1" and stored.get("stepsHash") == steps_hash(design["steps"])
    exported = next(root.iter(MA + "exported"))
    assert exported.get("at") == "2026-11-23T09:30:00Z" and exported.get("app") == "maestro"


def test_a_branch_with_a_single_path_falls_back_to_the_review():
    steps = [_step("S1"), _step("S2", branch="Only"), _step("S3")]  # the import cannot rebuild a one-path split
    assert bpmn_to_design(_export(steps)).kind == "foreign"


def test_text_that_would_break_xml_survives():
    nasty = "A <preview> & ]]> \"quotes\" é 😀"
    illegal = "x\x00\x0b\ufffey"  # characters XML 1.0 cannot carry
    steps = [_step("S1", title=nasty, detail="line & <b>")]
    design = _design_of(steps, orchestration_prompt="# Mission ]]> and ]]]]> done", summary=nasty + illegal)
    xml = design_to_bpmn(_wf(name=nasty, description="x ]]> y"), design, now=NOW)
    root = ET.fromstring(xml)  # well-formed
    process = root.find(M + "process")
    assert "&lt;preview&gt; &amp;" in xml and "<![CDATA[" in xml
    assert process.find(M + "serviceTask").get("name") == nasty and process.get("name") == nasty
    result = bpmn_to_design(xml)  # the split CDATA sections read back as one text, and the JSON keeps everything
    assert result.kind == "maestro" and result.design == design
    assert result.name == nasty and result.description == "x ]]> y"


def test_characters_xml_forbids_are_dropped_from_attributes_and_text():
    steps = [_step("S1", title="Bad\x00 title\x0b", detail="de\x01tail", gate_owner="P\x02O", actor="human")]
    _, _, nodes, _ = _parts(_export(steps))  # still well-formed
    assert nodes["S1"].get("name") == "Bad title" and nodes["S1"].get(MA + "gateOwner") == "PO"
    assert nodes["S1"].find(M + "documentation").text.splitlines()[0] == "detail"


def test_the_export_does_not_use_an_llm(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    calls = len(fake.design_calls)
    with SessionLocal() as db:
        wf = db.get(Workflow, wf_id)
        xml = design_to_bpmn(wf, wf.design_json, now=NOW)
        assert design_to_bpmn(wf, wf.design_json, now=NOW) == xml
    assert len(fake.design_calls) == calls
    restored = bpmn_to_design(xml)  # the generated design goes through the import unchanged
    assert restored.kind == "maestro" and restored.design["orchestration_prompt"] == wf.content
    assert restored.design["steps"] == wf.design_json["steps"] and "&lt;preview&gt;" in xml


def test_layout_gives_the_structure_the_export_follows():
    layout = compute_layout(_design_of(BRANCHES))
    assert [b.phase for b in layout.bands] == ["Triage", "Ship"] and layout.bands[0].first == "S1"
    assert layout.bands[0].last == "S4" and layout.bands[1].first == "S5" and layout.bands[1].last == "S5"
    branch = layout.blocks[1]
    assert [p.label for p in branch.paths] == ["Bug", "Feature"] and [len(p.steps) for p in branch.paths] == [1, 2]
    assert [r.source.id for r in layout.loops] == ["S5"] and layout.loops[0].target.id == "S1"


# --- Route and button ---------------------------------------------------------------------------------


def test_the_owner_downloads_the_bpmn_and_sees_the_button(client, fake):
    _setup(client)
    wf_id = _create(client)
    _design(wf_id)
    page = client.get(f"/workflows/{wf_id}/design").text
    assert f'href="/workflows/{wf_id}/design.bpmn"' in page and "Export BPMN" in page
    r = client.get(f"/workflows/{wf_id}/design.bpmn")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/xml")
    assert r.headers["content-disposition"] == 'attachment; filename="maestro-page-migration.bpmn"'
    assert r.text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    assert bpmn_to_design(r.content).kind == "maestro"


def test_access_to_the_bpmn_is_the_same_as_for_the_svg(client, fake):
    make_user("admin", admin=True)
    alice, bob = make_user("alice"), make_user("bob")
    _connect(alice, "github")
    login(client, "alice")
    token = csrf(client, "/maestro/new")
    ids = {}
    for name, shared in (("Mine", False), ("Team flow", True)):
        data = {"name": name, "description": DESCRIPTION, "csrf_token": token}
        if shared:
            data["shared"] = "1"
        r = client.post("/maestro/new", data=data, follow_redirects=False)
        ids[name] = int(r.headers["location"].split("/")[2])
        _design(ids[name])
    with SessionLocal() as db:
        pending = Workflow(user_id=alice, name="Pending", content="", description=DESCRIPTION, shared=True)
        db.add(pending)
        db.commit()
        ids["Pending"] = pending.id
    for name, wf_id in ids.items():
        for path in ("design.svg", "design.bpmn"):
            assert client.get(f"/workflows/{wf_id}/{path}").status_code == (404 if name == "Pending" else 200)
    client.post("/logout", data={"csrf_token": csrf(client, "/workflows")})
    login(client, "bob")
    expected = {"Mine": 404, "Team flow": 200, "Pending": 404}
    for name, status in expected.items():
        assert client.get(f"/workflows/{ids[name]}/design.bpmn").status_code == status, name
        assert client.get(f"/workflows/{ids[name]}/design.svg").status_code == status, name
    assert client.get("/workflows/99999/design.bpmn").status_code == 404
    assert bob
