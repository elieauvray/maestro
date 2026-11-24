"""Maestro projects: design from a description, compose each run, wake runs on merged pull requests."""

from __future__ import annotations

import hashlib
import logging
import re
import statistics
import time
from dataclasses import dataclass
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.anthropic_client import ManagedAgentsError
from app.maestro.bpmn import ImportResult, steps_hash
from app.maestro.knowledge import CONNECTOR_KEYS, CONNECTOR_NAMES
from app.deps import get_user_settings
from app.models import PendingBpmnImport, RunStatus, Skill, User, Workflow, WorkflowRun, utcnow
from app.plans.service import connector_policies
from app.settings.service import client_for, resources_ready, sync_user_resources
from app.maestro.diagram import fingerprint
from app.maestro.render import tag_text
from app.runs.progress import mark_continued
from app.workflows.compose import compose_workflow_message, instructions_block, instructions_hash, snapshot_skills

log = logging.getLogger(__name__)
GENERATING_TIMEOUT = timedelta(minutes=20)
ACTORS = ("maestro", "dispatch", "human", "upsun", "external")
DONE_WHEN = {"pr_merged", "pr_approved", "issue_closed", "pr_opened"}
MAX_STEP_ID = 40  # long enough to keep BPMN element ids as step ids


# --- Design -------------------------------------------------------------------


def create_maestro_workflow(db: Session, user: User, name: str, description: str,
                            diagram: tuple[str, bytes] | None = None, shared: bool = False) -> Workflow:
    wf = Workflow(user_id=user.id, name=name, description=description, content="", design_status="queued",
                  shared=shared)
    if diagram:
        wf.diagram_media_type, wf.diagram_data = diagram
    db.add(wf)
    db.commit()
    return wf


# --- BPMN import ------------------------------------------------------------------------------


def _unique_name(base: str, taken: set[str]) -> str:
    base = (base.strip() or "Imported BPMN process")[:128]
    name, n = base, 2
    while name in taken:
        suffix = f" ({n})"
        name, n = base[: 128 - len(suffix)] + suffix, n + 1
    return name


def _restore_export(db: Session, wf: Workflow, result: ImportResult) -> None:
    """An unchanged Maestro file: the stored design comes back as it was (no Anthropic call)."""
    stored = dict(result.design or {})
    prompt = str(stored.pop("orchestration_prompt", "") or "").strip()
    connectors = [c for c in stored.get("connectors_needed") or [] if c in CONNECTOR_NAMES]
    wf.design_json = {**_normalize_design(stored, stored), "connectors_needed": connectors}
    wf.content = prompt
    wf.clarifications = None
    wf.design_status = "ready"
    wf.design_error = None
    wf.design_feedback = None
    wf.design_progress = None
    wf.instructions_hash = current_instructions_hash(db)
    wf.design_updated_at = utcnow()


def _queue_import(wf: Workflow, result: ImportResult) -> None:
    """Any other file: the description and the findings are stored, the worker runs the review first."""
    wf.design_json = None
    wf.content = ""
    wf.clarifications = {"kind": "import_pending", "findings": result.findings}
    wf.design_status = "queued"
    wf.design_error = None
    wf.design_feedback = None
    wf.design_progress = None


def create_imported_workflow(db: Session, user: User, result: ImportResult, shared: bool = False,
                             name: str | None = None, context: str | None = None) -> Workflow:
    """A workflow from a BPMN file.

    With `name`, the project gets that name as is (a taken name raises IntegrityError, rolled back). Without it,
    the name is the process name, made unique among the user's workflows. With `context`, the description holds
    the user's context before the imported process, so the review and the designer read both.
    """
    process = result.description or "Imported from a BPMN file."
    description = (f"## Context from the user\n{context}\n\n## Imported BPMN process\n{process}"
                   if context else process)
    taken = set(db.scalars(select(Workflow.name).where(Workflow.user_id == user.id)))
    for _ in range(20):
        wf_name = name if name is not None else _unique_name(result.name, taken)
        wf = Workflow(user_id=user.id, name=wf_name, description=description,
                      content="", design_status="queued", shared=shared)
        if result.kind == "maestro":
            _restore_export(db, wf, result)
        else:
            _queue_import(wf, result)
        db.add(wf)
        try:
            db.commit()
            return wf
        except IntegrityError:  # the same name was taken meanwhile
            db.rollback()
            if name is not None:
                raise
            taken.add(wf_name)
    raise ManagedAgentsError("Could not find a free name for this project.")

def reimport_workflow(db: Session, wf: Workflow, result: ImportResult) -> None:
    """A corrected file replaces the description of the same workflow; the review runs again."""
    process = result.description or "Imported from a BPMN file."
    head, sep, _ = (wf.description or "").partition("\n\n## Imported BPMN process\n")
    # Keep the context typed on the Create page: a corrected file replaces the process only.
    wf.description = f"{head}{sep}{process}" if sep and head.startswith("## Context from the user\n") else process
    _apply_import(db, wf, result, _saved_config(wf))
    db.commit()


def _saved_config(wf: Workflow) -> list[dict[str, Any]]:
    """The non-empty config values to carry over: the design's, else those already carried by an earlier import."""
    values = [{"key": c["key"], "value": c["value"]}
              for c in (wf.design_json or {}).get("config") or [] if c.get("key") and c.get("value")]
    return values or list((wf.clarifications or {}).get("previous_config") or [])


def _apply_import(db: Session, wf: Workflow, result: ImportResult, saved: list[dict[str, Any]]) -> None:
    """Restore an unchanged Maestro export or queue any other file, keeping the saved config values."""
    if result.kind == "maestro":
        _restore_export(db, wf, result)
        if saved:
            values = {c["key"]: c["value"] for c in saved}
            design = dict(wf.design_json or {})
            design["config"] = [
                {**c, "value": c.get("value") or values.get(c.get("key"), "")} for c in design.get("config") or []
            ]
            wf.design_json = design
    else:
        _queue_import(wf, result)
        if saved:
            wf.clarifications = {**(wf.clarifications or {}), "previous_config": saved}


def update_from_bpmn(db: Session, wf: Workflow, result: ImportResult, context: str = "") -> None:
    """Update the same project from a BPMN file: name, sharing, owner and runs stay; the design is replaced.

    An unchanged Maestro export is restored as it was (no Anthropic call). Any other file goes through the
    review and the designer; the settings saved so far are kept in `clarifications["previous_config"]` and
    applied for the keys that still exist when the new design is normalized.
    """
    process = result.description or "Imported from a BPMN file."
    wf.description = f"## Context from the user\n{context}\n\n## Imported BPMN process\n{process}" if context else process
    _apply_import(db, wf, result, _saved_config(wf))
    db.commit()


# --- Existing project detection (Create page, BPMN file) ----------------------------------------


@dataclass
class ProjectMatch:
    workflow: Workflow
    reason: str  # "exported" | "name" | "steps"


def _freshest(candidates: list[Workflow]) -> Workflow | None:
    """The most recently designed project, or the highest id when none has a design date."""
    floor = datetime.min.replace(tzinfo=UTC)
    return max(candidates, key=lambda w: (_aware(w.design_updated_at) if w.design_updated_at else floor, w.id),
               default=None)


def find_matching_project(db: Session, user: User, result: ImportResult, typed_name: str) -> ProjectMatch | None:
    """The user's own project a BPMN file comes from or looks like (no LLM): exported, same name, same steps."""
    own = list(db.scalars(select(Workflow).where(Workflow.user_id == user.id, Workflow.description.is_not(None))))
    if result.source_id is not None:
        hit = _freshest([w for w in own if w.id == result.source_id])
        if hit:
            return ProjectMatch(hit, "exported")
    names = {n.strip().casefold() for n in (typed_name, result.name) if n and n.strip()}
    if names:
        hit = _freshest([w for w in own if w.name.strip().casefold() in names])
        if hit:
            return ProjectMatch(hit, "name")
    if result.steps_fingerprint is not None and result.steps_fingerprint != steps_hash([]):  # no steps: no match
        hit = _freshest([w for w in own if w.design_status == "ready"
                         and steps_hash((w.design_json or {}).get("steps") or []) == result.steps_fingerprint])
        if hit:
            return ProjectMatch(hit, "steps")
    return None


def proposed_name(db: Session, user: User, base: str) -> str:
    """`base` when the user has no project with that name, else the first free "base (2)", "base (3)"..."""
    taken = set(db.scalars(select(Workflow.name).where(Workflow.user_id == user.id)))
    if base.strip() and base.strip() not in taken and len(base.strip()) <= 128:
        return base.strip()
    return _unique_name(base, taken)


# --- Pending import: the file kept on the server until the user chooses -------------------------

PENDING_TTL = timedelta(hours=1)
PENDING_MAX_PER_USER = 3  # a few open tabs; older files of the user are dropped


def save_pending_import(db: Session, user: User, xml: bytes, name: str, context: str, shared: bool) -> str:
    """Keep the uploaded file for the choice page.

    Expired files (of every user) are deleted first, then the user's oldest files so that at most
    PENDING_MAX_PER_USER remain once this one is saved.
    """
    db.execute(delete(PendingBpmnImport).where(PendingBpmnImport.created_at < utcnow() - PENDING_TTL))
    stale = list(db.scalars(select(PendingBpmnImport.id).where(PendingBpmnImport.user_id == user.id)
                            .order_by(PendingBpmnImport.created_at.desc(), PendingBpmnImport.id)
                            .offset(PENDING_MAX_PER_USER - 1)))
    if stale:
        db.execute(delete(PendingBpmnImport).where(PendingBpmnImport.id.in_(stale)))
    row = PendingBpmnImport(id=str(uuid4()), user_id=user.id, xml=xml, name=name, context=context,
                            shared=shared, created_at=utcnow())
    db.add(row)
    db.commit()
    return row.id


def load_pending_import(db: Session, user: User, pending_id: str) -> PendingBpmnImport | None:
    """The user's own pending file, or None when it is unknown, someone else's or older than an hour."""
    row = db.get(PendingBpmnImport, pending_id)
    if row is None or row.user_id != user.id or _aware(row.created_at) < utcnow() - PENDING_TTL:
        return None
    return row


def drop_pending_import(db: Session, row: PendingBpmnImport) -> None:
    db.delete(row)
    db.commit()


def current_instructions(db: Session) -> list[dict[str, Any]]:
    """Active system instructions, in order (stored in the `skills` table)."""
    return snapshot_skills(db.scalars(select(Skill)))


def current_instructions_hash(db: Session) -> str:
    return instructions_hash(current_instructions(db))


def instructions_changed(wf: Workflow, current_hash: str) -> bool:
    """True when the system instructions changed since this workflow's design was generated."""
    return wf.design_status == "ready" and (wf.instructions_hash or "") != current_hash


def request_design(db: Session, wf: Workflow, feedback: str | None = None) -> None:
    wf.design_feedback = (feedback or "").strip() or None
    wf.design_status = "queued"
    wf.design_error = None
    db.commit()


def claim_design_jobs(db: Session) -> list[int]:
    """Workflows whose design must be generated (queued, or stuck in generating)."""
    stale = utcnow() - GENERATING_TIMEOUT
    ids = db.scalars(
        select(Workflow.id).where(
            (Workflow.design_status == "queued")
            | ((Workflow.design_status == "generating") & (Workflow.design_updated_at < stale))
        )
    ).all()
    claimed = []
    for wf_id in ids:
        res = db.execute(
            update(Workflow)
            .where(Workflow.id == wf_id, Workflow.design_status.in_(["queued", "generating"]))
            .values(design_status="generating", design_updated_at=utcnow())
        )
        if res.rowcount == 1:  # type: ignore[attr-defined]
            claimed.append(wf_id)
    db.commit()
    return claimed


def _normalize_design(raw: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    old_values = {c.get("key"): c.get("value", "") for c in (previous or {}).get("config", [])}
    steps = []
    for i, st in enumerate(raw.get("steps") or []):
        actor = st.get("actor") if st.get("actor") in ACTORS else "maestro"
        steps.append({
            "id": str(st.get("id") or f"S{i + 1}")[:MAX_STEP_ID],
            "phase": str(st.get("phase") or "").strip()[:60],
            "title": str(st.get("title") or "Step").strip()[:80],
            "detail": str(st.get("detail") or "").strip()[:160],
            "actor": actor,
            "dispatch_workflow": st.get("dispatch_workflow") if actor == "dispatch" else "none",
            "gate_owner": str(st.get("gate_owner") or "").strip()[:60] if actor == "human" else "",
            "repeat": tag_text(st.get("repeat")),
            "loop": tag_text(st.get("loop")),
            "done_when": st.get("done_when") if actor == "dispatch" and st.get("done_when") in DONE_WHEN else "none",
            "branch": tag_text(st.get("branch"))[:40],
            "loop_to": str(st.get("loop_to") or "").strip()[:MAX_STEP_ID],
        })
    # A loop can only go back to an earlier step.
    seen: set[str] = set()
    for st in steps:
        if st["loop_to"] not in seen:
            st["loop_to"] = ""
        seen.add(st["id"])
    config = [
        {"key": str(c.get("key", "")).strip()[:64], "description": str(c.get("description", "")).strip()[:200],
         "value": old_values.get(str(c.get("key", "")).strip()[:64], "")}
        for c in raw.get("config") or [] if str(c.get("key", "")).strip()
    ]
    return {
        "summary": str(raw.get("summary") or "").strip(),
        "steps": steps,
        "config": config,
        "assumptions": [str(a) for a in raw.get("assumptions") or []][:20],
        "instruction_adjustments": [
            {"instruction": str(a.get("instruction", "")), "change": str(a.get("change", ""))}
            for a in raw.get("instruction_adjustments") or [] if isinstance(a, dict)
        ][:20],
        "not_possible": [
            {"need": str(n.get("need", "")), "alternative": str(n.get("alternative", "")),
             "element_id": str(n.get("element_id") or "").strip()[:200]}
            for n in raw.get("not_possible") or []
        ][:20],
        "tools": [str(t) for t in raw.get("tools") or []][:20],
    }


def blocking_message(needed: list[str], connected: set[str], unsupported: list[dict[str, Any]]) -> str | None:
    """Why Maestro cannot run this workflow with the tools connected today (None when it can)."""
    problems = []
    missing = [CONNECTOR_NAMES[c] for c in needed if c not in connected]
    if missing:
        problems.append(
            "This project needs " + ", ".join(missing) + ", which " + ("is" if len(missing) == 1 else "are")
            + " not connected to Maestro. Connect " + ("it" if len(missing) == 1 else "them")
            + " in Settings, then submit your request again."
        )
    if unsupported:
        names = ", ".join(f"{u.get('name')} ({u.get('reason')})" for u in unsupported)
        problems.append(
            "It also mentions tools Maestro cannot connect to today: " + names
            + ". Remove them from the description or replace them with Linear, GitHub or Jira/Confluence."
            if missing else
            "This project mentions tools Maestro cannot connect to today: " + names
            + ". Remove them from the description or replace them with Linear, GitHub or Jira/Confluence, "
            "then submit your request again."
        )
    return " ".join(problems) or None


def missing_connectors(db: Session, user: User, wf: Workflow) -> list[str]:
    """Connectors the design relies on that are no longer connected."""
    needed = (wf.design_json or {}).get("connectors_needed") or []
    connected = set(connector_policies(db, user))
    return [CONNECTOR_NAMES[c] for c in needed if c in CONNECTOR_NAMES and c not in connected]


def decisions_block(clar: dict[str, Any] | None) -> str:
    decisions = (clar or {}).get("decisions") or []
    if not decisions:
        return ""
    lines = "\n".join(
        f"- {'[BPMN element ' + d['element_id'] + '] ' if d.get('element_id') else ''}{d['question']} -> {d['answer']}"
        for d in decisions
    )
    note = ""
    if any(d.get("element_id") for d in decisions):
        note = ("\nEvery [BPMN element ...] decision is a final choice about something that cannot run as drawn: "
                "list each of them in not_possible, with the original need and the chosen route as the alternative.")
    return f"\n\nDecisions made by the user (final, apply them):\n{lines}{note}"


def normalize_questions(raw: list[Any]) -> list[dict[str, Any]]:
    questions, seen = [], set()
    for i, q in enumerate(raw[:6]):
        if not isinstance(q, dict):
            continue
        options = [str(o).strip()[:160] for o in q.get("options") or [] if str(o).strip()][:4]
        text = str(q.get("question") or "").strip()[:300]
        if not text or len(options) < 2:
            continue
        qid = re.sub(r"[^A-Za-z0-9_-]", "", str(q.get("id") or ""))[:16] or f"Q{i + 1}"
        if qid in seen:
            qid = f"Q{i + 1}"
        seen.add(qid)
        questions.append({"id": qid, "question": text, "why": str(q.get("why") or "").strip()[:240],
                          "options": options})
    return questions


def answer_questions(db: Session, wf: Workflow, answers: dict[str, str]) -> list[str]:
    """Record the answers as decisions and queue the design. Returns the ids still unanswered."""
    clar = dict(wf.clarifications or {})
    questions = clar.get("questions") or []
    missing = [q["id"] for q in questions if not (answers.get(q["id"]) or "").strip()]
    if missing:
        return missing
    decisions = list(clar.get("decisions") or [])
    decisions += [{"question": q["question"], "answer": answers[q["id"]].strip()[:500]} for q in questions]
    wf.clarifications = {**clar, "questions": [], "decisions": decisions}
    wf.design_status = "queued"
    db.commit()
    return []


# --- BPMN import review -----------------------------------------------------------------------------

MAX_ISSUES = 8
GENERIC_PROBLEM = "This BPMN element is not supported"
GENERIC_OPTIONS = [
    {"label": "Describe it as a step done by a person",
     "effect": "The designer adds a human step that does what this element stands for."},
    {"label": "Leave it out", "effect": "This element does not appear in the workflow."},
]


def _ref_ids(value: str) -> list[str]:
    return [p for p in re.split(r"[,\s]+", value or "") if p]


def normalize_issues(raw: list[Any], findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The review's issues, cleaned, plus a generic issue for every finding the model left out.

    The model's issues are capped at 8, most blocking first. The generic ones come on top of that cap on purpose:
    every deterministic finding must reach the user.
    """
    issues: list[dict[str, Any]] = []
    seen: set[str] = set()
    covered: set[str] = set()

    def unique_id(wanted: str, fallback: str) -> str:
        iid = re.sub(r"[^A-Za-z0-9_-]", "", wanted)[:16] or fallback
        while iid in seen:
            iid += "x"
        seen.add(iid)
        return iid

    for i, q in enumerate(raw[:MAX_ISSUES]):
        if not isinstance(q, dict):
            continue
        problem = str(q.get("problem") or "").strip()[:300]
        options = [{"label": str(o.get("label")).strip()[:120], "effect": str(o.get("effect") or "").strip()[:240]}
                   for o in q.get("options") or [] if isinstance(o, dict) and str(o.get("label") or "").strip()][:4]
        if not problem or len(options) < 2:
            continue  # unusable: a finding it was about gets a generic issue below
        refs = _ref_ids(str(q.get("element_id") or ""))
        covered.update(refs)
        issues.append({
            "id": unique_id(str(q.get("id") or ""), f"I{i + 1}"),
            "element_id": ", ".join(refs)[:200],
            "element_name": str(q.get("element_name") or "").strip()[:120],
            "problem": problem,
            "why": str(q.get("why") or "").strip()[:240],
            "options": options,
        })
    for n, f in enumerate((f for f in findings if f.get("element_id") not in covered), start=1):
        issues.append({
            "id": unique_id(f"G{n}", f"G{n}"),
            "element_id": str(f.get("element_id") or "")[:200],
            "element_name": str(f.get("element_name") or f.get("element_id") or "")[:120],
            "problem": GENERIC_PROBLEM,
            "why": str(f.get("detail") or f"{f.get('element_type')} elements are not supported")[:240],
            "options": [dict(o) for o in GENERIC_OPTIONS],
        })
    return issues


def answer_import_review(db: Session, wf: Workflow, choices: dict[str, tuple[str, str]]) -> list[str]:
    """Record the chosen route of each issue as a final decision and queue the pipeline.

    `choices` maps an issue id to (picked option label or "__other__", the text written for Other).
    Returns the ids of the issues still unanswered (nothing is stored then).
    """
    clar = dict(wf.clarifications or {})
    issues = clar.get("questions") or []
    decisions, missing = [], []
    for q in issues:
        choice, other = choices.get(q["id"], ("", ""))
        option = next((o for o in q["options"] if o["label"] == choice), None)
        if option is not None:
            answer = f"{option['label']} — {option['effect']}" if option["effect"] else option["label"]
        elif choice == "__other__" and other.strip():
            answer = other.strip()
        else:
            missing.append(q["id"])
            continue
        name = q.get("element_name") or q.get("element_id") or ""
        question = q["problem"] if not name or name in q["problem"] else f"{name}: {q['problem']}"
        decisions.append({"question": question, "answer": answer[:500], "element_id": q.get("element_id", "")})
    if missing:
        return missing
    wf.clarifications = {**clar, "questions": [], "decisions": list(clar.get("decisions") or []) + decisions}
    wf.design_status = "queued"
    wf.design_error = None
    db.commit()
    return []


# --- Design progress -------------------------------------------------------------

STAGES = ("tools", "clarify", "design")
IMPORT_STAGES = ("review",) + STAGES  # an imported BPMN file starts with the review of what cannot run as drawn
ALL_STAGES = IMPORT_STAGES
STAGE_LABELS = {
    "review": "Reviewing the BPMN file",
    "tools": "Checking the tools it needs",
    "clarify": "Looking for decisions to ask you",
    "design": "Designing the project",
}
# Typical durations (seconds) and design length (characters) before any generation has been timed.
DEFAULT_TIMINGS = {"review": 45.0, "tools": 15.0, "clarify": 40.0, "design": 240.0, "design_chars": 20000.0}
# What the designer is writing, by JSON key, in output order.
WRITING_LABELS = [
    ("summary", "Summarizing the process"),
    ("steps", "Drawing the steps"),
    ("config", "Listing the settings"),
    ("assumptions", "Writing assumptions and limits"),
    ("orchestration_prompt", "Writing the orchestration prompt"),
]
PROGRESS_EVERY = 1.5  # seconds between two progress writes while streaming


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _set_progress(db: Session, wf: Workflow, **changes: Any) -> None:
    wf.design_progress = {**(wf.design_progress or {}), **changes, "updated": _iso(utcnow())}
    db.commit()


def _start_stage(db: Session, wf: Workflow, stage: str) -> None:
    progress = dict(wf.design_progress or {})
    now = utcnow()
    timings = dict(progress.get("current_timings") or {})
    previous, started = progress.get("stage"), _parse(progress.get("stage_started"))
    if previous in ALL_STAGES and started and previous != stage:
        timings[previous] = round((now - started).total_seconds(), 1)
    done = list(progress.get("done") or [])
    if previous in ALL_STAGES and previous != stage and previous not in done:
        done.append(previous)
    _set_progress(db, wf, stage=stage, stage_started=_iso(now), done=done, current_timings=timings,
                  phase="thinking" if stage == "design" else None, chars=0, writing=None)


def _skip_stage(wf: Workflow, stage: str, note: str) -> None:
    """A stage that did not need to run this time counts as done (with why), not as pending."""
    progress = dict(wf.design_progress or {})
    done = list(progress.get("done") or [])
    if stage not in done:
        done.append(stage)
    wf.design_progress = {**progress, "done": done, "notes": {**(progress.get("notes") or {}), stage: note}}


def _begin_generation(db: Session, wf: Workflow, with_review: bool = False) -> None:
    last = (wf.design_progress or {}).get("timings")
    progress: dict[str, Any] = {"started": _iso(utcnow()), "timings": last}
    if with_review:  # imports only: the other projects keep their three rows
        progress["stages"] = list(IMPORT_STAGES)
    wf.design_progress = progress
    _start_stage(db, wf, "review" if with_review else "tools")


def _finish_generation(db: Session, wf: Workflow, design_chars: int | None) -> None:
    """Keep this generation's timings: they make the next estimates more accurate."""
    progress = dict(wf.design_progress or {})
    timings = dict(progress.get("current_timings") or {})
    started = _parse(progress.get("stage_started"))
    if progress.get("stage") in ALL_STAGES and started:
        timings[progress["stage"]] = round((utcnow() - started).total_seconds(), 1)
    if design_chars:
        timings["design_chars"] = design_chars
    if "design" in timings:  # only complete generations teach the estimates
        progress["timings"] = timings
    wf.design_progress = {"timings": progress.get("timings")}


def design_estimates(db: Session) -> dict[str, float]:
    """Median durations of recent design generations (all users), falling back to typical values."""
    samples: dict[str, list[float]] = {k: [] for k in DEFAULT_TIMINGS}
    rows = db.scalars(
        select(Workflow.design_progress).where(Workflow.design_progress.is_not(None))
        .order_by(Workflow.design_updated_at.desc()).limit(40)
    )
    for progress in rows:
        for key, value in ((progress or {}).get("timings") or {}).items():
            if key in samples and isinstance(value, (int, float)) and value > 0:
                samples[key].append(float(value))
    return {k: (statistics.median(v) if v else DEFAULT_TIMINGS[k]) for k, v in samples.items()}


def progress_view(wf: Workflow, estimates: dict[str, float]) -> dict[str, Any] | None:
    """What the design page shows while Maestro designs: one row per stage, an overall bar, time left."""
    if wf.design_status not in {"queued", "generating"}:
        return None
    progress = wf.design_progress or {}
    now = utcnow()
    stage = progress.get("stage") if wf.design_status == "generating" else None
    done = set(progress.get("done") or [])
    stage_started = _parse(progress.get("stage_started"))
    elapsed_stage = (now - stage_started).total_seconds() if stage_started and stage else 0.0
    rows, remaining, total, spent = [], 0.0, 0.0, 0.0
    for key in progress.get("stages") or STAGES:
        expected = max(estimates[key], 1.0)
        total += expected
        if key in done:
            state, pct, note = "done", 100, (progress.get("notes") or {}).get(key, "")
            spent += expected
        elif key == stage:
            state = "active"
            time_ratio = elapsed_stage / expected
            note = ""
            if key == "design":
                chars = int(progress.get("chars") or 0)
                if progress.get("phase") == "writing" and chars:
                    # Writing: the characters written are the best measure of how far it is.
                    char_ratio = chars / max(estimates["design_chars"], 1.0)
                    time_ratio = max(time_ratio, 0.35 + 0.65 * char_ratio)
                    note = f"{progress.get('writing') or 'Writing the design'} · {chars:,} characters"
                else:
                    time_ratio = min(time_ratio, 0.35)  # thinking comes first, writing is the larger part
                    note = "Thinking about the process"
            ratio = min(time_ratio, 0.97)
            pct = round(ratio * 100)
            spent += expected * ratio
            remaining += max(expected - elapsed_stage, expected * (1 - ratio))
        else:
            state, pct, note = "pending", 0, ""
            remaining += expected
        rows.append({"key": key, "label": STAGE_LABELS[key], "state": state, "pct": pct, "note": note})
    started = _parse(progress.get("started"))
    elapsed = (now - started).total_seconds() if started and stage else 0.0
    return {
        "rows": rows,
        "pct": min(round(spent / total * 100), 97) if stage else 0,
        "remaining": _human_seconds(remaining) if stage else None,
        "elapsed": _human_seconds(elapsed) if stage else None,
        "queued": stage is None,
    }


def _human_seconds(seconds: float) -> str:
    seconds = max(int(seconds), 0)
    if seconds < 15:
        return "a few seconds"
    if seconds < 60:
        return f"{round(seconds / 5) * 5} s"
    minutes, rest = divmod(seconds, 60)
    return f"{minutes} min {rest:02d} s" if minutes < 10 else f"{minutes} min"


def writing_label(text_tail: str) -> str | None:
    """Which part of the design the streamed JSON is in, from the last key seen."""
    best, label = -1, None
    for key, name in WRITING_LABELS:
        pos = text_tail.rfind(f'"{key}"')
        if pos > best:
            best, label = pos, name
    return label


def review_import(db: Session, wf: Workflow, client: Any, settings: Any, instructions: str,
                  services: list[str]) -> bool:
    """Review an imported BPMN file. True when the dialog must be shown (status set to needs_answers)."""
    findings = list((wf.clarifications or {}).get("findings") or [])
    # Settings carried over from before an update of the project (applied when the design is normalized).
    carried = {"previous_config": (wf.clarifications or {})["previous_config"]} \
        if (wf.clarifications or {}).get("previous_config") else {}
    raw = client.review_import(settings.model, wf.description, findings, services, CONNECTOR_KEYS, instructions)
    issues = normalize_issues(raw.get("issues") or [], findings)
    if not issues:
        wf.clarifications = carried or None  # nothing to decide: straight on to the tool check
        db.commit()
        return False
    wf.clarifications = {"kind": "import_review", "questions": issues, "findings": findings, "decisions": [],
                         **carried}
    wf.design_status = "needs_answers"
    wf.design_error = None
    wf.design_updated_at = utcnow()
    _finish_generation(db, wf, None)
    db.commit()
    return True


def generate_design(db: Session, wf_id: int) -> None:
    """Called by the worker in a background thread."""
    wf = db.get(Workflow, wf_id)
    if wf is None or not wf.description:
        return
    user = db.get(User, wf.user_id)
    assert user is not None
    settings = get_user_settings(db, user)
    try:
        pending_review = (wf.clarifications or {}).get("kind") == "import_pending"
        _begin_generation(db, wf, with_review=pending_review)
        client = client_for(settings)
        # System instructions (admin) apply to every workflow and take precedence over the description.
        skills = current_instructions(db)
        instructions, ihash = instructions_block(skills), instructions_hash(skills)
        diagram = {"media_type": wf.diagram_media_type, "data": wf.diagram_data} if wf.diagram_data else None
        # 0. An imported BPMN file: what cannot run as drawn goes to the user first (the dialog).
        if pending_review and review_import(db, wf, client, settings, instructions,
                                            sorted(connector_policies(db, user))):
            return
        # 1. Tool check first (cheap): never write a prompt that needs a tool Maestro cannot use.
        text = wf.description + (f"\n\nRequested change: {wf.design_feedback}" if wf.design_feedback else "")
        clar = dict(wf.clarifications or {})
        if pending_review:
            _start_stage(db, wf, "tools")
        check = client.check_tools(settings.model, text + decisions_block(clar), instructions, diagram)
        needed = sorted({c for c in check.get("connectors_needed") or [] if c in CONNECTOR_NAMES})
        blocked = blocking_message(needed, set(connector_policies(db, user)), check.get("unsupported_tools") or [])
        if blocked:
            wf.design_status = "blocked"
            wf.design_error = blocked
            wf.design_updated_at = utcnow()
            _finish_generation(db, wf, None)
            db.commit()
            return
        # 2. Clarify: ask the user the decisions that make the workflow executable (once per request)
        services = sorted(connector_policies(db, user))
        request_hash = hashlib.sha256((text + ihash + fingerprint(wf.diagram_data)).encode()).hexdigest()
        if clar.get("for") != request_hash:
            _start_stage(db, wf, "clarify")
            asked = client.clarify_workflow(settings.model, text + decisions_block(clar), services, instructions, diagram)
            questions = normalize_questions(asked.get("questions") or [])
            carried = {"previous_config": clar["previous_config"]} if clar.get("previous_config") else {}
            clar = {"for": request_hash, "questions": questions, "decisions": clar.get("decisions") or [], **carried}
            wf.clarifications = clar
        else:  # already asked for this request (answered or nothing to ask): not asked again
            _skip_stage(wf, "clarify", "Already answered" if clar.get("decisions") else "Nothing to ask")
        if clar.get("questions"):
            wf.design_status = "needs_answers"
            wf.design_error = None
            wf.design_updated_at = utcnow()
            _finish_generation(db, wf, None)
            db.commit()
            return
        # 3. Full design, with the user's decisions
        _start_stage(db, wf, "design")
        written = {"chars": 0, "at": 0.0}

        def on_progress(chars: int, tail: str) -> None:
            written["chars"] = chars
            if time.monotonic() - written["at"] >= PROGRESS_EVERY:
                written["at"] = time.monotonic()
                _set_progress(db, wf, phase="writing", chars=chars, writing=writing_label(tail))

        raw = client.design_workflow(
            settings.model, wf.description + decisions_block(clar), services,
            wf.design_json, wf.design_feedback, instructions, diagram, on_progress=on_progress,
        )
        _finish_generation(db, wf, written["chars"] or None)
        carried_config = clar.get("previous_config")
        previous = {"config": carried_config} if carried_config else wf.design_json
        wf.design_json = {**_normalize_design(raw, previous), "connectors_needed": needed}
        if carried_config:  # applied: dropped (a new dict, so the JSON column notices)
            wf.clarifications = {k: v for k, v in clar.items() if k != "previous_config"}
        wf.content = str(raw["orchestration_prompt"]).strip()
        wf.design_status = "ready"
        wf.design_error = None
        wf.design_feedback = None
        wf.instructions_hash = ihash
        # The predicted plan diagram follows the new content automatically (hash changes).
    except (ManagedAgentsError, RuntimeError) as exc:
        wf.design_status = "failed"
        wf.design_error = str(exc)
        wf.design_progress = {"timings": (wf.design_progress or {}).get("timings")}
    wf.design_updated_at = utcnow()
    db.commit()



def save_config(db: Session, wf: Workflow, values: dict[str, str]) -> None:
    design = dict(wf.design_json or {})
    design["config"] = [
        {**c, "value": values.get(c["key"], c.get("value", "")).strip()[:500]} for c in design.get("config", [])
    ]
    wf.design_json = design
    db.commit()


# --- Runs ---------------------------------------------------------------------


REPORT_STEP = re.compile(r"\b(report|reporting|status|summary|escalat\w*)\b", re.IGNORECASE)


def report_step(steps: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The design step that holds the end-of-run report (the last step whose title is about reporting)."""
    return next((st for st in reversed(steps) if REPORT_STEP.search(str(st.get("title") or ""))), None)


def _report_marker_rule(steps: list[dict[str, Any]]) -> str:
    st = report_step(steps)
    if st is None:
        return ("Begin your final status report with the marker of the last step you worked on.")
    return (f"Your final status report, and any escalation to the user, belong to step {st['id']} "
            f"({st['title']}): begin that message with `[step: {st['id']}]`, even when the workflow is done "
            "or nothing else happened in this run.")


# --- Run settings, grouped for the Run page -------------------------------------

_ACRONYMS = {"pr": "PR", "prs": "PRs", "url": "URL", "id": "ID", "api": "API", "sfd": "SFD", "us": "US",
             "qa": "QA", "po": "PO", "ci": "CI", "e2e": "E2E", "cve": "CVE", "sla": "SLA",
             "github": "GitHub", "gitlab": "GitLab", "upsun": "Upsun", "linear": "Linear", "jira": "Jira"}
SETTING_GROUPS = [
    ("where", "Where the work lives", "Tracker, repository and branches Maestro works with."),
    ("people", "People", "Who is tagged, validates, reviews and receives escalations."),
    ("labels", "Labels", "Labels Maestro reads or adds in the tracker."),
    ("statuses", "Statuses", "Statuses Maestro reads or sets in the tracker."),
    ("limits", "Limits and timing", "Caps and delays before Maestro escalates."),
    ("other", "Other", ""),
]
_PEOPLE = ("USER", "ANALYST", "DEVELOPER", "OWNER", "LEAD", "REVIEWER", "VALIDATOR", "CLIENT", "MANAGER",
           "APPROVER", "ASSIGNEE", "PO")
_WHERE = ("LINEAR", "GITHUB", "JIRA", "CONFLUENCE", "REPO", "BRANCH", "DIR", "PROJECT", "TEAM", "SPACE",
          "UPSUN", "ENVIRONMENT", "BOARD")


def setting_group(key: str) -> str:
    words = key.upper().split("_")
    if words[0] == "LABEL":
        return "labels"
    if words[0] == "STATUS":
        return "statuses"
    if words[0] in ("MAX", "MIN") or words[-1] in ("DAYS", "HOURS", "MINUTES", "LIMIT", "TIMEOUT"):
        return "limits"
    if any(w.rstrip("S") in _PEOPLE for w in words):
        return "people"
    if any(w.rstrip("S") in _WHERE or w in _WHERE for w in words):
        return "where"
    return "other"


def setting_label(key: str) -> str:
    """LINEAR_TEAM -> "Linear team", STATUS_SFD_VALIDATED -> "SFD validated" (the group says it is a status)."""
    words = key.lower().split("_")
    if words[0] in ("label", "status") and len(words) > 1:
        words = words[1:]
    text = " ".join(_ACRONYMS.get(w, w) for w in words)
    return text[:1].upper() + text[1:]


def grouped_settings(config: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Settings of a run in readable groups, keeping the designer's order inside each group."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for c in config:
        key = str(c.get("key") or "")
        groups.setdefault(setting_group(key), []).append({**c, "label": setting_label(key)})
    return [
        {"key": g, "title": title, "hint": hint, "settings": groups[g],
         "filled": sum(1 for c in groups[g] if str(c.get("value") or "").strip())}
        for g, title, hint in SETTING_GROUPS if g in groups
    ]


def change_block(request: str | None) -> str:
    """What the user asked to change when running the project again after a run."""
    if not (request or "").strip():
        return ""
    return (
        "\n\n## Change requested for this run\n"
        "The user ran this project before and now asks for the change below. Read the ledgers first: keep what is "
        "already done, then carry the change through the same process (for example a new or reopened item, its "
        "spec and validations, a Dispatch run, review and release), from the step where it belongs. Do not redo "
        "finished work that the change does not touch. If the request is unclear, ask before starting.\n\n"
        f"<change_request>\n{request.strip()}\n</change_request>\n"
    )


def values_from_message(message: str, wf: Workflow) -> dict[str, str]:
    """Settings a previous run was started with (read back from its config table)."""
    keys = {str(c.get("key")) for c in (wf.design_json or {}).get("config") or []}
    values: dict[str, str] = {}
    for key, value in re.findall(r"^\| ([A-Z0-9_]+) \| (.*?) \|$", message or "", re.MULTILINE):
        if key in keys and not value.startswith("<not set"):
            values[key] = value
    return values


LEDGER_MARKER = "MAESTRO-LEDGER"


def run_context(wf: Workflow, trigger: str, values: dict[str, str] | None = None) -> str:
    """Block appended to the orchestration prompt on every Maestro run (`values`: settings given at launch)."""
    config = [
        {**c, "value": values[c["key"]]} if values is not None and c.get("key") in values else c
        for c in (wf.design_json or {}).get("config") or []
    ]
    rows = "\n".join(f"| {c['key']} | {c.get('value') or '<not set: ask the user>'} |" for c in config)
    table = f"| Key | Value |\n|-----|-------|\n{rows}\n" if rows else "No config values.\n"
    steps = (wf.design_json or {}).get("steps") or []
    step_rows = "\n".join(
        f"| {st['id']} | {st['actor']} | {st['title']} | {st.get('branch') or ''} |"
        + (f" back to {st['loop_to']} when {st.get('loop') or 'needed'}" if st.get("loop_to") else "")
        for st in steps
    )
    markers = (
        "## Step markers\n"
        "The person following this run sees the workflow diagram and the step you are on. Whenever you start "
        "working on a step, or report on one, begin that message with its marker on its own line, for example "
        f"`[step: {steps[0]['id']}]`. Use only these ids (Path: the alternative a step belongs to; only the "
        "path chosen for an item is followed):\n\n| Id | Actor | Step | Path |\n|----|-------|------|------|\n"
        f"{step_rows}\n\n{_report_marker_rule(steps)}\n\n"
        if steps else ""
    )
    return (
        "\n\n# This run\n"
        f"Trigger: {trigger}. You are Maestro. Read the ledgers first. For each Dispatch workflow you started, "
        "check its completion signal; never interact with it. Start what can be started, stop at human gates, "
        "do not wait for Dispatch, then end with the status report.\n\n"
        f"Begin every ledger comment you post or edit with the line `{LEDGER_MARKER}`: Maestro uses it to tell "
        "its own tracker activity from the activity of people.\n\n"
        f"{markers}"
        "## Workflow status\n"
        "The run page shows whether the workflow is finished. End your final status report with exactly one "
        "of these markers on its own line, even if the prompt above asks for another status line:\n"
        "- `[workflow: ongoing]`: something still has to happen (a Dispatch run to finish, a person to act, "
        "a step to start on a later run);\n"
        "- `[workflow: done]`: every step of the workflow is complete;\n"
        "- `[workflow: blocked]`: it cannot go on without the user (a PR closed without merge, a failed "
        "Dispatch run, a missing value).\n\n"
        f"## Config values\n{table}"
    )


def maestro_message(db: Session, wf: Workflow, trigger: str, values: dict[str, str] | None = None,
                    change_request: str | None = None) -> tuple[list[dict[str, Any]], str]:
    skills = current_instructions(db)
    body = wf.content + run_context(wf, trigger, values) + change_block(change_request)
    return skills, compose_workflow_message(skills, wf.name, body)


# --- Former schedules ----------------------------------------------------------------


def retire_schedules(db: Session) -> int:
    """Projects are no longer scheduled: archive the deployments left from the schedule feature."""
    retired = 0
    for wf in db.scalars(select(Workflow).where(Workflow.deployment_id.is_not(None))):
        user = db.get(User, wf.user_id)
        try:
            if user is not None:
                client_for(get_user_settings(db, user)).archive_deployment(wf.deployment_id)
        except ManagedAgentsError as exc:
            log.warning("Workflow %s: could not archive its old schedule: %s", wf.id, exc)
            continue
        wf.deployment_id, wf.deployment_status = None, None
        retired += 1
    db.commit()
    return retired


# --- Wake-up (GitHub webhook) ---------------------------------------------------

WAKE_DEBOUNCE = timedelta(minutes=2)


def _aware(value: Any) -> Any:
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def _mentions_repo(wf: Workflow, repo: str) -> bool:
    values = " ".join(str(c.get("value") or "") for c in (wf.design_json or {}).get("config") or [])
    haystack = " ".join([wf.content or "", wf.description or "", values]).lower()
    return repo.lower() in haystack


ACTIVE_STATUSES = {RunStatus.RUNNING, RunStatus.WAITING_APPROVAL, RunStatus.WAITING_INPUT}


SELF_QUIET = timedelta(minutes=3)  # Maestro's own tracker events still arrive just after its run ends


def _wake(db: Session, user: User, matches: Callable[[Workflow], bool], why: str,
          own_activity: bool = False) -> list[int]:
    """Start a Maestro run now for each ready project of the user that `matches` (GitHub and Linear wake-ups)."""
    woken: list[int] = []
    workflows = db.scalars(
        select(Workflow).where(Workflow.user_id == user.id, Workflow.description.is_not(None),
                               Workflow.design_status == "ready")
    )
    for wf in workflows:
        if not matches(wf) or missing_connectors(db, user, wf):
            continue
        active = db.scalar(
            select(WorkflowRun.id).where(WorkflowRun.workflow_id == wf.id, WorkflowRun.user_id == user.id,
                                         WorkflowRun.status.in_(ACTIVE_STATUSES)).limit(1)
        )
        if own_activity and (active or db.scalar(
                select(WorkflowRun.id).where(WorkflowRun.workflow_id == wf.id, WorkflowRun.user_id == user.id,
                                             WorkflowRun.finished_at >= utcnow() - SELF_QUIET).limit(1))):
            continue  # Maestro's own work during (or just after) its run: never a reason for another run
        if active:
            # The run going may have read the tracker before this change: start another one when it ends.
            if not wf.wake_pending:
                wf.wake_pending, wf.wake_pending_at = why[:500], utcnow()
                db.commit()
                log.info("Workflow %s: wake-up queued until run %s ends (%s)", wf.id, active, why)
            continue
        recent = db.scalar(
            select(WorkflowRun.id).where(WorkflowRun.workflow_id == wf.id, WorkflowRun.user_id == user.id,
                                         WorkflowRun.started_at >= utcnow() - WAKE_DEBOUNCE).limit(1)
        )
        if recent or (wf.last_woken_at and _aware(wf.last_woken_at) >= utcnow() - WAKE_DEBOUNCE):
            continue  # a run just started: it reads the tracker after this change
        try:
            from app.runs.service import create_workflow_run

            run = create_workflow_run(db, user, wf)
            run.trigger = "wake"
            db.commit()
            wf.last_woken_at = utcnow()
            db.commit()
            woken.append(wf.id)
            log.info("Workflow %s woken: %s", wf.id, why)
        except ManagedAgentsError as exc:
            log.warning("Workflow %s: wake-up failed: %s", wf.id, exc)
    return woken


def wake_for_repo(db: Session, user: User, repo: str) -> list[int]:
    """GitHub: a pull request was merged on this repository."""
    return _wake(db, user, lambda wf: _mentions_repo(wf, repo), f"pull request merged on {repo}")


def _mentions_team(wf: Workflow, teams: set[str]) -> bool:
    """The project works with one of these Linear teams or Jira projects (key, name or id, as a whole word)."""
    values = " ".join(str(c.get("value") or "") for c in (wf.design_json or {}).get("config") or [])
    haystack = " ".join([wf.content or "", wf.description or "", values])
    return any(re.search(r"(?<![A-Za-z0-9])" + re.escape(t) + r"(?![A-Za-z0-9])", haystack, re.IGNORECASE)
               for t in teams if t)


def linear_project_values(wf: Workflow) -> list[str]:
    """The Linear project(s) a Maestro project works on: its settings named like LINEAR_PROJECT."""
    return [str(c.get("value") or "").strip() for c in (wf.design_json or {}).get("config") or []
            if "LINEAR" in str(c.get("key") or "").upper() and "PROJECT" in str(c.get("key") or "").upper()
            and str(c.get("value") or "").strip()]


def _norm(text: str) -> str:
    text = text.strip().lower().rstrip("/")
    if "linear.app/" in text:  # a project URL: keep the segment after /project/ (slug ending with its short id)
        parts = text.split("?")[0].split("#")[0].split("/")
        text = parts[parts.index("project") + 1] if "project" in parts[:-1] else parts[-1]
    return text


def matches_linear_project(values: list[str], hints: set[str]) -> bool:
    """A setting value (name, id or URL) designates the event's Linear project (name, id, URL or short id)."""
    wanted = {_norm(v) for v in values} - {""}
    have = {_norm(h) for h in hints} - {""}
    if wanted & have:
        return True
    # A project URL slug ends with the short id ("tma-portal-3f2a1b9c0d"), which Linear also sends on its own.
    return any(w.endswith("-" + h) or h.endswith("-" + w) for w in wanted for h in have if len(min(w, h, key=len)) >= 6)


def wake_for_linear(db: Session, user: User, project_hints: set[str], why: str,
                    own_activity: bool = False) -> list[int]:
    """Linear: a request was created or changed in a Linear project. Only the Maestro projects whose settings
    name that Linear project are woken: other issues of the team are ignored."""
    return _wake(db, user, lambda wf: matches_linear_project(linear_project_values(wf), project_hints), why,
                 own_activity)


def wake_for_jira(db: Session, user: User, projects: set[str], why: str, own_activity: bool = False) -> list[int]:
    """Jira: a request was created or changed (status, labels, resolution, comment) in one of these projects."""
    return _wake(db, user, lambda wf: _mentions_team(wf, projects), why, own_activity)


MAX_FOLLOW_UPS = 3  # in a row: a safety net in case Maestro's own activity was not recognized


def start_pending_wakes(db: Session) -> list[int]:
    """Start the run a wake-up asked for while another run was going, once that run has ended."""
    started: list[int] = []
    for wf in db.scalars(select(Workflow).where(Workflow.wake_pending.is_not(None))):
        user = db.get(User, wf.user_id)
        if user is None or wf.design_status != "ready":
            wf.wake_pending = wf.wake_pending_at = None
            db.commit()
            continue
        if db.scalar(select(WorkflowRun.id).where(WorkflowRun.workflow_id == wf.id, WorkflowRun.user_id == user.id,
                                                  WorkflowRun.status.in_(ACTIVE_STATUSES)).limit(1)):
            continue  # still going
        last = list(db.scalars(select(WorkflowRun.trigger).where(WorkflowRun.workflow_id == wf.id)
                               .order_by(WorkflowRun.id.desc()).limit(MAX_FOLLOW_UPS)))
        why = wf.wake_pending
        wf.wake_pending = wf.wake_pending_at = None
        db.commit()
        if len(last) == MAX_FOLLOW_UPS and all(t == "follow-up" for t in last):
            log.warning("Workflow %s: %s follow-up runs in a row, not starting another (%s)", wf.id, MAX_FOLLOW_UPS, why)
            continue
        if missing_connectors(db, user, wf):
            continue
        try:
            from app.runs.service import create_workflow_run

            run = create_workflow_run(db, user, wf)
            run.trigger = "follow-up"
            wf.last_woken_at = utcnow()
            db.commit()
            started.append(wf.id)
            log.info("Workflow %s: follow-up run %s for a change during the previous run (%s)", wf.id, run.id, why)
        except ManagedAgentsError as exc:
            log.warning("Workflow %s: follow-up run failed to start: %s", wf.id, exc)
    return started
