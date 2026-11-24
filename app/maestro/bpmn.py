"""BPMN 2.0 import and export for Maestro: safe, deterministic conversion. No LLM ever reads or writes the XML.

Export: `design_to_bpmn()` turns the stored design JSON into a BPMN 2.0 file with diagram interchange (lanes by
actor, tasks, gateways, flows), plus a `maestro:design` block that restores the design exactly. BPMN is an
exchange and governance format here, not an execution format: the process has `isExecutable="false"`.

Import. Two kinds of files come in:
- a Maestro file that was not changed (`maestro:design` holds the stored design and `maestro:stepsHash` matches the
  steps rebuilt from the BPMN elements): the stored design is restored as is;
- any other file (another tool, or a Maestro file edited since): it becomes an English description plus
  deterministic findings, one per element Maestro and Upsun Dispatch cannot run as drawn. The review step then
  proposes routes for each finding.

The Maestro contract (the `maestro:` namespace, `maestro:design`, `steps_hash()`) is shared with the BPMN export.
Rebuilding the steps reads, per task: the id, `name` (title), the first line of the documentation (detail) and the
`maestro:*` attributes; `branch`, `loop` and `loop_to` come from the generated `*_split` / `*_join` / `*_loop` gateways.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from xml.etree.ElementTree import Element, ParseError, SubElement, indent, register_namespace, tostring

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

from app.maestro.render import (
    ACTOR_LABELS,
    ACTOR_ORDER,
    DISPATCH_LABELS,
    DONE_LABELS,
    BranchBlock,
    Layout,
    PhaseBand,
    Placement,
    StepBlock,
    compute_layout,
    tag_text,
)

MAX_BYTES = 2 * 1024 * 1024
MODEL_NS = "http://www.omg.org/spec/BPMN/20100524/MODEL"
MAESTRO_NS = "https://upsun.com/schema/maestro/1"
MODEL = "{" + MODEL_NS + "}"
MAESTRO = "{" + MAESTRO_NS + "}"

# The step fields of DESIGN_SCHEMA that make up the steps hash.
STEP_FIELDS = ("id", "phase", "title", "detail", "actor", "dispatch_workflow", "gate_owner", "repeat", "loop",
               "branch", "loop_to", "done_when")
ACTORS = ("maestro", "dispatch", "human", "upsun", "external")

TASK_BY_TAG = {"userTask": "human", "serviceTask": "maestro", "callActivity": "dispatch", "task": "external"}
EVENT_TAGS = {"startEvent", "endEvent", "intermediateCatchEvent", "intermediateThrowEvent", "boundaryEvent"}
GATEWAY_TAGS = {"exclusiveGateway", "parallelGateway", "inclusiveGateway", "eventBasedGateway", "complexGateway"}
ACTIVITY_TAGS = {"task", "userTask", "serviceTask", "callActivity", "sendTask", "receiveTask", "manualTask",
                 "scriptTask", "businessRuleTask", "subProcess", "transaction", "adHocSubProcess"}
FLOW_NODE_TAGS = EVENT_TAGS | GATEWAY_TAGS | ACTIVITY_TAGS
CONTAINER_TAGS = {"subProcess", "transaction", "adHocSubProcess"}
DATA_TAGS = {"dataObject", "dataObjectReference", "dataStore", "dataStoreReference"}

MAX_DESCRIPTION = 20000
MAX_DOC = 300
MAX_LISTED_NODES = 400


class BpmnImportError(ValueError):
    """The file cannot be imported. The message is shown to the user."""


@dataclass
class ImportResult:
    kind: str  # "maestro" (unchanged export, restored as is) or "foreign" (needs the review)
    name: str
    description: str
    design: dict[str, Any] | None = None
    findings: list[dict[str, str]] = field(default_factory=list)
    # Facts used to find the user's existing project this file comes from (or looks like)
    source_id: int | None = None  # id of the exporting project, from the process id "Process_<id>"
    steps_fingerprint: str | None = None  # steps_hash() of the steps rebuilt from the file


def steps_hash(steps: list[dict[str, Any]]) -> str:
    """SHA-256 of a canonical JSON of the step list: sorted keys, only the schema step fields, values stripped."""
    canonical = [{k: str((st or {}).get(k) or "").strip() for k in STEP_FIELDS} for st in steps or []]
    payload = json.dumps(canonical, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --- Safe parsing ------------------------------------------------------------------


def parse_bpmn(xml: bytes | str) -> Element:
    """Parse and check a BPMN 2.0 file. Raises BpmnImportError with a readable message."""
    data = xml.encode("utf-8") if isinstance(xml, str) else bytes(xml)
    if len(data) > MAX_BYTES:
        raise BpmnImportError("The BPMN file is larger than 2 MB.")
    if not data.strip():
        raise BpmnImportError("The BPMN file is empty.")
    try:
        root = safe_fromstring(data, forbid_dtd=True, forbid_entities=True, forbid_external=True)
    except DefusedXmlException:
        raise BpmnImportError("The BPMN file contains a DOCTYPE, entities or external references, "
                              "which are not allowed.") from None
    except (ParseError, UnicodeDecodeError, ValueError):
        raise BpmnImportError("The file is not valid XML.") from None
    if root.tag != MODEL + "definitions":
        raise BpmnImportError("This is not a BPMN 2.0 file: the root element must be definitions in the "
                              f"{MODEL_NS} namespace.")
    if not root.findall(MODEL + "process"):
        raise BpmnImportError("The BPMN file has no process.")
    return root


def _local(el: Element) -> str | None:
    """Local name of an element in the BPMN model namespace (None for anything else)."""
    tag = el.tag
    return tag[len(MODEL):] if isinstance(tag, str) and tag.startswith(MODEL) else None


def _clip(text: str | None, limit: int) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _doc(el: Element) -> str:
    d = el.find(MODEL + "documentation")
    return (d.text or "").strip() if d is not None else ""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:40]


def _assign_ids(root: Element) -> dict[Element, str]:
    """A unique id for every BPMN element: its own id, else a slug of its name, else a synthetic one."""
    ids: dict[Element, str] = {}
    used: set[str] = set()
    for el in root.iter():
        local = _local(el)
        if local is None:
            continue
        raw = (el.get("id") or "").strip()
        if raw and raw not in used:
            eid = raw
        else:
            base = _slug(el.get("name") or "") or _slug(raw) or local
            eid, n = base, 2
            while eid in used:
                eid, n = f"{base}_{n}", n + 1
        used.add(eid)
        ids[el] = eid
    return ids


# --- Flow graph of one process or sub process -----------------------------------------


@dataclass
class _Flow:
    id: str
    source: str
    target: str
    name: str
    condition: str


class _Graph:
    def __init__(self, container: Element, ids: dict[Element, str]) -> None:
        self.ids = ids
        self.nodes: dict[str, Element] = {}
        for child in container:
            if _local(child) in FLOW_NODE_TAGS:
                self.nodes[ids[child]] = child
        self.flows: list[_Flow] = []
        self.out: dict[str, list[_Flow]] = {n: [] for n in self.nodes}
        self.incoming: dict[str, list[_Flow]] = {n: [] for n in self.nodes}
        for child in container.findall(MODEL + "sequenceFlow"):
            cond = child.find(MODEL + "conditionExpression")
            flow = _Flow(ids[child], (child.get("sourceRef") or "").strip(), (child.get("targetRef") or "").strip(),
                         (child.get("name") or "").strip(), (cond.text or "").strip() if cond is not None else "")
            if flow.source in self.nodes and flow.target in self.nodes:
                self.flows.append(flow)
                self.out[flow.source].append(flow)
                self.incoming[flow.target].append(flow)

    def order(self) -> tuple[list[str], set[str]]:
        """Nodes in flow order and the ids of the flows that go back (loops).

        Reverse post-order of a depth-first walk: the paths of an exclusive split come one after the other, then
        the join and what follows. The walk is iterative, so a long chain cannot overflow the stack.
        """
        visited: set[str] = set()
        back: set[str] = set()

        def walk(start: str) -> list[str]:
            post: list[str] = []
            visited.add(start)
            on_stack = {start}
            stack = [(start, iter(reversed(self.out[start])))]
            while stack:
                node, it = stack[-1]
                for flow in it:
                    if flow.target in on_stack:
                        back.add(flow.id)
                    elif flow.target not in visited:
                        visited.add(flow.target)
                        on_stack.add(flow.target)
                        stack.append((flow.target, iter(reversed(self.out[flow.target]))))
                        break
                else:
                    stack.pop()
                    on_stack.discard(node)
                    post.append(node)
            return post

        roots = [n for n in self.nodes if not self.incoming[n]]
        main: list[str] = []
        for r in reversed(roots):
            main += walk(r)
        rest: list[str] = []
        for n in reversed(list(self.nodes)):
            if n not in visited:
                rest += walk(n)
        return list(reversed(main)) + list(reversed(rest)), back


# --- Findings: what Maestro and Upsun Dispatch cannot run as drawn ------------------------------


def _event_kinds(el: Element) -> list[str]:
    return [(_local(c) or "")[: -len("EventDefinition")] for c in el
            if (_local(c) or "").endswith("EventDefinition")]


def _label(el: Element, ids: dict[Element, str]) -> str:
    return (el.get("name") or "").strip() or ids.get(el, "")


def extract_findings(root: Element, ids: dict[Element, str] | None = None) -> list[dict[str, str]]:
    """One finding per unsupported element, in document order: element_id, element_type, element_name, detail."""
    ids = ids or _assign_ids(root)
    by_ref: dict[str, Element] = {}
    for el, eid in ids.items():
        by_ref.setdefault(eid, el)
    message_ends: set[str] = set()
    for mf in root.iter(MODEL + "messageFlow"):
        message_ends |= {(mf.get("sourceRef") or "").strip(), (mf.get("targetRef") or "").strip()}
    participants = list(root.iter(MODEL + "participant"))
    findings: list[dict[str, str]] = []

    def name_of(ref: str) -> str:
        el = by_ref.get(ref)
        return _label(el, ids) if el is not None else ref

    for el in root.iter():
        local = _local(el)
        if local is None:
            continue
        kinds = _event_kinds(el) if local in EVENT_TAGS else []
        detail = ""
        if local == "boundaryEvent":
            detail = (f"Boundary event ({', '.join(kinds) or 'untyped'}) attached to "
                      f"{name_of((el.get('attachedToRef') or '').strip())}")
        elif local == "messageFlow":
            detail = (f"Message from {name_of((el.get('sourceRef') or '').strip())} "
                      f"to {name_of((el.get('targetRef') or '').strip())}")
        elif local == "participant" and participants.index(el) > 0 and ids[el] not in message_ends:
            detail = "A second participant (pool): Maestro runs one process"
        elif local in CONTAINER_TAGS and el.get("triggeredByEvent") == "true":
            detail = "Event subprocess"
        elif local == "transaction":
            detail = "Transaction subprocess"
        elif local in ACTIVITY_TAGS and el.get("isForCompensation") == "true":
            detail = "Compensation handler"
        elif local in ("parallelGateway", "inclusiveGateway"):
            detail = f"{local[: -len('Gateway')].capitalize()} gateway: branches that run at the same time"
        elif local in DATA_TAGS:
            detail = f"Data element ({local})"
        elif local in ("scriptTask", "businessRuleTask"):
            detail = "Script" if local == "scriptTask" else "Business rule"
            detail += " task: Maestro does not run scripts or rule engines"
        elif local in EVENT_TAGS and {"signal", "escalation", "compensate"} & set(kinds):
            detail = f"{'/'.join(k for k in kinds if k in ('signal', 'escalation', 'compensate'))} event"
        if not detail:
            continue
        findings.append({"element_id": ids[el], "element_type": local, "element_name": (el.get("name") or "").strip(),
                         "detail": detail})
    return findings


# --- Description in English (what the review and the designer read) ---------------------------------


def _event_text(el: Element) -> str:
    kinds = _event_kinds(el)
    parts = []
    for child in el:
        if _local(child) == "timerEventDefinition":
            for sub in child:
                if (_local(sub) or "") in ("timeCycle", "timeDate", "timeDuration") and (sub.text or "").strip():
                    parts.append(f"timer {_local(sub)} {sub.text.strip()}")
                    break
            else:
                parts.append("timer")
        elif _local(child) == "conditionalEventDefinition":
            cond = child.find(MODEL + "condition")
            parts.append("wait until " + _clip(cond.text, 80) if cond is not None else "conditional")
    if not parts and kinds:
        parts = kinds
    return ", ".join(parts)


def _node_line(node: Element, graph: _Graph, ids: dict[Element, str], lanes: dict[str, str],
               targets: dict[str, str], back: set[str]) -> str:
    local = _local(node) or "element"
    eid = ids[node]
    name = _clip(node.get("name"), 120)
    bits = [local]
    if eid in lanes:
        bits.append(f"lane {lanes[eid]}")
    bits.append(f"id {eid}")
    if local in EVENT_TAGS:
        text = _event_text(node)
        if text:
            bits.append(text)
    if local in ACTIVITY_TAGS:
        for child in node:
            kind = _local(child)
            if kind == "multiInstanceLoopCharacteristics":
                bits.append("multi-instance: " + ("sequential" if child.get("isSequential") == "true" else "parallel"))
            elif kind == "standardLoopCharacteristics":
                bits.append("standard loop")
        if node.get("calledElement"):
            bits.append(f"calls {node.get('calledElement')}")
    line = f'- {"Gateway" if local in GATEWAY_TAGS else "Step"} "{name}" [{", ".join(bits)}]'
    doc = _clip(_doc(node), MAX_DOC)
    if doc:
        line += f": {doc}"
    outgoing = graph.out[eid]
    describe = len(outgoing) > 1 or any(f.name or f.condition or f.id in back for f in outgoing)
    if describe:
        nexts = []
        for f in outgoing:
            label = f.name or _clip(f.condition, 80)
            kind = "loops back to" if f.id in back else "goes to"
            nexts.append(f'{"when " + chr(34) + label + chr(34) + " " if label else ""}{kind} '
                         f'"{targets[f.target]}" ({f.target})')
        line += "\n    Next: " + "; ".join(nexts)
    return line


def _describe_container(container: Element, ids: dict[Element, str], lanes: dict[str, str], out: list[str],
                        depth: int = 0) -> None:
    graph = _Graph(container, ids)
    order, back = graph.order()
    targets = {n: _clip(el.get("name"), 80) or n for n, el in graph.nodes.items()}
    for n in order[:MAX_LISTED_NODES]:
        out.append(_node_line(graph.nodes[n], graph, ids, lanes, targets, back))
    if len(order) > MAX_LISTED_NODES:
        out.append(f"- … and {len(order) - MAX_LISTED_NODES} more elements")
    if depth < 4:
        for child in container:
            if _local(child) in CONTAINER_TAGS:
                out.append(f'Sub process "{_clip(child.get("name"), 80)}" ({ids[child]}) contains:')
                _describe_container(child, ids, lanes, out, depth + 1)


def build_description(root: Element, ids: dict[Element, str], findings: list[dict[str, str]]) -> str:
    """Structured English description of the process, in flow order (deterministic)."""
    lanes: dict[str, str] = {}
    lane_names: list[str] = []
    for lane in root.iter(MODEL + "lane"):
        lname = _clip(lane.get("name"), 80) or ids[lane]
        lane_names.append(lname)
        for ref in lane.iter(MODEL + "flowNodeRef"):
            lanes[(ref.text or "").strip()] = lname
    participants = {(p.get("processRef") or "").strip(): _clip(p.get("name"), 80) for p in root.iter(MODEL + "participant")}
    out = ["This process was imported from a BPMN 2.0 file. Maestro does not execute BPMN: the file describes the "
           "process to design."]
    for process in root.findall(MODEL + "process"):
        pname = _clip(process.get("name"), 120) or participants.get(ids[process], "") or ids[process]
        out.append(f'\nProcess: "{pname}"')
        if participants.get(ids[process]) and participants[ids[process]] != pname:
            out.append(f"Participant (pool): {participants[ids[process]]}")
        doc = _clip(_doc(process), 600)
        if doc:
            out.append(f"Documentation: {doc}")
        if lane_names:
            out.append("Lanes (roles): " + ", ".join(lane_names))
        out.append("Elements in flow order (the order the work flows, one path after the other at a split):")
        _describe_container(process, ids, lanes, out)
    if findings:
        out.append("\nElements Maestro and Upsun Dispatch cannot run as drawn (each needs a decision):")
        out += [f'- {f["element_type"]} "{f["element_name"] or f["element_id"]}" (id {f["element_id"]}): {f["detail"]}'
                for f in findings]
    text = "\n".join(out)
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 1].rstrip() + "…"


# --- Maestro files ---------------------------------------------------------------------------


def _first_line(text: str) -> str:
    return text.strip().split("\n", 1)[0].strip()


def steps_from_bpmn(root: Element, ids: dict[Element, str] | None = None) -> list[dict[str, str]] | None:
    """Rebuild the design steps from the BPMN elements of a Maestro export (None when it does not look like one)."""
    ids = ids or _assign_ids(root)
    processes = root.findall(MODEL + "process")
    if len(processes) != 1:
        return None
    graph = _Graph(processes[0], ids)
    order, back = graph.order()
    steps: dict[str, dict[str, str]] = {}
    for nid in order:
        el = graph.nodes[nid]
        local = _local(el) or ""
        if local not in TASK_BY_TAG:
            continue
        actor = el.get(MAESTRO + "actor") or TASK_BY_TAG[local]
        steps[nid] = {
            "id": nid, "phase": el.get(MAESTRO + "phase", ""), "title": (el.get("name") or "").strip(),
            "detail": _first_line(_doc(el)), "actor": actor,
            "dispatch_workflow": el.get(MAESTRO + "dispatchWorkflow") or el.get("calledElement") or "none",
            "gate_owner": el.get(MAESTRO + "gateOwner", ""), "repeat": el.get(MAESTRO + "repeat", ""),
            "loop": "", "branch": "", "loop_to": "", "done_when": el.get(MAESTRO + "doneWhen") or "none",
        }
    for nid, el in graph.nodes.items():
        if _local(el) != "exclusiveGateway":
            continue
        if nid.endswith("_loop") and nid[: -len("_loop")] in steps:
            for f in graph.out[nid]:
                if f.id in back or f.name.startswith("No"):
                    step = steps[nid[: -len("_loop")]]
                    step["loop"] = re.sub(r"^No\s*·\s*", "", f.name).strip()
                    step["loop_to"] = f.target
        elif nid.endswith("_split") and len(graph.out[nid]) > 1:
            join = nid[: -len("_split")] + "_join"
            for f in graph.out[nid]:
                seen: set[str] = set()
                pending = [f.target]
                while pending:
                    cur = pending.pop()
                    if cur in seen or cur == join:
                        continue
                    seen.add(cur)
                    if cur in steps:
                        steps[cur]["branch"] = f.name
                    pending += [g.target for g in graph.out[cur] if g.id not in back]
    return [steps[n] for n in order if n in steps]


def _is_untouched_export(root: Element, ids: dict[Element, str]) -> bool:
    """Only the elements the exporter writes: tasks, start/end, and generated gateways and done events."""
    if len(root.findall(MODEL + "process")) != 1 or len(list(root.iter(MODEL + "participant"))) > 1:
        return False
    for el in root.findall(MODEL + "process")[0]:
        local = _local(el)
        eid = ids.get(el, "")
        if local in ("sequenceFlow", "laneSet", "documentation", "extensionElements", "group", "category"):
            continue
        if local in TASK_BY_TAG or local in ("startEvent", "endEvent"):
            continue
        if local == "intermediateCatchEvent" and eid.endswith("_done"):
            continue
        if local == "exclusiveGateway" and eid.endswith(("_split", "_join", "_loop")):
            continue
        return False
    return True


def _stored_design(root: Element) -> tuple[dict[str, Any], str] | None:
    for el in root.iter(MAESTRO + "design"):
        try:
            stored = json.loads(el.text or "")
        except ValueError:
            return None
        if isinstance(stored, dict) and isinstance(stored.get("design"), dict):
            return stored, (el.get("stepsHash") or "").strip()
    return None


def bpmn_to_design(xml: bytes | str) -> ImportResult:
    """Parse a BPMN 2.0 file into an ImportResult (raises BpmnImportError for an invalid file)."""
    root = parse_bpmn(xml)
    ids = _assign_ids(root)
    findings = extract_findings(root, ids)
    processes = root.findall(MODEL + "process")
    participant = next(iter(root.iter(MODEL + "participant")), None)
    name = (_clip(processes[0].get("name"), 128) or (_clip(participant.get("name"), 128) if participant is not None
            else "") or _clip(root.get("name"), 128) or "Imported BPMN process")
    stored = _stored_design(root)
    try:
        rebuilt = steps_from_bpmn(root, ids)
    except Exception:  # noqa: BLE001 - the fingerprint is only a hint for the match with an existing project
        rebuilt = None
    fingerprint = steps_hash(rebuilt) if rebuilt is not None else None
    source_id = None
    if stored is not None and processes:
        found = re.fullmatch(r"Process_(\d+)", processes[0].get("id") or "")
        source_id = int(found.group(1)) if found else None
    if stored and not findings and _is_untouched_export(root, ids):
        data, declared = stored
        design = data["design"]
        stored_steps = design.get("steps")
        if (isinstance(stored_steps, list) and rebuilt is not None and design.get("orchestration_prompt")
                and steps_hash(rebuilt) == declared == steps_hash(stored_steps)):
            return ImportResult("maestro", _clip(str(data.get("name") or ""), 128) or name,
                                str(data.get("description") or ""), design, [], source_id, fingerprint)
    return ImportResult("foreign", name, build_description(root, ids, findings), None, findings,
                        source_id, fingerprint)


# --- Export: stored design to BPMN 2.0 with diagram interchange ------------------------------------------

BPMNDI_NS = "http://www.omg.org/spec/BPMN/20100524/DI"
DC_NS = "http://www.omg.org/spec/DD/20100524/DC"
DI_NS = "http://www.omg.org/spec/DD/20100524/DI"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
BPMNDI = "{" + BPMNDI_NS + "}"
DC = "{" + DC_NS + "}"
DI = "{" + DI_NS + "}"
XSI = "{" + XSI_NS + "}"
EXPORT_VERSION = "1"

TASK_TAGS = {"human": "userTask", "dispatch": "callActivity", "external": "task"}  # maestro, upsun: serviceTask
# (maestro: attribute, step field): every task carries them, unless the value is empty or "none".
MAESTRO_ATTRS = (("actor", "actor"), ("dispatchWorkflow", "dispatch_workflow"), ("doneWhen", "done_when"),
                 ("gateOwner", "gate_owner"), ("repeat", "repeat"), ("phase", "phase"))

# Diagram geometry in BPMN pixels: fixed shape sizes, one cell per node along the flow.
TASK_W, TASK_H = 100, 80
EVENT_SIZE, GATEWAY_SIZE = 36, 50
H_GAP = 60  # between two nodes
SUBROW_H = 110  # height of a lane for one row of nodes; concurrent branch paths stack in sub-rows
POOL_X, POOL_Y = 20, 20
POOL_HEADER = 30  # the strip with the participant name; the lanes start right of it
CELL_PAD = 40  # between the pool edge and the first or last node
LOOP_STEP = 16  # between two loop routes, as LOOP_LANE does in the SVG
LOOP_PAD = 24  # room left below the last lane's content for the loop routes
GROUP_PAD = 12

_XML_ILLEGAL = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")
_JSON_ILLEGAL = re.compile("[\ud800-\udfff\ufffe\uffff]")


def _x(value: Any) -> str:
    """Text for XML 1.0: the characters the standard forbids are dropped."""
    return _XML_ILLEGAL.sub("", "" if value is None else str(value))


def _ncname(raw: str, index: int) -> str:
    """A valid XML NCName from a step id: bad characters become "_", a bad first character gets the prefix "S_"."""
    name = re.sub(r"[^A-Za-z0-9_.-]", "_", raw.strip())
    if not name:
        return f"S_{index}"
    return name if re.match(r"[A-Za-z_]", name) else "S_" + name


class _Ids:
    """Unique element ids: a taken id gets the suffix _2, _3, ..."""

    def __init__(self, reserved: list[str]) -> None:
        self.used: set[str] = set(reserved)

    def take(self, base: str) -> str:
        eid, n = base, 2
        while eid in self.used:
            eid, n = f"{base}_{n}", n + 1
        self.used.add(eid)
        return eid


def _el(parent: Element | None, tag: str, attrs: dict[str, str] | None = None, text: str | None = None) -> Element:
    clean = {k: _x(v) for k, v in (attrs or {}).items()}
    el = Element(tag, clean) if parent is None else SubElement(parent, tag, clean)
    if text is not None:
        el.text = _x(text)
    return el


def _actor(step: dict[str, Any]) -> str:
    return step.get("actor") if step.get("actor") in ACTOR_LABELS else "maestro"  # type: ignore[return-value]


def _placements(layout: Layout) -> Iterator[Placement]:
    for block in layout.blocks:
        if isinstance(block, StepBlock):
            yield block.place
        else:
            for path in block.paths:
                yield from path.steps


@dataclass
class _Node:
    id: str
    tag: str
    lane: str
    w: int
    h: int
    col: int  # offset along the flow
    sub: int  # sub-row inside its lane
    block: int | None  # index of the layout block it belongs to
    label: str = ""  # drawn next to the shape (events)
    x: int = 0
    y: int = 0

    @property
    def cx(self) -> int:
        return self.x + self.w // 2

    @property
    def cy(self) -> int:
        return self.y + self.h // 2


@dataclass
class _Edge:
    id: str
    source: str
    target: str
    name: str = ""
    route: int | None = None  # a way back (loop): its index, for its own track below the lanes


class _Export:
    """Builds the process elements and the geometry of one design."""

    def __init__(self, workflow: Any, design: dict[str, Any], layout: Layout) -> None:
        self.workflow, self.design, self.layout = workflow, design, layout
        used = {_actor(s) for s in layout.steps}
        self.lanes = [a for a in ACTOR_ORDER if a in used] or ["maestro"]
        process_id = _ncname(f"Process_{getattr(workflow, 'id', None) or 1}", 1)
        self.process_id = process_id
        self.ids = _Ids(["Definitions_1", "Collaboration_1", "Participant_1", process_id, "Start", "End",
                         "BPMNDiagram_1", "BPMNPlane_1", "LaneSet_1", "Category_1",
                         *(f"Lane_{a}" for a in ACTOR_ORDER)])
        self.step_id: dict[int, str] = {}  # id(step dict) -> element id
        self.by_raw: dict[str, str] = {}  # the id written in the design -> element id
        for i, s in enumerate(layout.steps, 1):
            raw = str(s.get("id") or "").strip()
            self.step_id[id(s)] = self.ids.take(_ncname(raw, i))
            self.by_raw.setdefault(raw, self.step_id[id(s)])
        self.nodes: dict[str, _Node] = {}
        self.elements: list[Element] = []  # flow nodes, in creation order
        self.edges: list[_Edge] = []
        self.backs: list[tuple[str, str, str]] = []  # (loop gateway, step it goes back to, flow name)
        self.pending: list[tuple[str, str]] = []  # (node, flow name) the next node is connected from
        self.col = 0
        self.block: int | None = None
        self.rank: dict[str, int] = {}  # lane -> sub-row, for the nodes of the branch path being built
        # Filled by measure():
        self.lane_box: dict[str, tuple[int, int, int, int]] = {}
        self.pool = (POOL_X, POOL_Y, 0, 0)
        self.route_y = 0

    # --- Nodes and flows

    def node(self, el: Element, nid: str, lane: str, w: int, h: int, label: str = "") -> None:
        tag = el.tag[len(MODEL):]
        self.nodes[nid] = _Node(nid, tag, lane, w, h, self.col, self.rank.get(lane, 0), self.block, label)
        self.elements.append(el)
        self.col += w + H_GAP

    def link(self, source: str, target: str, name: str = "") -> None:
        self.edges.append(_Edge(self.ids.take(f"Flow_{source}_{target}"), source, target, name))

    def connect(self, target: str) -> None:
        for source, name in self.pending:
            self.link(source, target, name)
        self.pending = []

    def task(self, s: dict[str, Any], sid: str, actor: str) -> Element:
        attrs = {"id": sid, "name": str(s.get("title") or "")}
        dispatch = str(s.get("dispatch_workflow") or "").strip()
        if actor == "dispatch" and dispatch not in ("", "none"):
            attrs["calledElement"] = dispatch
        for attr, key in MAESTRO_ATTRS:
            value = str(s.get(key) or "").strip()
            if value and value != "none":
                attrs[MAESTRO + attr] = value
        el = _el(None, MODEL + TASK_TAGS.get(actor, "serviceTask"), attrs)
        # The first documentation line is the detail; the other lines only repeat what the attributes say.
        detail = str(s.get("detail") or "").strip()
        extras = []
        if actor == "human" and str(s.get("gate_owner") or "").strip():
            extras.append(f"Gate owner: {str(s['gate_owner']).strip()}")
        if actor == "dispatch" and dispatch in DISPATCH_LABELS:
            extras.append(f"Dispatch workflow: {DISPATCH_LABELS[dispatch]}")
        repeat = tag_text(s.get("repeat"))
        if repeat:
            extras.append(f"Repeat: {repeat}")
        if detail:
            _el(el, MODEL + "documentation", text="\n".join([detail, *extras]))
        elif extras:  # an empty first documentation keeps "detail" empty when the file is imported again
            _el(el, MODEL + "documentation")
            _el(el, MODEL + "documentation", text="\n".join(extras))
        if repeat:
            multi = _el(el, MODEL + "multiInstanceLoopCharacteristics", {"isSequential": "true"})
            _el(multi, MODEL + "documentation", text=repeat)
        return el

    def add_step(self, p: Placement) -> None:
        """The step, then its done event and its loop gateway when it has them."""
        s, sid, lane = p.step, self.step_id[id(p.step)], p.actor
        self.node(self.task(s, sid, p.actor), sid, lane, TASK_W, TASK_H)
        self.connect(sid)
        last, flow = sid, ""
        done = str(s.get("done_when") or "").strip()
        if done in DONE_LABELS:
            did = self.ids.take(f"{sid}_done")
            label = "Done when " + DONE_LABELS[done]
            event = _el(None, MODEL + "intermediateCatchEvent", {"id": did, "name": label})
            definition = _el(event, MODEL + "conditionalEventDefinition")
            _el(definition, MODEL + "condition", {XSI + "type": "bpmn:tFormalExpression"}, DONE_LABELS[done])
            self.node(event, did, lane, EVENT_SIZE, EVENT_SIZE, label)
            self.link(last, did)
            last = did
        loop_to = str(s.get("loop_to") or "").strip()
        if loop_to in self.by_raw:
            gid = self.ids.take(f"{sid}_loop")
            self.node(_el(None, MODEL + "exclusiveGateway", {"id": gid}), gid, lane, GATEWAY_SIZE, GATEWAY_SIZE)
            self.link(last, gid)
            loop = str(s.get("loop") or "").strip()
            self.backs.append((gid, self.by_raw[loop_to], f"No · {loop}".strip()))
            last, flow = gid, "Yes"
        self.pending = [(last, flow)]

    @staticmethod
    def path_ranks(block: BranchBlock) -> list[dict[str, int]]:
        """Paths that use the same lane go in different sub-rows of it."""
        seen: dict[str, int] = {}
        ranks = []
        for path in block.paths:
            lanes = {p.actor for p in path.steps}
            ranks.append({lane: seen.get(lane, 0) for lane in lanes})
            for lane in lanes:
                seen[lane] = seen.get(lane, 0) + 1
        return ranks

    def build(self, cron: str) -> None:
        steps = self.layout.steps
        first_lane = _actor(steps[0]) if steps else self.lanes[0]
        last_lane = _actor(steps[-1]) if steps else self.lanes[0]
        start = _el(None, MODEL + "startEvent", {"id": "Start"})
        if cron:
            timer = _el(start, MODEL + "timerEventDefinition")
            _el(timer, MODEL + "timeCycle", {XSI + "type": "bpmn:tFormalExpression"}, cron)
        self.node(start, "Start", first_lane, EVENT_SIZE, EVENT_SIZE)
        self.pending = [("Start", "")]
        decider = ("Start", first_lane)  # what a branch block hangs off: the step before it, else Start
        for block in self.layout.blocks:
            self.block = block.index
            if isinstance(block, StepBlock):
                self.add_step(block.place)
                decider = (self.step_id[id(block.place.step)], block.place.actor)
                continue
            base, lane = decider
            split = self.ids.take(f"{base}_split")
            self.node(_el(None, MODEL + "exclusiveGateway", {"id": split}), split, lane, GATEWAY_SIZE, GATEWAY_SIZE)
            self.connect(split)
            start_col, ends, tails = self.col, [], []
            for path, rank in zip(block.paths, self.path_ranks(block)):
                self.col, self.rank = start_col, rank
                self.pending = [(split, path.label)]
                for p in path.steps:
                    self.add_step(p)
                tails += self.pending
                ends.append(self.col)
            self.rank, self.col = {}, max(ends)
            join = self.ids.take(f"{base}_join")
            self.node(_el(None, MODEL + "exclusiveGateway", {"id": join}), join, lane, GATEWAY_SIZE, GATEWAY_SIZE)
            self.pending = tails
            self.connect(join)
            self.pending = [(join, "")]
        self.block = None
        self.node(_el(None, MODEL + "endEvent", {"id": "End"}), "End", last_lane, EVENT_SIZE, EVENT_SIZE)
        self.connect("End")
        for k, (gid, target, name) in enumerate(self.backs):  # after the forward flows: "Yes" comes first
            self.edges.append(_Edge(self.ids.take(f"Flow_{gid}_{target}"), gid, target, name, route=k))

    # --- Geometry

    def measure(self) -> None:
        rows = {lane: 1 for lane in self.lanes}
        for n in self.nodes.values():
            rows[n.lane] = max(rows[n.lane], n.sub + 1)
        heights = {lane: rows[lane] * SUBROW_H for lane in self.lanes}
        content_bottom = POOL_Y + sum(heights.values())
        self.route_y = content_bottom + LOOP_STEP  # the first way back runs here, the others below it
        if self.backs:
            heights[self.lanes[-1]] += LOOP_PAD + LOOP_STEP * len(self.backs)
        left = POOL_X + POOL_HEADER
        origin = left + CELL_PAD
        for n in self.nodes.values():
            n.x = origin + n.col
        right = max(n.x + n.w for n in self.nodes.values()) + CELL_PAD
        top = POOL_Y
        for lane in self.lanes:
            self.lane_box[lane] = (left, top, right - left, heights[lane])
            top += heights[lane]
        self.pool = (POOL_X, POOL_Y, right - POOL_X, top - POOL_Y)
        for n in self.nodes.values():
            _, lane_top, _, _ = self.lane_box[n.lane]
            n.y = lane_top + n.sub * SUBROW_H + (SUBROW_H - n.h) // 2

    def waypoints(self, e: _Edge) -> list[tuple[int, int]]:
        """Orthogonal path of a flow: a line, or horizontal-vertical-horizontal; a way back runs under the lanes."""
        src, dst = self.nodes[e.source], self.nodes[e.target]
        if e.route is not None:
            ry = self.route_y + LOOP_STEP * e.route
            return [(src.cx, src.y + src.h), (src.cx, ry), (dst.cx, ry), (dst.cx, dst.y + dst.h)]
        x1, y1, x2, y2 = src.x + src.w, src.cy, dst.x, dst.cy
        if y1 == y2:
            return [(x1, y1), (x2, y2)]
        mid = (x1 + x2) // 2
        return [(x1, y1), (mid, y1), (mid, y2), (x2, y2)]

    def groups(self, bands: list[PhaseBand]) -> list[tuple[int, int, int]]:
        """(band index, left, right) of each phase run: from its first to its last node along the flow."""
        out = []
        for i, band in enumerate(bands):
            stop = bands[i + 1].block if i + 1 < len(bands) else None
            members = [n for n in self.nodes.values()
                       if n.block is not None and n.block >= band.block and (stop is None or n.block < stop)]
            if members:
                out.append((i, min(n.x for n in members) - GROUP_PAD, max(n.x + n.w for n in members) + GROUP_PAD))
        return out


def _bounds(parent: Element, x: int, y: int, w: int, h: int) -> None:
    _el(parent, DC + "Bounds", {"x": str(x), "y": str(y), "width": str(w), "height": str(h)})


def _label_width(text: str) -> int:
    return min(160, max(24, int(len(text) * 6.5) + 8))


def _shape(plane: Element, ref: str, x: int, y: int, w: int, h: int, *, horizontal: bool = False, marker: bool = False,
           label: tuple[int, int, int, int] | None = None) -> None:
    attrs = {"id": f"{ref}_di", "bpmnElement": ref}
    if horizontal:
        attrs["isHorizontal"] = "true"
    if marker:
        attrs["isMarkerVisible"] = "true"
    shape = _el(plane, BPMNDI + "BPMNShape", attrs)
    _bounds(shape, x, y, w, h)
    if label:
        _bounds(_el(shape, BPMNDI + "BPMNLabel"), *label)


def _cdata(text: str) -> str:
    return "<![CDATA[" + text.replace("]]>", "]]]]><![CDATA[>") + "]]>"


def design_to_bpmn(workflow: Any, design: dict[str, Any], *, now: datetime | None = None) -> str:
    """BPMN 2.0 file (with diagram interchange) of a stored design. Deterministic for a given `now`.

    `workflow` supplies id, name, description and schedule_cron. Elements are built with ElementTree only.
    """
    for prefix, ns in (("bpmn", MODEL_NS), ("bpmndi", BPMNDI_NS), ("dc", DC_NS), ("di", DI_NS),
                       ("maestro", MAESTRO_NS), ("xsi", XSI_NS)):
        register_namespace(prefix, ns)
    stamp = now or datetime.now(UTC)
    stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    name = str(getattr(workflow, "name", "") or "").strip() or "Maestro workflow"
    description = str(getattr(workflow, "description", "") or "")
    # The database keeps the orchestration prompt in the workflow's content, apart from the design JSON.
    prompt = str(getattr(workflow, "content", "") or "").strip() or design.get("orchestration_prompt")
    if prompt:
        design = {**design, "orchestration_prompt": prompt}
    schedule = design.get("schedule")
    cron = (str(getattr(workflow, "schedule_cron", "") or "").strip()
            or (str(schedule.get("cron") or "").strip() if isinstance(schedule, dict) else ""))

    layout = compute_layout(design)
    ex = _Export(workflow, design, layout)
    ex.build(cron)
    ex.measure()

    defs = _el(None, MODEL + "definitions", {"id": "Definitions_1", "targetNamespace": MAESTRO_NS + "/export",
                                             "exporter": "Upsun Maestro", "exporterVersion": EXPORT_VERSION})
    bands = layout.bands
    group_ids: list[str] = []
    values: dict[str, str] = {}  # phase -> categoryValue id
    if bands:
        category = _el(defs, MODEL + "category", {"id": "Category_1", "name": "Phases"})
        for band in bands:
            if band.phase not in values:
                values[band.phase] = ex.ids.take(f"CategoryValue_{len(values) + 1}")
                _el(category, MODEL + "categoryValue", {"id": values[band.phase], "value": band.phase})
        group_ids = [ex.ids.take(f"Group_{i}") for i in range(1, len(bands) + 1)]
    collaboration = _el(defs, MODEL + "collaboration", {"id": "Collaboration_1"})
    _el(collaboration, MODEL + "participant", {"id": "Participant_1", "name": name, "processRef": ex.process_id})

    process = _el(defs, MODEL + "process", {"id": ex.process_id, "name": name, "isExecutable": "false"})
    if description:
        _el(process, MODEL + "documentation", text=description)
    extension = _el(process, MODEL + "extensionElements")
    stored = _el(extension, MAESTRO + "design", {"version": EXPORT_VERSION,
                                                 "stepsHash": steps_hash(design.get("steps") or [])})
    _el(extension, MAESTRO + "exported", {"at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "app": "maestro"})
    payload = json.dumps({"name": name, "description": description, "design": design}, ensure_ascii=False,
                         default=str)
    payload = _JSON_ILLEGAL.sub(lambda m: f"\\u{ord(m.group()):04x}", payload)

    lane_set = _el(process, MODEL + "laneSet", {"id": "LaneSet_1"})
    for lane in ex.lanes:
        el = _el(lane_set, MODEL + "lane", {"id": f"Lane_{lane}", "name": ACTOR_LABELS[lane]})
        for n in ex.nodes.values():
            if n.lane == lane:
                _el(el, MODEL + "flowNodeRef", text=n.id)
    process.extend(ex.elements)
    for e in ex.edges:
        attrs = {"id": e.id, "sourceRef": e.source, "targetRef": e.target}
        if e.name:
            attrs["name"] = e.name
        _el(process, MODEL + "sequenceFlow", attrs)
    groups = ex.groups(bands)
    for i, _, _ in groups:
        _el(process, MODEL + "group", {"id": group_ids[i], "categoryValueRef": values[bands[i].phase]})

    # Diagram interchange: the participant and lanes first, then the nodes and flows, the groups on top.
    diagram = _el(defs, BPMNDI + "BPMNDiagram", {"id": "BPMNDiagram_1"})
    plane = _el(diagram, BPMNDI + "BPMNPlane", {"id": "BPMNPlane_1", "bpmnElement": "Collaboration_1"})
    px, py, pw, ph = ex.pool
    _shape(plane, "Participant_1", px, py, pw, ph, horizontal=True)
    for lane in ex.lanes:
        _shape(plane, f"Lane_{lane}", *ex.lane_box[lane], horizontal=True)
    for n in ex.nodes.values():
        label = None
        if n.label:
            w = _label_width(n.label)
            label = (n.cx - w // 2, n.y + n.h + 4, w, 14)
        _shape(plane, n.id, n.x, n.y, n.w, n.h, marker=n.tag == "exclusiveGateway", label=label)
    for e in ex.edges:
        edge = _el(plane, BPMNDI + "BPMNEdge", {"id": f"{e.id}_di", "bpmnElement": e.id})
        points = ex.waypoints(e)
        for x, y in points:
            _el(edge, DI + "waypoint", {"x": str(x), "y": str(y)})
        if e.name:
            w = _label_width(e.name)
            if e.route is not None:  # on the horizontal run under the lanes
                lx, ly = (points[1][0] + points[2][0]) // 2 - w // 2, points[1][1] - 18
            else:  # above the horizontal run that enters the target
                lx, ly = (points[-2][0] + 4 if len(points) > 2 else points[0][0] + 4), points[-1][1] - 18
            _bounds(_el(edge, BPMNDI + "BPMNLabel"), lx, ly, w, 14)
    for i, left, right in groups:
        _shape(plane, group_ids[i], left, py, right - left, ph)

    indent(defs, space="  ")
    token = "MAESTRO-CDATA"
    while True:  # ElementTree cannot write CDATA: a token that occurs nowhere else marks the place
        stored.text = token
        xml = tostring(defs, encoding="unicode")
        if xml.count(token) == 1:
            break
        token += "X"
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml.replace(token, _cdata(payload))
