"""BPMN 2.0 import for Maestro: safe, deterministic parsing. No LLM ever reads or writes the XML.

Two kinds of files come in:
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
from dataclasses import dataclass, field
from typing import Any
from xml.etree.ElementTree import Element, ParseError

from defusedxml import DefusedXmlException
from defusedxml.ElementTree import fromstring as safe_fromstring

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
    if stored and not findings and _is_untouched_export(root, ids):
        data, declared = stored
        design = data["design"]
        stored_steps = design.get("steps")
        rebuilt = steps_from_bpmn(root, ids)
        if (isinstance(stored_steps, list) and rebuilt is not None and design.get("orchestration_prompt")
                and steps_hash(rebuilt) == declared == steps_hash(stored_steps)):
            return ImportResult("maestro", _clip(str(data.get("name") or ""), 128) or name,
                                str(data.get("description") or ""), design, [])
    return ImportResult("foreign", name, build_description(root, ids, findings), None, findings)
