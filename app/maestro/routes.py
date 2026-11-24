"""Maestro pages: describe a project, review the generated design, launch."""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db import get_db
from app.deps import csrf_protect, require_configured
from app.maestro import service
from app.maestro.bpmn import MAX_BYTES, BpmnImportError, ImportResult, bpmn_to_design, design_to_bpmn
from app.maestro.diagram import DIAGRAM_ONLY_DESCRIPTION, PDF_MAX_BYTES
from app.maestro.diagram import validate as validate_diagram
from app.maestro.render import render_design_svg, step_info
from app.models import PendingBpmnImport, User, Workflow
from app.security import get_or_create_csrf_token
from app.templating import flash, render, templates
from app.workflows.routes import get_owned_workflow, get_visible_workflow

router = APIRouter()


def _maestro(db: Session, workflow_id: int, user: User, *, view: bool = False) -> Workflow:
    """Owner only, unless `view`: then a workflow shared by another user is fine too (read and run)."""
    wf = (get_visible_workflow if view else get_owned_workflow)(db, workflow_id, user)
    if not wf.description:
        raise HTTPException(status_code=404)
    return wf


def _panel_ctx(request: Request, wf: Workflow, db: Session | None = None, user: User | None = None) -> dict[str, Any]:
    design = wf.design_json or {}
    progress = (service.progress_view(wf, service.design_estimates(db))
                if db is not None and wf.design_status in {"queued", "generating"} else None)
    owner = db.get(User, wf.user_id) if db is not None else None
    return {
        "is_owner": user is None or user.id == wf.user_id,
        "owner_login": owner.login if owner else "",
        "progress": progress,
        "request": request,
        "csrf_token": get_or_create_csrf_token(request.session),
        "wf": wf,
        "design": design,
        "svg": render_design_svg(design) if wf.design_status == "ready" and design else None,
        "counts": {a: sum(1 for s in design.get("steps", []) if s.get("actor") == a)
                   for a in ("maestro", "dispatch", "human", "upsun", "external")},
    }


@router.get("/")
@router.get("/maestro/new")
def maestro_new(request: Request, start: str = "describe", user: User = Depends(require_configured)) -> Response:
    return render(request, "maestro_new.html", {"name": "", "description": "", "shared": False,
                                                "start": "bpmn" if start == "bpmn" else "describe"})


@router.post("/maestro/new", dependencies=[Depends(csrf_protect)])
async def maestro_create(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    shared: str = Form(""),
    start: str = Form("describe"),
    diagram: UploadFile | None = File(None),
    bpmn: UploadFile | None = File(None),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    name, description = name.strip(), description.strip()
    if start == "bpmn":
        return await _create_from_bpmn(request, name, description, bool(shared), bpmn, user, db)
    error = None
    attached: tuple[str, bytes] | None = None
    if diagram is not None and diagram.filename:
        data = await diagram.read(PDF_MAX_BYTES + 1)
        media_type, error = validate_diagram(data)
        if media_type:
            attached = (media_type, data)
    if error:
        pass  # invalid diagram: report it first
    elif not name or len(name) > 128:
        error = "The name must be between 1 and 128 characters."
    elif not attached and len(description) < 40:
        error = "Describe the project in more detail (stages, who validates, tools), or add a diagram."
    if not error:
        try:
            wf = service.create_maestro_workflow(db, user, name, description or DIAGRAM_ONLY_DESCRIPTION, attached,
                                                 shared=bool(shared))
            return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
        except IntegrityError:
            db.rollback()
            error = f"You already have a project named {name}."
    if attached and error:
        error += " Attach the diagram again."
    return render(request, "maestro_new.html", {"name": name, "description": description, "error": error,
                                                "shared": bool(shared), "start": "describe"},
                  status_code=400)


async def _create_from_bpmn(
    request: Request, name: str, context: str, shared: bool, bpmn: UploadFile | None, user: User, db: Session
) -> Response:
    """Create page, "A BPMN file" mode: the typed name and the required context go with the imported process."""
    error = None
    result: ImportResult | None = None
    xml = b""
    if not name or len(name) > 128:
        error = "The name must be between 1 and 128 characters."
    elif len(context) < 40:
        error = "Add some context (at least a few sentences): why this project, who is involved, which tools."
    else:
        try:
            xml = await _read_bpmn_bytes(bpmn)
            result = bpmn_to_design(xml)
        except BpmnImportError as exc:
            error = str(exc)
    if result is not None:
        if service.find_matching_project(db, user, result, name):  # the user chooses: update it or create a new one
            pending_id = service.save_pending_import(db, user, xml, name, context, shared)
            return RedirectResponse(f"/maestro/import-match/{pending_id}", status_code=303)
        try:
            wf = service.create_imported_workflow(db, user, result, shared=shared, name=name, context=context)
        except IntegrityError:
            db.rollback()
            error = f"You already have a project named {name}."
        else:
            flash(request, "BPMN file imported." + _design_state(wf), "info")
            return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    return render(request, "maestro_new.html", {"name": name, "description": context, "shared": shared,
                                                "start": "bpmn", "error": f"{error} Choose the BPMN file again."},
                  status_code=400)


async def _read_bpmn_bytes(upload: UploadFile | None) -> bytes:
    """Read an uploaded .bpmn: one byte more than the 2 MB limit, so the parser can refuse a larger file."""
    if upload is None or not upload.filename:
        raise BpmnImportError("Choose a BPMN file (.bpmn).")
    return await upload.read(MAX_BYTES + 1)


async def _read_bpmn(upload: UploadFile | None) -> ImportResult:
    """Read an uploaded .bpmn (at most 2 MB) and parse it. Raises BpmnImportError for an invalid file."""
    return bpmn_to_design(await _read_bpmn_bytes(upload))


def _design_state(wf: Workflow) -> str:
    """The end of the flash after an import: the design came back as it was, or Maestro reviews the file."""
    return " Your design is restored." if wf.design_status == "ready" else " Maestro is reviewing it."


def _in_import_review(wf: Workflow) -> bool:
    return wf.design_status == "needs_answers" and (wf.clarifications or {}).get("kind") == "import_review"


@router.get("/maestro/import-bpmn")
def maestro_import_bpmn_page(user: User = Depends(require_configured)) -> Response:
    """The separate import page is gone: BPMN import is a mode of the Create page."""
    return RedirectResponse("/maestro/new?start=bpmn", status_code=303)


@router.post("/maestro/import-bpmn", dependencies=[Depends(csrf_protect)])
def maestro_import_bpmn(request: Request, user: User = Depends(require_configured)) -> Response:
    flash(request, "Import a BPMN file from the Create page.", "info")
    return RedirectResponse("/maestro/new?start=bpmn", status_code=303)


_BUSY = {"queued", "generating"}
_BUSY_MESSAGE = "Wait until the current design is finished."


@router.get("/workflows/{workflow_id}/import-bpmn")
def update_bpmn_page(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    """Update the project from a BPMN file (owner only)."""
    wf = _maestro(db, workflow_id, user)
    if _in_import_review(wf):  # the review dialog has its own "Import a corrected file" form
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    if wf.design_status in _BUSY:
        flash(request, _BUSY_MESSAGE, "info")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    return render(request, "maestro_update_bpmn.html", {"wf": wf})


@router.post("/workflows/{workflow_id}/import-bpmn", dependencies=[Depends(csrf_protect)])
async def design_reimport_bpmn(
    request: Request,
    workflow_id: int,
    bpmn: UploadFile | None = File(None),
    context: str = Form(""),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if _in_import_review(wf):  # "Import a corrected file" in the review dialog
        try:
            result = await _read_bpmn(bpmn)
        except BpmnImportError as exc:  # the workflow and its dialog stay as they were
            flash(request, str(exc), "error")
            return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
        service.reimport_workflow(db, wf, result)
        flash(request, "Corrected file imported." + (" Your design is restored." if wf.design_status == "ready"
                                                    else " Maestro is reviewing it again."), "info")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    if wf.design_status in _BUSY:
        flash(request, _BUSY_MESSAGE, "info")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    try:
        result = await _read_bpmn(bpmn)
    except BpmnImportError as exc:  # the project stays as it was
        flash(request, str(exc), "error")
        return RedirectResponse(f"/workflows/{wf.id}/import-bpmn", status_code=303)
    service.update_from_bpmn(db, wf, result, context.strip())
    flash(request, "Project updated from the BPMN file." + _design_state(wf), "info")
    return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)


# --- Create page: the file looks like one of the user's projects ---------------------------------

_REVIEW_MESSAGE = "It has an import review in progress: finish or cancel it first."
_PROJECT_BUSY_MESSAGE = "Wait until its current design is finished."
_MATCH_REASONS = {"exported": "It was exported from", "name": "It has the same name as",
                  "steps": "It has the same steps as"}


def _update_blocker(wf: Workflow) -> str | None:
    """Why the project cannot be updated now (the same limits as Import BPMN on its page), else None."""
    if wf.design_status in _BUSY:
        return _PROJECT_BUSY_MESSAGE
    if _in_import_review(wf):
        return _REVIEW_MESSAGE
    return None


def _pending(db: Session, user: User, pending_id: str) -> PendingBpmnImport:
    row = service.load_pending_import(db, user, pending_id)
    if row is None:
        raise HTTPException(status_code=404)
    return row


def _match_page(
    request: Request, db: Session, user: User, pending: PendingBpmnImport, result: ImportResult, *,
    error: str | None = None, new_name: str | None = None, choice: str | None = None, status: int = 200,
) -> Response:
    """The choice page. The match is recomputed each time: the project may have changed since the upload."""
    match = service.find_matching_project(db, user, result, pending.name)
    blocker = _update_blocker(match.workflow) if match else None
    can_update = match is not None and blocker is None
    if choice == "update" and not can_update:
        choice = None
    preselect = choice if choice in ("update", "create") else (
        "update" if can_update and match is not None and match.reason == "exported" else "create")
    proposed = new_name if new_name is not None else service.proposed_name(db, user, pending.name or result.name)
    return render(request, "maestro_bpmn_match.html", {
        "pending": pending, "match": match, "reason_text": _MATCH_REASONS.get(match.reason, "") if match else "",
        "update_blocker": blocker, "preselect": preselect, "proposed_name": proposed, "error": error,
    }, status_code=status)


def _pending_result(request: Request, db: Session, pending: PendingBpmnImport) -> ImportResult | None:
    """Parse the stored file again, with the same parser and limits as the upload. None (row dropped) if it fails."""
    try:
        return bpmn_to_design(pending.xml)
    except BpmnImportError as exc:  # should not happen: the file was parsed when it was uploaded
        service.drop_pending_import(db, pending)
        flash(request, str(exc), "error")
        return None


@router.get("/maestro/import-match/{pending_id}")
def import_match_page(
    request: Request, pending_id: str, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    pending = _pending(db, user, pending_id)
    result = _pending_result(request, db, pending)
    if result is None:
        return RedirectResponse("/maestro/new?start=bpmn", status_code=303)
    return _match_page(request, db, user, pending, result)


@router.post("/maestro/import-match/{pending_id}", dependencies=[Depends(csrf_protect)])
def import_match_choose(
    request: Request,
    pending_id: str,
    choice: str = Form(""),
    new_name: str = Form(""),
    match_id: int | None = Form(None),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    pending = _pending(db, user, pending_id)
    result = _pending_result(request, db, pending)
    if result is None:
        return RedirectResponse("/maestro/new?start=bpmn", status_code=303)
    if choice == "update":
        match = service.find_matching_project(db, user, result, pending.name)
        if match is None:
            return _match_page(request, db, user, pending, result, choice="create", status=400,
                               error="The project to update is no longer there. "
                                     "You can import the file as a new project.")
        if match_id != match.workflow.id:  # the user confirmed another project than the one matched now
            return _match_page(request, db, user, pending, result, choice="create", status=400,
                               error="The project to update has changed since this page was shown. "
                                     "Check the project below and choose again.")
        wf = _maestro(db, match.workflow.id, user)  # the target is the matched project, owner only
        if _update_blocker(wf):
            return _match_page(request, db, user, pending, result, choice="update", status=400)
        service.update_from_bpmn(db, wf, result, pending.context)
        service.drop_pending_import(db, pending)
        flash(request, "Project updated from the BPMN file." + _design_state(wf), "info")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    elif choice == "create":
        new_name = new_name.strip()
        if not new_name or len(new_name) > 128:
            return _match_page(request, db, user, pending, result, choice="create", new_name=new_name,
                               error="The name must be between 1 and 128 characters.", status=400)
        try:
            wf = service.create_imported_workflow(db, user, result, shared=pending.shared, name=new_name,
                                                  context=pending.context)
        except IntegrityError:
            db.rollback()
            return _match_page(request, db, user, pending, result, choice="create", new_name=new_name,
                               error=f"You already have a project named {new_name}.", status=400)
        service.drop_pending_import(db, pending)
        flash(request, "BPMN file imported." + _design_state(wf), "info")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    return _match_page(request, db, user, pending, result, error="Choose an option.", status=400)


@router.post("/maestro/import-match/{pending_id}/cancel", dependencies=[Depends(csrf_protect)])
def import_match_cancel(
    pending_id: str, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    service.drop_pending_import(db, _pending(db, user, pending_id))
    return RedirectResponse("/maestro/new?start=bpmn", status_code=303)


@router.get("/workflows/{workflow_id}/import/cancel")
def import_cancel_page(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if not _in_import_review(wf):
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    return render(request, "maestro_import_cancel.html", {"wf": wf})


@router.post("/workflows/{workflow_id}/import/cancel", dependencies=[Depends(csrf_protect)])
def import_cancel(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if not _in_import_review(wf):
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    db.delete(wf)
    db.commit()
    flash(request, "Import cancelled.", "success")
    return RedirectResponse("/workflows", status_code=303)


@router.get("/workflows/{workflow_id}/diagram")
def workflow_diagram(
    workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user, view=True)
    if not wf.diagram_data or not wf.diagram_media_type:
        raise HTTPException(status_code=404)
    return Response(wf.diagram_data, media_type=wf.diagram_media_type,
                    headers={"Cache-Control": "private, max-age=3600", "X-Content-Type-Options": "nosniff"})


@router.get("/workflows/{workflow_id}/design")
def design_page(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user, view=True)
    ctx = _panel_ctx(request, wf, db, user)
    ctx.pop("request"), ctx.pop("csrf_token")
    ctx["missing"] = service.missing_connectors(db, user, wf) if wf.design_status == "ready" else []
    ctx["steps_info"] = [step_info(st) for st in (wf.design_json or {}).get("steps") or []] if ctx["svg"] else []
    ctx["instructions_changed"] = service.instructions_changed(wf, service.current_instructions_hash(db))
    return render(request, "maestro_design.html", ctx)


@router.get("/workflows/{workflow_id}/design/panel")
def design_panel(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user, view=True)
    if wf.design_status == "ready":
        # Generation finished: reload the full page.
        return Response(status_code=204, headers={"HX-Redirect": f"/workflows/{wf.id}/design"})
    return HTMLResponse(templates.get_template("partials/maestro_status.html").render(_panel_ctx(request, wf, db, user)))


@router.post("/workflows/{workflow_id}/design/revise", dependencies=[Depends(csrf_protect)])
def design_revise(
    request: Request,
    workflow_id: int,
    feedback: str = Form(""),
    description: str = Form(""),
    user: User = Depends(require_configured),
    db: Session = Depends(get_db),
) -> Response:
    wf = _maestro(db, workflow_id, user)
    description = description.strip()
    if description and len(description) < 40 and not wf.diagram_data:
        flash(request, "Describe the project in more detail (stages, who validates, tools), or add a diagram.", "error")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    if description:
        wf.description = description
    service.request_design(db, wf, feedback)
    flash(request, "Maestro is regenerating the design.", "info")
    return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)


@router.post("/workflows/{workflow_id}/design/answers", dependencies=[Depends(csrf_protect)])
async def design_answers(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    if wf.design_status != "needs_answers":
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    form = await request.form()
    if (wf.clarifications or {}).get("kind") == "import_review":
        choices = {q["id"]: (str(form.get(f"q:{q['id']}") or ""), str(form.get(f"other:{q['id']}") or ""))
                   for q in wf.clarifications.get("questions") or []}
        if service.answer_import_review(db, wf, choices):
            flash(request, "Choose how to handle every part (pick an option, or choose Other and write your own).",
                  "error")
        else:
            flash(request, "Thanks. Maestro is designing the project with your choices.", "info")
        return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)
    answers: dict[str, str] = {}
    for q in (wf.clarifications or {}).get("questions") or []:
        choice = str(form.get(f"q:{q['id']}") or "")
        answers[q["id"]] = str(form.get(f"other:{q['id']}") or "").strip() if choice == "__other__" else choice
    missing = service.answer_questions(db, wf, answers)
    if missing:
        flash(request, "Answer every question (pick an option, or choose Other and write your answer).", "error")
    else:
        flash(request, "Thanks. Maestro is designing the project with your decisions.", "info")
    return RedirectResponse(f"/workflows/{wf.id}/design", status_code=303)


@router.post("/workflows/{workflow_id}/design/config", dependencies=[Depends(csrf_protect)])
async def design_config(
    request: Request, workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)
) -> Response:
    wf = _maestro(db, workflow_id, user)
    form = await request.form()
    values = {k[4:]: str(v) for k, v in form.items() if k.startswith("cfg:")}
    service.save_config(db, wf, values)
    flash(request, "Config saved.", "success")
    return RedirectResponse(f"/workflows/{wf.id}/design#config", status_code=303)


def _download_name(wf: Workflow) -> str:
    """File name (without extension) of the downloads of a design: maestro-<name as a slug>."""
    return "maestro-" + (re.sub(r"[^a-z0-9]+", "-", wf.name.lower()).strip("-") or "workflow")


@router.get("/workflows/{workflow_id}/design.svg")
def design_svg(workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)) -> Response:
    wf = _maestro(db, workflow_id, user, view=True)
    if wf.design_status != "ready" or not wf.design_json:
        raise HTTPException(status_code=404)
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>\n' + str(render_design_svg(wf.design_json)),
        media_type="image/svg+xml",
        headers={"Content-Disposition": f'attachment; filename="{_download_name(wf)}.svg"'},
    )


@router.get("/workflows/{workflow_id}/design.bpmn")
def design_bpmn(workflow_id: int, user: User = Depends(require_configured), db: Session = Depends(get_db)) -> Response:
    wf = _maestro(db, workflow_id, user, view=True)
    if wf.design_status != "ready" or not wf.design_json:
        raise HTTPException(status_code=404)
    return Response(
        design_to_bpmn(wf, wf.design_json),
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{_download_name(wf)}.bpmn"'},
    )
